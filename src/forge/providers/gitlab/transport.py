"""Bounded HTTPS HTTP with no redirects, ambient credentials or mutation retries."""

from dataclasses import dataclass
import json
from typing import Any
from urllib.parse import urlsplit

import httpx

from forge.providers.gitlab.values import MAX_BYTES, evidence


@dataclass(frozen=True)
class Endpoint:
    url: str

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "%" in parsed.path
            or "\\" in self.url
            or any(ord(c) < 33 or ord(c) == 127 for c in self.url)
            or any(part in (".", "..") for part in parsed.path.split("/"))
        ):
            raise ValueError("GitLab connection requires an explicit trusted HTTPS URL")

    @property
    def api_url(self) -> str:
        return self.url.rstrip("/") + "/api/v4"


class Failure(Exception):
    def __init__(self, reason: str, *, status: int | None = None, request_id: str = "") -> None:
        self.reason, self.status, self.request_id = reason, status, request_id
        self.evidence = evidence("http", request_id, reason=reason)
        super().__init__(reason)


@dataclass(frozen=True)
class Reply:
    document: object
    request_id: str
    next_page: int | None = None


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate response key")
        result[key] = value
    return result


class Transport:
    def __init__(
        self, endpoint: Endpoint, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.endpoint = endpoint
        self._http = httpx.AsyncClient(
            transport=transport, timeout=30, follow_redirects=False, trust_env=False
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def request(
        self,
        method: str,
        resource: str,
        token: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str | int] | None = None,
        limit: int = MAX_BYTES,
    ) -> Reply:
        if (
            not resource.startswith("/")
            or resource.startswith("//")
            or any(c in resource for c in "?#\\")
            or any(ord(c) < 33 or ord(c) == 127 for c in resource)
            or any(part in (".", "..") for part in resource.split("/"))
            or not 0 < limit <= MAX_BYTES
            or not token
            or any(ord(c) < 33 or ord(c) == 127 for c in token)
        ):
            raise ValueError("confined resource, bounded response and explicit credential required")
        try:
            async with self._http.stream(
                method,
                self.endpoint.api_url + resource,
                headers={
                    "PRIVATE-TOKEN": token,
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
                json=body,
                params=params,
            ) as response:
                request_id = response.headers.get("x-request-id", "")
                if token in request_id:
                    request_id = ""
                if not 200 <= response.status_code < 300:
                    raise Failure(
                        "rate_limited" if response.status_code == 429 else "http_unavailable",
                        status=response.status_code,
                        request_id=request_id,
                    )
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise Failure("encoded_response_refused", request_id=request_id)
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > limit:
                        raise Failure("response_too_large", request_id=request_id)
                next_value = response.headers.get("x-next-page", "")
                if next_value and (
                    not next_value.isascii()
                    or not next_value.isdecimal()
                    or len(next_value) > 6
                    or int(next_value) <= 1
                ):
                    raise Failure("invalid_pagination", request_id=request_id)
                if not data:
                    return Reply(None, request_id, int(next_value) if next_value else None)
                try:
                    document: object = json.loads(
                        data,
                        object_pairs_hook=_pairs,
                        parse_constant=lambda _: (_ for _ in ()).throw(
                            ValueError("nonfinite response")
                        ),
                    )
                except (ValueError, UnicodeError, RecursionError):
                    raise Failure("invalid_json", request_id=request_id) from None
                return Reply(document, request_id, int(next_value) if next_value else None)
        except httpx.HTTPError:
            raise Failure("transport_unavailable") from None
