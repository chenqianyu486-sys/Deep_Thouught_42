"""Tool filtering by loop phase.

Each phase gets a focused subset of tools to keep the LLM's attention
on the task at hand.  The ``report_step_state`` tool schema is patched
per-phase so the LLM only sees flow_control signals valid for the
current phase.

Design Consistency Principle:
  - READ-ONLY tools (analyze, report, search) are always safe
  - MODIFY tools (execute, optimize, place, route) require validation
  - LLM should use validation tools after any design modification
"""

from __future__ import annotations

import copy
from enum import Enum

from .constants import EXECUTE_STRATEGY_TOOL_MAP


class LoopPhase(str, Enum):
    ANALYZE = "analyze"
    SELECT_STRATEGY = "select_strategy"
    EXECUTE = "execute"
    EVALUATE = "evaluate"


# ── Design consistency validation tools ──────────────────────────────
# These tools help LLM verify design state without modifying it.
# Available in all phases for autonomous validation.
CONSISTENCY_VALIDATION_TOOLS: frozenset[str] = frozenset({
    "vivado_check_design_status",      # Check placement/routing status
    "vivado_validate_timing",          # Validate timing after modifications
    "rapidwright_estimate_timing",     # Quick timing estimation (direction only)
    "rapidwright_compare_designs",     # Compare designs for consistency
})


# ── Independent RapidWright tools (not part of auto-chains) ──────────
# These tools can be called independently by LLM for fine-grained control.
INDEPENDENT_RAPIDWRIGHT_TOOLS: frozenset[str] = frozenset({
    # Analysis tools (READ-ONLY, safe)
    "rapidwright_analyze_critical_path_spread",
    "rapidwright_analyze_congestion",
    "rapidwright_analyze_pblock_region",
    "rapidwright_analyze_net_detour",
    "rapidwright_report_timing",
    "rapidwright_search_cells",
    "rapidwright_get_design_info",
    "rapidwright_get_device_topology",
    # Execution tools (MODIFY design, require validation)
    "rapidwright_optimize_cell_placement",
    "rapidwright_optimize_lut_input_cone",
    "rapidwright_optimize_pin_swapping",
    "rapidwright_flatten_lut_cascade",
    "rapidwright_replicate_critical_cells",
    "rapidwright_execute_net_swapping",
    "rapidwright_execute_congestion_spreading",
    "rapidwright_optimize_fanout_batch",
    # Validation tools (READ-ONLY, safe)
    "rapidwright_compare_design_structure",
})


# ── Per-phase tool allowlists ──────────────────────────────────────

PHASE_TOOLS: dict[LoopPhase, frozenset[str]] = {
    LoopPhase.ANALYZE: frozenset({
        # Vivado timing/report tools
        "vivado_report_timing_summary",
        "vivado_extract_critical_path_cells",
        "vivado_extract_critical_path_pins",
        "vivado_get_critical_high_fanout_nets",
        "vivado_report_utilization_for_pblock",
        # REMOVED: vivado_report_route_status — data already in Dashboard from init_analysis
        # RapidWright analysis tools
        "rapidwright_analyze_net_detour",
        "rapidwright_analyze_critical_path_spread",
        "rapidwright_analyze_congestion",
        "rapidwright_analyze_pblock_region",
        # REMOVED: rapidwright_get_device_topology — data already in Dashboard from init_analysis
        "rapidwright_report_timing",
        "rapidwright_get_design_info",
        "rapidwright_search_cells",
        # Execution tools (LOW risk, useful as probes during analysis)
        "rapidwright_flatten_lut_cascade",  # probe LUT cascade depth; low risk, saves checkpoint before mutation
        # Internal tools
        "vivado_get_raw_tool_output",
        "vivado_get_cached_high_fanout_nets",
        "report_step_state",
    }) | CONSISTENCY_VALIDATION_TOOLS,

    LoopPhase.SELECT_STRATEGY: frozenset({
        "report_step_state",
        "vivado_get_raw_tool_output",
        "vivado_get_cached_high_fanout_nets",
        "rapidwright_analyze_pblock_region",
    }) | CONSISTENCY_VALIDATION_TOOLS,

    LoopPhase.EXECUTE: frozenset({
        # Strategy execution tools
        "rapidwright_execute_pblock_strategy",
        "rapidwright_execute_fanout_strategy",
        "rapidwright_execute_congestion_spreading",
        "rapidwright_optimize_pin_swapping",
        "rapidwright_flatten_lut_cascade",
        "rapidwright_replicate_critical_cells",
        "rapidwright_execute_net_swapping",
        "rapidwright_optimize_cell_placement",
        "rapidwright_smart_region_search",
        "rapidwright_analyze_pblock_region",
        "rapidwright_optimize_lut_input_cone",
        "rapidwright_execute_opt_design_strategy",
        "rapidwright_execute_combinational_rebalancing_strategy",
        "rapidwright_execute_lut_muxf_repack_strategy",
        "rapidwright_execute_muxf_tree_reorder_strategy",
        "rapidwright_execute_physopt_strategy",
        # Independent RapidWright tools (for fine-grained control)
        "rapidwright_optimize_fanout_batch",
        "rapidwright_analyze_critical_path_spread",
        "rapidwright_analyze_congestion",
        "rapidwright_analyze_net_detour",
        "rapidwright_search_cells",
        "rapidwright_get_design_info",
        "rapidwright_get_device_topology",
        # Vivado execution tools
        "vivado_place_design",
        "vivado_route_design",
        "vivado_opt_design",
        "vivado_phys_opt_design",
        "vivado_physopt_and_route",
        "vivado_write_checkpoint",
        "vivado_run_tcl",
        # Quick-check tools during execution
        "vivado_report_timing_summary",
        "vivado_get_wns",
        # Cached data tools
        "vivado_get_cached_high_fanout_nets",
        # Internal tools
        "report_step_state",
    }) | CONSISTENCY_VALIDATION_TOOLS,

    LoopPhase.EVALUATE: frozenset({
        "vivado_report_timing_summary",
        # REMOVED: vivado_report_route_status — data already in Dashboard from init_analysis
        "rapidwright_report_timing",
        "rapidwright_compare_design_structure",
        "vivado_extract_critical_path_cells",
        "report_step_state",
        "vivado_get_raw_tool_output",
    }) | CONSISTENCY_VALIDATION_TOOLS,
}

# ── Per-phase flow_control signals ─────────────────────────────────
# Each phase only accepts a subset of flow_control signals.  The
# ``report_step_state`` tool schema is patched per-phase so the LLM
# never sees signals that are invalid in the current context.
PHASE_FLOW_CONTROL: dict[LoopPhase, list[str]] = {
    LoopPhase.ANALYZE: [
        "ANALYZE_DONE", "CONTINUE", "DONE", "EXHAUSTED",
    ],
    LoopPhase.SELECT_STRATEGY: [
        "CONTINUE", "EXHAUSTED",
    ],
    LoopPhase.EXECUTE: [
        "EXEC_DONE", "CONTINUE", "EXHAUSTED",
    ],
    LoopPhase.EVALUATE: [
        "DONE", "NEXT_ITERATION", "SWITCH_STRATEGY",
        "CONTINUE", "ROLLBACK", "EXHAUSTED",
    ],
}

# ── Per-phase max tool rounds ──────────────────────────────────────

PHASE_MAX_ROUNDS: dict[LoopPhase, int] = {
    LoopPhase.ANALYZE: 12,
    LoopPhase.SELECT_STRATEGY: 6,
    # Keep EXECUTE short: strategies should run one modifying action and signal
    # EXEC_DONE; longer loops previously wasted wall-clock time on plateaus.
    LoopPhase.EXECUTE: 5,
    LoopPhase.EVALUATE: 8,
}

EXTENDED_EXECUTE_MAX_ROUNDS = 8
# No strategies currently qualify for extended rounds — retiming strategies
# that previously used this were removed (validation-unsafe: change latency).
# To re-enable, add latency-preserving multi-step strategies here.


def get_phase_max_rounds(phase: LoopPhase, strategy: str = "") -> int:
    """Return the round budget, extending only genuinely multi-step strategies."""
    default = PHASE_MAX_ROUNDS.get(phase, 0)
    return default


def filter_tools_for_phase(
    all_tools: list[dict],
    phase: LoopPhase,
    strategy: str = "",
) -> list[dict]:
    """Return only the tools allowed for the given phase.

    The ``report_step_state`` tool schema is patched so its
    ``flow_control`` enum contains only signals valid for *phase*.

    Args:
        all_tools: Full list of OpenAI-format tool definitions.
        phase: Current LoopPhase.
        strategy: Selected strategy. Known EXECUTE strategies expose only
            their mapped primary tool and flow-control callback.

    Returns:
        Filtered list of tool definitions (with patched report_step_state).
    """
    allowed = PHASE_TOOLS.get(phase)
    if allowed is None:
        return all_tools

    if phase == LoopPhase.EXECUTE and strategy:
        primary_tool = EXECUTE_STRATEGY_TOOL_MAP.get(strategy)
        if primary_tool:
            allowed = frozenset({primary_tool, "report_step_state"})

    filtered = []
    for tool in all_tools:
        name = tool.get("function", {}).get("name", "")
        if name in allowed:
            filtered.append(tool)

    # Patch report_step_state with phase-specific flow_control enum
    phase_signals = PHASE_FLOW_CONTROL.get(phase)
    if phase_signals:
        for i, tool in enumerate(filtered):
            if tool.get("function", {}).get("name") == "report_step_state":
                patched = copy.deepcopy(tool)
                props = patched["function"]["parameters"]["properties"]
                props["flow_control"]["enum"] = list(phase_signals)
                props["flow_control"]["description"] = (
                    f"Valid signals for {phase.value} phase: "
                    + ", ".join(phase_signals)
                )
                filtered[i] = patched
                break

    return filtered
