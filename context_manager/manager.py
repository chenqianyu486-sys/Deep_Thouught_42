"""Memory Manager - Core orchestration for context management."""

from dataclasses import dataclass
from typing import Optional
from pathlib import Path
from .interfaces import (
    Message, MessageRole, CompressionContext, EventType, ContextEvent,
    MemoryLayer, ContextSnapshot, CompressionStrategy
)
from .estimator import ContextEstimator
from .events import EventBus
from .stores.memory_store import InMemoryContextStore
from .memory.working_memory import WorkingMemory, WorkingMemoryConfig
from .memory.historical_memory import HistoricalMemory, HistoricalMemoryConfig


@dataclass
class MemoryManagerConfig:
    """Configuration for MemoryManager."""
    working_config: WorkingMemoryConfig = None
    historical_config: HistoricalMemoryConfig = None
    soft_threshold: int = 80_000
    hard_limit: int = 150_000


class MemoryManager:
    """Central manager for all memory operations."""

    def __init__(
        self,
        config: Optional[MemoryManagerConfig] = None,
        compression_strategy: Optional[CompressionStrategy] = None,
        event_bus: Optional[EventBus] = None,
        persistence_path: Optional[Path] = None
    ):
        self._config = config or MemoryManagerConfig()
        self._compression_strategy = compression_strategy
        self._event_bus = event_bus or EventBus()
        self._persistence_path = persistence_path

        working_cfg = self._config.working_config or WorkingMemoryConfig()
        historical_cfg = self._config.historical_config or HistoricalMemoryConfig()

        self._working_store = InMemoryContextStore()
        self._estimator = ContextEstimator()

        self._working_memory = WorkingMemory(
            store=self._working_store,
            config=working_cfg,
            estimator=self._estimator,
            event_bus=self._event_bus
        )

        self._historical_memory = HistoricalMemory(
            config=historical_cfg,
            event_bus=self._event_bus
        )

        self._failed_strategies: list[str] = []
        self._tool_call_details: list[dict] = []
        self._best_wns: float = float('-inf')
        self._initial_wns: Optional[float] = None
        self._iteration: int = 0
        self._clock_period: Optional[float] = None

        self._event_bus.subscribe(EventType.MESSAGE_ADDED, self._check_compression)

    def add_message(self, role: MessageRole, content: str, metadata: dict = None) -> ContextSnapshot:
        """Add message to working memory."""
        message = Message(role=role, content=content, metadata=metadata or {})
        return self._working_memory.add_message(message)

    def get_context(self) -> list[Message]:
        """Get all messages from working memory."""
        return self._working_memory.get_all()

    def get_formatted_for_api(self, system_prompt: str = None) -> list[dict]:
        """Get context formatted for LLM API call."""
        return self._working_memory.get_context_for_model(system_prompt or "", include_history=True)

    def _check_compression(self, event: ContextEvent) -> None:
        """Check if compression is needed after message added."""
        if not self._compression_strategy:
            return

        messages = self._working_memory.get_all()
        tokens = self._estimator.estimate_from_messages(messages)

        context = CompressionContext(
            current_tokens=tokens,
            threshold_tokens=self._config.soft_threshold,
            hard_limit_tokens=self._config.hard_limit,
            failed_strategies=self._failed_strategies,
            tool_call_details=self._tool_call_details,
            best_wns=self._best_wns,
            initial_wns=self._initial_wns,
            current_wns=self._get_current_wns(),
            iteration=self._iteration,
            clock_period=self._clock_period
        )

        if tokens > self._config.hard_limit:
            self._compress("aggressive", context)
        elif tokens > self._config.soft_threshold:
            self._compress("smart", context)

    def _compress(self, compression_type: str, context: CompressionContext) -> None:
        """Execute compression."""
        messages = self._working_memory.get_all()
        compressed = self._compression_strategy.compress(messages, context)

        summary = self._create_summary_from_messages(messages)
        self._historical_memory.add(
            content=summary,
            importance=0.8,
            task_type="compression_snapshot"
        )

        self._working_memory.clear()
        for msg in compressed:
            self._working_store.add(msg)

        self._event_bus.emit(ContextEvent(
            event_type=EventType.CONTEXT_COMPRESSED,
            data={
                "compression_type": compression_type,
                "original_count": len(messages),
                "compressed_count": len(compressed)
            }
        ))

    def _create_summary_from_messages(self, messages: list[Message]) -> str:
        """Create summary from messages for archival."""
        lines = [f"[Archived {len(messages)} messages]"]
        for msg in messages[-10:]:
            lines.append(f"{msg.role.value}: {msg.content[:100]}...")
        return "\n".join(lines)

    def _get_current_wns(self) -> Optional[float]:
        for call in reversed(self._tool_call_details):
            if call.get("wns") is not None:
                return call["wns"]
        return None

    def retrieve_historical(self, query):
        """Retrieve from historical memory."""
        return self._historical_memory.retrieve(query)

    def snapshot(self) -> ContextSnapshot:
        """Create snapshot of current working memory."""
        messages = self._working_memory.get_all()
        return ContextSnapshot(
            layer=MemoryLayer.WORKING,
            message_count=len(messages),
            token_estimate=self._estimator.estimate_from_messages(messages)
        )

    @property
    def failed_strategies(self) -> list[str]:
        return self._failed_strategies

    @property
    def tool_call_details(self) -> list[dict]:
        return self._tool_call_details

    @property
    def best_wns(self) -> float:
        return self._best_wns

    @property
    def initial_wns(self) -> Optional[float]:
        return self._initial_wns

    @property
    def iteration(self) -> int:
        return self._iteration

    def record_failure(self, strategy: str) -> None:
        if strategy not in self._failed_strategies:
            self._failed_strategies.append(strategy)

    def add_tool_result(self, tool_name: str, result: str, wns: float = None, error: bool = False) -> None:
        self._tool_call_details.append({
            "tool_name": tool_name,
            "result": result[:500] if result else "",
            "wns": wns,
            "error": error,
            "iteration": self._iteration
        })
        if wns is not None and wns > self._best_wns:
            self._best_wns = wns

    def set_initial_wns(self, wns: float) -> None:
        if self._initial_wns is None:
            self._initial_wns = wns

    def set_clock_period(self, period: float) -> None:
        self._clock_period = period

    def advance_iteration(self) -> None:
        self._iteration += 1