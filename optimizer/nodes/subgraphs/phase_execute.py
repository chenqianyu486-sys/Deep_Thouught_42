"""EXECUTE phase: run the chosen optimization strategy.

Full execution tools are exposed. Auto chain actions and post-eval
hooks are applied after critical tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from pathlib import Path

from optimizer.state import OptimizerState, PhaseEntry, ToolCallRecord, LLMCallRecord
from optimizer.deps import NodeDeps
from optimizer.edges import NodeName
from optimizer.pure.tool_filter import LoopPhase, PHASE_MAX_ROUNDS, filter_tools_for_phase
from optimizer.pure.tool_summary import summarize_tool_result
from optimizer.pure.tool_router import call_tool as call_tool_fn
from optimizer.pure.step_state import extract_step_state
from optimizer.pure.timing import parse_timing_summary, is_valid_wns
from optimizer.pure.constants import WNS_TARGET_THRESHOLD, DASHBOARD_REFRESH_MAP, SKILL_CHAIN_ACTIONS
from optimizer.pure.critical_path import parse_critical_path_cells, update_critical_paths
from optimizer.nodes.subgraphs.phase_handoff import build_phase_handoff, transition_phase
from optimizer.pure.context_snapshot import inject_merged_dashboard
from optimizer.color import green, yellow

logger = logging.getLogger(__name__)

# Tools that trigger mandatory WNS evaluation
POST_EVAL_TOOLS = frozenset({
    "vivado_route_design",
    "rapidwright_execute_fanout_strategy",
})


async def run_execute_phase(state: OptimizerState, deps: NodeDeps) -> LoopPhase:
    """Run the EXECUTE phase: execute the chosen strategy via tool calls.

    Returns:
        LoopPhase.EVALUATE (always, even on failure).
    """
    max_rounds = PHASE_MAX_ROUNDS.get(LoopPhase.EXECUTE, 30)
    tool_round = 0
    tools_called: list[str] = []
    wns_before = state.timing.latest_wns
    llm_summary = ""
    reached_callback = False  # track if the strategy reached callback indicating completion

    # Record phase entry
    phase_entry = PhaseEntry(
        phase="EXECUTE_STRATEGY",
        strategy=state.strategy.current_strategy,
        iteration=state.iteration.current,
        tool_round=0,
        wns_at_entry=state.timing.latest_wns,
    )
    state.strategy.phase_history.append(phase_entry)
    if len(state.strategy.phase_history) > 100:
        state.strategy.phase_history = state.strategy.phase_history[-100:]
    state.strategy.current_phase = "EXECUTE_STRATEGY"

    while True:
        tool_round += 1
        state.iteration.tool_round = tool_round

        if _check_phase_exit(state, tool_round, max_rounds):
            break

        # Call LLM with execution tools
        phase_tools = filter_tools_for_phase(deps.tools, LoopPhase.EXECUTE)
        response = await _call_phase_llm(state, deps, phase_tools)
        if response is None:
            break

        message = response.choices[0].message
        assistant_content = message.content or ""
        state.context.latest_assistant_response = assistant_content[:2000]
        state.context.llm_call_history.append(LLMCallRecord(
            timestamp=time.time(),
            phase=state.strategy.current_phase,
            model=state.model.current_model,
            iteration=state.iteration.current,
            user_prompt=state.context.latest_user_prompt,
            assistant_response=assistant_content[:2000],
        ))
        if len(state.context.llm_call_history) > state.context.llm_call_history_max:
            state.context.llm_call_history = state.context.llm_call_history[-state.context.llm_call_history_max:]

        if deps.compat is not None:
            metadata = {"tool_calls": message.tool_calls} if message.tool_calls else None
            deps.compat.add_message("assistant", assistant_content, metadata)

        # Track cost
        _track_cost(state, response)

        # Extract step state
        step_state = extract_step_state(message)
        state.control.step_state = step_state

        if step_state:
            state.context.step_state_misses = 0
            flow_signal = step_state.flow_control

            # EXEC_DONE -> move to evaluate
            if flow_signal == "EXEC_DONE":
                llm_summary = assistant_content
                logger.info(green(f"[EXECUTE] LLM signaled EXEC_DONE at round {tool_round}"))
                break

            # EXHAUSTED during execution
            if flow_signal == "EXHAUSTED":
                logger.info("[EXECUTE] LLM signaled EXHAUSTED")
                break

        else:
            state.context.step_state_misses += 1
            if deps.compat is not None:
                deps.compat.add_message("user", "[NOTE] report_step_state missing. Include it next turn.")

        # Execute tool calls
        if message.tool_calls:
            for tc in message.tool_calls:
                if not tc.function:
                    continue

                tool_name = tc.function.name
                state.iteration.tools_used.append(tool_name)
                tools_called.append(tool_name)

                try:
                    tool_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    tool_args = {}

                tool_start = time.time()
                logger.info(f"[EXECUTE] Calling {tool_name}")
                result = await call_tool_fn(
                    tool_name=tool_name, arguments=tool_args,
                    rapidwright_session=deps.rapidwright_session,
                    vivado_session=deps.vivado_session,
                    raw_tool_outputs=state.context.raw_tool_outputs,
                    iteration=state.iteration.current,
                    tool_round=tool_round,
                    high_fanout_nets=state.timing.high_fanout_nets,
                )
                tool_elapsed = time.time() - tool_start
                logger.info(f"[EXECUTE] {tool_name} completed in {tool_elapsed:.1f}s")

                summary = summarize_tool_result(
                    tool_name, result,
                    latest_wns=state.timing.latest_wns,
                    latest_tns=state.timing.latest_tns,
                    latest_failing_endpoints=state.timing.latest_failing_endpoints,
                    prev_best_wns=state.timing.prev_best_wns,
                )

                # Store raw output
                state.context.raw_tool_outputs[(state.iteration.current, tool_round)] = (tool_name, result)
                if len(state.context.raw_tool_outputs) > state.context.raw_tool_output_max:
                    oldest_key = min(state.context.raw_tool_outputs.keys(), key=lambda k: (k[0], k[1]))
                    del state.context.raw_tool_outputs[oldest_key]

                # Record tool call trace
                trace_record = ToolCallRecord(
                    tool_name=tool_name,
                    tool_call_id=tc.id,
                    arguments=tool_args,
                    summary=summary[:500],
                    result_chars=len(result) if isinstance(result, str) else 0,
                    elapsed_seconds=tool_elapsed,
                    iteration=state.iteration.current,
                    tool_round=tool_round,
                    status="completed",
                )
                state.context.tool_call_trace.append(trace_record)
                if len(state.context.tool_call_trace) > state.context.tool_call_trace_max:
                    state.context.tool_call_trace = state.context.tool_call_trace[-state.context.tool_call_trace_max:]

                if deps.compat is not None:
                    deps.compat.add_message("tool", summary, {
                        "tool_call_id": tc.id, "name": tool_name,
                    })

                # Track WNS from timing results
                _track_wns_from_result(state, tool_name, result)
                await _try_save_best_checkpoint(state, deps)

                # Track critical path data
                if tool_name == "vivado_extract_critical_path_cells":
                    cell_paths = parse_critical_path_cells(result)
                    if cell_paths:
                        update_critical_paths(state, cell_paths, iteration=state.iteration.current)

                # Mark critical paths stale after layout changes
                if tool_name in ("vivado_phys_opt_design", "vivado_route_design",
                                 "vivado_place_design", "vivado_create_and_apply_pblock"):
                    state.timing.critical_paths_stale = True

                # Dashboard freshness
                refreshable = DASHBOARD_REFRESH_MAP.get(tool_name)
                if refreshable:
                    state.timing.refreshed_fields |= refreshable

                # Post-eval hook for critical tools
                if tool_name in POST_EVAL_TOOLS:
                    try:
                        await _post_eval_hook(state, deps, tool_name)
                    except Exception as e:
                        logger.warning(f"[EXECUTE] Post-eval hook failed for {tool_name}: {e}")
                await _try_save_best_checkpoint(state, deps)

                # Chain actions for skills
                if tool_name in SKILL_CHAIN_ACTIONS:
                    try:
                        skill_data = json.loads(result) if result else {}
                        await _execute_chain_actions(state, deps, tool_name, skill_data, tools_called)
                        reached_callback = True
                    except Exception as e:
                        logger.warning(f"[EXECUTE] Chain actions failed for {tool_name}: {e}")

                # Track tool errors
                result_lower = summary.lower() if summary else ""
                if "error" in result_lower and "success" not in result_lower:
                    state.iteration.tool_errors.append({
                        "tool": tool_name,
                        "result": summary[:2000],
                    })

            # Auto-refresh critical paths if stale
            if state.timing.critical_paths_stale:
                try:
                    await _auto_refresh_critical_paths(state, deps)
                except Exception as e:
                    logger.warning(f"[EXECUTE] Critical path auto-refresh failed: {e}")

            continue

        # No tool calls
        if _check_wns_target_met(state):
            break

    # Phase exit: build handoff
    llm_summary = llm_summary or f"Execution of {state.strategy.current_strategy} completed."
    wns_after = state.timing.latest_wns

    handoff = build_phase_handoff(
        source_phase=LoopPhase.EXECUTE,
        llm_summary=llm_summary,
        wns=wns_after,
        tns=state.timing.latest_tns,
        failing_endpoints=state.timing.latest_failing_endpoints,
        tools_called=tools_called,
        key_findings={
            "strategy_name": state.strategy.current_strategy,
            "wns_before": wns_before,
            "wns_after": wns_after,
        },
        message_count=tool_round,
    )
    state.strategy.last_handoff_text = handoff.to_phase_context_string()
    await transition_phase(deps, LoopPhase.EXECUTE, LoopPhase.EVALUATE, handoff)
    return LoopPhase.EVALUATE


def _check_phase_exit(state: OptimizerState, tool_round: int, max_rounds: int) -> bool:
    if tool_round > max_rounds:
        logger.warning(f"[EXECUTE] Max rounds reached ({tool_round} > {max_rounds})")
        return True
    if state.control.start_time:
        elapsed = time.time() - state.control.start_time
        if elapsed > state.control.wall_clock_timeout:
            state.control.is_done = True
            state.control.done_reason = "wall_clock_timeout"
            return True
    if state.control.user_exit_requested:
        return True
    if state.cost.total_cost >= state.cost.cost_hard_limit:
        state.control.is_done = True
        state.control.done_reason = "cost_limit"
        return True
    return False


async def _call_phase_llm(state, deps, phase_tools, max_retries=3, retry_delay=2.0):
    """Call LLM with execution tools and retry logic."""
    if deps.openai_client is None or deps.compat is None:
        return None
    try:
        api_messages = deps.compat.get_formatted_for_api()
    except Exception:
        return None

    # Inject merged handoff + dashboard as last user message
    inject_merged_dashboard(api_messages, state, LoopPhase.EXECUTE)

    model = state.model.current_model
    state.model.llm_call_count += 1

    reasoning_cfg = None
    if deps.reasoning_config:
        if model == state.model.planner_model:
            reasoning_cfg = deps.reasoning_config.get("planner")
        elif model == state.model.worker_model:
            reasoning_cfg = deps.reasoning_config.get("worker")

    extra_body = None
    if reasoning_cfg and reasoning_cfg.get("enabled"):
        reasoning_payload = {"enabled": True}
        max_output = reasoning_cfg.get("max_output_tokens")
        if max_output is not None:
            reasoning_payload["max_output_tokens"] = max_output
        extra_body = {"reasoning": reasoning_payload}

    last_exception = None
    for retry in range(max_retries):
        try:
            kwargs = dict(
                model=model,
                messages=api_messages,
                tools=phase_tools if phase_tools else None,
                timeout=600.0,
            )
            if extra_body:
                kwargs["extra_body"] = extra_body
            # Log prompt for observability
            if deps.prompt_logger:
                deps.prompt_logger.log_prompt(
                    model=model,
                    messages=api_messages,
                    iteration=state.iteration.current,
                    job_id=state.control.run_dir.name if state.control.run_dir else ""
                )
            state.context.latest_user_prompt = (
                api_messages[-1].get("content", "") if api_messages else ""
            )[:2000]
            return await deps.openai_client.chat.completions.create(**kwargs)
        except Exception as e:
            last_exception = e
            if "429" in str(e):
                fallback = _get_fallback_model(state, model)
                if fallback and fallback != model:
                    state.model.current_model = fallback
                    model = fallback
                    await asyncio.sleep(retry_delay * (2 ** retry))
                    continue
            if retry < max_retries - 1:
                await asyncio.sleep(retry_delay * (2 ** retry))

    if last_exception:
        raise last_exception
    return None


def _track_wns_from_result(state: OptimizerState, tool_name: str, raw_result: str) -> None:
    if tool_name not in ("vivado_report_timing_summary", "vivado_phys_opt_design",
                         "vivado_route_design", "vivado_get_wns"):
        return
    timing = parse_timing_summary(raw_result)
    wns = timing.get("wns")
    tns = timing.get("tns")
    fe = timing.get("failing_endpoints")
    if wns is not None:
        state.timing.latest_wns = wns
        if tns is not None:
            state.timing.latest_tns = tns
        if fe is not None:
            state.timing.latest_failing_endpoints = fe
        if wns > state.timing.best_wns:
            state.timing.best_wns = wns
            state.timing.best_wns_iteration = state.iteration.current
            state.timing.best_wns_tns = tns
            state.timing.best_wns_failing_endpoints = fe
            state.control.needs_save = True


async def _save_best_checkpoint(state: OptimizerState, deps: NodeDeps) -> None:
    """Save a DCP checkpoint when best_wns improves, for rollback support.

    Writes to {run_dir}/best_checkpoint.dcp, overwriting previous best.
    Does nothing if no run_dir is configured or if vivado_session is unavailable.
    """
    if state.control.run_dir is None:
        return
    try:
        ckpt_path = state.control.run_dir / "best_checkpoint.dcp"
        await call_tool_fn(
            "vivado_write_checkpoint",
            {"dcp_path": str(ckpt_path.resolve()), "force": True},
            deps.rapidwright_session, deps.vivado_session,
        )
        state.control.best_checkpoint_path = ckpt_path
        logger.info(f"Saved best checkpoint: WNS={state.timing.best_wns:.3f}ns")
    except Exception as e:
        logger.warning(f"Failed to save best checkpoint: {e}")


async def _try_save_best_checkpoint(state: OptimizerState, deps: NodeDeps) -> None:
    """Save best checkpoint when needs_save flag is set, then clear it."""
    if state.control.needs_save and deps.vivado_session is not None:
        await _save_best_checkpoint(state, deps)
        state.control.needs_save = False


def _track_cost(state: OptimizerState, response) -> None:
    try:
        usage = getattr(response, 'usage', None)
        if usage:
            prompt = getattr(usage, 'prompt_tokens', 0) or 0
            completion = getattr(usage, 'completion_tokens', 0) or 0
            raw_total = getattr(usage, 'total_tokens', 0) or 0
            total = raw_total if raw_total > 0 else (prompt + completion)
            cost = float(getattr(usage, 'cost', 0.0) or 0.0)
            state.cost.total_prompt_tokens += prompt
            state.cost.total_completion_tokens += completion
            state.cost.total_tokens += total
            state.cost.total_cost += cost
            completion_details = getattr(usage, 'completion_tokens_details', None)
            if completion_details:
                reasoning = getattr(completion_details, 'reasoning_tokens', 0) or 0
                state.cost.total_reasoning_tokens += reasoning
    except Exception as e:
        logger.debug(f"[EXECUTE] Cost tracking failed: {e}")


def _check_wns_target_met(state: OptimizerState) -> bool:
    return (
        state.timing.latest_wns is not None
        and state.timing.latest_wns >= WNS_TARGET_THRESHOLD
        and is_valid_wns(state.timing.latest_wns, state.timing.clock_period, state.timing.best_wns)
    )


def _get_fallback_model(state: OptimizerState, current_model: str) -> str | None:
    if current_model == state.model.worker_model:
        fallbacks = state.model.worker_fallback_models
        idx = state.model.worker_fallback_index
        if idx < len(fallbacks):
            state.model.worker_fallback_index = idx + 1
            return fallbacks[idx]
    if current_model == state.model.planner_model:
        fallbacks = state.model.planner_fallback_models
        idx = state.model.planner_fallback_index
        if idx < len(fallbacks):
            state.model.planner_fallback_index = idx + 1
            return fallbacks[idx]
    return None


async def _post_eval_hook(state: OptimizerState, deps: NodeDeps, tool_name: str) -> None:
    """Force WNS evaluation after critical tools."""
    prev_wns = state.timing.latest_wns
    timing_result = await call_tool_fn(
        "vivado_report_timing_summary", {},
        deps.rapidwright_session, deps.vivado_session,
    )
    timing = parse_timing_summary(timing_result)
    wns = timing.get("wns")
    tns = timing.get("tns")
    fe = timing.get("failing_endpoints")
    if wns is None:
        return

    state.timing.latest_wns = wns
    if tns is not None:
        state.timing.latest_tns = tns
    if fe is not None:
        state.timing.latest_failing_endpoints = fe
    if wns > state.timing.best_wns:
        state.timing.best_wns = wns
        state.timing.best_wns_iteration = state.iteration.current
        state.timing.best_wns_tns = tns
        state.timing.best_wns_failing_endpoints = fe
        state.control.needs_save = True

    delta = wns - prev_wns if prev_wns is not None else 0.0
    verdict = "IMPROVED" if delta > 0.001 else ("UNCHANGED" if abs(delta) <= 0.001 else "REGRESSED")
    eval_notice = (
        f"[EVAL] After {tool_name}: WNS={wns:.3f}ns "
        f"(delta={delta:+.3f}ns vs previous). {verdict}."
    )
    if tns is not None:
        eval_notice += f" TNS={tns:.3f}ns"
    if deps.compat is not None:
        deps.compat.add_message("user", eval_notice)
    logger.info(f"[EXECUTE] Post-eval: {tool_name} -> WNS={wns:.3f}ns (delta={delta:+.3f}, {verdict})")


async def _execute_chain_actions(state, deps, tool_name, skill_result_data, tools_called):
    """Auto-execute chained MCP tools after a skill completes.

    Workflow:
      1. Save pre-chain checkpoint to /tmp/pre_chain_pblock.dcp for rollback
      2. Iterate through SKILL_CHAIN_ACTIONS[tool_name] steps:
         - Resolve args_from_skill from skill_result_data
         - Call each MCP tool via call_tool_fn
         - Track WNS changes via _track_wns_from_result
         - Mark critical_paths_stale = True for placement-affecting tools
      3. On any step failure:
         - Restore from pre-chain checkpoint
         - Break (do not continue with remaining steps)
         - Inject [AUTO-CHAIN ERROR] notification into LLM context
    """
    chain = SKILL_CHAIN_ACTIONS.get(tool_name)
    if not chain:
        return

    # Save pre-chain state for rollback on failure
    pre_chain_path = None
    try:
        pre_ckpt_result = await call_tool_fn(
            "vivado_write_checkpoint", {"dcp_path": "/tmp/pre_chain_pblock.dcp", "force": True},
            deps.rapidwright_session, deps.vivado_session,
        )
        pre_chain_path = "/tmp/pre_chain_pblock.dcp"
    except Exception as e:
        logger.warning(f"[chain] Could not save pre-chain checkpoint: {e}")

    for step in chain:
        target_tool = step["tool"]
        args = dict(step.get("args", {}))
        for key, skill_key in step.get("args_from_skill", {}).items():
            if isinstance(skill_key, str) and skill_key in skill_result_data:
                args[key] = skill_result_data[skill_key]
            elif isinstance(skill_key, bool):
                args[key] = skill_key

        try:
            logger.info(f"[chain] Auto-executing {target_tool} after {tool_name}")
            raw_result = await call_tool_fn(
                target_tool, args,
                deps.rapidwright_session, deps.vivado_session,
            )
            summary = summarize_tool_result(
                target_tool, raw_result,
                latest_wns=state.timing.latest_wns,
                latest_tns=state.timing.latest_tns,
                latest_failing_endpoints=state.timing.latest_failing_endpoints,
                prev_best_wns=state.timing.prev_best_wns,
            )
            if deps.compat is not None:
                deps.compat.add_message("user",
                    f"[AUTO-CHAIN] After {tool_name}: {target_tool} completed — {summary[:400]}")
            _track_wns_from_result(state, target_tool, raw_result)

            # Mark critical paths stale after placement-affecting chain tools
            if target_tool in ("vivado_place_design", "vivado_create_and_apply_pblock"):
                state.timing.critical_paths_stale = True

            await _try_save_best_checkpoint(state, deps)
            state.iteration.tools_used.append(target_tool)
            tools_called.append(target_tool)
        except Exception as e:
            logger.error(f"[chain] Tool {target_tool} failed: {e}")
            if pre_chain_path:
                try:
                    logger.warning(f"[chain] Restoring from pre-chain checkpoint: {pre_chain_path}")
                    await call_tool_fn(
                        "vivado_open_checkpoint", {"dcp_path": pre_chain_path},
                        deps.rapidwright_session, deps.vivado_session,
                    )
                    state.control.current_dcp_path = Path(pre_chain_path).resolve()
                except Exception as restore_err:
                    logger.error(f"[chain] Pre-chain restore also failed: {restore_err}")
            if deps.compat is not None:
                deps.compat.add_message("user",
                    f"[AUTO-CHAIN ERROR] {target_tool} failed, design restored to pre-chain state.")
            break


async def _auto_refresh_critical_paths(state: OptimizerState, deps: NodeDeps) -> None:
    """Re-extract critical paths after layout/routing changes."""
    result = await call_tool_fn(
        "vivado_extract_critical_path_cells",
        {"num_paths": 10},
        deps.rapidwright_session, deps.vivado_session,
    )
    cell_paths = parse_critical_path_cells(result)
    if cell_paths:
        update_critical_paths(state, cell_paths, iteration=state.iteration.current)
        logger.info(f"[EXECUTE] Auto-refreshed {len(cell_paths)} critical paths")
    else:
        state.timing.critical_paths_stale = False
