"""Base class for compression strategies."""

from abc import ABC, abstractmethod
from ..interfaces import CompressionStrategy, CompressionContext, Message


class BaseCompressionStrategy(CompressionStrategy, ABC):
    """Base class for compression strategies."""

    @abstractmethod
    def compress(self, messages: list[Message], context: CompressionContext) -> list[Message]:
        pass

    @abstractmethod
    def get_name(self) -> str:
        pass

    def _get_latest_wns(self, tool_call_details: list[dict]):
        """Get latest non-null WNS from tool calls."""
        for call in reversed(tool_call_details):
            if call.get("wns") is not None:
                return call["wns"]
        return None

    def _format_call_status(self, call: dict) -> str:
        """Format tool call status for summary."""
        if call.get("error"):
            return "[FAIL] Failed"
        wns = call.get("wns")
        if wns is not None:
            return f"[WNS] {wns:.3f} ns"
        return "[OK] Executed"