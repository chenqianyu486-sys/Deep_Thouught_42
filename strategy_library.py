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
        "name": "Physical Optimization (can try ExploreWithHoldFix for hold violations, AlternateReplication for high fanout, AggressiveFanoutOpt for fanout>1000)",
        "trigger": "1-2 paths with spread",
        "sequence": [
            {"step": "phys_opt_design", "platform": "Vivado", "params": None},
            {"step": "route_design", "platform": "Vivado", "params": None},
            {"step": "report_timing_summary", "platform": "Vivado", "params": None},
        ],
    },
    "OptDesign": {
        "name": "Logic Optimization (opt_design) (can try ExploreSequentialArea for sequential area optimization, DataSpreadMem for memory spreading)",
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
    "PlaceRouteDirectiveExplore": {
        "name": "Place & Route Directive Exploration (no retiming)",
        "trigger": "WNS stuck in recent iterations (last 2 rounds |delta| < 0.05ns), "
                   "and place/route directive combinations have not been fully explored",
        "sequence": [
            {"step": "place_design", "platform": "Vivado",
             "params": {"directive": "Explore"},
             "note": "Re-place with Explore directive (wider optimization net)"},
            {"step": "route_design", "platform": "Vivado",
             "params": {"directive": "Explore"},
             "note": "Re-route with Explore directive"},
            {"step": "report_timing_summary", "platform": "Vivado", "params": None,
             "note": "Evaluate timing after directive exploration"},
        ],
    },
    "CongestionRouteExplore": {
        "name": "Congestion-Aware Route Directive Exploration",
        "trigger": "analyze_congestion severity=MEDIUM/HIGH, WNS > -1.0, "
                   "and route directives have not been explored for congestion",
        "sequence": [
            {"step": "route_design", "platform": "Vivado",
             "params": {"directive": "Congestion_Explore"},
             "note": "Re-route with congestion-aware directive"},
            {"step": "report_timing_summary", "platform": "Vivado", "params": None,
             "note": "Evaluate timing after congestion-aware routing"},
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
    "PlaceRouteDirectiveExplore": "PlaceRouteDirectiveExplore",
    "CongestionRouteExplore": "CongestionRouteExplore",
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
    "PlaceRouteDirectiveExplore": True,   # placement+route only, no logic change
    "CongestionRouteExplore": True,       # routing only, no logic change
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


def get_strategy_catalog(exclude_strategies: list[str] | None = None,
                         blocked_strategies: dict[str, str] | None = None) -> str:
    """Compact strategy catalog for system prompt (names + purposes only).

    Strategies are listed alphabetically — no implied priority. The LLM
    should choose based on design characteristics, not list position.

    Args:
        exclude_strategies: Strategy keys to omit entirely from the catalog
            (e.g., strategies with reason='tool_error' that are retriable).
        blocked_strategies: Strategy keys to show as [BLOCKED] placeholders
            instead of removing (e.g., TTL/cooldown blocks so the LLM
            understands why they're unavailable). Values are reason strings.

    Note:
        Strategies marked unsafe in STRATEGY_VALIDATION_SAFE are always
        excluded — they insert pipeline FFs, change design latency, and
        will FAIL cycle-exact validation (validate_dcps.py). Their
        definitions and tool implementations remain in the codebase for
        potential future use with latency-tolerant validation.
    """
    excluded = set(exclude_strategies or [])
    blocked = blocked_strategies or {}
    # Always exclude validation-unsafe strategies (insert new FFs → change latency)
    excluded.update(k for k, safe in STRATEGY_VALIDATION_SAFE.items() if not safe)

    all_keys = set(STRATEGIES.keys())
    available_keys = sorted(k for k in all_keys if k not in excluded and k not in blocked)
    blocked_keys = sorted(k for k in all_keys if k in blocked)

    parts = ["Available strategies:"]
    for key in available_keys:
        s = STRATEGIES.get(key)
        if s:
            line = f"  - {s['name']} (trigger: {s['trigger']})"
            if s.get('ff_prerequisite'):
                line += f" - {s['ff_prerequisite']}"
            parts.append(line)
    if not parts[1:]:
        parts.append("  (all strategies unavailable)")

    # Append [BLOCKED] placeholders so the LLM understands why
    # some strategies are missing from the available list.
    if blocked_keys:
        parts.append("")
        parts.append("  # Blocked strategies (not available this iteration):")
        for key in blocked_keys:
            reason = blocked.get(key, "")
            s = STRATEGIES.get(key)
            name = s['name'] if s else key
            parts.append(f"  - {name} [BLOCKED{f': {reason}' if reason else ''}]")

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


def filter_applicable_strategies(design_profile: dict) -> list[str]:
    """Filter strategies based on design characteristics.
    
    Args:
        design_profile: dict with keys like 'utilization', 'fanout_count', 
                       'avg_distance', 'critical_path_types'
    Returns:
        List of applicable strategy names in priority order.
    """
    strategies = []
    util = design_profile.get("utilization", 0.5)
    high_fanout = design_profile.get("fanout_count", 0)
    avg_dist = design_profile.get("avg_distance", 0)
    
    # PBLOCK: best for distributed designs with high avg distance
    if avg_dist > 50 or util < 0.3:
        strategies.append("PBLOCK")
    
    # PhysOpt: universally applicable, priority depends on utilization
    if util > 0.1:  # almost always useful
        strategies.append("PhysOpt")
    
    # Fanout: only if there are high-fanout nets
    if high_fanout > 10:
        strategies.append("Fanout")
    
    # SmartRegion: for designs with multiple distinct regions
    if util > 0.2 and avg_dist > 30:
        strategies.append("SmartRegion")
    
    # NetDetour: last resort for routing-congested designs
    if util > 0.5:  # only for dense designs
        strategies.append("NetDetour")
    
    return strategies


# Timing check strategy: fast checks before expensive ones
TIMING_CHECK_ORDER = [
    "rapidwright_report_timing",  # ~2.5s, ~2% error
    "vivado_report_timing_summary",  # ~14s, 0% error (authoritative)
]

def get_fastest_timing_tool() -> str:
    """Return the fastest available timing check tool."""
    return "rapidwright_report_timing"

def should_use_full_timing(wns_change_ns: float) -> bool:
    """Decide if full Vivado timing is needed based on rapidwright estimate."""
    # If rapidwright shows significant change, verify with Vivado
    return abs(wns_change_ns) > 0.050

def suggest_next_strategy(current: str, wns_delta: float, attempt: int) -> str:
    """Suggest the next strategy based on current results."""
    if wns_delta > 0.020 and attempt < 3:
        return current  # Keep current strategy if working
    return "PhysOpt"  # Default fallback

def get_default_strategy_for_design(utilization: float, avg_distance: float) -> str:
    """Get the best starting strategy for a design profile."""
    if avg_distance > 70 and utilization < 0.3:
        return "PBLOCK"
    if utilization > 0.5:
        return "PhysOpt"
    return "PBLOCK"  # Default: most effective

def get_strategy_success_rate(strategy: str) -> float:
    """Get historical success rate for a strategy."""
    rates = {"PBLOCK": 0.85, "PhysOpt": 0.70, "Fanout": 0.40, "SmartRegion": 0.35, "NetDetour": 0.30}
    return rates.get(strategy, 0.30)

def get_strategy_timeout(strategy: str, design_params: dict) -> int:
    """Unified strategy timeout lookup."""
    timeouts = {"PBLOCK": 240, "PhysOpt": 180, "Fanout": 120, "SmartRegion": 90, "NetDetour": 60}
    return timeouts.get(strategy, 120)

def compute_strategy_confidence(strategy: str, design_features: dict) -> float:
    """Confidence score for a strategy given design features."""
    confidence = {"PBLOCK": 0.85, "PhysOpt": 0.70, "Fanout": 0.50, "SmartRegion": 0.45, "NetDetour": 0.35}
    return confidence.get(strategy, 0.30)

def compute_combined_strategy_score(scores: dict) -> float:
    """Combine multiple strategy scores into one."""
    if not scores: return 0.0
    return sum(scores.values()) / len(scores)

STRATEGY_COST_BENEFIT_ENABLED = True
STRATEGY_AUTO_SKIP_THRESHOLD = 0.01
STRATEGY_RETRY_WITH_DIFFERENT_PARAMS = True

def record_strategy_result(strategy: str, wns_before: float, wns_after: float, cost: float):
    """Record a strategy execution result for future learning."""
    pass  # Hook for future ML-based strategy selection

def get_strategy_attempt_limit(strategy: str) -> int:
    """Max attempts before giving up on a strategy."""
    return {"PBLOCK": 4, "PhysOpt": 3, "Fanout": 2, "SmartRegion": 2, "NetDetour": 2}.get(strategy, 2)

def compute_strategy_roi(strategy: str, wns_gain: float, cost_s: float) -> float:
    """Return on investment: WNS gain per minute of execution."""
    if cost_s <= 0: return float("inf")
    return (wns_gain * 1000) / (cost_s / 60)  # picoseconds per minute
