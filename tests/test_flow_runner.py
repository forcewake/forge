"""Durable execution contracts live in the real step runtime after YAML flows retire."""

from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool
from forge.models.base import Base
from forge.durable import ingest_event
from forge.durable.models import StepRun, EventInbox
from forge.worker.steps import (
    schedule_command_step,
    claim_command_step,
    complete_step,
    fail_step,
    run_pending_command_step,
    command_source_event_id,
)


@pytest.fixture()
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def accepted(db, note=7, max_attempts=3):
    command = {"command": "status", "project_id": 42, "issue_iid": 1, "note_text": "/status"}
    source = command_source_event_id("status", 42, note)
    async with db() as session:
        async with session.begin():
            _, created = await ingest_event(
                session,
                source_event_id=source,
                project_id=42,
                event_type="run_command",
                payload=command,
            )
            if created:
                await schedule_command_step(
                    session, command, source_event_id=source, max_attempts=max_attempts
                )
    return source


async def test_a_committed_command_needs_no_redis_wakeup_to_be_claimed(db):
    source = await accepted(db)
    claimed = await claim_command_step(db, "fresh-worker", source)
    assert claimed is not None and claimed.payload["command"] == "status"


async def test_redelivery_keeps_one_inbox_and_one_scheduled_step(db):
    await accepted(db)
    await accepted(db)
    async with db() as session:
        for model in (EventInbox, StepRun):
            assert await session.scalar(select(func.count()).select_from(model)) == 1


async def test_a_second_owner_cannot_claim_the_running_command(db):
    source = await accepted(db)
    assert await claim_command_step(db, "first", source) is not None
    assert await claim_command_step(db, "second", source) is None


async def test_a_wrong_fence_cannot_complete_the_running_command(db):
    claimed = await claim_command_step(db, "worker", await accepted(db))
    assert not await complete_step(db, replace(claimed, fence_token=claimed.fence_token + 1))
    assert await complete_step(db, claimed, output={"observed": "done"})


async def test_completed_and_missing_commands_are_inert(db):
    source = await accepted(db)
    claimed = await claim_command_step(db, "worker", source)
    assert await complete_step(db, claimed)
    assert await claim_command_step(db, "later", source) is None
    assert await claim_command_step(db, "later", "unknown") is None


async def test_exhausted_failure_preserves_the_error_without_claiming_success(db):
    claimed = await claim_command_step(db, "worker", await accepted(db, max_attempts=1))
    assert await fail_step(db, claimed, "explicit failure") == "dead"
    async with db() as session:
        row = await session.get(StepRun, claimed.id)
        assert row.status == "dead" and row.output["error"] == "explicit failure"


async def test_retry_is_bounded_and_due_in_the_future(db):
    claimed = await claim_command_step(db, "worker", await accepted(db, max_attempts=3))
    assert await fail_step(db, claimed, "temporary outage", retry_after=10) == "retry"
    assert await claim_command_step(db, "early", claimed.source_event_id) is None
    async with db() as session:
        row = await session.get(StepRun, claimed.id)
        assert row.attempt == 1 and row.output["error"] == "temporary outage"
        assert row.due_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)


async def test_two_wakeups_execute_the_committed_handler_once(db, monkeypatch):
    source = await accepted(db)
    handler = AsyncMock()
    monkeypatch.setattr("forge.worker.steps.execute_step_payload", handler)
    await run_pending_command_step(db, None, None, source, owner="first")
    await run_pending_command_step(db, None, None, source, owner="second")
    handler.assert_awaited_once()
