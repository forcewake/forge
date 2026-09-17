"""Terminal-failure classification and the bounded auto-revive schedule.

Shared by every provider service (GitLab, GitHub, Azure DevOps — parity is a
contract). Terminalization time is where a run's failure is sorted into:

- **transient** — the environment tripped (dispatch 5xx, network/timeout,
  rate limit, runner startup, an empty-error harness dispatch). The run
  parks ``failed`` with a revival schedule in its evidence blob and the
  reconciler re-dispatches the SAME branch when due — no issue comment, no
  operator action.
- **fatal** — everything else (agent verdicts, 4xx config errors, exhausted
  budgets, unknown outcomes). Those need a decision, so the run parks
  ``blocked`` with the precise reason: terminal ``failed`` is reserved for
  failures the factory may still fix on its own.

The schedule is bounded: at most ``FORGE_RUN_AUTO_REVIVE_LIMIT`` revives per
run, each after the :func:`revival_due_at` backoff.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

#: Failure severity of a terminal ``failed`` reason.
TRANSIENT = "transient"
FATAL = "fatal"

FailureSeverity = str  # TRANSIENT | FATAL

#: Reason fragments that mean the *execution environment* failed — the same
#: branch re-dispatched may simply work: timeouts, lost connections, provider
#: rate limits, dead runners and harness jobs that never got going.
_TRANSIENT_MARKERS: tuple[str, ...] = (
    "timed out",
    "timeout",
    "connection",
    "network",
    "reset by peer",
    "broken pipe",
    "unreachable",
    "temporarily unavailable",
    "rate limit",
    "rate-limit",
    "runner_system_failure",
    "runner startup",
    "startup_failure",
)

#: Reason fragments that mean a *config* error a retry cannot fix — a 4xx
#: payload (the "Unexpected inputs" family), a missing/invalid workflow.
#: Checked first: an explicit cause outranks a transient fragment.
_FATAL_MARKERS: tuple[str, ...] = (
    "unexpected inputs",
    "unexpected input",
    "invalid request",
    "missing workflow",
    "no workflow",
    "invalid workflow",
    "workflow file",
    "not found",
    "validation error",
)

#: Terminal reasons raised by a *dispatch* leg. A dispatch that died is
#: transient unless it names a config error — the empty-error case
#: (``harness_start_failed: `` with nothing after the colon) is the live
#: shape of a provider hiccup.
_DISPATCH_LEG_MARKERS: tuple[str, ...] = ("harness_start_failed",)

#: 429 (rate limit) and every 5xx are retryable; other 4xx are not.
_API_STATUS_RE = re.compile(r"\bapi error\s+(\d{3})\b", re.IGNORECASE)

#: Evidence keys of the revival schedule (ADR-0005 evidence blob). ``count``
#: persists for the run's whole life (the bound is per run, not per episode);
#: ``at``/``reason`` are cleared when the revive fires.
REVIVE_AT = "revive_at"
REVIVE_COUNT = "revive_count"
REVIVE_REASON = "revive_reason"
#: Keys whose presence marks a run as *pending* revival.
_SCHEDULE_KEYS: tuple[str, ...] = (REVIVE_AT, REVIVE_REASON)

#: Backoff ceiling — a run never waits more than this for its revive.
_MAX_BACKOFF_SECONDS = 900


def classify_terminal_failure(reason: str) -> FailureSeverity:
    """Classify a terminal ``failed`` *reason* as transient or fatal.

    Priority: an explicit fatal cause > a dispatch-leg death > a retryable
    provider status > a transient fragment > fatal. The default is fatal —
    an unclassified failure needs a human, never an automatic retry.
    """
    lowered = (reason or "").lower()
    if any(marker in lowered for marker in _FATAL_MARKERS):
        return FATAL
    if any(marker in lowered for marker in _DISPATCH_LEG_MARKERS):
        return TRANSIENT
    for code in _API_STATUS_RE.findall(lowered):
        if code == "429" or code.startswith("5"):
            return TRANSIENT
    if any(marker in lowered for marker in _TRANSIENT_MARKERS):
        return TRANSIENT
    return FATAL


def revival_count(evidence: dict | None) -> int:
    """How many times *evidence*'s run has been revived (auto or operator)."""
    try:
        return int((evidence or {}).get(REVIVE_COUNT) or 0)
    except (TypeError, ValueError):
        return 0


def revival_state(evidence: dict | None) -> tuple[datetime | None, int]:
    """The ``(revive_at, revive_count)`` recorded on a run's evidence blob."""
    evidence = evidence or {}
    at = evidence.get(REVIVE_AT)
    due: datetime | None = None
    if isinstance(at, str) and at:
        try:
            due = datetime.fromisoformat(at)
        except ValueError:
            due = None
    if due is not None and due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return due, revival_count(evidence)


def revival_pending(evidence: dict | None) -> bool:
    """True when the run carries an un-fired revival schedule."""
    evidence = evidence or {}
    return all(evidence.get(key) for key in _SCHEDULE_KEYS)


def revival_due(evidence: dict | None, now: datetime) -> bool:
    """True when *evidence* schedules a revival at or before *now*."""
    due, _ = revival_state(evidence)
    if due is None:
        return False
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now >= due


def revival_due_at(now: datetime, count: int, backoff_seconds: int) -> datetime:
    """When a run with *count* used revives becomes due: bounded doubling.

    ``count`` is the revival attempt about to be scheduled (1 = first), so
    the first wait is the bare backoff, the second is double, and the wait
    never exceeds :data:`_MAX_BACKOFF_SECONDS`.
    """
    delay = min(max(backoff_seconds, 1) * (2 ** max(count - 1, 0)), _MAX_BACKOFF_SECONDS)
    return now + timedelta(seconds=delay)


def revival_evidence(at: datetime, count: int, reason: str) -> dict:
    """The evidence patch scheduling a revival (keys of the run blob)."""
    return {
        REVIVE_AT: at.astimezone(timezone.utc).isoformat(),
        REVIVE_COUNT: count,
        REVIVE_REASON: (reason or "")[:200],
    }


def strip_revival_schedule(evidence: dict | None) -> dict:
    """The evidence blob without the pending schedule (the count stays)."""
    merged = dict(evidence or {})
    for key in _SCHEDULE_KEYS:
        merged.pop(key, None)
    return merged


def revival_repair_context(status_reason: str | None, evidence: dict | None) -> str:
    """Why the run died plus its last verification — a revival's brief context.

    The terminal reason and whatever proof the dead attempt had accumulated
    (pipeline verdict, review) give the lane enough to fix forward on the
    same branch without re-deriving the task. Callers redact/cap it with the
    evidence policy before it rides a dispatch.
    """
    sections = [f"The previous attempt of this run died: {status_reason or 'unknown reason'}"]
    pipeline = (evidence or {}).get("pipeline")
    if isinstance(pipeline, dict) and pipeline.get("id"):
        sections.append(
            f"Last verification: pipeline {pipeline.get('id')}"
            f" ({pipeline.get('status')}) {str(pipeline.get('url') or '')}".strip()
        )
    review = (evidence or {}).get("review")
    if isinstance(review, dict) and review.get("summary"):
        sections.append(f"Last review verdict ({review.get('verdict')}): {review['summary']}")
    return "\n\n".join(sections)
