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


class GraphQLFailure(TransportFailure):
    def __init__(self, codes: tuple[str, ...], request_id: str):
        self.codes = codes
        super().__init__("graphql_error", request_id=request_id)


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
        graphql: bool = False,
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
                    graphql=graphql,
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
            reply = await self._request(repo, "GET", f"/repositories/{repo.identity.repository_id}")
        except TransportFailure as error:
            if error.status == 404:
                raise TransportFailure(
                    "repository_unavailable", status=404, request_id=error.request_id
                ) from None
            raise
        value = reply.document
        if (
            not isinstance(value, dict)
            or type(value.get("id")) is not int
            or str(value["id"]) != repo.identity.repository_id
            or not isinstance(value.get("full_name"), str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["full_name"])
            or any(part in (".", "..") for part in value["full_name"].split("/"))
        ):
            raise TransportFailure("repository_identity_mismatch", request_id=reply.request_id)
        return reply

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

    async def graphql(
        self,
        repo: RepoRef,
        query: str,
        variables: dict[str, Any],
        *,
        write: bool = False,
        limit: int = MAX_JSON,
    ) -> Reply:
        if not query or len(query) > 65536:
            raise ValueError("GitHub GraphQL document must be explicitly bounded")
        # Reading also requires an explicit query document: callers cannot disguise a
        # mutation behind a low-privilege API call or an omitted operation declaration.
        operation = re.match(r"\s*(query|mutation)\b", query)
        if operation is None or (operation[1] == "mutation" and not write):
            raise ValueError("GitHub GraphQL operation requires explicit authority")
        reply = await self._request(
            repo,
            "POST",
            "/graphql",
            body={"query": query, "variables": variables},
            write=write,
            graphql=True,
            limit=limit,
        )
        value = reply.document
        if not isinstance(value, dict):
            raise TransportFailure("malformed_graphql_response", request_id=reply.request_id)
        if value.get("errors"):
            errors = value["errors"]
            codes: set[str] = set()
            if isinstance(errors, list):
                for item in errors:
                    if isinstance(item, dict):
                        extensions = item.get("extensions")
                        code = extensions.get("code") if isinstance(extensions, dict) else None
                        kind = code if isinstance(code, str) else item.get("type")
                        if kind in (
                            "STALE_DATA",
                            "CONFLICT",
                            "FORBIDDEN",
                            "NOT_FOUND",
                            "RATE_LIMITED",
                        ):
                            codes.add(kind)
            raise GraphQLFailure(tuple(sorted(codes)), reply.request_id)
        if "data" not in value or not isinstance(value["data"], dict):
            raise TransportFailure("malformed_graphql_response", request_id=reply.request_id)
        return reply
