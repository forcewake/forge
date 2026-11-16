"""GitLab native identity, write and recovery contracts on sanitized HTTP fixtures."""

import json
import hashlib

import httpx
import pytest
from forge_protocol import digest

from forge.domain.verdicts import Committed, Conflict, Unknown
from forge.port_values import (
    Applied,
    CommentRef,
    Found,
    Inconclusive,
    ReadUnavailable,
    Refused,
    ReviewBody,
)
from forge.providers.gitlab.actor import GitLabActor
from forge.providers.gitlab.commit import GitLabCommitter
from forge.providers.gitlab.transport import Endpoint, Failure, Transport
from tests import gitlab_engine_fixture as f
from tests.contract.test_provider_contract import candidate


@pytest.fixture
async def case():
    wire = f.Wire()
    values = f.adapters(wire)
    yield wire, *values
    await values[0].transport.close()


async def test_native_project_ids_remain_authority_after_project_rename(case):
    wire, api, repo, _, subjects, *_ = case
    assert (await subjects.get_task(f.SUBJECT)).title == "Task"
    tree = await repo.read_tree(f.REPO, f.SHA)
    assert tree.sha == f.SHA and tree.entries[0].mode == "100755"
    assert await repo.read_blob(f.REPO, f.SHA, "old.txt") == f.BODY
    assert all("old/name" not in str(request.url) for request in wire.calls)
    with pytest.raises(ReadUnavailable):
        await api.metadata(
            f.REPO.model_copy(
                update={"identity": f.REPO.identity.model_copy(update={"connection_id": "other"})}
            )
        )
    wire.metadata["id"] = 456
    with pytest.raises(ReadUnavailable):
        await repo.head(f.REPO, "main")


async def test_immutable_blob_integrity_and_confirmed_absence_are_distinct(case):
    wire, _, repo, *_ = case
    assert await repo.read_blob(f.REPO, f.SHA, "absent.txt") is None
    wire.routes["repository/blobs/" + f.BLOB]["content"] = "Y2hhbmdlZA=="
    with pytest.raises(ReadUnavailable, match="blob_integrity_mismatch"):
        await repo.read_blob(f.REPO, f.SHA, "old.txt")
    wire.routes["repository/commits/" + f.SHA] = httpx.Response(503)
    with pytest.raises(ReadUnavailable):
        await repo.read_blob(f.REPO, f.SHA, "absent.txt")


async def test_truncated_duplicate_or_invalid_tree_is_never_an_empty_snapshot(case):
    wire, _, repo, *_ = case
    original = wire.routes["repository/tree"][0]
    wire.routes["repository/tree"] = [original, original]
    with pytest.raises(ReadUnavailable):
        await repo.read_tree(f.REPO, f.SHA)
    wire.routes["repository/tree"] = [{**original, "mode": "120000", "type": "tree"}]
    with pytest.raises(ReadUnavailable):
        await repo.read_tree(f.REPO, f.SHA)
    wire.routes["repository/tree"] = httpx.Response(
        200, json=[original], headers={"x-next-page": "3"}
    )
    with pytest.raises(ReadUnavailable):
        await repo.read_tree(f.REPO, f.SHA)


async def test_commit_marker_recovers_its_exact_parent_after_human_advances_head(case):
    wire, _, repo, *_ = case
    validated = publication_tree(wire)
    wire.routes["repository/branches/forge/run-1"]["commit"]["id"] = "c" * 40
    wire.routes["repository/commits"].insert(
        0, {"id": "c" * 40, "message": "human", "parent_ids": [f.SHA]}
    )
    result = await repo.probe_commit(f.REPO, f.DISPATCH.ref, f.MARKER, f.OTHER, validated)
    assert isinstance(result, Found) and isinstance(result.value, Committed)
    assert result.value.sha == f.SHA
    wrong = await repo.probe_commit(f.REPO, f.DISPATCH.ref, f.MARKER, "d" * 40, candidate("d" * 40))
    assert isinstance(wrong, Found) and isinstance(wrong.value, Conflict)
    wire.routes["repository/commits"].append(
        dict(wire.routes["repository/commits"][1], id="e" * 40)
    )
    assert isinstance(
        await repo.probe_commit(f.REPO, f.DISPATCH.ref, f.MARKER, f.OTHER, validated), Inconclusive
    )


def publication_tree(wire):
    validated = candidate(f.OTHER)

    def blob(path, content, mode="100644"):
        raw = content.encode()
        oid = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        return {"id": oid, "path": path, "mode": mode, "type": "blob"}

    before = [blob("old.txt", "original\r\n")]
    after = [blob("old.txt", "changed\r\n"), blob("tool.sh", "echo ok\n", "100755")]
    wire.routes["repository/commits/" + f.OTHER] = {"id": f.OTHER, "parent_ids": []}
    wire.routes["repository/tree"] = lambda request: httpx.Response(
        200, json=before if request.url.params["ref"] == f.OTHER else after
    )
    return validated


@pytest.mark.parametrize("fault", ("content", "mode", "extra", "gitlink"))
async def test_copied_commit_marker_cannot_adopt_a_wrong_tree_or_extra_unapproved_path(case, fault):
    wire, _, repo, *_ = case
    validated = publication_tree(wire)
    valid = await repo.probe_commit(f.REPO, f.DISPATCH.ref, f.MARKER, f.OTHER, validated)
    assert isinstance(valid, Found) and isinstance(valid.value, Committed)
    original = wire.routes["repository/tree"]

    def tamper(request):
        response = original(request)
        if request.url.params["ref"] == f.SHA:
            rows = json.loads(response.content)
            if fault == "content":
                rows[0]["id"] = "e" * 40
            elif fault == "mode":
                rows[0]["mode"] = "100755"
            else:
                rows.append(
                    {
                        "id": "e" * 40,
                        "path": "unapproved.txt",
                        "mode": "160000" if fault == "gitlink" else "100644",
                        "type": "commit" if fault == "gitlink" else "blob",
                    }
                )
            return httpx.Response(200, json=rows)
        return response

    wire.routes["repository/tree"] = tamper
    assert isinstance(
        await repo.probe_commit(f.REPO, f.DISPATCH.ref, f.MARKER, f.OTHER, validated), Inconclusive
    )
    assert isinstance(
        await repo.probe_commit(f.REPO, f.DISPATCH.ref, f.MARKER, f.OTHER, candidate(f.SHA)),
        Inconclusive,
    )


async def test_lost_comment_response_recovers_once_without_forgiving_duplicates(case):
    wire, _, _, _, subjects, _, writes = case

    def accepted(request):
        body = json.loads(request.content)["body"]
        wire.routes["issues/7/notes"].append(f.note(body=body))
        raise httpx.ReadError("synthetic lost response", request=request)

    wire.mutations["POST", "issues/7/notes"] = accepted
    assert isinstance(await writes.post_comment(f.SUBJECT, "plan", f.MARKER), Unknown)
    result = await subjects.probe_comment(f.SUBJECT, f.MARKER)
    assert isinstance(result, Found) and result.value.value.native_id == "10"
    wire.routes["issues/7/notes"].append(f.note(11))
    assert isinstance(await subjects.probe_comment(f.SUBJECT, f.MARKER), Inconclusive)
    wire.routes["issues/7/notes"] = []
    assert isinstance(await subjects.probe_comment(f.SUBJECT, f.MARKER), Inconclusive)


async def test_review_command_reply_uses_native_conversation_and_exact_target(case):
    wire, _, _, _, subjects, _, writes = case

    def accepted(request):
        row = f.note(body=json.loads(request.content)["body"], review=True)
        wire.routes["merge_requests/8/notes"] = [row]
        return httpx.Response(201, json=row)

    wire.mutations["POST", "merge_requests/8/notes"] = accepted
    result = await writes.post_comment(f.REVIEW, "Status: ready", f.MARKER)
    assert isinstance(result, Applied) and result.value.subject == f.REVIEW
    assert result.value.kind == "issue_comment"
    recovered = await subjects.probe_comment(f.REVIEW, f.MARKER)
    assert isinstance(recovered, Found) and recovered.value.value == result.value
    wire.routes["merge_requests/8/notes"][0]["noteable_type"] = "Issue"
    assert isinstance(await subjects.probe_comment(f.REVIEW, f.MARKER), Inconclusive)


@pytest.mark.parametrize(
    "body",
    [
        "/merge",
        "summary\n /merge",
        "summary\n\t/ready",
        "```\n/merge\n```",
        "one\r/approve",
        "\u2003/run_pipeline",
    ],
)
async def test_quick_actions_are_refused_before_every_native_note_or_description_write(case, body):
    wire, _, _, _, _, _, writes = case
    assert isinstance(await writes.post_comment(f.SUBJECT, body, f.MARKER), Refused)
    assert isinstance(
        await writes.update_comment(CommentRef(subject=f.SUBJECT, native_id="10"), body), Refused
    )
    assert isinstance(await writes.ensure_draft(f.REPO, f.DISPATCH.ref, "Title", body), Refused)
    assert isinstance(
        await writes.post_review(
            f.REVIEW, ReviewBody(candidate_sha=f.SHA, body=body, decision="inconclusive"), f.MARKER
        ),
        Refused,
    )
    assert not wire.calls


async def test_comment_update_checks_owner_target_and_exact_body_digest(case):
    wire, _, _, _, subjects, _, writes = case
    ref = CommentRef(subject=f.SUBJECT, native_id="10")
    wire.routes["issues/7/notes/10"] = f.note(body="updated")
    result = await subjects.probe_comment_update(ref, digest({"body": "updated"}))
    assert isinstance(result, Found)
    wire.routes["issues/7/notes/10"]["author"] = f.user(False)
    assert isinstance(await writes.update_comment(ref, "changed"), Refused)
    wire.routes["issues/7/notes/10"] = f.note(noteable_iid=8)
    assert isinstance(await writes.update_comment(ref, "changed"), Refused)
    assert not [request for request in wire.calls if request.method != "GET"]


async def test_project_bot_and_factory_branch_policy_prevent_unscoped_writes(case):
    wire, _, _, _, _, _, writes = case
    assert isinstance(await writes.ensure_branch(f.REPO, "main", f.SHA), Refused)
    assert isinstance(await writes.ensure_branch(f.REPO, "feature/x", f.SHA), Refused)
    wire.routes["protected_branches"] = [{"name": "forge/*"}]
    assert isinstance(await writes.ensure_branch(f.REPO, f.DISPATCH.ref, f.SHA), Refused)
    wire.routes["protected_branches"] = []
    wire.routes["repository/branches/forge/new"] = httpx.Response(404)
    wire.mutations["POST", "repository/branches"] = {
        "name": "forge/new",
        "protected": False,
        "commit": {"id": f.SHA},
    }
    wire.user = {"id": 99, "username": "global-admin", "bot": False}
    assert isinstance(await writes.ensure_branch(f.REPO, "forge/new", f.SHA), Unknown)
    assert not [request for request in wire.calls if request.method != "GET"]


@pytest.mark.parametrize(
    "field,value",
    (
        ("archived", True),
        ("archived", None),
        ("default_branch", None),
        ("default_branch", f.DISPATCH.ref),
    ),
)
async def test_unknown_or_archived_project_policy_never_permits_mutation(case, field, value):
    wire, _, _, _, _, _, writes = case
    wire.metadata[field] = value
    result = await writes.ensure_branch(f.REPO, f.DISPATCH.ref, f.SHA)
    assert isinstance(result, (Unknown, Refused))
    assert not [request for request in wire.calls if request.method != "GET"]


async def test_project_metadata_discards_unneeded_native_credentials(case):
    wire, api, *_ = case
    wire.metadata["runners_token"] = "synthetic-sensitive-metadata"
    reply = await api.metadata(f.REPO)
    assert "runners_token" not in reply.document
    assert "synthetic-sensitive-metadata" not in repr(reply)


async def test_branch_creation_owns_creation_and_never_uses_start_branch(case):
    wire, _, _, _, _, _, writes = case
    wire.mutations["POST", "repository/branches"] = {
        "name": "forge/new",
        "protected": False,
        "commit": {"id": f.SHA},
    }
    result = await writes.ensure_branch(f.REPO, "forge/new", f.SHA)
    assert isinstance(result, Applied)
    mutations = [request for request in wire.calls if request.method != "GET"]
    assert len(mutations) == 1 and json.loads(mutations[0].content) == {
        "branch": "forge/new",
        "ref": f.SHA,
    }


async def test_draft_creation_and_marker_probes_keep_native_comment_kinds(case):
    wire, _, _, review, _, _, writes = case
    wire.mutations["POST", "merge_requests"] = f.mr()
    created = await writes.ensure_draft(f.REPO, f.DISPATCH.ref, "Title", "Description")
    assert isinstance(created, Applied) and created.value == f.REVIEW
    wire.routes["merge_requests"] = [f.mr()]
    assert isinstance(await review.probe_draft(f.REPO, f.DISPATCH.ref), Found)
    wire.mutations["POST", "merge_requests/8/notes"] = lambda request: httpx.Response(
        201, json=f.note(body=json.loads(request.content)["body"], review=True)
    )
    result = await writes.post_review(
        f.REVIEW, ReviewBody(candidate_sha=f.SHA, body="Evidence", decision="approve"), f.MARKER
    )
    assert isinstance(result, Applied) and result.value.kind == "review"
    body = json.loads(wire.calls[-1].content)["body"]
    wire.routes["merge_requests/8/notes"] = [f.note(body=body, review=True)]
    assert (await review.probe_review(f.REVIEW, f.MARKER)).value.value == result.value
    assert all(not request.url.path.endswith("/merge") for request in wire.calls)


async def test_native_discussion_cursor_and_reply_bind_original_feedback(case):
    wire, _, _, review, _, _, writes = case
    wire.routes["merge_requests/8/discussions"] = [
        {"id": f.DISCUSSION, "notes": [f.note(21, "Please fix", review=True, bot=False)]}
    ]
    note = (await review.list_feedback(f.REVIEW, None))[0]
    resource = f"merge_requests/8/discussions/{f.DISCUSSION}/notes"
    wire.mutations["POST", resource] = lambda request: httpx.Response(
        201, json=f.note(22, json.loads(request.content)["body"], review=True)
    )
    result = await writes.reply(note, "Fixed", f.MARKER)
    assert isinstance(result, Applied) and result.value.kind == "review_comment"
    body = json.loads(wire.calls[-1].content)["body"]
    wire.routes["merge_requests/8/discussions"][0]["notes"].append(f.note(22, body, review=True))
    assert (await review.probe_reply(note, f.MARKER)).value.value == result.value
    forged = note.model_copy(update={"body": "different"})
    assert isinstance(await writes.reply(forged, "Fixed", "another-marker"), Refused)


async def test_pipeline_and_job_execution_ids_are_real_attempts_and_lane_is_not_verification(case):
    wire, _, _, _, _, ci, writes = case
    handle = await ci.find_lane(f.REPO, f.DISPATCH.nonce)
    assert handle.native_id == "700" and handle.attempt == 700
    checks = await ci.checks_for(f.REPO, f.SHA)
    assert checks[0].role == "lane" and checks[0].attempt == 701
    wire.routes["pipelines/700/jobs"][0]["id"] = 711
    assert (await ci.checks_for(f.REPO, f.SHA))[0].attempt == 711
    wire.mutations["POST", "pipeline"] = f.pipeline(800)
    dispatch = await writes.dispatch_lane(f.REPO, f.DISPATCH)
    assert isinstance(dispatch, Applied) and dispatch.value.attempt == 800
    variables = {
        row["key"]: row["value"] for row in json.loads(wire.calls[-1].content)["variables"]
    }
    assert variables["FORGE_LANE_REQUIREMENTS_SHA256"] == "1" * 64
    assert variables["FORGE_PUBLIC_URL"] == "https://forge.example"
    assert "TOKEN" not in " ".join(variables)


async def test_independent_checks_bind_sha_source_job_and_duplicate_execution(case):
    wire, _, _, _, _, ci, _ = case
    wire.routes["pipelines/700"]["source"] = "push"
    wire.routes["pipelines/700/variables"] = []
    assert (await ci.checks_for(f.REPO, f.SHA))[0].role == "verification"
    wire.routes["pipelines/700/jobs"][0]["pipeline"]["sha"] = f.OTHER
    with pytest.raises(ReadUnavailable):
        await ci.checks_for(f.REPO, f.SHA)
    wire.routes["pipelines/700/jobs"][0]["pipeline"]["sha"] = f.SHA
    wire.routes["pipelines/700/jobs"].append(dict(wire.routes["pipelines/700/jobs"][0], id=702))
    with pytest.raises(ReadUnavailable):
        await ci.checks_for(f.REPO, f.SHA)


async def test_numeric_approver_allowlist_also_requires_current_native_membership(case):
    wire, api, *_ = case
    actor = GitLabActor(api, bot=f.BOT, approver_ids=["42"])
    assert await actor.is_approver(f.HUMAN, f.SUBJECT)
    assert not await actor.is_approver(f.BOT, f.SUBJECT)
    wire.routes["members/all/42"]["access_level"] = 20
    assert not await actor.is_approver(f.HUMAN, f.SUBJECT)
    wire.routes["members/all/42"]["username"] = "renamed"
    with pytest.raises(ReadUnavailable):
        await actor.is_approver(f.HUMAN, f.SUBJECT)


async def test_commit_wrapper_uses_scoped_https_git_and_passes_exact_validated_manifest(
    case, monkeypatch
):
    wire, api, repo, *_ = case
    captured = []
    validated = candidate(f.SHA)

    async def publish(target, payload, expected_head, marker, *, stamp):
        captured.append((target, payload, expected_head, marker, stamp))
        return f.OTHER

    monkeypatch.setattr("forge.providers.gitlab.commit.publish", publish)
    result = await GitLabCommitter(api, repo, f.Clock()).commit(f.REPO, validated, f.SHA, f.MARKER)
    assert isinstance(result, Committed) and result.sha == f.OTHER
    target, actual, parent, marker, stamp = captured[0]
    assert target.url == "https://ce.example/new/repository.git" and "synthetic-write" not in repr(
        target
    )
    assert actual is validated and parent == f.SHA and marker == f.MARKER and stamp > 0
    assert actual.manifest[1].mode == "100755"
    assert not [request for request in wire.calls if request.method != "GET"]


@pytest.mark.parametrize("status", [301, 401, 403, 429, 503])
async def test_transport_never_retries_mutations_or_retains_credentials_in_failures(status):
    calls = []

    def fail(request):
        calls.append(request)
        return httpx.Response(
            status,
            json={"message": "synthetic-secret"},
            headers={"location": "https://elsewhere.example", "x-request-id": "safe-id"},
        )

    transport = Transport(Endpoint("https://ce.example"), transport=httpx.MockTransport(fail))
    try:
        with pytest.raises(Failure) as caught:
            await transport.request("POST", "/projects/123/pipeline", "synthetic-secret")
        assert (
            len(calls) == 1 and "synthetic-secret" not in str(caught.value) + caught.value.evidence
        )
    finally:
        await transport.close()


async def test_a_read_timeout_is_a_typed_transport_failure_and_does_not_propagate():
    """A hung GitLab stub used to surface as httpx.ReadTimeout and stall the run.

    The current client bounds the call and returns a typed transport failure.
    """

    def timeout(request):
        raise httpx.ReadTimeout("synthetic read timeout", request=request)

    transport = Transport(Endpoint("https://ce.example"), transport=httpx.MockTransport(timeout))
    try:
        with pytest.raises(Failure) as caught:
            await transport.request("GET", "/projects/123", "synthetic-token")
        assert caught.value.reason == "transport_unavailable"
        assert "synthetic read timeout" not in str(caught.value)
    finally:
        await transport.close()
