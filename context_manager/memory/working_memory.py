"""Working memory layer for short-term context."""

from dataclasses import dataclass
from typing import Optional
from ..interfaces import ContextStore, Message, ContextEvent, EventType, MessageRole, ContextSnapshot
from ..events import EventBus
from ..estimator import ContextEstimator


@dataclass
class WorkingMemoryConfig:
    """Configuration for Working Memory."""
    max_tokens: int = 80_000
    hard_limit_tokens: int = 150_000
    recent_window: int = 20
    tool_result_truncate: int = 30_000


class WorkingMemory:
    """Working Memory - Short-term storage for current task."""

    def __init__(self, store: ContextStore, config: WorkingMemoryConfig, estimator: ContextEstimator, event_bus: Optional[EventBus] = None):
        self._store = store
        self._config = config
        self._estimator = estimator
        self._event_bus = event_bus

    def add_message(self, message: Message) -> ContextSnapshot:
        """Add message and check compression threshold."""
        snapshot = self._store.add(message)
        if self._event_bus:
            self._event_bus.emit(ContextEvent(
                event_type=EventType.MESSAGE_ADDED,
                data={"snapshot": snapshot, "message": message}
            ))
        return snapshot

    def get_context_for_model(self, system_prompt: str, include_history: bool = True) -> list[dict]:
        """Get formatted context for LLM API call."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if include_history:
            for m in self._store.get_all():
                msg_dict = {"role": m.role.value, "content": m.content}
                if m.name:
                    msg_dict["name"] = m.name
                if m.role == MessageRole.TOOL and m.tool_call_id:
                    msg_dict["tool_call_id"] = m.tool_call_id
                messages.append(msg_dict)
        return messages

    def get_all(self) -> list[Message]:
        return self._store.get_all()

    def clear(self) -> None:
        self._store.clear()

    def estimate_tokens(self) -> int:
        return self._estimator.estimate_from_messages(self._store.get_all())