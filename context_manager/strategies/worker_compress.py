"""
Worker Compressor - Context compression optimized for WORKER model calls.

Designed for fast execution tasks (get_utilization, get_timing, report_power, etc.):
- Fewer conversation turns (preserve_turns=20)
- Higher importance threshold (min_importance=0.35) to filter noise
- Smaller token budget (35K) for quick responses
- Aggressive truncation of tool results

This compressor prioritizes speed and efficiency over context completeness.
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


class WorkerCompressor(YAMLStructuredCompressor):
    """Compression strategy optimized for WORKER model calls.

    Worker tasks are routine operations (get_timing, get_utilization, etc.)
    that don't need extensive context. This compressor aggressively reduces
    context size for faster LLM responses.
    """

    def __init__(self):
        """Worker compressor with parameters optimized for quick execution."""
        super().__init__(
            token_budget=35_000,
            preserve_turns=20,
            min_importance_threshold=0.35,
            max_chars_multiplier=0.5
        )

    def get_name(self) -> str:
        return "worker"

    def _get_adaptive_max_chars(self, content: str) -> int:
        """Worker mode: aggressive truncation for speed.

        Worker tasks need minimal context for quick execution:
        - Timing reports: 4000 chars (summary only)
        - Utilization reports: 2000 chars (percentages only)
        - Error messages: 3000 chars (error type +简短 message)
        - General: 1500 chars
        """
        import re
        content_len = len(content)
        content_lower = content.lower()

        if content_len < 500:
            return min(content_len, 1000)

        if ('timing' in content_lower and
            ('wns' in content_lower or 'slack' in content_lower or 'critical' in content_lower)):
            if content.count('\n') > 20:
                return 4000
            return 3000

        if 'utilization' in content_lower or 'resource' in content_lower:
            return 2000

        if 'route' in content_lower and ('net' in content_lower or 'wire' in content_lower):
            return 2000

        if 'error' in content_lower or 'failed' in content_lower:
            return min(content_len, 3000)

        return min(content_len, 1500)
