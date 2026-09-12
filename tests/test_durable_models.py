"""Schema-level tests for the durable tables (CHECKs, defaults, usage ledger)."""

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from forge.durable import ActionLog, EventInbox, FlowRun, LLMCall, StepRun
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


class TestCheckConstraints:
    @pytest.mark.parametrize(
        ("model", "fields"),
        [
            (FlowRun, {"id": uuid4().hex, "project_id": 1, "status": "teleported"}),
            (StepRun, {"flow_run_id": uuid4().hex, "step_name": "commit", "status": "paused"}),
            (EventInbox, {"source_event_id": "0" * 64, "project_id": 1, "status": "lost"}),
            (ActionLog, {"action_kind": "commit", "status": "pending"}),
            (LLMCall, {"role": "planner", "provider": "p", "model": "m", "status": "retrying"}),
        ],
        ids=["flow_runs", "step_runs", "event_inbox", "action_log", "llm_calls"],
    )
    async def test_unknown_status_rejected(self, db_session: AsyncSession, model, fields):
        db_session.add(model(**fields))
        with pytest.raises(IntegrityError):
            await db_session.flush()


class TestDefaults:
    async def test_flow_run_id_defaults_to_uuid_hex(self, db_session: AsyncSession):
        run = FlowRun(project_id=1)
        db_session.add(run)
        await db_session.flush()

        assert len(run.id) == 32
        int(run.id, 16)  # must not raise
        assert run.status == "accepted"
        assert run.candidate_shas == []
        assert run.created_at is not None
        assert run.updated_at is not None

    async def test_action_log_defaults_to_requested(self, db_session: AsyncSession):
        action = ActionLog(action_kind="commit")
        db_session.add(action)
        await db_session.flush()

        assert action.status == "requested"
        assert action.flow_run_id is None

    async def test_step_run_attempt_defaults_to_zero(self, db_session: AsyncSession):
        run = FlowRun(id=uuid4().hex, project_id=1)
        db_session.add(run)
        step = StepRun(flow_run_id=run.id, step_name="validate", status="running")
        db_session.add(step)
        await db_session.flush()

        assert step.attempt == 0
        assert step.lease_owner is None
        assert step.lease_expires_at is None
        assert step.started_at is not None


class TestUsageLedger:
    async def test_failed_call_with_unknown_usage_is_recorded(self, db_session: AsyncSession):
        """ADR-0013: failures are recorded too, and unknown usage is not zero."""
        call = LLMCall(
            flow_run_id=uuid4().hex,
            role="implementer",
            provider="openai",
            model="gpt-x",
            status="failed",
            error="connection reset before usage was reported",
        )
        db_session.add(call)
        await db_session.flush()

        row = (await db_session.execute(select(LLMCall).where(LLMCall.id == call.id))).scalar_one()
        assert row.status == "failed"
        assert row.error is not None
        assert row.input_tokens is None
        assert row.output_tokens is None
        assert row.cached_tokens is None
        assert row.duration_ms is None

    async def test_successful_call_records_counters(self, db_session: AsyncSession):
        call = LLMCall(
            role="reviewer",
            provider="vllm",
            model="qwen-x",
            status="ok",
            input_tokens=500,
            output_tokens=200,
            cached_tokens=128,
            duration_ms=900,
        )
        db_session.add(call)
        await db_session.flush()

        assert call.id is not None
        assert call.status == "ok"
        assert call.cached_tokens == 128
