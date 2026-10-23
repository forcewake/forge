"""App credentials mint only explicit repository-scoped, least-privilege tokens."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import jwt

from forge.port_values import RepoRef
from forge.ports import Clock

from .transport import Transport, TransportFailure

READ = {
    "contents": "read",
    "issues": "read",
    "pull_requests": "read",
    "actions": "read",
    "checks": "read",
    "metadata": "read",
}
WRITE = {
    **READ,
    "contents": "write",
    "issues": "write",
    "pull_requests": "write",
    "actions": "write",
}


@dataclass(frozen=True)
class AccessToken:
    repository_id: str
    expires_at: datetime
    write: bool
    value: str = field(repr=False)


class AppAuth:
    def __init__(
        self,
        connection_id: str,
        issuer: str,
        private_key: str,
        installations: Mapping[str, int],
        clock: Clock,
        transport: Transport,
    ) -> None:
        if not connection_id or not issuer or not private_key or not installations:
            raise ValueError(
                "GitHub App credentials and explicit repository installations are required"
            )
        copied = dict(installations)
        if any(
            not key.isascii()
            or not key.isdecimal()
            or int(key) <= 0
            or str(int(key)) != key
            or type(value) is not int
            or value <= 0
            for key, value in copied.items()
        ):
            raise ValueError(
                "GitHub repository and installation IDs must be positive native integers"
            )
        self.connection_id, self._issuer, self._private_key = connection_id, issuer, private_key
        self._installations, self._clock, self._transport = copied, clock, transport
        self._tokens: dict[tuple[str, bool], AccessToken] = {}
        self._locks: dict[tuple[str, bool], asyncio.Lock] = {}

    def _identity(self, repo: RepoRef) -> str:
        identity = repo.identity
        if (
            identity.provider != "github"
            or identity.connection_id != self.connection_id
            or identity.repository_id not in self._installations
        ):
            raise TransportFailure("repository_not_authorized")
        return identity.repository_id

    async def token(self, repo: RepoRef, *, write: bool = False) -> AccessToken:
        native_id = self._identity(repo)
        cache_key = native_id, write
        async with self._locks.setdefault(cache_key, asyncio.Lock()):
            now = self._clock.now()
            if now.utcoffset() is None:
                raise ValueError("GitHub credential clock must be timezone aware")
            cached = self._tokens.get(cache_key)
            if cached is not None and cached.expires_at > now + timedelta(seconds=300):
                return cached
            stamp = int(now.timestamp())
            app_jwt = jwt.encode(
                {"iss": self._issuer, "iat": stamp - 60, "exp": stamp + 540},
                self._private_key,
                algorithm="RS256",
            )
            requested = dict(WRITE if write else READ)
            response, _ = await self._transport.request(
                "POST",
                f"/app/installations/{self._installations[native_id]}/access_tokens",
                app_jwt,
                body={"repository_ids": [int(native_id)], "permissions": requested},
                limit=1024 * 1024,
            )
            result = self._validate(response, native_id, write, requested, now)
            self._tokens[cache_key] = result
            return result

    @staticmethod
    def _validate(
        document: object, native_id: str, write: bool, requested: dict[str, str], now: datetime
    ) -> AccessToken:
        try:
            if not isinstance(document, dict):
                raise ValueError("missing token body")
            value, expiration = document.get("token"), document.get("expires_at")
            repositories, permissions = document.get("repositories"), document.get("permissions")
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 32768
                or any(ord(char) < 33 or ord(char) > 126 for char in value)
                or not isinstance(expiration, str)
                or not isinstance(repositories, list)
                or not isinstance(permissions, dict)
                or document.get("repository_selection") != "selected"
            ):
                raise ValueError("incomplete token scope")
            ids = [item.get("id") for item in repositories if isinstance(item, dict)]
            if (
                len(repositories) != 1
                or len(ids) != 1
                or type(ids[0]) is not int
                or ids[0] != int(native_id)
            ):
                raise ValueError("unexpected repository scope")
            if permissions != requested:
                raise ValueError("unexpected permission scope")
            expires = datetime.fromisoformat(expiration.replace("Z", "+00:00"))
            if expires.utcoffset() is None or not now + timedelta(
                seconds=300
            ) < expires <= now + timedelta(hours=1, seconds=60):
                raise ValueError("invalid token lifetime")
            return AccessToken(native_id, expires, write, value)
        except (ValueError, TypeError, OverflowError):
            raise TransportFailure("invalid_installation_token_scope") from None

    def invalidate(self, repo: RepoRef, *, write: bool = False) -> None:
        self._tokens.pop((self._identity(repo), write), None)
