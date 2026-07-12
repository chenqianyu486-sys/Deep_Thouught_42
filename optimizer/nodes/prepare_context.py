"""Prepare context node: compress and prepare LLM context.

Handles context compression, handoff injection, and snapshot building.

Reference: dcp_optimizer.py _compress_context() (L1662-1741),
_prepare_api_messages() (L1423-1477).
"""

from __future__ import annotations

import logging

from ..state import OptimizerState
from ..deps import NodeDeps
from ..edges import NodeName
from ..pure.compress import compress_context
from ..pure.constants import STRATEGY_MAP as _STRATEGY_MAP
from ..pure.tool_filter import LoopPhase

logger = logging.getLogger(__name__)

# Build strategy-to-tool mapping text from shared constant (single source of truth).
_STRATEGY_MAPPING_LINES = "\n".join(
    f"      {k} → {v.execute_tool}" for k, v in sorted(_STRATEGY_MAP.items())
)

# ── FORMAT_GUARD: split into BASE + per-phase addenda ──────────────────
# The BASE guard contains phase-agnostic rules that apply to every phase.
# Per-phase addenda contain ONLY the guidance relevant to that phase,
# avoiding token waste from irrelevant instructions (e.g. EXECUTE-only
# tool-filtering rules shown during ANALYZE).

BASE_FORMAT_GUARD = """OUTPUT FORMAT — call `report_step_state` in every response as a structured tool call,
alongside any other tool calls (or alone if making none). Process control goes in the tool
call; analysis and reasoning go in text.

RESPONSIVENESS — REASON BEFORE ACTING:
  Always include a brief reasoning line in the text body before/alongside tool
  calls. An empty text body with only tool calls wastes a turn and is treated
  as a no-op (2 consecutive empty responses force-exit the phase). State what
  you observed and what you are about to do.

CELL NAME CONTRACT — CRITICAL FOR TOOL CALLS:
  Cell-targeting tools (rapidwright_optimize_cell_placement, rapidwright_*_strategy,
  rapidwright_optimize_lut_input_cone, etc.) require HIERARCHICAL cell instance names
  that contain the '/' separator (e.g. 'u_core/u_alu/lut1').
  - The [CELL REGISTRY] section in your context (injected every turn, right after
    the system message) is the canonical, compression-resistant source of valid
    cell names. ALWAYS copy names from there when calling cell-targeting tools.
  - DO NOT reconstruct cell names from memory or from compressed tool outputs —
    these are frequently truncated or hallucinated.
  - Device sites (SLICE_X*, DSP*_X*, RAMB*_X*) and bare type names (LUT6, FDRE)
    are NOT valid cell names — they will be rejected at the tool boundary.
  - If you submit invalid names, the tool returns a structured rejection with
    suggested canonical names from the registry. Use those suggestions to correct
    and re-issue the call.
  - For execution tools (pblock, combinational_rebalance, lut_muxf_repack,
    muxf_tree_reorder, flatten_lut_cascade, cell_replication), the framework
    auto-injects critical_path_cells/paths from verified state data (extracted
    via vivado_extract_critical_path_cells) to avoid data-quality issues from
    raw TCL extraction. cell_replication injects a rich-object critical_paths
    format ([{cells:[{name,delay,type,fanout}]}]) built from verified state.
    If your provided cells/paths are replaced, you receive a [DATA INTEGRITY]
    notice with the verified cells used - this is expected and NOT an error;
    you do NOT need to manually extract paths for these tools.
  - After any design modification (place/route/opt_design), the registry is
    marked stale; re-fetch via vivado_extract_critical_path_cells or
    rapidwright_search_cells before targeting cells again.

NET NAME CONTRACT — CRITICAL FOR FANOUT TOOL CALLS:
  - rapidwright_execute_fanout_strategy takes a `nets` argument of NET names
    (e.g. 'M1w[21]') - a DIFFERENT name-space from cell names. Never feed
    hierarchical cell names to it, and never feed net names to cell-targeting
    tools.
  - Authoritative net-name source: Module 4 `high_fanout_nets` in the Dashboard
    (or vivado_get_cached_high_fanout_nets). In Module 2 `delay_hotspots`,
    entries tagged `[net]` are NET names; entries tagged with a cell type
    (`[LUT6]`, `[FDRE]`, `[MUXF7]`...) are cell names - mind the distinction.
  - The framework AUTO-INJECTS and OVERRIDES the `nets` argument from verified
    state data (vivado_get_critical_high_fanout_nets, resolved to parent net
    names). Simply call the tool directly. If your supplied nets are replaced
    you receive a [DATA INTEGRITY] notice - expected, NOT an error. If instead
    you get a warning that no verified nets are available, fetch ONCE with
    vivado_get_critical_high_fanout_nets(min_fanout=50) then retry.
  - NEVER hand-copy net names from timing-report text: Vivado drops the 'w'
    suffix on LUT/MUXF output nets (report shows 'M1[21]' but the netlist net
    is 'M1w[21]'), so report-copied names are wrong and cause regressions.

PIN NAME CONTRACT - lut_input_cone uses PIN names, NOT cell names:
  - rapidwright_optimize_lut_input_cone takes `hierarchical_input_pins` - PIN
    names, which are a cell name plus a pin suffix (e.g. 'u_core/u_alu/lut6/I0').
    These are a DIFFERENT name-space from cell names.
  - Construct pin names by appending an input pin suffix (/I0-/I5 for LUT
    inputs) to a cell name from [CELL REGISTRY]. Do NOT pass bare cell names -
    they pass the loose pin validation but fail in RapidWright (getCell returns
    null for a pin path).
  - PinSwap (rapidwright_optimize_pin_swapping) is the OTHER pin-related tool:
    it takes `critical_paths` as [{cells:[cell-name,...]}] (object-wrapped cell
    names, NOT list[list[str]] like the combinational tools) and is NOT
    auto-injected - build it manually from [CELL REGISTRY] cell names.

DESIGN CONSISTENCY — CRITICAL REQUIREMENT:
  The competition requires STRICT design logic equivalence. Any optimization must preserve
  functional correctness. Use validation tools to verify consistency after modifications.

  Safe tools (READ-ONLY, always safe):
    - vivado_report_timing_summary, vivado_extract_critical_path_cells
    - rapidwright_report_timing, rapidwright_analyze_*, rapidwright_search_cells
    - vivado_check_design_status, vivado_validate_timing

  Risky tools (MODIFY design, require validation):
    - rapidwright_optimize_*, rapidwright_execute_*, rapidwright_smart_retiming
    - vivado_place_design, vivado_route_design, vivado_phys_opt_design

  Validation workflow after ANY design modification:
    1. vivado_check_design_status — verify design is placed/routed
    2. vivado_validate_timing — verify WNS/TNS are acceptable
    3. rapidwright_compare_designs — verify structural consistency

  RapidWright accuracy warning for large designs (>200K cells):
    - Cannot predict route-congestion-induced timing
    - Absolute WNS may have 0.5ns+ error on cross-SLR paths
    - Only directional comparison (better/worse) is reliable
    - Always verify with Vivado for final decisions

STALE DATA HANDLING — CRITICAL:
  Dashboard fields marked `[stale]` mean the design was modified after that
  data was collected — they are NOT current. Freshness labels are kept in sync
  with actual refreshes, so `[fresh]` is trustworthy and `[stale]` is real.
  What the framework already refreshes for you (do NOT waste rounds duplicating):
  - WNS/TNS: auto-refreshed at ANALYZE/SELECT_STRATEGY entry and on strategy
    re-entry into EXECUTE. The WNS shown for strategy decisions is current.
  - Critical paths: auto-refreshed at EXECUTE entry for netlist strategies
    (MUXFTreeReorder, LUTCascade, CombinationalRebalance, LUTMUXFRepack,
    CellReplication) and
    auto-injected as verified state data for pblock/combinational execution
    tools. When your provided cells/paths are replaced you receive a
    [DATA INTEGRITY] notice — that is expected, not an error.
  - High-fanout nets: auto-refreshed at ANALYZE entry when stale, and
    auto-injected+overridden as verified state data for
    rapidwright_execute_fanout_strategy (see NET NAME CONTRACT). You do NOT
    need to fetch nets manually for the fanout tool.
  When you MUST refresh manually:
  - Before a cell-targeting operation whose critical paths still show `[stale]`
    AND were not auto-refreshed above: call vivado_extract_critical_path_cells
    (num_paths=10) once, then proceed to the execution tool.
  - If a TCL-driven change occurred outside the framework and you are uncertain
    whether `[fresh]` data is still valid: refresh before high-stakes decisions.
  Ignoring stale data leads to wrong strategy decisions, but redundant refreshes
  waste rounds and push you toward the no-progress limit. Trust `[fresh]`, and
  refresh `[stale]` only when the framework has not already done so.

STRICTLY FORBIDDEN:
  - XML/HTML tags in text
  - Omitting the report_step_state tool call entirely
  - Skipping validation after design modifications
"""

_PHASE_GUIDES: dict[str, str] = {
    LoopPhase.ANALYZE.value: f"""PHASE-GATED TOOL AVAILABILITY — CRITICAL:
  The tool set changes per phase. The phase label in the [PHASE — Context &
  Dashboard] header is the AUTHORITATIVE current phase (your report_step_state
  .strategy_phase is advisory and does NOT drive routing).
  - ANALYZE: only diagnostic/read-only tools are exposed. Execution tools
    (rapidwright_execute_*, vivado_place/route/phys_opt_design) are NOT
    available here. Do NOT attempt to execute a strategy during ANALYZE —
    you will see "tool not found". Finish analysis (ANALYZE_DONE) first.""",

    LoopPhase.SELECT_STRATEGY.value: f"""PHASE-GATED TOOL AVAILABILITY — CRITICAL:
  - SELECT_STRATEGY: pick exactly one strategy_name via report_step_state.
    Execution tools are NOT available here. Review the strategy_catalog in
    the Dashboard and the handoff findings, then signal your choice.

Strategy-to-tool mapping (for your reference when choosing):
{_STRATEGY_MAPPING_LINES}

NOTE: A strategy listed here has a corresponding execution tool in the
EXECUTE phase. CellReplication only becomes available during EXECUTE after
you have selected it and the phase transitions.""",

    LoopPhase.EXECUTE.value: f"""PHASE-GATED TOOL AVAILABILITY — CRITICAL:
  - EXECUTE_STRATEGY: only the selected strategy's primary tool(s) are exposed.

EXECUTE phase: tool filtering restricts available tools to the selected strategy.
Auto-chain actions handle post-skill workflow (checkpoint open, route, timing).
Strategy-to-tool mapping:
{_STRATEGY_MAPPING_LINES}

AUTO-INJECTED STRATEGY DATA (DO NOT extract before calling execution tools):
  For netlist-modifying strategies (MUXFTreeReorder, CombinationalRebalance,
  LUTMUXFRepack, LUTCascade, CellReplication), critical_paths are automatically injected from
  verified state data when you call the execution tool. Simply call the tool
  directly — the system fills in critical_paths. Manual extraction via
  vivado_extract_critical_path_cells or design_data_read BEFORE tool invocation
  wastes rounds and hits the no-progress limit. If the Dashboard shows
  critical_paths as [stale], refresh ONCE with vivado_extract_critical_path_cells(num_paths=10),
  then immediately call the execution tool.
  For Fanout strategy, the `nets` argument is automatically injected AND
  overridden from verified high_fanout_nets state data (parent net names) -
  call rapidwright_execute_fanout_strategy directly and do NOT hand-copy net
  names from timing reports (they drop the 'w' suffix). If your supplied nets
  are replaced you receive a [DATA INTEGRITY] notice; this is expected, not an
  error. If no verified nets are available, fetch ONCE with
  vivado_get_critical_high_fanout_nets(min_fanout=50) then retry.

PBLOCK AUTO-CHAIN BEHAVIOR (prefer LOCAL pblock, auto-fallback when too narrow):
  rapidwright_execute_pblock_strategy auto-chains: unplace_cells(cells=critical_path_cells) →
  create_and_apply_pblock(cells=critical_path_cells, is_soft=is_soft_recommended) →
  place_design → route_design → report_timing_summary.
  KEY: Only the critical_path_cells (~50 cells from Dashboard) are unplaced and
  bound to the pblock. The remaining 99%+ of the design stays placed/routed — this
  is INCREMENTAL P&R, not a full tear-down. Vivado auto-reuses prior routing for
  unchanged nets.

  The pblock region is SIZED for the bound cells (bound cell resources ×
  resource_multiplier), NOT the whole design. is_soft follows the BOUND cells'
  true density — when only a few cells are bound, density is low and IS_SOFT=0
  (hard pblock), providing a genuine placement constraint.
  EXCEPTION: if that local plan collapses to an ultra-tiny single-column region for
  a much larger distributed design, the tool may automatically fall back to a wider
  whole-design soft pblock, omit cells=... on the create/apply step, and replace
  local unplace_cells with a global place_design -unplace before re-placement.

  Very small WNS improvements (e.g., <0.05ns) may be P&R random noise rather than
  pblock effect. If PBLOCK yields delta ≈ 0 or negligible, do NOT re-select it
  the same iteration; switch strategies.

PBLOCK MANDATORY VIVADO FLOW:
  The RapidWright PBLOCK tool only plans the pblock region — it does NOT modify
  the design. The auto-chain handles all Vivado steps for you; do NOT call Vivado
  tools manually during PBLOCK execution. See the result's sizing_basis /
  bound_resources / bound_cell_count fields to understand region sizing.

PLACE/ROUTE DIRECTIVE TUNING (optional, advanced):
  Skill tools (rapidwright_execute_*_strategy, rapidwright_flatten_lut_cascade)
  accept optional place_directive / route_directive arguments that override the
  auto-chain's default "Explore". Omit them to use the safe default. Only pass
  values from the safe whitelists; invalid values abort the chain and waste a
  full P&R run.

  Place directives - pick by the CURRENT dominant bottleneck:
    - Explore (default, balanced)
    - ExtraTimingOpt - logic-depth-limited paths
    - ExtraPostPlacementOpt - WNS stuck, squeeze placement
    - AltSpreadLogic_high/medium/low - congestion-bound (check severity)
    - EarlyBlockPlacement - RAM/DSP-block-anchored placement
    - SSI_SpreadLogic_high/low - multi-SLR / long-net delay
  Route directives - pick by bottleneck:
    - Explore (default, balanced)
    - AggressiveExplore - timing-critical / congestion, explore more routes
    - HigherDelayCost - squeeze delay over iterations
    - NoTimingRelaxation - prevent router relaxing timing (congestion-bound)
    - RuntimeOptimized / Quick - fast runtime
    - NOTE: Congestion_Explore / Congestion_NetDelay_* / AlternateRoutability / SSI_Explore / Performance_* are Vivado strategy-preset names, NOT valid route_design -directive values (rejected by 2025.1 with Constraints 18-641).

  Guidance: match the directive to the dominant bottleneck reported in the
  Dashboard (congestion level, critical-path type, WNS slack distribution).
  When unsure, OMIT and let default Explore run. Do NOT cycle random
  directives hoping to get lucky — each failed chain costs a full P&R run.
  Not every skill tool accepts both; pblock/opt/fanout/flatten take both,
  physopt/muxf_tree take route_directive only.""",

    LoopPhase.EVALUATE.value: f"""PHASE-GATED TOOL AVAILABILITY — CRITICAL:
   - EVALUATE: read-only tools to assess the WNS delta and decide next.

DECISION GUIDANCE (choose flow_control based on verdict):
   - verdict=IMPROVED: PREFER CONTINUE for 1-2 more rounds - the strategy is still
     yielding WNS gains, so extract its remaining value (e.g. a tighter pblock, a
     different directive) before switching. Only choose SWITCH_STRATEGY if you have
     evidence the gains have plateaued (2+ CONTINUE rounds with shrinking deltas) or
     a different bottleneck now dominates. Abandoning after a single improving round
     wastes strategies that are still working.
   - verdict=UNCHANGED: choose SWITCH_STRATEGY (same strategy is now blocked this iteration; CONTINUE will not help) or NEXT_ITERATION.
   - verdict=REGRESSED: choose SWITCH_STRATEGY, or ROLLBACK if WNS regressed beyond threshold (auto-rollback may trigger).
   - If consecutive strategies yield no improvement, choose EXHAUSTED to end this iteration.
   - Do NOT choose CONTINUE after UNCHANGED — re-analyzing an unchanged design wastes budget.""",
}


def build_phase_format_guard(phase: LoopPhase) -> str:
    """Build the phase-specific FORMAT_GUARD text.

    Combines BASE_FORMAT_GUARD with the per-phase addendum. The result is
    prefixed with a marker so callers can detect prior injection (idempotency).
    """
    phase_key = phase.value if hasattr(phase, "value") else str(phase)
    addendum = _PHASE_GUIDES.get(phase_key, "")
    marker = f"[FORMAT_GUARD:{phase_key}]"
    return f"{marker}\n{BASE_FORMAT_GUARD}\n{addendum}"


async def prepare_context_node(
    state: OptimizerState, deps: NodeDeps
) -> str:
    """Prepare LLM context for the upcoming tool loop.

    Actions:
        1. Compress context if needed
        2. Inject FORMAT_GUARD (once, first iteration)
        3. Inject handoff prompt if not yet injected

    Note: Dashboard is injected per-LLM-call in each phase's
    _call_phase_llm() via inject_merged_dashboard(), not here.
    Node return values are not used for routing — graph edges decide.

    Returns:
        Next node name (deterministic: llm_tool_loop).
    """
    # 1. Compress context if memory_manager available
    if deps.memory_manager is not None:
        try:
            # Trigger compression if over threshold
            if compress_context(state, deps):
                state.context.compression_count += 1
                logger.info(f"[prepare_context] Context compressed (count={state.context.compression_count})")
        except Exception as e:
            logger.warning(f"[prepare_context] Compression failed: {e}")

    # 2. Inject handoff prompt
    if not state.model.iteration_handoff_injected and state.model.iteration_handoff_prompt:
        if deps.compat is not None:
            try:
                deps.compat.add_message("system", state.model.iteration_handoff_prompt)
                state.model.iteration_handoff_injected = True
                logger.info("[prepare_context] Handoff prompt injected")
            except Exception as e:
                logger.warning(f"[prepare_context] Handoff injection failed: {e}")

    # 4. Inject cost budget awareness (once, first iteration where cost > 0)
    if not state.model.budget_injected and deps.compat is not None:
        cost_used = getattr(getattr(state, "cost", None), "total_cost", 0.0)
        cost_limit = getattr(getattr(state, "cost", None), "cost_hard_limit", 1.0)
        if cost_limit > 0 and cost_used > 0:
            budget_pct = min(100 * cost_used / cost_limit, 100)
            budget_msg = (
                f"\n[BUDGET] Spent: ${cost_used:.4f} / ${cost_limit:.2f} limit "
                f"({budget_pct:.0f}% used). Prefer cheaper actions if budget is tight."
            )
            deps.compat.add_message("system", budget_msg)
            state.model.budget_injected = True
            logger.info(f"[prepare_context] BUDGET injected (${cost_used:.4f}/{cost_limit:.2f})")


    return NodeName.LLM_TOOL_LOOP
