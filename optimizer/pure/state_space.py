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
    CriticalPathEntry,
    DashboardGlobalState,
    DashboardTimingPath,
    DashboardPathCluster,
    DashboardTimingClusters,
    DashboardViolationSummary,
    DashboardCongestionHotspot,
    DashboardPhysicalCongestion,
    DashboardHighFanoutNet,
    DashboardNetlistQuality,
    DashboardConstraints,
    DashboardDynamicGradient,
    DashboardArchitectureOverview,
    DashboardModuleEntry,
    DesignState,
    StateSpace,
)
from .critical_path import DISPLAY_LIMIT_SNAPSHOT, MAX_DELAY_HOTSPOTS
from .timing import compute_violation_summary
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


def _freshness_tag(field_key: str, field_freshness: dict[str, str] | None) -> str:
    """Return freshness annotation for a dashboard field: ``[fresh]`` or ``[stale]``.

    Args:
        field_key: Field name in field_freshness dict (e.g. "timing_summary").
        field_freshness: The dict from state.timing.field_freshness, or None.

    Returns:
        " [fresh]" if the field was refreshed by a recent tool call,
        " [stale]" if design was modified since collection,
        "" if unknown or fresh_by_default.
    """
    if not field_freshness:
        return ""
    status = field_freshness.get(field_key)
    if status == "fresh":
        return " [fresh]"
    elif status == "stale":
        return " [stale]"
    return ""


# Phase-aware module filters for LLM context injection.
PHASE_STATESPACE_MODULES: dict[LoopPhase, frozenset[str]] = {
    LoopPhase.ANALYZE: frozenset({
        "global_state", "timing_clusters", "physical_congestion",
        "netlist_quality", "dynamic_gradient", "architecture_overview",
        "design_structure",  # cell composition (MUXF/LUT/FF ratios) for strategy-aware analysis
    }),
    LoopPhase.SELECT_STRATEGY: frozenset({
        "global_state", "timing_clusters", "physical_congestion",
        "netlist_quality", "constraints_env", "dynamic_gradient",
        "architecture_overview", "design_structure", "recent_analysis",
    }),
    LoopPhase.EXECUTE: frozenset({
        "global_state", "timing_clusters_summary", "dynamic_gradient",
    }),
    LoopPhase.EVALUATE: frozenset({
        "global_state", "timing_clusters_summary", "dynamic_gradient",
    }),
}


# ── Public API ──────────────────────────────────────────────────────

def build_state_space(state: OptimizerState) -> StateSpace:
    """Build the canonical 7-module StateSpace from raw OptimizerState.

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
        architecture_overview=_build_architecture_overview(state),
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
        baseline_wns=timing.baseline_wns,
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
        design_state=timing.design_state,
    )


# ── Module 2: Timing Path Clusters ───────────────────────────────────

def _build_timing_clusters(state: OptimizerState) -> DashboardTimingClusters:
    """Build Module 2: Top-N violating timing path endpoints + violation summary."""
    paths: list[DashboardTimingPath] = []
    for entry in state.timing.critical_paths[:MAX_VIOLATING_PATHS]:
        dp = _convert_critical_path(entry)
        paths.append(dp)

    vs_data = compute_violation_summary(
        state.timing.critical_paths,
        failing_endpoints=state.timing.latest_failing_endpoints,
    )
    violation_summary = None
    path_clusters: list[DashboardPathCluster] = []
    if vs_data is not None:
        for c in vs_data.get("path_clusters", []):
            path_clusters.append(DashboardPathCluster(
                cluster_id=c["cluster_id"],
                representative_path_idx=c["representative_path_idx"],
                path_count=c["path_count"],
                slack_range=(
                    f"{c['worst_slack']:.3f}ns to {c['best_slack']:.3f}ns"
                    if c["worst_slack"] is not None and c["best_slack"] is not None
                    else "N/A"
                ),
                avg_logic_delay_pct=c["avg_logic_delay_pct"],
                avg_logic_levels=c["avg_logic_levels"],
                module=c["module"],
            ))
        violation_summary = DashboardViolationSummary(
            total_failing_endpoints=vs_data["total_failing_endpoints"],
            severity_distribution=vs_data["severity_distribution"],
            delay_profile_breakdown=vs_data["delay_profile_breakdown"],
            logic_level_distribution=vs_data["logic_level_distribution"],
            top_violating_modules=vs_data["top_violating_modules"],
            path_clusters=path_clusters,
        )

    # Extract failing endpoint names from critical path cells (last cell = endpoint)
    failing_endpoint_names = state.timing.failing_endpoint_names

    return DashboardTimingClusters(
        top_violating_paths=paths,
        violation_summary=violation_summary,
        path_clusters=path_clusters if vs_data else [],
        failing_endpoint_names=failing_endpoint_names,
    )


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
        congestion_level=rs.get("congestion_level"),
        total_wirelength=rs.get("total_wirelength"),
        max_wirelength=rs.get("max_wirelength"),
        timing_violated_nets=rs.get("timing_violated_nets"),
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

    # Last action from strategy state — only show during EXECUTE/EVALUATE.
    # During ANALYZE/SELECT_STRATEGY of a new iteration, current_strategy
    # and evaluation_result are stale from the previous iteration and
    # mislead the LLM into thinking a strategy was already executed.
    current_phase = state.strategy.current_phase
    if current_phase in ("EXECUTE_STRATEGY", "EVALUATE"):
        last_action = state.strategy.current_strategy
        eval_result = state.strategy.evaluation_result
        if eval_result == "IMPROVED":
            action_status = "Success"
        elif eval_result == "REGRESSION":
            action_status = "Failed"
        elif eval_result == "UNCHANGED":
            action_status = "Success"  # No regression = success
        else:
            action_status = ""  # PENDING or not yet evaluated
    else:
        last_action = ""
        action_status = ""

    return DashboardDynamicGradient(
        delta_wns=delta_wns if delta_wns != 0.0 else None,
        delta_tns=delta_tns,
        delta_congestion=delta_congestion,
        last_action_taken=last_action,
        action_status=action_status,
    )


# ── Module 7: Architecture Overview ──────────────────────────────────────


def _extract_module_insights(state: OptimizerState) -> dict:
    """Extract module-level architecture insights from critical path cell names.

    Cell names like ``design_i/aes_core/sbox/LUT6`` encode hierarchical
    module structure.  This function parses those names to build a
    module-level view of timing hotspots — **at zero cost**, without any
    additional Vivado Tcl or RapidWright calls.

    Returns a dict with:
        top_modules (list[dict]): modules sorted by critical path hit count,
            each with ``name``, ``critical_path_hits``, ``cell_distribution_pct``,
            ``sub_modules``.
        cross_module_paths (int): number of paths spanning ≥2 modules.
        intra_module_paths (int): number of paths within a single module.
        deepest_module (str | None): module with the highest logic depth.
        total_cells_analyzed (int): total leaf cells examined.
    """
    paths = state.timing.critical_paths
    if not paths:
        return {
            "top_modules": [],
            "cross_module_paths": 0,
            "intra_module_paths": 0,
            "deepest_module": None,
            "total_cells_analyzed": 0,
        }

    # Count module prefix occurrences across all critical path cells.
    # Cell format:  <top_inst>/<module>/<sub_module>/.../<cell_type>
    module_hits: dict[str, int] = {}
    module_sub_modules: dict[str, set[str]] = {}

    total_cells = 0
    cross_module_count = 0
    intra_module_count = 0

    for path in paths:
        cells = path.cells if isinstance(path, CriticalPathEntry) else []
        if not cells:
            continue

        path_modules: set[str] = set()
        for cell in cells:
            parts = cell.split("/")
            if len(parts) >= 2:
                module = parts[1]  # first level after top_inst
                path_modules.add(module)
                module_hits[module] = module_hits.get(module, 0) + 1
                total_cells += 1
                # Level 2: sub-module
                if len(parts) >= 3:
                    sub = parts[2]
                    module_sub_modules.setdefault(module, set()).add(sub)

        if len(path_modules) >= 2:
            cross_module_count += 1
        elif len(path_modules) == 1:
            intra_module_count += 1

    # Sort modules by hit count
    sorted_modules = sorted(module_hits.items(), key=lambda x: -x[1])

    top_modules = []
    for name, hits in sorted_modules[:10]:
        sub_list = sorted(module_sub_modules.get(name, set()))[:5]
        top_modules.append({
            "name": name,
            "critical_path_hits": hits,
            "cell_distribution_pct": round(hits / total_cells * 100, 1) if total_cells else 0.0,
            "sub_modules": sub_list,
        })

    # Find deepest module (the module contributing to the path with
    # the highest logic_levels)
    deepest_module: str | None = None
    max_levels = -1
    for path in paths:
        if not isinstance(path, CriticalPathEntry):
            continue
        levels = path.levels
        cells = path.cells
        if levels is not None and levels > max_levels and cells:
            max_levels = levels
            module_counts: dict[str, int] = {}
            for cell in cells:
                parts = cell.split("/")
                if len(parts) >= 2:
                    m = parts[1]
                    module_counts[m] = module_counts.get(m, 0) + 1
            if module_counts:
                deepest_module = max(module_counts, key=module_counts.get)

    return {
        "top_modules": top_modules,
        "cross_module_paths": cross_module_count,
        "intra_module_paths": intra_module_count,
        "deepest_module": deepest_module,
        "total_cells_analyzed": total_cells,
    }


def _build_architecture_overview(state: OptimizerState) -> DashboardArchitectureOverview:
    """Build Module 7: architecture overview from critical path cell names."""
    insights = _extract_module_insights(state)

    entries = []
    for m in insights["top_modules"]:
        entries.append(DashboardModuleEntry(
            name=m["name"],
            critical_path_hits=m["critical_path_hits"],
            cell_distribution_pct=m["cell_distribution_pct"],
            sub_modules=m["sub_modules"],
        ))

    return DashboardArchitectureOverview(
        top_modules=entries,
        cross_module_paths=insights["cross_module_paths"],
        intra_module_paths=insights["intra_module_paths"],
        deepest_module=insights["deepest_module"],
        total_cells_analyzed=insights["total_cells_analyzed"],
    )


# ── LLM Context Formatting ───────────────────────────────────────────

def format_state_space_for_llm(
    *,
    space: StateSpace,
    phase: LoopPhase | None = None,
    handoff_summary: str = "",
    show_strategy_catalog: bool = False,
    exclude_strategies: list[str] | None = None,
    blocked_strategies: dict[str, str] | None = None,
    iteration_narratives: list[dict] | None = None,
    tools_used: list[str] | None = None,
    current_strategy: str = "",
    evaluation_result: str = "",
    state: OptimizerState | None = None,
) -> str:
    """Format the 6-module StateSpace as YAML for LLM context injection.

    Phase-aware: only modules enabled in PHASE_STATESPACE_MODULES are shown.
    Appended as the last user message for maximum attention weight.
    """
    enabled = PHASE_STATESPACE_MODULES.get(phase) if phase else None
    lines: list[str] = []

    # Freshness tag helper — available to all module renderers below
    _ff = state.timing.field_freshness if state else None
    _tag = lambda k: _freshness_tag(k, _ff)

    # ── Header ────────────────────────────────────────────────────
    phase_label = phase.value.upper() if phase else "OPTIMIZATION"
    lines.append(f"[{phase_label} — Context & Dashboard]")
    lines.append("")

    # ── Strategy catalog (SELECT_STRATEGY phase only) ──────────────
    if show_strategy_catalog:
        try:
            from strategy_library import get_strategy_catalog as _get_catalog
            catalog = _get_catalog(exclude_strategies=exclude_strategies,
                                   blocked_strategies=blocked_strategies)
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
        if gs.design_state == DesignState.UNPLACED:
            lines.append("  # WARNING: Design is unplaced — WNS based on wireload estimates, may be highly inaccurate")
        elif gs.design_state == DesignState.PLACED:
            lines.append("  # WARNING: Design has placement only — WNS based on estimated routing delays")
        lines.append(f"  iteration_count: {gs.iteration_count}")
        lines.append(f"  target_frequency: {gs.target_frequency}")
        lines.append(f"  wns_setup: {gs.wns_setup:.3f}{_tag('timing_summary')}" if gs.wns_setup is not None else '  wns_setup: "N/A(not_analyzed)"')
        if gs.baseline_wns is not None:
            lines.append(f"  baseline_wns: {gs.baseline_wns:.3f}  # iteration start WNS, refreshed on strategy switch")
        lines.append(f"  tns_setup: {gs.tns_setup:.3f}{_tag('timing_summary')}" if gs.tns_setup is not None else '  tns_setup: "N/A(not_analyzed)"')
        lines.append(f"  best_wns: {_annotated_val(gs.best_wns, '{:.3f}', 'initial_state')}")
        if gs.best_wns_iteration is not None:
            lines.append(f"  best_wns_iteration: {gs.best_wns_iteration}")
        if gs.whs_hold is not None:
            lines.append(f"  whs_hold: {gs.whs_hold:.3f}{_tag('timing_summary')}")
        if gs.ths_hold is not None:
            lines.append(f"  ths_hold: {gs.ths_hold:.3f}")
        if gs.lut_utilization is not None:
            lines.append(f"  lut_utilization: {gs.lut_utilization:.2%}{_tag('resource_utilization')}")
        if gs.ff_utilization is not None:
            lines.append(f"  ff_utilization: {gs.ff_utilization:.2%}{_tag('resource_utilization')}")
        if gs.bram_utilization is not None:
            lines.append(f"  bram_utilization: {gs.bram_utilization:.2%}")
        if gs.dsp_utilization is not None:
            lines.append(f"  dsp_utilization: {gs.dsp_utilization:.2%}")
        if gs.cell_count > 0:
            lines.append(f"  cell_count: {gs.cell_count}")
            lines.append(f"  net_count: {gs.net_count}")
        # Notify LLM that critical path data is pre-loaded (EXECUTE phase only)
        if phase == LoopPhase.EXECUTE and state and state.timing.critical_paths:
            total_cells = sum(len(cp.cells) for cp in state.timing.critical_paths[:15] if cp.cells)
            lines.append(f"  critical_paths_available_in_state: true (n={len(state.timing.critical_paths[:15])}, cells={total_cells})")
            lines.append("  # NOTE: Strategy tools auto-inject critical path data. Do NOT extract via TCL.")
        # Design delay profile (SELECT_STRATEGY only) — pure descriptive data, no recommendations
        if phase == LoopPhase.SELECT_STRATEGY and space.timing_clusters.top_violating_paths:
            logic_pcts = [
                p.logic_delay_pct for p in space.timing_clusters.top_violating_paths
                if p.logic_delay_pct is not None
            ]
            if logic_pcts:
                avg_logic = sum(logic_pcts) / len(logic_pcts)
                # Append delay_profile_breakdown counts for richer context
                dp_breakdown = ""
                vs = space.timing_clusters.violation_summary
                if vs and vs.delay_profile_breakdown:
                    dp = vs.delay_profile_breakdown
                    ld = dp.get("logic_dominated", 0)
                    rd = dp.get("route_dominated", 0)
                    mx = dp.get("mixed", 0)
                    dp_breakdown = f", logic={ld}, route={rd}, mixed={mx}"
                if avg_logic > 0.7:
                    profile = "logic_delay_dominated"
                elif avg_logic < 0.3:
                    profile = "route_delay_dominated"
                else:
                    profile = "mixed"
                lines.append(f"  design_delay_profile: {profile}"
                             f"  # avg_logic_delay_pct={avg_logic:.2f}{dp_breakdown}")
                if gs.ff_utilization is not None and gs.ff_utilization < 0.02:
                    lines.append(f"  ff_utilization: {gs.ff_utilization:.2%}  # low FF count")
        lines.append("")

    # ── Module 2: Timing Path Clusters ─────────────────────────────
    if enabled is None or "timing_clusters" in enabled:
        tc = space.timing_clusters
        lines.append("# Module 2: Timing Path Clusters (Top Violating Endpoints)")
        lines.append("timing_clusters:")
        # ── Freshness indicator ──
        if state and state.timing.critical_paths_stale is not None:
            stale_label = "true (place/route changed)" if state.timing.critical_paths_stale else "false"
            cp_fresh = _tag("critical_path_cells") if state else ""
            ext_iter = state.timing.critical_paths_iteration
            total_fe = state.timing.latest_failing_endpoints
            fresh_line = f"  freshness: extracted_iteration={ext_iter}, stale={stale_label}{cp_fresh}"
            if total_fe is not None:
                fresh_line += f", total_failing_from_timing_report={total_fe}"
            lines.append(fresh_line)
        # ── Empty data graceful degradation ──
        if not tc.top_violating_paths and tc.violation_summary is None:
            total_fe = state.timing.latest_failing_endpoints if state else None
            if total_fe is not None and total_fe > 0:
                lines.append(f"  status: not_extracted_or_all_cells_invalid")
                lines.append(f"  # Total failing endpoints from timing report: {total_fe}. "
                             f"Re-extract via vivado_extract_critical_path_cells for detailed path data.")
            else:
                lines.append(f"  status: no_data")
            lines.append("")
            lines.append("  # No detailed path data (not extracted or all timing met)")
        # ── Normal rendering ──
        if tc.top_violating_paths:
            lines.append(f"  top_paths:  # {len(tc.top_violating_paths)} paths")
            for i, p in enumerate(tc.top_violating_paths):
                lines.append(f"    - endpoint: {p.endpoint_name}")
                # D1: startpoint (ANALYZE/SELECT_STRATEGY only — EXECUTE/EVALUATE omit to save tokens)
                if p.startpoint and phase in (LoopPhase.ANALYZE, LoopPhase.SELECT_STRATEGY, None):
                    lines.append(f"      startpoint: {p.startpoint}")
                if p.source_clock or p.dest_clock:
                    lines.append(f"      source_clock: {p.source_clock or '?'}")
                    lines.append(f"      dest_clock: {p.dest_clock or '?'}")
                # D2: cross-clock flag
                if p.is_cross_clock:
                    lines.append(f"      cross_clock: true")
                lines.append(f"      slack: {p.slack:.3f}" if p.slack is not None else "      slack: ?")
                if p.logic_delay_pct is not None:
                    lines.append(f"      logic_delay_pct: {p.logic_delay_pct:.2f}")
                if p.route_delay_pct is not None:
                    lines.append(f"      route_delay_pct: {p.route_delay_pct:.2f}")
                if p.logic_levels is not None:
                    lines.append(f"      logic_levels: {p.logic_levels}")
                if p.path_group:
                    lines.append(f"      path_group: {p.path_group}")
                # Cell type chain (ANALYZE/SELECT_STRATEGY only)
                if p.cell_type_chain and phase in (LoopPhase.ANALYZE, LoopPhase.SELECT_STRATEGY, None):
                    lines.append(f"      cell_type_chain: {p.cell_type_chain}")
                    if p.cell_type_counts:
                        counts_str = ", ".join(f"{k}={v}" for k, v in sorted(p.cell_type_counts.items()))
                        lines.append(f"      # cell counts: {counts_str}")
                # D2: clock skew / uncertainty (ANALYZE/SELECT_STRATEGY full, EXECUTE/EVALUATE inline)
                if p.clock_skew is not None:
                    lines.append(f"      clock_skew: {p.clock_skew:.3f}ns")
                if p.clock_uncertainty is not None:
                    lines.append(f"      clock_uncertainty: {p.clock_uncertainty:.3f}ns")
                # D1: delay hotspots — full list in ANALYZE/SELECT, single-line summary in EXECUTE/EVALUATE
                if p.delay_hotspots:
                    if phase in (LoopPhase.ANALYZE, LoopPhase.SELECT_STRATEGY, None):
                        lines.append(f"      delay_hotspots:  # top contributors")
                        for h in p.delay_hotspots:
                            pct_str = f" ({h['pct_of_path']:.0%})" if h.get('pct_of_path') is not None else ""
                            loc_str = f" @ {h['location']}" if h.get('location') else ""
                            incr_str = f"{h['incr']:.3f}ns" if h.get('incr') is not None else "?"
                            lines.append(f"        - {h['name']} [{h['type']}] {incr_str}{pct_str}{loc_str}")
                    else:
                        # EXECUTE/EVALUATE: single-line summary (oracle m5 mitigation)
                        # NOTE: keep [type] annotation for clarity (net vs cell distinction)
                        parts = []
                        for h in p.delay_hotspots[:3]:
                            pct_str = f"({h['pct_of_path']:.0%})" if h.get('pct_of_path') is not None else ""
                            typ = h.get('type', '')
                            type_tag = f"[{typ}]" if typ else ""
                            parts.append(f"{h['name']}={h['incr']:.3f}ns{type_tag}{pct_str}" if h.get('incr') is not None else f"{h['name']}=?")
                        lines.append(f"      delay_hotspots: {', '.join(parts)}")
        else:
            ann = _annotated_list(tc.top_violating_paths, "no_violating_paths_extracted")
            lines.append(f"  top_paths: {ann}")
        # Violation summary (compact, always shown with full Module 2)
        if tc.violation_summary is not None:
            lines.append("")
            lines.append(f"  # Violation summary: {tc.violation_summary.total_failing_endpoints or '?'} failing endpoints")
            _format_violation_summary(
                lines, tc.violation_summary, indent="  ",
                path_clusters=tc.path_clusters,
                failing_endpoint_names=tc.failing_endpoint_names,
                total_failing_count=state.timing.latest_failing_endpoints if state else None,
            )
        lines.append("")

    # ── Module 2b: Timing Violation Summary (compact, for EXECUTE/EVALUATE phases) ──
    if enabled is not None and "timing_clusters_summary" in enabled:
        tc = space.timing_clusters
        vs = tc.violation_summary
        lines.append("# Module 2: Timing Violation Summary (compact)")
        lines.append("timing_violation_summary:")
        if vs is not None:
            lines.append(f"  failing_endpoints: {_annotated_val(vs.total_failing_endpoints, reason='not_analyzed')}")
            # Freshness indicator (compact)
            if state and state.timing.critical_paths_stale is not None:
                stale_label = "true (place/route changed)" if state.timing.critical_paths_stale else "false"
                ext_iter = state.timing.critical_paths_iteration
                fresh_line = f"  freshness: extracted_iteration={ext_iter}, stale={stale_label}"
                total_fe = state.timing.latest_failing_endpoints
                if total_fe is not None:
                    fresh_line += f", total_failing_from_timing_report={total_fe}"
                lines.append(fresh_line)
            _format_violation_summary(
                lines, vs, indent="  ",
                path_clusters=tc.path_clusters,
                failing_endpoint_names=tc.failing_endpoint_names,
                total_failing_count=state.timing.latest_failing_endpoints if state else None,
            )
            # D1: append top-3 path delay hotspots as single-line summaries
            # (oracle m5: compact form for EXECUTE/EVALUATE to avoid distracting re-analysis)
            if tc.top_violating_paths:
                lines.append("  top_path_hotspots:")
                for p in tc.top_violating_paths[:3]:
                    if not p.delay_hotspots:
                        continue
                    parts = []
                    for h in p.delay_hotspots[:3]:
                        pct_str = f"({h['pct_of_path']:.0%})" if h.get('pct_of_path') is not None else ""
                        typ = h.get('type', '')
                        type_tag = f"[{typ}]" if typ else ""
                        parts.append(f"{h['name']}={h['incr']:.3f}ns{type_tag}{pct_str}" if h.get('incr') is not None else f"{h['name']}=?")
                    slack_str = f"{p.slack:.3f}ns" if p.slack is not None else "?"
                    lines.append(f"    - slack={slack_str} endpoint={p.endpoint_name}: {', '.join(parts)}")
        else:
            lines.append(f"  failing_endpoints: {_annotated_val(space.global_state.wns_setup, reason='not_analyzed')}")
            lines.append("  # No critical path data available for violation summary")
        lines.append("")

    # ── Module 3: Physical & Congestion Metrics ────────────────────
    if enabled is None or "physical_congestion" in enabled:
        pc = space.physical_congestion
        lines.append("# Module 3: Physical & Congestion Metrics")
        lines.append("physical_congestion:")
        lines.append(f"  global_congestion_score: {_annotated_val(pc.global_congestion_score, '{:.2f}', 'congestion_analysis_not_supported')}{_tag('congestion_data')}")
        if pc.congestion_level:
            lines.append(f"  congestion_level: {pc.congestion_level}")
        lines.append(f"  avg_wirelength: {_annotated_val(pc.avg_wirelength, '{:.1f}', 'data_not_available')}{_tag('route_status')}")
        lr = _annotated_val(pc.long_route_nets_count, reason="data_not_available")
        lines.append(f"  long_route_nets_count: {lr}")
        if pc.total_wirelength is not None:
            lines.append(f"  total_wirelength: {pc.total_wirelength:.1f}")
        if pc.max_wirelength is not None:
            lines.append(f"  max_wirelength: {pc.max_wirelength:.1f}")
        if pc.timing_violated_nets is not None:
            lines.append(f"  timing_violated_nets: {pc.timing_violated_nets}")
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
        lines.append(f"  cross_domain_paths_count: {nq.cross_domain_paths_count}{_tag('cdc_paths')}")
        if nq.cell_type_summary:
            lines.append(f"  top_cell_types: {nq.cell_type_summary}{_tag('design_info')}")
        if nq.high_fanout_nets:
            lines.append(f"  high_fanout_nets:{_tag('high_fanout_nets')}  # {len(nq.high_fanout_nets)} nets")
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

        # ── Design Structure (SELECT_STRATEGY only) ─────────────────
        if enabled is not None and "design_structure" in enabled and state:
            _append_design_structure(lines, state)
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

    # ── Module 7: Architecture Overview ─────────────────────────
    if enabled is None or "architecture_overview" in enabled:
        ao = space.architecture_overview
        lines.append("# Module 7: Architecture Overview")
        lines.append("architecture_overview:")
        if ao.top_modules:
            lines.append(f"  top_modules:  # {len(ao.top_modules)} modules")
            for m in ao.top_modules:
                lines.append(f"    - name: \"{m.name}\"")
                lines.append(f"      critical_path_hits: {m.critical_path_hits}")
                lines.append(f"      cell_distribution: {m.cell_distribution_pct:.1f}%")
                if m.sub_modules:
                    subs = ", ".join(m.sub_modules)
                    lines.append(f"      sub_modules: [{subs}]")
            lines.append(f"  cross_module_paths: {ao.cross_module_paths}")
            lines.append(f"  intra_module_paths: {ao.intra_module_paths}")
            if ao.deepest_module:
                lines.append(f"  deepest_module: \"{ao.deepest_module}\"")
            lines.append(f"  total_cells_analyzed: {ao.total_cells_analyzed}")
            # Architecture-based insights (SELECT_STRATEGY only) — pure data, no recommendations
            if phase == LoopPhase.SELECT_STRATEGY and ao.top_modules:
                _append_architecture_insights(lines, ao)
        else:
            lines.append("  top_modules: \"N/A(no_critical_paths)\"")
        lines.append("")

        # ── Recent Analysis Results (SELECT_STRATEGY only) ─────────
        if enabled is not None and "recent_analysis" in enabled and state:
            _append_recent_analysis_results(lines, state)
            lines.append("")

    # ── Trajectory (if enabled in phase) ──────────────────────────
    if phase and "trajectory" not in PHASE_STATESPACE_MODULES.get(phase, frozenset()):
        pass  # trajectory now embedded in dynamic_gradient; skip separate section

    # ── Strategy lifecycle (always shown) ────────────────────────────
    # NOTE: Blocked strategies are shown as [BLOCKED] placeholders in the
    # strategy_catalog section (SELECT_STRATEGY) rather than here.
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
    """Infer current design stage from actual design state or tool context.

    Priority:
    1. timing.design_state (from Vivado timing report — most reliable)
    2. Heuristic from tools_used (fallback when design_state is the default "unplaced")
    """
    timing = state.timing

    # Priority 1: actual design state from last timing report
    ds = timing.design_state
    if ds == DesignState.UNPLACED:
        return "PLACEMENT_UNPLACED"
    elif ds == DesignState.PLACED:
        return "PLACEMENT"
    elif ds == DesignState.ROUTED:
        return "ROUTING"

    # Priority 2: infer from tool history (fallback for early iterations)
    if timing.best_wns_iteration is not None and timing.best_wns_iteration > 0:
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
    """Convert a CriticalPathEntry to a DashboardTimingPath.

    D2 fix: uses real clock-domain fields from entry.clock instead of
    the old string-guessing that hardcoded "clk_fpl26contest".
    D1: populates delay_hotspots from entry.top_delay_nodes.
    """
    from ..state import CriticalPathEntry

    endpoint = ""
    if isinstance(entry, CriticalPathEntry) and entry.cells:
        endpoint = entry.cells[-1] if entry.cells else ""

    # D2: real clock-domain context (no more string guessing)
    clk = entry.clock if isinstance(entry, CriticalPathEntry) else None
    source_clock = clk.source_clock if clk else ""
    dest_clock = clk.dest_clock if clk else ""
    path_group = clk.path_group if clk else ""
    clock_skew = clk.clock_skew if clk else None
    clock_uncertainty = clk.clock_uncertainty if clk else None
    is_cross_clock = clk.is_cross_clock if clk else False

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

    # D1: delay hotspots from top_delay_nodes
    delay_hotspots = []
    if isinstance(entry, CriticalPathEntry) and entry.top_delay_nodes:
        total_delay = None
        if entry.logic_delay is not None and entry.net_delay is not None:
            total_delay = entry.logic_delay + entry.net_delay
        for n in entry.top_delay_nodes[:MAX_DELAY_HOTSPOTS]:
            pct = None
            if total_delay and n.incr_delay is not None and total_delay > 0:
                pct = round(n.incr_delay / total_delay, 4)
            delay_hotspots.append({
                "name": n.name,
                "type": n.cell_type or n.kind,
                "incr": round(n.incr_delay, 4) if n.incr_delay is not None else None,
                "pct_of_path": pct,
                "location": n.location,
            })

    # Build cell type chain from path cell names
    from .critical_path import build_cell_type_chain
    cells = entry.cells if isinstance(entry, CriticalPathEntry) else []
    cell_type_chain, cell_type_counts = build_cell_type_chain(cells)

    return DashboardTimingPath(
        endpoint_name=endpoint,
        source_clock=source_clock,
        dest_clock=dest_clock,
        slack=entry.slack if isinstance(entry, CriticalPathEntry) else None,
        logic_delay_pct=logic_delay_pct,
        route_delay_pct=route_delay_pct,
        logic_levels=entry.levels if isinstance(entry, CriticalPathEntry) else None,
        path_group=path_group,
        # D1/D2
        startpoint=entry.startpoint if isinstance(entry, CriticalPathEntry) else "",
        clock_skew=clock_skew,
        clock_uncertainty=clock_uncertainty,
        is_cross_clock=is_cross_clock,
        delay_hotspots=delay_hotspots,
        cell_type_chain=cell_type_chain,
        cell_type_counts=cell_type_counts,
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
        from .constants import SKILL_CHAIN_ACTIONS as _CHAIN_ACTIONS, STRATEGY_MAP as _STRATEGY_MAP

        entry = _STRATEGY_MAP.get(current_strategy)
        tool = entry.execute_tool if entry else None
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

            # Combined strategy flow guidance
            if current_strategy == "PhysOpt+RegisterRetiming":
                lines.append("  combined_strategy_flow:")
                lines.append("    step_1: vivado_physopt_and_route(directive='Explore')")
                lines.append("    step_2: rapidwright_analyze_register_retiming(critical_paths=<from Dashboard>)")
                lines.append("    step_3: rapidwright_execute_register_retiming(retiming_candidates=<from step_2>)")
                lines.append("    step_4: signal EXEC_DONE")
                lines.append("  note: step_3 auto-chains open_checkpoint + route_design")
    except Exception:
        pass


def _append_architecture_insights(lines: list[str], ao: DashboardArchitectureOverview) -> None:
    """Append architecture-based structural observations (SELECT_STRATEGY only).
    Pure data descriptors — no strategy recommendations.
    """
    if not ao.top_modules:
        return

    top = ao.top_modules[0]
    top_hits = top.critical_path_hits
    top_coverage = top.cell_distribution_pct

    # Insight 1: Single-module dominance
    if top_coverage > 50.0:
        lines.append(f"  arch_insight: Critical paths concentrate in \"{top.name}\" "
                     f"({top_coverage:.0f}% coverage, {top_hits} hits)")
    elif top_coverage > 30.0:
        lines.append(f"  arch_insight: \"{top.name}\" is the primary timing hotspot "
                     f"({top_coverage:.0f}% coverage)")

    # Insight 2: Cross-module vs intra-module path ratio
    total_paths = ao.cross_module_paths + ao.intra_module_paths
    if ao.cross_module_paths > ao.intra_module_paths and ao.cross_module_paths >= 3:
        lines.append(f"  arch_insight: {ao.cross_module_paths}/{total_paths} "
                     f"paths cross module boundaries — inter-module routing delay may dominate")
    elif ao.cross_module_paths == 0 and ao.intra_module_paths > 0:
        lines.append(f"  arch_insight: All {ao.intra_module_paths} critical paths are intra-module")

    # Insight 3: Deepest module
    if ao.deepest_module:
        lines.append(f"  arch_insight: Deepest logic is in \"{ao.deepest_module}\"")


# ── Analysis Tools for Design Structure ────────────────────────────

_ANALYSIS_TOOL_NAMES: frozenset[str] = frozenset({
    "vivado_report_timing_summary", "vivado_extract_critical_path_cells",
    "vivado_get_cached_high_fanout_nets", "vivado_check_design_status",
    "rapidwright_analyze_critical_path_spread", "rapidwright_analyze_congestion",
    "rapidwright_analyze_net_detour", "rapidwright_get_design_info",
    "rapidwright_get_device_topology", "rapidwright_report_timing",
    "rapidwright_search_cells", "rapidwright_analyze_pblock_region",
    "rapidwright_flatten_lut_cascade",
})


def _append_design_structure(lines: list[str], state: OptimizerState) -> None:
    """Append cell-composition structural indicators from top_cell_types.
    Pure data — no strategy recommendations. SELECT_STRATEGY only.
    """
    di = state.timing.design_info or {}
    top_types = di.get("top_cell_types", {})
    if not top_types:
        return

    lut = top_types.get("LUT6", 0) + top_types.get("LUT5", 0) + top_types.get("LUT4", 0) \
          + top_types.get("LUT3", 0) + top_types.get("LUT2", 0) + top_types.get("LUT1", 0)
    muxf7 = top_types.get("MUXF7", 0)
    muxf8 = top_types.get("MUXF8", 0)
    muxf_total = muxf7 + muxf8
    ff = top_types.get("FDRE", 0) + top_types.get("FDSE", 0) \
         + top_types.get("FDCE", 0) + top_types.get("FDPE", 0)
    carry = top_types.get("CARRY4", 0) + top_types.get("CARRY8", 0)
    total_cells = sum(top_types.values()) if top_types else 0

    lines.append("# Module 4b: Design Structure")
    lines.append("design_structure:")
    lines.append(f"  cell_composition:")
    if lut:
        lines.append(f"    lut: {lut}")
    if muxf_total:
        lines.append(f"    muxf: {muxf_total}  # MUXF7={muxf7}, MUXF8={muxf8}")
    if ff:
        lines.append(f"    ff: {ff}")
    if carry:
        lines.append(f"    carry: {carry}")
    if total_cells > 0:
        muxf_ratio = muxf_total / total_cells if muxf_total else 0.0
        lines.append(f"    muxf_ratio: {muxf_ratio:.1%}")
        if lut and ff:
            ftl_ratio = ff / lut
            lines.append(f"    ff_to_lut_ratio: {ftl_ratio:.3f}")

    # Structural pattern detection
    patterns = []
    if muxf7 and muxf8:
        patterns.append("MUXF7+MUXF8_cascade")
    elif muxf7:
        patterns.append("MUXF7_present")
    if carry:
        patterns.append("carry_chain")
    if lut and ff and (ff / lut) < 0.1:
        patterns.append("shallow_pipeline")
    if patterns:
        lines.append(f"  structural_patterns: {', '.join(patterns)}")

    # Cell type dominance
    if top_types:
        sorted_types = sorted(top_types.items(), key=lambda x: -x[1])
        top3 = [f"{k}({v})" for k, v in sorted_types[:3]]
        lines.append(f"  dominant_cell_types: {' > '.join(top3)}")


def _append_recent_analysis_results(lines: list[str], state: OptimizerState) -> None:
    """Append recent analysis phase tool results as structured summaries.
    Reads from raw_tool_outputs — no extra MCP calls. SELECT_STRATEGY only.
    """
    current_iter = state.iteration.current
    entries: list[str] = []

    for (it, _phase, rd, name), raw in sorted(state.context.raw_tool_outputs.items()):
        if name not in _ANALYSIS_TOOL_NAMES:
            continue
        if it != current_iter and it != current_iter - 1:
            continue

        # Extract a compact one-line summary from the raw output
        summary = ""
        if name == "vivado_report_timing_summary":
            wns = _extract_timing_value(raw, "wns")
            tns = _extract_timing_value(raw, "tns")
            fe = _extract_timing_value(raw, "failing_endpoints")
            summary = f"WNS={wns}, TNS={tns}, FE={fe}"
        elif name == "rapidwright_analyze_critical_path_spread":
            import json
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    avg = data.get("avg_distance", data.get("avg_max_distance", "?"))
                    mx = data.get("max_distance", "?")
                    cnt = data.get("paths_analyzed", "?")
                    summary = f"avg={avg}, max={mx}, paths={cnt}"
            except (json.JSONDecodeError, TypeError):
                summary = raw.strip()[:80]
        elif name == "rapidwright_analyze_congestion":
            import json
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    score = data.get("global_score", data.get("congested_ratio", "?"))
                    sev = data.get("severity", "?")
                    summary = f"global_score={score}, severity={sev}"
            except (json.JSONDecodeError, TypeError):
                summary = raw.strip()[:80]
        elif name == "vivado_get_cached_high_fanout_nets":
            # Count lines with fanout info
            import re
            matches = re.findall(r"fanout=(\d+)", raw)
            if matches:
                max_fo = max(int(m) for m in matches)
                summary = f"{len(matches)} nets, max_fanout={max_fo}"
            else:
                summary = raw.strip()[:80]
        elif name == "rapidwright_analyze_net_detour":
            import json
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    summary = f"{len(data)} cells with detour > threshold"
                elif isinstance(data, dict) and data.get("cells"):
                    summary = f"{len(data['cells'])} cells"
            except (json.JSONDecodeError, TypeError):
                summary = raw.strip()[:80]
        else:
            # Generic: first non-empty, non-json line
            first_line = raw.strip().split("\n")[0][:80] if raw.strip() else ""
            summary = first_line if first_line else f"completed"

        if summary:
            entry = f"    - [{name}] {summary}"
            if entry not in entries:
                entries.append(entry)

    if entries:
        lines.append("# Module 8: Recent Analysis Results")
        lines.append("recent_analysis:")
        for e in entries[-6:]:
            lines.append(e)


def _extract_timing_value(raw: str, key: str) -> str:
    """Quick regex-based timing value extraction for dashboard summaries."""
    import re
    # Pattern: key: value  or  key=value
    pat = re.compile(rf'{re.escape(key)}[\s:=]+([-\d.]+)', re.IGNORECASE)
    m = pat.search(raw)
    return m.group(1) if m else "?"


def _format_violation_summary(
    lines: list[str],
    vs: DashboardViolationSummary,
    indent: str = "",
    path_clusters: list[DashboardPathCluster] | None = None,
    failing_endpoint_names: list[str] | None = None,
    total_failing_count: int | None = None,
) -> None:
    """Format violation summary YAML lines (used by both full and compact views)."""
    sev = vs.severity_distribution
    if sev:
        lines.append(f"{indent}severity_distribution:")
        lines.append(f"{indent}  critical_slack_lt_-1.0ns: {sev.get('critical', 0)}")
        lines.append(f"{indent}  moderate_slack_-1.0_to_-0.3ns: {sev.get('moderate', 0)}")
        lines.append(f"{indent}  marginal_slack_-0.3_to_0ns: {sev.get('marginal', 0)}")

    dp = vs.delay_profile_breakdown
    if dp:
        logic_d = dp.get("logic_dominated", 0)
        route_d = dp.get("route_dominated", 0)
        mixed = dp.get("mixed", 0)
        lines.append(f"{indent}delay_profile_breakdown:")
        lines.append(f"{indent}  logic_dominated_paths: {logic_d}")
        lines.append(f"{indent}  route_dominated_paths: {route_d}")
        lines.append(f"{indent}  mixed_paths: {mixed}")
        if logic_d + route_d + mixed > 0:
            dominant = "logic_dominated" if logic_d > route_d else "route_dominated" if route_d > logic_d else "mixed"
            lines.append(f"{indent}  dominant_delay_type: {dominant}")

    ll = vs.logic_level_distribution
    if ll:
        lines.append(f"{indent}logic_level_distribution:")
        lines.append(f"{indent}  levels_1_to_5: {ll.get('levels_1_to_5', 0)}")
        lines.append(f"{indent}  levels_6_to_10: {ll.get('levels_6_to_10', 0)}")
        lines.append(f"{indent}  levels_gt_10: {ll.get('levels_gt_10', 0)}")

    mod_stats = vs.top_violating_modules
    if mod_stats:
        lines.append(f"{indent}top_violating_modules:")
        for mod_name, mod_data in mod_stats.items():
            endpoints = mod_data.get("endpoint_count", 0)
            min_slack = mod_data.get("min_slack")
            slack_str = f", min_slack={min_slack}ns" if min_slack is not None else ""
            lines.append(f"{indent}  {mod_name}: {endpoints} endpoints{slack_str}")

    # Path clusters: representative path per (module, delay_profile) group
    if path_clusters:
        lines.append(f"{indent}path_clusters:  # Representative path per cluster")
        for pc in path_clusters:
            lines.append(f"{indent}  - cluster: {pc.cluster_id}")
            lines.append(f"{indent}    path_count: {pc.path_count}")
            lines.append(f"{indent}    slack_range: {pc.slack_range}")
            if pc.avg_logic_delay_pct is not None:
                lines.append(f"{indent}    avg_logic_delay_pct: {pc.avg_logic_delay_pct:.2f}")
            if pc.avg_logic_levels is not None:
                lines.append(f"{indent}    avg_logic_levels: {pc.avg_logic_levels:.1f}")
            lines.append(f"{indent}    module: {pc.module}")

    # Failing endpoint names (top violating path endpoints)
    # NOTE: The count shown (from critical_path_extraction) may differ from
    # the timing report's failing_endpoints count. We show both to avoid
    # misleading the LLM into thinking only N paths are violating.
    if failing_endpoint_names:
        shown = failing_endpoint_names[:10]
        shown_count = len(shown)
        total_fe = total_failing_count if total_failing_count is not None and total_failing_count > 0 else len(failing_endpoint_names)
        lines.append(f"{indent}top_violating_endpoints:  # showing {shown_count} of {total_fe} total failing (from timing report)")
        for ep in shown:
            lines.append(f"{indent}  - {ep}")
        if len(failing_endpoint_names) > 10:
            lines.append(f"{indent}  # ... and {len(failing_endpoint_names) - 10} more critical-path-derived, "
                         f"{max(0, total_fe - len(failing_endpoint_names))} additional from timing report")
