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

logger = logging.getLogger(__name__)

# FORMAT_GUARD: enforced on first iteration so the LLM reliably calls report_step_state.
# Matches the old optimize() flow (dcp_optimizer.py:5233-5255).
FORMAT_GUARD = """CRITICAL OUTPUT FORMAT - MUST FOLLOW:
Every response MUST call the `report_step_state` tool (in your structured function/tool
calls, NOT in the text body). This tool carries process control directives:
step_id, result_status, flow_control.

Call report_step_state ALONGSIDE any other tool calls you make. If you are making no
other tool calls, call report_step_state alone.

The report_step_state tool takes these parameters:
  - step_id (integer): incrementing per message in current strategy
  - result_status (string): SUCCESS | PARTIAL | FAIL
  - flow_control (string): ANALYZE_DONE | EXEC_DONE | CONTINUE | NEXT_ITERATION | SWITCH_STRATEGY | DONE | RETRY | ROLLBACK | EXHAUSTED
  - strategy_phase (string, optional): ANALYZE | SELECT_STRATEGY | EXECUTE_STRATEGY | EVALUATE
  - strategy_name (string, optional): PBLOCK | PhysOpt | Fanout | PinSwap | LUTCascade |
    CellReplication | CongestionSpreading | RegisterRetiming | NetSwap

Strategy Lifecycle (4-Phase Cycle):
  Phase 1 ANALYZE: Gather timing data, identify dominant obstacles via
    report_timing_summary, extract_critical_path_cells, analyze_congestion, etc.
    Report strategy_phase=ANALYZE.
  Phase 2 SELECT_STRATEGY: Based on analysis findings, choose a specific strategy.
    Report strategy_phase=SELECT_STRATEGY and strategy_name=<chosen strategy>.
  Phase 3 EXECUTE_STRATEGY: Execute the chosen strategy via tool calls.
    Report strategy_phase=EXECUTE_STRATEGY.
  Phase 4 EVALUATE: After execution completes, check WNS delta and determine if the
    strategy helped. Report strategy_phase=EVALUATE with evaluation (IMPROVED,
    REGRESSION, or UNCHANGED).

Your text response MUST contain your analysis (hypothesis, strategy_rationale,
observed signals) as free-form chain-of-thought reasoning.
Process control goes in the report_step_state tool call, analysis goes in text.

STRICTLY FORBIDDEN:
  - XML/HTML tags in text
  - Omitting the report_step_state tool call entirely

Maintain this output format throughout the entire conversation.
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

    return NodeName.LLM_TOOL_LOOP
