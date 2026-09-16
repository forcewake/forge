"""GitHub Actions harness lane tests (E3b, ADR-0020).

The /go decision on a repo onboarded for harness execution
(``FORGE_GITHUB_HARNESS_WORKFLOW``): dispatch → ``waiting_harness`` with a
journaled Actions handle → reconciler poll → trusted publisher → Draft PR →
review → ``ready_for_human`` — plus supersede-on-cancel and failure
classification and the worker-side reconciler tick, all over
:class:`tests.fixtures.fake_github.FakeGitHub`.
"""

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import ForgeConfig, Settings
from forge.durable import FlowRun, FlowStatus, GateApproval, Outbox, RunSpec
from forge.execution.github_actions import ActionsHandle, artifact_name_for
from forge.factory.reviewer import ReviewVerdict
from forge.integrations.github import GitHubAPIError
from forge.integrations.github_flow import GitHubAgents, GitHubPublishFlow
from forge.models.base import Base
from forge.runs.github_service import (
    GitHubRunService,
    evaluate_github_waiting_harness,
    run_github_harness_reconciler,
)
from forge.runs.stubs import StubImplementer, StubPlanner
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
    )
    values.update(overrides)
    return Settings(**values)


class StubPRReviewer:
    """Deterministic PR verdict — the GitHub-shaped review surface."""

    def __init__(self, verdict: str = "ok", summary: str = "clean implementation") -> None:
        self.verdict = verdict
        self.summary = summary
        self.calls: list[dict] = []

    async def review(self, **kwargs) -> ReviewVerdict:
        self.calls.append(kwargs)
        return ReviewVerdict(verdict=self.verdict, summary=self.summary, findings=())


def make_stack(fake: FakeGitHub, *, reviewer: StubPRReviewer | None = None) -> GitHubAgents:
    reviewer = reviewer or StubPRReviewer()
    flow = GitHubPublishFlow(fake, proposer=StubImplementer(), base_branch="main")
    return GitHubAgents(
        client=fake,
        reader=fake,
        planner=StubPlanner(),
        implementer=StubImplementer(),
        reviewer=reviewer,
        flow=flow,
    )


def make_service(
    db,
    fake: FakeGitHub,
    *,
    settings: Settings | None = None,
    stack: GitHubAgents | None = None,
) -> GitHubRunService:
    return GitHubRunService(
        db,
        settings or make_settings(),
        ForgeConfig(),
        stack=stack or make_stack(fake),
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


async def start(service: GitHubRunService, author: str = "alice") -> str:
    return await service.start_run(
        project_id=PROJECT_ID,
        issue_number=ISSUE,
        issue_title=ISSUE_TITLE,
        issue_description=ISSUE_DESC,
        author_username=author,
    )


async def go(service: GitHubRunService, run_id: str, author: str = "alice") -> None:
    await service.handle_go(
        project_id=PROJECT_ID,
        issue_number=ISSUE,
        note_text=f"@forge /go {run_id}",
        author_username=author,
    )


def comments(fake: FakeGitHub) -> list[str]:
    return [call[1][3] for call in fake.calls_of("create_issue_comment")]


def clear_comments(fake: FakeGitHub) -> None:
    fake.calls[:] = [call for call in fake.calls if call[0] != "create_issue_comment"]


def seed_success_with_candidate(
    fake: FakeGitHub,
    run_id: str,
    *,
    actions_run_id: int = 501,
    branch: str | None = None,
) -> None:
    """The workflow finished and uploaded the candidate artifact."""
    branch = branch or f"forge/{ISSUE}/{run_id[:8]}"
    for run in fake.actions_runs:
        if run["id"] == actions_run_id:
            run.update(status="completed", conclusion="success")
    fake.seed_candidate_artifact(
        actions_run_id,
        name=artifact_name_for(run_id),
        diff_text=create_diff(CANDIDATE_PATH, CANDIDATE_CONTENT),
        meta={
            "attempt_base": BASE_HEAD,
            "run_id": run_id,
            "driver": "claude-code",
            "model": MODEL,
            "exit": "completed",
            "usage": {"input_tokens": 500, "output_tokens": 200},
        },
    )


# ----------------------------------------------------------------------
# /go on a harness-onboarded repo: dispatch, park, journal
# ----------------------------------------------------------------------


class TestGoDispatchesHarness:
    async def test_go_dispatches_and_parks_in_waiting_harness(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)
        clear_comments(fake)

        await go(service, run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value

        # The factory branch was cut at the FROZEN attempt base; the
        # harness — not the builtin proposer — owns the next move.
        branch = f"forge/{ISSUE}/{run_id[:8]}"
        assert fake.calls_of("create_branch") == [
            ("create_branch", (OWNER, REPO_NAME, branch, BASE_HEAD))
        ]
        (dispatch,) = fake.dispatch_inputs
        assert dispatch["workflow"] == WORKFLOW
        assert dispatch["ref"] == branch
        assert dispatch["inputs"] == {
            "run_id": run_id,
            "attempt_base_oid": BASE_HEAD,
            "driver": "claude-code",
            "model": MODEL,
            # The brief TEXT never travels in dispatch inputs — the lane
            # fetches the forge plan comment read-only; it needs the number.
            "issue_number": str(ISSUE),
        }
        assert fake.calls_of("create_commit_on_branch") == []
        assert fake.calls_of("create_draft_pr") == []

        # The journaled Actions handle survives restarts (ADR-0005).
        harness = run.evidence["harness"]
        assert run.evidence["backend"] == "ci_harness"
        assert harness["workflow"] == WORKFLOW
        assert harness["run_id"] == 501  # the dispatch-returned run id
        assert harness["attempt_base"] == BASE_HEAD
        assert harness["started_at"]
        handle = ActionsHandle.from_json(harness["handle"])
        assert handle.run_id == 501
        assert handle.forge_run_id == run_id
        assert handle.branch == branch

    async def test_run_spec_freezes_the_harness_profile(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)

        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
        assert spec.document["backend_config"] == {
            "backend": "ci_harness",
            "model": MODEL,
            "target_branch": "main",
            # ADR-0023: the frozen harness decision — default preference ⇒
            # the configured driver alone, empty fallback tail.
            "harness": "claude-code",
            "harness_fallbacks": [],
            "budget_class": "standard",
            "selection_reason": "default",
            "harness_workflow": WORKFLOW,
            "driver": "claude-code",
        }

    async def test_builtin_lane_is_the_default_when_unset(self, db, fake):
        service = make_service(db, fake, settings=make_settings(FORGE_GITHUB_HARNESS_WORKFLOW=""))
        run_id = await start(service)
        clear_comments(fake)

        await go(service, run_id)

        # R02: the builtin publish parks at waiting_ci like every lane —
        # no dispatch happened, and the verification pass (no CI configured
        # → unverified) takes it the rest of the way.
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        assert fake.dispatch_inputs == []  # no dispatch on the builtin lane
        assert fake.calls_of("create_commit_on_branch") != []

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value

    async def test_dispatch_failure_fails_the_run_without_a_second_dispatch(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)
        clear_comments(fake)

        dispatch_calls: list[dict] = []

        async def boom(owner, repo, workflow_filename, ref, inputs=None):
            dispatch_calls.append({"workflow": workflow_filename, "ref": ref})
            raise GitHubAPIError(422, "workflow does not have 'workflow_dispatch'")

        fake.dispatch_workflow = boom  # type: ignore[method-assign]

        await go(service, run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.FAILED.value
        assert "harness_start_failed" in (run.status_reason or "")
        assert len(dispatch_calls) == 1  # never replayed


# ----------------------------------------------------------------------
# Reconciler: waiting_harness → publish → ready_for_human
# ----------------------------------------------------------------------


class TestReconcile:
    async def test_completed_run_publishes_candidate_and_reaches_ready(self, db, fake):
        reviewer = StubPRReviewer()
        service = make_service(db, fake, stack=make_stack(fake, reviewer=reviewer))
        run_id = await start(service)
        await go(service, run_id)
        seed_success_with_candidate(fake, run_id)
        clear_comments(fake)

        await service.evaluate_waiting_harness()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value  # R02: parked for checks

        await service.evaluate_waiting_ci_one(run_id)  # no CI → unverified

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value

        # The SAME trusted publisher wrote the candidate: branch-CAS commit
        # pinned to the attempt base, Draft PR, evidence comment.
        branch = f"forge/{ISSUE}/{run_id[:8]}"
        (commit,) = fake.calls_of("create_commit_on_branch")
        assert commit[1][2] == branch
        assert commit[1][3] == (CANDIDATE_PATH,)  # the artifact's file became the change
        (pr,) = fake.prs_for(REPO, branch)
        assert pr["draft"] is True
        assert run.mr_iid == pr["number"]
        assert run.candidate_shas == [pr["head"]["sha"]]
        evidence = run.evidence
        assert evidence["published_candidate"]["base"] == BASE_HEAD
        assert evidence["published_candidate"]["harness_workflow"] == WORKFLOW
        assert evidence["published_candidate"]["actions_run_id"] == 501

        evidence_notes = [body for body in comments(fake) if "ready for human review" in body]
        assert len(evidence_notes) == 1
        (review,) = reviewer.calls
        assert review["candidate_sha"] == pr["head"]["sha"]
        assert evidence["review"]["sha"] == pr["head"]["sha"]

        async with db() as session:
            targets = [
                row.payload["to"]
                for row in (
                    await session.execute(
                        select(Outbox).where(Outbox.flow_run_id == run_id).order_by(Outbox.id)
                    )
                )
                .scalars()
                .all()
            ]
        assert targets[-4:] == [
            FlowStatus.WAITING_CI.value,
            FlowStatus.EVALUATING_CI.value,
            FlowStatus.REVIEWING.value,
            FlowStatus.READY_FOR_HUMAN.value,
        ]

    async def test_supersede_on_cancel(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)
        await go(service, run_id)
        seed_success_with_candidate(fake, run_id)
        # Mid-flight cancel: the grant is revoked while the run is still
        # waiting_harness — the late candidate can never be published (F13).
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.cancel_requested = True
            await session.commit()

        await service.evaluate_waiting_harness()

        run = await get_run(db, run_id)
        assert fake.calls_of("create_commit_on_branch") == []
        assert fake.calls_of("create_draft_pr") == []
        assert run.evidence["superseded"] == {
            "reason": "cancelled",
            "attempt_base": BASE_HEAD,
        }

    async def test_handle_cancel_also_stops_the_actions_run(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)
        await go(service, run_id)
        assert fake.cancelled_runs == []

        await service.handle_cancel(
            project_id=PROJECT_ID,
            issue_number=ISSUE,
            note_text=f"@forge /cancel {run_id}",
            author_username="alice",
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.CANCELLED.value
        assert run.cancel_requested is True
        assert fake.cancelled_runs == [501]  # the Actions run was stopped too

    async def test_code_failure_blocks_without_repair(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)
        await go(service, run_id)
        for run in fake.actions_runs:
            if run["id"] == 501:
                run.update(status="completed", conclusion="failure")
        fake.seed_actions_jobs(501, [{"id": 9, "name": "harness", "conclusion": "failure"}])
        fake.seed_job_log(9, "claude: the agent exited with code 1\n")

        await service.evaluate_waiting_harness()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert (run.status_reason or "").startswith("harness_code:")
        # Harness failures never enter the repair loop — no new dispatch.
        assert len(fake.dispatch_inputs) == 1

    async def test_infrastructure_failure_blocks_with_its_kind(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)
        await go(service, run_id)
        for run in fake.actions_runs:
            if run["id"] == 501:
                run.update(status="completed", conclusion="failure")
        fake.seed_actions_jobs(501, [{"id": 9, "name": "harness", "conclusion": "failure"}])
        fake.seed_job_log(9, "Error: quota exceeded for this API key\n")

        await service.evaluate_waiting_harness()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert (run.status_reason or "").startswith("harness_infrastructure:")

    async def test_uncorrelated_handle_converges_after_a_restart(self, db, fake):
        """A legacy empty-202 dispatch leaves the handle uncorrelated; the
        reconciler discovers the run and the pipeline completes anyway."""
        fake.dispatch_mode = "legacy"
        service = make_service(db, fake)
        run_id = await start(service)
        await go(service, run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value
        assert run.evidence["harness"]["run_id"] is None  # parked uncorrelated

        # The run shows up in Actions afterwards (delayed visibility).
        branch = f"forge/{ISSUE}/{run_id[:8]}"
        fake.seed_actions_run(
            run_id=77,
            head_branch=branch,
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

        await service.evaluate_waiting_harness()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value  # R02: parked for checks

        await service.evaluate_waiting_ci_one(run_id)  # no CI → unverified

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        assert run.evidence["harness"]["run_id"] == 77  # discovery persisted


# ----------------------------------------------------------------------
# Gate semantics are untouched by the lane choice
# ----------------------------------------------------------------------


class TestGateUnchanged:
    async def test_go_still_consumes_the_decision_exactly_once(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)

        await go(service, run_id)
        await go(service, run_id)  # re-delivered

        async with db() as session:
            gate = (
                (
                    await session.execute(
                        select(GateApproval).where(GateApproval.flow_run_id == run_id)
                    )
                )
                .scalars()
                .one()
            )
        assert gate.consumed_at is not None
        assert len(fake.dispatch_inputs) == 1  # one harness run, not two


# ----------------------------------------------------------------------
# Worker-side reconciler pass (the worker's GitHub harness tick)
# ----------------------------------------------------------------------


class TestWorkerReconcilerPass:
    async def test_pass_ticks_every_repo_with_waiting_harness_runs(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)
        await go(service, run_id)
        seed_success_with_candidate(fake, run_id)
        clear_comments(fake)

        await evaluate_github_waiting_harness(
            make_settings(), ForgeConfig(), db, stack_factory=lambda o, r: make_stack(fake)
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value  # R02: parked for checks

        from forge.runs.github_service import evaluate_github_waiting_ci

        await evaluate_github_waiting_ci(
            make_settings(), ForgeConfig(), db, stack_factory=lambda o, r: make_stack(fake)
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value  # driven to the end

    async def test_pass_is_a_noop_when_no_harness_lane_is_configured(self, db, fake):
        settings = make_settings(FORGE_GITHUB_HARNESS_WORKFLOW="")

        await evaluate_github_waiting_harness(settings, ForgeConfig(), db)  # no repos, no stacks

    async def test_reconciler_loop_exits_when_github_is_disabled(self, db):
        settings = make_settings(FORGE_GITHUB_ENABLED=False)  # explicit: beats any .env
        shutdown = asyncio.Event()
        shutdown.set()  # a pre-set event: the loop exits after (at most) one pass

        await asyncio.wait_for(
            run_github_harness_reconciler(settings, ForgeConfig(), db, shutdown_event=shutdown),
            timeout=5.0,
        )

    async def test_reconciler_loop_exits_without_github_credentials(self, db):
        settings = make_settings(
            FORGE_GITHUB_ENABLED=True,
            # Explicit Nones beat the repo's .env — no App key, no PAT.
            FORGE_GITHUB_PRIVATE_KEY=None,
            FORGE_GITHUB_TOKEN=None,
        )
        shutdown = asyncio.Event()
        shutdown.set()

        await asyncio.wait_for(
            run_github_harness_reconciler(settings, ForgeConfig(), db, shutdown_event=shutdown),
            timeout=5.0,
        )
