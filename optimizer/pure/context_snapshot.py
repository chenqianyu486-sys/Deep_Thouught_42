"""Context snapshot building pure functions.

Builds a phase-aware data dashboard injected before every LLM call.
Each phase sees only the sections relevant to its focus.
The handoff summary is merged into the same message.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..state import OptimizerState, CriticalPathEntry

from .critical_path import format_critical_paths_snapshot
from .tool_filter import LoopPhase

logger = logging.getLogger(__name__)

SNAPSHOT_HEADER = "--- Optimization Dashboard ---"

# Phase-aware section filters: which sections to show for each phase.
PHASE_DASHBOARD_SECTIONS: dict[LoopPhase, frozenset[str]] = {
    LoopPhase.ANALYZE: frozenset({
        "core_timing", "trajectory", "design_signals",
        "critical_paths", "active_tools", "strategy_lifecycle",
    }),
    LoopPhase.SELECT_STRATEGY: frozenset({
        "core_timing", "trajectory", "design_signals",
        "strategy_lifecycle",
    }),
    LoopPhase.EXECUTE: frozenset({
        "core_timing", "active_tools", "strategy_lifecycle",
    }),
    LoopPhase.EVALUATE: frozenset({
        "core_timing", "active_tools", "strategy_lifecycle",
    }),
}


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
    iteration_narratives: list[dict] | None = None,
    tools_used: list[str] | None = None,
    critical_paths: list | None = None,
    refreshed_fields: set[str] | None = None,
    input_dcp: str | None = None,
    output_dcp: str | None = None,
    strategy_phase: str = "",
    current_strategy: str = "",
    evaluation_result: str = "",
    phase: LoopPhase | None = None,
    handoff_summary: str = "",
) -> str:
    """Build a phase-aware data dashboard.

    Args:
        phase: Current LoopPhase. Controls which sections are shown.
        handoff_summary: Text from PhaseHandoff.to_phase_context_string().
        All other args: same as before, data values for the dashboard.

    Returns:
        Dashboard text injected as the last user message.
    """
    enabled = PHASE_DASHBOARD_SECTIONS.get(phase) if phase else None
    lines = []

    # ── Title ───────────────────────────────────────────────────
    phase_label = phase.value.upper() if phase else ""
    lines.append(f"[{phase_label} — Context & Dashboard]")
    lines.append("")

    # ── Handoff summary (from previous phase) ─────────────────────
    if handoff_summary:
        lines.append(handoff_summary)
        lines.append("")

    # ── Core timing metrics ───────────────────────────────────────
    if enabled is None or "core_timing" in enabled:
        if clock_period:
            lines.append(f"clock_period: {clock_period:.3f}")
        lines.append(f"wns: {current_wns:.3f}" if current_wns is not None else "wns: N/A")
        if best_wns is not None and best_wns > float('-inf'):
            lines.append(f"wns_best: {best_wns:.3f}")
        elif current_wns is not None:
            lines.append(f"wns_best: {current_wns:.3f}")
        lines.append(f"wns_best_iter: {best_wns_iteration}" if best_wns_iteration is not None else "wns_best_iter: N/A")
        lines.append(f"tns: {tns:.3f}" if tns is not None else "tns: N/A")
        lines.append(f"failing_endpoints: {failing_endpoints}" if failing_endpoints is not None else "failing_endpoints: N/A")
        lines.append("")

    # ── Trajectory (work history across iterations) ───────────────
    if enabled is None or "trajectory" in enabled:
        trajectory = _format_trajectory(iteration_narratives)
        if trajectory:
            lines.append("trajectory:")
            for entry in trajectory:
                lines.append(f"  - iter: {entry['iter']}")
                lines.append(f"    strategy: {entry['strategy']}")
                if "wns_before" in entry:
                    lines.append(f"    wns_before: {entry['wns_before']:.3f}")
                    lines.append(f"    wns_after: {entry['wns_after']:.3f}")
                    lines.append(f"    delta: {entry['delta']:+.4f}")
        else:
            lines.append("trajectory: []")
        lines.append("")

    # ── Design signals (fanout, congestion, spread, utilization) ──
    if enabled is None or "design_signals" in enabled:
        signals = _compute_design_signals(high_fanout_nets, critical_path_spread, resource_utilization)
        if signals:
            refreshed = refreshed_fields or set()
            lines.append("design_signals:")
            for k, v in signals.items():
                stale_tag = _stale_annotation(k, refreshed)
                lines.append(f"  {k}: {v}{stale_tag}")
            # Design type hint for combinational-only designs
            if signals.get("design_type") == "combinational_only":
                lines.append("")
                lines.append("design_type_note: Pure combinational design (no FFs). "
                              "PBLOCK placement is the primary lever for reducing routing delay.")
        else:
            lines.append("design_signals: {}")
        lines.append("")

    # ── Critical paths ───────────────────────────────────────────
    if enabled is None or "critical_paths" in enabled:
        if critical_paths:
            cp_lines = format_critical_paths_snapshot(critical_paths)
            if cp_lines:
                lines.append("critical_paths:")
                for cp in cp_lines:
                    lines.append(f"  - {cp}")
            else:
                lines.append("critical_paths: []")
        else:
            lines.append("critical_paths: []")
        lines.append("")

    # ── Active tools (current phase only) ─────────────────────────
    if enabled is None or "active_tools" in enabled:
        active = _compute_active_tools(tools_used)
        if active:
            lines.append("active_tools:")
            for tool in active:
                lines.append(f"  - {tool}")
        else:
            lines.append("active_tools: []")
        lines.append("")

    # ── Strategy lifecycle ───────────────────────────────────────
    if enabled is None or "strategy_lifecycle" in enabled:
        if strategy_phase or current_strategy:
            lines.append("strategy_lifecycle:")
            if strategy_phase:
                lines.append(f"  current_phase: {strategy_phase}")
            if current_strategy:
                lines.append(f"  current_strategy: {current_strategy}")
            if evaluation_result and evaluation_result != "PENDING":
                lines.append(f"  evaluation: {evaluation_result}")
        lines.append("")

    lines.append("--- End Dashboard ---")
    return "\n".join(lines)


def _stale_annotation(signal_key: str, refreshed_fields: set[str]) -> str:
    """Map a derived signal key back to its source field and check freshness."""
    _STATIC_RESOURCE_KEYS = frozenset({"lut", "dsp", "bram", "uram", "design_type"})
    if signal_key.lower() in _STATIC_RESOURCE_KEYS:
        return ""
    if signal_key in ("max_fanout", "high_fanout_count"):
        return "" if "high_fanout_nets" in refreshed_fields else " (initial, not refreshed)"
    if signal_key.startswith("cp_spread_"):
        return "" if "critical_path_spread" in refreshed_fields else " (initial, not refreshed)"
    return "" if "resource_utilization" in refreshed_fields else " (initial, not refreshed)"


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
                signals[k] = int(v)
        ff_count = resource_utilization.get("FF", resource_utilization.get("ff", None))
        if ff_count is not None and ff_count == 0:
            signals["design_type"] = "combinational_only"
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
    """Inject or update the context snapshot as the FIRST user message.

    Scans for an existing snapshot (by header marker), removes it to
    prevent accumulation, then inserts a fresh snapshot after all
    system messages. (V1 compatibility mode.)
    """
    header_markers = (
        "[ANALYZE — Context & Dashboard]",
        "[SELECT_STRATEGY — Context & Dashboard]",
        "[EXECUTE — Context & Dashboard]",
        "[EVALUATE — Context & Dashboard]",
        "--- Optimization Dashboard ---",
    )
    for i, msg in enumerate(api_messages):
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            if any(msg["content"].startswith(m) for m in header_markers):
                del api_messages[i]
                break
    insert_idx = 0
    for i, msg in enumerate(api_messages):
        if msg.get("role") != "system":
            insert_idx = i
            break
    else:
        insert_idx = len(api_messages)
    api_messages.insert(insert_idx, {"role": "user", "content": snapshot_yaml})


def inject_context_snapshot_at_end(api_messages: list[dict], snapshot_yaml: str) -> None:
    """Inject or update a dashboard message as the LAST user message.

    Finds and replaces any existing dashboard message (by header marker),
    then appends the new one at the end.
    """
    # Remove any existing merged dashboard message
    header_markers = (
        "[ANALYZE — Context & Dashboard]",
        "[SELECT_STRATEGY — Context & Dashboard]",
        "[EXECUTE — Context & Dashboard]",
        "[EVALUATE — Context & Dashboard]",
        "--- Optimization Dashboard ---",
    )
    for i, msg in enumerate(api_messages):
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            if any(msg["content"].startswith(m) for m in header_markers):
                del api_messages[i]
                break

    # Append at end for maximum attention weight
    api_messages.append({"role": "user", "content": snapshot_yaml})


def inject_merged_dashboard(
    api_messages: list,
    state: "OptimizerState",
    phase: LoopPhase,
) -> None:
    """Build and inject merged handoff + dashboard as the last user message.

    Called by each phase's _call_phase_llm() before every LLM call.
    The handoff summary (from previous phase) is merged into the same message
    so it gets maximum attention weight at the end of the conversation.
    """
    current_wns = state.timing.latest_wns
    best_wns = state.timing.best_wns if state.timing.best_wns > float('-inf') else None

    snapshot = build_context_snapshot(
        clock_period=state.timing.clock_period,
        current_wns=current_wns,
        best_wns=best_wns,
        best_wns_iteration=state.timing.best_wns_iteration,
        tns=state.timing.latest_tns,
        failing_endpoints=state.timing.latest_failing_endpoints,
        high_fanout_nets=state.timing.high_fanout_nets or [],
        critical_path_spread=state.timing.critical_path_spread,
        resource_utilization=state.timing.resource_utilization,
        iteration_narratives=state.iteration.narratives,
        tools_used=state.iteration.tools_used,
        critical_paths=state.timing.critical_paths,
        refreshed_fields=state.timing.refreshed_fields,
        input_dcp=str(state.control.input_dcp.resolve()) if state.control.input_dcp else None,
        output_dcp=str(state.control.output_dcp.resolve()) if state.control.output_dcp else None,
        strategy_phase=state.strategy.current_phase,
        current_strategy=state.strategy.current_strategy,
        evaluation_result=state.strategy.evaluation_result,
        phase=phase,
        handoff_summary=state.strategy.last_handoff_text,
    )

    inject_context_snapshot_at_end(api_messages, snapshot)
