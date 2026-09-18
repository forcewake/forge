"""R17 liveness: deadlines fire before provider I/O, discovery and retries are
bounded, and terminal states are never revived by a late callback.

Found live: the worker stalled for hours on discovery 401s and frozen polls
while a run's ``harness_timeout`` never fired — the deadline check lived on
the same stalled path as the provider I/O. Every test here pins the invariant
that broke: the LOCAL deadline/cancel/attempts evaluation happens FIRST, with
no provider call once the budget is spent.
"""

import dataclasses
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import ForgeConfig, Settings
from forge.durable import FlowRun, FlowStatus, StepRun
from forge.execution.github_actions import ActionsHandle, artifact_name_for
from forge.integrations.github import GitHubAPIError
from forge.integrations.github_flow import GitHubAgents, GitHubPublishFlow
from forge.models.base import Base
from forge.runs.github_service import GitHubRunService
from forge.runs.stubs import StubImplementer, StubPlanner
from forge.worker.steps import (
    claim_due_steps,
    reap_deadline_exceeded,
    reschedule_expired_leases,
    schedule_command_step,
)
from tests.fixtures.candidate import create_diff
from tests.fixtures.fake_github import FakeGitHub

REPO = "acme/acme-widget"
OWNER, REPO_NAME = REPO.split("/", 1)
PROJECT_ID = 70010
ISSUE = 42
ISSUE_TITLE = "Add password reset"
ISSUE_DESC = "Users cannot reset their password."
BASE_HEAD = "1" * 40
WORKFLOW = "forge-harness.github.yml"
MODEL = "glm-5.3-flash[1m]"
CANDIDATE_PATH = "forge-demo/implemented.md"
CANDIDATE_CONTENT = "# implemented by the harness\n"


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        FORGE_APPROVERS="alice",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        FORGE_HARNESS_MODEL=MODEL,
        FORGE_GITHUB_HARNESS_WORKFLOW=WORKFLOW,
        FORGE_VERIFICATION_GRACE_SECONDS=0,  # hermetic: grace needs a sleep
    )
    values.update(overrides)
    return Settings(**values)


def make_stack(fake: FakeGitHub) -> GitHubAgents:
    from forge.factory.reviewer import ReviewVerdict

    class StubPRReviewer:
        async def review(self, **kwargs) -> ReviewVerdict:
            return ReviewVerdict(verdict="ok", summary="clean", findings=())

    flow = GitHubPublishFlow(fake, proposer=StubImplementer(), base_branch="main")
    return GitHubAgents(
        client=fake,
        reader=fake,
        planner=StubPlanner(),
        implementer=StubImplementer(),
        reviewer=StubPRReviewer(),
        flow=flow,
    )


def make_service(
    db,
    fake: FakeGitHub,
    *,
    settings: Settings | None = None,
) -> GitHubRunService:
    return GitHubRunService(
        db,
        settings or make_settings(),
        ForgeConfig(),
        stack=make_stack(fake),
        repo_full_name=REPO,
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
def fake() -> FakeGitHub:
    github = FakeGitHub()
    github.seed_repo(REPO, {"src/app.py": "print('hi')\n"})
    github.heads[REPO]["main"] = BASE_HEAD
    github.seed_issue(REPO, ISSUE, ISSUE_TITLE, ISSUE_DESC)
    return github


async def get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


async def start(service: GitHubRunService) -> str:
    return await service.start_run(
        project_id=PROJECT_ID,
        issue_number=ISSUE,
        issue_title=ISSUE_TITLE,
        issue_description=ISSUE_DESC,
        author_username="alice",
    )


async def go(service: GitHubRunService, run_id: str) -> None:
    await service.handle_go(
        project_id=PROJECT_ID,
        issue_number=ISSUE,
        note_text=f"@forge /go {run_id}",
        author_username="alice",
    )


def seed_success_with_candidate(fake: FakeGitHub, run_id: str, actions_run_id: int = 501) -> None:
    """The workflow finished and uploaded the candidate artifact."""
    for run in fake.actions_runs:
        if run["id"] == actions_run_id:
            run.update(status="completed", conclusion="success")
    fake.seed_candidate_artifact(
        actions_run_id,
        name=artifact_name_for(run_id),
        diff_text=create_diff(CANDIDATE_PATH, CANDIDATE_CONTENT),
        meta={"attempt_base": BASE_HEAD, "run_id": run_id, "exit": "completed"},
    )


async def age_journaled_handle(db, run_id: str, *, seconds: int) -> None:
    """Rewind the journaled handle's started_at — the deadline rides on it."""
    async with db() as session:
        run = await session.get(FlowRun, run_id)
        harness = dict(run.evidence["harness"])
        handle = ActionsHandle.from_json(harness["handle"])
        aged = dataclasses.replace(
            handle,
            started_at=(datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(),
        )
        harness["handle"] = aged.to_json()
        run.evidence = {**run.evidence, "harness": harness}
        await session.commit()


# ----------------------------------------------------------------------
# 1. Deadline BEFORE provider I/O (the harness lane)
# ----------------------------------------------------------------------


class TestHarnessDeadlineBeforeIO:
    async def test_expired_deadline_blocks_without_any_provider_call(self, db, fake):
        """The journaled deadline passed: the run parks blocked on
        harness_timeout even though the Actions API errors on every call —
        and the failing API is never asked (poll is not reached)."""
        service = make_service(db, fake, settings=make_settings(FORGE_HARNESS_TIMEOUT_SECONDS=600))
        run_id = await start(service)
        await go(service, run_id)
        await age_journaled_handle(db, run_id, seconds=601)

        async def always_401(owner, repo, actions_run_id):
            raise GitHubAPIError(401, "Bad credentials")

        fake.get_workflow_run = always_401  # type: ignore[method-assign]
        discovery_calls_before = len(fake.calls_of("list_workflow_dispatch_runs"))

        await service.evaluate_waiting_harness()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "harness_timeout" in (run.status_reason or "")
        # The provider was NEVER called: no poll, no discovery after expiry.
        assert fake.calls_of("get_workflow_run") == []
        assert len(fake.calls_of("list_workflow_dispatch_runs")) == discovery_calls_before

    async def test_poll_failure_cannot_extend_the_deadline(self, db, fake):
        """A poll error keeps the run waiting only until the journaled
        deadline — the next evaluation's deadline check fires without I/O."""
        service = make_service(db, fake, settings=make_settings(FORGE_HARNESS_TIMEOUT_SECONDS=600))
        run_id = await start(service)
        await go(service, run_id)

        async def always_401(owner, repo, actions_run_id):
            raise GitHubAPIError(401, "Bad credentials")

        fake.get_workflow_run = always_401  # type: ignore[method-assign]

        # While the deadline is ahead, the poll error just keeps it waiting.
        await service.evaluate_waiting_harness()
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_HARNESS.value

        # Once it is behind, the deadline fires — no provider call needed.
        await age_journaled_handle(db, run_id, seconds=601)
        calls_before_expiry = len(fake.calls_of("get_workflow_run"))
        await service.evaluate_waiting_harness()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "harness_timeout" in (run.status_reason or "")
        assert len(fake.calls_of("get_workflow_run")) == calls_before_expiry


# ----------------------------------------------------------------------
# 2. Deadline BEFORE provider I/O (the waiting_ci verification lane)
# ----------------------------------------------------------------------


class TestVerificationDeadlineBeforeIO:
    async def _waiting_ci_run(self, db, fake, settings) -> str:
        """A run parked at waiting_ci by the builtin publish leg."""
        service = make_service(db, fake, settings=settings)
        run_id = await start(service)
        await go(service, run_id)
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_CI.value
        return run_id

    async def test_verification_timeout_fires_without_checks_call(self, db, fake):
        """The checks API has been dead the whole wait: the deadline is
        evaluated locally and blocks without asking the provider again."""
        settings = make_settings(
            FORGE_GITHUB_HARNESS_WORKFLOW="",
            FORGE_VERIFICATION_TIMEOUT_SECONDS=1800,
        )
        run_id = await self._waiting_ci_run(db, fake, settings)
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.updated_at = datetime.now(timezone.utc) - timedelta(seconds=1801)
            await session.commit()

        checks_calls: list[str] = []

        async def checks_api_down(owner, repo, sha, workflow_name=None):
            checks_calls.append(sha)
            raise GitHubAPIError(401, "Bad credentials")

        fake.list_workflow_runs_for_sha = checks_api_down  # type: ignore[method-assign]
        service = make_service(db, fake, settings=settings)

        await service.evaluate_waiting_ci_one(run_id, now=datetime.now(timezone.utc))

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "verification_timeout" in (run.status_reason or "")
        assert checks_calls == []

    async def test_verification_inside_the_deadline_still_polls(self, db, fake):
        """Control: within the budget the checks API is asked (and a
        transient error keeps the run waiting, not blocked)."""
        settings = make_settings(
            FORGE_GITHUB_HARNESS_WORKFLOW="",
            FORGE_VERIFICATION_TIMEOUT_SECONDS=1800,
        )
        run_id = await self._waiting_ci_run(db, fake, settings)
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.updated_at = datetime.now(timezone.utc) - timedelta(seconds=60)
            await session.commit()

        checks_calls: list[str] = []

        async def checks_api_down(owner, repo, sha, workflow_name=None):
            checks_calls.append(sha)
            raise GitHubAPIError(503, "unavailable")

        fake.list_workflow_runs_for_sha = checks_api_down  # type: ignore[method-assign]
        service = make_service(db, fake, settings=settings)

        await service.evaluate_waiting_ci_one(run_id, now=datetime.now(timezone.utc))

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        assert checks_calls


# ----------------------------------------------------------------------
# 3. Bounded discovery: a dispatch that never surfaces parks blocked
# ----------------------------------------------------------------------


class TestBoundedDiscovery:
    async def test_unknown_dispatch_blocks_after_the_attempt_cap(self, db, fake):
        """Legacy empty-202, the Actions run NEVER appears: discovery retries
        exactly up to FORGE_HARNESS_DISCOVERY_MAX_ATTEMPTS, then the run
        parks blocked with the precise "dispatch never observed" reason."""
        fake.dispatch_mode = "legacy"  # dispatch response carries no run id
        settings = make_settings(FORGE_HARNESS_DISCOVERY_MAX_ATTEMPTS=3)
        service = make_service(db, fake, settings=settings)
        run_id = await start(service)
        await go(service, run_id)
        assert (await get_run(db, run_id)).evidence["harness"]["run_id"] is None
        # The launch itself probed once (launch-time discovery); the cap
        # bounds the RECONCILER's retries on top of that.
        discovery_at_dispatch = len(fake.calls_of("list_workflow_dispatch_runs"))

        for _ in range(3):
            await service.evaluate_waiting_harness()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "dispatch never observed" in (run.status_reason or "")
        assert "harness_infrastructure" in (run.status_reason or "")
        assert len(fake.calls_of("list_workflow_dispatch_runs")) == discovery_at_dispatch + 3

        # The blocked run is out of the reconciler's set — no further calls.
        await service.evaluate_waiting_harness()
        assert len(fake.calls_of("list_workflow_dispatch_runs")) == discovery_at_dispatch + 3

    async def test_a_late_surfacing_run_still_completes_within_the_cap(self, db, fake):
        """Discovery is bounded but not eager: while attempts remain, a run
        that surfaces later is adopted and the pipeline completes."""
        fake.dispatch_mode = "legacy"
        settings = make_settings(FORGE_HARNESS_DISCOVERY_MAX_ATTEMPTS=3)
        service = make_service(db, fake, settings=settings)
        run_id = await start(service)
        await go(service, run_id)

        await service.evaluate_waiting_harness()  # attempt 1: not there yet
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_HARNESS.value

        # The run shows up in Actions afterwards (delayed visibility).
        fake.seed_actions_run(
            run_id=77,
            head_branch=f"forge/{ISSUE}/{run_id[:8]}",
            head_sha=BASE_HEAD,
            status="completed",
            conclusion="success",
            created_at=datetime.now(timezone.utc),
        )
        fake.seed_candidate_artifact(
            77,
            name=artifact_name_for(run_id),
            diff_text=create_diff(CANDIDATE_PATH, CANDIDATE_CONTENT),
            meta={"attempt_base": BASE_HEAD, "run_id": run_id, "exit": "completed"},
        )
        await service.evaluate_waiting_harness()  # attempt 2: found

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        assert run.evidence["harness"]["run_id"] == 77


# ----------------------------------------------------------------------
# 4. The step reaper: exhausted attempts and passed deadlines park dead
# ----------------------------------------------------------------------


async def _scheduled_step(db, *, max_attempts: int = 3) -> tuple[int, str]:
    """One scheduled command step; returns (step id, source_event_id)."""
    source_event_id = uuid4().hex
    async with db() as session:
        async with session.begin():
            # Distinct (project_id, issue_iid): the partial unique index
            # admits one active run per issue.
            session.add(
                FlowRun(
                    id=uuid4().hex,
                    project_id=uuid4().int % 1_000_000,
                    issue_iid=1,
                )
            )
            step = await schedule_command_step(
                session,
                {"command": "start_run", "project_id": 1},
                source_event_id=source_event_id,
                max_attempts=max_attempts,
            )
            return step.id, source_event_id


async def _expire_lease(db, step_id: int) -> None:
    async with db() as session:
        async with session.begin():
            await session.execute(
                update(StepRun)
                .where(StepRun.id == step_id)
                .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
            )


async def _claim_step(db, source_event_id: str, owner: str):
    claimed = await claim_due_steps(db, owner, source_event_id=source_event_id)
    assert len(claimed) == 1
    return claimed[0]


class TestReaperParksDeadSteps:
    async def test_reaped_attempt_that_exhausts_the_budget_parks_dead(self, db):
        """A step whose worker dies before its heartbeat loops claim → crash
        → reap forever unless the REAPER's own attempt bump can park it
        dead. With max_attempts=2 the second lease reaping kills it."""
        step_id, seid = await _scheduled_step(db, max_attempts=2)

        assert await _claim_step(db, seid, "worker-a")
        await _expire_lease(db, step_id)
        assert await reschedule_expired_leases(db) == 1  # attempt 1: still alive

        reclaimed = await _claim_step(db, seid, "worker-b")
        assert reclaimed.attempt == 1
        await _expire_lease(db, step_id)
        assert await reschedule_expired_leases(db) == 1  # attempt 2: exhausted

        async with db() as session:
            step = await session.get(StepRun, step_id)
        assert step.status == "dead"
        assert step.attempt == 2
        assert step.finished_at is not None
        assert "lease_expired" in (step.output or {}).get("error", "")
        assert await claim_due_steps(db, "worker-c", source_event_id=seid) == []

    async def test_reaper_preserves_a_worker_recorded_error_on_dead(self, db):
        """When the dying worker DID record its error, the reaper keeps it —
        the reason is preserved, not overwritten by the reap note."""
        from forge.worker.steps import fail_step

        step_id, seid = await _scheduled_step(db, max_attempts=2)
        claimed = await _claim_step(db, seid, "worker-a")
        # The worker failed once (error recorded), the retry then died mid-
        # flight and its lease expired at max attempts.
        assert await fail_step(db, claimed, "gitlab 502") == "retry"
        async with db() as session:
            async with session.begin():
                await session.execute(
                    update(StepRun)
                    .where(StepRun.id == step_id)
                    .values(due_at=datetime.now(timezone.utc))  # skip the backoff
                )
        reclaimed = await _claim_step(db, seid, "worker-a")
        assert reclaimed.attempt == 1
        await _expire_lease(db, step_id)

        assert await reschedule_expired_leases(db) == 1

        async with db() as session:
            step = await session.get(StepRun, step_id)
        assert step.status == "dead"
        assert "gitlab 502" in (step.output or {}).get("error", "")

    async def test_passed_deadline_parks_the_step_dead(self, db):
        """deadline_at is the schedule-to-close budget: once it passes, no
        retry can fit — scheduled AND running steps park dead, terminal ones
        are untouched, and an in-flight owner's completion is fenced out."""
        from forge.worker.steps import complete_step

        past_step, past_seid = await _scheduled_step(db)
        running_step, running_seid = await _scheduled_step(db)
        future_step, _ = await _scheduled_step(db)
        done_step, done_seid = await _scheduled_step(db)
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        async with db() as session:
            async with session.begin():
                await session.execute(
                    update(StepRun)
                    .where(StepRun.id.in_([past_step, running_step, done_step]))
                    .values(deadline_at=past)
                )
                await session.execute(
                    update(StepRun).where(StepRun.id == future_step).values(deadline_at=future)
                )

        claimed = await _claim_step(db, running_seid, "worker-a")
        done_claim = await _claim_step(db, done_seid, "worker-b")
        assert await complete_step(db, done_claim) is True
        # claimed's step (running_step) is now mid-flight when its deadline
        # passes — exactly the zombie-owner shape the reaper must bound.

        assert await reap_deadline_exceeded(db) == 2

        async with db() as session:
            rows = {
                step.id: step
                for step in (
                    (
                        await session.execute(
                            select(StepRun).where(
                                StepRun.id.in_([past_step, running_step, future_step, done_step])
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            }
        assert rows[past_step].status == "dead"
        assert rows[running_step].status == "dead"
        assert "deadline_exceeded" in (rows[running_step].output or {}).get("error", "")
        assert rows[future_step].status == "scheduled", "future budget is untouched"
        assert rows[done_step].status == "succeeded", "terminal steps are never reaped"

        # The reaped running step's zombie owner cannot commit its result.
        assert await complete_step(db, claimed) is False

    async def test_reaper_loop_survives_and_uses_both_reaps(self, db):
        """The wired loop runs both reapers every pass and never raises on an
        empty database."""
        import asyncio

        from forge.worker.steps import run_step_reaper

        shutdown = asyncio.Event()

        async def stop_soon() -> None:
            await asyncio.sleep(0.05)
            shutdown.set()

        stopper = asyncio.create_task(stop_soon())
        await asyncio.wait_for(run_step_reaper(db, shutdown, interval=0.01), timeout=5.0)
        await stopper
        assert await reap_deadline_exceeded(db) == 0


# ----------------------------------------------------------------------
# 4b. A05 liveness: a stale claim never waits for the reaper
# ----------------------------------------------------------------------


class TestStaleClaimNeverRotates:
    async def test_requeued_stale_claim_leaves_nothing_for_the_reaper(self, db, monkeypatch):
        """A claim whose lease died while queued is requeued at handler-entry
        time (A05 fresh-entry validation) — the reaper finds NOTHING to reap
        and the step is immediately claimable by the next worker."""
        from forge.worker.steps import execute_claimed_step

        async def never(settings, forge_config, session_factory, metadata):
            raise AssertionError("handler must not run on a stale claim")

        monkeypatch.setattr("forge.worker.steps.execute_run_command", never)

        step_id, seid = await _scheduled_step(db)
        claimed = await _claim_step(db, seid, "worker-a")
        await _expire_lease(db, step_id)

        await execute_claimed_step(db, object(), object(), claimed)

        assert await reschedule_expired_leases(db) == 0, "nothing lingered for the reaper"
        reclaimed = await claim_due_steps(db, "worker-b", source_event_id=seid)
        assert [s.id for s in reclaimed] == [step_id]


# ----------------------------------------------------------------------
# 5. A terminal run is never revived by a late callback
# ----------------------------------------------------------------------


class TestTerminalRunNeverRevived:
    @pytest.mark.parametrize(
        ("terminal_status", "expected_reason"),
        [
            (FlowStatus.CANCELLED.value, "cancelled"),
            (FlowStatus.FAILED.value, "run already failed"),
            (FlowStatus.BLOCKED.value, "run already blocked"),
            (FlowStatus.READY_FOR_HUMAN.value, "run already ready_for_human"),
        ],
    )
    async def test_late_candidate_for_terminal_run_is_superseded(
        self, db, fake, terminal_status, expected_reason
    ):
        """The candidate completes AFTER the run went terminal (cancel,
        failure, or even ready): the late result is recorded as superseded
        evidence and nothing is published — the run is never revived, never
        re-ready'd."""
        service = make_service(db, fake)
        run_id = await start(service)
        await go(service, run_id)
        seed_success_with_candidate(fake, run_id)

        original = fake.get_workflow_run

        async def terminalize_mid_poll(owner, repo, actions_run_id):
            answer = await original(owner, repo, actions_run_id)
            async with db() as session:
                run = await session.get(FlowRun, run_id)
                run.status = terminal_status  # the run dies mid-evaluation
                run.status_reason = "operator intervention"
                await session.commit()
            return answer

        fake.get_workflow_run = terminalize_mid_poll  # type: ignore[method-assign]

        await service.evaluate_waiting_harness()

        run = await get_run(db, run_id)
        assert run.status == terminal_status, "terminal state is never revived"
        assert run.evidence["superseded"] == {
            "reason": expected_reason,
            "attempt_base": BASE_HEAD,
        }
        assert fake.calls_of("create_commit_on_branch") == []
        assert fake.calls_of("create_draft_pr") == []
