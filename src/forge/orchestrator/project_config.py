from __future__ import annotations

import base64
import logging
import time
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from forge.gitlab.client import GitLabClient

logger = logging.getLogger(__name__)

_CACHE_TTL = 300  # 5 minutes
_cache: dict[int, tuple[ProjectConfig, float]] = {}


class ProjectConfig(BaseModel):
    """Per-project configuration loaded from .forge.yml in the repository."""

    enabled_agents: list[str] | None = Field(
        default=None,
        description="If set, only these agents are allowed. None means all.",
    )
    disabled_agents: list[str] = Field(
        default_factory=list,
        description="Agents to disable for this project.",
    )
    review_rules: list[str] = Field(
        default_factory=list,
        description="Project-specific review instructions appended to prompts.",
    )
    skip_paths: list[str] = Field(
        default_factory=list,
        description="Glob patterns for files to exclude from review.",
    )
    mcp_servers: dict[str, list[str]] | None = Field(
        default=None,
        description="Per-agent MCP server overrides. Keys are agent names, values are lists of server names.",
    )


def _default_config() -> ProjectConfig:
    return ProjectConfig()


async def load_project_config(
    client: GitLabClient,
    project_id: int,
    ref: str = "HEAD",
) -> ProjectConfig:
    """Load .forge.yml from a project's repo. Return defaults if not found.

    Results are cached per project with a 5-minute TTL.
    """
    now = time.monotonic()
    cached = _cache.get(project_id)
    if cached is not None:
        config, ts = cached
        if now - ts < _CACHE_TTL:
            return config

    try:
        repo_file = await client.get_file(project_id, ".forge.yml", ref)
        content = repo_file.content
        if repo_file.encoding == "base64":
            content = base64.b64decode(content).decode("utf-8")

        data = yaml.safe_load(content)
        if not isinstance(data, dict):
            logger.warning("Invalid .forge.yml in project %d: expected mapping", project_id)
            config = _default_config()
        else:
            # Support both top-level and nested under "forge" key
            forge_data = data.get("forge", data)
            # Parse per-agent MCP server overrides
            mcp_raw = forge_data.get("mcp_servers")
            mcp_servers = None
            if isinstance(mcp_raw, dict):
                mcp_servers = {}
                for agent_name, agent_cfg in mcp_raw.items():
                    if isinstance(agent_cfg, dict):
                        mcp_servers[agent_name] = agent_cfg.get("mcp_servers", [])
                    elif isinstance(agent_cfg, list):
                        mcp_servers[agent_name] = agent_cfg

            config = ProjectConfig(
                enabled_agents=forge_data.get("enabled_agents"),
                disabled_agents=forge_data.get("disabled_agents", []),
                review_rules=forge_data.get("review_rules", []),
                skip_paths=forge_data.get("skip_paths", []),
                mcp_servers=mcp_servers,
            )
        logger.debug("Loaded .forge.yml for project %d", project_id)
    except Exception:
        # 404 or any other error — use defaults
        logger.debug("No .forge.yml found for project %d, using defaults", project_id)
        config = _default_config()

    _cache[project_id] = (config, now)
    return config


def clear_cache() -> None:
    """Clear the project config cache (useful for testing)."""
    _cache.clear()
