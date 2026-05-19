"""Prepare context node: compress and prepare LLM context.

Handles context compression, handoff injection, and snapshot building.

Reference: dcp_optimizer.py _compress_context() (L1662-1741),
_prepare_api_messages() (L1423-1477).
"""

from __future__ import annotations

import logging
import time

from ..state import OptimizerState
from ..deps import NodeDeps
from ..edges import NodeName
from ..pure.context_snapshot import build_context_snapshot, inject_context_snapshot
from ..pure.compress import compress_context

logger = logging.getLogger(__name__)


async def prepare_context_node(
    state: OptimizerState, deps: NodeDeps
) -> str:
    """Prepare LLM context for the upcoming tool loop.

    Actions:
        1. Compress context if needed
        2. Inject handoff prompt if not yet injected
        3. Build and inject context snapshot

    Returns:
        Next node name (deterministic: llm_tool_loop).
    """
    # 1. Compress context if memory_manager available
    if deps.memory_manager is not None:
        try:
            # Sync state to memory manager
            _sync_state_to_memory_manager(state, deps.memory_manager)

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

    # 3. Build context snapshot
    current_wns = state.timing.latest_wns
    best_wns = state.timing.best_wns if state.timing.best_wns > float('-inf') else None
    elapsed = 0.0
    remaining = state.control.wall_clock_timeout
    if state.control.start_time:
        elapsed = time.time() - state.control.start_time
        remaining = max(0, state.control.wall_clock_timeout - elapsed)

    snapshot = build_context_snapshot(
        current_wns=current_wns,
        best_wns=best_wns,
        best_wns_iteration=state.timing.best_wns_iteration,
        strategy_sequence=state.iteration.strategy_sequence,
        failed_strategy_names=[],  # Will be populated from compat
        global_no_improvement=state.iteration.global_no_improvement,
        cost_hard_limit=state.cost.cost_hard_limit,
        total_cost=state.cost.total_cost,
        elapsed_time=elapsed,
        remaining_time=remaining,
        iteration_narratives=state.iteration.narratives,
        tool_call_details=[],  # Will be populated from compat
    )

    # Inject snapshot into messages
    if deps.compat is not None:
        try:
            messages = deps.compat.messages
            inject_context_snapshot(messages, snapshot)
            logger.info("[prepare_context] Context snapshot injected")
        except Exception as e:
            logger.warning(f"[prepare_context] Snapshot injection failed: {e}")

    return NodeName.LLM_TOOL_LOOP


def _sync_state_to_memory_manager(state: OptimizerState, memory_manager) -> None:
    """Sync optimizer state to memory manager for accurate compression."""
    try:
        if hasattr(memory_manager, '_state'):
            mm_state = memory_manager._state
            mm_state.best_wns = state.timing.best_wns
            mm_state.latest_wns = state.timing.latest_wns
            mm_state.iteration = state.iteration.current
    except Exception:
        pass


def _estimate_tokens(deps: NodeDeps) -> int:
    """Estimate current token count from messages."""
    try:
        if deps.compat is not None and hasattr(deps.compat, '_context_estimator'):
            messages = deps.compat.messages
            return deps.compat._context_estimator.estimate_from_messages(messages)
    except Exception:
        pass
    return 0
