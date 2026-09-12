from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.mcp_client.manager import MCPConnectionManager, _build_headers
from forge.mcp_client.registry import MCPRegistry, MCPServerConfig


@dataclass
class FakeAgentDef:
    name: str = "test-agent"
    mcp_servers: list[str] = field(default_factory=list)


@dataclass
class FakeProjectConfig:
    mcp_servers: dict[str, list[str]] | None = None


class TestBuildHeaders:
    def test_bearer_auth(self, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "tok123")
        config = MCPServerConfig(
            name="test",
            url="http://test/mcp",
            auth_type="bearer",
            auth_token_env="MY_TOKEN",
        )
        headers = _build_headers(config)
        assert headers["Authorization"] == "Bearer tok123"

    def test_bearer_missing_env(self, monkeypatch):
        monkeypatch.delenv("MISSING_TOKEN", raising=False)
        config = MCPServerConfig(
            name="test",
            url="http://test/mcp",
            auth_type="bearer",
            auth_token_env="MISSING_TOKEN",
        )
        headers = _build_headers(config)
        assert "Authorization" not in headers

    def test_no_auth(self):
        config = MCPServerConfig(
            name="test",
            url="http://test/mcp",
            auth_type="none",
        )
        headers = _build_headers(config)
        assert headers == {}

    def test_header_auth_with_custom_headers(self):
        config = MCPServerConfig(
            name="test",
            url="http://test/mcp",
            auth_type="header",
            headers={"X-API-Key": "mykey"},
        )
        headers = _build_headers(config)
        assert headers["X-API-Key"] == "mykey"


class TestMCPConnectionManager:
    def _make_registry(self, servers: dict) -> MCPRegistry:
        return MCPRegistry(servers)

    @pytest.mark.asyncio
    async def test_get_tools_disabled_server(self):
        registry = self._make_registry(
            {
                "test": {"url": "http://test/mcp", "enabled": False},
            }
        )
        manager = MCPConnectionManager(registry)
        result = await manager.get_tools("test")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_tools_unknown_server(self):
        registry = self._make_registry({})
        manager = MCPConnectionManager(registry)
        result = await manager.get_tools("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_tools_connection_failure(self):
        registry = self._make_registry(
            {
                "bad": {"url": "http://unreachable:9999/mcp"},
            }
        )
        manager = MCPConnectionManager(registry)

        with patch("forge.mcp_client.manager.MCPTools") as MockMCPTools:
            mock_instance = AsyncMock()
            mock_instance.connect = AsyncMock(side_effect=ConnectionError("refused"))
            MockMCPTools.return_value = mock_instance

            result = await manager.get_tools("bad")
            assert result is None

    @pytest.mark.asyncio
    async def test_get_tools_success_and_caching(self):
        registry = self._make_registry(
            {
                "test": {"url": "http://test/mcp", "auth_type": "none"},
            }
        )
        manager = MCPConnectionManager(registry)

        with patch("forge.mcp_client.manager.MCPTools") as MockMCPTools:
            mock_instance = AsyncMock()
            mock_instance.connect = AsyncMock()
            mock_instance.is_alive = AsyncMock(return_value=True)
            mock_instance.functions = {"tool_a": MagicMock()}
            MockMCPTools.return_value = mock_instance

            # First call connects
            result1 = await manager.get_tools("test")
            assert result1 is mock_instance
            MockMCPTools.assert_called_once()

            # Second call reuses cached connection
            result2 = await manager.get_tools("test")
            assert result2 is mock_instance
            # MCPTools constructor NOT called again
            assert MockMCPTools.call_count == 1

    @pytest.mark.asyncio
    async def test_get_tools_for_agent_basic(self):
        registry = self._make_registry(
            {
                "jira": {"url": "http://jira/mcp", "auth_type": "none"},
                "slack": {"url": "http://slack/mcp", "auth_type": "none"},
            }
        )
        manager = MCPConnectionManager(registry)

        with patch("forge.mcp_client.manager.MCPTools") as MockMCPTools:
            mock_instance = AsyncMock()
            mock_instance.connect = AsyncMock()
            mock_instance.is_alive = AsyncMock(return_value=True)
            mock_instance.functions = {}
            MockMCPTools.return_value = mock_instance

            agent_def = FakeAgentDef(mcp_servers=["jira", "slack"])
            tools = await manager.get_tools_for_agent(agent_def)
            assert len(tools) == 2

    @pytest.mark.asyncio
    async def test_get_tools_for_agent_project_override(self):
        registry = self._make_registry(
            {
                "jira": {"url": "http://jira/mcp", "auth_type": "none"},
                "slack": {"url": "http://slack/mcp", "auth_type": "none"},
            }
        )
        manager = MCPConnectionManager(registry)

        with patch("forge.mcp_client.manager.MCPTools") as MockMCPTools:
            mock_instance = AsyncMock()
            mock_instance.connect = AsyncMock()
            mock_instance.is_alive = AsyncMock(return_value=True)
            mock_instance.functions = {}
            MockMCPTools.return_value = mock_instance

            agent_def = FakeAgentDef(name="code-reviewer", mcp_servers=["jira", "slack"])
            project_config = FakeProjectConfig(
                mcp_servers={"code-reviewer": ["jira"]},  # Override: only jira
            )
            tools = await manager.get_tools_for_agent(agent_def, project_config)
            assert len(tools) == 1

    @pytest.mark.asyncio
    async def test_close_all(self):
        registry = self._make_registry(
            {
                "test": {"url": "http://test/mcp", "auth_type": "none"},
            }
        )
        manager = MCPConnectionManager(registry)

        mock_conn = AsyncMock()
        mock_conn.close = AsyncMock()
        manager._connections["test"] = mock_conn

        await manager.close_all()
        mock_conn.close.assert_awaited_once()
        assert len(manager._connections) == 0

    @pytest.mark.asyncio
    async def test_stale_connection_reconnects(self):
        registry = self._make_registry(
            {
                "test": {"url": "http://test/mcp", "auth_type": "none"},
            }
        )
        manager = MCPConnectionManager(registry)

        # Put a stale connection in cache
        stale_conn = AsyncMock()
        stale_conn.is_alive = AsyncMock(return_value=False)
        stale_conn.close = AsyncMock()
        manager._connections["test"] = stale_conn

        with patch("forge.mcp_client.manager.MCPTools") as MockMCPTools:
            fresh_conn = AsyncMock()
            fresh_conn.connect = AsyncMock()
            fresh_conn.is_alive = AsyncMock(return_value=True)
            fresh_conn.functions = {"tool_a": MagicMock()}
            MockMCPTools.return_value = fresh_conn

            result = await manager.get_tools("test")
            assert result is fresh_conn
            stale_conn.close.assert_awaited_once()
