import base64
from unittest.mock import AsyncMock

import pytest

from forge.orchestrator.project_config import (
    ProjectConfig,
    clear_cache,
    load_project_config,
    read_project_config,
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


# ----------------------------------------------------------------------
# D01: the cache key is the authority identity — repo, ref, path
# ----------------------------------------------------------------------


class TestCacheIdentityD01:
    async def test_two_repositories_of_one_project_never_share_policy(self):
        """Probe P01: same project id, different bound repositories — reader
        B must be CALLED and see its own policy, never A's from cache."""

        class ReaderStub:
            def __init__(self, owner: str, repo: str, paths: list[str]) -> None:
                self._owner, self._repo = owner, repo
                self.calls = 0
                self.paths = paths

            async def read_blob(self, project_id, file_path, ref="HEAD"):
                from forge.gitlab.blob_reads import BlobReadResult

                self.calls += 1
                content = "implement:\n  paths:\n" + "".join(f"    - '{p}'\n" for p in self.paths)
                return BlobReadResult.found(content)

        clear_cache()
        reader_a = ReaderStub("org", "repo-a", ["src/a/**"])
        reader_b = ReaderStub("org", "repo-b", ["src/b/**"])

        result_a = await read_project_config(reader_a, 42, ref="main")
        result_b = await read_project_config(reader_b, 42, ref="main")

        assert result_a.config is not None and list(result_a.config.implement_paths) == ["src/a/**"]
        assert result_b.config is not None and list(result_b.config.implement_paths) == ["src/b/**"]
        assert reader_b.calls == 1  # actually read — no cross-repo cache hit

    async def test_two_refs_of_one_repository_are_different_snapshots(self):
        """Probe P02: a different requested ref is a different authority —
        never a cache hit for the previous ref's policy."""

        class ReaderStub:
            def __init__(self) -> None:
                self._owner, self._repo = "org", "repo"
                self.by_ref: dict[str, str] = {}
                self.calls: list[str] = []

            async def read_blob(self, project_id, file_path, ref="HEAD"):
                from forge.gitlab.blob_reads import BlobReadResult

                self.calls.append(ref)
                content = self.by_ref[ref]
                return BlobReadResult.found(content)

        clear_cache()
        reader = ReaderStub()
        reader.by_ref["main"] = "implement:\n  paths:\n    - 'src/**'\n"
        reader.by_ref["deadbeef" * 5] = "implement:\n  paths:\n    - 'docs/**'\n"

        first = await read_project_config(reader, 7, ref="main")
        second = await read_project_config(reader, 7, ref="deadbeef" * 5)

        assert list(first.config.implement_paths) == ["src/**"]
        assert list(second.config.implement_paths) == ["docs/**"]
        assert reader.calls == ["main", "deadbeef" * 5]

    async def test_cached_absence_of_one_repo_does_not_mask_another(self):
        class ReaderStub:
            def __init__(self, owner: str, repo: str, found: bool) -> None:
                self._owner, self._repo = owner, repo
                self.found = found

            async def read_blob(self, project_id, file_path, ref="HEAD"):
                from forge.gitlab.blob_reads import BlobReadResult

                if not self.found:
                    return BlobReadResult.not_found("confirmed 404")
                content = "implement:\n  paths:\n    - 'only/**'\n"
                return BlobReadResult.found(content)

        clear_cache()
        absent = ReaderStub("org", "repo-x", found=False)
        restricted = ReaderStub("org", "repo-y", found=True)

        first = await read_project_config(absent, 42, ref="main")
        second = await read_project_config(restricted, 42, ref="main")

        assert first.status == "confirmed_absent"
        assert second.config is not None and list(second.config.implement_paths) == ["only/**"]


# ----------------------------------------------------------------------
# D02: a malformed policy is INVALID — never silently unrestricted
# ----------------------------------------------------------------------


class TestStrictPolicySchemaD02:
    async def test_a_string_paths_value_is_invalid_not_unrestricted(self):
        """Probe P03: ``implement.paths: "src/**"`` (a string, not a list)
        used to fall through to [] == whole repository. Now typed invalid."""
        client = _make_gitlab_client(file_content='forge:\n  implement:\n    paths: "src/**"\n')
        result = await read_project_config(client, 11, ref="main")
        assert result.status == "invalid"
        assert "implement.paths" in (result.detail or "")

    async def test_a_non_mapping_forge_key_is_invalid_not_an_exception(self):
        """Probe P04: ``forge: []`` used to raise AttributeError straight
        through the typed reader. Now a typed invalid result."""
        client = _make_gitlab_client(file_content="forge: []\n")
        result = await read_project_config(client, 12, ref="main")
        assert result.status == "invalid"

    async def test_null_and_non_string_entries_are_invalid(self):
        for bad in (
            "implement:\n    paths: null\n",
            "implement:\n    paths: [1, 2]\n",
        ):
            client = _make_gitlab_client(file_content="forge:\n  " + bad)
            result = await read_project_config(client, 13, ref="main")
            assert result.status == "invalid", bad

    async def test_a_non_mapping_implement_key_is_invalid(self):
        client = _make_gitlab_client(file_content="forge:\n  implement: 'nope'\n")
        result = await read_project_config(client, 14, ref="main")
        assert result.status == "invalid"

    async def test_a_valid_list_still_parses(self):
        client = _make_gitlab_client(
            file_content="forge:\n  implement:\n    paths:\n      - 'src/**'\n      - 'docs/**'\n"
        )
        result = await read_project_config(client, 15, ref="main")
        assert result.config is not None
        assert list(result.config.implement_paths) == ["src/**", "docs/**"]
