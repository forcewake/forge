"""R18 write-policy profiles: config hooks and provider-sensitive paths.

The configuration half of the profile matrix —

- ``FORGE_WRITE_PROFILES`` / forge.yml ``write_profiles:``: custom write
  profiles (name → denied_paths / allowed_paths / require_special_approval),
  fail-closed validation, built-in names never shadowed;
- ``FORGE_PIPELINE_ENTRYPOINTS`` / forge.yml ``pipeline_entrypoints:``: the
  per-project sensitive pipeline paths (Azure's pipeline definition may be
  ANY file — onboarding names the real entrypoints) and the run-key lookup
  that feeds them to the publisher;
- :func:`forge.repository.changeset.resolve_write_policy` — the pure
  resolution from profile name + parsed config to an effective
  :class:`~forge.repository.changeset.WritePolicy`.

The enforcement half lives in test_repository_changeset.py (validation),
test_publisher.py and test_publication_boundary.py (the boundary).
"""

import pytest

from forge.config import (
    ForgeConfig,
    parse_pipeline_entrypoints,
    parse_write_profiles,
    validate_pipeline_entrypoints,
    validate_write_profiles,
)
from forge.durable import FlowRun
from forge.repository.changeset import (
    BUILTIN_WRITE_PROFILES,
    DEFAULT_WRITE_PROFILE,
    normalize_repo_path,
    resolve_write_policy,
)
from forge.runs.publisher import run_project_keys, sensitive_paths_for_run


def azure_run(**overrides) -> FlowRun:
    run = FlowRun(
        id="0" * 32,
        project_id=42,
        issue_iid=7,
        base_sha="base-sha-1",
    )
    run.provider = "azure_devops"
    for key, value in overrides.items():
        setattr(run, key, value)
    return run


class TestParseWriteProfiles:
    def test_empty_and_none_are_unconfigured(self):
        assert parse_write_profiles(None) == {}
        assert parse_write_profiles("") == {}
        assert validate_write_profiles(None) == {}

    def test_full_custom_profile_parses(self):
        parsed = parse_write_profiles(
            '{"frontend": {"denied_paths": ["web/legacy/**"],'
            ' "allowed_paths": ["web/src/**"],'
            ' "require_special_approval": true}}'
        )
        assert parsed == {
            "frontend": {
                "denied_paths": ["web/legacy/**"],
                "allowed_paths": ["web/src/**"],
                "require_special_approval": True,
            }
        }

    def test_defaults_are_applied_for_omitted_keys(self):
        parsed = parse_write_profiles('{"minimal": {}}')
        assert parsed == {
            "minimal": {"denied_paths": [], "allowed_paths": [], "require_special_approval": False}
        }

    def test_malformed_json_fails_closed(self):
        with pytest.raises(ValueError, match="FORGE_WRITE_PROFILES is not valid JSON"):
            parse_write_profiles("{not json")

    def test_non_mapping_fails(self):
        with pytest.raises(ValueError, match="mapping"):
            validate_write_profiles(["nope"])

    def test_builtin_names_may_not_be_shadowed(self):
        for name in BUILTIN_WRITE_PROFILES:
            with pytest.raises(ValueError, match="shadows a built-in"):
                validate_write_profiles({name: {}})

    def test_bad_path_lists_fail(self):
        with pytest.raises(ValueError, match="denied_paths"):
            validate_write_profiles({"x": {"denied_paths": ["", "ok/**"]}})
        with pytest.raises(ValueError, match="allowed_paths"):
            validate_write_profiles({"x": {"allowed_paths": [42]}})

    def test_bad_approval_flag_fails(self):
        with pytest.raises(ValueError, match="require_special_approval"):
            validate_write_profiles({"x": {"require_special_approval": "yes"}})


class TestForgeYamlWriteProfiles:
    def _write_config(self, tmp_path, body: str) -> ForgeConfig:
        path = tmp_path / "forge.yml"
        path.write_text(body)
        return ForgeConfig(path)

    def test_yaml_form_loads(self, tmp_path):
        config = self._write_config(
            tmp_path,
            "write_profiles:\n"
            "  backend:\n"
            "    denied_paths:\n"
            "      - 'infra/**'\n"
            "    require_special_approval: false\n",
        )
        assert config.write_profiles == {
            "backend": {
                "denied_paths": ["infra/**"],
                "allowed_paths": [],
                "require_special_approval": False,
            }
        }

    def test_yaml_pipeline_entrypoints_load(self, tmp_path):
        config = self._write_config(
            tmp_path,
            "pipeline_entrypoints:\n"
            "  azure_devops:42:\n"
            "    - azure-pipelines.yml\n"
            "    - ci/build.yml\n",
        )
        assert config.pipeline_entrypoints == {
            "azure_devops:42": ["azure-pipelines.yml", "ci/build.yml"]
        }

    def test_yaml_form_resolves_into_a_policy(self, tmp_path):
        config = self._write_config(
            tmp_path, "write_profiles:\n  backend:\n    denied_paths: ['infra/**']\n"
        )
        policy = resolve_write_policy("backend", custom_profiles=config.write_profiles)
        assert policy.name == "backend"

    def test_broken_yaml_form_fails(self, tmp_path):
        config = self._write_config(tmp_path, "write_profiles:\n  ci_change: {}\n")
        with pytest.raises(ValueError, match="shadows a built-in"):
            _ = config.write_profiles

    def test_missing_file_keeps_defaults(self, tmp_path):
        config = ForgeConfig(tmp_path / "absent.yml")
        assert config.write_profiles == {}
        assert config.pipeline_entrypoints == {}


class TestParsePipelineEntrypoints:
    def test_empty_and_none_are_unconfigured(self):
        assert parse_pipeline_entrypoints(None) == {}
        assert parse_pipeline_entrypoints("") == {}

    def test_mapping_parses(self):
        assert parse_pipeline_entrypoints('{"azure_devops:42": ["ci/build.yml"]}') == {
            "azure_devops:42": ["ci/build.yml"]
        }

    def test_malformed_json_fails_closed(self):
        with pytest.raises(ValueError, match="FORGE_PIPELINE_ENTRYPOINTS is not valid JSON"):
            parse_pipeline_entrypoints("{oops")

    def test_bad_shapes_fail(self):
        with pytest.raises(ValueError, match="mapping"):
            validate_pipeline_entrypoints(["ci/build.yml"])
        with pytest.raises(ValueError, match="non-empty project keys"):
            validate_pipeline_entrypoints({" ": ["x.yml"]})
        with pytest.raises(ValueError, match="list of non-empty pipeline paths"):
            validate_pipeline_entrypoints({"azure_devops:42": [""]})


class TestRunProjectKeys:
    def test_provider_scoped_key(self):
        assert run_project_keys(azure_run()) == ("azure_devops:42",)

    def test_gitlab_default_provider(self):
        run = FlowRun(id="0" * 32, project_id=7, issue_iid=1, base_sha="b")
        assert run_project_keys(run) == ("gitlab:7",)

    def test_github_answers_to_repo_identity_too(self):
        run = azure_run(
            provider="github", project_id=1234, github_repo_full_name="acme/acme-widget"
        )
        assert run_project_keys(run) == ("github:1234", "github:acme/acme-widget")


class TestSensitivePathsForRun:
    def test_unconfigured_map_yields_nothing(self):
        assert sensitive_paths_for_run(azure_run(), None) == []
        assert sensitive_paths_for_run(azure_run(), {}) == []

    def test_project_match_collects_entrypoints(self):
        entrypoints = {
            "azure_devops:42": ["azure-pipelines.yml", "ci/build.yml"],
            "gitlab:42": ["other.yml"],
        }
        assert sensitive_paths_for_run(azure_run(), entrypoints) == [
            "azure-pipelines.yml",
            "ci/build.yml",
        ]

    def test_github_repo_key_matches(self):
        run = azure_run(provider="github", github_repo_full_name="acme/acme-widget")
        entrypoints = {"github:acme/acme-widget": [".github/workflows/deploy.yml"]}
        assert sensitive_paths_for_run(run, entrypoints) == [".github/workflows/deploy.yml"]

    def test_matches_are_order_deduplicated(self):
        run = azure_run(provider="github", github_repo_full_name="acme/acme-widget")
        entrypoints = {
            "github:1234": ["shared.yml"],
            "github:acme/acme-widget": ["shared.yml", "own.yml"],
        }
        assert sensitive_paths_for_run(run, entrypoints) == ["shared.yml", "own.yml"]

    def test_blank_entries_are_dropped(self):
        assert sensitive_paths_for_run(azure_run(), {"azure_devops:42": ["", "  "]}) == []


class TestResolveWritePolicy:
    def test_default_is_the_historical_behavior(self):
        policy = resolve_write_policy()
        assert policy.name == DEFAULT_WRITE_PROFILE == "no_dependencies"
        assert policy.denied_paths == frozenset({".gitlab-ci.yml", ".forge.yml"})
        assert policy.denied_prefixes == (".github/",)
        assert policy.deny_lockfiles is True
        assert policy.require_special_approval is False
        assert policy.sensitive_paths == frozenset()

    def test_builtin_names_are_the_closed_default_set(self):
        assert BUILTIN_WRITE_PROFILES == (
            "no_dependencies",
            "code_only",
            "dependency_update",
            "ci_change",
        )

    def test_extra_sensitive_paths_survive_normalization(self):
        policy = resolve_write_policy(extra_denied_paths=["./ci//build.yml", "x\\y.yml", " "])
        assert policy.sensitive_paths == frozenset({"ci/build.yml", "x/y.yml"})

    def test_unknown_name_names_the_known_ones(self):
        with pytest.raises(ValueError) as exc:
            resolve_write_policy("nope", custom_profiles={"mine": {}})
        message = str(exc.value)
        for known in (*BUILTIN_WRITE_PROFILES, "mine"):
            assert known in message

    def test_normalization_helper_contract(self):
        assert normalize_repo_path("") == ""
        assert normalize_repo_path("a/..") == "a/.."  # traversal is never resolved
        assert normalize_repo_path("\\\\\\srv\\share") == "srv/share"
