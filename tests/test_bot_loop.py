import json
from pathlib import Path

from forge.gateway.validator import is_bot_event
from forge.gitlab.events import MergeRequestEvent, NoteEvent, PushEvent
from tests.conftest import TEST_WEBHOOK_SECRET

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_note_from_bot_is_detected():
    payload = _load("note_mr.json")
    payload["user"]["username"] = "forge-bot"
    event = NoteEvent.model_validate(payload)
    assert is_bot_event(event, "forge-bot") is True


def test_note_from_other_user_is_not_bot():
    payload = _load("note_mr.json")
    event = NoteEvent.model_validate(payload)
    assert event.user.username == "bob"
    assert is_bot_event(event, "forge-bot") is False


def test_mr_event_from_bot_user():
    payload = _load("mr_open.json")
    payload["user"]["username"] = "forge-bot"
    event = MergeRequestEvent.model_validate(payload)
    assert is_bot_event(event, "forge-bot") is True


def test_mr_update_with_bot_commit_author():
    payload = _load("mr_update.json")
    payload["user"]["username"] = "alice"  # user is not bot
    payload["object_attributes"]["action"] = "update"
    payload["object_attributes"]["last_commit"]["author"]["name"] = "forge-bot"
    event = MergeRequestEvent.model_validate(payload)
    assert is_bot_event(event, "forge-bot") is True


def test_mr_update_with_human_commit_author():
    payload = _load("mr_update.json")
    event = MergeRequestEvent.model_validate(payload)
    assert is_bot_event(event, "forge-bot") is False


def test_push_from_regular_user():
    payload = _load("push.json")
    event = PushEvent.model_validate(payload)
    assert is_bot_event(event, "forge-bot") is False


async def test_webhook_skips_bot_event(client):
    payload = _load("note_mr.json")
    payload["user"]["username"] = "forge-bot"

    response = await client.post(
        "/webhook",
        json=payload,
        headers={
            "X-Gitlab-Token": TEST_WEBHOOK_SECRET,
            "X-Gitlab-Event": "Note Hook",
        },
    )

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "skipped"
    assert data["reason"] == "bot-loop"


async def test_webhook_accepts_non_bot_event(client):
    payload = _load("note_mr.json")

    response = await client.post(
        "/webhook",
        json=payload,
        headers={
            "X-Gitlab-Token": TEST_WEBHOOK_SECRET,
            "X-Gitlab-Event": "Note Hook",
        },
    )

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "accepted"
    assert data["event"] == "note"
