"""Retained deviation lessons on complete native evidence and single mutations.

No raw_diffs/Accept:text-plain compatibility path remains. Implementation reads
exact immutable native objects; a failed object read cannot become diff evidence.
"""

import httpx
import pytest

from forge.domain.verdicts import Unknown
from forge.port_values import ReadUnavailable
from tests.native_clients_fixture import resource_calls


async def test_raw_diff_uses_documented_raw_diffs_endpoint(gitlab):
    tree = await gitlab.repositories.read_tree(gitlab.repo, gitlab.sha)
    assert tree.sha == gitlab.sha
    request = resource_calls(gitlab, "/repository/tree")[0]
    assert request.url.params["ref"] == gitlab.sha and request.url.params["recursive"] == "true"
    assert request.headers["accept"] == "application/json"
    assert not any("/raw_diffs" in r.url.path or "/diffs" in r.url.path for r in gitlab.wire.calls)


async def test_raw_diff_falls_back_to_diffs_when_raw_diffs_404s(gitlab):
    # Historical fallback is retired: missing immutable evidence fails closed.
    gitlab.wire.routes["repository/commits/" + gitlab.sha] = httpx.Response(404)
    with pytest.raises(ReadUnavailable):
        await gitlab.repositories.read_tree(gitlab.repo, gitlab.sha)
    assert len(resource_calls(gitlab, "/repository/commits/" + gitlab.sha)) == 1
    assert not any(
        "/diffs" in r.url.path or "/repository/tree" in r.url.path for r in gitlab.wire.calls
    )


async def test_raw_diff_non_404_error_does_not_fall_back(gitlab):
    gitlab.wire.routes["repository/commits/" + gitlab.sha] = httpx.Response(500)
    with pytest.raises(ReadUnavailable):
        await gitlab.repositories.read_tree(gitlab.repo, gitlab.sha)
    assert len(resource_calls(gitlab, "/repository/commits/" + gitlab.sha)) == 1
    assert not any("/diffs" in r.url.path for r in gitlab.wire.calls)


async def test_create_merge_request_is_not_retried_on_503(gitlab):
    gitlab.wire.mutations["POST", "merge_requests"] = httpx.Response(503)
    result = await gitlab.writes.ensure_draft(gitlab.repo, gitlab.dispatch.ref, "Title", "Body")
    assert isinstance(result, Unknown)
    assert len(resource_calls(gitlab, "/merge_requests", "POST")) == 1
