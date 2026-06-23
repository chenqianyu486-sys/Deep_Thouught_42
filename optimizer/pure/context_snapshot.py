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

# Map strategy names → primary skill tool names for skill_guidance.
STRATEGY_TO_PRIMARY_TOOL: dict[str, str] = {
    "PBLOCK": "rapidwright_execute_pblock_strategy",
    "PhysOpt": "vivado_phys_opt_design",
    "Fanout": "rapidwright_execute_fanout_strategy",
    "PinSwap": "rapidwright_optimize_pin_swapping",
    "LUTCascade": "rapidwright_flatten_lut_cascade",
    "CellReplication": "rapidwright_replicate_critical_cells",
    "CongestionSpreading": "rapidwright_execute_congestion_spreading",
    "RegisterRetiming": "rapidwright_execute_register_retiming",
    "NetSwap": "rapidwright_execute_net_swapping",
    "OptDesign": "rapidwright_execute_opt_design_strategy",
    "PhysOpt+RegisterRetiming": "vivado_physopt_and_route",
    "CombinationalRebalance": "rapidwright_execute_combinational_rebalancing_strategy",
    "LUTMUXFRepack": "rapidwright_execute_lut_muxf_repack_strategy",
    "MUXFTreeReorder": "rapidwright_execute_muxf_tree_reorder_strategy",
}

# Phase-aware section filters: which sections to show for each phase.
PHASE_DASHBOARD_SECTIONS: dict[LoopPhase, frozenset[str]] = {
    LoopPhase.ANALYZE: frozenset({
        "core_timing", "trajectory", "design_signals",
        "critical_paths", "active_tools", "strategy_lifecycle",
    }),
    LoopPhase.SELECT_STRATEGY: frozenset({
        "core_timing", "trajectory", "design_signals",
        "critical_paths", "strategy_lifecycle",
    }),
    LoopPhase.EXECUTE: frozenset({
        "core_timing", "violation_summary", "active_tools", "strategy_lifecycle",
    }),
    LoopPhase.EVALUATE: frozenset({
        "core_timing", "violation_summary", "active_tools", "strategy_lifecycle",
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
    show_strategy_catalog: bool = False,
) -> str:
    """Build a phase-aware data dashboard.

    Args:
        phase: Current LoopPhase. Controls which sections are shown.
        handoff_summary: Text from PhaseHandoff.to_phase_context_string().
        show_strategy_catalog: If True, inject strategy catalog from strategy_library.
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

    # ── Strategy catalog (SELECT_STRATEGY phase only) ─────────────
    if show_strategy_catalog:
        try:
            from strategy_library import get_strategy_catalog as _get_catalog
            catalog = _get_catalog()
            if catalog:
                lines.append("strategy_catalog:")
                for line in catalog.strip().split("\n"):
                    lines.append(f"  {line}")
                lines.append("")
        except Exception as e:
            logger.debug(f"[context_snapshot] strategy_catalog unavailable: {e}")

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

    # ── Skill guidance (EXECUTE phase) ──────────────────────────
    if current_strategy:
        try:
            from strategy_library import STRATEGIES as _STRATEGIES
            from .constants import SKILL_CHAIN_ACTIONS as _CHAIN_ACTIONS

            tool = STRATEGY_TO_PRIMARY_TOOL.get(current_strategy)
            if tool:
                lines.append("skill_guidance:")
                lines.append(f"  tool: {tool}")

                # Auto-chain info
                chain = _CHAIN_ACTIONS.get(tool)
                if chain:
                    chain_steps = []
                    for a in chain:
                        args = a.get("args", {})
                        args_from = a.get("args_from_skill", {})
                        step = a["tool"]
                        if args:
                            arg_str = ", ".join(f"{k}={v}" for k, v in args.items())
                            step += f"({arg_str})"
                        if args_from:
                            step += f"<{', '.join(args_from.keys())}>"
                        chain_steps.append(step)
                    lines.append(f"  auto_chain: {' → '.join(chain_steps)}")

                # Sequence from strategy_library
                strat = _STRATEGIES.get(current_strategy)
                if strat and "sequence" in strat:
                    seq_steps = []
                    for s in strat["sequence"]:
                        step = s["step"]
                        platform = s.get("platform", "")
                        seq_steps.append(f"{step}({platform})" if platform else step)
                    lines.append(f"  sequence: {' → '.join(seq_steps)}")

                # Anti-guidance for vivado_run_tcl
                lines.append("  avoid: vivado_run_tcl — use the tool above instead.")
        except Exception as e:
            logger.debug(f"[context_snapshot] skill_guidance unavailable: {e}")

    lines.append("--- End Dashboard ---")
    return "\n".join(lines)


def _stale_annotation(signal_key: str, refreshed_fields: set[str]) -> str:
    """Map a derived signal key back to its source field and check freshness."""
    # Static fields: never show stale marker
    _STATIC_KEYS = frozenset({
        "lut", "dsp", "bram", "uram", "design_type",
        "cell_count", "net_count", "top_cell_types",
        "clock_period", "target_frequency", "pvt_corner",
        "device_capacity", "total_control_sets", "avg_control_sets_per_slice",
    })
    if signal_key.lower() in _STATIC_KEYS:
        return ""
    # Freshness-gated fields
    if signal_key in ("max_fanout", "high_fanout_count"):
        return "" if "high_fanout_nets" in refreshed_fields else " (initial, not refreshed)"
    if signal_key.startswith("cp_spread_"):
        return "" if "critical_path_spread" in refreshed_fields else " (initial, not refreshed)"
    if signal_key in ("avg_wirelength", "long_route_nets_count"):
        return "" if "route_status" in refreshed_fields else " (initial, not refreshed)"
    if signal_key == "cross_domain_paths_count":
        return "" if "cdc_paths" in refreshed_fields else " (initial, not refreshed)"
    if signal_key in ("false_paths_count", "multicycle_paths_count", "io_delay_defined_pct"):
        return ""  # constraints are static (don't change during optimization)
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
            elif isinstance(n, (list, tuple)) and len(n) >= 2:
                fanouts.append(n[1])  # (net_name, fanout, path_count)
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

    Uses the canonical 6-module StateSpace representation via
    build_state_space() + format_state_space_for_llm().

    Called by each phase's _call_phase_llm() before every LLM call.
    The handoff summary (from previous phase) is merged into the same message
    so it gets maximum attention weight at the end of the conversation.
    """
    from .state_space import build_state_space, format_state_space_for_llm

    space = build_state_space(state)

    # Exclude previously failed strategies from the catalog
    _failed_strategies = [
        fs.strategy for fs in state.context.failed_strategies
    ] if state.context.failed_strategies else None

    # Blocked strategy lists for Dashboard strategy_lifecycle section.
    # Per-iteration cooldown (cleared each iteration at iteration_start).
    _blocked_this_iter = list(state.iteration.blocked_strategies) if state.iteration.blocked_strategies else None
    # TTL-persistent blocks (strategy_ineffective, unblocks after TTL).
    _blocked_ttl = None
    if state.context.failed_strategies:
        _ttl = []
        for fs in state.context.failed_strategies:
            if fs.reason == "strategy_ineffective":
                remaining = max(0, fs.blocked_until_iter - state.iteration.current)
                if remaining > 0:
                    _ttl.append(f"{fs.strategy}(unblocks in {remaining} iter)")
        if _ttl:
            _blocked_ttl = _ttl

    snapshot = format_state_space_for_llm(
        space=space,
        phase=phase,
        handoff_summary=state.strategy.last_handoff_text,
        show_strategy_catalog=(phase == LoopPhase.SELECT_STRATEGY),
        exclude_strategies=_failed_strategies,
        iteration_narratives=state.iteration.narratives,
        tools_used=state.iteration.tools_used,
        current_strategy=state.strategy.current_strategy,
        evaluation_result=state.strategy.evaluation_result,
        blocked_this_iteration=_blocked_this_iter,
        blocked_ttl=_blocked_ttl,
    )

    inject_context_snapshot_at_end(api_messages, snapshot)
