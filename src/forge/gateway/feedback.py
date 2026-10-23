"""Classify human review feedback into bounded follow-up commands."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

__all__ = [
    "FORGE_REVIEW_FEEDBACK_ENV",
    "FORGE_MAX_REVIEW_ROUNDS_ENV",
    "DEFAULT_MAX_REVIEW_ROUNDS",
    "REVIEW_FEEDBACK_NOTE_COMMANDS",
    "max_review_rounds",
    "review_feedback_commands_enabled",
    "review_feedback_command_set",
]

#: The env var that turns the review-feedback surface on. Default OFF.
FORGE_REVIEW_FEEDBACK_ENV: Final = "FORGE_REVIEW_FEEDBACK_ENABLED"

#: the operator policy bounding how many linked review rounds a delivery lineage may open
#: after ``ready_for_human``.
FORGE_MAX_REVIEW_ROUNDS_ENV: Final = "FORGE_MAX_REVIEW_ROUNDS"

#: The default round bound (delivery 1 + 1 review round).
DEFAULT_MAX_REVIEW_ROUNDS: Final = 1

#: The closed bound the policy clamps into — a round count above this is an operator typo,
#: and a typo must narrow authority, never widen it (the same fail-closed reading
#: ``parse_tactical_policy`` gives an unrecognized token).
_MAX_BOUND_REVIEW_ROUNDS: Final = 10

#: The review-feedback verbs — the note shapes
#: func:`forge.adaptive.revisions.parse_review_feedback_note` accepts.
REVIEW_FEEDBACK_NOTE_COMMANDS: Final = frozenset({"/fix", "/ask"})

#: Truthy spellings — the same closed set ``FORGE_ADAPTIVE_COMMANDS_ENABLED`` uses; anything
#: else fails CLOSED.
_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})


def review_feedback_commands_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether the review-feedback surface is turned on (default: off)."""
    source = os.environ if env is None else env
    return str(source.get(FORGE_REVIEW_FEEDBACK_ENV, "")).strip().lower() in _TRUTHY


def review_feedback_command_set(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """The feedback verbs a gateway may parse right now (empty when off)."""
    return REVIEW_FEEDBACK_NOTE_COMMANDS if review_feedback_commands_enabled(env) else frozenset()


def max_review_rounds(env: Mapping[str, str] | None = None) -> int:
    """The bounded review-round policy: how many post-readiness rounds a delivery lineage may open."""
    source = os.environ if env is None else env
    raw = str(source.get(FORGE_MAX_REVIEW_ROUNDS_ENV, "")).strip()
    if not raw:
        return DEFAULT_MAX_REVIEW_ROUNDS
    try:
        requested = int(raw)
    except ValueError:
        return DEFAULT_MAX_REVIEW_ROUNDS
    return max(0, min(requested, _MAX_BOUND_REVIEW_ROUNDS))
