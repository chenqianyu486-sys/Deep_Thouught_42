"""Handoff prompt generation pure functions.

Extracted from dcp_optimizer.py: _generate_planner_handoff (L3071-3121),
_generate_worker_handoff (L3123-3174).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..state import OptimizerState


def build_handoff_prompt(
    state: OptimizerState,
    tier: str,
    tool_call_details: list[dict],
    failed_strategies: list[dict],
    current_wns: float | None,
) -> str:
    """Generate model-tier-specific handoff prompt.

    Args:
        state: Current optimizer state.
        tier: "planner" or "worker".
        tool_call_details: Recent tool call details.
        failed_strategies: List of failed strategy dicts with strategy/reason/detail.
        current_wns: Current WNS value.
    """
    if tier == "planner":
        return _generate_planner_handoff(state, current_wns, failed_strategies)
    else:
        return _generate_worker_handoff(state, current_wns, failed_strategies)


def _format_optimization_history(history: list) -> str:
    """Format optimization_history into an "Applied optimizations" section."""
    if not history:
        return "\nAPPLIED OPTIMIZATIONS (in best_checkpoint):\n  (none yet)"
    lines = ["\nAPPLIED OPTIMIZATIONS (in best_checkpoint):"]
    for rec in history:
        strategy = rec.strategy if isinstance(rec.strategy, str) else (rec.get("strategy", "?"))
        wns_before = rec.wns_before if isinstance(rec.wns_before, float) else rec.get("wns_before", 0.0)
        wns_after = rec.wns_after if isinstance(rec.wns_after, float) else rec.get("wns_after", 0.0)
        iteration = rec.iteration if isinstance(rec.iteration, int) else rec.get("iteration", 0)
        lines.append(f"  - {strategy}: {wns_before:.3f}ns -> {wns_after:.3f}ns (iter {iteration})")
    return "\n".join(lines)


def _generate_planner_handoff(
    state: OptimizerState,
    current_wns: float | None = None,
    failed_strategies: list[dict] | None = None,
) -> str:
    """Generate handoff for planner models (1M context).

    Only carries what the Dashboard does NOT already contain:
    - Rollback notice (design restoration notification)
    - Applied optimizations from best_checkpoint history

    Dashboard already provides: narratives trajectory, blocked_strategies,
    status signals, WNS/TNS/FE, critical paths, design signals.
    """
    exit_reason = state.control.done_reason or ""
    exit_line = f"EXIT_REASON: {exit_reason}" if exit_reason else ""

    rollback_notice = ""
    if exit_reason == "rollback":
        best_iter = state.timing.best_wns_iteration or "?"
        best_str = (
            f"{state.timing.best_wns:.3f}"
            if state.timing.best_wns != float('-inf') else "N/A"
        )
        rollback_notice = (
            f"{exit_line}\n"
            f"DESIGN RESTORED: Design rolled back to best checkpoint "
            f"from iteration {best_iter}.\n"
            f"  WNS restored to: {best_str}ns\n\n"
        )

    opt_history = _format_optimization_history(state.context.optimization_history)

    return (
        f"--- Iteration {state.iteration.current + 1} Handoff ---\n\n"
        f"{rollback_notice}{opt_history}"
    )


def _generate_worker_handoff(
    state: OptimizerState,
    current_wns: float | None = None,
    failed_strategies: list[dict] | None = None,
) -> str:
    """Generate handoff for worker models (250K context).

    Only carries what the Dashboard does NOT already contain:
    - Rollback notice (design restoration notification)
    - Applied optimizations from best_checkpoint history

    Dashboard already provides: narratives trajectory, blocked_strategies,
    status signals, WNS/TNS/FE.
    """
    exit_reason = state.control.done_reason or ""
    exit_line = f"EXIT_REASON: {exit_reason}" if exit_reason else ""

    rollback_notice = ""
    if exit_reason == "rollback":
        best_iter = state.timing.best_wns_iteration or "?"
        best_str = (
            f"{state.timing.best_wns:.3f}"
            if state.timing.best_wns != float('-inf') else "N/A"
        )
        rollback_notice = (
            f"{exit_line}\n"
            f"DESIGN RESTORED: Design rolled back to best checkpoint "
            f"from iteration {best_iter}.\n"
            f"  WNS restored to: {best_str}ns\n\n"
        )

    opt_history = _format_optimization_history(state.context.optimization_history)

    return (
        f"--- Iteration {state.iteration.current + 1} ---\n\n"
        f"{rollback_notice}{opt_history}"
    )


