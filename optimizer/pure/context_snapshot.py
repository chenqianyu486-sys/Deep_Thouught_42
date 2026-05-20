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

SNAPSHOT_HEADER = "--- Optimization Dashboard ---"


def build_context_snapshot(
    *,
    clock_period: float | None,
    current_wns: float | None,
    best_wns: float | None,
    best_wns_iteration: int | None,
    tns: float | None,
    failing_endpoints: int | None,
    high_fanout_nets: list,
    critical_path_spread: dict | None,
    resource_utilization: dict | None,
    elapsed_time: float,
    remaining_time: float,
    total_cost: float,
    cost_hard_limit: float,
    iteration_narratives: list[dict] | None = None,
    tools_used: list[str] | None = None,
    critical_paths: list | None = None,
) -> str:
    """Build factual data dashboard for the current optimization state.

    Injected as the last user message before every LLM call in the tool loop.
    Presents raw measurements only — the LLM decides the next action.
    Appends a call-to-action reminding the LLM to call report_step_state.
    """
    lines = []
    lines.append(SNAPSHOT_HEADER)
    lines.append("This is a factual data dashboard for the current optimization state.")
    lines.append("All values are raw measurements. You decide the next action.")
    lines.append("")

    # -- Core timing metrics --
    lines.append(f"clock_period: {clock_period:.3f}" if clock_period else "clock_period: N/A")
    lines.append(f"wns_current: {current_wns:.3f}" if current_wns is not None else "wns_current: N/A")
    lines.append(f"wns_best: {best_wns:.3f}" if best_wns is not None and best_wns > float('-inf') else "wns_best: N/A")
    lines.append(f"wns_best_iter: {best_wns_iteration}" if best_wns_iteration is not None else "wns_best_iter: N/A")
    lines.append(f"tns: {tns:.3f}" if tns is not None else "tns: N/A")
    lines.append(f"failing_endpoints: {failing_endpoints}" if failing_endpoints is not None else "failing_endpoints: N/A")

    # -- Budget --
    remaining_budget = max(0.0, cost_hard_limit - total_cost)
    lines.append(f"budget_remaining: ${remaining_budget:.3f}")
    lines.append(f"elapsed: {elapsed_time:.0f}s")

    # -- Trajectory (work history) --
    trajectory = _format_trajectory(iteration_narratives)
    if trajectory:
        lines.append("")
        lines.append("trajectory:")
        for entry in trajectory:
            lines.append(f"  - iter: {entry['iter']}")
            lines.append(f"    strategy: {entry['strategy']}")
            if "wns_before" in entry:
                lines.append(f"    wns_before: {entry['wns_before']:.3f}")
                lines.append(f"    wns_after: {entry['wns_after']:.3f}")
                lines.append(f"    delta: {entry['delta']:+.4f}")
    else:
        lines.append("")
        lines.append("trajectory: []")

    # -- Design signals --
    signals = _compute_design_signals(high_fanout_nets, critical_path_spread, resource_utilization)
    if signals:
        lines.append("")
        lines.append("design_signals:")
        for k, v in signals.items():
            lines.append(f"  {k}: {v}")
    else:
        lines.append("")
        lines.append("design_signals: {}")

    # -- Critical paths --
    if critical_paths:
        cp_lines = format_critical_paths_snapshot(critical_paths)
        if cp_lines:
            lines.append("")
            lines.append("critical_paths:")
            for cp in cp_lines:
                lines.append(f"  - {cp}")
        else:
            lines.append("")
            lines.append("critical_paths: []")
    else:
        lines.append("")
        lines.append("critical_paths: []")

    # -- Active tools --
    active = _compute_active_tools(tools_used)
    if active:
        lines.append("")
        lines.append("active_tools:")
        for tool in active:
            lines.append(f"  - {tool}")
    else:
        lines.append("")
        lines.append("active_tools: []")

    lines.append("")
    lines.append("next_action: Call report_step_state(step_id, result_status, flow_control) alongside your optimization/analysis tools. Text body = chain-of-thought analysis.")
    lines.append("--- End Dashboard ---")
    return "\n".join(lines)


def _compute_design_signals(
    high_fanout_nets: list,
    critical_path_spread: dict | None,
    resource_utilization: dict | None,
) -> dict:
    """Compute objective signals from raw data. No judgments."""
    signals = {}
    if high_fanout_nets:
        fanouts = []
        for n in high_fanout_nets:
            if isinstance(n, dict) and "fanout" in n:
                fanouts.append(n["fanout"])
        if fanouts:
            signals["max_fanout"] = max(fanouts)
            signals["high_fanout_count"] = len(fanouts)
    if critical_path_spread and isinstance(critical_path_spread, dict):
        for k, v in critical_path_spread.items():
            if isinstance(v, (int, float)):
                signals[f"cp_spread_{k}"] = round(v, 2)
    if resource_utilization:
        for k, v in resource_utilization.items():
            if isinstance(v, (int, float)):
                signals[k] = round(v, 1)
    return signals


def _compute_active_tools(tools_used: list[str] | None) -> list[str]:
    """Deduplicate tool names preserving order."""
    if not tools_used:
        return []
    seen = set()
    result = []
    for name in tools_used:
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def _format_trajectory(iteration_narratives: list[dict] | None) -> list[dict]:
    """Extract brief trajectory from iteration narratives."""
    if not iteration_narratives:
        return []
    trajectory = []
    for n in iteration_narratives:
        entry: dict = {
            "iter": n.get("iteration", "?"),
            "strategy": n.get("strategy_label", n.get("strategy", "unknown")),
        }
        wns_before = n.get("wns_before")
        wns_after = n.get("wns_after")
        if wns_before is not None and wns_after is not None:
            entry["wns_before"] = wns_before
            entry["wns_after"] = wns_after
            entry["delta"] = round(wns_after - wns_before, 4)
        trajectory.append(entry)
    return trajectory


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


def inject_context_snapshot_at_end(api_messages: list[dict], snapshot_yaml: str) -> None:
    """Inject or update the context snapshot as the LAST user message.

    Unlike inject_context_snapshot() which places the snapshot after system messages,
    this function appends it at the end of the message list so the LLM sees it
    with maximum attention weight right before generating a response.
    """
    # Find and remove existing snapshot message
    for i, msg in enumerate(api_messages):
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            if msg["content"].startswith(SNAPSHOT_HEADER):
                del api_messages[i]
                break

    # Append at end
    api_messages.append({"role": "user", "content": snapshot_yaml})
