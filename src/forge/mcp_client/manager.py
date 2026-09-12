from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from agno.tools.mcp import MCPTools

from forge.mcp_client.registry import MCPRegistry, MCPServerConfig

if TYPE_CHECKING:
    from forge.agents.registry import AgentDefinition
    from forge.orchestrator.project_config import ProjectConfig

logger = logging.getLogger(__name__)


def _build_headers(config: MCPServerConfig) -> dict[str, str]:
    """Build HTTP headers for an MCP server connection."""
    headers: dict[str, str] = dict(config.headers)

    if config.auth_type == "bearer" and config.auth_token_env:
        token = os.environ.get(config.auth_token_env)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            logger.warning(
                "MCP server '%s': env var '%s' not set — connecting without auth",
                config.name,
                config.auth_token_env,
            )
    # auth_type "header" — custom headers already in config.headers
    # auth_type "none" — no auth needed

    return headers


class MCPConnectionManager:
    """Manages connections to external MCP servers.

    Connections are created lazily on first use and cached for reuse.
    """

    def __init__(self, registry: MCPRegistry) -> None:
        self.registry = registry
        self._connections: dict[str, MCPTools] = {}

    async def get_tools(self, server_name: str) -> MCPTools | None:
        """Get an Agno MCPTools instance for a server.

        Creates connection on first use, caches for reuse.
        Returns None if server is not configured or unavailable.
        """
        if server_name in self._connections:
            # Check if cached connection is still alive
            cached = self._connections[server_name]
            if await cached.is_alive():
                return cached
            # Stale — remove and reconnect
            logger.info("MCP connection '%s' is stale — reconnecting", server_name)
            await self._close_one(server_name)

        config = self.registry.get(server_name)
        if not config or not config.enabled:
            return None

        try:
            headers = _build_headers(config)
            mcp_tools = MCPTools(
                url=config.url,
                transport=config.transport,
                headers=headers,
                tool_name_prefix=config.name,
            )
            await mcp_tools.connect()
            self._connections[server_name] = mcp_tools
            logger.info(
                "Connected to MCP server '%s' — %d tool(s) available",
                server_name,
                len(mcp_tools.functions),
            )
            return mcp_tools
        except Exception as e:
            logger.warning(
                "MCP connection to '%s' failed: %s",
                server_name,
                e,
            )
            return None

    async def get_tools_for_agent(
        self,
        agent_def: AgentDefinition,
        project_config: ProjectConfig | None = None,
    ) -> list[MCPTools]:
        """Get all MCP tool sets configured for an agent.

        Respects per-project overrides: if ``project_config`` specifies
        ``mcp_servers`` for this agent, only those servers are used.
        """
        server_names = list(agent_def.mcp_servers)

        # Apply project-level override if present
        if project_config and project_config.mcp_servers:
            agent_override = project_config.mcp_servers.get(agent_def.name)
            if agent_override is not None:
                # Override completely replaces the agent-level list
                server_names = agent_override

        tools: list[MCPTools] = []
        for server_name in server_names:
            mcp = await self.get_tools(server_name)
            if mcp:
                tools.append(mcp)
        return tools

    async def health_check(self) -> dict[str, bool]:
        """Check connectivity to all configured servers."""
        result: dict[str, bool] = {}
        for config in self.registry.list_enabled():
            try:
                mcp = await self.get_tools(config.name)
                result[config.name] = mcp is not None and await mcp.is_alive()
            except Exception:
                result[config.name] = False
        return result

    async def _close_one(self, name: str) -> None:
        conn = self._connections.pop(name, None)
        if conn:
            try:
                await conn.close()
            except Exception:
                logger.debug("Error closing MCP connection '%s'", name, exc_info=True)

    async def close_all(self) -> None:
        """Close all MCP connections."""
        for name in list(self._connections):
            await self._close_one(name)
