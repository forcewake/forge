"""Durable lane controls share the command decision's transaction and fence."""

from collections.abc import Mapping
from datetime import datetime
import json
from uuid import uuid4

from forge_protocol import ControlCommand, RunIdentity
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.command_values import CommandEnvelope, CommandLease, CommandResult
from forge.domain.run_spec import RunSpec2
from forge.domain.states import State
from forge.store.command_runs import target_runs
from forge.store.commands import command_fence, resolve_in
from forge.store.lane_auth import current, encoded
from forge.store.models_lane import EngineControlCommand, EngineLaneSession
from forge.store.models_commands import EngineCommand
from forge.store.models_runs import EngineRun, EngineRunSpec
from forge.store.records import StoreConflict
from forge.store.transactions import SessionFactory, locked_run, transaction


class ControlStore:
    def __init__(self, factory: SessionFactory, capabilities: Mapping[str, frozenset[str]]) -> None:
        self.factory, self.capabilities = factory, dict(capabilities)

    async def control(self, lease: CommandLease, run_id: str, *, now: datetime) -> CommandResult:
        async with transaction(self.factory) as session:
            command = await command_fence(session, lease, now)
            if command.result_json is not None:
                return CommandResult.model_validate_json(command.result_json)
            envelope = CommandEnvelope.model_validate_json(command.document_json)
            kind = envelope.command.kind
            if kind not in {"pause", "steer"}:
                raise StoreConflict("command is not a lane control")
            run = await locked_run(session, run_id)
            if run.id not in {row.id for row in await target_runs(session, envelope)}:
                raise StoreConflict("run_scope_mismatch")
            reason = None
            if (kind == "pause" and envelope.command.argument.strip()) or (
                kind == "steer" and not envelope.command.argument.strip()
            ):
                reason = "malformed_command"
            spec_row = await session.scalar(
                select(EngineRunSpec).where(EngineRunSpec.run_id == run.id)
            )
            lane = await session.scalar(
                select(EngineLaneSession).where(
                    EngineLaneSession.run_id == run.id,
                    EngineLaneSession.attempt == run.attempt,
                    EngineLaneSession.generation == run.generation,
                )
            )
            if (
                spec_row is None
                or lane is None
                or run.state != State.RUNNING.value
                or run.cancel_requested
            ):
                reason = reason or "lane_not_active"
            if reason is None:
                if spec_row is None or lane is None:
                    raise StoreConflict("active control context is missing")
                spec = RunSpec2.model_validate_json(spec_row.document_json)
                if spec.digest != spec_row.digest:
                    raise StoreConflict("frozen control policy is corrupt")
                capability = "steer" if kind == "steer" else "interrupt"
                if capability not in self.capabilities.get(spec.driver, frozenset()):
                    reason = "capability_unavailable"
                elif json.loads(lane.api_state_json).get("report") is not None:
                    reason = "lane_not_active"
            if reason is not None:
                result = CommandResult(status="refused", reason=reason, run_id=run.id)
            else:
                if lane is None:
                    raise StoreConflict("active lane session is missing")
                result = await queue_control(session, command, run, lane, envelope)
            resolve_in(command, result)
            return result


async def queue_control(
    session: AsyncSession,
    command: EngineCommand,
    run: EngineRun,
    lane: EngineLaneSession,
    envelope: CommandEnvelope,
) -> CommandResult:
    kind = envelope.command.kind
    if kind != "pause" and kind != "steer":
        raise StoreConflict("command is not a lane control")
    pending = current(
        run,
        lane,
        RunIdentity(
            forge_run_id=run.id,
            attempt=run.attempt,
            generation=run.generation,
        ),
    )
    sequence = 1 + (
        await session.scalar(
            select(func.max(EngineControlCommand.sequence)).where(
                EngineControlCommand.run_id == run.id,
                EngineControlCommand.attempt == run.attempt,
            )
        )
        or 0
    )
    if sequence > 256:
        raise StoreConflict("lane control count exceeds its bound")
    value = ControlCommand(
        forge_run_id=run.id,
        attempt=run.attempt,
        generation=run.generation,
        command_id=command.id,
        sequence=sequence,
        action=kind,
        text=envelope.command.argument if kind == "steer" else None,
    )
    session.add(
        EngineControlCommand(
            id=uuid4().hex,
            run_id=run.id,
            command_key=value.command_id,
            attempt=run.attempt,
            generation=run.generation,
            sequence=sequence,
            world_version=run.version,
            payload_json=encoded(value.model_dump(mode="json")),
            deadline_at=pending.expires_at,
        )
    )
    return CommandResult(
        status="accepted",
        reason="control_queued",
        run_id=run.id,
        data_json=encoded({"control_id": command.id, "sequence": sequence}),
    )
