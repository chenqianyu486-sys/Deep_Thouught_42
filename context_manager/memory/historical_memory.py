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
    relevance_threshold: float = 0.5
    age_based_decay: float = 0.95


class HistoricalMemory:
    """Historical Memory - Long-term storage for cross-task context."""

    def __init__(self, config: HistoricalMemoryConfig, event_bus: Optional[EventBus] = None):
        self._config = config
        self._event_bus = event_bus
        self._entries: dict[str, HistoricalEntry] = {}
        self._index_by_time: list[str] = []
        self._index_by_importance: list[str] = []
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
        """Add entry to historical memory."""
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
        results = []

        for entry in self._entries.values():
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

        imp_idx = 0
        for i, eid in enumerate(self._index_by_importance):
            if self._entries[eid].importance_score < entry.importance_score:
                imp_idx = i + 1
        self._index_by_importance.insert(imp_idx, entry_id)

        if entry.task_type:
            if entry.task_type not in self._index_by_task_type:
                self._index_by_task_type[entry.task_type] = []
            self._index_by_task_type[entry.task_type].append(entry_id)

    def __len__(self) -> int:
        return len(self._entries)