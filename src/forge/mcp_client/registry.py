from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")

# Accept both underscore and hyphen variants, normalize to Agno convention.
_TRANSPORT_ALIASES: dict[str, str] = {
    "streamable_http": "streamable-http",
    "streamable-http": "streamable-http",
    "sse": "sse",
}


def _expand_env_vars(value: str) -> str:
    """Expand ``${VAR}`` references in *value* from the environment."""

    def _replace(match: re.Match) -> str:
        var = match.group(1)
        env_val = os.environ.get(var)
        if env_val is None:
            logger.warning("Environment variable '%s' not set", var)
            return match.group(0)  # leave unexpanded
        return env_val

    return _ENV_VAR_RE.sub(_replace, value)


@dataclass
class MCPServerConfig:
    """Configuration for a single external MCP server."""

    name: str
    url: str
    transport: str = "streamable-http"
    auth_type: str = "bearer"  # "bearer" | "none" | "header"
    auth_token_env: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    description: str = ""


class MCPRegistry:
    """Manages configured external MCP servers.

    Reads the ``mcp_servers`` section from :class:`ForgeConfig` and
    produces :class:`MCPServerConfig` instances for each entry.
    """

    def __init__(self, config: dict[str, dict]) -> None:
        self.servers: dict[str, MCPServerConfig] = {}
        self._load_from_config(config)

    def _load_from_config(self, config: dict[str, dict]) -> None:
        for name, raw in config.items():
            if not isinstance(raw, dict):
                logger.warning("Skipping invalid MCP server config: %s", name)
                continue

            url = raw.get("url")
            if not url:
                logger.warning("MCP server '%s' has no url — skipping", name)
                continue

            raw_transport = raw.get("transport", "streamable-http")
            transport = _TRANSPORT_ALIASES.get(raw_transport, raw_transport)
            if transport not in ("streamable-http", "sse"):
                logger.warning(
                    "MCP server '%s' has unsupported transport '%s' — skipping",
                    name,
                    raw_transport,
                )
                continue

            # Expand env vars in header values
            headers = {}
            for k, v in raw.get("headers", {}).items():
                headers[k] = _expand_env_vars(str(v))

            self.servers[name] = MCPServerConfig(
                name=name,
                url=url,
                transport=transport,
                auth_type=raw.get("auth_type", "bearer"),
                auth_token_env=raw.get("auth_token_env"),
                headers=headers,
                enabled=raw.get("enabled", True),
                description=raw.get("description", ""),
            )
            logger.info("Registered MCP server '%s' (%s)", name, url)

    def get(self, name: str) -> MCPServerConfig | None:
        return self.servers.get(name)

    def list_enabled(self) -> list[MCPServerConfig]:
        return [s for s in self.servers.values() if s.enabled]
