"""Event system for Context Manager."""

import logging
import uuid
from typing import Callable
from .interfaces import ContextEvent, EventType, EventHook
from .logging_config import get_trace_id

logger = logging.getLogger(__name__)


class EventBus:
    """Event bus for context change notifications."""

    def __init__(self):
        self._sync_handlers: dict[EventType, list[Callable]] = {}
        self._global_handlers: list[Callable] = []
        self._event_history: list[ContextEvent] = []
        self._max_history = 1000
        self._handler_tokens: dict[str, tuple[EventType, Callable]] = {}  # token -> (event_type, handler)
        self._global_handler_tokens: dict[str, Callable] = {}  # token -> handler

    def subscribe(self, event_type: EventType, handler: Callable) -> str:
        """Subscribe to specific event type. Returns a token for unsubscribe."""
        if event_type not in self._sync_handlers:
            self._sync_handlers[event_type] = []
        self._sync_handlers[event_type].append(handler)
        token = str(uuid.uuid4())
        self._handler_tokens[token] = (event_type, handler)
        logger.debug(
            "[EVENT_SUBSCRIBE] handler=%s, event_type=%s, token=%s",
            handler.__name__ if hasattr(handler, '__name__') else str(handler),
            event_type.value if hasattr(event_type, 'value') else event_type,
            token,
            extra={"event_type": event_type.value if hasattr(event_type, 'value') else event_type,
                   "handler_name": handler.__name__ if hasattr(handler, '__name__') else str(handler),
                   "token": token, "trace_id": get_trace_id()}
        )
        return token

    def unsubscribe_by_token(self, token: str) -> bool:
        """Unsubscribe by token. Returns True if found and removed."""
        if token in self._handler_tokens:
            event_type, handler = self._handler_tokens.pop(token)
            if event_type in self._sync_handlers:
                try:
                    self._sync_handlers[event_type].remove(handler)
                    logger.debug(
                        "[EVENT_UNSUBSCRIBE] token=%s, event_type=%s, handler=%s",
                        token,
                        event_type.value if hasattr(event_type, 'value') else event_type,
                        handler.__name__ if hasattr(handler, '__name__') else str(handler),
                        extra={"event_type": event_type.value if hasattr(event_type, 'value') else event_type,
                               "handler_name": handler.__name__ if hasattr(handler, '__name__') else str(handler),
                               "token": token, "trace_id": get_trace_id()}
                    )
                    return True
                except ValueError:
                    pass
        return False

    def unsubscribe(self, event_type: EventType, handler: Callable) -> None:
        """Unsubscribe a specific handler from an event type."""
        if event_type in self._sync_handlers:
            try:
                self._sync_handlers[event_type].remove(handler)
                # Remove associated tokens
                tokens_to_remove = [t for t, (et, h) in self._handler_tokens.items() if et == event_type and h == handler]
                for t in tokens_to_remove:
                    self._handler_tokens.pop(t, None)
                logger.debug(
                    "[EVENT_UNSUBSCRIBE] event_type=%s, handler=%s",
                    event_type.value if hasattr(event_type, 'value') else event_type,
                    handler.__name__ if hasattr(handler, '__name__') else str(handler),
                    extra={"event_type": event_type.value if hasattr(event_type, 'value') else event_type,
                           "handler_name": handler.__name__ if hasattr(handler, '__name__') else str(handler),
                           "trace_id": get_trace_id()}
                )
            except ValueError:
                pass  # Handler not found, ignore

    def subscribe_global(self, handler: Callable) -> str:
        """Subscribe to all events. Returns a token for unsubscribe."""
        self._global_handlers.append(handler)
        token = str(uuid.uuid4())
        self._global_handler_tokens[token] = handler
        return token

    def unsubscribe_global_by_token(self, token: str) -> bool:
        """Unsubscribe global handler by token. Returns True if found and removed."""
        if token in self._global_handler_tokens:
            handler = self._global_handler_tokens.pop(token)
            try:
                self._global_handlers.remove(handler)
                return True
            except ValueError:
                pass
        return False

    def unsubscribe_global(self, handler: Callable) -> None:
        """Unsubscribe a specific global handler."""
        try:
            self._global_handlers.remove(handler)
            # Remove associated tokens
            tokens_to_remove = [t for t, h in self._global_handler_tokens.items() if h == handler]
            for t in tokens_to_remove:
                self._global_handler_tokens.pop(t, None)
        except ValueError:
            pass  # Handler not found, ignore

    def emit(self, event: ContextEvent) -> None:
        """Emit event synchronously."""
        self._event_history.append(event)
        if len(self._event_history) > self._max_history:
            self._event_history.pop(0)

        # Log event emission at DEBUG level
        logger.debug(
            "[EVENT] Emitting event: type=%s, source_agent=%s",
            event.event_type.value if hasattr(event.event_type, 'value') else event.event_type,
            event.source_agent_id,
            extra={
                "event_type": event.event_type.value if hasattr(event.event_type, 'value') else event.event_type,
                "event_source_agent": event.source_agent_id,
                "event_timestamp": event.timestamp,
                "trace_id": get_trace_id(),
            }
        )

        for handler in self._sync_handlers.get(event.event_type, []):
            try:
                handler(event)
            except Exception:
                logger.exception(
                    "[EVENT_HANDLER_ERROR] Handler failed for event type: %s",
                    event.event_type.value if hasattr(event.event_type, 'value') else event.event_type,
                    extra={
                        "event_type": event.event_type.value if hasattr(event.event_type, 'value') else event.event_type,
                        "handler": repr(handler),
                        "trace_id": get_trace_id(),
                    }
                )

        for handler in self._global_handlers:
            try:
                handler(event)
            except Exception:
                logger.exception(
                    "[EVENT_HANDLER_ERROR] Global handler failed for event type: %s",
                    event.event_type.value if hasattr(event.event_type, 'value') else event.event_type,
                    extra={
                        "event_type": event.event_type.value if hasattr(event.event_type, 'value') else event.event_type,
                        "handler": repr(handler),
                        "trace_id": get_trace_id(),
                    }
                )

    def get_history(self, event_type: EventType = None, limit: int = 100) -> list[ContextEvent]:
        """Get recent event history."""
        events = self._event_history
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        return events[-limit:]