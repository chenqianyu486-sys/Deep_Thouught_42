"""Static tool catalog and strategy-to-tool relationships."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StrategyEntry:
    """Mapping entry for a single strategy."""

    skill_id: str
    execute_tool: str


STRATEGY_MAP: dict[str, StrategyEntry] = {
    "PBLOCK": StrategyEntry("pblock_strategy", "rapidwright_execute_pblock_strategy"),
    "PhysOpt": StrategyEntry("physopt_strategy", "vivado_physopt_and_route"),
    "Fanout": StrategyEntry("fanout_strategy", "rapidwright_execute_fanout_strategy"),
    "LUTCascade": StrategyEntry("lut_cascade_flattening", "rapidwright_flatten_lut_cascade"),
    "PinSwap": StrategyEntry("pin_swapping_strategy", "rapidwright_optimize_pin_swapping"),
    "CellReplication": StrategyEntry("critical_path_cell_replication", "rapidwright_replicate_critical_cells"),
    "CongestionSpreading": StrategyEntry("execute_congestion_spreading", "rapidwright_execute_congestion_spreading"),
    "NetSwap": StrategyEntry("execute_net_swapping", "rapidwright_execute_net_swapping"),
    "OptDesign": StrategyEntry("opt_design_strategy", "rapidwright_execute_opt_design_strategy"),
    "RegisterRetiming": StrategyEntry("execute_register_retiming", "rapidwright_execute_register_retiming"),
    "SmartRetiming": StrategyEntry("smart_retiming", "rapidwright_smart_retiming"),
    "LogicResynthesis": StrategyEntry("logic_resynthesis", "vivado_run_tcl"),
    "PhysOptAggressive": StrategyEntry("physopt_strategy", "vivado_physopt_and_route"),
    "CombinationalRebalance": StrategyEntry("combinational_rebalancing_strategy", "rapidwright_execute_combinational_rebalancing_strategy"),
    "LUTMUXFRepack": StrategyEntry("lut_muxf_repack_strategy", "rapidwright_execute_lut_muxf_repack_strategy"),
    "MUXFTreeReorder": StrategyEntry("muxf_tree_reorder_strategy", "rapidwright_execute_muxf_tree_reorder_strategy"),
    "LogicOptimization": StrategyEntry("opt_design_strategy", "rapidwright_execute_opt_design_strategy"),
    "PhysOpt+RegisterRetiming": StrategyEntry("physopt_strategy", "vivado_physopt_and_route"),
}

SKILL_TOOL_MAP: dict[str, str] = {
    entry.execute_tool: entry.skill_id
    for entry in STRATEGY_MAP.values()
}

PRIMARY_STRATEGY_TOOL_NAMES: frozenset[str] = frozenset(
    entry.execute_tool for entry in STRATEGY_MAP.values()
)


def get_strategy_primary_tool(strategy: str) -> str | None:
    entry = STRATEGY_MAP.get(strategy)
    return entry.execute_tool if entry else None


STRATEGY_TOOL_NAMES: frozenset[str] = frozenset(
    PRIMARY_STRATEGY_TOOL_NAMES
    | {
        "vivado_physopt_and_route",
        "vivado_phys_opt_design",
        "vivado_opt_design",
        "vivado_place_design",
        "vivado_route_design",
        "rapidwright_optimize_cell_placement",
        "rapidwright_optimize_lut_input_cone",
    }
)

EXECUTE_CORE_TOOLS: frozenset[str] = frozenset(
    PRIMARY_STRATEGY_TOOL_NAMES
    | {
        "vivado_physopt_and_route",
        "vivado_phys_opt_design",
        "vivado_opt_design",
        "vivado_place_design",
        "vivado_route_design",
        "vivado_write_checkpoint",
        "vivado_open_checkpoint",
        "vivado_report_timing_summary",
        "report_step_state",
    }
)

POST_EVAL_TOOLS: frozenset[str] = frozenset({
    "vivado_route_design",
    "vivado_phys_opt_design",
    "vivado_physopt_and_route",
    "rapidwright_execute_pblock_strategy",
    "rapidwright_execute_muxf_tree_reorder_strategy",
})

DESIGN_MODIFICATION_TOOLS: frozenset[str] = frozenset(
    STRATEGY_TOOL_NAMES
    | {
        "vivado_create_and_apply_pblock",
        "vivado_unplace_cells",
        "vivado_open_checkpoint",
        "rapidwright_optimize_fanout_batch",
        "rapidwright_execute_physopt_strategy",
        "rapidwright_smart_retiming",
        "vivado_opt_design",
    }
)

SIDE_EFFECT_TOOLS: frozenset[str] = DESIGN_MODIFICATION_TOOLS
