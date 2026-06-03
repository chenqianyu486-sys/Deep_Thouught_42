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
from ..pure.constants import EXECUTE_STRATEGY_TOOL_MAP

logger = logging.getLogger(__name__)

# Build strategy-to-tool mapping text from shared constant (single source of truth).
_STRATEGY_MAPPING_LINES = "\n".join(
    f"      {k} → {v}" for k, v in sorted(EXECUTE_STRATEGY_TOOL_MAP.items())
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

STRICTLY FORBIDDEN:
  - XML/HTML tags in text
  - Omitting the report_step_state tool call entirely
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
