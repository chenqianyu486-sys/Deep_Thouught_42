"""Context snapshot building pure functions.

Extracted from dcp_optimizer.py: _build_context_snapshot (L1199-1297),
_inject_context_snapshot (L1299-1327).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..state import OptimizerState, CriticalPathEntry

from .critical_path import format_critical_paths_snapshot

logger = logging.getLogger(__name__)

SNAPSHOT_HEADER = "# === FPGA Context Snapshot ==="


def build_context_snapshot(
    current_wns: float | None,
    best_wns: float | None,
    best_wns_iteration: int | None,
    strategy_sequence: list[str],
    failed_strategy_names: list[str],
    global_no_improvement: int,
    cost_hard_limit: float,
    total_cost: float,
    elapsed_time: float,
    remaining_time: float,
    iteration_narratives: list[dict],
    tool_call_details: list[dict],
    critical_paths: list | None = None,
) -> str:
    """Build compact YAML context snapshot of current optimization state.

    Injected as the first user message before every LLM call.
    """
    # --- current_best_wns ---
    if best_wns is not None:
        best_wns_label = ""
        if best_wns_iteration is not None:
            for entry in iteration_narratives:
                if entry.get("iteration") == best_wns_iteration:
                    strat = entry.get("strategy_label", "")
                    if strat and strat not in ("Information", "Unknown"):
                        best_wns_label = f" via {strat}"
                    break
        cb_wns = f"{best_wns:.3f} ns{best_wns_label}"
    else:
        cb_wns = "N/A"

    # --- remaining_violation ---
    if current_wns is not None and current_wns < 0:
        remaining_str = f"{-current_wns:.3f} ns"
    else:
        remaining_str = "N/A"

    # --- active_strategy ---
    strategy_parts = []
    for s in strategy_sequence[-4:]:
        if s in failed_strategy_names:
            strategy_parts.append(f"{s} (FAILED)")
        elif global_no_improvement >= 2:
            strategy_parts.append(f"{s} (PLATEAUED)")
        else:
            strategy_parts.append(f"{s} (ACTIVE)")
    active_strategy = " -> ".join(strategy_parts) if strategy_parts else "None"

    # --- do_not_repeat ---
    dnr_entries = []
    tool_stats: dict[str, list[float]] = {}
    for td in tool_call_details:
        name = td.get("tool_name", "")
        wns_val = td.get("wns")
        if name and wns_val is not None and not td.get("error", False):
            tool_stats.setdefault(name, []).append(wns_val)
    for tool_name, wns_values in tool_stats.items():
        if len(wns_values) > 3:
            delta = max(wns_values) - min(wns_values)
            if delta < 0.01:
                dnr_entries.append(f'  - "{tool_name} (already called {len(wns_values)} times with no improvement)"')

    lines = []
    lines.append(SNAPSHOT_HEADER)
    lines.append('primary_goal: "Achieve WNS >= 0 ns"')
    lines.append(f'current_best_wns: "{cb_wns}"')
    lines.append(f'remaining_violation: "{remaining_str}"')
    lines.append(f'active_strategy: "{active_strategy}"')
    if failed_strategy_names:
        lines.append("failed_strategies:")
        for fs in failed_strategy_names[-5:]:
            lines.append(f'  - "{fs}"')
    else:
        lines.append("failed_strategies: []")
    if dnr_entries:
        lines.append("do_not_repeat:")
        lines.extend(dnr_entries[:5])
    else:
        lines.append("do_not_repeat: []")

    # --- iteration_history ---
    narrative_lines = _format_narrative_summary(iteration_narratives, max_entries=5)
    if narrative_lines:
        lines.append("iteration_history:")
        for nl in narrative_lines:
            lines.append(f'  - "{nl}"')
    else:
        lines.append("iteration_history: []")

    # --- critical_paths ---
    if critical_paths:
        cp_lines = format_critical_paths_snapshot(critical_paths)
        if cp_lines:
            lines.append("critical_paths:")
            for cp in cp_lines:
                lines.append(f'  - "{cp}"')
    else:
        lines.append("critical_paths: []")

    # --- budget_status ---
    remaining_budget = max(0.0, cost_hard_limit - total_cost)
    budget_pct = (total_cost / cost_hard_limit * 100) if cost_hard_limit > 0 else 0
    lines.append(f'remaining_budget: "${remaining_budget:.2f}" ({budget_pct:.0f}% used)')
    lines.append(f'elapsed_time: "{elapsed_time:.0f}s" (remaining: {remaining_time:.0f}s)')

    return "\n".join(lines)


def _format_narrative_summary(narratives: list[dict], max_entries: int = 5) -> list[str]:
    """Format recent iteration narratives into summary lines."""
    if not narratives:
        return []
    recent = narratives[-max_entries:]
    lines = []
    for entry in recent:
        it = entry.get("iteration", "?")
        model = entry.get("model", "?")
        outcome = entry.get("outcome", "?")
        wns_before = entry.get("wns_before")
        wns_after = entry.get("wns_after")
        wns_delta = entry.get("wns_delta", 0)
        strategy = entry.get("strategy_label", "")

        before_str = f"{wns_before:.3f}" if wns_before is not None else "N/A"
        after_str = f"{wns_after:.3f}" if wns_after is not None else "N/A"
        lines.append(
            f"Iter {it}: {model} | {strategy} | "
            f"WNS {before_str}->{after_str} ({wns_delta:+.3f}) | {outcome}"
        )
    return lines


def inject_context_snapshot(api_messages: list[dict], snapshot_yaml: str) -> None:
    """Inject or update the context snapshot as the first user message.

    Scans for an existing snapshot (by header marker), removes it to
    prevent accumulation, then inserts a fresh snapshot after all
    system messages.
    """
    # Find and remove existing snapshot message
    for i, msg in enumerate(api_messages):
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            if msg["content"].startswith(SNAPSHOT_HEADER):
                del api_messages[i]
                break

    # Find the first non-system message index to insert before it
    insert_idx = 0
    for i, msg in enumerate(api_messages):
        if msg.get("role") != "system":
            insert_idx = i
            break
    else:
        insert_idx = len(api_messages)

    api_messages.insert(insert_idx, {"role": "user", "content": snapshot_yaml})
