"""Terminal-failure classification and the bounded auto-revive policy.

A run that dies terminal is classified BEFORE the terminal status is
written, from the same operator-facing reason string:

- **transient** — transport-shaped noise (dispatch 5xx / network / timeout,
  rate limits, runner startup failures, empty dispatch errors). The run is
  revived automatically: it keeps its (resumable) status, arms ``revive``
  evidence — ``at`` (due time) + ``count`` — and the reconciler re-fires the
  frozen advance leg on the SAME branch once the backoff elapses. Bounded by
  ``FORGE_RUN_AUTO_REVIVE_LIMIT``; journaled as ``auto_revive`` actions
  (ADR-0005).
- **fatal** — a real quality signal or a configuration error (agent driver
  exit failed, 4xx "Unexpected inputs", missing workflow, cycles exhausted,
  cancelled). The run parks ``blocked`` — not ``failed`` — with the precise
  reason and no auto-retry; only the operator's ``/retry`` may revive it.

The revive wait is a *worker-free wait* exactly like ``waiting_ci``: the
run keeps the status it failed in (always one of
``RESUMABLE_ADVANCE_STATUSES``), so a revival re-enters the advance leg the
same way a crashed worker resumes one (ADR-0017 §3) — stages already left
behind are not re-entered, and the attempt base stays the last candidate
(ADR-0016 §4).

Shared by the GitLab, GitHub and Azure DevOps run services (parity: the
same classification table and the same limits everywhere).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum

#: The statuses an advance leg may be (re-)entered in without re-deriving
#: work (ADR-0017 §3 crash-resume entry points). A failure in any other
#: status has no fix-forward continuation, so it never arms a revival.
RESUMABLE_ADVANCE_STATUSES = frozenset(
    {"proposing", "validating", "committing", "ensuring_draft_mr"}
)


class FailureClass(str, Enum):
    """What a terminal failure reason blames."""

    TRANSIENT = "transient"
    FATAL = "fatal"


#: Ordered rules — the FIRST matching pattern (in the lower-cased reason)
#: decides. Fatal rules come first: a reason that carries both a config
#: marker and a transport marker is a configuration error we must not flap
#: on. Reasons are the ``prefix: detail`` strings ``_to_terminal`` writes.
_CLASSIFICATION_RULES: tuple[tuple[FailureClass, tuple[str, ...]], ...] = (
    (
        FailureClass.FATAL,
        (
            # A cancel that raced the failure is a human decision, final.
            "cancel",
            # The loop is spent / the contract failed — needs a human.
            "cycles_exhausted",
            "quality_contract",
            # Configuration errors: 4xx dispatch rejections, missing workflow.
            "unexpected inputs",
            "backend_config",
            "missing workflow",
            "no workflow",
            "workflow file",
            "401",
            "403",
            "404",
            "422",
            "unauthorized",
            "forbidden",
            "permission",
            # A write that MAY have landed — reconcile, never blind-retry.
            "unknown_outcome",
            "unknown outcome",
            # The agent driver exiting non-zero is a real quality signal.
            "driver exit",
            "exit code",
            "exit_failed",
        ),
    ),
    (
        FailureClass.TRANSIENT,
        (
            # Transport-shaped noise: 5xx, rate limits, timeouts, network.
            "timeout",
            "timed out",
            "connection",
            "network",
            "unreachable",
            "temporarily",
            "rate limit",
            "429",
            "500",
            "502",
            "503",
            "504",
            "internal server error",
            "bad gateway",
            "service unavailable",
            # Runner startup failures on the CI lanes.
            "runner",
            # Transport noise the harness lane could see (claude-code/agent
            # startup, sandbox/API flakes reported verbatim in the reason).
            "overloaded",
            "segmentation",
            "oom",
            "killed",
        ),
    ),
)

#: Dispatch prefixes whose EMPTY detail means the API died before it could
#: say why — transport noise, not a verdict.
_DISPATCH_PREFIXES = ("harness_start_failed", "commit_failed", "mr_failed")

#: Nothing matched: a failure we cannot blame on transport needs a human.
DEFAULT_CLASS = FailureClass.FATAL

#: Bounded backoff before the Nth auto-revive fires (1-based): 2 min, 4 min,
#: then capped — a dead endpoint is retried, not hammered.
REVIVE_BACKOFF_BASE_SECONDS = 120
REVIVE_BACKOFF_MAX_SECONDS = 900


def classify_terminal_failure(reason: str) -> FailureClass:
    """Classify the terminal reason *reason* (the ``_to_terminal`` string)."""
    lowered = (reason or "").strip().lower()
    for failure_class, patterns in _CLASSIFICATION_RULES:
        if any(pattern in lowered for pattern in patterns):
            return failure_class
    prefix, _, detail = lowered.partition(":")
    if prefix.strip() in _DISPATCH_PREFIXES and not detail.strip():
        return FailureClass.TRANSIENT
    return DEFAULT_CLASS


def revive_backoff_seconds(revive_count: int) -> int:
    """Seconds to wait before auto-revive number *revive_count* (1-based)."""
    delay = REVIVE_BACKOFF_BASE_SECONDS * (2 ** max(revive_count - 1, 0))
    return min(delay, REVIVE_BACKOFF_MAX_SECONDS)


def revive_evidence(evidence: dict | None) -> dict:
    """The run's ``revive`` evidence fragment (empty when none is armed)."""
    fragment = (evidence or {}).get("revive")
    return dict(fragment) if isinstance(fragment, dict) else {}


def revive_count(evidence: dict | None) -> int:
    """How many auto-revives the run has already spent."""
    try:
        return int(revive_evidence(evidence).get("count") or 0)
    except (TypeError, ValueError):
        return 0


def revive_at(evidence: dict | None) -> datetime | None:
    """When the armed auto-revive becomes due (None — never / unparsable)."""
    raw = str(revive_evidence(evidence).get("at") or "")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def arm_revive(evidence_count: int, reason: str, *, now: datetime | None = None) -> dict:
    """The evidence patch arming auto-revive number *evidence_count* + 1.

    *reason* is the terminal reason that triggered the revival — it rides
    the evidence so a later exhaustion (or ``/retry``) can quote it.
    """
    due = (now or datetime.now(timezone.utc)) + timedelta(
        seconds=revive_backoff_seconds(evidence_count + 1)
    )
    return {
        "revive": {
            "at": due.astimezone(timezone.utc).isoformat(),
            "count": evidence_count + 1,
            "reason": (reason or "")[:200],
        }
    }
