"""Conservative failure classification and bounded retry delay, with explicit inputs."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

FailureClass = Literal["transient", "fatal"]


TRANSIENT: FailureClass = "transient"


FATAL: FailureClass = "fatal"


class Retryability(StrEnum):
    """WHY a run is retryable — the typed revival-attempt class."""

    #: Tier-1: a transient infrastructure death (dispatch 5xx / network / rate limit /
    #: runner startup) the reconciler re-drives on its own.
    TRANSIENT_INFRASTRUCTURE = "transient_infrastructure"
    #: Tier-2: an operator explicitly granted the revival (``/retry``).
    OPERATOR_OVERRIDE = "operator_override"
    #: Tier-1: the transient cause was a verification/harness timeout.
    VERIFICATION_TIMEOUT = "verification_timeout"


RevivalKind = Literal["retry_requested", "auto_revive"]


_VERIFICATION_TIMEOUT_RE = re.compile(
    r"verification[_ ]?(timed?|timeout)|timed? ?out[^\n]*verification", re.IGNORECASE
)


def classify_retryability(kind: RevivalKind, reason: str = "") -> Retryability:
    """The retryability class for a revival attempt of *kind*."""
    if kind == "retry_requested":
        return Retryability.OPERATOR_OVERRIDE
    if _VERIFICATION_TIMEOUT_RE.search(reason or ""):
        return Retryability.VERIFICATION_TIMEOUT
    return Retryability.TRANSIENT_INFRASTRUCTURE


DEFAULT_REVIVE_LIMIT = 2


DEFAULT_BACKOFF_SECONDS = 60


MAX_BACKOFF_SECONDS = 900


_TRANSIENT_MARKERS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"\b(500|502|503|504|429)\b",
        r"bad gateway",
        r"service (temporarily )?unavailable",
        r"gateway time-?out",
        r"internal server error",
        r"rate limit",
        r"too many requests",
        r"timed? ?out",
        r"timeout",
        r"\bconnection\b",
        r"\bnetwork\b",
        r"unreachable",
        r"try again",
        r"runner (startup|start|unavailable|failed|failure)",
        r"no (matching )?runner",
    )
)


_DISPATCH_PREFIXES = ("harness_start_failed",)


def classify_terminal_failure(reason: str) -> FailureClass:
    """Classify a terminal ``failed`` reason as ``transient`` or ``fatal``."""
    text = (reason or "").strip()
    lowered = text.lower()
    if lowered.startswith(_DISPATCH_PREFIXES) and not _detail_of(text):
        return TRANSIENT
    for marker in _TRANSIENT_MARKERS:
        if marker.search(lowered):
            return TRANSIENT
    return FATAL


def _detail_of(reason: str) -> str:
    """The part of ``prefix: detail`` after the first colon ("" when absent)."""
    _, _, detail = reason.partition(":")
    return detail.strip()


def backoff_seconds(attempt: int, base_seconds: int = DEFAULT_BACKOFF_SECONDS) -> int:
    """Bound retry delay without reading settings, a clock or an environment."""
    if attempt >= MAX_BACKOFF_SECONDS.bit_length():
        return MAX_BACKOFF_SECONDS
    return min(max(base_seconds, 1) * (1 << max(attempt, 0)), MAX_BACKOFF_SECONDS)
