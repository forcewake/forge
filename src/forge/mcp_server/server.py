from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from forge.gitlab.client import GitLabClient

if TYPE_CHECKING:
    from forge.config import Settings
    from forge.utils.redis_client import RedisManager

_CACHE_PREFIX = "mcp:project:"
_CACHE_TTL = 3600  # 1 hour

logger = logging.getLogger(__name__)


class MCPAuthMiddleware:
    """ASGI middleware that validates Bearer tokens on the MCP mount."""

    def __init__(self, app: ASGIApp, api_key: str) -> None:
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            auth = headers.get(b"authorization", b"").decode()
            if not auth.startswith("Bearer ") or auth[7:] != self.api_key:
                response = JSONResponse({"error": "Unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
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
    mcp = FastMCP(
        "Forge",
        stateless_http=True,
    )

    # Attach forge-specific state so tools/resources/prompts can access it.
    mcp._forge_settings = settings  # type: ignore[attr-defined]
    mcp._forge_redis = redis_manager  # type: ignore[attr-defined]

    # Register tools, resources, and prompts
    from forge.mcp_server.prompts import register_prompts
    from forge.mcp_server.resources import register_resources
    from forge.mcp_server.tools import register_tools

    register_tools(mcp)
    register_resources(mcp)
    register_prompts(mcp)

    return mcp
