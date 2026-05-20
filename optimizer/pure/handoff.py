"""Handoff prompt generation pure functions.

Extracted from dcp_optimizer.py: _generate_planner_handoff (L3071-3121),
_generate_worker_handoff (L3123-3174).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .critical_path import format_critical_paths_handoff, DISPLAY_LIMIT_HANDOFF_PLANNER, DISPLAY_LIMIT_HANDOFF_WORKER

if TYPE_CHECKING:
    from ..state import OptimizerState

logger = logging.getLogger(__name__)


def build_situation_summary(
    current_wns: float | None,
    best_wns: float | None,
    best_wns_iteration: int | None,
    global_no_improvement: int,
    elapsed_time: float,
    remaining_time: float,
) -> str:
    """Build factual situation summary. No prescriptions."""
    wns_str = f"{current_wns:.3f}ns" if current_wns is not None else "N/A"
    best_str = f"{best_wns:.3f}ns" if best_wns is not None and best_wns > float('-inf') else "N/A"
    best_iter_str = f"@iter {best_wns_iteration}" if best_wns_iteration is not None else ""

    lines = [f"WNS: {wns_str} (best: {best_str} {best_iter_str})"]
    if global_no_improvement > 0:
        lines.append(f"No-improvement rounds: {global_no_improvement}")
    lines.append(f"Time: {elapsed_time:.0f}s used, {remaining_time:.0f}s remaining")
    return "\n".join(lines)


def build_status_signal(
    global_no_improvement: int,
    consecutive_same_strategy: int = 0,
) -> str:
    """Build objective status signal. No instructions."""
    parts = []
    if global_no_improvement >= 3:
        parts.append(f"No-improvement rounds: {global_no_improvement}")
    if consecutive_same_strategy >= 3:
        parts.append(f"Same-strategy streak: {consecutive_same_strategy}")
    return "\n".join(parts) if parts else ""


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
        failed_strategies: List of failed strategy dicts (unused, kept for compat).
        current_wns: Current WNS value.
    """
    if tier == "planner":
        return _generate_planner_handoff(state, current_wns)
    else:
        return _generate_worker_handoff(state, current_wns)


def _format_trajectory_brief(narratives: list[dict], max_entries: int = 5) -> str:
    """Format recent iteration trajectories into brief lines."""
    if not narratives:
        return "(no history)"
    recent = narratives[-max_entries:]
    lines = []
    for entry in recent:
        it = entry.get("iteration", "?")
        strategy = entry.get("strategy_label", entry.get("strategy", "?"))
        wns_before = entry.get("wns_before")
        wns_after = entry.get("wns_after")
        wns_delta = entry.get("wns_delta", 0)

        before_str = f"{wns_before:.3f}" if wns_before is not None else "N/A"
        after_str = f"{wns_after:.3f}" if wns_after is not None else "N/A"
        lines.append(f"  Iter {it}: {strategy} | WNS {before_str}->{after_str} ({wns_delta:+.3f})")
    return "\n".join(lines)


def _generate_planner_handoff(
    state: OptimizerState,
    current_wns: float | None,
) -> str:
    """Generate handoff for planner models (1M context)."""
    best_wns = state.timing.best_wns if state.timing.best_wns > float('-inf') else None
    best_wns_iter = state.timing.best_wns_iteration
    clock_period = state.timing.clock_period

    situation = build_situation_summary(
        current_wns=current_wns,
        best_wns=best_wns,
        best_wns_iteration=best_wns_iter,
        global_no_improvement=state.iteration.global_no_improvement,
        elapsed_time=0.0,
        remaining_time=state.control.wall_clock_timeout,
    )
    trajectory = _format_trajectory_brief(state.iteration.narratives, max_entries=10)
    status = build_status_signal(
        state.iteration.global_no_improvement,
        _count_consecutive_same_strategy(state.iteration.strategy_sequence),
    )
    status_section = f"\nSTATUS:\n{status}\n" if status else ""

    wns_str = f"{current_wns:.3f}ns" if current_wns is not None else "N/A"
    tns_str = f"{state.timing.latest_tns:.3f}ns" if state.timing.latest_tns is not None else "N/A"
    fep_str = str(state.timing.latest_failing_endpoints) if state.timing.latest_failing_endpoints is not None else "N/A"

    critical_paths_str = format_critical_paths_handoff(
        state.timing.critical_paths, limit=DISPLAY_LIMIT_HANDOFF_PLANNER
    )

    return f"""--- Iteration {state.iteration.current} Handoff ---

SITUATION:
{situation}

STATE:
WNS={wns_str} TNS={tns_str} FailingEP={fep_str}

CRITICAL PATHS (top {DISPLAY_LIMIT_HANDOFF_PLANNER}):
{critical_paths_str}

TRAJECTORY:
{trajectory}
{status_section}"""


def _generate_worker_handoff(
    state: OptimizerState,
    current_wns: float | None,
) -> str:
    """Generate handoff for worker models (250K context)."""
    wns_str = f"{current_wns:.3f}ns" if current_wns is not None else "N/A"
    tns_str = f"{state.timing.latest_tns:.3f}ns" if state.timing.latest_tns is not None else "N/A"
    fep_str = str(state.timing.latest_failing_endpoints) if state.timing.latest_failing_endpoints is not None else "N/A"

    critical_paths_str = format_critical_paths_handoff(
        state.timing.critical_paths, limit=DISPLAY_LIMIT_HANDOFF_WORKER
    )

    status = build_status_signal(
        state.iteration.global_no_improvement,
        _count_consecutive_same_strategy(state.iteration.strategy_sequence),
    )
    status_section = f"\n{status}" if status else ""

    return f"""--- Iteration {state.iteration.current} ---

WNS={wns_str} TNS={tns_str} FailingEP={fep_str}
{critical_paths_str}
{status_section}"""


def _count_consecutive_same_strategy(strategy_sequence: list[str]) -> int:
    """Count consecutive occurrences of the last strategy."""
    if not strategy_sequence:
        return 0
    last = strategy_sequence[-1]
    count = 0
    for s in reversed(strategy_sequence):
        if s == last:
            count += 1
        else:
            break
    return count
