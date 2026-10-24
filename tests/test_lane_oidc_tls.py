"""Configured native CA trust reaches real JWKS HTTPS and stays connection-scoped."""

import json

from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
from jwt.algorithms import RSAAlgorithm
from pydantic import SecretStr
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from forge.api.lane_auth_values import LaneAuthError
from forge.api.lane_jwks import JwksCache
from forge.runtime.assembly import database, lane_auth
from tests import runtime_fixture


def owned_keys(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "GET" and request.url.path == "/keys"
        return httpx.Response(200, json={"keys": [{**jwk, "kid": "owned", "alg": "RS256"}]})

    monkeypatch.setattr(runtime_fixture.native, "Wire", lambda: handle)
    return key.public_key(), requests


async def test_native_connection_ca_reaches_actual_https_jwks(tmp_path, monkeypatch):
    expected, requests = owned_keys(monkeypatch)
    with runtime_fixture.native_server(tmp_path) as (_, url, ca):
        config, values = runtime_fixture.configuration(tmp_path, native_url=url, ca=ca)
        engine = database(values["SYNTHETIC_DATABASE_URL"])
        try:
            auth = lane_auth(
                config,
                SecretStr("owned-signing-value-at-least32-bytes"),
                async_sessionmaker(engine),
            )
            assert auth.authorities["ce-1"].tls_ca_path == ca
            key = await auth.jwks.key("ce-1", "owned")
            assert key.public_numbers() == expected.public_numbers()
            assert len(requests) == 1
        finally:
            await engine.dispose()


async def test_private_ca_never_leaks_to_another_connection_at_the_same_issuer(
    tmp_path, monkeypatch
):
    expected, requests = owned_keys(monkeypatch)
    with runtime_fixture.native_server(tmp_path) as (_, url, ca):
        cache = JwksCache(
            {"trusted": (url, url + "/keys"), "other": (url, url + "/keys")},
            ca_paths={"trusted": ca},
        )
        assert (await cache.key("trusted", "owned")).public_numbers() == expected.public_numbers()
        with pytest.raises(LaneAuthError, match="could not be loaded"):
            await cache.key("other", "owned")
        assert len(requests) == 1
