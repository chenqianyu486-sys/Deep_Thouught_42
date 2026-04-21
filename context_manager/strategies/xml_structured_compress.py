"""XML Structured Compressor for context management."""

from dataclasses import dataclass
from typing import Optional
import xml.etree.ElementTree as ET

from .base import BaseCompressionStrategy
from ..interfaces import Message, CompressionContext, MessageRole
from ..estimator import ContextEstimator


@dataclass
class XMLCompressionConfig:
    """Configuration for XML structured compression."""
    token_budget: int = 80000
    preserve_turns: int = 20
    min_importance_threshold: float = 0.3


class ImportanceScorer:
    """Importance scoring based on message type and content."""

    TAG_WEIGHTS = {
        'tool_result': 3.0,
        'tool_call': 2.5,
        'error': 2.5,
        'assistant_message': 1.5,
        'user_message': 1.0,
    }

    KEYWORD_BOOST = {
        'WNS': 2.0, 'critical': 1.8, 'timing': 1.5,
        'error': 2.0, 'failed': 1.8, 'success': 1.3,
    }

    def score(self, message: Message) -> float:
        """Calculate importance score for a message."""
        base_weight = self.TAG_WEIGHTS.get(
            message.metadata.get('type', ''), 1.0
        )
        boost = 1.0
        content_lower = message.content.lower()
        for keyword, weight in self.KEYWORD_BOOST.items():
            if keyword.lower() in content_lower:
                boost = max(boost, weight)
        return base_weight * boost


class TopicClassifier:
    """Topic classification based on keywords."""

    TOPICS = {
        'placement': ['place_design', 'pblock', 'slice', 'DSP', 'FF', 'BRAM'],
        'routing': ['route_design', 'net', 'fanout', 'timing'],
        'timing': ['report_timing', 'WNS', 'TNS', 'critical_path', 'slack'],
    }

    def classify(self, content: str) -> dict[str, float]:
        """Classify content into topics based on keywords."""
        content_lower = content.lower()
        return {
            topic: sum(1 for kw in kws if kw.lower() in content_lower)
            for topic, kws in self.TOPICS.items()
        }


def messages_to_xml(messages: list[Message], context: Optional[CompressionContext] = None) -> str:
    """Convert message list to XML string with FPGA design context.

    Args:
        messages: List of messages to convert
        context: Compression context containing design state (timing, WNS, etc.)
    """
    root = ET.Element('context')
    meta = ET.SubElement(root, 'meta')
    ET.SubElement(meta, 'token_count').text = str(
        ContextEstimator.estimate_tokens(''.join(m.content for m in messages))
    )
    ET.SubElement(meta, 'message_count').text = str(len(messages))

    # Add FPGA design state section if context is available
    if context is not None:
        design_state = ET.SubElement(root, 'design_state')

        # Timing information
        timing_elem = ET.SubElement(design_state, 'timing')
        if context.clock_period is not None:
            timing_elem.set('clock_period', f"{context.clock_period:.3f}")
        if context.initial_wns is not None:
            timing_elem.set('initial_wns', f"{context.initial_wns:.3f}")
        if context.best_wns is not None:
            timing_elem.set('best_wns', f"{context.best_wns:.3f}")
        if context.current_wns is not None:
            timing_elem.set('current_wns', f"{context.current_wns:.3f}")

        # Iteration info
        ET.SubElement(design_state, 'iteration').text = str(context.iteration)

        # Failed strategies (blocked approaches)
        if context.failed_strategies:
            blocked = ET.SubElement(design_state, 'blocked_strategies')
            for strategy in context.failed_strategies[-10:]:
                s = ET.SubElement(blocked, 'strategy')
                s.text = strategy
                s.set('status', 'failed')

    sys_msgs = [m for m in messages if m.role == MessageRole.SYSTEM]
    sys_section = ET.SubElement(root, 'system_messages')
    for msg in sys_msgs:
        sm = ET.SubElement(sys_section, 'system_message')
        sc = ET.SubElement(sm, 'content')
        sc.text = msg.content
        ET.SubElement(sm, 'priority').set('value', 'critical')

    # Initialize topic classifier for use in conversation loop
    topic_classifier = TopicClassifier()

    conv = ET.SubElement(root, 'conversation')
    for i, msg in enumerate(messages):
        if msg.role == MessageRole.SYSTEM:
            continue
        turn = ET.SubElement(conv, 'turn')
        turn.set('index', str(i))
        turn.set('role', msg.role.value)
        turn.set('type', msg.metadata.get('type', 'unknown'))

        # Compute and expose topic classification
        topics = topic_classifier.classify(msg.content)
        active_topics = [(t, s) for t, s in topics.items() if s > 0]
        if active_topics:
            topics_elem = ET.SubElement(turn, 'topics')
            for topic_name, topic_score in active_topics:
                t = ET.SubElement(topics_elem, 'topic')
                t.set('name', topic_name)
                t.set('score', str(topic_score))

        content = ET.SubElement(turn, 'content')
        content.text = msg.content

        imp = ET.SubElement(turn, 'importance')
        imp.text = str(ImportanceScorer().score(msg))

    return ET.tostring(root, encoding='unicode')


class XMLStructuredCompressor(BaseCompressionStrategy):
    """
    XML Structured Compressor.

    Core Principles:
    1. System Message is completely preserved and not involved in compression
    2. Only non-system messages are compressed
    3. Compressed context is also in XML format
    """

    def __init__(self, config: Optional[XMLCompressionConfig] = None):
        self.config = config or XMLCompressionConfig()
        self.scorer = ImportanceScorer()
        self.topic_classifier = TopicClassifier()
        self.estimator = ContextEstimator()

    def compress(self, messages: list[Message], context: CompressionContext) -> list[Message]:
        """Compress messages while preserving system messages."""
        if not messages:
            return []

        system_messages = [m for m in messages if m.role == MessageRole.SYSTEM]
        other_messages = [m for m in messages if m.role != MessageRole.SYSTEM]

        result = list(system_messages)

        if other_messages:
            # Calculate system tokens before passing non-system messages
            system_tokens = sum(
                self.estimator.estimate_tokens(m.content)
                for m in system_messages
            )
            compressed = self._compress_others(other_messages, context, system_tokens)
            result.extend(compressed)

        return result

    def _compress_others(self, messages: list[Message], context: CompressionContext,
                         system_tokens: int = 0) -> list[Message]:
        """Compress non-system messages."""
        scored = [(self.scorer.score(m), m) for m in messages]
        scored.sort(key=lambda x: -x[0])

        selected = []
        total_tokens = 0
        budget = self.config.token_budget - system_tokens

        for score, msg in scored:
            msg_tokens = self.estimator.estimate_tokens(msg.content)
            is_recent = len(messages) - messages.index(msg) <= self.config.preserve_turns

            if total_tokens + msg_tokens <= budget:
                selected.append(msg)
                total_tokens += msg_tokens
            elif is_recent and score >= self.config.min_importance_threshold:
                selected.append(msg)
                total_tokens += msg_tokens

        selected.sort(key=lambda m: messages.index(m))
        return selected

    def get_name(self) -> str:
        return "xml_structured"
