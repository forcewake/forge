"""Tests for ForgeConfig: defaults isolation between instances (F24).

``ForgeConfig._DEFAULTS`` holds nested dicts; ``_deep_merge`` mutates the
instance data in place, so the instance copy must be deep — a shallow copy
leaks overrides into the class-level defaults and thus into every later
ForgeConfig instance.
"""

from pathlib import Path

from forge.config import ForgeConfig

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
