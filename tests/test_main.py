"""Tests for create_app wiring (see forge.main)."""

import logging

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from forge.config import Settings
from forge.main import create_app


def make_settings(**overrides) -> Settings:
    """Base settings for app-creation tests (no MCP key by default)."""
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test-token"),
        GITLAB_WEBHOOK_SECRET=SecretStr("test-secret"),
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        LITELLM_URL="http://litellm:4000",
        FORGE_MCP_KEY=None,
    )
    values.update(overrides)
    return Settings(**values)


def mcp_mounted(application) -> bool:
    return any(getattr(route, "path", None) == "/mcp" for route in application.routes)


class TestMCPServerMounting:
    async def test_no_key_mcp_not_mounted(self, caplog):
        """Fail closed: without FORGE_MCP_KEY the MCP endpoint is absent."""
        with caplog.at_level(logging.WARNING, logger="forge.main"):
            app = create_app(settings=make_settings())

        assert not mcp_mounted(app)
        assert not hasattr(app.state, "mcp_server")
        assert "MCP server disabled: FORGE_MCP_KEY not configured" in caplog.text

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/mcp")

        assert resp.status_code == 404

    async def test_enabled_with_key_mounted_and_requires_auth(self):
        """Enabled + key: mounted, and unauthenticated requests get 401."""
        app = create_app(
            settings=make_settings(
                FORGE_MCP_ENABLED=True,
                FORGE_MCP_KEY=SecretStr("test-mcp-key"),
            )
        )

        assert mcp_mounted(app)

        # The mount serves at "/mcp/" ("/mcp" itself is a 307 redirect).
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/mcp/")

        assert resp.status_code == 401

    async def test_disabled_with_key_mcp_not_mounted(self, caplog):
        """Explicit opt-out: FORGE_MCP_ENABLED=false keeps the mount off."""
        with caplog.at_level(logging.WARNING, logger="forge.main"):
            app = create_app(
                settings=make_settings(
                    FORGE_MCP_ENABLED=False,
                    FORGE_MCP_KEY=SecretStr("test-mcp-key"),
                )
            )

        assert not mcp_mounted(app)
        assert not hasattr(app.state, "mcp_server")
        assert "MCP server disabled: FORGE_MCP_KEY not configured" in caplog.text

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/mcp")

        assert resp.status_code == 404
