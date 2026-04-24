"""
YAML Structured Compressor - YAML-based context compression for FPGA optimization.

Uses LightYAML (zero-dependency) for serialization, preserving:
- FPGA design state (timing, WNS, clock period)
- Failed strategies tracking
- Topic classification per message
- Importance scoring

Design: Zero external dependencies, uses only Python standard library.
"""

import logging
import os
import time
from typing import List, Optional
from collections import OrderedDict

try:
    from ..logging_config import get_trace_id
except ImportError:
    def get_trace_id():
        return ""

from ..interfaces import Message, MessageRole, CompressionContext, CompressionStrategy
from ..lightyaml import LightYAML
from ..estimator import ContextEstimator

logger = logging.getLogger(__name__)

# Optional YAML roundtrip validation (off by default, controlled by env var)
_YAML_ROUNDTRIP_VALIDATE = os.environ.get('YAML_ROUNDTRIP_VALIDATE', '0') == '1'

# Tag-based importance weights
TAG_WEIGHTS = {
    'tool_result': 3.0,
    'tool_call': 2.5,
    'error': 2.5,
    'assistant_message': 1.5,
    'user_message': 1.0,
}

# Keyword-based importance boost
KEYWORD_BOOST = {
    'WNS': 2.0,
    'critical': 1.8,
    'timing': 1.5,
    'error': 2.0,
    'failed': 1.8,
    'success': 1.3,
    'violation': 2.0,
    'slack': 1.5,
    'constraint': 1.5,
    'optimize': 1.3,
    'place': 1.2,
    'route': 1.2,
}

# Topic patterns for classification
TOPIC_PATTERNS = {
    'placement': ['place', 'placement', 'floorplan', 'pblock', 'slicel', 'slicex', 'dsp', 'bram'],
    'routing': ['route', 'routing', 'net', 'wire', 'connection'],
    'timing': ['timing', 'wns', 'tns', 'slack', 'critical', 'delay', 'period', 'frequency', 'fmax'],
    'utilization': ['utilization', 'utilization', 'lut', 'ff', ' Registers', ' Resources'],
    'power': ['power', 'thermal'],
    'synthesis': ['synthesize', 'synthesis', 'hdl', 'verilog', 'vhdl', 'rtl'],
}

# Keywords that indicate high-value content in timing reports
CRITICAL_KEYWORDS = [
    'wns', 'tns', 'failing', 'clock', 'period', 'target', 'frequency',
    'slack', 'critical', 'path', 'delay', 'setup', 'hold',
    'worst', 'max', 'min', 'total', 'endpoint'
]

# Lines that should be preserved from timing reports (header/summary lines)
PRESERVE_LINE_PATTERNS = [
    'WNS', 'TNS', 'Failing', 'Clock', 'Target', 'Frequency',
    '====', '----', '****', 'delay'
]


class ImportanceScorer:
    """Calculate importance score for a message."""

    @staticmethod
    def score(message: Message) -> float:
        """Calculate importance score (0.0 - 1.0+)."""
        content = message.content.lower()
        role = message.role

        # Base score from role
        score = TAG_WEIGHTS.get(role.value, 1.0)

        # Keyword boost
        for keyword, boost in KEYWORD_BOOST.items():
            if keyword.lower() in content:
                score *= boost

        # Tool result mentions WNS directly get extra boost
        if 'wns' in content and ('-' in content or 'negative' in content):
            score *= 1.5

        return score

    @classmethod
    def classify_and_score(cls, messages: List[Message], context: CompressionContext) -> List[tuple]:
        """Classify messages by topic and calculate importance scores."""
        scored_messages = []

        for i, msg in enumerate(messages):
            importance = cls.score(msg)
            topic = cls.classify(msg.content)
            scored_messages.append((msg, importance, topic))

        return scored_messages

    @staticmethod
    def classify(content: str) -> List[str]:
        """Classify message content into topics."""
        content_lower = content.lower()
        topics = []

        for topic, patterns in TOPIC_PATTERNS.items():
            for pattern in patterns:
                if pattern.lower() in content_lower:
                    topics.append(topic)
                    break

        return topics if topics else ['general']


class YAMLStructuredCompressor(CompressionStrategy):
    """Compress messages using YAML format with FPGA-aware structure."""

    def get_name(self) -> str:
        return "yaml_structured"

    def __init__(
        self,
        token_budget: int = 80_000,
        preserve_turns: int = 20,
        min_importance_threshold: float = 0.3
    ):
        """
        Args:
            token_budget: Maximum tokens for compressed output
            preserve_turns: Number of recent turns to preserve
            min_importance_threshold: Minimum importance score to keep a message
        """
        self.token_budget = token_budget
        self.preserve_turns = preserve_turns
        self.min_importance_threshold = min_importance_threshold

    def compress(self, messages: List[Message], context: CompressionContext) -> List[Message]:
        """Compress messages into YAML format with FPGA design state.

        Compression intensity is determined by context.force_aggressive:
        - False (normal): preserve_turns=20, min_importance_threshold=0.3
        - True (aggressive): preserve_turns=3, min_importance_threshold=0.8
        """
        if not messages:
            logger.debug("[COMPRESS] No messages to compress")
            return []

        # Determine compression intensity based on force_aggressive flag
        is_aggressive = getattr(context, 'force_aggressive', False)
        if is_aggressive:
            preserve_turns = 3
            min_importance_threshold = 0.8
        else:
            preserve_turns = 20
            min_importance_threshold = 0.3

        logger.info(
            "[COMPRESS] Starting compression: is_aggressive=%s, preserve_turns=%s, threshold=%s",
            is_aggressive, preserve_turns, min_importance_threshold,
            extra={"is_aggressive": is_aggressive, "preserve_turns": preserve_turns,
                  "threshold": min_importance_threshold, "trace_id": get_trace_id()}
        )

        # Score and classify all messages
        scored = ImportanceScorer.classify_and_score(messages, context)

        # Separate system messages (protected) from conversation
        system_msgs = []
        conversation_msgs = []

        for msg, importance, topic in scored:
            if msg.role == MessageRole.SYSTEM or msg.metadata.get('protected'):
                system_msgs.append((msg, importance, topic))
            else:
                conversation_msgs.append((msg, importance, topic))

        # Calculate available budget (system messages take priority)
        system_tokens = self._estimate_tokens([m for m, _, _ in system_msgs])
        available_budget = max(5000, self.token_budget - system_tokens - 2000)  # 2K buffer

        # Select important messages within budget (use dynamic threshold)
        selected = self._select_messages(conversation_msgs, available_budget, context, min_importance_threshold)

        # Also preserve recent turns regardless of importance (use dynamic preserve_turns)
        recent = self._get_recent_turns(conversation_msgs, preserve_turns)
        for msg, importance, topic in recent:
            if msg not in [m for m, _, _ in selected]:
                selected.append((msg, importance, topic))

        # Sort by iteration/position to maintain order
        selected.sort(key=lambda x: x[0].metadata.get('index', 0))

        # Build YAML structure
        yaml_data = self._build_yaml_structure(
            system_msgs,
            selected,
            context
        )

        trace_id = get_trace_id()

        # Instrumented YAML dump with timing and error logging
        _dump_start = time.perf_counter()
        try:
            yaml_str = LightYAML.dump(yaml_data, trace_id=trace_id)
        except Exception:
            logger.exception(
                "[COMPRESS_DUMP_ERROR] YAML dump failed during compression",
                extra={
                    "trace_id": trace_id,
                    "yaml_data_keys": list(yaml_data.keys()) if isinstance(yaml_data, dict) else None,
                }
            )
            raise
        _dump_duration_ms = (time.perf_counter() - _dump_start) * 1000
        logger.debug(
            "[COMPRESS_DUMP] YAML serialization: %d chars in %.2fms",
            len(yaml_str), _dump_duration_ms,
            extra={
                "compress_dump_duration_ms": round(_dump_duration_ms, 2),
                "compress_dump_output_length": len(yaml_str),
                "trace_id": trace_id,
            }
        )

        # Optional roundtrip validation (diagnostic-only, never blocks pipeline)
        if _YAML_ROUNDTRIP_VALIDATE:
            try:
                LightYAML.load(yaml_str, trace_id=trace_id)
                logger.debug(
                    "[COMPRESS_ROUNDTRIP] YAML roundtrip validation OK",
                    extra={"trace_id": trace_id, "compress_roundtrip_valid": True}
                )
            except Exception as e:
                logger.error(
                    "[COMPRESS_ROUNDTRIP_FAIL] YAML roundtrip validation failed: %s",
                    e,
                    extra={"trace_id": trace_id, "compress_roundtrip_valid": False}
                )

        # Create summary message
        compression_label = 'yaml_structured_aggressive' if is_aggressive else 'yaml_structured'
        summary_msg = Message(
            role=MessageRole.SYSTEM,
            content=yaml_str,
            metadata={'protected': True, 'compression_type': compression_label}
        )

        # Keep system messages + summary
        result = [msg for msg, _, _ in system_msgs]
        result.append(summary_msg)

        logger.info(
            "[COMPRESS] Completed: %d messages -> %d messages (YAML: %d chars, ~%d tokens)",
            len(messages), len(result), len(yaml_str), len(yaml_str) // 4,
            extra={
                "compression_type": compression_label,
                "input_count": len(messages),
                "output_count": len(result),
                "compress_output_chars": len(yaml_str),
                "compress_output_estimated_tokens": len(yaml_str) // 4,
                "trace_id": trace_id,
            }
        )
        return result

    def _select_messages(
        self,
        messages: List[tuple],
        budget: int,
        context: CompressionContext,
        min_importance_threshold: float
    ) -> List[tuple]:
        """Select messages within token budget, prioritizing high-importance."""
        # Sort by importance descending
        sorted_msgs = sorted(messages, key=lambda x: x[1], reverse=True)

        selected = []
        current_tokens = 0

        # First pass: add high-importance messages
        for msg, importance, topic in sorted_msgs:
            if importance < min_importance_threshold:
                continue

            msg_tokens = self._estimate_tokens([msg])
            if current_tokens + msg_tokens <= budget:
                selected.append((msg, importance, topic))
                current_tokens += msg_tokens

        # Second pass: fill remaining budget with medium importance
        for msg, importance, topic in sorted_msgs:
            if (msg, importance, topic) in selected:
                continue
            if importance < min_importance_threshold * 0.5:
                continue

            msg_tokens = self._estimate_tokens([msg])
            if current_tokens + msg_tokens <= budget:
                selected.append((msg, importance, topic))
                current_tokens += msg_tokens

        return selected

    def _get_recent_turns(self, messages: List[tuple], count: int) -> List[tuple]:
        """Get most recent N turns."""
        # Sort by timestamp in metadata (fallback to position in list)
        def get_sort_key(x):
            msg = x[0]
            ts = msg.metadata.get('timestamp')
            if ts is not None:
                return ts
            return -1  # Items without timestamp go first

        sorted_msgs = sorted(messages, key=get_sort_key, reverse=True)
        return sorted_msgs[:count]

    def _estimate_tokens(self, messages: list[Message]) -> int:
        """Token estimation using tiktoken."""
        return ContextEstimator.estimate_from_messages(messages)

    def _build_yaml_structure(
        self,
        system_msgs: List[tuple],
        selected_msgs: List[tuple],
        context: CompressionContext
    ) -> OrderedDict:
        """Build the YAML data structure."""
        data = OrderedDict()

        # Meta section
        data['meta'] = OrderedDict([
            ('compression_type', 'yaml_structured'),
            ('message_count', len(selected_msgs)),
            ('total_input_messages', len(system_msgs) + len(selected_msgs)),
        ])

        # Design state section
        design_state = OrderedDict()

        timing = OrderedDict()
        if context.clock_period is not None:
            timing['clock_period'] = context.clock_period
        if context.initial_wns is not None:
            timing['initial_wns'] = context.initial_wns
        if context.best_wns is not None:
            timing['best_wns'] = context.best_wns
        if context.current_wns is not None:
            timing['current_wns'] = context.current_wns

        design_state['timing'] = timing
        design_state['iteration'] = context.iteration

        # Failed/blocked strategies
        if context.failed_strategies:
            design_state['blocked_strategies'] = context.failed_strategies[-10:]

        data['design_state'] = design_state

        # System messages (preserved)
        system_content = []
        for msg, _, _ in system_msgs:
            system_content.append(msg.content[:500] if len(msg.content) > 500 else msg.content)
        if system_content:
            data['system_messages'] = system_content

        # Historical memory entries (retrieved past optimization context)
        retrieved_history = getattr(context, 'retrieved_history', None)
        if retrieved_history:
            history_section = []
            for entry in retrieved_history[:5]:  # Limit to top 5 entries
                history_item = OrderedDict()
                history_item['timestamp'] = entry.timestamp
                history_item['importance'] = entry.importance_score
                if entry.task_type:
                    history_item['task_type'] = entry.task_type
                # Truncate content for history entries
                history_item['content'] = entry.content[:300] + '...' if len(entry.content) > 300 else entry.content
                history_section.append(history_item)
            data['historical_summary'] = history_section

        # Conversation section
        conversation = []
        for msg, importance, topics in selected_msgs:
            turn = OrderedDict()
            turn['role'] = msg.role.value
            turn['importance'] = round(importance, 2)
            if topics:
                turn['topics'] = topics

            # Smart truncate long content
            content = self._smart_truncate_content(msg.content, max_chars=2000)
            turn['content'] = content

            conversation.append(turn)

        data['conversation'] = conversation

        return data

    def _smart_truncate_content(self, content: str, max_chars: int = 2000) -> str:
        """
        Intelligently truncate content while preserving critical information.

        Strategy:
        1. If content fits within max_chars, return as-is
        2. Split content into lines
        3. Identify critical lines (contain keywords like WNS, TNS, critical path)
        4. Preserve: first N lines (summary) + critical lines + last M lines (details)
        5. Add truncation marker with position info

        Args:
            content: The content string to truncate
            max_chars: Maximum characters to preserve

        Returns:
            Truncated content with smart preservation of critical information
        """
        if len(content) <= max_chars:
            return content

        lines = content.split('\n')
        if len(lines) <= 3:
            return content[:max_chars] + '...'

        # Reserve space for truncation marker
        marker_max_len = 80
        available_chars = max_chars - marker_max_len

        # Identify critical lines (contain important keywords)
        critical_line_indices = set()
        for i, line in enumerate(lines):
            line_upper = line.upper()
            # Check for preserve patterns (headers, separators)
            for pattern in PRESERVE_LINE_PATTERNS:
                if pattern.upper() in line_upper:
                    critical_line_indices.add(i)
                    break
            # Check for critical keywords
            line_lower = line.lower()
            for keyword in CRITICAL_KEYWORDS:
                if keyword.lower() in line_lower:
                    critical_line_indices.add(i)
                    break

        # Build smart truncation
        # Strategy: Keep first lines (summary) + critical lines + last lines (details)
        preserved_indices = set()

        # Always keep first 10 lines (summary/header)
        for i in range(min(10, len(lines))):
            preserved_indices.add(i)

        # Always keep last 5 lines (final details)
        for i in range(max(0, len(lines) - 5), len(lines)):
            preserved_indices.add(i)

        # Add critical lines
        critical_count = 0
        max_critical = 15  # Limit critical lines to prevent overflow
        for idx in sorted(critical_line_indices):
            if idx not in preserved_indices and critical_count < max_critical:
                preserved_indices.add(idx)
                critical_count += 1

        # Sort and build result
        preserved_indices = sorted(preserved_indices)
        result_lines = [lines[i] for i in preserved_indices]

        # Check if we need to truncate to fit
        result = '\n'.join(result_lines)
        if len(result) <= available_chars:
            # We can fit all preserved lines
            truncation_pos = sum(len(lines[i]) + 1 for i in preserved_indices if i < len(lines))
            marker = f'\n... [truncated after line {preserved_indices[-1] if preserved_indices else 0}, original {len(content)} chars]'
        else:
            # Need to further truncate - use simpler strategy
            # Keep: header lines + middle portion + footer
            header_count = 8
            footer_count = 3
            middle_lines = available_chars // 80  # Rough estimate of lines that fit

            result_lines = lines[:header_count]
            result_lines.append(f'... [{len(lines) - header_count - footer_count} intermediate lines truncated] ...')
            result_lines.extend(lines[-footer_count:])

            truncation_pos = sum(len(l) + 1 for l in lines[:header_count]) + len(lines[-footer_count:])
            marker = f'\n... [truncated, original {len(content)} chars in {len(lines)} lines]'

        # Final check and trim
        result = '\n'.join(result_lines)
        if len(result) + len(marker) > max_chars:
            # Emergency truncation: just take from front
            result = content[:available_chars]
            marker = f'\n... [truncated at position {available_chars}, original {len(content)} chars]'

        result += marker
        return result


def messages_to_yaml(messages: List[Message], context: CompressionContext) -> str:
    """
    Convert messages to YAML string with FPGA design context.

    This is a standalone function for debugging/testing.
    Production use should go through YAMLStructuredCompressor.compress().
    """
    compressor = YAMLStructuredCompressor()
    compressed = compressor.compress(messages, context)

    # Find the YAML summary message
    for msg in compressed:
        if msg.metadata.get('compression_type') == 'yaml_structured':
            return msg.content

    return ""
