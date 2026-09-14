import base64
from unittest.mock import AsyncMock

import pytest

from forge.orchestrator.project_config import (
    ProjectConfig,
    clear_cache,
    load_project_config,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_cache()
    yield
    clear_cache()


def _make_gitlab_client(file_content: str | None = None, raise_error: bool = False):
    """Create a mock GitLab client."""
    client = AsyncMock()
    if raise_error:
        client.get_file.side_effect = Exception("404 Not Found")
    elif file_content is not None:
        encoded = base64.b64encode(file_content.encode()).decode()
        file_obj = AsyncMock()
        file_obj.content = encoded
        file_obj.encoding = "base64"
        client.get_file.return_value = file_obj
    return client


class TestLoadProjectConfig:
    async def test_returns_defaults_on_404(self):
        client = _make_gitlab_client(raise_error=True)
        config = await load_project_config(client, project_id=1)
        assert isinstance(config, ProjectConfig)
        assert config.enabled_agents is None
        assert config.disabled_agents == []
        assert config.review_rules == []
        assert config.skip_paths == []

    async def test_loads_config_from_file(self):
        yaml_content = (
            "disabled_agents:\n"
            "  - security-scanner\n"
            "review_rules:\n"
            "  - Always use type hints\n"
            "skip_paths:\n"
            "  - '*.generated.py'\n"
        )
        client = _make_gitlab_client(file_content=yaml_content)
        config = await load_project_config(client, project_id=1)
        assert "security-scanner" in config.disabled_agents
        assert "Always use type hints" in config.review_rules
        assert "*.generated.py" in config.skip_paths

    async def test_loads_nested_forge_key(self):
        yaml_content = "forge:\n  review_rules:\n    - Use Python 3.12+\n"
        client = _make_gitlab_client(file_content=yaml_content)
        config = await load_project_config(client, project_id=2)
        assert "Use Python 3.12+" in config.review_rules

    async def test_cache_hit(self):
        client = _make_gitlab_client(file_content="review_rules:\n  - Rule 1\n")
        config1 = await load_project_config(client, project_id=3)
        config2 = await load_project_config(client, project_id=3)
        assert config1 is config2
        # Should only call API once due to cache
        assert client.get_file.call_count == 1

    async def test_different_projects_not_cached_together(self):
        client = _make_gitlab_client(file_content="review_rules:\n  - Rule 1\n")
        await load_project_config(client, project_id=4)
        await load_project_config(client, project_id=5)
        assert client.get_file.call_count == 2

    async def test_invalid_yaml_returns_defaults(self):
        client = _make_gitlab_client(file_content="not: a: valid: [[[")
        config = await load_project_config(client, project_id=6)
        # yaml.safe_load raises on this — should return defaults
        assert isinstance(config, ProjectConfig)

    async def test_enabled_agents(self):
        yaml_content = "enabled_agents:\n  - code-reviewer\n"
        client = _make_gitlab_client(file_content=yaml_content)
        config = await load_project_config(client, project_id=7)
        assert config.enabled_agents == ["code-reviewer"]


class TestImplementPaths:
    """`implement.paths` — the v0.7 monorepo path-scope allowlist."""

    async def test_parses_implement_paths(self):
        yaml_content = "implement:\n  paths:\n    - 'services/api/**'\n    - 'packages/shared/*'\n"
        client = _make_gitlab_client(file_content=yaml_content)
        config = await load_project_config(client, project_id=20)
        assert config.implement_paths == ["services/api/**", "packages/shared/*"]

    async def test_implement_paths_empty_by_default(self):
        client = _make_gitlab_client(file_content="review_rules:\n  - Rule 1\n")
        config = await load_project_config(client, project_id=21)
        assert config.implement_paths == []

    async def test_implement_paths_under_forge_key(self):
        yaml_content = "forge:\n  implement:\n    paths:\n      - 'webapp/**'\n"
        client = _make_gitlab_client(file_content=yaml_content)
        config = await load_project_config(client, project_id=22)
        assert config.implement_paths == ["webapp/**"]

    async def test_malformed_implement_section_yields_empty_scope(self):
        yaml_content = "implement:\n  paths: not-a-list\n"
        client = _make_gitlab_client(file_content=yaml_content)
        config = await load_project_config(client, project_id=23)
        assert config.implement_paths == []
