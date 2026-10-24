"""Command authority permits only its fixed reply or owned cancellation-key blocks."""

from datetime import datetime, timezone
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.command_values import CommandLease, CommandResult
from forge.store.commands import command_fence
from forge.store.effects import effect_record
from forge.store.models_commands import EngineCommand
from forge.store.models_execution import EngineEffect
from forge.store.models_budget import EngineGatewayKey
from forge.store.models_runs import EngineRun
from forge.store.records import EffectRecord, StoreConflict
from forge.store.transactions import SessionFactory, canonical_json, payload_digest, transaction


async def allowed(session: AsyncSession, command: EngineCommand, kind: str, payload: str) -> None:
    canonical_json(payload)
    if command.result_json is None:
        raise StoreConflict("command effects require a durable decision")
    if kind == "post_comment":
        if command.reply_json != payload:
            raise StoreConflict("command reply differs from the fixed resolved reply")
        return
    if kind != "gw_block" or command.kind != "cancel" or command.run_id is None:
        raise StoreConflict("command authority cannot perform this external operation")
    result = CommandResult.model_validate_json(command.result_json)
    decision = json.loads(result.data_json)
    run = await session.get(EngineRun, command.run_id)
    data = json.loads(payload)
    key = await session.scalar(
        select(EngineGatewayKey).where(
            EngineGatewayKey.alias == data.get("alias"), EngineGatewayKey.run_id == command.run_id
        )
    )
    if (
        result.status != "accepted"
        or result.reason != "cancelled"
        or run is None
        or not run.cancel_requested
        or run.state != "cancelled"
        or decision.get("cancellation_generation") != run.cancellation_generation
        or key is None
        or set(data) != {"alias"}
    ):
        raise StoreConflict("cancellation authority is not bound to this run's persisted key")


class CommandEffectStore:
    def __init__(self, factory: SessionFactory) -> None:
        self.factory = factory

    async def request_once(
        self, lease: CommandLease, *, key: str, kind: str, payload_json: str
    ) -> EffectRecord:
        async with transaction(self.factory) as session:
            command = await command_fence(session, lease, datetime.now(timezone.utc))
            await allowed(session, command, kind, payload_json)
            if not key.startswith(f"command:{command.id}:") or len(key) > 300:
                raise StoreConflict("command effect key differs from its durable owner")
            previous = await session.scalar(
                select(EngineEffect).where(EngineEffect.key == key).with_for_update()
            )
            fingerprint = payload_digest(payload_json)
            if previous is not None:
                if (
                    previous.command_id,
                    previous.kind,
                    previous.payload_digest,
                    previous.payload_json,
                    previous.step_id,
                ) != (command.id, kind, fingerprint, payload_json, None):
                    raise StoreConflict("command effect intent cannot change")
                return effect_record(previous)
            row = EngineEffect(
                id=uuid4().hex,
                key=key,
                kind=kind,
                command_id=command.id,
                run_id=command.run_id,
                step_id=None,
                lease_epoch=lease.epoch,
                payload_json=payload_json,
                payload_digest=fingerprint,
            )
            session.add(row)
            await session.flush()
            return effect_record(row)

    async def mark_dispatched(self, lease: CommandLease, effect: EffectRecord) -> EffectRecord:
        async with transaction(self.factory) as session:
            command = await command_fence(session, lease, datetime.now(timezone.utc))
            row = await session.scalar(
                select(EngineEffect).where(EngineEffect.id == effect.id).with_for_update()
            )
            if (
                row is None
                or row.command_id != command.id
                or row.step_id is not None
                or (row.state != "requested" or row.version != effect.version)
            ):
                raise StoreConflict("command effect ownership or dispatch state changed")
            await allowed(session, command, row.kind, row.payload_json)
            row.state, row.lease_epoch, row.version = "dispatched", lease.epoch, row.version + 1
            row.dispatched_at, row.dispatch_deadline_at = (
                datetime.now(timezone.utc),
                lease.expires_at,
            )
            await session.flush()
            return effect_record(row)
