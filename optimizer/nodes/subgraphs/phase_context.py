"""Phase-specific context builders.

Each phase gets a tailored prompt injected as the first user message.
The prompts are designed to be direct, clear, and focused on the
single task the LLM should perform in that phase.
"""

from __future__ import annotations

from optimizer.pure.tool_filter import LoopPhase
from optimizer.nodes.subgraphs.phase_handoff import PhaseHandoff


def build_phase_context(phase: LoopPhase, handoff: PhaseHandoff | None = None) -> str:
    """Build the phase-specific context prompt.

    Args:
        phase: The phase to build context for.
        handoff: Structured handoff from the previous phase (None for ANALYZE).

    Returns:
        Context string to inject as the first user message of the phase.
    """
    builders = {
        LoopPhase.ANALYZE: _build_analyze_context,
        LoopPhase.SELECT_STRATEGY: _build_select_strategy_context,
        LoopPhase.EXECUTE: _build_execute_context,
        LoopPhase.EVALUATE: _build_evaluate_context,
    }
    builder = builders.get(phase, _build_analyze_context)
    return builder(handoff)


def _build_analyze_context(_handoff: PhaseHandoff | None) -> str:
    return """[ANALYZE PHASE]
You are in the ANALYSIS phase. Your ONLY job is to gather data about the
design's timing bottlenecks. DO NOT call optimization or execution tools.

Build a multi-dimensional picture by calling analysis tools:
  1. Timing: report_timing_summary, extract_critical_path_cells
  2. Placement: analyze_net_detour, analyze_critical_path_spread
  3. Congestion: analyze_congestion
  4. Fanout: get_critical_high_fanout_nets

Check the Dashboard first — data marked as fresh does not need re-querying.

When you have sufficient data, call:
  report_step_state(flow_control="ANALYZE_DONE")
with a summary of findings in your text response."""


def _build_select_strategy_context(handoff: PhaseHandoff | None) -> str:
    summary_text = ""
    if handoff and handoff.llm_summary:
        summary_text = handoff.llm_summary

    wns_str = f"{handoff.wns:.3f}ns" if handoff and handoff.wns is not None else "unknown"
    tns_str = f"{handoff.tns:.3f}ns" if handoff and handoff.tns is not None else "unknown"
    fe_str = str(handoff.failing_endpoints) if handoff and handoff.failing_endpoints is not None else "unknown"
    tools_str = ", ".join(handoff.tools_called[-10:]) if handoff and handoff.tools_called else "none"

    return f"""[STRATEGY SELECTION]
## Current Timing
WNS: {wns_str}, TNS: {tns_str}, Failing Endpoints: {fe_str}

## Analysis Findings (from ANALYZE phase)
{summary_text}

## Tools Used in Analysis
{tools_str}

## Available Strategies
1. PBLOCK — Re-placement for distributed logic (avg_distance > 70)
2. Fanout — High fanout net optimization (fanout > 100)
3. PhysOpt — Physical optimization (1-2 paths, WNS > -2.0)
4. PinSwap — LUT input pin remapping (WNS stuck ~-0.3ns)
5. LUTCascade — Flatten LUT cascades (>3 LUT levels)
6. CellReplication — Replicate critical cells (fanout > 10 or delay > 0.3ns)
7. CongestionSpreading — Spread cells in congested regions
8. RegisterRetiming — Pipeline register insertion (deep chains >2 LUTs, FF>0)
9. NetSwap — Intra-SLICE net swapping

## Constraints
- PBLOCK MUST be applied BEFORE fanout on distributed designs
- Pure combinational design (FF=0): PhysOpt/RegisterRetiming limited impact
- Check Dashboard for failed strategies before choosing

## Instructions
Choose ONE strategy. Explain:
1. Why this strategy fits the current obstacles
2. Expected WNS improvement range
3. Risks and fallback plan

Call: report_step_state(strategy_phase="SELECT_STRATEGY", strategy_name="<chosen>")"""


def _build_execute_context(handoff: PhaseHandoff | None) -> str:
    rationale_text = ""
    if handoff and handoff.llm_summary:
        rationale_text = handoff.llm_summary

    strategy_name = handoff.key_findings.get("strategy_name", "unknown") if handoff else "unknown"

    # Build execution plan hint based on strategy
    plan_hints = {
        "PBLOCK": "analyze_pblock_region -> place_design -unplace -> create_and_apply_pblock -> place_design -> route_design",
        "Fanout": "execute_fanout_strategy -> route_design -> report_timing_summary",
        "PhysOpt": "phys_opt_design -> route_design -> report_timing_summary",
        "PinSwap": "optimize_pin_swapping -> route_design -> report_timing_summary",
        "LUTCascade": "flatten_lut_cascade -> route_design -> report_timing_summary",
        "CellReplication": "replicate_critical_cells -> route_design -> report_timing_summary",
        "CongestionSpreading": "execute_congestion_spreading -> route_design -> report_timing_summary",
        "RegisterRetiming": "execute_register_retiming -> route_design -> report_timing_summary",
        "NetSwap": "execute_net_swapping -> route_design -> report_timing_summary",
    }
    plan = plan_hints.get(strategy_name, "Execute the strategy tools, then route and verify")

    return f"""[EXECUTE PHASE]
## Strategy: {strategy_name}
## Rationale
{rationale_text}

## Execution Plan
{plan}

## Instructions
Execute the chosen strategy via tool calls.
- Chain actions will auto-run for applicable strategies (PBLOCK etc.)
- Auto-evaluation will run after route_design or fanout_strategy
- Call report_step_state(flow_control="EXEC_DONE") when execution is complete
- If a tool fails, report the error and call EXEC_DONE to move to evaluation"""


def _build_evaluate_context(handoff: PhaseHandoff | None) -> str:
    strategy_name = handoff.key_findings.get("strategy_name", "unknown") if handoff else "unknown"
    wns_before = handoff.key_findings.get("wns_before", "unknown") if handoff else "unknown"
    wns_after_raw = handoff.key_findings.get("wns_after")
    wns_after_str = f"{wns_after_raw:.3f}ns" if isinstance(wns_after_raw, (int, float)) else "unknown"

    # Compute delta if before/after data available
    delta_str = ""
    wns_before_val = handoff.key_findings.get("wns_before") if handoff else None
    if isinstance(wns_after_raw, (int, float)) and isinstance(wns_before_val, (int, float)):
        delta = wns_after_raw - wns_before_val
        delta_str = f" (delta={delta:+.3f}ns)"
        if delta > 0.001:
            delta_str += " IMPROVED"
        elif delta < -0.001:
            delta_str += " REGRESSED"
        else:
            delta_str += " UNCHANGED"

    return f"""[EVALUATE PHASE]
## Strategy Executed: {strategy_name}
## Before: WNS={wns_before}
## After: WNS={wns_after_str}{delta_str}

## Instructions
1. Verify the WNS result is accurate (call report_timing_summary if stale)
2. Determine if the strategy achieved its goal
3. Decide next action via report_step_state:
   - Significant improvement + diminishing returns -> flow_control=NEXT_ITERATION
   - No improvement or regression -> flow_control=SWITCH_STRATEGY
   - WNS >= 0 (timing met) -> flow_control=DONE
   - Need more refinement of same strategy -> flow_control=CONTINUE (back to ANALYZE)

If CONTINUE: set strategy_phase="" and strategy_name="" to reset for re-analysis.
If SWITCH_STRATEGY: set strategy_phase="" and strategy_name="" to reset for new strategy."""
