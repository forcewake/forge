"""Initial command mutations share the command's live transaction and decision."""

from datetime import datetime
import json

from sqlalchemy.ext.asyncio import AsyncSession

from forge.command_values import CommandEnvelope, CommandLease
from forge.port_values import TaskSnapshot
from forge.store.approval_values import ApprovalClaim
from forge.store.command_runs import target_runs
from forge.store.commands import command_fence
from forge.store.models_commands import EngineCommand
from forge.store.models_runs import EngineRun
from forge.store.records import RunRequest, StepRequest, StoreConflict


async def admission_command(
    session: AsyncSession, lease: CommandLease, request: RunRequest, payload: str, now: datetime
) -> EngineCommand:
    row = await command_fence(session, lease, now)
    envelope = CommandEnvelope.model_validate_json(row.document_json)
    data = json.loads(payload)
    task = TaskSnapshot.model_validate_json(json.dumps(data["task"]))
    repo = task.subject.repo.identity
    if (
        envelope.command.kind != "implement"
        or envelope.target != task.subject
        or data["actor"] != envelope.actor.model_dump(mode="json")
        or (
            request.provider,
            request.connection_id,
            request.repository_id,
            request.subject_kind,
            request.subject_id,
        )
        != (
            repo.provider,
            repo.connection_id,
            repo.repository_id,
            task.subject.kind,
            str(task.subject.number),
        )
    ):
        raise StoreConflict("implementation command differs from admitted authority")
    if row.result_json is not None:
        raise StoreConflict("implementation command was already decided")
    return row


async def approval_command(
    session: AsyncSession, lease: CommandLease, request: StepRequest, now: datetime
) -> EngineCommand:
    row = await command_fence(session, lease, now)
    envelope = CommandEnvelope.model_validate_json(row.document_json)
    claim = ApprovalClaim.model_validate_json(request.payload_json)
    run = await session.get(EngineRun, request.run_id)
    if (
        run is None
        or run.root_run_id != run.id
        or envelope.command.kind != "go"
        or envelope.actor != claim.actor
        or request.run_id not in {run.id for run in await target_runs(session, envelope)}
        or envelope.command.argument.strip() not in ("", request.run_id)
    ):
        raise StoreConflict("approval command differs from the original subject or actor")
    if row.result_json is not None:
        raise StoreConflict("approval command was already decided")
    return row
