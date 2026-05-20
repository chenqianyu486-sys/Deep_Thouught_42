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


async def prepare_context_node(
    state: OptimizerState, deps: NodeDeps
) -> str:
    """Prepare LLM context for the upcoming tool loop.

    Actions:
        1. Compress context if needed
        2. Inject handoff prompt if not yet injected

    Note: Dashboard is injected per-LLM-call in the tool loop
    (via _inject_dashboard_at_end), not here.

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

    return NodeName.LLM_TOOL_LOOP
