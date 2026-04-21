"""Context estimation for token counting."""

import re
from .interfaces import Message


class ContextEstimator:
    """Estimates token count for context management."""

    # Character-to-token ratios by content type (more accurate than flat //4)
    # Based on OpenAI's cl100k_base tokenizer behavior
    CHINESE_CHARS_PER_TOKEN = 1.5      # ~1.5 Chinese chars ≈ 1 token
    ENGLISH_CHARS_PER_TOKEN = 3.5      # ~3.5 English chars ≈ 1 token
    DIGITS_CHARS_PER_TOKEN = 4.0       # digits compress well
    WHITESPACE_CHARS_PER_TOKEN = 5.0    # whitespace-heavy content
    CODE_CHARS_PER_TOKEN = 2.5         # code mixed with special chars

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Estimate tokens using content-type-aware method.

        More accurate than simple len//4 as it accounts for:
        - Chinese characters (higher token density)
        - English words (lower token density)
        - Code with special characters
        - Digits and whitespace
        """
        if not text:
            return 0

        # Count character types for weighted estimation
        chinese_chars = len(re.findall(r'[一-鿿]', text))
        english_chars = len(re.findall(r'[a-zA-Z]', text))
        digit_chars = len(re.findall(r'\d', text))
        whitespace_chars = len(re.findall(r'\s', text))
        special_chars = len(text) - chinese_chars - english_chars - digit_chars - whitespace_chars

        # Weighted calculation based on character types
        tokens = (
            chinese_chars / ContextEstimator.CHINESE_CHARS_PER_TOKEN +
            english_chars / ContextEstimator.ENGLISH_CHARS_PER_TOKEN +
            digit_chars / ContextEstimator.DIGITS_CHARS_PER_TOKEN +
            whitespace_chars / ContextEstimator.WHITESPACE_CHARS_PER_TOKEN +
            special_chars / ContextEstimator.CODE_CHARS_PER_TOKEN
        )

        return max(1, int(tokens))

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