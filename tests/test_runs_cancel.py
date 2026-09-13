"""Stage B2 (F13, ADR-0018 §4): cancel = revoke the publication grant first.

- ``handle_cancel_note`` sets ``run.cancel_requested`` and withdraws scheduled
  steps (status ``cancelled``) BEFORE the terminal cancelled transition — a
  late scheduled execution finds nothing claimable.
- A proposal leg that was already in flight when the cancel landed re-reads
  the grant right before ``writer.apply`` and stands down.
- A verified harness candidate for a cancelled run is recorded as
  ``superseded`` evidence and never adopted — the run stays cancelled.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import Settings
from forge.durable import FlowRun, FlowStatus, StepRun
from forge.models.base import Base
from forge.repository import ChangesetWriter
from forge.runs import RunService
from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer
from forge.worker.steps import claim_due_steps
from tests.fixtures.candidate import create_diff, seed_candidate
from tests.fixtures.fake_gitlab import FakeGitLab
from tests.test_runs_service import FakeWriter

PROJECT_ID = 42
ISSUE_IID = 7
ISSUE_TITLE = "Add a widget"
ISSUE_DESC = "Widgets make the app better."


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        FORGE_IMPLEMENTER_BACKEND="builtin",
        FORGE_APPROVERS="alice",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
    )
    values.update(overrides)
    return Settings(**values)


def make_service(db, fake_gitlab, **overrides) -> RunService:
    values = dict(
        session_factory=db,
        gitlab=fake_gitlab,
        settings=make_settings(),
        writer_class=FakeWriter,
        planner=StubPlanner(),
        implementer=StubImplementer(),
        reviewer=StubReviewer(),
    )
    values.update(overrides)
    return RunService(**values)


@pytest.fixture()
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture()
def fake_gitlab() -> FakeGitLab:
    fake = FakeGitLab()
    fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
    fake.seed_commit("main", "base-sha-1", "initial")
    return fake


@pytest.fixture()
def service(db, fake_gitlab):
    FakeWriter.reset()
    return make_service(db, fake_gitlab)


async def get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


async def seed_scheduled_step(db, run_id: str) -> int:
    async with db() as session:
        step = StepRun(
            flow_run_id=run_id,
            step_name="go",
            status="scheduled",
            due_at=datetime.now(timezone.utc),
            source_event_id="e" * 64,
        )
        session.add(step)
        await session.commit()
        return step.id


async def get_step(db, step_id: int) -> StepRun:
    async with db() as session:
        return await session.get(StepRun, step_id)


class TestCancelWithdrawsSteps:
    async def test_cancel_sets_flag_and_cancels_scheduled_steps(self, service, db):
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        step_id = await seed_scheduled_step(db, run_id)

        await service.handle_cancel_note(PROJECT_ID, f"@forge /cancel {run_id}", "alice", ISSUE_IID)

        run = await get_run(db, run_id)
        assert run.cancel_requested is True
        assert run.status == FlowStatus.CANCELLED.value
        assert (await get_step(db, step_id)).status == "cancelled"

    async def test_late_scheduled_execution_finds_nothing(self, service, db):
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        await seed_scheduled_step(db, run_id)
        await service.handle_cancel_note(PROJECT_ID, f"@forge /cancel {run_id}", "alice", ISSUE_IID)

        # A worker poll after the cancel claims nothing: the withdrawn step
        # is no longer scheduled.
        assert await claim_due_steps(db, owner="worker-1") == []


class TestPublicationGrant:
    async def test_cancel_mid_proposal_revokes_the_grant(self, db, fake_gitlab, monkeypatch):
        service = make_service(db, fake_gitlab)
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        # Simulate a cancel note landing while the proposal leg is in flight:
        # it lands right AFTER the committing transition, before the write.
        original_transition = service._transition

        async def cancel_after_committing(target_run_id, status, reason=None):
            result = await original_transition(target_run_id, status, reason=reason)
            if status == FlowStatus.COMMITTING:
                await service.handle_cancel_note(
                    PROJECT_ID, f"@forge /cancel {target_run_id}", "alice", ISSUE_IID
                )
            return result

        monkeypatch.setattr(service, "_transition", cancel_after_committing)

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.CANCELLED.value
        assert run.cancel_requested is True
        # The grant was revoked before publication: nothing was written.
        assert all(len(writer.calls) == 0 for writer in FakeWriter.instances)
        assert run.candidate_shas in (None, [])
        assert fake_gitlab.merge_requests == {}

    async def test_verified_harness_result_after_cancel_is_superseded(self, db, fake_gitlab):
        harness_settings = make_settings(FORGE_IMPLEMENTER_BACKEND="ci_harness")
        service = make_service(
            db, fake_gitlab, settings=harness_settings, writer_class=ChangesetWriter
        )
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_HARNESS.value

        await service.handle_cancel_note(PROJECT_ID, f"@forge /cancel {run_id}", "alice", ISSUE_IID)

        # The harness finished anyway; the reconciler polls the (already
        # cancelled) run and finds a well-formed candidate bundle.
        pipeline_id = int((await get_run(db, run_id)).evidence["harness"]["pipeline_id"])
        fake_gitlab.set_pipeline_jobs(
            pipeline_id,
            [{"id": 555, "name": "forge-agent", "status": "success"}],
        )
        seed_candidate(
            fake_gitlab,
            555,
            attempt_base="base-sha-1",
            diff=create_diff("forge-demo/x.md", "hello\n"),
        )

        await service._evaluate_harness_one(run_id, datetime.now(timezone.utc))

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.CANCELLED.value  # stays cancelled
        assert run.candidate_shas in (None, [])  # never adopted
        assert fake_gitlab.merge_requests == {}  # no Draft MR either
        assert run.evidence["superseded"] == {
            "reason": "cancelled",
            "attempt_base": "base-sha-1",
        }

    async def test_evaluate_waiting_harness_skips_cancelled_runs(self, db, fake_gitlab):
        """The reconciler loop itself never polls terminal runs."""
        harness_settings = make_settings(FORGE_IMPLEMENTER_BACKEND="ci_harness")
        service = make_service(
            db, fake_gitlab, settings=harness_settings, writer_class=ChangesetWriter
        )
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )
        await service.handle_cancel_note(PROJECT_ID, f"@forge /cancel {run_id}", "alice", ISSUE_IID)
        pipelines_before = len(fake_gitlab.pipelines)

        await service.evaluate_waiting_harness()

        assert (await get_run(db, run_id)).status == FlowStatus.CANCELLED.value
        assert "superseded" not in ((await get_run(db, run_id)).evidence or {})
        assert len(fake_gitlab.pipelines) == pipelines_before
