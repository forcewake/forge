"""The review-feedback ingress capability flag (R40-01 / issue #337).

``/fix`` and ``/ask`` — a reviewer's bounded correction request and
clarification on a Draft MR discussion — are served by the durable run
service's ``review_feedback`` command (Q39-13/#332:
:meth:`forge.runs.service.RunService.handle_review_feedback_note`). This
module is the ONE flag that decides whether the checked GitLab ingress
parses those verbs at all, following the ``adaptive_command_set()``
pattern (NXT-10): with ``FORGE_REVIEW_FEEDBACK_ENABLED`` unset (the
default) the verbs are not recognized — zero routing, the classic
workflow byte for byte — and with the flag on the note parser emits the
SAME durable run-command metadata the MR-bound ``/security`` surface
already produces (``note_id``, ``mr_iid``, ``discussion_id`` — the
metadata shape the worker's ``execute_run_command`` dispatch carries).

Scope honesty (the capability-manifest row reads the same bounds): ONE
native platform is qualified — GitLab MR notes, ingress
``forge.gateway.router`` → step runtime → ``RunService.run_command`` →
``run_reconciler``'s correction pass. GitHub and Azure DevOps parity is
deliberately NOT claimed until one native path is qualified (#337's
out-of-scope list); those ingresses never parse the verbs.
"""

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

#: R40-02 (#338): the operator policy bounding how many linked review
#: rounds a delivery lineage may open after ``ready_for_human``. The
#: window is bounded by construction (an unbounded correction ladder is a
#: budget drain, not a review); the default admits the review's own model
#: journey — delivery 1 plus ONE correction round — and a second
#: independent round needs an explicit operator raise.
FORGE_MAX_REVIEW_ROUNDS_ENV: Final = "FORGE_MAX_REVIEW_ROUNDS"

#: The default round bound (delivery 1 + 1 review round).
DEFAULT_MAX_REVIEW_ROUNDS: Final = 1

#: The closed bound the policy clamps into — a round count above this is
#: an operator typo, and a typo must narrow authority, never widen it
#: (the same fail-closed reading ``parse_tactical_policy`` gives an
#: unrecognized token).
_MAX_BOUND_REVIEW_ROUNDS: Final = 10

#: The review-feedback verbs — the note shapes
#: :func:`forge.adaptive.revisions.parse_review_feedback_note` accepts.
#: They are MR-discussion-bound (the Draft MR the candidate opened); an
#: issue-bound or commit-bound ``/fix``/``/ask`` is not a run command.
REVIEW_FEEDBACK_NOTE_COMMANDS: Final = frozenset({"/fix", "/ask"})

#: Truthy spellings — the same closed set ``FORGE_ADAPTIVE_COMMANDS_ENABLED``
#: uses; anything else fails CLOSED.
_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})


def review_feedback_commands_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether the review-feedback surface is turned on (default: off)."""
    source = os.environ if env is None else env
    return str(source.get(FORGE_REVIEW_FEEDBACK_ENV, "")).strip().lower() in _TRUTHY


def review_feedback_command_set(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """The feedback verbs a gateway may parse right now (empty when off).

    The gateway unions this into its accepted command set per delivery —
    the disabled default therefore means the verbs are not even
    recognized, which is what "zero routing" must mean (not
    parse-then-refuse).
    """
    return REVIEW_FEEDBACK_NOTE_COMMANDS if review_feedback_commands_enabled(env) else frozenset()


def max_review_rounds(env: Mapping[str, str] | None = None) -> int:
    """The bounded review-round policy (R40-02): how many post-readiness
    rounds a delivery lineage may open.

    ``FORGE_MAX_REVIEW_ROUNDS`` names the count; unset means the default
    (one round after the initial delivery). The value is CLAMPED into
    ``[0, 10]`` — zero disables the round route (the pre-#338 behavior,
    every post-readiness /fix answered ``correction_window_closed``), and
    anything above the closed bound reads as the bound: an operator typo
    narrows, never widens.
    """
    source = os.environ if env is None else env
    raw = str(source.get(FORGE_MAX_REVIEW_ROUNDS_ENV, "")).strip()
    if not raw:
        return DEFAULT_MAX_REVIEW_ROUNDS
    try:
        requested = int(raw)
    except ValueError:
        return DEFAULT_MAX_REVIEW_ROUNDS
    return max(0, min(requested, _MAX_BOUND_REVIEW_ROUNDS))
