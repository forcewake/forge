"""ADR-0017 durable step runtime: transactional ingress, atomic claiming,
fencing, lease recovery and the poison pill.

SQLite is the fast unit profile here (research doc §5): the conditional
UPDATE claim is the portable ownership mechanism; SKIP LOCKED only decorates
the candidate SELECT and is ignored by SQLite (single writer).
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import Settings
from forge.database import reset_engine
from forge.durable import EventInbox, FlowRun, FlowStatus, GateApproval, StepRun, record_approval
from forge.main import create_app
from forge.models.base import Base
from forge.runs import RunService
from forge.worker.steps import (
    ClaimedStep,
    claim_command_step,
    claim_due_steps,
    command_source_event_id,
    complete_step,
    execute_claimed_step,
    reschedule_expired_leases,
    run_pending_command_step,
    schedule_command_step,
)
from tests.fixtures.fake_gitlab import FakeGitLab, FakeGitLabClientFactory

TEST_SECRET = "test-secret-token"  # noqa: S105 — fake value for tests
PROJECT_ID = 42
ISSUE_IID = 5


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test"),
        GITLAB_WEBHOOK_SECRET=SecretStr(TEST_SECRET),
        FORGE_IMPLEMENTER_BACKEND="builtin",
        FORGE_APPROVERS="alice",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        LITELLM_URL="http://litellm:4000",
        # Pin the environment explicitly — a developer .env must not leak in.
        REDIS_URL=None,
        FORGE_CAPTURE_DIR=None,
        FORGE_BOT_TOKEN=None,
        FORGE_BOT_USERNAME="forge-bot",
    )
    values.update(overrides)
    return Settings(**values)


def note_payload(note: str, *, username: str = "alice", note_id: int = 900) -> dict:
    return {
        "object_kind": "note",
        "event_type": "note",
        "user": {"id": 11, "name": "Alice", "username": username},
        "project": {
            "id": PROJECT_ID,
            "name": "test",
            "path_with_namespace": "group/test",
            "web_url": "https://gitlab.test/group/test",
        },
        "object_attributes": {
            "id": note_id,
            "note": note,
            "noteable_type": "Issue",
        },
        "issue": {
            "id": ISSUE_IID,
            "iid": ISSUE_IID,
            "title": "Add a widget",
            "state": "opened",
        },
    }


def webhook_headers() -> dict[str, str]:
    return {"X-Gitlab-Token": TEST_SECRET, "X-Gitlab-Event": "Note Hook"}


def stub_gitlab(monkeypatch) -> FakeGitLab:
    fake = FakeGitLab()
    fake.seed_issue(ISSUE_IID, "Add a widget", "Make widgets real.")
    fake.seed_commit("main", "base-sha-1", "initial")
    monkeypatch.setattr("forge.runs.service.GitLabClient", FakeGitLabClientFactory(shared=fake))
    return fake


def stub_agents(monkeypatch) -> None:
    from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer

    monkeypatch.setattr(
        "forge.runs.service.build_default_agents",
        lambda *args, **kwargs: (StubPlanner(), StubImplementer(), StubReviewer()),
    )


@pytest.fixture()
async def db(tmp_path):
    """File-backed SQLite: claim tests need several concurrent sessions."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/steps.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture()
async def mem_db():
    """In-memory SQLite (StaticPool) for single-session invariant tests."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class TestTransactionalIngress:
    """(a) 202 only after inbox + scheduled step commit; the BackgroundTask
    executes the persisted step through the claim/lease/fence protocol."""

    @pytest.fixture()
    async def app(self, tmp_path):
        reset_engine()
        application = create_app(
            settings=make_settings(DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/ingress.db")
        )
        async with application.router.lifespan_context(application):
            yield application
        reset_engine()

    @pytest.fixture()
    async def client(self, app) -> AsyncClient:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    async def test_implement_persists_inbox_and_step_then_reaches_gate(
        self, app, client, monkeypatch
    ):
        fake = stub_gitlab(monkeypatch)
        stub_agents(monkeypatch)

        resp = await client.post(
            "/webhook", json=note_payload("@forge /implement"), headers=webhook_headers()
        )

        assert resp.status_code == 202
        assert resp.json()["run_command"] is True

        session_factory = app.state.session_factory
        source_event_id = command_source_event_id("start_run", PROJECT_ID, 900)
        async with session_factory() as session:
            inbox = (await session.execute(select(EventInbox))).scalars().all()
            assert len(inbox) == 1
            assert inbox[0].source_event_id == source_event_id
            assert inbox[0].event_type == "run_command"
            steps = (await session.execute(select(StepRun))).scalars().all()
        assert len(steps) == 1
        step = steps[0]
        assert step.step_name == "start_run"
        assert step.status == "succeeded"  # the BackgroundTask already ran it
        assert step.fence_token == 1
        assert step.lease_owner is not None
        assert step.source_event_id == source_event_id

        # The step walked the run to the human gate.
        async with session_factory() as session:
            runs = (await session.execute(select(FlowRun))).scalars().all()
        assert len(runs) == 1
        assert runs[0].status == FlowStatus.WAITING_APPROVAL.value
        assert fake.notes, "plan comment posted"

    async def test_go_note_schedules_step_without_run(self, app, client):
        """go/cancel steps bind their run at execution time — ingress persists
        the step with flow_run_id NULL in the same transaction as the inbox."""
        resp = await client.post(
            "/webhook",
            json=note_payload(f"@forge /go {'a' * 32}"),
            headers=webhook_headers(),
        )
        assert resp.status_code == 202
        assert resp.json()["run_command"] is True

        async with app.state.session_factory() as session:
            steps = (await session.execute(select(StepRun))).scalars().all()
        assert len(steps) == 1
        assert steps[0].step_name == "go"
        assert steps[0].status == "succeeded"  # unknown run: handler no-ops
        assert steps[0].flow_run_id is None

    async def test_duplicate_note_id_is_deduplicated(self, app, client, monkeypatch):
        """(b) The same note delivered twice → deduplicated response, one run."""
        fake = stub_gitlab(monkeypatch)
        stub_agents(monkeypatch)

        first = await client.post(
            "/webhook", json=note_payload("@forge /implement"), headers=webhook_headers()
        )
        assert first.status_code == 202
        assert first.json().get("deduplicated") is None

        # Exact re-delivery: same note id, same command → same inbox identity.
        second = await client.post(
            "/webhook", json=note_payload("@forge /implement"), headers=webhook_headers()
        )
        assert second.status_code == 202
        assert second.json()["deduplicated"] is True

        async with app.state.session_factory() as session:
            inbox = (await session.execute(select(EventInbox))).scalars().all()
            steps = (await session.execute(select(StepRun))).scalars().all()
            runs = (await session.execute(select(FlowRun))).scalars().all()
        assert len(inbox) == 1
        assert len(steps) == 1
        assert len(runs) == 1
        assert runs[0].status == FlowStatus.WAITING_APPROVAL.value
        # Only the plan comment — the duplicate produced no side effects.
        assert len(fake.notes) == 1


class TestStepClaiming:
    async def test_claim_is_atomic_between_two_sessions(self, db):
        """(d) Two claims of the same step: exactly one wins."""
        async with db() as session:
            async with session.begin():
                session.add(FlowRun(id=uuid4().hex, project_id=1, issue_iid=1))
                await schedule_command_step(
                    session,
                    {"command": "start_run", "project_id": 1},
                    source_event_id=command_source_event_id("start_run", 1, 7),
                )

        first = await claim_due_steps(db, "worker-a")
        assert len(first) == 1
        assert first[0].fence_token == 1
        assert first[0].owner == "worker-a"
        assert first[0].payload["command"] == "start_run"

        # The second session sees the step running — nothing claimable.
        assert await claim_due_steps(db, "worker-b") == []

        async with db() as session:
            step = await session.get(StepRun, first[0].id)
        assert step.status == "running"
        assert step.lease_owner == "worker-a"
        assert step.lease_expires_at is not None

    async def test_claim_respects_due_at(self, db):
        async with db() as session:
            async with session.begin():
                session.add(FlowRun(id=uuid4().hex, project_id=1, issue_iid=1))
                step = await schedule_command_step(
                    session,
                    {"command": "go", "project_id": 1},
                    source_event_id="x" * 64,
                )
                # Backoff pushed the step into the future — not claimable yet.
                step.due_at = step.due_at + timedelta(hours=1)

        assert await claim_due_steps(db, "worker-a") == []

    async def test_claim_command_step_addresses_the_command(self, db):
        seid = command_source_event_id("cancel", PROJECT_ID, 11)
        other = command_source_event_id("go", PROJECT_ID, 12)
        async with db() as session:
            async with session.begin():
                session.add(FlowRun(id=uuid4().hex, project_id=PROJECT_ID, issue_iid=1))
                await schedule_command_step(
                    session,
                    {"command": "cancel", "project_id": PROJECT_ID},
                    source_event_id=seid,
                )
                await schedule_command_step(
                    session,
                    {"command": "go", "project_id": PROJECT_ID},
                    source_event_id=other,
                )

        claimed = await claim_command_step(db, "worker-a", seid)
        assert claimed is not None
        assert claimed.step_name == "cancel"

        # The other command's step is still scheduled for the generic loop.
        rest = await claim_due_steps(db, "worker-a")
        assert [s.step_name for s in rest] == ["go"]


def make_claimed(claimed: ClaimedStep, **overrides) -> ClaimedStep:
    values = dict(
        id=claimed.id,
        flow_run_id=claimed.flow_run_id,
        step_name=claimed.step_name,
        fence_token=claimed.fence_token,
        attempt=claimed.attempt,
        max_attempts=claimed.max_attempts,
        payload=claimed.payload,
        owner=claimed.owner,
        source_event_id=claimed.source_event_id,
    )
    values.update(overrides)
    return ClaimedStep(**values)


class TestFencing:
    async def _scheduled_step(self, db) -> int:
        async with db() as session:
            async with session.begin():
                session.add(FlowRun(id=uuid4().hex, project_id=1, issue_iid=1))
                step = await schedule_command_step(
                    session, {"command": "start_run", "project_id": 1}, source_event_id="e" * 64
                )
                return step.id

    async def _claim(self, db, step_id: int) -> ClaimedStep:
        claimed = await claim_due_steps(db, "worker-a")
        assert [s.id for s in claimed] == [step_id]
        return claimed[0]

    async def test_stale_fence_cannot_complete(self, db):
        """(e) A stale owner's completion (wrong fence token) updates 0 rows
        and leaves the step running for the live owner."""
        step_id = await self._scheduled_step(db)
        claimed = await self._claim(db, step_id)
        zombie = make_claimed(claimed, fence_token=claimed.fence_token - 1, owner="zombie")

        assert await complete_step(db, zombie) is False

        async with db() as session:
            step = await session.get(StepRun, step_id)
        assert step.status == "running"

        # The real owner completes with its own fence.
        assert await complete_step(db, claimed, output={"ok": True}) is True
        async with db() as session:
            step = await session.get(StepRun, step_id)
        assert step.status == "succeeded"
        assert step.output == {"ok": True}

    async def test_lease_expiry_is_reaped_with_attempt_bump(self, db):
        """(f) Expired lease → rescheduled, attempt + 1, fence unchanged."""
        step_id = await self._scheduled_step(db)
        claimed = await self._claim(db, step_id)

        # Simulate a crashed worker: the lease is already in the past.
        async with db() as session:
            async with session.begin():
                await session.execute(
                    update(StepRun)
                    .where(StepRun.id == step_id)
                    .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
                )

        assert await reschedule_expired_leases(db) == 1

        async with db() as session:
            step = await session.get(StepRun, step_id)
        assert step.status == "scheduled"
        assert step.attempt == 1
        assert step.fence_token == claimed.fence_token, "fence stays so zombies lose the CAS"
        assert step.lease_owner is None

        # The zombie still cannot complete with its old fence.
        assert await complete_step(db, claimed) is False

        # And the step is claimable again by any worker.
        reclaimed = await claim_due_steps(db, "worker-b")
        assert [s.id for s in reclaimed] == [step_id]

    async def test_attempts_exhaustion_parks_step_as_dead(self, db, monkeypatch):
        """(g) max_attempts reached → dead, with the last error kept."""
        step_id = await self._scheduled_step(db)
        async with db() as session:
            async with session.begin():
                await session.execute(
                    update(StepRun).where(StepRun.id == step_id).values(max_attempts=1)
                )

        async def exploding(settings, forge_config, session_factory, metadata):
            raise RuntimeError("gitlab down")

        monkeypatch.setattr("forge.worker.steps.execute_run_command", exploding)

        claimed = (await claim_due_steps(db, "worker-a"))[0]
        with pytest.raises(RuntimeError):
            await execute_claimed_step(db, object(), object(), claimed)

        async with db() as session:
            step = await session.get(StepRun, step_id)
        assert step.status == "dead"
        assert step.attempt == 1
        assert "gitlab down" in (step.output or {}).get("error", "")
        assert step.finished_at is not None

        # Dead steps are never re-claimed (poison pill stays put).
        assert await claim_due_steps(db, "worker-b") == []

    async def test_failed_attempt_reschedules_with_backoff(self, db, monkeypatch):
        step_id = await self._scheduled_step(db)

        async def exploding(settings, forge_config, session_factory, metadata):
            raise RuntimeError("llm timeout")

        monkeypatch.setattr("forge.worker.steps.execute_run_command", exploding)

        claimed = (await claim_due_steps(db, "worker-a"))[0]
        with pytest.raises(RuntimeError):
            await execute_claimed_step(db, object(), object(), claimed)

        async with db() as session:
            step = await session.get(StepRun, step_id)
        assert step.status == "scheduled"
        assert step.attempt == 1
        assert step.lease_owner is None
        assert step.due_at >= step.started_at, "retry delay is real (next_due_at)"
        assert await reschedule_expired_leases(db) == 0, "no lease to reap"


class TestRunCreationInvariant:
    def _service(self, db, fake: FakeGitLab) -> RunService:
        from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer

        return RunService(
            session_factory=db,
            gitlab=fake,
            settings=make_settings(),
            planner=StubPlanner(),
            implementer=StubImplementer(),
            reviewer=StubReviewer(),
        )

    async def test_concurrent_start_run_steps_yield_one_flow_run(self, mem_db, monkeypatch):
        """(c) The partial unique index is the arbiter: two start_run
        executions for one issue produce exactly one FlowRun, and the loser
        of the insert race treats the IntegrityError as a duplicate (F12)."""
        fake = FakeGitLab()
        fake.seed_issue(ISSUE_IID, "Add a widget", "Make widgets real.")
        fake.seed_commit("main", "base-sha-1", "initial")
        service = self._service(mem_db, fake)

        # Simulate the race: both handlers' active-run checks run BEFORE
        # either run exists, so both reach the INSERT and the index decides.
        real_find = RunService._find_active_run
        calls = {"n": 0}

        async def racing_find(self, project_id, issue_iid):
            calls["n"] += 1
            if calls["n"] <= 2:
                return None
            return await real_find(self, project_id, issue_iid)

        monkeypatch.setattr(RunService, "_find_active_run", racing_find)

        first = await service.start_run(PROJECT_ID, ISSUE_IID, "t", "d", "alice")
        second = await service.start_run(PROJECT_ID, ISSUE_IID, "t", "d", "alice")

        assert calls["n"] >= 3
        assert second == first, "the race loser adopts the winner's run"
        async with mem_db() as session:
            runs = (await session.execute(select(FlowRun))).scalars().all()
        assert len(runs) == 1
        assert runs[0].status == FlowStatus.WAITING_APPROVAL.value
        # The duplicate still gets the friendly refusal comment.
        assert fake.notes_containing("already active on this issue")

    async def test_terminal_run_does_not_block_a_new_run(self, mem_db):
        """Negative test, other direction: terminal statuses are excluded from
        the partial index, so a cancelled run never blocks a fresh /implement."""
        fake = FakeGitLab()
        fake.seed_issue(ISSUE_IID, "Add a widget", "Make widgets real.")
        fake.seed_commit("main", "base-sha-1", "initial")
        service = self._service(mem_db, fake)

        first = await service.start_run(PROJECT_ID, ISSUE_IID, "t", "d", "alice")
        await service.handle_cancel_note(PROJECT_ID, "/cancel", "alice", ISSUE_IID)
        second = await service.start_run(PROJECT_ID, ISSUE_IID, "t", "d", "alice")

        assert first != second
        async with mem_db() as session:
            runs = (
                (await session.execute(select(FlowRun).order_by(FlowRun.created_at)))
                .scalars()
                .all()
            )
        assert [r.status for r in runs] == [
            FlowStatus.CANCELLED.value,
            FlowStatus.WAITING_APPROVAL.value,
        ]


class TestGateGenerations:
    async def test_one_gate_per_generation(self, mem_db):
        """The (flow_run_id, generation) unique index admits successive
        approval rounds but rejects duplicates of the same round."""
        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        run_id = uuid4().hex
        async with mem_db() as session:
            async with session.begin():
                session.add(FlowRun(id=run_id, project_id=1, issue_iid=1))
                first = await record_approval(
                    session,
                    flow_run_id=run_id,
                    plan_digest="p1",
                    base_sha="b1",
                    policy_digest="pol",
                    approver_user_id=1,
                    source_event_id="s1",
                    expires_at=expires,
                )
                assert first.generation == 0
                second = await record_approval(
                    session,
                    flow_run_id=run_id,
                    plan_digest="p2",
                    base_sha="b2",
                    policy_digest="pol",
                    approver_user_id=1,
                    source_event_id="s2",
                    expires_at=expires,
                )
                assert second.generation == 1

        with pytest.raises(IntegrityError):
            async with mem_db() as session:
                async with session.begin():
                    session.add(
                        GateApproval(
                            flow_run_id=run_id,
                            generation=1,  # duplicate of the second round
                            plan_digest="p3",
                            base_sha="b3",
                            policy_digest="pol",
                            approver_user_id=1,
                            source_event_id="s3",
                            expires_at=expires,
                        )
                    )


class TestSchemaInvariants:
    def test_active_run_index_predicate_matches_terminal_statuses(self):
        """The uq_active_run_per_issue predicate is a literal list of the
        terminal statuses — it must never drift from Controller.TERMINAL_STATUSES
        (ready_for_human included: terminal per ADR-0004, so it does NOT block).
        """
        from forge.durable.controller import TERMINAL_STATUSES

        index = next(
            idx for idx in FlowRun.__table__.indexes if idx.name == "uq_active_run_per_issue"
        )
        predicate = str(index.dialect_options["postgresql"]["where"]).lower()
        for status in TERMINAL_STATUSES:
            assert f"'{status.value}'" in predicate

    def test_step_statuses_include_scheduling_states(self):
        """The step lifecycle gained scheduled/dead (ADR-0017) and the model
        default schedules new steps."""
        assert StepRun.__table__.c.status.default.arg == "scheduled"
        assert StepRun.__table__.c.fence_token.default.arg == 0
        assert StepRun.__table__.c.max_attempts.default.arg == 3
        assert StepRun.__table__.c.flow_run_id.nullable


class TestBackgroundFallbackUsesStepRuntime:
    async def test_run_pending_command_step_claims_then_executes(self, db, monkeypatch):
        """The no-Redis gateway fallback executes the PERSISTED step; a
        re-delivery finds nothing claimable and executes nothing."""
        executed: list[dict] = []

        async def fake_execute(settings, forge_config, session_factory, metadata):
            executed.append(metadata)

        monkeypatch.setattr("forge.worker.steps.execute_run_command", fake_execute)

        seid = command_source_event_id("start_run", PROJECT_ID, 3)
        async with db() as session:
            async with session.begin():
                session.add(FlowRun(id=uuid4().hex, project_id=PROJECT_ID, issue_iid=1))
                await schedule_command_step(
                    session,
                    {"command": "start_run", "project_id": PROJECT_ID},
                    source_event_id=seid,
                )

        await run_pending_command_step(db, object(), object(), seid, owner="gateway-1")
        assert executed == [{"command": "start_run", "project_id": PROJECT_ID}]

        await run_pending_command_step(db, object(), object(), seid, owner="gateway-2")
        assert len(executed) == 1
