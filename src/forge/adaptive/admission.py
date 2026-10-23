"""Bound admission and native execution occupancy using durable leases."""

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

    #: The issue already consumed its run budget — repeated /implement on one issue is a
    #: repair signal, not more runs (ADR-0008).
    ISSUE_RUN_LIMIT = "issue_run_limit"
    #: The requesting actor hit the hourly fair-use rate across issues.
    USER_RATE_LIMIT = "user_rate_limit"
    #: The project's work-in-progress bound: one project burst cannot monopolize all
    #: admitted work indefinitely.
    PROJECT_ACTIVE_LIMIT = "project_active_limit"
    #: The queue is at capacity — admit nothing until work drains.
    QUEUE_FULL = "queue_full"


#: The env names :meth:`AdmissionPolicy.from_env` reads (ints; a value that does not parse
#: raises — a typo must never silently become the default, the same fail-closed posture as
#: the harness pin tables).
ENV_MAX_ACTIVE_PER_PROJECT = "FORGE_ADMISSION_MAX_ACTIVE_PER_PROJECT"
ENV_MAX_QUEUED_RUNS = "FORGE_ADMISSION_MAX_QUEUED_RUNS"
ENV_MAX_RUNS_PER_ISSUE = "FORGE_ADMISSION_MAX_RUNS_PER_ISSUE"
ENV_USER_RUNS_PER_HOUR = "FORGE_ADMISSION_USER_RUNS_PER_HOUR"


@dataclass(frozen=True)
class AdmissionPolicy:
    """The fair-use bounds. Defaults are deliberately small: a project at capacity parks new work instead of queueing it forever."""

    max_active_per_project: int = 3
    max_queued_runs: int = 10
    max_runs_per_issue: int = 5
    max_user_runs_per_hour: int = 6

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> AdmissionPolicy:
        """Load the policy from *environ* (the process env by default)."""

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
    """The fair-use verdict for ONE admission attempt, with the evidence needed to explain it: the counts observed, the policy that judged them, and — when refused — the typed:class:`RefusalReason` and the human sentence an operator or an issue comment can quote verbatim."""

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


#: The pre-execution statuses a QUEUED run sits in — a run waiting for a human gate decision
#: holds QUEUE capacity, never ACTIVE capacity ("a waiting human decision releases expensive
#: execution capacity").
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
    """Decide one admission attempt against the fair-use bounds (pure)."""

    counts = {
        "active": active_count,
        "queued": queued_count,
        "issue_runs": issue_run_count,
        "user_recent": user_recent_count,
    }

    def _refuse(reason: RefusalReason, limit: int, observed: int) -> AdmissionDecision:
        # the lifetime per-issue cap must NOT suggest that draining unrelated work would
        # reset it — only the three capacity dimensions (per-user rate, project WIP, queue
        # depth) recover on their own as work drains.
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


# durable execution leases — the slot reservation at dispatch
#

#: Free released slots while retaining completed-attempt audit rows.
_LEASE_OPEN = text("released_at IS NULL")

#: the occupancy watchlist — OPEN leases that carry a native start intent (dispatched,
#: verdict unknown).
_INTENT_OPEN = text("released_at IS NULL AND native_intent_at IS NOT NULL")


def _utcnow() -> datetime:
    return datetime.now(UTC)


#: The two CAS constraints an INSERT into ``execution_leases`` can lose: the open-SLOT race
#: (another run took this slot) and the open-RUN race (THIS run already holds a slot).
_SLOT_CONSTRAINT = "uq_execution_lease_slot"
_RUN_CONSTRAINT = "uq_execution_lease_open_run"


def lease_conflict_kind(exc: BaseException) -> str:
    """Classify an:class:`IntegrityError` from a lease INSERT."""
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
    """ONE reserved execution slot for a project, durable."""

    __tablename__ = "execution_leases"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    #: The canonical connection identity: same numbers on two connections stay independent
    #: aggregates — it keys the CAS index.
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    slot: Mapped[int] = mapped_column(Integer, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    #: the native-start INTENT — set BEFORE the provider call
    #: (:func:`record_native_start_intent`), NULL when no native job was ever dispatched (the
    #: builtin lane, a pre-call abort).
    native_intent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    native_intent_ref: Mapped[str | None] = mapped_column(String(200), nullable=True, default=None)
    #: the dispatched native job's correlation (pipeline/run id). NULL until the provider
    #: answers dispatch with an id (:func:`record_native_handle`); the reconciler's probe
    #: key.
    native_handle: Mapped[str | None] = mapped_column(String(256), nullable=True, default=None)
    #: set when the RUN reached its local terminal verdict but the NATIVE job was still
    #: running (``release_lease(..., native_completed=False)``).
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
        # the idempotency unit is the RUN — one OPEN lease per run_id, alongside the slot
        # uniqueness.
        Index(
            "uq_execution_lease_open_run",
            "run_id",
            unique=True,
            sqlite_where=_LEASE_OPEN,
            postgresql_where=_LEASE_OPEN,
        ),
        # the occupancy watchlist — open leases whose dispatch intent is live (dispatched,
        # native verdict unknown). Reconciler scans start here instead of sweeping the audit
        # trail.
        Index(
            "ix_execution_leases_native_intent",
            "native_intent_at",
            sqlite_where=_INTENT_OPEN,
            postgresql_where=_INTENT_OPEN,
        ),
    )


# occupancy — the slot's DERIVED state, from evidence columns only
#


class LeaseOccupancy(str, Enum):
    """The five occupancy states of a lease, DERIVED — never stored: the columns are facts (intent persisted, handle attached, drain parked, release landed) and occupancy is their reading."""

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
    """What a native-occupancy probe OBSERVED about one native job."""

    TERMINAL = "terminal"
    RUNNING = "running"
    UNKNOWN = "unknown"


#: One provider's occupancy probe: maps a lease's PROBE KEY (the ``native_handle`` once
#: recorded, else the ``native_intent_ref``) to its observed :class:`NativeStatus`.
NativeProbe = Callable[[str], "bool | NativeStatus | Awaitable[bool | NativeStatus]"]

_NATIVE_PROBES: dict[str, NativeProbe] = {}


def register_native_probe(prefix: str, probe: NativeProbe) -> None:
    """Register *probe* for the provider whose probe keys start *prefix*."""

    _NATIVE_PROBES[str(prefix)] = probe


def native_probe_for(key: str) -> NativeProbe | None:
    """The probe owning *key* (by its provider prefix), or None."""

    prefix = key.split(":", 1)[0]
    return _NATIVE_PROBES.get(prefix)


def _coerce_native_status(observed: bool | NativeStatus) -> NativeStatus:
    if isinstance(observed, NativeStatus):
        return observed
    return NativeStatus.TERMINAL if observed else NativeStatus.RUNNING


#: Client-error statuses that do NOT mean "the server may have accepted the request anyway":
#: 408/425/429 are retryable conditions where the start may have landed (or the request was
#: never sent — undecidable), so they stay ambiguous like 5xx and transport errors.
_RETRYABLE_CLIENT_STATUSES = frozenset({408, 425, 429})


def definite_start_refusal(status_code: int) -> bool:
    """Whether a provider start call's HTTP *status_code* PROVES the start was refused before acceptance ( evidence classifier)."""

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
    """The in-memory handle of an acquired:class:`ExecutionLease` row."""

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
    """The parked-at-dispatch operator note — the ONE renderer every provider's dispatch choke point posts when capacity refuses."""
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
    """Reclaim OPEN leases whose runs are already terminal — by OCCUPANCY, never by local status alone."""
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
            # a DRAINING lease already knows its run is terminal — it is waiting on the
            # NATIVE job, and only the reconciler's probe may release it.
            continue
        if not row.run_id:
            continue
        run = await session.get(FlowRun, row.run_id)
        if run is None or run.status not in terminal:
            continue
        if row.native_intent_at is None:
            # PROVEN never dispatched — the local terminal verdict is the whole truth; the
            # slot returns now.
            row.released_at = now
            row.release_reason = f"reclaimed: run {run.status}"
            reclaimed += 1
            continue
        # A dispatched intent may still occupy a runner; only the reconciler's probe
        # can prove it safe to release.
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
    """Reserve ONE execution slot for *project_id* — or lose honestly."""
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
                    # THIS run already holds a slot — the open-run index refused the double
                    # reservation.
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
                    # The winner released between our refused INSERT and this read — retry
                    # the insert on the SAME slot.
                    run_conflict_misses += 1
                    if run_conflict_misses >= 3:
                        raise
                    continue
                if conflict != _SLOT_CONSTRAINT:
                    # an unrelated constraint violation is never a "busy slot" — retrying
                    # the next slot forever (the disabled-limit mode loops unbounded) would
                    # hide it.
                    raise
                slot += 1  # this slot lost the race — next slot
        return None


async def open_lease_for_run(
    session_factory: async_sessionmaker[AsyncSession], run_id: str
) -> Lease | None:
    """The run's OPEN lease, if any (the idempotent pre-read spelling)."""

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
    """Persist the native-start INTENT on the run's OPEN lease(s) — BEFORE the provider call."""

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
            # Attempt replacement REUSES the held reservation: a fresh dispatch supersedes a
            # parked drain — the occupancy question restarts with THIS attempt's native job.
            row.draining_at = None
        if rows:
            await session.commit()
        return len(rows)


async def clear_native_start_intent(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    intent_ref: str | None = None,
) -> int:
    """Clear the native-start intent — the dispatch aborted PRE-CALL."""

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
    """Attach the dispatched native job's id to its lease."""
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
    """Compare-and-set the release — exactly once, under any race."""

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
    """Release one lease — frees its slot for the next dispatch."""

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
        # drain, don't free. The slot stays held; the reason documents WHY the lease is
        # parked (the full release overwrites it with its own reason when the reconciler
        # lands it).
        row.draining_at = _utcnow()
        row.release_reason = reason[:100]
        await session.commit()
        return True
    if row.native_intent_at is not None and not force:
        # an intent proves a start was attempted — the local verdict alone cannot free this
        # slot. Park draining; the reconciler's probe (or an explicit override) finishes it.
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
    """The outcome of an occupancy-aware release: how many of the run's leases actually FREED versus parked DRAINING — the caller logs both, because "parked draining" is the honest answer whenever native occupancy is unresolved."""

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
    """Release the run's OPEN lease(s) from EVIDENCE — the production terminal-transition spelling."""

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
                # dispatched-but-unobserved — park draining for the reconciler's native
                # probe; never a free pass.
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
    """Release every OPEN lease naming *run_id* (the terminal-transition spelling — a run reached a terminal status, its slot(s) free now)."""

    if not native_completed and not force:
        # The spelling: drain every OPEN lease, release nothing.
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
    """Release DRAINING leases whose native jobs are observed terminal — exactly once, under any race."""
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
                # Without a native correlation, release against the local terminal verdict.
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
    """The project's execution-capacity snapshot ( evidence)."""
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
    """The operator view: occupancy BY STATE, not just counts."""

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
    """The fair-use accounting: three SEPARABLE counters."""
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


# saturation signals — the operator's "when does it queue / pause / refuse" view, derived
# from the same durable rows
#


async def saturation_report(
    policy: AdmissionPolicy,
    project_id: int,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    provider: str = "",
    now: datetime | None = None,
) -> dict:
    """The saturation signals over the project's live lease rows."""

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
