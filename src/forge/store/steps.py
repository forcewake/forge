"""Fenced step claims; completion and successor scheduling commit together."""

from datetime import datetime, timedelta, timezone
from typing import Literal, cast
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.states import TERMINAL, Handler, State, transition
from forge.store.models_execution import EngineStep
from forge.store.models_runs import EngineRun
from forge.store.records import Lease, StepRecord, StepRequest, StepState, StoreConflict
from forge.store.transactions import (
    SessionFactory,
    aware,
    canonical_json,
    fence,
    locked_run,
    notify_steps,
    stored_time,
    transaction,
)


def step_record(row: EngineStep) -> StepRecord:
    return StepRecord(
        row.id,
        row.key,
        row.run_id,
        Handler(row.handler),
        row.payload_json,
        cast(StepState, row.state),
        stored_time(row.due_at),
        stored_time(row.deadline_at) if row.deadline_at is not None else None,
        row.attempts,
        row.max_attempts,
        row.lease_epoch,
        row.result_json,
    )


def lease_record(row: EngineStep) -> Lease:
    assert row.lease_owner is not None and row.lease_expires_at is not None
    return Lease(
        row.id,
        row.run_id,
        Handler(row.handler),
        row.lease_epoch,
        row.lease_owner,
        stored_time(row.lease_expires_at),
        row.attempt,
        row.generation,
        row.cancellation_generation,
    )


def validate_request(request: StepRequest) -> None:
    canonical_json(request.payload_json)
    aware(request.due_at)
    if not request.key or len(request.key) > 300 or request.max_attempts <= 0:
        raise ValueError("invalid step key or attempt bound")
    if request.deadline_at is not None and aware(request.deadline_at) <= aware(request.due_at):
        raise ValueError("step deadline must be after its due time")


async def enqueue_in(session: AsyncSession, run: EngineRun, request: StepRequest) -> EngineStep:
    if (
        request.run_id != run.id
        or run.cancel_requested
        or run.state in {state.value for state in TERMINAL}
    ):
        raise StoreConflict("successor run is not active")
    row = await session.scalar(select(EngineStep).where(EngineStep.key == request.key))
    identity = (run.attempt, run.generation, run.cancellation_generation)
    if row is not None:
        previous = (
            row.run_id,
            row.handler,
            row.payload_json,
            row.max_attempts,
            stored_time(row.due_at),
            stored_time(row.deadline_at) if row.deadline_at is not None else None,
            row.attempt,
            row.generation,
            row.cancellation_generation,
        )
        supplied = (
            request.run_id,
            request.handler.value,
            request.payload_json,
            request.max_attempts,
            aware(request.due_at),
            aware(request.deadline_at) if request.deadline_at is not None else None,
            *identity,
        )
        if previous != supplied:
            raise StoreConflict("step key reused with different frozen inputs")
        return row
    row = EngineStep(
        id=uuid4().hex,
        key=request.key,
        run_id=run.id,
        handler=request.handler.value,
        payload_json=request.payload_json,
        due_at=aware(request.due_at),
        deadline_at=aware(request.deadline_at) if request.deadline_at is not None else None,
        max_attempts=request.max_attempts,
        attempt=identity[0],
        generation=identity[1],
        cancellation_generation=identity[2],
    )
    session.add(row)
    await session.flush()
    await notify_steps(session)
    return row


async def claim_one(
    session: AsyncSession, row: EngineStep, owner: str, now: datetime, lease_seconds: float
) -> Lease | None:
    run = await locked_run(session, row.run_id)
    if run.cancel_requested or State(run.state) in TERMINAL:
        return None
    expired = row.deadline_at is not None and stored_time(row.deadline_at) <= now
    if row.attempts >= row.max_attempts or expired:
        row.state, row.lease_owner, row.lease_expires_at = "dead", None, None
        run.state = transition(State(run.state), State.BLOCKED).value
        run.failed_handler = row.handler
        run.reason = "step_deadline_expired" if expired else "step_attempts_exhausted"
        run.version += 1
        return None
    row.state, row.lease_owner = "running", owner
    row.lease_epoch += 1
    row.attempts += 1
    expiry = now + timedelta(seconds=lease_seconds)
    row.lease_expires_at = (
        min(expiry, stored_time(row.deadline_at)) if row.deadline_at is not None else expiry
    )
    return lease_record(row)


class StepStore:
    def __init__(self, factory: SessionFactory) -> None:
        self.factory = factory

    async def enqueue(self, request: StepRequest) -> StepRecord:
        validate_request(request)
        async with transaction(self.factory) as session:
            run = await locked_run(session, request.run_id)
            return step_record(await enqueue_in(session, run, request))

    async def get(self, key: str) -> StepRecord | None:
        async with self.factory() as session:
            row = await session.scalar(select(EngineStep).where(EngineStep.key == key))
            return step_record(row) if row is not None else None

    async def load(self, step_id: str) -> StepRecord | None:
        async with self.factory() as session:
            row = await session.get(EngineStep, step_id)
            return step_record(row) if row is not None else None

    async def assert_current(self, lease: Lease, *, now: datetime) -> None:
        async with transaction(self.factory) as session:
            await fence(session, lease, now)

    async def claim_due(
        self, owner: str, now: datetime, lease_seconds: float, limit: int = 1
    ) -> tuple[Lease, ...]:
        now = aware(now)
        if not owner or len(owner) > 200 or not 0 < lease_seconds <= 3600 or not 1 <= limit <= 100:
            raise ValueError("invalid worker identity or claim bound")
        due = or_(
            EngineStep.state == "scheduled",
            and_(EngineStep.state == "running", EngineStep.lease_expires_at <= now),
        )
        async with transaction(self.factory) as session:
            rows = (
                await session.scalars(
                    select(EngineStep)
                    .join(EngineRun, EngineRun.id == EngineStep.run_id)
                    .where(
                        due,
                        EngineStep.due_at <= now,
                        EngineRun.cancel_requested.is_(False),
                        EngineRun.state.not_in(tuple(state.value for state in TERMINAL)),
                        EngineStep.attempt == EngineRun.attempt,
                        EngineStep.generation == EngineRun.generation,
                        EngineStep.cancellation_generation == EngineRun.cancellation_generation,
                    )
                    .order_by(EngineStep.due_at, EngineStep.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True, of=(EngineRun, EngineStep))
                )
            ).all()
            leases: list[Lease] = []
            for row in rows:
                lease = await claim_one(session, row, owner, now, lease_seconds)
                if lease is not None:
                    leases.append(lease)
            await session.flush()
            return tuple(leases)

    async def renew(self, lease: Lease, *, now: datetime, lease_seconds: float) -> Lease:
        if not 0 < lease_seconds <= 3600:
            raise ValueError("invalid lease duration")
        async with transaction(self.factory) as session:
            row = await fence(session, lease, now)
            expiry = aware(now) + timedelta(seconds=lease_seconds)
            row.lease_expires_at = (
                min(expiry, stored_time(row.deadline_at)) if row.deadline_at is not None else expiry
            )
            await session.flush()
            return lease_record(row)

    async def complete_and_enqueue(
        self,
        lease: Lease,
        result_json: str,
        successors: tuple[StepRequest, ...] = (),
        *,
        now: datetime | None = None,
        transition_to: State | None = None,
        reason: str | None = None,
        outcome: Literal["succeeded", "failed"] = "succeeded",
    ) -> StepRecord:
        canonical_json(result_json)
        for request in successors:
            validate_request(request)
        async with transaction(self.factory) as session:
            row = await fence(session, lease, now or datetime.now(timezone.utc))
            run = await locked_run(session, lease.run_id)
            for request in successors:
                await enqueue_in(session, run, request)
            if transition_to is not None:
                run.state = transition(State(run.state), transition_to).value
                run.failed_handler = (
                    row.handler if transition_to in {State.BLOCKED, State.FAILED} else None
                )
                run.reason = reason
                run.version += 1
            row.state, row.result_json = outcome, result_json
            row.lease_owner, row.lease_expires_at = None, None
            await session.flush()
            return step_record(row)

    async def reschedule(
        self, lease: Lease, *, due_at: datetime, now: datetime, reason: str = "retry_exhausted"
    ) -> StepRecord:
        due_at = aware(due_at)
        if due_at < aware(now):
            raise ValueError("retry cannot be due in the past")
        async with transaction(self.factory) as session:
            row = await fence(session, lease, now)
            exhausted = row.attempts >= row.max_attempts or (
                row.deadline_at is not None and due_at >= stored_time(row.deadline_at)
            )
            row.state = "dead" if exhausted else "scheduled"
            if exhausted:
                run = await locked_run(session, lease.run_id)
                run.state = transition(State(run.state), State.BLOCKED).value
                run.failed_handler, run.reason = row.handler, reason
                run.version += 1
            row.due_at, row.lease_owner, row.lease_expires_at = due_at, None, None
            await session.flush()
            await notify_steps(session)
            return step_record(row)
