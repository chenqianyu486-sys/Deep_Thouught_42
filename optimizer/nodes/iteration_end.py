"""Iteration end node: post-iteration processing.

Updates counters, builds narratives, pre-decides next model,
generates handoff prompt.

Reference: dcp_optimizer.py _on_iteration_end() (L2413-2450),
optimize() (L5229-5300).
"""

from __future__ import annotations

import logging

from ..state import OptimizerState, record_flow_signal
from ..deps import NodeDeps
from ..edges import NodeName
from ..pure.iteration_logic import (
    update_iteration_counters,
    infer_strategy_from_tools,
    build_iteration_narrative,
)
from ..pure.handoff import build_handoff_prompt
from ..pure.model_select import (
    compute_model_scores,
    select_model as select_model_fn,
    estimate_context_complexity,
)
from ..color import green

logger = logging.getLogger(__name__)


async def iteration_end_node(
    state: OptimizerState, deps: NodeDeps
) -> str:
    """Post-iteration processing.

    Actions:
        1. Determine if WNS improved
        2. Update iteration counters
        3. Build iteration narrative
        4. Pre-decide next iteration's model
        5. Generate handoff prompt

    Note: Node return values are not used for routing — graph edges decide.
    The static edge from iteration_end always routes to check_exit.

    Returns:
        Next node name (deterministic: check_exit).
    """
    # Determine WNS improvement
    wns_improved = False
    if state.timing.best_wns > float('-inf') and state.timing.prev_best_wns is not None:
        wns_improved = state.timing.best_wns > state.timing.prev_best_wns
    elif state.timing.best_wns > float('-inf') and state.timing.prev_best_wns is None:
        wns_improved = True  # First valid WNS

    is_rollback = state.control.done_reason == "rollback"

    # Compute strategy label before any usage (must precede the counters/narrative block)
    tools_this_iter = state.iteration.tools_used
    strategy_label = state.strategy.current_strategy or infer_strategy_from_tools(tools_this_iter)

    # Update counters (skip for rollback — design will be restored)
    if not is_rollback:
        update_iteration_counters(state, wns_improved, state.model.current_model)
        # Record iteration outcome signal
        if wns_improved:
            record_flow_signal(state, "ITERATION_IMPROVED",
                               f"wns_delta={state.timing.best_wns - state.timing.prev_best_wns:.3f}" if state.timing.prev_best_wns is not None else "initial_improvement",
                               phase="ITERATION_END",
                               strategy=strategy_label)
        elif state.control.done_reason == "iteration_success":
            record_flow_signal(state, "ITERATION_SUCCESS", "llm_signaled_success",
                               phase="ITERATION_END", strategy=strategy_label)
        elif state.control.done_reason == "switch_strategy":
            record_flow_signal(state, "ITERATION_FAILED", "strategy_switch",
                               phase="ITERATION_END", strategy=strategy_label)
        else:
            record_flow_signal(state, "ITERATION_COMPLETE", "no_improvement",
                               phase="ITERATION_END", strategy=strategy_label)

    if not is_rollback:
        narrative = build_iteration_narrative(
            iteration=state.iteration.current,
            model_used=state.model.current_model,
            current_task_type=state.model.current_task_type,
            wns_before=state.timing.prev_best_wns,
            wns_after=state.timing.best_wns if state.timing.best_wns > float('-inf') else None,
            tools_used=tools_this_iter,
            result_status=(
                state.control.step_state.result_status
                if state.control.step_state else None
            ),
            declared_strategy=state.strategy.current_strategy or None,
        )
        state.iteration.narratives.append(narrative)
        if len(state.iteration.narratives) > 20:
            state.iteration.narratives.pop(0)

        # Record strategy lifecycle evaluation at end of iteration
        if state.strategy.current_strategy:
            if wns_improved:
                state.strategy.evaluation_result = "IMPROVED"
                if state.timing.best_wns > float('-inf') and state.timing.prev_best_wns is not None:
                    state.strategy.evaluation_wns_delta = state.timing.best_wns - state.timing.prev_best_wns
            elif state.timing.best_wns > float('-inf') and state.timing.prev_best_wns is not None:
                delta = state.timing.best_wns - state.timing.prev_best_wns
                if delta < -0.001:
                    state.strategy.evaluation_result = "REGRESSION"
                    state.strategy.evaluation_wns_delta = delta
                else:
                    state.strategy.evaluation_result = "UNCHANGED"
                    state.strategy.evaluation_wns_delta = 0.0

            # Record EVALUATE phase entry if not already the last entry
            from optimizer.state import PhaseEntry
            last_phase = state.strategy.phase_history[-1].phase if state.strategy.phase_history else ""
            if last_phase != "EVALUATE":
                phase_entry = PhaseEntry(
                    phase="EVALUATE",
                    strategy=state.strategy.current_strategy,
                    iteration=state.iteration.current,
                    tool_round=state.iteration.tool_round,
                    wns_at_entry=state.timing.latest_wns,
                )
                state.strategy.phase_history.append(phase_entry)
                if len(state.strategy.phase_history) > 100:
                    state.strategy.phase_history = state.strategy.phase_history[-100:]

        # Track strategy sequence and record failures
        if strategy_label and strategy_label not in ("Information", "Unknown"):
            state.iteration.strategy_sequence.append(strategy_label)
            if len(state.iteration.strategy_sequence) > 10:
                state.iteration.strategy_sequence.pop(0)

            # Record failure if iteration didn't improve and we have a known strategy.
            # Skip when LLM explicitly signaled success (NEXT_ITERATION) — the
            # iteration may be a preparation step that doesn't directly improve WNS.
            # Recording failure here would poison the strategy in future context.
            if (not wns_improved
                    and deps.compat is not None
                    and state.control.done_reason != "iteration_success"):
                reason = _determine_failure_reason(state, strategy_label)
                deps.compat.record_failure(
                    strategy=strategy_label,
                    reason=reason,
                    tool=", ".join(tools_this_iter[:3]),
                    detail=f"Iteration {state.iteration.current}: no WNS improvement",
                )
                logger.info(f"[iteration_end] Recorded failure: {strategy_label} ({reason})")

    # Pre-decide next iteration's model
    msg_count = 0
    token_est = 0
    if deps.memory_manager is not None:
        try:
            messages = deps.memory_manager.get_context()
            msg_count = len(messages)
            total_chars = sum(
                len(m.content) if hasattr(m, 'content') and isinstance(m.content, str) else 0
                for m in messages
            )
            token_est = total_chars // 4
        except Exception:
            pass
    context_complexity = estimate_context_complexity(
        task_type=state.model.current_task_type,
        msg_count=msg_count,
        token_est=token_est,
        iteration=state.iteration.current,
        failed_strategy_count=len(state.iteration.tool_errors),
        task_type_stats=state.model.task_type_stats,
    )
    planner_score, worker_score = compute_model_scores(state, context_complexity, token_est)
    next_model = select_model_fn(planner_score, worker_score, state, token_est)
    state.model.next_iteration_model = next_model
    logger.info(f"[iteration_end] Next iteration model: {next_model}")

    # Generate handoff prompt
    current_wns = state.timing.latest_wns
    failed_strategies = deps.compat.failed_strategies if deps.compat else []
    handoff = build_handoff_prompt(
        state=state,
        tier="planner" if next_model == state.model.planner_model else "worker",
        tool_call_details=[],
        failed_strategies=failed_strategies,
        current_wns=current_wns,
    )
    state.model.iteration_handoff_prompt = handoff
    state.model.iteration_handoff_injected = False

    # Advance MemoryManager's iteration counter (syncs with state.iteration.current)
    if deps.compat is not None:
        try:
            deps.compat.advance_iteration()
        except Exception as e:
            logger.warning(f"[iteration_end] advance_iteration failed: {e}")

    logger.info(green(
        f"[iteration_end] Iteration {state.iteration.current} complete: "
        f"wns_improved={wns_improved}, strategy={strategy_label}, "
        f"best_wns={state.timing.best_wns:.3f}ns"
    ))

    return NodeName.CHECK_EXIT


# Patterns indicating a tool returned empty/zero results (not a strategy failure,
# but a design constraint mismatch). These should use reason="tool_error" so the
# strategy can be retried after a 2-iteration cooldown.
_EMPTY_RESULT_PATTERNS = (
    "0 candidates",
    "no candidates",
    "no cells exceeded",
    "no deep combinational",
    "no actionable results",
    "total_candidates\": 0",
    "no high fanout",
)


def _determine_failure_reason(state: OptimizerState, strategy_label: str) -> str:
    """Determine failure reason with finer granularity.

    Returns:
        "tool_error" — tool returned empty/zero results or had errors;
                       strategy can be retried after cooldown.
        "strategy_ineffective" — strategy executed but didn't help;
                                 permanently blocked.
        "no_improvement" — non-switch_strategy exit with no WNS gain.
    """
    # Actual tool errors always get "tool_error"
    if state.iteration.tool_errors:
        return "tool_error"

    if state.control.done_reason == "switch_strategy":
        # Check raw tool outputs for empty-result indicators
        for (iter_num, _round), (tool_name, raw_output) in state.context.raw_tool_outputs.items():
            if iter_num != state.iteration.current:
                continue
            output_lower = raw_output.lower() if raw_output else ""
            if any(pattern in output_lower for pattern in _EMPTY_RESULT_PATTERNS):
                logger.info(
                    f"[iteration_end] Empty result detected in {tool_name}, "
                    f"using tool_error (retriable) instead of strategy_ineffective"
                )
                return "tool_error"
        return "strategy_ineffective"

    return "no_improvement"
