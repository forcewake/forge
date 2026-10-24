"""Bounded public-key cache for explicitly configured OIDC authorities."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import ssl
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from forge.api.lane_auth_values import LaneAuthError


def trusted_https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("lane authentication URLs must be configured HTTPS URLs")
    return value.rstrip("/")


def ca_context(path: Path) -> ssl.SSLContext:
    if not path.is_absolute():
        raise ValueError("OIDC CA path must be an explicit absolute path")
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=str(path))
    return context


class JwksCache:
    """No token-selected issuer, key URL, redirects, stale keys or unbounded bodies."""

    def __init__(
        self,
        authorities: Mapping[str, tuple[str, str]],
        *,
        ttl: int = 300,
        transport: httpx.AsyncBaseTransport | None = None,
        ca_paths: Mapping[str, Path] | None = None,
    ):
        if not 10 <= ttl <= 3600 or not 1 <= len(authorities) <= 64:
            raise ValueError("invalid OIDC cache configuration")
        self.authorities = {
            provider: (trusted_https_url(issuer), trusted_https_url(url))
            for provider, (issuer, url) in authorities.items()
        }
        self.ca_paths = dict(ca_paths or {})
        if set(self.ca_paths) - set(self.authorities):
            raise ValueError("OIDC CA trust must belong to a configured connection")
        self._contexts = {key: ca_context(path) for key, path in self.ca_paths.items()}
        self.ttl = ttl
        self.transport = transport
        self._entries: dict[str, tuple[float, dict[str, RSAPublicKey]]] = {}
        self._refresh_at: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def key(self, provider: str, kid: str) -> RSAPublicKey:
        if provider not in self.authorities:
            raise LaneAuthError("OIDC provider is not configured")
        async with self._lock:
            now = time.monotonic()
            entry = self._entries.get(provider)
            if entry and now - entry[0] < self.ttl and kid in entry[1]:
                return entry[1][kid]
            # Unknown key IDs cannot force one outbound fetch per attacker token.
            if now - self._refresh_at.get(provider, float("-inf")) < 30:
                raise LaneAuthError("OIDC signing key is not available")
            self._refresh_at[provider] = now
            keys = await self._fetch(
                self.authorities[provider][1], verify=self._contexts.get(provider, True)
            )
            self._entries[provider] = (now, keys)
            if kid not in keys:
                raise LaneAuthError("OIDC signing key is not available")
            return keys[kid]

    async def _fetch(self, url: str, *, verify: ssl.SSLContext | bool) -> dict[str, RSAPublicKey]:
        try:
            async with (
                httpx.AsyncClient(
                    transport=self.transport,
                    verify=verify,
                    timeout=httpx.Timeout(5, connect=2),
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream("GET", url) as response,
            ):
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 131072:
                        raise LaneAuthError("OIDC key response is too large")
            document = json.loads(body)
            values = document["keys"]
            if not isinstance(values, list) or not 1 <= len(values) <= 32:
                raise LaneAuthError("OIDC key response has invalid key count")
            keys: dict[str, RSAPublicKey] = {}
            for value in values:
                kid, key = self._public_key(value)
                if kid in keys:
                    raise LaneAuthError("OIDC key response has duplicate key ids")
                keys[kid] = key
            return keys
        except (httpx.HTTPError, ValueError, KeyError, TypeError, jwt.PyJWTError):
            raise LaneAuthError("OIDC signing keys could not be loaded") from None

    @staticmethod
    def _public_key(value: dict[str, Any]) -> tuple[str, RSAPublicKey]:
        if (
            not isinstance(value, dict)
            or value.get("kty") != "RSA"
            or value.get("alg", "RS256") != "RS256"
            or value.get("use", "sig") != "sig"
            or value.get("key_ops", ["verify"]) != ["verify"]
            or any(name in value for name in ("d", "p", "q", "dp", "dq", "qi", "oth"))
            or not isinstance(value.get("kid"), str)
            or not 1 <= len(value["kid"]) <= 256
        ):
            raise LaneAuthError("OIDC key response contains an unsupported key")
        key = jwt.PyJWK.from_dict(value, algorithm="RS256").key
        if not isinstance(key, RSAPublicKey) or key.key_size < 2048:
            raise LaneAuthError("OIDC signing key must be RSA with at least 2048 bits")
        return value["kid"], key
