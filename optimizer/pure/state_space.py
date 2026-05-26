"""StateSpace builder: transforms OptimizerState into the canonical 6-module
dashboard representation consumed by both the web UI (via serializer) and
the LLM context (via context_snapshot).

All functions are pure: they read OptimizerState, produce StateSpace.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..state import OptimizerState

from ..state import (
    DashboardGlobalState,
    DashboardTimingPath,
    DashboardTimingClusters,
    DashboardCongestionHotspot,
    DashboardPhysicalCongestion,
    DashboardHighFanoutNet,
    DashboardNetlistQuality,
    DashboardConstraints,
    DashboardDynamicGradient,
    StateSpace,
)
from .critical_path import DISPLAY_LIMIT_SNAPSHOT
from .tool_filter import LoopPhase

logger = logging.getLogger(__name__)

# Maximum violating paths in StateSpace (user spec says Top 20)
MAX_VIOLATING_PATHS = 20

# ── Annotation helpers for LLM context formatting ─────────────────
# These distinguish "not analyzed" (None) from "analyzed but empty" ([] / 0)
# so the LLM doesn't misinterpret empty lists as "no issues".


def _annotated_list(items, empty_reason: str) -> str | None:
    """Generate YAML list annotation.

    Returns:
        None  — caller should format items directly (list has data)
        str   — annotated YAML line for None (not analyzed) or [] (empty result)
    """
    if items is None:
        return '"N/A(not_analyzed)"'
    if not items:
        return f"[]  # {empty_reason}"
    return None  # has data — caller formats items


def _annotated_val(value, fmt: str | None = None, reason: str = "not_available") -> str:
    """Generate YAML scalar value with annotation for None.

    Non-None values are formatted with fmt (if provided) or str().
    None gets a quoted N/A with reason:  "N/A(reason)"
    """
    if value is None:
        return f'"N/A({reason})"'
    if fmt:
        return fmt.format(value)
    return str(value)

# Phase-aware module filters for LLM context injection.
PHASE_STATESPACE_MODULES: dict[LoopPhase, frozenset[str]] = {
    LoopPhase.ANALYZE: frozenset({
        "global_state", "timing_clusters", "physical_congestion",
        "netlist_quality", "dynamic_gradient",
    }),
    LoopPhase.SELECT_STRATEGY: frozenset({
        "global_state", "timing_clusters", "physical_congestion",
        "netlist_quality", "constraints_env", "dynamic_gradient",
    }),
    LoopPhase.EXECUTE: frozenset({
        "global_state", "dynamic_gradient",
    }),
    LoopPhase.EVALUATE: frozenset({
        "global_state", "dynamic_gradient",
    }),
}


# ── Public API ──────────────────────────────────────────────────────

def build_state_space(state: OptimizerState) -> StateSpace:
    """Build the canonical 6-module StateSpace from raw OptimizerState.

    This is the single entry point used by both the dashboard serializer
    and the LLM context injector.
    """
    return StateSpace(
        global_state=_build_global_state(state),
        timing_clusters=_build_timing_clusters(state),
        physical_congestion=_build_physical_congestion(state),
        netlist_quality=_build_netlist_quality(state),
        constraints_env=_build_constraints_env(state),
        dynamic_gradient=_build_dynamic_gradient(state),
    )


# ── Module 1: Global State & Targets ─────────────────────────────────

def _build_global_state(state: OptimizerState) -> DashboardGlobalState:
    """Build Module 1: global state, timing margins, and utilization."""
    timing = state.timing

    # Derive current stage from phase + tool context
    current_stage = _infer_current_stage(state)

    # Target frequency from clock period (MHz)
    target_freq = 0.0
    if timing.clock_period and timing.clock_period > 0:
        target_freq = 1000.0 / timing.clock_period

    # Utilization percentages from raw counts / device capacity
    lu = _compute_utilization(timing.resource_utilization, timing.device_capacity, "LUT")
    fu = _compute_utilization(timing.resource_utilization, timing.device_capacity, "FF")
    bu = _compute_utilization(timing.resource_utilization, timing.device_capacity, "BRAM")
    du = _compute_utilization(timing.resource_utilization, timing.device_capacity, "DSP")

    # Best WNS: handle -inf initial state
    best_wns = timing.best_wns if timing.best_wns > float('-inf') else None

    # Design scale from design_info (populated by RapidWright)
    di = timing.design_info or {}
    cell_count = di.get("cell_count", 0)
    net_count = di.get("net_count", 0)

    return DashboardGlobalState(
        current_stage=current_stage,
        iteration_count=state.iteration.current,
        target_frequency=round(target_freq, 1),
        wns_setup=timing.latest_wns,
        tns_setup=timing.latest_tns,
        whs_hold=timing.hold_wns,
        ths_hold=timing.hold_tns,
        lut_utilization=lu,
        ff_utilization=fu,
        bram_utilization=bu,
        dsp_utilization=du,
        best_wns=best_wns,
        best_wns_iteration=timing.best_wns_iteration,
        cell_count=cell_count,
        net_count=net_count,
    )


# ── Module 2: Timing Path Clusters ───────────────────────────────────

def _build_timing_clusters(state: OptimizerState) -> DashboardTimingClusters:
    """Build Module 2: Top-N violating timing path endpoints."""
    paths: list[DashboardTimingPath] = []
    for entry in state.timing.critical_paths[:MAX_VIOLATING_PATHS]:
        dp = _convert_critical_path(entry)
        paths.append(dp)
    return DashboardTimingClusters(top_violating_paths=paths)


# ── Module 3: Physical & Congestion Metrics ──────────────────────────

def _build_physical_congestion(state: OptimizerState) -> DashboardPhysicalCongestion:
    """Build Module 3: physical congestion and hotspot data."""
    cd = state.timing.congestion_data or {}

    hotspots: list[DashboardCongestionHotspot] = []
    raw_hotspots = cd.get("hotspots", [])
    if isinstance(raw_hotspots, list):
        for h in raw_hotspots:
            hotspots.append(DashboardCongestionHotspot(
                x1=h.get("x1", 0), y1=h.get("y1", 0),
                x2=h.get("x2", 0), y2=h.get("y2", 0),
                severity=h.get("severity", 0.0),
                dominant_module=h.get("dominant_module", ""),
            ))

    rs = state.timing.route_status or {}
    # long_route_nets_count: None if route_status not available, otherwise the count
    lr = rs.get("long_route_nets_count")
    long_route_val: int | None = lr if lr is not None else None
    return DashboardPhysicalCongestion(
        global_congestion_score=cd.get("global_score"),
        avg_wirelength=rs.get("avg_wirelength"),
        long_route_nets_count=long_route_val,
        congestion_hotspots=hotspots,
        pblock_overflow_count=cd.get("pblock_overflow_count"),
    )


# ── Module 4: Netlist Quality Profiler ───────────────────────────────

def _build_netlist_quality(state: OptimizerState) -> DashboardNetlistQuality:
    """Build Module 4: netlist architecture quality metrics."""
    nets: list[DashboardHighFanoutNet] = []
    for item in state.timing.high_fanout_nets:
        if isinstance(item, dict):
            nets.append(DashboardHighFanoutNet(
                net_name=item.get("net_name", item.get("net", "")),
                fanout_count=item.get("fanout", item.get("fanout_count", 0)),
                is_replicated=item.get("is_replicated", False),
            ))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            nets.append(DashboardHighFanoutNet(
                net_name=str(item[0]),
                fanout_count=int(item[1]),
                is_replicated=False,
            ))

    cs = state.timing.control_sets or {}
    di = state.timing.design_info or {}
    cell_summary = ""
    if di.get("top_cell_types"):
        cell_summary = ", ".join(
            f"{k}:{v}" for k, v in
            sorted(di["top_cell_types"].items(), key=lambda x: -x[1])[:8]
        )

    return DashboardNetlistQuality(
        total_control_sets=cs.get("total_control_sets", 0),
        avg_control_sets_per_slice=cs.get("avg_per_slice"),
        high_fanout_nets=nets,
        failed_inferences=[],  # synthesis log not available post-synth
        cross_domain_paths_count=state.timing.cross_domain_paths_count,
        cell_type_summary=cell_summary,
    )


# ── Module 5: Constraints Environment ───────────────────────────────

def _build_constraints_env(state: OptimizerState) -> DashboardConstraints:
    """Build Module 5: timing constraints environment."""
    clock_defs: dict[str, float] = {}
    cp = state.timing.clock_period
    if cp and cp > 0:
        clock_defs["clk_fpl26contest"] = round(1000.0 / cp, 1)

    ci = state.timing.constraints_info or {}
    return DashboardConstraints(
        clock_definitions=clock_defs,
        false_paths_count=ci.get("false_paths_count", 0),
        multicycle_paths_count=ci.get("multicycle_paths_count", 0),
        io_delay_defined_pct=ci.get("io_delay_defined_pct"),
        total_io_ports=ci.get("total_io_ports"),
        pvt_corner=state.timing.pvt_corner or "slow_0p95v_85c",
    )


# ── Module 6: Dynamic Gradient (Delta) ──────────────────────────────

def _build_dynamic_gradient(state: OptimizerState) -> DashboardDynamicGradient:
    """Build Module 6: iteration-over-iteration delta data."""
    # Delta WNS from strategy evaluation
    delta_wns = state.strategy.evaluation_wns_delta

    # Compute delta TNS from last two narratives
    delta_tns = _compute_delta_tns(state)

    # Delta congestion: needs before/after snapshots — not yet implemented
    delta_congestion = None  # TODO: store previous congestion score for comparison

    # Last action from strategy state
    last_action = state.strategy.current_strategy

    # Action status from evaluation result
    eval_result = state.strategy.evaluation_result
    if eval_result == "IMPROVED":
        action_status = "Success"
    elif eval_result == "REGRESSION":
        action_status = "Failed"
    elif eval_result == "UNCHANGED":
        action_status = "Success"  # No regression = success
    else:
        action_status = ""  # PENDING or not yet evaluated

    return DashboardDynamicGradient(
        delta_wns=delta_wns if delta_wns != 0.0 else None,
        delta_tns=delta_tns,
        delta_congestion=delta_congestion,
        last_action_taken=last_action,
        action_status=action_status,
    )


# ── LLM Context Formatting ───────────────────────────────────────────

def format_state_space_for_llm(
    *,
    space: StateSpace,
    phase: LoopPhase | None = None,
    handoff_summary: str = "",
    show_strategy_catalog: bool = False,
    exclude_strategies: list[str] | None = None,
    iteration_narratives: list[dict] | None = None,
    tools_used: list[str] | None = None,
    current_strategy: str = "",
    evaluation_result: str = "",
) -> str:
    """Format the 6-module StateSpace as YAML for LLM context injection.

    Phase-aware: only modules enabled in PHASE_STATESPACE_MODULES are shown.
    Appended as the last user message for maximum attention weight.
    """
    enabled = PHASE_STATESPACE_MODULES.get(phase) if phase else None
    lines: list[str] = []

    # ── Header ────────────────────────────────────────────────────
    phase_label = phase.value.upper() if phase else "OPTIMIZATION"
    lines.append(f"[{phase_label} — Context & Dashboard]")
    lines.append("")

    # ── Strategy catalog (SELECT_STRATEGY phase only) ──────────────
    if show_strategy_catalog:
        try:
            from strategy_library import get_strategy_catalog as _get_catalog
            catalog = _get_catalog(exclude_strategies=exclude_strategies)
            if catalog:
                lines.append("strategy_catalog:")
                for line in catalog.strip().split("\n"):
                    lines.append(f"  {line}")
                lines.append("")
        except Exception:
            pass

    # ── Handoff summary (from previous phase) ──────────────────────
    if handoff_summary:
        lines.append(handoff_summary)
        lines.append("")

    # ── Module 1: Global State & Targets ───────────────────────────
    if enabled is None or "global_state" in enabled:
        gs = space.global_state
        lines.append("# Module 1: Global State & Targets")
        lines.append("global_state:")
        lines.append(f"  current_stage: {gs.current_stage or 'UNKNOWN'}")
        lines.append(f"  iteration_count: {gs.iteration_count}")
        lines.append(f"  target_frequency: {gs.target_frequency}")
        lines.append(f"  wns_setup: {gs.wns_setup:.3f}" if gs.wns_setup is not None else '  wns_setup: "N/A(not_analyzed)"')
        lines.append(f"  tns_setup: {gs.tns_setup:.3f}" if gs.tns_setup is not None else '  tns_setup: "N/A(not_analyzed)"')
        lines.append(f"  best_wns: {_annotated_val(gs.best_wns, '{:.3f}', 'initial_state')}")
        if gs.best_wns_iteration is not None:
            lines.append(f"  best_wns_iteration: {gs.best_wns_iteration}")
        if gs.whs_hold is not None:
            lines.append(f"  whs_hold: {gs.whs_hold:.3f}")
        if gs.ths_hold is not None:
            lines.append(f"  ths_hold: {gs.ths_hold:.3f}")
        if gs.lut_utilization is not None:
            lines.append(f"  lut_utilization: {gs.lut_utilization:.2%}")
        if gs.ff_utilization is not None:
            lines.append(f"  ff_utilization: {gs.ff_utilization:.2%}")
        if gs.bram_utilization is not None:
            lines.append(f"  bram_utilization: {gs.bram_utilization:.2%}")
        if gs.dsp_utilization is not None:
            lines.append(f"  dsp_utilization: {gs.dsp_utilization:.2%}")
        if gs.cell_count > 0:
            lines.append(f"  cell_count: {gs.cell_count}")
            lines.append(f"  net_count: {gs.net_count}")
        lines.append("")

    # ── Module 2: Timing Path Clusters ─────────────────────────────
    if enabled is None or "timing_clusters" in enabled:
        tc = space.timing_clusters
        lines.append("# Module 2: Timing Path Clusters (Top Violating Endpoints)")
        lines.append("timing_clusters:")
        if tc.top_violating_paths:
            lines.append(f"  top_paths:  # {len(tc.top_violating_paths)} paths")
            for i, p in enumerate(tc.top_violating_paths):
                lines.append(f"    - endpoint: {p.endpoint_name}")
                if p.source_clock or p.dest_clock:
                    lines.append(f"      source_clock: {p.source_clock or '?'}")
                    lines.append(f"      dest_clock: {p.dest_clock or '?'}")
                lines.append(f"      slack: {p.slack:.3f}" if p.slack is not None else "      slack: ?")
                if p.logic_delay_pct is not None:
                    lines.append(f"      logic_delay_pct: {p.logic_delay_pct:.2f}")
                if p.route_delay_pct is not None:
                    lines.append(f"      route_delay_pct: {p.route_delay_pct:.2f}")
                if p.logic_levels is not None:
                    lines.append(f"      logic_levels: {p.logic_levels}")
                if p.path_group:
                    lines.append(f"      path_group: {p.path_group}")
        else:
            ann = _annotated_list(tc.top_violating_paths, "no_violating_paths_extracted")
            lines.append(f"  top_paths: {ann}")
        lines.append("")

    # ── Module 3: Physical & Congestion Metrics ────────────────────
    if enabled is None or "physical_congestion" in enabled:
        pc = space.physical_congestion
        lines.append("# Module 3: Physical & Congestion Metrics")
        lines.append("physical_congestion:")
        lines.append(f"  global_congestion_score: {_annotated_val(pc.global_congestion_score, '{:.2f}', 'congestion_analysis_not_supported')}")
        lines.append(f"  avg_wirelength: {_annotated_val(pc.avg_wirelength, '{:.1f}', 'data_not_available')}")
        lr = _annotated_val(pc.long_route_nets_count, reason="data_not_available")
        lines.append(f"  long_route_nets_count: {lr}")
        pb = _annotated_val(pc.pblock_overflow_count, reason="not_measured")
        lines.append(f"  pblock_overflow_count: {pb}")
        ann = _annotated_list(pc.congestion_hotspots, "no_congestion_hotspots")
        if ann is not None:
            lines.append(f"  hotspots: {ann}")
        elif pc.congestion_hotspots:
            lines.append(f"  hotspots:  # {len(pc.congestion_hotspots)} regions")
            for h in pc.congestion_hotspots[:5]:
                lines.append(f"    - bbox: [{h.x1},{h.y1}]-[{h.x2},{h.y2}]")
                lines.append(f"      severity: {h.severity:.2f}")
                lines.append(f"      module: {h.dominant_module}")
        else:
            lines.append("  hotspots: []  # no_congestion_hotspots")
        lines.append("")

    # ── Module 4: Netlist Quality Profiler ─────────────────────────
    if enabled is None or "netlist_quality" in enabled:
        nq = space.netlist_quality
        lines.append("# Module 4: Netlist Quality Profiler")
        lines.append("netlist_quality:")
        lines.append(f"  total_control_sets: {nq.total_control_sets}")
        lines.append(f"  avg_control_sets_per_slice: {_annotated_val(nq.avg_control_sets_per_slice, '{:.2f}', 'data_not_available')}")
        lines.append(f"  cross_domain_paths_count: {nq.cross_domain_paths_count}")
        if nq.cell_type_summary:
            lines.append(f"  top_cell_types: {nq.cell_type_summary}")
        if nq.high_fanout_nets:
            lines.append(f"  high_fanout_nets:  # {len(nq.high_fanout_nets)} nets")
            for net in nq.high_fanout_nets[:10]:
                rep = " (replicated)" if net.is_replicated else ""
                lines.append(f"    - {net.net_name}: fanout={net.fanout_count}{rep}")
        else:
            ann = _annotated_list(nq.high_fanout_nets, "no_high_fanout_nets_found")
            lines.append(f"  high_fanout_nets: {ann}")
        if nq.failed_inferences:
            lines.append(f"  failed_inferences:  # {len(nq.failed_inferences)}")
            for fi in nq.failed_inferences[:5]:
                lines.append(f"    - {fi}")
        else:
            ann = _annotated_list(nq.failed_inferences, "synthesis_log_not_available_post_synthesis")
            lines.append(f"  failed_inferences: {ann}")
        lines.append("")

    # ── Module 5: Constraints Environment ──────────────────────────
    if enabled is None or "constraints_env" in enabled:
        ce = space.constraints_env
        lines.append("# Module 5: Constraints Environment")
        lines.append("constraints_env:")
        if ce.clock_definitions:
            lines.append("  clock_definitions:")
            for clk_name, freq in ce.clock_definitions.items():
                lines.append(f"    {clk_name}: {freq:.1f} MHz")
        else:
            lines.append("  clock_definitions: {}")
        lines.append(f"  false_paths_count: {ce.false_paths_count}")
        lines.append(f"  multicycle_paths_count: {ce.multicycle_paths_count}")
        if ce.io_delay_defined_pct is None:
            if ce.total_io_ports == 0:
                iodp = '"N/A(no_io_ports)"'
            else:
                iodp = '"N/A(parse_failed)"'
        else:
            iodp = f"{ce.io_delay_defined_pct:.2%}"
        lines.append(f"  io_delay_defined_pct: {iodp}")
        lines.append(f"  pvt_corner: {ce.pvt_corner}")
        lines.append("")

    # ── Module 6: Dynamic Gradient Data ────────────────────────────
    if enabled is None or "dynamic_gradient" in enabled:
        dg = space.dynamic_gradient
        lines.append("# Module 6: Dynamic Gradient (Delta)")
        lines.append("dynamic_gradient:")
        lines.append(f"  delta_wns: {_annotated_val(dg.delta_wns, '{:+.4f}', 'initial_state_no_delta')}")
        lines.append(f"  delta_tns: {_annotated_val(dg.delta_tns, '{:+.4f}', 'initial_state_no_delta')}")
        lines.append(f"  delta_congestion: {_annotated_val(dg.delta_congestion, '{:+.4f}', 'initial_state_no_delta')}")
        lines.append(f"  last_action_taken: {dg.last_action_taken or 'none'}")
        lines.append(f"  action_status: {dg.action_status or 'PENDING'}")
        lines.append("")

    # ── Trajectory (if enabled in phase) ──────────────────────────
    if phase and "trajectory" not in PHASE_STATESPACE_MODULES.get(phase, frozenset()):
        pass  # trajectory now embedded in dynamic_gradient; skip separate section

    # ── Strategy lifecycle (brief, always shown) ──────────────────
    if current_strategy or evaluation_result:
        lines.append("strategy_lifecycle:")
        if current_strategy:
            lines.append(f"  current_strategy: {current_strategy}")
        if evaluation_result and evaluation_result != "PENDING":
            lines.append(f"  evaluation: {evaluation_result}")
        lines.append("")

    # ── Skill guidance (EXECUTE phase) ─────────────────────────────
    if phase == LoopPhase.EXECUTE and current_strategy:
        _append_skill_guidance(lines, current_strategy)

    lines.append("--- End Dashboard ---")
    return "\n".join(lines)


# ── Helpers ──────────────────────────────────────────────────────────

def _infer_current_stage(state: OptimizerState) -> str:
    """Infer current design stage from strategy phase and state context.

    Heuristic based on tool calls available at each stage:
    - SYNTHESIS: initial analysis not yet complete (no DCP loaded)
    - PLACEMENT: place_design available, routing not yet done
    - ROUTING: route_design available or in progress
    - POST_ROUTE: routing complete, phys_opt or evaluation ongoing
    """
    timing = state.timing
    # If we have a best_wns iteration > 0, we're past init (placement or later)
    if timing.best_wns_iteration is not None and timing.best_wns_iteration > 0:
        # Check if route_design has been called
        tools = state.iteration.tools_used
        if any("route" in t.lower() for t in tools):
            return "ROUTING"
        if any("phys_opt" in t.lower() for t in tools):
            return "POST_ROUTE"
        return "PLACEMENT"
    return "PLACEMENT"  # Default to placement (post-synthesis DCP)


def _compute_utilization(
    resource_utilization: dict | None,
    device_capacity: dict | None,
    resource_key: str,
) -> float | None:
    """Compute utilization percentage for a resource type."""
    if not resource_utilization or not device_capacity:
        return None
    raw = resource_utilization.get(resource_key, resource_utilization.get(resource_key.lower()))
    cap = device_capacity.get(resource_key, device_capacity.get(resource_key.lower()))
    if raw is not None and cap and cap > 0:
        util = raw / cap
        return round(util, 4) if 0.0 <= util <= 1.0 else round(util, 4)
    return None


def _convert_critical_path(entry) -> DashboardTimingPath:
    """Convert a CriticalPathEntry to a DashboardTimingPath."""
    from ..state import CriticalPathEntry

    endpoint = ""
    source_clock = ""
    dest_clock = ""
    path_group = ""

    # Extract endpoint from last cell name (convention: cell names encode hierarchy)
    if isinstance(entry, CriticalPathEntry) and entry.cells:
        last_cell = entry.cells[-1] if entry.cells else ""
        endpoint = last_cell

        # Try to extract clock domain from cell path (e.g., "clk_fpl26contest" prefix)
        clk_keywords = ["clk_fpl26contest", "clk", "CLK"]
        for ck in clk_keywords:
            if ck.lower() in last_cell.lower():
                dest_clock = ck if ck == "clk_fpl26contest" else "clk_fpl26contest"
                path_group = "clk_fpl26contest"
                break
        if not dest_clock:
            dest_clock = "clk_fpl26contest"
            path_group = "clk_fpl26contest"
        if not source_clock:
            source_clock = "clk_fpl26contest"

    # Compute delay percentages
    logic_delay_pct = None
    route_delay_pct = None
    if isinstance(entry, CriticalPathEntry):
        ld = entry.logic_delay
        nd = entry.net_delay
        if ld is not None and nd is not None and (ld + nd) > 0:
            total = ld + nd
            logic_delay_pct = round(ld / total, 4)
            route_delay_pct = round(nd / total, 4)

    return DashboardTimingPath(
        endpoint_name=endpoint,
        source_clock=source_clock,
        dest_clock=dest_clock,
        slack=entry.slack if isinstance(entry, CriticalPathEntry) else None,
        logic_delay_pct=logic_delay_pct,
        route_delay_pct=route_delay_pct,
        logic_levels=entry.levels if isinstance(entry, CriticalPathEntry) else None,
        path_group=path_group,
    )


def _compute_delta_tns(state: OptimizerState) -> float | None:
    """Compute TNS delta from the last two iteration narratives."""
    narratives = state.iteration.narratives
    if len(narratives) >= 2:
        prev_tns = narratives[-2].get("tns")
        curr_tns = narratives[-1].get("tns")
        if prev_tns is not None and curr_tns is not None:
            return round(curr_tns - prev_tns, 4)
    return None


def _append_skill_guidance(lines: list[str], current_strategy: str) -> None:
    """Append skill guidance section for EXECUTE phase."""
    try:
        from strategy_library import STRATEGIES as _STRATEGIES
        from .constants import SKILL_CHAIN_ACTIONS as _CHAIN_ACTIONS
        from .context_snapshot import STRATEGY_TO_PRIMARY_TOOL

        tool = STRATEGY_TO_PRIMARY_TOOL.get(current_strategy)
        if tool:
            lines.append("skill_guidance:")
            lines.append(f"  tool: {tool}")

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
                lines.append(f"  auto_chain: {' -> '.join(chain_steps)}")

            strat = _STRATEGIES.get(current_strategy)
            if strat and "sequence" in strat:
                seq_steps = []
                for s in strat["sequence"]:
                    step = s["step"]
                    platform = s.get("platform", "")
                    seq_steps.append(f"{step}({platform})" if platform else step)
                lines.append(f"  sequence: {' -> '.join(seq_steps)}")

            lines.append("  avoid: vivado_run_tcl — use the tool above instead.")
    except Exception:
        pass
