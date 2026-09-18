"""Durable step runtime (ADR-0017): Postgres-owned scheduling and claiming.

The gateway persists a scheduled ``StepRun`` in the same transaction as the
command's inbox row and answers ``202``; this module owns everything after
that commit:

- **Claim** (§2): due steps are claimed with a conditional
  ``UPDATE ... WHERE status='scheduled'`` whose rowcount is the ownership
  decision (atomic on Postgres and SQLite alike); the candidate SELECT
  additionally carries ``FOR UPDATE SKIP LOCKED`` so concurrent Postgres
  workers do not fight over the same rows (SQLite ignores FOR UPDATE —
  single writer). The claim grants a lease (owner + expiry) and bumps the
  per-row monotonic ``fence_token``, and snapshots the bound run's
  cancellation generation (R10) when the step already has one.
- **Execute** (§8): external calls happen OUTSIDE any DB transaction; the
  claim is RE-VALIDATED immediately before the handler (A05: a lease that
  died while the claim sat queued is requeued — or parked dead per attempts —
  instead of executing unowned), and a per-task heartbeat renews the lease.
  The heartbeat is SUPERVISED: a renewal hitting 0 rows OR RAISING fails the
  handler — the remaining effects stop at the next checkpoint (the guarded
  write boundaries refuse a dead claim) and the step is failed into a
  recoverable retry. Shutdown cancellation and lease loss are distinct: a
  worker-teardown CancelledError records nothing (the reaper owns the row),
  a lease loss records the failure and raises :class:`LeaseLostError`. The
  :class:`~forge.durable.claims.ExecutionClaim` minted from the claim is
  bound as the task's ambient context for the whole dispatch, so the write
  boundaries (publisher grant, guarded transitions) can pin the fence —
  queue ownership implies effect ownership (R10).
- **Commit/abort** (§3): completion and failure are fenced CASes
  (``WHERE fence_token = <claimed> AND status='running'``) — a stale owner's
  write hits 0 rows and is abandoned silently. A handler that finishes after
  its run went terminal still commits its step, but the completion records
  superseded evidence instead of a plain result — the run is never walked
  out of a terminal state by a late callback (R17 pattern, mirrored here).
- **Recovery**: a reaper reschedules steps whose lease expired (fence stays —
  the zombie's old token can no longer commit); failures reschedule with
  exponential backoff + full jitter and park as ``dead`` after
  ``max_attempts``, keeping the last error (poison pill — never deleted).
  A shutdown worker hands its never-started pre-claimed steps back to the
  queue un-failed (A05) instead of leaving them to rot on expiring leases.

The legacy Redis queue (orchestrator events, flow steps) is untouched; for
run commands Redis is demoted to a wake-up accelerator (``STEP_WAKE_KEY``),
never the authority.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from sqlalchemy import case, select, type_coerce, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.durable.claims import ExecutionClaim, bind_claim
from forge.durable.controller import TERMINAL_STATUSES, as_aware_utc
from forge.durable.models import FlowRun, StepRun
from forge.runs import execute_run_command

logger = logging.getLogger(__name__)

# Step lifecycle statuses (subset of the model's closed set).
STEP_SCHEDULED = "scheduled"
STEP_RUNNING = "running"
STEP_SUCCEEDED = "succeeded"
STEP_DEAD = "dead"

#: Lease granted per claim, renewed by the per-task heartbeat.
STEP_LEASE_SECONDS = 120
STEP_LEASE_RENEW_INTERVAL = 30

#: Poison-pill ceiling (ADR-0017 §4): attempt >= max_attempts parks the step.
STEP_MAX_ATTEMPTS = 3

#: Exponential backoff with FULL jitter: uniform(0, min(cap, base*2^attempt)).
STEP_BACKOFF_BASE_SECONDS = 5
STEP_BACKOFF_CAP_SECONDS = 300

#: Stored per-step deadline (fires independently of poll outcomes once the
#: reconciler learns to consult it; recorded from day one).
STEP_DEADLINE_SECONDS = 900

#: A05: the shipped worker executes claims SEQUENTIALLY, so it must hold
#: exactly one lease at a time — claiming a batch it cannot run concurrently
#: left the unstarted steps rotting on leases that expired mid-execution of
#: the previous one, and the worker then started them with dead claims. Raise
#: this only together with a bounded-parallel executor that heartbeats every
#: in-flight claim; every claim is re-validated immediately before its
#: handler either way.
STEP_CLAIM_BATCH = 1
STEP_POLL_INTERVAL = 5.0
STEP_REAP_INTERVAL = 30.0

#: Redis wake-up key (accelerator only — losing it costs one poll interval).
STEP_WAKE_KEY = "forge:steps:wake"

#: Recorded when an attempt is accounted as lost before its handler ran —
#: the eager requeue of a stale claim (A05) parks a step that exhausted its
#: budget this way with the same evidence the reaper would have written.
_EXPIRED_BEFORE_HANDLER_ERROR = "lease_expired: attempts exhausted before the handler ran"


class LeaseLostError(RuntimeError):
    """The execution lost its step lease mid-flight (A05).

    Raised when the supervised heartbeat observed the ownership loss (a
    renewal matched 0 rows, or the renewal itself failed) and the handler was
    aborted. Deliberately NOT a ``CancelledError``: worker shutdown and lease
    loss must stay distinguishable — shutdown records nothing, a lease loss
    fails the step into a recoverable retry.
    """


@dataclass(frozen=True)
class ClaimedStep:
    """A step row owned by one worker until its lease expires."""

    id: int
    flow_run_id: str | None
    step_name: str
    fence_token: int
    attempt: int
    max_attempts: int
    payload: dict[str, Any]
    owner: str
    source_event_id: str | None
    #: R10: the bound run's cancellation generation as of the claim (``None``
    #: when the step was not bound to a run yet — command steps bind at
    #: execution time, and only the flag-level grant check applies then).
    cancellation_generation: int | None = None


def execution_claim(claimed: ClaimedStep) -> ExecutionClaim:
    """Mint the effect-side :class:`ExecutionClaim` from a queue claim (R10)."""
    return ExecutionClaim(
        step_id=claimed.id,
        attempt=claimed.attempt,
        owner=claimed.owner,
        fence_token=claimed.fence_token,
        cancellation_generation=claimed.cancellation_generation,
    )


def command_source_event_id(command: str, project_id: int, note_id: int | str) -> str:
    """Content-stable inbox identity for a run command (ADR-0017 §1).

    Re-delivered note webhooks collapse onto one id: sha256 over
    ``run:{command}:{project_id}:{note_id}``.
    """
    material = f"run:{command}:{project_id}:{note_id}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with full jitter (base 5s, cap 300s)."""
    ceiling = min(STEP_BACKOFF_CAP_SECONDS, STEP_BACKOFF_BASE_SECONDS * (2 ** max(attempt, 0)))
    return random.uniform(0, ceiling)


# ----------------------------------------------------------------------
# Ingress helper (runs inside the gateway's transaction)
# ----------------------------------------------------------------------


async def schedule_command_step(
    session: AsyncSession,
    run_command: dict[str, Any],
    *,
    source_event_id: str,
    due_at: datetime | None = None,
    max_attempts: int = STEP_MAX_ATTEMPTS,
) -> StepRun:
    """Insert the first scheduled step for a run command.

    Called inside the gateway's ingress transaction: the step row is the
    durability contract behind the ``202`` (ADR-0017 §1) — once it commits,
    some worker WILL attempt the command. Command steps bind their FlowRun
    only at execution time, so ``flow_run_id`` stays NULL here.
    """
    now = due_at or _utcnow()
    step = StepRun(
        flow_run_id=None,
        step_name=str(run_command.get("command") or "unknown"),
        status=STEP_SCHEDULED,
        payload=dict(run_command),
        source_event_id=source_event_id,
        due_at=now,
        deadline_at=now + timedelta(seconds=STEP_DEADLINE_SECONDS),
        max_attempts=max_attempts,
    )
    session.add(step)
    await session.flush()
    return step


# ----------------------------------------------------------------------
# Claiming (atomic ownership: lease + fence token)
# ----------------------------------------------------------------------


async def claim_due_steps(
    session_factory: async_sessionmaker[AsyncSession],
    owner: str,
    *,
    limit: int = STEP_CLAIM_BATCH,
    lease_seconds: int = STEP_LEASE_SECONDS,
    source_event_id: str | None = None,
) -> list[ClaimedStep]:
    """Claim due scheduled steps for *owner*.

    Two mechanisms behind one helper: the candidate SELECT carries
    ``FOR UPDATE SKIP LOCKED`` (no-op on SQLite, row locks on Postgres), and
    the conditional UPDATE on ``status='scheduled'`` is the actual ownership
    decision via its rowcount — atomic on every backend (ADR-0017 §2).
    """
    now = _utcnow()
    claimed: list[ClaimedStep] = []
    async with session_factory() as session:
        async with session.begin():
            candidate = (
                select(StepRun.id)
                .where(StepRun.status == STEP_SCHEDULED, StepRun.due_at <= now)
                .order_by(StepRun.due_at, StepRun.id)
                .limit(limit)
            )
            if source_event_id is not None:
                candidate = candidate.where(StepRun.source_event_id == source_event_id)
            candidate = candidate.with_for_update(skip_locked=True)
            ids = (await session.execute(candidate)).scalars().all()
            for step_id in ids:
                # CAS: fence bumps here, on the lease-granting write only.
                result = cast(
                    CursorResult[Any],
                    await session.execute(
                        update(StepRun)
                        .where(StepRun.id == step_id, StepRun.status == STEP_SCHEDULED)
                        .values(
                            status=STEP_RUNNING,
                            lease_owner=owner,
                            lease_expires_at=now + timedelta(seconds=lease_seconds),
                            fence_token=StepRun.fence_token + 1,
                            started_at=now,
                        )
                    ),
                )
                if result.rowcount != 1:
                    continue  # another owner's CAS won this row
                row = await session.get(StepRun, step_id)
                if row is None:  # pragma: no cover — the CAS just matched it
                    continue
                # R10: snapshot the bound run's publication-grant generation at
                # claim time. Command steps bind at execution time (NULL here) —
                # their claim carries no generation and only the flag-level
                # grant check fences them.
                generation = (
                    await session.execute(
                        select(FlowRun.cancellation_generation).where(FlowRun.id == row.flow_run_id)
                    )
                ).scalar_one_or_none()
                claimed.append(
                    ClaimedStep(
                        id=row.id,
                        flow_run_id=row.flow_run_id,
                        step_name=row.step_name,
                        fence_token=int(row.fence_token),
                        attempt=int(row.attempt),
                        max_attempts=int(row.max_attempts),
                        payload=dict(row.payload or {}),
                        owner=owner,
                        source_event_id=row.source_event_id,
                        cancellation_generation=generation,
                    )
                )
    return claimed


async def claim_command_step(
    session_factory: async_sessionmaker[AsyncSession],
    owner: str,
    source_event_id: str,
    *,
    lease_seconds: int = STEP_LEASE_SECONDS,
) -> ClaimedStep | None:
    """Claim the persisted step behind a run command (wake-up path)."""
    claimed = await claim_due_steps(
        session_factory,
        owner,
        limit=1,
        lease_seconds=lease_seconds,
        source_event_id=source_event_id,
    )
    return claimed[0] if claimed else None


async def command_step_known(
    session_factory: async_sessionmaker[AsyncSession],
    source_event_id: str,
) -> bool:
    """Whether a step was persisted for this command identity."""
    async with session_factory() as session:
        row = await session.execute(
            select(StepRun.id).where(StepRun.source_event_id == source_event_id).limit(1)
        )
        return row.first() is not None


# ----------------------------------------------------------------------
# Fenced completion / failure / heartbeat
# ----------------------------------------------------------------------


async def renew_step_lease(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedStep,
    *,
    seconds: int = STEP_LEASE_SECONDS,
) -> bool:
    """Per-task heartbeat: renew THIS step's lease (ADR-0017 §3).

    A global worker heartbeat cannot stop a zombie — renewal is bound to the
    task. ``False`` means the lease was lost/expired/reassigned: the caller
    must abort immediately.
    """
    async with session_factory() as session:
        async with session.begin():
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(StepRun)
                    .where(
                        StepRun.id == claimed.id,
                        StepRun.lease_owner == claimed.owner,
                        StepRun.fence_token == claimed.fence_token,
                        StepRun.status == STEP_RUNNING,
                    )
                    .values(lease_expires_at=_utcnow() + timedelta(seconds=seconds))
                ),
            )
            return result.rowcount == 1


async def complete_step(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedStep,
    *,
    output: dict | None = None,
) -> bool:
    """Fenced success: 0 rows updated means this owner was fenced — abandon."""
    async with session_factory() as session:
        async with session.begin():
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(StepRun)
                    .where(
                        StepRun.id == claimed.id,
                        StepRun.fence_token == claimed.fence_token,
                        StepRun.status == STEP_RUNNING,
                    )
                    .values(status=STEP_SUCCEEDED, finished_at=_utcnow(), output=output)
                ),
            )
            return result.rowcount == 1


async def fail_step(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedStep,
    error: str,
) -> str:
    """Fenced failure: reschedule with jittered backoff, or park as dead.

    Returns ``"retry"``, ``"dead"`` or ``"fenced"``. Rows keep the last error
    in ``output`` on BOTH outcomes — the poison pill is an incident record,
    never deleted (ADR-0017 §4), and a row that dies later to the reaper
    (exhausted lease retries, passed deadline) carries its worker's error
    with it instead of a bare reap note.
    """
    now = _utcnow()
    ownership = (
        StepRun.id == claimed.id,
        StepRun.fence_token == claimed.fence_token,
        StepRun.status == STEP_RUNNING,
    )
    async with session_factory() as session:
        async with session.begin():
            row = (
                await session.execute(
                    select(StepRun.attempt, StepRun.max_attempts).where(*ownership)
                )
            ).first()
            if row is None:
                return "fenced"
            # `attempt` counts executions tried; THIS failure produces
            # new_attempt. new_attempt >= max_attempts parks the step.
            new_attempt = int(row.attempt) + 1
            max_attempts = int(row.max_attempts)
            if new_attempt >= max_attempts:
                await session.execute(
                    update(StepRun)
                    .where(*ownership)
                    .values(
                        status=STEP_DEAD,
                        attempt=new_attempt,
                        finished_at=now,
                        lease_owner=None,
                        lease_expires_at=None,
                        output={"error": error[:2000]},
                    )
                )
                return "dead"
            await session.execute(
                update(StepRun)
                .where(*ownership)
                .values(
                    status=STEP_SCHEDULED,
                    attempt=new_attempt,
                    due_at=now + timedelta(seconds=_backoff_delay(new_attempt)),
                    lease_owner=None,
                    lease_expires_at=None,
                    output={"error": error[:2000]},
                )
            )
            return "retry"


async def reschedule_expired_leases(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Reaper: steps whose lease expired mid-flight go back to ``scheduled``.

    The fence token stays: the zombie's completion carries the old token and
    is rejected by the fenced CAS (0 rows) — it cannot commit effects.

    Liveness bound: a reaped attempt that exhausts the poison-pill budget
    parks the step ``dead`` instead of rescheduling — the reaper bumps the
    attempt counter too, so without this a step crashing before its heartbeat
    would loop claim → crash → reap forever and never reach ``fail_step``'s
    dead parking. The last error (if a worker recorded one) is preserved in
    ``output``; otherwise the reap reason is written there.
    """
    now = _utcnow()
    reap_error = {"error": "lease_expired: attempts exhausted by the reaper"}
    async with session_factory() as session:
        async with session.begin():
            dead = cast(
                CursorResult[Any],
                await session.execute(
                    update(StepRun)
                    .where(
                        StepRun.status == STEP_RUNNING,
                        StepRun.lease_expires_at < now,
                        StepRun.attempt + 1 >= StepRun.max_attempts,
                    )
                    .values(
                        status=STEP_DEAD,
                        attempt=StepRun.attempt + 1,
                        finished_at=now,
                        lease_owner=None,
                        lease_expires_at=None,
                        output=case(
                            (
                                StepRun.output.is_(None),
                                type_coerce(reap_error, StepRun.output.type),
                            ),
                            else_=StepRun.output,
                        ),
                    )
                ),
            )
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(StepRun)
                    .where(StepRun.status == STEP_RUNNING, StepRun.lease_expires_at < now)
                    .values(
                        status=STEP_SCHEDULED,
                        due_at=now,
                        attempt=StepRun.attempt + 1,
                        lease_owner=None,
                        lease_expires_at=None,
                    )
                ),
            )
            return dead.rowcount + result.rowcount


async def reap_deadline_exceeded(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Reaper: steps whose ``deadline_at`` passed park as ``dead``.

    ``deadline_at`` is the step's schedule-to-close budget, recorded at
    ingress (ADR-0017): once it has passed, no retry of this step can finish
    in time — a retry that cannot fit the deadline is a new problem, not a
    retry. The step dies with the reason preserved (a worker's last error
    stays in ``output``); the fenced completion of an in-flight owner hits 0
    rows and is abandoned silently.
    """
    now = _utcnow()
    deadline_error = {"error": "deadline_exceeded: step budget spent before completion"}
    async with session_factory() as session:
        async with session.begin():
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(StepRun)
                    .where(
                        StepRun.status.in_([STEP_SCHEDULED, STEP_RUNNING]),
                        StepRun.deadline_at.is_not(None),
                        StepRun.deadline_at < now,
                    )
                    .values(
                        status=STEP_DEAD,
                        finished_at=now,
                        lease_owner=None,
                        lease_expires_at=None,
                        output=case(
                            (
                                StepRun.output.is_(None),
                                type_coerce(deadline_error, StepRun.output.type),
                            ),
                            else_=StepRun.output,
                        ),
                    )
                ),
            )
            return result.rowcount


# ----------------------------------------------------------------------
# Execution
# ----------------------------------------------------------------------


def _bound_run_id(claimed: ClaimedStep) -> str | None:
    """The run this step's result belongs to, if it is known to the claim.

    Prefer the persisted ``flow_run_id`` binding; command steps that carry an
    explicit ``run_id`` payload key (the step-runtime convention for steps
    bound at execution time) resolve through it.
    """
    if claimed.flow_run_id is not None:
        return claimed.flow_run_id
    raw = claimed.payload.get("run_id")
    return str(raw) if raw else None


async def _stale_claim_reason(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedStep,
) -> str | None:
    """Why *claimed* no longer owns its step row, or ``None`` while it does (A05).

    The pre-handler-entry re-validation: the row must still show ``running``
    under THIS owner and fence token with a live unexpired lease — a claim
    that died while queued (the batch>1 queuing case) or was reaped and
    reassigned while the worker was busy must never reach the handler. The
    effect-side siblings of this check are the A04 guarded CASes and the
    publisher's reservation-time ownership validation.
    """
    async with session_factory() as session:
        row = await session.get(StepRun, claimed.id)
    if row is None:
        return "step row vanished"
    if row.status != STEP_RUNNING:
        return f"step is {row.status!r}, not running"
    if row.lease_owner != claimed.owner:
        return f"lease owner is {row.lease_owner!r}, not {claimed.owner!r}"
    if int(row.fence_token) != claimed.fence_token:
        return f"fence moved to {row.fence_token}"
    if row.lease_expires_at is None or as_aware_utc(row.lease_expires_at) <= _utcnow():
        return "lease expired"
    return None


async def _requeue_expired_claim(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedStep,
) -> str:
    """Fenced requeue of a claim whose lease died BEFORE its handler ran (A05).

    The reaper's semantics, scoped to this one row: the expired-lease attempt
    is bumped once and a step that exhausts its poison-pill budget parks
    ``dead`` — so a claim cannot be requeued into a busy loop. Fenced on
    (this owner, this fence, ``running``, expired lease): a row the reaper
    already reaped, or that another worker already re-claimed, matches 0 rows
    and is left entirely alone.

    Returns ``"scheduled"``, ``"dead"`` or ``"fenced"``.
    """
    now = _utcnow()
    ownership = (
        StepRun.id == claimed.id,
        StepRun.lease_owner == claimed.owner,
        StepRun.fence_token == claimed.fence_token,
        StepRun.status == STEP_RUNNING,
        StepRun.lease_expires_at < now,
    )
    async with session_factory() as session:
        async with session.begin():
            row = (
                await session.execute(
                    select(StepRun.attempt, StepRun.max_attempts).where(*ownership)
                )
            ).first()
            if row is None:
                return "fenced"  # someone else (reaper / new owner) manages the row now
            new_attempt = int(row.attempt) + 1
            if new_attempt >= int(row.max_attempts):
                await session.execute(
                    update(StepRun)
                    .where(*ownership)
                    .values(
                        status=STEP_DEAD,
                        attempt=new_attempt,
                        finished_at=now,
                        lease_owner=None,
                        lease_expires_at=None,
                        output=case(
                            (
                                StepRun.output.is_(None),
                                type_coerce(
                                    {"error": _EXPIRED_BEFORE_HANDLER_ERROR}, StepRun.output.type
                                ),
                            ),
                            else_=StepRun.output,
                        ),
                    )
                )
                return "dead"
            await session.execute(
                update(StepRun)
                .where(*ownership)
                .values(
                    status=STEP_SCHEDULED,
                    attempt=new_attempt,
                    due_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
            return "scheduled"


async def _release_unstarted_claim(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedStep,
) -> bool:
    """Hand a claimed-but-NEVER-STARTED step back to the queue (A05 shutdown).

    A worker holding a claim it will not run (SIGTERM mid-pass, task
    cancelled) must neither leave the row leased-and-rotting nor mark it
    failed — nothing was attempted, so there is no attempt bump and no error.
    Fenced on this claim's owner+fence+``running``: a row the reaper already
    reaped or another worker re-claimed is never clobbered. The fence token
    stays — it bumps only on the lease-granting claim.
    """
    async with session_factory() as session:
        async with session.begin():
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(StepRun)
                    .where(
                        StepRun.id == claimed.id,
                        StepRun.lease_owner == claimed.owner,
                        StepRun.fence_token == claimed.fence_token,
                        StepRun.status == STEP_RUNNING,
                    )
                    .values(
                        status=STEP_SCHEDULED,
                        lease_owner=None,
                        lease_expires_at=None,
                    )
                ),
            )
            return result.rowcount == 1


async def _superseded_evidence(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedStep,
) -> dict[str, Any] | None:
    """Evidence that this execution finished AFTER its run moved on (R10/R17).

    A fenced handler completing late — its run already terminal, or its
    publication grant cancelled away (generation bumped) — records WHY its
    result is superseded instead of presenting a plain success. Returns
    ``None`` while the claim is still fresh.
    """
    run_id = _bound_run_id(claimed)
    if run_id is None:
        return None
    async with session_factory() as session:
        run = await session.get(FlowRun, run_id)
    if run is None:
        return None
    terminal = run.status in {status.value for status in TERMINAL_STATUSES}
    revoked = (
        claimed.cancellation_generation is not None
        and int(run.cancellation_generation) != claimed.cancellation_generation
    )
    if not terminal and not revoked:
        return None
    reason = "run_terminal" if terminal else "publication_grant_cancelled"
    return {
        "superseded": {
            "reason": reason,
            "run_status": run.status,
            "fence_token": claimed.fence_token,
            "claimed_generation": claimed.cancellation_generation,
            "run_generation": int(run.cancellation_generation),
        }
    }


async def execute_claimed_step(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Any,
    forge_config: Any,
    claimed: ClaimedStep,
    *,
    heartbeat_interval: float = STEP_LEASE_RENEW_INTERVAL,
) -> None:
    """Execute one claimed step (re-validate → call → fenced commit; §8, A05).

    The claim is RE-VALIDATED immediately before the handler: a step whose
    lease died while its claim sat queued is requeued (or parked dead per
    attempts) instead of executing unowned. The external call runs outside
    any DB transaction. The per-task heartbeat is SUPERVISED: a renewal that
    hits 0 rows — or RAISES (DB down) — fails the handler; the remaining
    effects stop at the handler's next checkpoint (the A04 guarded write
    boundaries refuse a dead claim), the step is failed into a recoverable
    retry and :class:`LeaseLostError` is raised. A CancelledError that is NOT
    a lease loss — worker shutdown/process teardown — propagates untouched
    and records nothing (the reaper owns the row). Completion/failure are
    fenced — a fenced owner abandons silently. The claim is bound as the
    task's ambient :class:`~forge.durable.claims.ExecutionClaim` for the
    dispatch, so effectful handlers and the publication boundary fence
    against THIS execution (R10).
    """
    stale = await _stale_claim_reason(session_factory, claimed)
    if stale is not None:
        requeue = await _requeue_expired_claim(session_factory, claimed)
        logger.warning(
            "Step %d claim stale before handler entry (%s, requeue=%s) — skipped unexecuted",
            claimed.id,
            stale,
            requeue,
        )
        return

    me = asyncio.current_task()
    lease_lost: list[str] = []

    async def _heartbeat() -> None:
        while True:
            await asyncio.sleep(heartbeat_interval)
            alive: bool
            failure: str | None
            try:
                alive = await renew_step_lease(session_factory, claimed)
                failure = None if alive else "lease lost: renewal matched 0 rows"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # DB down mid-renewal: ownership can no longer be proven
                alive = False
                failure = f"lease lost: renewal failed: {exc}"
            if alive:
                continue
            assert failure is not None
            lease_lost.append(failure)
            logger.warning(
                "Step %d %s (owner=%s fence=%d) — aborting execution",
                claimed.id,
                failure,
                claimed.owner,
                claimed.fence_token,
            )
            if me is not None:
                me.cancel()  # stop the handler's remaining effects at its next checkpoint
            return

    heartbeat = asyncio.create_task(_heartbeat())
    try:
        with bind_claim(execution_claim(claimed)):
            await execute_run_command(settings, forge_config, session_factory, claimed.payload)
    except asyncio.CancelledError:
        if lease_lost:
            # Lease loss, not shutdown: fail the step (recoverable retry) and
            # convert the self-inflicted cancellation into a typed failure —
            # the queue loop must never mistake this for worker teardown.
            if me is not None:
                me.uncancel()
            outcome = await fail_step(session_factory, claimed, lease_lost[0])
            logger.warning(
                "Step %d (%s) attempt %d/%d aborted — %s (%s)",
                claimed.id,
                claimed.step_name,
                claimed.attempt + 1,
                claimed.max_attempts,
                lease_lost[0],
                outcome,
            )
            raise LeaseLostError(lease_lost[0]) from None
        raise
    except Exception as exc:
        outcome = await fail_step(session_factory, claimed, str(exc))
        logger.warning(
            "Step %d (%s) attempt %d/%d failed (%s): %s",
            claimed.id,
            claimed.step_name,
            claimed.attempt + 1,
            claimed.max_attempts,
            outcome,
            exc,
        )
        raise
    finally:
        # Awaited teardown (A05): the heartbeat never dies unobserved — its
        # outcome is either consumed above or cancelled deliberately here.
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
    # R10: a completion that lands after its run moved on carries superseded
    # evidence instead of a plain result — the run is never walked out of a
    # terminal state by a late callback.
    output = await _superseded_evidence(session_factory, claimed)
    if output is not None:
        logger.info(
            "Step %d completed late — result superseded (%s)",
            claimed.id,
            output["superseded"]["reason"],
        )
    if not await complete_step(session_factory, claimed, output=output):
        logger.warning(
            "Step %d completion fenced (owner=%s fence=%d) — abandoned",
            claimed.id,
            claimed.owner,
            claimed.fence_token,
        )


async def _release_remaining(
    session_factory: async_sessionmaker[AsyncSession],
    unstarted: list[ClaimedStep],
    why: str,
) -> None:
    """Best-effort handback of never-started claims (A05 shutdown/cancel).

    Every failure here is survivable: the reaper recovers an abandoned lease
    once this worker is gone, so a failed handback only costs one reap cycle.
    """
    for step in unstarted:
        try:
            await _release_unstarted_claim(session_factory, step)
        except Exception:
            logger.warning(
                "Step %d handback failed (%s) — left to the reaper",
                step.id,
                why,
                exc_info=True,
            )


async def run_due_steps(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Any,
    forge_config: Any,
    *,
    owner: str,
    limit: int = STEP_CLAIM_BATCH,
    shutdown_event: asyncio.Event | None = None,
) -> int:
    """One bounded pass: claim due steps and execute them (at-least-once).

    A05: the shipped worker executes sequentially, so it claims ONE step per
    pass (``STEP_CLAIM_BATCH = 1``) — a lease is held only while it is
    actually being executed, and no unstarted steps rot on expiring leases. A
    larger *limit* is safe only with a bounded-parallel executor: every claim
    is still re-validated immediately before its handler, and claims the pass
    will not start (shutdown between steps, or a cancellation) are handed
    back to the queue un-executed and un-failed. Returns the number of steps
    claimed by this pass.
    """
    claimed = await claim_due_steps(session_factory, owner, limit=limit)
    for index, step in enumerate(claimed):
        if shutdown_event is not None and shutdown_event.is_set():
            # SIGTERM mid-pass: the remaining pre-claimed steps go back to
            # the queue unstarted — never started, never failed.
            await _release_remaining(session_factory, claimed[index:], "shutdown")
            break
        try:
            await execute_claimed_step(session_factory, settings, forge_config, step)
        except LeaseLostError as exc:
            # fail_step already recorded the recoverable retry on the row.
            logger.warning("Step %d aborted — %s", step.id, exc)
        except asyncio.CancelledError:
            # Worker teardown / process death, not a step failure: the
            # in-flight step stays with the reaper (nothing recorded — the
            # ADR-0017 §5 contract), the never-started claims are handed
            # back, and the cancellation propagates so the queue loop stops
            # instead of rolling into the next pre-claimed task.
            await _release_remaining(session_factory, claimed[index + 1 :], "cancelled")
            raise
        except Exception:
            # Retry/dead was already recorded on the step row by fail_step.
            logger.exception("Step %d execution failed", step.id)
    return len(claimed)


async def run_pending_command_step(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Any,
    forge_config: Any,
    source_event_id: str,
    *,
    owner: str,
) -> None:
    """Execute the persisted step for a run command in-process.

    The no-Redis gateway fallback's BackgroundTasks target: it goes through
    the same claim → execute → fenced-complete protocol as the worker, so a
    re-delivered command finds the step already owned or succeeded.
    """
    claimed = await claim_command_step(session_factory, owner, source_event_id)
    if claimed is None:
        logger.info(
            "No claimable step for command %s — already owned or executed",
            source_event_id[:12],
        )
        return
    await execute_claimed_step(session_factory, settings, forge_config, claimed)


# ----------------------------------------------------------------------
# Worker loops (wired alongside the legacy Redis queue loop)
# ----------------------------------------------------------------------


async def run_step_worker(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Any,
    forge_config: Any,
    worker_id: str,
    shutdown_event: asyncio.Event,
    redis_manager: Any | None = None,
    *,
    poll_interval: float = STEP_POLL_INTERVAL,
) -> None:
    """Step-runtime loop: claim due steps from Postgres forever.

    Redis is only a wake-up accelerator — a consumed wake shortens the poll;
    a lost wake (or Redis being down entirely) never delays work by more
    than one poll interval.
    """
    logger.info("Step worker %s started (interval=%ss)", worker_id, poll_interval)
    while not shutdown_event.is_set():
        if redis_manager is not None:
            try:
                # Any single worker consumes each wake; the rest keep polling.
                await redis_manager.blpop(STEP_WAKE_KEY, timeout=poll_interval)
            except Exception:
                logger.warning("Step wake listen failed — polling only", exc_info=True)
                await asyncio.sleep(poll_interval)
        else:
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                pass  # Interval elapsed — next tick.
        try:
            # A05: the sequential default claims one step per pass — keep
            # claiming back-to-back while the queue keeps yielding work, so a
            # backlog does not drain at one step per poll interval. The pass
            # itself re-checks the shutdown event between steps and hands
            # never-started claims back.
            while not shutdown_event.is_set():
                processed = await run_due_steps(
                    session_factory,
                    settings,
                    forge_config,
                    owner=worker_id,
                    shutdown_event=shutdown_event,
                )
                if not processed:
                    break  # nothing due — wait for the next interval/wake
        except Exception:
            # A failed pass must never kill the step worker.
            logger.exception("Step worker pass failed")
    logger.info("Step worker %s stopped", worker_id)


async def run_step_reaper(
    session_factory: async_sessionmaker[AsyncSession],
    shutdown_event: asyncio.Event,
    *,
    interval: float = STEP_REAP_INTERVAL,
) -> None:
    """Periodically reschedule steps whose worker died mid-flight."""
    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            break  # Event was set
        except asyncio.TimeoutError:
            pass  # Interval elapsed, do work

        try:
            count = await reschedule_expired_leases(session_factory)
            if count:
                logger.warning("Step reaper rescheduled %d step(s) with expired leases", count)
            deadline_count = await reap_deadline_exceeded(session_factory)
            if deadline_count:
                logger.warning("Step reaper parked %d step(s) past their deadline", deadline_count)
        except Exception:
            logger.error("Step reaper error", exc_info=True)
