"""
Planner Compressor - Context compression optimized for PLANNER model calls.

Designed for complex reasoning tasks (place_design, phys_opt, route_design, etc.):
- Preserves more conversation turns (preserve_turns=60)
- Lower importance threshold (min_importance=0.1) to retain more context
- Larger token budget (100K) for detailed design state
- Maintains comprehensive WNS history and failed strategies

This compressor prioritizes context completeness for strategic decision-making.
"""

import logging
from typing import List

try:
    from ..logging_config import get_trace_id
except ImportError:
    def get_trace_id():
        return ""

from .yaml_structured_compress import (
    YAMLStructuredCompressor,
)
from ..interfaces import Message, CompressionContext

logger = logging.getLogger(__name__)


class PlannerCompressor(YAMLStructuredCompressor):
    """Compression strategy optimized for PLANNER model calls.

    Planner tasks involve complex reasoning (placement optimization, routing strategy,
    timing debug). This compressor preserves more context to support strategic decisions.
    """

    def __init__(self):
        """Planner compressor with parameters optimized for strategic reasoning."""
        super().__init__(
            token_budget=100_000,
            preserve_turns=60,
            min_importance_threshold=0.1,
            max_chars_multiplier=1.0
        )

    def get_name(self) -> str:
        return "planner"

    def _get_adaptive_max_chars(self, content: str) -> int:
        """Planner mode: larger budgets for detailed analysis.

        Planner tasks need more context for strategic decisions:
        - Timing reports: 10000 chars (full critical path analysis)
        - Utilization reports: 5000 chars
        - Error messages: 8000 chars (preserve full debugging context)
        - General: 5000 chars
        """
        content_len = len(content)
        content_lower = content.lower()

        if content_len < 1000:
            return min(content_len, 2500)

        if ('timing' in content_lower and
            ('wns' in content_lower or 'slack' in content_lower or 'critical' in content_lower)):
            if content.count('\n') > 20:
                return 12000
            return 10000

        if 'utilization' in content_lower or 'resource' in content_lower:
            return 5000

        if 'route' in content_lower and ('net' in content_lower or 'wire' in content_lower):
            return 5000

        if 'error' in content_lower or 'failed' in content_lower:
            return min(content_len, 8000)

        return min(content_len, 5000)
