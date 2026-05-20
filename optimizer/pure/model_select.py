"""Model selection scoring pure functions.

Extracted from dcp_optimizer.py: _select_model (L3223-3326),
_get_task_capability_score (L3328-3340), classify_task (L3178-3196),
_estimate_context_complexity (L1113-1155).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .constants import (
    INFORMATION_PATTERNS,
    OPTIMIZATION_PATTERNS,
    TaskCategory,
    WORKER_CONTEXT_WARN_TOKENS,
    WORKER_CONTEXT_FORCE_TOKENS,
    WORKER_UPGRADE_THRESHOLD,
    WORKER_DOWNGRADE_THRESHOLD,
    GLOBAL_NO_IMPROVEMENT_LIMIT,
)

if TYPE_CHECKING:
    from ..state import OptimizerState

logger = logging.getLogger(__name__)


def classify_task(tool_name: str, arguments: dict | None = None) -> str:
    """Classify tool as INFORMATION / OPTIMIZATION / UNKNOWN."""
    if not tool_name:
        return TaskCategory.UNKNOWN

    name_lower = tool_name.lower()

    if any(p in name_lower for p in OPTIMIZATION_PATTERNS):
        return TaskCategory.OPTIMIZATION

    if any(p in name_lower for p in INFORMATION_PATTERNS):
        return TaskCategory.INFORMATION

    if tool_name == "vivado_run_tcl" and arguments:
        tcl_cmd = str(arguments.get("command", "")).lower()
        if any(p in tcl_cmd for p in OPTIMIZATION_PATTERNS):
            return TaskCategory.OPTIMIZATION

    return TaskCategory.UNKNOWN


def get_task_capability_score(task_type: str, task_type_stats: dict) -> float:
    """Calculate Worker model capability score for specific task based on historical performance.

    Returns:
        0.0-1.0 (Worker success rate), 0.5 (neutral) if task never seen.
    """
    if not task_type or task_type not in task_type_stats:
        return 0.5
    stats = task_type_stats[task_type]
    total = stats.get('total', 0)
    if total == 0:
        return 0.5
    success = stats.get('success', 0)
    return success / total


def estimate_context_complexity(
    task_type: str,
    msg_count: int,
    token_est: int,
    iteration: int,
    failed_strategy_count: int,
    task_type_stats: dict,
) -> int:
    """Estimate context complexity score (0-10).

    Data-driven: uses historical task success rate for task complexity.
    """
    iteration_factor = min(iteration / 10, 2)
    failure_factor = min(failed_strategy_count / 5, 2)

    task_complexity_factor = 0
    if task_type and task_type in task_type_stats:
        stats = task_type_stats[task_type]
        if stats.get('total', 0) >= 3:
            success_rate = stats['success'] / stats['total']
            if success_rate >= 0.8:
                task_complexity_factor = 0
            elif success_rate >= 0.6:
                task_complexity_factor = 1
            elif success_rate >= 0.4:
                task_complexity_factor = 2
            else:
                task_complexity_factor = 3
    else:
        task_complexity_factor = 1

    base_score = min(msg_count / 20, 3) + min(token_est / 50000, 3)
    complexity = base_score + iteration_factor + failure_factor + task_complexity_factor
    return int(min(complexity, 10))


def compute_model_scores(
    state: OptimizerState,
    context_complexity: int,
    current_tokens: int,
) -> tuple[int, int]:
    """Compute (planner_score, worker_score) from 8 dimensions.

    Reads from state.model, state.timing, state.iteration, state.cost.
    """
    planner_score = 0
    worker_score = 0

    # ── Dimension 3: Context complexity ───────────────────────────
    if context_complexity >= 6:
        planner_score += 2
    elif context_complexity < 3:
        worker_score += 1

    # ── Dimension 4: Historical capability score ──────────────────
    capability = get_task_capability_score(
        state.model.current_task_type, state.model.task_type_stats
    )
    if capability >= 0.7:
        worker_score += 2
    elif capability < 0.3:
        planner_score += 2

    # ── Dimension 5: Counter state ────────────────────────────────
    if state.model.worker_consecutive_failures >= WORKER_UPGRADE_THRESHOLD:
        planner_score += 4
    if state.model.worker_consecutive_success >= WORKER_DOWNGRADE_THRESHOLD:
        worker_score += 1

    # ── Dimension 6: Global no-improvement signal ─────────────────
    if state.iteration.global_no_improvement >= GLOBAL_NO_IMPROVEMENT_LIMIT // 2:
        planner_score += 1

    # ── Dimension 7: Context window capacity ──────────────────────
    if current_tokens >= WORKER_CONTEXT_WARN_TOKENS:
        planner_score += 2

    # ── Dimension 8: WNS / timing state ───────────────────────────
    if state.timing.initial_wns is not None and state.timing.best_wns != float('-inf'):
        wns_improvement = state.timing.best_wns - state.timing.initial_wns
        if wns_improvement < -2.0:
            planner_score += 3
        elif wns_improvement < -0.5:
            planner_score += 2
        elif wns_improvement < 0:
            planner_score += 1

    # ── Dimension 9: Budget awareness ─────────────────────────────
    budget_used_pct = (
        (state.cost.total_cost / state.cost.cost_hard_limit * 100)
        if state.cost.cost_hard_limit > 0 else 0
    )
    if budget_used_pct > 80:
        worker_score += 3
    elif budget_used_pct > 60:
        worker_score += 1

    return planner_score, worker_score


def select_model(
    planner_score: int,
    worker_score: int,
    state: OptimizerState,
    current_tokens: int,
) -> str:
    """Apply decision threshold, margin, exhaustion checks.

    Returns:
        Selected model identifier string.
    """
    # Hard override: context window safety
    if current_tokens >= WORKER_CONTEXT_FORCE_TOKENS:
        logger.info(f"Context window override: ~{current_tokens:,} tokens >= {WORKER_CONTEXT_FORCE_TOKENS:,}, forcing planner")
        return state.model.planner_model

    # Filter out exhausted models
    worker_exhausted = (
        state.model.worker_model in state.model.exhausted_worker_fallbacks
        or state.model.worker_model in state.model.exhausted_planner_fallbacks
    )

    if worker_exhausted:
        logger.info(f"Model {state.model.worker_model} is exhausted, forcing planner")
        selected_model = state.model.planner_model
    elif planner_score > worker_score + 1:  # Margin of 2 required
        selected_model = state.model.planner_model
    elif worker_score > planner_score:
        selected_model = state.model.worker_model
    else:
        selected_model = state.model.planner_model  # Default to safe choice

    # Track model usage history
    if state.model.model_usage_history and state.model.model_usage_history[-1] != selected_model:
        logger.info(f"Model switch detected: {state.model.model_usage_history[-1]} -> {selected_model}")
    state.model.model_usage_history.append(selected_model)
    if len(state.model.model_usage_history) > 10:
        state.model.model_usage_history.pop(0)

    return selected_model
