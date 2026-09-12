import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from forge.gateway.parser import parse_webhook
from forge.gitlab.events import (
    GitLabEvent,
    MergeRequestEvent,
    NoteEvent,
    PipelineEvent,
    PushEvent,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_parse_mr_open():
    payload = _load("mr_open.json")
    event = parse_webhook("Merge Request Hook", payload)

    assert isinstance(event, MergeRequestEvent)
    assert event.object_kind == "merge_request"
    assert event.object_attributes.iid == 7
    assert event.object_attributes.action == "open"
    assert event.object_attributes.source_branch == "feature/auth"
    assert event.object_attributes.target_branch == "main"
    assert event.object_attributes.title == "Add user authentication"
    assert event.user.username == "alice"
    assert event.project.path_with_namespace == "mygroup/my-project"
    assert event.object_attributes.draft is False
    assert len(event.labels) == 1
    assert event.labels[0].title == "feature"


def test_parse_mr_update():
    payload = _load("mr_update.json")
    event = parse_webhook("Merge Request Hook", payload)

    assert isinstance(event, MergeRequestEvent)
    assert event.object_attributes.action == "update"
    assert event.object_attributes.last_commit is not None
    assert event.object_attributes.last_commit.message == "Fix token refresh edge case"


def test_parse_note_on_mr():
    payload = _load("note_mr.json")
    event = parse_webhook("Note Hook", payload)

    assert isinstance(event, NoteEvent)
    assert event.object_kind == "note"
    assert event.object_attributes.noteable_type == "MergeRequest"
    assert "@forge review" in event.object_attributes.note
    assert event.merge_request is not None
    assert event.merge_request.iid == 7
    assert event.user.username == "bob"


def test_parse_note_on_issue():
    payload = _load("note_issue.json")
    event = parse_webhook("Note Hook", payload)

    assert isinstance(event, NoteEvent)
    assert event.object_attributes.noteable_type == "Issue"
    assert "@forge explain" in event.object_attributes.note
    assert event.issue is not None
    assert event.issue.iid == 5
    assert event.merge_request is None


def test_parse_pipeline_failed():
    payload = _load("pipeline_failed.json")
    event = parse_webhook("Pipeline Hook", payload)

    assert isinstance(event, PipelineEvent)
    assert event.object_kind == "pipeline"
    assert event.object_attributes.status == "failed"
    assert event.object_attributes.ref == "feature/auth"
    assert event.merge_request is not None
    assert event.merge_request.iid == 7
    assert len(event.builds) == 3

    failed_build = [b for b in event.builds if b.status == "failed"][0]
    assert failed_build.name == "test-unit"
    assert failed_build.failure_reason == "script_failure"


def test_parse_push():
    payload = _load("push.json")
    event = parse_webhook("Push Hook", payload)

    assert isinstance(event, PushEvent)
    assert event.object_kind == "push"
    assert event.total_commits_count == 3
    assert len(event.commits) == 3
    assert event.ref == "refs/heads/feature/auth"
    assert event.commits[0].author.name == "Alice Dev"
    assert event.user.username == "alice"


def test_unknown_event_returns_base_model():
    payload = {
        "object_kind": "wiki_page",
        "user": {"id": 1, "name": "Test", "username": "test"},
        "project": {
            "id": 42,
            "name": "proj",
            "path_with_namespace": "g/p",
            "web_url": "https://gitlab.example.com/g/p",
        },
    }
    event = parse_webhook("Wiki Page Hook", payload)

    assert type(event) is GitLabEvent
    assert event.object_kind == "wiki_page"


def test_extra_fields_ignored():
    payload = _load("push.json")
    payload["some_future_field"] = "should be ignored"
    payload["another_unknown"] = {"nested": True}

    event = parse_webhook("Push Hook", payload)
    assert isinstance(event, PushEvent)


def test_malformed_payload_raises_validation_error():
    with pytest.raises(ValidationError):
        parse_webhook("Merge Request Hook", {"object_kind": "merge_request"})


def test_empty_payload_raises_validation_error():
    with pytest.raises(ValidationError):
        parse_webhook("Merge Request Hook", {})
