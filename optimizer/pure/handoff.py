"""Handoff prompt generation pure functions.

Extracted from dcp_optimizer.py: _generate_planner_handoff (L3071-3121),
_generate_worker_handoff (L3123-3174).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..state import OptimizerState


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
        failed_strategies: List of failed strategy dicts with strategy/reason/detail.
        current_wns: Current WNS value.
    """
    if tier == "planner":
        return _generate_planner_handoff(state, current_wns, failed_strategies)
    else:
        return _generate_worker_handoff(state, current_wns, failed_strategies)


def _format_failed_strategies(failed_strategies: list[dict]) -> str:
    """Format failed strategies into a brief summary for the handoff."""
    if not failed_strategies:
        return ""
    lines = []
    for f in failed_strategies:
        strategy = f.get("strategy", "?")
        reason = f.get("reason", "unknown")
        detail = f.get("detail", "")
        line = f"  - {strategy} ({reason})"
        if detail:
            line += f": {detail[:100]}"
        lines.append(line)
    return "\nFAILED STRATEGIES:\n" + "\n".join(lines)


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
        wns_delta = entry.get("wns_delta")
        outcome = entry.get("outcome", "unknown")

        before_str = f"{wns_before:.3f}" if wns_before is not None else "N/A"
        after_str = f"{wns_after:.3f}" if wns_after is not None else "N/A"
        delta_str = f"{wns_delta:+.3f}" if wns_delta is not None else "N/A"
        lines.append(f"  Iter {it}: {strategy} | WNS {before_str}->{after_str} ({delta_str}) | {outcome}")
    return "\n".join(lines)


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
    current_wns: float | None,
    failed_strategies: list[dict] | None = None,
) -> str:
    """Generate handoff for planner models (1M context).

    Handoff carries only what the Dashboard does NOT contain:
    - Iteration trajectory (compact WNS history across iterations)
    - Failed strategies (excluded strategies with reasons)
    - Status signals (no-improvement count, same-strategy streak)
    - Exit reason from the previous iteration
    - Applied optimizations from best_checkpoint history

    WNS/TNS/FE, critical paths, and design signals are in the Dashboard
    (injected per-LLM-call) and are NOT duplicated here.
    """
    exit_reason = state.control.done_reason or ""
    exit_line = f"EXIT_REASON: {exit_reason}" if exit_reason else ""

    rollback_notice = ""
    if exit_reason == "rollback":
        best_iter = state.timing.best_wns_iteration or "?"
        best_str = f"{state.timing.best_wns:.3f}" if state.timing.best_wns != float('-inf') else "N/A"
        rollback_notice = f"""{exit_line}
DESIGN RESTORED: Design rolled back to best checkpoint from iteration {best_iter}.
  WNS restored to: {best_str}ns

"""

    trajectory = _format_trajectory_brief(state.iteration.narratives, max_entries=10)
    status = build_status_signal(
        state.iteration.global_no_improvement,
        _count_consecutive_same_strategy(state.iteration.strategy_sequence),
    )
    status_section = f"\nSTATUS:\n{status}\n" if status else ""
    opt_history = _format_optimization_history(state.context.optimization_history)

    return f"""--- Iteration {state.iteration.current + 1} Handoff ---

{rollback_notice}TRAJECTORY:
{trajectory}
{status_section}{_format_failed_strategies(failed_strategies or [])}{opt_history}"""


def _generate_worker_handoff(
    state: OptimizerState,
    current_wns: float | None,
    failed_strategies: list[dict] | None = None,
) -> str:
    """Generate handoff for worker models (250K context).

    Compact version: trajectory (last 5) + status + failed strategies.
    WNS/TNS/FE and critical paths are in the Dashboard.
    """
    exit_reason = state.control.done_reason or ""
    exit_line = f"EXIT_REASON: {exit_reason}" if exit_reason else ""

    rollback_notice = ""
    if exit_reason == "rollback":
        best_iter = state.timing.best_wns_iteration or "?"
        best_str = f"{state.timing.best_wns:.3f}" if state.timing.best_wns != float('-inf') else "N/A"
        rollback_notice = f"""{exit_line}
DESIGN RESTORED: Design rolled back to best checkpoint from iteration {best_iter}.
  WNS restored to: {best_str}ns

"""

    trajectory = _format_trajectory_brief(state.iteration.narratives, max_entries=5)
    status = build_status_signal(
        state.iteration.global_no_improvement,
        _count_consecutive_same_strategy(state.iteration.strategy_sequence),
    )
    status_section = f"\n{status}" if status else ""
    opt_history = _format_optimization_history(state.context.optimization_history)

    return f"""--- Iteration {state.iteration.current + 1} ---

{rollback_notice}TRAJECTORY:
{trajectory}
{status_section}{_format_failed_strategies(failed_strategies or [])}{opt_history}"""


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
