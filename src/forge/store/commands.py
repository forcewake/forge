"""Durable native command receipts and a separately fenced operator lease."""

from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from forge.command_values import (
    CommandEnvelope,
    CommandLease,
    CommandRecord,
    CommandResult,
    render,
)
from forge.store.models_commands import EngineCommand
from forge.store.records import StaleLease, StoreConflict
from forge.store.transactions import SessionFactory, aware, stored_time, transaction


def record(row: EngineCommand) -> CommandRecord:
    return CommandRecord(
        row.id,
        CommandEnvelope.model_validate_json(row.document_json),
        row.run_id,
        row.state,
        CommandResult.model_validate_json(row.result_json) if row.result_json is not None else None,
    )


async def command_fence(session: AsyncSession, lease: CommandLease, now: datetime) -> EngineCommand:
    row = await session.scalar(
        select(EngineCommand).where(EngineCommand.id == lease.command_id).with_for_update()
    )
    if (
        row is None
        or row.state not in ("running", "resolved")
        or row.lease_epoch != lease.epoch
        or row.lease_owner != lease.owner
        or row.lease_expires_at is None
        or stored_time(row.lease_expires_at) <= aware(now)
        or lease.expires_at <= aware(now)
    ):
        raise StaleLease("command lease is no longer current")
    return row


def command_retry_budget() -> tuple[int, int, int]:
    """Attempts, base seconds and cap. Defaults cover more than a 15s GitHub blip."""
    import os

    def bounded(name: str, default: int, low: int, high: int) -> int:
        raw = os.environ.get(name, str(default))
        try:
            value = int(raw)
        except ValueError:
            return default
        return min(high, max(low, value))

    return (
        bounded("FORGE_COMMAND_RETRY_ATTEMPTS", 8, 1, 30),
        bounded("FORGE_COMMAND_RETRY_BASE_SECONDS", 5, 1, 300),
        bounded("FORGE_COMMAND_RETRY_CAP_SECONDS", 120, 1, 900),
    )


class CommandStore:
    def __init__(self, factory: SessionFactory) -> None:
        self.factory = factory
        self.max_attempts, self.retry_base, self.retry_cap = command_retry_budget()

    async def accept(self, envelope: CommandEnvelope, *, now: datetime) -> CommandRecord:
        try:
            async with transaction(self.factory) as session:
                row = await session.scalar(
                    select(EngineCommand)
                    .where(EngineCommand.source_key == envelope.source_key)
                    .with_for_update()
                )
                if row is not None:
                    if row.document_json != envelope.canonical_bytes.decode():
                        raise StoreConflict(
                            "native command receipt was replayed with changed content"
                        )
                    return record(row)
                row = EngineCommand(
                    id=uuid4().hex,
                    source_key=envelope.source_key,
                    kind=envelope.command.kind,
                    document_json=envelope.canonical_bytes.decode(),
                    due_at=aware(now),
                    max_attempts=self.max_attempts,
                )
                session.add(row)
                await session.flush()
                return record(row)
        except IntegrityError:
            return await self.accept(envelope, now=now)

    async def load(self, command_id: str) -> CommandRecord:
        async with self.factory() as session:
            row = await session.get(EngineCommand, command_id)
            if row is None:
                raise StoreConflict("command does not exist")
            return record(row)

    async def claim(
        self, owner: str, *, now: datetime, seconds: int = 60, command_id: str | None = None
    ) -> CommandLease | None:
        if not owner or not 0 < seconds <= 3600:
            raise ValueError("command lease owner and expiry must be bounded")
        async with transaction(self.factory) as session:
            query = select(EngineCommand).where(
                EngineCommand.due_at <= aware(now),
                EngineCommand.attempts < EngineCommand.max_attempts,
                or_(
                    EngineCommand.state == "accepted",
                    and_(
                        EngineCommand.state.in_(("running", "resolved")),
                        EngineCommand.lease_expires_at <= aware(now),
                    ),
                ),
            )
            if command_id is not None:
                query = query.where(EngineCommand.id == command_id)
            row = await session.scalar(
                query.order_by(EngineCommand.created_at, EngineCommand.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            row.lease_epoch += 1
            row.attempts += 1
            row.lease_owner, row.lease_expires_at = owner, aware(now) + timedelta(seconds=seconds)
            if row.state == "accepted":
                row.state = "running"
            await session.flush()
            return CommandLease(row.id, row.lease_epoch, owner, row.lease_expires_at)

    async def resolve(
        self, lease: CommandLease, result: CommandResult, *, now: datetime
    ) -> CommandRecord:
        async with transaction(self.factory) as session:
            row = await command_fence(session, lease, now)
            resolve_in(row, result)
            await session.flush()
            return record(row)

    async def release_for_retry(self, lease: CommandLease, *, now: datetime) -> bool:
        """Defer a transient failure. False means the attempt budget is spent."""
        async with transaction(self.factory) as session:
            row = await command_fence(session, lease, now)
            if row.attempts >= row.max_attempts:
                return False
            delay = min(self.retry_cap, self.retry_base * (2 ** (row.attempts - 1)))
            row.due_at = aware(now) + timedelta(seconds=delay)
            row.lease_owner = None
            row.lease_expires_at = aware(now)
            return True

    async def complete(self, lease: CommandLease, *, now: datetime) -> None:
        async with transaction(self.factory) as session:
            row = await command_fence(session, lease, now)
            if row.result_json is None:
                raise StoreConflict("unresolved command cannot complete")
            row.state, row.lease_owner, row.lease_expires_at = "completed", None, None


def resolve_in(row: EngineCommand, result: CommandResult) -> None:
    if row.result_json not in (None, result.canonical_bytes.decode()):
        raise StoreConflict("resolved command decision is immutable")
    row.run_id, row.state, row.result_json = (
        result.run_id,
        "resolved",
        result.canonical_bytes.decode(),
    )
    envelope = CommandEnvelope.model_validate_json(row.document_json)
    if envelope.native_reply:
        import json

        row.reply_json = json.dumps(
            {
                "subject": envelope.target.model_dump(mode="json"),
                "body": render(result),
                "marker": f"forge:command:{row.id}",
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
