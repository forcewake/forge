"""Strict-xfail specs for KNOWN deviations from the documented GitLab API.

These tests pin ``GitLabClient`` to the DOCUMENTED GitLab REST API v4
behavior. They fail against the current client (which carries upstream
codeward behavior); each ``reason`` cites the upstream file and the
documented alternative. They must xfail — and the moment the client is
fixed they must start passing (strict=True), at which point the spec is
simply un-xfail'd and becomes a regular contract test.

Deviations documented here:

1. Raw/unified MR diffs are fetched from
   ``GET /projects/:id/merge_requests/:iid/diffs`` with an undocumented
   ``Accept: text/plain`` request header (upstream
   ``codeward/gitlab/client.py``, ``get_merge_request_raw_diff``). The
   documented alternatives are:
   - ``GET /projects/:id/merge_requests/:iid/raw_diffs`` — returns the raw
     unified diff (gitlab-org/gitlab MR !178813).
   - ``GET /projects/:id/merge_requests/:iid/diffs?unidiff=true`` — returns
     diff files whose ``diff`` fields are full unified diffs
     (parameter introduced in GitLab 16.5).
   https://docs.gitlab.com/api/merge_requests/#get-merge-request-diff-files

2. The shared request helper retries EVERY method on 500/502/503 (and 429)
   with exponential backoff (upstream ``codeward/gitlab/client.py``,
   ``_RETRYABLE_STATUSES`` / ``_MAX_RETRIES`` / ``_request``). For
   non-idempotent POST endpoints such as ``create_merge_request``
   (``POST /projects/:id/merge_requests``) a retry can duplicate the
   resource: if the first attempt succeeded server-side but the response
   was lost, the retry creates a second merge request.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from forge.gitlab.client import GitLabClient

from .conftest import BASE, load_fixture

UPSTREAM_RAW_DIFF_REF = (
    "upstream codeward/gitlab/client.py get_merge_request_raw_diff() calls "
    "GET /projects/:id/merge_requests/:iid/diffs with an undocumented "
    "'Accept: text/plain' header"
)

UNIFIED_DIFF_TEXT = (
    "diff --git a/src/auth/rotation.py b/src/auth/rotation.py\n"
    "index 0000000..1111111 100644\n"
    "--- /dev/null\n"
    "+++ b/src/auth/rotation.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def rotate_token():\n"
    "+    return True\n"
)


@pytest.mark.xfail(
    strict=True,
    reason=(
        f"{UPSTREAM_RAW_DIFF_REF}; documented alternative is "
        "GET /projects/:id/merge_requests/:iid/raw_diffs "
        "(gitlab-org/gitlab !178813), https://docs.gitlab.com/api/merge_requests/"
    ),
)
async def test_raw_diff_uses_documented_raw_diffs_endpoint(
    httpx_mock: HTTPXMock, gitlab_client: GitLabClient
) -> None:
    """get_merge_request_raw_diff must use the documented raw_diffs endpoint.

    The documented endpoint for raw unified diff text is
    ``GET /projects/:id/merge_requests/:iid/raw_diffs``, which serves the
    same payload as appending ``.diff`` to a merge request URL. The client
    must not send ``Accept: text/plain`` on the JSON ``/diffs`` endpoint.
    """
    # The client currently calls /diffs; register it so no retry storm starts.
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7/diffs",
        text="[]",
    )
    # The documented raw-diff endpoint the client SHOULD call.
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7/raw_diffs",
        text=UNIFIED_DIFF_TEXT,
        is_optional=True,
    )

    async with gitlab_client as client:
        raw = await client.get_merge_request_raw_diff(42, 7)

    assert raw == UNIFIED_DIFF_TEXT
    request = httpx_mock.get_requests()[-1]
    assert request.method == "GET"
    assert str(request.url) == f"{BASE}/projects/42/merge_requests/7/raw_diffs"
    assert request.headers.get("accept") != "text/plain"


@pytest.mark.xfail(
    strict=True,
    reason=(
        f"{UPSTREAM_RAW_DIFF_REF}; documented alternative is "
        "GET /projects/:id/merge_requests/:iid/diffs?unidiff=true "
        "(unidiff parameter, GitLab 16.5+), "
        "https://docs.gitlab.com/api/merge_requests/#get-merge-request-diff-files"
    ),
)
async def test_raw_diff_uses_documented_unidiff_param(
    httpx_mock: HTTPXMock, gitlab_client: GitLabClient
) -> None:
    """A raw-diff request to /diffs must pass the documented unidiff=true.

    ``unidiff=true`` makes the documented diffs endpoint return diff files
    whose ``diff`` fields are full unified diffs instead of abbreviated
    hunks. The client currently sends no ``unidiff`` parameter at all.
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests/7/diffs",
        json=load_fixture("merge_request_diffs"),
    )

    async with gitlab_client as client:
        await client.get_merge_request_raw_diff(42, 7)

    request = httpx_mock.get_requests()[-1]
    assert str(request.url) == f"{BASE}/projects/42/merge_requests/7/diffs"
    assert request.url.params.get("unidiff") == "true"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "upstream codeward/gitlab/client.py _request() retries every method "
        "(incl. non-idempotent POST) on 500/502/503 via _RETRYABLE_STATUSES/"
        "_MAX_RETRIES; create_merge_request must NOT be auto-retried because "
        "the first attempt may have succeeded server-side (duplicate MR risk)"
    ),
)
async def test_create_merge_request_is_not_retried_on_503(
    httpx_mock: HTTPXMock, gitlab_client: GitLabClient
) -> None:
    """POST /projects/:id/merge_requests must be sent exactly once on a 503.

    create_merge_request is non-idempotent: if the first attempt reached the
    server and GitLab only failed to answer, a retry creates a SECOND merge
    request. The documented-safe behavior is to fail fast on 5xx for
    creation endpoints instead of replaying the POST.
    """
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests",
        method="POST",
        status_code=503,
    )
    httpx_mock.add_response(
        url=f"{BASE}/projects/42/merge_requests",
        method="POST",
        status_code=201,
        json=load_fixture("merge_request"),
        is_optional=True,
    )

    async with gitlab_client as client:
        created = await client.create_merge_request(
            42,
            source_branch="feature/token-rotation",
            target_branch="main",
            title="Add token rotation flow",
        )

    # Either the 201 arrived (single request) or the client raised — but
    # there must never be a second POST on the wire.
    assert len(httpx_mock.get_requests()) == 1
    assert created["iid"] == 7
