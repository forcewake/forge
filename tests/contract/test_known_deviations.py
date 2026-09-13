"""Strict-xfail specs for KNOWN deviations from the documented GitLab API.

These tests pin ``GitLabClient`` to the DOCUMENTED GitLab REST API v4
behavior. They fail against the current client (which carries upstream
codeward behavior); each ``reason`` cites the upstream file and the
documented alternative. They must xfail — and the moment the client is
fixed they must start passing (strict=True), at which point the spec is
simply un-xfail'd and becomes a regular contract test.

Deviations documented here:

1. FIXED (F27): raw/unified MR diffs were fetched from
   ``GET /projects/:id/merge_requests/:iid/diffs`` with an undocumented
   ``Accept: text/plain`` request header (upstream
   ``codeward/gitlab/client.py``, ``get_merge_request_raw_diff``). The
   client now uses the documented
   ``GET /projects/:id/merge_requests/:iid/raw_diffs`` endpoint
   (gitlab-org/gitlab MR !178813); the legacy header path survives ONLY as
   the 404 fallback for older CE instances — pinned as a regular contract
   test below.
   https://docs.gitlab.com/api/merge_requests/#get-merge-request-diff-files

2. The shared request helper retries EVERY method on 500/502/503 (and 429)
   with exponential backoff (upstream ``codeward/gitlab/client.py``,
   ``_RETRYABLE_STATUSES`` / ``_MAX_RETRIES`` / ``_request``). For
   non-idempotent POST endpoints such as ``create_merge_request``
   (``POST /projects/:id/merge_requests``) a retry can duplicate the
   resource: if the first attempt succeeded server-side but the response
   was lost, the retry creates a second merge request.

   FIXED (F06): the creation endpoints (``create_commit``,
   ``create_merge_request``, ``create_pipeline``, ``create_issue_note``)
   now opt out of the generic retry via ``retry=False``;
   ``test_create_merge_request_is_not_retried_on_503`` below has been
   un-xfail'd accordingly and is a regular contract test.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from forge.gitlab.client import GitLabAPIError, GitLabClient

from .conftest import BASE

UNIFIED_DIFF_TEXT = (
    "diff --git a/src/auth/rotation.py b/src/auth/rotation.py\n"
    "index 0000000..1111111 100644\n"
    "--- /dev/null\n"
    "+++ b/src/auth/rotation.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def rotate_token():\n"
    "+    return True\n"
)


async def test_raw_diff_uses_documented_raw_diffs_endpoint(
    httpx_mock: HTTPXMock, gitlab_client: GitLabClient
) -> None:
    """get_merge_request_raw_diff uses the documented raw_diffs endpoint (F27).

    Was a strict-xfail spec while the client called /diffs with the
    undocumented ``Accept: text/plain`` header; un-xfail'd now that the
    documented endpoint
    (``GET /projects/:id/merge_requests/:iid/raw_diffs``, gitlab-org/gitlab
    !178813) is the primary path.
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7/raw_diffs",
        text=UNIFIED_DIFF_TEXT,
    )

    async with gitlab_client as client:
        raw = await client.get_merge_request_raw_diff(42, 7)

    assert raw == UNIFIED_DIFF_TEXT
    request = httpx_mock.get_requests()[-1]
    assert request.method == "GET"
    assert str(request.url) == f"{BASE}/projects/42/merge_requests/7/raw_diffs"
    assert request.headers.get("accept") != "text/plain"


async def test_raw_diff_falls_back_to_diffs_when_raw_diffs_404s(
    httpx_mock: HTTPXMock, gitlab_client: GitLabClient
) -> None:
    """Older CE without raw_diffs (404) falls back to the legacy /diffs path.

    The fallback keeps the pre-F27 behavior — ``GET .../diffs`` with the
    undocumented ``Accept: text/plain`` header — so instances older than
    gitlab-org/gitlab !178813 keep working. A non-404 failure must NOT fall
    back (the error propagates).
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7/raw_diffs",
        status_code=404,
    )
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7/diffs",
        text=UNIFIED_DIFF_TEXT,
    )

    async with gitlab_client as client:
        raw = await client.get_merge_request_raw_diff(42, 7)

    assert raw == UNIFIED_DIFF_TEXT
    requests = httpx_mock.get_requests()
    assert len(requests) == 2
    assert str(requests[0].url) == f"{BASE}/projects/42/merge_requests/7/raw_diffs"
    assert str(requests[1].url) == f"{BASE}/projects/42/merge_requests/7/diffs"
    assert requests[1].headers.get("accept") == "text/plain"  # legacy deviation, fallback only


async def test_raw_diff_non_404_error_does_not_fall_back(
    httpx_mock: HTTPXMock, gitlab_client: GitLabClient
) -> None:
    """A 500 on raw_diffs propagates — the fallback is only for missing endpoints."""
    # A 500 is retryable: one registered response per attempt.
    for _ in range(3):
        httpx_mock.add_response(
            url=f"{BASE}/projects/42/merge_requests/7/raw_diffs",
            status_code=500,
        )

    async with gitlab_client as client:
        with pytest.raises(GitLabAPIError) as excinfo:
            await client.get_merge_request_raw_diff(42, 7)

    assert excinfo.value.status_code == 500
    # Every retry stays on raw_diffs — /diffs is never contacted but on 404.
    requests = httpx_mock.get_requests()
    assert len(requests) == 3
    assert all(r.url.path.endswith("raw_diffs") for r in requests)


async def test_create_merge_request_is_not_retried_on_503(
    httpx_mock: HTTPXMock, gitlab_client: GitLabClient
) -> None:
    """POST /projects/:id/merge_requests must be sent exactly once on a 503.

    create_merge_request is non-idempotent: if the first attempt reached the
    server and GitLab only failed to answer, a retry creates a SECOND merge
    request. It therefore opts out of the generic retry (``retry=False``,
    F06) and fails fast on 5xx instead of replaying the POST.

    Was an xfail spec while the client retried every method; un-xfail'd now
    that create_merge_request passes ``retry=False``.
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests",
        method="POST",
        status_code=503,
    )

    async with gitlab_client as client:
        with pytest.raises(GitLabAPIError):
            await client.create_merge_request(
                42,
                source_branch="feature/token-rotation",
                target_branch="main",
                title="Add token rotation flow",
            )

    # The 503 failed fast: there must never be a second POST on the wire.
    assert len(httpx_mock.get_requests()) == 1
