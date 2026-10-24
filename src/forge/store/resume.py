"""Resume only exact paused WIP, advancing the generation inside one transaction."""

from datetime import datetime
import json

from forge_protocol import CheckpointManifest, LaneDispatch, LaneReport, RepairContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.command_values import CommandEnvelope, CommandLease, CommandResult
from forge.domain.states import Handler, State
from forge.engine.lane_values import DispatchInput
from forge.store.command_runs import target_runs
from forge.store.commands import command_fence, resolve_in
from forge.store.lane_auth import approved_brief
from forge.store.models_execution import EngineStep
from forge.store.models_lane import EngineCheckpoint, EngineLaneSession
from forge.store.models_runs import EngineRun
from forge.store.records import StepRequest, StoreConflict
from forge.store.retry import retry_reason
from forge.store.steps import enqueue_in
from forge.store.transactions import SessionFactory, locked_run, transaction


class ResumeStore:
    def __init__(self, factory: SessionFactory) -> None:
        self.factory = factory

    async def resume(
        self, lease: CommandLease, run_id: str, observed_head: str | None, *, now: datetime
    ) -> CommandResult:
        async with transaction(self.factory) as session:
            command = await command_fence(session, lease, now)
            if command.result_json is not None:
                return CommandResult.model_validate_json(command.result_json)
            envelope = CommandEnvelope.model_validate_json(command.document_json)
            if envelope.command.kind != "resume" or envelope.command.argument.strip():
                raise StoreConflict("malformed_command")
            run = await locked_run(session, run_id)
            if run.id not in {row.id for row in await target_runs(session, envelope)}:
                raise StoreConflict("run_scope_mismatch")
            reason = await retry_reason(session, run)
            if reason not in (None, "not_retryable"):
                raise StoreConflict(reason)
            lane = await session.scalar(
                select(EngineLaneSession).where(
                    EngineLaneSession.run_id == run.id,
                    EngineLaneSession.attempt == run.attempt,
                    EngineLaneSession.generation == run.generation,
                )
            )
            if run.state != State.BLOCKED.value or run.reason != "lane_paused" or lane is None:
                raise StoreConflict("checkpoint_unavailable")
            manifest, dispatch = await paused_checkpoint(session, run, lane)
            if observed_head != manifest.base_sha:
                raise StoreConflict("branch_drift")
            brief, _ = await approved_brief(session, dispatch)
            repair = (
                RepairContext.model_validate(
                    {**brief.repair.model_dump(), "generation": run.generation + 1}
                )
                if brief.repair
                else None
            )
            work = DispatchInput(
                approved_digest=brief.approved_digest,
                revision=brief.revision,
                checkpoint_id=manifest.checkpoint_id,
                repair=repair,
            )
            old_steps = await session.scalars(
                select(EngineStep).where(
                    EngineStep.run_id == run.id,
                    EngineStep.state.in_(("scheduled", "running")),
                )
            )
            for step in old_steps:
                step.state, step.lease_owner, step.lease_expires_at = "cancelled", None, None
            run.generation, run.version = run.generation + 1, run.version + 1
            run.state, run.failed_handler, run.reason = State.RUNNING.value, None, "operator_resume"
            await enqueue_in(
                session,
                run,
                StepRequest(
                    run.id,
                    Handler.DISPATCH,
                    f"{run.id}:{run.attempt}:{run.generation}:resume:{command.id}",
                    work.canonical_bytes.decode(),
                    now,
                ),
            )
            result = CommandResult(status="accepted", reason="resume_scheduled", run_id=run.id)
            resolve_in(command, result)
            return result


async def paused_checkpoint(
    session: AsyncSession,
    run: EngineRun,
    lane: EngineLaneSession,
) -> tuple[CheckpointManifest, LaneDispatch]:
    try:
        report = LaneReport.model_validate(json.loads(lane.api_state_json).get("report"))
        dispatch = LaneDispatch.model_validate_json(lane.dispatch_json)
    except (ValueError, TypeError):
        raise StoreConflict("checkpoint_unavailable") from None
    checkpoint = await session.get(EngineCheckpoint, report.checkpoint_id)
    if report.status != "paused" or checkpoint is None or not checkpoint.retained:
        raise StoreConflict("checkpoint_unavailable")
    try:
        manifest = CheckpointManifest.model_validate_json(checkpoint.manifest_json)
    except (ValueError, TypeError):
        raise StoreConflict("checkpoint_unavailable") from None
    identity = (run.id, run.attempt, run.generation)
    if (
        manifest.checkpoint_id != report.checkpoint_id
        or checkpoint.digest != manifest.checkpoint_id
        or (manifest.forge_run_id, manifest.attempt, manifest.generation) != identity
        or (checkpoint.run_id, checkpoint.attempt, checkpoint.generation) != identity
        or (dispatch.forge_run_id, dispatch.attempt, dispatch.generation) != identity
        or (report.forge_run_id, report.attempt, report.generation) != identity
        or manifest.base_sha != dispatch.execution_base_sha
        or manifest.approved_digest != dispatch.approved_digest
        or report.approved_digest != dispatch.approved_digest
        or manifest.driver != dispatch.driver
        or report.driver != dispatch.driver
        or report.model != dispatch.model
        or dispatch.provider != run.provider
        or lane.cancellation_generation != run.cancellation_generation
    ):
        raise StoreConflict("checkpoint_unavailable")
    return manifest, dispatch
