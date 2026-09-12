"""Tests for the durable webhook inbox (ADR-0005 idempotent ingestion)."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from forge.durable import (
    EventInbox,
    build_source_event_id,
    ingest_event,
    mark_processed,
    mark_rejected,
)
from forge.models.base import Base


@pytest.fixture()
async def db_session():
    """Create an in-memory SQLite database and yield a session."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session

    await engine.dispose()


class TestIngestEvent:
    async def test_ingest_creates_pending_row(self, db_session: AsyncSession):
        row, created = await ingest_event(
            db_session,
            source_event_id="a" * 64,
            project_id=7,
            event_type="note",
            payload={"object_kind": "note"},
        )

        assert created is True
        assert row.id is not None
        assert row.status == "pending"
        assert row.processed_at is None
        assert row.handler_result is None
        assert row.payload == {"object_kind": "note"}
        assert row.received_at is not None

    async def test_duplicate_delivery_returns_existing_row(self, db_session: AsyncSession):
        source_event_id = "b" * 64
        first, created_first = await ingest_event(
            db_session,
            source_event_id=source_event_id,
            project_id=7,
            event_type="note",
            payload={"body": "original"},
        )

        second, created_second = await ingest_event(
            db_session,
            source_event_id=source_event_id,
            project_id=7,
            event_type="note",
            payload={"body": "redelivered-different-payload"},
        )

        assert created_first is True
        assert created_second is False
        assert second.id == first.id
        assert second.payload == {"body": "original"}

    async def test_duplicate_delivery_stores_only_one_row(self, db_session: AsyncSession):
        source_event_id = "c" * 64
        for _ in range(3):
            await ingest_event(
                db_session,
                source_event_id=source_event_id,
                project_id=7,
                event_type="issue",
                payload={},
            )

        count = (
            await db_session.execute(select(func.count()).select_from(EventInbox))
        ).scalar_one()
        assert count == 1

    async def test_unique_index_enforced_at_db_level(self, db_session: AsyncSession):
        db_session.add(EventInbox(source_event_id="d" * 64, project_id=7, event_type="note"))
        await db_session.flush()
        db_session.add(EventInbox(source_event_id="d" * 64, project_id=7, event_type="note"))

        with pytest.raises(IntegrityError):
            await db_session.flush()


class TestMarking:
    async def test_mark_processed(self, db_session: AsyncSession):
        row, _ = await ingest_event(
            db_session, source_event_id="e" * 64, project_id=7, event_type="note", payload={}
        )

        marked = await mark_processed(db_session, row, handler_result={"accepted": True})

        assert marked.status == "processed"
        assert marked.processed_at is not None
        assert marked.handler_result == {"accepted": True}

    async def test_mark_rejected(self, db_session: AsyncSession):
        row, _ = await ingest_event(
            db_session, source_event_id="f" * 64, project_id=7, event_type="note", payload={}
        )

        marked = await mark_rejected(db_session, row, handler_result={"reason": "unsupported"})

        assert marked.status == "rejected"
        assert marked.processed_at is not None
        assert marked.handler_result == {"reason": "unsupported"}


class TestBuildSourceEventId:
    def test_is_deterministic(self):
        kwargs = {
            "project_id": 7,
            "object_kind": "merge_request",
            "object_iid": 12,
            "action": "open",
            "delivery_id": "uuid-1",
        }
        assert build_source_event_id(**kwargs) == build_source_event_id(**kwargs)

    def test_is_sha256_hex(self):
        event_id = build_source_event_id(7, "merge_request", 12, "open", "uuid-1")
        assert len(event_id) == 64
        int(event_id, 16)  # must not raise

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"project_id": 8},
            {"object_kind": "note"},
            {"object_iid": 13},
            {"action": "merge"},
            {"delivery_id": "uuid-2"},
            {"object_iid": None},
        ],
        ids=[
            "project",
            "kind",
            "iid",
            "action",
            "delivery",
            "iid_absent",
        ],
    )
    def test_distinct_inputs_produce_distinct_ids(self, kwargs: dict):
        baseline = {
            "project_id": 7,
            "object_kind": "merge_request",
            "object_iid": 12,
            "action": "open",
            "delivery_id": "uuid-1",
        }
        assert build_source_event_id(**{**baseline, **kwargs}) != build_source_event_id(**baseline)
