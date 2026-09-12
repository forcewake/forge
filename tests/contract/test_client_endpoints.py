"""GitLab REST API v4 contract tests for ``forge.gitlab.client.GitLabClient``.

Every test invokes one client method against pytest-httpx and asserts the
HTTP method, request path and key parameters/body against the DOCUMENTED
GitLab API (https://docs.gitlab.com/api/):

- Merge requests: https://docs.gitlab.com/api/merge_requests/
- Commits: https://docs.gitlab.com/api/commits/
- Repositories (compare/tree/branches): https://docs.gitlab.com/api/repositories/
- Pipelines: https://docs.gitlab.com/api/pipelines/
- Jobs: https://docs.gitlab.com/api/jobs/
- Job artifacts: https://docs.gitlab.com/api/job_artifacts/
- Issues / notes / discussions: https://docs.gitlab.com/api/issues/,
  https://docs.gitlab.com/api/notes/, https://docs.gitlab.com/api/discussions/
- Projects / groups / search / hooks: https://docs.gitlab.com/api/projects/,
  https://docs.gitlab.com/api/groups/, https://docs.gitlab.com/api/search/,
  https://docs.gitlab.com/api/projects/#hooks

Known, deliberate deviations from the documented API are NOT asserted here;
they are pinned as strict-xfail specs in test_known_deviations.py.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from forge.gitlab.client import GitLabClient

from .conftest import BASE, load_fixture

UNIFIED_DIFF_TEXT = (
    "diff --git a/src/auth/rotation.py b/src/auth/rotation.py\n"
    "index 0000000..1111111 100644\n"
    "--- /dev/null\n"
    "+++ b/src/auth/rotation.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def rotate_token():\n"
    "+    return True\n"
)


def request_bodies(httpx_mock: HTTPXMock) -> list[dict]:
    """Return the decoded JSON bodies of all sent requests, in order."""
    return [json.loads(request.read()) for request in httpx_mock.get_requests()]


# ---------------------------------------------------------------------------
# Merge requests
# ---------------------------------------------------------------------------


async def test_get_merge_request(httpx_mock: HTTPXMock, gitlab_client: GitLabClient) -> None:
    """GET /projects/:id/merge_requests/:mr_iid returns a single MR.

    https://docs.gitlab.com/api/merge_requests/#single-merge-request
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7",
        json=load_fixture("merge_request"),
    )

    async with gitlab_client as client:
        mr = await client.get_merge_request(42, 7)

    request = httpx_mock.get_requests()[0]
    assert request.method == "GET"
    assert str(request.url) == f"{BASE}/projects/42/merge_requests/7"
    assert mr.iid == 7
    assert mr.title == "Add token rotation flow"
    assert mr.state == "opened"
    assert mr.source_branch == "feature/token-rotation"
    assert mr.target_branch == "main"
    assert mr.has_conflicts is False


async def test_get_merge_request_diffs_paginates(
    httpx_mock: HTTPXMock, gitlab_client: GitLabClient
) -> None:
    """GET /projects/:id/merge_requests/:mr_iid/diffs lists diff files.

    By default each ``diff`` holds an abbreviated hunk; the documented
    ``unidiff=true`` parameter (GitLab 16.5+) upgrades it to a full unified
    diff. See https://docs.gitlab.com/api/merge_requests/#get-merge-request-diff-files
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7/diffs?per_page=100",
        json=load_fixture("merge_request_diffs"),
    )

    async with gitlab_client as client:
        diffs = await client.get_merge_request_diffs(42, 7)

    request = httpx_mock.get_requests()[0]
    assert request.method == "GET"
    assert request.url.params["per_page"] == "100"
    assert len(diffs) == 2
    assert diffs[0].new_path == "src/auth/rotation.py"
    assert diffs[0].new_file is True
    assert diffs[1].old_path == "src/config.py"


async def test_get_merge_request_raw_diff(
    httpx_mock: HTTPXMock, gitlab_client: GitLabClient
) -> None:
    """Contract note: current client fetches the raw diff from
    GET /projects/:id/merge_requests/:mr_iid/diffs with an undocumented
    ``Accept: text/plain`` request header.

    This test records the actual request the client makes; the documented
    ways to obtain unified diff text (raw_diffs endpoint / unidiff=true) are
    pinned as strict-xfail specs in test_known_deviations.py.
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7/diffs",
        text=UNIFIED_DIFF_TEXT,
    )

    async with gitlab_client as client:
        raw = await client.get_merge_request_raw_diff(42, 7)

    request = httpx_mock.get_requests()[0]
    assert request.method == "GET"
    assert str(request.url) == f"{BASE}/projects/42/merge_requests/7/diffs"
    assert request.headers["accept"] == "text/plain"  # undocumented deviation
    assert raw.startswith("diff --git a/src/auth/rotation.py")


async def test_list_discussions(httpx_mock: HTTPXMock, gitlab_client: GitLabClient) -> None:
    """GET /projects/:id/merge_requests/:mr_iid/discussions lists threads.

    https://docs.gitlab.com/api/discussions/#merge-requests
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7/discussions?per_page=100",
        json=[
            {
                "id": "abc123def456abc123def456abc123de",
                "individual_note": False,
                "notes": [
                    {
                        "id": 501,
                        "type": "DiscussionNote",
                        "body": "Please add tests.",
                        "author": {
                            "id": 11,
                            "username": "forge-user",
                            "name": "Forge User",
                        },
                        "created_at": "2026-09-02T11:00:00Z",
                        "updated_at": "2026-09-02T11:00:00Z",
                        "system": False,
                        "resolvable": True,
                        "resolved": False,
                    }
                ],
            }
        ],
    )

    async with gitlab_client as client:
        discussions = await client.list_discussions(42, 7)

    request = httpx_mock.get_requests()[0]
    assert request.method == "GET"
    assert str(request.url) == f"{BASE}/projects/42/merge_requests/7/discussions?per_page=100"
    assert request.url.params["per_page"] == "100"
    assert len(discussions) == 1
    assert discussions[0].id == "abc123def456abc123def456abc123de"
    assert discussions[0].notes[0].body == "Please add tests."
    assert discussions[0].notes[0].resolvable is True


async def test_create_mr_note(httpx_mock: HTTPXMock, gitlab_client: GitLabClient) -> None:
    """POST /projects/:id/merge_requests/:mr_iid/notes with {"body": ...}.

    https://docs.gitlab.com/api/notes/#create-new-merge-request-note
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7/notes",
        method="POST",
        json={
            "id": 502,
            "body": "CI failed on job #302 — see trace.",
            "author": {"id": 99, "username": "forge-bot", "name": "Forge Bot"},
            "system": False,
            "resolvable": False,
        },
    )

    async with gitlab_client as client:
        note = await client.create_mr_note(42, 7, "CI failed on job #302 — see trace.")

    request = httpx_mock.get_requests()[0]
    assert request.method == "POST"
    assert str(request.url) == f"{BASE}/projects/42/merge_requests/7/notes"
    assert request_bodies(httpx_mock) == [{"body": "CI failed on job #302 — see trace."}]
    assert note.id == 502
    assert note.author is not None and note.author.username == "forge-bot"


async def test_create_mr_discussion(httpx_mock: HTTPXMock, gitlab_client: GitLabClient) -> None:
    """POST /projects/:id/merge_requests/:mr_iid/discussions with {"body": ...}.

    https://docs.gitlab.com/api/discussions/#create-new-merge-request-thread
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7/discussions",
        method="POST",
        json={
            "id": "def456def456def456def456def456de",
            "individual_note": True,
            "notes": [
                {
                    "id": 503,
                    "body": "Reviewing now.",
                    "author": {"id": 99, "username": "forge-bot", "name": "Forge Bot"},
                    "system": False,
                    "resolvable": False,
                }
            ],
        },
    )

    async with gitlab_client as client:
        discussion = await client.create_mr_discussion(42, 7, "Reviewing now.")

    request = httpx_mock.get_requests()[0]
    assert request.method == "POST"
    assert str(request.url) == f"{BASE}/projects/42/merge_requests/7/discussions"
    assert request_bodies(httpx_mock) == [{"body": "Reviewing now."}]
    assert discussion.id == "def456def456def456def456def456de"


async def test_create_mr_discussion_with_position(
    httpx_mock: HTTPXMock, gitlab_client: GitLabClient
) -> None:
    """Thread creation accepts the documented ``position`` attribute.

    https://docs.gitlab.com/api/discussions/#create-new-merge-request-thread
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7/discussions",
        method="POST",
        json={
            "id": "pos789pos789pos789pos789pos789pos",
            "individual_note": True,
            "notes": [],
        },
    )
    position = {
        "base_sha": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
        "start_sha": "c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0a1b2",
        "head_sha": "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0a1",
        "position_type": "text",
        "new_path": "src/auth/rotation.py",
        "old_path": "src/auth/rotation.py",
        "new_line": 2,
    }

    async with gitlab_client as client:
        await client.create_mr_discussion(42, 7, "Nit: naming", position=position)

    assert request_bodies(httpx_mock) == [{"body": "Nit: naming", "position": position}]


async def test_resolve_discussion(httpx_mock: HTTPXMock, gitlab_client: GitLabClient) -> None:
    """PUT /projects/:id/merge_requests/:mr_iid/discussions/:discussion_id
    with {"resolved": true}.

    https://docs.gitlab.com/api/discussions/#resolve-a-merge-request-thread
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7/discussions/abc123def456abc123def456abc123de",
        method="PUT",
        json={
            "id": "abc123def456abc123def456abc123de",
            "individual_note": False,
            "notes": [
                {
                    "id": 501,
                    "body": "Please add tests.",
                    "author": {"id": 11, "username": "forge-user", "name": "Forge User"},
                    "system": False,
                    "resolvable": True,
                    "resolved": True,
                }
            ],
        },
    )

    async with gitlab_client as client:
        discussion = await client.resolve_discussion(42, 7, "abc123def456abc123def456abc123de")

    request = httpx_mock.get_requests()[0]
    assert request.method == "PUT"
    assert str(request.url) == (
        f"{BASE}/projects/42/merge_requests/7/discussions/abc123def456abc123def456abc123de"
    )
    assert request_bodies(httpx_mock) == [{"resolved": True}]
    assert discussion.notes[0].resolved is True


async def test_add_mr_labels(httpx_mock: HTTPXMock, gitlab_client: GitLabClient) -> None:
    """PUT /projects/:id/merge_requests/:mr_iid with {"add_labels": ...}.

    ``add_labels`` is documented on the update-MR endpoint as a
    comma-separated label list.
    https://docs.gitlab.com/api/merge_requests/#update-merge-request
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7",
        method="PUT",
        json=load_fixture("merge_request"),
    )

    async with gitlab_client as client:
        await client.add_mr_labels(42, 7, ["security-review", "approved"])

    request = httpx_mock.get_requests()[0]
    assert request.method == "PUT"
    assert str(request.url) == f"{BASE}/projects/42/merge_requests/7"
    assert request_bodies(httpx_mock) == [{"add_labels": "security-review,approved"}]


async def test_remove_mr_labels(httpx_mock: HTTPXMock, gitlab_client: GitLabClient) -> None:
    """PUT /projects/:id/merge_requests/:mr_iid with {"remove_labels": ...}.

    https://docs.gitlab.com/api/merge_requests/#update-merge-request
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7",
        method="PUT",
        json=load_fixture("merge_request"),
    )

    async with gitlab_client as client:
        await client.remove_mr_labels(42, 7, ["approved"])

    request = httpx_mock.get_requests()[0]
    assert request.method == "PUT"
    assert request_bodies(httpx_mock) == [{"remove_labels": "approved"}]


async def test_list_merge_requests(httpx_mock: HTTPXMock, gitlab_client: GitLabClient) -> None:
    """GET /projects/:id/merge_requests?state=<state>.

    https://docs.gitlab.com/api/merge_requests/#list-project-merge-requests
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests?state=opened&per_page=20",
        json=[load_fixture("merge_request")],
    )

    async with gitlab_client as client:
        mrs = await client.list_merge_requests(42, state="opened", per_page=20)

    request = httpx_mock.get_requests()[0]
    assert request.method == "GET"
    assert request.url.params["state"] == "opened"
    assert request.url.params["per_page"] == "20"
    assert len(mrs) == 1
    assert mrs[0].iid == 7


async def test_create_merge_request(httpx_mock: HTTPXMock, gitlab_client: GitLabClient) -> None:
    """POST /projects/:id/merge_requests with source_branch/target_branch/
    title/description (+ optional labels, assignee_id).

    https://docs.gitlab.com/api/merge_requests/#create-mr
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests",
        method="POST",
        status_code=201,
        json=load_fixture("merge_request"),
    )

    async with gitlab_client as client:
        created = await client.create_merge_request(
            42,
            source_branch="feature/token-rotation",
            target_branch="main",
            title="Add token rotation flow",
            description="Implements automatic credential rotation.",
            labels=["backend"],
        )

    request = httpx_mock.get_requests()[0]
    assert request.method == "POST"
    assert str(request.url) == f"{BASE}/projects/42/merge_requests"
    assert request_bodies(httpx_mock) == [
        {
            "source_branch": "feature/token-rotation",
            "target_branch": "main",
            "title": "Add token rotation flow",
            "description": "Implements automatic credential rotation.",
            "labels": "backend",
        }
    ]
    assert created["iid"] == 7


# ---------------------------------------------------------------------------
# Repository: files, tree, compare, branches
# ---------------------------------------------------------------------------


async def test_get_file_url_encodes_path(
    httpx_mock: HTTPXMock, gitlab_client: GitLabClient
) -> None:
    """GET /projects/:id/repository/files/:file_path?ref=<ref>.

    ``:file_path`` must be fully URL-encoded (``/`` as ``%2F``).
    https://docs.gitlab.com/api/repository_files/#get-file-from-repository
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/repository/files/src%2Fauth%2Frotation.py?ref=main",
        json={
            "file_name": "rotation.py",
            "file_path": "src/auth/rotation.py",
            "size": 42,
            "encoding": "base64",
            "content": "ZGVmIHJvdGF0ZV90b2tlbigpOg==",
            "content_sha256": "deadbeefdeadbeefdeadbeefdeadbeef",
            "ref": "main",
            "blob_id": "abc123",
            "commit_id": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
            "last_commit_id": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
        },
    )

    async with gitlab_client as client:
        repo_file = await client.get_file(42, "src/auth/rotation.py", ref="main")

    request = httpx_mock.get_requests()[0]
    assert request.method == "GET"
    assert (
        str(request.url) == f"{BASE}/projects/42/repository/files/src%2Fauth%2Frotation.py?ref=main"
    )
    assert request.url.params["ref"] == "main"
    assert repo_file.file_path == "src/auth/rotation.py"
    assert repo_file.content == "ZGVmIHJvdGF0ZV90b2tlbigpOg=="


async def test_get_tree(httpx_mock: HTTPXMock, gitlab_client: GitLabClient) -> None:
    """GET /projects/:id/repository/tree with ref/path/recursive params.

    https://docs.gitlab.com/api/repositories/#list-repository-tree
    """
    httpx_mock.add_response(
        url=(f"{BASE}/projects/42/repository/tree?ref=main&recursive=false&path=src&per_page=100"),
        json=[
            {
                "id": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
                "name": "auth",
                "type": "tree",
                "path": "src/auth",
                "mode": "040000",
            },
            {
                "id": "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0a1",
                "name": "rotation.py",
                "type": "blob",
                "path": "src/auth/rotation.py",
                "mode": "100644",
            },
        ],
    )

    async with gitlab_client as client:
        entries = await client.get_tree(42, path="src", ref="main", recursive=False)

    request = httpx_mock.get_requests()[0]
    assert request.method == "GET"
    assert request.url.params["ref"] == "main"
    assert request.url.params["path"] == "src"
    assert request.url.params["recursive"] == "false"
    assert request.url.params["per_page"] == "100"
    assert [entry.name for entry in entries] == ["auth", "rotation.py"]


async def test_compare_commits(httpx_mock: HTTPXMock, gitlab_client: GitLabClient) -> None:
    """GET /projects/:id/repository/compare?from=<sha>&to=<sha>.

    Documented attributes: from (required), to (required), straight (optional).
    https://docs.gitlab.com/api/repositories/#compare-branches-tags-or-commits
    """
    from_sha = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
    to_sha = "c9d8b7a6f5e4d3c2b1a0f9e8d7c6b5a4f3e2d1c0"
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/repository/compare?from={from_sha}&to={to_sha}",
        json={
            "commit": {"id": to_sha, "message": "Add token rotation\n"},
            "commits": [{"id": to_sha, "message": "Add token rotation\n"}],
            "diffs": load_fixture("merge_request_diffs"),
            "compare_timeout": False,
            "compare_same_ref": False,
        },
    )

    async with gitlab_client as client:
        result = await client.compare_commits(42, from_sha, to_sha)

    request = httpx_mock.get_requests()[0]
    assert request.method == "GET"
    assert str(request.url) == (
        f"{BASE}/projects/42/repository/compare?from={from_sha}&to={to_sha}"
    )
    assert request.url.params["from"] == from_sha
    assert request.url.params["to"] == to_sha
    assert result["commit"]["id"] == to_sha
    assert len(result["diffs"]) == 2


async def test_create_branch(httpx_mock: HTTPXMock, gitlab_client: GitLabClient) -> None:
    """POST /projects/:id/repository/branches with {"branch": ..., "ref": ...}.

    https://docs.gitlab.com/api/branches/#create-repository-branch
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/repository/branches",
        method="POST",
        status_code=201,
        json={
            "name": "forge/fix-pipeline",
            "commit": {
                "id": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
                "message": "Baseline\n",
            },
            "merged": False,
            "protected": False,
            "developers_can_push": False,
            "developers_can_merge": False,
            "can_push": True,
            "default": False,
            "web_url": "https://gitlab.example.com/group/project/-/tree/forge/fix-pipeline",
        },
    )

    async with gitlab_client as client:
        branch = await client.create_branch(42, "forge/fix-pipeline", ref="main")

    request = httpx_mock.get_requests()[0]
    assert request.method == "POST"
    assert str(request.url) == f"{BASE}/projects/42/repository/branches"
    assert request_bodies(httpx_mock) == [{"branch": "forge/fix-pipeline", "ref": "main"}]
    assert branch["name"] == "forge/fix-pipeline"


# ---------------------------------------------------------------------------
# Pipelines and jobs
# ---------------------------------------------------------------------------


async def test_get_pipeline(httpx_mock: HTTPXMock, gitlab_client: GitLabClient) -> None:
    """GET /projects/:id/pipelines/:pipeline_id.

    https://docs.gitlab.com/api/pipelines/#get-a-single-pipeline
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/pipelines/200",
        json=load_fixture("pipeline"),
    )

    async with gitlab_client as client:
        pipeline = await client.get_pipeline(42, 200)

    request = httpx_mock.get_requests()[0]
    assert request.method == "GET"
    assert str(request.url) == f"{BASE}/projects/42/pipelines/200"
    assert pipeline.id == 200
    assert pipeline.status == "failed"
    assert pipeline.ref == "feature/token-rotation"


async def test_list_pipeline_jobs(httpx_mock: HTTPXMock, gitlab_client: GitLabClient) -> None:
    """GET /projects/:id/pipelines/:pipeline_id/jobs.

    https://docs.gitlab.com/api/jobs/#list-pipeline-jobs
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/pipelines/200/jobs?per_page=100",
        json=[
            {
                "id": 301,
                "name": "build",
                "stage": "build",
                "status": "success",
                "web_url": "https://gitlab.example.com/group/project/-/jobs/301",
                "duration": 37,
                "failure_reason": None,
            },
            {
                "id": 302,
                "name": "unit-tests",
                "stage": "test",
                "status": "failed",
                "web_url": "https://gitlab.example.com/group/project/-/jobs/302",
                "duration": 75,
                "failure_reason": "script_failure",
            },
        ],
    )

    async with gitlab_client as client:
        jobs = await client.list_pipeline_jobs(42, 200)

    request = httpx_mock.get_requests()[0]
    assert request.method == "GET"
    assert str(request.url) == f"{BASE}/projects/42/pipelines/200/jobs?per_page=100"
    assert request.url.params["per_page"] == "100"
    assert [job.id for job in jobs] == [301, 302]
    assert jobs[1].failure_reason == "script_failure"


async def test_get_job_log(httpx_mock: HTTPXMock, gitlab_client: GitLabClient) -> None:
    """GET /projects/:id/jobs/:job_id/trace serves the job log file.

    https://docs.gitlab.com/api/jobs/#get-a-job-log
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/jobs/302/trace",
        text="Running with gitlab-runner...\n$ pytest tests -q\n1 failed\n",
    )

    async with gitlab_client as client:
        log = await client.get_job_log(42, 302)

    request = httpx_mock.get_requests()[0]
    assert request.method == "GET"
    assert str(request.url) == f"{BASE}/projects/42/jobs/302/trace"
    assert "pytest tests -q" in log


async def test_get_job_artifacts_file(httpx_mock: HTTPXMock, gitlab_client: GitLabClient) -> None:
    """GET /projects/:id/jobs/:job_id/artifacts/:artifact_path downloads a
    single artifact file; ``:artifact_path`` must be URL-encoded.

    https://docs.gitlab.com/api/job_artifacts/#download-a-single-artifact-file-by-path
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/jobs/302/artifacts/reports%2Fcoverage.xml",
        content=b"<coverage />",
    )

    async with gitlab_client as client:
        payload = await client.get_job_artifacts_file(42, 302, "reports/coverage.xml")

    request = httpx_mock.get_requests()[0]
    assert request.method == "GET"
    assert str(request.url) == f"{BASE}/projects/42/jobs/302/artifacts/reports%2Fcoverage.xml"
    assert payload == b"<coverage />"


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------


async def test_get_issue(httpx_mock: HTTPXMock, gitlab_client: GitLabClient) -> None:
    """GET /projects/:id/issues/:issue_iid.

    https://docs.gitlab.com/api/issues/#single-project-issue
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/issues/12",
        json={
            "id": 312,
            "iid": 12,
            "title": "Rotation breaks CI",
            "description": "Unit tests fail after rotation change.",
            "state": "opened",
            "labels": ["bug"],
            "web_url": "https://gitlab.example.com/group/project/-/issues/12",
            "author": {"id": 11, "username": "forge-user", "name": "Forge User"},
            "created_at": "2026-09-03T08:00:00Z",
            "updated_at": "2026-09-03T08:00:00Z",
        },
    )

    async with gitlab_client as client:
        issue = await client.get_issue(42, 12)

    request = httpx_mock.get_requests()[0]
    assert request.method == "GET"
    assert str(request.url) == f"{BASE}/projects/42/issues/12"
    assert issue.iid == 12
    assert issue.title == "Rotation breaks CI"
    assert issue.state == "opened"


async def test_list_issues(httpx_mock: HTTPXMock, gitlab_client: GitLabClient) -> None:
    """GET /projects/:id/issues?state=<state> (and optional labels).

    https://docs.gitlab.com/api/issues/#list-project-issues
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/issues?state=opened&per_page=20&labels=bug",
        json=[
            {
                "id": 312,
                "iid": 12,
                "title": "Rotation breaks CI",
                "state": "opened",
                "labels": ["bug"],
            }
        ],
    )

    async with gitlab_client as client:
        issues = await client.list_issues(42, state="opened", labels="bug", per_page=20)

    request = httpx_mock.get_requests()[0]
    assert request.method == "GET"
    assert request.url.params["state"] == "opened"
    assert request.url.params["labels"] == "bug"
    assert request.url.params["per_page"] == "20"
    assert len(issues) == 1
    assert issues[0].iid == 12


async def test_create_issue_note(httpx_mock: HTTPXMock, gitlab_client: GitLabClient) -> None:
    """POST /projects/:id/issues/:issue_iid/notes with {"body": ...}.

    https://docs.gitlab.com/api/notes/#create-new-issue-note
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/issues/12/notes",
        method="POST",
        status_code=201,
        json={"id": 601, "body": "Fix merged in MR !8."},
    )

    async with gitlab_client as client:
        note = await client.create_issue_note(42, 12, "Fix merged in MR !8.")

    request = httpx_mock.get_requests()[0]
    assert request.method == "POST"
    assert str(request.url) == f"{BASE}/projects/42/issues/12/notes"
    assert request_bodies(httpx_mock) == [{"body": "Fix merged in MR !8."}]
    assert note["body"] == "Fix merged in MR !8."


# ---------------------------------------------------------------------------
# Projects, groups, search, hooks
# ---------------------------------------------------------------------------


async def test_get_project_by_id(httpx_mock: HTTPXMock, gitlab_client: GitLabClient) -> None:
    """GET /projects/:id (numeric ID).

    https://docs.gitlab.com/api/projects/#get-a-single-project
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42",
        json={
            "id": 42,
            "name": "project",
            "path_with_namespace": "group/project",
            "description": "Sample service",
            "web_url": "https://gitlab.example.com/group/project",
            "default_branch": "main",
        },
    )

    async with gitlab_client as client:
        project = await client.get_project(42)

    request = httpx_mock.get_requests()[0]
    assert request.method == "GET"
    assert str(request.url) == f"{BASE}/projects/42"
    assert project.id == 42
    assert project.path_with_namespace == "group/project"
    assert project.default_branch == "main"


async def test_get_project_by_url_encoded_path(
    httpx_mock: HTTPXMock, gitlab_client: GitLabClient
) -> None:
    """GET /projects/:id accepts a URL-encoded path (``%2F`` for ``/``).

    https://docs.gitlab.com/api/projects/#single-project
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/group%2Fproject",
        json={
            "id": 42,
            "name": "project",
            "path_with_namespace": "group/project",
            "default_branch": "main",
        },
    )

    async with gitlab_client as client:
        project = await client.get_project("group/project")

    request = httpx_mock.get_requests()[0]
    assert str(request.url) == f"{BASE}/projects/group%2Fproject"
    assert project.id == 42


async def test_search_code(httpx_mock: HTTPXMock, gitlab_client: GitLabClient) -> None:
    """GET /projects/:id/search?scope=blobs&search=<query>.

    https://docs.gitlab.com/api/search/#scope-blobs
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/search?scope=blobs&search=rotate_token&per_page=100",
        json=[
            {
                "basename": "rotation",
                "data": "def rotate_token():\n    return True\n",
                "path": "src/auth/rotation.py",
                "filename": "rotation.py",
                "ref": "main",
            }
        ],
    )

    async with gitlab_client as client:
        results = await client.search_code(42, "rotate_token")

    request = httpx_mock.get_requests()[0]
    assert request.method == "GET"
    assert str(request.url) == (
        f"{BASE}/projects/42/search?scope=blobs&search=rotate_token&per_page=100"
    )
    assert request.url.params["scope"] == "blobs"
    assert request.url.params["search"] == "rotate_token"
    assert results[0]["path"] == "src/auth/rotation.py"


async def test_list_project_hooks(httpx_mock: HTTPXMock, gitlab_client: GitLabClient) -> None:
    """GET /projects/:id/hooks.

    https://docs.gitlab.com/api/projects/#list-project-hooks
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/hooks?per_page=100",
        json=[
            {
                "id": 9,
                "url": "https://forge.example.com/webhook",
                "push_events": True,
                "merge_requests_events": True,
                "note_events": True,
                "pipeline_events": True,
                "job_events": True,
                "issues_events": False,
            }
        ],
    )

    async with gitlab_client as client:
        hooks = await client.list_project_hooks(42)

    request = httpx_mock.get_requests()[0]
    assert request.method == "GET"
    assert str(request.url) == f"{BASE}/projects/42/hooks?per_page=100"
    assert len(hooks) == 1
    assert hooks[0]["pipeline_events"] is True


async def test_create_project_hook(httpx_mock: HTTPXMock, gitlab_client: GitLabClient) -> None:
    """POST /projects/:id/hooks with documented event-toggle attributes.

    https://docs.gitlab.com/api/projects/#add-project-hook
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/hooks",
        method="POST",
        status_code=201,
        json={
            "id": 10,
            "url": "https://forge.example.com/webhook",
            "push_events": True,
            "merge_requests_events": True,
            "note_events": True,
            "pipeline_events": True,
            "job_events": True,
            "issues_events": False,
        },
    )

    async with gitlab_client as client:
        hook = await client.create_project_hook(
            42, "https://forge.example.com/webhook", "forge-hook-token"
        )

    request = httpx_mock.get_requests()[0]
    assert request.method == "POST"
    assert str(request.url) == f"{BASE}/projects/42/hooks"
    assert request_bodies(httpx_mock) == [
        {
            "url": "https://forge.example.com/webhook",
            "token": "forge-hook-token",
            "push_events": True,
            "merge_requests_events": True,
            "note_events": True,
            "pipeline_events": True,
            "job_events": True,
            "issues_events": False,
        }
    ]
    assert hook["id"] == 10


async def test_list_group_projects(httpx_mock: HTTPXMock, gitlab_client: GitLabClient) -> None:
    """GET /groups/:id/projects?include_subgroups=true.

    https://docs.gitlab.com/api/groups/#list-a-groups-projects
    """
    httpx_mock.add_response(
        url=f"{BASE}/groups/4/projects?include_subgroups=true&per_page=100",
        json=[
            {
                "id": 42,
                "name": "project",
                "path_with_namespace": "group/project",
                "default_branch": "main",
            }
        ],
    )

    async with gitlab_client as client:
        projects = await client.list_group_projects(4)

    request = httpx_mock.get_requests()[0]
    assert request.method == "GET"
    assert str(request.url) == (f"{BASE}/groups/4/projects?include_subgroups=true&per_page=100")
    assert request.url.params["include_subgroups"] == "true"
    assert len(projects) == 1
    assert projects[0]["id"] == 42


# ---------------------------------------------------------------------------
# Create commit (spec only — method not implemented yet)
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="create_commit lands in M1")
async def test_create_commit_documented_request(
    httpx_mock: HTTPXMock, gitlab_client: GitLabClient
) -> None:
    """Spec: create_commit must POST to the documented commits endpoint.

    When ``GitLabClient.create_commit`` lands, it MUST issue exactly one
    request of the form (https://docs.gitlab.com/api/commits/#create-a-commit):

        POST /projects/:id/repository/commits
        Content-Type: application/json

        {
          "branch": "forge/fix-pipeline",           # required: target branch
          "commit_message": "Apply review fixes",   # required
          "start_branch": "main",                   # optional: parent branch,
                                                    # mutually exclusive with
                                                    # start_sha (a full 40-char
                                                    # commit SHA). Defaults to
                                                    # "branch" when omitted.
          "actions": [                              # batched file actions
            {
              "action": "update",                   # create|update|delete|move|chmod
              "file_path": "src/config.py",         # required: full path
              "content": "path = 'forge.yml'",      # required except delete/chmod/move
              "encoding": "text",                   # text (default) | base64
              "last_commit_id": "a1b2c3d4..."       # last known file commit id;
                                                    # ONLY considered for update,
                                                    # move and delete actions
                                                    # (conflict guard)
            }
          ]
        }

    A 201 response body is the created commit (see fixtures/gitlab/commit.json):
    {"id": <40-char sha>, "short_id", "created_at", "parent_ids", "title",
     "message", "author_name", "author_email", "web_url", ...}.
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/repository/commits",
        method="POST",
        status_code=201,
        json=load_fixture("commit"),
    )

    async with gitlab_client as client:
        created = await client.create_commit(  # type: ignore[attr-defined]
            42,
            branch="forge/fix-pipeline",
            commit_message="Apply review fixes",
            start_branch="main",
            actions=[
                {
                    "action": "update",
                    "file_path": "src/config.py",
                    "content": "path = 'forge.yml'",
                    "last_commit_id": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
                }
            ],
        )

    request = httpx_mock.get_requests()[0]
    assert request.method == "POST"
    assert str(request.url) == f"{BASE}/projects/42/repository/commits"
    body = request_bodies(httpx_mock)[0]
    assert body["branch"] == "forge/fix-pipeline"
    assert body["commit_message"] == "Apply review fixes"
    assert body["start_branch"] == "main"
    assert body["actions"][0]["last_commit_id"] == ("a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0")
    assert created["short_id"] == "d1c2b3a4"
