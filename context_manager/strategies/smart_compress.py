"""Smart compression strategy - preserves key information."""

from ..interfaces import Message, MessageRole, CompressionContext
from .base import BaseCompressionStrategy


class SmartCompressionStrategy(BaseCompressionStrategy):
    """Smart light compression - preserves key information."""

    def __init__(self, recent_window: int = 20):
        self._recent_window = recent_window

    def compress(self, messages: list[Message], context: CompressionContext) -> list[Message]:
        """Execute smart compression."""
        if len(messages) < 3:
            return messages

        current_wns = self._get_latest_wns(context.tool_call_details)

        summary_lines = [
            "[CONTEXT COMPRESSED - SMART] OPTIMIZATION STATE SUMMARY",
            "History has been intelligently compressed, preserving key operation traces.",
            f"- Target Clock: {context.clock_period:.3f} ns" if context.clock_period else "- Target Clock: N/A",
            f"- Initial WNS: {context.initial_wns:.3f} ns" if context.initial_wns else "- Initial WNS: N/A",
            f"- Historical Best WNS: {context.best_wns:.3f} ns",
            f"- Current Measured WNS: {current_wns:.3f} ns" if current_wns else "- Current WNS: awaiting",
            f"- Current Iteration: {context.iteration}",
        ]

        if context.failed_strategies:
            summary_lines.append(
                f"[BLOCKED] Failed strategies: {', '.join(context.failed_strategies[-10:])}"
            )

        summary_lines.append("[WARNING] Must be data-driven. Evaluate effects after each phys_opt.")
        summary_lines.append("[STAT] Key operation traces:")

        merged_calls = self._merge_tool_calls(context.tool_call_details)

        for call in merged_calls[-30:]:
            status = self._format_call_status(call)
            summary_lines.append(f"  Iter {call.get('iteration', '?')}: {call.get('tool_name', 'unknown')} -> {status}")

        new_messages = [messages[0], messages[1]]
        new_messages.append(Message(
            role=MessageRole.USER,
            content="\n".join(summary_lines),
            metadata={"compression": "smart", "strategy": self.get_name()}
        ))

        if len(messages) > 2:
            recent_start = max(2, len(messages) - self._recent_window)
            new_messages.extend(messages[recent_start:])

        return new_messages

    def get_name(self) -> str:
        return "smart_compression"

    def _merge_tool_calls(self, tool_calls: list[dict]) -> list[dict]:
        """Merge tool calls by iteration, keeping unique iterations."""
        seen = set()
        merged = []
        for call in tool_calls[-40:]:
            if call.get("iteration") not in seen:
                seen.add(call.get("iteration", None))
                merged.append(call)
        return merged