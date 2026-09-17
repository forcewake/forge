"""GitHub webhook ingress tests: signature validation, fail-closed, routing.

Exercises the durable ingestion path (inbox row + scheduled step in ONE
transaction, ADR-0017 §1 semantics) over the sqlite harness. The task queue
is an AsyncMock, exactly like the GitLab wiring tests, so the wake-up
accelerator is observed without executing the step.
"""

import hashlib
import hmac
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
from tests.fixtures.fake_github import sample_installation_token  # noqa: F401 — shape reference

FIXTURES = Path(__file__).parent / "fixtures" / "github_payloads"
GITHUB_WEBHOOK_SECRET = "github-hook-secret"
TEST_DELIVERY = "d" * 32


def load_payload(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def sign(body: bytes, secret: str = GITHUB_WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def signed_headers(body: bytes, secret: str = GITHUB_WEBHOOK_SECRET) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sign(body, secret),
        "X-GitHub-Event": "issue_comment",
        "X-GitHub-Delivery": TEST_DELIVERY,
    }


def github_settings(tmp_path, **overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test"),
        GITLAB_WEBHOOK_SECRET=SecretStr("test-secret-token"),
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/github-webhook.db",
        LITELLM_URL="http://litellm:4000",
        REDIS_URL=None,
        FORGE_CAPTURE_DIR=None,
        FORGE_BOT_TOKEN=None,
        FORGE_BOT_USERNAME="forge-bot",
        # GitHub slice settings
        FORGE_GITHUB_ENABLED=True,
        FORGE_GITHUB_WEBHOOK_SECRET=SecretStr(GITHUB_WEBHOOK_SECRET),
        FORGE_GITHUB_APP_ID="123456",
        FORGE_GITHUB_INSTALLATION_ID="777",
        FORGE_GITHUB_PRIVATE_KEY=SecretStr("not-a-real-key"),
    )
    values.update(overrides)
    return Settings(**values)


class TestFailClosed:
    @pytest.fixture()
    async def disabled_app(self, tmp_path):
        reset_engine()
        application = create_app(settings=github_settings(tmp_path, FORGE_GITHUB_ENABLED=False))
        async with application.router.lifespan_context(application):
            yield application
        reset_engine()

    @pytest.fixture()
    async def disabled_client(self, disabled_app) -> AsyncClient:
        transport = ASGITransport(app=disabled_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    async def test_disabled_returns_503(self, disabled_client: AsyncClient):
        body = load_payload("ping.json")
        response = await disabled_client.post(
            "/webhook/github", content=body, headers=signed_headers(body)
        )
        assert response.status_code == 503
        assert response.json() == {"error": "github ingress disabled"}

    async def test_missing_secret_returns_503(self, tmp_path):
        reset_engine()
        application = create_app(
            settings=github_settings(tmp_path, FORGE_GITHUB_WEBHOOK_SECRET=None)
        )
        from httpx import ASGITransport as T, AsyncClient as C

        try:
            async with application.router.lifespan_context(application):
                transport = T(app=application)
                async with C(transport=transport, base_url="http://test") as ac:
                    body = load_payload("ping.json")
                    response = await ac.post(
                        "/webhook/github", content=body, headers=signed_headers(body)
                    )
                    assert response.status_code == 503
        finally:
            reset_engine()


class TestSignature:
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

    async def test_missing_signature_rejected(self, client: AsyncClient):
        body = load_payload("ping.json")
        response = await client.post(
            "/webhook/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "ping",
                "X-GitHub-Delivery": TEST_DELIVERY,
            },
        )
        assert response.status_code == 401

    async def test_wrong_signature_rejected(self, client: AsyncClient):
        body = load_payload("ping.json")
        headers = signed_headers(body, secret="an-entirely-different-secret")
        response = await client.post("/webhook/github", content=body, headers=headers)
        assert response.status_code == 401

    async def test_tampered_body_rejected(self, client: AsyncClient):
        body = load_payload("ping.json")
        headers = signed_headers(body)
        tampered = body.replace(b"Design", b"Broken")
        response = await client.post("/webhook/github", content=tampered, headers=headers)
        assert response.status_code == 401

    async def test_ping_answers_pong(self, client: AsyncClient):
        body = load_payload("ping.json")
        headers = signed_headers(body)
        headers["X-GitHub-Event"] = "ping"
        response = await client.post("/webhook/github", content=body, headers=headers)
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "message": "pong"}

    async def test_official_test_vector(self):
        """docs.github.com's published HMAC vector (research §2.1)."""
        secret = "It's a Secret to Everybody"
        body = b"Hello, World!"
        assert sign(body, secret) == (
            "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
        )


class TestIngress:
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

    async def post_event(self, client, app, name: str, event: str | None = None):
        body = load_payload(name)
        headers = signed_headers(body)
        if event is not None:
            headers["X-GitHub-Event"] = event
        response = await client.post("/webhook/github", content=body, headers=headers)
        inbox = []
        async with app.state.session_factory() as session:
            rows = (await session.execute(select(EventInbox))).scalars().all()
            inbox = list(rows)
        steps = []
        async with app.state.session_factory() as session:
            rows = (await session.execute(select(StepRun))).scalars().all()
            steps = list(rows)
        return response, inbox, steps

    async def test_implement_comment_persists_inbox_and_step(self, app, client: AsyncClient):
        response, inbox, steps = await self.post_event(client, app, "issue_comment_created.json")

        assert response.status_code == 202
        assert response.json()["run_command"] is True
        assert len(inbox) == 1 and len(steps) == 1
        payload = inbox[0].payload
        assert payload["provider"] == "github"
        assert payload["command"] == "start_run"
        assert payload["repo_full_name"] == "acme/acme-widget"
        assert payload["project_id"] == 70010
        assert payload["issue_number"] == 42
        assert payload["issue_is_pr"] is False
        assert payload["author_username"] == "alice"
        assert payload["note_id"] == 9010
        # Connection-scoped identity: inbox id and step are bound together.
        assert steps[0].source_event_id == inbox[0].source_event_id
        assert steps[0].status == "scheduled"
        # The wake-up accelerator received the command.
        (task,) = app.state.task_queue.submit.await_args[0]
        assert task.task_type == "run_command"
        assert task.metadata["provider"] == "github"

    async def test_pr_comment_is_flagged_as_pr(self, app, client: AsyncClient):
        _, inbox, _ = await self.post_event(client, app, "issue_comment_pr.json")
        assert inbox[0].payload["issue_is_pr"] is True

    async def test_redelivered_command_is_deduplicated(self, app, client: AsyncClient):
        first, inbox, steps = await self.post_event(client, app, "issue_comment_created.json")
        body = load_payload("issue_comment_created.json")
        second = await client.post("/webhook/github", content=body, headers=signed_headers(body))

        assert first.json().get("run_command") is True
        assert second.json() == {
            "status": "accepted",
            "event": "issue_comment",
            "deduplicated": True,
        }
        assert len(inbox) == 1
        # The re-delivery created no second step row.
        async with app.state.session_factory() as session:
            all_steps = (await session.execute(select(StepRun))).scalars().all()
        assert len(all_steps) == 1

    async def test_non_command_comment_recorded_without_step(self, app, client: AsyncClient):
        payload = json.loads(load_payload("issue_comment_created.json"))
        payload["comment"]["body"] = "Looks great, thanks!"
        body = json.dumps(payload).encode()
        response = await client.post("/webhook/github", content=body, headers=signed_headers(body))

        assert response.status_code == 202
        assert response.json() == {"status": "accepted", "event": "issue_comment", "recorded": True}
        async with app.state.session_factory() as session:
            inbox = (await session.execute(select(EventInbox))).scalars().all()
            steps = (await session.execute(select(StepRun))).scalars().all()
        assert len(inbox) == 1
        assert inbox[0].event_type == "github:issue_comment"
        assert steps == []

    async def test_bot_comment_is_skipped(self, app, client: AsyncClient):
        payload = json.loads(load_payload("issue_comment_created.json"))
        payload["comment"]["user"]["login"] = "forge-bot"
        body = json.dumps(payload).encode()
        response = await client.post("/webhook/github", content=body, headers=signed_headers(body))

        assert response.json() == {"status": "skipped", "reason": "bot-loop"}
        async with app.state.session_factory() as session:
            inbox = (await session.execute(select(EventInbox))).scalars().all()
        assert inbox == []

    async def test_push_event_recorded_inbox_only(self, app, client: AsyncClient):
        _, inbox, steps = await self.post_event(client, app, "push.json", event="push")

        assert len(inbox) == 1
        assert inbox[0].event_type == "github:push"
        assert steps == []  # reconciliation hooks come later

    async def test_installation_removed_records_disabled_connection(self, app, client: AsyncClient):
        _, inbox, _ = await self.post_event(
            client, app, "installation_deleted.json", event="installation"
        )

        assert len(inbox) == 1
        assert inbox[0].handler_result == {"connection_disabled": True}

    async def test_repeated_push_delivery_collapses(self, app, client: AsyncClient):
        await self.post_event(client, app, "push.json", event="push")
        _, inbox, _ = await self.post_event(client, app, "push.json", event="push")
        assert len(inbox) == 1  # same delivery GUID → same identity


class TestSignatureUnit:
    """Direct unit coverage of the constant-time check."""

    def test_verify_signature_accepts_correct_hmac(self):
        from forge.gateway.github_webhook import verify_github_signature

        body = b'{"zen": "x"}'
        assert verify_github_signature("s3cret", body, sign(body, "s3cret"))

    def test_verify_signature_rejects_missing_or_bad(self):
        from forge.gateway.github_webhook import verify_github_signature

        body = b"{}"
        assert not verify_github_signature("s3cret", body, None)
        assert not verify_github_signature("", body, "sha256=00")
        assert not verify_github_signature("s3cret", body, sign(body, "other"))
        # Legacy sha1 headers are not accepted (research §2.1).
        assert not verify_github_signature("s3cret", body, "sha1=abcdef")


class TestLabeledTrigger:
    """ADR-0020 §4: issues.labeled with the reserved label → run command."""

    def normalize(self, payload: dict, trigger: str = "forge") -> dict | None:
        from forge.gateway.github_webhook import normalize_labeled_event

        return normalize_labeled_event(payload, trigger_label=trigger)

    def labeled_payload(self, *, label: str = "Forge", sender: str = "alice") -> dict:
        payload = json.loads(load_payload("issues_labeled.json"))
        payload["label"]["name"] = label
        payload["sender"]["login"] = sender
        return payload

    async def test_matching_label_normalizes_to_start_run(self):
        metadata = self.normalize(self.labeled_payload())

        assert metadata is not None
        assert metadata["command"] == "start_run"
        assert metadata["provider"] == "github"
        assert metadata["repo_full_name"] == "acme/acme-widget"
        assert metadata["project_id"] == 70010
        assert metadata["issue_number"] == 42
        assert metadata["issue_is_pr"] is False
        assert metadata["author_username"] == "alice"  # the labeler is the actor
        # The ISSUE id keys dedupe — the label id is constant across every
        # issue carrying it and silently swallowed all but the first event
        # (LIVE-found: #28 planned, #29 with the same label never fired).
        assert metadata["note_id"] == 1010  # fixture issue.id, not label.id 88100

    async def test_distinct_issues_with_the_same_label_do_not_collide(self):
        """Two issues labeled by the same sender must produce distinct
        delivery identities — the label id alone is NOT an event identity."""
        from forge.gateway.github_webhook import github_source_event_id

        first = self.labeled_payload()
        second = self.labeled_payload()
        second["issue"]["number"] = 43
        second["issue"]["id"] = 88101

        id_first = self.normalize(first)["note_id"]
        id_second = self.normalize(second)["note_id"]
        assert id_first != id_second

        source_first = github_source_event_id(
            "github:1:acme/acme-widget", "issues", "labeled", f"label:{id_first}"
        )
        source_second = github_source_event_id(
            "github:1:acme/acme-widget", "issues", "labeled", f"label:{id_second}"
        )
        assert source_first != source_second

    async def test_redelivered_label_event_keeps_a_stable_identity(self):
        """Redelivery of the SAME labeled event collapses onto one identity
        (dedup intact) even when the delivery GUID differs."""
        from forge.gateway.github_webhook import github_source_event_id

        metadata = self.normalize(self.labeled_payload())
        again = self.normalize(self.labeled_payload())
        assert metadata["note_id"] == again["note_id"]
        assert github_source_event_id(
            "github:1:acme/acme-widget", "issues", "labeled", f"label:{metadata['note_id']}"
        ) == github_source_event_id(
            "github:1:acme/acme-widget", "issues", "labeled", f"label:{again['note_id']}"
        )

    async def test_label_match_is_case_insensitive(self):
        assert self.normalize(self.labeled_payload(label="FORGE")) is not None
        assert self.normalize(self.labeled_payload(label="forge")) is not None

    async def test_other_labels_are_ignored(self):
        assert self.normalize(self.labeled_payload(label="bug")) is None
        assert self.normalize(self.labeled_payload(label="forge-it")) is None  # no prefix match

    async def test_trigger_label_is_configurable(self):
        assert self.normalize(self.labeled_payload(label="forge"), trigger="run-forge") is None
        assert (
            self.normalize(self.labeled_payload(label="Run-Forge"), trigger="run-forge") is not None
        )

    async def test_pr_labeling_is_flagged(self):
        payload = self.labeled_payload()
        payload["issue"]["pull_request"] = {"url": "https://github.test/x"}
        assert self.normalize(payload)["issue_is_pr"] is True

    async def test_labeled_delivery_ingests_a_durable_run_command(self, tmp_path):
        """Full ingress: inbox row + scheduled step in ONE transaction, keyed
        by the label id — the same durable path an /implement comment takes."""
        reset_engine()
        application = create_app(settings=github_settings(tmp_path))
        try:
            async with application.router.lifespan_context(application):
                application.state.task_queue = AsyncMock()
                application.state.task_queue.is_duplicate = AsyncMock(return_value=False)
                transport = ASGITransport(app=application)
                async with AsyncClient(transport=transport, base_url="http://test") as ac:
                    body = load_payload("issues_labeled.json")
                    headers = signed_headers(body)
                    headers["X-GitHub-Event"] = "issues"
                    response = await ac.post("/webhook/github", content=body, headers=headers)

                assert response.status_code == 202
                assert response.json()["run_command"] is True
                async with application.state.session_factory() as session:
                    inbox = (await session.execute(select(EventInbox))).scalars().all()
                    steps = (await session.execute(select(StepRun))).scalars().all()
                assert len(inbox) == 1 and len(steps) == 1
                assert inbox[0].payload["command"] == "start_run"
                assert inbox[0].payload["author_username"] == "alice"
                assert steps[0].status == "scheduled"
                (task,) = application.state.task_queue.submit.await_args[0]
                assert task.task_type == "run_command"
        finally:
            reset_engine()

    async def test_non_matching_label_delivery_is_recorded_inbox_only(self, tmp_path):
        reset_engine()
        application = create_app(settings=github_settings(tmp_path))
        try:
            async with application.router.lifespan_context(application):
                application.state.task_queue = AsyncMock()
                application.state.task_queue.is_duplicate = AsyncMock(return_value=False)
                transport = ASGITransport(app=application)
                async with AsyncClient(transport=transport, base_url="http://test") as ac:
                    payload = self.labeled_payload(label="bug")
                    body = json.dumps(payload).encode()
                    headers = signed_headers(body)
                    headers["X-GitHub-Event"] = "issues"
                    response = await ac.post("/webhook/github", content=body, headers=headers)

                assert response.json()["recorded"] is True
                async with application.state.session_factory() as session:
                    inbox = (await session.execute(select(EventInbox))).scalars().all()
                    steps = (await session.execute(select(StepRun))).scalars().all()
                assert len(inbox) == 1 and inbox[0].event_type == "github:issues"
                assert steps == []  # no run for an unrelated label
        finally:
            reset_engine()


class TestIssueEditedTrigger:
    """``issues.edited`` → ``issue_edited``: the replan-on-edit trigger.

    The gateway only normalizes — deciding whether the edit actually went
    stale is the executor's job (it owns the plan-time snapshot).
    """

    def edited_payload(
        self,
        *,
        body: str | None = None,
        sender: str = "alice",
        updated_at: str | None = None,
    ) -> dict:
        payload = json.loads(load_payload("issues_edited.json"))
        if body is not None:
            payload["issue"]["body"] = body
        if updated_at is not None:
            payload["issue"]["updated_at"] = updated_at
        payload["sender"]["login"] = sender
        return payload

    def normalize(self, payload: dict) -> dict | None:
        from forge.gateway.github_webhook import normalize_issue_edited_event

        return normalize_issue_edited_event(payload)

    async def test_edited_issue_normalizes_to_issue_edited(self):
        payload = self.edited_payload()

        metadata = self.normalize(payload)

        assert metadata is not None
        assert metadata["command"] == "issue_edited"
        assert metadata["provider"] == "github"
        assert metadata["repo_full_name"] == "acme/acme-widget"
        assert metadata["project_id"] == 70010
        assert metadata["issue_number"] == 42
        assert metadata["author_username"] == "alice"
        # The edited text travels in the metadata — the executor compares it
        # against the plan-time snapshot without an extra API read.
        assert metadata["issue_title"] == "Add password reset"
        assert metadata["issue_body"].startswith("Users cannot reset")

    async def test_pr_edit_is_ignored(self):
        payload = self.edited_payload()
        payload["issue"]["pull_request"] = {"url": "https://github.test/x"}

        assert self.normalize(payload) is None

    async def test_redelivered_edit_keeps_a_stable_identity(self):
        """A redelivered edit collapses onto one inbox identity — even when
        the delivery GUID differs — while a genuinely different edit does
        not (it is new information, not a replay)."""
        from forge.gateway.github_webhook import github_source_event_id

        connection = "github:12345:acme/acme-widget"
        first = self.normalize(self.edited_payload())
        again = self.normalize(self.edited_payload())
        other_edit = self.normalize(self.edited_payload(body="entirely different text"))

        assert first["delivery_key"] == again["delivery_key"]
        assert first["delivery_key"] != other_edit["delivery_key"]
        assert github_source_event_id(
            connection, "issues", "edited", first["delivery_key"]
        ) == github_source_event_id(connection, "issues", "edited", again["delivery_key"])
        assert github_source_event_id(
            connection, "issues", "edited", first["delivery_key"]
        ) != github_source_event_id(connection, "issues", "edited", other_edit["delivery_key"])

    async def test_edit_back_to_a_seen_text_is_not_swallowed(self):
        """The inbox identity is permanent, so an edit landing BACK on a
        previously-seen text (A→B→A) must not collide with the earlier
        A-edit's row — a collision would swallow the command entirely and
        leave the waiting plan silently stale again."""
        body_b = self.normalize(self.edited_payload(body="body B", updated_at="t1"))
        body_a = self.normalize(self.edited_payload(body="body A", updated_at="t2"))
        body_again = self.normalize(self.edited_payload(body="body B", updated_at="t3"))

        assert body_b["delivery_key"] != body_again["delivery_key"]
        assert body_a["delivery_key"] not in (
            body_b["delivery_key"],
            body_again["delivery_key"],
        )

    async def test_edited_delivery_ingests_a_durable_run_command(self, tmp_path):
        """Full ingress: inbox row + scheduled step in ONE transaction — the
        same durable path a /implement comment takes."""
        reset_engine()
        application = create_app(settings=github_settings(tmp_path))
        try:
            async with application.router.lifespan_context(application):
                application.state.task_queue = AsyncMock()
                application.state.task_queue.is_duplicate = AsyncMock(return_value=False)
                transport = ASGITransport(app=application)
                async with AsyncClient(transport=transport, base_url="http://test") as ac:
                    body = load_payload("issues_edited.json")
                    headers = signed_headers(body)
                    headers["X-GitHub-Event"] = "issues"
                    response = await ac.post("/webhook/github", content=body, headers=headers)

                assert response.status_code == 202
                assert response.json()["run_command"] is True
                async with application.state.session_factory() as session:
                    inbox = (await session.execute(select(EventInbox))).scalars().all()
                    steps = (await session.execute(select(StepRun))).scalars().all()
                assert len(inbox) == 1 and len(steps) == 1
                assert inbox[0].payload["command"] == "issue_edited"
                assert inbox[0].payload["issue_body"].startswith("Users cannot reset")
                assert steps[0].status == "scheduled"
        finally:
            reset_engine()


class TestUnlabeledTrigger:
    """``issues.unlabeled`` (trigger label) → ``unlabeled``: label-off = cancel.

    Mirror of the label-on trigger (ADR-0020 §4); whether the actor may
    cancel is the executor's admission decision.
    """

    def unlabeled_payload(self, *, label: str = "Forge", sender: str = "alice") -> dict:
        payload = json.loads(load_payload("issues_unlabeled.json"))
        payload["label"]["name"] = label
        payload["sender"]["login"] = sender
        return payload

    def normalize(self, payload: dict, delivery_key: str = "d" * 32) -> dict | None:
        from forge.gateway.github_webhook import normalize_issue_unlabeled_event

        return normalize_issue_unlabeled_event(payload, delivery_key=delivery_key)

    async def test_trigger_label_removal_normalizes_to_unlabeled(self):
        metadata = self.normalize(self.unlabeled_payload())

        assert metadata is not None
        assert metadata["command"] == "unlabeled"
        assert metadata["provider"] == "github"
        assert metadata["issue_number"] == 42
        assert metadata["author_username"] == "alice"  # the remover is the actor
        assert metadata["note_id"] == "d" * 32  # the delivery GUID keys identity

    async def test_other_label_removal_is_ignored(self):
        assert self.normalize(self.unlabeled_payload(label="bug")) is None

    async def test_pr_unlabel_is_ignored(self):
        payload = self.unlabeled_payload()
        payload["issue"]["pull_request"] = {"url": "https://github.test/x"}

        assert self.normalize(payload) is None

    async def test_unlabeled_delivery_ingests_a_durable_run_command(self, tmp_path):
        """Full ingress, and the delivery GUID is the dedupe identity: a
        redelivered unlabel collapses onto the same inbox row."""
        reset_engine()
        application = create_app(settings=github_settings(tmp_path))
        try:
            async with application.router.lifespan_context(application):
                application.state.task_queue = AsyncMock()
                application.state.task_queue.is_duplicate = AsyncMock(return_value=False)
                transport = ASGITransport(app=application)
                async with AsyncClient(transport=transport, base_url="http://test") as ac:
                    body = load_payload("issues_unlabeled.json")
                    headers = signed_headers(body)
                    headers["X-GitHub-Event"] = "issues"
                    first = await ac.post("/webhook/github", content=body, headers=headers)
                    second = await ac.post("/webhook/github", content=body, headers=headers)

                assert first.json()["run_command"] is True
                assert second.json()["deduplicated"] is True
                async with application.state.session_factory() as session:
                    inbox = (await session.execute(select(EventInbox))).scalars().all()
                    steps = (await session.execute(select(StepRun))).scalars().all()
                assert len(inbox) == 1 and len(steps) == 1
                assert inbox[0].payload["command"] == "unlabeled"
        finally:
            reset_engine()


class TestPayloadCapture:
    """FORGE_CAPTURE_DIR persists GitHub deliveries too (diagnosability).

    Without a capture, a live routing gap is undiagnosable: the inbox row
    records what WAS ingested, never why a delivery didn't route.
    """

    @pytest.fixture()
    async def app(self, tmp_path):
        reset_engine()
        capture_dir = tmp_path / "captured"
        application = create_app(
            settings=github_settings(tmp_path, FORGE_CAPTURE_DIR=str(capture_dir))
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

    async def test_pull_request_delivery_is_captured(self, app, client: AsyncClient):
        body = load_payload("pull_request_synchronize.json")
        headers = signed_headers(body)
        headers["X-GitHub-Event"] = "pull_request"
        response = await client.post("/webhook/github", content=body, headers=headers)
        assert response.status_code == 202

        records = list(app.state.capture_dir.glob("*pull_request*.json"))
        assert len(records) == 1
        record = json.loads(records[0].read_text())
        assert record["x_github_event"] == "pull_request"
        assert record["payload"]["action"] == "synchronize"
        # The webhook secret must never appear in captured records.
        assert GITHUB_WEBHOOK_SECRET not in json.dumps(record)

    async def test_ping_is_captured(self, app, client: AsyncClient):
        body = load_payload("ping.json")
        headers = signed_headers(body)
        headers["X-GitHub-Event"] = "ping"
        response = await client.post("/webhook/github", content=body, headers=headers)
        assert response.status_code == 200
        assert list(app.state.capture_dir.glob("*ping*.json"))
