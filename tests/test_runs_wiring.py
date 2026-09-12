"""Wiring tests: gateway /implement + /go cutover, worker dispatch, end-to-end."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select

from forge.config import Settings
from forge.database import reset_engine
from forge.durable import FlowRun, FlowStatus
from forge.main import create_app
from forge.runs import RunService
from forge.worker.tasks import create_run_command_task
from tests.fixtures.fake_gitlab import FakeGitLab, FakeGitLabClientFactory

TEST_SECRET = "test-secret-token"  # noqa: S105 — fake value for tests
PROJECT_ID = 42
ISSUE_IID = 5


def note_payload(note: str, *, username: str = "alice", issue: bool = True) -> dict:
    payload = {
        "object_kind": "note",
        "event_type": "note",
        "user": {"id": 11, "name": "Alice", "username": username},
        "project": {
            "id": PROJECT_ID,
            "name": "test",
            "path_with_namespace": "group/test",
            "web_url": "https://gitlab.test/group/test",
        },
        "object_attributes": {
            "id": 900,
            "note": note,
            "noteable_type": "Issue" if issue else "MergeRequest",
        },
    }
    if issue:
        payload["issue"] = {
            "id": ISSUE_IID,
            "iid": ISSUE_IID,
            "title": "Add a widget",
            "state": "opened",
        }
    else:
        payload["merge_request"] = {
            "iid": 3,
            "title": "An MR",
            "source_branch": "f",
            "target_branch": "main",
        }
    return payload


def webhook_headers() -> dict[str, str]:
    return {"X-Gitlab-Token": TEST_SECRET, "X-Gitlab-Event": "Note Hook"}


def run_settings(tmp_path, **overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test"),
        GITLAB_WEBHOOK_SECRET=SecretStr(TEST_SECRET),
        FORGE_APPROVERS="alice",
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/forge.db",
        LITELLM_URL="http://litellm:4000",
        # Pin the environment explicitly — a developer .env must not leak in.
        REDIS_URL=None,
        FORGE_CAPTURE_DIR=None,
        FORGE_BOT_USERNAME="forge-bot",
    )
    values.update(overrides)
    return Settings(**values)


class TestGatewayRouting:
    @pytest.fixture()
    async def app(self, tmp_path):
        reset_engine()
        application = create_app(settings=run_settings(tmp_path))
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

    async def test_implement_note_on_issue_becomes_run_command_task(self, app, client):
        resp = await client.post(
            "/webhook", json=note_payload("@forge /implement"), headers=webhook_headers()
        )

        assert resp.status_code == 202
        assert resp.json()["run_command"] is True
        (task,) = app.state.task_queue.submit.await_args[0]
        assert task.task_type == "run_command"
        assert task.metadata["command"] == "start_run"
        assert task.metadata["project_id"] == PROJECT_ID
        assert task.metadata["issue_iid"] == ISSUE_IID
        assert task.metadata["author_username"] == "alice"

    async def test_go_note_becomes_go_run_command_task(self, app, client):
        run_id = "a" * 32
        resp = await client.post(
            "/webhook",
            json=note_payload(f"@forge /go {run_id}"),
            headers=webhook_headers(),
        )

        assert resp.json()["run_command"] is True
        (task,) = app.state.task_queue.submit.await_args[0]
        assert task.task_type == "run_command"
        assert task.metadata["command"] == "go"
        assert run_id in task.metadata["note_text"]

    async def test_duplicate_implement_note_is_deduplicated(self, app, client):
        app.state.task_queue.is_duplicate = AsyncMock(return_value=True)
        resp = await client.post(
            "/webhook", json=note_payload("@forge /implement"), headers=webhook_headers()
        )
        assert resp.json()["deduplicated"] is True
        app.state.task_queue.submit.assert_not_awaited()

    async def test_plain_mention_note_keeps_legacy_path(self, app, client):
        """Notes without a run command stay on the orchestrator event path."""
        resp = await client.post(
            "/webhook", json=note_payload("@forge explain this"), headers=webhook_headers()
        )
        assert resp.json().get("run_command") is None
        (task,) = app.state.task_queue.submit.await_args[0]
        assert task.task_type == "event"

    async def test_implement_note_on_mr_keeps_legacy_path(self, app, client):
        resp = await client.post(
            "/webhook",
            json=note_payload("@forge /implement", issue=False),
            headers=webhook_headers(),
        )
        assert resp.json().get("run_command") is None
        (task,) = app.state.task_queue.submit.await_args[0]
        assert task.task_type == "event"

    async def test_mention_pattern_is_respected(self, app, client):
        """Only the configured mention trigger starts a run."""
        resp = await client.post(
            "/webhook", json=note_payload("@otherbot /implement"), headers=webhook_headers()
        )
        assert resp.json().get("run_command") is None

    async def test_bot_author_is_skipped_before_routing(self, app, client):
        resp = await client.post(
            "/webhook",
            json=note_payload("@forge /implement", username="forge-bot"),
            headers=webhook_headers(),
        )
        assert resp.json()["status"] == "skipped"
        app.state.task_queue.submit.assert_not_awaited()

    async def test_run_command_task_helper_metadata(self):
        task = create_run_command_task({"command": "start_run", "project_id": 1}, note_id=77)
        assert task.task_type == "run_command"
        assert task.task_id == "run:start_run:1:0:77"


class TestWorkerDispatch:
    async def test_worker_handles_run_command_task(self, monkeypatch):
        from forge.worker.app import run_worker

        executed: list[dict] = []

        async def fake_execute(settings, forge_config, session_factory, metadata):
            executed.append(metadata)

        monkeypatch.setattr("forge.worker.app.execute_run_command", fake_execute)

        task = create_run_command_task({"command": "go", "project_id": 1}, note_id=5)
        queue = AsyncMock()
        state = {"claimed": False}

        async def claim(*args, **kwargs):
            if not state["claimed"]:
                state["claimed"] = True
                return task
            await asyncio.sleep(0.05)
            return None

        queue.claim = AsyncMock(side_effect=claim)
        shutdown = asyncio.Event()
        queue.complete = AsyncMock(side_effect=lambda t: shutdown.set())

        await run_worker("w", object(), object(), object(), object(), queue, shutdown)

        assert executed == [{"command": "go", "project_id": 1}]
        queue.complete.assert_awaited_once_with(task)

    async def test_worker_fails_run_command_on_error(self, monkeypatch):
        from forge.worker.app import run_worker

        async def failing_execute(*args):
            raise RuntimeError("gitlab down")

        monkeypatch.setattr("forge.worker.app.execute_run_command", failing_execute)

        task = create_run_command_task({"command": "go", "project_id": 1}, note_id=5)
        queue = AsyncMock()

        state = {"claimed": False}

        async def claim(*args, **kwargs):
            if not state["claimed"]:
                state["claimed"] = True
                return task
            await asyncio.sleep(0.05)
            return None

        queue.claim = AsyncMock(side_effect=claim)
        shutdown = asyncio.Event()
        queue.fail = AsyncMock(side_effect=lambda t, e: shutdown.set())

        await run_worker("w", object(), object(), object(), object(), queue, shutdown)
        queue.fail.assert_awaited_once()


class TestEndToEnd:
    @pytest.fixture()
    async def app(self, tmp_path):
        reset_engine()
        application = create_app(settings=run_settings(tmp_path))
        async with application.router.lifespan_context(application):
            yield application
        reset_engine()

    @pytest.fixture()
    async def client(self, app) -> AsyncClient:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    async def test_issue_note_to_gate_to_mr_to_ready(self, app, client, monkeypatch):
        """issue /implement → gate /go → waiting_ci → reconciler → ready_for_human."""
        fake = FakeGitLab()
        fake.seed_issue(ISSUE_IID, "Add a widget", "Make widgets real.")
        fake.seed_commit("main", "base-sha-1", "initial")
        factory = FakeGitLabClientFactory(shared=fake)
        monkeypatch.setattr("forge.runs.service.GitLabClient", factory)

        # 1. @forge /implement on the issue (no Redis → BackgroundTask fallback).
        resp = await client.post(
            "/webhook", json=note_payload("@forge /implement"), headers=webhook_headers()
        )
        assert resp.status_code == 202
        assert resp.json()["run_command"] is True
        assert fake.notes, "plan comment posted"

        session_factory = app.state.session_factory
        async with session_factory() as session:
            runs = (await session.execute(select(FlowRun))).scalars().all()
        assert len(runs) == 1
        run = runs[0]
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        run_id = run.id
        assert f"@forge /go {run_id}" in fake.notes[0]["body"]

        # 2. Non-approver /go is ignored.
        resp = await client.post(
            "/webhook",
            json=note_payload(f"@forge /go {run_id}", username="mallory"),
            headers=webhook_headers(),
        )
        async with session_factory() as session:
            run = await session.get(FlowRun, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value

        # 3. Approver /go drives stub propose → commit → Draft MR → waiting_ci.
        resp = await client.post(
            "/webhook",
            json=note_payload(f"@forge /go {run_id}", username="alice"),
            headers=webhook_headers(),
        )
        async with session_factory() as session:
            run = await session.get(FlowRun, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        commit_sha = run.candidate_shas[-1]
        assert run.mr_iid is not None
        (mr,) = fake.merge_requests.values()
        assert mr["title"] == "Draft: Add a widget"

        # 4. CI succeeds for the exact candidate sha → reconciler finishes it.
        from forge.runs.stubs import factory_branch

        branch = factory_branch(ISSUE_IID, run_id)
        assert fake.branches[branch][0]["sha"] == commit_sha  # exact-SHA correlation
        pipeline_id = (await fake.create_pipeline(PROJECT_ID, branch))["id"]
        fake.set_pipeline_status(pipeline_id, "success", commit_sha)

        service = RunService(
            session_factory=session_factory, gitlab=fake, settings=app.state.settings
        )
        await service.evaluate_waiting_ci()

        async with session_factory() as session:
            run = await session.get(FlowRun, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        assert fake.notes_containing(commit_sha), "evidence comment posted"


class TestBotAuthorGate:
    """Forge's own comments contain /go lines — a bot-authored note must
    never act as a trigger or an approval (live acceptance found forge
    self-approving when it posted with a human admin token)."""

    async def test_bot_authored_go_note_is_ignored(self, app, client):
        resp = await client.post(
            "/webhook",
            json=note_payload(f"@forge /go {'b' * 32}", username="forge-bot"),
            headers=webhook_headers(),
        )
        assert resp.json().get("run_command") is None
        (task,) = app.state.task_queue.submit.await_args[0]
        assert task.task_type == "event"  # legacy path, not a run command

    async def test_bot_authored_implement_note_is_ignored(self, app, client):
        resp = await client.post(
            "/webhook",
            json=note_payload("@forge /implement", username="forge-bot"),
            headers=webhook_headers(),
        )
        assert resp.json().get("run_command") is None
        (task,) = app.state.task_queue.submit.await_args[0]
        assert task.task_type == "event"

    def test_forge_token_prefers_bot_identity(self, tmp_path):
        from forge.runs.service import forge_token

        settings = run_settings(tmp_path)
        assert forge_token(settings) == "glpat-test"  # fallback: GITLAB_TOKEN
        settings.FORGE_BOT_TOKEN = SecretStr("glpat-bot")
        assert forge_token(settings) == "glpat-bot"
