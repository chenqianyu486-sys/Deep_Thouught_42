"""LLM call cost accumulation shared across all phases.

Previously only the EXECUTE phase tracked token/cost usage, so
``state.cost.total_cost`` silently missed ANALYZE / SELECT_STRATEGY /
EVALUATE calls (run-20260710_190708: reported $0.1262 vs. real $0.3488,
a 2.77x underestimate that polluted the budget guard and the remaining-budget
figure shown to the LLM). Centralizing the accumulator here lets every phase
call it right after its LLM response.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def track_llm_call_cost(state, response) -> None:
    """Accumulate one LLM call's token usage and cost into ``state.cost``.

    Safe to call with ``response=None`` or a response missing ``usage``;
    such calls are no-ops. Mirrors the OpenRouter usage fields exposed on the
    OpenAI SDK response object (prompt/completion/total tokens, reasoning
    tokens, cost, and prompt-cache read/creation tokens).
    """
    try:
        usage = getattr(response, "usage", None)
        if not usage:
            return
        prompt = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0
        raw_total = getattr(usage, "total_tokens", 0) or 0
        total = raw_total if raw_total > 0 else (prompt + completion)
        cost = float(getattr(usage, "cost", 0.0) or 0.0)
        state.cost.total_prompt_tokens += prompt
        state.cost.total_completion_tokens += completion
        state.cost.total_tokens += total
        state.cost.total_cost += cost
        completion_details = getattr(usage, "completion_tokens_details", None)
        if completion_details:
            reasoning = getattr(completion_details, "reasoning_tokens", 0) or 0
            state.cost.total_reasoning_tokens += reasoning
        # Prompt caching metrics (OpenRouter returns these in usage object
        # when cache: {prompt: true} is set in extra_body)
        cache_read = getattr(usage, "cache_read_input_tokens", None)
        if cache_read is not None:
            state.cost.total_cache_read_tokens += int(cache_read)
        cache_create = getattr(usage, "cache_creation_input_tokens", None)
        if cache_create is not None:
            state.cost.total_cache_creation_tokens += int(cache_create)
    except Exception as e:
        logger.debug(f"[cost] Cost tracking failed: {e}")
