"""Tests for the Commits API additions on GitLabClient (M1 write path).

Covers the ADR-0005 rule that create_commit is never auto-retried and turns
timeouts into CommitOutcomeUnknown, plus list_commits / create_pipeline /
list_pipelines(sha=...).
"""

import httpx
import pytest
from pytest_httpx import HTTPXMock

from forge.gitlab.client import CommitOutcomeUnknown, GitLabAPIError, GitLabClient

BASE = "https://gitlab.test/api/v4"


@pytest.fixture()
def gitlab_client():
    return GitLabClient(base_url="https://gitlab.test", token="test-token", timeout=5.0)


async def test_create_commit_posts_actions(httpx_mock: HTTPXMock, gitlab_client: GitLabClient):
    responses = []

    def _respond(request: httpx.Request) -> httpx.Response:
        responses.append(request)
        return httpx.Response(201, json={"id": "abc123", "message": "forge: implement 7"})

    httpx_mock.add_callback(_respond, url=f"{BASE}/projects/42/repository/commits")

    async with gitlab_client as client:
        commit = await client.create_commit(
            42,
            "factory/7/deadbeef",
            [{"action": "create", "file_path": "forge-demo/x.md", "content": "hi"}],
            "forge: implement 7",
            start_branch="main",
        )

    assert commit["id"] == "abc123"
    body = responses[0].read().decode()
    assert '"start_branch"' in body.replace(" ", "")
    assert "factory/7/deadbeef" in body


async def test_create_commit_single_post_on_503_no_generic_retry(
    httpx_mock: HTTPXMock, gitlab_client: GitLabClient
):
    """create_commit bypasses the generic retry: exactly one POST on 503."""
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/repository/commits",
        method="POST",
        status_code=503,
        text="unavailable",
    )

    async with gitlab_client as client:
        with pytest.raises(GitLabAPIError) as exc_info:
            await client.create_commit(
                42,
                "factory/7/deadbeef",
                [{"action": "create", "file_path": "a", "content": "b"}],
                "forge: implement 7",
            )

    assert exc_info.value.status_code == 503
    assert len(httpx_mock.get_requests()) == 1


async def test_create_commit_timeout_raises_commit_outcome_unknown(
    httpx_mock: HTTPXMock, gitlab_client: GitLabClient
):
    httpx_mock.add_exception(
        httpx.TimeoutException("timed out"), url=f"{BASE}/projects/42/repository/commits"
    )

    async with gitlab_client as client:
        with pytest.raises(CommitOutcomeUnknown):
            await client.create_commit(
                42,
                "factory/7/deadbeef",
                [{"action": "create", "file_path": "a", "content": "b"}],
                "forge: implement 7",
            )

    # No retry was attempted for the non-idempotent write.
    assert len(httpx_mock.get_requests()) == 1


async def test_list_commits_returns_sha_message_and_parents(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/repository/commits?ref_name=factory%2F7%2Fdeadbeef&per_page=100",
        json=[
            {"id": "sha2", "short_id": "sha2", "message": "second", "parent_ids": ["sha1"]},
            {"id": "sha1", "short_id": "sha1", "message": "first"},
        ],
    )
    async with GitLabClient("https://gitlab.test", "t") as client:
        commits = await client.list_commits(42, "factory/7/deadbeef")

    # parent_ids feeds the F07 parent check in the writer's reconciliation.
    assert commits[0] == {
        "sha": "sha2",
        "short_id": "sha2",
        "message": "second",
        "parent_ids": ["sha1"],
    }
    assert commits[1] == {
        "sha": "sha1",
        "short_id": "sha1",
        "message": "first",
        "parent_ids": [],
    }


async def test_create_pipeline_posts_ref(httpx_mock: HTTPXMock):
    bodies = []

    def _respond(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read().decode())
        return httpx.Response(
            201, json={"id": 77, "ref": "factory/7/deadbeef", "status": "pending"}
        )

    httpx_mock.add_callback(_respond, url=f"{BASE}/projects/42/pipeline")

    async with GitLabClient("https://gitlab.test", "t") as client:
        pipeline = await client.create_pipeline(42, "factory/7/deadbeef")

    assert pipeline["id"] == 77
    assert "factory/7/deadbeef" in bodies[0]


async def test_list_pipelines_filters_by_sha(httpx_mock: HTTPXMock):
    urls = []

    def _respond(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json=[{"id": 9, "status": "success", "sha": "abc"}])

    httpx_mock.add_callback(
        _respond,
        url=f"{BASE}/projects/42/pipelines?per_page=20&order_by=id&sort=desc&sha=abc",
    )

    async with GitLabClient("https://gitlab.test", "t") as client:
        pipelines = await client.list_pipelines(42, sha="abc")

    assert pipelines[0].id == 9
    assert pipelines[0].sha == "abc"
