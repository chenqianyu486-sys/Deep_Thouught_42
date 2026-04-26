"""Core interfaces for Context Manager."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional
import time


class MemoryLayer(Enum):
    """Memory layer types for hierarchical storage."""
    WORKING = "working"
    HISTORICAL = "historical"
    ARCHIVE = "archive"


class MessageRole(Enum):
    """Message role types compatible with OpenAI API."""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class Message:
    """Standard message format compatible with OpenAI API."""
    role: MessageRole
    content: str
    name: Optional[str] = None
    tool_calls: Optional[list] = None
    tool_call_id: Optional[str] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class ContextSnapshot:
    """Point-in-time snapshot of context state."""
    timestamp: float = field(default_factory=time.time)
    layer: MemoryLayer = MemoryLayer.WORKING
    message_count: int = 0
    token_estimate: int = 0
    agent_id: Optional[str] = None
    parent_snapshot_id: Optional[str] = None


@dataclass
class ModelContextConfig:
    """Model-specific context configuration for adaptive compression.

    Attributes:
        model_tier: Model tier identifier - "flash" or "pro"
        max_context_tokens: Maximum context window for the model
        soft_threshold: Token count to trigger soft compression (80% of max)
        hard_limit: Token count that triggers aggressive compression (90% of max)
        token_budget: Target token budget for compressed output
        preserve_turns: Number of recent turns to preserve in normal compression
        preserve_turns_aggressive: Number of recent turns in aggressive mode
        min_importance_threshold: Minimum importance score to keep a message (normal)
        min_importance_threshold_aggressive: Minimum importance threshold (aggressive)
        history_retrieval_limit: Number of historical entries to retrieve
        history_retrieval_min_importance: Minimum importance for historical retrieval
    """
    model_tier: str
    max_context_tokens: int
    soft_threshold: int
    hard_limit: int
    token_budget: int
    preserve_turns: int = 20
    preserve_turns_aggressive: int = 3
    min_importance_threshold: float = 0.3
    min_importance_threshold_aggressive: float = 0.8
    history_retrieval_limit: int = 5
    history_retrieval_min_importance: float = 0.6


@dataclass
class CompressionContext:
    """Context information for compression decisions."""
    current_tokens: int = 0
    threshold_tokens: int = 80_000
    hard_limit_tokens: int = 150_000
    failed_strategies: list = field(default_factory=list)
    tool_call_details: list = field(default_factory=list)
    best_wns: float = 0.0
    initial_wns: Optional[float] = None
    current_wns: Optional[float] = None
    iteration: int = 0
    clock_period: Optional[float] = None
    agent_id: Optional[str] = None
    retrieved_history: list = field(default_factory=list)  # Historical entries for context
    # Model-aware compression fields
    model_context_config: Optional[ModelContextConfig] = None  # Model-specific configuration
    model_switch_detected: bool = False  # True if model tier switched since last compression
    previous_model_tier: Optional[str] = None  # Previous model tier ("flash" or "pro")


class EventType(Enum):
    """Types of context events."""
    MESSAGE_ADDED = "message_added"
    CONTEXT_COMPRESSED = "context_compressed"
    LAYER_PROMOTED = "layer_promoted"
    # LAYER_ARCHIVED - removed: defined but never emitted
    BRANCH_CREATED = "branch_created"
    BRANCH_MERGED = "branch_merged"
    # SNAPSHOT_CREATED - removed: defined but never emitted
    # RETRIEVAL_COMPLETED - removed: defined but never emitted


@dataclass
class ContextEvent:
    """Event emitted on context changes."""
    event_type: EventType
    timestamp: float = field(default_factory=time.time)
    data: dict = field(default_factory=dict)
    source_agent_id: Optional[str] = None


EventHook = Callable[[ContextEvent], None]


@dataclass
class RetrievalQuery:
    """Query for retrieving from historical memory."""
    text: Optional[str] = None
    task_type: Optional[str] = None
    time_range: Optional[tuple[float, float]] = None
    min_importance: float = 0.0
    limit: int = 10
    agent_id: Optional[str] = None


@dataclass
class HistoricalEntry:
    """Single entry in historical memory."""
    id: str
    timestamp: float
    content: str
    importance_score: float = 0.5
    task_type: Optional[str] = None
    agent_id: Optional[str] = None
    tags: list = field(default_factory=list)
    embedding: Optional[list] = None


class ContextStore(ABC):
    """Abstract interface for message storage."""

    @abstractmethod
    def add(self, message: Message) -> ContextSnapshot:
        """Add a message to the store."""
        pass

    @abstractmethod
    def get(self, index: int) -> Optional[Message]:
        """Get message at index."""
        pass

    @abstractmethod
    def get_range(self, start: int, end: int) -> list[Message]:
        """Get messages in range [start, end)."""
        pass

    @abstractmethod
    def get_recent(self, n: int) -> list[Message]:
        """Get last n messages."""
        pass

    @abstractmethod
    def get_all(self) -> list[Message]:
        """Get all messages."""
        pass

    @abstractmethod
    def search(self, predicate: Callable[[Message], bool]) -> list[Message]:
        """Search messages by predicate."""
        pass

    @abstractmethod
    def snapshot(self) -> ContextSnapshot:
        """Create a snapshot of current state."""
        pass

    def restore(self, snapshot: ContextSnapshot) -> None:
        """Restore state from snapshot.

        Default implementation is a no-op. Subclasses may override
        with actual restoration logic if snapshot includes message data.
        """
        pass

    @abstractmethod
    def clear(self) -> None:
        """Clear all messages."""
        pass

    @abstractmethod
    def __len__(self) -> int:
        """Return number of messages."""
        pass


class CompressionStrategy(ABC):
    """Abstract interface for compression strategies."""

    @abstractmethod
    def compress(
        self,
        messages: list[Message],
        context: CompressionContext
    ) -> list[Message]:
        """Compress message list based on strategy."""
        pass

    @abstractmethod
    def get_name(self) -> str:
        """Return strategy name for logging."""
        pass
