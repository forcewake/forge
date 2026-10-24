"""Authenticated CE payloads keep numeric identities and never self-trigger."""

import json

import pytest

from forge.port_values import ReadUnavailable

from forge.providers.gitlab.ingress import GitLabIngress, command
from tests import gitlab_engine_fixture as f

SECRET = b"synthetic-webhook-secret-at-least-32-bytes"
DELIVERY = "12345678-1234-1234-1234-123456789abc"


def payload(**changes):
    value = {
        "object_kind": "note",
        "project": {"id": 123, "path_with_namespace": "new/repository"},
        "user": f.user(False),
        "object_attributes": {
            "id": 91,
            "project_id": 123,
            "noteable_type": "Issue",
            "noteable_id": 70,
            "author_id": 42,
            "action": "create",
            "note": "/go " + "a" * 64,
            "system": False,
        },
        "issue": {"id": 70, "iid": 7, "project_id": 123},
    }
    value.update(changes)
    return value


def signed(document, event="Note Hook"):
    return {
        "X-Gitlab-Token": SECRET.decode(),
        "X-Gitlab-Event-UUID": DELIVERY,
        "X-Gitlab-Event": event,
    }, json.dumps(document).encode()


@pytest.fixture
def ingress():
    return GitLabIngress("ce-1", SECRET, bots={"123": f.BOT})


def test_authenticated_note_preserves_actor_target_digest_and_durable_delivery_key(ingress):
    event = ingress.parse(*signed(payload()))
    assert (
        event.target.repo.identity == f.REPO.identity
        and event.target.number == 7
        and event.actor == f.HUMAN
    )
    assert event.delivery_key == "gitlab:ce-1:" + DELIVERY
    assert event.commands[0].argument == "a" * 64
    assert ingress.parse(*signed(payload())) == event
    headers, body = signed(payload())
    headers["X-Gitlab-Token"] = "wrong"
    with pytest.raises(ValueError):
        ingress.parse(headers, body)


@pytest.mark.parametrize(
    "user",
    [
        f.user(),
        {"id": 99, "username": "renamed", "bot": False},
        {"id": 888, "username": f.BOT.login, "bot": False},
        {"id": 777, "username": "group_42_bot_other", "bot": False},
    ],
)
def test_project_bot_native_id_login_and_all_native_bot_accounts_are_ignored(ingress, user):
    assert ingress.parse(*signed(payload(user=user))) is None


def test_forged_author_project_or_header_kind_is_refused(ingress):
    for field, value in (("author_id", 99), ("project_id", 456), ("noteable_id", 999)):
        document = payload()
        document["object_attributes"][field] = value
        with pytest.raises((ValueError, ReadUnavailable)):
            ingress.parse(*signed(document))
    with pytest.raises(ValueError):
        ingress.parse(*signed(payload(), "Issue Hook"))


def test_note_edits_and_quoted_or_later_slash_lines_do_not_execute(ingress):
    document = payload()
    document["object_attributes"]["action"] = "update"
    assert ingress.parse(*signed(document)).commands == ()
    for body in ("> /cancel", "```\n/cancel", "hello\n/cancel", " /cancel"):
        assert command(body) == ()
    assert command("/steer first\nsecond")[0].argument == "first\nsecond"


def test_native_label_transitions_plan_and_cancel_only_on_actual_change(ingress):
    document = payload(
        object_kind="issue",
        object_attributes={"id": 70, "iid": 7, "project_id": 123, "action": "update"},
        changes={"labels": {"previous": [], "current": [{"title": "forge"}]}},
    )
    assert ingress.parse(*signed(document, "Issue Hook")).commands[0].kind == "implement"
    document["changes"]["labels"] = {"previous": [{"title": "forge"}], "current": []}
    assert ingress.parse(*signed(document, "Issue Hook")).commands[0].kind == "cancel"
    document["changes"]["labels"] = {
        "previous": [{"title": "forge"}],
        "current": [{"title": "forge"}],
    }
    assert ingress.parse(*signed(document, "Issue Hook")).commands == ()
    document["object_attributes"].update(action="open", labels=[{"title": "forge"}])
    document["changes"] = {}
    assert ingress.parse(*signed(document, "Issue Hook")).commands[0].kind == "implement"


def test_merge_request_push_is_readonly_review_hint_and_forks_are_refused(ingress):
    document = payload(
        object_kind="merge_request",
        object_attributes={**f.mr(), "action": "update", "oldrev": f.OTHER},
    )
    event = ingress.parse(*signed(document, "Merge Request Hook"))
    assert (
        event.target.repo.identity == f.REPO.identity
        and event.target.number == 8
        and event.target.kind == "review"
        and event.commands == ()
    )
    document["object_attributes"]["source_project_id"] = 456
    with pytest.raises(ValueError):
        ingress.parse(*signed(document, "Merge Request Hook"))


def test_documented_pipeline_hook_and_job_retry_keep_real_native_execution_ids(ingress):
    # GitLab pipeline hooks do not carry object_attributes.project_id. The
    # authenticated top-level project is authoritative; CI re-reads the ID.
    document = payload(
        object_kind="pipeline", user=f.user(), object_attributes={"id": 700, "status": "success"}
    )
    event = ingress.parse(*signed(document, "Pipeline Hook"))
    assert event.native_id == "700" and event.native_attempt == 700 and event.commands == ()
    document = payload(
        object_kind="build",
        user=f.user(),
        project_id=123,
        pipeline_id=700,
        build_id=711,
        build_status="success",
        commit={"id": 700},
    )
    event = ingress.parse(*signed(document, "Job Hook"))
    assert event.native_id == "700" and event.native_attempt == 711


def test_webhook_duplicate_keys_nonfinite_and_bound_are_refused(ingress):
    headers, _ = signed(payload())
    for body in (
        b'{"object_kind":"note","object_kind":"issue"}',
        b'{"bad":NaN}',
        b"x" * (2 * 1024 * 1024 + 1),
    ):
        with pytest.raises((ValueError, ReadUnavailable)):
            ingress.parse(headers, body)


def test_signed_merge_request_update_carries_the_native_head_without_a_command(ingress):
    document = payload(
        object_kind="merge_request",
        object_attributes={**f.mr(), "action": "update", "last_commit": {"id": f.SHA}},
    )
    event = ingress.parse(*signed(document, "Merge Request Hook"))
    assert event.head_sha == f.SHA and event.target.kind == "review" and event.commands == ()
    headers, body = signed(document, "Merge Request Hook")
    headers["X-Gitlab-Token"] = "wrong"
    with pytest.raises(ValueError):
        ingress.parse(headers, body)
