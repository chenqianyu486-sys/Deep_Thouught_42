"""EVALUATE phase: assess the strategy's effect on WNS.

Compares before/after WNS from the EXECUTE phase and decides the
next action: NEXT_ITERATION, SWITCH_STRATEGY, DONE, or CONTINUE.
"""

from __future__ import annotations

import json
import logging
import time

from optimizer.state import OptimizerState, PhaseEntry, FlowControlRecord, LLMCallRecord
from optimizer.deps import NodeDeps
from optimizer.edges import NodeName
from optimizer.pure.tool_filter import LoopPhase, PHASE_MAX_ROUNDS, filter_tools_for_phase
from optimizer.pure.tool_summary import summarize_tool_result
from optimizer.pure.tool_router import call_tool as call_tool_fn
from optimizer.pure.step_state import extract_step_state
from optimizer.pure.timing import parse_timing_summary, is_valid_wns
from optimizer.pure.constants import WNS_TARGET_THRESHOLD, DASHBOARD_REFRESH_MAP, WNS_ROLLBACK_THRESHOLD
from optimizer.nodes.subgraphs.phase_handoff import build_phase_handoff, transition_phase
from optimizer.pure.context_snapshot import inject_merged_dashboard
from optimizer.color import green, yellow

logger = logging.getLogger(__name__)


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


async def run_evaluate_phase(state: OptimizerState, deps: NodeDeps) -> LoopPhase:
    """Run the EVALUATE phase: assess strategy results and decide next action.

    Returns:
        LoopPhase.ANALYZE if continuing/restarting, or signals exit via state.
    """
    max_rounds = PHASE_MAX_ROUNDS.get(LoopPhase.EVALUATE, 8)
    tool_round = 0
    tools_called: list[str] = []
    llm_summary = ""

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

    # ── Auto-detect WNS regression and request rollback ──
    if detect_rollback_needed(state):
        logger.warning(yellow(
            f"[EVALUATE] WNS regression detected: latest={state.timing.latest_wns:.3f} "
            f"< best={state.timing.best_wns:.3f} (threshold={WNS_ROLLBACK_THRESHOLD:.3f})"
        ))
        state.strategy.current_phase = ""
        state.strategy.current_strategy = ""
        state.control.done_reason = "rollback"
        _record_flow_signal(state, "ROLLBACK", "rollback_auto")
        return LoopPhase.ANALYZE

    while True:
        tool_round += 1
        state.iteration.tool_round = tool_round

        if _check_phase_exit(state, tool_round, max_rounds):
            break

        # Call LLM with evaluation tools
        phase_tools = filter_tools_for_phase(deps.tools, LoopPhase.EVALUATE)
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
                _record_flow_signal(state, "ROLLBACK", "rollback_llm")
                state.control.done_reason = "rollback"
                state.strategy.current_phase = ""
                state.strategy.current_strategy = ""
                return LoopPhase.ANALYZE

            elif flow_signal == "CONTINUE":
                logger.info("[EVALUATE] LLM chose CONTINUE — re-entering ANALYZE")
                # Reset phase tracking for re-analysis
                state.strategy.current_phase = ""
                state.strategy.current_strategy = ""
                return LoopPhase.ANALYZE

            elif flow_signal == "EXHAUSTED":
                _handle_exhausted(state, deps)
                return LoopPhase.ANALYZE

            # Unknown signal: treat as CONTINUE
            logger.info(f"[EVALUATE] Unknown flow_signal: {flow_signal}, treating as CONTINUE")
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

                result = await call_tool_fn(
                    tool_name=tool_name, arguments=tool_args,
                    rapidwright_session=deps.rapidwright_session,
                    vivado_session=deps.vivado_session,
                )
                summary = summarize_tool_result(
                    tool_name, result,
                    latest_wns=state.timing.latest_wns,
                    latest_tns=state.timing.latest_tns,
                    latest_failing_endpoints=state.timing.latest_failing_endpoints,
                    prev_best_wns=state.timing.prev_best_wns,
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
        if deps.compat is not None:
            wns = state.timing.latest_wns
            wns_str = f"{wns:.3f}ns" if wns is not None else "unknown"
            deps.compat.add_message("user",
                f"[NOTE] Current WNS: {wns_str}. Please decide: "
                "NEXT_ITERATION, SWITCH_STRATEGY, DONE, or CONTINUE via report_step_state.")

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

    _record_flow_signal(state, "DONE", state.control.done_reason or "done")


def _handle_next_iteration(state: OptimizerState, deps, assistant_content: str) -> None:
    """Handle NEXT_ITERATION signal — success, start fresh iteration."""
    logger.info(green("[EVALUATE] LLM signaled NEXT_ITERATION"))
    state.control.done_reason = "iteration_success"
    _record_flow_signal(state, "NEXT_ITERATION", "iteration_success")


def _handle_switch_strategy(state: OptimizerState, deps, assistant_content: str) -> None:
    """Handle SWITCH_STRATEGY signal — current strategy failed."""
    logger.info(yellow("[EVALUATE] LLM signaled SWITCH_STRATEGY"))
    state.control.done_reason = "switch_strategy"
    if deps.compat is not None:
        current_wns = state.timing.latest_wns
        wns_str = f"{current_wns:.3f}ns" if current_wns is not None else "unknown"
        deps.compat.add_message("user",
            f"[STRATEGY SWITCH] Previous strategy ({state.strategy.current_strategy}) ended. "
            f"WNS={wns_str}. New iteration starts with fresh context. "
            f"Failed strategies are listed in the dashboard.")
    _record_flow_signal(state, "SWITCH_STRATEGY", "switch_strategy")


def _handle_exhausted(state: OptimizerState, deps) -> None:
    """Handle EXHAUSTED signal — all strategies exhausted."""
    logger.info(yellow("[EVALUATE] LLM signaled EXHAUSTED"))
    state.control.is_done = True
    state.control.done_reason = "strategies_exhausted"
    _record_flow_signal(state, "EXHAUSTED", "strategies_exhausted")


def _record_flow_signal(state: OptimizerState, signal: str, reason: str) -> None:
    """Record flow control decision for trajectory tracking."""
    record = FlowControlRecord(
        signal=signal,
        iteration=state.iteration.current,
        tool_round=state.iteration.tool_round,
        done_reason=reason,
        wns_at_decision=state.timing.latest_wns,
    )
    state.context.flow_control_log.append(record)
    if len(state.context.flow_control_log) > state.context.flow_control_log_max:
        state.context.flow_control_log = state.context.flow_control_log[-state.context.flow_control_log_max:]


def _check_phase_exit(state: OptimizerState, tool_round: int, max_rounds: int) -> bool:
    if tool_round > max_rounds:
        logger.warning(f"[EVALUATE] Max rounds reached ({tool_round} > {max_rounds})")
        return True
    if state.control.user_exit_requested:
        return True
    return False


async def _call_phase_llm(state, deps, phase_tools):
    """Call LLM with evaluation tools."""
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
        logger.error(f"[EVALUATE] LLM call failed: {e}")
        return None
