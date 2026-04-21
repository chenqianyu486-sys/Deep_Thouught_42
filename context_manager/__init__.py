"""Context Manager Module - Modular context management for AI agents."""

from .interfaces import (
    Message,
    MessageRole,
    ContextStore,
    CompressionStrategy,
    CompressionContext,
    EventType,
    ContextEvent,
    MemoryLayer,
    ContextSnapshot,
    RetrievalQuery,
    HistoricalEntry,
)
from .estimator import ContextEstimator
from .events import EventBus, EventHook
from .manager import MemoryManager
from .agent_context import AgentContext, AgentContextManager
from .strategies.smart_compress import SmartCompressionStrategy
from .strategies.aggressive_compress import AggressiveCompressionStrategy
from .formatters import XMLMessageFormatter, XMLResponseParser

__all__ = [
    "Message",
    "MessageRole",
    "ContextStore",
    "CompressionStrategy",
    "CompressionContext",
    "EventType",
    "ContextEvent",
    "MemoryLayer",
    "ContextSnapshot",
    "RetrievalQuery",
    "HistoricalEntry",
    "ContextEstimator",
    "EventBus",
    "EventHook",
    "MemoryManager",
    "AgentContext",
    "AgentContextManager",
    "SmartCompressionStrategy",
    "AggressiveCompressionStrategy",
    "XMLMessageFormatter",
    "XMLResponseParser",
]