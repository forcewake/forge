"""NXT-31: the GitHub harness reconciler is scoped to the bound repository.

The acceptance shape from the review: the OUTER production reconciler
(:func:`forge.runs.github_service.evaluate_github_waiting_harness`) runs
with TWO repositories holding waiting runs that share the issue number and
file paths — each run must be processed exactly by its own bound service's
reader/executor/publisher, never by the sibling's. The negative tests pin
the three refusal edges: a bound service's own scan never admits the other
repository's run, a foreign run handed directly to the handler is rejected
before any repository read or write, and a wrong-repository ActionsHandle
cannot route artifacts into the bound repository. One provider failure
must not mutate the unrelated repository's run.
"""

from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import ForgeConfig, Settings
from forge.durable import FlowRun, FlowStatus
from forge.execution.github_actions import ActionsHandle, artifact_name_for
from forge.factory.reviewer import ReviewVerdict
from forge.integrations.github_flow import GitHubAgents, GitHubPublishFlow
from forge.models.base import Base
from forge.runs.github_service import (
    GitHubRunService,
    evaluate_github_waiting_harness,
)
from forge.runs.stubs import StubImplementer, StubPlanner
from tests.fixtures.candidate import create_diff
from tests.fixtures.fake_github import FakeGitHub

REPO_A = "acme/acme-widget"
REPO_B = "beta/beta-gadget"
OWNER_A, NAME_A = REPO_A.split("/", 1)
OWNER_B, NAME_B = REPO_B.split("/", 1)
# Two projects on the same connection, deliberately SHARING the issue
# number and the file layout — identical subjects except the repository.
PROJECT_A = 70010
PROJECT_B = 70011
SHARED_ISSUE = 42
ISSUE_TITLE = "Add password reset"
ISSUE_DESC = "Users cannot reset their password."
BASE_HEAD = "1" * 40
WORKFLOW = "forge-harness.github.yml"
MODEL = "glm-5.3-flash[1m]"
# The same candidate path in both repositories ("similar paths").
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


class StubPRReviewer:
    """Deterministic PR verdict — the GitHub-shaped review surface."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def review(self, **kwargs) -> ReviewVerdict:
        self.calls.append(kwargs)
        return ReviewVerdict(verdict="ok", summary="clean implementation", findings=())


def make_stack(fake: FakeGitHub) -> GitHubAgents:
    return GitHubAgents(
        client=fake,
        reader=fake,
        planner=StubPlanner(),
        implementer=StubImplementer(),
        reviewer=StubPRReviewer(),
        flow=GitHubPublishFlow(fake, proposer=StubImplementer(), base_branch="main"),
    )


class ExplodingPollClient:
    """Delegates everything to the wrapped fake except the Actions poll.

    One provider's API failing must not leak into the other repository's
    pass: the poll raises, the per-run handler keeps the run waiting, and
    the reconciler moves on to the sibling repository.
    """

    def __init__(self, inner: FakeGitHub) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def get_workflow_run(self, owner: str, repo: str, run_id: int) -> dict:
        raise RuntimeError(f"provider of {owner}/{repo} is down")


def seeded_fake(repo_full_name: str) -> FakeGitHub:
    fake = FakeGitHub()
    # The SAME file layout in both repositories ("similar paths").
    fake.seed_repo(repo_full_name, {"src/app.py": "print('hi')\n"})
    fake.heads[repo_full_name]["main"] = BASE_HEAD
    fake.seed_issue(repo_full_name, SHARED_ISSUE, ISSUE_TITLE, ISSUE_DESC)
    return fake


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


async def get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


def make_service(db, fake: FakeGitHub, repo_full_name: str) -> GitHubRunService:
    return GitHubRunService(
        db,
        make_settings(),
        ForgeConfig(),
        stack=make_stack(fake),
        repo_full_name=repo_full_name,
    )


async def start_and_go(service: GitHubRunService, project_id: int) -> str:
    """One run parked in ``waiting_harness`` with a journaled handle."""
    run_id = await service.start_run(
        project_id=project_id,
        issue_number=SHARED_ISSUE,
        issue_title=ISSUE_TITLE,
        issue_description=ISSUE_DESC,
        author_username="alice",
    )
    await service.handle_go(
        project_id=project_id,
        issue_number=SHARED_ISSUE,
        note_text=f"@forge /go {run_id}",
        author_username="alice",
    )
    return run_id


def seed_success(fake: FakeGitHub, run_id: str, actions_run_id: int = 501) -> None:
    """The workflow finished and uploaded the candidate artifact."""
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


def polled_actions_run_ids(fake: FakeGitHub) -> list[int]:
    """The Actions run ids this provider was polled for.

    The scoping signal: a service polling for ANOTHER repository's Actions
    run id on its own provider is the routing defect itself, even when the
    poll happens to succeed or fail harmlessly.
    """
    return [call[1][2] for call in fake.calls_of("get_workflow_run")]


def distinct_actions_ids(fake_b: FakeGitHub) -> int:
    """Bump fake_b so B's dispatch gets run id 502, not A's 501.

    With distinct ids, a cross-repository poll is observable per provider:
    run A's handle carries 501, run B's carries 502 — whichever id shows up
    on the wrong fake names the run that was misrouted.
    """
    fake_b._next_actions_run = 501  # next dispatch allocates 502
    return 502


def commit_targets(fake: FakeGitHub) -> list[tuple[str, str]]:
    """The (owner, repo) every branch commit of this fake targeted."""
    return [(call[1][0], call[1][1]) for call in fake.calls_of("create_commit_on_branch")]


class TestOuterReconcilerScopesEachRepository:
    async def test_two_repos_processed_only_by_their_own_bound_services(self, db):
        fake_a, fake_b = seeded_fake(REPO_A), seeded_fake(REPO_B)
        actions_run_b = distinct_actions_ids(fake_b)
        service_a = make_service(db, fake_a, REPO_A)
        service_b = make_service(db, fake_b, REPO_B)
        run_a = await start_and_go(service_a, PROJECT_A)
        run_b = await start_and_go(service_b, PROJECT_B)
        assert (await get_run(db, run_a)).status == FlowStatus.WAITING_HARNESS.value
        assert (await get_run(db, run_b)).status == FlowStatus.WAITING_HARNESS.value
        seed_success(fake_a, run_a)
        seed_success(fake_b, run_b, actions_run_id=actions_run_b)
        fake_a.calls.clear()
        fake_b.calls.clear()

        def stack_factory(owner: str, repo: str) -> GitHubAgents:
            # The production seam: one bound stack per repository.
            if (owner, repo) == (OWNER_A, NAME_A):
                return make_stack(fake_a)
            assert (owner, repo) == (OWNER_B, NAME_B)
            return make_stack(fake_b)

        await evaluate_github_waiting_harness(
            make_settings(), ForgeConfig(), db, stack_factory=stack_factory
        )

        # Each run advanced to waiting_ci through ITS OWN publisher.
        assert (await get_run(db, run_a)).status == FlowStatus.WAITING_CI.value
        assert (await get_run(db, run_b)).status == FlowStatus.WAITING_CI.value

        # Each provider polled ONLY its own run's Actions id — the misroute
        # itself is observable here even when it would happen to succeed.
        assert polled_actions_run_ids(fake_a) == [501]
        assert polled_actions_run_ids(fake_b) == [actions_run_b]

        # ... and each provider saw exactly ONE commit — its own run's
        # candidate, targeted at its own repository.
        assert commit_targets(fake_a) == [(OWNER_A, NAME_A)]
        assert commit_targets(fake_b) == [(OWNER_B, NAME_B)]
        branch_a = f"forge/{SHARED_ISSUE}/{run_a[:8]}"
        branch_b = f"forge/{SHARED_ISSUE}/{run_b[:8]}"
        (commit_a,) = fake_a.calls_of("create_commit_on_branch")
        assert commit_a[1][2] == branch_a
        (commit_b,) = fake_b.calls_of("create_commit_on_branch")
        assert commit_b[1][2] == branch_b
        (pr_a,) = fake_a.prs_for(REPO_A, branch_a)
        (pr_b,) = fake_b.prs_for(REPO_B, branch_b)
        assert pr_a["draft"] is True and pr_b["draft"] is True

    async def test_one_provider_failure_does_not_mutate_the_other_repo_run(self, db):
        fake_a, fake_b = seeded_fake(REPO_A), seeded_fake(REPO_B)
        actions_run_b = distinct_actions_ids(fake_b)
        service_a = make_service(db, fake_a, REPO_A)
        service_b = make_service(db, fake_b, REPO_B)
        run_a = await start_and_go(service_a, PROJECT_A)
        run_b = await start_and_go(service_b, PROJECT_B)
        seed_success(fake_a, run_a)
        seed_success(fake_b, run_b, actions_run_id=actions_run_b)
        fake_a.calls.clear()
        fake_b.calls.clear()

        failing_stack_a = GitHubAgents(
            client=ExplodingPollClient(fake_a),
            reader=fake_a,
            planner=StubPlanner(),
            implementer=StubImplementer(),
            reviewer=StubPRReviewer(),
            flow=GitHubPublishFlow(fake_a, proposer=StubImplementer(), base_branch="main"),
        )

        def stack_factory(owner: str, repo: str) -> GitHubAgents:
            if (owner, repo) == (OWNER_A, NAME_A):
                return failing_stack_a
            return make_stack(fake_b)

        await evaluate_github_waiting_harness(
            make_settings(), ForgeConfig(), db, stack_factory=stack_factory
        )

        # The broken provider's run is left exactly where it was — still
        # waiting, NOT blocked, no fallback, no state write of any kind.
        assert (await get_run(db, run_a)).status == FlowStatus.WAITING_HARNESS.value
        assert commit_targets(fake_a) == []
        # The sibling repository completed its own pass, polled only its own
        # run, and published only its own candidate.
        assert (await get_run(db, run_b)).status == FlowStatus.WAITING_CI.value
        assert polled_actions_run_ids(fake_b) == [actions_run_b]
        assert commit_targets(fake_b) == [(OWNER_B, NAME_B)]


class TestBoundServiceRefusesForeignSubjects:
    async def test_bound_scan_never_admits_the_other_repository_run(self, db):
        fake_a, fake_b = seeded_fake(REPO_A), seeded_fake(REPO_B)
        distinct_actions_ids(fake_b)
        service_a = make_service(db, fake_a, REPO_A)
        service_b = make_service(db, fake_b, REPO_B)
        run_a = await start_and_go(service_a, PROJECT_A)
        run_b = await start_and_go(service_b, PROJECT_B)
        # Only A's workflow completed; B's run must not even be looked at by
        # A's pass — B's fake records nothing at all, and A's provider is
        # never polled for B's Actions run id.
        seed_success(fake_a, run_a)
        fake_a.calls.clear()
        calls_b_before = len(fake_b.calls)

        await service_a.evaluate_waiting_harness()

        assert (await get_run(db, run_a)).status == FlowStatus.WAITING_CI.value
        assert (await get_run(db, run_b)).status == FlowStatus.WAITING_HARNESS.value
        assert polled_actions_run_ids(fake_a) == [501]  # never B's id
        assert len(fake_b.calls) == calls_b_before  # B's provider never touched
        assert commit_targets(fake_a) == [(OWNER_A, NAME_A)]

    async def test_foreign_run_passed_directly_to_the_handler_is_refused(self, db):
        fake_a, fake_b = seeded_fake(REPO_A), seeded_fake(REPO_B)
        actions_run_b = distinct_actions_ids(fake_b)
        service_a = make_service(db, fake_a, REPO_A)
        service_b = make_service(db, fake_b, REPO_B)
        run_a = await start_and_go(service_a, PROJECT_A)
        run_b = await start_and_go(service_b, PROJECT_B)
        # B's candidate exists: without the entry guard service A would poll
        # for it and drive B's run through A's bound adapters.
        seed_success(fake_b, run_b, actions_run_id=actions_run_b)
        fake_a.calls.clear()
        evidence_before = dict((await get_run(db, run_b)).evidence or {})

        await service_a._evaluate_harness_one(run_b, datetime.now(timezone.utc))

        run_b_row = await get_run(db, run_b)
        assert run_b_row.status == FlowStatus.WAITING_HARNESS.value  # untouched
        assert dict(run_b_row.evidence or {}) == evidence_before  # no discovery/supersede
        assert len(fake_a.calls) == 0  # refused before ANY provider call
        assert (await get_run(db, run_a)).status == FlowStatus.WAITING_HARNESS.value

    async def test_wrong_repository_handle_cannot_route_artifacts_here(self, db):
        fake_a, fake_b = seeded_fake(REPO_A), seeded_fake(REPO_B)
        distinct_actions_ids(fake_b)
        service_a = make_service(db, fake_a, REPO_A)
        service_b = make_service(db, fake_b, REPO_B)
        run_a = await start_and_go(service_a, PROJECT_A)
        await start_and_go(service_b, PROJECT_B)
        seed_success(fake_a, run_a)  # a real, publishable candidate exists
        # Tamper the journaled handle: A's run now points at B's repository.
        async with db() as session:
            run = await session.get(FlowRun, run_a)
            evidence = dict(run.evidence or {})
            harness = dict(evidence.get("harness") or {})
            handle = ActionsHandle.from_json(str(harness.get("handle") or ""))
            harness["handle"] = replace(handle, owner=OWNER_B, repo=NAME_B).to_json()
            evidence["harness"] = harness
            run.evidence = evidence
            await session.commit()
        fake_a.calls.clear()
        calls_b_before = len(fake_b.calls)

        await service_a.evaluate_waiting_harness()

        # The handle/repo disagreement refuses the run BEFORE any poll,
        # publication or fallback: no state move, no provider effect on
        # either side.
        assert (await get_run(db, run_a)).status == FlowStatus.WAITING_HARNESS.value
        assert len(fake_a.calls) == 0
        assert len(fake_b.calls) == calls_b_before
        assert commit_targets(fake_a) == [] and commit_targets(fake_b) == []
