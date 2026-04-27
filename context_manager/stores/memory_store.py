"""Memory store implementation for Context Manager."""

import logging
import time
from typing import Callable, Optional
from ..interfaces import ContextStore, Message, ContextSnapshot, MemoryLayer

logger = logging.getLogger(__name__)


class InMemoryContextStore(ContextStore):
    """In-memory implementation of ContextStore."""

    def __init__(self):
        self._messages: list[Message] = []

    def add(self, message: Message) -> ContextSnapshot:
        self._messages.append(message)
        count = len(self._messages)
        logger.debug(
            "[MEMORY] Added message, working_memory_size=%d",
            count,
            extra={"message_count": count, "role": message.role.value}
        )
        return ContextSnapshot(
            timestamp=message.metadata.get("timestamp") or time.time(),
            layer=MemoryLayer.WORKING,
            message_count=count
        )

    def get(self, index: int) -> Optional[Message]:
        if 0 <= index < len(self._messages):
            return self._messages[index]
        return None

    def get_range(self, start: int, end: int) -> list[Message]:
        return self._messages[start:end]

    def get_recent(self, n: int) -> list[Message]:
        return self._messages[-n:] if n <= len(self._messages) else self._messages

    def get_all(self) -> list[Message]:
        return list(self._messages)

    def search(self, predicate: Callable[[Message], bool]) -> list[Message]:
        return [msg for msg in self._messages if predicate(msg)]

    def snapshot(self) -> ContextSnapshot:
        return ContextSnapshot(
            layer=MemoryLayer.WORKING,
            message_count=len(self._messages)
        )

    def restore(self, snapshot: ContextSnapshot) -> None:
        """Restore state from snapshot.

        Raises:
            NotImplementedError: ContextSnapshot does not store message data,
            so restoration is not possible. This method exists for interface
            compatibility but cannot function as-is.
        """
        raise NotImplementedError(
            "ContextSnapshot.restore() is not implemented: snapshot contains "
            "only metadata (count, timestamp), not actual messages. "
            "To support restore(), ContextSnapshot would need to store "
            "a full message list."
        )

    def clear(self) -> None:
        count = len(self._messages)
        self._messages.clear()
        logger.debug("[MEMORY] Cleared working memory, removed %d messages", count)

    def __len__(self) -> int:
        return len(self._messages)

    def __bool__(self) -> bool:
        return len(self._messages) > 0