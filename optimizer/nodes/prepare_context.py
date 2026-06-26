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

logger = logging.getLogger(__name__)

# Build strategy-to-tool mapping text from shared constant (single source of truth).
_STRATEGY_MAPPING_LINES = "\n".join(
    f"      {k} → {v.execute_tool}" for k, v in sorted(_STRATEGY_MAP.items())
)

# FORMAT_GUARD: enforced on first iteration so the LLM reliably calls report_step_state.
# Matches the old optimize() flow (dcp_optimizer.py:5233-5255).
FORMAT_GUARD = f"""OUTPUT FORMAT — call `report_step_state` in every response as a structured tool call,
alongside any other tool calls (or alone if making none). Process control goes in the tool
call; analysis and reasoning go in text.

EXECUTE phase: tool filtering restricts available tools to the selected strategy.
Auto-chain actions handle post-skill workflow (checkpoint open, route, timing).
Strategy-to-tool mapping:
{_STRATEGY_MAPPING_LINES}

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

STRICTLY FORBIDDEN:
  - XML/HTML tags in text
  - Omitting the report_step_state tool call entirely
  - Skipping validation after design modifications
"""


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

    # 2. Inject FORMAT_GUARD (once, first iteration)
    if not state.model.format_guard_injected and deps.compat is not None:
        try:
            deps.compat.add_message("user", FORMAT_GUARD)
            state.model.format_guard_injected = True
            logger.info("[prepare_context] FORMAT_GUARD injected")
        except Exception as e:
            logger.warning(f"[prepare_context] FORMAT_GUARD injection failed: {e}")

    # 3. Inject handoff prompt
    if not state.model.iteration_handoff_injected and state.model.iteration_handoff_prompt:
        if deps.compat is not None:
            try:
                deps.compat.add_message("system", state.model.iteration_handoff_prompt)
                state.model.iteration_handoff_injected = True
                logger.info("[prepare_context] Handoff prompt injected")
            except Exception as e:
                logger.warning(f"[prepare_context] Handoff injection failed: {e}")

    # Inject cost budget awareness into LLM context
    cost_used = getattr(getattr(state, "cost", None), "total_spent", 0.0)
    cost_limit = getattr(getattr(state, "control", None), "cost_hard_limit", 5.0)
    if cost_limit > 0 and cost_used > 0:
        budget_pct = min(100 * cost_used / cost_limit, 100)
        budget_msg = (
            f"\n[BUDGET] Spent: ${cost_used:.4f} / ${cost_limit:.2f} limit "
            f"({budget_pct:.0f}% used). Prefer cheaper actions if budget is tight."
        )
        deps.compat.add_message("user", budget_msg)


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
