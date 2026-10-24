"""Accepted pauses fence publication until their exact checkpoint resume decision."""

from forge_protocol import CheckpointManifest, ControlCommand, canonical_json
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.command_values import CommandEnvelope, CommandResult
from forge.engine.lane_values import DispatchInput
from forge.store.models_commands import EngineCommand
from forge.store.models_execution import EngineStep
from forge.store.models_runs import EngineRun
from forge.store.models_lane import EngineCheckpoint, EngineControlCommand
from forge.store.records import Lease, StoreConflict


class PublicationPaused(StoreConflict):
    """A durable accepted pause still owns its publication fence."""


async def accepted_command(
    session: AsyncSession, command_id: str, run_id: str, kind: str, reason: str
) -> bool:
    row = await session.get(EngineCommand, command_id)
    if row is None or row.run_id != run_id or row.result_json is None:
        return False
    try:
        envelope = CommandEnvelope.model_validate_json(row.document_json)
        result = CommandResult.model_validate_json(row.result_json)
        return (
            row.state in ("resolved", "completed")
            and row.source_key == envelope.source_key
            and row.kind == envelope.command.kind == kind
            and (
                bool(envelope.command.argument.strip())
                if kind == "steer"
                else not envelope.command.argument.strip()
            )
            and (result.status, result.reason, result.run_id) == ("accepted", reason, run_id)
        )
    except (ValueError, TypeError):
        return False


async def resumed(
    session: AsyncSession, pause: EngineControlCommand, lease: Lease | EngineRun
) -> bool:
    if (lease.attempt, lease.generation) <= (pause.attempt, pause.generation):
        return False
    steps = await session.scalars(
        select(EngineStep).where(
            EngineStep.run_id == pause.run_id,
            EngineStep.handler == "dispatch",
            EngineStep.attempt == pause.attempt,
            EngineStep.generation == pause.generation + 1,
        )
    )
    prefix = f"{pause.run_id}:{pause.attempt}:{pause.generation + 1}:resume:"
    for step in steps:
        if not step.key.startswith(prefix) or not await accepted_command(
            session, step.key[len(prefix) :], pause.run_id, "resume", "resume_scheduled"
        ):
            continue
        try:
            work = DispatchInput.model_validate_json(step.payload_json)
            checkpoint = (
                await session.get(EngineCheckpoint, work.checkpoint_id)
                if work.checkpoint_id
                else None
            )
            if checkpoint is None or not checkpoint.retained:
                continue
            manifest = CheckpointManifest.model_validate_json(checkpoint.manifest_json)
            command = await session.get(EngineCommand, step.key[len(prefix) :])
            if command is None or command.result_json is None:
                continue
            decision = CommandResult.model_validate_json(command.result_json)
            if (
                (checkpoint.run_id, checkpoint.attempt, checkpoint.generation)
                == (pause.run_id, pause.attempt, pause.generation)
                == (manifest.forge_run_id, manifest.attempt, manifest.generation)
                and checkpoint.id
                == checkpoint.digest
                == manifest.checkpoint_id
                == work.checkpoint_id
                and manifest.approved_digest == work.approved_digest == checkpoint.approved_digest
                and checkpoint.base_sha == manifest.base_sha
                and checkpoint.driver == manifest.driver
                and decision.data_json
                == canonical_json(
                    {
                        "checkpoint_id": checkpoint.id,
                        "attempt": pause.attempt,
                        "from_generation": pause.generation,
                        "generation": pause.generation + 1,
                    },
                ).decode()
            ):
                return True
        except (ValueError, TypeError):
            continue
    return False


async def require_unpaused(session: AsyncSession, lease: Lease | EngineRun) -> None:
    rows = await session.scalars(
        select(EngineControlCommand).where(
            EngineControlCommand.run_id
            == (lease.id if isinstance(lease, EngineRun) else lease.run_id)
        )
    )
    for row in rows:
        try:
            value = ControlCommand.model_validate_json(row.payload_json)
        except (ValueError, TypeError):
            raise PublicationPaused("publication_paused") from None
        if (
            value.forge_run_id,
            value.attempt,
            value.generation,
            value.sequence,
            value.command_id,
        ) != (
            row.run_id,
            row.attempt,
            row.generation,
            row.sequence,
            row.command_key,
        ) or not await accepted_command(
            session, row.command_key, row.run_id, value.action, "control_queued"
        ):
            raise PublicationPaused("publication_paused")
        if value.action == "pause" and not await resumed(session, row, lease):
            raise PublicationPaused("publication_paused")
