"""Tests for the durable flow controller (ADR-0004 lifecycle, ADR-0005 journal/leases)."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from forge.durable import (
    ALLOWED_TRANSITIONS,
    FLOW_STATUSES,
    TERMINAL_STATUSES,
    ActionLog,
    ActionLogNotFound,
    Controller,
    FlowRun,
    FlowStatus,
    InvalidActionTransition,
    InvalidTransition,
    Outbox,
    RunNotFound,
    StepRun,
    StepRunNotFound,
    as_aware_utc,
)
from forge.models.base import Base

HAPPY_PATH = [
    FlowStatus.PREFLIGHT,
    FlowStatus.PLANNING,
    FlowStatus.WAITING_APPROVAL,
    FlowStatus.PROPOSING,
    FlowStatus.VALIDATING,
    FlowStatus.COMMITTING,
    FlowStatus.ENSURING_DRAFT_MR,
    FlowStatus.WAITING_CI,
    FlowStatus.EVALUATING_CI,
    FlowStatus.REVIEWING,
    FlowStatus.READY_FOR_HUMAN,
]

NON_TERMINAL_STATUSES = [status for status in FlowStatus if status not in TERMINAL_STATUSES]


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


def _make_run(status: str = "accepted") -> FlowRun:
    return FlowRun(id=uuid4().hex, project_id=1, status=status)


async def _add_run(db_session: AsyncSession, status: str = "accepted") -> FlowRun:
    run = _make_run(status)
    db_session.add(run)
    await db_session.flush()
    return run


async def _add_step(db_session: AsyncSession, flow_run_id: str, **kwargs) -> StepRun:
    step = StepRun(flow_run_id=flow_run_id, step_name="commit", status="running", **kwargs)
    db_session.add(step)
    await db_session.flush()
    return step


class TestTransitionGraph:
    def test_flow_status_matches_model_whitelist(self):
        assert tuple(status.value for status in FlowStatus) == FLOW_STATUSES

    def test_terminal_states_have_no_outgoing_transitions(self):
        # Revival amendment: failed/blocked keep ONE authorized edge back into
        # the graph (the bounded auto-revive or an operator /retry — same run,
        # same branch, never a re-plan); cancelled and ready stay final.
        for status in TERMINAL_STATUSES:
            allowed = ALLOWED_TRANSITIONS[status]
            if status in (FlowStatus.BLOCKED, FlowStatus.FAILED):
                assert allowed == {FlowStatus.PROPOSING}
            else:
                assert allowed == set()

    async def test_happy_path_transition_sequence(self, db_session: AsyncSession):
        run = await _add_run(db_session)
        controller = Controller(db_session)

        for target in HAPPY_PATH:
            updated = await controller.transition(run.id, target)
            assert updated.status == target.value

        assert run.status == FlowStatus.READY_FOR_HUMAN.value

    async def test_repair_loop_from_evaluating_ci(self, db_session: AsyncSession):
        run = await _add_run(db_session)
        controller = Controller(db_session)
        for target in HAPPY_PATH[: HAPPY_PATH.index(FlowStatus.EVALUATING_CI) + 1]:
            await controller.transition(run.id, target)

        # code_failure -> repair loop: proposing -> validating -> committing -> waiting_ci
        repair_path = [
            FlowStatus.PROPOSING,
            FlowStatus.VALIDATING,
            FlowStatus.COMMITTING,
            FlowStatus.ENSURING_DRAFT_MR,
            FlowStatus.WAITING_CI,
            FlowStatus.EVALUATING_CI,
        ]
        for target in repair_path:
            await controller.transition(run.id, target)
        assert run.status == FlowStatus.EVALUATING_CI.value

    @pytest.mark.parametrize(
        ("start", "target"),
        [
            ("accepted", "committing"),
            ("waiting_approval", "reviewing"),
            ("evaluating_ci", "ensuring_draft_mr"),
            ("ready_for_human", "reviewing"),
            ("blocked", "preflight"),
            ("failed", "cancelled"),
            ("cancelled", "preflight"),
            ("accepted", "accepted"),
        ],
    )
    async def test_invalid_transition_rejected(
        self, db_session: AsyncSession, start: str, target: str
    ):
        run = await _add_run(db_session, status=start)
        controller = Controller(db_session)

        with pytest.raises(InvalidTransition):
            await controller.transition(run.id, target)
        assert run.status == start

    async def test_unknown_status_string_rejected(self, db_session: AsyncSession):
        run = await _add_run(db_session)
        controller = Controller(db_session)

        with pytest.raises(InvalidTransition):
            await controller.transition(run.id, "teleported")

    @pytest.mark.parametrize(
        "terminal",
        [FlowStatus.BLOCKED, FlowStatus.FAILED, FlowStatus.CANCELLED],
        ids=lambda status: status.value,
    )
    @pytest.mark.parametrize("start", NON_TERMINAL_STATUSES, ids=lambda status: status.value)
    async def test_blocked_failed_cancelled_allowed_from_every_state(
        self, db_session: AsyncSession, start: FlowStatus, terminal: FlowStatus
    ):
        run = await _add_run(db_session, status=start.value)
        controller = Controller(db_session)

        updated = await controller.transition(run.id, terminal)
        assert updated.status == terminal.value


class TestWaitingHarnessTransitions:
    """ADR-0015: the durable harness wait (worker-free, like waiting_ci)."""

    async def test_harness_leg_transition_sequence(self, db_session: AsyncSession):
        run = await _add_run(db_session)
        controller = Controller(db_session)

        for target in [
            FlowStatus.PREFLIGHT,
            FlowStatus.PLANNING,
            FlowStatus.WAITING_APPROVAL,
            FlowStatus.PROPOSING,
            FlowStatus.WAITING_HARNESS,  # harness job started, run parked
            FlowStatus.COMMITTING,  # verified harness head adopted
            FlowStatus.ENSURING_DRAFT_MR,
            FlowStatus.WAITING_CI,
            FlowStatus.EVALUATING_CI,
            FlowStatus.REVIEWING,
            FlowStatus.READY_FOR_HUMAN,
        ]:
            await controller.transition(run.id, target)

        assert run.status == FlowStatus.READY_FOR_HUMAN.value

    @pytest.mark.parametrize("start", ["proposing", "committing"])
    async def test_entry_into_waiting_harness(self, db_session: AsyncSession, start: str):
        run = await _add_run(db_session, status=start)
        controller = Controller(db_session)

        await controller.transition(run.id, FlowStatus.WAITING_HARNESS)
        assert run.status == FlowStatus.WAITING_HARNESS.value

    @pytest.mark.parametrize(
        "target",
        [
            FlowStatus.COMMITTING,  # verified ok
            FlowStatus.BLOCKED,
            FlowStatus.FAILED,
            FlowStatus.CANCELLED,
        ],
    )
    async def test_legal_exits_from_waiting_harness(
        self, db_session: AsyncSession, target: FlowStatus
    ):
        run = await _add_run(db_session, status="waiting_harness")
        controller = Controller(db_session)

        await controller.transition(run.id, target)
        assert run.status == target.value

    @pytest.mark.parametrize(
        "target",
        [
            FlowStatus.PROPOSING,
            FlowStatus.VALIDATING,
            FlowStatus.ENSURING_DRAFT_MR,
            FlowStatus.WAITING_CI,
            FlowStatus.REVIEWING,
            FlowStatus.READY_FOR_HUMAN,
            FlowStatus.WAITING_HARNESS,  # no self-transition
        ],
    )
    async def test_illegal_exits_from_waiting_harness(
        self, db_session: AsyncSession, target: FlowStatus
    ):
        run = await _add_run(db_session, status="waiting_harness")
        controller = Controller(db_session)

        with pytest.raises(InvalidTransition):
            await controller.transition(run.id, target)
        assert run.status == "waiting_harness"


class TestTransitionPersistence:
    async def test_transition_records_reason_and_updated_at(self, db_session: AsyncSession):
        run = await _add_run(db_session)
        controller = Controller(db_session)

        updated = await controller.transition(run.id, FlowStatus.BLOCKED, reason="runner_offline")

        assert updated.status_reason == "runner_offline"
        assert updated.updated_at is not None
        assert updated.updated_at >= updated.created_at

    async def test_transition_writes_outbox_row_per_transition(self, db_session: AsyncSession):
        run = await _add_run(db_session)
        controller = Controller(db_session)
        await controller.transition(run.id, FlowStatus.PREFLIGHT, reason="kickoff")
        await controller.transition(run.id, FlowStatus.PLANNING)

        rows = (await db_session.execute(select(Outbox).order_by(Outbox.id))).scalars().all()
        assert len(rows) == 2
        assert rows[0].flow_run_id == run.id
        assert rows[0].event_type == "flow.transition"
        assert rows[0].payload == {
            "flow_run_id": run.id,
            "from": "accepted",
            "to": "preflight",
            "reason": "kickoff",
        }
        assert rows[0].processed_at is None
        assert rows[1].payload["from"] == "preflight"
        assert rows[1].payload["to"] == "planning"

    async def test_status_and_outbox_commit_together(self, db_session: AsyncSession):
        run = await _add_run(db_session)
        await db_session.commit()
        run_id = run.id
        controller = Controller(db_session)
        await controller.transition(run_id, FlowStatus.PREFLIGHT)

        # Nothing was committed yet; rolling back must discard both the status
        # change and the outbox row (single-transaction guarantee, ADR-0005).
        await db_session.rollback()

        run_count = (
            await db_session.execute(select(func.count()).select_from(Outbox))
        ).scalar_one()
        assert run_count == 0
        reloaded = (
            await db_session.execute(select(FlowRun).where(FlowRun.id == run_id))
        ).scalar_one()
        assert reloaded.status == "accepted"

    async def test_transition_unknown_run_raises(self, db_session: AsyncSession):
        controller = Controller(db_session)
        with pytest.raises(RunNotFound):
            await controller.transition("no-such-run", FlowStatus.PREFLIGHT)


class TestActionJournal:
    async def test_record_action_creates_requested_row(self, db_session: AsyncSession):
        run = await _add_run(db_session)
        controller = Controller(db_session)

        action_id = await controller.record_action(
            run.id, "commit", params_digest="d" * 64, correlation_id="corr-1"
        )

        row = await db_session.get(ActionLog, action_id)
        assert row.status == "requested"
        assert row.action_kind == "commit"
        assert row.params_digest == "d" * 64
        assert row.correlation_id == "corr-1"
        assert row.remote_result is None

    @pytest.mark.parametrize("outcome", ["succeeded", "failed", "unknown_outcome"])
    async def test_requested_action_completes_once(self, db_session: AsyncSession, outcome: str):
        run = await _add_run(db_session)
        controller = Controller(db_session)
        action_id = await controller.record_action(run.id, "merge_request")

        completed = await controller.complete_action(action_id, outcome, remote_result={"iid": 7})

        assert completed.status == outcome
        assert completed.remote_result == {"iid": 7}

    @pytest.mark.parametrize("first", ["succeeded", "failed", "unknown_outcome"])
    @pytest.mark.parametrize("second", ["succeeded", "failed", "unknown_outcome"])
    async def test_terminal_action_cannot_change_again(
        self, db_session: AsyncSession, first: str, second: str
    ):
        run = await _add_run(db_session)
        controller = Controller(db_session)
        action_id = await controller.record_action(run.id, "commit")
        await controller.complete_action(action_id, first)

        with pytest.raises(InvalidActionTransition):
            await controller.complete_action(action_id, second)

    async def test_requested_to_requested_invalid(self, db_session: AsyncSession):
        run = await _add_run(db_session)
        controller = Controller(db_session)
        action_id = await controller.record_action(run.id, "commit")

        with pytest.raises(InvalidActionTransition):
            await controller.complete_action(action_id, "requested")

    async def test_record_action_unknown_run_raises(self, db_session: AsyncSession):
        controller = Controller(db_session)
        with pytest.raises(RunNotFound):
            await controller.record_action("no-such-run", "commit")

    async def test_complete_missing_action_raises(self, db_session: AsyncSession):
        controller = Controller(db_session)
        with pytest.raises(ActionLogNotFound):
            await controller.complete_action(9999, "succeeded")


class TestLeases:
    async def test_acquire_sets_owner_and_expiry(self, db_session: AsyncSession):
        run = await _add_run(db_session)
        step = await _add_step(db_session, run.id)
        controller = Controller(db_session)

        assert await controller.acquire_lease(step.id, "worker-a", 30) is True
        assert step.lease_owner == "worker-a"
        assert step.lease_expires_at is not None
        remaining = as_aware_utc(step.lease_expires_at) - datetime.now(timezone.utc)
        assert timedelta(0) < remaining <= timedelta(seconds=30)

    async def test_acquire_rejected_while_live_lease_held(self, db_session: AsyncSession):
        run = await _add_run(db_session)
        step = await _add_step(db_session, run.id)
        controller = Controller(db_session)
        assert await controller.acquire_lease(step.id, "worker-a", 30) is True

        assert await controller.acquire_lease(step.id, "worker-b", 30) is False
        assert step.lease_owner == "worker-a"

    async def test_acquire_takeover_after_expiry(self, db_session: AsyncSession):
        run = await _add_run(db_session)
        step = await _add_step(db_session, run.id)
        controller = Controller(db_session)
        assert await controller.acquire_lease(step.id, "worker-a", 30) is True

        step.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert await controller.acquire_lease(step.id, "worker-b", 30) is True
        assert step.lease_owner == "worker-b"

    async def test_renew_extends_expiry_for_same_owner(self, db_session: AsyncSession):
        run = await _add_run(db_session)
        step = await _add_step(db_session, run.id)
        controller = Controller(db_session)
        assert await controller.acquire_lease(step.id, "worker-a", 30) is True
        first_expiry = step.lease_expires_at

        assert await controller.renew_lease(step.id, "worker-a", 30) is True
        assert step.lease_owner == "worker-a"
        assert step.lease_expires_at > first_expiry

    async def test_renew_fencing_rejected_when_owner_differs_before_expiry(
        self, db_session: AsyncSession
    ):
        run = await _add_run(db_session)
        step = await _add_step(db_session, run.id)
        controller = Controller(db_session)
        assert await controller.acquire_lease(step.id, "worker-a", 30) is True
        expiry_before = step.lease_expires_at

        assert await controller.renew_lease(step.id, "worker-b", 30) is False
        assert step.lease_owner == "worker-a"
        assert step.lease_expires_at == expiry_before

    async def test_renew_takeover_after_lease_expiry(self, db_session: AsyncSession):
        run = await _add_run(db_session)
        step = await _add_step(db_session, run.id)
        controller = Controller(db_session)
        assert await controller.acquire_lease(step.id, "worker-a", 30) is True

        step.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert await controller.renew_lease(step.id, "worker-b", 30) is True
        assert step.lease_owner == "worker-b"

    async def test_missing_step_run_raises(self, db_session: AsyncSession):
        controller = Controller(db_session)
        with pytest.raises(StepRunNotFound):
            await controller.acquire_lease(9999, "worker-a", 30)
        with pytest.raises(StepRunNotFound):
            await controller.renew_lease(9999, "worker-a", 30)
