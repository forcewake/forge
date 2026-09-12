from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Core settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # GitLab connection
    GITLAB_URL: str
    GITLAB_TOKEN: SecretStr
    GITLAB_WEBHOOK_SECRET: SecretStr
    FORGE_BOT_USERNAME: str = "forge-bot"

    # LiteLLM proxy
    LITELLM_URL: str = "http://litellm:4000"

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/forge.db"

    # Redis (optional)
    REDIS_URL: str | None = None

    # Agents
    FORGE_AGENTS_DIR: str = "agents"

    # Behaviour
    FORGE_MENTION_PATTERN: str = "@forge"
    LOG_LEVEL: str = "INFO"

    # MCP server (optional)
    FORGE_MCP_KEY: SecretStr | None = None

    # Agno telemetry (disabled for self-hosted)
    AGNO_TELEMETRY: bool = False


class ForgeConfig:
    """Optional YAML-based configuration loaded from forge.yml.

    Provides agent model aliases, default behaviours, labels, and rate limits.
    Falls back to sensible defaults when the file is missing.
    """

    _DEFAULTS: dict[str, Any] = {
        "version": "1",
        "bot_username": "forge-bot",
        "webhook_path": "/webhook",
        "port": 8420,
        "models": {
            "fast": "fast",
            "default": "strong",
            "strong": "strong",
            "code": "code",
        },
        "defaults": {
            "auto_review": True,
            "auto_pipeline_debug": True,
            "auto_security_triage": False,
            "mention_trigger": "@forge",
            "cooldown_seconds": 120,
            "max_diff_lines": 2000,
            "skip_draft_mrs": True,
        },
        "labels": {
            "reviewed": "ai-reviewed",
            "needs_changes": "ai-needs-changes",
            "security_critical": "security-critical",
        },
        "rate_limits": {
            "per_project_per_hour": 30,
            "per_user_per_hour": 20,
            "global_per_minute": 10,
        },
        "token_budgets": {
            "total": 24000,
            "diff": 12000,
            "per_file_diff": 4000,
            "pipeline_logs": 3000,
            "description": 2000,
            "previous_reviews": 3000,
        },
        "redaction": {
            "extra_patterns": [],
            "entropy_threshold": 4.5,
        },
        "mcp_servers": {},
    }

    def __init__(self, path: str | Path = "forge.yml") -> None:
        self._data: dict[str, Any] = dict(self._DEFAULTS)
        config_path = Path(path)
        if config_path.exists():
            with open(config_path) as f:
                raw = yaml.safe_load(f)
            if raw and isinstance(raw, dict):
                forge_section = raw.get("forge", raw)
                self._deep_merge(self._data, forge_section)

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> None:
        """Recursively merge *override* into *base* in place."""
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                ForgeConfig._deep_merge(base[key], value)
            else:
                base[key] = value

    # Convenience accessors
    @property
    def models(self) -> dict[str, str]:
        return self._data["models"]

    @property
    def defaults(self) -> dict[str, Any]:
        return self._data["defaults"]

    @property
    def labels(self) -> dict[str, str]:
        return self._data["labels"]

    @property
    def rate_limits(self) -> dict[str, int]:
        return self._data["rate_limits"]

    @property
    def token_budgets(self) -> dict[str, int]:
        return self._data["token_budgets"]

    @property
    def redaction(self) -> dict[str, Any]:
        return self._data["redaction"]

    @property
    def mcp_servers(self) -> dict[str, Any]:
        return self._data["mcp_servers"]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)


def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()  # type: ignore[call-arg]


def get_forge_config(path: str | Path = "forge.yml") -> ForgeConfig:
    """Return a ForgeConfig loaded from *path*."""
    return ForgeConfig(path)
