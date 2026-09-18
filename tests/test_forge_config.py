"""Tests for ForgeConfig: defaults isolation between instances (F24).

``ForgeConfig._DEFAULTS`` holds nested dicts; ``_deep_merge`` mutates the
instance data in place, so the instance copy must be deep — a shallow copy
leaks overrides into the class-level defaults and thus into every later
ForgeConfig instance.
"""

from pathlib import Path

import pytest

from forge.config import ForgeConfig, Settings, parse_driver_versions

#: Nested dict sections of _DEFAULTS that must never be shared by identity.
_NESTED_SECTIONS = (
    "models",
    "defaults",
    "labels",
    "rate_limits",
    "token_budgets",
    "redaction",
    "mcp_servers",
)


class TestDefaultsIsolation:
    def test_override_in_file_does_not_leak_into_later_instances(self, tmp_path: Path):
        """A forge.yml overriding a nested value must not poison the defaults."""
        config_file = tmp_path / "forge.yml"
        config_file.write_text(
            "forge:\n  rate_limits:\n    per_project_per_hour: 99\n",
            encoding="utf-8",
        )

        first = ForgeConfig(path=config_file)
        assert first.rate_limits["per_project_per_hour"] == 99

        second = ForgeConfig(path=tmp_path / "nonexistent.yml")
        assert second.rate_limits["per_project_per_hour"] == 30

    def test_mutation_does_not_leak_into_later_instances(self, tmp_path: Path):
        """Mutating one instance's nested dicts must not touch the defaults."""
        first = ForgeConfig(path=tmp_path / "nonexistent.yml")
        first.defaults["auto_review"] = False
        first.rate_limits["global_per_minute"] = 999
        first.models["fast"] = "overridden"

        second = ForgeConfig(path=tmp_path / "nonexistent.yml")
        assert second.defaults["auto_review"] is True
        assert second.rate_limits["global_per_minute"] == 10
        assert second.models["fast"] == "fast"

    def test_instances_never_share_nested_dicts(self, tmp_path: Path):
        """Every instance gets its own copy of every nested default dict."""
        first = ForgeConfig(path=tmp_path / "nonexistent.yml")
        second = ForgeConfig(path=tmp_path / "nonexistent.yml")
        for section in _NESTED_SECTIONS:
            assert first.get(section) is not second.get(section), section
            assert first.get(section) is not ForgeConfig._DEFAULTS[section], section


class TestDriverVersionsParser:
    """R15: the FORGE_DRIVER_VERSIONS control-plane form — shape-only
    validation (the closed driver-id set and the per-version charset are
    the lane's fail-closed concern: forge.harness_entry)."""

    def test_absent_and_empty_are_an_empty_map(self):
        assert parse_driver_versions(None) == {}
        assert parse_driver_versions("") == {}
        assert parse_driver_versions("   ") == {}

    def test_the_json_form_is_a_driver_to_version_map(self):
        assert parse_driver_versions('{"grok-build": "1.0.30", "copilot": "latest"}') == {
            "grok-build": "1.0.30",
            "copilot": "latest",
        }

    def test_malformed_json_is_refused(self):
        with pytest.raises(ValueError, match="FORGE_DRIVER_VERSIONS is not valid JSON"):
            parse_driver_versions("{oops")

    def test_a_non_object_is_refused(self):
        with pytest.raises(ValueError, match="JSON object"):
            parse_driver_versions('["claude-code"]')

    def test_empty_keys_or_values_are_refused(self):
        with pytest.raises(ValueError, match="non-empty"):
            parse_driver_versions('{"": "1.0"}')
        with pytest.raises(ValueError, match="non-empty"):
            parse_driver_versions('{"claude-code": "   "}')

    def test_the_settings_field_defaults_to_empty(self):
        """Additive and empty by default: unset means the lane's known-good
        pins apply (forge.harness_entry.DEFAULT_DRIVER_VERSIONS)."""
        assert Settings.model_fields["FORGE_DRIVER_VERSIONS"].default == ""
