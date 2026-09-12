"""Webhook payload contract tests for ``forge.gateway.parser.parse_webhook``.

Fixtures under ``fixtures/gitlab/*_webhook.json`` mirror the DOCUMENTED
GitLab webhook payload shapes:

https://docs.gitlab.com/user/project/integrations/webhook_events/
- Push events         → X-Gitlab-Event: Push Hook
- Merge request events → X-Gitlab-Event: Merge Request Hook
- Note events         → X-Gitlab-Event: Note Hook
- Pipeline events     → X-Gitlab-Event: Pipeline Hook

These payloads are the raw data the gateway consumes; the tests assert that
key documented fields survive parsing intact. This includes the pipeline
webhook authored by the bot itself (``user.username == "forge-bot"``),
which grounds the M1 fix: the gateway must stop blanket-dropping
bot-authored pipeline events, so the parser must round-trip
``user.username`` faithfully (forge.gateway.validator.is_bot_event is the
current drop mechanism, forge/gateway/router.py:124).
"""

from __future__ import annotations

from typing import Any

from forge.gateway.parser import EVENT_KIND_MAP, parse_webhook
from forge.gitlab.events import GitLabEvent

from .conftest import load_fixture

# Documented values of the X-Gitlab-Event header per webhook type.
HEADER_BY_FIXTURE: dict[str, str] = {
    "mr_webhook": "Merge Request Hook",
    "note_webhook": "Note Hook",
    "pipeline_webhook": "Pipeline Hook",
    "push_webhook": "Push Hook",
}


async def test_merge_request_webhook_parses(fixtures) -> None:  # type: ignore[no-untyped-def]
    """Merge Request Hook payload parses into MergeRequestEvent.

    Documented key fields: object_kind, user, project, object_attributes
    (iid, source_branch, target_branch, state, action), labels, changes.
    """
    payload: dict[str, Any] = load_fixture("mr_webhook")

    event = parse_webhook(HEADER_BY_FIXTURE["mr_webhook"], payload)

    assert isinstance(event, GitLabEvent)
    assert event.object_kind == "merge_request"
    assert event.user is not None and event.user.username == "forge-user"
    assert event.project is not None
    assert event.project.path_with_namespace == "group/project"

    mr = event.object_attributes
    assert mr.iid == 7
    assert mr.state == "opened"
    assert mr.action == "open"
    assert mr.source_branch == "feature/token-rotation"
    assert mr.target_branch == "main"
    assert mr.last_commit is not None
    assert mr.last_commit.id == "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0a1"
    assert event.labels[0].title == "backend"


async def test_note_webhook_parses(fixtures) -> None:  # type: ignore[no-untyped-def]
    """Note Hook payload parses into NoteEvent with discussion context.

    Documented key fields: object_attributes.note, noteable_type,
    discussion_id, and the embedded merge_request object.
    """
    payload: dict[str, Any] = load_fixture("note_webhook")

    event = parse_webhook(HEADER_BY_FIXTURE["note_webhook"], payload)

    assert event.object_kind == "note"
    attrs = event.object_attributes
    assert attrs.note == "forge review"
    assert attrs.noteable_type == "MergeRequest"
    # discussion_id must survive: it is the reply anchor for discussions.
    assert attrs.discussion_id == "abc123def456abc123def456abc123de"
    assert event.merge_request is not None
    assert event.merge_request.iid == 7
    assert event.merge_request.source_branch == "feature/token-rotation"


async def test_push_webhook_parses(fixtures) -> None:  # type: ignore[no-untyped-def]
    """Push Hook payload parses into PushEvent (top-level fields, no
    object_attributes).

    Documented key fields: before/after/ref/checkout_sha, user_username,
    project_id, commits, total_commits_count.
    """
    payload: dict[str, Any] = load_fixture("push_webhook")

    event = parse_webhook(HEADER_BY_FIXTURE["push_webhook"], payload)

    assert event.object_kind == "push"
    assert event.ref == "refs/heads/feature/token-rotation"
    assert event.after == "c9d8b7a6f5e4d3c2b1a0f9e8d7c6b5a4f3e2d1c0"
    assert event.user_username == "forge-user"
    assert event.project_id == 42
    assert event.total_commits_count == 1
    assert event.commits is not None and len(event.commits) == 1
    assert event.commits[0].modified == ["src/config.py"]


async def test_pipeline_webhook_parses(fixtures) -> None:  # type: ignore[no-untyped-def]
    """Pipeline Hook payload parses into PipelineEvent.

    Documented key fields: object_attributes (id, ref, status, stages,
    source, sha), merge_request, builds array.
    """
    payload: dict[str, Any] = load_fixture("pipeline_webhook")

    event = parse_webhook(HEADER_BY_FIXTURE["pipeline_webhook"], payload)

    assert event.object_kind == "pipeline"
    attrs = event.object_attributes
    assert attrs.id == 200
    assert attrs.status == "failed"
    assert attrs.ref == "feature/token-rotation"
    assert attrs.stages == ["build", "test"]
    assert attrs.source == "push"
    assert event.merge_request is not None and event.merge_request.iid == 7
    assert len(event.builds) == 2
    failed = event.builds[1]
    assert failed.name == "unit-tests"
    assert failed.status == "failed"
    assert failed.failure_reason == "script_failure"


async def test_pipeline_webhook_preserves_bot_username(fixtures) -> None:  # type: ignore[no-untyped-def]
    """A pipeline webhook authored by the bot parses and keeps user.username.

    The M1 fix requires the gateway to stop dropping the bot's OWN pipeline
    events (forge/gateway/router.py routes them through
    forge.gateway.validator.is_bot_event, which drops anything whose
    event.user.username equals FORGE_BOT_USERNAME). Grounding for that fix:
    the documented pipeline payload carries the trigger user, and the
    parser must round-trip "forge-bot" so downstream code can tell a
    bot-triggered pipeline from any other.
    """
    payload: dict[str, Any] = load_fixture("pipeline_webhook")
    assert payload["user"]["username"] == "forge-bot"

    event = parse_webhook(HEADER_BY_FIXTURE["pipeline_webhook"], payload)

    assert event.user is not None
    assert event.user.id == 99
    assert event.user.name == "Forge Bot"
    assert event.user.username == "forge-bot"


async def test_event_kind_map_agrees_with_parse_webhook(fixtures) -> None:  # type: ignore[no-untyped-def]
    """EVENT_KIND_MAP must map each fixture's object_kind to the same model
    that parse_webhook picks for the documented X-Gitlab-Event header.

    The worker re-hydrates queued payloads through EVENT_KIND_MAP, so both
    entry points must stay in sync.
    """
    for fixture_name, header in HEADER_BY_FIXTURE.items():
        payload: dict[str, Any] = load_fixture(fixture_name)

        via_header = parse_webhook(header, payload)
        via_kind_map = EVENT_KIND_MAP[payload["object_kind"]].model_validate(payload)

        assert type(via_header) is type(via_kind_map), fixture_name
        assert via_header.object_kind == payload["object_kind"]


async def test_unknown_event_header_falls_back_to_base_event(fixtures) -> None:  # type: ignore[no-untyped-def]
    """An undocumented X-Gitlab-Event header parses via the base GitLabEvent
    model instead of failing (documented parser behavior).

    New GitLab webhook types must not crash the gateway; they degrade to
    the common object_kind/user/project subset.
    """
    payload: dict[str, Any] = load_fixture("mr_webhook")

    event = parse_webhook("Wiki Page Hook", payload)

    assert type(event) is GitLabEvent
    assert event.object_kind == "merge_request"
    assert event.user is not None and event.user.username == "forge-user"
