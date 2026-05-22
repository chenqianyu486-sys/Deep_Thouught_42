"""Context compression helper for v2 nodes.

Wraps MemoryManager._compress() with proper CompressionContext construction
and threshold checks. Reused by prepare_context_node and llm_tool_loop_node.

Reference: dcp_optimizer.py _compress_context() (L1662-1741),
_build_compression_context() (L1479-1510).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from config_loader import get_model_config_loader
from context_manager.interfaces import CompressionContext, ModelContextConfig

if TYPE_CHECKING:
    from ..state import OptimizerState
    from ..deps import NodeDeps

logger = logging.getLogger(__name__)

# Lazy-loaded model configs (module-level singleton)
_worker_config: ModelContextConfig | None = None
_planner_config: ModelContextConfig | None = None


def _get_model_config(tier: str) -> ModelContextConfig:
    """Load ModelContextConfig for the given tier (worker/planner)."""
    global _worker_config, _planner_config

    if tier == "planner":
        if _planner_config is None:
            loader = get_model_config_loader()
            data = loader.get_planner_config()
            _planner_config = ModelContextConfig(
                model_tier=data.model_tier,
                max_context_tokens=data.max_tokens,
                soft_threshold=data.soft_threshold,
                hard_limit=data.hard_limit,
                token_budget=data.token_budget,
                preserve_turns=data.preserve_turns,
                preserve_turns_aggressive=data.preserve_turns_aggressive,
                min_importance_threshold=data.min_importance_threshold,
                min_importance_threshold_aggressive=data.min_importance_threshold_aggressive,
                preserve_turns_hard_limit=data.preserve_turns_hard_limit,
                min_importance_threshold_hard_limit=data.min_importance_threshold_hard_limit,
                history_retrieval_limit=data.history_retrieval_limit,
                history_retrieval_min_importance=data.history_retrieval_min_importance,
            )
        return _planner_config
    else:
        if _worker_config is None:
            loader = get_model_config_loader()
            data = loader.get_worker_config()
            _worker_config = ModelContextConfig(
                model_tier=data.model_tier,
                max_context_tokens=data.max_tokens,
                soft_threshold=data.soft_threshold,
                hard_limit=data.hard_limit,
                token_budget=data.token_budget,
                preserve_turns=data.preserve_turns,
                preserve_turns_aggressive=data.preserve_turns_aggressive,
                min_importance_threshold=data.min_importance_threshold,
                min_importance_threshold_aggressive=data.min_importance_threshold_aggressive,
                preserve_turns_hard_limit=data.preserve_turns_hard_limit,
                min_importance_threshold_hard_limit=data.min_importance_threshold_hard_limit,
                history_retrieval_limit=data.history_retrieval_limit,
                history_retrieval_min_importance=data.history_retrieval_min_importance,
            )
        return _worker_config


def _estimate_tokens_from_messages(messages) -> int:
    """Rough token estimate from messages (char-based, ~4 chars/token).

    Accepts both Message objects and plain dicts.
    """
    total_chars = 0
    for m in messages:
        # Support both Message objects and dicts
        content = m.content if hasattr(m, 'content') else m.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        # tool_calls contribute tokens too
        tc = getattr(m, 'tool_calls', None) or (m.get("tool_calls") if isinstance(m, dict) else None)
        if tc:
            total_chars += len(str(tc)) // 2  # JSON is compact
    return total_chars // 4


def _infer_model_tier(model_name: str | None, state_model=None) -> str:
    """Infer model tier from model name.

    Priority: exact match against state_model.planner_model first,
    then fallback to heuristic string matching.
    """
    if not model_name:
        return "worker"
    # Exact match against configured planner model (most reliable)
    if state_model and hasattr(state_model, 'planner_model'):
        if model_name == state_model.planner_model:
            return "planner"
    # Heuristic fallback for models not in state_model
    name = model_name.lower()
    if any(k in name for k in ("pro", "plus")):
        return "planner"
    return "worker"


def compress_context(state: OptimizerState, deps: NodeDeps) -> bool:
    """Check token thresholds and trigger compression if needed.

    Builds a CompressionContext from current state and calls
    MemoryManager._compress() synchronously.

    Returns True if compression was performed.
    """
    if deps.memory_manager is None or deps.compat is None:
        return False

    try:
        # 1. Get messages and estimate tokens
        #    Use MemoryManager.get_context() (Message objects) instead of
        #    compat.messages (dict copies) to avoid creating temporary dicts.
        messages = deps.memory_manager.get_context()
        current_tokens = _estimate_tokens_from_messages(messages)

        # 2. Determine model tier and config
        model_tier = _infer_model_tier(state.model.current_model, state.model)
        config = _get_model_config(model_tier)

        # 3. Check thresholds
        if current_tokens <= config.soft_threshold:
            logger.debug(
                f"[compress] Skipped: {current_tokens:,} tokens < soft_threshold {config.soft_threshold:,}"
            )
            return False

        force_aggressive = current_tokens > config.hard_limit
        if force_aggressive:
            logger.warning(
                f"[compress] Hard limit: {current_tokens:,} > {config.hard_limit:,} ({model_tier})"
            )

        # 4. Build CompressionContext
        #    Read from OptimizerState (canonical) instead of MemoryManager (shadow),
        #    because V2 never populates MemoryManager's internal fields via compat layer.
        #    Only failed_strategies still comes from compat (populated by record_failure()).
        failed_strategies = deps.compat.failed_strategies if deps.compat else []
        context = CompressionContext(
            current_tokens=current_tokens,
            threshold_tokens=config.soft_threshold,
            hard_limit_tokens=config.hard_limit,
            failed_strategies=failed_strategies,
            tool_call_details=[],  # Compressor never reads this field
            best_wns=state.timing.best_wns if state.timing.best_wns > float('-inf') else None,
            initial_wns=state.timing.initial_wns,
            current_wns=state.timing.latest_wns,
            iteration=state.iteration.current,
            clock_period=state.timing.clock_period,
            model_context_config=config,
            force_aggressive=force_aggressive,
        )

        # 5. Call _compress synchronously (NOT async)
        deps.memory_manager._compress("yaml_structured", context, model_tier=model_tier)
        logger.info(f"[compress] Compressed ({model_tier}, aggressive={force_aggressive}, {current_tokens:,} tokens)")
        return True

    except Exception as e:
        logger.warning(f"[compress] Failed: {e}")
        return False
