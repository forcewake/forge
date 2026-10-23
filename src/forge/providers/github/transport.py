"""Bounded same-origin HTTP; response errors never retain request credentials."""

from dataclasses import dataclass
import json
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from forge_protocol import canonical_json

API_VERSION = "2022-11-28"
MAX_JSON = 16 * 1024 * 1024


@dataclass(frozen=True)
class Endpoint:
    api_url: str = "https://api.github.com"
    graphql_url: str = "https://api.github.com/graphql"
    git_url: str = "https://github.com"

    def __post_init__(self) -> None:
        if any(
            "\\" in value or any(ord(char) < 33 for char in value)
            for value in (self.api_url, self.graphql_url, self.git_url)
        ):
            raise ValueError("GitHub endpoints cannot contain whitespace or backslashes")
        api, graphql = urlsplit(self.api_url), urlsplit(self.graphql_url)
        for url in (api, graphql, urlsplit(self.git_url)):
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or url.query
                or url.fragment
                or any(part in (".", "..") for part in url.path.split("/"))
            ):
                raise ValueError("GitHub endpoints require trusted credential-free HTTPS URLs")
        if (api.hostname, api.port) != (graphql.hostname, graphql.port):
            raise ValueError("GitHub REST and GraphQL must share the trusted origin")


class TransportFailure(Exception):
    def __init__(
        self,
        reason: str,
        *,
        status: int | None = None,
        request_id: str = "",
        retry_after: int | None = None,
    ):
        self.reason, self.status = reason, status
        self.request_id = request_id if re.fullmatch(r"[A-Fa-f0-9:]{1,128}", request_id) else ""
        self.retry_after = retry_after
        super().__init__(reason)

    @property
    def evidence(self) -> str:
        return canonical_json(
            {
                "reason": self.reason,
                "status": self.status,
                "request_id": self.request_id,
                "retry_after": self.retry_after,
            }
        ).decode()


class Transport:
    def __init__(
        self, endpoint: Endpoint = Endpoint(), *, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.endpoint = endpoint
        self._http = httpx.AsyncClient(
            transport=transport, timeout=30, follow_redirects=False, trust_env=False
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str | int] | None = None,
        graphql: bool = False,
        limit: int = MAX_JSON,
    ) -> tuple[object, str]:
        if (
            not path.startswith("/")
            or path.startswith("//")
            or "?" in path
            or "#" in path
            or "\\" in path
            or any(ord(char) < 33 for char in path)
            or any(part in (".", "..") for part in path.split("/"))
        ):
            raise ValueError("GitHub request path must be explicit and relative")
        if not 0 < limit <= MAX_JSON:
            raise ValueError("invalid response bound")
        url = self.endpoint.graphql_url if graphql else self.endpoint.api_url.rstrip("/") + path
        headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "Accept-Encoding": "identity",
        }
        try:
            async with self._http.stream(
                method, url, headers=headers, json=body, params=params
            ) as response:
                request_id = response.headers.get("x-github-request-id", "")
                if not 200 <= response.status_code < 300:
                    retry = response.headers.get("retry-after")
                    delay = (
                        min(int(retry), 3600)
                        if retry and len(retry) <= 10 and retry.isascii() and retry.isdecimal()
                        else None
                    )
                    limited = response.status_code == 429 or (
                        response.status_code == 403
                        and (
                            retry is not None
                            or response.headers.get("x-ratelimit-remaining") == "0"
                        )
                    )
                    raise TransportFailure(
                        "rate_limited" if limited else "http_error",
                        status=response.status_code,
                        request_id=request_id,
                        retry_after=delay,
                    )
                data = bytearray()
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise TransportFailure("response_encoding_refused", request_id=request_id)
                async for part in response.aiter_bytes():
                    if len(data) + len(part) > limit:
                        raise TransportFailure("response_too_large", request_id=request_id)
                    data.extend(part)
                if response.status_code == 204:
                    return None, request_id
                try:
                    return json.loads(
                        data, object_pairs_hook=_unique_object, parse_constant=_finite
                    ), request_id
                except (ValueError, RecursionError):
                    raise TransportFailure("malformed_response", request_id=request_id) from None
        except httpx.HTTPError:
            raise TransportFailure("transport_unavailable") from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate provider JSON key")
        result[key] = value
    return result


def _finite(value: str) -> None:
    raise ValueError("nonfinite provider JSON number")
