"""Working memory layer for short-term context."""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)
from ..interfaces import ContextStore, Message, ContextEvent, EventType, MessageRole, ContextSnapshot
from ..events import EventBus
from ..estimator import ContextEstimator


@dataclass
class WorkingMemoryConfig:
    """Configuration for Working Memory."""
    max_tokens: int = 80_000
    hard_limit_tokens: int = 150_000
    # DEPRECATED: unused fields - will be removed in future version
    recent_window: int = 20  # DEPRECATED: not used
    tool_result_truncate: int = 30_000  # DEPRECATED: not used


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

        # Check token capacity limits
        current_tokens = self.estimate_tokens()
        if current_tokens >= self._config.hard_limit_tokens:
            logger.warning(
                "[WORKING_MEMORY] Hard token limit exceeded: %d >= %d",
                current_tokens, self._config.hard_limit_tokens,
                extra={"current_tokens": current_tokens,
                       "hard_limit_tokens": self._config.hard_limit_tokens}
            )
        elif current_tokens >= self._config.max_tokens:
            logger.debug(
                "[WORKING_MEMORY] Soft token limit reached: %d >= %d",
                current_tokens, self._config.max_tokens,
                extra={"current_tokens": current_tokens,
                       "max_tokens": self._config.max_tokens}
            )

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
                if m.role == MessageRole.ASSISTANT and m.tool_calls:
                    msg_dict["tool_calls"] = m.tool_calls
                messages.append(msg_dict)
        return messages

    def get_all(self) -> list[Message]:
        return self._store.get_all()

    def clear(self) -> None:
        self._store.clear()

    def estimate_tokens(self) -> int:
        return self._estimator.estimate_from_messages(self._store.get_all())