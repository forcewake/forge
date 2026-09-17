"""Terminal-failure revival (forge.runs.revival): the contract tests.

Tier 1 — a transient death (dispatch 5xx/network, runner startup) schedules
its own revival and the reconciler re-dispatches the SAME branch after a
bounded backoff, with ``auto_revive`` journaled (ADR-0005). Tier 2 — an
operator ``/retry`` revives in place what Tier 1 correctly refuses to touch,
never minting a new run id or a new branch. Fatal deaths park ``blocked``
with the precise cause and no auto-retry.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import httpx
from forge.config import Settings
from forge.durable import ActionLog, FlowRun, FlowStatus, Outbox
from forge.durable.controller import Controller, InvalidTransition
from forge.durable.identity import factory_branch
from forge.models.base import Base
from forge.repository import ChangesetWriter
from forge.runs import RunService
from forge.runs.candidate import attempt_base_for
from forge.runs.revival import (
    FATAL,
    TRANSIENT,
    RETRY_RE,
    classify_dispatch_error,
    classify_failure_reason,
    revive_delay_seconds,
    revive_due,
    revival_fragment,
)
from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer
from tests.fixtures.candidate import create_diff, seed_candidate
from tests.fixtures.fake_gitlab import FakeGitLab

PROJECT_ID = 42
ISSUE_IID = 7
ISSUE_TITLE = "Add a widget"
ISSUE_DESC = "Widgets make the app better."


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


def make_service(db, fake_gitlab, settings=None) -> RunService:
    settings = settings or make_settings()
    return RunService(
        session_factory=db,
        gitlab=fake_gitlab,
        settings=settings,
        writer_class=ChangesetWriter,
        planner=StubPlanner(),
        implementer=StubImplementer(),
        reviewer=StubReviewer(),
    )


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


async def actions_of(db, run_id: str, kind: str) -> list[ActionLog]:
    async with db() as session:
        rows = (
            (
                await session.execute(
                    select(ActionLog)
                    .where(ActionLog.flow_run_id == run_id, ActionLog.action_kind == kind)
                    .order_by(ActionLog.id)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


async def start_and_go(service, db, fake_gitlab) -> tuple[str, int, str]:
    """/implement + /go → waiting_harness. Returns (run_id, pipeline_id, branch)."""
    run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
    await service.handle_command_note(
        PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
    )
    run = await get_run(db, run_id)
    assert run.status == FlowStatus.WAITING_HARNESS.value
    pipeline_id = int(run.evidence["harness"]["pipeline_id"])
    return run_id, pipeline_id, factory_branch(ISSUE_IID, run_id)


def seed_agent_job(
    fake_gitlab: FakeGitLab, pipeline_id: int, *, status: str, failure_reason=None, log=None
) -> None:
    job: dict = {"id": 555, "name": "forge-agent", "status": status}
    if failure_reason is not None:
        job["failure_reason"] = failure_reason
    fake_gitlab.set_pipeline_jobs(pipeline_id, [job])
    if log is not None:
        fake_gitlab.set_job_log(555, log)


def seed_candidate_bundle(fake_gitlab: FakeGitLab, pipeline_id: int, attempt_base: str) -> str:
    """A successful lane job with a candidate artifact; returns the new sha."""
    seed_agent_job(fake_gitlab, pipeline_id, status="success")
    seed_candidate(
        fake_gitlab,
        555,
        attempt_base=attempt_base,
        diff=create_diff("forge-demo/x.md", "hello\n"),
    )
    return "candidate-sha"


class TestFailureClassification:
    """The classification table: the same table on all three lanes."""

    @pytest.mark.parametrize(
        ("reason", "expected"),
        [
            ("harness_start_failed: GitLab API error 502: bad gateway", TRANSIENT),
            ("harness_start_failed: timed out", TRANSIENT),
            ("infrastructure_failure: pipeline 7 failed", TRANSIENT),
            (
                "harness_infrastructure: harness job forge-agent failed (runner_system_failure)",
                TRANSIENT,
            ),
            ("harness_start_failed: GitLab API error 422: Unexpected inputs", FATAL),
            ("harness_code: harness_driver_failed (exit=1)", FATAL),
            ("harness_config: harness job lint failed (config_error)", FATAL),
            ("backend_config: no lane pipeline configured", FATAL),
            ("commit_cycles_exhausted: 3 of 3 commit cycles used", FATAL),
            ("ci_timeout", FATAL),
            ("external_change", FATAL),
        ],
    )
    def test_classification_table(self, reason, expected):
        assert classify_failure_reason(reason) == expected

    def test_dispatch_4xx_is_fatal_and_5xx_is_transient(self):
        class APIError(Exception):
            def __init__(self, status):
                self.status_code = status

        assert classify_dispatch_error(APIError(422)) is FATAL
        assert classify_dispatch_error(APIError(400)) is FATAL
        assert classify_dispatch_error(APIError(502)) is TRANSIENT
        assert classify_dispatch_error(APIError(429)) is TRANSIENT
        # A bare httpx error carries no status: network, so transient.
        assert classify_dispatch_error(httpx.ConnectError("refused")) is TRANSIENT


class TestBackoff:
    def test_backoff_grows_and_is_bounded(self):
        assert revive_delay_seconds(1) == 60
        assert revive_delay_seconds(2) == 120
        assert revive_delay_seconds(9) == revive_delay_seconds(8)

    def test_fragment_stops_at_the_limit(self):
        now = datetime.now(timezone.utc)
        assert revival_fragment(2, "boom", now=now, limit=2) is None
        fragment = revival_fragment(1, "boom", now=now, limit=2)
        assert fragment is not None
        assert fragment["revive_count"] == 2
        assert fragment["revive_at"] == (
            (now + timedelta(seconds=revive_delay_seconds(2))).isoformat()
        )

    def test_reconciler_skips_until_due(self):
        now = datetime.now(timezone.utc)
        fragment = revival_fragment(0, "boom", now=now, limit=2)
        evidence = {"revive": fragment}
        assert not revive_due(evidence, now + timedelta(seconds=59))
        assert revive_due(evidence, now + timedelta(seconds=60))
        assert not revive_due({"revive": None}, now + timedelta(hours=1))
        assert not revive_due(None, now)


class TestAttemptBase:
    def test_revive_and_retry_continue_from_the_last_candidate(self):
        """A revived or /retry'd run re-dispatches on the candidate, not the
        source base — a true continuation of the same attempt (ADR-0016 §4)."""

        def run(cycle, candidates):
            return type(
                "R", (), {"commit_cycle": cycle, "candidate_shas": candidates, "base_sha": "src-1"}
            )

        assert attempt_base_for(run(1, None)) == "src-1"
        assert attempt_base_for(run(1, ["c1"])) == "c1"  # died after a candidate
        assert attempt_base_for(run(2, ["c1", "c2"])) == "c2"


class TestAutoRevive:
    async def test_transient_death_schedules_and_revives_on_the_same_branch(
        self, service, fake_gitlab, db
    ):
        run_id, pipeline_id, branch = await start_and_go(service, db, fake_gitlab)
        seed_candidate_bundle(fake_gitlab, pipeline_id, "base-sha-1")
        # Adopt the candidate → Draft MR → waiting_ci, then a runner flake.
        await service.evaluate_waiting_harness()
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        candidate_sha = run.candidate_shas[-1]
        red_pipeline = fake_gitlab._id()
        fake_gitlab.pipelines.append(
            {"id": red_pipeline, "ref": branch, "status": "failed", "sha": candidate_sha}
        )
        fake_gitlab.set_pipeline_jobs(
            red_pipeline,
            [
                {
                    "id": fake_gitlab._id(),
                    "name": "build",
                    "status": "failed",
                    "failure_reason": "runner_system_failure",
                }
            ],
        )
        await service.evaluate_waiting_ci()

        dead = await get_run(db, run_id)
        assert dead.status == FlowStatus.FAILED.value
        assert dead.status_reason.startswith("infrastructure_failure")
        scheduled = dead.evidence["revive"]
        assert scheduled["revive_count"] == 1

        # Not due yet → the reconciler skips it, the run stays parked.
        now = datetime.now(timezone.utc)
        await service.evaluate_revival(now=now + timedelta(seconds=1))
        assert (await get_run(db, run_id)).status == FlowStatus.FAILED.value
        assert len(fake_gitlab.pipelines) == 2

        # Due → re-dispatched on the SAME branch at the last candidate.
        await service.evaluate_revival(now=now + timedelta(seconds=61))
        revived = await get_run(db, run_id)
        assert revived.status == FlowStatus.WAITING_HARNESS.value
        assert len(fake_gitlab.pipelines) == 3
        redispatch = fake_gitlab.pipelines[-1]
        assert redispatch["ref"] == branch
        by_key = {v["key"]: v["value"] for v in redispatch["variables"]}
        assert by_key["FORGE_ATTEMPT_BASE"] == candidate_sha
        assert "infrastructure_failure" in by_key["FORGE_PLAN"]

        (action,) = await actions_of(db, run_id, "auto_revive")
        assert action.status == "succeeded"
        assert action.remote_result["attempt"] == 1

    async def test_revive_limit_parks_the_run_dead_for_good(self, fake_gitlab, db):
        service = make_service(
            db, fake_gitlab, settings=make_settings(FORGE_RUN_AUTO_REVIVE_LIMIT=1)
        )
        run_id, pipeline_id, _branch = await start_and_go(service, db, fake_gitlab)
        seed_agent_job(
            fake_gitlab, pipeline_id, status="failed", failure_reason="runner_system_failure"
        )
        now = datetime.now(timezone.utc)

        await service.evaluate_waiting_harness()
        assert (await get_run(db, run_id)).evidence["revive"]["revive_count"] == 1
        await service.evaluate_revival(now=now + timedelta(hours=1))
        assert len(fake_gitlab.pipelines) == 2
        redispatch_id = fake_gitlab.pipelines[-1]["id"]

        # The re-dispatch dies the same way: the limit is spent, no more revive.
        seed_agent_job(
            fake_gitlab,
            redispatch_id,
            status="failed",
            failure_reason="runner_system_failure",
        )
        await service.evaluate_waiting_harness()
        dead = await get_run(db, run_id)
        assert dead.status == FlowStatus.FAILED.value
        # The spent schedule is defused, not left due: the reconciler is done.
        assert dead.evidence["revive"]["exhausted"] is True
        await service.evaluate_revival(now=now + timedelta(hours=2))
        assert len(fake_gitlab.pipelines) == 2  # no third leg
        (revive_action,) = await actions_of(db, run_id, "auto_revive")
        assert revive_action.remote_result["attempt"] == 1

    async def test_cancelled_runs_are_never_revived(self, service, fake_gitlab, db):
        """A cancelled run — and one whose cancel is still in flight — never
        comes back: revival is for the environment, not for a human's no."""
        now = datetime.now(timezone.utc)
        fragment = revival_fragment(0, "boom", now=now - timedelta(hours=1), limit=2)
        assert fragment is not None
        async with db() as session:
            session.add_all(
                [
                    FlowRun(
                        id="c" * 32,
                        project_id=PROJECT_ID,
                        status=FlowStatus.CANCELLED.value,
                        evidence={"revive": fragment},
                    ),
                    FlowRun(
                        id="x" * 32,
                        project_id=PROJECT_ID,
                        status=FlowStatus.FAILED.value,
                        cancel_requested=True,
                        evidence={"revive": fragment},
                    ),
                ]
            )
            await session.commit()

        await service.evaluate_revival(now=now)

        for run_id in ("c" * 32, "x" * 32):
            run = await get_run(db, run_id)
            assert run.status in (FlowStatus.CANCELLED.value, FlowStatus.FAILED.value)
        assert fake_gitlab.pipelines == []


class TestRevivalGraphEdge:
    async def test_ordinary_transition_out_of_terminal_is_refused(self, db):
        async with db() as session:
            run = FlowRun(id="r" * 32, project_id=1, status=FlowStatus.FAILED.value)
            session.add(run)
            await session.commit()
            controller = Controller(session)
            with pytest.raises(InvalidTransition):
                await controller.transition(run.id, FlowStatus.PROPOSING, reason="nope")

    async def test_authorized_revive_walks_failed_onto_proposing(self, db):
        async with db() as session:
            run = FlowRun(id="r" * 32, project_id=1, status=FlowStatus.FAILED.value)
            session.add(run)
            await session.commit()
            controller = Controller(session)
            revived = await controller.revive(
                run.id, reason="operator asked", authorized_by="operator:@alice"
            )
            assert revived.status == FlowStatus.PROPOSING.value
            await session.commit()

        async with db() as session:
            rows = (
                (
                    await session.execute(
                        select(Outbox).where(Outbox.flow_run_id == run.id).order_by(Outbox.id)
                    )
                )
                .scalars()
                .all()
            )
        outbox = rows[-1]
        async with db() as session:
            assert outbox.payload["from"] == "failed"
            assert outbox.payload["to"] == "proposing"
            assert outbox.payload["authorized_by"] == "operator:@alice"

    async def test_cancelled_is_not_revivable(self, db):
        async with db() as session:
            run = FlowRun(id="r" * 32, project_id=1, status=FlowStatus.CANCELLED.value)
            session.add(run)
            await session.commit()
            with pytest.raises(InvalidTransition):
                await Controller(session).revive(
                    run.id, reason="nope", authorized_by="operator:@alice"
                )


class TestRetryCommand:
    async def test_retry_walks_a_blocked_run_back_onto_its_branch(self, fake_gitlab, db):
        # A required job the lane never runs = a quality-contract block: fatal.
        service = make_service(
            db, fake_gitlab, settings=make_settings(FORGE_REQUIRED_JOBS="pytest")
        )
        run_id, pipeline_id, branch = await start_and_go(service, db, fake_gitlab)
        seed_candidate_bundle(fake_gitlab, pipeline_id, "base-sha-1")
        await service.evaluate_waiting_harness()
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        candidate_sha = run.candidate_shas[-1]
        green_pipeline = fake_gitlab._id()
        fake_gitlab.pipelines.append(
            {"id": green_pipeline, "ref": branch, "status": "success", "sha": candidate_sha}
        )
        fake_gitlab.set_pipeline_jobs(green_pipeline, [])
        await service.evaluate_waiting_ci()
        dead = await get_run(db, run_id)
        assert dead.status == FlowStatus.BLOCKED.value
        assert "quality_contract" in dead.status_reason
        assert "revive" not in dead.evidence  # fatal: no auto-retry

        await service.handle_retry_note(
            project_id=PROJECT_ID,
            issue_iid=ISSUE_IID,
            note_text=f"@forge /retry {run_id}",
            author_username="alice",
        )

        retried = await get_run(db, run_id)
        assert retried.status == FlowStatus.WAITING_HARNESS.value
        assert retried.commit_cycle == 2  # one operator-granted cycle
        assert len(fake_gitlab.pipelines) == 3
        redispatch = fake_gitlab.pipelines[-1]
        assert redispatch["ref"] == branch  # same branch, never a new one
        by_key = {v["key"]: v["value"] for v in redispatch["variables"]}
        assert by_key["FORGE_ATTEMPT_BASE"] == candidate_sha
        assert "quality_contract" in by_key["FORGE_PLAN"]  # terminal reason rides along
        (action,) = await actions_of(db, run_id, "retry_requested")
        assert action.status == "succeeded"
        assert action.remote_result["cycle"] == 2
        assert action.remote_result["by"] == "alice"

    async def test_retry_grants_a_cycle_past_the_configured_max(self, fake_gitlab, db):
        service = make_service(
            db,
            fake_gitlab,
            settings=make_settings(FORGE_MAX_COMMIT_CYCLES=1, FORGE_REQUIRED_JOBS="pytest"),
        )
        run_id, pipeline_id, branch = await start_and_go(service, db, fake_gitlab)
        seed_candidate_bundle(fake_gitlab, pipeline_id, "base-sha-1")
        await service.evaluate_waiting_harness()
        run = await get_run(db, run_id)
        green_pipeline = fake_gitlab._id()
        fake_gitlab.pipelines.append(
            {
                "id": green_pipeline,
                "ref": branch,
                "status": "success",
                "sha": run.candidate_shas[-1],
            }
        )
        await service.evaluate_waiting_ci()  # a quality-contract block: fatal
        assert (await get_run(db, run_id)).status == FlowStatus.BLOCKED.value

        await service.handle_retry_note(
            project_id=PROJECT_ID,
            issue_iid=ISSUE_IID,
            note_text="@forge /retry",
            author_username="alice",
        )
        retried = await get_run(db, run_id)
        assert retried.status == FlowStatus.WAITING_HARNESS.value
        assert retried.commit_cycle == 2  # beyond FORGE_MAX_COMMIT_CYCLES=1

    async def test_retry_without_a_candidate_suggests_implement(self, service, fake_gitlab, db):
        run_id, _pipeline_id, _branch = await start_and_go(service, db, fake_gitlab)
        seed_agent_job(
            fake_gitlab, _pipeline_id, status="failed", failure_reason="runner_system_failure"
        )
        await service.evaluate_waiting_harness()
        assert (await get_run(db, run_id)).status == FlowStatus.FAILED.value

        await service.handle_retry_note(
            project_id=PROJECT_ID,
            issue_iid=ISSUE_IID,
            note_text=f"@forge /retry {run_id}",
            author_username="alice",
        )

        assert (await get_run(db, run_id)).status == FlowStatus.FAILED.value
        assert len(fake_gitlab.pipelines) == 1  # nothing re-dispatched
        (note,) = fake_gitlab.notes_containing("/implement")
        assert "never recorded a candidate" in note["body"]
        assert not await actions_of(db, run_id, "retry_requested")

    async def test_retry_refuses_a_non_terminal_run(self, service, fake_gitlab, db):
        run_id, _pipeline_id, _branch = await start_and_go(service, db, fake_gitlab)

        await service.handle_retry_note(
            project_id=PROJECT_ID,
            issue_iid=ISSUE_IID,
            note_text=f"@forge /retry {run_id}",
            author_username="alice",
        )

        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_HARNESS.value
        notes = fake_gitlab.notes_containing("only revives")
        assert len(notes) == 1
        assert "waiting_harness" in notes[0]["body"]

    async def test_retry_from_a_non_approver_is_ignored(self, service, fake_gitlab, db):
        run_id, pipeline_id, _branch = await start_and_go(service, db, fake_gitlab)
        seed_agent_job(
            fake_gitlab, pipeline_id, status="failed", failure_reason="runner_system_failure"
        )
        await service.evaluate_waiting_harness()

        await service.handle_retry_note(
            project_id=PROJECT_ID,
            issue_iid=ISSUE_IID,
            note_text=f"@forge /retry {run_id}",
            author_username="mallory",
        )

        assert (await get_run(db, run_id)).status == FlowStatus.FAILED.value
        assert fake_gitlab.notes_containing("/implement") == []

    def test_retry_regex_accepts_a_bare_and_an_id_form(self):
        assert RETRY_RE.search("/retry") is not None
        assert RETRY_RE.search("@forge /retry 0a1b2c3d").group(1) == "0a1b2c3d"
        assert RETRY_RE.search("/implement") is None


class TestLaneParity:
    """Same classification, same limits — every lane rides the same mixin."""

    def test_all_three_services_share_the_revival_mixin(self):
        from forge.runs.azure_service import AzureRunService
        from forge.runs.github_service import GitHubRunService
        from forge.runs.revival import RevivalMixin

        for service in (RunService, GitHubRunService, AzureRunService):
            assert issubclass(service, RevivalMixin)

    def test_the_limit_is_one_setting(self):
        from types import SimpleNamespace

        from forge.runs.revival import RevivalMixin

        assert make_settings().FORGE_RUN_AUTO_REVIVE_LIMIT == 2  # the default
        assert (
            RevivalMixin._revival_limit(
                SimpleNamespace(_settings=make_settings(FORGE_RUN_AUTO_REVIVE_LIMIT=5))
            )
            == 5
        )
