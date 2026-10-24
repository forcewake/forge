"""Native numeric repository authority resolves display names for each REST call."""

from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import unquote

from forge.port_values import RepoRef

from .auth import AppAuth
from .transport import MAX_JSON, Transport, TransportFailure


@dataclass(frozen=True)
class Reply:
    document: object
    request_id: str


class API:
    def __init__(self, auth: AppAuth, transport: Transport):
        self._auth, self._transport = auth, transport
        self.git_url = transport.endpoint.git_url

    async def _request(
        self,
        repo: RepoRef,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str | int] | None = None,
        write: bool = False,
        limit: int = MAX_JSON,
    ) -> Reply:
        for attempt in range(2):
            token = await self._auth.token(repo, write=write)
            try:
                document, request_id = await self._transport.request(
                    method,
                    path,
                    token.value,
                    body=body,
                    params=params,
                    limit=limit,
                )
                return Reply(document, request_id)
            except TransportFailure as error:
                if error.status != 401 or attempt:
                    raise
                self._auth.invalidate(repo, write=write)
        raise AssertionError("bounded credential retry exhausted")

    async def metadata(self, repo: RepoRef) -> Reply:
        try:
            reply = await self._request(
                repo, "GET", "/installation/repositories", params={"per_page": 1}
            )
        except TransportFailure as error:
            if error.status == 404:
                raise TransportFailure(
                    "repository_unavailable", status=404, request_id=error.request_id
                ) from None
            raise
        document = reply.document
        if (
            not isinstance(document, dict)
            or type(document.get("total_count")) is not int
            or document["total_count"] != 1
            or not isinstance(document.get("repositories"), list)
            or len(document["repositories"]) != 1
        ):
            raise TransportFailure("repository_identity_mismatch", request_id=reply.request_id)
        value = document["repositories"][0]
        if (
            not isinstance(value, dict)
            or type(value.get("id")) is not int
            or str(value["id"]) != repo.identity.repository_id
            or not isinstance(value.get("full_name"), str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["full_name"])
            or any(part in (".", "..") for part in value["full_name"].split("/"))
        ):
            raise TransportFailure("repository_identity_mismatch", request_id=reply.request_id)
        # Native metadata can contain a temporary clone credential. Retain only
        # the identity and policy fields consumed by these adapters.
        return Reply(
            {
                key: value[key]
                for key in ("id", "node_id", "full_name", "default_branch", "archived", "disabled")
                if key in value
            },
            reply.request_id,
        )

    async def request(
        self,
        repo: RepoRef,
        method: str,
        resource: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str | int] | None = None,
        write: bool = False,
        limit: int = MAX_JSON,
    ) -> Reply:
        decoded = unquote(resource, errors="strict")
        if (
            not resource
            or resource.startswith("/")
            or not re.fullmatch(r"[A-Za-z0-9_./%~-]+", resource)
            or re.search(r"%(?![0-9a-fA-F]{2})", resource)
            or any(char in decoded for char in ("%", "\\"))
            or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
            or any(part in ("", ".", "..") for part in decoded.split("/"))
        ):
            raise ValueError("GitHub resource must contain explicit confined path segments")
        if method not in ("GET", "POST", "PATCH", "PUT", "DELETE") or (
            method != "GET" and not write
        ):
            raise ValueError("GitHub mutations require explicit write authority")
        metadata = await self.metadata(repo)
        assert isinstance(metadata.document, dict)
        path = f"/repos/{metadata.document['full_name']}/{resource}"
        try:
            return await self._request(
                repo, method, path, body=body, params=params, write=write, limit=limit
            )
        except TransportFailure as error:
            if error.status == 404:
                raise TransportFailure(
                    "resource_not_found", status=404, request_id=error.request_id
                ) from None
            raise
