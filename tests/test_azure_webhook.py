"""Azure DevOps webhook ingress tests: Basic validation, fail-closed, routing.

Exercises the durable ingestion path (inbox row + scheduled step in ONE
transaction, ADR-0017 §1 semantics) over the sqlite harness — the GitHub
ingress tests' twin (ADR-0024 AZ-2). Azure service hooks have no HMAC: the
Basic credentials ARE the authenticator, so the auth matrix replaces the
signature matrix. The task queue is an AsyncMock, exactly like the GitHub
wiring tests, so the wake-up accelerator is observed without executing the
step.
"""

import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select

from forge.config import Settings
from forge.database import reset_engine
from forge.durable import EventInbox, StepRun
from forge.main import create_app

FIXTURES = Path(__file__).parent / "fixtures" / "azure_payloads"
WEBHOOK_USERNAME = "forge-hooks"
WEBHOOK_PASSWORD = "hook-pass"  # noqa: S105 — fake value for tests

ORG_URL = "https://dev.azure.com/fabrikam"
PROJECT = "Fabrikam"
PROJECT_GUID = "9f8e7d6c-0000-0000-0000-000000000009"
WORK_ITEM = 142
RUN_ID = "6f24f6a1b7c34d2e8a9012345678abcd"


def load_payload(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def load_json(name: str) -> dict:
    return json.loads(load_payload(name))


def basic_header(username: str = WEBHOOK_USERNAME, password: str = WEBHOOK_PASSWORD) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


def auth_headers(
    username: str = WEBHOOK_USERNAME, password: str = WEBHOOK_PASSWORD
) -> dict[str, str]:
    return {"Content-Type": "application/json", "Authorization": basic_header(username, password)}


def azure_settings(tmp_path, **overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test"),
        GITLAB_WEBHOOK_SECRET=SecretStr("test-secret-token"),
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/azure-webhook.db",
        LITELLM_URL="http://litellm:4000",
        REDIS_URL=None,
        FORGE_CAPTURE_DIR=None,
        FORGE_BOT_TOKEN=None,
        FORGE_BOT_USERNAME="forge-bot",
        # Azure DevOps slice settings (ADR-0024)
        FORGE_AZDO_ENABLED=True,
        FORGE_AZDO_ORG_URL=ORG_URL,
        FORGE_AZDO_WEBHOOK_USERNAME=WEBHOOK_USERNAME,
        FORGE_AZDO_WEBHOOK_PASSWORD=SecretStr(WEBHOOK_PASSWORD),
        FORGE_AZDO_BOT_NAME="forge-bot",
        FORGE_AZDO_APPROVERS="dev@fabrikam.example",
    )
    values.update(overrides)
    return Settings(**values)


class TestFailClosed:
    @pytest.fixture()
    async def disabled_client(self, tmp_path):
        reset_engine()
        application = create_app(settings=azure_settings(tmp_path, FORGE_AZDO_ENABLED=False))
        async with application.router.lifespan_context(application):
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                yield ac
        reset_engine()

    async def test_disabled_returns_503(self, disabled_client: AsyncClient):
        body = load_payload("push_normal.json")
        response = await disabled_client.post(
            "/webhook/azure_devops", content=body, headers=auth_headers()
        )
        assert response.status_code == 503
        assert response.json() == {"error": "azure devops ingress disabled"}

    async def test_missing_username_returns_503(self, tmp_path):
        reset_engine()
        application = create_app(settings=azure_settings(tmp_path, FORGE_AZDO_WEBHOOK_USERNAME=""))
        try:
            async with application.router.lifespan_context(application):
                transport = ASGITransport(app=application)
                async with AsyncClient(transport=transport, base_url="http://test") as ac:
                    body = load_payload("push_normal.json")
                    response = await ac.post(
                        "/webhook/azure_devops", content=body, headers=auth_headers()
                    )
                    assert response.status_code == 503
        finally:
            reset_engine()

    async def test_missing_password_returns_503(self, tmp_path):
        reset_engine()
        application = create_app(
            settings=azure_settings(tmp_path, FORGE_AZDO_WEBHOOK_PASSWORD=None)
        )
        try:
            async with application.router.lifespan_context(application):
                transport = ASGITransport(app=application)
                async with AsyncClient(transport=transport, base_url="http://test") as ac:
                    body = load_payload("push_normal.json")
                    response = await ac.post(
                        "/webhook/azure_devops", content=body, headers=auth_headers()
                    )
                    assert response.status_code == 503
        finally:
            reset_engine()


class TestBasicAuth:
    """Azure webhooks have NO HMAC (research §2.0) — the Basic pair is the
    whole authenticator, checked in constant time."""

    @pytest.fixture()
    async def app(self, tmp_path):
        reset_engine()
        application = create_app(settings=azure_settings(tmp_path))
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

    async def test_missing_authorization_rejected(self, client: AsyncClient):
        body = load_payload("push_normal.json")
        response = await client.post(
            "/webhook/azure_devops",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 401

    async def test_wrong_password_rejected(self, client: AsyncClient):
        body = load_payload("push_normal.json")
        response = await client.post(
            "/webhook/azure_devops", content=body, headers=auth_headers(password="nope")
        )
        assert response.status_code == 401

    async def test_wrong_username_rejected(self, client: AsyncClient):
        body = load_payload("push_normal.json")
        response = await client.post(
            "/webhook/azure_devops", content=body, headers=auth_headers(username="someone-else")
        )
        assert response.status_code == 401

    async def test_non_basic_scheme_rejected(self, client: AsyncClient):
        body = load_payload("push_normal.json")
        token = base64.b64encode(f"{WEBHOOK_USERNAME}:{WEBHOOK_PASSWORD}".encode()).decode()
        response = await client.post(
            "/webhook/azure_devops",
            content=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401

    async def test_undecodable_credentials_rejected(self, client: AsyncClient):
        body = load_payload("push_normal.json")
        response = await client.post(
            "/webhook/azure_devops",
            content=body,
            headers={"Content-Type": "application/json", "Authorization": "Basic %%%not-base64"},
        )
        assert response.status_code == 401

    async def test_credential_without_separator_rejected(self, client: AsyncClient):
        body = load_payload("push_normal.json")
        blob = base64.b64encode(WEBHOOK_PASSWORD.encode()).decode()  # no "user:pass" colon
        response = await client.post(
            "/webhook/azure_devops",
            content=body,
            headers={"Content-Type": "application/json", "Authorization": f"Basic {blob}"},
        )
        assert response.status_code == 401

    async def test_good_credentials_accepted(self, app, client: AsyncClient):
        body = load_payload("push_normal.json")
        response = await client.post("/webhook/azure_devops", content=body, headers=auth_headers())
        assert response.status_code == 202
        assert response.json()["recorded"] is True  # git.push is inbox-only

    async def test_unit_rejects_edge_shapes(self):
        from forge.gateway.azure_webhook import verify_azure_basic_auth

        assert not verify_azure_basic_auth("u", "p", None)
        assert not verify_azure_basic_auth("", "p", basic_header())
        assert not verify_azure_basic_auth("u", "", basic_header())
        assert not verify_azure_basic_auth("u", "p", "Digest abc")
        assert not verify_azure_basic_auth("u", "p", "Basic")
        assert verify_azure_basic_auth("u", "p", basic_header("u", "p"))


class TestWorkitemCommands:
    @pytest.fixture()
    async def app(self, tmp_path):
        reset_engine()
        application = create_app(settings=azure_settings(tmp_path))
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

    async def post(self, client, app, name: str):
        body = load_payload(name)
        response = await client.post("/webhook/azure_devops", content=body, headers=auth_headers())
        async with app.state.session_factory() as session:
            inbox = list((await session.execute(select(EventInbox))).scalars().all())
            steps = list((await session.execute(select(StepRun))).scalars().all())
        return response, inbox, steps

    async def test_implement_comment_persists_inbox_and_step(self, app, client: AsyncClient):
        response, inbox, steps = await self.post(client, app, "workitem_commented_implement.json")

        assert response.status_code == 202
        assert response.json()["run_command"] is True
        assert len(inbox) == 1 and len(steps) == 1
        payload = inbox[0].payload
        assert payload["provider"] == "azure_devops"
        assert payload["command"] == "start_run"
        assert payload["connection_id"] == f"azure_devops:{ORG_URL}:{PROJECT}"
        assert payload["project"] == PROJECT
        assert payload["project_id"] == payload["project_id"]  # deterministic int key
        assert payload["repo_full_name"] == ""  # resolved lazily by the run service
        assert payload["issue_number"] == WORK_ITEM
        assert payload["issue_is_pr"] is False
        assert payload["author_username"] == "dev@fabrikam.example"
        assert payload["note_text"] == "/implement please focus on the retry path"
        assert payload["note_id"] == f"workitem:{WORK_ITEM}:comment:9"
        # Connection-scoped identity: inbox id and step are bound together.
        assert steps[0].source_event_id == inbox[0].source_event_id
        assert steps[0].status == "scheduled"
        # The wake task is stamped with the ACTUAL inbox identity (the
        # 64243ff rule) — never a recomputed hash.
        (task,) = app.state.task_queue.submit.await_args[0]
        assert task.task_type == "run_command"
        assert task.metadata["provider"] == "azure_devops"
        assert task.metadata["source_event_id"] == inbox[0].source_event_id

    async def test_go_command_from_workitem_comment(self, app, client: AsyncClient):
        _, inbox, _ = await self.post(client, app, "workitem_commented_go.json")
        assert inbox[0].payload["command"] == "go"
        assert inbox[0].payload["note_id"] == f"workitem:{WORK_ITEM}:comment:10"

    async def test_cancel_command_via_mention_form(self, app, client: AsyncClient):
        payload = load_json("workitem_commented_go.json")
        payload["resource"]["fields"]["System.History"] = f"@forge /cancel {RUN_ID}"
        body = json.dumps(payload).encode()
        response = await client.post("/webhook/azure_devops", content=body, headers=auth_headers())

        assert response.status_code == 202
        async with app.state.session_factory() as session:
            inbox = list((await session.execute(select(EventInbox))).scalars().all())
        assert inbox[0].payload["command"] == "cancel"

    async def test_security_command_from_workitem_comment(self, app, client: AsyncClient):
        payload = load_json("workitem_commented_implement.json")
        payload["resource"]["fields"]["System.History"] = "/security"
        body = json.dumps(payload).encode()
        response = await client.post("/webhook/azure_devops", content=body, headers=auth_headers())

        assert response.status_code == 202
        async with app.state.session_factory() as session:
            inbox = list((await session.execute(select(EventInbox))).scalars().all())
        assert inbox[0].payload["command"] == "security_triage"

    async def test_bot_comment_is_skipped(self, app, client: AsyncClient):
        payload = load_json("workitem_commented_implement.json")
        payload["resource"]["fields"]["System.ChangedBy"] = "forge-bot@fabrikam.example"
        body = json.dumps(payload).encode()
        response = await client.post("/webhook/azure_devops", content=body, headers=auth_headers())

        assert response.json() == {"status": "skipped", "reason": "bot-loop"}
        async with app.state.session_factory() as session:
            inbox = (await session.execute(select(EventInbox))).scalars().all()
        assert inbox == []

    async def test_bot_display_name_is_also_skipped(self, app, client: AsyncClient):
        payload = load_json("workitem_commented_implement.json")
        payload["resource"]["fields"]["System.ChangedBy"] = {"displayName": "forge-bot"}
        body = json.dumps(payload).encode()
        response = await client.post("/webhook/azure_devops", content=body, headers=auth_headers())
        assert response.json() == {"status": "skipped", "reason": "bot-loop"}

    async def test_non_command_comment_recorded_without_step(self, app, client: AsyncClient):
        payload = load_json("workitem_commented_implement.json")
        payload["resource"]["fields"]["System.History"] = "Looks great, thanks!"
        body = json.dumps(payload).encode()
        response = await client.post("/webhook/azure_devops", content=body, headers=auth_headers())

        assert response.status_code == 202
        assert response.json() == {
            "status": "accepted",
            "event": "workitem.commented",
            "recorded": True,
        }
        async with app.state.session_factory() as session:
            inbox = (await session.execute(select(EventInbox))).scalars().all()
            steps = (await session.execute(select(StepRun))).scalars().all()
        assert len(inbox) == 1
        assert inbox[0].event_type == "azure_devops:workitem.commented"
        assert steps == []

    async def test_redelivered_command_is_deduplicated(self, app, client: AsyncClient):
        first, inbox, steps = await self.post(client, app, "workitem_commented_implement.json")
        body = load_payload("workitem_commented_implement.json")
        second = await client.post("/webhook/azure_devops", content=body, headers=auth_headers())

        assert first.json().get("run_command") is True
        assert second.json() == {
            "status": "accepted",
            "event": "workitem.commented",
            "deduplicated": True,
        }
        assert len(inbox) == 1
        # The re-delivery created no second step row.
        async with app.state.session_factory() as session:
            all_steps = (await session.execute(select(StepRun))).scalars().all()
        assert len(all_steps) == 1

    async def test_publisher_id_instability_does_not_change_routing(self, app, client: AsyncClient):
        """publisherId is unstable (tfs AND azure-devops both documented) —
        routing keys off eventType only."""
        payload = load_json("workitem_commented_implement.json")
        payload["publisherId"] = "azure-devops"
        body = json.dumps(payload).encode()
        response = await client.post("/webhook/azure_devops", content=body, headers=auth_headers())
        assert response.json()["run_command"] is True


class TestWorkitemUpdated:
    """#29 lifecycle commands off ``workitem.updated``: edit → issue_edited,
    trigger tag gone → unlabeled (the detection's honest limits are pinned
    in the unit tests)."""

    @pytest.fixture()
    async def app(self, tmp_path):
        reset_engine()
        application = create_app(settings=azure_settings(tmp_path))
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

    async def post(self, client, app, name: str):
        body = load_payload(name)
        response = await client.post("/webhook/azure_devops", content=body, headers=auth_headers())
        async with app.state.session_factory() as session:
            inbox = list((await session.execute(select(EventInbox))).scalars().all())
            steps = list((await session.execute(select(StepRun))).scalars().all())
        return response, inbox, steps

    async def test_text_edit_normalizes_to_issue_edited(self, app, client: AsyncClient):
        response, inbox, steps = await self.post(client, app, "workitem_updated_edited.json")

        assert response.status_code == 202
        assert response.json()["run_command"] is True
        assert len(inbox) == 1 and len(steps) == 1
        payload = inbox[0].payload
        assert payload["command"] == "issue_edited"
        assert payload["provider"] == "azure_devops"
        assert payload["connection_id"] == f"azure_devops:{ORG_URL}:{PROJECT}"
        assert payload["repo_full_name"] == ""  # resolved lazily by the run service
        assert payload["issue_number"] == WORK_ITEM
        assert payload["issue_is_pr"] is False
        assert payload["issue_title"] == "Ship the flux capacitor"
        # System.Description travels RAW (HTML) — the run service strips it
        # before digesting, the same way start_run freezes the snapshot.
        assert "<p>" in payload["issue_body"]
        assert payload["issue_title_present"] is True
        assert payload["issue_description_present"] is True
        assert payload["author_username"] == "dev@fabrikam.example"
        assert payload["note_id"].startswith(f"edit:{WORK_ITEM}:")
        # The wake task is stamped with the ACTUAL inbox identity.
        assert steps[0].source_event_id == inbox[0].source_event_id
        assert steps[0].status == "scheduled"
        (task,) = app.state.task_queue.submit.await_args[0]
        assert task.task_type == "run_command"
        assert task.metadata["command"] == "issue_edited"
        assert task.metadata["source_event_id"] == inbox[0].source_event_id

    async def test_trigger_tag_absent_normalizes_to_unlabeled(self, app, client: AsyncClient):
        response, inbox, _ = await self.post(client, app, "workitem_updated_tag_removed.json")

        assert response.status_code == 202
        assert response.json()["run_command"] is True
        payload = inbox[0].payload
        assert payload["command"] == "unlabeled"
        assert payload["issue_number"] == WORK_ITEM
        assert payload["tags"] == ["ci-cleanup"]
        assert payload["note_id"].startswith(f"unlabel:{WORK_ITEM}:")

    async def test_trigger_tag_present_normalizes_to_issue_edited(self, app, client: AsyncClient):
        """The tag set carrying the trigger label is NOT a removal — the
        delivery is an edit."""
        payload = load_json("workitem_updated_edited.json")
        payload["resource"]["fields"]["System.Tags"] = "forge; ci-cleanup"
        body = json.dumps(payload).encode()
        resp = await client.post("/webhook/azure_devops", content=body, headers=auth_headers())

        assert resp.status_code == 202
        async with app.state.session_factory() as session:
            inbox = list((await session.execute(select(EventInbox))).scalars().all())
        assert inbox[0].payload["command"] == "issue_edited"

    async def test_case_insensitive_trigger_label_match(self, app, client: AsyncClient):
        payload = load_json("workitem_updated_edited.json")
        payload["resource"]["fields"]["System.Tags"] = "Forge"
        body = json.dumps(payload).encode()
        await client.post("/webhook/azure_devops", content=body, headers=auth_headers())

        async with app.state.session_factory() as session:
            inbox = list((await session.execute(select(EventInbox))).scalars().all())
        assert inbox[0].payload["command"] == "issue_edited"

    async def test_no_tags_field_is_an_edit_not_an_unlabel(self, app, client: AsyncClient):
        """A work item without tags never routes unlabeled (the fixture's
        System.Tags key is dropped, not emptied)."""
        payload = load_json("workitem_updated_tag_removed.json")
        del payload["resource"]["fields"]["System.Tags"]
        body = json.dumps(payload).encode()
        await client.post("/webhook/azure_devops", content=body, headers=auth_headers())

        async with app.state.session_factory() as session:
            inbox = list((await session.execute(select(EventInbox))).scalars().all())
        assert inbox[0].payload["command"] == "issue_edited"
        assert inbox[0].payload["issue_description_present"] is True

    async def test_bot_update_is_skipped(self, app, client: AsyncClient):
        payload = load_json("workitem_updated_edited.json")
        payload["resource"]["fields"]["System.ChangedBy"] = "forge-bot@fabrikam.example"
        body = json.dumps(payload).encode()
        response = await client.post("/webhook/azure_devops", content=body, headers=auth_headers())

        assert response.json() == {"status": "skipped", "reason": "bot-loop"}
        async with app.state.session_factory() as session:
            inbox = (await session.execute(select(EventInbox))).scalars().all()
        assert inbox == []

    async def test_redelivered_update_is_deduplicated(self, app, client: AsyncClient):
        first, inbox, _ = await self.post(client, app, "workitem_updated_edited.json")
        body = load_payload("workitem_updated_edited.json")
        second = await client.post("/webhook/azure_devops", content=body, headers=auth_headers())

        assert first.json().get("run_command") is True
        assert second.json()["deduplicated"] is True
        assert len(inbox) == 1

    async def test_a_new_edit_is_not_swallowed_by_the_first(self, app, client: AsyncClient):
        """The A→B→A inbox-collision guard: two genuinely different updates
        of one work item must each schedule their step (the rev bumps)."""
        await self.post(client, app, "workitem_updated_edited.json")
        payload = load_json("workitem_updated_edited.json")
        payload["resource"]["rev"] = 14
        payload["resource"]["fields"]["System.Title"] = "Ship the flux capacitor, fast"
        body = json.dumps(payload).encode()
        await client.post("/webhook/azure_devops", content=body, headers=auth_headers())

        async with app.state.session_factory() as session:
            inbox = list((await session.execute(select(EventInbox))).scalars().all())
            steps = list((await session.execute(select(StepRun))).scalars().all())
        assert len(inbox) == 2
        assert len(steps) == 2
        assert {step.source_event_id for step in steps} == {row.source_event_id for row in inbox}


class TestPRCommentCommands:
    @pytest.fixture()
    async def app(self, tmp_path):
        reset_engine()
        application = create_app(settings=azure_settings(tmp_path))
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

    async def test_implement_on_a_human_pr_routes_a_command(self, app, client: AsyncClient):
        body = load_payload("pr_commented_on_implement.json")
        response = await client.post("/webhook/azure_devops", content=body, headers=auth_headers())

        assert response.status_code == 202
        assert response.json()["run_command"] is True
        async with app.state.session_factory() as session:
            inbox = list((await session.execute(select(EventInbox))).scalars().all())
            steps = list((await session.execute(select(StepRun))).scalars().all())
        assert len(inbox) == 1 and len(steps) == 1
        payload = inbox[0].payload
        assert payload["command"] == "start_run"
        assert payload["issue_is_pr"] is True
        assert payload["pr_id"] == 513
        assert payload["head_branch"] == "dev/topic"
        assert payload["repo_full_name"] == "Fabrikam/core"
        assert payload["author_username"] == "dev@fabrikam.example"
        assert payload["note_id"] == "pr:513:comment:302"
        assert steps[0].source_event_id == inbox[0].source_event_id

    async def test_cancel_on_forges_own_pr_is_inbox_only(self, app, client: AsyncClient):
        """The fixture PR's head is forge/wi-42 (forge's own output) — the
        forge/* head-branch guard keeps it from acting as a trigger."""
        body = load_payload("pr_commented_on.json")
        response = await client.post("/webhook/azure_devops", content=body, headers=auth_headers())

        assert response.json() == {
            "status": "accepted",
            "event": "ms.vss-code.git-pullrequest-comment-event",
            "recorded": True,
        }
        async with app.state.session_factory() as session:
            inbox = (await session.execute(select(EventInbox))).scalars().all()
            steps = (await session.execute(select(StepRun))).scalars().all()
        assert len(inbox) == 1 and steps == []

    async def test_bot_pr_comment_is_skipped(self, app, client: AsyncClient):
        payload = load_json("pr_commented_on_implement.json")
        payload["resource"]["comment"]["author"]["uniqueName"] = "forge-bot@fabrikam.example"
        body = json.dumps(payload).encode()
        response = await client.post("/webhook/azure_devops", content=body, headers=auth_headers())
        assert response.json() == {"status": "skipped", "reason": "bot-loop"}


class TestPRReviewEvents:
    @pytest.fixture()
    async def app(self, tmp_path):
        reset_engine()
        application = create_app(settings=azure_settings(tmp_path))
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

    def humanized_pr(self, name: str) -> dict:
        payload = load_json(name)
        payload["resource"]["createdBy"] = {
            "id": "cc33dd44-0000-0000-0000-0000000000cc",
            "displayName": "Dev User",
            "uniqueName": "dev@fabrikam.example",
        }
        payload["resource"]["sourceRefName"] = "refs/heads/dev/topic"
        return payload

    async def test_pr_created_normalizes_to_review_pr(self, app, client: AsyncClient):
        body = json.dumps(self.humanized_pr("pr_created.json")).encode()
        response = await client.post("/webhook/azure_devops", content=body, headers=auth_headers())

        assert response.status_code == 202
        assert response.json()["run_command"] is True
        async with app.state.session_factory() as session:
            inbox = list((await session.execute(select(EventInbox))).scalars().all())
            steps = list((await session.execute(select(StepRun))).scalars().all())
        assert len(inbox) == 1 and len(steps) == 1
        payload = inbox[0].payload
        assert payload["command"] == "review_pr"
        assert payload["provider"] == "azure_devops"
        assert payload["pr_id"] == 512
        assert payload["action"] == "git.pullrequest.created"
        # The review lane's contract: the FULL ref (the engine strips it).
        assert payload["head_branch"] == "refs/heads/dev/topic"
        assert payload["after_sha"] == "b47e09d3c5a28f16e0d9a4c7138b52fa60e7d831"
        assert payload["before_sha"] == ""  # the delta comes from iterations
        assert payload["repo_full_name"] == "Fabrikam/core"
        assert payload["delivery_key"].startswith("pr:512:git.pullrequest.created:")
        assert steps[0].source_event_id == inbox[0].source_event_id

    async def test_pr_updated_normalizes_to_review_pr_with_new_head(self, app, client: AsyncClient):
        payload = self.humanized_pr("pr_updated_push.json")
        payload["eventType"] = "git.pullrequest.updated"
        body = json.dumps(payload).encode()
        response = await client.post("/webhook/azure_devops", content=body, headers=auth_headers())

        assert response.json()["run_command"] is True
        async with app.state.session_factory() as session:
            inbox = list((await session.execute(select(EventInbox))).scalars().all())
        payload = inbox[0].payload
        assert payload["command"] == "review_pr"
        assert payload["action"] == "git.pullrequest.updated"
        assert payload["after_sha"] == "e6b42d90a1c7583f0d8e4a6b29c17f5308da2b47"
        assert payload["delivery_key"] == (
            "pr:512:git.pullrequest.updated:e6b42d90a1c7583f0d8e4a6b29c17f5308da2b47"
        )

    async def test_forges_own_pr_creation_is_inbox_only(self, app, client: AsyncClient):
        """A human-authored PR whose head is forge/wi-42 (forge's own output)
        is recorded, never routed — reviewing it would self-trigger."""
        payload = load_json("pr_created.json")
        payload["resource"]["createdBy"] = {
            "id": "cc33dd44-0000-0000-0000-0000000000cc",
            "displayName": "Dev User",
            "uniqueName": "dev@fabrikam.example",
        }
        body = json.dumps(payload).encode()
        response = await client.post("/webhook/azure_devops", content=body, headers=auth_headers())

        assert response.json()["recorded"] is True
        async with app.state.session_factory() as session:
            steps = (await session.execute(select(StepRun))).scalars().all()
        assert steps == []

    async def test_bot_created_pr_is_skipped(self, app, client: AsyncClient):
        payload = self.humanized_pr("pr_created.json")
        payload["resource"]["createdBy"]["uniqueName"] = "forge-bot@fabrikam.example"
        body = json.dumps(payload).encode()
        response = await client.post("/webhook/azure_devops", content=body, headers=auth_headers())
        assert response.json() == {"status": "skipped", "reason": "bot-loop"}


class TestBuildComplete:
    @pytest.fixture()
    async def app(self, tmp_path):
        reset_engine()
        application = create_app(settings=azure_settings(tmp_path))
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

    async def test_failed_build_normalizes_to_debug_ci(self, app, client: AsyncClient):
        body = load_payload("build_complete_failed.json")
        response = await client.post("/webhook/azure_devops", content=body, headers=auth_headers())

        assert response.status_code == 202
        assert response.json()["run_command"] is True
        async with app.state.session_factory() as session:
            inbox = list((await session.execute(select(EventInbox))).scalars().all())
            steps = list((await session.execute(select(StepRun))).scalars().all())
        assert len(inbox) == 1 and len(steps) == 1
        payload = inbox[0].payload
        assert payload["command"] == "debug_ci"
        assert payload["build_id"] == 88231
        assert payload["definition_id"] == 207
        assert payload["pipeline_name"] == "core-ci"
        assert payload["head_sha"] == "c94f2e6b08d13a57b9e0c4a827f36d1059be4d70"
        assert payload["source_version"] == payload["head_sha"]
        assert payload["result"] == "failed"
        assert payload["note_id"] == "build:88231:failed"
        assert steps[0].source_event_id == inbox[0].source_event_id

    async def test_forge_lane_failure_is_skipped(self, tmp_path):
        """The lane pipeline's own builds have their own triage — debugging
        them here would recurse into forge's own output."""
        reset_engine()
        application = create_app(settings=azure_settings(tmp_path, FORGE_AZDO_LANE_PIPELINE_ID=207))
        try:
            async with application.router.lifespan_context(application):
                transport = ASGITransport(app=application)
                async with AsyncClient(transport=transport, base_url="http://test") as ac:
                    body = load_payload("build_complete_failed.json")
                    response = await ac.post(
                        "/webhook/azure_devops", content=body, headers=auth_headers()
                    )
                    assert response.json()["recorded"] is True
                    async with application.state.session_factory() as session:
                        steps = (await session.execute(select(StepRun))).scalars().all()
                    assert steps == []
        finally:
            reset_engine()

    async def test_succeeded_build_is_inbox_only(self, app, client: AsyncClient):
        body = load_payload("build_complete_succeeded.json")
        response = await client.post("/webhook/azure_devops", content=body, headers=auth_headers())

        assert response.json() == {
            "status": "accepted",
            "event": "build.complete",
            "recorded": True,
        }
        async with app.state.session_factory() as session:
            steps = (await session.execute(select(StepRun))).scalars().all()
        assert steps == []


class TestInboxOnlyAndDedupe:
    @pytest.fixture()
    async def app(self, tmp_path):
        reset_engine()
        application = create_app(settings=azure_settings(tmp_path))
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

    async def test_push_event_recorded_inbox_only(self, app, client: AsyncClient):
        body = load_payload("push_normal.json")
        response = await client.post("/webhook/azure_devops", content=body, headers=auth_headers())

        assert response.status_code == 202
        async with app.state.session_factory() as session:
            inbox = (await session.execute(select(EventInbox))).scalars().all()
            steps = (await session.execute(select(StepRun))).scalars().all()
        assert len(inbox) == 1
        assert inbox[0].event_type == "azure_devops:git.push"
        assert steps == []  # reconciliation hooks come later

    async def test_repeated_push_delivery_collapses(self, app, client: AsyncClient):
        body = load_payload("push_normal.json")
        await client.post("/webhook/azure_devops", content=body, headers=auth_headers())
        await client.post("/webhook/azure_devops", content=body, headers=auth_headers())
        async with app.state.session_factory() as session:
            inbox = (await session.execute(select(EventInbox))).scalars().all()
        assert len(inbox) == 1  # same content-stable identity

    async def test_malformed_json_is_a_400(self, client: AsyncClient):
        response = await client.post(
            "/webhook/azure_devops",
            content=b"{not json",
            headers={"Content-Type": "application/json", "Authorization": basic_header()},
        )
        assert response.status_code == 400


class TestUnitNormalizers:
    """Direct unit coverage of the identity + normalization helpers."""

    def test_azure_project_key_is_deterministic_and_int_sized(self):
        from forge.gateway.azure_webhook import azure_project_key

        first = azure_project_key(PROJECT_GUID)
        assert first == azure_project_key(PROJECT_GUID)
        assert first != azure_project_key("another-project")
        assert 0 <= first < 2**31

    def test_identity_name_prefers_uniquename(self):
        from forge.gateway.azure_webhook import identity_name

        assert identity_name({"uniqueName": "a@b.c", "displayName": "A B"}) == "a@b.c"
        assert identity_name({"displayName": "A B"}) == "A B"
        assert identity_name("bare@b.c") == "bare@b.c"
        assert identity_name(None) == ""

    def test_bot_identity_matches_both_forms(self):
        from forge.gateway.azure_webhook import is_bot_identity

        assert is_bot_identity("forge-bot@fabrikam.example", "forge-bot")
        assert is_bot_identity("forge-bot", "forge-bot")
        assert is_bot_identity("forge-bot", "forge-bot")
        assert not is_bot_identity("dev@fabrikam.example", "forge-bot")
        assert not is_bot_identity("forge-bot@fabrikam.example", "")

    def test_source_event_id_is_content_stable(self):
        from forge.gateway.azure_webhook import azure_source_event_id

        one = azure_source_event_id("conn", "workitem.commented", "workitem:142:comment:9")
        two = azure_source_event_id("conn", "workitem.commented", "workitem:142:comment:9")
        other = azure_source_event_id("conn", "workitem.commented", "workitem:142:comment:10")
        assert one == two
        assert one != other

    def test_org_from_payload_falls_back_to_collection(self):
        from forge.gateway.azure_webhook import org_from_payload

        payload = {"resourceContainers": {"collection": {"baseUrl": "https://server/tfs/"}}}
        assert org_from_payload(payload) == "https://server/tfs"
        assert org_from_payload({}) == ""

    def test_workitem_updated_unit_normalization(self):
        import hashlib

        from forge.gateway.azure_webhook import normalize_workitem_updated

        edited = normalize_workitem_updated(load_json("workitem_updated_edited.json"))
        assert edited is not None
        assert edited["command"] == "issue_edited"
        assert edited["delivery_key"] == (
            f"edit:{WORK_ITEM}:"
            + hashlib.sha256(
                b"Ship the flux capacitor\n<p>Users <b>cannot</b> reset."
                b" The reset mail bounces with SMTP 550.</p>"
            ).hexdigest()
            + ":12"
        )

        unlabel = normalize_workitem_updated(load_json("workitem_updated_tag_removed.json"))
        assert unlabel is not None
        assert unlabel["command"] == "unlabeled"
        assert unlabel["delivery_key"] == f"unlabel:{WORK_ITEM}:13"

        # A work item WITHOUT tags never routes unlabeled.
        no_tags = load_json("workitem_updated_edited.json")
        untagged = normalize_workitem_updated(no_tags)
        assert untagged is not None and untagged["command"] == "issue_edited"

        # A sparse delivery (changed-fields-only subscription) flags the
        # missing fields instead of digesting empty strings blindly.
        sparse = load_json("workitem_updated_edited.json")
        del sparse["resource"]["fields"]["System.Title"]
        del sparse["resource"]["fields"]["System.Description"]
        sparse_command = normalize_workitem_updated(sparse)
        assert sparse_command is not None
        assert sparse_command["issue_title_present"] is False
        assert sparse_command["issue_description_present"] is False
        assert sparse_command["issue_title"] == ""
        assert sparse_command["issue_body"] == ""

        assert normalize_workitem_updated({"resource": {}}) is None


class TestNoRedisFallback:
    """Without Redis the persisted step executes in-process through the SAME
    claim/lease/fence protocol as the worker."""

    async def test_run_command_step_scheduled_and_executed(self, tmp_path, monkeypatch):
        reset_engine()
        application = create_app(settings=azure_settings(tmp_path))
        executed: list[dict] = []

        import forge.worker.steps as steps_module

        async def routed(settings, forge_config, session_factory, metadata):
            executed.append(metadata)

        monkeypatch.setattr(steps_module, "execute_run_command", routed)
        try:
            async with application.router.lifespan_context(application):
                application.state.task_queue = None  # the no-Redis shape
                transport = ASGITransport(app=application)
                async with AsyncClient(transport=transport, base_url="http://test") as ac:
                    body = load_payload("workitem_commented_implement.json")
                    response = await ac.post(
                        "/webhook/azure_devops", content=body, headers=auth_headers()
                    )

                assert response.status_code == 202
                assert response.json() == {
                    "status": "accepted",
                    "event": "workitem.commented",
                    "run_command": True,
                }
                # The BackgroundTask runs after the response — the command
                # went through the same durable step path.
                assert len(executed) == 1
                assert executed[0]["provider"] == "azure_devops"
                assert executed[0]["command"] == "start_run"
        finally:
            reset_engine()


class TestPayloadCapture:
    """FORGE_CAPTURE_DIR persists Azure DevOps deliveries too."""

    @pytest.fixture()
    async def app(self, tmp_path):
        reset_engine()
        capture_dir = tmp_path / "captured"
        application = create_app(
            settings=azure_settings(tmp_path, FORGE_CAPTURE_DIR=str(capture_dir))
        )
        async with application.router.lifespan_context(application):
            application.state.task_queue = AsyncMock()
            application.state.task_queue.is_duplicate = AsyncMock(return_value=False)
            application.state.capture_dir = capture_dir
            yield application
        reset_engine()

    @pytest.fixture()
    async def client(self, app) -> AsyncClient:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    async def test_workitem_delivery_is_captured(self, app, client: AsyncClient):
        body = load_payload("workitem_commented_implement.json")
        response = await client.post("/webhook/azure_devops", content=body, headers=auth_headers())
        assert response.status_code == 202

        records = list(app.state.capture_dir.glob("*workitem*.json"))
        assert len(records) == 1
        record = json.loads(records[0].read_text())
        assert record["x_ado_event"] == "workitem.commented"
        assert record["payload"]["eventType"] == "workitem.commented"
        # The webhook password must never appear in captured records.
        assert WEBHOOK_PASSWORD not in json.dumps(record)


class TestIdentityName:
    """Live-found (ADR-0024 lab): bare-string identities arrive as
    "Display Name <user@domain>" — the uniqueName is the stable form."""

    def test_bracketed_string_yields_uniquename(self):
        from forge.gateway.azure_webhook import identity_name

        assert (
            identity_name("Pavel Nasovich <Pavel_Nasovich@epam.com>") == "Pavel_Nasovich@epam.com"
        )

    def test_identityref_wins_uniquename(self):
        from forge.gateway.azure_webhook import identity_name

        assert identity_name({"displayName": "Pavel", "uniqueName": "p@x.io"}) == "p@x.io"

    def test_bare_display_name_passes_through(self):
        from forge.gateway.azure_webhook import identity_name

        assert identity_name("Pavel Nasovich") == "Pavel Nasovich"
