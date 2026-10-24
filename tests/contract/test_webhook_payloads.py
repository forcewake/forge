"""Historical CE captures through authenticated native ingress, never raw authority.

The narrow typed event retains routing facts. Native reads supply the current
full task/feedback/CI evidence before mutable execution; raw push and unknown
hooks cannot degrade into executable generic events.
"""

import pytest

from forge.port_values import ReadUnavailable, ReviewRef
from tests.contract.conftest import HEADERS, complete_note, load_fixture, signed


async def test_merge_request_webhook_parses(ingress):
    payload = load_fixture("mr_webhook")
    event = ingress.parse(*signed(payload, HEADERS["mr_webhook"]))
    assert event.repo.identity.repository_id == "42"
    assert event.repo.full_name == "group/project" and event.actor.native_id == "11"
    assert event.actor.login == "forge-user" and isinstance(event.target, ReviewRef)
    assert event.target.number == 7 and event.event == "merge_request" and event.action == "open"
    assert event.commands == ()
    payload["object_attributes"]["action"] = "update"
    event = ingress.parse(*signed(payload, HEADERS["mr_webhook"]))
    assert event.head_sha == payload["object_attributes"]["last_commit"]["id"]


async def test_note_webhook_parses(ingress):
    # The archived note capture omits action/native project target bindings; refuse it.
    with pytest.raises((ValueError, ReadUnavailable)):
        ingress.parse(*signed(load_fixture("note_webhook"), HEADERS["note_webhook"]))
    payload = complete_note()
    event = ingress.parse(*signed(payload, HEADERS["note_webhook"]))
    assert event.native_id == "501" and event.actor.native_id == "11"
    assert event.target.number == 7 and isinstance(event.target, ReviewRef)
    assert event.commands == ()  # historical natural-language review request is not a command
    payload["object_attributes"]["note"] = "/ask Explain the finding"
    event = ingress.parse(*signed(payload, HEADERS["note_webhook"]))
    assert [(command.kind, command.argument) for command in event.commands] == [
        ("ask", "Explain the finding")
    ]
    payload["object_attributes"]["author_id"] = 99
    with pytest.raises(ValueError):
        ingress.parse(*signed(payload, HEADERS["note_webhook"]))


async def test_push_webhook_parses(ingress):
    payload = load_fixture("push_webhook")
    assert "user" not in payload and payload["user_username"] == "forge-user"
    # Captured pushes carry no native command/target identity. No generic-event fallback.
    with pytest.raises(ValueError):
        ingress.parse(*signed(payload, HEADERS["push_webhook"]))


async def test_pipeline_webhook_parses(ingress):
    payload = load_fixture("pipeline_webhook")
    event = ingress.parse(*signed(payload, HEADERS["pipeline_webhook"]))
    assert event.event == "pipeline" and event.action == "failed"
    assert event.native_id == "200" and event.native_attempt == 200
    assert event.repo.identity.repository_id == "42" and event.commands == ()
    assert event.target is None
    # A signed webhook remains a wake fact; native CI adapters verify SHA, jobs and failures.
    payload["object_attributes"]["project_id"] = 999
    with pytest.raises(ValueError):
        ingress.parse(*signed(payload, HEADERS["pipeline_webhook"]))


async def test_pipeline_webhook_preserves_bot_username(ingress):
    payload = load_fixture("pipeline_webhook")
    event = ingress.parse(*signed(payload, HEADERS["pipeline_webhook"]))
    assert event.actor.native_id == "99" and event.actor.login == "forge-bot"
    assert event.commands == () and event.native_id == "200"
    payload["user"]["bot"] = True
    assert ingress.parse(*signed(payload, HEADERS["pipeline_webhook"])).actor.is_bot
    # The identical bot identity cannot self-trigger a note command.
    note = complete_note()
    note["user"] = payload["user"]
    note["object_attributes"].update(author_id=99, note="/implement")
    assert ingress.parse(*signed(note, HEADERS["note_webhook"])) is None


async def test_event_kind_map_agrees_with_parse_webhook(ingress):
    for fixture_name in ("mr_webhook", "pipeline_webhook"):
        payload = load_fixture(fixture_name)
        headers, body = signed(payload, HEADERS[fixture_name])
        event = ingress.parse(headers, body)
        assert event.event == payload["object_kind"]
        wrong, _ = signed(payload, "Issue Hook")
        with pytest.raises(ValueError):
            ingress.parse(wrong, body)
        headers["X-Gitlab-Token"] = "synthetic-wrong-secret"
        with pytest.raises(ValueError):
            ingress.parse(headers, body)


async def test_unknown_event_header_falls_back_to_base_event(ingress):
    # Historical generic fallback is retired. Even authentic unknown hooks cannot create commands.
    payload = load_fixture("mr_webhook")
    with pytest.raises(ValueError):
        ingress.parse(*signed(payload, "Wiki Page Hook"))
    payload["object_kind"] = "wiki_page"
    with pytest.raises(ValueError):
        ingress.parse(*signed(payload, "Merge Request Hook"))
