"""SELECT_STRATEGY phase: choose the optimal optimization strategy.

Receives the analysis summary from ANALYZE phase via handoff.
Exposes minimal tools — the LLM focuses purely on decision-making.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from optimizer.state import OptimizerState, PhaseEntry, LLMCallRecord, record_flow_signal
from optimizer.deps import NodeDeps
from optimizer.pure.tool_filter import LoopPhase, PHASE_MAX_ROUNDS, filter_tools_for_phase
from optimizer.pure.tool_router import call_tool as call_tool_fn
from optimizer.pure.tool_summary import summarize_tool_result
from optimizer.pure.model_select import classify_task
from optimizer.pure.step_state import extract_step_state
from optimizer.pure.timing import parse_timing_summary
from optimizer.nodes.subgraphs.phase_handoff import build_phase_handoff, transition_phase
from optimizer.pure.context_snapshot import inject_merged_dashboard, inject_pinned_cell_registry, extract_system_message
from optimizer.pure.constants import build_llm_extra_body
from optimizer.pure.constants import DASHBOARD_REFRESH_MAP, DESIGN_MODIFICATION_TOOLS
from optimizer.color import green, yellow

logger = logging.getLogger(__name__)


async def run_select_strategy_phase(state: OptimizerState, deps: NodeDeps) -> LoopPhase:
    """Run the SELECT_STRATEGY phase: choose a strategy based on analysis.

    Returns:
        LoopPhase.EXECUTE if a strategy was selected, or EVALUATE if exhausted.
    """


    # HARD RULE: First iteration ALWAYS tries PBLOCK (85% success, +0.532ns avg gain),
    # but only on the FIRST strategy selection of iteration 1 — skip if a strategy was
    # already selected this iteration (phase_history entry exists), or if PBLOCK is
    # currently blocked (iteration-level block or TTL-based ineffective block).
    _pblock_blocked = (
        "PBLOCK" in state.iteration.blocked_strategies
        or "PBLOCK" in _get_permanently_blocked_strategies(state)
    )
    _strategy_was_already_selected = any(
        e.iteration == state.iteration.current and e.phase == "SELECT_STRATEGY"
        for e in state.strategy.phase_history
    )
    if state.iteration.current <= 1 and not _strategy_was_already_selected and not _pblock_blocked:
        logger.info("[SELECT_STRATEGY] Override: forcing PBLOCK as first strategy")
        state.strategy.current_strategy = "PBLOCK"
    elif _pblock_blocked and state.iteration.current <= 1:
        logger.info("[SELECT_STRATEGY] Override suppressed: PBLOCK is currently blocked")
    elif _strategy_was_already_selected and state.iteration.current <= 1:
        logger.info("[SELECT_STRATEGY] Override suppressed: strategy already selected this iteration")
    state.strategy.current_phase = "SELECT_STRATEGY"
    max_rounds = PHASE_MAX_ROUNDS.get(LoopPhase.SELECT_STRATEGY, 6)
    tool_round = 0
    state.context.consecutive_empty_responses = 0
    tools_called: list[str] = []
    llm_summary = ""
    assistant_content = ""

    # ── Auto-refresh stale timing data on SELECT_STRATEGY entry ──
    if state.timing.field_freshness.get("timing_summary") == "stale":
        try:
            _refresh = await call_tool_fn(
                tool_name="vivado_report_timing_summary", arguments={},
                rapidwright_session=deps.rapidwright_session,
                vivado_session=deps.vivado_session,
                raw_tool_outputs=state.context.raw_tool_outputs,
                iteration=state.iteration.current,
                tool_round=0,
                tool_cache=state.context.tool_cache,
                design_size_factor=state.timing.design_size_factor,
                entity_registry=state.entity_registry,
            )
            _parsed = parse_timing_summary(_refresh)
            if _parsed and "wns" in _parsed:
                state.timing.latest_wns = _parsed["wns"]
                state.timing.latest_tns = _parsed.get("tns")
                state.timing.latest_failing_endpoints = _parsed.get("failing_endpoints")
                # Mark all fields refreshed by vivado_report_timing_summary
                # fresh (timing_summary AND cdc_paths) — matches the main-loop
                # DASHBOARD_REFRESH_MAP handling so cdc_paths does not stay
                # stale after the auto-refresh (F2).
                for _f in DASHBOARD_REFRESH_MAP.get("vivado_report_timing_summary", frozenset()):
                    state.timing.field_freshness[_f] = "fresh"
                logger.info(f"[SELECT_STRATEGY] Auto-refreshed stale WNS: {_parsed['wns']:.3f}ns")
        except Exception as _e:
            logger.warning(f"[SELECT_STRATEGY] Auto-refresh failed: {_e}")

    while True:
        tool_round += 1
        state.iteration.tool_round = tool_round
        _pending_signal: str | None = None  # defer exit until pending tools execute
        if _check_phase_exit(state, tool_round, max_rounds):
            break

        # Call LLM with minimal tools
        phase_tools = filter_tools_for_phase(deps.tools, LoopPhase.SELECT_STRATEGY)
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

            # EXHAUSTED: no more strategies to try
            if step_state.flow_control == "EXHAUSTED":
                logger.info("[SELECT_STRATEGY] LLM signaled EXHAUSTED")
                record_flow_signal(state, "EXHAUSTED", "strategies_exhausted",
                                   phase="SELECT_STRATEGY", result_status=step_state.result_status or "")
                state.control.is_done = True
                state.control.done_reason = "strategies_exhausted"
                if message.tool_calls:
                    _pending_signal = "EXHAUSTED"
                else:
                    return LoopPhase.EVALUATE

            # Strategy selected: check for strategy_name
            if step_state.strategy_name:
                # Guard: reject persistent TTL blocks and strategies that
                # already stalled during the current iteration.
                iteration_blocked = set(state.iteration.blocked_strategies)
                blocked = _get_permanently_blocked_strategies(state) | iteration_blocked
                chosen_key = step_state.strategy_name
                if chosen_key in blocked:
                    if chosen_key in iteration_blocked:
                        reason = "already stalled in this iteration"
                    else:
                        unblock_iter = 0
                        for entry in state.context.failed_strategies:
                            if (entry.strategy == chosen_key
                                    and entry.reason == "strategy_ineffective"):
                                unblock_iter = entry.blocked_until_iter
                                break
                        remaining = max(0, unblock_iter - state.iteration.current)
                        reason = f"temporarily ineffective; unblocks in {remaining} iterations"
                    logger.warning(yellow(
                        f"[SELECT_STRATEGY] Blocked strategy '{chosen_key}' — {reason}"
                    ))
                    if deps.compat is not None:
                        deps.compat.add_message("user",
                            f"[BLOCKED] Strategy '{chosen_key}' is unavailable: {reason}. "
                            f"Please select a different strategy from the available catalog.")
                    continue

                state.strategy.current_strategy = step_state.strategy_name
                state.strategy.strategy_rationale = assistant_content
                llm_summary = assistant_content
                logger.info(green(f"[SELECT_STRATEGY] Strategy selected: {step_state.strategy_name}"))
                record_flow_signal(state, "STRATEGY_SELECTED", "strategy_selected",
                                   phase="SELECT_STRATEGY", strategy=step_state.strategy_name,
                                   result_status=step_state.result_status or "")

                # Record phase transition
                phase_entry = PhaseEntry(
                    phase="SELECT_STRATEGY",
                    strategy=step_state.strategy_name,
                    iteration=state.iteration.current,
                    tool_round=state.iteration.tool_round,
                    wns_at_entry=state.timing.latest_wns,
                )
                state.strategy.phase_history.append(phase_entry)
                if len(state.strategy.phase_history) > 100:
                    state.strategy.phase_history = state.strategy.phase_history[-100:]
                state.strategy.current_phase = "SELECT_STRATEGY"
                if message.tool_calls:
                    _pending_signal = "STRATEGY_SELECTED"
                else:
                    break

        else:
            state.context.step_state_misses += 1
            if deps.compat is not None:
                deps.compat.add_message("user",
                    "[NOTE] report_step_state with strategy_name required.")

        # Execute any simple tools the LLM may have called (e.g. raw_tool_output)
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

                result = await call_tool_fn(
                    tool_name=tool_name, arguments=tool_args,
                    rapidwright_session=deps.rapidwright_session,
                    vivado_session=deps.vivado_session,
                    tool_cache=state.context.tool_cache,
                    raw_tool_outputs=state.context.raw_tool_outputs,
                    iteration=state.iteration.current,
                    tool_round=tool_round,
                    design_size_factor=state.timing.design_size_factor,
                    entity_registry=state.entity_registry,
                )
                summary = summarize_tool_result(
                    tool_name, result,
                    latest_wns=state.timing.latest_wns,
                    latest_tns=state.timing.latest_tns,
                    latest_failing_endpoints=state.timing.latest_failing_endpoints,
                    prev_best_wns=state.timing.prev_best_wns,
                    prev_best_tns=state.timing.prev_best_tns,
                )

                # Track dashboard freshness (mirrors ANALYZE/EVALUATE pattern)
                if tool_name in DESIGN_MODIFICATION_TOOLS:
                    state.timing.critical_paths_stale = True
                    state.timing.critical_paths_stale_reason = (
                        "checkpoint reloaded"
                        if tool_name == "vivado_open_checkpoint"
                        else "place/route changed"
                    )
                    for field in state.timing.field_freshness:
                        state.timing.field_freshness[field] = "stale"
                refreshable = DASHBOARD_REFRESH_MAP.get(tool_name)
                if refreshable:
                    for field in refreshable:
                        state.timing.field_freshness[field] = "fresh"

                if deps.compat is not None:
                    deps.compat.add_message("tool", summary, {
                        "tool_call_id": tc.id, "name": tool_name,
                    })
            if _pending_signal == "STRATEGY_SELECTED":
                break
            if _pending_signal == "EXHAUSTED":
                return LoopPhase.EVALUATE
            continue

        # No tool calls, no strategy selected: prompt again
        if not assistant_content.strip() and not message.tool_calls:
            state.context.consecutive_empty_responses += 1
            if state.context.consecutive_empty_responses >= 2:
                logger.warning(
                    f"[SELECT] {state.context.consecutive_empty_responses} consecutive "
                    f"empty responses, forcing exit"
                )
                record_flow_signal(state, "SYSTEM_EXIT", "empty_responses",
                                   phase="SELECT_STRATEGY")
                break
        else:
            state.context.consecutive_empty_responses = 0

        if deps.compat is not None:
            deps.compat.add_message("user",
                "[NOTE] Please select a strategy by calling report_step_state with strategy_name.")

    # Phase exit: build handoff
    llm_summary = llm_summary or assistant_content or "Strategy selection completed."
    handoff = build_phase_handoff(
        source_phase=LoopPhase.SELECT_STRATEGY,
        llm_summary=llm_summary,
        wns=state.timing.latest_wns,
        tns=state.timing.latest_tns,
        tools_called=tools_called,
        key_findings={
            "strategy_name": state.strategy.current_strategy,
            "wns_before": state.timing.latest_wns,
            # Explicit rationale so the chosen strategy's reasoning survives
            # into EXECUTE/EVALUATE even if the prose summary is trimmed —
            # prevents the LLM from re-deriving (and contradicting) it later.
            "rationale": (state.strategy.strategy_rationale or "")[:300],
        },
        message_count=tool_round,
        design_stage=getattr(state.timing, 'current_stage', ''),
        critical_paths_count=len(state.timing.critical_paths),
        stalled_strategies=list(state.iteration.blocked_strategies),
    )
    state.strategy.last_handoff_text = handoff.to_phase_context_string()
    await transition_phase(deps, LoopPhase.SELECT_STRATEGY, LoopPhase.EXECUTE, handoff, tool_cache=state.context.tool_cache, design_fingerprint=str(state.control.best_checkpoint_path))
    return LoopPhase.EXECUTE


def _check_phase_exit(state: OptimizerState, tool_round: int, max_rounds: int) -> bool:
    if tool_round > max_rounds:
        logger.warning(f"[SELECT_STRATEGY] Max rounds reached ({tool_round} > {max_rounds})")
        record_flow_signal(state, "SYSTEM_EXIT", "max_rounds", phase="SELECT_STRATEGY")
        return True
    if state.control.user_exit_requested:
        record_flow_signal(state, "SYSTEM_EXIT", "user_requested", phase="SELECT_STRATEGY")
        return True
    return False


async def _call_phase_llm(state, deps, phase_tools):
    """Call LLM with phase-specific tools (simplified — no retry for selection phase)."""
    if deps.openai_client is None or deps.compat is None:
        return None

    try:
        api_messages = deps.compat.get_formatted_for_api()
    except Exception:
        return None

    # Extract the first system message for the top-level API ``system``
    # parameter (prompt caching). Remaining system messages stay in context.
    system_text, api_messages = extract_system_message(api_messages)

    # Inject merged handoff + dashboard as last user message
    inject_pinned_cell_registry(api_messages, state)
    inject_merged_dashboard(api_messages, state, LoopPhase.SELECT_STRATEGY)

    model = state.model.current_model
    state.model.llm_call_count += 1

    extra_body = build_llm_extra_body(
        deps.reasoning_config, model,
        state.model.planner_model, state.model.worker_model,
    )

    try:
        kwargs = dict(
            model=model,
            messages=api_messages,
            tools=phase_tools if phase_tools else None,
            timeout=600.0,
        )
        if extra_body:
            if system_text:
                extra_body["system"] = system_text
            kwargs["extra_body"] = extra_body
        elif system_text:
            kwargs["extra_body"] = {"system": system_text}
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
                response=response, phase="SELECT_STRATEGY",
            )
        return response
    except Exception as e:
        logger.error(f"[SELECT_STRATEGY] LLM call failed: {e}")
        return None


def _get_permanently_blocked_strategies(state: OptimizerState) -> set[str]:
    """Return strategy keys that are currently blocked (strategy_ineffective).

    TTL-based: strategies with reason='strategy_ineffective' are blocked
    until the current iteration reaches entry.blocked_until_iter.
    After TTL expires, the strategy becomes selectable again.

    Strategies with reason='tool_error' remain always selectable (retriable).

    Reads from state.context.failed_strategies (canonical V2 source).
    """
    blocked: set[str] = set()
    current_iter = state.iteration.current
    for entry in state.context.failed_strategies:
        if entry.reason == "strategy_ineffective":
            if current_iter < entry.blocked_until_iter:
                blocked.add(entry.strategy)
            # else: TTL expired, strategy is unblocked and can be retried
    return blocked
