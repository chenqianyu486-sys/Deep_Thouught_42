"""EVALUATE phase: assess the strategy's effect on WNS.

Compares before/after WNS from the EXECUTE phase and decides the
next action: NEXT_ITERATION, SWITCH_STRATEGY, DONE, or CONTINUE.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from optimizer.state import (
    OptimizerState,
    PhaseEntry,
    LLMCallRecord,
    record_flow_signal,
)
from optimizer.deps import NodeDeps
from optimizer.edges import NodeName
from optimizer.pure.tool_filter import LoopPhase, PHASE_MAX_ROUNDS, filter_tools_for_phase
from optimizer.pure.tool_summary import summarize_tool_result
from optimizer.pure.tool_router import call_tool as call_tool_fn
from optimizer.pure.model_select import classify_task
from optimizer.pure.step_state import extract_step_state
from optimizer.pure.timing import parse_timing_summary, is_valid_wns
from optimizer.pure.critical_path import refresh_violation_summary
from optimizer.pure.constants import WNS_TARGET_THRESHOLD, DASHBOARD_REFRESH_MAP, WNS_ROLLBACK_THRESHOLD, PHASE_TOOL_RATE_LIMITS, build_llm_extra_body
from optimizer.nodes.subgraphs.phase_handoff import build_phase_handoff, transition_phase
from optimizer.pure.context_snapshot import inject_merged_dashboard
from optimizer.color import green, yellow

logger = logging.getLogger(__name__)

STRATEGY_IMPROVEMENT_EPSILON_NS = 0.001


def detect_rollback_needed(state: OptimizerState) -> bool:
    """Check if current WNS has regressed significantly from best WNS.

    Returns True when latest_wns is below best_wns by more than the
    WNS_ROLLBACK_THRESHOLD and a best checkpoint exists on disk.
    """
    if (state.timing.latest_wns is None
            or state.timing.best_wns == float('-inf')
            or state.control.best_checkpoint_path is None
            or not state.control.best_checkpoint_path.exists()):
        return False
    return state.timing.latest_wns < state.timing.best_wns - WNS_ROLLBACK_THRESHOLD


def _strategy_wns_delta_since_entry(state: OptimizerState) -> float | None:
    """Return best WNS gain since the current strategy entered EXECUTE.

    Auto-chains can update ``best_wns`` before ``latest_wns`` catches up, so
    cooldown decisions must use the best result produced during the attempt.
    """
    strategy = state.strategy.current_strategy
    best_wns = state.timing.best_wns
    if not strategy or best_wns == float('-inf'):
        return None

    for entry in reversed(state.strategy.phase_history):
        if (entry.phase == "EXECUTE_STRATEGY"
                and entry.strategy == strategy
                and entry.iteration == state.iteration.current
                and entry.wns_at_entry is not None):
            return best_wns - entry.wns_at_entry
    return None


def _cool_down_current_strategy_if_stalled(
    state: OptimizerState,
    detail: str,
) -> bool:
    """Block a switched strategy only when its measured WNS did not improve.

    Skips cooldown when tool errors occurred during this iteration's EXECUTE
    phase — a tool crash (e.g. MCP server exception) means the strategy never
    got a fair execution chance, so blocking it would penalize the strategy
    for an infrastructure failure rather than a strategy deficiency. The
    strategy remains selectable for retry within the same iteration.
    """
    strategy = state.strategy.current_strategy
    delta = _strategy_wns_delta_since_entry(state)
    if not strategy or delta is None or delta > STRATEGY_IMPROVEMENT_EPSILON_NS:
        return False

    # Don't cool down if tools crashed — strategy didn't actually execute.
    # tool_errors is cleared at iteration_start and accumulates during EXECUTE,
    # so any entries here belong to the current strategy's execution attempt.
    if state.iteration.tool_errors:
        logger.info(
            f"[EVALUATE] Skipping cooldown for '{strategy}' — "
            f"{len(state.iteration.tool_errors)} tool error(s) detected this iteration "
            f"(strategy did not get a fair execution chance; delta={delta:+.3f}ns)"
        )
        return False

    if strategy not in state.iteration.blocked_strategies:
        state.iteration.blocked_strategies.append(strategy)
    logger.info(
        f"[EVALUATE] Cooling down stalled strategy '{strategy}' for this iteration "
        f"(delta={delta:+.3f}ns; {detail})"
    )
    return True


async def run_evaluate_phase(state: OptimizerState, deps: NodeDeps) -> LoopPhase:
    """Run the EVALUATE phase: assess strategy results and decide next action.

    Returns:
        LoopPhase.ANALYZE if continuing/restarting, or signals exit via state.
    """
    max_rounds = PHASE_MAX_ROUNDS.get(LoopPhase.EVALUATE, 8)
    tool_round = 0
    tools_called: list[str] = []
    llm_summary = ""
    state.context.tool_phase_call_counts.clear()
    state.context.consecutive_empty_responses = 0

    # Record phase entry
    phase_entry = PhaseEntry(
        phase="EVALUATE",
        strategy=state.strategy.current_strategy,
        iteration=state.iteration.current,
        tool_round=0,
        wns_at_entry=state.timing.latest_wns,
    )
    state.strategy.phase_history.append(phase_entry)
    if len(state.strategy.phase_history) > 100:
        state.strategy.phase_history = state.strategy.phase_history[-100:]
    state.strategy.current_phase = "EVALUATE"

    # ── Handle pre-check regression (Vivado state unchanged) ──
    # Level 1 pre-check in EXECUTE detected directional WNS regression
    # and skipped the entire Vivado P&R chain. No rollback is needed
    # because Vivado was never touched — just auto-switch strategy.
    if state.control.done_reason == "precheck_direction_regress":
        logger.info(
            f"[EVALUATE] Pre-check rejected strategy '{state.strategy.current_strategy}' — "
            f"auto-switching (design state unchanged, no rollback needed)"
        )
        strategy = state.strategy.current_strategy
        if strategy and strategy not in state.iteration.blocked_strategies:
            state.iteration.blocked_strategies.append(strategy)
        state.strategy.current_phase = ""
        state.strategy.current_strategy = ""
        state.control.done_reason = ""
        record_flow_signal(state, "SWITCH_STRATEGY", "precheck_regress", phase="EVALUATE")
        if deps.compat is not None:
            deps.compat.add_message("user",
                "[SYSTEM — Pre-check Regression]\n"
                "The previous strategy was rejected by the RapidWright timing pre-check — "
                "the modification appeared to directionally regress WNS. "
                "No Vivado P&R was run, so the design state is unchanged. "
                "Continuing analysis from the current best state."
            )
        return LoopPhase.ANALYZE

    # ── Auto-detect WNS regression and request rollback ──
    if detect_rollback_needed(state):
        logger.warning(yellow(
            f"[EVALUATE] WNS regression detected: latest={state.timing.latest_wns:.3f} "
            f"< best={state.timing.best_wns:.3f} (threshold={WNS_ROLLBACK_THRESHOLD:.3f})"
        ))
        state.strategy.current_phase = ""
        state.strategy.current_strategy = ""
        state.control.done_reason = "rollback"
        state.control.post_rollback_analyze = True
        record_flow_signal(state, "ROLLBACK", "rollback_auto", phase="EVALUATE")
        return LoopPhase.ANALYZE

    while True:
        tool_round += 1
        state.iteration.tool_round = tool_round

        if _check_phase_exit(state, tool_round, max_rounds):
            break

        # Call LLM with evaluation tools
        phase_tools = filter_tools_for_phase(deps.tools, LoopPhase.EVALUATE)
        try:
            response = await _call_phase_llm(state, deps, phase_tools)
        except Exception as e:
            logger.error(f"[EVALUATE] LLM call failed after retries: {e}")
            _handle_switch_strategy(state, deps, f"LLM error: {e}")
            return LoopPhase.ANALYZE
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

        # Extract step state
        step_state = extract_step_state(message)
        state.control.step_state = step_state

        if step_state:
            state.context.step_state_misses = 0
            flow_signal = step_state.flow_control
            llm_summary = assistant_content

            # Handle terminal signals
            if flow_signal == "DONE":
                _handle_done(state, deps, assistant_content)
                return LoopPhase.ANALYZE  # Will be caught by outer loop

            elif flow_signal == "NEXT_ITERATION":
                _handle_next_iteration(state, deps, assistant_content)
                return LoopPhase.ANALYZE  # Will be caught by outer loop

            elif flow_signal == "SWITCH_STRATEGY":
                _handle_switch_strategy(state, deps, assistant_content)
                return LoopPhase.ANALYZE  # Restart analysis for new strategy

            elif flow_signal == "ROLLBACK":
                logger.warning(yellow("[EVALUATE] LLM signaled ROLLBACK"))
                record_flow_signal(state, "ROLLBACK", "rollback_llm", phase="EVALUATE",
                                   result_status=step_state.result_status or "")
                state.control.done_reason = "rollback"
                state.control.post_rollback_analyze = True
                state.strategy.current_phase = ""
                state.strategy.current_strategy = ""
                return LoopPhase.ANALYZE

            elif flow_signal == "CONTINUE":
                logger.info("[EVALUATE] LLM chose CONTINUE — re-entering ANALYZE")
                record_flow_signal(state, "CONTINUE", "re_analyze", phase="EVALUATE",
                                   result_status=step_state.result_status or "")
                # Reset phase tracking for re-analysis
                state.strategy.current_phase = ""
                state.strategy.current_strategy = ""
                return LoopPhase.ANALYZE

            elif flow_signal == "EXHAUSTED":
                _handle_exhausted(state, deps)
                return LoopPhase.ANALYZE

            elif flow_signal == "EXEC_DONE":
                # LLM sometimes sends EXEC_DONE from EVALUATE (confused with EXECUTE phase).
                # Treat as NEXT_ITERATION — execution is done, proceed to next iteration.
                logger.info("[EVALUATE] EXEC_DONE received in EVALUATE phase, treating as NEXT_ITERATION")
                _handle_next_iteration(state, deps, llm_summary or "Execution complete (EXEC_DONE from EVALUATE).")
                state.strategy.current_phase = ""
                state.strategy.current_strategy = ""
                return LoopPhase.ANALYZE

            elif flow_signal == "ANALYZE_DONE":
                # LLM confused about phase — ANALYZE_DONE is only valid in ANALYZE.
                # Map to SWITCH_STRATEGY (not CONTINUE) so the stalled strategy is
                # cooled down and the multi-strategy loop advances. CONTINUE would
                # skip cooldown and re-enter a full ANALYZE cycle, wasting rounds.
                logger.info("[EVALUATE] ANALYZE_DONE from EVALUATE (phase confusion), treating as SWITCH_STRATEGY")
                _handle_switch_strategy(state, deps, assistant_content)
                return LoopPhase.ANALYZE

            # Unknown signal: treat as CONTINUE
            logger.warning(f"[EVALUATE] Unknown flow_signal: {flow_signal}, treating as CONTINUE")
            return LoopPhase.ANALYZE

        else:
            state.context.step_state_misses += 1
            if deps.compat is not None:
                deps.compat.add_message("user",
                    "[NOTE] report_step_state required. Decide: NEXT_ITERATION, SWITCH_STRATEGY, DONE, or CONTINUE.")

        # Execute any evaluation tools
        if message.tool_calls:
            for tc in message.tool_calls:
                if not tc.function:
                    continue
                tool_name = tc.function.name
                tools_called.append(tool_name)
                try:
                    tool_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    tool_args = {}
                task_type = classify_task(tool_name, tool_args)
                if task_type == "optimization" or (
                    task_type != "unknown" and state.model.current_task_type != "optimization"
                ):
                    state.model.current_task_type = task_type

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
                        elif tool_name == "vivado_get_cached_high_fanout_nets":
                            result += (
                                "High-fanout net data is already in Dashboard Module 4 "
                                "(Netlist Quality). Do not re-fetch — use the Dashboard values."
                            )
                        elif tool_name == "vivado_check_design_status":
                            result += (
                                "Design placement/routing status is shown in Dashboard Module 1 "
                                "(Global State, current_stage field). Do not re-check."
                            )
                        if deps.compat is not None:
                            deps.compat.add_message("tool", result, {
                                "tool_call_id": tc.id, "name": tool_name,
                            })
                        continue

                result = await call_tool_fn(
                    tool_name=tool_name, arguments=tool_args,
                    rapidwright_session=deps.rapidwright_session,
                    vivado_session=deps.vivado_session,
                    tool_cache=state.context.tool_cache,
                    design_size_factor=state.timing.design_size_factor,
                )
                summary = summarize_tool_result(
                    tool_name, result,
                    latest_wns=state.timing.latest_wns,
                    latest_tns=state.timing.latest_tns,
                    latest_failing_endpoints=state.timing.latest_failing_endpoints,
                    prev_best_wns=state.timing.prev_best_wns,
                    prev_best_tns=state.timing.prev_best_tns,
                )
                if deps.compat is not None:
                    deps.compat.add_message("tool", summary, {
                        "tool_call_id": tc.id, "name": tool_name,
                    })

                # Track WNS from timing tools
                if tool_name in ("vivado_report_timing_summary", "vivado_get_wns"):
                    timing = parse_timing_summary(result)
                    wns = timing.get("wns")
                    if wns is not None:
                        state.timing.latest_wns = wns
                        tns = timing.get("tns")
                        if tns is not None:
                            state.timing.latest_tns = tns
                        fe = timing.get("failing_endpoints")
                        if fe is not None:
                            state.timing.latest_failing_endpoints = fe
                            refresh_violation_summary(state)
                        if wns > state.timing.best_wns:
                            state.timing.best_wns = wns
                            state.timing.best_wns_iteration = state.iteration.current
                            state.control.needs_save = True
                    # Track dashboard freshness
                    refreshable = DASHBOARD_REFRESH_MAP.get(tool_name)
                    if refreshable:
                        state.timing.refreshed_fields |= refreshable

            continue

        # No tool calls, no decision — prompt again
        if not assistant_content.strip() and not message.tool_calls:
            state.context.consecutive_empty_responses += 1
            if state.context.consecutive_empty_responses >= 3:
                logger.warning(
                    f"[EVALUATE] {state.context.consecutive_empty_responses} consecutive "
                    f"empty responses, forcing SWITCH_STRATEGY"
                )
                _handle_switch_strategy(state, deps, "Empty responses — forcing strategy switch")
                return LoopPhase.ANALYZE
        else:
            state.context.consecutive_empty_responses = 0

        if deps.compat is not None:
            wns = state.timing.latest_wns
            wns_str = f"{wns:.3f}ns" if wns is not None else "unknown"
            deps.compat.add_message("user",
                f"[NOTE] Current WNS: {wns_str}. Please make a decision via report_step_state.")

    # Fallback: max rounds reached, default to SWITCH_STRATEGY
    logger.info(f"[EVALUATE] Max rounds reached, defaulting to SWITCH_STRATEGY")
    _handle_switch_strategy(state, deps, llm_summary or "Evaluation phase timeout.")
    return LoopPhase.ANALYZE


def _handle_done(state: OptimizerState, deps, assistant_content: str) -> None:
    """Handle DONE signal from LLM."""
    logger.info(green("[EVALUATE] LLM signaled DONE"))
    if (
        state.timing.latest_wns is not None
        and state.timing.latest_wns >= WNS_TARGET_THRESHOLD
        and is_valid_wns(state.timing.latest_wns, state.timing.clock_period, state.timing.best_wns)
    ):
        state.control.is_done = True
        state.control.done_reason = "wns_target_met"
    else:
        state.control.done_reason = "flow_control_done_next_iteration"

    record_flow_signal(state, "DONE", state.control.done_reason or "done", phase="EVALUATE",
                       result_status=state.control.step_state.result_status if state.control.step_state else "")


def _handle_next_iteration(state: OptimizerState, deps, assistant_content: str) -> None:
    """Handle NEXT_ITERATION signal — success, start fresh iteration."""
    logger.info(green("[EVALUATE] LLM signaled NEXT_ITERATION"))
    state.control.done_reason = "iteration_success"
    record_flow_signal(state, "NEXT_ITERATION", "iteration_success", phase="EVALUATE",
                       result_status=state.control.step_state.result_status if state.control.step_state else "")


def _handle_switch_strategy(state: OptimizerState, deps, assistant_content: str) -> None:
    """Handle SWITCH_STRATEGY signal — current strategy failed."""
    logger.info(yellow("[EVALUATE] LLM signaled SWITCH_STRATEGY"))
    _cool_down_current_strategy_if_stalled(
        state,
        detail="EVALUATE switched away from strategy",
    )
    state.control.done_reason = "switch_strategy"
    if deps.compat is not None:
        current_wns = state.timing.latest_wns
        wns_str = f"{current_wns:.3f}ns" if current_wns is not None else "unknown"
        deps.compat.add_message("user",
            f"[STRATEGY SWITCH] Previous strategy ({state.strategy.current_strategy}) ended. "
            f"WNS={wns_str}. New iteration starts with fresh context. "
            f"Failed strategies are listed in the dashboard.")
    record_flow_signal(state, "SWITCH_STRATEGY", "switch_strategy", phase="EVALUATE",
                       result_status=state.control.step_state.result_status if state.control.step_state else "")


def _handle_exhausted(state: OptimizerState, deps) -> None:
    """Handle EXHAUSTED signal — all strategies exhausted."""
    logger.info(yellow("[EVALUATE] LLM signaled EXHAUSTED"))
    state.control.is_done = True
    state.control.done_reason = "strategies_exhausted"
    record_flow_signal(state, "EXHAUSTED", "strategies_exhausted", phase="EVALUATE",
                       result_status=state.control.step_state.result_status if state.control.step_state else "")


def _check_phase_exit(state: OptimizerState, tool_round: int, max_rounds: int) -> bool:
    if tool_round > max_rounds:
        logger.warning(f"[EVALUATE] Max rounds reached ({tool_round} > {max_rounds})")
        return True
    if state.control.user_exit_requested:
        return True
    return False


async def _call_phase_llm(state, deps, phase_tools, max_retries=3, retry_delay=2.0):
    """Call LLM with evaluation tools and retry logic."""
    if deps.openai_client is None or deps.compat is None:
        return None

    try:
        api_messages = deps.compat.get_formatted_for_api()
    except Exception:
        return None

    # Inject merged handoff + dashboard as last user message
    inject_merged_dashboard(api_messages, state, LoopPhase.EVALUATE)

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
                    response=response, phase="EVALUATE",
                )
            return response
        except Exception as e:
            last_exception = e
            error_str = str(e)
            if "429" in error_str:
                fallback = _get_fallback_model(state, model)
                if fallback and fallback != model:
                    logger.warning(f"[EVALUATE] Rate limit, fallback: {model} -> {fallback}")
                    state.model.current_model = fallback
                    model = fallback
                    wait_time = retry_delay * (2 ** retry)
                    await asyncio.sleep(wait_time)
                    continue
            if retry < max_retries - 1:
                wait_time = retry_delay * (2 ** retry)
                logger.warning(f"[EVALUATE] Retry {retry+1}/{max_retries}: {e}")
                await asyncio.sleep(wait_time)

    if last_exception:
        raise last_exception
    return None


def _get_fallback_model(state: OptimizerState, current_model: str) -> str | None:
    """Get fallback model for rate-limit recovery (mirrors ANALYZE logic)."""
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
