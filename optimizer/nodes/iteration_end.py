"""Iteration end node: post-iteration processing.

Updates counters, builds narratives, pre-decides next model,
generates handoff prompt.

Reference: dcp_optimizer.py _on_iteration_end() (L2413-2450),
optimize() (L5229-5300).
"""

from __future__ import annotations

import logging

from ..state import OptimizerState
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

    Returns:
        Next node name (deterministic: check_exit).
    """
    # Determine WNS improvement
    wns_improved = False
    if state.timing.best_wns > float('-inf') and state.timing.prev_best_wns is not None:
        wns_improved = state.timing.best_wns > state.timing.prev_best_wns
    elif state.timing.best_wns > float('-inf') and state.timing.prev_best_wns is None:
        wns_improved = True  # First valid WNS

    # Update counters
    update_iteration_counters(state, wns_improved, state.model.current_model)

    # Build narrative
    tool_call_details = deps.compat.tool_call_details if deps.compat else []
    tools_this_iter = [
        t.get('tool_name', '')
        for t in tool_call_details
        if t.get('iteration') == state.iteration.current
    ]
    strategy_label = infer_strategy_from_tools(tools_this_iter)

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
    )
    state.iteration.narratives.append(narrative)
    if len(state.iteration.narratives) > 20:
        state.iteration.narratives.pop(0)

    # Track strategy sequence and record failures
    if strategy_label and strategy_label not in ("Information", "Unknown"):
        state.iteration.strategy_sequence.append(strategy_label)
        if len(state.iteration.strategy_sequence) > 10:
            state.iteration.strategy_sequence.pop(0)

        # Record failure if iteration didn't improve and we have a known strategy
        if not wns_improved and deps.compat is not None:
            reason = "strategy_ineffective" if state.control.done_reason == "switch_strategy" else "strategy_ineffective"
            deps.compat.record_failure(
                strategy=strategy_label,
                reason=reason,
                tool=", ".join(tools_this_iter[:3]),
                detail=f"Iteration {state.iteration.current}: no WNS improvement",
            )
            logger.info(f"[iteration_end] Recorded failure: {strategy_label} ({reason})")

    # Pre-decide next iteration's model
    context_complexity = estimate_context_complexity(
        task_type=state.model.current_task_type,
        msg_count=0,
        token_est=0,
        iteration=state.iteration.current,
        failed_strategy_count=len(state.iteration.tool_errors),
        task_type_stats=state.model.task_type_stats,
    )
    planner_score, worker_score = compute_model_scores(state, context_complexity, 0)
    next_model = select_model_fn(planner_score, worker_score, state, 0)
    state.model.next_iteration_model = next_model
    logger.info(f"[iteration_end] Next iteration model: {next_model}")

    # Generate handoff prompt
    current_wns = state.timing.latest_wns
    failed_strategies = deps.compat.failed_strategies if deps.compat else []
    handoff = build_handoff_prompt(
        state=state,
        tier="planner" if next_model == state.model.planner_model else "worker",
        tool_call_details=tool_call_details,
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

    logger.info(
        f"[iteration_end] Iteration {state.iteration.current} complete: "
        f"wns_improved={wns_improved}, strategy={strategy_label}, "
        f"best_wns={state.timing.best_wns:.3f}ns"
    )

    return NodeName.CHECK_EXIT
