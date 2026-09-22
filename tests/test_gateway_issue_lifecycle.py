"""GitLab gateway #29 lifecycle normalization: issue updates → run commands.

An ``Issue Hook`` update carrying a title/description change normalizes to
``issue_edited``; a ``changes.labels`` update that removes the trigger label
normalizes to ``unlabeled`` (RELIABLE on GitLab — the payload carries both
label sets, so the removal is proven). Both route through the SAME durable
step path as the note commands, with per-delivery inbox identities so two
distinct edits of one issue never collapse.
"""

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
from forge.gateway.parser import parse_webhook
from forge.gateway.router import _match_issue_lifecycle
from forge.gitlab.events import IssueEvent, MergeRequestEvent, NoteEvent
from forge.main import create_app
from tests.conftest import TEST_WEBHOOK_SECRET

FIXTURES = Path(__file__).parent / "fixtures"

PROJECT_ID = 42
ISSUE_IID = 5
ISSUE_DB_ID = 12


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test"),
        GITLAB_WEBHOOK_SECRET=SecretStr(TEST_WEBHOOK_SECRET),
        FORGE_BOT_USERNAME="forge-bot",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        LITELLM_URL="http://litellm:4000",
        REDIS_URL=None,
        FORGE_CAPTURE_DIR=None,
        FORGE_BOT_TOKEN=None,
    )
    values.update(overrides)
    return Settings(**values)


# ----------------------------------------------------------------------
# Matcher unit tests (typed event models over the fixtures)
# ----------------------------------------------------------------------


def _match(name: str, **settings_overrides) -> dict | None:
    settings = _settings(**settings_overrides)
    payload = _load(name)
    event = parse_webhook("Issue Hook", payload)
    return _match_issue_lifecycle(event, settings)


def test_description_edit_matches_issue_edited():
    command = _match("issue_updated.json")

    assert command is not None
    assert command["command"] == "issue_edited"
    assert command["provider"] == "gitlab"
    assert command["project_id"] == PROJECT_ID
    assert command["issue_iid"] == ISSUE_IID
    assert command["author_username"] == "alice"
    assert command["issue_title"] == "Users cannot reset their password"
    assert "SMTP 550" in command["issue_body"]
    # Per-delivery identity: content digest + updated_at.
    assert command["delivery_key"].startswith(f"edit:{ISSUE_DB_ID}:")
    assert command["delivery_key"].endswith(":2026-03-22T09:15:00Z")


def test_title_edit_matches_issue_edited():
    """object_attributes carries the NEW title (GitLab issue hooks)."""
    payload = _load("issue_updated.json")
    payload["object_attributes"]["title"] = "Users cannot reset their password at all"
    payload["changes"] = {
        "title": {
            "previous": "Users cannot reset their password",
            "current": "Users cannot reset their password at all",
        }
    }
    event = parse_webhook("Issue Hook", payload)
    command = _match_issue_lifecycle(event, _settings())

    assert command is not None
    assert command["command"] == "issue_edited"
    assert command["issue_title"] == "Users cannot reset their password at all"


def test_trigger_label_removal_matches_unlabeled():
    command = _match("issue_unlabeled.json")

    assert command is not None
    assert command["command"] == "unlabeled"
    assert command["provider"] == "gitlab"
    assert command["issue_iid"] == ISSUE_IID
    assert command["author_username"] == "alice"
    assert command["delivery_key"].startswith(f"unlabel:{ISSUE_DB_ID}:")


def test_trigger_label_removal_is_case_insensitive():
    payload = _load("issue_unlabeled.json")
    payload["changes"]["labels"]["previous"][1]["title"] = "Forge"
    event = parse_webhook("Issue Hook", payload)
    command = _match_issue_lifecycle(event, _settings())

    assert command is not None
    assert command["command"] == "unlabeled"


def test_label_addition_is_not_a_match():
    """Label-on is the plan trigger (ADR-0020 §4) — the legacy labeled path
    owns it, never the unlabeled cancel."""
    payload = _load("issue_unlabeled.json")
    payload["changes"]["labels"] = {
        "previous": [],
        "current": [{"id": 290, "title": "forge"}],
    }
    event = parse_webhook("Issue Hook", payload)
    assert _match_issue_lifecycle(event, _settings()) is None


def test_unrelated_label_change_is_not_a_match():
    """Neither removed nor added the trigger label — legacy path."""
    payload = _load("issue_unlabeled.json")
    payload["changes"]["labels"] = {
        "previous": [{"id": 284, "title": "bug"}],
        "current": [{"id": 291, "title": "docs"}],
    }
    event = parse_webhook("Issue Hook", payload)
    assert _match_issue_lifecycle(event, _settings()) is None


def test_state_only_update_is_not_a_match():
    payload = _load("issue_updated.json")
    payload["changes"] = {
        "state_id": {"previous": 1, "current": 2},
        "updated_at": {"previous": "2026-03-21T15:00:00Z", "current": "2026-03-22T09:15:00Z"},
    }
    event = parse_webhook("Issue Hook", payload)
    assert _match_issue_lifecycle(event, _settings()) is None


def test_non_update_actions_are_not_a_match():
    payload = _load("issue_updated.json")
    payload["object_attributes"]["action"] = "open"
    event = parse_webhook("Issue Hook", payload)
    assert _match_issue_lifecycle(event, _settings()) is None


def test_note_and_mr_events_are_not_a_match():
    settings = _settings()
    note = parse_webhook("Note Hook", _load("note_issue.json"))
    assert isinstance(note, NoteEvent)
    assert _match_issue_lifecycle(note, settings) is None
    mr = parse_webhook("Merge Request Hook", _load("mr_update.json"))
    assert isinstance(mr, MergeRequestEvent)
    assert _match_issue_lifecycle(mr, settings) is None


def test_custom_trigger_label_setting_is_respected():
    payload = _load("issue_unlabeled.json")
    payload["changes"]["labels"]["previous"][1]["title"] = "forge-it"
    event = parse_webhook("Issue Hook", payload)
    command = _match_issue_lifecycle(event, _settings(FORGE_TRIGGER_LABEL="forge-it"))

    assert command is not None
    assert command["command"] == "unlabeled"


def test_issue_event_model_parses_the_fixtures():
    """The typed model carries everything the matcher reads (labels + changes)."""
    event = parse_webhook("Issue Hook", _load("issue_unlabeled.json"))
    assert isinstance(event, IssueEvent)
    assert event.object_attributes.action == "update"
    assert event.object_attributes.updated_at == "2026-03-22T10:00:00Z"
    assert [label.title for label in event.labels] == ["bug"]
    previous_titles = [
        label.get("title")
        for label in event.changes["labels"]["previous"]
        if isinstance(label, dict)
    ]
    assert "forge" in previous_titles


# ----------------------------------------------------------------------
# End-to-end: the normalized command lands on the durable step path
# ----------------------------------------------------------------------


class TestIssueLifecycleIngress:
    @pytest.fixture()
    async def app(self, tmp_path):
        reset_engine()
        application = create_app(settings=_settings())
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

    async def _post(self, client, name: str):
        return await client.post(
            "/webhook",
            json=_load(name),
            headers={"X-Gitlab-Token": TEST_WEBHOOK_SECRET, "X-Gitlab-Event": "Issue Hook"},
        )

    async def test_issue_edit_ingests_inbox_row_and_step(self, app, client):
        response = await self._post(client, "issue_updated.json")

        assert response.status_code == 202
        assert response.json() == {
            "status": "accepted",
            "event": "issue",
            "queued": True,
            "run_command": True,
        }
        async with app.state.session_factory() as session:
            inbox = list((await session.execute(select(EventInbox))).scalars().all())
            steps = list((await session.execute(select(StepRun))).scalars().all())
        assert len(inbox) == 1 and len(steps) == 1
        payload = inbox[0].payload
        assert payload["command"] == "issue_edited"
        assert payload["provider"] == "gitlab"
        assert payload["project_id"] == PROJECT_ID
        assert payload["issue_iid"] == ISSUE_IID
        assert payload["author_username"] == "alice"
        assert inbox[0].event_type == "run_command"
        # ONE transaction: the step's identity IS the inbox identity.
        assert steps[0].source_event_id == inbox[0].source_event_id
        assert steps[0].step_name == "issue_edited"
        assert steps[0].status == "scheduled"
        # The wake task carries the inbox identity (the 64243ff rule).
        (task,) = app.state.task_queue.submit.await_args[0]
        assert task.task_type == "run_command"
        assert task.metadata["command"] == "issue_edited"
        assert task.metadata["source_event_id"] == inbox[0].source_event_id

    async def test_label_removal_ingests_an_unlabeled_step(self, app, client):
        response = await self._post(client, "issue_unlabeled.json")

        assert response.status_code == 202
        assert response.json()["run_command"] is True
        async with app.state.session_factory() as session:
            inbox = list((await session.execute(select(EventInbox))).scalars().all())
            steps = list((await session.execute(select(StepRun))).scalars().all())
        assert len(inbox) == 1 and len(steps) == 1
        assert inbox[0].payload["command"] == "unlabeled"
        assert steps[0].step_name == "unlabeled"

    async def test_redelivered_edit_is_deduplicated(self, app, client):
        first = await self._post(client, "issue_updated.json")
        second = await self._post(client, "issue_updated.json")

        assert first.json()["run_command"] is True
        assert second.json()["deduplicated"] is True
        async with app.state.session_factory() as session:
            steps = list((await session.execute(select(StepRun))).scalars().all())
        assert len(steps) == 1

    async def test_a_second_distinct_edit_schedules_a_second_step(self, app, client):
        """The digest+updated_at identity: two genuinely different edits of
        one issue must each schedule their step (A→B→A never collides)."""
        await self._post(client, "issue_updated.json")
        payload = _load("issue_updated.json")
        payload["object_attributes"]["description"] = "edited again, differently"
        payload["object_attributes"]["updated_at"] = "2026-03-22T11:30:00Z"
        payload["changes"]["description"]["current"] = "edited again, differently"
        payload["changes"]["updated_at"]["current"] = "2026-03-22T11:30:00Z"
        await client.post(
            "/webhook",
            json=payload,
            headers={"X-Gitlab-Token": TEST_WEBHOOK_SECRET, "X-Gitlab-Event": "Issue Hook"},
        )

        async with app.state.session_factory() as session:
            inbox = list((await session.execute(select(EventInbox))).scalars().all())
            steps = list((await session.execute(select(StepRun))).scalars().all())
        assert len(inbox) == 2
        assert len(steps) == 2
        assert len({row.source_event_id for row in inbox}) == 2

    async def test_bot_edited_issue_is_skipped(self, app, client):
        payload = _load("issue_updated.json")
        payload["user"]["username"] = "forge-bot"
        response = await client.post(
            "/webhook",
            json=payload,
            headers={"X-Gitlab-Token": TEST_WEBHOOK_SECRET, "X-Gitlab-Event": "Issue Hook"},
        )

        assert response.json() == {"status": "skipped", "reason": "bot-loop"}
        async with app.state.session_factory() as session:
            inbox = (await session.execute(select(EventInbox))).scalars().all()
        assert inbox == []

    async def test_state_only_update_takes_the_legacy_path(self, app, client):
        payload = _load("issue_updated.json")
        payload["changes"] = {
            "updated_at": {
                "previous": "2026-03-21T15:00:00Z",
                "current": "2026-03-22T09:15:00Z",
            }
        }
        response = await client.post(
            "/webhook",
            json=payload,
            headers={"X-Gitlab-Token": TEST_WEBHOOK_SECRET, "X-Gitlab-Event": "Issue Hook"},
        )

        assert response.status_code == 202
        assert response.json() == {"status": "accepted", "event": "issue", "queued": True}
        async with app.state.session_factory() as session:
            steps = (await session.execute(select(StepRun))).scalars().all()
        assert steps == []


# ----------------------------------------------------------------------
# NXT-10: adaptive operator commands (/pause /resume /steer /answer)
# ----------------------------------------------------------------------


class TestAdaptiveCommandIngress:
    """The adaptive verbs reach the ControlCommandRouter through the real
    GitLab ingress — and ONLY while FORGE_ADAPTIVE_COMMANDS_ENABLED is on
    (the disabled default is zero routing: the note is not parsed as a
    command at all and takes the legacy path)."""

    @pytest.fixture()
    async def app(self, tmp_path):
        reset_engine()
        application = create_app(settings=_settings())
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

    @staticmethod
    def _note(text: str) -> dict:
        payload = _load("note_issue.json")
        payload["user"]["username"] = "alice"
        payload["object_attributes"]["note"] = text
        return payload

    async def _post(self, client, text: str):
        return await client.post(
            "/webhook",
            json=self._note(text),
            headers={"X-Gitlab-Token": TEST_WEBHOOK_SECRET, "X-Gitlab-Event": "Note Hook"},
        )

    async def test_disabled_default_is_zero_routing(self, app, client, monkeypatch):
        monkeypatch.delenv("FORGE_ADAPTIVE_COMMANDS_ENABLED", raising=False)
        response = await self._post(client, "/pause")

        assert response.status_code == 202
        # Not a run command, not adaptive — the note takes the legacy path.
        body = response.json()
        assert body.get("adaptive_command") is None
        assert body.get("run_command") is None
        async with app.state.session_factory() as session:
            steps = (await session.execute(select(StepRun))).scalars().all()
        assert steps == []

    async def test_enabled_routes_to_the_command_router(self, app, client, monkeypatch):
        routed: list[dict] = []

        async def fake_route(settings, session_factory, note):
            routed.append(note)
            return {"status": "applied"}

        import forge.gateway.router as gateway_router_module

        monkeypatch.setattr(gateway_router_module, "route_adaptive_command_note", fake_route)
        monkeypatch.setenv("FORGE_ADAPTIVE_COMMANDS_ENABLED", "1")
        response = await self._post(client, "@forge /pause feedface1")

        assert response.status_code == 202
        assert response.json() == {
            "status": "accepted",
            "event": "note",
            "adaptive_command": True,
        }
        (note,) = routed
        assert note["command"] == "adaptive_control"
        assert note["adaptive_verb"] == "pause"
        assert note["provider"] == "gitlab"
        assert note["project_id"] == PROJECT_ID
        assert note["issue_iid"] == ISSUE_IID
        assert note["author_username"] == "alice"
        assert note["note_text"] == "@forge /pause feedface1"
        # The mailbox leg never schedules a classic run-command step.
        async with app.state.session_factory() as session:
            steps = (await session.execute(select(StepRun))).scalars().all()
        assert steps == []

    async def test_bot_note_is_never_routed(self, app, client, monkeypatch):
        monkeypatch.setenv("FORGE_ADAPTIVE_COMMANDS_ENABLED", "1")
        payload = self._note("/pause")
        payload["user"]["username"] = "forge-bot"
        response = await client.post(
            "/webhook",
            json=payload,
            headers={"X-Gitlab-Token": TEST_WEBHOOK_SECRET, "X-Gitlab-Event": "Note Hook"},
        )

        assert response.json() == {"status": "skipped", "reason": "bot-loop"}

    async def test_adaptive_note_on_an_mr_is_not_routed(self, app, client, monkeypatch):
        """Gate notes are issue-bound: an MR-note /pause is not routed (the
        same boundary every non-/security run command has)."""
        routed: list[dict] = []

        async def fake_route(settings, session_factory, note):
            routed.append(note)
            return {"status": "applied"}

        import forge.gateway.router as gateway_router_module

        monkeypatch.setattr(gateway_router_module, "route_adaptive_command_note", fake_route)
        monkeypatch.setenv("FORGE_ADAPTIVE_COMMANDS_ENABLED", "1")
        payload = _load("note_mr.json")
        payload["user"]["username"] = "alice"
        payload["object_attributes"]["note"] = "/pause"
        response = await client.post(
            "/webhook",
            json=payload,
            headers={"X-Gitlab-Token": TEST_WEBHOOK_SECRET, "X-Gitlab-Event": "Note Hook"},
        )

        assert response.json().get("adaptive_command") is None
        assert routed == []
        async with app.state.session_factory() as session:
            steps = (await session.execute(select(StepRun))).scalars().all()
        assert steps == []
