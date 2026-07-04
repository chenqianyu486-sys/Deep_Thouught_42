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
  - After any design modification (place/route/opt_design), the registry is
    marked stale; re-fetch via vivado_extract_critical_path_cells or
    rapidwright_search_cells before targeting cells again.

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
  data was collected — they are NOT current. Before any timing-related decision:
  1. WNS/TNS marked `[stale]` MUST be refreshed via vivado_report_timing_summary
     before evaluating improvement or making strategy decisions.
  2. Critical paths marked `[stale]` MUST be re-extracted via
     vivado_extract_critical_path_cells before any cell-targeting operation.
  3. `[fresh]` means no design modification has been recorded since this field
     was last refreshed. It is generally reliable, but if you are uncertain
     whether a modification occurred (e.g. a TCL-driven change), refresh
     before high-stakes decisions.
  4. Ignoring stale data leads to wrong strategy decisions. When uncertain,
     refresh before deciding.

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

PBLOCK AUTO-CHAIN BEHAVIOR:
  rapidwright_execute_pblock_strategy auto-chains: unplace → place_design
  (Explore) → route_design (Explore). It therefore tears down and rebuilds
  the existing place/route. On an already-routed design this can land on an
  equal-or-worse result with zero WNS delta — that is a fair "no improvement"
  outcome, NOT a tool error. If PBLOCK yields delta ≈ 0 once, do NOT re-select
  it the same iteration; switch strategies.

PBLOCK MANDATORY VIVADO FLOW:
  The RapidWright PBLOCK tool only plans the pblock — it does NOT modify the
  design. Without the auto-chained Vivado place+route, PBLOCK has ZERO effect
  (always returns UNCHANGED). The auto-chain handles this for you; do NOT
  skip it. Refer to skill_guidance in the Dashboard for the current chain
  and multiplier values.

PLACE/ROUTE DIRECTIVE TUNING (optional, advanced):
  Skill tools (rapidwright_execute_*_strategy, rapidwright_flatten_lut_cascade)
  accept optional place_directive / route_directive arguments that override the
  auto-chain's default "Explore". Omit them to use the safe default. Only pass
  values from the safe whitelists; invalid values abort the chain and waste a
  full P&R run.

  Place directives — pick by the CURRENT dominant bottleneck:
    - Explore (default, balanced)
    - ExtraTimingOpt / Performance_ExtraTimingOpt — logic-depth-limited paths
    - Performance_Explore / Performance_RefinePlacement — WNS stuck, squeeze placement
    - Congestion_SpreadLogic_high/medium/low — congestion-bound (check severity)
    - NetDelay_high/medium/low — long-net / inter-SLR delay dominated
    - Area_Explore — area-pressure limited
    - SSI_SpreadLogic_high/low — multi-SLR designs
  Route directives — pick by bottleneck:
    - Explore (default, balanced)
    - AggressiveExplore / HigherDelayCost — timing-critical, squeeze delay
    - NoTimingRelaxation — prevent router from relaxing timing targets
    - Congestion_Explore / Congestion_NetDelay_high/medium/low — congestion-bound
    - Performance_Explore — general performance route
    - SSI_Explore — multi-SLR (cross-SLR) designs
    - AlternateRoutability — routability-congested

  Guidance: match the directive to the dominant bottleneck reported in the
  Dashboard (congestion level, critical-path type, WNS slack distribution).
  When unsure, OMIT and let default Explore run. Do NOT cycle random
  directives hoping to get lucky — each failed chain costs a full P&R run.
  Not every skill tool accepts both; pblock/opt/fanout/flatten take both,
  physopt/muxf_tree take route_directive only.""",

    LoopPhase.EVALUATE.value: f"""PHASE-GATED TOOL AVAILABILITY — CRITICAL:
   - EVALUATE: read-only tools to assess the WNS delta and decide next.

DECISION GUIDANCE (choose flow_control based on verdict):
   - verdict=IMPROVED: choose CONTINUE (keep refining same strategy) or SWITCH_STRATEGY.
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

    # 2. FORMAT_GUARD is now injected per-phase in inject_merged_dashboard
    #    (see context_snapshot.py), not here. The old once-per-iteration
    #    injection was lost after the first phase transition; per-phase
    #    injection ensures the guard is always present with phase-specific
    #    addenda. Reset the legacy flag so old runs don't carry stale state.
    state.model.format_guard_injected = False

    # 3. Inject handoff prompt
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

def get_optimal_context_size(iteration: int) -> int:
    """Optimal context size in tokens per iteration."""
    if iteration == 1: return 80000
    return 50000

def _compute_context_strategy(state) -> str:
    """Select context assembly strategy."""
    if state.iteration.current == 1: return "full"
    if state.iteration.global_no_improvement >= 2: return "minimal"
    return "normal"
