"""Short DB transactions and canonical input checks, with no external I/O."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.domain.states import TERMINAL, State
from forge.store.models_execution import EngineStep
from forge.store.models_runs import EngineRun
from forge.store.records import Lease, StaleLease, StoreConflict

SessionFactory = async_sessionmaker[AsyncSession]


@asynccontextmanager
async def transaction(factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        if session.get_bind().dialect.name == "sqlite":
            await session.execute(text("BEGIN IMMEDIATE"))
        else:
            await session.begin()
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def stored_time(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else aware(value)


def canonical_json(value: str) -> str:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = item
        return result

    parsed = json.loads(value, object_pairs_hook=pairs, parse_constant=lambda _: _bad_constant())
    canonical = json.dumps(
        parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    if canonical != value:
        raise ValueError("JSON must be canonical")
    return canonical


def _bad_constant() -> None:
    raise ValueError("nonfinite JSON number")


def payload_digest(value: str) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


async def locked_run(session: AsyncSession, run_id: str) -> EngineRun:
    row = await session.scalar(select(EngineRun).where(EngineRun.id == run_id).with_for_update())
    if row is None:
        raise StoreConflict("run does not exist")
    return row


async def fence(session: AsyncSession, lease: Lease, now: datetime) -> EngineStep:
    now = aware(now)
    run = await locked_run(session, lease.run_id)
    row = await session.scalar(
        select(EngineStep).where(EngineStep.id == lease.step_id).with_for_update()
    )
    if (
        row is None
        or row.run_id != lease.run_id
        or row.handler != lease.handler.value
        or row.state != "running"
        or row.lease_epoch != lease.epoch
        or row.lease_owner != lease.owner
        or row.lease_expires_at is None
        or stored_time(row.lease_expires_at) <= now
        or lease.expires_at <= now
        or run.cancel_requested
        or State(run.state) in TERMINAL
        or (run.attempt, run.generation, run.cancellation_generation)
        != (lease.attempt, lease.generation, lease.cancellation_generation)
        or (row.attempt, row.generation, row.cancellation_generation)
        != (lease.attempt, lease.generation, lease.cancellation_generation)
    ):
        raise StaleLease("step epoch or run identity is no longer current")
    return row


async def notify_steps(session: AsyncSession) -> None:
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(text("SELECT pg_notify('forge_engine_steps', '')"))
