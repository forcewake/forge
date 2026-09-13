import pytest
from pytest_httpx import HTTPXMock

from forge.gitlab.client import GitLabClient, GitLabAPIError

BASE = "https://gitlab.test/api/v4"


@pytest.fixture()
def gitlab_client():
    return GitLabClient(
        base_url="https://gitlab.test",
        token="test-token",
        timeout=5.0,
    )


async def test_get_merge_request(httpx_mock: HTTPXMock, gitlab_client: GitLabClient):
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7",
        json={
            "id": 100,
            "iid": 7,
            "title": "Add auth",
            "state": "opened",
            "source_branch": "feature/auth",
            "target_branch": "main",
            "web_url": "https://gitlab.test/g/p/-/merge_requests/7",
        },
    )

    async with gitlab_client as client:
        mr = await client.get_merge_request(42, 7)

    assert mr.iid == 7
    assert mr.title == "Add auth"
    assert mr.state == "opened"
    assert mr.source_branch == "feature/auth"


async def test_get_merge_request_diffs(httpx_mock: HTTPXMock, gitlab_client: GitLabClient):
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7/diffs?per_page=100",
        json=[
            {
                "old_path": "src/auth.py",
                "new_path": "src/auth.py",
                "diff": "@@ -0,0 +1,10 @@\n+def login():\n+    pass",
                "new_file": True,
                "renamed_file": False,
                "deleted_file": False,
            }
        ],
    )

    async with gitlab_client as client:
        diffs = await client.get_merge_request_diffs(42, 7)

    assert len(diffs) == 1
    assert diffs[0].new_path == "src/auth.py"
    assert diffs[0].new_file is True


async def test_pagination_two_pages(httpx_mock: HTTPXMock, gitlab_client: GitLabClient):
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7/diffs?per_page=100",
        json=[{"old_path": "a.py", "new_path": "a.py", "diff": ""}],
        headers={"x-next-page": "2"},
    )
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7/diffs?per_page=100&page=2",
        json=[{"old_path": "b.py", "new_path": "b.py", "diff": ""}],
    )

    async with gitlab_client as client:
        diffs = await client.get_merge_request_diffs(42, 7)

    assert len(diffs) == 2
    assert diffs[0].new_path == "a.py"
    assert diffs[1].new_path == "b.py"


async def test_create_mr_note(httpx_mock: HTTPXMock, gitlab_client: GitLabClient):
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7/notes",
        method="POST",
        json={
            "id": 500,
            "body": "LGTM!",
            "author": {"id": 99, "name": "Bot", "username": "forge-bot"},
            "created_at": "2026-03-21T10:00:00Z",
        },
    )

    async with gitlab_client as client:
        note = await client.create_mr_note(42, 7, "LGTM!")

    assert note.id == 500
    assert note.body == "LGTM!"
    assert note.author.username == "forge-bot"


async def test_create_mr_discussion_with_position(
    httpx_mock: HTTPXMock, gitlab_client: GitLabClient
):
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7/discussions",
        method="POST",
        json={
            "id": "abc123",
            "individual_note": False,
            "notes": [
                {
                    "id": 501,
                    "body": "Potential null dereference here",
                    "created_at": "2026-03-21T10:00:00Z",
                }
            ],
        },
    )

    position = {
        "base_sha": "aaa",
        "head_sha": "bbb",
        "start_sha": "ccc",
        "new_path": "src/auth.py",
        "old_path": "src/auth.py",
        "position_type": "text",
        "new_line": 5,
    }

    async with gitlab_client as client:
        disc = await client.create_mr_discussion(42, 7, "Potential null dereference here", position)

    assert disc.id == "abc123"
    assert len(disc.notes) == 1


async def test_retry_on_429(httpx_mock: HTTPXMock, gitlab_client: GitLabClient):
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7",
        status_code=429,
        headers={"Retry-After": "0"},
        text="rate limited",
    )
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7",
        json={
            "id": 100,
            "iid": 7,
            "title": "Retry success",
            "state": "opened",
            "source_branch": "main",
            "target_branch": "main",
        },
    )

    async with gitlab_client as client:
        mr = await client.get_merge_request(42, 7)

    assert mr.title == "Retry success"


async def test_retry_on_500(httpx_mock: HTTPXMock, gitlab_client: GitLabClient):
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7",
        status_code=500,
        text="internal error",
    )
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7",
        json={
            "id": 100,
            "iid": 7,
            "title": "Recovered",
            "state": "opened",
            "source_branch": "main",
            "target_branch": "main",
        },
    )

    async with gitlab_client as client:
        mr = await client.get_merge_request(42, 7)

    assert mr.title == "Recovered"


async def test_no_retry_on_404(httpx_mock: HTTPXMock, gitlab_client: GitLabClient):
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/999",
        status_code=404,
        text="Not Found",
    )

    async with gitlab_client as client:
        with pytest.raises(GitLabAPIError) as exc_info:
            await client.get_merge_request(42, 999)

    assert exc_info.value.status_code == 404


async def test_non_idempotent_posts_disable_retry(
    httpx_mock: HTTPXMock, gitlab_client: GitLabClient
):
    """F06: create_pipeline / create_merge_request / create_issue_note must
    pass retry=False to _request — a lost response must be reconciled, not
    replayed (each can duplicate its resource on a retry)."""
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/pipeline",
        method="POST",
        json={"id": 77, "status": "pending"},
    )
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests",
        method="POST",
        json={
            "id": 101,
            "iid": 8,
            "title": "t",
            "state": "opened",
            "source_branch": "feat",
            "target_branch": "main",
        },
    )
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/issues/7/notes",
        method="POST",
        json={"id": 5, "body": "hi"},
    )

    original = gitlab_client._request
    retries: list[bool | None] = []

    async def spying(method: str, path: str, **kwargs):
        retries.append(kwargs.get("retry"))
        return await original(method, path, **kwargs)

    gitlab_client._request = spying  # type: ignore[method-assign]
    async with gitlab_client as client:
        await client.create_pipeline(42, "factory/7/x")
        await client.create_merge_request(42, "feat", "main", "t")
        await client.create_issue_note(42, 7, "hi")

    assert retries == [False, False, False]


async def test_non_idempotent_posts_single_attempt_on_503(
    httpx_mock: HTTPXMock, gitlab_client: GitLabClient
):
    """F06: with retry disabled, a 503 fails fast — exactly one POST each."""
    for url in (
        f"{BASE}/projects/42/pipeline",
        f"{BASE}/projects/42/merge_requests",
        f"{BASE}/projects/42/issues/7/notes",
    ):
        httpx_mock.add_response(url=url, method="POST", status_code=503, text="unavailable")

    async with gitlab_client as client:
        with pytest.raises(GitLabAPIError):
            await client.create_pipeline(42, "factory/7/x")
        with pytest.raises(GitLabAPIError):
            await client.create_merge_request(42, "feat", "main", "t")
        with pytest.raises(GitLabAPIError):
            await client.create_issue_note(42, 7, "hi")

    assert len(httpx_mock.get_requests()) == 3


async def test_get_file_url_encoding(httpx_mock: HTTPXMock, gitlab_client: GitLabClient):
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/repository/files/src%2Fauth%2Fmiddleware.py?ref=HEAD",
        json={
            "file_name": "middleware.py",
            "file_path": "src/auth/middleware.py",
            "content": "ZGVmIGF1dGgoKTogcGFzcw==",
            "encoding": "base64",
        },
    )

    async with gitlab_client as client:
        f = await client.get_file(42, "src/auth/middleware.py")

    assert f.file_name == "middleware.py"
    assert f.file_path == "src/auth/middleware.py"


async def test_client_context_manager(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=f"{BASE}/projects/1/merge_requests/1",
        json={
            "id": 1,
            "iid": 1,
            "title": "Test",
            "state": "opened",
            "source_branch": "a",
            "target_branch": "b",
        },
    )

    async with GitLabClient("https://gitlab.test", "tok") as client:
        mr = await client.get_merge_request(1, 1)
        assert mr.iid == 1


async def test_ensure_label_exists_conflict(httpx_mock: HTTPXMock, gitlab_client: GitLabClient):
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/labels",
        method="POST",
        status_code=409,
        text="Label already exists",
    )

    async with gitlab_client as client:
        result = await client.ensure_label_exists(42, "ai-reviewed", "#00ff00")

    assert result["exists"] is True


async def test_get_job_artifacts_file(httpx_mock: HTTPXMock, gitlab_client: GitLabClient):
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/jobs/101/artifacts/gl-sast-report.json",
        content=b'{"vulnerabilities": []}',
    )

    async with gitlab_client as client:
        data = await client.get_job_artifacts_file(42, 101, "gl-sast-report.json")

    assert data == b'{"vulnerabilities": []}'


async def test_get_job_artifacts_file_url_encodes_path(
    httpx_mock: HTTPXMock, gitlab_client: GitLabClient
):
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/jobs/101/artifacts/reports%2Fgl-sast-report.json",
        content=b"{}",
    )

    async with gitlab_client as client:
        data = await client.get_job_artifacts_file(42, 101, "reports/gl-sast-report.json")

    assert data == b"{}"


async def test_create_commit_comment(httpx_mock: HTTPXMock, gitlab_client: GitLabClient):
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/repository/commits/abc123/comments",
        method="POST",
        json={
            "note": "Pipeline failed: test error",
            "author": {"id": 99, "name": "Bot", "username": "forge-bot"},
        },
    )

    async with gitlab_client as client:
        result = await client.create_commit_comment(42, "abc123", "Pipeline failed: test error")

    assert result["note"] == "Pipeline failed: test error"


async def test_get_project_by_path(httpx_mock: HTTPXMock, gitlab_client: GitLabClient):
    httpx_mock.add_response(
        url=f"{BASE}/projects/mygroup%2Fmyproject",
        json={
            "id": 42,
            "name": "myproject",
            "path_with_namespace": "mygroup/myproject",
            "web_url": "https://gitlab.test/mygroup/myproject",
            "default_branch": "main",
        },
    )

    async with gitlab_client as client:
        project = await client.get_project("mygroup/myproject")

    assert project.id == 42
    assert project.path_with_namespace == "mygroup/myproject"
    assert project.default_branch == "main"


async def test_get_project_by_numeric_id(httpx_mock: HTTPXMock, gitlab_client: GitLabClient):
    httpx_mock.add_response(
        url=f"{BASE}/projects/42",
        json={
            "id": 42,
            "name": "myproject",
            "path_with_namespace": "mygroup/myproject",
        },
    )

    async with gitlab_client as client:
        project = await client.get_project(42)

    assert project.id == 42


async def test_list_merge_requests(httpx_mock: HTTPXMock, gitlab_client: GitLabClient):
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests?state=opened&per_page=20",
        json=[
            {
                "id": 100,
                "iid": 1,
                "title": "First MR",
                "state": "opened",
                "source_branch": "feat-1",
                "target_branch": "main",
            },
            {
                "id": 101,
                "iid": 2,
                "title": "Second MR",
                "state": "opened",
                "source_branch": "feat-2",
                "target_branch": "main",
            },
        ],
    )

    async with gitlab_client as client:
        mrs = await client.list_merge_requests(42)

    assert len(mrs) == 2
    assert mrs[0].iid == 1
    assert mrs[1].title == "Second MR"


async def test_get_issue(httpx_mock: HTTPXMock, gitlab_client: GitLabClient):
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/issues/5",
        json={
            "id": 200,
            "iid": 5,
            "title": "Bug report",
            "state": "opened",
            "labels": ["bug"],
            "web_url": "https://gitlab.test/g/p/-/issues/5",
        },
    )

    async with gitlab_client as client:
        issue = await client.get_issue(42, 5)

    assert issue.iid == 5
    assert issue.title == "Bug report"
    assert "bug" in issue.labels


async def test_list_issues(httpx_mock: HTTPXMock, gitlab_client: GitLabClient):
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/issues?state=opened&per_page=20",
        json=[
            {
                "id": 200,
                "iid": 1,
                "title": "Issue one",
                "state": "opened",
            },
        ],
    )

    async with gitlab_client as client:
        issues = await client.list_issues(42)

    assert len(issues) == 1
    assert issues[0].title == "Issue one"


async def test_list_issues_with_labels(httpx_mock: HTTPXMock, gitlab_client: GitLabClient):
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/issues?state=opened&per_page=20&labels=bug%2Cp1",
        json=[],
    )

    async with gitlab_client as client:
        issues = await client.list_issues(42, labels="bug,p1")

    assert issues == []


async def test_create_issue(httpx_mock: HTTPXMock, gitlab_client: GitLabClient):
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/issues",
        method="POST",
        json={
            "id": 300,
            "iid": 10,
            "title": "New issue",
            "state": "opened",
            "web_url": "https://gitlab.test/g/p/-/issues/10",
        },
    )

    async with gitlab_client as client:
        issue = await client.create_issue(42, "New issue", description="Details here")

    assert issue.iid == 10
    assert issue.title == "New issue"


async def test_search_code(httpx_mock: HTTPXMock, gitlab_client: GitLabClient):
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/search?scope=blobs&search=def+login&per_page=100",
        json=[
            {
                "basename": "auth",
                "data": "def login():\n    pass",
                "filename": "src/auth.py",
                "ref": "main",
            },
        ],
    )

    async with gitlab_client as client:
        results = await client.search_code(42, "def login")

    assert len(results) == 1
    assert results[0]["filename"] == "src/auth.py"


async def test_list_project_labels(httpx_mock: HTTPXMock, gitlab_client: GitLabClient):
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/labels?per_page=100",
        json=[
            {"id": 1, "name": "bug", "color": "#ff0000"},
            {"id": 2, "name": "feature", "color": "#00ff00", "description": "New features"},
        ],
    )

    async with gitlab_client as client:
        labels = await client.list_project_labels(42)

    assert len(labels) == 2
    assert labels[0].name == "bug"
    assert labels[1].description == "New features"


async def test_list_pipelines(httpx_mock: HTTPXMock, gitlab_client: GitLabClient):
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/pipelines?per_page=20&order_by=id&sort=desc&ref=main",
        json=[
            {
                "id": 500,
                "status": "success",
                "ref": "main",
                "web_url": "https://gitlab.test/g/p/-/pipelines/500",
            },
        ],
    )

    async with gitlab_client as client:
        pipelines = await client.list_pipelines(42, ref="main")

    assert len(pipelines) == 1
    assert pipelines[0].id == 500
    assert pipelines[0].status == "success"


async def test_get_branch_head_returns_head_commit(
    httpx_mock: HTTPXMock, gitlab_client: GitLabClient
):
    """F28: get_branch_head reads the branch object, not commit history."""
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/repository/branches/main",
        json={"name": "main", "commit": {"id": "abc123def", "short_id": "abc123d"}},
    )

    async with gitlab_client as client:
        head = await client.get_branch_head(42, "main")

    assert head == "abc123def"
    request = httpx_mock.get_requests()[0]
    assert str(request.url) == f"{BASE}/projects/42/repository/branches/main"


async def test_get_branch_head_missing_branch_raises_404(
    httpx_mock: HTTPXMock, gitlab_client: GitLabClient
):
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/repository/branches/feature%2Fgone",
        status_code=404,
        text="404 Branch Not Found",
    )

    async with gitlab_client as client:
        with pytest.raises(GitLabAPIError) as exc_info:
            await client.get_branch_head(42, "feature/gone")

    assert exc_info.value.status_code == 404
    assert len(httpx_mock.get_requests()) == 1  # one GET, never a history scan


async def test_pagination_warns_when_page_cap_reached(
    httpx_mock: HTTPXMock, gitlab_client: GitLabClient, monkeypatch: pytest.MonkeyPatch, caplog
):
    """F27 honesty fix: when the last fetched page still advertises
    X-Next-Page, the truncation is logged — evidence must not silently end
    at the 50-page cap. _MAX_PAGES is lowered here to keep the test small."""
    import logging

    import forge.gitlab.client as client_module

    monkeypatch.setattr(client_module, "_MAX_PAGES", 2)
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/repository/commits?per_page=100&ref_name=main",
        json=[{"id": "a", "short_id": "a", "message": "1"}],
        headers={"x-next-page": "2", "x-total-count": "300"},
    )
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/repository/commits?per_page=100&ref_name=main&page=2",
        json=[{"id": "b", "short_id": "b", "message": "2"}],
        headers={"x-next-page": "3"},
    )

    with caplog.at_level(logging.WARNING, logger="forge.gitlab.client"):
        async with gitlab_client as client:
            commits = await client.list_commits(42, "main")

    assert [c["sha"] for c in commits] == ["a", "b"]  # partial list returned as-is
    assert any(
        "pagination limit reached" in r.message and "evidence may be incomplete" in r.message
        for r in caplog.records
    )


async def test_pagination_no_warning_when_last_page_reached(
    httpx_mock: HTTPXMock, gitlab_client: GitLabClient, caplog
):
    import logging

    httpx_mock.add_response(
        url=f"{BASE}/projects/42/repository/commits?per_page=100&ref_name=main",
        json=[{"id": "a", "short_id": "a", "message": "1"}],
        headers={"x-next-page": "2"},
    )
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/repository/commits?per_page=100&ref_name=main&page=2",
        json=[{"id": "b", "short_id": "b", "message": "2"}],
    )

    with caplog.at_level(logging.WARNING, logger="forge.gitlab.client"):
        async with gitlab_client as client:
            commits = await client.list_commits(42, "main")

    assert [c["sha"] for c in commits] == ["a", "b"]
    assert not any("pagination limit reached" in r.message for r in caplog.records)
