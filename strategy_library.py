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
     "detection": "get_tile_info: utilization > 80%"},
]

SCENARIO_WORKFLOW = [
    "Critical Path Analysis: extract_critical_path_cells(num_paths=50), report_timing_summary",
    "Candidate Detection: get_critical_high_fanout_nets(min_fanout=100), analyze_critical_path_spread",
    "Scenario Ranking: Rank by WNS contribution x path_count",
]

# ── Strategy Selection ──────────────────────────────────────────

STRATEGY_DECISION_TABLE = [
    {"condition": "initial_analysis.recommendation == 'PBLOCK'", "strategy": "strategy_1"},
    {"condition": "paths_analyzed <= 2 AND avg_distance < threshold", "strategy": "strategy_2"},
    {"condition": "High fanout nets present, no spread", "strategy": "strategy_3"},
]

# ── Strategy Sequences ──────────────────────────────────────────

STRATEGIES = {
    "PBLOCK": {
        "name": "PBLOCK-Based Re-placement",
        "trigger": "recommendation == 'PBLOCK'",
        "sequence": [
            {"step": "report_utilization_for_pblock", "platform": "Vivado", "params": None},
            {"step": "analyze_fabric_for_pblock", "platform": "RapidWright",
             "params": {"LUT": "1.5x", "FF": "1.5x"}},
            {"step": "convert_fabric_region_to_pblock", "platform": "RapidWright",
             "params": {"use_clock_regions": False}},
            {"step": "place_design -unplace", "platform": "Vivado", "params": None},
            {"step": "create_and_apply_pblock", "platform": "Vivado",
             "params": {"ranges_format": "SLICE_X...DSP48E2_X...RAMB18_X..."}},
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
}

# Map from _infer_strategy_from_tools labels to STRATEGIES keys
STRATEGY_LABEL_MAP = {
    "PBLOCK": "PBLOCK",
    "PhysOpt": "PhysOpt",
    "Fanout": "Fanout",
}

# ── Skill Guidance ──────────────────────────────────────────────

SKILL_GUIDANCE = {
    "analyze_net_detour": {
        "category": "ANALYSIS",
        "input": "pin_paths list from Vivado extract_critical_path_pins",
        "output": "cells with detour_ratio > threshold, sorted descending",
        "threshold": "2.0 (higher = worse placement)",
        "condition": "Critical path has >3 LUT levels OR high detour cells detected",
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
    lines.append("")
    lines.append("Decision Table:")
    for d in STRATEGY_DECISION_TABLE:
        lines.append(f"  - IF {d['condition']} -> {d['strategy']}")
    return "\n".join(lines)


def get_strategy_catalog() -> str:
    """Compact strategy catalog for system prompt (names + purposes only)."""
    parts = ["Available strategies:"]
    # ordered list matching original numbering
    ordered = ["PBLOCK", "PhysOpt", "Fanout"]
    for i, key in enumerate(ordered, 1):
        s = STRATEGIES.get(key)
        if s:
            parts.append(f"  strategy_{i}: {s['name']} (trigger: {s['trigger']})")
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
