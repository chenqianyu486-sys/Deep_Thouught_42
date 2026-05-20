"""LLM tool loop node: inner loop for LLM calls and tool execution.

This is the most complex node. It contains an internal while-True loop
that handles: LLM calls, response parsing, tool execution, flow control,
repetition detection, and reflection checkpoints.

Reference: dcp_optimizer.py get_completion() (L4447-5079, ~632 lines).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from ...state import OptimizerState
from ...deps import NodeDeps
from ...edges import NodeName
from ...pure.timing import parse_timing_summary, is_valid_wns
from ...pure.tool_summary import summarize_tool_result
from ...pure.tool_router import call_tool as call_tool_fn
from ...pure.step_state import extract_step_state
from ...pure.constants import WNS_TARGET_THRESHOLD, GLOBAL_NO_IMPROVEMENT_LIMIT, DASHBOARD_REFRESH_MAP
from ...pure.compress import compress_context
from ...pure.critical_path import parse_critical_path_cells, update_critical_paths
from ...pure.context_snapshot import build_context_snapshot, inject_context_snapshot_at_end
from ...color import green, yellow

logger = logging.getLogger(__name__)

# Max tool rounds per iteration (safety limit)
MAX_TOOL_ROUNDS = 80

# Tools that require mandatory WNS evaluation after execution.
# After these tools complete, report_timing_summary is auto-called
# and the WNS delta is injected into context for the LLM's next decision.
POST_EVAL_TOOLS = frozenset({
    "vivado_route_design",
    "rapidwright_execute_fanout_strategy",
})

# Compression check interval: only check every N rounds to reduce CPU overhead
COMPRESS_CHECK_INTERVAL = 5



async def llm_tool_loop_node(
    state: OptimizerState, deps: NodeDeps
) -> str:
    """Execute LLM calls and tool calls in an inner loop.

    The loop continues until:
        - flow_control=DONE/SWITCH_STRATEGY from LLM
        - Tool round limit reached
        - Wall-clock timeout
        - User exit requested
        - WNS target met

    Returns:
        Next node name (deterministic: iteration_end).
    """
    tool_round = 0
    iteration_start_wns = state.timing.best_wns

    while True:
        tool_round += 1
        state.iteration.tool_round = tool_round

        # ── Check exit conditions ─────────────────────────────────
        if _check_exit_conditions(state, tool_round):
            return NodeName.ITERATION_END

        # ── Compress context (throttled) ───────────────────────────
        if deps.memory_manager is not None and tool_round % COMPRESS_CHECK_INTERVAL == 0:
            try:
                compress_context(state, deps)
            except Exception as e:
                logger.warning(f"[llm_tool_loop] Compression failed: {e}")

        # ── Prepare API messages ──────────────────────────────────
        api_messages = _prepare_api_messages(deps, state)

        # ── Call LLM ──────────────────────────────────────────────
        state.model.llm_call_count += 1
        current_model = state.model.current_model
        llm_start_time = time.time()

        # Log prompt to file for observability
        if deps.prompt_logger is not None:
            try:
                deps.prompt_logger.log_prompt(
                    model=current_model,
                    messages=api_messages,
                    iteration=state.iteration.current,
                )
            except Exception as e:
                logger.debug(f"[llm_tool_loop] PromptLogger failed: {e}")

        # Use reasoning mode when planner model is active
        reasoning_cfg = None
        if current_model == state.model.planner_model and deps.reasoning_config:
            reasoning_cfg = deps.reasoning_config

        try:
            response = await _call_llm_with_retry(
                state, deps, api_messages, current_model,
                reasoning_config=reasoning_cfg,
            )
        except Exception as e:
            logger.error(f"[llm_tool_loop] LLM call failed: {e}")
            return NodeName.ITERATION_END

        if response is None:
            logger.info("[llm_tool_loop] LLM call returned None (exit requested)")
            return NodeName.ITERATION_END

        # ── Track cost ────────────────────────────────────────────
        _track_cost(state, response)

        # ── Parse response ────────────────────────────────────────
        if not response.choices:
            logger.error("[llm_tool_loop] Empty choices in API response")
            return NodeName.ITERATION_END

        message = response.choices[0].message
        assistant_content = message.content or ""

        # Add assistant message to compat
        if deps.compat is not None:
            metadata = {"tool_calls": message.tool_calls} if message.tool_calls else None
            deps.compat.add_message("assistant", assistant_content, metadata)

        # ── Log LLM response ─────────────────────────────────────
        llm_elapsed = time.time() - llm_start_time
        usage = getattr(response, 'usage', None)
        prompt_tok = getattr(usage, 'prompt_tokens', 0) if usage else 0
        completion_tok = getattr(usage, 'completion_tokens', 0) if usage else 0
        call_cost = float(getattr(usage, 'cost', 0.0) or 0.0) if usage else 0.0
        tool_call_count = len(message.tool_calls) if message.tool_calls else 0
        # Reasoning tokens (OpenRouter extended fields)
        reasoning_tok = 0
        if usage:
            comp_details = getattr(usage, 'completion_tokens_details', None)
            if comp_details:
                reasoning_tok = getattr(comp_details, 'reasoning_tokens', 0) or 0
        reasoning_suffix = f", reasoning={reasoning_tok}" if reasoning_tok > 0 else ""
        logger.info(
            f"[LLM] {current_model} | {llm_elapsed:.1f}s | "
            f"prompt={prompt_tok}, completion={completion_tok}{reasoning_suffix}, "
            f"cost=${call_cost:.4f}, total=${state.cost.total_cost:.4f} | "
            f"tools={tool_call_count}"
        )

        # ── Extract step_state ────────────────────────────────────
        step_state = extract_step_state(message)
        state.control.step_state = step_state

        if step_state:
            logger.info(
                f"[llm_tool_loop] STEP_STATE: step_id={step_state.step_id}, "
                f"result_status={step_state.result_status}, "
                f"flow_control={step_state.flow_control}"
            )
            flow_signal = step_state.flow_control
        else:
            # report_step_state missing — do NOT skip tool execution.
            # Inject a short note and synthesize default CONTINUE.
            logger.warning("[llm_tool_loop] report_step_state missing, auto-CONTINUE")
            deps.compat.add_message("user", "[NOTE] report_step_state missing. Auto-CONTINUE. Include it next turn.")
            flow_signal = "CONTINUE"

        # ── Check flow_control before tool execution ──────────────
        if flow_signal in ("DONE", "SWITCH_STRATEGY"):
            _handle_flow_signal(state, deps, flow_signal, assistant_content)
            return NodeName.ITERATION_END

        # ── Execute tool calls ────────────────────────────────────
        if message.tool_calls:
            for tc in message.tool_calls:
                if not tc.function:
                    continue

                tool_name = tc.function.name
                state.iteration.tools_used.append(tool_name)
                try:
                    tool_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    tool_args = {}

                # Execute tool
                tool_start = time.time()
                logger.info(
                    f"[TOOL_CALL_START] round={tool_round}, tool={tool_name}"
                )
                result = await call_tool_fn(
                    tool_name=tool_name,
                    arguments=tool_args,
                    rapidwright_session=deps.rapidwright_session,
                    vivado_session=deps.vivado_session,
                    raw_tool_outputs=state.context.raw_tool_outputs,
                    iteration=state.iteration.current,
                    tool_round=tool_round,
                    high_fanout_nets=state.timing.high_fanout_nets,
                )
                tool_elapsed = time.time() - tool_start
                logger.info(
                    f"[TOOL_CALL_END] round={tool_round}, tool={tool_name}, "
                    f"elapsed={tool_elapsed:.1f}s"
                )

                # Summarize result
                raw_result = result
                summary = summarize_tool_result(
                    tool_name, raw_result,
                    latest_wns=state.timing.latest_wns,
                    latest_tns=state.timing.latest_tns,
                    latest_failing_endpoints=state.timing.latest_failing_endpoints,
                    prev_best_wns=state.timing.prev_best_wns,
                )

                # Store raw output
                state.context.raw_tool_outputs[(state.iteration.current, tool_round)] = (tool_name, raw_result)
                if len(state.context.raw_tool_outputs) > state.context.raw_tool_output_max:
                    oldest_key = min(state.context.raw_tool_outputs.keys(), key=lambda k: (k[0], k[1]))
                    del state.context.raw_tool_outputs[oldest_key]

                # Add tool result to compat
                if deps.compat is not None:
                    deps.compat.add_message("tool", summary, {
                        "tool_call_id": tc.id,
                        "name": tool_name,
                    })

                # Track WNS from timing results
                _track_wns_from_result(state, tool_name, raw_result)

                # Track critical path data from extract tool
                _update_critical_paths_from_tool(state, tool_name, raw_result)

                # Mark critical paths stale after layout/routing changes
                if tool_name in ("vivado_phys_opt_design", "vivado_route_design"):
                    state.timing.critical_paths_stale = True

                # Track dashboard field freshness
                refreshable = DASHBOARD_REFRESH_MAP.get(tool_name)
                if refreshable:
                    state.timing.refreshed_fields |= refreshable

                # Post-eval hook: force WNS check after critical tools
                if tool_name in POST_EVAL_TOOLS:
                    try:
                        await _post_eval_hook(state, deps, tool_name)
                    except Exception as e:
                        logger.warning(f"[llm_tool_loop] Post-eval hook failed for {tool_name}: {e}")

                # Track tool errors
                result_lower = summary.lower() if summary else ""
                if "error" in result_lower and "success" not in result_lower:
                    state.iteration.tool_errors.append({
                        "tool": tool_name,
                        "result": summary[:2000],
                    })

            # Auto-refresh critical paths if stale after layout/routing changes
            if state.timing.critical_paths_stale:
                try:
                    await _auto_refresh_critical_paths(state, deps)
                except Exception as e:
                    logger.warning(f"[llm_tool_loop] Critical path auto-refresh failed: {e}")

            continue  # Loop continues to process tool results

        # ── No tool calls — check if done ─────────────────────────
        # This handles the case where LLM returns text only (no tools, no flow_control)

        # Check WNS target
        if _check_wns_target_met(state):
            return NodeName.ITERATION_END

        # Reflection checkpoint every 8 rounds
        if tool_round > 1 and tool_round % 8 == 0:
            _inject_reflection(state, deps, tool_round)

        continue


def _check_exit_conditions(state: OptimizerState, tool_round: int) -> bool:
    """Check if the inner loop should exit."""
    # Tool round limit
    if tool_round > MAX_TOOL_ROUNDS:
        logger.warning(yellow(f"[llm_tool_loop] Tool round limit reached ({tool_round} > {MAX_TOOL_ROUNDS})"))
        return True

    # Wall-clock timeout
    if state.control.start_time:
        elapsed = time.time() - state.control.start_time
        if elapsed > state.control.wall_clock_timeout:
            logger.warning(f"[llm_tool_loop] Wall-clock timeout: {elapsed:.0f}s")
            state.control.is_done = True
            state.control.done_reason = "wall_clock_timeout"
            return True

    # User exit
    if state.control.user_exit_requested:
        logger.info("[llm_tool_loop] User exit requested")
        return True

    # Cost limit
    if state.cost.total_cost >= state.cost.cost_hard_limit:
        logger.warning(
            yellow(f"[llm_tool_loop] Cost limit reached: "
                   f"${state.cost.total_cost:.4f} >= ${state.cost.cost_hard_limit:.2f}")
        )
        state.control.is_done = True
        state.control.done_reason = "cost_limit"
        return True

    return False


def _check_wns_target_met(state: OptimizerState) -> bool:
    """Check if WNS target is met."""
    return (
        state.timing.latest_wns is not None
        and state.timing.latest_wns >= WNS_TARGET_THRESHOLD
        and is_valid_wns(state.timing.latest_wns, state.timing.clock_period, state.timing.best_wns)
    )


FORMAT_STAMP = (
    "[FORMAT: EVERY response MUST call the report_step_state tool with "
    "step_id/result_status/flow_control. Chain-of-thought text OK.]"
)


def _prepare_api_messages(deps: NodeDeps, state: OptimizerState) -> list:
    """Prepare API messages with fresh dashboard as last user message."""
    if deps.compat is None:
        return []
    try:
        api_messages = deps.compat.get_formatted_for_api()
    except Exception:
        return []

    # Prepend FORMAT stamp to system prompt for persistent attention
    if api_messages and api_messages[0].get("role") == "system":
        system_content = api_messages[0].get("content", "")
        if not system_content.startswith("[FORMAT:"):
            api_messages[0]["content"] = FORMAT_STAMP + "\n\n" + system_content

    # Rebuild and inject dashboard as last user message for maximum attention
    _inject_dashboard_at_end(api_messages, state, deps)
    return api_messages


def _inject_dashboard_at_end(
    api_messages: list, state: OptimizerState, deps: NodeDeps
) -> None:
    """Build fresh dashboard from current state and inject as last user message."""
    current_wns = state.timing.latest_wns
    best_wns = state.timing.best_wns if state.timing.best_wns > float('-inf') else None
    elapsed = 0.0
    remaining = state.control.wall_clock_timeout
    if state.control.start_time:
        elapsed = time.time() - state.control.start_time
        remaining = max(0, state.control.wall_clock_timeout - elapsed)

    snapshot = build_context_snapshot(
        clock_period=state.timing.clock_period,
        current_wns=current_wns,
        best_wns=best_wns,
        best_wns_iteration=state.timing.best_wns_iteration,
        tns=state.timing.latest_tns,
        failing_endpoints=state.timing.latest_failing_endpoints,
        high_fanout_nets=state.timing.high_fanout_nets or [],
        critical_path_spread=state.timing.critical_path_spread,
        resource_utilization=state.timing.resource_utilization,
        elapsed_time=elapsed,
        remaining_time=remaining,
        total_cost=state.cost.total_cost,
        cost_hard_limit=state.cost.cost_hard_limit,
        iteration_narratives=state.iteration.narratives,
        tools_used=state.iteration.tools_used,
        critical_paths=state.timing.critical_paths,
        refreshed_fields=state.timing.refreshed_fields,
        input_dcp=str(state.control.input_dcp.resolve()) if state.control.input_dcp else None,
        output_dcp=str(state.control.output_dcp.resolve()) if state.control.output_dcp else None,
    )

    inject_context_snapshot_at_end(api_messages, snapshot)


async def _call_llm_with_retry(
    state: OptimizerState,
    deps: NodeDeps,
    api_messages: list,
    model: str,
    max_retries: int = 3,
    retry_delay: float = 2.0,
    reasoning_config: dict | None = None,
):
    """Call LLM with retry logic for rate limits and errors."""
    if deps.openai_client is None:
        logger.error("[llm_tool_loop] No OpenAI client available")
        return None

    # Build extra_body for reasoning mode (OpenRouter)
    extra_body = None
    if reasoning_config and reasoning_config.get("enabled"):
        reasoning_payload: dict = {"enabled": True}
        max_output_tokens = reasoning_config.get("max_output_tokens")
        if max_output_tokens is not None:
            reasoning_payload["max_output_tokens"] = max_output_tokens
        extra_body = {"reasoning": reasoning_payload}
        logger.info(f"[llm_tool_loop] Reasoning mode enabled for {model}")

    last_exception = None
    for retry in range(max_retries):
        try:
            kwargs = dict(
                model=model,
                messages=api_messages,
                tools=deps.tools if deps.tools else None,
                timeout=600.0,
            )
            if extra_body:
                kwargs["extra_body"] = extra_body
            response = await deps.openai_client.chat.completions.create(**kwargs)
            return response
        except Exception as e:
            last_exception = e
            error_str = str(e)

            # Rate limit (429)
            if "429" in error_str:
                # Try fallback models
                next_model = _get_fallback_model(state, model)
                if next_model and next_model != model:
                    logger.warning(f"[llm_tool_loop] Rate limit on {model}, fallback to {next_model}")
                    state.model.current_model = next_model
                    model = next_model
                    wait_time = retry_delay * (2 ** retry)
                    await asyncio.sleep(wait_time)
                    continue

            if retry < max_retries - 1:
                wait_time = retry_delay * (2 ** retry)
                logger.warning(f"[llm_tool_loop] Retry {retry+1}/{max_retries}, waiting {wait_time}s: {e}")
                await asyncio.sleep(wait_time)
            else:
                logger.error(f"[llm_tool_loop] LLM call failed after {max_retries} retries: {e}")

    if last_exception:
        raise last_exception
    return None


def _get_fallback_model(state: OptimizerState, current_model: str) -> str | None:
    """Get next fallback model for rate limit recovery."""
    # Try worker fallbacks
    if current_model == state.model.worker_model:
        fallbacks = state.model.worker_fallback_models
        idx = state.model.worker_fallback_index
        if idx < len(fallbacks):
            state.model.worker_fallback_index = idx + 1
            return fallbacks[idx]

    # Try planner fallbacks
    if current_model == state.model.planner_model:
        fallbacks = state.model.planner_fallback_models
        idx = state.model.planner_fallback_index
        if idx < len(fallbacks):
            state.model.planner_fallback_index = idx + 1
            return fallbacks[idx]

    return None


def _track_cost(state: OptimizerState, response) -> None:
    """Track token usage and cost from LLM response."""
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

            # Track reasoning tokens (OpenRouter extended fields)
            completion_details = getattr(usage, 'completion_tokens_details', None)
            if completion_details:
                reasoning = getattr(completion_details, 'reasoning_tokens', 0) or 0
                state.cost.total_reasoning_tokens += reasoning
    except Exception as e:
        logger.warning(f"[llm_tool_loop] Failed to parse usage: {e}")


def _update_critical_paths_from_tool(state: OptimizerState, tool_name: str, raw_result: str) -> None:
    """Parse and cache critical path cells from tool result."""
    if tool_name != "vivado_extract_critical_path_cells":
        return
    cell_paths = parse_critical_path_cells(raw_result)
    if cell_paths:
        update_critical_paths(state, cell_paths, iteration=state.iteration.current)


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
        logger.info(f"[llm_tool_loop] Auto-refreshed {len(cell_paths)} critical paths")
    else:
        state.timing.critical_paths_stale = False


async def _post_eval_hook(state: OptimizerState, deps: NodeDeps, tool_name: str) -> None:
    """Force WNS evaluation after critical tools (route_design, fanout_strategy).

    Calls report_timing_summary, computes WNS delta, and injects an eval
    notice into the conversation so the LLM has concrete data for its next
    flow_control decision.
    """
    prev_wns = state.timing.latest_wns

    # Call report_timing_summary
    timing_result = await call_tool_fn(
        "vivado_report_timing_summary",
        {},
        deps.rapidwright_session, deps.vivado_session,
    )

    # Parse WNS from result
    timing = parse_timing_summary(timing_result)
    wns = timing.get("wns")
    tns = timing.get("tns")
    fe = timing.get("failing_endpoints")

    if wns is None:
        logger.warning(f"[llm_tool_loop] Post-eval: could not parse WNS from report_timing_summary")
        return

    # Update state
    state.timing.latest_wns = wns
    if tns is not None:
        state.timing.latest_tns = tns
    if fe is not None:
        state.timing.latest_failing_endpoints = fe

    # Update best WNS
    if wns > state.timing.best_wns:
        state.timing.best_wns = wns
        state.timing.best_wns_iteration = state.iteration.current
        state.timing.best_wns_tns = tns
        state.timing.best_wns_failing_endpoints = fe
        state.control.needs_save = True

    # Compute delta
    delta = wns - prev_wns if prev_wns is not None else 0.0
    if delta > 0.001:
        verdict = "IMPROVED"
    elif abs(delta) <= 0.001:
        verdict = "UNCHANGED"
    else:
        verdict = "REGRESSED"

    # Build eval notice
    prev_str = f"{prev_wns:.3f}ns" if prev_wns is not None else "unknown"
    eval_notice = (
        f"[EVAL] After {tool_name}: WNS={wns:.3f}ns "
        f"(delta={delta:+.3f}ns vs previous {prev_str}). "
        f"{verdict}. "
        f"Use this data for your next report_step_state decision."
    )
    if tns is not None:
        eval_notice += f" TNS={tns:.3f}ns"
    if fe is not None:
        eval_notice += f" FE={fe}"

    # Inject into conversation
    if deps.compat is not None:
        deps.compat.add_message("user", eval_notice)

    logger.info(f"[llm_tool_loop] Post-eval: {tool_name} -> WNS={wns:.3f}ns (delta={delta:+.3f}, {verdict})")


def _track_wns_from_result(state: OptimizerState, tool_name: str, raw_result: str) -> None:
    """Parse WNS from tool result and update state."""
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

        # Update best WNS
        if wns > state.timing.best_wns:
            state.timing.best_wns = wns
            state.timing.best_wns_iteration = state.iteration.current
            state.timing.best_wns_tns = tns
            state.timing.best_wns_failing_endpoints = fe
            state.control.needs_save = True


def _handle_flow_signal(
    state: OptimizerState,
    deps: NodeDeps,
    flow_signal: str,
    assistant_content: str,
) -> None:
    """Handle DONE or SWITCH_STRATEGY flow signals."""
    if flow_signal == "DONE":
        logger.info(green("[llm_tool_loop] LLM signaled DONE"))
        # Check if WNS target is actually met
        if _check_wns_target_met(state):
            state.control.is_done = True
            state.control.done_reason = "wns_target_met"
        else:
            state.control.done_reason = "flow_control_done_next_iteration"

    elif flow_signal == "SWITCH_STRATEGY":
        logger.info(yellow("[llm_tool_loop] LLM signaled SWITCH_STRATEGY"))
        state.control.done_reason = "switch_strategy"
        # Inject analysis-forcing prompt
        if deps.compat is not None:
            current_wns = state.timing.latest_wns
            wns_str = f"{current_wns:.3f}ns" if current_wns is not None else "unknown"
            enforced_msg = (
                f"SYSTEM ENFORCED SWITCH: The previous model signaled SWITCH_STRATEGY while WNS={wns_str}. "
                f"Strategy switch triggered. You MUST start this iteration with structured analysis:\n"
                f"1. Call report_timing_summary and extract_critical_path_cells to gather current signal data\n"
                f"2. Form a hypothesis about the dominant timing obstacle\n"
                f"3. Select a strategy based on the hypothesis\n"
                f"DO NOT repeat the same strategy that just failed."
            )
            deps.compat.add_message("user", enforced_msg)


def _inject_reflection(state: OptimizerState, deps: NodeDeps, tool_round: int) -> None:
    """Inject reflection checkpoint prompt."""
    if deps.compat is None:
        return

    current_wns = state.timing.latest_wns
    best_wns = state.timing.best_wns if state.timing.best_wns > float('-inf') else None
    wns_str = f"{current_wns:.3f}ns" if current_wns is not None else "(unknown)"

    reflection = (
        f"REFLECTION CHECKPOINT (tool round {tool_round}):\n"
        f"- Current WNS: {wns_str}"
    )
    if best_wns is not None:
        reflection += f" (best: {best_wns:.3f}ns)"
    reflection += (
        f"\n- Step back and evaluate:\n"
        f"  1. Is your current strategy producing significant WNS improvement?\n"
        f"  2. If yes, continue. If no, is it time to SWITCH_STRATEGY?\n"
        f"  3. If unsure, call report_timing_summary to re-assess."
    )
    deps.compat.add_message("user", reflection)
    logger.info(f"[llm_tool_loop] Reflection checkpoint injected at round {tool_round}")
