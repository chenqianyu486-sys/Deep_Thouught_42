"""Context estimation for token counting."""

from .interfaces import Message


class ContextEstimator:
    """Estimates token count for context management."""

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Estimate tokens using character-based method."""
        return len(text) // 4

    def estimate_from_messages(self, messages: list[Message]) -> int:
        """Estimate total tokens from message list."""
        total = 0
        for msg in messages:
            content = str(msg.content) if msg.content else ""
            total += self.estimate_tokens(content)
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    if isinstance(tc, dict) and "function" in tc:
                        total += self.estimate_tokens(str(tc["function"]))
        return total

    def estimate_context_complexity(
        self,
        messages: list[Message],
        iteration: int = 0,
        failed_strategies_count: int = 0
    ) -> float:
        """Estimate context complexity score (0-10)."""
        msg_count = len(messages)
        token_est = self.estimate_from_messages(messages)

        score = 0.0
        score += min(msg_count / 20, 3)
        score += min(token_est / 50_000, 3)
        score += min(iteration / 10, 2)
        score += min(failed_strategies_count / 5, 2)
        return min(score, 10)