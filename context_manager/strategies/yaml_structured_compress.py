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
import re
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


def _detect_report_type(content: str) -> str:
    """Detect report type for smart truncation."""
    content_lower = content.lower()
    if 'timing' in content_lower and ('wns' in content_lower or 'slack' in content_lower):
        return 'timing'
    if 'utilization' in content_lower or 'resource' in content_lower:
        return 'utilization'
    if 'route' in content_lower and 'net' in content_lower:
        return 'routing'
    return 'general'


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

    @staticmethod
    def apply_iteration_weight(score: float, message: Message, current_iteration: int) -> float:
        """Apply iteration-based weight boost.

        Current iteration messages get 1.5x boost (most relevant).
        Previous iteration gets 1.2x boost.
        Older messages retain original score.
        """
        msg_iteration = message.metadata.get('iteration', 0)
        if msg_iteration == current_iteration:
            return score * 1.5
        elif msg_iteration == current_iteration - 1:
            return score * 1.2
        return score

    @staticmethod
    def apply_wns_trend_weight(score: float, wns_trend: str) -> float:
        """Apply WNS trend weight boost.

        Improving WNS (better timing): 1.2x boost for tool results.
        Degrading WNS (worse timing): 1.3x boost (need to remember failures).
        """
        if wns_trend == 'improving':
            return score * 1.2
        elif wns_trend == 'degrading':
            return score * 1.3
        return score


class YAMLStructuredCompressor(CompressionStrategy):
    """Compress messages using YAML format with FPGA-aware structure."""

    def get_name(self) -> str:
        return "yaml_structured"

    def __init__(
        self,
        token_budget: int = 80_000,
        preserve_turns: int = 20,
        min_importance_threshold: float = 0.3,
        max_chars_multiplier: float = 1.0
    ):
        """
        Args:
            token_budget: Maximum tokens for compressed output
            preserve_turns: Number of recent turns to preserve
            min_importance_threshold: Minimum importance score to keep a message
            max_chars_multiplier: Multiplier for adaptive max_chars (0.5=aggressive, 1.0=normal)
        """
        self.token_budget = token_budget
        self.preserve_turns = preserve_turns
        self.min_importance_threshold = min_importance_threshold
        self.max_chars_multiplier = max_chars_multiplier

    def compress(self, messages: List[Message], context: CompressionContext) -> List[Message]:
        """Compress messages into YAML format with FPGA design state.

        Compression intensity is determined by:
        - context.force_aggressive: aggressive vs normal mode
        - context.model_context_config: model-specific parameters
        - context.model_switch_detected: adjusts strategy on model tier switch

        Model-aware defaults:
        - False (normal): preserve_turns=20, min_importance_threshold=0.3
        - True (aggressive): preserve_turns=3, min_importance_threshold=0.8
        - With model_config: uses model-specific thresholds
        """
        if not messages:
            logger.debug("[COMPRESS] No messages to compress")
            return []

        # Get model-aware configuration
        model_config = getattr(context, 'model_context_config', None)
        model_switched = getattr(context, 'model_switch_detected', False)
        previous_tier = getattr(context, 'previous_model_tier', None)

        # Get tier-aware parameters from instance attributes (set by subclasses)
        # These can be overridden by model_config if available
        preserve_turns = getattr(self, 'preserve_turns', 25)
        min_importance_threshold = getattr(self, 'min_importance_threshold', 0.2)
        max_chars_multiplier = getattr(self, 'max_chars_multiplier', 1.0)

        # Adjust parameters based on model tier switch
        if model_switched and previous_tier and model_config:
            # Planner -> Worker: Worker needs more condensed summary
            if previous_tier == "planner" and model_config.model_tier == "worker":
                min_importance_threshold = max(min_importance_threshold, 0.35)
                preserve_turns = max(preserve_turns - 5, 20)
                logger.info("[COMPRESS] Model switch detected: planner->worker, adjusted threshold=%.2f, preserve_turns=%d",
                           min_importance_threshold, preserve_turns)
            # Worker -> Planner: Planner needs more comprehensive context
            elif previous_tier == "worker" and model_config.model_tier == "planner":
                min_importance_threshold = min(min_importance_threshold, 0.1)
                preserve_turns = min(preserve_turns + 10, 60)
                logger.info("[COMPRESS] Model switch detected: worker->planner, adjusted threshold=%.2f, preserve_turns=%d",
                           min_importance_threshold, preserve_turns)

        # Use model's token budget if available
        effective_token_budget = model_config.token_budget if model_config else self.token_budget

        logger.info(
            "[COMPRESS] Starting compression: preserve_turns=%s, threshold=%s",
            preserve_turns, min_importance_threshold,
            extra={"preserve_turns": preserve_turns,
                  "threshold": min_importance_threshold, "trace_id": get_trace_id()}
        )

        # Score and classify all messages
        scored = ImportanceScorer.classify_and_score(messages, context)

        # Apply iteration-aware weight boost
        current_iteration = getattr(context, 'iteration', 0)
        scored = [
            (msg, ImportanceScorer.apply_iteration_weight(importance, msg, current_iteration), topic)
            for msg, importance, topic in scored
        ]

        # Determine WNS trend for scoring
        wns_trend = 'stable'
        if context.best_wns is not None and context.current_wns is not None:
            if context.current_wns < context.best_wns:
                wns_trend = 'improving'
            elif context.current_wns > context.best_wns:
                wns_trend = 'degrading'

        # Apply WNS trend weight to tool results
        scored = [
            (msg, ImportanceScorer.apply_wns_trend_weight(importance, wns_trend), topic)
            if msg.role == MessageRole.TOOL else (msg, importance, topic)
            for msg, importance, topic in scored
        ]

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
        # Reserve budget for preserve_turns to ensure recent turns are actually preserved
        preserve_reserve = min(preserve_turns * 1500, 10000)  # ~1500 tokens per turn, max 10K
        available_budget = max(5000, effective_token_budget - system_tokens - 2000 - preserve_reserve)  # 2K buffer

        # Select important messages within budget (use dynamic threshold)
        selected = self._select_messages(conversation_msgs, available_budget, context, min_importance_threshold)

        # Also preserve recent turns regardless of importance (use dynamic preserve_turns)
        # Use O(1) set lookup instead of O(n) list comprehension
        selected_ids = set(id(m) for m, _, _ in selected)
        recent = self._get_recent_turns(conversation_msgs, preserve_turns)
        for msg, importance, topic in recent:
            if id(msg) not in selected_ids:
                selected.append((msg, importance, topic))
                selected_ids.add(id(msg))

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
        # Estimate tokens from yaml_str directly (summary_msg not yet created)
        yaml_tokens_est = len(yaml_str) // 4  # Approximate for logging
        logger.debug(
            "[COMPRESS_DUMP] YAML serialization: %d chars (~%d tokens) in %.2fms",
            len(yaml_str), yaml_tokens_est, _dump_duration_ms,
            extra={
                "compress_dump_duration_ms": round(_dump_duration_ms, 2),
                "compress_dump_output_length": len(yaml_str),
                "compress_dump_output_tokens_est": yaml_tokens_est,
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
        compression_label = 'yaml_structured'
        summary_msg = Message(
            role=MessageRole.SYSTEM,
            content=yaml_str,
            metadata={'protected': True, 'compression_type': compression_label}
        )

        # Keep system messages + summary
        result = [msg for msg, _, _ in system_msgs]
        result.append(summary_msg)

        # Calculate accurate token counts using tiktoken
        input_tokens = self._estimate_tokens(messages)
        output_tokens = self._estimate_tokens(result)

        logger.info(
            "[COMPRESS] Completed: %d messages, %d tokens -> %d messages, %d tokens (saved %d tokens, ratio: %.1f%%)",
            len(messages), input_tokens, len(result), output_tokens,
            input_tokens - output_tokens,
            (1 - output_tokens / input_tokens) * 100 if input_tokens > 0 else 0,
            extra={
                "compression_type": compression_label,
                "input_count": len(messages),
                "input_tokens": input_tokens,
                "output_count": len(result),
                "output_tokens": output_tokens,
                "tokens_saved": input_tokens - output_tokens,
                "compression_ratio": round((1 - output_tokens / input_tokens) * 100, 1) if input_tokens > 0 else 0,
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
        selected_keys: set[tuple[int, float, tuple]] = set()  # O(1) membership check

        # Reserve 60% of budget for first pass (high importance), 40% for second pass (medium importance)
        first_pass_budget = int(budget * 0.6)
        current_tokens = 0

        # First pass: add high-importance messages (up to 60% of budget)
        for msg, importance, topic in sorted_msgs:
            if importance < min_importance_threshold:
                continue

            msg_tokens = self._estimate_tokens([msg])
            if current_tokens + msg_tokens <= first_pass_budget:
                selected.append((msg, importance, topic))
                selected_keys.add((id(msg), importance, tuple(topic)))
                current_tokens += msg_tokens

        # Second pass: fill remaining budget with medium importance
        remaining_budget = budget - current_tokens
        current_tokens = 0

        for msg, importance, topic in sorted_msgs:
            key = (id(msg), importance, tuple(topic))
            if key in selected_keys:  # O(1) lookup
                continue
            if importance < min_importance_threshold * 0.5:
                continue

            msg_tokens = self._estimate_tokens([msg])
            if current_tokens + msg_tokens <= remaining_budget:
                selected.append((msg, importance, topic))
                selected_keys.add(key)
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

            # Preserve tool_call function names and arguments
            if msg.tool_calls:
                tool_calls_preserved = []
                for tc in msg.tool_calls:
                    if isinstance(tc, dict):
                        func = tc.get("function", tc)
                        tool_info = {"name": func.get("name", "unknown")}
                        # Preserve arguments for recent/important tool calls (limit to 5)
                        if len(tool_calls_preserved) < 5:
                            args = func.get("arguments", "")
                            if args and isinstance(args, str) and len(args) < 500:
                                tool_info["arguments"] = args[:500]
                            elif args and isinstance(args, dict):
                                tool_info["arguments"] = str(args)[:500]
                        tool_calls_preserved.append(tool_info)
                if tool_calls_preserved:
                    turn['tool_calls'] = tool_calls_preserved

            # Smart truncate long content (adaptive max_chars based on content type)
            content = self._smart_truncate_content(msg.content)
            turn['content'] = content

            conversation.append(turn)

        data['conversation'] = conversation

        return data

    def _get_adaptive_max_chars(self, content: str) -> int:
        """Calculate adaptive max_chars based on content type and structure.

        Strategy:
        - Timing reports: up to 8000 chars (critical path info is in middle)
        - Utilization reports: 4000 chars
        - Error messages: 6000 chars (preserve full context for debugging)
        - Short messages: 2000 chars (already short)
        - General: 4000 chars
        """
        content_len = len(content)
        content_lower = content.lower()

        # Short messages - don't need large budget
        if content_len < 1000:
            return min(content_len, 2000)

        # Timing reports - need more space for critical path details
        if ('timing' in content_lower and
            ('wns' in content_lower or 'slack' in content_lower or 'critical' in content_lower)):
            # Check if it has multi-line critical path info
            if content.count('\n') > 20:
                return 8000  # Long timing report - preserve middle sections
            return 5000

        # Utilization reports
        if 'utilization' in content_lower or 'resource' in content_lower:
            return 4000

        # Routing reports
        if 'route' in content_lower and ('net' in content_lower or 'wire' in content_lower):
            return 4000

        # Error messages - preserve full context for debugging
        if 'error' in content_lower or 'failed' in content_lower:
            return min(content_len, 6000)

        # Default - moderate truncation
        return min(content_len, 4000)

    def _score_line(self, line: str, report_type: str) -> float:
        """Score line importance based on content and report type."""
        score = 0.0
        line_upper = line.upper()
        line_lower = line.lower()

        if report_type == 'timing':
            # Timing-specific scoring (highest weight)
            if re.search(r'wns\s*[:=]\s*-?[\d.]+', line_lower):
                score += 5.0
            if re.search(r'tns\s*[:=]\s*-?[\d.]+', line_lower):
                score += 4.0
            if 'critical' in line_lower:
                score += 3.5
            if re.search(r'slack\s*[:=]\s*-[\d.]+', line_lower):
                score += 4.0  # Negative slack is critical
            if 'fmax' in line_lower:
                score += 3.0
            if 'endpoint' in line_lower:
                score += 2.5
            if 'path' in line_lower:
                score += 1.5
            if any(p in line_upper for p in ['WNS', 'TNS', 'FMAX', 'CRITICAL', 'VIOLATED']):
                score += 2.0
            # Data path lines
            if 'data path' in line_lower or 'clock path' in line_lower:
                score += 2.0
        elif report_type == 'utilization':
            if '%' in line and any(r in line_lower for r in ['lut', 'ff', 'bram', 'dsp']):
                score += 2.5
            if 'utilization' in line_lower or 'resource' in line_lower:
                score += 1.5
        else:
            # General scoring - keyword match
            for keyword in CRITICAL_KEYWORDS:
                if keyword.lower() in line_lower:
                    score += 1.0
                    break

        # Boost for complete logical units (key:value format)
        if ':' in line or '=>' in line or '=' in line:
            score *= 1.3

        # Boost lines with numbers (likely contain values)
        if re.search(r'[\d.]+', line):
            score *= 1.1

        # Boost header/summary lines (usually short)
        if len(line) < 80 and any(c.isupper() for c in line):
            score *= 1.2

        return score

    def _smart_truncate_content(self, content: str, max_chars: int = None) -> str:
        """Intelligent truncation with priority-based line selection.

        Args:
            content: Content to truncate
            max_chars: Maximum chars (if None, auto-calculated based on content type and multiplier)
        """
        if max_chars is None:
            base_max = self._get_adaptive_max_chars(content)
            max_chars = int(base_max * self.max_chars_multiplier)

        if len(content) <= max_chars:
            return content

        lines = content.split('\n')
        if len(lines) <= 3:
            return content[:max_chars] + '...'

        report_type = _detect_report_type(content)

        # Use specialized truncation for timing reports to preserve critical path data
        if report_type == 'timing':
            return self._smart_truncate_timing_report(content, lines, max_chars)

        # Score each line by importance
        scored_lines = []
        for i, line in enumerate(lines):
            score = self._score_line(line, report_type)
            scored_lines.append((i, score, line))

        # Sort by score descending (highest importance first)
        scored_lines.sort(key=lambda x: x[1], reverse=True)

        # Greedily select lines within budget
        selected_indices = []
        current_len = 0
        marker_len = 100  # Reserve space for marker

        for idx, score, line in scored_lines:
            line_len = len(line) + 1
            if current_len + line_len <= max_chars - marker_len:
                selected_indices.append(idx)
                current_len += line_len

        # Sort back to original order for readability
        selected_indices.sort()
        result_lines = [lines[i] for i in selected_indices]

        return '\n'.join(result_lines) + f'\n... [preserved {len(selected_indices)}/{len(lines)} lines, original {len(content)} chars]'

    def _smart_truncate_timing_report(self, content: str, lines: list, max_chars: int) -> str:
        """Specialized truncation for timing reports that preserves critical path data."""
        header_lines = []
        data_lines = []
        other_lines = []

        for i, line in enumerate(lines):
            line_upper = line.upper()
            # Header: contains WNS, TNS, Clock, summary keywords
            if any(k in line_upper for k in ['WNS', 'TNS', 'FMAX', 'CLOCK', '====', '----', 'TARGET', 'Failing']):
                header_lines.append((i, line))
            # Data path lines: contain path, delay, endpoint, slack, critical
            elif any(k in line.lower() for k in ['path', 'slack', 'endpoint', 'critical', 'delay']) or \
                 re.search(r'-?[\d.]+\s*ns', line.lower()):
                data_lines.append((i, line))
            else:
                other_lines.append((i, line))

        # Score each section
        header_score = sum(self._score_line(line, 'timing') for _, line in header_lines)
        data_score = sum(self._score_line(line, 'timing') for _, line in data_lines)
        total_score = header_score + data_score + 1

        # Allocate budget proportionally to score
        header_budget = int((max_chars - 200) * (header_score / total_score))
        data_budget = int((max_chars - 200) * (data_score / total_score))

        selected_indices = []

        # Select header lines within budget
        current_len = 0
        for idx, line in header_lines:
            if current_len + len(line) + 1 <= header_budget:
                selected_indices.append(idx)
                current_len += len(line) + 1

        # Select data lines within budget
        current_len = 0
        for idx, line in data_lines:
            if current_len + len(line) + 1 <= data_budget:
                selected_indices.append(idx)
                current_len += len(line) + 1

        # Fill remaining with other lines
        remaining = max_chars - 200 - sum(len(lines[i]) + 1 for i in selected_indices)
        for idx, line in other_lines:
            if remaining - len(line) - 1 >= 0:
                selected_indices.append(idx)
                remaining -= len(line) + 1

        selected_indices.sort()
        result_lines = [lines[i] for i in selected_indices]

        return '\n'.join(result_lines) + f'\n... [preserved {len(selected_indices)}/{len(lines)} lines, timing report]'


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
