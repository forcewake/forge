"""Bounded admission and fair use (R28-23) — before preemptive scheduling.

More repositories and long interactive tasks increase queueing and
reviewer load; a custom fleet scheduler is NOT the first requirement.
Bounded admission is: controlled work-in-progress per project, a bounded
queue, a per-issue attempt cap and a per-user hourly rate — all enforced
BEFORE a run is admitted to (paid) planning, so a burst cannot
monopolize the fleet and a waiting human decision releases expensive
execution capacity instead of consuming it.

This module is the PURE half (mirrors :mod:`forge.runs.admission`, the
identity/authority half):

- :class:`AdmissionPolicy` — the numeric bounds, loaded from env
  (:meth:`AdmissionPolicy.from_env`); every dimension follows the repo
  convention that ``0`` (or negative) DISABLES it.
- :func:`check_admission` — the pure decision over the four live counts.
  No clocks, no I/O, no randomness: same inputs → same decision, so the
  counts are gathered by the caller (the service) and the decision is
  explainable after the fact from its snapshot.

Refusals are TYPED (:class:`RefusalReason`) and the decision carries the
observed counts plus the policy that judged them — "the operator can
explain why a task is queued" is an acceptance criterion, not a nice-to-
have. The check order is fixed and documented: per-issue, per-user,
per-project WIP, queue depth — the most specific bound refuses first.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "AdmissionDecision",
    "AdmissionPolicy",
    "RefusalReason",
    "check_admission",
]


class RefusalReason(str, Enum):
    """The typed fair-use refusals, most specific first (check order)."""

    #: The issue already consumed its run budget — repeated /implement on
    #: one issue is a repair signal, not more runs (ADR-0008).
    ISSUE_RUN_LIMIT = "issue_run_limit"
    #: The requesting actor hit the hourly fair-use rate across issues.
    USER_RATE_LIMIT = "user_rate_limit"
    #: The project's work-in-progress bound: one project burst cannot
    #: monopolize all admitted work indefinitely.
    PROJECT_ACTIVE_LIMIT = "project_active_limit"
    #: The queue is at capacity — admit nothing until work drains.
    QUEUE_FULL = "queue_full"


#: The env names :meth:`AdmissionPolicy.from_env` reads (ints; a value
#: that does not parse raises — a typo must never silently become the
#: default, the same fail-closed posture as the harness pin tables).
ENV_MAX_ACTIVE_PER_PROJECT = "FORGE_ADMISSION_MAX_ACTIVE_PER_PROJECT"
ENV_MAX_QUEUED_RUNS = "FORGE_ADMISSION_MAX_QUEUED_RUNS"
ENV_MAX_RUNS_PER_ISSUE = "FORGE_ADMISSION_MAX_RUNS_PER_ISSUE"
ENV_USER_RUNS_PER_HOUR = "FORGE_ADMISSION_USER_RUNS_PER_HOUR"


@dataclass(frozen=True)
class AdmissionPolicy:
    """The fair-use bounds (R28-23). Defaults are deliberately small: a
    project at capacity parks new work instead of queueing it forever.

    ``max_active_per_project`` — concurrent NON-terminal, executing runs
    per project (controlled WIP; 3 by default).
    ``max_queued_runs`` — runs admitted but not yet executing (accepted,
    preflight, planning, waiting_approval) per project; 10 by default.
    ``max_runs_per_issue`` — total runs ever per (project, issue),
    terminal included; 5 by default.
    ``max_user_runs_per_hour`` — runs the same requesting actor started
    in the trailing hour, project-wide; 6 by default.

    A limit ``<= 0`` disables that dimension (the auto-revive convention:
    explicit opt-out, never a silent zero-budget brick).
    """

    max_active_per_project: int = 3
    max_queued_runs: int = 10
    max_runs_per_issue: int = 5
    max_user_runs_per_hour: int = 6

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> AdmissionPolicy:
        """Load the policy from *environ* (the process env by default).

        Fail-closed on junk: a non-integer value raises ``ValueError``
        naming the variable — admission bounds are safety bounds, and a
        typo'd bound silently becoming the default is exactly the drift
        this module exists to prevent.
        """

        source = os.environ if environ is None else environ

        def _limit(name: str, default: int) -> int:
            raw = str(source.get(name, "")).strip()
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise ValueError(f"{name} must be an integer, got {raw!r}") from exc

        return cls(
            max_active_per_project=_limit(ENV_MAX_ACTIVE_PER_PROJECT, 3),
            max_queued_runs=_limit(ENV_MAX_QUEUED_RUNS, 10),
            max_runs_per_issue=_limit(ENV_MAX_RUNS_PER_ISSUE, 5),
            max_user_runs_per_hour=_limit(ENV_USER_RUNS_PER_HOUR, 6),
        )


@dataclass(frozen=True)
class AdmissionDecision:
    """The fair-use verdict for ONE admission attempt, with the evidence
    needed to explain it: the counts observed, the policy that judged
    them, and — when refused — the typed :class:`RefusalReason` and the
    human sentence an operator or an issue comment can quote verbatim."""

    allowed: bool
    reason: str
    refusal: RefusalReason | None
    policy: AdmissionPolicy
    counts: dict[str, int]

    def as_document(self) -> dict:
        """The JSON-shape record for evidence/journal surfaces."""
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "refusal": self.refusal.value if self.refusal is not None else None,
            "policy": {
                "max_active_per_project": self.policy.max_active_per_project,
                "max_queued_runs": self.policy.max_queued_runs,
                "max_runs_per_issue": self.policy.max_runs_per_issue,
                "max_user_runs_per_hour": self.policy.max_user_runs_per_hour,
            },
            "counts": dict(self.counts),
        }


#: The pre-execution statuses a QUEUED run sits in — a run waiting for a
#: human gate decision holds QUEUE capacity, never ACTIVE capacity ("a
#: waiting human decision releases expensive execution capacity").
QUEUED_STATUSES: frozenset[str] = frozenset(
    {"accepted", "preflight", "planning", "waiting_approval"}
)


def check_admission(
    policy: AdmissionPolicy,
    active_count: int,
    queued_count: int,
    issue_run_count: int,
    user_recent_count: int,
) -> AdmissionDecision:
    """Decide one admission attempt against the fair-use bounds (pure).

    The counts describe the world WITHOUT the candidate run (the caller
    gathers them before creation): *active_count* executing runs in the
    project, *queued_count* admitted-but-not-executing runs, *issue_run_count*
    total runs ever for this issue, *user_recent_count* runs the same
    actor started in the trailing hour.

    A limit ``<= 0`` disables its dimension. Checks run most-specific
    first — per-issue, per-user, per-project WIP, queue depth — so the
    refusal an operator sees names the tightest bound that actually
    refused. The decision is a total function of its inputs: identical
    counts and policy always produce the identical verdict.
    """

    counts = {
        "active": active_count,
        "queued": queued_count,
        "issue_runs": issue_run_count,
        "user_recent": user_recent_count,
    }

    def _refuse(reason: RefusalReason, limit: int, observed: int) -> AdmissionDecision:
        return AdmissionDecision(
            allowed=False,
            reason=(
                f"fair use: {reason.value} — {observed} against the limit of "
                f"{limit}; retry after the existing work drains, or ask an "
                "operator to raise the bound"
            ),
            refusal=reason,
            policy=policy,
            counts=counts,
        )

    if policy.max_runs_per_issue > 0 and issue_run_count >= policy.max_runs_per_issue:
        return _refuse(RefusalReason.ISSUE_RUN_LIMIT, policy.max_runs_per_issue, issue_run_count)
    if policy.max_user_runs_per_hour > 0 and user_recent_count >= policy.max_user_runs_per_hour:
        return _refuse(
            RefusalReason.USER_RATE_LIMIT, policy.max_user_runs_per_hour, user_recent_count
        )
    if policy.max_active_per_project > 0 and active_count >= policy.max_active_per_project:
        return _refuse(
            RefusalReason.PROJECT_ACTIVE_LIMIT, policy.max_active_per_project, active_count
        )
    if policy.max_queued_runs > 0 and queued_count >= policy.max_queued_runs:
        return _refuse(RefusalReason.QUEUE_FULL, policy.max_queued_runs, queued_count)
    return AdmissionDecision(
        allowed=True,
        reason="admitted within fair-use bounds",
        refusal=None,
        policy=policy,
        counts=counts,
    )
