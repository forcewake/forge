"""Recovery and mutations retain exact native targets without destructive replacement."""

import json

import httpx
import pytest
from forge_protocol import digest

from forge.domain.verdicts import Conflict, Unknown
from forge.port_values import Applied, CommentRef, Found, Inconclusive, ReadUnavailable, Refused
from forge.providers.gitlab.commit import GitLabCommitter
from forge.providers.git_process import GitFailure
from tests import gitlab_engine_fixture as f
from tests.contract.test_provider_contract import candidate


@pytest.fixture
async def case():
    wire = f.Wire()
    values = f.adapters(wire)
    yield wire, *values
    await values[0].transport.close()


async def test_branch_probe_preserves_applied_conflict_and_unknown_absence(case):
    wire, _, repo, *_ = case
    found = await repo.probe_branch(f.REPO, f.DISPATCH.ref, f.SHA)
    assert isinstance(found, Found) and isinstance(found.value, Applied)
    conflict = await repo.probe_branch(f.REPO, f.DISPATCH.ref, f.OTHER)
    assert isinstance(conflict, Found) and isinstance(conflict.value, Conflict)
    wire.routes["repository/branches/forge/run-1"] = httpx.Response(404)
    assert isinstance(await repo.probe_branch(f.REPO, f.DISPATCH.ref, f.SHA), Inconclusive)


async def test_label_mutation_changes_only_the_requested_label_and_probe_observes_exact_state(case):
    wire, _, _, _, subjects, _, writes = case
    original = ["forge", "human-label"]
    wire.routes["issues/7"]["labels"] = original.copy()

    def modify(request):
        body = json.loads(request.content)
        assert set(body) in ({"add_labels"}, {"remove_labels"})
        labels = wire.routes["issues/7"]["labels"]
        if "add_labels" in body:
            labels.append(body["add_labels"])
        else:
            labels.remove(body["remove_labels"])
        return httpx.Response(200, json=wire.routes["issues/7"])

    wire.mutations["PUT", "issues/7"] = modify
    assert isinstance(await writes.set_label(f.SUBJECT, "ready", True), Applied)
    assert isinstance(await subjects.probe_label(f.SUBJECT, "ready", True), Found)
    assert isinstance(await subjects.probe_label(f.SUBJECT, "ready", False), Inconclusive)
    assert isinstance(await writes.set_label(f.SUBJECT, "ready", False), Applied)
    assert wire.routes["issues/7"]["labels"] == original
    assert isinstance(await writes.set_label(f.SUBJECT, "one,two", True), Refused)


async def test_updates_keep_review_and_inline_comment_namespaces_separate(case):
    wire, _, _, _, subjects, _, writes = case
    wire.routes["merge_requests/8/notes/10"] = f.note(review=True)
    wire.mutations["PUT", "merge_requests/8/notes/10"] = f.note(body="updated", review=True)
    ref = CommentRef(subject=f.REVIEW, native_id="10", kind="review")
    result = await writes.update_comment(ref, "updated")
    assert isinstance(result, Applied) and result.value == ref
    wire.routes["merge_requests/8/discussions"] = [
        {"id": f.DISCUSSION, "notes": [f.note(21, "original", review=True)]}
    ]
    resource = f"merge_requests/8/discussions/{f.DISCUSSION}/notes/21"
    wire.mutations["PUT", resource] = f.note(21, "updated inline", review=True)
    inline = CommentRef(subject=f.REVIEW, native_id="21", kind="review_comment")
    result = await writes.update_comment(inline, "updated inline")
    assert isinstance(result, Applied) and result.value == inline
    wire.routes["merge_requests/8/discussions"][0]["notes"][0]["body"] = "updated inline"
    assert isinstance(
        await subjects.probe_comment_update(inline, digest({"body": "updated inline"})), Found
    )


async def test_native_lane_status_refuses_a_rebound_handle_and_feedback_cursor_does_not_skip(case):
    wire, _, _, review, _, ci, _ = case
    handle = await ci.find_lane(f.REPO, f.DISPATCH.nonce)
    assert (await ci.lane_status(handle)).status == "succeeded"
    with pytest.raises(ReadUnavailable):
        await ci.lane_status(handle.model_copy(update={"attempt": 701}))
    wire.routes["merge_requests/8/discussions"] = [
        {
            "id": f.DISCUSSION,
            "notes": [
                f.note(21, "first", review=True, bot=False),
                f.note(22, "second", review=True, bot=False, created_at="2026-10-23T11:00:00Z"),
            ],
        }
    ]
    notes = await review.list_feedback(f.REVIEW, None)
    assert await review.list_feedback(f.REVIEW, notes[0].cursor) == (notes[1],)
    with pytest.raises(ReadUnavailable):
        await review.list_feedback(f.REVIEW, "not-visible")


@pytest.mark.parametrize(
    "row",
    [
        f.mr(draft=False),
        f.mr(state="closed"),
        f.mr(source_project_id=456),
        f.mr(author=f.user(False)),
    ],
)
async def test_foreign_or_changed_draft_cannot_be_adopted(case, row):
    wire, _, _, review, *_ = case
    wire.routes["merge_requests"] = [row]
    assert isinstance(await review.probe_draft(f.REPO, f.DISPATCH.ref), Inconclusive)


async def test_git_uncertainty_is_not_success_and_native_stale_ref_remains_conflict(
    case, monkeypatch
):
    _, api, repo, *_ = case
    payload = candidate(f.SHA)

    async def unavailable(*args, **kwargs):
        raise GitFailure("git_transport_unavailable")

    monkeypatch.setattr("forge.providers.gitlab.commit.publish", unavailable)
    committer = GitLabCommitter(api, repo, f.Clock())
    assert isinstance(await committer.commit(f.REPO, payload, f.SHA, f.MARKER), Unknown)

    async def stale(*args, **kwargs):
        raise GitFailure("git_ref_conflict", stale=True)

    monkeypatch.setattr("forge.providers.gitlab.commit.publish", stale)
    result = await committer.commit(f.REPO, payload, f.OTHER, f.MARKER)
    assert isinstance(result, Conflict) and result.actual_head == f.SHA
