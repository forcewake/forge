"""Signed native ingress facts never bypass engine authorization or inbox dedup."""

import hashlib
import hmac
import json

import pytest

from forge.providers.github.ingress import GitHubIngress, parse_command
from tests.test_github_rest import BOT, DISPATCH, run

SECRET = b"synthetic-webhook-secret-at-least-32-bytes"
DELIVERY = "12345678-1234-1234-1234-123456789abc"


def payload(**changes):
    value = {
        "action": "created",
        "repository": {"id": 123, "full_name": "new/repository"},
        "installation": {"id": 900},
        "sender": {"id": 42, "login": "reviewer", "type": "User"},
        "issue": {"number": 7},
        "comment": {
            "id": 91,
            "body": "/go " + "a" * 64,
            "user": {"id": 42, "login": "reviewer", "type": "User"},
        },
    }
    value.update(changes)
    return value


def signed(document, event="issue_comment"):
    body = json.dumps(document, separators=(",", ":")).encode()
    return {
        "X-Hub-Signature-256": "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest(),
        "X-GitHub-Delivery": DELIVERY,
        "X-GitHub-Event": event,
    }, body


@pytest.fixture
def ingress():
    return GitHubIngress("conn-1", SECRET, repositories={"123": 900}, bot=BOT)


def test_exact_signed_native_event_preserves_connection_actor_digest_and_delivery_key(ingress):
    headers, body = signed(payload())
    event = ingress.parse(headers, body)
    assert event.repo.identity.repository_id == "123" and event.actor.native_id == "42"
    assert event.delivery_key == "github:conn-1:" + DELIVERY
    assert event.commands[0].kind == "go" and event.commands[0].argument == "a" * 64
    # Dedup is durable inbox ownership, not an in-process seen-deliveries cache.
    assert ingress.parse(headers, body) == event
    with pytest.raises(ValueError):
        ingress.parse(headers, body + b" ")


@pytest.mark.parametrize(
    "changes",
    [
        {"installation": {"id": 901}},
        {"repository": {"id": 666, "full_name": "new/repository"}},
        {
            "comment": {
                "id": 91,
                "body": "/cancel",
                "user": {"id": 99, "login": "forge[bot]", "type": "Bot"},
            }
        },
    ],
)
def test_signed_scope_and_author_mismatch_never_authorize_a_command(ingress, changes):
    with pytest.raises(ValueError):
        ingress.parse(*signed(payload(**changes)))


@pytest.mark.parametrize(
    "sender",
    [
        {"id": 99, "login": "renamed-bot", "type": "User"},
        {"id": 777, "login": "forge[bot]", "type": "User"},
        {"id": 888, "login": "other-bot", "type": "Bot"},
    ],
)
def test_native_bot_and_login_loop_guards_ignore_notes(ingress, sender):
    assert ingress.parse(*signed(payload(sender=sender))) is None


def test_comment_edits_quotes_and_embedded_commands_never_execute(ingress):
    assert ingress.parse(*signed(payload(action="edited"))).commands == ()
    for body in ("> /cancel", "``\n/cancel", "Please /cancel", "hello\n/cancel", " /cancel"):
        assert parse_command(body) == ()
    command = parse_command("/steer First line\nSecond line")[0]
    assert command.kind == "steer" and command.argument == "First line\nSecond line"


def test_review_and_label_events_keep_exact_native_subject_kind(ingress):
    doc = payload(
        issue={
            "number": 8,
            "pull_request": {"url": "https://api.github.com/repos/new/repository/pulls/8"},
        }
    )
    event = ingress.parse(*signed(doc))
    assert event.target.kind == "review"
    event = ingress.parse(*signed(payload(action="labeled", label={"name": "forge"}), "issues"))
    assert event.target.kind == "issue" and event.commands[0].kind == "implement"


def test_workflow_bot_event_is_readonly_native_hint_with_exact_nonce(ingress):
    doc = payload(
        action="completed",
        workflow_run=run(),
        sender={"id": 99, "login": "forge[bot]", "type": "Bot"},
    )
    doc["workflow_run"].pop("repository")
    event = ingress.parse(*signed(doc, "workflow_run"))
    assert event.native_id == "700" and event.native_attempt == 1
    assert event.correlation_id == DISPATCH.nonce and event.commands == ()
    doc["workflow_run"]["head_repository"] = {"id": 666}
    with pytest.raises(ValueError):
        ingress.parse(*signed(doc, "workflow_run"))


def test_duplicate_json_nonfinite_oversize_or_invalid_delivery_are_refused(ingress):
    for body in (b'{"x":1,"x":2}', b'{"x":NaN}', b"x" * (2 * 1024 * 1024 + 1)):
        headers = {
            "X-Hub-Signature-256": "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest(),
            "X-GitHub-Delivery": DELIVERY,
            "X-GitHub-Event": "issue_comment",
        }
        with pytest.raises(ValueError):
            ingress.parse(headers, body)
    headers, body = signed(payload())
    headers["X-GitHub-Delivery"] = "../../different"
    with pytest.raises(ValueError):
        ingress.parse(headers, body)
