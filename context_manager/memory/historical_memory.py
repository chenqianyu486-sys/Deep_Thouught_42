"""Historical memory layer for long-term context."""

from dataclasses import dataclass, field
from typing import Optional
import time
import uuid
from ..interfaces import HistoricalEntry, RetrievalQuery, ContextEvent, EventType
from ..events import EventBus


@dataclass
class HistoricalMemoryConfig:
    """Configuration for Historical Memory."""
    max_entries: int = 10_000
    # DEPRECATED: unused fields - will be removed in future version
    relevance_threshold: float = 0.5  # DEPRECATED: not used
    age_based_decay: float = 0.95  # DEPRECATED: not used


class HistoricalMemory:
    """Historical Memory - Long-term storage for cross-task context."""

    def __init__(self, config: HistoricalMemoryConfig, event_bus: Optional[EventBus] = None):
        self._config = config
        self._event_bus = event_bus
        self._entries: dict[str, HistoricalEntry] = {}
        self._index_by_time: list[str] = []
        self._index_by_importance: set[str] = set()
        self._index_by_task_type: dict[str, list[str]] = {}

    def add(
        self,
        content: str,
        importance: float = 0.5,
        task_type: Optional[str] = None,
        agent_id: Optional[str] = None,
        tags: list = None,
        embedding: Optional[list] = None
    ) -> str:
        """Add entry to historical memory. Evicts oldest entries if at max_entries."""
        if len(self._entries) >= self._config.max_entries:
            # Evict oldest entry (first in time index)
            oldest_id = self._index_by_time[0]
            old_entry = self._entries.get(oldest_id)
            self._entries.pop(oldest_id, None)
            self._index_by_time.pop(0)
            # Remove from importance index (use discard for safety)
            self._index_by_importance.discard(oldest_id)
            # Remove from task type index
            if old_entry and old_entry.task_type and old_entry.task_type in self._index_by_task_type:
                try:
                    self._index_by_task_type[old_entry.task_type].remove(oldest_id)
                except ValueError:
                    pass

        entry_id = str(uuid.uuid4())
        entry = HistoricalEntry(
            id=entry_id,
            timestamp=time.time(),
            content=content,
            importance_score=importance,
            task_type=task_type,
            agent_id=agent_id,
            tags=tags or [],
            embedding=embedding
        )

        self._entries[entry_id] = entry
        self._reindex_entry(entry_id)

        if self._event_bus:
            self._event_bus.emit(ContextEvent(
                event_type=EventType.LAYER_PROMOTED,
                data={"entry_id": entry_id, "entry": entry}
            ))

        return entry_id

    def retrieve(self, query: RetrievalQuery) -> list[HistoricalEntry]:
        """Retrieve entries matching query criteria."""
        # If no text search and no agent_id filter, use importance index for efficiency
        if not query.text and not query.agent_id and query.task_type:
            # Use task_type index + importance index
            candidate_ids = self._index_by_task_type.get(query.task_type, [])
            candidates = [self._entries[eid] for eid in candidate_ids if eid in self._entries]
        elif not query.text and not query.agent_id and not query.task_type:
            # Use importance index only (sort by importance descending at retrieval time)
            sorted_by_importance = sorted(
                [eid for eid in self._index_by_importance if eid in self._entries],
                key=lambda eid: self._entries[eid].importance_score,
                reverse=True
            )
            candidates = [self._entries[eid] for eid in sorted_by_importance]
        else:
            # Fall back to full scan for complex queries
            candidates = list(self._entries.values())

        results = []
        for entry in candidates:
            if query.time_range:
                start, end = query.time_range
                if not (start <= entry.timestamp <= end):
                    continue

            if query.task_type and entry.task_type != query.task_type:
                continue

            if query.agent_id and entry.agent_id != query.agent_id:
                continue

            if entry.importance_score < query.min_importance:
                continue

            if query.text:
                if query.text.lower() not in entry.content.lower():
                    continue

            results.append(entry)

        # Only sort if we didn't use the importance index (which is already sorted)
        if query.text or query.agent_id:
            results.sort(key=lambda e: e.importance_score, reverse=True)

        return results[:query.limit] if query.limit else results

    def _reindex_entry(self, entry_id: str) -> None:
        """Reindex an entry by time, importance, and task type."""
        entry = self._entries[entry_id]

        time_idx = 0
        for i, eid in enumerate(self._index_by_time):
            if self._entries[eid].timestamp < entry.timestamp:
                time_idx = i + 1
        self._index_by_time.insert(time_idx, entry_id)

        # Add to importance set (order handled at retrieval time via sorting)
        self._index_by_importance.add(entry_id)

        if entry.task_type:
            if entry.task_type not in self._index_by_task_type:
                self._index_by_task_type[entry.task_type] = []
            self._index_by_task_type[entry.task_type].append(entry_id)

    def __len__(self) -> int:
        return len(self._entries)