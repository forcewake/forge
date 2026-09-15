"""ADR-0022: MCP provisioning for harness lanes.

The canonical FORGE_HARNESS_MCP config (claude mcpServers interchange
schema) must render correctly per driver in the Actions lane, and a
malformed config must fail the lane closed.
"""

import json

import pytest

from forge.harness_entry import render_driver_script
from forge.harnesses.mcp import (
    McpConfigError,
    for_claude,
    for_copilot,
    for_grok,
    for_opencode,
    parse_servers,
)

CONTEXT7 = {"type": "http", "url": "https://mcp.context7.com/mcp"}
LEARN = {"type": "http", "url": "https://learn.microsoft.com/api/mcp"}
LOCAL = {"type": "stdio", "command": "npx", "args": ["-y", "@some/mcp"]}


class TestParseServers:
    def test_empty_is_no_servers(self):
        assert parse_servers(None) == {}
        assert parse_servers("") == {}
        assert parse_servers("  ") == {}

    def test_bare_object(self):
        assert parse_servers(json.dumps({"context7": CONTEXT7})) == {"context7": CONTEXT7}

    def test_wrapped_mcpServers_form(self):
        assert parse_servers(json.dumps({"mcpServers": {"context7": CONTEXT7}})) == {
            "context7": CONTEXT7
        }

    def test_bad_json_fails_closed(self):
        with pytest.raises(McpConfigError):
            parse_servers("{not json")

    def test_non_object_fails_closed(self):
        with pytest.raises(McpConfigError):
            parse_servers("[1,2]")

    def test_unknown_type_fails_closed(self):
        with pytest.raises(McpConfigError, match="unknown type"):
            parse_servers(json.dumps({"x": {"type": "carrier-pigeon"}}))

    def test_http_without_url_fails_closed(self):
        with pytest.raises(McpConfigError, match="url"):
            parse_servers(json.dumps({"x": {"type": "http"}}))

    def test_stdio_without_command_fails_closed(self):
        with pytest.raises(McpConfigError, match="command"):
            parse_servers(json.dumps({"x": {"type": "stdio"}}))


class TestPerDriverRendering:
    def test_claude_config_is_canonical(self):
        assert json.loads(for_claude({"context7": CONTEXT7})) == {
            "mcpServers": {"context7": CONTEXT7}
        }

    def test_grok_and_copilot_pass_through(self):
        for render in (for_grok, for_copilot):
            assert json.loads(render({"context7": CONTEXT7}))["mcpServers"] == {
                "context7": CONTEXT7
            }

    def test_opencode_translates_http_to_remote(self):
        rendered = json.loads(for_opencode({"context7": CONTEXT7, "learn": LEARN}))
        assert rendered["context7"] == {
            "type": "remote",
            "url": "https://mcp.context7.com/mcp",
            "enabled": True,
        }
        assert rendered["learn"]["type"] == "remote"

    def test_opencode_translates_stdio_to_local(self):
        rendered = json.loads(for_opencode({"loc": LOCAL}))
        assert rendered["loc"] == {
            "type": "local",
            "command": ["npx", "-y", "@some/mcp"],
            "enabled": True,
        }


class TestActionsLaneScript:
    def test_claude_always_strict_mcp_config(self):
        script = render_driver_script("claude-code", "m", ".forge/brief.md")
        assert "--strict-mcp-config" in script
        assert "/tmp/forge-mcp.json" in script
        # Empty map by default: isolation without servers.
        assert '"mcpServers": {}' in script

    def test_claude_grants_mcp_tools_per_server(self):
        script = render_driver_script(
            "claude-code", "m", ".forge/brief.md", mcp_servers={"context7": CONTEXT7}
        )
        assert "mcp__context7__*" in script
        assert "mcp__context7" in script

    def test_grok_writes_settings_only_with_servers(self):
        without = render_driver_script("grok-build", "", ".forge/brief.md")
        assert ".grok/settings.json" not in without
        with_mcp = render_driver_script(
            "grok-build", "", ".forge/brief.md", mcp_servers={"context7": CONTEXT7}
        )
        assert ".grok/settings.json" in with_mcp
        assert '"url": "https://mcp.context7.com/mcp"' in with_mcp

    def test_copilot_writes_config_and_grants_servers(self):
        with_mcp = render_driver_script(
            "copilot", "", ".forge/brief.md", mcp_servers={"learn": LEARN}
        )
        assert ".copilot/mcp-config.json" in with_mcp
        assert "--allow-tool learn" in with_mcp

    def test_opencode_mcp_rides_the_config_env(self):
        with_mcp = render_driver_script(
            "opencode", "", ".forge/brief.md", mcp_servers={"context7": CONTEXT7}
        )
        assert '"mcp"' in with_mcp
        assert '"remote"' in with_mcp
