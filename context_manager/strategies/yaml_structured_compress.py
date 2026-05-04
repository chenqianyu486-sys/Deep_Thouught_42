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


OUTDATED_TIMING_MIN_LENGTH = 500
OUTDATED_TIMING_ITERATION_GAP = 1
TIMING_REPORT_KEYWORDS = [
    'wns', 'tns', 'timing summary', 'slack', 'critical path',
    'report_timing', 'setup', 'hold', 'endpoint', 'startpoint',
    'fmax', 'clock period', 'data path delay'
]

# Tool result pruning: compress old tool messages to minimal markers
TOOL_RESULT_ITERATION_GAP = 2  # Compress tool results older than this many iterations
TOOL_RESULT_KEYWORDS = [
    'wns', 'tns', 'slack', 'failing_endpoint', 'route', 'phys_opt',
    'place_design', 'route_design', 'report_timing'
]

# Analysis tool results whose YAML summaries should NOT be compressed to markers.
# These are diagnostic/structural analysis outputs the model needs to reference for
# decision-making. Compressing them triggers re-call loops ("ghosting") because the
# marker looks like truncated output, causing the model to re-invoke the tool.
PROTECTED_ANALYSIS_TOOLS = frozenset({
    'rapidwright_analyze_pblock_region',
    'rapidwright_analyze_fabric_for_pblock',
    'rapidwright_analyze_net_detour',
    'rapidwright_smart_region_search',
    'rapidwright_read_checkpoint',
    'vivado_get_cached_high_fanout_nets',
    'vivado_get_raw_tool_output',
})


class YAMLStructuredCompressor(CompressionStrategy):
    """Compress messages using YAML format with FPGA-aware structure."""

    def get_name(self) -> str:
        return "yaml_structured"

    def _is_timing_report(self, msg: Message) -> bool:
        """Detect if a message is a timing report from a tool call result."""
        if msg.role != MessageRole.TOOL:
            return False
        content_lower = msg.content.lower()
        length_ok = len(msg.content) >= OUTDATED_TIMING_MIN_LENGTH
        keyword_ok = any(kw in content_lower for kw in TIMING_REPORT_KEYWORDS)
        return length_ok and keyword_ok

    def _get_outdated_iteration_boundary(self, current_iteration: int) -> int:
        """Return the iteration number below which timing reports are considered outdated."""
        return current_iteration - OUTDATED_TIMING_ITERATION_GAP

    def _is_failed_strategy_tool_result(self, msg: Message, failed_strategies: list) -> bool:
        """Check if a tool result corresponds to a known failed strategy.

        Tool messages from strategies that have been marked as failed are candidates
        for early compression, since the YAML blocked_strategies section provides
        the distilled summary.

        Supports both legacy list[str] and new list[dict] formats.
        """
        if not failed_strategies or msg.role != MessageRole.TOOL:
            return False

        name_lower = (msg.name or "").lower()
        for fs in failed_strategies:
            fs_name = fs["strategy"] if isinstance(fs, dict) else fs
            fs_lower = fs_name.lower()
            if fs_lower == "pblock" and ("pblock" in name_lower):
                return True
            if fs_lower == "physopt" and ("phys_opt" in name_lower):
                return True
            if fs_lower == "fanout" and ("fanout" in name_lower or "optimize_fanout" in name_lower):
                return True
            if fs_lower == "placeroute" and ("place_design" in name_lower or "route_design" in name_lower):
                return True
        return False

    def _compress_outdated_timing_reports(
        self, scored: List[tuple], current_iteration: int
    ) -> List[tuple]:
        """Replace outdated timing reports with brief markers to save tokens."""
        boundary = self._get_outdated_iteration_boundary(current_iteration)
        if boundary <= 0:
            return scored

        result = []
        replaced_count = 0
        saved_chars = 0

        for msg, importance, topic in scored:
            if self._is_timing_report(msg):
                msg_iter = msg.metadata.get('iteration', 0)
                if msg_iter < boundary:
                    marker = f"[Outdated timing report from iteration {msg_iter}]"
                    saved_chars += len(msg.content)
                    replaced_count += 1
                    compressed_msg = Message(
                        role=msg.role,
                        content=marker,
                        tool_call_id=msg.tool_call_id,
                        name=msg.name,
                        tool_calls=msg.tool_calls,
                        metadata=dict(msg.metadata)
                    )
                    result.append((compressed_msg, importance, topic))
                    continue
            result.append((msg, importance, topic))

        if replaced_count > 0:
            logger.info(
                "[OUTDATED_TIMING] Replaced %d outdated timing reports (saved ~%d chars)",
                replaced_count, saved_chars,
                extra={"replaced_count": replaced_count, "saved_chars": saved_chars,
                       "boundary_iteration": boundary, "trace_id": get_trace_id()}
            )
        return result

    def _compress_outdated_tool_results(
        self, scored: List[tuple], current_iteration: int,
        context: Optional[CompressionContext] = None
    ) -> List[tuple]:
        """Compress old tool result messages to minimal structured markers.

        Tool results older than TOOL_RESULT_ITERATION_GAP iterations are replaced
        with concise markers preserving only key metrics and tool name.
        Tool results from known failed strategies are compressed regardless of age.
        This prevents verbose YAML summary blocks from accumulating in history.
        """
        boundary = current_iteration - TOOL_RESULT_ITERATION_GAP
        if boundary <= 0 and not (context and context.failed_strategies):
            return scored

        result = []
        replaced_count = 0
        saved_chars = 0
        failed_strategies = context.failed_strategies if context else []

        for msg, importance, topic in scored:
            if msg.role == MessageRole.TOOL:
                msg_iter = msg.metadata.get('iteration', 0)
                is_tool_result = msg_iter > 0 and len(msg.content) > 100
                is_outdated = is_tool_result and msg_iter < boundary
                is_failed = is_tool_result and self._is_failed_strategy_tool_result(msg, failed_strategies)
                is_protected = msg.name in PROTECTED_ANALYSIS_TOOLS if msg.name else False
                if (is_outdated or is_failed) and not is_protected:
                    # Try to extract key metrics from YAML summary format
                    stripped = msg.content.strip()
                    lines = stripped.split('\n')
                    tool_name = ""
                    summary_text = ""
                    key_details = {}

                    for line in lines:
                        line_stripped = line.strip()
                        if line_stripped.startswith('tool:'):
                            tool_name = line_stripped.split(':', 1)[1].strip()
                        elif line_stripped.startswith('summary:'):
                            summary_text = line_stripped.split(':', 1)[1].strip().strip('"')
                        elif line_stripped.startswith('wns:'):
                            key_details['wns'] = line_stripped.split(':', 1)[1].strip()

                    if tool_name:
                        # Structured YAML summary format — keep only metadata
                        detail_str = f", wns={key_details['wns']}" if 'wns' in key_details else ""
                        marker = f"[SYSTEM COMPRESSED TOOL: {tool_name} (iteration {msg_iter}){detail_str}]"
                    else:
                        # Raw text format — try regex extraction for WNS
                        wns_match = re.search(r'WNS[=:]\s*([-\d.]+)', stripped, re.IGNORECASE)
                        wns_str = f", wns={wns_match.group(1)}" if wns_match else ""
                        # Truncate tool name from first line
                        first_line = lines[0][:80] if lines else ""
                        marker = f"[SYSTEM COMPRESSED: tool result (iteration {msg_iter}){wns_str}: {first_line}]"

                    saved_chars += len(msg.content)
                    replaced_count += 1
                    compressed_msg = Message(
                        role=msg.role,
                        content=marker,
                        tool_call_id=msg.tool_call_id,
                        name=msg.name,
                        tool_calls=msg.tool_calls,
                        metadata=dict(msg.metadata)
                    )
                    result.append((compressed_msg, importance, topic))
                    continue
            result.append((msg, importance, topic))

        if replaced_count > 0:
            self._tools_compressed_count = replaced_count
            logger.info(
                "[OUTDATED_TOOL_RESULT] Compressed %d old tool results (saved ~%d chars)",
                replaced_count, saved_chars,
                extra={"replaced_count": replaced_count, "saved_chars": saved_chars,
                       "boundary_iteration": boundary, "trace_id": get_trace_id()}
            )
        return result

    def __init__(
        self,
        token_budget: int = 80_000,
        preserve_turns: int = 20,
        min_importance_threshold: float = 0.3,
        max_chars_multiplier: float = 1.0,
        preserve_role_turns: int = 6
    ):
        """
        Args:
            token_budget: Maximum tokens for compressed output
            preserve_turns: Number of recent turns to preserve
            min_importance_threshold: Minimum importance score to keep a message
            max_chars_multiplier: Multiplier for adaptive max_chars (0.5=aggressive, 1.0=normal)
            preserve_role_turns: Number of recent messages to keep with original API roles
                (user/assistant/tool) instead of embedding in YAML. This preserves
                role-structured context for the LLM's API-level role processing.
        """
        self.token_budget = token_budget
        self.preserve_turns = preserve_turns
        self.min_importance_threshold = min_importance_threshold
        self.max_chars_multiplier = max_chars_multiplier
        self.preserve_role_turns = preserve_role_turns
        self._tools_compressed_count = 0  # Tracks tool results compressed to markers in current pass

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
        force_aggressive = getattr(context, 'force_aggressive', False)

        # Read all compression budget parameters from model_config when available.
        # model_config is always set in production (see _build_compression_context in dcp_optimizer.py).
        # The fallback path covers tests and direct API usage.
        if model_config:
            preserve_turns = model_config.preserve_turns
            min_importance_threshold = model_config.min_importance_threshold
            effective_token_budget = model_config.token_budget
            if force_aggressive:
                preserve_turns = model_config.preserve_turns_hard_limit
                min_importance_threshold = model_config.min_importance_threshold_hard_limit
        else:
            # Fallback defaults when no model_config is available
            preserve_turns = 20
            min_importance_threshold = 0.3
            effective_token_budget = 80_000
            if force_aggressive:
                preserve_turns = 5
                min_importance_threshold = 0.8

        # max_chars_multiplier is not a tier-specific budget parameter; stays as instance attribute
        max_chars_multiplier = getattr(self, 'max_chars_multiplier', 1.0)

        if force_aggressive:
            logger.info("[COMPRESS] Hard limit level compression: preserve_turns=%d, threshold=%.2f",
                       preserve_turns, min_importance_threshold)

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

        logger.info(
            "[COMPRESS] Starting compression: preserve_turns=%s, threshold=%s",
            preserve_turns, min_importance_threshold,
            extra={"preserve_turns": preserve_turns,
                  "threshold": min_importance_threshold, "trace_id": get_trace_id()}
        )

        # Reset compression tracking for this pass
        self._tools_compressed_count = 0

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
                wns_trend = 'degrading'
            elif context.current_wns > context.best_wns:
                wns_trend = 'improving'

        # Apply WNS trend weight to tool results
        scored = [
            (msg, ImportanceScorer.apply_wns_trend_weight(importance, wns_trend), topic)
            if msg.role == MessageRole.TOOL else (msg, importance, topic)
            for msg, importance, topic in scored
        ]

        # Replace outdated timing reports with brief markers to save tokens
        scored = self._compress_outdated_timing_reports(scored, current_iteration)

        # Compress old tool results (iterations older than gap) to minimal markers
        # Also compress tool results from known failed strategies regardless of age
        scored = self._compress_outdated_tool_results(scored, current_iteration, context)

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

        # === Split recent turns for API role preservation ===
        # Keep the last `preserve_role_turns` messages as their original API roles
        # (user/assistant/tool) so the LLM's API-level role processing works correctly.
        # Older messages go into the YAML conversation block.
        preserve_role_count = getattr(self, 'preserve_role_turns', 3)
        if preserve_role_count > 0 and len(selected) > preserve_role_count:
            role_preserved = selected[-preserve_role_count:]
            yaml_selected = selected[:-preserve_role_count]
        else:
            # Not enough messages to separate — all messages are recent
            role_preserved = []
            yaml_selected = selected

        # === Repair: ensure tool_calls / tool response pairs stay together ===
        # DeepSeek API requires every assistant message with tool_calls to be
        # immediately followed by tool messages matching each tool_call_id.
        # The role_preserved/yaml_selected split is count-based and can break
        # these pairs. Move orphaned tool responses into role_preserved.
        if role_preserved:
            # Collect tool_call_ids from assistant messages in role_preserved
            assistant_tc_ids = set()
            for msg, _, _ in role_preserved:
                if msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                    for tc in msg.tool_calls:
                        tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                        if tc_id:
                            assistant_tc_ids.add(tc_id)

            if assistant_tc_ids:
                # Collect tool_call_ids already in role_preserved
                present_tc_ids = set()
                for msg, _, _ in role_preserved:
                    if msg.role == MessageRole.TOOL and msg.tool_call_id:
                        present_tc_ids.add(msg.tool_call_id)

                # Find tool_call_ids referenced by assistant but missing responses
                missing_ids = assistant_tc_ids - present_tc_ids
                if missing_ids:
                    moved = []
                    still_yaml = []
                    for item in yaml_selected:
                        msg = item[0]
                        if msg.role == MessageRole.TOOL and msg.tool_call_id in missing_ids:
                            moved.append(item)
                            missing_ids.discard(msg.tool_call_id)
                        else:
                            still_yaml.append(item)
                    if moved:
                        yaml_selected = still_yaml
                        role_preserved = moved + role_preserved
                        role_preserved.sort(key=lambda x: x[0].metadata.get('index', 0))

        # Build YAML structure from yaml_selected (older messages) only
        yaml_data = self._build_yaml_structure(
            system_msgs,
            yaml_selected,
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

        # Keep system messages + YAML summary + role-preserved messages
        result = [msg for msg, _, _ in system_msgs]
        result.append(summary_msg)

        # Inject compression notification when tool results were compressed to markers
        # This prevents the model from misinterpreting [SYSTEM COMPRESSED TOOL:] markers
        # as truncated output and re-calling tools ("ghosting" behavior).
        if self._tools_compressed_count > 0:
            notification_msg = Message(
                role=MessageRole.USER,
                content=(
                    "SYSTEM NOTICE: Context compression was applied to conserve tokens. "
                    "Tool results marked with [SYSTEM COMPRESSED TOOL: ...] were intentionally "
                    "compressed by the system; their key metrics are preserved in the YAML "
                    "summary above. Do NOT re-call tools whose results show as compressed."
                ),
                metadata={
                    'protected': True,
                    'compression_notification': True,
                }
            )
            result.append(notification_msg)

        for msg, _, _ in role_preserved:
            result.append(msg)

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

    # 改进5: 最低评分阈值
    MIN_HEADER_SCORE: float = 2.0
    MIN_DATA_SCORE: float = 1.5
    MIN_OTHER_SCORE: float = 0.5

    # 改进2: WNS严重性预算上限
    WNS_SEVERITY_BUDGET_CAP: int = 15000

    # 改进3: 回退保护阈值
    FALLBACK_CHAR_RATIO: float = 0.2
    FALLBACK_CHAR_MIN: int = 200

    def _extract_wns_value(self, content: str) -> Optional[float]:
        match = re.search(r'WNS\s*[:=]\s*(-?[\d.]+)', content, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None
        return None

    def _detect_clock_domain_sections(self, lines: list) -> list:
        boundary_pattern = re.compile(r'^\s*(?:From\s+)?Clock\s*:?\s*', re.IGNORECASE)
        boundaries = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped and boundary_pattern.match(stripped):
                boundaries.append((i, stripped))

        if len(boundaries) <= 1:
            return [(0, len(lines), None, lines)]

        sections = []
        first_boundary_idx = boundaries[0][0]
        if first_boundary_idx > 0:
            sections.append((0, first_boundary_idx, '_preamble', lines[0:first_boundary_idx]))

        for j, (boundary_idx, label) in enumerate(boundaries):
            start = boundary_idx
            end = boundaries[j+1][0] if j+1 < len(boundaries) else len(lines)
            sections.append((start, end, label, lines[start:end]))

        return sections

    def _ensure_startpoint_endpoint_pairs(self, lines: list, selected: set) -> set:
        sp_pattern = re.compile(r'start\s*point\s*:', re.IGNORECASE)
        ep_pattern = re.compile(r'end\s*point\s*:', re.IGNORECASE)

        sp_indices = [i for i, line in enumerate(lines) if sp_pattern.search(line)]
        ep_indices = [i for i, line in enumerate(lines) if ep_pattern.search(line)]

        if not sp_indices or not ep_indices:
            return selected

        result = set(selected)
        for sp_idx in sp_indices:
            ep_idx = next((e for e in ep_indices if e > sp_idx), None)
            if ep_idx is None:
                break
            if sp_idx in result or ep_idx in result:
                result.add(sp_idx)
                result.add(ep_idx)

        return result

    def _select_timing_lines_in_domain(
        self, lines: list, available_budget: int
    ) -> set:
        header_entries = []
        data_entries = []
        other_entries = []

        for i, line in enumerate(lines):
            line_upper = line.upper()
            if any(k in line_upper for k in ['WNS', 'TNS', 'FMAX', 'CLOCK', '====', '----', 'TARGET', 'Failing']):
                header_entries.append((i, line))
            elif any(k in line.lower() for k in ['path', 'slack', 'endpoint', 'critical', 'delay']) or \
                 re.search(r'-?[\d.]+\s*ns', line.lower()):
                data_entries.append((i, line))
            else:
                other_entries.append((i, line))

        scored_header = [(idx, line, self._score_line(line, 'timing')) for idx, line in header_entries]
        scored_data = [(idx, line, self._score_line(line, 'timing')) for idx, line in data_entries]
        scored_other = [(idx, line, self._score_line(line, 'timing')) for idx, line in other_entries]

        scored_header.sort(key=lambda x: x[2], reverse=True)
        scored_data.sort(key=lambda x: x[2], reverse=True)
        scored_other.sort(key=lambda x: x[2], reverse=True)

        header_score_total = sum(s for _, _, s in scored_header)
        data_score_total = sum(s for _, _, s in scored_data)
        total_score = header_score_total + data_score_total + 1

        overhead = 50
        effective_budget = max(100, available_budget - overhead)
        header_budget = int(effective_budget * (header_score_total / total_score))
        data_budget = int(effective_budget * (data_score_total / total_score))

        selected_set = set()
        current_len = 0

        # Header选择（带最低阈值）
        for idx, line, score in scored_header:
            if score < self.MIN_HEADER_SCORE:
                continue
            line_len = len(line) + 1
            if current_len + line_len <= header_budget:
                selected_set.add(idx)
                current_len += line_len
        unused_header = max(0, header_budget - current_len)

        # Data选择（带最低阈值）
        current_len = 0
        for idx, line, score in scored_data:
            if score < self.MIN_DATA_SCORE:
                continue
            line_len = len(line) + 1
            if current_len + line_len <= data_budget:
                selected_set.add(idx)
                current_len += line_len
        unused_data = max(0, data_budget - current_len)

        # 未用完预算重新分配
        other_budget_extra = 0
        if unused_header > 0:
            other_budget_extra += unused_header * 2 // 3
        if unused_data > 0:
            other_budget_extra += unused_data * 2 // 3

        # Other选择（带最低阈值）
        remaining = effective_budget - sum(len(lines[i]) + 1 for i in selected_set)
        remaining += other_budget_extra
        for idx, line, score in scored_other:
            if score < self.MIN_OTHER_SCORE:
                continue
            line_len = len(line) + 1
            if remaining >= line_len:
                selected_set.add(idx)
                remaining -= line_len

        return selected_set

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
            # 改进2: 根据WNS违例严重程度动态调整预算
            wns_val = self._extract_wns_value(content)
            if wns_val is not None:
                abs_wns = abs(wns_val)
                if abs_wns < 0.3:
                    severity_mult = 1.0
                elif abs_wns < 1.0:
                    severity_mult = 1.3
                else:
                    severity_mult = 1.6
                adjusted = int(max_chars * severity_mult)
                max_chars = min(adjusted, self.WNS_SEVERITY_BUDGET_CAP)
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
        """时序报告专用截断，集成5项改进：
        1. 成对保留 Startpoint/Endpoint
        2. (动态预算在上游 _smart_truncate_content 中处理)
        3. 异常格式回退保护
        4. 时钟域感知的两级预算分配
        5. 最低评分阈值过滤
        """
        marker_overhead = 200
        available_budget = max(1, max_chars - marker_overhead)

        # 改进4: 时钟域检测
        sections = self._detect_clock_domain_sections(lines)

        if len(sections) == 1:
            # 单域路径（向后兼容）
            selected = self._select_timing_lines_in_domain(lines, available_budget)
        else:
            # 多域路径：按 domain 分配子预算
            section_weights = []
            section_wns = []
            for sec_start, sec_end, clock_label, sec_lines in sections:
                sec_text = '\n'.join(sec_lines)
                wns = self._extract_wns_value(sec_text)
                section_wns.append(wns)
                violation_factor = 1.0
                if wns is not None:
                    violation_factor = 1.0 + min(abs(wns), 5.0)
                section_weights.append(len(sec_lines) * violation_factor)

            total_weight = sum(section_weights) or 1

            # 为有违例的 section 保证最少 5 行
            MIN_LINES_PER_SECTION = 5
            EST_CHARS_PER_LINE = 60
            min_guarantee_total = 0
            section_minimums = []
            for i, wns in enumerate(section_wns):
                if wns is not None and abs(wns) > 0.001:
                    minimum = MIN_LINES_PER_SECTION * EST_CHARS_PER_LINE
                    section_minimums.append(minimum)
                    min_guarantee_total += minimum
                else:
                    section_minimums.append(0)

            allocatable = max(0, available_budget - min_guarantee_total)

            selected = set()
            for i, (sec_start, sec_end, clock_label, sec_lines) in enumerate(sections):
                if not sec_lines:
                    continue
                proportion = section_weights[i] / total_weight
                sub_budget = section_minimums[i] + int(allocatable * proportion)
                section_selected = self._select_timing_lines_in_domain(
                    sec_lines, sub_budget
                )
                # 将相对索引映射为绝对索引
                for rel_idx in section_selected:
                    selected.add(sec_start + rel_idx)

        # 改进1: Startpoint/Endpoint 成对绑定
        selected = self._ensure_startpoint_endpoint_pairs(lines, selected)

        # 改进3: 回退保护
        total_chars = sum(len(lines[i]) + 1 for i in selected)
        fallback_threshold = max(int(max_chars * self.FALLBACK_CHAR_RATIO), self.FALLBACK_CHAR_MIN)
        if total_chars < fallback_threshold:
            logger.warning(
                "[SMART_TRUNCATE] Timing report produced only %d chars (threshold=%d), "
                "using head+tail fallback", total_chars, fallback_threshold,
                extra={"trace_id": get_trace_id(), "total_chars": total_chars,
                       "fallback_threshold": fallback_threshold}
            )
            head_count = max(1, int(len(lines) * 0.7))
            tail_count = max(1, int(len(lines) * 0.3))
            fallback_indices = set(range(head_count)) | set(
                range(len(lines) - tail_count, len(lines))
            )
            result_lines = [lines[i] for i in sorted(fallback_indices)]
            total_preserved = len(fallback_indices)
            marker = f'\n[ABNORMAL TIMING REPORT - FALLBACK] ... [preserved {total_preserved}/{len(lines)} lines, timing report]'
        else:
            result_lines = [lines[i] for i in sorted(selected)]
            total_preserved = len(selected)
            marker = f'\n... [preserved {total_preserved}/{len(lines)} lines, timing report]'

        return '\n'.join(result_lines) + marker


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
