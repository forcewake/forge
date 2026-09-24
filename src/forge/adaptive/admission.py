"""Bounded admission and fair use (R28-23, NEXT-11/NEXT-12) — before
preemptive scheduling.

More repositories and long interactive tasks increase queueing and
reviewer load; a custom fleet scheduler is NOT the first requirement.
Bounded admission is: controlled work-in-progress per project, a bounded
queue, a per-issue attempt cap and a per-user hourly rate — all enforced
BEFORE a run is admitted to (paid) planning, so a burst cannot
monopolize the fleet and a waiting human decision releases expensive
execution capacity instead of consuming it.

This module is the PURE decision half (mirrors :mod:`forge.runs.admission`,
the identity/authority half):

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

NEXT-11 adds the DURABLE half: the queue-admission check above counts
ACTIVE runs at ``/implement`` time, but the execution slot is only
reserved when work is DISPATCHED (``/go``) — four tasks can all pass
admission while the gate is quiet and then all activate at once.
:class:`ExecutionLease` is the reservation: a durable per-project slot
row taken by compare-and-set INSERT at dispatch, held until the run
reaches a terminal status, released explicitly or reclaimed lazily when
the next acquirer observes its run terminal (a crashed worker can
therefore never permanently leak capacity). Two workers racing for the
final slot produce exactly one winner — the database's unique
open-slot index decides, not a read-then-write.

NEXT-12 separates the fair-use accounting: a request REFUSED at
admission never entered (and never consumed an execution attempt),
admitted work waits in the queue, and execution attempts are the leases
actually held or completed — :func:`admission_report` returns the three
counters over the same durable rows, so an operator never again reads a
queue-full refusal as an execution attempt.

R32-06 (review 0fca1b7) closes the second hole in the CAS: the
open-slot index alone still let ONE run hold TWO slots — two acquirers
both observe no existing lease for the run, one wins slot 1, the loser
of slot 1 legally wins slot 2. The idempotency unit is the RUN: the
partial unique index ``uq_execution_lease_open_run`` over ``run_id``
WHERE ``released_at IS NULL`` makes "one OPEN lease per run" a database
invariant, and :func:`try_acquire_lease` answers the run conflict by
reading the existing winner back (idempotent success), while an
UNRELATED :class:`IntegrityError` is refused loudly instead of being
read as "try the next slot forever".

R32-07 (review 0fca1b7, same review) separates the RESERVATION's
lifetime from the run's LOCAL status: a FlowRun reaching terminal says
the CONTROL PLANE is done with the work, not that the dispatched native
job (the CI pipeline, the runner) stopped burning capacity — releasing
the slot at the local transition could overbook the fleet while the
native job still runs. The lease therefore carries a ``native_handle``
(the dispatched job's id, recorded at dispatch via
:func:`record_native_handle`) and a DRAINING state:
:func:`release_lease` with ``native_completed=False`` parks the lease
draining — ``draining_at`` set, ``released_at`` still NULL, so the
partial open indexes keep holding the slot — and
:func:`reconcile_draining` (the reconciler's periodic call) probes the
native job through an INJECTED provider callable and releases only
leases whose native jobs are observed terminal. Absence and unknown
stay distinct: a probe that raises (provider outage) leaves the lease
draining — uncertain occupancy holds capacity, never silently frees it.

Q35-04 (review c7ae8db) closes the binding gap the review found: R32-07
built the draining machinery but NO production dispatch ever recorded a
native correlation, and every provider release wrapper released with
``native_completed=True`` — the capacity limit was a RESERVATION
guarantee, not an OCCUPANCY guarantee. A provider that accepted the
start request and then lost the response (worker death in between) left
the slot free on local status while the native job ran. The fix is the
native-start INTENT: :func:`record_native_start_intent` persists
``native_intent_at`` / ``native_intent_ref`` BEFORE the provider call
(every dispatch leg), :func:`record_native_handle` attaches the id the
provider answers with, and occupancy is a DERIVED state
(:func:`lease_occupancy`)::

    never_dispatched   native_intent_at IS NULL
    dispatched_unknown native_intent_at set, native_handle NULL, not terminal
    native_running     native_handle set, not terminal
    draining           draining_at set, released_at NULL
    observed_terminal  released_at set

A slot frees ONLY from evidence (:func:`release_lease_with_evidence`):
an observed native-terminal verdict, a PROVEN never-dispatched intent
(no intent was ever persisted — the builtin lane, a pre-call abort), or
an explicit audited override. A dispatched intent with no handle is
NOT empty capacity: it parks draining and the reconciler probes it by
its ``native_intent_ref`` prefix through the probe registry
(:func:`register_native_probe`); an undecidable probe keeps the slot
held with its draining age visible.
"""

from __future__ import annotations

import inspect
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from forge.models.base import Base

__all__ = [
    "AdmissionDecision",
    "AdmissionPolicy",
    "ExecutionLease",
    "Lease",
    "LeaseOccupancy",
    "LeaseRelease",
    "NativeProbe",
    "NativeStatus",
    "RefusalReason",
    "admission_report",
    "check_admission",
    "clear_native_start_intent",
    "definite_start_refusal",
    "execution_capacity_comment",
    "lease_conflict_kind",
    "lease_occupancy",
    "lease_snapshot",
    "native_probe_for",
    "occupancy_report",
    "reconcile_draining",
    "record_native_handle",
    "record_native_start_intent",
    "register_native_probe",
    "release_lease",
    "release_lease_with_evidence",
    "release_run_leases",
    "saturation_report",
    "try_acquire_lease",
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
        # NEXT-12: the lifetime per-issue cap must NOT suggest that
        # draining unrelated work would reset it — only the three
        # capacity dimensions (per-user rate, project WIP, queue depth)
        # recover on their own as work drains.
        draining_helps = reason is not RefusalReason.ISSUE_RUN_LIMIT
        guidance = (
            "retry after the existing work drains, or ask an operator to raise the bound"
            if draining_helps
            else (
                "this is a lifetime per-issue limit — draining other work will not "
                "reset it; ask an operator to raise the bound or continue on a new "
                "issue"
            )
        )
        return AdmissionDecision(
            allowed=False,
            reason=(
                f"fair use: {reason.value} — {observed} against the limit of {limit}; {guidance}"
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


# ---------------------------------------------------------------------------
# NEXT-11: durable execution leases — the slot reservation at dispatch
# ---------------------------------------------------------------------------

#: The partial-index predicate making "one OPEN lease per (project, slot)"
#: a database invariant: the unique index below fires ONLY while the
#: lease is held (``released_at IS NULL``), so a released slot is
#: immediately reusable while its row stays as the completed-attempt
#: audit trail (NEXT-12's ``execution_attempts.completed`` counter).
_LEASE_OPEN = text("released_at IS NULL")

#: Q35-04: the occupancy watchlist — OPEN leases that carry a native
#: start intent (dispatched, verdict unknown). The reconciler and the
#: operator's occupancy report scan this, not the audit trail.
_INTENT_OPEN = text("released_at IS NULL AND native_intent_at IS NOT NULL")


def _utcnow() -> datetime:
    return datetime.now(UTC)


#: The two CAS constraints an INSERT into ``execution_leases`` can lose:
#: the open-SLOT race (another run took this slot) and the open-RUN race
#: (THIS run already holds a slot). Everything else is an unrelated
#: constraint violation and must be refused, never retried (R32-06).
_SLOT_CONSTRAINT = "uq_execution_lease_slot"
_RUN_CONSTRAINT = "uq_execution_lease_open_run"


def lease_conflict_kind(exc: BaseException) -> str:
    """Classify an :class:`IntegrityError` from a lease INSERT.

    Returns :data:`_SLOT_CONSTRAINT` (the loser of a slot race — the next
    slot may be tried), :data:`_RUN_CONSTRAINT` (the run already holds an
    open lease — the existing winner is the answer) or ``""`` (unrelated
    — raise it, never mine it for retry signal).

    Best-effort by necessity: PostgreSQL names the violated index in the
    message and in ``orig.diag.constraint_name``; SQLite reports the
    violated column list for a partial unique index. The slot index
    covers ``(project_id, provider, slot)``; the open-run index covers
    ``(run_id)`` alone — the column set distinguishes them.
    """
    message = f"{exc}"
    for name in (_SLOT_CONSTRAINT, _RUN_CONSTRAINT):
        if name in message:
            return name
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    constraint = getattr(diag, "constraint_name", None)
    if constraint in (_SLOT_CONSTRAINT, _RUN_CONSTRAINT):
        return constraint
    if "UNIQUE constraint failed" in message:
        if "execution_leases.run_id" in message and "execution_leases.slot" not in message:
            return _RUN_CONSTRAINT
        if "execution_leases.slot" in message:
            return _SLOT_CONSTRAINT
    return ""


class ExecutionLease(Base):
    """ONE reserved execution slot for a project (NEXT-11), durable.

    The queue-admission check counts ACTIVE runs at ``/implement`` time,
    but four waiting approvals can later activate together — a count
    read before creation is not a reservation. This row IS the
    reservation: acquired at dispatch (``/go``) through a
    compare-and-set INSERT against the unique OPEN-slot index
    (``uq_execution_lease_slot`` over ``(project_id, provider, slot)``
    WHERE ``released_at IS NULL``) and — since R32-06 — the unique
    OPEN-run index (``uq_execution_lease_open_run`` over ``run_id``),
    held until the run's terminal status, released explicitly
    (:func:`release_lease`) or reclaimed lazily by the next acquirer
    that observes its run terminal — a worker death leaks capacity only
    until the next dispatch in the same project.

    Since Q35-04 the reservation is bound to OBSERVED native occupancy:
    ``native_intent_at``/``native_intent_ref`` persist the native-start
    intent BEFORE the provider call, ``native_handle`` attaches the id
    the provider answers with, and :func:`lease_occupancy` derives the
    five occupancy states from those columns — a slot frees from
    evidence (:func:`release_lease_with_evidence`), never from the
    run's local status alone.

    ``slot`` is the CAS key, not a scheduler decision: acquirers try
    1..max_active_per_project until one INSERT wins; the winner of a
    race is chosen by the database, never by a read-then-write. With
    ``max_active_per_project <= 0`` the dimension is disabled and the
    slot loop is unbounded (leases are still recorded — the accounting
    stays honest — but nothing is ever refused).

    ``provider`` is part of the CAS identity (NEXT-12's canonical
    connection identity): ``project_id`` is provider-local — the same
    number on two connections names two different real projects, and
    their capacities never share a pot.
    """

    __tablename__ = "execution_leases"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    #: The canonical connection identity (NEXT-12): same numbers on two
    #: connections stay independent aggregates — it keys the CAS index.
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    slot: Mapped[int] = mapped_column(Integer, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    #: Q35-04: the native-start INTENT — set BEFORE the provider call
    #: (:func:`record_native_start_intent`), NULL when no native job was
    #: ever dispatched (the builtin lane, a pre-call abort). NULL is the
    #: PROOF of never-dispatched occupancy: the only evidence that may
    #: free a slot without observing the provider.
    native_intent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    #: Q35-04: the correlation marker of the intended start (short,
    #: provider-shaped: ``github:workflow:<owner>/<repo>/<workflow>@<branch>``,
    #: ``gitlab:pipeline:<project>@<branch>``,
    #: ``azure:pipeline:<project>:<pipeline>@<branch>``). The prefix
    #: before the first ``:`` routes :func:`reconcile_draining` to the
    #: owning provider's probe when the handle was never recorded.
    native_intent_ref: Mapped[str | None] = mapped_column(String(200), nullable=True, default=None)
    #: R32-07: the dispatched native job's correlation (pipeline/run id).
    #: NULL until the provider answers dispatch with an id
    #: (:func:`record_native_handle`); the reconciler's probe key.
    native_handle: Mapped[str | None] = mapped_column(String(256), nullable=True, default=None)
    #: R32-07: set when the RUN reached its local terminal verdict but the
    #: NATIVE job was still running (``release_lease(...,
    #: native_completed=False)``). A draining lease still HOLDS its slot
    #: (``released_at`` stays NULL, the open indexes keep firing) until
    #: :func:`reconcile_draining` observes the native job terminal.
    draining_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    release_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)

    __table_args__ = (
        CheckConstraint("slot >= 1", name="ck_execution_leases_slot"),
        Index(
            "uq_execution_lease_slot",
            "project_id",
            "provider",
            "slot",
            unique=True,
            sqlite_where=_LEASE_OPEN,
            postgresql_where=_LEASE_OPEN,
        ),
        # R32-06: the idempotency unit is the RUN — one OPEN lease per
        # run_id, alongside the slot uniqueness. Without it the legal SQL
        # schedule "both pre-reads miss; A wins slot 1; B loses slot 1 and
        # wins slot 2" leaves ONE run holding TWO slots. ``run_id`` is
        # nullable (a lease may be taken anonymously); NULLs are distinct
        # in both SQLite and PostgreSQL unique indexes, so anonymous rows
        # never collide.
        Index(
            "uq_execution_lease_open_run",
            "run_id",
            unique=True,
            sqlite_where=_LEASE_OPEN,
            postgresql_where=_LEASE_OPEN,
        ),
        # Q35-04: the occupancy watchlist — open leases whose dispatch
        # intent is live (dispatched, native verdict unknown). Reconciler
        # scans start here instead of sweeping the audit trail.
        Index(
            "ix_execution_leases_native_intent",
            "native_intent_at",
            sqlite_where=_INTENT_OPEN,
            postgresql_where=_INTENT_OPEN,
        ),
    )


# ---------------------------------------------------------------------------
# Q35-04: occupancy — the slot's DERIVED state, from evidence columns only
# ---------------------------------------------------------------------------


class LeaseOccupancy(str, Enum):
    """The five occupancy states of a lease (Q35-04), DERIVED — never
    stored: the columns are facts (intent persisted, handle attached,
    drain parked, release landed) and occupancy is their reading.

    ``never_dispatched`` — no native-start intent was ever persisted:
    the builtin lane (forge's own worker is the occupant) or a dispatch
    that aborted before the provider call. The ONLY occupancy a local
    terminal verdict may free on its own.
    ``dispatched_unknown`` — the provider call was intended and may
    have been accepted, but no handle came back (lost response, worker
    death in between). NOT empty capacity.
    ``native_running`` — the provider answered with a handle; the job
    occupies its slot until observed terminal.
    ``draining`` — the run is locally terminal but occupancy is
    unresolved: the slot stays held pending the reconciler's probe.
    ``observed_terminal`` — released: the terminal evidence landed
    (native terminal observed, proven never-dispatched, or override).
    """

    NEVER_DISPATCHED = "never_dispatched"
    DISPATCHED_UNKNOWN = "dispatched_unknown"
    NATIVE_RUNNING = "native_running"
    DRAINING = "draining"
    OBSERVED_TERMINAL = "observed_terminal"


def lease_occupancy(row: ExecutionLease) -> LeaseOccupancy:
    """Derive one lease's occupancy from its evidence columns (pure)."""

    if row.released_at is not None:
        return LeaseOccupancy.OBSERVED_TERMINAL
    if row.draining_at is not None:
        return LeaseOccupancy.DRAINING
    if row.native_intent_at is None:
        return LeaseOccupancy.NEVER_DISPATCHED
    if row.native_handle:
        return LeaseOccupancy.NATIVE_RUNNING
    return LeaseOccupancy.DISPATCHED_UNKNOWN


class NativeStatus(str, Enum):
    """What a native-occupancy probe OBSERVED about one native job.

    ``TERMINAL`` — the provider answered and the job is finished: the
    slot may free. ``RUNNING`` — the provider answered and the job is
    active: the slot is genuinely occupied. ``UNKNOWN`` — no decision
    (undecidable correlation, provider outage, a foreign provider's
    key): uncertain occupancy HOLDS the slot, never frees it.
    """

    TERMINAL = "terminal"
    RUNNING = "running"
    UNKNOWN = "unknown"


#: One provider's occupancy probe: maps a lease's PROBE KEY (the
#: ``native_handle`` once recorded, else the ``native_intent_ref``) to
#: its observed :class:`NativeStatus`. Sync or async; may raise — a
#: raising probe reads as UNKNOWN (keep holding). Bool results are
#: accepted for back-compat (``True`` → terminal, ``False`` → running).
NativeProbe = Callable[[str], "bool | NativeStatus | Awaitable[bool | NativeStatus]"]

#: The probe registry, keyed by the probe key's provider prefix (the
#: text before the first ``:`` — ``github``, ``gitlab``, ``azure``).
#: Each service registers its probe; :func:`reconcile_draining` without
#: an explicit callable routes each lease to its owning provider.
_NATIVE_PROBES: dict[str, NativeProbe] = {}


def register_native_probe(prefix: str, probe: NativeProbe) -> None:
    """Register *probe* for the provider whose probe keys start *prefix*.

    The reconciler's routing table: a lease whose key is
    ``gitlab:pipeline:123`` is probed by the ``gitlab`` probe, never by
    the GitHub client that happens to tick first. Re-registering a
    prefix replaces the previous probe (services are rebuilt per
    reconciler pass).
    """

    _NATIVE_PROBES[str(prefix)] = probe


def native_probe_for(key: str) -> NativeProbe | None:
    """The probe owning *key* (by its provider prefix), or None."""

    prefix = key.split(":", 1)[0]
    return _NATIVE_PROBES.get(prefix)


def _coerce_native_status(observed: bool | NativeStatus) -> NativeStatus:
    if isinstance(observed, NativeStatus):
        return observed
    return NativeStatus.TERMINAL if observed else NativeStatus.RUNNING


#: Client-error statuses that do NOT mean "the server may have accepted
#: the request anyway": 408/425/429 are retryable conditions where the
#: start may have landed (or the request was never sent — undecidable),
#: so they stay ambiguous like 5xx and transport errors.
_RETRYABLE_CLIENT_STATUSES = frozenset({408, 425, 429})


def definite_start_refusal(status_code: int) -> bool:
    """Whether a provider start call's HTTP *status_code* PROVES the
    start was refused before acceptance (Q35-04 evidence classifier).

    A definitive non-retryable client error (4xx except 408/425/429)
    means the provider rejected the request — no native job exists, so
    the dispatch leg may :func:`clear_native_start_intent` and return
    capacity immediately. Everything else — 5xx (the server may have
    accepted before failing), the retryable client statuses, and
    transport errors (no status at all) — is ambiguous: the intent
    STAYS and the reconciler's probe decides the occupancy.
    """

    return 400 <= status_code < 500 and status_code not in _RETRYABLE_CLIENT_STATUSES


async def _probe_status(probe: NativeProbe, key: str) -> NativeStatus:
    """Run one probe (sync or async); a raise reads as UNKNOWN."""

    try:
        observed = probe(key)
        if inspect.isawaitable(observed):
            observed = await observed
    except Exception:  # noqa: BLE001 — outage/unknown: keep holding
        return NativeStatus.UNKNOWN
    return _coerce_native_status(observed)


@dataclass(frozen=True)
class Lease:
    """The in-memory handle of an acquired :class:`ExecutionLease` row.

    Carries the identity needed to release the slot later
    (:func:`release_lease`) and the evidence a run journals when its
    admission check ran at dispatch (:meth:`as_document`).
    ``native_handle`` (R32-07) is the dispatched native job's id once
    the provider answered — empty until :func:`record_native_handle`
    lands it (dispatch takes the slot BEFORE the pipeline exists).
    """

    lease_id: str
    project_id: int
    slot: int
    run_id: str
    provider: str
    acquired_at: datetime
    native_handle: str = ""

    def as_document(self) -> dict:
        """The JSON-shape record for run evidence and refusal snippets."""
        return {
            "lease_id": self.lease_id,
            "project_id": self.project_id,
            "slot": self.slot,
            "run_id": self.run_id,
            "provider": self.provider,
            "acquired_at": self.acquired_at.isoformat(),
            "native_handle": self.native_handle or None,
        }


def execution_capacity_comment(run_id: str, snapshot: dict) -> str:
    """The parked-at-dispatch operator note (R32-05) — the ONE renderer
    every provider's dispatch choke point posts when capacity refuses.

    Capacity, not refusal: the run was approved and stays parked in a
    queued state with the precise next action; a waiting human decision
    never holds an execution slot and a capacity wait never consumed one.
    """
    held = snapshot.get("held")
    limit = snapshot.get("limit")
    capacity = f"{held} of {limit} slots held" if limit is not None else f"{held} slots held"
    return (
        "## Forge — run parked at dispatch\n\n"
        f"Run `{run_id[:8]}` was approved but is **parked**: no execution slot is free "
        f"in this project ({capacity}). Nothing is executing for it.\n\n"
        f"- Retry when the running work drains: `/retry {run_id}`\n"
        "- An operator can raise the bound via `FORGE_ADMISSION_MAX_ACTIVE_PER_PROJECT`\n\n"
        "*This is an automated message.*"
    )


def _lease_of(row: ExecutionLease) -> Lease:
    return Lease(
        lease_id=row.id,
        project_id=int(row.project_id),
        slot=int(row.slot),
        run_id=str(row.run_id or ""),
        provider=str(row.provider or ""),
        acquired_at=row.acquired_at,
        native_handle=str(row.native_handle or ""),
    )


async def _reclaim_terminal_run_leases(
    session: AsyncSession, project_id: int, *, provider: str, now: datetime
) -> int:
    """Reclaim OPEN leases whose runs are already terminal (NEXT-11) —
    by OCCUPANCY, never by local status alone (Q35-04).

    The crash backstop: a worker that died between dispatch and release
    leaves an open lease behind; the run row it names eventually reaches
    a terminal status (or is parked by the reconciler), and the NEXT
    acquirer in this project reclaims the slot instead of leaking it
    forever. What "reclaim" means now depends on the lease's evidence:

    - a DRAINING lease already knows its run is terminal — it waits on
      the NATIVE job, and only the reconciler's probe may release it;
    - a lease with a live native-start INTENT (Q35-04) may have a native
      job still running (the worker died between the provider accepting
      the start and the handle landing) — it is PARKED draining for the
      reconciler, never released on the local verdict;
    - only a lease with NO intent — proven never dispatched — releases
      here: nothing native ever occupied the slot.

    Returns how many leases were reclaimed (released or parked).
    """
    from forge.durable import FlowRun
    from forge.durable.controller import TERMINAL_STATUSES

    terminal = {status.value for status in TERMINAL_STATUSES}
    reclaimed = 0
    rows = (
        (
            await session.execute(
                select(ExecutionLease).where(
                    ExecutionLease.project_id == project_id,
                    ExecutionLease.released_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        if provider and row.provider and row.provider != provider:
            continue  # a different connection's lease: not ours to judge
        if row.draining_at is not None:
            # R32-07: a DRAINING lease already knows its run is terminal —
            # it is waiting on the NATIVE job, and only the reconciler's
            # probe may release it. The terminal-run reclaim is for leases
            # whose release never ran at all, never a way around draining.
            continue
        if not row.run_id:
            continue
        run = await session.get(FlowRun, row.run_id)
        if run is None or run.status not in terminal:
            continue
        if row.native_intent_at is None:
            # Q35-04: PROVEN never dispatched — the local terminal
            # verdict is the whole truth; the slot returns now.
            row.released_at = now
            row.release_reason = f"reclaimed: run {run.status}"
            reclaimed += 1
            continue
        # Q35-04: a dispatched intent (handle or not) may still be
        # occupying a runner — park it draining for the reconciler's
        # probe. AT-04: the terminal-run reclaim cannot bypass this.
        row.draining_at = now
        row.release_reason = f"reclaimed: run {run.status} — native occupancy unobserved"
        reclaimed += 1
    if reclaimed:
        await session.commit()
    return reclaimed


async def try_acquire_lease(
    policy: AdmissionPolicy,
    project_id: int,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: str = "",
    provider: str = "",
    native_handle: str = "",
    now: datetime | None = None,
) -> Lease | None:
    """Reserve ONE execution slot for *project_id* — or lose honestly.

    The NEXT-11 reservation, taken at DISPATCH (``/go``), not at queue
    admission: first any OPEN lease whose run is already terminal is
    reclaimed (worker-death recovery), then slots ``1..limit`` are tried
    by compare-and-set INSERT against the unique OPEN-slot index — two
    workers racing for the final slot produce exactly one winner because
    the database rejects the loser's INSERT, not because of a count read
    before it. Returns the :class:`Lease` handle, or ``None`` when the
    project holds its full complement (the caller parks the run
    ``blocked(execution_capacity)`` — a queued state with a reason,
    never work an observer must later stop).

    ``max_active_per_project <= 0`` disables the refusal: the loop runs
    unbounded, so leases are still recorded (honest accounting) but a
    slot is always granted.

    Idempotent per run, at BOTH layers (R32-06): an OPEN lease naming
    *run_id* is returned as-is by the pre-read, and — when the pre-read
    raced a concurrent acquire — the database's unique OPEN-RUN index
    refuses the second reservation and the loser reads the winner back.
    A re-driven dispatch (a revival, a reconciler recovery) therefore
    holds exactly one slot, never one per racing driver.

    Attempt-replacement policy (Q35-04, recorded so it is a decision,
    not an accident): a later attempt for the SAME run id REUSES the
    held reservation — the pre-read above — including its occupancy
    evidence (intent/handle/draining). A re-dispatch never stacks a
    second lease for the same run and never wipes the occupancy the
    previous attempt recorded.
    """
    moment = now or _utcnow()
    limit = policy.max_active_per_project
    async with session_factory() as session:
        await _reclaim_terminal_run_leases(session, project_id, provider=provider, now=moment)
        if run_id:
            existing = (
                (
                    await session.execute(
                        select(ExecutionLease).where(
                            ExecutionLease.project_id == project_id,
                            ExecutionLease.run_id == run_id,
                            ExecutionLease.released_at.is_(None),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if existing is not None:
                return _lease_of(existing)
        slot = 1
        run_conflict_misses = 0
        while limit <= 0 or slot <= limit:
            row = ExecutionLease(
                id=uuid4().hex,
                project_id=project_id,
                provider=provider,
                run_id=run_id or None,
                slot=slot,
                acquired_at=moment,
                native_handle=native_handle or None,
            )
            session.add(row)
            try:
                await session.commit()
                return _lease_of(row)
            except IntegrityError as exc:
                await session.rollback()
                conflict = lease_conflict_kind(exc)
                if conflict == _RUN_CONSTRAINT:
                    # R32-06: THIS run already holds a slot — the open-run
                    # index refused the double reservation. The existing
                    # winner IS the reservation (idempotent success), not a
                    # signal to consume another slot.
                    winner = (
                        (
                            await session.execute(
                                select(ExecutionLease).where(
                                    ExecutionLease.run_id == run_id,
                                    ExecutionLease.released_at.is_(None),
                                )
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if winner is not None:
                        return _lease_of(winner)
                    # The winner released between our refused INSERT and
                    # this read — retry the insert on the SAME slot. Bounded:
                    # a winner that keeps vanishing is not a race shape a
                    # real schedule produces; fail loudly after three tries.
                    run_conflict_misses += 1
                    if run_conflict_misses >= 3:
                        raise
                    continue
                if conflict != _SLOT_CONSTRAINT:
                    # R32-06: an unrelated constraint violation is never a
                    # "busy slot" — retrying the next slot forever (the
                    # disabled-limit mode loops unbounded) would hide it.
                    raise
                slot += 1  # this slot lost the race — next slot
        return None


async def open_lease_for_run(
    session_factory: async_sessionmaker[AsyncSession], run_id: str
) -> Lease | None:
    """The run's OPEN lease, if any (the idempotent pre-read spelling).

    The dispatch legs' helper: after :func:`try_acquire_lease` reserved
    the slot, this re-reads the same row (the open-run index guarantees
    at most one) so the leg can attach the native handle without a
    second reservation.
    """

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    select(ExecutionLease).where(
                        ExecutionLease.run_id == run_id,
                        ExecutionLease.released_at.is_(None),
                    )
                )
            )
            .scalars()
            .first()
        )
        return None if row is None else _lease_of(row)


async def record_native_start_intent(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    intent_ref: str,
    *,
    now: datetime | None = None,
) -> int:
    """Persist the native-start INTENT on the run's OPEN lease(s) —
    BEFORE the provider call (Q35-04).

    This is the write that turns the capacity limit from a reservation
    guarantee into an OCCUPANCY guarantee: once the intent is durable, a
    lost start response or a worker death leaves ``dispatched_unknown``
    occupancy — the slot cannot free on the run's local status, and the
    reconciler has a correlation marker (``intent_ref``) to probe even
    without a handle. Record it immediately before the provider call —
    after every local pre-flight — so an intent can only exist when the
    call was actually attempted.

    The *intent_ref* is short and provider-shaped
    (``github:workflow:...@branch`` / ``gitlab:pipeline:...@branch`` /
    ``azure:pipeline:...@branch``): its prefix routes the reconciler to
    the owning provider's probe, its branch names the run-owned ref the
    job would live on. A re-dispatch (attempt replacement) REUSES the
    held lease and refreshes the marker. Returns how many leases were
    stamped (0 when the run holds no open lease — nothing to bind).
    """

    if not intent_ref:
        raise ValueError("intent_ref must be non-empty")
    moment = now or _utcnow()
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ExecutionLease).where(
                        ExecutionLease.run_id == run_id,
                        ExecutionLease.released_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.native_intent_at = moment
            row.native_intent_ref = intent_ref[:200]
            # Attempt replacement REUSES the held reservation: a fresh
            # dispatch supersedes a parked drain — the occupancy question
            # restarts with THIS attempt's native job.
            row.draining_at = None
        if rows:
            await session.commit()
        return len(rows)


async def clear_native_start_intent(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    intent_ref: str | None = None,
) -> int:
    """Clear the native-start intent — the dispatch aborted PRE-CALL.

    The narrow undo of :func:`record_native_start_intent`: a failure
    between persisting the intent and reaching the provider call (never
    the provider call itself) must not leave a phantom intent parking a
    never-started lease draining forever — with the intent cleared the
    lease is PROVEN never-dispatched again and the ordinary terminal
    release frees the slot (AT-06: capacity returns without waiting for
    a nonexistent remote job). When *intent_ref* is given only leases
    carrying that marker are cleared (a replaced attempt must not wipe
    its successor's intent). Never clears a lease that already carries a
    native HANDLE — a handle proves the provider accepted a start.
    Returns how many leases were cleared.
    """

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ExecutionLease).where(
                        ExecutionLease.run_id == run_id,
                        ExecutionLease.released_at.is_(None),
                        ExecutionLease.native_intent_at.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        cleared = 0
        for row in rows:
            if row.native_handle:
                continue  # a handle proves a start — never clear it
            if intent_ref is not None and (row.native_intent_ref or "") != intent_ref:
                continue  # a different attempt's marker — not ours to wipe
            row.native_intent_at = None
            row.native_intent_ref = None
            cleared += 1
        if cleared:
            await session.commit()
        return cleared


async def record_native_handle(
    lease_id: str,
    native_handle: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    """Attach the dispatched native job's id to its lease (R32-07).

    Dispatch takes the slot BEFORE the provider answers with a pipeline
    or run id, so the correlation lands in a second write the moment it
    exists. Idempotent-shaped like every lease write: an unknown or
    already-released lease answers ``False`` and changes nothing (the
    reconciler has nothing to probe for a released lease anyway).
    """
    if not native_handle:
        raise ValueError("native_handle must be non-empty")
    async with session_factory() as session:
        row = await session.get(ExecutionLease, lease_id)
        if row is None or row.released_at is not None:
            return False
        row.native_handle = native_handle
        await session.commit()
        return True


async def _cas_release(session: AsyncSession, lease_id: str, reason: str, *, now: datetime) -> bool:
    """Compare-and-set the release — exactly once, under any race.

    ``UPDATE ... WHERE released_at IS NULL``: a concurrent release
    (another reconciler tick, a terminal callback that raced the
    reconciler) loses the CAS and changes nothing, so a slot is freed
    exactly once no matter how many drivers reach for it. Returns
    whether THIS call landed the release.
    """

    result = await session.execute(
        update(ExecutionLease)
        .where(ExecutionLease.id == lease_id, ExecutionLease.released_at.is_(None))
        .values(released_at=now, release_reason=reason[:100])
    )
    return bool(result.rowcount)


async def release_lease(
    lease_id: str,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    *,
    session: AsyncSession | None = None,
    reason: str = "released",
    native_completed: bool = True,
    force: bool = False,
) -> bool:
    """Release one lease — frees its slot for the next dispatch.

    Idempotent: an unknown or already-released id answers ``False`` and
    changes nothing. Accepts either a *session_factory* (own
    transaction) or an open *session* (the caller's transaction — used
    when the release rides a terminal transition committed by the
    service, so the slot frees exactly when the status lands).

    R32-07: ``native_completed=False`` is the SPLIT release — the run
    reached its local terminal verdict but the dispatched native job is
    still running. The lease enters the DRAINING state instead of
    freeing the slot: ``draining_at`` is set, ``released_at`` stays
    NULL (the partial open indexes keep holding the slot), and the
    reconciler's :func:`reconcile_draining` releases it once the native
    job is observed terminal.

    Q35-04: with ``native_completed=True`` a lease that carries a live
    native-start INTENT no longer releases on the local verdict alone —
    it parks DRAINING (the intent proves a start was attempted; only
    evidence frees the slot). ``force=True`` is the explicit audited
    override for that guard (operator action, tests): it releases
    regardless of occupancy and the *reason* records WHO decided.
    """

    if session is not None:
        return await _release_in_session(
            session, lease_id, reason, native_completed=native_completed, force=force
        )
    if session_factory is None:
        raise ValueError("release_lease needs a session or a session_factory")
    async with session_factory() as own:
        return await _release_in_session(
            own, lease_id, reason, native_completed=native_completed, force=force
        )


async def _release_in_session(
    session: AsyncSession,
    lease_id: str,
    reason: str,
    *,
    native_completed: bool = True,
    force: bool = False,
) -> bool:
    row = await session.get(ExecutionLease, lease_id)
    if row is None or row.released_at is not None:
        return False
    if not native_completed:
        # R32-07: drain, don't free. The slot stays held; the reason
        # documents WHY the lease is parked (the full release overwrites
        # it with its own reason when the reconciler lands it).
        row.draining_at = _utcnow()
        row.release_reason = reason[:100]
        await session.commit()
        return True
    if row.native_intent_at is not None and not force:
        # Q35-04: an intent proves a start was attempted — the local
        # verdict alone cannot free this slot. Park draining; the
        # reconciler's probe (or an explicit override) finishes it.
        row.draining_at = _utcnow()
        row.release_reason = reason[:100]
        await session.commit()
        return True
    row.released_at = _utcnow()
    row.release_reason = f"{reason} [override]" if force else reason[:100]
    await session.commit()
    return True


@dataclass(frozen=True)
class LeaseRelease:
    """The outcome of an occupancy-aware release (Q35-04): how many of
    the run's leases actually FREED versus parked DRAINING — the caller
    logs both, because "parked draining" is the honest answer whenever
    native occupancy is unresolved."""

    released: int
    drained: int


async def release_lease_with_evidence(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    *,
    reason: str = "released",
    native_terminal: bool = False,
    override: str = "",
) -> LeaseRelease:
    """Release the run's OPEN lease(s) from EVIDENCE (Q35-04) — the
    production terminal-transition spelling.

    A slot frees per lease ONLY from one of the three evidences the
    issue names:

    - ``native_terminal=True`` — the caller OBSERVED the native job
      finished (the harness reconciler's own poll);
    - a lease with NO native-start intent — PROVEN never dispatched
      (the builtin lane's work lives in forge's own worker; AT-06: a
      never-started bootstrap refusal returns capacity immediately);
    - a non-empty *override* — the explicit audited operator decision
      (recorded into ``release_reason`` for the audit trail).

    Anything else — an intent without a terminal observation — parks
    the lease DRAINING: the slot stays held, ``draining_at`` is set,
    and :func:`reconcile_draining` finishes it once the native job is
    observed terminal (or holds it, age visible, while undecidable).
    The run's LOCAL status is never sufficient on its own. Release
    writes are compare-and-set: however many drivers race the terminal
    transition, the slot frees exactly once.
    """

    released = drained = 0
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ExecutionLease).where(
                        ExecutionLease.run_id == run_id,
                        ExecutionLease.released_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        now = _utcnow()
        for row in rows:
            if native_terminal or row.native_intent_at is None:
                if await _cas_release(session, row.id, reason, now=now):
                    released += 1
            elif override:
                audited = f"{reason} [override: {override}]"[:100]
                if await _cas_release(session, row.id, audited, now=now):
                    released += 1
            else:
                # Q35-04: dispatched-but-unobserved — park draining for
                # the reconciler's native probe; never a free pass.
                result = await session.execute(
                    update(ExecutionLease)
                    .where(
                        ExecutionLease.id == row.id,
                        ExecutionLease.released_at.is_(None),
                        ExecutionLease.draining_at.is_(None),
                    )
                    .values(draining_at=now, release_reason=reason[:100])
                )
                if result.rowcount:
                    drained += 1
        if released or drained:
            await session.commit()
    return LeaseRelease(released=released, drained=drained)


async def release_run_leases(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    *,
    reason: str = "released",
    native_completed: bool = True,
    force: bool = False,
) -> int:
    """Release every OPEN lease naming *run_id* (the terminal-transition
    spelling — a run reached a terminal status, its slot(s) free now).

    ``native_completed=False`` parks every OPEN lease of the run
    DRAINING instead (R32-07) — the same split :func:`release_lease`
    gives one lease, at the run's terminal transition.

    Q35-04: with ``native_completed=True`` the release is
    evidence-aware — a lease carrying a live native-start intent parks
    DRAINING (never frees on the local verdict alone), a lease with no
    intent releases immediately (proven never-started). ``force=True``
    is the explicit audited override releasing everything regardless
    (deliberate test/operator callers only; production terminal paths
    use :func:`release_lease_with_evidence`). Returns how many leases
    were touched: RELEASED rows with ``native_completed=True``, DRAINED
    rows with the drain spelling (the historical count contract).
    """

    if not native_completed and not force:
        # The R32-07 spelling: drain every OPEN lease, release nothing.
        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ExecutionLease).where(
                            ExecutionLease.run_id == run_id,
                            ExecutionLease.released_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            now = _utcnow()
            drained = 0
            for row in rows:
                result = await session.execute(
                    update(ExecutionLease)
                    .where(
                        ExecutionLease.id == row.id,
                        ExecutionLease.released_at.is_(None),
                        ExecutionLease.draining_at.is_(None),
                    )
                    .values(draining_at=now, release_reason=reason[:100])
                )
                drained += bool(result.rowcount)
            if drained:
                await session.commit()
        return drained
    outcome = await release_lease_with_evidence(
        session_factory, run_id, reason=reason, override="forced" if force else ""
    )
    return outcome.released


async def reconcile_draining(
    session_factory: async_sessionmaker[AsyncSession],
    native_status: NativeProbe | None = None,
) -> int:
    """Release DRAINING leases whose native jobs are observed terminal
    (R32-07, Q35-04) — exactly once, under any race.

    The reconciler's periodic call. Every lease parked draining
    (``released_at IS NULL AND draining_at IS NOT NULL``) is probed
    through its PROBE KEY — the ``native_handle`` once recorded, else
    the ``native_intent_ref`` (Q35-04: a dispatched intent with no
    handle is NOT empty capacity; its correlation marker is probed
    instead of being released as "nothing to observe"):

    - an explicit *native_status* callable probes every lease (the
      injected-probe contract since R32-07; each service passes its own
      provider probe);
    - otherwise the probe is looked up in the registry
      (:func:`register_native_probe`) by the key's provider prefix —
      each service owns its provider's keys, and a lease with no
      registered probe stays draining (undecidable, age visible);
    - observed terminal (:class:`NativeStatus.TERMINAL` or a truthy
      probe) → the lease is RELEASED through a compare-and-set: a
      concurrent callback racing the reconciler still frees the slot
      exactly once;
    - still running → the lease keeps holding the slot (honest: the
      capacity is genuinely occupied);
    - the probe RAISES or answers UNKNOWN (provider outage,
      undecidable correlation) → the lease also keeps holding the slot:
      absence and unknown stay distinct from terminal, and uncertain
      occupancy never silently frees capacity;
    - NO key at all (a legacy pre-Q35-04 row: no handle, no intent) →
      released: with no intent persisted the lease is PROVEN
      never-dispatched — the local terminal verdict is the only truth
      there is (back-compat with the R32-07 release shape).

    Returns how many leases were released.
    """
    released = 0
    now = _utcnow()
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ExecutionLease).where(
                        ExecutionLease.released_at.is_(None),
                        ExecutionLease.draining_at.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            key = str(row.native_handle or row.native_intent_ref or "")
            if not key:
                # Legacy pre-Q35-04 shape: no intent was ever persisted,
                # so never-dispatched is PROVEN — the local verdict is
                # the whole truth.
                if await _cas_release(
                    session, row.id, "reconciled: no native correlation to observe", now=now
                ):
                    released += 1
                continue
            probe = native_status if native_status is not None else native_probe_for(key)
            if probe is None:
                continue  # undecidable: stays draining, age visible
            if await _probe_status(probe, key) is NativeStatus.TERMINAL:
                if await _cas_release(session, row.id, "reconciled: native job terminal", now=now):
                    released += 1
        if released:
            await session.commit()
    return released


async def lease_snapshot(
    policy: AdmissionPolicy,
    project_id: int,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    provider: str = "",
) -> dict[str, int | None]:
    """The project's execution-capacity snapshot (NEXT-12 evidence).

    ``draining`` (R32-07) is the operator's uncertain-occupancy view:
    slots held by leases whose runs are locally terminal but whose
    native jobs are still running — counted INSIDE ``held`` (the slot is
    genuinely occupied), never inside ``completed``.
    """
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ExecutionLease).where(ExecutionLease.project_id == project_id)
                )
            )
            .scalars()
            .all()
        )
    if provider:
        rows = [row for row in rows if not row.provider or row.provider == provider]
    held = sum(1 for row in rows if row.released_at is None)
    draining = sum(1 for row in rows if row.released_at is None and row.draining_at is not None)
    limit = policy.max_active_per_project
    return {
        "held": held,
        "draining": draining,
        "completed": len(rows) - held,
        "limit": limit if limit > 0 else None,
        "available": max(0, limit - held) if limit > 0 else None,
    }


async def occupancy_report(
    policy: AdmissionPolicy,
    project_id: int,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    provider: str = "",
    now: datetime | None = None,
) -> dict:
    """The Q35-04 operator view: occupancy BY STATE, not just counts.

    ``lease_snapshot`` answers "how many slots"; this answers "what is
    each held slot DOING" — the review's observability asks
    (``execution.occupancy_unknown``, ``execution.draining_age``,
    ``execution.native_vs_reserved``):

    - ``occupancy`` — open leases per :class:`LeaseOccupancy` state;
    - ``occupancy_unknown`` — ``dispatched_unknown`` + ``draining``:
      every slot whose native occupancy is not proven (the count an
      operator watches after an incident);
    - ``draining_age_seconds`` — the OLDEST draining lease's age (None
      when nothing drains): an undecidable lease stays draining with
      its age visible, never silently freed;
    - ``native_vs_reserved`` — handles recorded vs slots held: reserved
      minus native-attributed is exactly the unknown occupancy the
      reconciler is chasing.
    """

    moment = now or _utcnow()
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ExecutionLease).where(ExecutionLease.project_id == project_id)
                )
            )
            .scalars()
            .all()
        )
    if provider:
        rows = [row for row in rows if not row.provider or row.provider == provider]
    open_rows = [row for row in rows if row.released_at is None]
    occupancy = {state.value: 0 for state in LeaseOccupancy}
    for row in open_rows:
        occupancy[lease_occupancy(row).value] += 1
    held = len(open_rows)

    def _aware(value: datetime) -> datetime:
        # SQLite returns naive datetimes; the lease clocks are UTC.
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    ages = [
        int((moment - _aware(row.draining_at)).total_seconds())
        for row in open_rows
        if row.draining_at is not None
    ]
    native_running = occupancy[LeaseOccupancy.NATIVE_RUNNING.value]
    return {
        "project_id": project_id,
        "provider": provider or None,
        "held": held,
        "occupancy": occupancy,
        "occupancy_unknown": (
            occupancy[LeaseOccupancy.DISPATCHED_UNKNOWN.value]
            + occupancy[LeaseOccupancy.DRAINING.value]
        ),
        "draining_age_seconds": max(ages) if ages else None,
        "native_vs_reserved": {"native_attributed": native_running, "reserved": held},
    }


async def admission_report(
    policy: AdmissionPolicy,
    project_id: int,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    provider: str = "",
) -> dict:
    """The NEXT-12 fair-use accounting: three SEPARABLE counters.

    - ``rejected_requests`` — runs this project never admitted (their
      ``status_reason`` records an ``admission_denied``/``fair_use_denied``
      park): a refused request consumed NO execution attempt;
    - ``admitted_work`` — runs currently in :data:`QUEUED_STATUSES`
      (accepted, preflight, planning, waiting_approval): capacity is
      held in the QUEUE, never on the execution slots;
    - ``execution_attempts`` — the lease ledger: ``held`` (slots
      occupied right now, bounded by the policy) and ``completed``
      (released rows — the audit trail), from the same durable rows the
      dispatch check CAS-inserts against.

    The report carries the policy bounds so the snapshot is explainable
    after the fact (the same posture as :class:`AdmissionDecision`).
    """
    from forge.durable import FlowRun
    from forge.durable.controller import TERMINAL_STATUSES

    terminal = {status.value for status in TERMINAL_STATUSES}
    conditions = [FlowRun.project_id == project_id]
    if provider:
        conditions.append(FlowRun.provider == provider)
    async with session_factory() as session:
        rows = (
            await session.execute(select(FlowRun.status, FlowRun.status_reason).where(*conditions))
        ).all()
    rejected_requests = 0
    admitted_work = 0
    for status, status_reason in rows:
        reason = status_reason or ""
        if reason.startswith("fair_use_denied") or reason.startswith("admission_denied"):
            rejected_requests += 1
            continue  # never entered — never counted anywhere else
        if status in QUEUED_STATUSES:
            admitted_work += 1
    return {
        "project_id": project_id,
        "provider": provider or None,
        "policy": {
            "max_active_per_project": policy.max_active_per_project,
            "max_queued_runs": policy.max_queued_runs,
            "max_runs_per_issue": policy.max_runs_per_issue,
            "max_user_runs_per_hour": policy.max_user_runs_per_hour,
        },
        "rejected_requests": rejected_requests,
        "admitted_work": admitted_work,
        "execution_attempts": await lease_snapshot(
            policy, project_id, session_factory, provider=provider
        ),
        "terminal_runs": sum(1 for status, _reason in rows if status in terminal),
    }


# ---------------------------------------------------------------------------
# R36-21 (issue #280): saturation signals — the operator's "when does it
# queue / pause / refuse" view, derived from the same durable rows
# ---------------------------------------------------------------------------


async def saturation_report(
    policy: AdmissionPolicy,
    project_id: int,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    provider: str = "",
    now: datetime | None = None,
) -> dict:
    """The R36-21 saturation signals over the project's live lease rows.

    One read, three low-cardinality signals (no secrets, no prompts —
    counts and ages over the durable occupancy columns only):

    - ``execution.occupied_vs_limit`` — how many slots the project
      OCCUPIES (open leases, draining included: an uncertain slot is a
      genuinely occupied one) against the configured limit, plus
      ``at_limit`` — the moment new dispatches start parking
      (``blocked(execution_capacity)``) rather than executing;
    - ``native_start.unknown_age`` — the OLDEST age, in seconds, of an
      open lease whose native occupancy is NOT proven
      (``dispatched_unknown`` or ``draining``): the number an operator
      watches after a lost-response incident. ``None`` when nothing is
      unknown. Unknown occupancy never frees itself: the bounded
      escalation is the reconciler's probe
      (:func:`reconcile_draining`) or an EXPLICIT audited override —
      the report names that path so "wait" is never the only answer;
    - ``native_start.unknown_count`` — how many such leases there are.

    The ages are read against *now* (the caller's clock or the real
    one); naive timestamps from SQLite are interpreted as UTC, the
    lease clocks' zone.
    """

    moment = now or _utcnow()
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ExecutionLease).where(ExecutionLease.project_id == project_id)
                )
            )
            .scalars()
            .all()
        )
    if provider:
        rows = [row for row in rows if not row.provider or row.provider == provider]
    open_rows = [row for row in rows if row.released_at is None]
    held = len(open_rows)
    limit = policy.max_active_per_project

    def _aware(value: datetime) -> datetime:
        # SQLite returns naive datetimes; the lease clocks are UTC.
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    unknown_ages: list[int] = []
    for row in open_rows:
        occupancy = lease_occupancy(row)
        if occupancy is LeaseOccupancy.DISPATCHED_UNKNOWN:
            stamp = row.native_intent_at
        elif occupancy is LeaseOccupancy.DRAINING:
            stamp = row.draining_at
        else:
            continue
        if stamp is not None:
            unknown_ages.append(max(0, int((moment - _aware(stamp)).total_seconds())))
    return {
        "project_id": project_id,
        "provider": provider or None,
        "execution.occupied_vs_limit": {
            "occupied": held,
            "limit": limit if limit > 0 else None,
            "available": max(0, limit - held) if limit > 0 else None,
            "at_limit": bool(limit > 0 and held >= limit),
        },
        "native_start.unknown_count": len(unknown_ages),
        "native_start.unknown_age": max(unknown_ages) if unknown_ages else None,
        "escalation": (
            "unknown occupancy holds its slot until the reconciler's native probe "
            "observes terminal (reconcile_draining) or an operator applies an "
            "explicit audited override — it never frees itself"
        ),
    }
