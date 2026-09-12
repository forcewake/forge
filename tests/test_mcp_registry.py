import os

from forge.mcp_client.registry import MCPRegistry, _expand_env_vars


class TestExpandEnvVars:
    def test_expands_known_var(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET", "s3cret")
        assert _expand_env_vars("Bearer ${MY_SECRET}") == "Bearer s3cret"

    def test_leaves_unknown_var(self):
        # Ensure the var is NOT set
        os.environ.pop("NONEXISTENT_VAR_XYZ", None)
        assert _expand_env_vars("${NONEXISTENT_VAR_XYZ}") == "${NONEXISTENT_VAR_XYZ}"

    def test_multiple_vars(self, monkeypatch):
        monkeypatch.setenv("A", "1")
        monkeypatch.setenv("B", "2")
        assert _expand_env_vars("${A}-${B}") == "1-2"

    def test_no_vars(self):
        assert _expand_env_vars("plain text") == "plain text"


class TestMCPRegistry:
    def test_load_basic_config(self):
        config = {
            "jira": {
                "url": "https://jira-mcp.example.com/mcp",
                "auth_token_env": "JIRA_TOKEN",
                "description": "Jira tracking",
            }
        }
        registry = MCPRegistry(config)
        assert "jira" in registry.servers

        jira = registry.get("jira")
        assert jira is not None
        assert jira.url == "https://jira-mcp.example.com/mcp"
        assert jira.transport == "streamable-http"
        assert jira.auth_type == "bearer"
        assert jira.auth_token_env == "JIRA_TOKEN"
        assert jira.enabled is True

    def test_load_multiple_servers(self):
        config = {
            "jira": {"url": "https://jira.example.com/mcp"},
            "slack": {"url": "https://slack.example.com/mcp"},
            "grafana": {
                "url": "http://grafana:3001/mcp",
                "auth_type": "none",
            },
        }
        registry = MCPRegistry(config)
        assert len(registry.servers) == 3
        assert registry.get("grafana").auth_type == "none"

    def test_disabled_server_excluded_from_list_enabled(self):
        config = {
            "jira": {"url": "https://jira.example.com/mcp", "enabled": True},
            "slack": {"url": "https://slack.example.com/mcp", "enabled": False},
        }
        registry = MCPRegistry(config)
        enabled = registry.list_enabled()
        assert len(enabled) == 1
        assert enabled[0].name == "jira"

    def test_transport_normalization_underscore(self):
        config = {
            "test": {
                "url": "https://test.example.com/mcp",
                "transport": "streamable_http",
            }
        }
        registry = MCPRegistry(config)
        assert registry.get("test").transport == "streamable-http"

    def test_transport_sse(self):
        config = {
            "test": {
                "url": "https://test.example.com/sse",
                "transport": "sse",
            }
        }
        registry = MCPRegistry(config)
        assert registry.get("test").transport == "sse"

    def test_unsupported_transport_skipped(self):
        config = {
            "test": {
                "url": "https://test.example.com/mcp",
                "transport": "stdio",
            }
        }
        registry = MCPRegistry(config)
        assert registry.get("test") is None

    def test_missing_url_skipped(self):
        config = {"test": {"description": "no url"}}
        registry = MCPRegistry(config)
        assert registry.get("test") is None

    def test_invalid_entry_skipped(self):
        config = {"test": "not-a-dict"}
        registry = MCPRegistry(config)
        assert registry.get("test") is None

    def test_empty_config(self):
        registry = MCPRegistry({})
        assert len(registry.servers) == 0
        assert registry.list_enabled() == []

    def test_get_nonexistent(self):
        registry = MCPRegistry({})
        assert registry.get("nope") is None

    def test_header_env_expansion(self, monkeypatch):
        monkeypatch.setenv("MY_API_KEY", "key123")
        config = {
            "custom": {
                "url": "http://custom:9000/mcp",
                "auth_type": "header",
                "headers": {"X-API-Key": "${MY_API_KEY}"},
            }
        }
        registry = MCPRegistry(config)
        custom = registry.get("custom")
        assert custom.headers["X-API-Key"] == "key123"

    def test_description_preserved(self):
        config = {
            "jira": {
                "url": "https://jira.example.com/mcp",
                "description": "Issue tracker",
            }
        }
        registry = MCPRegistry(config)
        assert registry.get("jira").description == "Issue tracker"
