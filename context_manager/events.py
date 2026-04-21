"""Event system for Context Manager."""

import logging
from typing import Callable
from .interfaces import ContextEvent, EventType

logger = logging.getLogger(__name__)
EventHook = Callable[[ContextEvent], None]


class EventBus:
    """Event bus for context change notifications."""

    def __init__(self):
        self._sync_handlers: dict[EventType, list[Callable]] = {}
        self._global_handlers: list[Callable] = []
        self._event_history: list[ContextEvent] = []
        self._max_history = 1000

    def subscribe(self, event_type: EventType, handler: Callable) -> None:
        """Subscribe to specific event type."""
        if event_type not in self._sync_handlers:
            self._sync_handlers[event_type] = []
        self._sync_handlers[event_type].append(handler)

    def unsubscribe(self, event_type: EventType, handler: Callable) -> None:
        """Unsubscribe a specific handler from an event type."""
        if event_type in self._sync_handlers:
            try:
                self._sync_handlers[event_type].remove(handler)
            except ValueError:
                pass  # Handler not found, ignore

    def subscribe_global(self, handler: Callable) -> None:
        """Subscribe to all events."""
        self._global_handlers.append(handler)

    def unsubscribe_global(self, handler: Callable) -> None:
        """Unsubscribe a specific global handler."""
        try:
            self._global_handlers.remove(handler)
        except ValueError:
            pass  # Handler not found, ignore

    def emit(self, event: ContextEvent) -> None:
        """Emit event synchronously."""
        self._event_history.append(event)
        if len(self._event_history) > self._max_history:
            self._event_history.pop(0)

        for handler in self._sync_handlers.get(event.event_type, []):
            try:
                handler(event)
            except Exception:
                logger.exception(f"Event handler failed for {event.event_type}: {handler}")

        for handler in self._global_handlers:
            try:
                handler(event)
            except Exception:
                logger.exception(f"Global event handler failed: {handler}")

    def get_history(self, event_type: EventType = None, limit: int = 100) -> list[ContextEvent]:
        """Get recent event history."""
        events = self._event_history
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        return events[-limit:]