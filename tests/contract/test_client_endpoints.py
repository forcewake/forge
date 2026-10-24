"""Retained documented-wire lessons on the narrow native CE capability ports.

Historical function names remain registered evidence. Retired group/admin,
raw-diff fallback, artifact and commit-action endpoints are not reintroduced.
Their applicable scope, byte-integrity, no-replay and approval-boundary lessons
pin concrete native reads/writes below.
"""

import json

import httpx
import pytest

from forge.domain.verdicts import Committed
from forge.port_values import Applied, Found, ReadUnavailable, Refused
from tests import gitlab_engine_fixture as gl
from tests import test_native_git_publication as proof
from tests.contract.conftest import load_fixture, signed
from tests.native_clients_fixture import blob, resource_calls


def request_bodies(env):
    return [json.loads(request.content) for request in env.wire.calls if request.method != "GET"]


def discussion(env, *, position=None):
    note = gl.note(501, "Please add tests", review=True, bot=False)
    if position is not None:
        note["position"] = position
    env.wire.routes["merge_requests/8/discussions"] = [{"id": gl.DISCUSSION, "notes": [note]}]
    return note


async def test_get_merge_request(gitlab):
    sample = load_fixture("merge_request")
    row = gl.mr(
        title=sample["title"],
        description=sample["description"],
        has_conflicts=sample["has_conflicts"],
        labels=sample["labels"],
    )
    gitlab.wire.routes["merge_requests/8"] = row
    result = await gitlab.reviews.document(gitlab.review)
    assert result["title"] == sample["title"] and result["has_conflicts"] is False
    request = resource_calls(gitlab, "/merge_requests/8")[0]
    assert request.method == "GET" and request.headers["private-token"] == "synthetic-read"
    assert result["project_id"] == result["source_project_id"] == result["target_project_id"] == 123


async def test_get_merge_request_diffs_paginates(gitlab):
    # Frozen complete object enumeration replaces abbreviated/truncated diff hunk pages.
    def tree_page(request):
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json=[
                {
                    "path": "a.py" if page == 1 else "b.py",
                    "id": gl.BLOB,
                    "mode": "100644",
                    "type": "blob",
                }
            ],
            headers={"x-next-page": "2"} if page == 1 else {},
        )

    gitlab.wire.routes["repository/tree"] = tree_page
    tree = await gitlab.repositories.read_tree(gitlab.repo, gitlab.sha)
    assert [entry.path for entry in tree.entries] == ["a.py", "b.py"]
    calls = resource_calls(gitlab, "/repository/tree")
    assert [request.url.params["page"] for request in calls] == ["1", "2"]
    assert all(request.url.params["ref"] == gitlab.sha for request in calls)


async def test_get_merge_request_raw_diff(gitlab):
    payload = b"diff-like bytes\r\n\x00\xff"
    blob(gitlab, payload)
    result = await gitlab.repositories.read_blob(gitlab.repo, gitlab.sha, gitlab.path)
    assert result == payload
    request = resource_calls(gitlab, "/repository/tree")[0]
    assert request.url.params["ref"] == gitlab.sha
    assert not any("/diffs" in request.url.path for request in gitlab.wire.calls)


async def test_list_discussions(gitlab):
    discussion(gitlab)
    feedback = await gitlab.reviews.list_feedback(gitlab.review, None)
    assert len(feedback) == 1 and feedback[0].body == "Please add tests"
    assert feedback[0].ref.native_id == "501" and feedback[0].ref.subject == gitlab.review
    assert (
        resource_calls(gitlab, "/merge_requests/8/discussions")[0].url.params["per_page"] == "100"
    )
    assert gl.DISCUSSION in feedback[0].cursor


async def test_create_mr_note(gitlab):
    gitlab.wire.mutations["POST", "merge_requests/8/notes"] = lambda request: httpx.Response(
        201, json=gl.note(502, json.loads(request.content)["body"], review=True)
    )
    result = await gitlab.writes.post_comment(
        gitlab.review, "CI failed: native evidence", gl.MARKER
    )
    assert isinstance(result, Applied) and result.value.native_id == "502"
    assert request_bodies(gitlab) == [{"body": "CI failed: native evidence\n\n" + gl.MARKER}]
    assert resource_calls(gitlab, "/merge_requests/8/notes", "POST")


async def test_create_mr_discussion(gitlab):
    discussion(gitlab)
    note = (await gitlab.reviews.list_feedback(gitlab.review, None))[0]
    endpoint = "merge_requests/8/discussions/" + gl.DISCUSSION + "/notes"
    gitlab.wire.mutations["POST", endpoint] = lambda request: httpx.Response(
        201, json=gl.note(503, json.loads(request.content)["body"], review=True)
    )
    result = await gitlab.writes.reply(note, "Addressed with tests", gl.MARKER)
    assert isinstance(result, Applied) and result.value.native_id == "503"
    body = request_bodies(gitlab)[0]["body"]
    assert body.startswith("Addressed with tests\n\n" + gl.MARKER)
    assert "forge:reply:" in body and gl.DISCUSSION in body
    assert resource_calls(gitlab, "/" + endpoint, "POST")


async def test_create_mr_discussion_with_position(gitlab):
    note = discussion(gitlab, position={"new_path": "old.txt", "new_line": 1})
    feedback = (await gitlab.reviews.list_feedback(gitlab.review, None))[0]
    # A changed native finding cannot be answered using stale captured positioning/content.
    note["body"] = "Human changed the finding"
    result = await gitlab.writes.reply(feedback, "Addressed", gl.MARKER)
    assert isinstance(result, Refused) and result.reason == "reply_feedback_changed"
    assert request_bodies(gitlab) == []


async def test_resolve_discussion(gitlab):
    # Human resolution stays human-owned. GitLab quick actions cannot smuggle it through a comment.
    result = await gitlab.writes.post_comment(gitlab.review, "/resolve", gl.MARKER)
    assert isinstance(result, Refused) and result.reason == "gitlab_quick_action_refused"
    assert gitlab.wire.calls == []


async def test_add_mr_labels(gitlab):
    # Narrow label capability targets the original issue; partial native update keeps other labels.
    def accepted(request):
        assert json.loads(request.content) == {"add_labels": "reviewed"}
        row = {**gitlab.wire.routes["issues/7"], "labels": ["forge", "reviewed"]}
        return httpx.Response(200, json=row)

    gitlab.wire.mutations["PUT", "issues/7"] = accepted
    result = await gitlab.writes.set_label(gitlab.subject, "reviewed", True)
    assert isinstance(result, Applied) and result.value.present is True
    assert request_bodies(gitlab) == [{"add_labels": "reviewed"}]


async def test_remove_mr_labels(gitlab):
    gitlab.wire.routes["issues/7"]["labels"] = ["forge", "human-label"]
    gitlab.wire.mutations["PUT", "issues/7"] = httpx.Response(
        200, json={**gitlab.wire.routes["issues/7"], "labels": ["human-label"]}
    )
    result = await gitlab.writes.set_label(gitlab.subject, "forge", False)
    assert isinstance(result, Applied) and result.value.present is False
    assert request_bodies(gitlab) == [{"remove_labels": "forge"}]


async def test_list_merge_requests(gitlab):
    gitlab.wire.routes["merge_requests"] = [gl.mr()]
    assert await gitlab.reviews.find_draft(gitlab.repo, gitlab.dispatch.ref) == gitlab.review
    request = resource_calls(gitlab, "/merge_requests")[0]
    assert dict(request.url.params) == {
        "scope": "all",
        "state": "all",
        "source_branch": gitlab.dispatch.ref,
        "per_page": "100",
        "page": "1",
    }


async def test_create_merge_request(gitlab):
    gitlab.wire.mutations["POST", "merge_requests"] = httpx.Response(201, json=gl.mr())
    result = await gitlab.writes.ensure_draft(gitlab.repo, gitlab.dispatch.ref, "Title", "Body")
    assert isinstance(result, Applied) and result.value == gitlab.review
    assert request_bodies(gitlab) == [
        {
            "source_branch": gitlab.dispatch.ref,
            "target_branch": "main",
            "title": "Draft: Title",
            "description": "Body",
            "remove_source_branch": False,
            "allow_collaboration": False,
        }
    ]
    assert (
        resource_calls(gitlab, "/merge_requests", "POST")[0].headers["private-token"]
        == "synthetic-write"
    )


async def test_get_file_url_encodes_path(gitlab):
    gitlab.wire.routes["repository/tree"] = []
    assert (
        await gitlab.repositories.read_blob(gitlab.repo, gitlab.sha, "src/auth/rotation.py") is None
    )
    request = resource_calls(gitlab, "/repository/files/src/auth/rotation.py")[0]
    assert b"src%2Fauth%2Frotation.py" in request.url.raw_path
    assert request.url.params["ref"] == gitlab.sha


async def test_get_tree(gitlab):
    tree = await gitlab.repositories.read_tree(gitlab.repo, gitlab.sha, "old.txt")
    assert tree.sha == gitlab.sha and [entry.path for entry in tree.entries] == ["old.txt"]
    request = resource_calls(gitlab, "/repository/tree")[0]
    assert dict(request.url.params) == {
        "ref": gitlab.sha,
        "recursive": "true",
        "per_page": "100",
        "page": "1",
    }
    assert tree.entries[0].mode == "100755"


async def test_compare_commits(gitlab_native):
    env = gitlab_native
    result = await env.writer.commit(env.repo, env.candidate, env.base, "native-comparison")
    assert isinstance(result, Committed)
    observed = await env.repositories.probe_commit(
        env.repo, proof.BRANCH, "native-comparison", env.base, env.candidate
    )
    assert isinstance(observed, Found) and observed.value.sha == result.sha
    assert proof.git(env.server, "show", env.base + ":src/file.txt", raw=True) == b"old\n"
    assert proof.git(env.server, "show", result.sha + ":src/file.txt", raw=True) == b"approved\n"


async def test_create_branch(gitlab):
    gitlab.wire.routes.pop("repository/branches/forge/run-1")
    gitlab.wire.mutations["POST", "repository/branches"] = httpx.Response(
        201, json={"name": gitlab.dispatch.ref, "protected": False, "commit": {"id": gitlab.sha}}
    )
    result = await gitlab.writes.ensure_branch(gitlab.repo, gitlab.dispatch.ref, gitlab.sha)
    assert isinstance(result, Applied) and result.value.sha == gitlab.sha
    assert request_bodies(gitlab) == [{"branch": gitlab.dispatch.ref, "ref": gitlab.sha}]


async def test_get_pipeline(gitlab):
    row = await gitlab.ci.pipeline(gitlab.repo, "700")
    assert row["id"] == 700 and row["project_id"] == 123 and row["sha"] == gitlab.sha
    request = resource_calls(gitlab, "/pipelines/700")[0]
    assert request.method == "GET"
    gitlab.wire.routes["pipelines/700"]["project_id"] = 999
    with pytest.raises(ReadUnavailable):
        await gitlab.ci.pipeline(gitlab.repo, "700")


async def test_list_pipeline_jobs(gitlab):
    jobs = gitlab.wire.routes["pipelines/700/jobs"]
    jobs.append(
        {
            **jobs[0],
            "id": 702,
            "name": "test",
            "status": "failed",
            "failure_reason": "script_failure",
        }
    )
    checks = await gitlab.ci.checks_for(gitlab.repo, gitlab.sha)
    assert [(check.name, check.native_id, check.status) for check in checks] == [
        ("forge-lane-v1", "701", "passed"),
        ("test", "702", "failed"),
    ]
    assert checks[1].failure_kind == "unknown"  # generic script failure is not proved code defect
    assert resource_calls(gitlab, "/pipelines/700/jobs")[0].url.params["include_retried"] == "false"


async def test_get_job_log(gitlab):
    # Raw logs cannot become authority. A failed native job is classified from native structured facts.
    gitlab.wire.routes["pipelines/700/jobs"][0].update(
        status="failed", failure_reason="runner_system_failure"
    )
    checks = await gitlab.ci.checks_for(gitlab.repo, gitlab.sha)
    assert checks[0].status == "failed" and checks[0].failure_kind == "infrastructure"
    assert not any("/trace" in request.url.path for request in gitlab.wire.calls)


async def test_get_job_artifacts_file(gitlab):
    blob(gitlab, b"<coverage />", path="reports/coverage.xml")
    assert (
        await gitlab.repositories.read_blob(gitlab.repo, gitlab.sha, "reports/coverage.xml")
        == b"<coverage />"
    )
    assert not any("/artifacts/" in request.url.path for request in gitlab.wire.calls)
    with pytest.raises(ReadUnavailable):
        await gitlab.repositories.read_blob(gitlab.repo, gitlab.sha, "../reports/coverage.xml")


async def test_get_issue(gitlab):
    task = await gitlab.subjects.get_task(gitlab.subject)
    assert (
        task.subject == gitlab.subject and task.title == "Task" and task.description == "Exact body"
    )
    assert resource_calls(gitlab, "/issues/7")[0].method == "GET"


async def test_list_issues(gitlab):
    gitlab.wire.routes["issues/7"]["labels"] = ["bug", "p1"]
    assert (await gitlab.subjects.get_task(gitlab.subject)).labels == ("bug", "p1")
    # Exact authorized task replaces ambient enumeration/filtering under another actor's scope.
    assert [request.url.path for request in gitlab.wire.calls] == [
        "/api/v4/projects/123",
        "/api/v4/projects/123/issues/7",
    ]


async def test_create_issue_note(gitlab):
    gitlab.wire.mutations["POST", "issues/7/notes"] = lambda request: httpx.Response(
        201, json=gl.note(body=json.loads(request.content)["body"])
    )
    result = await gitlab.writes.post_comment(gitlab.subject, "Draft ready for review", gl.MARKER)
    assert isinstance(result, Applied) and result.value.subject == gitlab.subject
    assert request_bodies(gitlab) == [{"body": "Draft ready for review\n\n" + gl.MARKER}]
    assert resource_calls(gitlab, "/issues/7/notes", "POST")


async def test_get_project_by_id(gitlab):
    result = await gitlab.api.metadata(gitlab.repo)
    assert result.document["id"] == 123 and result.document["default_branch"] == "main"
    assert resource_calls(gitlab, "/projects/123")[0].method == "GET"
    assert resource_calls(gitlab, "/projects/123")[0].headers["private-token"] == "synthetic-read"


async def test_get_project_by_url_encoded_path(gitlab):
    renamed = gitlab.repo.model_copy(update={"full_name": "renamed/description"})
    assert (await gitlab.api.metadata(renamed)).document["path_with_namespace"] == "new/repository"
    assert resource_calls(gitlab, "/projects/123")
    assert not any("renamed" in request.url.path for request in gitlab.wire.calls)


async def test_search_code(gitlab):
    blob(gitlab, b"def rotate_token():\n    return True\n")
    tree = await gitlab.repositories.read_tree(gitlab.repo, gitlab.sha)
    assert tree.entries[0].path == gitlab.path
    assert (
        await gitlab.repositories.read_blob(gitlab.repo, gitlab.sha, gitlab.path)
        == b"def rotate_token():\n    return True\n"
    )
    assert not resource_calls(gitlab, "/search")


async def test_list_project_hooks(ingress, gitlab):
    # Operational hook administration is retired; authentication is configured out-of-band.
    payload = load_fixture("pipeline_webhook")
    headers, body = signed(payload, "Pipeline Hook")
    assert ingress.parse(headers, body).native_id == "200"
    headers["X-Gitlab-Token"] = "wrong-token"
    with pytest.raises(ValueError):
        ingress.parse(headers, body)
    assert gitlab.wire.calls == []


async def test_create_project_hook(ingress, gitlab):
    payload = load_fixture("pipeline_webhook")
    payload["project"]["id"] = 999
    with pytest.raises(ValueError):
        ingress.parse(*signed(payload, "Pipeline Hook"))
    # A webhook cannot onboard an unconfigured project or call administrative endpoints.
    assert gitlab.wire.calls == []


async def test_list_group_projects(gitlab):
    foreign = gitlab.repo.model_copy(
        update={"identity": gitlab.repo.identity.model_copy(update={"repository_id": "999"})}
    )
    with pytest.raises(ReadUnavailable):
        await gitlab.api.metadata(foreign)
    assert gitlab.wire.calls == []
    assert (await gitlab.api.metadata(gitlab.repo)).document["id"] == 123
    assert all(request.url.path == "/api/v4/projects/123" for request in gitlab.wire.calls)


async def test_create_commit_documented_request(gitlab_native):
    # Previously skipped REST commit spec is now real native Git CAS evidence, without skip.
    env = gitlab_native
    result = await env.writer.commit(env.repo, env.candidate, env.base, "contract-native")
    assert isinstance(result, Committed)
    assert proof.git(env.server, "show", result.sha + ":src/file.txt", raw=True) == b"approved\n"
    pushes = [call for call in env.calls if call[0] == "push"]
    assert len(pushes) == 1
    assert f"--force-with-lease=refs/heads/{proof.BRANCH}:{env.base}" in pushes[0]
    assert all(request.method == "GET" for request in env.wire.calls)
