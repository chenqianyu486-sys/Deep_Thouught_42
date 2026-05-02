"""Memory Manager - Core orchestration for context management."""

import logging
import time
from dataclasses import dataclass
from typing import Optional, Literal
from pathlib import Path
from .interfaces import (
    Message, MessageRole, CompressionContext, EventType, ContextEvent,
    MemoryLayer, ContextSnapshot
)
from .estimator import ContextEstimator
from .events import EventBus
from .stores.memory_store import InMemoryContextStore
from .memory.working_memory import WorkingMemory, WorkingMemoryConfig
from .memory.historical_memory import HistoricalMemory, HistoricalMemoryConfig
from .logging_config import get_trace_id

logger = logging.getLogger(__name__)


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
        event_bus: Optional[EventBus] = None,
        persistence_path: Optional[Path] = None
    ):
        self._config = config or MemoryManagerConfig()
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

        # NOTE: Automatic compression subscription DISABLED.
        # Compression is now triggered exclusively by DCPOptimizer._compress_context().
        # Benefits:
        # 1. Eliminates implicit behavior - compression timing is fully controllable
        # 2. Avoids conflicts with DCPOptimizer's explicit compression calls
        # 3. Dual-trigger mechanism (auto + explicit) was confirmed to cause duplicate compressions
        # To re-enable automatic compression, uncomment the following:
        # self._event_bus.subscribe(
        #     EventType.MESSAGE_ADDED,
        #     lambda event: self._check_compression(event)
        # )

        self._failed_strategies: list[str] = []
        self._tool_call_details: list[dict] = []
        self._best_wns: float = float('-inf')
        self._initial_wns: Optional[float] = None
        self._iteration: int = 0
        self._clock_period: Optional[float] = None
        self._compressing: bool = False  # Explicitly initialized

    def add_message(self, role: MessageRole, content: str, metadata: dict = None) -> ContextSnapshot:
        """Add message to working memory with auto-injected metadata."""
        metadata = metadata or {}
        # Auto-inject iteration for iteration-aware compression
        if 'iteration' not in metadata and self._iteration > 0:
            metadata['iteration'] = self._iteration
        # Auto-inject timestamp for _get_recent_turns() ordering
        if 'timestamp' not in metadata:
            metadata['timestamp'] = time.time()
        # Auto-inject index for stable message ordering after selection
        if 'index' not in metadata:
            metadata['index'] = len(self._working_store)
        tool_call_id = metadata.pop("tool_call_id", None)
        name = metadata.pop("name", None)
        tool_calls = metadata.pop("tool_calls", None)
        message = Message(
            role=role,
            content=content,
            tool_call_id=tool_call_id,
            name=name,
            tool_calls=tool_calls,
            metadata=metadata
        )
        return self._working_memory.add_message(message)

    def get_context(self) -> list[Message]:
        """Get all messages from working memory."""
        return self._working_memory.get_all()

    def get_formatted_for_api(self, system_prompt: str = None) -> list[dict]:
        """Get context formatted for LLM API call."""
        return self._working_memory.get_context_for_model(system_prompt or "", include_history=True)

    def replace_all_messages(self, messages: list[Message]) -> None:
        """
        Replace all working memory messages in a single batch operation.

        This is more efficient than clearing and re-adding messages one by one,
        as it avoids triggering N MESSAGE_ADDED events.

        Args:
            messages: List of Message objects to set as the new working memory
        """
        self._working_store.clear()
        for msg in messages:
            self._working_store.add(msg)

    def _compress(self, compression_type: Literal["yaml_structured"], context: CompressionContext, model_tier: str = None) -> None:
        """Execute compression with system message protection.

        NOTE: Only yaml_structured is used. Aggressive/light compression levels
        are handled internally by YAMLStructuredCompressor based on context.force_aggressive.

        Args:
            compression_type: Compression algorithm type (yaml_structured only)
            context: CompressionContext with metadata for compression decisions
            model_tier: Optional model tier ("planner" or "worker") to select strategy
        """
        if getattr(self, '_compressing', False):
            logger.debug("Compression already in progress, skipping")
            return
        self._compressing = True

        # Select compression strategy based on model tier
        if model_tier == "planner":
            from .strategies.planner_compress import PlannerCompressor
            strategy = PlannerCompressor()
            strategy_name = "planner"
        elif model_tier == "worker":
            from .strategies.worker_compress import WorkerCompressor
            strategy = WorkerCompressor()
            strategy_name = "worker"
        else:
            from .strategies.yaml_structured_compress import YAMLStructuredCompressor
            strategy = YAMLStructuredCompressor()
            strategy_name = compression_type

        logger.info(
            "[COMPRESSION] Starting compression: type=%s, model_tier=%s, force_aggressive=%s",
            compression_type,
            model_tier,
            getattr(context, 'force_aggressive', False),
            extra={
                "compression_type": compression_type,
                "compression_model_tier": model_tier,
                "compression_force_aggressive": getattr(context, 'force_aggressive', False),
                "trace_id": get_trace_id(),
            }
        )
        try:

            all_messages = self._working_memory.get_all()

            # Separate system messages (never compressed) from others
            system_messages = [
                m for m in all_messages
                if m.role == MessageRole.SYSTEM or
                (m.metadata and m.metadata.get('protected'))
            ]
            non_system_messages = [
                m for m in all_messages
                if m.role != MessageRole.SYSTEM and
                not (m.metadata and m.metadata.get('protected'))
            ]

            # Only compress non-system messages
            if non_system_messages:
                compressed_non_system = strategy.compress(non_system_messages, context)
            else:
                compressed_non_system = []

            # Rebuild: system messages + compressed non-system messages
            compressed = system_messages + compressed_non_system

            summary = self._create_summary_from_messages(all_messages)
            self._historical_memory.add(
                content=summary,
                importance=0.8,
                task_type="compression_snapshot"
            )

            self._working_memory.clear()
            for msg in compressed:
                self._working_store.add(msg)

            original_tokens = self._estimator.estimate_from_messages(all_messages)
            compressed_tokens = self._estimator.estimate_from_messages(compressed)

            self._event_bus.emit(ContextEvent(
                event_type=EventType.CONTEXT_COMPRESSED,
                data={
                    "compression_type": strategy_name,
                    "original_count": len(all_messages),
                    "compressed_count": len(compressed),
                    "original_tokens": original_tokens,
                    "compressed_tokens": compressed_tokens,
                    "compression_ratio_token": round(
                        (original_tokens - compressed_tokens) / max(original_tokens, 1), 4
                    ),
                    "force_aggressive": getattr(context, 'force_aggressive', False),
                    "iteration": context.iteration if hasattr(context, 'iteration') else None,
                }
            ))
            compression_ratio = (len(all_messages) - len(compressed)) / len(all_messages) if len(all_messages) > 0 else 0
            logger.info(
                "[COMPRESSION] Completed: %d messages -> %d messages (%d removed)",
                len(all_messages),
                len(compressed),
                len(all_messages) - len(compressed),
                extra={
                    "compression_original_count": len(all_messages),
                    "compression_compressed_count": len(compressed),
                    "compression_removed_count": len(all_messages) - len(compressed),
                    "compression_ratio": round(compression_ratio, 4),
                    "trace_id": get_trace_id(),
                }
            )
        finally:
            self._compressing = False

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
            logger.warning(
                "[FAILED_STRATEGY] Recorded: %s (total failed: %d)",
                strategy, len(self._failed_strategies),
                extra={"strategy": strategy, "failed_count": len(self._failed_strategies),
                       "trace_id": get_trace_id()}
            )

    def add_tool_result(self, tool_name: str, result: str, wns: float = None, error: bool = False, extra_fields: dict = None) -> None:
        entry = {
            "tool_name": tool_name,
            "result": result if result else "",
            "wns": wns,
            "error": error,
            "iteration": self._iteration
        }
        if extra_fields:
            entry.update(extra_fields)
        self._tool_call_details.append(entry)
        # Defensive WNS sanity check: reject values that are clearly parsing errors
        if wns is not None and wns > self._best_wns:
            if abs(wns) > 1000:
                logger.warning(
                    "[TOOL_RESULT] WNS %.3f rejected (abs > 1000, likely parsing error)", wns,
                    extra={"tool_name": tool_name, "wns": wns, "iteration": self._iteration,
                           "trace_id": get_trace_id()}
                )
            elif self._best_wns > -1.0:
                # best_wns already close to convergence — trust the improvement
                self._best_wns = wns
                logger.info(
                    "[TOOL_RESULT] New best WNS (near convergence): %.3f",
                    wns,
                    extra={"tool_name": tool_name, "wns": wns, "iteration": self._iteration,
                           "trace_id": get_trace_id()}
                )
            elif self._best_wns > float('-inf') and self._best_wns < -0.01 and wns > abs(self._best_wns) * 10:
                logger.warning(
                    "[TOOL_RESULT] WNS %.3f rejected (unrealistic jump from %.3f)",
                    wns, self._best_wns,
                    extra={"tool_name": tool_name, "wns": wns, "iteration": self._iteration,
                           "trace_id": get_trace_id()}
                )
            else:
                self._best_wns = wns
                logger.info(
                "[TOOL_RESULT] New best WNS: %.3f",
                wns,
                extra={"tool_name": tool_name, "wns": wns, "iteration": self._iteration,
                       "trace_id": get_trace_id()}
            )
        elif error:
            logger.warning(
                "[TOOL_RESULT] Tool error: %s (iteration=%d)",
                tool_name, self._iteration,
                extra={"tool_name": tool_name, "error": error, "iteration": self._iteration,
                       "trace_id": get_trace_id()}
            )

    def set_initial_wns(self, wns: float) -> None:
        if self._initial_wns is None:
            self._initial_wns = wns

    def set_clock_period(self, period: float) -> None:
        self._clock_period = period

    def advance_iteration(self) -> None:
        old_iteration = self._iteration
        self._iteration += 1
        logger.info(
            "[ITERATION] Advanced from %d to %d",
            old_iteration, self._iteration,
            extra={"old_iteration": old_iteration, "iteration": self._iteration,
                   "trace_id": get_trace_id()}
        )