"""Application wiring keeps operator reads while retiring the MCP mount."""

from httpx import ASGITransport, AsyncClient
import pytest
from forge.config import Settings
from forge.main import create_app


@pytest.mark.parametrize(
    "legacy",
    [{}, {"FORGE_MCP_ENABLED": True, "FORGE_MCP_KEY": "stale-key"}, {"FORGE_MCP_ENABLED": False}],
)
async def test_removed_mcp_cannot_be_reenabled_by_old_settings(legacy):
    app = create_app(Settings(_env_file=None, FORGE_GITLAB_ENABLED=False, **legacy))
    assert not hasattr(app.state, "mcp_server")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://forge.test"
    ) as client:
        for path in ("/mcp", "/mcp/", "/mcp/mcp"):
            assert (await client.post(path, json={})).status_code == 404
    assert any(getattr(route, "path", "") == "/operator/runs" for route in app.routes)
