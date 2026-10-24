"""Native authority survives name changes and HTTP uncertainty never duplicates a write."""

import json

import httpx
import pytest

from forge.providers.github.auth import AppAuth
from forge.providers.github.client import API
from forge.providers.github.transport import Transport, TransportFailure
from tests.fakes.ports import FixedClock
from tests.test_github_engine_auth import key as key, response
from tests.test_ports import NOW, REPO


def selected_repository(**changes):
    return httpx.Response(
        200,
        json={"total_count": 1, "repositories": [{"id": 42, "full_name": "owner/repo", **changes}]},
    )


def api_fixture(key, handler):
    _, pem = key
    clock = FixedClock(NOW)
    requests = []

    async def route(request):
        requests.append(request)
        if request.url.path == "/app/installations/7/access_tokens":
            requested = json.loads(request.content)["permissions"]
            return httpx.Response(201, json=response(clock, requested))
        return handler(request)

    transport = Transport(transport=httpx.MockTransport(route))
    auth = AppAuth("install-1", "app", pem, {"42": 7}, clock, transport)
    return API(auth, transport), transport, requests


async def test_rest_resolves_native_id_for_each_call_and_uses_current_name(key):
    def handler(request):
        if request.url.path == "/installation/repositories":
            assert request.url.params["per_page"] == "1"
            return selected_repository(full_name="new-owner/renamed")
        assert request.url.path == "/repos/new-owner/renamed/git/ref/heads/fix/issue"
        assert request.headers["Authorization"] == "Bearer synthetic-installation-token"
        return httpx.Response(200, json={"object": {"sha": "a" * 40}})

    api, transport, requests = api_fixture(key, handler)
    try:
        reply = await api.request(REPO, "GET", "git/ref/heads/fix%2Fissue")
        assert reply.document == {"object": {"sha": "a" * 40}}
        await api.request(REPO, "GET", "git/ref/heads/fix/issue")
        assert sum(request.url.path == "/installation/repositories" for request in requests) == 2
        assert all("old" not in request.url.path for request in requests)
    finally:
        await transport.close()


@pytest.mark.parametrize("stage", ["metadata", "resource"])
async def test_only_authorized_resource_absence_is_classified_as_resource_not_found(key, stage):
    def handler(request):
        if request.url.path == "/installation/repositories" and stage == "resource":
            return selected_repository()
        return httpx.Response(404)

    api, transport, _ = api_fixture(key, handler)
    try:
        with pytest.raises(TransportFailure) as caught:
            await api.request(REPO, "GET", "git/commits/" + "a" * 40)
        assert caught.value.reason == (
            "repository_unavailable" if stage == "metadata" else "resource_not_found"
        )
    finally:
        await transport.close()


@pytest.mark.parametrize("status", [401, 429, 500])
async def test_only_an_explicit_unauthenticated_response_retries_a_write(key, status):
    writes = []

    def handler(request):
        if request.url.path == "/installation/repositories":
            return selected_repository()
        writes.append(request)
        return httpx.Response(status, json={"message": "synthetic-private-error"})

    api, transport, requests = api_fixture(key, handler)
    try:
        with pytest.raises(TransportFailure):
            await api.request(REPO, "POST", "issues/1/comments", body={"body": "plan"}, write=True)
        assert len(writes) == (2 if status == 401 else 1)
        # Read and write scopes require separate tokens; only a confirmed401 renews one.
        assert sum("access_tokens" in request.url.path for request in requests) == (
            3 if status == 401 else 2
        )
    finally:
        await transport.close()


async def test_native_http_errors_keep_no_remote_message_and_never_retry_uncertainty(key):
    def route(request):
        if request.url.path == "/installation/repositories":
            return selected_repository()
        return httpx.Response(422, json={"message": "synthetic-secret"})

    api, transport, requests = api_fixture(key, route)
    try:
        with pytest.raises(TransportFailure) as caught:
            await api.request(
                REPO, "POST", "git/refs", body={"ref": "refs/heads/forge/test"}, write=True
            )
        assert caught.value.status == 422
        assert "synthetic" not in caught.value.evidence + str(caught.value)
        assert sum(request.url.path.endswith("/git/refs") for request in requests) == 1
    finally:
        await transport.close()


@pytest.mark.parametrize(
    "path",
    ["//attacker", "../repos", "%2e%2e/repos", "git/%252e%252e", "git/ref?key=x", "git/%0aref"],
)
async def test_unsafe_resource_path_refuses_before_http(key, path):
    api, transport, requests = api_fixture(key, lambda request: httpx.Response(200))
    try:
        with pytest.raises(ValueError):
            await api.request(REPO, "GET", path)
        assert not requests
    finally:
        await transport.close()


async def test_metadata_id_mismatch_and_implicit_write_authority_refuse(key):
    api, transport, requests = api_fixture(key, lambda request: selected_repository(id=43))
    try:
        with pytest.raises(ValueError, match="write authority"):
            await api.request(REPO, "POST", "issues/1/comments", body={})
        assert not requests
        with pytest.raises(TransportFailure, match="repository_identity_mismatch"):
            await api.metadata(REPO)
    finally:
        await transport.close()


@pytest.mark.parametrize("label", ["needs%20review", "why%3F", "C%23"])
async def test_encoded_native_label_spaces_remain_in_the_path(key, label):
    def handler(request):
        if request.url.path == "/installation/repositories":
            return selected_repository()
        assert request.url.raw_path == b"/repos/owner/repo/issues/1/labels/" + label.encode()
        assert not request.url.query
        return httpx.Response(204)

    api, transport, _ = api_fixture(key, handler)
    try:
        assert (
            await api.request(REPO, "DELETE", "issues/1/labels/" + label, write=True)
        ).document is None
    finally:
        await transport.close()


async def test_documented_installation_metadata_discards_clone_credentials_and_rejects_scope_growth(
    key,
):
    count = 1

    def handler(request):
        return httpx.Response(
            200,
            json={
                "total_count": count,
                "repositories": [
                    {"id": 42, "full_name": "owner/repo", "temp_clone_token": "synthetic-secret"}
                ],
            },
        )

    api, transport, requests = api_fixture(key, handler)
    try:
        metadata = await api.metadata(REPO)
        assert metadata.document == {"id": 42, "full_name": "owner/repo"}
        assert "synthetic-secret" not in repr(metadata)
        count = 2
        with pytest.raises(TransportFailure, match="identity_mismatch"):
            await api.metadata(REPO)
        assert all("/repositories/42" not in request.url.path for request in requests)
    finally:
        await transport.close()
