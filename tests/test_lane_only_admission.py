"""Lane-only admission refuses before planner spend or provider dispatch."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import ForgeConfig, Settings
from forge.durable import FlowRun
from forge.integrations.github_flow import GitHubAgents, GitHubPublishFlow
from forge.models.base import Base
from forge.runs.github_service import GitHubRunService
from forge.runs.service import RunService, build_default_agents
from forge.runs.stubs import StubPlanner, StubReviewer
from tests.fixtures.fake_github import FakeGitHub
from tests.fixtures.fake_gitlab import FakeGitLab


class NoPaidAgent:
    async def plan(self, *args, **kwargs):
        raise AssertionError("lane refusal must precede paid planning")

    async def propose(self, *args, **kwargs):
        raise AssertionError("server-side implementation is forbidden")


@pytest.fixture()
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def settings(**overrides):
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="test-token",
        GITLAB_WEBHOOK_SECRET="test-secret",
        FORGE_APPROVERS="alice",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        FORGE_IMPLEMENTER_BACKEND="ci_harness:claude-code",
        FORGE_AVAILABLE_DRIVERS=json.dumps(["claude-code"]),
    )
    values.update(overrides)
    return Settings(**values)


async def assert_refused(db, run_id, calls):
    async with db() as session:
        run = await session.get(FlowRun, run_id)
    assert run.status == "blocked"
    assert run.status_reason.startswith("lane_not_onboarded:")
    assert "docs/harnesses/onboarding.md" in run.status_reason
    assert run.evidence["admission"]["code"] == "lane_not_onboarded"
    assert run.evidence["admission"]["onboarding_url"].endswith("docs/harnesses/onboarding.md")
    assert not run.spec_digest and not run.plan_digest
    assert not any(name in {"create_pipeline", "create_commit"} for name, _ in calls)


class TestLaneAdmission:
    @pytest.mark.parametrize("backend", ["builtin", "", "unknown"])
    async def test_gitlab_removed_or_unknown_backend_refuses_before_spend(self, db, backend):
        remote = FakeGitLab()
        service = RunService(
            db,
            remote,
            settings(FORGE_IMPLEMENTER_BACKEND=backend),
            ForgeConfig(),
            planner=NoPaidAgent(),
            implementer=NoPaidAgent(),
            reviewer=StubReviewer(),
        )
        run_id = await service.start_run(42, 7, "Change", "description", "alice")
        await assert_refused(db, run_id, remote.calls)

    @pytest.mark.parametrize(
        "configuration",
        [
            {"valid": True, "jobs": []},
            {"valid": False, "jobs": [{"name": "forge-agent-claude-code"}]},
            {"valid": True, "jobs": [{"name": "forge-agent-copilot"}]},
        ],
    )
    async def test_global_inventory_never_proves_a_project_lane(self, db, configuration):
        remote = FakeGitLab()
        remote.ci_configuration = configuration
        service = RunService(
            db,
            remote,
            settings(),
            ForgeConfig(),
            planner=NoPaidAgent(),
            implementer=NoPaidAgent(),
            reviewer=StubReviewer(),
        )
        run_id = await service.start_run(42, 7, "Change", "description", "alice")
        await assert_refused(db, run_id, remote.calls)

    async def test_github_without_workflow_refuses_before_spend(self, db):
        remote = FakeGitHub()
        stack = GitHubAgents(
            client=remote,
            reader=SimpleNamespace(),
            planner=NoPaidAgent(),
            implementer=NoPaidAgent(),
            reviewer=StubReviewer(),
            flow=GitHubPublishFlow(remote),
        )
        service = GitHubRunService(
            db,
            settings(FORGE_GITHUB_HARNESS_WORKFLOW=""),
            ForgeConfig(),
            stack=stack,
            repo_full_name="acme/widget",
        )
        run_id = await service.start_run(
            project_id=42,
            issue_number=7,
            issue_title="Change",
            issue_description="description",
            author_username="alice",
        )
        await assert_refused(db, run_id, remote.calls)
        assert remote.calls_of("dispatch_workflow") == []

    async def test_onboarded_gitlab_reaches_approval_without_server_implementation(self, db):
        remote = FakeGitLab()
        remote.seed_commit("main", "b" * 40, "base")
        remote.ci_configuration = {"valid": True, "jobs": [{"name": "forge-agent-claude-code"}]}
        service = RunService(
            db,
            remote,
            settings(),
            ForgeConfig(),
            planner=StubPlanner(),
            implementer=NoPaidAgent(),
            reviewer=StubReviewer(),
        )
        run_id = await service.start_run(42, 7, "Change", "description", "alice")
        async with db() as session:
            run = await session.get(FlowRun, run_id)
        assert run.status == "waiting_approval"
        assert run.evidence["backend"].startswith("ci_harness")
        assert remote.calls_of("create_pipeline") == []

    def test_factory_builds_only_planner_and_read_only_reviewer(self, db):
        planner, reviewer = build_default_agents(settings(), FakeGitLab(), db)
        assert not hasattr(planner, "propose")
        assert hasattr(reviewer, "review")

    def test_server_implementer_module_is_absent(self):
        assert not (Path(__file__).parents[1] / "src/forge/factory/implementer.py").exists()


@pytest.mark.parametrize(
    "content,accepted",
    [
        ("# workflow_dispatch\nname: unrelated\n", False),
        ("on: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n", False),
        ("on:\n  workflow_dispatch: {}\njobs:\n  lane:\n    runs-on: ubuntu-latest\n", True),
    ],
)
async def test_github_lane_requires_the_project_workflow_contract(content, accepted):
    from forge.runs.lane import github_lane_refusal

    remote = FakeGitHub()
    remote.seed_repo("acme/widget", {".github/workflows/forge.yml": content})
    reader = remote
    refusal = await github_lane_refusal(reader, 42, "main", "forge.yml")
    assert (refusal is None) is accepted
    assert remote.calls_of("dispatch_workflow") == []


async def test_gitlab_ci_admission_is_a_read_only_project_scoped_request(httpx_mock):
    from forge.gitlab.client import GitLabClient

    httpx_mock.add_response(json={"valid": True, "jobs": [{"name": "forge-agent"}]})
    async with GitLabClient("https://gitlab.test", "fixture-token") as client:
        result = await client.lint_ci_configuration(42, "b" * 40)
    request = httpx_mock.get_request()
    assert request.method == "GET"
    assert request.url.path == "/api/v4/projects/42/ci/lint"
    assert request.url.params["content_ref"] == "b" * 40
    assert request.url.params["include_jobs"] == "true"
    assert request.url.params["dry_run"] == "false"
    assert result["jobs"] == [{"name": "forge-agent"}]


async def test_planner_cannot_select_a_driver_missing_from_project_ci(db):
    class Planner(StubPlanner):
        async def plan(self, *args, **kwargs):
            text = await super().plan(*args, **kwargs)
            self.last_plan = {"harness": "copilot", "reason": "model preference"}
            return text

    remote = FakeGitLab()
    remote.seed_commit("main", "b" * 40, "base")
    remote.ci_configuration = {"valid": True, "jobs": [{"name": "forge-agent-claude-code"}]}
    service = RunService(
        db,
        remote,
        settings(
            FORGE_AVAILABLE_DRIVERS='["claude-code", "copilot"]',
            FORGE_HARNESS_PREFERENCE="claude-code,copilot",
        ),
        ForgeConfig(),
        planner=Planner(),
        reviewer=StubReviewer(),
    )
    run_id = await service.start_run(42, 7, "Change", "description", "alice")
    async with db() as session:
        run = await session.get(FlowRun, run_id)
    assert run.status == "waiting_approval"
    assert run.evidence["harness_selection"]["harness"] == "claude-code"
    assert run.evidence["harness_selection"]["harness_fallbacks"] == []


async def test_frozen_retired_backend_never_dispatches():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from forge.runs.github_service import GitHubRunService

    service = object.__new__(GitHubRunService)
    service._spec_or_block = AsyncMock(return_value=SimpleNamespace(backend="builtin"))
    service._refuse_lane = AsyncMock()
    service._advance_harness = AsyncMock(side_effect=AssertionError("retired execution dispatched"))
    await service._advance_publish("run", project_id=1, issue_number=1)
    assert service._refuse_lane.await_args.args[1].code == "lane_not_onboarded"
    service._advance_harness.assert_not_awaited()
