"""Select model node: choose the best LLM for this iteration.

Uses 9-dimension scoring from pure/model_select.py.

Reference: dcp_optimizer.py _select_model() (L3223-3326),
get_completion() (L4449-4468).
"""

from __future__ import annotations

import logging

from ..state import OptimizerState
from ..deps import NodeDeps
from ..edges import NodeName
from ..pure.model_select import compute_model_scores, select_model, estimate_context_complexity

logger = logging.getLogger(__name__)


async def select_model_node(
    state: OptimizerState, deps: NodeDeps
) -> str:
    """Select the best LLM model for this iteration.

    Actions:
        1. If next_iteration_model is pre-decided (from iteration_end), use it
        2. Otherwise, compute scores and select via pure function
        3. Set state.model.current_model

    Note: Node return values are not used for routing — graph edges decide.
    The static edge from select_model always routes to prepare_context.

    Returns:
        Next node name (deterministic: prepare_context).
    """
    # Use pre-decided model if available
    if state.model.next_iteration_model:
        selected = state.model.next_iteration_model
        state.model.next_iteration_model = None  # Clear after use
        logger.info(f"[select_model] Using pre-decided model: {selected}")
    else:
        # Get real context metrics from memory manager
        msg_count = 0
        token_est = 0
        if deps.memory_manager is not None:
            try:
                from context_manager.estimator import ContextEstimator
                messages = deps.memory_manager.get_context()
                msg_count = len(messages)
                token_est = ContextEstimator.estimate_from_messages(messages)
            except Exception:
                pass

        # Estimate context complexity
        context_complexity = estimate_context_complexity(
            task_type=state.model.current_task_type,
            msg_count=msg_count,
            token_est=token_est,
            iteration=state.iteration.current,
            failed_strategy_count=len(state.iteration.tool_errors),
            task_type_stats=state.model.task_type_stats,
        )

        # Compute scores
        planner_score, worker_score = compute_model_scores(
            state, context_complexity, current_tokens=token_est,
        )

        # Select model
        selected = select_model(planner_score, worker_score, state, current_tokens=token_est)
        logger.info(
            f"[select_model] Scores: planner={planner_score}, worker={worker_score} "
            f"-> {selected}"
        )

    # Track previous tier for switch detection
    if state.model.last_used_model:
        state.model.previous_tier = state.model.last_used_model

    # Update state
    state.model.current_model = selected
    state.model.last_used_model = selected

    return NodeName.PREPARE_CONTEXT
