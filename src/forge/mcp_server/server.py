from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from forge.gitlab.client import GitLabClient
from forge.mcp_server.auth import (
    McpPrincipal,
    _token_label,
    apply_repo_allowlist,
    guard_fn,
    parse_scoped_tokens,
    parse_token_repos,
    resolve_principal,
    stash_principal,
    stash_session_factory,
)

if TYPE_CHECKING:
    from forge.config import Settings
    from forge.utils.redis_client import RedisManager

_CACHE_PREFIX = "mcp:project:"
_CACHE_TTL = 3600  # 1 hour

logger = logging.getLogger(__name__)


class MCPAuthMiddleware:
    """ASGI middleware that validates Bearer tokens on the MCP mount.

    Two token classes (ADR-0021 §4): the legacy master key (all scopes) and
    per-token scoped principals from ``FORGE_MCP_SCOPED_TOKENS``. A valid
    token's principal is stashed on the ASGI scope so tools can enforce
    scopes per call; an invalid token is a flat 401.
    """

    def __init__(
        self,
        app: ASGIApp,
        api_key: str,
        scoped_principals: dict[str, McpPrincipal] | None = None,
    ) -> None:
        self.app = app
        self.api_key = api_key
        self.scoped_principals = scoped_principals or {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            auth = headers.get(b"authorization", b"").decode()
            token = auth[7:] if auth.startswith("Bearer ") else ""
            principal = resolve_principal(token, self.api_key, self.scoped_principals)
            if principal is None:
                response = JSONResponse({"error": "Unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
            stash_principal(scope, principal)
            stash_session_factory(scope)
        await self.app(scope, receive, send)


async def resolve_project_id(
    gitlab: GitLabClient,
    project_ref: str,
    redis: RedisManager | None = None,
) -> int:
    """Resolve a project path (e.g. ``group/project``) to a numeric ID.

    Numeric strings are returned directly.  Path lookups are cached in Redis
    for one hour when available.
    """
    if project_ref.isdigit():
        return int(project_ref)

    if redis is not None:
        cached = await redis.get(f"{_CACHE_PREFIX}{project_ref}")
        if cached is not None:
            return int(cached)

    project = await gitlab.get_project(project_ref)

    if redis is not None:
        await redis.set_ex(f"{_CACHE_PREFIX}{project_ref}", str(project.id), ex=_CACHE_TTL)

    return project.id


@asynccontextmanager
async def gitlab_client_from(settings: Settings) -> AsyncIterator[GitLabClient]:
    """Create a short-lived GitLab client from settings."""
    async with GitLabClient(
        base_url=settings.GITLAB_URL,
        token=settings.GITLAB_TOKEN.get_secret_value(),
    ) as client:
        yield client


def create_mcp_server(settings: Settings, redis_manager: RedisManager | None = None) -> FastMCP:
    """Create and configure the FastMCP server instance."""
    # Behind a proxy the Host header is the public domain, not localhost —
    # list it or the SDK's DNS-rebinding protection answers 421 to every
    # proxied request (config: FORGE_MCP_ALLOWED_HOSTS).
    allowed_hosts = [h.strip() for h in settings.FORGE_MCP_ALLOWED_HOSTS.split(",") if h.strip()]
    transport_security = None
    if allowed_hosts:
        from mcp.server.transport_security import TransportSecuritySettings

        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=[f"https://{h}" for h in allowed_hosts],
        )
    mcp = FastMCP(
        "Forge",
        stateless_http=True,
        transport_security=transport_security,
    )

    # Attach forge-specific state so tools/resources/prompts can access it.
    mcp._forge_settings = settings  # type: ignore[attr-defined]
    mcp._forge_redis = redis_manager  # type: ignore[attr-defined]

    # Register tools, resources, and prompts
    from forge.mcp_server.prompts import register_prompts
    from forge.mcp_server.resources import register_resources
    from forge.mcp_server.tools import register_tools
    from forge.mcp_server.tools_runs import register_run_tools

    register_tools(mcp)
    register_run_tools(mcp)
    register_resources(mcp)
    register_prompts(mcp)

    # R19: the GitLab read surfaces (resources/prompts) go behind the same
    # default-deny guard as tools — they resolve a caller-supplied project
    # through the shared platform token, so scope + repo-target checks
    # apply here too.
    _guard_gitlab_read_surfaces(mcp)

    return mcp


def _guard_gitlab_read_surfaces(mcp: FastMCP) -> None:
    """Wrap resource templates and prompts with the ``forge:read`` guard.

    The fn swap is invisible to the SDK — name, description and parameter
    schema were captured at registration; only the call path changes. These
    are reads, so the guard audits allowed calls like every other surface.
    """
    for template in mcp._resource_manager._templates.values():  # type: ignore[attr-defined]
        template.fn = guard_fn(
            template.fn,
            name=f"resource:{template.name}",
            scope_needed="forge:read",
            repo_arg="project_path",
        )
    for prompt in mcp._prompt_manager._prompts.values():  # type: ignore[attr-defined]
        prompt.fn = guard_fn(
            prompt.fn,
            name=f"prompt:{prompt.name}",
            scope_needed="forge:read",
            repo_arg="project_path",
        )


def scoped_principals_from_settings(settings: Settings) -> dict[str, McpPrincipal]:
    """Parse the scoped-token config (fail closed on a malformed config).

    R19: the optional ``FORGE_MCP_TOKEN_REPOS`` allowlist (token or its
    audit label → repo globs) is merged onto the resolved principals.
    Tokens it does not mention stay unrestricted — existing configs keep
    working. Keys matching no scoped token are logged and ignored: the
    FORGE_MCP_KEY master is unrestricted by design.
    """
    raw = settings.FORGE_MCP_SCOPED_TOKENS
    principals = parse_scoped_tokens(raw.get_secret_value() if raw is not None else None)
    repos_raw = settings.FORGE_MCP_TOKEN_REPOS
    token_repos = parse_token_repos(repos_raw.get_secret_value() if repos_raw is not None else None)
    if token_repos:
        known = set(principals) | {_token_label(token) for token in principals}
        orphans = sorted(set(token_repos) - known)
        if orphans:
            logger.warning(
                "FORGE_MCP_TOKEN_REPOS entries match no scoped token and are ignored: %s "
                "(FORGE_MCP_KEY master stays unrestricted by design)",
                ", ".join(orphans),
            )
        principals = apply_repo_allowlist(principals, token_repos)
    return principals
