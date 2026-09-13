"""M2-2/M2-3: RunService end-to-end with the ci_harness backend (ADR-0015).

/go triggers the harness pipeline → durable waiting_harness; the reconciler
polls, verifies the branch head and adopts it (Draft MR → waiting_ci →
ready_for_human). A failed HARNESS JOB blocks; a CI code failure with budget
left re-delegates to the harness with the repair context in the brief.
Builtin runs are covered by test_runs_service.py / test_runs_repair.py and
must stay untouched.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import Settings
from forge.durable import ActionLog, FlowRun, FlowStatus
from forge.durable.identity import factory_branch
from forge.models.base import Base
from forge.repository import ChangesetWriter
from forge.runs import RunService
from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer
from tests.fixtures.fake_gitlab import FakeGitLab

PROJECT_ID = 42
ISSUE_IID = 7
ISSUE_TITLE = "Add a widget"
ISSUE_DESC = "Widgets make the app better."
HARNESS_SHA = "harness-sha-1"


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        FORGE_APPROVERS="alice",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        FORGE_IMPLEMENTER_BACKEND="ci_harness",
    )
    values.update(overrides)
    return Settings(**values)


def make_service(db, fake_gitlab, **overrides) -> RunService:
    """RunService on the ci_harness backend over fakes (no LLM anywhere).

    The real ChangesetWriter is kept: the harness backend only uses its
    public ensure_branch. Planner/implementer/reviewer stay stubs — the
    planner runs before the gate, the reviewer is backend-independent, and
    the stub implementer is never invoked on the harness path.
    """
    values = dict(
        session_factory=db,
        gitlab=fake_gitlab,
        settings=make_settings(),
        writer_class=ChangesetWriter,
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
    return make_service(db, fake_gitlab)


async def get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


async def start_and_go(service, db, fake_gitlab) -> tuple[str, int, str]:
    """Drive a run through /implement + /go on the harness backend.

    Returns (run_id, pipeline_id, factory_branch).
    """
    run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "bob")
    await service.handle_command_note(
        PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
    )
    run = await get_run(db, run_id)
    assert run.status == FlowStatus.WAITING_HARNESS.value
    pipeline_id = int(run.evidence["harness"]["pipeline_id"])
    return run_id, pipeline_id, factory_branch(ISSUE_IID, run_id)


def seed_forge_agent_job(
    fake_gitlab: FakeGitLab, pipeline_id: int, *, status: str, failure_reason=None, log=None
) -> None:
    job: dict = {"id": 555, "name": "forge-agent", "status": status}
    if failure_reason is not None:
        job["failure_reason"] = failure_reason
    fake_gitlab.set_pipeline_jobs(pipeline_id, [job])
    if log is not None:
        fake_gitlab.set_job_log(555, log)


def result_log(sha: str, summary: str = "done") -> str:
    return f'claude output...\nFORGE_RESULT:{{"head": "{sha}", "summary": "{summary}"}}\n'


class TestGoStartsHarness:
    async def test_go_creates_pipeline_and_parks_in_waiting_harness(self, service, fake_gitlab, db):
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "bob")
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value

        # The pipeline was created on the factory branch with the expected
        # run variables (ADR-0015 §5).
        branch = factory_branch(ISSUE_IID, run_id)
        assert branch in fake_gitlab.branches  # ensure_branch ran
        (pipeline,) = fake_gitlab.pipelines
        assert pipeline["ref"] == branch
        by_key = {v["key"]: v["value"] for v in pipeline["variables"]}
        assert by_key["FORGE_RUN_ID"] == run_id
        assert by_key["FORGE_ISSUE_IID"] == str(ISSUE_IID)
        assert by_key["FORGE_ISSUE_TITLE"] == ISSUE_TITLE
        assert by_key["FORGE_PLAN"]
        assert by_key["FORGE_HARNESS_MODEL"]

        # The backend choice and the durable handle live in the evidence.
        assert run.evidence["backend"] == "ci_harness"
        assert run.evidence["harness"]["pipeline_id"] == pipeline["id"]
        assert run.evidence["harness"]["handle"]
        assert run.base_sha == "base-sha-1"

        # The journaled harness_start intent, correlated to the pipeline.
        async with db() as session:
            actions = (await session.execute(select(ActionLog))).scalars().all()
        (action,) = [a for a in actions if a.action_kind == "harness_start"]
        assert action.status == "succeeded"
        assert action.correlation_id == f"pipeline-{pipeline['id']}"
        assert action.remote_result["pipeline_id"] == pipeline["id"]

        # No forge-side implementer commit, no MR yet.
        assert fake_gitlab.merge_requests == {}
        assert run.candidate_shas in (None, [])

    async def test_redelivered_go_is_ignored(self, service, fake_gitlab, db):
        run_id, _pipeline_id, _branch = await start_and_go(service, db, fake_gitlab)

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_HARNESS.value
        assert len(fake_gitlab.pipelines) == 1  # no second harness pipeline


class TestHarnessReconciler:
    async def test_running_job_keeps_waiting(self, service, fake_gitlab, db):
        run_id, pipeline_id, _branch = await start_and_go(service, db, fake_gitlab)
        seed_forge_agent_job(fake_gitlab, pipeline_id, status="running")

        await service.evaluate_waiting_harness()

        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_HARNESS.value

    async def test_verified_change_flows_to_ready_for_human(self, service, fake_gitlab, db):
        run_id, pipeline_id, branch = await start_and_go(service, db, fake_gitlab)
        seed_forge_agent_job(
            fake_gitlab, pipeline_id, status="success", log=result_log(HARNESS_SHA)
        )
        fake_gitlab.seed_commit(branch, HARNESS_SHA, "forge: implement 7")

        await service.evaluate_waiting_harness()

        run = await get_run(db, run_id)
        # Verified head adopted as candidate; Draft MR created; waiting for CI.
        assert run.status == FlowStatus.WAITING_CI.value
        assert run.candidate_shas == [HARNESS_SHA]
        assert run.mr_iid is not None
        assert run.evidence["harness_change"]["sha"] == HARNESS_SHA

        # The verification pipeline for the candidate: green → review → ready.
        verify_pipeline = (await fake_gitlab.create_pipeline(PROJECT_ID, branch))["id"]
        fake_gitlab.set_pipeline_status(verify_pipeline, "success", HARNESS_SHA)
        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        assert run.evidence["backend"] == "ci_harness"
        assert run.evidence["review"]["sha"] == HARNESS_SHA  # review bound to it

    async def test_infrastructure_failure_blocks_without_repair(self, service, fake_gitlab, db):
        run_id, pipeline_id, _branch = await start_and_go(service, db, fake_gitlab)
        seed_forge_agent_job(
            fake_gitlab, pipeline_id, status="failed", failure_reason="runner_system_failure"
        )

        await service.evaluate_waiting_harness()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("harness_infrastructure")
        # Blocked before any forge-side implementer/MR work happened.
        assert fake_gitlab.merge_requests == {}

    async def test_timeout_blocks_as_harness_timeout(self, service, fake_gitlab, db):
        run_id, pipeline_id, _branch = await start_and_go(service, db, fake_gitlab)
        seed_forge_agent_job(fake_gitlab, pipeline_id, status="running")

        run = await get_run(db, run_id)
        started_at = json.loads(run.evidence["harness"]["handle"])["started_at"]
        late = datetime.fromisoformat(started_at) + timedelta(
            seconds=make_settings().FORGE_HARNESS_TIMEOUT_SECONDS + 1
        )

        await service.evaluate_waiting_harness(now=late)

        blocked = await get_run(db, run_id)
        assert blocked.status == FlowStatus.BLOCKED.value
        assert "harness_timeout" in blocked.status_reason

    async def test_before_deadline_still_blocks_never(self, service, fake_gitlab, db):
        run_id, pipeline_id, _branch = await start_and_go(service, db, fake_gitlab)
        seed_forge_agent_job(fake_gitlab, pipeline_id, status="running")

        await service.evaluate_waiting_harness(
            now=datetime.now(timezone.utc) + timedelta(seconds=1)
        )

        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_HARNESS.value

    async def test_code_failure_blocks_with_harness_kind(self, service, fake_gitlab, db):
        run_id, pipeline_id, _branch = await start_and_go(service, db, fake_gitlab)
        seed_forge_agent_job(
            fake_gitlab,
            pipeline_id,
            status="failed",
            failure_reason="script_failure",
            log="AssertionError",
        )

        await service.evaluate_waiting_harness()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("harness_code")

    async def test_sha_mismatch_blocks_the_run(self, service, fake_gitlab, db):
        run_id, pipeline_id, branch = await start_and_go(service, db, fake_gitlab)
        seed_forge_agent_job(
            fake_gitlab,
            pipeline_id,
            status="success",
            log=result_log("claimed-sha"),
        )
        fake_gitlab.seed_commit(branch, HARNESS_SHA, "actual head")

        await service.evaluate_waiting_harness()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "harness_sha_mismatch" in run.status_reason
        assert fake_gitlab.merge_requests == {}


class TestHarnessRepairInterplay:
    async def _adopt_candidate(self, service, fake_gitlab, db) -> tuple[str, int, str]:
        """Drive a harness run to waiting_ci with a verified candidate."""
        run_id, pipeline_id, branch = await start_and_go(service, db, fake_gitlab)
        seed_forge_agent_job(
            fake_gitlab, pipeline_id, status="success", log=result_log(HARNESS_SHA)
        )
        fake_gitlab.seed_commit(branch, HARNESS_SHA, "forge: implement 7")
        await service.evaluate_waiting_harness()
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        return run_id, pipeline_id, branch

    async def test_ci_code_failure_repairs_via_new_harness_pipeline(self, service, fake_gitlab, db):
        """CI code failure with budget left re-delegates to the harness with
        the repair context in the brief (ADR-0015) — not a builtin repair."""
        run_id, _pipeline_id, branch = await self._adopt_candidate(service, fake_gitlab, db)

        failed_pipeline = (await fake_gitlab.create_pipeline(PROJECT_ID, branch))["id"]
        fake_gitlab.set_pipeline_status(failed_pipeline, "failed", HARNESS_SHA)
        fake_gitlab.set_pipeline_jobs(
            failed_pipeline,
            [
                {
                    "id": 999,
                    "name": "tests",
                    "status": "failed",
                    "failure_reason": "script_failure",
                }
            ],
        )
        fake_gitlab.set_job_log(999, "AssertionError: 2 + 2 != 5\nE   assert 4 == 5")

        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value
        assert run.commit_cycle == 2

        # A SECOND harness pipeline was started on the same branch (after the
        # original harness pipeline and the failed verification pipeline), and
        # its brief (FORGE_PLAN) carries the bounded CI failure context.
        assert len(fake_gitlab.pipelines) == 3
        repair_pipeline = fake_gitlab.pipelines[-1]
        assert repair_pipeline["ref"] == branch
        by_key = {v["key"]: v["value"] for v in repair_pipeline["variables"]}
        assert "Repair context" in by_key["FORGE_PLAN"]
        assert "AssertionError" in by_key["FORGE_PLAN"]

        # The repair delegation is journaled like the first start.
        async with db() as session:
            actions = (await session.execute(select(ActionLog))).scalars().all()
        starts = [a for a in actions if a.action_kind == "harness_start"]
        assert len(starts) == 2
        assert starts[-1].correlation_id == f"pipeline-{repair_pipeline['id']}"

    async def test_repair_budget_exhaustion_blocks(self, service, fake_gitlab, db):
        """After FORGE_MAX_COMMIT_CYCLES repair attempts the run blocks."""
        settings = make_settings(FORGE_MAX_COMMIT_CYCLES=2)
        svc = make_service(db, fake_gitlab, settings=settings)
        run_id, _pipeline_id, branch = await self._adopt_candidate(svc, fake_gitlab, db)

        for _ in range(settings.FORGE_MAX_COMMIT_CYCLES):
            failed_pipeline = (await fake_gitlab.create_pipeline(PROJECT_ID, branch))["id"]
            fake_gitlab.set_pipeline_status(failed_pipeline, "failed", HARNESS_SHA)
            fake_gitlab.set_pipeline_jobs(
                failed_pipeline,
                [
                    {
                        "id": 999,
                        "name": "tests",
                        "status": "failed",
                        "failure_reason": "script_failure",
                    }
                ],
            )
            # Repair cycles park the run in waiting_harness — release them.
            repair_pipeline_id = int((await get_run(db, run_id)).evidence["harness"]["pipeline_id"])
            seed_forge_agent_job(
                fake_gitlab, repair_pipeline_id, status="success", log=result_log(HARNESS_SHA)
            )
            fake_gitlab.seed_commit(branch, HARNESS_SHA, "forge: implement 7 (repair)")
            await svc.evaluate_waiting_harness()
            await svc.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("commit_cycles_exhausted")
