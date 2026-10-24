"""Outbound App authority is repository scoped and never follows target-controlled URLs."""

import asyncio
from datetime import timedelta
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import jwt
import pytest

from forge.domain.plan import RepositoryIdentity
from forge.port_values import RepoRef
from forge.providers.github.auth import AppAuth, READ, WRITE
from forge.providers.github.transport import Endpoint, Transport, TransportFailure
from tests.fakes.ports import FixedClock
from tests.test_ports import NOW, REPO


@pytest.fixture(scope="module")
def key():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return private.public_key(), pem


def response(clock, requested, **changes):
    return {
        "token": "synthetic-installation-token",
        "expires_at": (clock.now() + timedelta(hours=1)).isoformat(),
        "repository_selection": "selected",
        "repositories": [{"id": 42}],
        "permissions": requested,
        **changes,
    }


async def test_repository_scoped_tokens_are_separate_for_read_and_write_and_refresh_once(key):
    public, pem = key
    clock = FixedClock(NOW)
    requests = []

    async def handler(request):
        requests.append(request)
        claims = jwt.decode(
            request.headers["Authorization"].split()[1],
            public,
            algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
        )
        assert claims == {
            "iss": "Iv1.test",
            "iat": int(clock.now().timestamp()) - 60,
            "exp": int(clock.now().timestamp()) + 540,
        }
        data = json.loads(request.content)
        assert request.url.path == "/app/installations/7/access_tokens"
        assert data["repository_ids"] == [42]
        assert data["permissions"] in (READ, WRITE)
        return httpx.Response(201, json=response(clock, data["permissions"]))

    transport = Transport(transport=httpx.MockTransport(handler))
    auth = AppAuth("install-1", "Iv1.test", pem, {"42": 7}, clock, transport)
    try:
        tokens = await asyncio.gather(*(auth.token(REPO) for _ in range(6)))
        assert len(requests) == 1 and all(token is tokens[0] for token in tokens)
        assert "synthetic-installation-token" not in repr(tokens[0])
        write = await auth.token(REPO, write=True)
        assert write.write and not tokens[0].write and len(requests) == 2
        assert json.loads(requests[0].content)["permissions"] == READ
        assert json.loads(requests[1].content)["permissions"] == WRITE
        clock.instant += timedelta(minutes=56)
        assert await auth.token(REPO) is not tokens[0] and len(requests) == 3
    finally:
        await transport.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"repositories": [{"id": 42}, {"id": 43}]},
        {"repositories": [{"id": 42}, "unproven"]},
        {"repositories": [{"id": True}]},
        {"repository_selection": "all"},
        {"permissions": {**READ, "contents": "write"}},
        {"permissions": {**READ, "administration": "write"}},
        {"token": "unsafe\nheader"},
        {"expires_at": "2099-01-01T00:00:00Z"},
        {"expires_at": "2026-10-23T00:00:00"},
    ],
)
async def test_unproven_or_widened_installation_token_is_never_cached(key, changes):
    _, pem = key
    clock = FixedClock(NOW)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(201, json=response(clock, READ, **changes))

    transport = Transport(transport=httpx.MockTransport(handler))
    auth = AppAuth("install-1", "app", pem, {"42": 7}, clock, transport)
    try:
        for _ in range(2):
            with pytest.raises(TransportFailure, match="invalid_installation_token_scope"):
                await auth.token(REPO)
        assert len(calls) == 2
    finally:
        await transport.close()


async def test_cross_connection_repository_refuses_before_any_http(key):
    _, pem = key
    calls = []
    transport = Transport(transport=httpx.MockTransport(lambda request: calls.append(request)))
    auth = AppAuth("install-1", "app", pem, {"42": 7}, FixedClock(NOW), transport)
    try:
        for provider, connection, native in (
            ("gitlab", "install-1", "42"),
            ("github", "other", "42"),
            ("github", "install-1", "43"),
        ):
            repo = RepoRef(
                identity=RepositoryIdentity(
                    provider=provider, connection_id=connection, repository_id=native
                ),
                full_name="owner/project",
            )
            with pytest.raises(TransportFailure, match="repository_not_authorized"):
                await auth.token(repo)
        assert not calls
    finally:
        await transport.close()


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
async def test_transport_never_retries_redirects_or_uncertain_writes_or_exposes_error_body(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            headers={
                "location": "https://attacker.example",
                "x-github-request-id": "ABCD:1234",
                "retry-after": "7",
            },
            json={"token": "synthetic-body-secret"},
        )

    transport = Transport(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(TransportFailure) as caught:
            await transport.request(
                "POST",
                "/repos/owner/repo/issues/1/comments",
                "synthetic-auth-secret",
                body={"body": "plan"},
            )
        assert len(calls) == 1
        assert "synthetic" not in str(caught.value) + caught.value.evidence
        assert caught.value.request_id == "ABCD:1234"
        if status in (403, 429):
            assert caught.value.reason == "rate_limited" and caught.value.retry_after == 7
    finally:
        await transport.close()


@pytest.mark.parametrize(
    "payload", [b'{"id":1,"id":2}', b'{"value":NaN}', b'{"long":"' + b"x" * 100 + b'"}']
)
async def test_transport_refuses_ambiguous_or_oversized_provider_json(payload):
    transport = Transport(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
    )
    try:
        with pytest.raises(TransportFailure):
            await transport.request("GET", "/repositories/42", "synthetic", limit=32)
    finally:
        await transport.close()


@pytest.mark.parametrize(
    "api,git",
    [
        ("http://example.test", "http://example.test"),
        ("https://user:pass@example.test", "https://example.test"),
        ("https://example.test", "https://user:pass@other.test"),
        ("https://example.test?key=secret", "https://example.test"),
        ("https://example.test/../api", "https://example.test"),
    ],
)
def test_endpoints_require_explicit_https_without_credentials_or_path_rewriting(api, git):
    with pytest.raises(ValueError):
        Endpoint(api, git)
