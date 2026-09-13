"""GitHub client tests: JWT shape, token lifecycle, REST/GraphQL semantics.

Everything runs against pytest-httpx — no live GitHub. Endpoint/field ground
truth: docs/research/github-api.md.
"""

import base64
import json
import time
from datetime import datetime, timezone
from functools import lru_cache

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pytest_httpx import HTTPXMock

from forge.integrations.github import (
    GitHubAPIError,
    GitHubAppCredentials,
    GitHubClient,
    GitHubRateLimited,
    GitHubRepositoryReader,
    GitHubStaleBranchError,
)
from tests.fixtures.fake_github import sample_installation_token

BASE = "https://api.github.test"
MINT_URL = f"{BASE}/app/installations/777/access_tokens"
TEST_HEAD = "a" * 40


@lru_cache(maxsize=1)
def _keypair() -> tuple[str, str]:
    """One 2048-bit RSA keypair per test session (JWT signing only)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return private_pem, public_pem


def make_credentials(base_url: str = BASE) -> GitHubAppCredentials:
    private_pem, _ = _keypair()
    return GitHubAppCredentials(
        app_id="123456",
        private_key=private_pem,
        installation_id="777",
        base_url=base_url,
    )


def mint_requests(httpx_mock: HTTPXMock) -> list:
    return [r for r in httpx_mock.get_requests() if str(r.url) == MINT_URL]


@pytest.fixture()
async def github(httpx_mock: HTTPXMock):
    """A GitHubClient whose single token mint is pre-registered."""
    httpx_mock.add_response(url=MINT_URL, method="POST", json=sample_installation_token("ghs_test"))
    client = GitHubClient(base_url=BASE, token_provider=make_credentials())
    yield client
    await client.aclose()


@pytest.fixture()
async def reader(github: GitHubClient) -> GitHubRepositoryReader:
    return GitHubRepositoryReader(github, "acme", "widget")


# ---------------------------------------------------------------------------
# JWT shape (research §1.1) — no live call
# ---------------------------------------------------------------------------


async def test_app_jwt_is_rs256_with_required_claims():
    private_pem, public_pem = _keypair()
    creds = GitHubAppCredentials(
        app_id="123456", private_key=private_pem, installation_id="777", base_url=BASE
    )
    before = datetime.now(timezone.utc)
    token = creds.app_jwt()

    assert pyjwt.get_unverified_header(token)["alg"] == "RS256"
    claims = pyjwt.decode(token, public_pem, algorithms=["RS256"])
    assert claims["iss"] == "123456"
    assert abs(claims["iat"] - (before.timestamp() - 60)) < 5  # clock-skew margin
    assert claims["exp"] == claims["iat"] + 600  # 9 min ahead; 10 min is the cap


async def test_mint_sends_bearer_jwt_with_github_headers(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=MINT_URL, method="POST", json=sample_installation_token())
    _, public_pem = _keypair()

    await make_credentials().installation_token()

    request = mint_requests(httpx_mock)[0]
    assert request.headers["x-github-api-version"] == "2022-11-28"
    assert request.headers["accept"] == "application/vnd.github+json"
    auth = request.headers["authorization"]
    assert auth.startswith("Bearer ")  # JWTs never use the `token` prefix
    claims = pyjwt.decode(auth.removeprefix("Bearer "), public_pem, algorithms=["RS256"])
    assert claims["iss"] == "123456"


# ---------------------------------------------------------------------------
# Installation token lifecycle (research §1.2/§1.6)
# ---------------------------------------------------------------------------


async def test_installation_token_is_cached(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=MINT_URL, method="POST", json=sample_installation_token("ghs_one"))

    creds = make_credentials()
    first = await creds.installation_token()
    second = await creds.installation_token()

    assert first.token == second.token == "ghs_one"
    assert len(mint_requests(httpx_mock)) == 1  # cache until near expiry


async def test_token_near_expiry_is_re_minted(httpx_mock: HTTPXMock):
    # 200 s left — under the 5-minute safety margin, so a fresh mint is due.
    httpx_mock.add_response(
        url=MINT_URL, method="POST", json=sample_installation_token("ghs_old", 200)
    )
    httpx_mock.add_response(url=MINT_URL, method="POST", json=sample_installation_token("ghs_new"))

    creds = make_credentials()
    assert (await creds.installation_token()).token == "ghs_old"
    assert (await creds.installation_token()).token == "ghs_new"
    assert len(mint_requests(httpx_mock)) == 2


async def test_invalidate_forces_re_mint(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=MINT_URL, method="POST", json=sample_installation_token("ghs_a"))
    httpx_mock.add_response(url=MINT_URL, method="POST", json=sample_installation_token("ghs_b"))

    creds = make_credentials()
    await creds.installation_token()
    await creds.invalidate()
    assert (await creds.installation_token()).token == "ghs_b"


async def test_client_re_mints_once_on_401(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=MINT_URL, method="POST", json=sample_installation_token("ghs_a"))
    httpx_mock.add_response(
        url=f"{BASE}/repos/acme/widget", status_code=401, json={"message": "Bad credentials"}
    )
    httpx_mock.add_response(url=MINT_URL, method="POST", json=sample_installation_token("ghs_b"))
    httpx_mock.add_response(
        url=f"{BASE}/repos/acme/widget",
        json={"id": 70010, "name": "widget", "full_name": "acme/widget", "private": True},
    )

    client = GitHubClient(base_url=BASE, token_provider=make_credentials())
    try:
        repo = await client.get_repository("acme", "widget")
    finally:
        await client.aclose()

    assert repo["full_name"] == "acme/widget"
    assert len(mint_requests(httpx_mock)) == 2  # exactly one re-mint
    api_requests = [
        r for r in httpx_mock.get_requests() if str(r.url) == f"{BASE}/repos/acme/widget"
    ]
    assert len(api_requests) == 2
    assert api_requests[0].headers["authorization"] == "Bearer ghs_a"
    assert api_requests[1].headers["authorization"] == "Bearer ghs_b"


# ---------------------------------------------------------------------------
# Rate limits (research §8.4) — surfaced, never hot-looped
# ---------------------------------------------------------------------------


async def test_rate_limit_surfaces_retry_after_without_retries(
    httpx_mock: HTTPXMock, github: GitHubClient
):
    httpx_mock.add_response(
        url=f"{BASE}/repos/acme/widget",
        status_code=403,
        headers={"Retry-After": "123", "x-ratelimit-remaining": "0"},
        json={"message": "API rate limit exceeded"},
    )

    with pytest.raises(GitHubRateLimited) as exc_info:
        await github.get_repository("acme", "widget")

    assert exc_info.value.retry_after == 123  # Retry-After respected verbatim
    assert (
        len([r for r in httpx_mock.get_requests() if str(r.url) == f"{BASE}/repos/acme/widget"])
        == 1
    )


async def test_primary_limit_without_retry_after_uses_reset(
    httpx_mock: HTTPXMock, github: GitHubClient
):
    reset_at = int(time.time()) + 1800
    httpx_mock.add_response(
        url=f"{BASE}/repos/acme/widget",
        status_code=403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset_at)},
        json={"message": "API rate limit exceeded"},
    )

    with pytest.raises(GitHubRateLimited) as exc_info:
        await github.get_repository("acme", "widget")

    assert 1 <= exc_info.value.retry_after <= 1800


# ---------------------------------------------------------------------------
# REST semantics
# ---------------------------------------------------------------------------


async def test_get_issue_maps_onto_neutral_dto(httpx_mock: HTTPXMock, github: GitHubClient):
    httpx_mock.add_response(
        url=f"{BASE}/repos/acme/widget/issues/42",
        json={
            "id": 1010,
            "number": 42,
            "title": "Add password reset",
            "body": "Users are locked out.",
            "state": "open",
            "html_url": "https://github.test/acme/widget/issues/42",
            "labels": [{"name": "bug"}],
            "user": {"id": 501, "login": "alice", "name": "Alice"},
        },
    )

    issue = await github.get_issue("acme", "widget", 42)

    assert issue.iid == 42
    assert issue.title == "Add password reset"
    assert issue.description == "Users are locked out."
    assert issue.author is not None and issue.author.username == "alice"


async def test_get_issue_comments_follows_link_pagination(
    httpx_mock: HTTPXMock, github: GitHubClient
):
    httpx_mock.add_response(
        url=f"{BASE}/repos/acme/widget/issues/42/comments?per_page=100",
        json=[{"id": 1}, {"id": 2}],
        headers={
            "link": (
                f'<{BASE}/repos/acme/widget/issues/42/comments?per_page=100&page=2>; rel="next"'
            )
        },
    )
    httpx_mock.add_response(
        url=f"{BASE}/repos/acme/widget/issues/42/comments?per_page=100&page=2",
        json=[{"id": 3}],
    )

    comments = await github.get_issue_comments("acme", "widget", 42)

    assert [c["id"] for c in comments] == [1, 2, 3]


async def test_create_issue_comment_is_never_retried(httpx_mock: HTTPXMock, github: GitHubClient):
    httpx_mock.add_response(
        url=f"{BASE}/repos/acme/widget/issues/42/comments", method="POST", status_code=500
    )

    with pytest.raises(GitHubAPIError):
        await github.create_issue_comment("acme", "widget", 42, "Working on it.")

    # Non-idempotent: the API call fired exactly once — no retry, ever.
    comment_calls = [r for r in httpx_mock.get_requests() if str(r.url).endswith("/comments")]
    assert len(comment_calls) == 1


async def test_idempotent_gets_retry_on_5xx(httpx_mock: HTTPXMock, github: GitHubClient):
    httpx_mock.add_response(url=f"{BASE}/repos/acme/widget", status_code=503)
    httpx_mock.add_response(
        url=f"{BASE}/repos/acme/widget",
        json={"id": 70010, "name": "widget", "full_name": "acme/widget"},
    )

    repo = await github.get_repository("acme", "widget")

    assert repo["name"] == "widget"
    assert len([r for r in httpx_mock.get_requests() if "repos" in str(r.url)]) == 2


async def test_validation_422_surfaces_errors_list(httpx_mock: HTTPXMock, github: GitHubClient):
    httpx_mock.add_response(
        url=f"{BASE}/repos/acme/widget/pulls",
        method="POST",
        status_code=422,
        json={
            "message": "Validation Failed",
            "errors": [{"resource": "PullRequest", "field": "head", "code": "invalid"}],
        },
    )

    with pytest.raises(GitHubAPIError) as exc_info:
        await github.create_draft_pr("acme", "widget", head="forge/42/abc", base="main", title="t")

    assert exc_info.value.status_code == 422
    assert exc_info.value.errors[0]["code"] == "invalid"


# ---------------------------------------------------------------------------
# Branch head + createCommitOnBranch (research §3)
# ---------------------------------------------------------------------------


async def test_get_branch_head_from_refs_endpoint(httpx_mock: HTTPXMock, github: GitHubClient):
    httpx_mock.add_response(
        url=f"{BASE}/repos/acme/widget/git/refs/heads/main",
        json={"ref": "refs/heads/main", "object": {"sha": TEST_HEAD, "type": "commit"}},
    )

    assert await github.get_branch_head("acme", "widget", "main") == TEST_HEAD


async def test_get_branch_head_falls_back_to_commits_endpoint(
    httpx_mock: HTTPXMock, github: GitHubClient
):
    httpx_mock.add_response(url=f"{BASE}/repos/acme/widget/git/refs/heads/main", status_code=404)
    httpx_mock.add_response(url=f"{BASE}/repos/acme/widget/commits/main", json={"sha": TEST_HEAD})

    assert await github.get_branch_head("acme", "widget", "main") == TEST_HEAD


async def test_create_commit_on_branch_sends_cas_and_base64_contents(
    httpx_mock: HTTPXMock, github: GitHubClient
):
    httpx_mock.add_response(
        url=f"{BASE}/graphql",
        json={
            "data": {
                "createCommitOnBranch": {
                    "clientMutationId": "op-123",
                    "commit": {"oid": "b" * 40, "url": "https://github.test/c/b"},
                }
            }
        },
    )

    result = await github.create_commit_on_branch(
        "acme",
        "widget",
        "forge/42/abcd1234",
        headline="forge: implement 42",
        additions=[("src/app.py", "print('hi')\n")],
        deletions=["old.txt"],
        expected_head_oid=TEST_HEAD,
        client_mutation_id="op-123",
    )

    assert result["oid"] == "b" * 40
    assert result["client_mutation_id"] == "op-123"
    (request,) = [r for r in httpx_mock.get_requests() if str(r.url) == f"{BASE}/graphql"]
    body = json.loads(request.content)
    mutation_input = body["variables"]["input"]
    assert mutation_input["expectedHeadOid"] == TEST_HEAD  # the CAS token
    assert mutation_input["branch"] == {
        "repositoryNameWithOwner": "acme/widget",
        "branchName": "forge/42/abcd1234",
    }
    assert mutation_input["fileChanges"]["additions"] == [
        {"path": "src/app.py", "contents": base64.b64encode(b"print('hi')\n").decode("ascii")}
    ]
    assert mutation_input["fileChanges"]["deletions"] == [{"path": "old.txt"}]


async def test_stale_data_mismatch_surfaces_as_drift(httpx_mock: HTTPXMock, github: GitHubClient):
    httpx_mock.add_response(
        url=f"{BASE}/graphql",
        json={
            "data": {"createCommitOnBranch": None},
            "errors": [
                {
                    "type": "STALE_DATA",
                    "path": ["createCommitOnBranch"],
                    "message": f'Expected branch to point to "{TEST_HEAD}" but it did not.',
                }
            ],
        },
    )

    with pytest.raises(GitHubStaleBranchError) as exc_info:
        await github.create_commit_on_branch(
            "acme",
            "widget",
            "forge/42/abcd1234",
            headline="forge: implement 42",
            additions=[("src/app.py", "x")],
            expected_head_oid=TEST_HEAD,
        )

    assert exc_info.value.expected_head_oid == TEST_HEAD
    assert exc_info.value.branch == "forge/42/abcd1234"


async def test_graphql_non_stale_errors_surface_as_api_error(
    httpx_mock: HTTPXMock, github: GitHubClient
):
    httpx_mock.add_response(
        url=f"{BASE}/graphql",
        json={
            "data": {"createCommitOnBranch": None},
            "errors": [{"type": "UNPROCESSABLE", "message": "Ref does not exist"}],
        },
    )

    with pytest.raises(GitHubAPIError) as exc_info:
        await github.create_commit_on_branch(
            "acme",
            "widget",
            "ghost",
            headline="m",
            expected_head_oid=TEST_HEAD,
        )

    assert "Ref does not exist" in exc_info.value.message


# ---------------------------------------------------------------------------
# Pull requests (research §4)
# ---------------------------------------------------------------------------


async def test_create_draft_pr_sends_draft_true(httpx_mock: HTTPXMock, github: GitHubClient):
    httpx_mock.add_response(
        url=f"{BASE}/repos/acme/widget/pulls",
        method="POST",
        json={"number": 7, "draft": True, "html_url": "https://github.test/pull/7"},
    )

    pr = await github.create_draft_pr(
        "acme", "widget", head="forge/42/abcd1234", base="main", title="Draft: x"
    )

    assert pr["number"] == 7
    (request,) = [r for r in httpx_mock.get_requests() if str(r.url).endswith("/pulls")]
    body = json.loads(request.content)
    assert body["draft"] is True
    assert body["head"] == "forge/42/abcd1234"
    assert body["base"] == "main"


async def test_get_pr_by_head_filters_owner_branch(httpx_mock: HTTPXMock, github: GitHubClient):
    httpx_mock.add_response(
        url=(
            f"{BASE}/repos/acme/widget/pulls?state=open&head=acme%3Aforge%2F42%2Fabcd1234&base=main"
        ),
        json=[
            {
                "number": 7,
                "state": "open",
                "draft": True,
                "head": {"ref": "forge/42/abcd1234", "sha": "b" * 40},
            }
        ],
    )

    pr = await github.get_pr_by_head("acme", "widget", "forge/42/abcd1234", base="main")

    assert pr is not None and pr["number"] == 7


async def test_get_pr_by_head_returns_none_when_absent(httpx_mock: HTTPXMock, github: GitHubClient):
    httpx_mock.add_response(
        url=f"{BASE}/repos/acme/widget/pulls?state=open&head=acme%3Aghost&base=main",
        json=[],
    )

    assert await github.get_pr_by_head("acme", "widget", "ghost", base="main") is None


# ---------------------------------------------------------------------------
# Verification reads (research §5)
# ---------------------------------------------------------------------------


async def test_list_check_runs_for_sha(httpx_mock: HTTPXMock, github: GitHubClient):
    httpx_mock.add_response(
        url=f"{BASE}/repos/acme/widget/commits/{TEST_HEAD}/check-runs",
        json={
            "total_count": 1,
            "check_runs": [
                {
                    "name": "ci",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": TEST_HEAD,
                }
            ],
        },
    )

    runs = await github.list_check_runs_for_sha("acme", "widget", TEST_HEAD)

    assert runs[0]["conclusion"] == "success"


async def test_list_workflow_runs_for_sha_filters_by_name(
    httpx_mock: HTTPXMock, github: GitHubClient
):
    runs_response = {
        "total_count": 2,
        "workflow_runs": [
            {"id": 1, "name": "CI", "head_sha": TEST_HEAD},
            {"id": 2, "name": "Docs", "head_sha": TEST_HEAD},
        ],
    }
    httpx_mock.add_response(
        url=f"{BASE}/repos/acme/widget/actions/runs?head_sha={TEST_HEAD}&per_page=100",
        json=runs_response,
    )
    httpx_mock.add_response(
        url=f"{BASE}/repos/acme/widget/actions/runs?head_sha={TEST_HEAD}&per_page=100",
        json=runs_response,
    )

    all_runs = await github.list_workflow_runs_for_sha("acme", "widget", TEST_HEAD)
    ci_runs = await github.list_workflow_runs_for_sha(
        "acme", "widget", TEST_HEAD, workflow_name="CI"
    )

    assert len(all_runs) == 2
    assert [run["id"] for run in ci_runs] == [1]


# ---------------------------------------------------------------------------
# Repository reader: AuthoritativeReader semantics
# ---------------------------------------------------------------------------


async def test_reader_get_file_returns_full_base64_blob(
    httpx_mock: HTTPXMock, reader: GitHubRepositoryReader
):
    content = base64.b64encode("hello world\n".encode()).decode()
    httpx_mock.add_response(
        url=f"{BASE}/repos/acme/widget/contents/src/app.py?ref={TEST_HEAD}",
        json={
            "name": "app.py",
            "path": "src/app.py",
            "sha": "c" * 40,
            "size": 12,
            "type": "file",
            "content": content,
            "encoding": "base64",
        },
    )

    repo_file = await reader.get_file(0, "src/app.py", ref=TEST_HEAD)

    assert repo_file.encoding == "base64"
    assert repo_file.file_path == "src/app.py"


async def test_reader_oversized_file_resolves_through_blob_api(
    httpx_mock: HTTPXMock, reader: GitHubRepositoryReader
):
    # >1 MB files arrive with empty inline content; the reader must fetch the
    # complete blob instead of silently truncating (ADR-0016 §2).
    payload = "x" * 64
    httpx_mock.add_response(
        url=f"{BASE}/repos/acme/widget/contents/big.txt?ref=main",
        json={
            "name": "big.txt",
            "path": "big.txt",
            "sha": "d" * 40,
            "size": 2_000_000,
            "type": "file",
            "content": "",
            "encoding": None,
        },
    )
    httpx_mock.add_response(
        url=f"{BASE}/repos/acme/widget/git/blobs/{'d' * 40}",
        json={
            "sha": "d" * 40,
            "content": base64.b64encode(payload.encode()).decode("ascii"),
            "encoding": "base64",
        },
    )

    assert await reader.read_text("big.txt", ref="main") == payload


async def test_reader_rejects_truncated_tree(httpx_mock: HTTPXMock, reader: GitHubRepositoryReader):
    httpx_mock.add_response(
        url=f"{BASE}/repos/acme/widget/git/trees/{TEST_HEAD}?recursive=1",
        json={"sha": TEST_HEAD, "tree": [{"path": "a", "type": "blob"}], "truncated": True},
    )

    with pytest.raises(GitHubAPIError, match="truncated"):
        await reader.get_tree(0, ref=TEST_HEAD, recursive=True)


async def test_reader_rejects_non_regular_files(
    httpx_mock: HTTPXMock, reader: GitHubRepositoryReader
):
    httpx_mock.add_response(
        url=f"{BASE}/repos/acme/widget/contents/link?ref=main",
        json={"name": "link", "path": "link", "type": "symlink", "size": 5, "sha": "e" * 40},
    )

    with pytest.raises(GitHubAPIError, match="symlink"):
        await reader.get_file(0, "link", ref="main")


async def test_reader_maps_issue_from_client(httpx_mock: HTTPXMock, reader: GitHubRepositoryReader):
    httpx_mock.add_response(
        url=f"{BASE}/repos/acme/widget/issues/42",
        json={"id": 1010, "number": 42, "title": "T", "body": None, "state": "open"},
    )

    issue = await reader.get_issue(0, 42)

    assert issue.iid == 42
    assert issue.description is None
