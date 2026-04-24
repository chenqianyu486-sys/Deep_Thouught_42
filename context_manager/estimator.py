"""Context estimation for token counting using tiktoken."""

import tiktoken
from .interfaces import Message


class ContextEstimator:
    """Estimates token count using tiktoken (cl100k_base encoding)."""

    _ENCODING = None

    @classmethod
    def _get_encoding(cls):
        """Lazy-load the tiktoken encoding singleton."""
        if cls._ENCODING is None:
            cls._ENCODING = tiktoken.get_encoding("cl100k_base")
        return cls._ENCODING

    @classmethod
    def estimate_tokens(cls, text: str) -> int:
        """Accurate token count using OpenAI's cl100k_base tokenizer.

        Args:
            text: The text string to count tokens for

        Returns:
            Number of tokens (integer), minimum 0
        """
        if not text:
            return 0
        encoding = cls._get_encoding()
        return len(encoding.encode(text))

    @classmethod
    def estimate_from_messages(cls, messages: list[Message]) -> int:
        """Estimate total tokens from message list using tiktoken.

        Args:
            messages: List of Message objects

        Returns:
            Total token count across all messages
        """
        total = 0
        for msg in messages:
            content = str(msg.content) if msg.content else ""
            total += cls.estimate_tokens(content)
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    if isinstance(tc, dict) and "function" in tc:
                        total += cls.estimate_tokens(str(tc["function"]))
        return total

    def estimate_context_complexity(
        self,
        messages: list[Message],
        iteration: int = 0,
        failed_strategies_count: int = 0
    ) -> float:
        """Estimate context complexity score (0-10) using accurate token counts.

        Args:
            messages: List of Message objects
            iteration: Current optimization iteration
            failed_strategies_count: Number of failed strategies

        Returns:
            Complexity score from 0.0 to 10.0
        """
        msg_count = len(messages)
        token_est = self.estimate_from_messages(messages)

        score = 0.0
        score += min(msg_count / 20, 3)
        score += min(token_est / 50_000, 3)
        score += min(iteration / 10, 2)
        score += min(failed_strategies_count / 5, 2)
        return min(score, 10)
