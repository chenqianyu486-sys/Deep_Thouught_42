"""SELECT_STRATEGY phase: choose the optimal optimization strategy.

Receives the analysis summary from ANALYZE phase via handoff.
Exposes minimal tools — the LLM focuses purely on decision-making.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from optimizer.state import OptimizerState, PhaseEntry, LLMCallRecord
from optimizer.deps import NodeDeps
from optimizer.pure.tool_filter import LoopPhase, PHASE_MAX_ROUNDS, filter_tools_for_phase
from optimizer.pure.tool_router import call_tool as call_tool_fn
from optimizer.pure.tool_summary import summarize_tool_result
from optimizer.pure.step_state import extract_step_state
from optimizer.nodes.subgraphs.phase_handoff import build_phase_handoff, transition_phase
from optimizer.pure.context_snapshot import inject_merged_dashboard
from optimizer.color import green, yellow

logger = logging.getLogger(__name__)


async def run_select_strategy_phase(state: OptimizerState, deps: NodeDeps) -> LoopPhase:
    """Run the SELECT_STRATEGY phase: choose a strategy based on analysis.

    Returns:
        LoopPhase.EXECUTE if a strategy was selected, or EVALUATE if exhausted.
    """
    max_rounds = PHASE_MAX_ROUNDS.get(LoopPhase.SELECT_STRATEGY, 6)
    tool_round = 0
    tools_called: list[str] = []
    llm_summary = ""

    while True:
        tool_round += 1
        state.iteration.tool_round = tool_round

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
                state.control.is_done = True
                state.control.done_reason = "strategies_exhausted"
                return LoopPhase.EVALUATE

            # Strategy selected: check for strategy_name
            if step_state.strategy_name:
                state.strategy.current_strategy = step_state.strategy_name
                state.strategy.strategy_rationale = assistant_content
                llm_summary = assistant_content
                logger.info(green(f"[SELECT_STRATEGY] Strategy selected: {step_state.strategy_name}"))

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
                break

        else:
            state.context.step_state_misses += 1
            if deps.compat is not None:
                deps.compat.add_message("user",
                    "[NOTE] report_step_state with strategy_name required. Example: "
                    'report_step_state(strategy_phase="SELECT_STRATEGY", strategy_name="PBLOCK")')

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

                result = await call_tool_fn(
                    tool_name=tool_name, arguments=tool_args,
                    rapidwright_session=deps.rapidwright_session,
                    vivado_session=deps.vivado_session,
                )
                summary = summarize_tool_result(tool_name, result)
                if deps.compat is not None:
                    deps.compat.add_message("tool", summary, {
                        "tool_call_id": tc.id, "name": tool_name,
                    })
            continue

        # No tool calls, no strategy selected: prompt again
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
        },
        message_count=tool_round,
    )
    state.strategy.last_handoff_text = handoff.to_phase_context_string()
    await transition_phase(deps, LoopPhase.SELECT_STRATEGY, LoopPhase.EXECUTE, handoff)
    return LoopPhase.EXECUTE


def _check_phase_exit(state: OptimizerState, tool_round: int, max_rounds: int) -> bool:
    if tool_round > max_rounds:
        logger.warning(f"[SELECT_STRATEGY] Max rounds reached ({tool_round} > {max_rounds})")
        return True
    if state.control.user_exit_requested:
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

    # Inject merged handoff + dashboard as last user message
    inject_merged_dashboard(api_messages, state, LoopPhase.SELECT_STRATEGY)

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
                response=response, phase="SELECT_STRATEGY",
            )
        return response
    except Exception as e:
        logger.error(f"[SELECT_STRATEGY] LLM call failed: {e}")
        return None
