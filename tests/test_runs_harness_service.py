"""M2-2/M2-3 + Stage D: RunService end-to-end with the ci_harness backend.

/go triggers the harness pipeline → durable waiting_harness; the reconciler
polls, downloads the candidate artifacts (ADR-0016) and publishes them
through the trusted publisher (Draft MR → waiting_ci → ready_for_human).
A failed HARNESS JOB blocks; a CI code failure with budget left re-delegates
to the harness with the repair context in the brief. Builtin runs are
covered by test_runs_service.py / test_runs_repair.py and must stay
untouched.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import Settings
from forge.durable import ActionLog, FlowRun, FlowStatus, LLMCall
from forge.durable.identity import factory_branch
from forge.models.base import Base
from forge.repository import ChangesetWriter
from forge.runs import RunService
from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer
from tests.fixtures.candidate import create_diff, seed_candidate
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
    run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
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


def seed_success_with_candidate(
    fake_gitlab: FakeGitLab,
    pipeline_id: int,
    *,
    attempt_base: str,
    diff: str | None = None,
    exit: str = "completed",
    usage: dict | None = None,
) -> None:
    """Job success + the candidate artifacts (ADR-0016 proposal-only flow)."""
    seed_forge_agent_job(fake_gitlab, pipeline_id, status="success")
    seed_candidate(
        fake_gitlab,
        555,
        attempt_base=attempt_base,
        diff=diff if diff is not None else create_diff("forge-demo/x.md", "hello\n"),
        exit=exit,
        usage=usage,
    )


class TestGoStartsHarness:
    async def test_go_creates_pipeline_and_parks_in_waiting_harness(self, service, fake_gitlab, db):
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
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
        assert by_key["FORGE_ATTEMPT_BASE"] == run.base_sha  # frozen attempt base

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

    async def test_candidate_artifact_flows_to_ready_for_human(self, service, fake_gitlab, db):
        run_id, pipeline_id, branch = await start_and_go(service, db, fake_gitlab)
        seed_success_with_candidate(fake_gitlab, pipeline_id, attempt_base="base-sha-1")

        await service.evaluate_waiting_harness()

        run = await get_run(db, run_id)
        # The publisher wrote the candidate; the published commit sha is the
        # new branch head and the only candidate (ADR-0016).
        assert run.status == FlowStatus.WAITING_CI.value
        (candidate_sha,) = run.candidate_shas
        assert candidate_sha != "base-sha-1"
        assert fake_gitlab.branches[branch][0]["sha"] == candidate_sha
        assert run.mr_iid is not None
        assert run.evidence["harness_change"]["sha"] == candidate_sha
        assert run.evidence["published_candidate"]["attempt_base"] == "base-sha-1"

        # The verification pipeline for the candidate: green → review → ready.
        verify_pipeline = (await fake_gitlab.create_pipeline(PROJECT_ID, branch))["id"]
        fake_gitlab.set_pipeline_status(verify_pipeline, "success", candidate_sha)
        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        assert run.evidence["backend"] == "ci_harness"
        assert run.evidence["review"]["sha"] == candidate_sha  # review bound to it

    async def test_published_candidate_records_usage_receipt(self, service, fake_gitlab, db):
        run_id, pipeline_id, _branch = await start_and_go(service, db, fake_gitlab)
        seed_success_with_candidate(
            fake_gitlab,
            pipeline_id,
            attempt_base="base-sha-1",
            usage={"input_tokens": 21, "cached_input_tokens": 4, "output_tokens": 7},
        )

        await service.evaluate_waiting_harness()

        async with db() as session:
            rows = (await session.execute(select(LLMCall))).scalars().all()
        (row,) = [row for row in rows if row.role == "implementer"]
        assert row.provider == "ci_harness"
        assert row.input_tokens == 21  # uncached input, cached kept separate
        assert row.cached_tokens == 4
        assert row.output_tokens == 7
        assert row.driver == "claude-code"
        assert row.completeness == "aggregate"

    async def test_no_changes_blocks_with_harness_no_changes(self, service, fake_gitlab, db):
        run_id, pipeline_id, _branch = await start_and_go(service, db, fake_gitlab)
        seed_success_with_candidate(fake_gitlab, pipeline_id, attempt_base="base-sha-1", diff="")

        await service.evaluate_waiting_harness()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "harness_no_changes" in run.status_reason
        assert fake_gitlab.merge_requests == {}

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

    async def test_unknown_failure_reason_blocks_as_infrastructure(self, service, fake_gitlab, db):
        """No failure reason does not blame the code (ADR-0008) — seen live
        when a canceled job was read transiently as failed with no reason."""
        run_id, pipeline_id, _branch = await start_and_go(service, db, fake_gitlab)
        seed_forge_agent_job(
            fake_gitlab,
            pipeline_id,
            status="failed",
            failure_reason=None,
            log="still streaming output...",
        )

        await service.evaluate_waiting_harness()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("harness_infrastructure")

    async def test_artifact_base_mismatch_blocks_the_run(self, service, fake_gitlab, db):
        run_id, pipeline_id, _branch = await start_and_go(service, db, fake_gitlab)
        seed_success_with_candidate(fake_gitlab, pipeline_id, attempt_base="claimed-base")

        await service.evaluate_waiting_harness()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "harness_attempt_base_mismatch" in run.status_reason
        assert fake_gitlab.merge_requests == {}

    async def test_denied_path_candidate_blocks_the_run(self, service, fake_gitlab, db):
        """The publisher's policy validation rejects CI/config writes even
        when the harness produced a well-formed bundle (ADR-0016)."""
        run_id, pipeline_id, _branch = await start_and_go(service, db, fake_gitlab)
        seed_success_with_candidate(
            fake_gitlab,
            pipeline_id,
            attempt_base="base-sha-1",
            diff=create_diff(".gitlab-ci.yml", "rogue: true\n"),
        )

        await service.evaluate_waiting_harness()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "candidate_rejected" in run.status_reason
        assert "denylisted" in run.status_reason
        assert fake_gitlab.merge_requests == {}


class TestHarnessRepairInterplay:
    async def _adopt_candidate(self, service, fake_gitlab, db) -> tuple[str, int, str]:
        """Drive a harness run to waiting_ci with a published candidate."""
        run_id, pipeline_id, branch = await start_and_go(service, db, fake_gitlab)
        seed_success_with_candidate(fake_gitlab, pipeline_id, attempt_base="base-sha-1")
        await service.evaluate_waiting_harness()
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        return run_id, pipeline_id, branch

    async def test_ci_code_failure_repairs_via_new_harness_pipeline(self, service, fake_gitlab, db):
        """CI code failure with budget left re-delegates to the harness with
        the repair context in the brief (ADR-0015) — not a builtin repair."""
        run_id, _pipeline_id, branch = await self._adopt_candidate(service, fake_gitlab, db)
        candidate_sha = (await get_run(db, run_id)).candidate_shas[-1]

        failed_pipeline = (await fake_gitlab.create_pipeline(PROJECT_ID, branch))["id"]
        fake_gitlab.set_pipeline_status(failed_pipeline, "failed", candidate_sha)
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
        # original harness pipeline and the failed verification pipeline), its
        # brief (FORGE_PLAN) carries the bounded CI failure context, and its
        # FORGE_ATTEMPT_BASE is the last verified candidate (ADR-0016 §4).
        assert len(fake_gitlab.pipelines) == 3
        repair_pipeline = fake_gitlab.pipelines[-1]
        assert repair_pipeline["ref"] == branch
        by_key = {v["key"]: v["value"] for v in repair_pipeline["variables"]}
        assert "Repair context" in by_key["FORGE_PLAN"]
        assert "AssertionError" in by_key["FORGE_PLAN"]
        assert by_key["FORGE_ATTEMPT_BASE"] == candidate_sha

        # The repair delegation is journaled like the first start.
        async with db() as session:
            actions = (await session.execute(select(ActionLog))).scalars().all()
        starts = [a for a in actions if a.action_kind == "harness_start"]
        assert len(starts) == 2
        assert starts[-1].correlation_id == f"pipeline-{repair_pipeline['id']}"

    async def test_empty_repair_candidate_blocks_as_repair_no_effect(
        self, service, fake_gitlab, db
    ):
        """F20: a repair that changes nothing is blocked as no-effect, never
        adopted as a no-op cycle."""
        run_id, _pipeline_id, branch = await self._adopt_candidate(service, fake_gitlab, db)
        candidate_sha = (await get_run(db, run_id)).candidate_shas[-1]

        failed_pipeline = (await fake_gitlab.create_pipeline(PROJECT_ID, branch))["id"]
        fake_gitlab.set_pipeline_status(failed_pipeline, "failed", candidate_sha)
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
        fake_gitlab.set_job_log(999, "AssertionError")

        await service.evaluate_waiting_ci()
        repair_pipeline_id = int((await get_run(db, run_id)).evidence["harness"]["pipeline_id"])
        seed_success_with_candidate(
            fake_gitlab, repair_pipeline_id, attempt_base=candidate_sha, diff=""
        )

        await service.evaluate_waiting_harness()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "repair_no_effect" in run.status_reason

    async def test_repair_publishes_on_top_of_the_previous_candidate(
        self, service, fake_gitlab, db
    ):
        """A successful repair candidate builds on the last verified commit —
        the published sha's parent IS the previous candidate."""
        run_id, _pipeline_id, branch = await self._adopt_candidate(service, fake_gitlab, db)
        first_sha = (await get_run(db, run_id)).candidate_shas[-1]

        failed_pipeline = (await fake_gitlab.create_pipeline(PROJECT_ID, branch))["id"]
        fake_gitlab.set_pipeline_status(failed_pipeline, "failed", first_sha)
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
        fake_gitlab.set_job_log(999, "AssertionError")

        await service.evaluate_waiting_ci()
        repair_pipeline_id = int((await get_run(db, run_id)).evidence["harness"]["pipeline_id"])
        seed_success_with_candidate(
            fake_gitlab,
            repair_pipeline_id,
            attempt_base=first_sha,
            diff=create_diff("forge-demo/y.md", "repair\n"),
        )

        await service.evaluate_waiting_harness()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        assert run.commit_cycle == 2
        (second_sha,) = run.candidate_shas[-1:]
        assert second_sha != first_sha
        # The repair commit's parent is the previous candidate: the branch
        # never moved except by forge's own writes.
        commits = fake_gitlab.branches[branch]
        assert commits[0]["sha"] == second_sha
        assert commits[0]["parent_ids"] == [first_sha]

    async def test_repair_budget_exhaustion_blocks(self, service, fake_gitlab, db):
        """After FORGE_MAX_COMMIT_CYCLES repair attempts the run blocks."""
        settings = make_settings(FORGE_MAX_COMMIT_CYCLES=2)
        svc = make_service(db, fake_gitlab, settings=settings)
        run_id, _pipeline_id, branch = await self._adopt_candidate(svc, fake_gitlab, db)

        for _ in range(settings.FORGE_MAX_COMMIT_CYCLES):
            run = await get_run(db, run_id)
            if run.status != FlowStatus.WAITING_CI.value:
                break
            current_sha = run.candidate_shas[-1]
            failed_pipeline = (await fake_gitlab.create_pipeline(PROJECT_ID, branch))["id"]
            fake_gitlab.set_pipeline_status(failed_pipeline, "failed", current_sha)
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
            # The CI verdict either blocks or re-delegates to the harness.
            await svc.evaluate_waiting_ci()
            if (await get_run(db, run_id)).status != FlowStatus.WAITING_HARNESS.value:
                break
            repair_pipeline_id = int((await get_run(db, run_id)).evidence["harness"]["pipeline_id"])
            seed_success_with_candidate(
                fake_gitlab,
                repair_pipeline_id,
                attempt_base=current_sha,
                diff=create_diff("forge-demo/fix.md", f"fix for {current_sha}\n"),
            )
            await svc.evaluate_waiting_harness()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("commit_cycles_exhausted")
