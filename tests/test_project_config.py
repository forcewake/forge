import base64
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


class _FakeGitlabClient:
    """A typed double.

    ``identity()`` is SYNC in production — the repository-identity
    resolver calls it synchronously, so an AsyncMock made it a
    never-awaited coroutine (16 RuntimeWarnings per run). The double
    returns None (the not-adopted-contract path the resolver handles)
    and keeps ``get_file`` async like the real client.
    """

    def __init__(self, file_content: str | None = None, raise_error: bool = False) -> None:
        self._raise = raise_error
        self.calls = 0
        self._file = None
        if file_content is not None:
            self._file = {
                "content": base64.b64encode(file_content.encode()).decode(),
                "encoding": "base64",
            }

    def identity(self, *args: object, **kwargs: object) -> None:
        return None

    async def get_file(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        if self._raise:
            raise Exception("404 Not Found")

        class _File:
            content = self._file["content"] if self._file else ""
            encoding = "base64"

        return _File()


def _make_gitlab_client(file_content: str | None = None, raise_error: bool = False):
    """Create a typed fake GitLab client."""
    return _FakeGitlabClient(file_content=file_content, raise_error=raise_error)


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
        assert client.calls == 1

    async def test_different_projects_not_cached_together(self):
        client = _make_gitlab_client(file_content="review_rules:\n  - Rule 1\n")
        await load_project_config(client, project_id=4)
        await load_project_config(client, project_id=5)
        assert client.calls == 2

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


# ----------------------------------------------------------------------
# FND-01: the PUBLIC RepositoryIdentity contract — real adapter shapes
# ----------------------------------------------------------------------


class TestRepositoryIdentityContractFND01:
    def _real_readers(self):
        """The REAL reader constructors over stub HTTP transports (the
        review's acceptance: not doubles that conveniently add _owner)."""
        import httpx
        from forge.integrations.azure import AzureDevOpsClient
        from forge.integrations.github import GitHubClient

        class _StaticToken:
            def get_token(self) -> str:
                return "t"

        gh_transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        gh_client = GitHubClient(
            "https://api.github.test", token_provider=_StaticToken(), transport=gh_transport
        )
        az_transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        az_client = AzureDevOpsClient("https://dev.azure.test/org", "t", transport=az_transport)
        return gh_client, az_client

    def test_two_azure_repos_of_one_project_have_different_identities(self):
        from forge.integrations.azure import AzureRepositoryReader

        _, az_client = self._real_readers()
        reader_a = AzureRepositoryReader(az_client, "Proj", "repo-a")
        reader_b = AzureRepositoryReader(az_client, "Proj", "repo-b")

        key_a = reader_a.identity().cache_key("main", ".forge.yml")
        key_b = reader_b.identity().cache_key("main", ".forge.yml")
        assert key_a != key_b, "two repositories of ONE project must never share a policy entry"
        assert reader_a.identity().native_id == "Proj/repo-a"
        assert reader_b.identity().native_id == "Proj/repo-b"

    def test_two_hosts_with_identical_names_never_share(self):
        import httpx
        from forge.integrations.github import GitHubClient, GitHubRepositoryReader

        t = httpx.MockTransport(lambda request: httpx.Response(200, json={}))

        class _StaticToken:
            def get_token(self) -> str:
                return "t"

        host1 = GitHubRepositoryReader(
            GitHubClient("https://api.github.test", token_provider=_StaticToken(), transport=t),
            "acme",
            "widget",
        )
        host2 = GitHubRepositoryReader(
            GitHubClient("https://api.github.mirror", token_provider=_StaticToken(), transport=t),
            "acme",
            "widget",
        )
        assert host1.identity().cache_key("main", ".forge.yml") != host2.identity().cache_key(
            "main", ".forge.yml"
        )

    def test_gitlab_identity_is_project_qualified(self):
        from forge.gitlab.client import GitLabClient

        client = GitLabClient("https://gitlab.test", "t")
        assert client.identity(4).cache_key("main", ".forge.yml") != client.identity(5).cache_key(
            "main", ".forge.yml"
        )

    async def test_real_reader_identities_key_the_authority_cache(self):
        """End-to-end: two REAL Azure readers of one project, two policies —
        the cache must not cross them (the review's first remaining defect)."""
        from forge.integrations.azure import AzureRepositoryReader

        _, az_client = self._real_readers()

        class ConfigurableReader(AzureRepositoryReader):
            def __init__(self, client, project, repo, paths):
                super().__init__(client, project, repo)
                self._paths = paths

            async def read_blob(self, project_id, file_path, ref="HEAD"):
                from forge.gitlab.blob_reads import BlobReadResult

                content = "implement:\n  paths:\n" + "".join(f"    - '{p}'\n" for p in self._paths)
                return BlobReadResult.found(content)

        clear_cache()
        reader_a = ConfigurableReader(az_client, "Proj", "repo-a", ["src/a/**"])
        reader_b = ConfigurableReader(az_client, "Proj", "repo-b", ["src/b/**"])

        result_a = await read_project_config(reader_a, 42, ref="main")
        result_b = await read_project_config(reader_b, 42, ref="main")

        assert list(result_a.config.implement_paths) == ["src/a/**"]
        assert list(result_b.config.implement_paths) == ["src/b/**"]
