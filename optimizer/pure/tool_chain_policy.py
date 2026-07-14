"""Static skill-chain and directive policy."""

from __future__ import annotations


STRATEGY_DEFAULT_DIRECTIVES: dict[str, tuple[str | None, str | None]] = {
    "rapidwright_execute_pblock_strategy": ("Explore", "Explore"),
    "rapidwright_execute_physopt_strategy": ("Explore", "Explore"),
    "rapidwright_execute_opt_design_strategy": ("ExtraTimingOpt", "NoTimingRelaxation"),
    "rapidwright_execute_combinational_rebalancing_strategy": ("ExtraTimingOpt", "NoTimingRelaxation"),
    "rapidwright_execute_lut_muxf_repack_strategy": ("ExtraTimingOpt", "NoTimingRelaxation"),
    "rapidwright_execute_muxf_tree_reorder_strategy": (None, "Explore"),
    "rapidwright_execute_fanout_strategy": ("Explore", "NoTimingRelaxation"),
    "rapidwright_flatten_lut_cascade": ("ExtraTimingOpt", "NoTimingRelaxation"),
}

KNOWN_BROKEN_DIRECTIVES: frozenset[str] = frozenset({
    "Performance_ExtraTimingOpt",
})

RAPIDWRIGHT_PRECHECK_ENABLED: bool = False
PLACE_ONLY_CHECK_ENABLED: bool = False
PLACE_ONLY_REGRESS_THRESHOLD: float = 0.030
# After a global `place_design -unplace` chain step, more placed primitives
# than this means the unplace silently did nothing (run-20260708_012142: the
# step was rejected at the MCP boundary, the chain kept going, and the PBLOCK
# "re-place" degenerated into a pure re-route). Fail the chain instead.
UNPLACE_VERIFY_MAX_PLACED_CELLS: int = 1000
PLACE_ONLY_CHECK_SKILLS: frozenset[str] = frozenset({
    "rapidwright_execute_fanout_strategy",
    "rapidwright_execute_opt_design_strategy",
    "rapidwright_execute_combinational_rebalancing_strategy",
    "rapidwright_execute_lut_muxf_repack_strategy",
})

HEAVY_CHAIN_SKILLS: frozenset[str] = frozenset()

SKILL_CHAIN_ACTIONS: dict[str, list[dict]] = {
    "rapidwright_execute_pblock_strategy": [
        {"tool": "vivado_unplace_cells", "args_from_skill": {"cells": "critical_path_cells"}},
        {
            "tool": "vivado_create_and_apply_pblock",
            "args_from_skill": {
                "pblock_name": "pblock_name",
                "ranges": "pblock_ranges",
                "is_soft": "is_soft_recommended",
                "cells": "critical_path_cells",
            },
        },
        {"tool": "vivado_place_design", "args": {"directive": "Explore"}, "args_from_skill": {"directive": "place_directive"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"}, "args_from_skill": {"directive": "route_directive"}},
        {"tool": "vivado_report_timing_summary", "args": {}},
    ],
    "rapidwright_execute_register_retiming": [
        {"tool": "vivado_open_checkpoint", "args_from_skill": {"dcp_path": "checkpoint_path"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"}},
        {"tool": "vivado_report_timing_summary", "args": {}},
    ],
    "rapidwright_execute_fanout_strategy": [
        {"tool": "vivado_open_checkpoint", "args_from_skill": {"dcp_path": "checkpoint_path"}},
        {"tool": "vivado_place_design", "args": {"directive": "Explore"}, "args_from_skill": {"directive": "place_directive"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"}, "args_from_skill": {"directive": "route_directive"}},
        {"tool": "vivado_report_timing_summary", "args": {}},
    ],
    "rapidwright_execute_opt_design_strategy": [
        {"tool": "vivado_opt_design", "args_from_skill": {"directive": "directive", "retarget": "retarget"}},
        {"tool": "vivado_place_design", "args": {"directive": "Explore"}, "args_from_skill": {"directive": "place_directive"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"}, "args_from_skill": {"directive": "route_directive"}},
        {"tool": "vivado_report_timing_summary", "args": {}},
        {"tool": "vivado_extract_critical_path_cells", "args": {"num_paths": 10}, "args_from_skill": {"num_paths": "extract_num_paths"}},
    ],
    "rapidwright_execute_combinational_rebalancing_strategy": [
        {"tool": "vivado_opt_design", "args_from_skill": {"directive": "directive", "retarget": "retarget"}},
        {"tool": "vivado_place_design", "args": {"directive": "Explore"}, "args_from_skill": {"directive": "place_directive"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"}, "args_from_skill": {"directive": "route_directive"}},
        {"tool": "vivado_report_timing_summary", "args": {}},
        {"tool": "vivado_extract_critical_path_cells", "args": {"num_paths": 10}, "args_from_skill": {"num_paths": "extract_num_paths"}},
    ],
    "rapidwright_execute_lut_muxf_repack_strategy": [
        {"tool": "vivado_opt_design", "args_from_skill": {"directive": "directive", "retarget": "retarget"}},
        {"tool": "vivado_place_design", "args": {"directive": "Explore"}, "args_from_skill": {"directive": "place_directive"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"}, "args_from_skill": {"directive": "route_directive"}},
        {"tool": "vivado_report_timing_summary", "args": {}},
        {"tool": "vivado_extract_critical_path_cells", "args": {"num_paths": 10}, "args_from_skill": {"num_paths": "extract_num_paths"}},
    ],
    "rapidwright_execute_muxf_tree_reorder_strategy": [
        {"tool": "vivado_phys_opt_design", "args_from_skill": {"directive": "directive"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"}, "args_from_skill": {"directive": "route_directive"}},
        {"tool": "vivado_report_timing_summary", "args": {}},
        {"tool": "vivado_extract_critical_path_cells", "args": {"num_paths": 10}, "args_from_skill": {"num_paths": "extract_num_paths"}},
    ],
    "rapidwright_execute_physopt_strategy": [
        {"tool": "vivado_phys_opt_design", "args_from_skill": {"directive": "directive"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"}, "args_from_skill": {"directive": "route_directive"}},
        {"tool": "vivado_report_timing_summary", "args": {}},
    ],
    "rapidwright_flatten_lut_cascade": [
        {"tool": "vivado_open_checkpoint", "args_from_skill": {"dcp_path": "post_checkpoint_path"}},
        {"tool": "vivado_place_design", "args": {"directive": "Explore"}, "args_from_skill": {"directive": "place_directive"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"}, "args_from_skill": {"directive": "route_directive"}},
        {"tool": "vivado_report_timing_summary", "args": {}},
        {"tool": "vivado_extract_critical_path_cells", "args": {"num_paths": 10}, "args_from_skill": {"num_paths": "extract_num_paths"}},
    ],
    # P2 2A: DCP-writing structural strategies previously had NO chain, so their
    # mutated DCP was never opened/placed/routed in Vivado (mutation silently lost).
    # run-20260713_130643: replicate wrote a DCP that was never applied. Mirror
    # fanout chain (open RW DCP, re-place, re-route, evaluate). Skipped automatically
    # when the skill reports no effect (no checkpoint_path) via should_skip_chain_
    # for_empty_result.
    "rapidwright_replicate_critical_cells": [
        {"tool": "vivado_open_checkpoint", "args_from_skill": {"dcp_path": "checkpoint_path"}},
        {"tool": "vivado_place_design", "args": {"directive": "Explore"}, "args_from_skill": {"directive": "place_directive"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"}, "args_from_skill": {"directive": "route_directive"}},
        {"tool": "vivado_report_timing_summary", "args": {}},
        {"tool": "vivado_extract_critical_path_cells", "args": {"num_paths": 10}, "args_from_skill": {"num_paths": "extract_num_paths"}},
    ],
    "rapidwright_optimize_pin_swapping": [
        {"tool": "vivado_open_checkpoint", "args_from_skill": {"dcp_path": "checkpoint_path"}},
        {"tool": "vivado_place_design", "args": {"directive": "Explore"}, "args_from_skill": {"directive": "place_directive"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"}, "args_from_skill": {"directive": "route_directive"}},
        {"tool": "vivado_report_timing_summary", "args": {}},
        {"tool": "vivado_extract_critical_path_cells", "args": {"num_paths": 10}, "args_from_skill": {"num_paths": "extract_num_paths"}},
    ],
    "rapidwright_execute_net_swapping": [
        {"tool": "vivado_open_checkpoint", "args_from_skill": {"dcp_path": "checkpoint_path"}},
        {"tool": "vivado_place_design", "args": {"directive": "Explore"}, "args_from_skill": {"directive": "place_directive"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"}, "args_from_skill": {"directive": "route_directive"}},
        {"tool": "vivado_report_timing_summary", "args": {}},
        {"tool": "vivado_extract_critical_path_cells", "args": {"num_paths": 10}, "args_from_skill": {"num_paths": "extract_num_paths"}},
    ],
    "rapidwright_execute_congestion_spreading": [
        {"tool": "vivado_open_checkpoint", "args_from_skill": {"dcp_path": "checkpoint_path"}},
        {"tool": "vivado_place_design", "args": {"directive": "Explore"}, "args_from_skill": {"directive": "place_directive"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"}, "args_from_skill": {"directive": "route_directive"}},
        {"tool": "vivado_report_timing_summary", "args": {}},
        {"tool": "vivado_extract_critical_path_cells", "args": {"num_paths": 10}, "args_from_skill": {"num_paths": "extract_num_paths"}},
    ],
    "rapidwright_smart_retiming": [
        {"tool": "vivado_open_checkpoint", "args_from_skill": {"dcp_path": "final_checkpoint_path"}},
        {"tool": "vivado_place_design", "args": {"directive": "Explore"}, "args_from_skill": {"directive": "place_directive"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"}, "args_from_skill": {"directive": "route_directive"}},
        {"tool": "vivado_report_timing_summary", "args": {}},
        {"tool": "vivado_extract_critical_path_cells", "args": {"num_paths": 10}, "args_from_skill": {"num_paths": "extract_num_paths"}},
    ],
}


def get_skill_chain_actions(tool_name: str) -> list[dict] | None:
    return SKILL_CHAIN_ACTIONS.get(tool_name)


def has_skill_chain(tool_name: str) -> bool:
    return tool_name in SKILL_CHAIN_ACTIONS


RW_PRECHECK_EXEMPT_CHAIN_TOOLS: frozenset[str] = frozenset({
    "rapidwright_execute_pblock_strategy",
})

EMPTY_RESULT_CHAIN_EXEMPT_TOOLS: frozenset[str] = frozenset({
    "rapidwright_execute_pblock_strategy",
    "rapidwright_flatten_lut_cascade",
})


def tool_uses_rw_precheck(tool_name: str) -> bool:
    return has_skill_chain(tool_name) and tool_name not in RW_PRECHECK_EXEMPT_CHAIN_TOOLS


def should_skip_chain_for_empty_result(
    tool_name: str,
    skill_result_data: dict | None,
) -> tuple[bool, str | None]:
    if not has_skill_chain(tool_name) or not isinstance(skill_result_data, dict):
        return False, None

    status = skill_result_data.get("status")
    is_skipped = (status in ("skipped", "no_action", "unchanged")
                  or skill_result_data.get("skipped") is True)
    # Plan-style results (status "ready"/"planned" with non-empty steps) carry
    # an executable Vivado chain even without optimized_cells/critical_paths.
    # run-20260710_190708: LUTMUXFRepack/MUXFTreeReorder returned ready plans
    # but were misjudged as "no data produced", so their opt_design/phys_opt
    # chains never ran. `steps` (not analysis_summary) is the signal: skipped
    # plans also attach analysis_summary but never steps.
    has_ready_plan = (
        status in ("ready", "planned")
        and bool(skill_result_data.get("steps"))
    )
    # A DCP-writing skill that wrote a checkpoint has a real effect (mutated
    # netlist on disk) even without optimized_cells/critical_paths/successful_count.
    # P2 2A: replicate/pin_swap/net_swap/congestion_spreading/smart_retiming report
    # effect via checkpoint_path (None on no-op); without this they were wrongly
    # skipped and their mutation never applied (run-20260713_130643: replicate).
    has_written_dcp = bool(
        skill_result_data.get("checkpoint_path")
        or skill_result_data.get("final_checkpoint_path")
        or skill_result_data.get("post_checkpoint_path")
    )
    # Fanout: successful_count; replicate: replications_performed; swaps: swaps_performed
    # (run-20260711_232113: 16 splits orphaned when the chain was wrongly skipped).
    has_effect = (
        has_written_dcp
        or skill_result_data.get("optimized_cells")
        or skill_result_data.get("critical_paths")
        or (skill_result_data.get("successful_count") or 0) > 0
        or (skill_result_data.get("replications_performed") or 0) > 0
        or (skill_result_data.get("swaps_performed") or 0) > 0
    )
    has_empty_payload = not has_ready_plan and not has_effect

    if is_skipped:
        return True, "skipped"
    if has_empty_payload and tool_name not in EMPTY_RESULT_CHAIN_EXEMPT_TOOLS:
        return True, "no data produced"
    return False, None
