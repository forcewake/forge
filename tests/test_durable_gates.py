"""Tests for human gate approvals (ADR-0009)."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from forge.durable import (
    FlowRun,
    GateAlreadyConsumed,
    GateNotFound,
    RunNotFound,
    consume_approval,
    is_valid,
    record_approval,
)
from forge.models.base import Base

NOW = datetime.now(timezone.utc)


def _gate_kwargs(**overrides):
    kwargs = {
        "plan_digest": "plan-digest-1",
        "base_sha": "a" * 40,
        "policy_digest": "policy-digest-1",
        "approver_user_id": 42,
        "source_event_id": "note-event-1",
        "expires_at": NOW + timedelta(hours=1),
    }
    kwargs.update(overrides)
    return kwargs


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


@pytest.fixture()
async def flow_run_id(db_session: AsyncSession) -> str:
    run = FlowRun(id=uuid4().hex, project_id=1)
    db_session.add(run)
    await db_session.flush()
    return run.id


class TestRecordApproval:
    async def test_persists_all_gate_bindings(self, db_session: AsyncSession, flow_run_id: str):
        gate = await record_approval(db_session, flow_run_id=flow_run_id, **_gate_kwargs())

        assert gate.id is not None
        assert gate.flow_run_id == flow_run_id
        assert gate.plan_digest == "plan-digest-1"
        assert gate.base_sha == "a" * 40
        assert gate.policy_digest == "policy-digest-1"
        assert gate.approver_user_id == 42
        assert gate.source_event_id == "note-event-1"
        assert gate.consumed_at is None
        assert gate.created_at is not None

    async def test_unknown_run_raises(self, db_session: AsyncSession):
        with pytest.raises(RunNotFound):
            await record_approval(db_session, flow_run_id="no-such-run", **_gate_kwargs())


class TestGateValidity:
    async def test_fresh_gate_is_valid(self, db_session: AsyncSession, flow_run_id: str):
        gate = await record_approval(db_session, flow_run_id=flow_run_id, **_gate_kwargs())

        assert is_valid(gate, NOW) is True
        assert is_valid(
            gate,
            NOW,
            plan_digest="plan-digest-1",
            base_sha="a" * 40,
            policy_digest="policy-digest-1",
        )

    async def test_expired_gate_is_invalid(self, db_session: AsyncSession, flow_run_id: str):
        gate = await record_approval(
            db_session,
            flow_run_id=flow_run_id,
            **_gate_kwargs(expires_at=NOW - timedelta(seconds=1)),
        )

        assert is_valid(gate, NOW) is False

    async def test_consumed_gate_is_invalid(self, db_session: AsyncSession, flow_run_id: str):
        gate = await record_approval(db_session, flow_run_id=flow_run_id, **_gate_kwargs())
        await consume_approval(db_session, gate.id, NOW)

        assert is_valid(gate, NOW + timedelta(seconds=1)) is False

    @pytest.mark.parametrize(
        "expectation",
        [
            {"plan_digest": "plan-digest-2"},
            {"base_sha": "b" * 40},
            {"policy_digest": "policy-digest-2"},
        ],
        ids=["plan_changed", "base_moved", "policy_changed"],
    )
    async def test_changed_expectations_invalidate_gate(
        self, db_session: AsyncSession, flow_run_id: str, expectation: dict
    ):
        gate = await record_approval(db_session, flow_run_id=flow_run_id, **_gate_kwargs())

        assert is_valid(gate, NOW, **expectation) is False


class TestConsumeApproval:
    async def test_consume_sets_consumed_at_exactly_once(
        self, db_session: AsyncSession, flow_run_id: str
    ):
        gate = await record_approval(db_session, flow_run_id=flow_run_id, **_gate_kwargs())
        consumed_at = NOW + timedelta(minutes=1)

        consumed = await consume_approval(db_session, gate.id, consumed_at)

        assert consumed.consumed_at == consumed_at

    async def test_second_consume_raises(self, db_session: AsyncSession, flow_run_id: str):
        gate = await record_approval(db_session, flow_run_id=flow_run_id, **_gate_kwargs())
        first = NOW + timedelta(minutes=1)
        await consume_approval(db_session, gate.id, first)

        with pytest.raises(GateAlreadyConsumed):
            await consume_approval(db_session, gate.id, first + timedelta(minutes=1))

        assert gate.consumed_at == first

    async def test_consume_missing_gate_raises(self, db_session: AsyncSession):
        with pytest.raises(GateNotFound):
            await consume_approval(db_session, 9999, NOW)
