"""Every project request is scoped to configured native identity and credential."""

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import unquote

from pydantic import SecretStr

from forge.port_values import Actor, ReadUnavailable, RepoRef
from forge.providers.gitlab.transport import Failure, Reply, Transport
from forge.providers.gitlab.values import (
    MAX_BYTES,
    array,
    native_id,
    numeric,
    obj,
    text,
    unavailable,
)


@dataclass(frozen=True)
class Credentials:
    read_token: SecretStr
    write_token: SecretStr
    bot: Actor

    def __post_init__(self) -> None:
        if (
            not self.read_token.get_secret_value()
            or not self.write_token.get_secret_value()
            or not self.bot.is_bot
            or self.bot.provider != "gitlab"
        ):
            raise ValueError("explicit read/write project access tokens and native bot required")


class API:
    def __init__(
        self, connection_id: str, transport: Transport, credentials: Mapping[str, Credentials]
    ) -> None:
        if not connection_id or any(
            numeric(key) != key or value.bot.connection_id != connection_id
            for key, value in credentials.items()
        ):
            raise ValueError("project credentials must belong to one explicit connection")
        self.connection_id, self.transport, self._credentials = (
            connection_id,
            transport,
            dict(credentials),
        )

    def credentials(self, repo: RepoRef) -> Credentials:
        if (
            repo.identity.provider != "gitlab"
            or repo.identity.connection_id != self.connection_id
            or repo.identity.repository_id not in self._credentials
        ):
            raise unavailable("project_scope_refused")
        return self._credentials[repo.identity.repository_id]

    async def write_token(self, repo: RepoRef) -> str:
        credential = self.credentials(repo)
        token = credential.write_token.get_secret_value()
        row = obj((await self.transport.request("GET", "/user", token)).document)
        if (
            native_id(row.get("id")) != credential.bot.native_id
            or row.get("bot") is not True
            or text(row.get("username")) != credential.bot.login
            or not credential.bot.login.startswith(f"project_{repo.identity.repository_id}_bot_")
        ):
            raise unavailable("write_credential_not_project_bot")
        return token

    async def metadata(self, repo: RepoRef) -> Reply:
        credential = self.credentials(repo)
        try:
            reply = await self.transport.request(
                "GET",
                "/projects/" + repo.identity.repository_id,
                credential.read_token.get_secret_value(),
            )
        except Failure as error:
            raise Failure(
                "project_unavailable", status=error.status, request_id=error.request_id
            ) from None
        row = obj(reply.document)
        full_name = text(row.get("path_with_namespace"))
        if (
            native_id(row.get("id")) != repo.identity.repository_id
            or re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", full_name) is None
            or any(part in (".", "..") for part in full_name.split("/"))
        ):
            raise unavailable("project_identity_mismatch")
        return Reply(
            {
                key: row[key]
                for key in (
                    "id",
                    "path_with_namespace",
                    "default_branch",
                    "archived",
                    "ci_config_path",
                    "http_url_to_repo",
                )
                if key in row
            },
            reply.request_id,
            reply.next_page,
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
        limit: int = MAX_BYTES,
    ) -> Reply:
        decoded = unquote(resource, errors="strict")
        if (
            not resource
            or resource.startswith("/")
            or re.fullmatch(r"[A-Za-z0-9_./%~-]+", resource) is None
            or re.search(r"%(?![A-Fa-f0-9]{2})", resource)
            or any(c in decoded for c in "%\\?#")
            or any(ord(c) < 32 or ord(c) == 127 for c in decoded)
            or any(part in ("", ".", "..") for part in decoded.split("/"))
            or method not in ("GET", "POST", "PUT", "PATCH", "DELETE")
            or (method != "GET" and not write)
        ):
            raise ValueError(
                "project resources require confined segments and explicit write authority"
            )
        await self.metadata(repo)
        token = (
            await self.write_token(repo)
            if write
            else self.credentials(repo).read_token.get_secret_value()
        )
        try:
            return await self.transport.request(
                method,
                "/projects/" + repo.identity.repository_id + "/" + resource,
                token,
                body=body,
                params=params,
                limit=limit,
            )
        except Failure as error:
            if error.status == 404:
                raise Failure(
                    "resource_not_found", status=404, request_id=error.request_id
                ) from None
            raise

    async def read(
        self, repo: RepoRef, resource: str, *, params: dict[str, str | int] | None = None
    ) -> Reply:
        try:
            return await self.request(repo, "GET", resource, params=params)
        except Failure as error:
            raise ReadUnavailable("gitlab_read_unavailable", error.evidence) from None
        except ValueError:
            raise unavailable("unsupported_read_resource") from None

    async def pages(
        self,
        repo: RepoRef,
        resource: str,
        *,
        params: dict[str, str | int] | None = None,
        max_pages: int = 10,
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for page in range(1, max_pages + 1):
            reply = await self.read(
                repo, resource, params={**(params or {}), "per_page": 100, "page": page}
            )
            values = array(reply.document)
            if len(values) > 100 or (reply.next_page is not None and reply.next_page != page + 1):
                raise unavailable("invalid_page_boundary")
            rows.extend(obj(value) for value in values)
            if len(values) < 100 and reply.next_page is None:
                return rows
        raise unavailable("scan_truncated")
