from __future__ import annotations

import random
from dataclasses import dataclass, field

# Errors that should not be retried (permanent failures).
_NON_RETRYABLE = frozenset(
    {
        "ValidationError",
        "KeyError",
        "TypeError",
        "AttributeError",
    }
)


@dataclass(frozen=True)
class RetryPolicy:
    """Configurable retry policy for task execution."""

    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    jitter_fraction: float = 0.25
    non_retryable: frozenset[str] = field(default_factory=lambda: _NON_RETRYABLE)

    def should_retry(self, attempt: int, error: Exception | str) -> bool:
        """Return True if the task should be retried.

        *error* can be an Exception instance or a string like "ValidationError: ...".
        """
        if attempt >= self.max_retries:
            return False
        if isinstance(error, str):
            error_name = error.split(":")[0].strip() if ":" in error else error.strip()
        else:
            error_name = type(error).__name__
        return error_name not in self.non_retryable

    def compute_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter: base * 2^attempt + random jitter."""
        delay = min(self.base_delay * (2**attempt), self.max_delay)
        jitter = delay * self.jitter_fraction * random.random()
        return delay + jitter
