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
"""

from __future__ import annotations

import os
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
    "RefusalReason",
    "admission_report",
    "check_admission",
    "execution_capacity_comment",
    "lease_conflict_kind",
    "lease_snapshot",
    "release_lease",
    "release_run_leases",
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
    )


@dataclass(frozen=True)
class Lease:
    """The in-memory handle of an acquired :class:`ExecutionLease` row.

    Carries the identity needed to release the slot later
    (:func:`release_lease`) and the evidence a run journals when its
    admission check ran at dispatch (:meth:`as_document`).
    """

    lease_id: str
    project_id: int
    slot: int
    run_id: str
    provider: str
    acquired_at: datetime

    def as_document(self) -> dict:
        """The JSON-shape record for run evidence and refusal snippets."""
        return {
            "lease_id": self.lease_id,
            "project_id": self.project_id,
            "slot": self.slot,
            "run_id": self.run_id,
            "provider": self.provider,
            "acquired_at": self.acquired_at.isoformat(),
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
    )


async def _reclaim_terminal_run_leases(
    session: AsyncSession, project_id: int, *, provider: str, now: datetime
) -> int:
    """Release OPEN leases whose runs are already terminal (NEXT-11).

    The crash backstop: a worker that died between dispatch and release
    leaves an open lease behind; the run row it names eventually reaches
    a terminal status (or is parked by the reconciler), and the NEXT
    acquirer in this project reclaims the slot instead of leaking it
    forever. Returns how many leases were reclaimed.
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
        if not row.run_id:
            continue
        run = await session.get(FlowRun, row.run_id)
        if run is None or run.status not in terminal:
            continue
        row.released_at = now
        row.release_reason = f"reclaimed: run {run.status}"
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


async def release_lease(
    lease_id: str,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    *,
    session: AsyncSession | None = None,
    reason: str = "released",
) -> bool:
    """Release one lease — frees its slot for the next dispatch.

    Idempotent: an unknown or already-released id answers ``False`` and
    changes nothing. Accepts either a *session_factory* (own
    transaction) or an open *session* (the caller's transaction — used
    when the release rides a terminal transition committed by the
    service, so the slot frees exactly when the status lands).
    """
    if session is not None:
        return await _release_in_session(session, lease_id, reason)
    if session_factory is None:
        raise ValueError("release_lease needs a session or a session_factory")
    async with session_factory() as own:
        return await _release_in_session(own, lease_id, reason)


async def _release_in_session(session: AsyncSession, lease_id: str, reason: str) -> bool:
    row = await session.get(ExecutionLease, lease_id)
    if row is None or row.released_at is not None:
        return False
    row.released_at = _utcnow()
    row.release_reason = reason[:100]
    await session.commit()
    return True


async def release_run_leases(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    *,
    reason: str = "released",
) -> int:
    """Release every OPEN lease naming *run_id* (the terminal-transition
    spelling — a run reached a terminal status, its slot(s) free now)."""
    released = 0
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
            row.released_at = _utcnow()
            row.release_reason = reason[:100]
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
    """The project's execution-capacity snapshot (NEXT-12 evidence)."""
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
    limit = policy.max_active_per_project
    return {
        "held": held,
        "completed": len(rows) - held,
        "limit": limit if limit > 0 else None,
        "available": max(0, limit - held) if limit > 0 else None,
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
