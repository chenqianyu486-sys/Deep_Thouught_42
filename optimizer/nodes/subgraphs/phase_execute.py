"""EXECUTE phase: run the chosen optimization strategy.

Full execution tools are exposed. Auto chain actions and post-eval
hooks are applied after critical tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time

from pathlib import Path

from optimizer.state import OptimizerState, PhaseEntry, ToolCallRecord, LLMCallRecord, record_flow_signal
from optimizer.deps import NodeDeps
from optimizer.edges import NodeName
from optimizer.pure.tool_filter import LoopPhase, PHASE_MAX_ROUNDS, filter_tools_for_phase
from optimizer.pure.tool_summary import summarize_tool_result
from optimizer.pure.tool_router import call_tool as call_tool_fn
from optimizer.pure.step_state import extract_step_state
from optimizer.pure.timing import parse_timing_summary, is_valid_wns
from optimizer.pure.constants import WNS_TARGET_THRESHOLD, DASHBOARD_REFRESH_MAP, SKILL_CHAIN_ACTIONS, HEAVY_CHAIN_SKILLS, PHASE_TOOL_RATE_LIMITS, _TOOL_TIMEOUT_DEFAULTS, build_llm_extra_body, EXECUTE_STRATEGY_TOOL_MAP
from optimizer.pure.critical_path import parse_critical_path_cells, update_critical_paths
from optimizer.nodes.subgraphs.phase_handoff import build_phase_handoff, transition_phase
from optimizer.pure.context_snapshot import inject_merged_dashboard
from optimizer.color import green, yellow

logger = logging.getLogger(__name__)

# Tools that trigger mandatory WNS evaluation
POST_EVAL_TOOLS = frozenset({
    "vivado_route_design",
    "rapidwright_execute_pblock_strategy",
    "vivado_physopt_and_route",  # triggers WNS eval after PhysOpt+route
    "vivado_phys_opt_design",  # standalone phys_opt_design for split physopt chain
})

# Tools that modify the design (side-effect tools).
# Used for no-progress detection: if LLM only calls read-only tools for
# NO_PROGRESS_LIMIT consecutive rounds, exit the phase early.
SIDE_EFFECT_TOOLS = frozenset({
    "vivado_place_design",
    "vivado_route_design",
    "vivado_phys_opt_design",
    "vivado_physopt_and_route",
    "vivado_opt_design",
    "vivado_create_and_apply_pblock",
    "rapidwright_execute_pblock_strategy",
    "rapidwright_execute_fanout_strategy",
    "rapidwright_execute_congestion_spreading",
    "rapidwright_optimize_pin_swapping",
    "rapidwright_flatten_lut_cascade",
    "rapidwright_replicate_critical_cells",
    "rapidwright_execute_register_retiming",
    "rapidwright_execute_net_swapping",
    "rapidwright_optimize_cell_placement",
    "rapidwright_optimize_lut_input_cone",
})
# No-progress detection threshold. After this many consecutive rounds
# without any side-effect tool call, exit the EXECUTE phase early.
#
# Coordinated with _TOOL_TIMEOUT_DEFAULTS:
#   - Longest read-only tool: vivado_run_tcl (120s base × design_size_factor)
#   - 6 rounds × ~120s worst-case tool wait = ~720s before exit
#   - Execution tools (place_design=1800s, route_design=1800s) always reset counter
#   - LLM round-trip latency ~5-15s → ~30-90s overhead per 6-round window
NO_PROGRESS_LIMIT = 6


async def run_execute_phase(state: OptimizerState, deps: NodeDeps) -> LoopPhase:
    """Run the EXECUTE phase: execute the chosen strategy via tool calls.

    Returns:
        LoopPhase.EVALUATE (always, even on failure).
    """
    max_rounds = PHASE_MAX_ROUNDS.get(LoopPhase.EXECUTE, 30)
    tool_round = 0
    tools_called: list[str] = []
    wns_before = state.timing.latest_wns
    unplaced_without_replace = False
    pre_unplace_path: Path | None = None
    llm_summary = ""
    state.context.tool_phase_call_counts.clear()
    reached_callback = False  # track if the strategy reached callback indicating completion
    no_progress_count = 0  # consecutive rounds without side-effect tool calls

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
                record_flow_signal(state, "EXEC_DONE", "execution_complete",
                                   phase="EXECUTE_STRATEGY", result_status=step_state.result_status or "")
                break

            # EXHAUSTED during execution
            if flow_signal == "EXHAUSTED":
                logger.info("[EXECUTE] LLM signaled EXHAUSTED")
                record_flow_signal(state, "EXHAUSTED", "execution_exhausted",
                                   phase="EXECUTE_STRATEGY", result_status=step_state.result_status or "")
                break

        else:
            state.context.step_state_misses += 1
            if deps.compat is not None:
                deps.compat.add_message("user", "[NOTE] report_step_state missing. Include it next turn.")

        # Execute tool calls
        if message.tool_calls:
            round_had_side_effect = False
            force_exit = False  # set by inner loop to break outer while
            _pending_tool_count = len([tc for tc in message.tool_calls if tc.function])  # Track pending tool calls
            for tc in message.tool_calls:
                if not tc.function:
                    continue

                tool_name = tc.function.name
                state.iteration.tools_used.append(tool_name)
                tools_called.append(tool_name)
                if tool_name in SIDE_EFFECT_TOOLS:
                    round_had_side_effect = True

                try:
                    tool_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    tool_args = {}

                # Auto-inject critical_path_cells for pblock tools
                if tool_name in ("rapidwright_execute_pblock_strategy", "rapidwright_analyze_pblock_region"):
                    if not tool_args.get("critical_path_cells") and state.timing.critical_paths:
                        cells = []
                        seen = set()
                        for cp in state.timing.critical_paths[:10]:
                            for cell_name in cp.cells:
                                if cell_name not in seen:
                                    seen.add(cell_name)
                                    cells.append(cell_name)
                                    if len(cells) >= 50:
                                        break
                            if len(cells) >= 50:
                                break
                        if cells:
                            tool_args["critical_path_cells"] = cells
                            logger.info(f"[EXECUTE] Injected {len(cells)} critical path cells for {tool_name}")

                # Auto-compute adaptive resource_multiplier for pblock strategy
                if tool_name == "rapidwright_execute_pblock_strategy":
                    if "resource_multiplier" not in tool_args:
                        tool_args["resource_multiplier"] = _compute_adaptive_pblock_multiplier(state)
                        logger.info(f"[EXECUTE] Adaptive resource_multiplier: {tool_args['resource_multiplier']:.2f}")

                # Auto-inject critical_paths for LUT cascade tool
                if tool_name == "rapidwright_flatten_lut_cascade":
                    if not tool_args.get("critical_paths") and state.timing.critical_paths:
                        paths = [cp.cells for cp in state.timing.critical_paths[:10] if cp.cells]
                        if paths:
                            tool_args["critical_paths"] = paths
                            logger.info(f"[EXECUTE] Injected {len(paths)} critical paths for {tool_name}")

                # Rate limiting for read-only tools
                if tool_name in PHASE_TOOL_RATE_LIMITS:
                    call_count = state.context.tool_phase_call_counts.get(tool_name, 0) + 1
                    state.context.tool_phase_call_counts[tool_name] = call_count
                    if call_count > PHASE_TOOL_RATE_LIMITS[tool_name]:
                        result = (f"[RATE LIMITED] Tool '{tool_name}' called {call_count} times this phase "
                                  f"(limit: {PHASE_TOOL_RATE_LIMITS[tool_name]}). ")
                        if tool_name == "rapidwright_search_cells":
                            result += ("Use cell_types parameter to batch multiple types in one call, "
                                       "or get_design_info for type overview.")
                        elif tool_name == "vivado_run_tcl":
                            result += (
                                "Dashboard already contains fresh timing data from init_analysis. "
                                "Use vivado_report_timing_summary or Dashboard values directly "
                                "instead of raw Tcl. For execution commands (place_design, "
                                "route_design, phys_opt_design), use the dedicated MCP tools."
                            )
                        if deps.compat is not None:
                            deps.compat.add_message("tool", result, {
                                "tool_call_id": tc.id, "name": tool_name,
                            })
                        _pending_tool_count -= 1  # Rate-limited tools count as completed
                        continue

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
                    tool_cache=state.context.tool_cache,
                    design_size_factor=state.timing.design_size_factor,
                )
                tool_elapsed = time.time() - tool_start
                logger.info(f"[EXECUTE] {tool_name} completed in {tool_elapsed:.1f}s")
                _pending_tool_count -= 1  # This tool call is no longer pending

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

                # Track place_design -unplace for rollback guard
                if tool_name == "vivado_place_design":
                    directive = tool_args.get("directive", "").lower()
                    if directive == "unplace":
                        unplaced_without_replace = True
                        try:
                            pre_unplace_ckpt = Path(f"/tmp/pre_unplace_{state.iteration.current}_{tool_round}.dcp")
                            await call_tool_fn(
                                "vivado_write_checkpoint", {"dcp_path": str(pre_unplace_ckpt), "force": True},
                                deps.rapidwright_session, deps.vivado_session,
                                design_size_factor=state.timing.design_size_factor,
                            )
                            pre_unplace_path = pre_unplace_ckpt
                        except Exception as e:
                            logger.warning(f"[EXECUTE] Failed to save pre-unplace checkpoint: {e}")
                    else:
                        unplaced_without_replace = False

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
                post_eval_verdict = None
                if tool_name in POST_EVAL_TOOLS:
                    # For physopt_and_route, WNS is already in the JSON result
                    if tool_name == "vivado_physopt_and_route":
                        try:
                            data = json.loads(result) if result else {}
                            post = data.get("post_optimization", {})
                            if isinstance(post, dict) and post.get("wns") is not None:
                                prev_wns = state.timing.latest_wns
                                new_wns = float(post["wns"])
                                raw_tns = post.get("tns")
                                new_tns = float(raw_tns) if isinstance(raw_tns, (int, float)) else None
                                raw_fe = post.get("failing_endpoints")
                                new_fe = int(raw_fe) if isinstance(raw_fe, (int, float)) else None
                                state.timing.latest_wns = new_wns
                                if new_tns is not None:
                                    state.timing.latest_tns = new_tns
                                if new_fe is not None:
                                    state.timing.latest_failing_endpoints = new_fe
                                if new_wns > state.timing.best_wns:
                                    state.timing.best_wns = new_wns
                                    state.timing.best_wns_iteration = state.iteration.current
                                    state.timing.best_wns_tns = new_tns
                                    state.timing.best_wns_failing_endpoints = new_fe
                                    state.control.needs_save = True
                                delta = new_wns - prev_wns if prev_wns is not None else 0.0
                                verdict = "IMPROVED" if delta > 0.001 else ("UNCHANGED" if abs(delta) <= 0.001 else "REGRESSED")
                                post_eval_verdict = verdict
                                eval_notice = f"[EVAL] After {tool_name}: WNS={new_wns:.3f}ns (delta={delta:+.3f}ns vs previous). {verdict}."
                                if new_tns is not None:
                                    eval_notice += f" TNS={new_tns:.3f}ns"
                                if deps.compat is not None:
                                    deps.compat.add_message("user", eval_notice)
                                logger.info(f"[EXECUTE] Post-eval (from result): {tool_name} -> WNS={new_wns:.3f}ns (delta={delta:+.3f}, {verdict})")
                            else:
                                # Fallback to full timing report if JSON doesn't have WNS
                                post_eval_verdict = await _post_eval_hook(state, deps, tool_name)
                        except (json.JSONDecodeError, TypeError, ValueError):
                            post_eval_verdict = await _post_eval_hook(state, deps, tool_name)
                    else:
                        try:
                            post_eval_verdict = await _post_eval_hook(state, deps, tool_name)
                        except Exception as e:
                            logger.warning(f"[EXECUTE] Post-eval hook failed for {tool_name}: {e}")
                await _try_save_best_checkpoint(state, deps)

                # Chain actions for skills — gated by post-eval verdict.
                # When a heavy-chain skill reports UNCHANGED, the skill produced no
                # netlist change, so running place_design+route_design (~180s) is
                # wasteful. Run a lightweight validation instead.
                if tool_name in SKILL_CHAIN_ACTIONS:
                    if (post_eval_verdict == "UNCHANGED"
                            and tool_name in HEAVY_CHAIN_SKILLS
                            and deps.vivado_session):
                        logger.info(
                            f"[EXECUTE] Post-eval UNCHANGED for {tool_name}, "
                            f"skipping heavy chain (saves ~180s). Running lightweight validation."
                        )
                        if deps.compat is not None:
                            deps.compat.add_message("user",
                                f"[CHAIN GATE] {tool_name}: post-eval UNCHANGED — "
                                f"skill did not modify the netlist. Skipping "
                                f"place+create_pblock+place+route chain. "
                                f"Running lightweight place_design to verify."
                            )
                        await _lightweight_chain_validation(state, deps, tool_name, tools_called)
                    else:
                        try:
                            skill_data = json.loads(result) if result else {}
                            if isinstance(skill_data, dict) and "error" in skill_data:
                                logger.warning(f"[EXECUTE] Skill {tool_name} returned error, skipping chain: {skill_data['error']}")
                            else:
                                await _execute_chain_actions(state, deps, tool_name, skill_data, tools_called)
                                reached_callback = True
                        except Exception as e:
                            logger.warning(f"[EXECUTE] Chain actions failed for {tool_name}: {e}")

                # Force exit on large WNS regression (>0.5ns below best).
                # EVALUATE will detect regression via detect_rollback_needed()
                # and trigger automatic rollback — no need for LLM to spin here.
                if (state.timing.latest_wns is not None
                        and state.timing.best_wns != float('-inf')
                        and state.timing.latest_wns < state.timing.best_wns - 0.5):
                    logger.warning(
                        yellow(f"[EXECUTE] Large WNS regression: {state.timing.latest_wns:.3f} "
                               f"< best {state.timing.best_wns:.3f} - 0.5, forcing exit")
                    )
                    record_flow_signal(
                        state, "SYSTEM_EXIT", "large_regression",
                        phase="EXECUTE_STRATEGY",
                    )
                    force_exit = True
                    break

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

            # Break outer while if inner for loop requested force exit
            if force_exit:
                break

            # No-progress detection: exit if LLM only calls read-only tools.
            # Only count a round when ALL tools have completed (none pending).
            # This prevents penalizing rounds where slow tools (place_design/route_design)
            # are still executing and the LLM hasn't had a chance to call execution tools yet.
            if round_had_side_effect:
                no_progress_count = 0
            elif _pending_tool_count <= 0:
                no_progress_count += 1
            # else: tools still pending — do NOT count this round toward no-progress

            if no_progress_count >= NO_PROGRESS_LIMIT:
                logger.warning(
                    f"[EXECUTE] No-progress limit reached ({no_progress_count} rounds "
                    f"without side-effect tools)"
                )
                record_flow_signal(
                    state, "SYSTEM_EXIT", "no_progress",
                    phase="EXECUTE_STRATEGY",
                )
                break

            continue

        # No tool calls — count as no-progress round
        no_progress_count += 1
        if no_progress_count >= NO_PROGRESS_LIMIT:
            logger.warning(
                f"[EXECUTE] No-progress limit reached ({no_progress_count} rounds "
                f"without side-effect tools)"
            )
            record_flow_signal(
                state, "SYSTEM_EXIT", "no_progress",
                phase="EXECUTE_STRATEGY",
            )
            break

        if _check_wns_target_met(state):
            break

    # Auto-rollback if design left unplaced at phase exit
    if unplaced_without_replace and pre_unplace_path is not None:
        logger.warning(
            f"[EXECUTE] WARNING: Design left unplaced at phase exit — "
            f"restoring from pre-unplace checkpoint"
        )
        try:
            await call_tool_fn(
                "vivado_open_checkpoint", {"dcp_path": str(pre_unplace_path)},
                deps.rapidwright_session, deps.vivado_session,
                design_size_factor=state.timing.design_size_factor,
            )
            # Refresh WNS after restore
            restore_result = await call_tool_fn(
                "vivado_report_timing_summary", {},
                deps.rapidwright_session, deps.vivado_session,
                design_size_factor=state.timing.design_size_factor,
            )
            restore_timing = parse_timing_summary(restore_result)
            if restore_timing.get("wns") is not None:
                state.timing.latest_wns = restore_timing["wns"]
                state.timing.latest_tns = restore_timing.get("tns")
                state.timing.latest_failing_endpoints = restore_timing.get("failing_endpoints")
        except Exception as e:
            logger.warning(f"[EXECUTE] Auto-rollback failed: {e}")

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
    await transition_phase(deps, LoopPhase.EXECUTE, LoopPhase.EVALUATE, handoff, tool_cache=state.context.tool_cache)
    return LoopPhase.EVALUATE


def _check_phase_exit(state: OptimizerState, tool_round: int, max_rounds: int) -> bool:
    if tool_round > max_rounds:
        logger.warning(f"[EXECUTE] Max rounds reached ({tool_round} > {max_rounds})")
        record_flow_signal(state, "SYSTEM_EXIT", "max_rounds", phase="EXECUTE_STRATEGY")
        return True
    if state.control.start_time:
        elapsed = time.time() - state.control.start_time
        if elapsed > state.control.wall_clock_timeout:
            state.control.is_done = True
            state.control.done_reason = "wall_clock_timeout"
            record_flow_signal(state, "SYSTEM_EXIT", "wall_clock_timeout", phase="EXECUTE_STRATEGY")
            return True
    if state.control.user_exit_requested:
        record_flow_signal(state, "SYSTEM_EXIT", "user_requested", phase="EXECUTE_STRATEGY")
        return True
    if state.cost.total_cost >= state.cost.cost_hard_limit:
        state.control.is_done = True
        state.control.done_reason = "cost_limit"
        record_flow_signal(state, "SYSTEM_EXIT", "cost_limit", phase="EXECUTE_STRATEGY")
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

    # Strategy enforcement: constrain LLM to execute only the selected strategy.
    # Prevents drift where LLM switches strategies mid-execution (e.g., selecting
    # PhysOpt in SELECT_STRATEGY but running PBLOCK+Fanout in EXECUTE).
    strategy = state.strategy.current_strategy
    if strategy:
        tool = EXECUTE_STRATEGY_TOOL_MAP.get(strategy, "")
        if tool:
            constraint = (
                f"[EXECUTE CONSTRAINT] Selected strategy: {strategy}. "
                f"You MUST call `{tool}` now. Do NOT call any other strategy tool. "
                f"Do NOT call analysis tools (vivado_report_timing_summary, "
                f"vivado_extract_critical_path_cells, vivado_run_tcl). "
                f"After `{tool}` completes, call report_step_state(EXEC_DONE)."
            )
            api_messages.append({"role": "user", "content": constraint})

    model = state.model.current_model
    state.model.llm_call_count += 1

    extra_body = build_llm_extra_body(
        deps.reasoning_config, model,
        state.model.planner_model, state.model.worker_model,
    )

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
            response = await deps.openai_client.chat.completions.create(**kwargs)
            # Log LLM call with state snapshot
            if deps.llm_call_logger:
                deps.llm_call_logger.log_call(
                    state, model=model, messages=api_messages, tools=phase_tools,
                    response=response, phase="EXECUTE",
                )
            return response
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
                         "vivado_route_design", "vivado_get_wns", "vivado_physopt_and_route"):
        return
    if tool_name != "vivado_report_timing_summary":
        state.timing.design_not_routed = False  # clear stale flag for non-timing tools
    # Try JSON path for physopt_and_route
    wns = None
    tns = None
    fe = None
    if tool_name == "vivado_physopt_and_route":
        try:
            data = json.loads(raw_result)
            post = data.get("post_optimization", {})
            if isinstance(post, dict) and "wns" in post and post.get("wns") is not None:
                wns = float(post["wns"])
                raw_tns = post.get("tns")
                tns = float(raw_tns) if isinstance(raw_tns, (int, float)) else None
                raw_fe = post.get("failing_endpoints")
                fe = int(raw_fe) if isinstance(raw_fe, (int, float)) else None
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    # Fallback to string parsing
    if wns is None:
        # Warn if timing report comes from an unrouted design
        if tool_name == "vivado_report_timing_summary":
            not_routed = "Design State" in raw_result and "Routed" not in raw_result
            state.timing.design_not_routed = not_routed
            if not_routed:
                logger.warning("[EXECUTE] WARNING: Timing report from unplaced/unrouted design — WNS may be inaccurate")
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
            design_size_factor=state.timing.design_size_factor,
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


async def _post_eval_hook(state: OptimizerState, deps: NodeDeps, tool_name: str) -> str | None:
    """Force WNS evaluation after critical tools.

    Returns:
        Verdict string ("IMPROVED", "UNCHANGED", "REGRESSED") or None on failure.
    """
    prev_wns = state.timing.latest_wns
    timing_result = await call_tool_fn(
        "vivado_report_timing_summary", {},
        deps.rapidwright_session, deps.vivado_session,
        design_size_factor=state.timing.design_size_factor,
    )
    # Detect false-positive timing from unplaced/unrouted designs
    design_not_routed = "Design State" in timing_result and "Routed" not in timing_result
    state.timing.design_not_routed = design_not_routed
    if design_not_routed:
        logger.warning("[EXECUTE] WARNING: Timing report from unplaced/unrouted design — WNS may be inaccurate")
    timing = parse_timing_summary(timing_result)
    wns = timing.get("wns")
    tns = timing.get("tns")
    fe = timing.get("failing_endpoints")
    if wns is None:
        return None

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
    if design_not_routed:
        eval_notice += " [WARNING: design not routed]"
    if tns is not None:
        eval_notice += f" TNS={tns:.3f}ns"
    if deps.compat is not None:
        deps.compat.add_message("user", eval_notice)
    logger.info(f"[EXECUTE] Post-eval: {tool_name} -> WNS={wns:.3f}ns (delta={delta:+.3f}, {verdict})")
    return verdict


async def _lightweight_chain_validation(state, deps, tool_name, tools_called):
    """Lightweight validation when a heavy-chain skill reports UNCHANGED.

    Runs a single vivado_place_design (no pblock, no unplace) to confirm
    the skill didn't change anything, then checks WNS. If WNS improves
    (false UNCHANGED), the full chain is re-run. Otherwise we skip the
    ~180s place+create_pblock+place+route chain.
    """
    logger.info(f"[chain-gate] Lightweight validation for {tool_name}: place_design only")
    try:
        result = await call_tool_fn(
            "vivado_place_design", {},
            deps.rapidwright_session, deps.vivado_session,
            design_size_factor=state.timing.design_size_factor,
        )
        state.iteration.tools_used.append("vivado_place_design")
        tools_called.append("vivado_place_design")
        state.timing.critical_paths_stale = True

        # Re-evaluate WNS
        verdict = await _post_eval_hook(state, deps, "vivado_place_design")
        if verdict == "IMPROVED":
            logger.info(
                f"[chain-gate] Lightweight validation found IMPROVED for {tool_name}, "
                f"re-running full chain"
            )
            if deps.compat is not None:
                deps.compat.add_message("user",
                    f"[CHAIN GATE] Lightweight validation found WNS improvement after "
                    f"{tool_name}. Running full chain (create_pblock + place + route)."
                )
            # Re-run the skill to get fresh data, then execute chain
            skill_result = await call_tool_fn(
                tool_name, {},
                deps.rapidwright_session, deps.vivado_session,
                design_size_factor=state.timing.design_size_factor,
            )
            skill_data = json.loads(skill_result) if isinstance(skill_result, str) else {}
            await _execute_chain_actions(state, deps, tool_name,
                                         skill_data if isinstance(skill_data, dict) else {},
                                         tools_called)
        else:
            logger.info(
                f"[chain-gate] Lightweight validation confirms UNCHANGED for {tool_name}, "
                f"chain fully skipped"
            )
            if deps.compat is not None:
                deps.compat.add_message("user",
                    f"[CHAIN GATE] Lightweight validation confirmed UNCHANGED — "
                    f"full chain skipped. Saved ~180s."
                )
    except Exception as e:
        logger.warning(f"[chain-gate] Lightweight validation failed: {e}, falling back to full chain")
        # If validation itself fails, run the full chain as fallback
        skill_result = await call_tool_fn(
            tool_name, {},
            deps.rapidwright_session, deps.vivado_session,
            design_size_factor=state.timing.design_size_factor,
        )
        skill_data = json.loads(skill_result) if isinstance(skill_result, str) else {}
        await _execute_chain_actions(state, deps, tool_name,
                                     skill_data if isinstance(skill_data, dict) else {},
                                     tools_called)


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
            design_size_factor=state.timing.design_size_factor,
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
                design_size_factor=state.timing.design_size_factor,
            )
            summary = summarize_tool_result(
                target_tool, raw_result,
                latest_wns=state.timing.latest_wns,
                latest_tns=state.timing.latest_tns,
                latest_failing_endpoints=state.timing.latest_failing_endpoints,
                prev_best_wns=state.timing.prev_best_wns,
            )
            # Determine chain step status from raw_result (JSON error check)
            step_failed = False
            try:
                parsed = json.loads(raw_result) if isinstance(raw_result, str) else {}
                if isinstance(parsed, dict) and "error" in parsed:
                    step_failed = True
            except (json.JSONDecodeError, TypeError):
                pass
            status_label = "failed" if step_failed else "completed"
            if deps.compat is not None:
                deps.compat.add_message("user",
                    f"[AUTO-CHAIN] After {tool_name}: {target_tool} {status_label} — {summary[:400]}")
            if step_failed:
                raise RuntimeError(f"{target_tool} reported error in result: {summary[:200]}")
            _track_wns_from_result(state, target_tool, raw_result)

            # Update current_dcp_path after opening a new checkpoint
            if target_tool == "vivado_open_checkpoint" and "dcp_path" in args:
                state.control.current_dcp_path = Path(args["dcp_path"]).resolve()

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
                        design_size_factor=state.timing.design_size_factor,
                    )
                    state.control.current_dcp_path = Path(pre_chain_path).resolve()
                    # Refresh WNS after restore so state matches Vivado
                    try:
                        restore_result = await call_tool_fn(
                            "vivado_report_timing_summary", {},
                            deps.rapidwright_session, deps.vivado_session,
                            design_size_factor=state.timing.design_size_factor,
                        )
                        restore_wns = parse_timing_summary(restore_result)
                        if restore_wns is not None:
                            state.timing.latest_wns = restore_wns
                            logger.info(f"[chain] Post-restore WNS: {restore_wns:.3f}")
                        else:
                            state.timing.latest_wns = None
                            logger.warning("[chain] Could not parse timing after restore")
                    except Exception as timing_err:
                        state.timing.latest_wns = None
                        logger.warning(f"[chain] Timing report after restore failed: {timing_err}")
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
        design_size_factor=state.timing.design_size_factor,
    )
    cell_paths = parse_critical_path_cells(result)
    if cell_paths:
        update_critical_paths(state, cell_paths, iteration=state.iteration.current)
        state.timing.critical_paths_stale = False
        logger.info(f"[EXECUTE] Auto-refreshed {len(cell_paths)} critical paths")
    # cell_paths 为空时不修改 stale 标志 — 下次触发时重试


def _compute_adaptive_pblock_multiplier(state) -> float:
    """Compute adaptive PBLOCK resource_multiplier using Formula C.

    M = max(1.10, 1.2 + util_local x 0.3 - 0.1 x log10(N_LUT))
    Clamp: [1.10, 1.50]

    Uses global utilization as fallback when local utilization is unavailable.
    """
    # Local utilization (preferred) — fallback to global if not available
    util = _get_local_pblock_utilization(state)
    if util is None:
        lut_used = state.timing.resource_utilization.get("LUT", 0) if state.timing.resource_utilization else 0
        lut_total = state.timing.device_capacity.get("LUT", 1) if state.timing.device_capacity else 1
        util = lut_used / max(lut_total, 1)

    # Size-aware decay: larger modules need less relative multiplier
    lut_count = state.timing.resource_utilization.get("LUT", 0) if state.timing.resource_utilization else 0
    size_penalty = 0.1 * math.log10(max(lut_count, 1))

    multiplier = 1.2 + util * 0.3 - size_penalty
    return max(1.10, min(1.50, multiplier))


def _get_local_pblock_utilization(state) -> float | None:
    """Extract local utilization within the critical-path bounding box.

    TODO: Implement by querying cell coordinates from RapidWright.
    Returns None to fall back to global utilization.
    """
    return None
