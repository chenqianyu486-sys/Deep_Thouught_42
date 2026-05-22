"""ANALYZE phase: multi-dimensional timing analysis.

Gathers timing, placement, congestion, and fanout data to identify
dominant obstacles. Only analysis tools are exposed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from optimizer.state import OptimizerState, LLMCallRecord
from optimizer.deps import NodeDeps
from optimizer.edges import NodeName
from optimizer.pure.tool_filter import LoopPhase, PHASE_MAX_ROUNDS, filter_tools_for_phase
from optimizer.pure.tool_summary import summarize_tool_result
from optimizer.pure.tool_router import call_tool as call_tool_fn
from optimizer.pure.step_state import extract_step_state
from optimizer.pure.timing import parse_timing_summary, is_valid_wns
from optimizer.pure.constants import WNS_TARGET_THRESHOLD, DASHBOARD_REFRESH_MAP
from optimizer.nodes.subgraphs.phase_handoff import build_phase_handoff, transition_phase
from optimizer.pure.context_snapshot import inject_merged_dashboard
from optimizer.color import green, yellow

logger = logging.getLogger(__name__)


async def run_analyze_phase(state: OptimizerState, deps: NodeDeps) -> LoopPhase:
    """Run the ANALYZE phase: gather multi-dimensional timing data.

    Returns:
        LoopPhase.SELECT_STRATEGY (always, even if analysis was incomplete).
    """
    max_rounds = PHASE_MAX_ROUNDS.get(LoopPhase.ANALYZE, 12)
    tool_round = 0
    tools_called: list[str] = []
    llm_summary = ""

    while True:
        tool_round += 1
        state.iteration.tool_round = tool_round

        # Check exit conditions
        if _check_phase_exit(state, tool_round, max_rounds):
            break

        # Call LLM with phase-filtered tools
        phase_tools = filter_tools_for_phase(deps.tools, LoopPhase.ANALYZE)
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

            # ANALYZE_DONE -> exit phase
            if flow_signal == "ANALYZE_DONE":
                llm_summary = assistant_content
                logger.info(green(f"[ANALYZE] LLM signaled ANALYZE_DONE at round {tool_round}"))
                break

            # Terminal signals: forward to outer loop
            if flow_signal in ("DONE", "EXHAUSTED"):
                logger.info(f"[ANALYZE] Terminal signal {flow_signal}, exiting")
                state.control.done_reason = flow_signal
                return LoopPhase.EVALUATE  # Skip to evaluate for final decision

        else:
            state.context.step_state_misses += 1
            if deps.compat is not None:
                deps.compat.add_message("user", "[NOTE] report_step_state missing. Include it next turn.")

        # Execute analysis tools
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
                logger.info(f"[ANALYZE] {tool_name} completed in {tool_elapsed:.1f}s")

                # Summarize and inject
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

                if deps.compat is not None:
                    deps.compat.add_message("tool", summary, {
                        "tool_call_id": tc.id, "name": tool_name,
                    })

                # Track WNS
                _track_wns_from_result(state, tool_name, result)

                # Track dashboard freshness
                refreshable = DASHBOARD_REFRESH_MAP.get(tool_name)
                if refreshable:
                    state.timing.refreshed_fields |= refreshable

            continue

        # No tool calls — check if we should continue
        if _check_wns_target_met(state):
            state.control.is_done = True
            state.control.done_reason = "wns_target_met"
            return LoopPhase.EVALUATE

    # Phase exit: build handoff and transition
    llm_summary = llm_summary or assistant_content or "Analysis phase completed."
    handoff = build_phase_handoff(
        source_phase=LoopPhase.ANALYZE,
        llm_summary=llm_summary,
        wns=state.timing.latest_wns,
        tns=state.timing.latest_tns,
        failing_endpoints=state.timing.latest_failing_endpoints,
        tools_called=tools_called,
        message_count=tool_round,
    )
    state.strategy.analysis_summary = llm_summary
    state.strategy.last_handoff_text = handoff.to_phase_context_string()
    await transition_phase(deps, LoopPhase.ANALYZE, LoopPhase.SELECT_STRATEGY, handoff)
    return LoopPhase.SELECT_STRATEGY


def _check_phase_exit(state: OptimizerState, tool_round: int, max_rounds: int) -> bool:
    """Check common exit conditions for the phase loop."""
    if tool_round > max_rounds:
        logger.warning(f"[ANALYZE] Max rounds reached ({tool_round} > {max_rounds})")
        return True

    if state.control.start_time:
        elapsed = time.time() - state.control.start_time
        if elapsed > state.control.wall_clock_timeout:
            logger.warning(f"[ANALYZE] Wall-clock timeout: {elapsed:.0f}s")
            state.control.is_done = True
            state.control.done_reason = "wall_clock_timeout"
            return True

    if state.control.user_exit_requested:
        logger.info("[ANALYZE] User exit requested")
        return True

    if state.cost.total_cost >= state.cost.cost_hard_limit:
        logger.warning(f"[ANALYZE] Cost limit reached")
        state.control.is_done = True
        state.control.done_reason = "cost_limit"
        return True

    return False


async def _call_phase_llm(state, deps, phase_tools, max_retries=3, retry_delay=2.0):
    """Call LLM with phase-specific tools and retry logic."""
    if deps.openai_client is None or deps.compat is None:
        return None

    try:
        api_messages = deps.compat.get_formatted_for_api()
    except Exception:
        return None

    # Inject merged handoff + dashboard as last user message
    inject_merged_dashboard(api_messages, state, LoopPhase.ANALYZE)

    model = state.model.current_model
    state.model.llm_call_count += 1

    # Build reasoning config
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
            response = await deps.openai_client.chat.completions.create(**kwargs)
            return response
        except Exception as e:
            last_exception = e
            error_str = str(e)
            if "429" in error_str:
                fallback = _get_fallback_model(state, model)
                if fallback and fallback != model:
                    logger.warning(f"[ANALYZE] Rate limit, fallback: {model} -> {fallback}")
                    state.model.current_model = fallback
                    model = fallback
                    wait_time = retry_delay * (2 ** retry)
                    await asyncio.sleep(wait_time)
                    continue
            if retry < max_retries - 1:
                wait_time = retry_delay * (2 ** retry)
                logger.warning(f"[ANALYZE] Retry {retry+1}/{max_retries}: {e}")
                await asyncio.sleep(wait_time)

    if last_exception:
        raise last_exception
    return None


def _track_wns_from_result(state: OptimizerState, tool_name: str, raw_result: str) -> None:
    """Parse WNS from timing tool results."""
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
