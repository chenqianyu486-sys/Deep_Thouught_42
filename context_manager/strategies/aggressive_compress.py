"""Aggressive compression strategy - preserves only core state."""

from ..interfaces import Message, MessageRole, CompressionContext
from .base import BaseCompressionStrategy


class AggressiveCompressionStrategy(BaseCompressionStrategy):
    """Aggressive compression - preserves only core state."""

    def compress(self, messages: list[Message], context: CompressionContext) -> list[Message]:
        """Execute aggressive compression."""
        if not messages:
            return []

        current_wns = self._get_latest_wns(context.tool_call_details)

        summary_lines = [
            "[CONTEXT COMPRESSED - AGGRESSIVE] OPTIMIZATION STATE SUMMARY",
            "[WARNING] History heavily compressed. Rely on state below to continue.",
            f"- Target Clock: {context.clock_period:.3f} ns" if context.clock_period else "- Target Clock: N/A",
            f"- Initial WNS: {context.initial_wns:.3f} ns" if context.initial_wns else "- Initial WNS: N/A",
            f"- Historical Best WNS: {context.best_wns:.3f} ns",
            f"- Current Measured WNS: {current_wns:.3f} ns" if current_wns else "- Current WNS: N/A",
            f"- Current Iteration: {context.iteration}",
            f"- Total Tool Calls: {len(context.tool_call_details)}",
        ]

        if context.failed_strategies:
            summary_lines.append(
                f"[BLOCKED] Failed strategies: {', '.join(context.failed_strategies[-5:])}"
            )

        summary_lines.append("[STAT] Last 5 key operations:")

        recent_iters = set()
        count = 0
        for call in reversed(context.tool_call_details):
            if call["iteration"] not in recent_iters and count < 5:
                recent_iters.add(call["iteration"])
                count += 1
                status = "[FAIL] Failed" if call.get("error") else (
                    f"[WNS] {call['wns']:.3f} ns" if call.get("wns") else "[OK]"
                )
                summary_lines.append(f"  Iter {call['iteration']}: {call['tool_name']} -> {status}")

        new_messages = [messages[0]]
        new_messages.append(Message(
            role=MessageRole.USER,
            content="\n".join(summary_lines),
            metadata={"compression": "aggressive", "strategy": self.get_name()}
        ))

        if len(messages) > 2:
            new_messages.extend(messages[-3:])

        return new_messages

    def get_name(self) -> str:
        return "aggressive_compression"