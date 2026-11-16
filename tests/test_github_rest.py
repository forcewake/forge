"""Native REST write, recovery and Actions contracts on bounded HTTP fixtures."""

from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest
from forge_protocol import LaneDispatch, digest

from forge.domain.verdicts import Conflict, Unknown
from forge.port_values import (
    Applied,
    CommentRef,
    Found,
    Inconclusive,
    ReadUnavailable,
    Refused,
    ReviewBody,
    ReviewRef,
)
from forge.providers.github.auth import AppAuth
from forge.providers.github.ci import GitHubCI, run_title
from forge.providers.github.client import API
from forge.providers.github.repo import GitHubRepo
from forge.providers.github.review import GitHubReview
from forge.providers.github.subject import GitHubSubject
from forge.providers.github.transport import Transport
from forge.providers.github.write import GitHubWrites
from tests import test_github_reads as read_fixture

app_key = read_fixture.app_key
REPO, SUBJECT, BOT = read_fixture.REPO, read_fixture.SUBJECT, read_fixture.BOT
SHA, OTHER = read_fixture.COMMIT, read_fixture.PARENT
REVIEW = ReviewRef(repo=REPO, number=8)
MARKER = read_fixture.MARKER
WORKFLOW = ".github/workflows/forge-lane.yml"
DISPATCH = LaneDispatch(
    forge_run_id="run-1",
    attempt=0,
    generation=2,
    server_url="https://forge.example",
    provider="github",
    repository="new/repository",
    ref="forge/run-1",
    base_sha=SHA,
    approved_digest="1" * 64,
    driver="claude_code",
    model="synthetic",
    timeout_seconds=60,
    nonce="nonce-123",
)


class Clock:
    def now(self):
        return datetime.now(timezone.utc)


class NoCommitter:
    async def commit(self, *args):
        raise AssertionError("REST must never synthesize a native commit")


def bot():
    return {"id": 99, "login": "forge[bot]", "type": "Bot"}


def pr(**changes):
    value = {
        "id": 80,
        "number": 8,
        "state": "open",
        "draft": True,
        "user": bot(),
        "head": {"ref": "forge/run-1", "sha": SHA, "repo": {"id": 123}},
        "base": {"ref": "main", "sha": OTHER, "repo": {"id": 123}},
    }
    value.update(changes)
    return value


def comment(identifier=10, body=MARKER, **changes):
    value = {
        "id": identifier,
        "body": body,
        "user": bot(),
        "issue_url": "https://api.github.com/repos/new/repository/issues/8",
        "pull_request_url": "https://api.github.com/repos/new/repository/pulls/8",
        "created_at": "2026-10-23T10:00:00Z",
        "submitted_at": "2026-10-23T10:00:00Z",
        "state": "COMMENTED",
        "commit_id": SHA,
    }
    value.update(changes)
    return value


def run(**changes):
    value = {
        "id": 700,
        "workflow_id": 71,
        "path": WORKFLOW,
        "event": "workflow_dispatch",
        "display_title": run_title(DISPATCH),
        "head_branch": DISPATCH.ref,
        "head_sha": SHA,
        "repository": {"id": 123},
        "head_repository": {"id": 123},
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "success",
    }
    value.update(changes)
    return value


class Wire(read_fixture.Wire):
    def __init__(self):
        super().__init__()
        self.metadata["default_branch"] = "main"
        self.metadata.update(archived=False, disabled=False)
        self.routes.update(
            {
                "rules/branches/forge/run-1": [],
                "branches/forge/run-1": {"name": "forge/run-1", "protected": False},
                "git/ref/heads/forge/run-1": {
                    "ref": "refs/heads/forge/run-1",
                    "object": {"type": "commit", "sha": SHA},
                },
                "pulls": [],
                "pulls/8": pr(),
                "pulls/8/reviews": [],
                "pulls/8/comments": [],
                "issues/8/comments": [],
                "actions/workflows/forge-lane.yml": {"id": 71, "path": WORKFLOW, "state": "active"},
                "actions/workflows/71/runs": {"total_count": 1, "workflow_runs": [run()]},
                "actions/runs/700/attempts/1": run(),
            }
        )
        self.mutations = {}

    def __call__(self, req):
        if req.url.path == "/app/installations/900/access_tokens":
            self.calls.append(req)
            body = json.loads(req.content)
            assert body["repository_ids"] == [123]
            return httpx.Response(
                201,
                json={
                    "token": "synthetic-token",
                    "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                    "repositories": [{"id": 123}],
                    "repository_selection": "selected",
                    "permissions": body["permissions"],
                },
            )
        resource = req.url.path.removeprefix("/repos/new/repository/")
        if req.method != "GET" and resource in self.mutations:
            self.calls.append(req)
            response = self.mutations[resource]
            return response(req) if callable(response) else response
        return super().__call__(req)


@pytest.fixture
async def provider(app_key):
    wire = Wire()
    transport = Transport(transport=httpx.MockTransport(wire))
    auth = AppAuth("conn-1", "app-9", app_key, {"123": 900}, Clock(), transport)
    api = API(auth, transport)

    async def lookup(repo, nonce):
        return DISPATCH if repo.identity == REPO.identity and nonce == DISPATCH.nonce else None

    ci = GitHubCI(
        api,
        Clock(),
        dispatch_lookup=lookup,
        lane_workflow_path=WORKFLOW,
        verification_workflow_paths={".github/workflows/ci.yml"},
    )
    review = GitHubReview(api, bot=BOT)
    writer = GitHubWrites(
        api,
        bot=BOT,
        repositories=GitHubRepo(api),
        reviews=review,
        ci=ci,
        committer=NoCommitter(),
        requirements_url="https://release.example/requirements.txt",
        requirements_sha256="a" * 64,
    )
    try:
        yield writer, review, ci, wire, GitHubSubject(api, bot=BOT)
    finally:
        await transport.close()


async def test_comment_kind_selects_correct_native_endpoint_and_owner(provider):
    writer, _, _, wire, subject = provider
    for kind, resource in (
        ("issue_comment", "issues/comments/10"),
        ("review_comment", "pulls/comments/10"),
        ("review", "pulls/8/reviews/10"),
    ):
        ref = CommentRef(subject=REVIEW, native_id="10", kind=kind)
        wire.routes[resource] = comment()
        wire.mutations[resource] = lambda req: httpx.Response(
            200, json=comment(body=json.loads(req.content)["body"])
        )
        result = await writer.update_comment(ref, "updated")
        assert isinstance(result, Applied) and result.value == ref
        assert wire.calls[-1].method == ("PUT" if kind == "review" else "PATCH")
        wire.routes[resource] = comment(body="updated")
        assert isinstance(
            await subject.probe_comment_update(ref, digest({"body": "updated"})), Found
        )
        wire.routes[resource] = comment(user={"id": 42, "login": "human", "type": "User"})
        before = len([req for req in wire.calls if req.method != "GET"])
        assert isinstance(await writer.update_comment(ref, "erase"), Refused)
        assert len([req for req in wire.calls if req.method != "GET"]) == before


async def test_comment_lost_response_is_unknown_and_recovers_unique_bot_marker(provider):
    writer, _, _, wire, subject = provider

    def lost(req):
        wire.routes["issues/7/comments"] = [
            read_fixture.comment(body=json.loads(req.content)["body"])
        ]
        raise httpx.ReadTimeout("synthetic lost response")

    wire.mutations["issues/7/comments"] = lost
    assert isinstance(await writer.post_comment(SUBJECT, "hello", MARKER), Unknown)
    result = await subject.probe_comment(SUBJECT, MARKER)
    assert isinstance(result, Found) and result.value.value.native_id == "10"
    assert (
        len(
            [
                req
                for req in wire.calls
                if req.method == "POST" and req.url.path.endswith("/comments")
            ]
        )
        == 1
    )


async def test_review_command_reply_uses_native_conversation_and_exact_target(provider):
    writer, _, _, wire, subject = provider

    def accepted(req):
        row = comment(body=json.loads(req.content)["body"])
        wire.routes["issues/8/comments"] = [row]
        return httpx.Response(201, json=row)

    wire.mutations["issues/8/comments"] = accepted
    result = await writer.post_comment(REVIEW, "Status: ready", MARKER)
    assert isinstance(result, Applied) and result.value.subject == REVIEW
    assert result.value.kind == "issue_comment"
    recovered = await subject.probe_comment(REVIEW, MARKER)
    assert isinstance(recovered, Found) and recovered.value.value == result.value
    wire.routes["issues/8/comments"][0]["issue_url"] = (
        "https://api.github.com/repos/new/repository/issues/7"
    )
    assert isinstance(await subject.probe_comment(REVIEW, MARKER), Inconclusive)


@pytest.mark.parametrize(
    "policy",
    [
        "default",
        "protected",
        "rules",
        "rules_unavailable",
        "archived",
        "disabled",
        "missing_default",
        "missing_archived",
    ],
)
async def test_branch_policy_refuses_before_any_native_mutation(provider, policy):
    writer, _, _, wire, _ = provider
    branch = "main" if policy == "default" else "forge/run-1"
    if policy == "protected":
        wire.routes["branches/forge/run-1"]["protected"] = True
    if policy == "rules":
        wire.routes["rules/branches/forge/run-1"] = [{"type": "pull_request"}]
    if policy == "rules_unavailable":
        wire.routes["rules/branches/forge/run-1"] = httpx.Response(
            403, json={"message": "forbidden"}
        )
    if policy in ("archived", "disabled"):
        wire.metadata[policy] = True
    if policy == "missing_default":
        wire.metadata.pop("default_branch")
    if policy == "missing_archived":
        wire.metadata.pop("archived")
    assert isinstance(
        await writer.ensure_branch(REPO, branch, SHA),
        Applied
        if policy == "rules_unavailable"
        else Unknown
        if policy in ("missing_default", "missing_archived")
        else Refused,
    )
    assert not [
        req
        for req in wire.calls
        if req.method != "GET" and not req.url.path.endswith("/access_tokens")
    ]


async def test_existing_branch_drift_never_resets_human_head(provider):
    writer, _, _, wire, _ = provider
    wire.routes["git/ref/heads/forge/run-1"]["object"]["sha"] = OTHER
    result = await writer.ensure_branch(REPO, "forge/run-1", SHA)
    assert isinstance(result, Conflict) and result.actual_head == OTHER
    assert not [req for req in wire.calls if req.method == "PATCH"]


async def test_new_branch_create_keeps_native_base_and_lost_response_unknown(provider):
    writer, _, _, wire, _ = provider
    wire.routes.pop("branches/forge/run-1")
    wire.routes.pop("git/ref/heads/forge/run-1")
    wire.mutations["git/refs"] = lambda req: httpx.Response(
        201, json={"ref": "refs/heads/forge/run-1", "object": {"type": "commit", "sha": SHA}}
    )
    assert isinstance(await writer.ensure_branch(REPO, "forge/run-1", SHA), Applied)
    sent = json.loads(wire.calls[-1].content)
    assert sent == {"ref": "refs/heads/forge/run-1", "sha": SHA}
    wire.mutations["git/refs"] = httpx.Response(503, json={"message": "unavailable"})
    assert isinstance(await writer.ensure_branch(REPO, "forge/run-1", SHA), Unknown)


async def test_labels_preserve_native_current_state_and_post_response_validation(provider):
    writer, _, _, wire, _ = provider
    # Existing state is an idempotent read, without replacing the complete label set.
    assert isinstance(await writer.set_label(SUBJECT, "forge", True), Applied)
    wire.mutations["issues/7/labels"] = lambda req: httpx.Response(
        200, json=[{"name": "forge"}, {"name": "additional"}]
    )
    assert isinstance(await writer.set_label(SUBJECT, "additional", True), Applied)
    assert json.loads(wire.calls[-1].content) == {"labels": ["additional"]}
    wire.mutations["issues/7/labels/forge"] = httpx.Response(200, json=[])
    assert isinstance(await writer.set_label(SUBJECT, "forge", False), Applied)


async def test_issue_feedback_reply_is_bound_to_original_native_comment(provider):
    writer, review, _, wire, _ = provider
    wire.routes["issues/8/comments"] = [
        comment(user={"id": 42, "login": "reviewer", "type": "User"})
    ]
    note = (await review.list_feedback(REVIEW, None))[0]
    assert note.ref.kind == "issue_comment"
    wire.mutations["issues/8/comments"] = lambda req: httpx.Response(
        201, json=comment(11, json.loads(req.content)["body"])
    )
    assert isinstance(await writer.reply(note, "Addressed", MARKER), Applied)
    content = json.loads(wire.calls[-1].content)["body"]
    assert "<!-- forge:reply:issue:10 -->" in content
    wire.routes["issues/8/comments"].append(comment(11, content))
    assert isinstance(await review.probe_reply(note, MARKER), Found)
    wire.routes["issues/8/comments"][-1]["body"] = content.replace(
        "reply:issue:10", "reply:issue:12"
    )
    assert isinstance(await review.probe_reply(note, MARKER), Inconclusive)


async def test_draft_ensure_is_bot_owned_exact_head_and_never_converts_ready(provider):
    writer, review, _, wire, _ = provider
    wire.mutations["pulls"] = lambda req: httpx.Response(201, json=pr())
    result = await writer.ensure_draft(REPO, "forge/run-1", "Title", "body")
    assert isinstance(result, Applied) and result.value == REVIEW
    create = json.loads(wire.calls[-1].content)
    assert create["draft"] is True and create["maintainer_can_modify"] is False
    wire.routes["pulls"] = [pr()]
    assert await review.find_draft(REPO, "forge/run-1") == REVIEW
    assert isinstance(await writer.ensure_draft(REPO, "forge/run-1", "Title", "body"), Applied)
    wire.routes["pulls"] = [pr(draft=False)]
    assert isinstance(await writer.ensure_draft(REPO, "forge/run-1", "Title", "body"), Unknown)
    wire.routes["pulls"] = [pr(), pr()]
    assert isinstance(await review.probe_draft(REPO, "forge/run-1"), Inconclusive)


async def test_review_is_candidate_bound_comment_never_human_approval(provider):
    writer, review, _, wire, _ = provider
    wire.mutations["pulls/8/reviews"] = lambda req: httpx.Response(
        200, json=comment(body=json.loads(req.content)["body"])
    )
    body = ReviewBody(candidate_sha=SHA, body="Review evidence", decision="approve")
    result = await writer.post_review(REVIEW, body, MARKER)
    assert isinstance(result, Applied) and result.value.kind == "review"
    sent = json.loads(wire.calls[-1].content)
    assert sent["event"] == "COMMENT" and sent["commit_id"] == SHA
    wire.routes["pulls/8/reviews"] = [comment(body=sent["body"])]
    assert isinstance(await review.probe_review(REVIEW, MARKER), Found)
    wire.routes["pulls/8"]["head"]["sha"] = OTHER
    assert isinstance(await writer.post_review(REVIEW, body, MARKER), Refused)


async def test_feedback_cursor_keeps_native_kind_and_reply_target(provider):
    writer, review, _, wire, _ = provider
    human = {"id": 42, "login": "reviewer", "type": "User"}
    wire.routes["pulls/8/comments"] = [comment(user=human)]
    notes = await review.list_feedback(REVIEW, None)
    assert len(notes) == 1 and notes[0].ref.kind == "review_comment"
    wire.mutations["pulls/8/comments/10/replies"] = lambda req: httpx.Response(
        201, json=comment(11, json.loads(req.content)["body"], in_reply_to_id=10)
    )
    result = await writer.reply(notes[0], "Addressed", MARKER)
    assert isinstance(result, Applied) and result.value.kind == "review_comment"
    wire.routes["pulls/8/comments"].append(comment(11, "Addressed\n\n" + MARKER, in_reply_to_id=10))
    assert isinstance(await review.probe_reply(notes[0], MARKER), Found)
    wire.routes["pulls/8/comments"][0]["body"] = "human edit"
    assert isinstance(await writer.reply(notes[0], "Again", MARKER), Refused)
    with pytest.raises(ReadUnavailable):
        await review.list_feedback(REVIEW, "invented-cursor")


async def test_dispatch_204_is_not_success_until_native_exact_nonce_is_visible(provider):
    writer, _, ci, wire, _ = provider
    wire.routes["actions/workflows/71/runs"] = {"total_count": 0, "workflow_runs": []}
    wire.mutations["actions/workflows/71/dispatches"] = httpx.Response(204)
    assert isinstance(await writer.dispatch_lane(REPO, DISPATCH), Unknown)
    sent = next(req for req in wire.calls if req.url.path.endswith("/dispatches"))
    body = json.loads(sent.content)
    assert json.loads(body["inputs"]["dispatch"])["nonce"] == DISPATCH.nonce
    assert body["ref"] == DISPATCH.ref and body["inputs"]["requirements_sha256"] == "a" * 64
    wire.routes["actions/workflows/71/runs"] = {"total_count": 1, "workflow_runs": [run()]}
    handle = await ci.find_lane(REPO, DISPATCH.nonce)
    assert handle.native_id == "700" and handle.attempt == 1
    assert (await ci.lane_status(handle)).status == "succeeded"
    assert sent.url.params.get("head_branch") is None


@pytest.mark.parametrize(
    "change",
    [
        {"path": ".github/workflows/other.yml"},
        {"head_branch": "forge/other"},
        {"head_sha": OTHER},
        {"head_repository": {"id": 666}},
        {"run_attempt": 0},
        {"event": "push"},
    ],
)
async def test_dispatch_nonce_requires_full_native_workflow_binding(provider, change):
    _, _, ci, wire, _ = provider
    wire.routes["actions/workflows/71/runs"]["workflow_runs"] = [run(**change)]
    assert isinstance(await ci.probe_dispatch(REPO, DISPATCH.nonce), Inconclusive)


async def test_duplicate_nonce_or_stale_attempt_never_adopts_lane(provider):
    _, _, ci, wire, _ = provider
    wire.routes["actions/workflows/71/runs"] = {
        "total_count": 2,
        "workflow_runs": [run(), run(id=701)],
    }
    assert isinstance(await ci.probe_dispatch(REPO, DISPATCH.nonce), Inconclusive)
    wire.routes["actions/workflows/71/runs"] = {"total_count": 1, "workflow_runs": [run()]}
    handle = await ci.find_lane(REPO, DISPATCH.nonce)
    wire.routes["actions/runs/700/attempts/1"] = run(run_attempt=2)
    with pytest.raises(ReadUnavailable):
        await ci.lane_status(handle)


async def test_verification_uses_native_attempt_jobs_not_lane_display_name(provider):
    _, _, ci, wire, _ = provider
    wire.routes["actions/runs"] = {
        "total_count": 2,
        "workflow_runs": [
            run(),
            run(id=701, path=".github/workflows/ci.yml", event="push", display_title="forge-lane"),
        ],
    }
    job = {
        "id": 901,
        "run_id": 700,
        "run_attempt": 1,
        "head_sha": SHA,
        "name": "required",
        "status": "completed",
        "conclusion": "success",
    }
    wire.routes["actions/runs/700/attempts/1/jobs"] = {"total_count": 1, "jobs": [job]}
    wire.routes["actions/runs/701/attempts/1/jobs"] = {
        "total_count": 1,
        "jobs": [{**job, "id": 902, "run_id": 701}],
    }
    checks = await ci.checks_for(REPO, SHA)
    assert [(value.role, value.native_id, value.attempt) for value in checks] == [
        ("lane", "901", 1),
        ("verification", "902", 1),
    ]
    wire.routes["actions/runs/701/attempts/1/jobs"]["jobs"][0]["head_sha"] = OTHER
    with pytest.raises(ReadUnavailable):
        await ci.checks_for(REPO, SHA)


def test_both_workflow_sources_expose_exact_nonce_and_run_identity():
    from pathlib import Path

    for path in (".github/workflows/lane.yml", "templates/github/forge-lane.yml"):
        line = next(
            value for value in Path(path).read_text().splitlines() if value.startswith("run-name:")
        )
        assert (
            line
            == "run-name: 'forge-lane:${{ fromJSON(inputs.dispatch).forge_run_id }}:${{ fromJSON(inputs.dispatch).attempt }}:${{ fromJSON(inputs.dispatch).generation }}:${{ fromJSON(inputs.dispatch).nonce }}'"
        )
