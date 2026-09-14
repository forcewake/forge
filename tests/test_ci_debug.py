"""The durable CI debug lane (v0.7, research F3 port).

Covers both executors in :mod:`forge.reactive.ci_debug` and their wiring:

- **GitHub ``debug_ci``**: the ``workflow_job`` failure normalizer (plus the
  forge-harness skip), the durable ingress (inbox row + scheduled step), and
  the executor — fork-safe head-SHA → PR correlation, the forge-App PR
  recursion guard, the bounded/redacted log evidence, and the sticky
  root-cause comment on the PR.
- **GitLab ``debug_pipeline``**: the failed-pipeline routing at the ingress,
  the executor's MR correlation (payload iid, else open MR by source
  branch), and the factory-branch skip — the run's own repair loop already
  fetches failed-job logs into the repair brief and posts the repair-cycle
  MR note (asserted end-to-end by
  ``test_runs_repair.test_code_failure_repairs_without_a_second_gate``);
  this lane must not duplicate either.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select

from forge.agents.models import FailedJobAnalysis, PipelineDebugResult
from forge.config import Settings
from forge.database import reset_engine
from forge.durable import EventInbox, StepRun
from forge.gateway.github_webhook import normalize_workflow_job_event
from forge.gateway.router import _match_pipeline_debug
from forge.gitlab.events import PipelineEvent
from forge.main import create_app
from forge.reactive.ci_debug import (
    DEBUG_LOG_PER_JOB_CHARS,
    DEBUG_MARKER_TEMPLATE,
    execute_debug_ci_command,
    execute_debug_pipeline_command,
    format_debug_comment,
    is_forge_harness_run,
)
from tests.fixtures.fake_github import FakeGitHub
from tests.fixtures.fake_gitlab import FakeGitLab
from tests.conftest import TEST_WEBHOOK_SECRET

GITHUB_FIXTURES = Path(__file__).parent / "fixtures" / "github_payloads"
GITHUB_WEBHOOK_SECRET = "github-hook-secret"
TEST_DELIVERY = "d" * 32
PROJECT_ID = 42

HEAD_SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
JOB_ID = 3994444969


def load_github_payload(name: str) -> bytes:
    return (GITHUB_FIXTURES / name).read_bytes()


def sign(body: bytes, secret: str = GITHUB_WEBHOOK_SECRET) -> str:
    import hashlib
    import hmac

    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def github_settings(tmp_path=None, **overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test"),
        GITLAB_WEBHOOK_SECRET=SecretStr(TEST_WEBHOOK_SECRET),
        DATABASE_URL=(
            f"sqlite+aiosqlite:///{tmp_path}/ci-debug.db"
            if tmp_path is not None
            else "sqlite+aiosqlite:///:memory:"
        ),
        LITELLM_URL="http://litellm:4000",
        REDIS_URL=None,
        FORGE_CAPTURE_DIR=None,
        FORGE_BOT_TOKEN=None,
        FORGE_BOT_USERNAME="forge-bot",
        FORGE_GITHUB_ENABLED=True,
        FORGE_GITHUB_WEBHOOK_SECRET=SecretStr(GITHUB_WEBHOOK_SECRET),
        FORGE_GITHUB_APP_ID="123456",
        FORGE_GITHUB_INSTALLATION_ID="777",
        FORGE_GITHUB_PRIVATE_KEY=SecretStr("not-a-real-key"),
    )
    values.update(overrides)
    return Settings(**values)


def debug_result(
    summary="The test step asserts a widget count that never increments.",
) -> PipelineDebugResult:
    return PipelineDebugResult(
        summary=summary,
        is_flaky=False,
        jobs=[
            FailedJobAnalysis(
                job_name="build-and-test",
                job_stage="test",
                root_cause="assertion compares against a stale counter",
                error_type="test",
                fix_suggestion="reset the counter before the assertion",
                relevant_files=["src/widget.py"],
                confidence="high",
            )
        ],
        suggested_actions=["Re-run the failed job after the fix"],
    )


def gitlab_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test"),
        GITLAB_WEBHOOK_SECRET=SecretStr(TEST_WEBHOOK_SECRET),
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        LITELLM_URL="http://litellm:4000",
    )
    values.update(overrides)
    return Settings(**values)


def seed_pr(fake_github: FakeGitHub, *, login: str, user_type: str, sha: str) -> dict:
    """Seed a PR whose head sits at *sha* — the correlation target."""
    pr = {
        "number": 7,
        "id": 7,
        "title": "Draft: add rate limiter",
        "state": "open",
        "draft": True,
        "user": {"login": login, "type": user_type},
        "head": {"ref": "feature/rate-limiter", "label": f"acme:{sha[:4]}", "sha": sha},
        "base": {"ref": "main"},
        "html_url": "https://github.test/acme/acme-widget/pull/7",
    }
    fake_github.pull_requests.setdefault("acme/acme-widget", []).append(pr)
    return pr


# ----------------------------------------------------------------------
# GitHub: normalization + ingress
# ----------------------------------------------------------------------


class TestNormalizeWorkflowJob:
    def test_failed_job_becomes_debug_ci_command(self):
        payload = json.loads(load_github_payload("workflow_job_failed.json"))
        command = normalize_workflow_job_event(payload, harness_workflow="forge-harness.yml")

        assert command is not None
        assert command["command"] == "debug_ci"
        assert command["provider"] == "github"
        assert command["repo_full_name"] == "acme/acme-widget"
        assert command["head_sha"] == HEAD_SHA
        assert command["head_branch"] == "feature/rate-limiter"
        assert command["job_id"] == JOB_ID
        assert command["run_id"] == 3994440871
        assert command["job_name"] == "build-and-test"
        assert command["workflow_name"] == "CI"
        assert command["delivery_key"] == f"wfjob:{JOB_ID}:failure"

    def test_successful_completion_is_ignored(self):
        payload = json.loads(load_github_payload("workflow_job_failed.json"))
        payload["workflow_job"]["conclusion"] = "success"
        assert normalize_workflow_job_event(payload) is None

    def test_in_progress_action_is_ignored(self):
        payload = json.loads(load_github_payload("workflow_job_failed.json"))
        payload["action"] = "in_progress"
        assert normalize_workflow_job_event(payload) is None

    def test_forge_harness_run_is_skipped(self):
        payload = json.loads(load_github_payload("workflow_job_failed.json"))
        payload["workflow_job"]["head_branch"] = "forge/42/abcd1234"
        payload["workflow_name"] = "forge-harness.yml"
        assert normalize_workflow_job_event(payload, harness_workflow="forge-harness.yml") is None

    def test_non_harness_workflow_on_forge_branch_is_kept(self):
        payload = json.loads(load_github_payload("workflow_job_failed.json"))
        payload["workflow_job"]["head_branch"] = "forge/42/abcd1234"
        payload["workflow_name"] = "ci.yml"
        command = normalize_workflow_job_event(payload, harness_workflow="forge-harness.yml")
        assert command is not None  # only forge's own HARNESS runs are skipped


class TestWorkflowJobIngress:
    @pytest.fixture()
    async def app(self, tmp_path):
        reset_engine()
        application = create_app(settings=github_settings(tmp_path))
        async with application.router.lifespan_context(application):
            application.state.task_queue = AsyncMock()
            application.state.task_queue.is_duplicate = AsyncMock(return_value=False)
            yield application
        reset_engine()

    @pytest.fixture()
    async def client(self, app) -> AsyncClient:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    async def test_failed_job_persists_inbox_and_step(self, app, client: AsyncClient):
        body = load_github_payload("workflow_job_failed.json")
        response = await client.post(
            "/webhook/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sign(body),
                "X-GitHub-Event": "workflow_job",
                "X-GitHub-Delivery": TEST_DELIVERY,
            },
        )

        assert response.status_code == 202
        assert response.json()["run_command"] is True
        async with app.state.session_factory() as session:
            inbox = (await session.execute(select(EventInbox))).scalars().all()
            steps = (await session.execute(select(StepRun))).scalars().all()
        assert len(inbox) == 1 and len(steps) == 1
        assert inbox[0].payload["command"] == "debug_ci"
        assert inbox[0].payload["head_sha"] == HEAD_SHA
        assert steps[0].step_name == "debug_ci"
        assert steps[0].status == "scheduled"
        assert steps[0].source_event_id == inbox[0].source_event_id
        (task,) = app.state.task_queue.submit.await_args[0]
        assert task.task_type == "run_command"
        assert task.metadata["command"] == "debug_ci"

    async def test_successful_job_is_recorded_without_step(self, app, client: AsyncClient):
        payload = json.loads(load_github_payload("workflow_job_failed.json"))
        payload["workflow_job"]["conclusion"] = "success"
        body = json.dumps(payload).encode()
        response = await client.post(
            "/webhook/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sign(body),
                "X-GitHub-Event": "workflow_job",
                "X-GitHub-Delivery": TEST_DELIVERY,
            },
        )

        assert response.status_code == 202
        assert response.json() == {
            "status": "accepted",
            "event": "workflow_job",
            "recorded": True,
        }
        async with app.state.session_factory() as session:
            steps = (await session.execute(select(StepRun))).scalars().all()
        assert steps == []


# ----------------------------------------------------------------------
# GitHub: the executor
# ----------------------------------------------------------------------


class TestDebugCiExecutor:
    async def test_failed_job_debugs_onto_the_associated_pr(self):
        fake = FakeGitHub()
        seed_pr(fake, login="alice", user_type="User", sha=HEAD_SHA)
        fake.seed_job_log(JOB_ID, "E  AssertionError: widget count 0 != 1")
        seen: dict = {}

        async def runner(failed_jobs, job_logs):
            seen["jobs"] = failed_jobs
            seen["logs"] = job_logs
            return debug_result()

        outcome = await execute_debug_ci_command(
            github_settings(),
            None,
            None,
            {
                "command": "debug_ci",
                "provider": "github",
                "repo_full_name": "acme/acme-widget",
                "head_sha": HEAD_SHA,
                "head_branch": "feature/rate-limiter",
                "job_id": JOB_ID,
                "job_name": "build-and-test",
                "workflow_name": "CI",
            },
            client=fake,
            debug_runner=runner,
        )

        assert outcome["status"] == "debugged"
        assert outcome["pr_number"] == 7
        # The agent saw the failed job and its bounded log.
        assert seen["jobs"][0].id == JOB_ID
        assert "widget count 0 != 1" in seen["logs"][JOB_ID]
        # The root-cause comment landed on the PR, sticky marker included.
        comments = fake.issue_comments["acme/acme-widget"][7]
        assert len(comments) == 1
        assert DEBUG_MARKER_TEMPLATE.format(head_sha=HEAD_SHA) in comments[0]["body"]
        assert "stale counter" in comments[0]["body"]
        assert "Re-run the failed job" in comments[0]["body"]

    async def test_comment_is_edited_in_place_on_re_debug(self):
        fake = FakeGitHub()
        seed_pr(fake, login="alice", user_type="User", sha=HEAD_SHA)
        marker = DEBUG_MARKER_TEMPLATE.format(head_sha=HEAD_SHA)
        fake.issue_comments.setdefault("acme/acme-widget", {})[7] = [
            {"id": 555, "body": f"{marker} older diagnosis", "user": {"login": "forge-bot"}}
        ]

        async def runner(failed_jobs, job_logs):
            return debug_result(summary="Fresh diagnosis.")

        outcome = await execute_debug_ci_command(
            github_settings(),
            None,
            None,
            {
                "command": "debug_ci",
                "provider": "github",
                "repo_full_name": "acme/acme-widget",
                "head_sha": HEAD_SHA,
                "head_branch": "feature/rate-limiter",
                "job_id": JOB_ID,
                "job_name": "build-and-test",
            },
            client=fake,
            debug_runner=runner,
        )

        assert outcome["status"] == "debugged"
        comments = fake.issue_comments["acme/acme-widget"][7]
        assert len(comments) == 1  # edited, not duplicated
        assert comments[0]["id"] == 555
        assert "Fresh diagnosis." in comments[0]["body"]

    async def test_bot_authored_pr_is_skipped(self):
        fake = FakeGitHub()
        seed_pr(fake, login="forge-app[bot]", user_type="Bot", sha=HEAD_SHA)
        runner = AsyncMock(return_value=debug_result())

        outcome = await execute_debug_ci_command(
            github_settings(),
            None,
            None,
            {
                "command": "debug_ci",
                "provider": "github",
                "repo_full_name": "acme/acme-widget",
                "head_sha": HEAD_SHA,
                "head_branch": "feature/rate-limiter",
                "job_id": JOB_ID,
                "job_name": "build-and-test",
            },
            client=fake,
            debug_runner=runner,
        )

        assert outcome == {"status": "skipped", "reason": "bot_authored_pr"}
        runner.assert_not_awaited()
        assert fake.issue_comments == {}

    async def test_run_without_an_associated_pr_is_skipped(self):
        fake = FakeGitHub()  # no PRs at all (e.g. a fork-PR run or branch push)
        runner = AsyncMock(return_value=debug_result())

        outcome = await execute_debug_ci_command(
            github_settings(),
            None,
            None,
            {
                "command": "debug_ci",
                "provider": "github",
                "repo_full_name": "acme/acme-widget",
                "head_sha": HEAD_SHA,
                "head_branch": "dependabot/pip/x",
                "job_id": JOB_ID,
                "job_name": "build-and-test",
            },
            client=fake,
            debug_runner=runner,
        )

        assert outcome == {"status": "skipped", "reason": "no_associated_pr"}
        runner.assert_not_awaited()

    async def test_forge_harness_run_is_skipped_in_depth(self):
        fake = FakeGitHub()
        seed_pr(fake, login="alice", user_type="User", sha=HEAD_SHA)
        runner = AsyncMock(return_value=debug_result())

        outcome = await execute_debug_ci_command(
            github_settings(FORGE_GITHUB_HARNESS_WORKFLOW="forge-harness.yml"),
            None,
            None,
            {
                "command": "debug_ci",
                "provider": "github",
                "repo_full_name": "acme/acme-widget",
                "head_sha": HEAD_SHA,
                "head_branch": "forge/42/abcd1234",
                "job_id": JOB_ID,
                "job_name": "harness",
                "workflow_name": "forge-harness.yml",
            },
            client=fake,
            debug_runner=runner,
        )

        assert outcome == {"status": "skipped", "reason": "forge_harness_run"}
        runner.assert_not_awaited()
        # The fork-safe correlation was never even paid for.
        assert not any(name == "list_pull_requests_for_commit" for name, _ in fake.calls)

    async def test_log_evidence_is_tail_bounded_and_redacted(self):
        fake = FakeGitHub()
        seed_pr(fake, login="alice", user_type="User", sha=HEAD_SHA)
        secret_line = "export TOKEN=glpat-secret-value-abcdef\n"
        huge_log = ("line of routine build output\n" * 4000) + secret_line
        fake.seed_job_log(JOB_ID, huge_log)
        seen: dict = {}

        async def runner(failed_jobs, job_logs):
            seen["log"] = job_logs[JOB_ID]
            return debug_result()

        await execute_debug_ci_command(
            github_settings(),
            None,
            None,
            {
                "command": "debug_ci",
                "provider": "github",
                "repo_full_name": "acme/acme-widget",
                "head_sha": HEAD_SHA,
                "head_branch": "feature/rate-limiter",
                "job_id": JOB_ID,
                "job_name": "build-and-test",
            },
            client=fake,
            debug_runner=runner,
        )

        assert len(seen["log"]) <= DEBUG_LOG_PER_JOB_CHARS
        assert seen["log"].endswith("\n")  # the tail slice, not the log head
        assert "widget count" not in seen["log"]
        # F23: CI-log secrets never leave the boundary unredacted.
        assert "glpat-secret-value-abcdef" not in seen["log"]

    def test_harness_guard_needs_branch_and_workflow(self):
        assert is_forge_harness_run("forge/1/abcd", "h.yml", "h.yml")
        assert not is_forge_harness_run("forge/1/abcd", "ci.yml", "h.yml")
        assert not is_forge_harness_run("main", "h.yml", "h.yml")
        assert not is_forge_harness_run("forge/1/abcd", "h.yml", "")


# ----------------------------------------------------------------------
# GitLab: routing + executor
# ----------------------------------------------------------------------


def pipeline_event(**overrides) -> PipelineEvent:
    payload = {
        "object_kind": "pipeline",
        "user": {"username": "alice", "name": "Alice", "id": 1, "email": "a@b.c"},
        "project": {
            "id": PROJECT_ID,
            "name": "p",
            "path_with_namespace": "g/p",
            "web_url": "https://gitlab.test/g/p",
        },
        "object_attributes": {
            "id": 200,
            "ref": "feature/auth",
            "status": "failed",
            "sha": "b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3",
        },
        "merge_request": {"id": 100, "iid": 7},
    }
    payload.update(overrides)
    return PipelineEvent.model_validate(payload)


class TestPipelineDebugRouting:
    def test_failed_pipeline_matches_the_debug_command(self):
        command = _match_pipeline_debug(pipeline_event(), gitlab_settings())
        assert command is not None
        assert command["command"] == "debug_pipeline"
        assert command["project_id"] == PROJECT_ID
        assert command["pipeline_id"] == 200
        assert command["branch"] == "feature/auth"
        assert command["mr_iid"] == 7

    def test_successful_pipeline_is_not_routed(self):
        event = pipeline_event()
        event.object_attributes.status = "success"
        assert _match_pipeline_debug(event, gitlab_settings()) is None

    def test_factory_branch_pipeline_is_routed_with_its_branch(self):
        event = pipeline_event()
        event.object_attributes.ref = "factory/7/abcd1234"
        event.merge_request = None
        command = _match_pipeline_debug(event, gitlab_settings())
        assert command is not None
        assert command["branch"] == "factory/7/abcd1234"
        assert command["mr_iid"] is None


class TestDebugPipelineExecutor:
    async def test_failed_pipeline_debugs_onto_the_mr(self):
        fake = FakeGitLab()
        mr = await fake.create_merge_request(PROJECT_ID, "feature/auth", "main", "Add auth")
        fake.set_pipeline_jobs(
            200,
            [
                {"id": 301, "name": "build", "status": "success"},
                {
                    "id": 302,
                    "name": "test-unit",
                    "status": "failed",
                    "failure_reason": "script_failure",
                },
            ],
        )
        fake.set_job_log(302, "E  AssertionError: token missing")
        seen: dict = {}

        async def runner(failed_jobs, job_logs):
            seen["jobs"] = failed_jobs
            seen["logs"] = job_logs
            return debug_result()

        outcome = await execute_debug_pipeline_command(
            gitlab_settings(),
            None,
            None,
            {
                "command": "debug_pipeline",
                "project_id": PROJECT_ID,
                "pipeline_id": 200,
                "branch": "feature/auth",
                "sha": "b" * 40,
                "mr_iid": mr["iid"],
            },
            gitlab=fake,
            debug_runner=runner,
        )

        assert outcome["status"] == "debugged"
        assert outcome["mr_iid"] == mr["iid"]
        assert outcome["failed_jobs"] == ["test-unit"]
        # The agent saw the failed job and its log.
        assert seen["jobs"][0].id == 302
        assert "token missing" in seen["logs"][302]
        # The root-cause note landed on the MR.
        (note,) = fake.mr_notes_containing("stale counter")
        assert note["mr_iid"] == mr["iid"]
        assert "token missing" not in note["body"]  # the log itself is not pasted

    async def test_mr_found_via_branch_name_when_payload_has_none(self):
        fake = FakeGitLab()
        mr = await fake.create_merge_request(PROJECT_ID, "feature/auth", "main", "Add auth")
        fake.set_pipeline_jobs(200, [{"id": 302, "name": "test-unit", "status": "failed"}])

        async def runner(failed_jobs, job_logs):
            return debug_result()

        outcome = await execute_debug_pipeline_command(
            gitlab_settings(),
            None,
            None,
            {
                "command": "debug_pipeline",
                "project_id": PROJECT_ID,
                "pipeline_id": 200,
                "branch": "feature/auth",
                "sha": "b" * 40,
                "mr_iid": None,
            },
            gitlab=fake,
            debug_runner=runner,
        )

        assert outcome["status"] == "debugged"
        assert outcome["mr_iid"] == mr["iid"]
        assert fake.mr_notes_containing("stale counter")

    async def test_factory_branch_is_skipped_repair_loop_owns_it(self):
        fake = FakeGitLab()
        await fake.create_merge_request(PROJECT_ID, "factory/7/abcd1234", "main", "Draft: x")
        runner = AsyncMock(return_value=debug_result())

        outcome = await execute_debug_pipeline_command(
            gitlab_settings(),
            None,
            None,
            {
                "command": "debug_pipeline",
                "project_id": PROJECT_ID,
                "pipeline_id": 200,
                "branch": "factory/7/abcd1234",
                "sha": "b" * 40,
                "mr_iid": None,
            },
            gitlab=fake,
            debug_runner=runner,
        )

        assert outcome == {"status": "skipped", "reason": "factory_branch_repair_loop"}
        runner.assert_not_awaited()
        assert fake.mr_notes == []
        assert fake.calls_of("list_pipeline_jobs") == []

    async def test_failure_without_an_open_mr_is_skipped(self):
        fake = FakeGitLab()  # no MRs at all
        runner = AsyncMock(return_value=debug_result())

        outcome = await execute_debug_pipeline_command(
            gitlab_settings(),
            None,
            None,
            {
                "command": "debug_pipeline",
                "project_id": PROJECT_ID,
                "pipeline_id": 200,
                "branch": "feature/auth",
                "sha": "b" * 40,
                "mr_iid": None,
            },
            gitlab=fake,
            debug_runner=runner,
        )

        assert outcome == {"status": "skipped", "reason": "no_associated_mr"}
        runner.assert_not_awaited()


# ----------------------------------------------------------------------
# Shared comment rendering
# ----------------------------------------------------------------------


def test_format_debug_comment_renders_analysis_and_actions():
    body = format_debug_comment(debug_result(), subject="job `build-and-test` on `ci`")
    assert "## \U0001f527 Forge CI Failure Analysis" in body
    assert "job `build-and-test` on `ci`" in body
    assert "**Root cause:**" in body
    assert "**Suggested fix:**" in body
    assert "1. Re-run the failed job after the fix" in body


def test_format_debug_comment_flags_flaky():
    result = debug_result()
    result = result.model_copy(update={"is_flaky": True})
    assert "*(likely flaky)*" in format_debug_comment(result, subject="job `j`")


# ----------------------------------------------------------------------
# Dispatch wiring (worker metadata → executor)
# ----------------------------------------------------------------------


class TestDispatch:
    async def test_debug_pipeline_metadata_dispatches_to_the_executor(self, monkeypatch):
        from forge.runs import execute_run_command

        seen: dict = {}

        async def fake_executor(settings, forge_config, session_factory, metadata, *, gitlab=None):
            seen["metadata"] = metadata

        monkeypatch.setattr("forge.reactive.ci_debug.execute_debug_pipeline_command", fake_executor)
        await execute_run_command(
            gitlab_settings(),
            None,
            None,
            {
                "command": "debug_pipeline",
                "project_id": PROJECT_ID,
                "pipeline_id": 200,
                "branch": "factory/7/abcd1234",  # executor skipped; dispatch asserted
                "sha": "",
                "mr_iid": None,
            },
        )

        assert seen["metadata"]["command"] == "debug_pipeline"

    async def test_debug_ci_metadata_dispatches_through_the_github_path(self, monkeypatch):
        from forge.runs import execute_run_command

        seen: dict = {}

        async def fake_executor(settings, forge_config, session_factory, metadata):
            seen["metadata"] = metadata

        monkeypatch.setattr("forge.reactive.ci_debug.execute_debug_ci_command", fake_executor)
        await execute_run_command(
            github_settings(),
            None,
            None,
            {
                "command": "debug_ci",
                "provider": "github",
                "repo_full_name": "acme/acme-widget",
                "head_sha": HEAD_SHA,
                "head_branch": "feature/x",
                "job_id": JOB_ID,
            },
        )

        assert seen["metadata"]["command"] == "debug_ci"
