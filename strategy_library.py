"""External Strategy Library for FPGA Timing Optimization.

Extracted from SYSTEM_PROMPT.TXT for dynamic on-demand injection.
Reduces always-present system prompt size by ~50%+.
"""

from typing import Optional

# ── Scenario Identification ─────────────────────────────────────

SCENARIO_DETECTION_MATRIX = [
    {"id": "wide_lut", "scenario": "Wide LUT Cascades",
     "detection": "extract_critical_path_cells: >3 LUT levels in series"},
    {"id": "high_fanout", "scenario": "High Fanout Nets",
     "detection": "report_timing: fo=N > 100"},
    {"id": "distributed", "scenario": "Distributed Logic",
     "detection": "analyze_critical_path_spread: avg_distance > 70 tiles"},
    {"id": "control_imbalance", "scenario": "Control Logic Imbalance",
     "detection": "report_timing_summary: max_delay variation > 2x"},
    {"id": "congestion", "scenario": "Routing Congestion",
     "detection": "analyze_congestion: severity=HIGH or congested_ratio > 0.3"},
    {"id": "congestion_spread", "scenario": "Congestion-Aware Spreading",
     "detection": "analyze_congestion: severity=HIGH AND PBLOCK ineffective"},
    {"id": "deep_chain", "scenario": "Deep Combinational Chains",
     "detection": "extract_critical_path_pins: >2 LUT levels between FFs on critical paths"},
]

SCENARIO_WORKFLOW = [
    "Critical Path Analysis: extract_critical_path_cells(num_paths=50), report_timing_summary",
    "Candidate Detection: get_critical_high_fanout_nets(min_fanout=100), analyze_critical_path_spread",
    "Scenario Ranking: Rank by WNS contribution x path_count",
]

# ── Strategy Sequences ──────────────────────────────────────────

STRATEGIES = {
    "PBLOCK": {
        "name": "PBLOCK-Based Re-placement",
        "trigger": "recommendation == 'PBLOCK'",
        "sequence": [
            {"step": "report_utilization_for_pblock", "platform": "Vivado", "params": None,
             "note": "Get current resource counts"},
            {"step": "analyze_pblock_region", "platform": "RapidWright",
             "params": {"LUT": "1.5x", "FF": "1.5x"},
             "note": "READ-ONLY: finds optimal pblock region, returns pblock_ranges"},
            {"step": "place_design -unplace", "platform": "Vivado", "params": None},
            {"step": "create_and_apply_pblock", "platform": "Vivado",
             "params": {"ranges": "from analyze_pblock_region output", "is_soft": "auto (from skill IS_SOFT recommendation)"}},
            {"step": "place_design", "platform": "Vivado", "params": None},
            {"step": "route_design", "platform": "Vivado", "params": None},
            {"step": "report_timing_summary", "platform": "Vivado", "params": None},
        ],
    },
    "PhysOpt": {
        "name": "Physical Optimization",
        "trigger": "1-2 paths with spread",
        "sequence": [
            {"step": "phys_opt_design", "platform": "Vivado", "params": None},
            {"step": "route_design", "platform": "Vivado", "params": None},
            {"step": "report_timing_summary", "platform": "Vivado", "params": None},
        ],
    },
    "OptDesign": {
        "name": "Logic Optimization (opt_design)",
        "trigger": "Logic-depth limited design (>70% logic delay), "
                   "6-7 LUT levels on critical paths",
        "ff_prerequisite": "",
        "sequence": [
            {"step": "opt_design_strategy", "platform": "RapidWright",
             "params": {"directive": "Explore", "retarget": True},
             "note": "Generate opt_design plan + auto-chain Vivado execution"},
            {"step": "place_design", "platform": "Vivado", "params": None,
             "note": "Auto-chained: re-place after netlist change"},
            {"step": "route_design", "platform": "Vivado", "params": None,
             "note": "Auto-chained: re-route"},
            {"step": "report_timing_summary", "platform": "Vivado", "params": None,
             "note": "Auto-chained: evaluate timing"},
        ],
    },
    "Fanout": {
       "name": "High Fanout Net Optimization",
       "trigger": "High fanout nets, no spread",
       "sequence": [
           {"step": "optimize_fanout_batch", "platform": "RapidWright",
            "params": {"nets": "[{net_name: ..., fanout: ...}, ...]"}},
           {"step": "write_checkpoint", "platform": "RapidWright",
            "params": {"overwrite": True, "directory": "temp"}},
           {"step": "open_checkpoint", "platform": "Vivado", "params": None},
           {"step": "route_design", "platform": "Vivado", "params": None},
           {"step": "report_timing_summary", "platform": "Vivado", "params": None},
       ],
    },
    "PinSwap": {
        "name": "Pin Swapping Optimization",
        "trigger": "WNS stuck ~-0.3ns, LUT input pins have delay variation",
        "sequence": [
            {"step": "optimize_pin_swapping", "platform": "RapidWright",
             "params": {"critical_paths": "from Vivado extract_critical_path_pins"}},
            {"step": "write_checkpoint", "platform": "RapidWright",
             "params": {"overwrite": True, "directory": "temp"}},
            {"step": "open_checkpoint", "platform": "Vivado", "params": None},
            {"step": "route_design", "platform": "Vivado", "params": None},
            {"step": "report_timing_summary", "platform": "Vivado", "params": None},
        ],
    },
    "LUTCascade": {
        "name": "LUT Cascade Flattening",
        "trigger": ">3 LUT levels in series on critical paths",
        "sequence": [
            {"step": "extract_critical_path_cells", "platform": "Vivado",
             "params": {"num_paths": 50},
             "note": "Get critical path cell lists"},
            {"step": "flatten_lut_cascade", "platform": "RapidWright",
             "params": {"min_cascade_depth": 3, "temp_dir": "temp"},
             "note": "Merge LUT cascades, writes checkpoint"},
            {"step": "open_checkpoint", "platform": "Vivado", "params": None},
            {"step": "route_design", "platform": "Vivado", "params": None},
            {"step": "report_timing_summary", "platform": "Vivado", "params": None},
        ],
    },
    "CellReplication": {
        "name": "Critical Path Cell Replication",
        "trigger": "High delay cells on critical paths (fanout > 10 or delay > 0.3 ns)",
        "sequence": [
            {"step": "extract_critical_path_cells", "platform": "Vivado",
             "params": {"num_paths": 50},
             "note": "Get critical path cell lists with delays"},
            {"step": "replicate_critical_cells", "platform": "RapidWright",
             "params": {"delay_threshold": 0.3, "max_replications": 10},
             "note": "Replicate high-delay cells, writes checkpoint"},
            {"step": "open_checkpoint", "platform": "Vivado", "params": None},
            {"step": "route_design", "platform": "Vivado", "params": None},
            {"step": "report_timing_summary", "platform": "Vivado", "params": None},
        ],
    },
    "CongestionSpreading": {
        "name": "Congestion-Aware Cell Spreading",
        "trigger": "analyze_congestion severity=HIGH",
        "sequence": [
            {"step": "analyze_congestion", "platform": "RapidWright",
             "params": {"utilization_threshold": 0.8},
             "note": "Identify congested columns and severity"},
            {"step": "execute_congestion_spreading", "platform": "RapidWright",
             "params": {"max_cells_to_spread": 20, "spread_distance": 10},
             "note": "Spread high-score cells outward, writes checkpoint"},
            {"step": "open_checkpoint", "platform": "Vivado", "params": None},
            {"step": "route_design", "platform": "Vivado", "params": None},
            {"step": "report_timing_summary", "platform": "Vivado", "params": None},
        ],
    },
    "RegisterRetiming": {
        "name": "Register Retiming (Targeted Pipeline Insertion)",
        "trigger": "WNS stuck, deep combinational chains (>2 LUTs) on critical paths",
        "ff_prerequisite": "⚠️  REQUIRES adequate flip-flops (FF utilization >= 2%). "
                           "With low FF count, retiming has minimal impact — "
                           "FFs are physical insertion targets for pipeline stage creation.",
        "sequence": [
            {"step": "extract_critical_path_pins", "platform": "Vivado",
             "params": {"num_paths": 20},
             "note": "Get pin-level critical path data"},
            {"step": "analyze_register_retiming", "platform": "RapidWright",
             "params": {"delay_threshold": 0.5, "min_chain_depth": 2},
             "note": "READ-ONLY: identify retiming candidates"},
            {"step": "execute_register_retiming", "platform": "RapidWright",
             "params": {"max_retiming_ops": 5},
             "note": "Insert pipeline FFs, writes checkpoint"},
            {"step": "open_checkpoint", "platform": "Vivado", "params": None},
            {"step": "route_design", "platform": "Vivado", "params": None},
            {"step": "report_timing_summary", "platform": "Vivado", "params": None},
        ],
    },
    "SmartRetiming": {
        "name": "Smart Register Retiming (Verified Pipeline Insertion)",
        "trigger": "WNS stuck, critical paths have deep combinational chains "
                   "(>2 LUTs) between pipeline registers, FF>0 required",
        "sequence": [
            {"step": "extract_critical_path_pins", "platform": "Vivado",
             "params": {"num_paths": 20},
             "note": "Extract pin-level critical path data for analysis"},
            {"step": "smart_retiming", "platform": "RapidWright",
             "params": {"verify_each": True, "auto_rollback": True, "max_ops": 5},
             "note": "Score candidates, insert FFs incrementally, verify each, "
                     "auto-rollback degradations, writes final checkpoint"},
            {"step": "open_checkpoint", "platform": "Vivado", "params": None},
            {"step": "route_design", "platform": "Vivado", "params": None},
            {"step": "report_timing_summary", "platform": "Vivado", "params": None},
            {"step": "compare_design_structure", "platform": "RapidWright",
             "params": {"golden_dcp": "original DCP path", "revised_dcp": "final checkpoint"},
             "note": "OPTIONAL: verify structural equivalence if golden DCP available"},
        ],
    },
    "NetSwap": {
        "name": "Net Swapping Optimization",
        "trigger": "Routing congestion within SLICE sites, LUT pairs with swappable input nets",
        "sequence": [
            {"step": "analyze_net_swapping", "platform": "RapidWright",
             "params": {"max_candidates": 20, "wirelength_threshold": 50},
             "note": "READ-ONLY: identify net swap candidates within SLICEs"},
            {"step": "execute_net_swapping", "platform": "RapidWright",
             "params": {"candidates": "from analyze_net_swapping output"},
             "note": "Perform net swaps, writes checkpoint"},
            {"step": "open_checkpoint", "platform": "Vivado", "params": None},
            {"step": "route_design", "platform": "Vivado", "params": None},
            {"step": "report_timing_summary", "platform": "Vivado", "params": None},
        ],
    },
    "PhysOpt+RegisterRetiming": {
        "name": "PhysOpt + Register Retiming (Combined)",
        "trigger": "Logic-depth limited design (logic_delay > 70%), WNS > -2.0, "
                   "deep combinational chains (>2 LUTs), FF > 0",
        "sequence": [
            {"step": "vivado_physopt_and_route", "platform": "Vivado",
             "params": {"directive": "Explore"},
             "note": "Combined PhysOpt + route in one atomic call. Returns pre/post WNS."},
            {"step": "analyze_register_retiming", "platform": "RapidWright",
             "params": {"delay_threshold": 0.5, "min_chain_depth": 2},
             "note": "READ-ONLY: find retiming candidates on routed design"},
            {"step": "execute_register_retiming", "platform": "RapidWright",
             "params": {"max_retiming_ops": 5},
             "note": "Insert pipeline FFs. Auto-chains open_checkpoint + route_design"},
        ],
    },
    "LogicResynthesis": {
        "name": "Logic Resynthesis (synth_design -remap)",
        "trigger": "NN/datapath design with MUXF7/8 cascades, "
                   "100% logic delay or deep combinational levels on critical paths",
        "sequence": [
            {"step": "vivado_run_tcl", "platform": "Vivado",
             "params": {"command": "synth_design -remap -flatten_hierarchy rebuilt -top [current_top]"},
             "note": "Re-synthesize logic with remapping. May reduce LUT levels by restructuring."},
            {"step": "vivado_place_design", "platform": "Vivado", "params": None},
            {"step": "vivado_route_design", "platform": "Vivado", "params": None},
            {"step": "report_timing_summary", "platform": "Vivado", "params": None},
        ],
    },
    "PhysOptAggressive": {
        "name": "Aggressive Physical Optimization (Explore directive)",
        "trigger": "WNS > -3.0, logic-depth limited design with spread, "
                   "need more aggressive optimization than standard PhysOpt",
        "sequence": [
            {"step": "vivado_physopt_and_route", "platform": "Vivado",
             "params": {"directive": "AggressiveExplore"},
             "note": "Aggressive PhysOpt with Explore directive. Tries multiple optimization passes."},
        ],
    },
    "CombinationalRebalance": {
        "name": "Combinational Logic Rebalancing (validation-safe retiming)",
        "trigger": "Deep combinational chains (LUT6/MUXF7/MUXF8 cascades) "
                   "between registers on critical paths, logic levels >= 3",
        "ff_prerequisite": "",
        "sequence": [
            {"step": "extract_critical_path_cells", "platform": "Vivado",
             "params": {"num_paths": 10},
             "note": "Get critical path cell lists for combinational cone analysis"},
            {"step": "execute_combinational_rebalancing_strategy", "platform": "RapidWright",
             "params": {"min_depth": 3, "directive": "Explore", "retarget": True},
             "note": "RapidWright identifies deep combinational segments, generates "
                     "Vivado opt_design -remap plan (logic-equivalent, NO FF insert)"},
            {"step": "opt_design", "platform": "Vivado",
             "params": {"directive": "from plan", "retarget": "from plan"},
             "note": "Auto-chained: logic-equivalent resynthesis to rebalance depth"},
            {"step": "place_design", "platform": "Vivado", "params": None,
             "note": "Auto-chained: re-place after netlist change"},
            {"step": "route_design", "platform": "Vivado", "params": None,
             "note": "Auto-chained: re-route"},
            {"step": "report_timing_summary", "platform": "Vivado", "params": None,
             "note": "Auto-chained: evaluate timing"},
        ],
    },
    "LUTMUXFRepack": {
        "name": "LUT6+MUXF Co-Repack (validation-safe LUT merge)",
        "trigger": "NN/wide-datapath design with MUXF7/MUXF8 + LUT6 cascade on critical "
                   "paths (flatten_lut_cascade likely returns optimized_count=0 on cones "
                   "exceeding 6-input LUT limit — run it in ANALYZE to confirm)",
        "ff_prerequisite": "",
        "sequence": [
            {"step": "extract_critical_path_cells", "platform": "Vivado",
             "params": {"num_paths": 10},
             "note": "Get critical path cell lists for LUT/MUXF adjacency analysis"},
            {"step": "execute_lut_muxf_repack_strategy", "platform": "RapidWright",
             "params": {"directive": "AddRemap", "retarget": True},
             "note": "RapidWright identifies LUT6<->MUXF pairs + LUT5 merge candidates, "
                     "generates Vivado opt_design -AddRemap plan (logic-equivalent, NO FF insert)"},
            {"step": "opt_design", "platform": "Vivado",
             "params": {"directive": "from plan", "retarget": "from plan"},
             "note": "Auto-chained: aggressive LUT-equation repacking"},
            {"step": "place_design", "platform": "Vivado", "params": None,
             "note": "Auto-chained: re-place after repack"},
            {"step": "route_design", "platform": "Vivado", "params": None,
             "note": "Auto-chained: re-route"},
            {"step": "report_timing_summary", "platform": "Vivado", "params": None,
             "note": "Auto-chained: evaluate timing"},
        ],
    },
    "MUXFTreeReorder": {
        "name": "MUXF Tree Reorder (validation-safe carry-reorder analogue)",
        "trigger": "NN/datapath design without CARRY4, MUXF7/MUXF8 mux trees >= 2 levels "
                   "on critical paths, route-dominated delay profile",
        "ff_prerequisite": "",
        "sequence": [
            {"step": "extract_critical_path_cells", "platform": "Vivado",
             "params": {"num_paths": 10},
             "note": "Get critical path cell lists for MUXF tree analysis"},
            {"step": "execute_muxf_tree_reorder_strategy", "platform": "RapidWright",
             "params": {"directive": "Explore", "min_tree_depth": 2},
             "note": "RapidWright identifies MUXF tree runs, generates Vivado "
                     "phys_opt_design plan (NO -retime, logic-equivalent, NO FF insert)"},
            {"step": "phys_opt_design", "platform": "Vivado",
             "params": {"directive": "from plan"},
             "note": "Auto-chained: reorder MUXF selection paths (no retiming)"},
            {"step": "route_design", "platform": "Vivado", "params": None,
             "note": "Auto-chained: re-route after phys_opt"},
            {"step": "report_timing_summary", "platform": "Vivado", "params": None,
             "note": "Auto-chained: evaluate timing"},
        ],
    },
}

# Map from _infer_strategy_from_tools labels to STRATEGIES keys
STRATEGY_LABEL_MAP = {
    "PBLOCK": "PBLOCK",
    "PhysOpt": "PhysOpt",
    "OptDesign": "OptDesign",
    "Fanout": "Fanout",
    "LUTCascade": "LUTCascade",
    "PinSwap": "PinSwap",
    "CellReplication": "CellReplication",
    "CongestionSpreading": "CongestionSpreading",
    "RegisterRetiming": "RegisterRetiming",
    "NetSwap": "NetSwap",
    "SmartRetiming": "SmartRetiming",
    "PhysOpt+RegisterRetiming": "PhysOpt+RegisterRetiming",
    "LogicResynthesis": "LogicResynthesis",
    "PhysOptAggressive": "PhysOptAggressive",
    "CombinationalRebalance": "CombinationalRebalance",
    "LUTMUXFRepack": "LUTMUXFRepack",
    "MUXFTreeReorder": "MUXFTreeReorder",
}

# ── Validation Compatibility ────────────────────────────────────
# validate_dcps.py uses cycle-exact functional simulation (compares
# outputs every clock cycle). Strategies marked False change design
# latency (insert new pipeline FFs), causing output misalignment →
# MISMATCH → validation FAIL. Only latency-preserving strategies pass.
# Exposed to the LLM via get_strategy_catalog() to prevent selecting
# validation-incompatible strategies that would waste iterations.
STRATEGY_VALIDATION_SAFE: dict[str, bool] = {
    "PBLOCK": True,               # placement only, no logic change
    "PhysOpt": True,              # Vivado-guaranteed (retiming blocked by safety guard)
    "OptDesign": True,            # logic-equivalent remapping (Vivado-guaranteed)
    "Fanout": True,               # net splitting, preserves function
    "PinSwap": True,              # pin swapping within LUT, preserves function
    "LUTCascade": True,           # LUT merging, preserves function
    "CellReplication": True,      # cell replication, preserves function
    "CongestionSpreading": True,  # placement only, no logic change
    "RegisterRetiming": False,    # INSERTS new FFs → changes latency → FAILS validation
    "SmartRetiming": False,       # INSERTS new FFs → changes latency → FAILS validation
    "NetSwap": True,              # net swapping within SLICE, preserves function
    "PhysOpt+RegisterRetiming": False,  # includes register retiming → changes latency
    "LogicResynthesis": True,     # synth_design -remap, logic-equivalent remapping
    "PhysOptAggressive": True,    # Vivado-guaranteed (retiming blocked by safety guard)
    "CombinationalRebalance": True,   # opt_design -remap, logic-equivalent, NO FF insert
    "LUTMUXFRepack": True,           # opt_design -AddRemap, logic-equivalent, NO FF insert
    "MUXFTreeReorder": True,         # phys_opt_design (no -retime), logic-equivalent, NO FF insert
}

# Map strategy names to registered skill identifiers
STRATEGY_SKILL_MAP = {
    "PBLOCK": "pblock_strategy",
    "PhysOpt": "physopt_strategy",
    "OptDesign": "opt_design_strategy",
    "Fanout": "fanout_strategy",
    "LUTCascade": "lut_cascade_flattening",
    "PinSwap": "pin_swapping_strategy",
    "CellReplication": "critical_path_cell_replication",
    "CongestionSpreading": "execute_congestion_spreading",
    "RegisterRetiming": "execute_register_retiming",
    "SmartRetiming": "smart_retiming",
    "NetSwap": "execute_net_swapping",
    "LogicResynthesis": "logic_resynthesis",
    "PhysOptAggressive": "physopt_strategy",
    "CombinationalRebalance": "combinational_rebalancing_strategy",
    "LUTMUXFRepack": "lut_muxf_repack_strategy",
    "MUXFTreeReorder": "muxf_tree_reorder_strategy",
}

# ── Skill Guidance ──────────────────────────────────────────────

SKILL_GUIDANCE = {
    "analyze_net_detour": {
        "category": "ANALYSIS",
        "input": "pin_paths list from Vivado extract_critical_path_pins",
        "output": "cells with detour_ratio > threshold, sorted descending",
        "threshold": "2.0 (higher = worse placement)",
        "condition": "Critical path has >3 LUT levels OR high detour cells detected",
        "interpretation": "Empty result (no cells > threshold) = routing already compact for analyzed paths. Valid diagnosis, not a failure.",
    },
    "optimize_cell_placement": {
        "category": "PLACEMENT",
        "input": "list of cell names identified by analyze_net_detour",
        "output": "new placement for each cell at connection centroid",
        "note": "Must write_checkpoint and re-route in Vivado",
        "condition": "Cells with detour_ratio > 2.0 identified",
    },
    "smart_region_search": {
        "category": "PLACEMENT",
        "input": "target resource counts (1.5x current usage from utilization report)",
        "output": "optimal rectangular region with pblock ranges",
        "advantage": "Avoids delay-heavy columns (URAM, HPIO) and prioritizes high-density columns",
        "condition": "Need to create pblock but optimal region unknown",
    },
    "execute_fanout_strategy": {
        "category": "OPTIMIZATION",
        "input": "List of high fanout nets from vivado_get_critical_high_fanout_nets: [{\"net_name\": str, \"fanout\": int}, ...]",
        "output": "Optimization results: nets_processed, successful_count, failed_count, checkpoint_path, per-net results",
        "condition": "High fanout nets present (fanout > 100), no path spread",
        "prerequisite": "Call vivado_get_critical_high_fanout_nets first to get the list of high fanout nets with fanout counts",
        "note": "split_factor is calculated internally as max(3, min(8, fanout // 100)) — do NOT provide it",
        "prerequisite": "PBLOCK placement for distributed designs (avg_distance > 70). Run execute_pblock_strategy first.",
        "risk": "HIGH when used after PBLOCK placement. Fanout splitting disrupts PBLOCK dense layout — WNS often regresses. "
                "Running fanout BEFORE PBLOCK on distributed designs causes WNS regression of 0.5ns+ (observed: -0.978 → -1.660ns).",
        "contraindications": "Risk: Fanout splitting after PBLOCK disrupts dense layout (WNS regression observed). "
                             "On distributed designs (avg_distance > 70), running before PBLOCK regresses WNS ~0.5ns.",
    },
    "analyze_pblock_region": {
       "category": "ANALYSIS",
       "input": "target_lut_count, target_ff_count, target_dsp_count, target_bram_count from vivado_report_utilization_for_pblock",
       "output": "region coordinates, pblock_ranges string, estimated resources, deficit (LUT/FF/DSP/BRAM), next_steps list, is_soft_recommended",
       "advantage": "Finds optimal pblock region in one call; avoids delay-heavy columns. Returns pblock_ranges ready for vivado_create_and_apply_pblock. "
                   "DSP/BRAM deficits now reported. IS_SOFT auto-recommended based on utilization density (>80% → soft constraint).",
       "condition": "avg_distance > 70 (distributed scenario) or recommendation == 'PBLOCK'",
       "prerequisite": "Call vivado_report_utilization_for_pblock first to get LUT/FF/DSP/BRAM counts",
    },
    "optimize_pin_swapping": {
        "category": "OPTIMIZATION",
        "input": "critical_paths JSON from Vivado extract_critical_path_pins",
        "output": "swap results + checkpoint_path",
        "condition": "WNS stuck ~-0.3ns, LUT input pins have delay variation",
        "prerequisite": "Load DCP via read_checkpoint first",
        "risk": "If WNS regresses > 0.05ns after reroute, rollback to pre_swap_checkpoint",
    },
    "flatten_lut_cascade": {
        "category": "OPTIMIZATION",
        "input": "critical_paths from Vivado extract_critical_path_cells: [[cell1, cell2, ...], ...]",
        "output": "cascades_found, optimized_count, checkpoint_path, per-pin results",
        "condition": "Critical paths have >3 LUT levels in series (logic depth bottleneck)",
        "prerequisite": "Call vivado extract_critical_path_cells first to get path cell lists",
        "risk": "LOW — saves checkpoint before mutation. If WNS regresses >0.05ns, roll back to pre-flatten checkpoint.",
        "contraindications": "Ineffective on neural network / wide-datapath designs where logic cones exceed 6-input LUT physical limit — optimized_count will be 0.",
    },
    "replicate_critical_cells": {
        "category": "OPTIMIZATION",
        "input": "critical_paths JSON + delay_threshold (default 0.3 ns)",
        "output": "replication results + checkpoint_path",
        "condition": "High delay cells on critical paths (fanout > 10 or delay > 0.3 ns)",
        "prerequisite": "Load DCP via read_checkpoint first",
        "risk": "MEDIUM — cell replication increases resource usage. Limit to 10 cells max.",
    },
    "analyze_congestion_spreading": {
        "category": "ANALYSIS",
        "input": "congestion_threshold (default 0.8), max_cells_to_spread (default 20)",
        "output": "ranked candidate cells with congestion_connectivity_score and spread direction",
        "condition": "analyze_congestion shows severity=HIGH or congested_ratio > 0.3",
        "prerequisite": "Design must be loaded via read_checkpoint",
        "interpretation": "Empty candidates = no congestion issues. Non-empty = cells with many connections in congested regions.",
    },
    "execute_congestion_spreading": {
        "category": "OPTIMIZATION",
        "input": "max_cells_to_spread (default 20), spread_distance (default 10 columns)",
        "output": "cells_moved, density_reduction, checkpoint_path",
        "condition": "analyze_congestion_spreading identified candidates AND congestion severity=HIGH",
        "prerequisite": "Call analyze_congestion_spreading first to understand impact",
        "risk": "MEDIUM — moving cells can disrupt existing good placement. Limit spread_distance to avoid excessive displacement.",
        "contraindications": "Ineffective when congestion is LOW or MODERATE. PBLOCK is typically more effective for geographic constraints.",
    },
    "analyze_register_retiming": {
        "category": "ANALYSIS",
        "input": "critical_paths JSON from Vivado extract_critical_path_pins",
        "output": "retiming candidates with source/dest FF, chain depth, insertion point",
        "condition": "WNS stuck, critical paths have deep combinational chains (>2 LUTs between FFs)",
        "prerequisite": "Load DCP via read_checkpoint first, call extract_critical_path_pins in Vivado",
        "interpretation": "Empty candidates = no deep chains found. Non-empty = segments where pipeline registers would help.",
        "contraindications": "Low impact when FF utilization < 2% — few FFs available as pipeline insertion targets. "
                             "PBLOCK or PhysOpt typically more effective for combinational-dominated designs.",
    },
    "execute_register_retiming": {
        "category": "OPTIMIZATION",
        "input": "retiming_candidates from analyze_register_retiming, max_retiming_ops (default 5)",
        "output": "retiming_ops_performed, checkpoint_path, per-candidate results",
        "condition": "analyze_register_retiming identified candidates with deep chains",
        "prerequisite": "Call analyze_register_retiming first to get candidate list",
        "risk": "MEDIUM - inserting FFs changes logic depth and routing. Limit to 5 ops per call.",
        "contraindications": "Risk: If Vivado global retiming (phys_opt_design -retime) already caused functional errors, "
                             "targeted retiming may also be problematic. Low impact when FF utilization < 2% — "
                             "few sequential elements available for pipeline insertion.",
    },
    "analyze_net_swapping": {
        "category": "ANALYSIS",
        "input": "max_candidates (default 20), wirelength_threshold (default 50)",
        "output": "ranked list of net swap candidates within SLICE sites",
        "condition": "Routing congestion within SLICEs, LUT pairs with identical INIT strings",
        "prerequisite": "Design must be loaded via read_checkpoint",
        "interpretation": "Empty candidates = no beneficial swaps found. Non-empty = swaps that reduce wirelength.",
    },
    "execute_net_swapping": {
        "category": "OPTIMIZATION",
        "input": "candidates from analyze_net_swapping, temp_dir, checkpoint_prefix",
        "output": "swaps_performed, swaps_failed, checkpoint_path",
        "condition": "analyze_net_swapping identified candidates",
        "prerequisite": "Call analyze_net_swapping first to get candidate list",
        "risk": "LOW - swaps are within a single SLICE, limited blast radius. If WNS regresses >0.05ns, roll back to pre_swap_checkpoint.",
    },
    "opt_design_strategy": {
        "category": "OPTIMIZATION",
        "input": "directive (default: Explore), retarget (default: True)",
        "output": "execution plan with recommended opt_design parameters",
        "condition": "Logic-depth limited design (>70% logic delay), "
                     "6-7 LUT levels on critical paths",
        "prerequisite": "Design must be loaded. opt_design runs BEFORE placement — "
                        "no placement/routing prerequisites needed.",
        "post_actions": "Chain auto-executes: vivado_opt_design → vivado_place_design → "
                        "vivado_route_design → vivado_report_timing_summary → "
                        "vivado_extract_critical_path_cells",
        "risk": "LOW — opt_design has NO retiming options. Unlike phys_opt_design, "
                "all directives are safe for functional correctness. "
                "Netlist changes are logic-equivalent remapping (Vivado-guaranteed).",
        "contraindications": "Ineffective on designs already at minimal LUT depth, or where routing delay "
                             "(not logic depth) is the bottleneck. Note: validate_dcps.py Phase 1 (structural) "
                             "may fail due to expected cell name changes — Phase 2 (functional simulation) "
                             "provides the correctness guarantee.",
    },
    "execute_combinational_rebalancing_strategy": {
        "category": "OPTIMIZATION",
        "input": "critical_paths from Vivado extract_critical_path_cells: [[cell1, cell2, ...], ...]",
        "output": "StrategyPlan with Vivado opt_design -remap steps (segments_found, max_depth, avg_depth)",
        "condition": "Deep combinational chains (LUT6/MUXF7/MUXF8 cascades) between registers "
                     "on critical paths; register retiming unsafe (cycle-exact validation required)",
        "prerequisite": "Call vivado_extract_critical_path_cells (num_paths=10) first to get path cell lists",
        "post_actions": "Chain auto-executes: vivado_opt_design -> vivado_place_design -> "
                        "vivado_route_design -> vivado_report_timing_summary -> "
                        "vivado_extract_critical_path_cells",
        "risk": "LOW — opt_design -remap is logic-equivalent resynthesis only. NO flip-flops inserted, "
                "latency preserved. Validation-safe (cycle-exact).",
        "advantage": "Validation-safe alternative to register retiming: achieves the same goal "
                     "(shortening critical-path logic depth) via combinational resynthesis only.",
        "contraindications": "Ineffective when critical paths are already shallow (logic depth < 3) "
                             "or when routing delay (not logic depth) is the dominant bottleneck.",
    },
    "execute_lut_muxf_repack_strategy": {
        "category": "OPTIMIZATION",
        "input": "critical_paths from Vivado extract_critical_path_cells: [[cell1, cell2, ...], ...]",
        "output": "StrategyPlan with Vivado opt_design -AddRemap steps (lut_muxf_pairs, lut5_candidates)",
        "condition": "NN/wide-datapath design with MUXF7/MUXF8 + LUT6 cascades on critical paths. "
                     "Tip: run flatten_lut_cascade first — if optimized_count=0, wide cones confirm LUTMUXFRepack is ideal",
        "prerequisite": "Call vivado_extract_critical_path_cells (num_paths=10) first to get path cell lists",
        "post_actions": "Chain auto-executes: vivado_opt_design -> vivado_place_design -> "
                        "vivado_route_design -> vivado_report_timing_summary",
        "risk": "LOW — opt_design -AddRemap aggressively remaps LUT equations but is still "
                "logic-equivalent. NO flip-flops inserted, latency preserved. Validation-safe.",
        "advantage": "Handles the wide LUT6/MUXF cones that flatten_lut_cascade cannot (it returns "
                     "optimized_count=0 on cones > 6 inputs). Targets 8:1/16:1 mux tree cascades.",
        "contraindications": "Ineffective when no LUT<->MUXF adjacency pairs or LUT5 merge candidates "
                             "exist on critical paths.",
    },
    "execute_muxf_tree_reorder_strategy": {
        "category": "OPTIMIZATION",
        "input": "critical_paths from Vivado extract_critical_path_cells: [[cell1, cell2, ...], ...]",
        "output": "StrategyPlan with Vivado phys_opt_design steps (muxf_trees, max_depth, avg_depth)",
        "condition": "NN/datapath design without CARRY4, MUXF7/MUXF8 mux trees >= 2 levels on critical paths, "
                     "route-dominated delay profile",
        "prerequisite": "Call vivado_extract_critical_path_cells (num_paths=10) first to get path cell lists. "
                        "Design must be placed (phys_opt_design requires placement).",
        "post_actions": "Chain auto-executes: vivado_phys_opt_design -> vivado_route_design -> "
                        "vivado_report_timing_summary",
        "risk": "LOW — phys_opt_design without -retime performs only placement-level "
                "logic-equivalent optimization (pin swap, cell relocation, duplication). "
                "NO flip-flops inserted or moved, latency preserved. Validation-safe.",
        "advantage": "Maps 'carry chain reorder' onto MUXF-tree designs (no CARRY4). Pulls critical "
                     "mux inputs to faster selection levels.",
        "contraindications": "Do NOT pass directive='AddRetime' — it would insert/move FFs and fail "
                             "validation (the skill rejects AddRetime). Ineffective when no MUXF tree "
                             "runs (depth >= 2) exist on critical paths.",
    },
}

SKILL_EXECUTION_PATTERN = [
    "Use Vivado report_timing/extract_critical_path_cells to get path data",
    "Call appropriate skill via MCP tool",
    "Interpret results and decide on next action",
    "For placement changes: write_checkpoint -> open in Vivado -> route_design -> report_timing",
]

# ── Custom Optimization ─────────────────────────────────────────

CUSTOM_OPTIMIZATION_PATTERNS = [
    "LUT cascade flattening: >3 LUTs in series",
    "Fanout splitting: nets with fanout > 100",
    "Physical replication: High latency across spread",
    "Pblock constraint: Geographic clustering",
]

CUSTOM_OPTIMIZATION_WORKFLOW = [
    "Identify transformation pattern",
    "Check RapidWright ECO classes: LUTInputConeOpt, FanOutOptimization, PortDirectioning, ReplaceFlopASICWithFPGA",
    "Implement in rapidwright_tools.py",
    "Register in server.py TOOL_DEFINITIONS",
]

# ── Public Formatting Functions ─────────────────────────────────


def get_scenario_guide() -> str:
    """Full scenario identification guide + decision table."""
    lines = ["**Scenario Identification:**"]
    lines.append("Detection Matrix:")
    for s in SCENARIO_DETECTION_MATRIX:
        lines.append(f"  - {s['id']}: {s['scenario']} ({s['detection']})")
    lines.append("Workflow:")
    for i, step in enumerate(SCENARIO_WORKFLOW, 1):
        lines.append(f"  {i}. {step}")
    return "\n".join(lines)


def get_strategy_catalog(exclude_strategies: list[str] | None = None) -> str:
    """Compact strategy catalog for system prompt (names + purposes only).

    Strategies are listed alphabetically — no implied priority. The LLM
    should choose based on design characteristics, not list position.

    Args:
        exclude_strategies: Strategy keys to omit from the catalog
            (e.g., strategies with reason='strategy_ineffective').

    Note:
        Strategies marked unsafe in STRATEGY_VALIDATION_SAFE are always
        excluded — they insert pipeline FFs, change design latency, and
        will FAIL cycle-exact validation (validate_dcps.py). Their
        definitions and tool implementations remain in the codebase for
        potential future use with latency-tolerant validation.
    """
    excluded = set(exclude_strategies or [])
    # Always exclude validation-unsafe strategies (insert new FFs → change latency)
    excluded.update(k for k, safe in STRATEGY_VALIDATION_SAFE.items() if not safe)
    parts = ["Available strategies:"]
    # Alphabetical ordering — no implied priority
    ordered = sorted(k for k in STRATEGIES.keys() if k not in excluded)
    for key in ordered:
        s = STRATEGIES.get(key)
        if s:
            line = f"  - {s['name']} (trigger: {s['trigger']})"
            if s.get('ff_prerequisite'):
                line += f" - {s['ff_prerequisite']}"
            parts.append(line)
    if not parts[1:]:
        parts.append("  (all strategies excluded)")
    return "\n".join(parts)


def get_strategy_details(name: str) -> Optional[str]:
    """Return formatted strategy sequence for a given strategy name/label.

    Accepts both infer_strategy labels (PBLOCK/PhysOpt/Fanout) and
    full strategy keys.
    """
    key = STRATEGY_LABEL_MAP.get(name, name)
    strategy = STRATEGIES.get(key)
    if not strategy:
        return None
    lines = [f"**Strategy: {strategy['name']}**"]
    lines.append(f"Trigger: {strategy['trigger']}")
    lines.append("Sequence:")
    for step in strategy["sequence"]:
        platform = step["platform"]
        step_name = step["step"]
        if step["params"]:
            lines.append(f"  - {step_name} ({platform}, params: {step['params']})")
        else:
            lines.append(f"  - {step_name} ({platform})")
    return "\n".join(lines)


def get_skill_guide(name: Optional[str] = None) -> str:
    """Return skill guidance. If name is None, returns all skills catalog."""
    if name:
        skill = SKILL_GUIDANCE.get(name)
        if not skill:
            return ""
        lines = [f"**Skill: {name}**"]
        for k, v in skill.items():
            lines.append(f"  {k}: {v}")
        return "\n".join(lines)

    # Full catalog
    lines = ["**Skill Catalog:**"]
    for sname, sinfo in SKILL_GUIDANCE.items():
        lines.append(f"  - {sname} ({sinfo.get('category', 'N/A')}): "
                     f"{sinfo.get('condition', '')}")
    lines.append("")
    lines.append("Execution Pattern:")
    for i, step in enumerate(SKILL_EXECUTION_PATTERN, 1):
        lines.append(f"  {i}. {step}")
    return "\n".join(lines)


def get_custom_optimization() -> str:
    """Return custom optimization guide (rarely used)."""
    lines = ["**Custom Optimization** (when existing tools insufficient):"]
    lines.append("Workflow:")
    for step in CUSTOM_OPTIMIZATION_WORKFLOW:
        lines.append(f"  - {step}")
    lines.append("Patterns:")
    for p in CUSTOM_OPTIMIZATION_PATTERNS:
        lines.append(f"  - {p}")
    return "\n".join(lines)
