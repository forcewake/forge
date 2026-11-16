"""One retry refusal table and a fenced, bounded original-handler revival."""

from datetime import datetime, timedelta
import json
from uuid import uuid4

from forge_protocol import RepairContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.command_values import CommandEnvelope, CommandLease, CommandResult
from forge.domain.budget import Pool
from forge.domain.run_spec import RunSpec2
from forge.domain.states import TERMINAL, Handler, State, revive
from forge.engine.lane_values import DispatchInput
from forge.store.commands import command_fence, resolve_in
from forge.store.command_runs import target_runs
from forge.store.models_budget import EngineBudgetPool, EngineBudgetReservation
from forge.store.models_execution import EngineCandidate, EngineStep
from forge.store.pause import PublicationPaused, require_unpaused
from forge.store.models_runs import EngineApprovedInput, EngineRun, EngineRunSpec
from forge.store.records import StepRequest, StoreConflict
from forge.store.steps import enqueue_in
from forge.store.transactions import (
    SessionFactory,
    locked_run,
    stored_time,
    transaction,
    notify_steps,
)


async def retained_failure_context(session: AsyncSession, run: EngineRun) -> str:
    """Keep the check evidence that started a repair, not the latest lane error."""
    rows = await session.scalars(
        select(EngineStep)
        .where(
            EngineStep.run_id == run.id,
            EngineStep.handler == Handler.DISPATCH.value,
        )
        .order_by(EngineStep.created_at.desc(), EngineStep.id.desc())
    )
    for row in rows:
        raw = json.loads(row.payload_json).get("repair")
        text = raw.get("failure_context") if isinstance(raw, dict) else None
        if isinstance(text, str) and text.strip():
            return text
    return run.reason or "Operator-authorized retry"


async def release_unknown_holds(session: AsyncSession, run_id: str) -> None:
    """Settle unproven holds at their known lower bound inside the retry transaction."""
    pools = (
        await session.scalars(
            select(EngineBudgetPool).where(EngineBudgetPool.run_id == run_id).with_for_update()
        )
    ).all()
    for row in pools:
        pool = Pool.model_validate_json(row.state_json)
        resolved = pool.resolve_unknown()
        if resolved == pool:
            continue
        row.state_json, row.version = resolved.model_dump_json(), row.version + 1
        for reservation in resolved.reservations:
            item = await session.scalar(
                select(EngineBudgetReservation).where(
                    EngineBudgetReservation.pool_id == row.id,
                    EngineBudgetReservation.reservation_key == reservation.identity,
                )
            )
            if item is None:
                item = EngineBudgetReservation(
                    id=uuid4().hex,
                    pool_id=row.id,
                    reservation_key=reservation.identity,
                    version=1,
                )
                session.add(item)
            else:
                item.version += 1
            item.requested_json = reservation.requested.model_dump_json()
            item.minimum_json = reservation.minimum.model_dump_json()
            item.allowance_json = reservation.allowance.model_dump_json()
            item.state = "reserved" if reservation.status == "open" else reservation.status
            seen = reservation.observed
            item.observed_usd = None if seen is None else seen.usd
            item.observed_calls = None if seen is None else seen.calls
            item.observed_tokens = None if seen is None else seen.tokens


async def retry_reason(session: AsyncSession, run: EngineRun) -> str | None:
    other = await session.scalar(
        select(EngineRun.id)
        .where(
            EngineRun.subject_key == run.subject_key,
            EngineRun.id != run.id,
            EngineRun.state.not_in(tuple(state.value for state in TERMINAL)),
        )
        .limit(1)
    )
    if other is not None:
        return "another_run_active"
    if run.cancel_requested or run.state == State.CANCELLED.value:
        return "cancelled"
    if run.state not in (State.BLOCKED.value, State.FAILED.value) or run.failed_handler is None:
        return "not_retryable"
    if run.failed_handler == Handler.PLAN.value:
        return None
    approved = await session.scalar(
        select(EngineApprovedInput.id).where(EngineApprovedInput.run_id == run.id)
    )
    if approved is None:
        return "no_committed_candidate"
    if run.failed_handler in (Handler.DISPATCH.value, Handler.AWAIT_LANE.value):
        return None
    candidate = await session.scalar(
        select(EngineCandidate.id)
        .where(EngineCandidate.run_id == run.id, EngineCandidate.published_sha.is_not(None))
        .limit(1)
    )
    if candidate is None and run.failed_handler != Handler.PUBLISH.value:
        return "no_committed_candidate"
    return None


class RetryStore:
    def __init__(self, factory: SessionFactory) -> None:
        self.factory = factory

    async def explain(self, run_id: str) -> str | None:
        async with self.factory() as session:
            run = await session.get(EngineRun, run_id)
            if run is None:
                return "run_not_found"
            return await retry_reason(session, run)

    async def retry(
        self, lease: CommandLease, run_id: str, observed_head: str | None, *, now: datetime
    ) -> CommandResult:
        async with transaction(self.factory) as session:
            command = await command_fence(session, lease, now)
            if command.result_json is not None:
                return CommandResult.model_validate_json(command.result_json)
            envelope = CommandEnvelope.model_validate_json(command.document_json)
            if envelope.command.kind != "retry":
                raise StoreConflict("command is not a retry")
            run = await locked_run(session, run_id)
            if run.id not in {row.id for row in await target_runs(session, envelope)}:
                raise StoreConflict("run_scope_mismatch")
            reason = await retry_reason(session, run)
            try:
                await require_unpaused(session, run)
            except PublicationPaused:
                reason = "resume_required"
            if reason is not None:
                result = CommandResult(status="refused", reason=reason, run_id=run.id)
            else:
                await self.revive(session, command.id, run, observed_head, now)
                result = CommandResult(status="accepted", reason="retry_scheduled", run_id=run.id)
            resolve_in(command, result)
            return result

    async def revive(
        self,
        session: AsyncSession,
        command_id: str,
        run: EngineRun,
        head: str | None,
        now: datetime,
    ) -> None:
        if run.failed_handler is None:
            raise StoreConflict("failed handler identity is missing")
        handler = Handler(run.failed_handler)
        target = revive(State(run.state), handler)
        if handler in (Handler.DISPATCH, Handler.AWAIT_LANE):
            await self.lane(session, command_id, run, head, now)
            return
        row = await session.scalar(
            select(EngineStep)
            .where(
                EngineStep.run_id == run.id,
                EngineStep.handler == handler.value,
                EngineStep.attempt == run.attempt,
                EngineStep.generation == run.generation,
            )
            .order_by(EngineStep.created_at.desc(), EngineStep.id)
            .limit(1)
            .with_for_update()
        )
        if row is None:
            raise StoreConflict("failed handler has no recoverable original step")
        # Reclaim the same logical step and immutable payload, never an old intent
        # under a new step's authority. Only its lease epoch can advance on claim.
        row.state, row.due_at = "scheduled", now
        row.lease_owner, row.lease_expires_at = None, None
        row.max_attempts = max(row.max_attempts, row.attempts + 1)
        if handler == Handler.PLAN:
            await release_unknown_holds(session, run.id)
        if handler == Handler.PLAN and (
            row.deadline_at is None or stored_time(row.deadline_at) <= now
        ):
            frozen = await session.scalar(
                select(EngineRunSpec).where(EngineRunSpec.run_id == run.id)
            )
            seconds = 90
            if frozen is not None:
                seconds = int(
                    RunSpec2.model_validate_json(frozen.document_json).limits.wallclock_seconds
                )
            # The model grant stays the planning wall clock. The step deadline
            # also has to outlive one bounded handler retry, whose delay can
            # exceed that grant.
            row.deadline_at = now + timedelta(seconds=max(seconds, 3600))
        elif row.deadline_at is not None and stored_time(row.deadline_at) <= now:
            row.deadline_at = None
        run.state, run.failed_handler, run.reason = target.value, None, "operator_retry"
        run.version += 1
        await notify_steps(session)

    async def lane(
        self,
        session: AsyncSession,
        command_id: str,
        run: EngineRun,
        head: str | None,
        now: datetime,
    ) -> None:
        frozen = await session.scalar(select(EngineRunSpec).where(EngineRunSpec.run_id == run.id))
        if frozen is None:
            raise StoreConflict("frozen lane specification is missing")
        spec = RunSpec2.model_validate_json(frozen.document_json)
        if spec.digest != frozen.digest:
            raise StoreConflict("frozen retry policy is corrupt")
        from forge.domain.approved_input import ApprovedInput

        approval = ApprovedInput.from_spec(spec)
        candidate = await session.scalar(
            select(EngineCandidate)
            .where(
                EngineCandidate.run_id == run.id,
                EngineCandidate.state == "published",
                EngineCandidate.published_sha.is_not(None),
            )
            .order_by(EngineCandidate.generation.desc())
            .limit(1)
        )
        base = candidate.published_sha if candidate is not None else spec.base_sha
        if base is None:
            raise StoreConflict("published retry candidate has no native head")
        if head is not None and head != base:
            raise StoreConflict("branch_drift")
        await release_unknown_holds(session, run.id)
        attempt, generation = run.attempt + 1, run.generation + 1
        repair = (
            RepairContext(
                forge_run_id=run.id,
                attempt=attempt,
                generation=generation,
                approved_digest=approval.approved_digest,
                original_base_sha=spec.base_sha,
                candidate_sha=base,
                prior_attempt=candidate.attempt,
                prior_generation=candidate.generation,
                failure_context=await retained_failure_context(session, run),
            )
            if candidate is not None
            else None
        )
        work = DispatchInput(
            approved_digest=approval.approved_digest, revision=approval.revision, repair=repair
        )
        run.state, run.failed_handler, run.reason = State.RUNNING.value, None, "operator_retry"
        run.attempt, run.generation, run.version = attempt, generation, run.version + 1
        await enqueue_in(
            session,
            run,
            StepRequest(
                run.id,
                Handler.DISPATCH,
                f"{run.id}:{attempt}:{generation}:retry:{command_id}",
                work.canonical_bytes.decode(),
                now,
            ),
        )
