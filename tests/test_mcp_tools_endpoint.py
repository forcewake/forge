from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_mcp_tools_no_servers(app, client):
    """With no MCP servers configured, returns an empty list."""
    resp = await client.get("/mcp-tools")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_mcp_tools_with_configured_servers(app):
    """With MCP servers configured, returns server info."""
    from forge.mcp_client.registry import MCPRegistry, MCPServerConfig

    # Inject a mock registry and manager
    registry = MCPRegistry({})
    registry.servers["jira"] = MCPServerConfig(
        name="jira",
        url="http://jira-mcp:8080/mcp",
        description="Jira issues",
    )
    app.state.mcp_registry = registry

    mock_manager = AsyncMock()
    mock_mcp = MagicMock()
    mock_mcp.functions = {"jira_list_issues": MagicMock(), "jira_get_issue": MagicMock()}
    mock_manager.get_tools = AsyncMock(return_value=mock_mcp)
    app.state.mcp_manager = mock_manager

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/mcp-tools")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["server"] == "jira"
    assert data[0]["status"] == "connected"
    assert set(data[0]["tools"]) == {"jira_list_issues", "jira_get_issue"}


@pytest.mark.asyncio
async def test_mcp_tools_unavailable_server(app):
    """Server that fails to connect shows unavailable status."""
    from forge.mcp_client.registry import MCPRegistry, MCPServerConfig

    registry = MCPRegistry({})
    registry.servers["bad"] = MCPServerConfig(
        name="bad",
        url="http://bad:9999/mcp",
        description="Down",
    )
    app.state.mcp_registry = registry

    mock_manager = AsyncMock()
    mock_manager.get_tools = AsyncMock(return_value=None)
    app.state.mcp_manager = mock_manager

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/mcp-tools")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["status"] == "unavailable"
