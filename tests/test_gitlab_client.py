"""Retained CE client lessons on selected native project adapters.

Historical discovery, issue creation, arbitrary commit comments and artifact
helpers are retired. Their applicable identity, confinement, complete-evidence
and one-write lessons exercise the actual narrow native ports instead.
"""

import json

import httpx
import pytest

from forge.domain.blob import BlobContent
from forge.port_values import Applied, DefinitelyAbsent, Found, Inconclusive, ReadUnavailable
from forge.domain.verdicts import Unknown
from tests import gitlab_engine_fixture as gl
from tests import native_clients_fixture as fixture
from tests.native_clients_fixture import blob, resource_calls

gitlab = fixture.gitlab


async def test_get_merge_request(gitlab):
    row = await gitlab.reviews.document(gitlab.review)
    assert row["iid"] == gitlab.review.number and row["source_branch"] == gitlab.dispatch.ref
    assert row["project_id"] == 123 and row["source_project_id"] == row["target_project_id"]


async def test_get_merge_request_diffs(gitlab):
    # Exact immutable native tree replaces provider diff fragments and their truncation flags.
    tree = await gitlab.repositories.read_tree(gitlab.repo, gitlab.sha)
    assert tree.sha == gitlab.sha and tree.entries[0].path == "old.txt"
    gitlab.wire.routes["repository/tree"][0]["path"] = "../escape"
    with pytest.raises(ReadUnavailable):
        await gitlab.repositories.read_tree(gitlab.repo, gitlab.sha)


async def test_pagination_two_pages(gitlab):
    def page(request):
        number = int(request.url.params["page"])
        return httpx.Response(
            200,
            json=[gl.note(1)] if number == 1 else [gl.note(2)],
            headers={"x-next-page": "2"} if number == 1 else {},
        )

    gitlab.wire.routes["issues/7/notes"] = page
    rows = await gitlab.subjects.notes(gitlab.subject)
    assert [row["id"] for row in rows] == [1, 2]
    assert [r.url.params["page"] for r in resource_calls(gitlab, "/notes")] == ["1", "2"]


async def test_create_mr_note(gitlab):
    def accepted(request):
        return httpx.Response(
            201, json=gl.note(body=json.loads(request.content)["body"], review=True)
        )

    gitlab.wire.mutations["POST", "merge_requests/8/notes"] = accepted
    result = await gitlab.writes.post_comment(gitlab.review, "Review evidence", gl.MARKER)
    assert isinstance(result, Applied) and result.value.subject == gitlab.review
    assert result.value.native_id == "10"
    assert json.loads(resource_calls(gitlab, "/notes", "POST")[0].content)["body"].endswith(
        gl.MARKER
    )


async def test_create_mr_discussion_with_position(gitlab):
    # New writes reply to exact persisted native feedback; no guessed fresh diff position.
    note = gl.note(review=True, bot=False, position={"new_path": "old.txt", "new_line": 1})
    gitlab.wire.routes["merge_requests/8/discussions"] = [{"id": gl.DISCUSSION, "notes": [note]}]
    feedback = (await gitlab.reviews.list_feedback(gitlab.review, None))[0]
    endpoint = "merge_requests/8/discussions/" + gl.DISCUSSION + "/notes"
    gitlab.wire.mutations["POST", endpoint] = lambda request: httpx.Response(
        201, json=gl.note(11, json.loads(request.content)["body"], review=True)
    )
    result = await gitlab.writes.reply(feedback, "Addressed", gl.MARKER)
    assert isinstance(result, Applied) and result.value.kind == "review_comment"
    assert resource_calls(gitlab, "/" + endpoint, "POST")
    assert len([r for r in gitlab.wire.calls if r.method == "POST"]) == 1


async def test_retry_on_429(gitlab):
    gitlab.wire.routes["merge_requests/8"] = httpx.Response(429, headers={"Retry-After": "0"})
    with pytest.raises(ReadUnavailable):
        await gitlab.reviews.document(gitlab.review)
    assert len(resource_calls(gitlab, "/merge_requests/8")) == 1
    gitlab.wire.routes["merge_requests/8"] = gl.mr()
    assert (await gitlab.reviews.document(gitlab.review))["iid"] == 8


async def test_retry_on_500(gitlab):
    gitlab.wire.routes["merge_requests/8"] = httpx.Response(500)
    with pytest.raises(ReadUnavailable):
        await gitlab.reviews.document(gitlab.review)
    assert len(resource_calls(gitlab, "/merge_requests/8")) == 1
    gitlab.wire.routes["merge_requests/8"] = gl.mr()
    assert await gitlab.reviews.review_head(gitlab.review) == gitlab.sha
    assert len(resource_calls(gitlab, "/merge_requests/8")) == 2


async def test_no_retry_on_404(gitlab):
    gitlab.wire.routes["merge_requests/8"] = httpx.Response(404)
    with pytest.raises(ReadUnavailable) as failure:
        await gitlab.reviews.document(gitlab.review)
    assert failure.value.evidence and len(resource_calls(gitlab, "/merge_requests/8")) == 1


async def test_non_idempotent_posts_disable_retry(gitlab):
    gitlab.wire.mutations["POST", "pipeline"] = httpx.Response(503)
    gitlab.wire.mutations["POST", "merge_requests"] = httpx.Response(503)
    gitlab.wire.mutations["POST", "issues/7/notes"] = httpx.Response(503)
    assert isinstance(await gitlab.writes.dispatch_lane(gitlab.repo, gitlab.dispatch), Unknown)
    assert isinstance(
        await gitlab.writes.ensure_draft(gitlab.repo, gitlab.dispatch.ref, "T", "body"), Unknown
    )
    assert isinstance(await gitlab.writes.post_comment(gitlab.subject, "hello", gl.MARKER), Unknown)
    assert [r.url.path.rsplit("/", 1)[-1] for r in gitlab.wire.calls if r.method == "POST"] == [
        "pipeline",
        "merge_requests",
        "notes",
    ]


async def test_non_idempotent_posts_single_attempt_on_503(gitlab):
    def lost(request):
        gitlab.wire.routes["issues/7/notes"] = [gl.note(body=json.loads(request.content)["body"])]
        return httpx.Response(503)

    gitlab.wire.mutations["POST", "issues/7/notes"] = lost
    assert isinstance(await gitlab.writes.post_comment(gitlab.subject, "hello", gl.MARKER), Unknown)
    recovered = await gitlab.subjects.probe_comment(gitlab.subject, gl.MARKER)
    assert isinstance(recovered, Found) and recovered.value.value.native_id == "10"
    assert len(resource_calls(gitlab, "/notes", "POST")) == 1


async def test_get_file_url_encoding(gitlab):
    gitlab.wire.routes["repository/tree"] = []
    assert (
        await gitlab.repositories.read_blob(gitlab.repo, gitlab.sha, "src/auth/middleware.py")
        is None
    )
    request = resource_calls(gitlab, "/repository/files/src/auth/middleware.py")[0]
    assert b"src%2Fauth%2Fmiddleware.py" in request.url.raw_path
    assert request.url.params["ref"] == gitlab.sha


async def test_client_context_manager(gitlab):
    assert (await gitlab.subjects.get_task(gitlab.subject)).subject == gitlab.subject
    await gitlab.transport.close()
    with pytest.raises(RuntimeError, match="closed"):
        await gitlab.api.metadata(gitlab.repo)


async def test_ensure_label_exists_conflict(gitlab):
    # Existing state is a native idempotent observation, not a swallowed create conflict.
    assert isinstance(await gitlab.writes.set_label(gitlab.subject, "forge", True), Applied)
    assert not [r for r in gitlab.wire.calls if r.method != "GET"]
    gitlab.wire.mutations["PUT", "issues/7"] = httpx.Response(409)
    assert isinstance(await gitlab.writes.set_label(gitlab.subject, "new", True), Unknown)
    assert len(resource_calls(gitlab, "/issues/7", "PUT")) == 1


async def test_get_job_artifacts_file(gitlab):
    # Candidate transport is authenticated lane upload; exact binary read contract stays lossless.
    body = b'\x00{"vulnerabilities": []}\xff'
    blob(gitlab, body)
    result = await gitlab.repositories.read_blob(gitlab.repo, gitlab.sha, gitlab.path)
    assert BlobContent.model_validate_json(BlobContent(raw=result).model_dump_json()).raw == body


async def test_get_job_artifacts_file_url_encodes_path(gitlab):
    gitlab.wire.routes["repository/tree"] = []
    assert (
        await gitlab.repositories.read_blob(gitlab.repo, gitlab.sha, "reports/report.json") is None
    )
    request = resource_calls(gitlab, "/repository/files/reports/report.json")[0]
    assert b"reports%2Freport.json" in request.url.raw_path
    with pytest.raises(ReadUnavailable):
        await gitlab.repositories.read_blob(gitlab.repo, gitlab.sha, "../report.json")


async def test_create_commit_comment(gitlab):
    # Native review conversation replaces unowned arbitrary commit comments.
    gitlab.wire.mutations["POST", "merge_requests/8/notes"] = lambda request: httpx.Response(
        201, json=gl.note(body=json.loads(request.content)["body"], review=True)
    )
    result = await gitlab.writes.post_comment(gitlab.review, "Pipeline evidence", gl.MARKER)
    assert isinstance(result, Applied) and result.value.subject == gitlab.review
    assert resource_calls(gitlab, "/merge_requests/8/notes", "POST")


async def test_get_project_by_path(gitlab):
    # Display paths may rename; native configured ID is the only lookup authority.
    row = (await gitlab.api.metadata(gitlab.repo)).document
    assert row["path_with_namespace"] == "new/repository" and gitlab.repo.full_name == "old/name"
    assert gitlab.wire.calls[-1].url.path == "/api/v4/projects/123"
    foreign = gitlab.repo.model_copy(
        update={"identity": gitlab.repo.identity.model_copy(update={"repository_id": "999"})}
    )
    before = len(gitlab.wire.calls)
    with pytest.raises(ReadUnavailable):
        await gitlab.api.metadata(foreign)
    assert len(gitlab.wire.calls) == before


async def test_get_project_by_numeric_id(gitlab):
    assert (await gitlab.api.metadata(gitlab.repo)).document["id"] == 123
    gitlab.wire.metadata["id"] = 999
    with pytest.raises(ReadUnavailable):
        await gitlab.api.metadata(gitlab.repo)
    assert not any("/projects/999" in r.url.path for r in gitlab.wire.calls)


async def test_list_merge_requests(gitlab):
    gitlab.wire.routes["merge_requests"] = [gl.mr()]
    assert await gitlab.reviews.find_draft(gitlab.repo, gitlab.dispatch.ref) == gitlab.review
    request = resource_calls(gitlab, "/merge_requests")[0]
    assert request.url.params["source_branch"] == gitlab.dispatch.ref
    gitlab.wire.routes["merge_requests"].append(gl.mr(iid=9, id=81))
    with pytest.raises(ReadUnavailable):
        await gitlab.reviews.find_draft(gitlab.repo, gitlab.dispatch.ref)


async def test_get_issue(gitlab):
    task = await gitlab.subjects.get_task(gitlab.subject)
    assert task.title == "Task" and task.description == "Exact body" and task.labels == ("forge",)
    assert task.subject.number == 7


async def test_list_issues(gitlab):
    # Admission fetches only the explicitly authorized native issue, never ambient discovery.
    task = await gitlab.subjects.get_task(gitlab.subject)
    assert task.subject == gitlab.subject
    assert [r.url.path for r in gitlab.wire.calls] == [
        "/api/v4/projects/123",
        "/api/v4/projects/123/issues/7",
    ]


async def test_list_issues_with_labels(gitlab):
    gitlab.wire.routes["issues/7"]["labels"] = ["p1", "bug"]
    task = await gitlab.subjects.get_task(gitlab.subject)
    assert task.labels == ("bug", "p1")
    gitlab.wire.routes["issues/7"]["labels"] = ["bug", "bug"]
    with pytest.raises(ReadUnavailable):
        await gitlab.subjects.get_task(gitlab.subject)


async def test_create_issue(gitlab):
    # Factory consumes native tasks and cannot silently create a replacement for a missing issue.
    gitlab.wire.routes["issues/7"] = httpx.Response(404)
    with pytest.raises(ReadUnavailable):
        await gitlab.subjects.get_task(gitlab.subject)
    assert all(r.method == "GET" for r in gitlab.wire.calls)


async def test_search_code(gitlab):
    # Frozen commit bytes replace mutable search excerpts as implementation evidence.
    tree = await gitlab.repositories.read_tree(gitlab.repo, gitlab.sha)
    raw = await gitlab.repositories.read_blob(gitlab.repo, gitlab.sha, tree.entries[0].path)
    assert raw == gl.BODY and tree.sha == gitlab.sha
    assert all(r.url.params.get("ref", gitlab.sha) == gitlab.sha for r in gitlab.wire.calls)
    assert not resource_calls(gitlab, "/search")


async def test_list_project_labels(gitlab):
    gitlab.wire.routes["issues/7"]["labels"] = ["feature", "bug"]
    result = await gitlab.subjects.get_task(gitlab.subject)
    assert result.labels == ("bug", "feature")
    assert isinstance(await gitlab.subjects.probe_label(gitlab.subject, "bug", True), Found)
    assert isinstance(
        await gitlab.subjects.probe_label(gitlab.subject, "missing", True), Inconclusive
    )


async def test_list_pipelines(gitlab):
    handle = await gitlab.ci.find_lane(gitlab.repo, gitlab.dispatch.nonce)
    assert handle.native_id == "700" and (await gitlab.ci.lane_status(handle)).status == "succeeded"
    request = resource_calls(gitlab, "/pipelines")[0]
    assert (
        request.url.params["ref"] == gitlab.dispatch.ref and request.url.params["source"] == "api"
    )


async def test_get_branch_head_returns_head_commit(gitlab):
    assert await gitlab.repositories.head(gitlab.repo, "main") == gitlab.sha
    assert resource_calls(gitlab, "/repository/branches/main")
    assert not resource_calls(gitlab, "/repository/commits")


async def test_get_branch_head_missing_branch_raises_404(gitlab):
    assert await gitlab.repositories.head(gitlab.repo, "feature/gone") is None
    assert len(resource_calls(gitlab, "/repository/branches/feature/gone")) == 1
    assert isinstance(
        await gitlab.repositories.probe_branch(gitlab.repo, "feature/gone", gitlab.sha),
        DefinitelyAbsent,
    )
    assert not resource_calls(gitlab, "/repository/commits")


async def test_pagination_warns_when_page_cap_reached(gitlab):
    # Partial evidence now refuses instead of returning a merely logged truncated result.
    gitlab.wire.routes["issues/7/notes"] = lambda request: httpx.Response(
        200,
        json=[gl.note(int(request.url.params["page"]))],
        headers={"x-next-page": str(int(request.url.params["page"]) + 1)},
    )
    with pytest.raises(ReadUnavailable) as failure:
        await gitlab.api.pages(gitlab.repo, "issues/7/notes", max_pages=2)
    assert "scan_truncated" in failure.value.evidence
    assert len(resource_calls(gitlab, "/notes")) == 2


async def test_pagination_no_warning_when_last_page_reached(gitlab):
    gitlab.wire.routes["issues/7/notes"] = lambda request: httpx.Response(
        200,
        json=[gl.note(int(request.url.params["page"]))],
        headers={"x-next-page": "2"} if request.url.params["page"] == "1" else {},
    )
    assert [
        row["id"] for row in await gitlab.api.pages(gitlab.repo, "issues/7/notes", max_pages=2)
    ] == [1, 2]
    assert len(resource_calls(gitlab, "/notes")) == 2
