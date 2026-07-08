"""Static tool runtime policy such as freshness, rate limits, and timeouts."""

from __future__ import annotations


DASHBOARD_REFRESH_MAP: dict[str, frozenset[str]] = {
    "vivado_report_utilization_for_pblock": frozenset({"resource_utilization"}),
    "vivado_get_critical_high_fanout_nets": frozenset({"high_fanout_nets"}),
    "rapidwright_analyze_critical_path_spread": frozenset({"critical_path_spread"}),
    "vivado_extract_critical_path_pins": frozenset({"critical_path_spread"}),
    "vivado_report_route_status": frozenset({"route_status", "route_status_detail"}),
    "vivado_report_timing_summary": frozenset({"timing_summary", "cdc_paths"}),
    "rapidwright_get_design_info": frozenset({"design_info"}),
    "vivado_extract_critical_path_cells": frozenset({"critical_path_cells"}),
    "vivado_report_qor_suggestions": frozenset({"qor_suggestions"}),
    "vivado_report_high_fanout_nets": frozenset({"high_fanout_nets"}),
    "vivado_get_wns": frozenset({"timing_summary"}),
    "rapidwright_analyze_congestion": frozenset({"congestion_data"}),
}

PHASE_TOOL_RATE_LIMITS: dict[str, int] = {
    "rapidwright_search_cells": 3,
    "vivado_run_tcl": 2,
    "vivado_write_checkpoint": 3,
    "rapidwright_analyze_net_detour": 2,
    "vivado_get_cached_high_fanout_nets": 2,
    "vivado_check_design_status": 3,
}

_TOOL_TIMEOUT_DEFAULTS: dict[str, float] = {
    "vivado_get_wns": 30.0,
    "vivado_search_cells": 60.0,
    "vivado_get_cached_high_fanout_nets": 10.0,
    "rapidwright_get_device_topology": 30.0,
    "rapidwright_get_design_info": 30.0,
    "rapidwright_search_cells": 60.0,
    "rapidwright_analyze_critical_path_spread": 60.0,
    "rapidwright_analyze_congestion": 60.0,
    "vivado_report_timing_summary": 120.0,
    "vivado_report_route_status": 120.0,
    "vivado_run_tcl": 120.0,
    "vivado_extract_critical_path_cells": 120.0,
    "vivado_extract_critical_path_pins": 120.0,
    "vivado_get_critical_high_fanout_nets": 120.0,
    "vivado_report_utilization_for_pblock": 120.0,
    "rapidwright_read_checkpoint": 120.0,
    "rapidwright_analyze_pblock_region": 120.0,
    "rapidwright_initialize_rapidwright": 60.0,
    "vivado_open_checkpoint": 600.0,
    "vivado_place_design": 1800.0,
    "vivado_route_design": 1800.0,
    "vivado_phys_opt_design": 3600.0,
    "vivado_physopt_and_route": 3600.0,
    "vivado_write_checkpoint": 300.0,
    "vivado_write_verilog_simulation": 300.0,
    "vivado_create_and_apply_pblock": 300.0,
    "rapidwright_execute_pblock_strategy": 600.0,
    "rapidwright_execute_fanout_strategy": 300.0,
    "rapidwright_optimize_cell_placement": 300.0,
    "rapidwright_execute_congestion_spreading": 300.0,
    "rapidwright_execute_register_retiming": 300.0,
    "rapidwright_execute_net_swapping": 300.0,
    "rapidwright_optimize_lut_input_cone": 300.0,
    "rapidwright_replicate_critical_cells": 300.0,
    "rapidwright_smart_region_search": 300.0,
    "rapidwright_optimize_fanout_batch": 300.0,
    "rapidwright_write_checkpoint": 300.0,
    "rapidwright_compare_design_structure": 120.0,
    "rapidwright_smart_retiming": 300.0,
    "rapidwright_execute_opt_design_strategy": 120.0,
    "rapidwright_execute_combinational_rebalancing_strategy": 120.0,
    "rapidwright_execute_lut_muxf_repack_strategy": 120.0,
    "rapidwright_execute_muxf_tree_reorder_strategy": 120.0,
    "rapidwright_execute_physopt_strategy": 120.0,
    "vivado_opt_design": 600.0,
}

_DEFAULT_TOOL_TIMEOUT: float = 300.0
_TOOL_TIMEOUT_MAX: float = 900.0

_MCP_ERROR_PATTERNS: tuple[str, ...] = (
    "[ERROR] Tcl command timed out",
    "[ERROR] Application-level timeout",
    "[ERROR] Multi-line script aborted",
    "[ERROR] Multi-line script validation failed",
    "[ERROR] Vivado process terminated",
    '"error":',
    # MCP SDK inputSchema validation rejections (defense-in-depth alongside
    # the isError wrapping in tool_router).
    "Input validation error",
    "MCP tool error:",
)
