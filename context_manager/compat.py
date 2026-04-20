"""Compatibility layer for dcp_optimizer1_5.py."""

from typing import Optional
from .manager import MemoryManager
from .interfaces import MessageRole, Message


class DCPOptimizerCompat:
    """Compatibility layer for dcp_optimizer1_5.py."""

    def __init__(self, memory_manager: MemoryManager):
        self._mm = memory_manager

    @property
    def messages(self) -> list[dict]:
        """Get messages in original dict format for API compatibility."""
        return [
            {"role": m.role.value, "content": m.content, **m.metadata}
            for m in self._mm.get_context()
        ]

    @property
    def failed_strategies(self) -> list[str]:
        return self._mm.failed_strategies

    @property
    def tool_call_details(self) -> list[dict]:
        return self._mm.tool_call_details

    @property
    def best_wns(self) -> float:
        return self._mm.best_wns

    @property
    def initial_wns(self) -> Optional[float]:
        return self._mm.initial_wns

    @property
    def iteration(self) -> int:
        return self._mm.iteration

    def add_message(self, role: str, content: str) -> None:
        """Add message in original format."""
        self._mm.add_message(MessageRole(role), content)

    def add_tool_result(
        self,
        tool_name: str,
        result: str,
        wns: Optional[float] = None,
        error: bool = False
    ) -> None:
        """Track tool call result."""
        self._mm.add_tool_result(tool_name, result, wns, error)

    def record_failure(self, strategy: str) -> None:
        """Record a failed strategy."""
        self._mm.record_failure(strategy)

    def advance_iteration(self) -> None:
        """Advance iteration counter."""
        self._mm.advance_iteration()

    def set_initial_wns(self, wns: float) -> None:
        """Set initial WNS value."""
        self._mm.set_initial_wns(wns)

    def set_clock_period(self, period: float) -> None:
        """Set clock period."""
        self._mm.set_clock_period(period)

    def get_formatted_for_api(self, system_prompt: str = None) -> list[dict]:
        """Get context formatted for LLM API call."""
        return self._mm.get_formatted_for_api(system_prompt)