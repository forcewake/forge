"""Tests for the waiting_ci reconciler tick and the run_reconciler loop."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import Settings
from forge.durable import Controller, FlowRun, FlowStatus, Outbox
from forge.gitlab.client import GitLabAPIError
from forge.models.base import Base
from forge.runs import RunService, run_reconciler
from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer, factory_branch
from tests.fixtures.fake_gitlab import FakeGitLab

PROJECT_ID = 42
ISSUE_IID = 7
SHA = "candidate-sha-1"


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
    )
    values.update(overrides)
    return Settings(**values)


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
    fake.seed_issue(ISSUE_IID, "Add a widget")
    return fake


@pytest.fixture()
def service(db, fake_gitlab):
    return RunService(
        session_factory=db,
        gitlab=fake_gitlab,
        settings=make_settings(),
        planner=StubPlanner(),
        implementer=StubImplementer(),
        reviewer=StubReviewer(),
    )


async def make_waiting_ci_run(
    db, fake_gitlab: FakeGitLab | None = None, *, issue_iid: int = ISSUE_IID
) -> str:
    """Create a run parked in waiting_ci with a candidate sha (as /go leaves it)."""
    run_id = uuid4().hex
    mr_iid = 12
    if fake_gitlab is not None:
        mr = await fake_gitlab.create_merge_request(
            PROJECT_ID, branch_for(run_id), "main", "Draft: Add a widget"
        )
        mr_iid = mr["iid"]
    async with db() as session:
        controller = Controller(session)
        session.add(
            FlowRun(id=run_id, project_id=PROJECT_ID, issue_iid=issue_iid, plan_digest="dig")
        )
        await controller.transition(run_id, FlowStatus.PREFLIGHT)
        await controller.transition(run_id, FlowStatus.PLANNING)
        await controller.transition(run_id, FlowStatus.WAITING_APPROVAL)
        await controller.transition(run_id, FlowStatus.PROPOSING)
        await controller.transition(run_id, FlowStatus.VALIDATING)
        await controller.transition(run_id, FlowStatus.COMMITTING)
        await controller.transition(run_id, FlowStatus.ENSURING_DRAFT_MR)
        await controller.transition(run_id, FlowStatus.WAITING_CI, reason="pipeline")
        run = await session.get(FlowRun, run_id)
        run.mr_iid = mr_iid
        run.candidate_shas = [SHA]
        await session.commit()
    return run_id


async def get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


async def entered_waiting_ci_at(db, run_id: str):
    """The durable waiting_ci outbox timestamp — the CI deadline anchor."""
    async with db() as session:
        rows = (
            (
                await session.execute(
                    select(Outbox).where(Outbox.flow_run_id == run_id).order_by(Outbox.id)
                )
            )
            .scalars()
            .all()
        )
    return next(r.created_at for r in rows if r.payload["to"] == "waiting_ci")


def past_deadline(entered_at):
    """A reconciler ``now`` one second beyond the FORGE_CI_WAIT_SECONDS deadline."""
    import datetime as dt

    return entered_at + dt.timedelta(seconds=make_settings().FORGE_CI_WAIT_SECONDS + 1)


def branch_for(run_id: str) -> str:
    return factory_branch(ISSUE_IID, run_id)


async def seed_success_pipeline(fake_gitlab: FakeGitLab, run_id: str) -> int:
    """Head of the bot branch equals the candidate; CI succeeded for that sha."""
    fake_gitlab.seed_commit(branch_for(run_id), SHA, "forge: implement 7")
    pipeline_id = (await fake_gitlab.create_pipeline(PROJECT_ID, branch_for(run_id)))["id"]
    fake_gitlab.set_pipeline_status(pipeline_id, "success", SHA)
    return pipeline_id


class TestEvaluateWaitingCi:
    async def test_success_reaches_ready_for_human_with_evidence(self, service, fake_gitlab, db):
        run_id = await make_waiting_ci_run(db, fake_gitlab)
        await seed_success_pipeline(fake_gitlab, run_id)

        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value

        # Evidence comment binds MR, exact sha, pipeline and plan digest.
        (note,) = fake_gitlab.notes
        assert "merge_requests/" in note["body"]  # MR web url from the API
        assert SHA in note["body"]
        assert "success" in note["body"]
        assert "dig" in note["body"]  # plan digest

    async def test_review_verdict_recorded_in_reviewing_reason(self, service, fake_gitlab, db):
        run_id = await make_waiting_ci_run(db, fake_gitlab)
        await seed_success_pipeline(fake_gitlab, run_id)

        await service.evaluate_waiting_ci()

        # The readonly review leg is visible in the durable transition history.
        async with db() as session:
            rows = (
                (
                    await session.execute(
                        select(Outbox).where(Outbox.flow_run_id == run_id).order_by(Outbox.id)
                    )
                )
                .scalars()
                .all()
            )
        reviewing = next(r for r in rows if r.payload["to"] == "reviewing")
        assert "readonly review" in reviewing.payload["reason"]

    async def test_reviewing_transition_happens_before_ready(self, service, fake_gitlab, db):
        run_id = await make_waiting_ci_run(db, fake_gitlab)
        await seed_success_pipeline(fake_gitlab, run_id)

        await service.evaluate_waiting_ci()

        async with db() as session:
            rows = (
                (
                    await session.execute(
                        select(Outbox).where(Outbox.flow_run_id == run_id).order_by(Outbox.id)
                    )
                )
                .scalars()
                .all()
            )
        targets = [row.payload["to"] for row in rows]
        assert (
            targets.index("evaluating_ci")
            < targets.index("reviewing")
            < targets.index("ready_for_human")
        )

    async def test_failed_pipeline_blocks_when_repair_budget_spent(self, db, fake_gitlab):
        """Code failure with no repair budget left parks the run (ADR-0004/0008)."""
        service = RunService(
            session_factory=db,
            gitlab=fake_gitlab,
            settings=make_settings(FORGE_MAX_COMMIT_CYCLES=1),
            planner=StubPlanner(),
            implementer=StubImplementer(),
            reviewer=StubReviewer(),
        )
        run_id = await make_waiting_ci_run(db, fake_gitlab)
        fake_gitlab.seed_commit(branch_for(run_id), SHA, "forge: implement 7")
        pipeline_id = (await fake_gitlab.create_pipeline(PROJECT_ID, branch_for(run_id)))["id"]
        fake_gitlab.set_pipeline_status(pipeline_id, "failed", SHA)
        # A script_failure makes this an honest code failure — empty evidence
        # would classify as unknown and block before the budget is consulted.
        fake_gitlab.set_pipeline_jobs(
            pipeline_id,
            [{"id": 1, "name": "pytest", "status": "failed", "failure_reason": "script_failure"}],
        )

        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("commit_cycles_exhausted")

    async def test_branch_head_drift_blocks_external_change(self, service, fake_gitlab, db):
        """A human push on the bot branch invalidates the verdict (ADR-0006)."""
        run_id = await make_waiting_ci_run(db, fake_gitlab)
        await seed_success_pipeline(fake_gitlab, run_id)
        fake_gitlab.seed_commit(branch_for(run_id), "human-sha-99", "human push")  # new head

        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason == "external_change"

    async def test_missing_pipeline_keeps_waiting_before_deadline(self, service, fake_gitlab, db):
        run_id = await make_waiting_ci_run(db, fake_gitlab)
        fake_gitlab.seed_commit(
            branch_for(run_id), SHA, "forge: implement 7"
        )  # head ok, no pipeline

        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value

    async def test_missing_pipeline_blocks_after_deadline(self, service, fake_gitlab, db):
        run_id = await make_waiting_ci_run(db, fake_gitlab)
        fake_gitlab.seed_commit(branch_for(run_id), SHA, "forge: implement 7")

        entered = await entered_waiting_ci_at(db, run_id)
        await service.evaluate_waiting_ci(now=past_deadline(entered))

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason == "ci_timeout"

    async def test_running_pipeline_blocks_after_deadline(self, service, fake_gitlab, db):
        """F17: a pipeline stuck in an active state past the deadline times
        out instead of keeping the run waiting forever."""
        run_id = await make_waiting_ci_run(db, fake_gitlab)
        fake_gitlab.seed_commit(branch_for(run_id), SHA, "forge: implement 7")
        pipeline_id = (await fake_gitlab.create_pipeline(PROJECT_ID, branch_for(run_id)))["id"]
        fake_gitlab.set_pipeline_status(pipeline_id, "running", SHA)

        entered = await entered_waiting_ci_at(db, run_id)
        await service.evaluate_waiting_ci(now=past_deadline(entered))

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason == "ci_timeout"

    async def test_pipeline_read_errors_block_after_deadline(self, service, fake_gitlab, db):
        """F17: repeated pipeline API failures past the deadline time out
        instead of waiting forever behind a broken GitLab read."""
        run_id = await make_waiting_ci_run(db, fake_gitlab)
        fake_gitlab.seed_commit(branch_for(run_id), SHA, "forge: implement 7")

        async def failing_pipelines(*args, **kwargs):
            raise GitLabAPIError(500, "gitlab down")

        fake_gitlab.list_pipelines = failing_pipelines

        entered = await entered_waiting_ci_at(db, run_id)
        await service.evaluate_waiting_ci()  # before the deadline: keep waiting
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_CI.value

        await service.evaluate_waiting_ci(now=past_deadline(entered))
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason == "ci_timeout"

    async def test_pending_pipeline_keeps_waiting(self, service, fake_gitlab, db):
        run_id = await make_waiting_ci_run(db, fake_gitlab)
        fake_gitlab.seed_commit(branch_for(run_id), SHA, "forge: implement 7")
        pipeline_id = (await fake_gitlab.create_pipeline(PROJECT_ID, branch_for(run_id)))["id"]
        fake_gitlab.set_pipeline_status(pipeline_id, "running", SHA)

        await service.evaluate_waiting_ci()

        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_CI.value

    async def test_one_broken_run_does_not_stall_others(self, service, fake_gitlab, db):
        run_id = await make_waiting_ci_run(db, fake_gitlab)
        await seed_success_pipeline(fake_gitlab, run_id)
        # A second, healthy run in waiting_ci on a DIFFERENT issue — the F12
        # invariant allows only one ACTIVE run per (project, issue).
        other = await make_waiting_ci_run(db, issue_iid=ISSUE_IID + 1)
        await seed_success_pipeline(fake_gitlab, other)

        # Corrupt the first run's project so its reads explode mid-tick.
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.project_id = 0  # fake treats unknown project gracefully; force error
            run.issue_iid = None
            await session.commit()

        # Force a hard failure for the broken run by monkeypatching list_commits
        # to raise only for the corrupted branch.
        original = fake_gitlab.list_commits
        broken_branch = factory_branch(None, run_id)

        async def exploding(project_id, ref):
            if ref == broken_branch:
                raise RuntimeError("boom")
            return await original(project_id, ref)

        fake_gitlab.list_commits = exploding

        await service.evaluate_waiting_ci()  # must not raise

        assert (await get_run(db, other)).status == FlowStatus.READY_FOR_HUMAN.value

    async def test_missing_candidate_sha_blocks(self, service, fake_gitlab, db):
        run_id = await make_waiting_ci_run(db, fake_gitlab)
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.candidate_shas = []
            await session.commit()

        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value


class TestReconcilerLoop:
    async def test_loop_runs_until_shutdown(self, db, fake_gitlab):
        service = RunService(session_factory=db, gitlab=fake_gitlab, settings=make_settings())
        shutdown = asyncio.Event()
        calls = {"n": 0}

        async def counting_evaluate(now=None):
            calls["n"] += 1
            if calls["n"] >= 2:
                shutdown.set()

        service.evaluate_waiting_ci = counting_evaluate  # type: ignore[method-assign]

        await run_reconciler(service, interval_seconds=0.01, shutdown_event=shutdown)
        assert calls["n"] == 2

    async def test_loop_swallows_evaluate_errors(self, db, fake_gitlab):
        service = RunService(session_factory=db, gitlab=fake_gitlab, settings=make_settings())
        shutdown = asyncio.Event()
        calls = {"n": 0}

        async def failing_evaluate(now=None):
            calls["n"] += 1
            shutdown.set()
            raise RuntimeError("pass blew up")

        service.evaluate_waiting_ci = failing_evaluate  # type: ignore[method-assign]

        await run_reconciler(service, interval_seconds=0.01, shutdown_event=shutdown)
        assert calls["n"] == 1
