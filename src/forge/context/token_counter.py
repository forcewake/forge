from __future__ import annotations

from functools import lru_cache
from typing import Literal

import tiktoken

DEFAULT_BUDGETS: dict[str, int] = {
    "total": 24_000,
    "diff": 12_000,
    "per_file_diff": 4_000,
    "pipeline_logs": 3_000,
    "description": 2_000,
    "previous_reviews": 3_000,
}


@lru_cache(maxsize=4)
def _get_encoding(name: str = "cl100k_base") -> tiktoken.Encoding:
    return tiktoken.get_encoding(name)


class TokenCounter:
    """Thin wrapper around a tiktoken encoding for counting and truncating."""

    def __init__(self, encoding: str = "cl100k_base") -> None:
        self._enc = _get_encoding(encoding)

    def count(self, text: str) -> int:
        """Return the number of tokens in *text*."""
        return len(self._enc.encode(text))

    def truncate(
        self,
        text: str,
        max_tokens: int,
        strategy: Literal["tail", "head", "middle"] = "tail",
    ) -> str:
        """Truncate *text* to fit within *max_tokens*.

        Strategies:
            tail  — keep the beginning, cut the end (default, good for diffs)
            head  — keep the end, cut the beginning (good for logs)
            middle — keep beginning and end, cut middle (good for large files)
        """
        tokens = self._enc.encode(text)
        if len(tokens) <= max_tokens:
            return text

        if max_tokens <= 0:
            return ""

        if strategy == "tail":
            truncated = self._enc.decode(tokens[:max_tokens])
            return truncated + "\n... [truncated]"

        if strategy == "head":
            truncated = self._enc.decode(tokens[-max_tokens:])
            return "[truncated] ...\n" + truncated

        # middle: keep first half and last half
        half = max_tokens // 2
        head = self._enc.decode(tokens[:half])
        tail = self._enc.decode(tokens[-half:])
        return head + "\n... [truncated middle] ...\n" + tail


class TokenBudget:
    """Track cumulative token usage across multiple context categories."""

    def __init__(self, budgets: dict[str, int] | None = None) -> None:
        self._budgets = dict(DEFAULT_BUDGETS)
        if budgets:
            self._budgets.update(budgets)
        self._used: dict[str, int] = {}
        self._counter = TokenCounter()

    @property
    def total_budget(self) -> int:
        return self._budgets["total"]

    @property
    def total_used(self) -> int:
        return self._used.get("total", 0)

    def remaining(self, category: str) -> int:
        """Tokens remaining in *category* (also capped by total remaining)."""
        cat_remaining = self._budgets.get(category, 0) - self._used.get(category, 0)
        total_remaining = self._budgets["total"] - self._used.get("total", 0)
        return max(0, min(cat_remaining, total_remaining))

    def consume(self, category: str, tokens: int) -> int:
        """Record *tokens* consumed in *category*. Returns actual consumed (clamped)."""
        available = self.remaining(category)
        actual = min(tokens, available)
        self._used[category] = self._used.get(category, 0) + actual
        self._used["total"] = self._used.get("total", 0) + actual
        return actual

    def fit(
        self,
        text: str,
        category: str,
        strategy: Literal["tail", "head", "middle"] = "tail",
    ) -> str:
        """Truncate *text* to fit in *category* budget, then consume."""
        budget = self.remaining(category)
        if budget <= 0:
            return ""
        truncated = self._counter.truncate(text, budget, strategy)
        token_count = self._counter.count(truncated)
        self.consume(category, token_count)
        return truncated
