from unittest.mock import MagicMock

from forge.agents.base import ForgeAgent
from forge.agents.registry import AgentDefinition


class TestBuildToolsWithMCP:
    """Verify that MCP tools are included in _build_tools() output."""

    def _make_agent(self, mcp_tools=None):
        definition = AgentDefinition(
            name="test-agent",
            type="generic",
            actions={},
        )
        # Create minimal mocks for required args
        context = MagicMock()
        context.mr = None  # No MR → no GitLabToolkit
        model = MagicMock()
        project_config = MagicMock()
        gitlab = MagicMock()

        return ForgeAgent(
            definition=definition,
            model=model,
            context=context,
            project_config=project_config,
            gitlab=gitlab,
            mcp_tools=mcp_tools,
        )

    def test_no_mcp_tools(self):
        agent = self._make_agent(mcp_tools=None)
        tools = agent._build_tools()
        assert tools == []

    def test_empty_mcp_tools(self):
        agent = self._make_agent(mcp_tools=[])
        tools = agent._build_tools()
        assert tools == []

    def test_mcp_tools_appended(self):
        mock_mcp1 = MagicMock()
        mock_mcp2 = MagicMock()
        agent = self._make_agent(mcp_tools=[mock_mcp1, mock_mcp2])
        tools = agent._build_tools()
        assert mock_mcp1 in tools
        assert mock_mcp2 in tools
        assert len(tools) == 2

    def test_mcp_tools_alongside_gitlab_toolkit(self):
        """When MR context exists and inline_comments enabled, both toolkits present."""
        definition = AgentDefinition(
            name="test-agent",
            type="code-reviewer",
            actions={"inline_comments": True},
        )
        context = MagicMock()
        context.mr = MagicMock()
        context.mr.iid = 42
        context.mr.diff_refs = {"base_sha": "a", "head_sha": "b", "start_sha": "c"}
        context.project_id = 1
        model = MagicMock()
        project_config = MagicMock()
        gitlab = MagicMock()

        mock_mcp = MagicMock()
        agent = ForgeAgent(
            definition=definition,
            model=model,
            context=context,
            project_config=project_config,
            gitlab=gitlab,
            mcp_tools=[mock_mcp],
        )

        tools = agent._build_tools()
        # Should have GitLabToolkit + MCP tool
        assert len(tools) == 2
        assert mock_mcp in tools


class TestAgentDefinitionMCPServers:
    """Verify AgentDefinition parses mcp_servers from YAML."""

    def test_mcp_servers_default_empty(self):
        defn = AgentDefinition(name="test")
        assert defn.mcp_servers == []

    def test_mcp_servers_set(self):
        defn = AgentDefinition(name="test", mcp_servers=["jira", "slack"])
        assert defn.mcp_servers == ["jira", "slack"]

    def test_parse_from_yaml_data(self):
        from forge.agents.registry import _parse_definition

        data = {
            "name": "code-reviewer",
            "type": "code-reviewer",
            "mcp_servers": ["jira", "confluence"],
            "system_prompt": "Review code.",
        }
        defn = _parse_definition(data)
        assert defn.mcp_servers == ["jira", "confluence"]

    def test_parse_from_yaml_data_missing(self):
        from forge.agents.registry import _parse_definition

        data = {
            "name": "chat",
            "type": "chat",
            "system_prompt": "Chat.",
        }
        defn = _parse_definition(data)
        assert defn.mcp_servers == []
