"""Tool filtering by loop phase.

Each phase gets a focused subset of tools to keep the LLM's attention
on the task at hand.
"""

from __future__ import annotations

from enum import Enum


class LoopPhase(str, Enum):
    ANALYZE = "analyze"
    SELECT_STRATEGY = "select_strategy"
    EXECUTE = "execute"
    EVALUATE = "evaluate"


# ── Per-phase tool allowlists ──────────────────────────────────────

PHASE_TOOLS: dict[LoopPhase, frozenset[str]] = {
    LoopPhase.ANALYZE: frozenset({
        # Vivado timing/report tools
        "vivado_report_timing_summary",
        "vivado_get_wns",
        "vivado_extract_critical_path_cells",
        "vivado_extract_critical_path_pins",
        "vivado_get_critical_high_fanout_nets",
        "vivado_report_utilization_for_pblock",
        "vivado_report_route_status",
        # RapidWright analysis tools
        "rapidwright_analyze_net_detour",
        "rapidwright_analyze_critical_path_spread",
        "rapidwright_analyze_congestion",
        "rapidwright_analyze_pblock_region",
        "rapidwright_get_device_topology",
        "rapidwright_report_timing",
        "rapidwright_get_design_info",
        "rapidwright_search_cells",
        # Internal tools
        "vivado_get_raw_tool_output",
        "vivado_get_cached_high_fanout_nets",
        "report_step_state",
    }),

    LoopPhase.SELECT_STRATEGY: frozenset({
        "report_step_state",
        "vivado_get_raw_tool_output",
        "vivado_get_cached_high_fanout_nets",
        "rapidwright_analyze_pblock_region",
    }),

    LoopPhase.EXECUTE: frozenset({
        # Strategy execution tools
        "rapidwright_execute_pblock_strategy",
        "rapidwright_execute_fanout_strategy",
        "rapidwright_execute_congestion_spreading",
        "rapidwright_optimize_pin_swapping",
        "rapidwright_flatten_lut_cascade",
        "rapidwright_replicate_critical_cells",
        "rapidwright_execute_register_retiming",
        "rapidwright_execute_net_swapping",
        "rapidwright_optimize_cell_placement",
        "rapidwright_smart_region_search",
        "rapidwright_analyze_pblock_region",
        "rapidwright_optimize_lut_input_cone",
        # Vivado execution tools
        "vivado_place_design",
        "vivado_route_design",
        "vivado_phys_opt_design",
        "vivado_write_checkpoint",
        "vivado_run_tcl",
        "vivado_open_checkpoint",
        # Quick-check tools during execution
        "vivado_report_timing_summary",
        "vivado_get_wns",
        "vivado_extract_critical_path_cells",
        "vivado_get_critical_high_fanout_nets",
        "rapidwright_report_timing",
        # Internal tools
        "report_step_state",
        "vivado_get_raw_tool_output",
    }),

    LoopPhase.EVALUATE: frozenset({
        "vivado_report_timing_summary",
        "vivado_get_wns",
        "vivado_report_route_status",
        "rapidwright_report_timing",
        "rapidwright_compare_design_structure",
        "vivado_extract_critical_path_cells",
        "report_step_state",
        "vivado_get_raw_tool_output",
    }),
}

# ── Per-phase max tool rounds ──────────────────────────────────────

PHASE_MAX_ROUNDS: dict[LoopPhase, int] = {
    LoopPhase.ANALYZE: 12,
    LoopPhase.SELECT_STRATEGY: 6,
    LoopPhase.EXECUTE: 30,
    LoopPhase.EVALUATE: 8,
}


def filter_tools_for_phase(all_tools: list[dict], phase: LoopPhase) -> list[dict]:
    """Return only the tools allowed for the given phase.

    Args:
        all_tools: Full list of OpenAI-format tool definitions.
        phase: Current LoopPhase.

    Returns:
        Filtered list of tool definitions.
    """
    allowed = PHASE_TOOLS.get(phase)
    if allowed is None:
        return all_tools

    filtered = []
    for tool in all_tools:
        name = tool.get("function", {}).get("name", "")
        if name in allowed:
            filtered.append(tool)
    return filtered
