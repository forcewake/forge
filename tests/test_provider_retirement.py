"""Retired providers have no runtime adapter, settings or webhook surface."""

from pathlib import Path
import importlib.util
import re

import pytest

from forge.config import Settings
from forge.main import create_app

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_source_has_no_retired_provider():
    pattern = re.compile(r"azure|azdo", re.IGNORECASE)
    hits = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src").rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".sh", ".mjs"}
        and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert not hits, hits


@pytest.mark.parametrize(
    "module",
    [
        "forge.integrations.azure",
        "forge.runs.azure_service",
        "forge.execution.azure_pipelines",
        "forge.gateway.azure_webhook",
        "forge.reactive.azure_review",
        "forge.reactive.azure_ci_debug",
    ],
)
def test_retired_adapter_module_is_absent(module):
    assert importlib.util.find_spec(module) is None


def test_retired_provider_has_no_settings():
    assert not any(name.startswith("FORGE_AZDO_") for name in Settings.model_fields)


def test_retired_provider_has_no_ingress():
    routes = {
        getattr(route, "path", "")
        for route in create_app(
            settings=Settings(_env_file=None, FORGE_GITLAB_ENABLED=False)
        ).routes
    }
    assert not any("azure" in route for route in routes)


def test_supported_authorization_settings_are_not_retired_with_the_provider():
    from forge.config import Settings

    settings = Settings(
        _env_file=None,
        FORGE_GITHUB_APPROVERS="release-owner",
        FORGE_LANE_CONTROL_SECRET="fixture-control-secret",
    )
    assert settings.FORGE_GITHUB_APPROVERS == "release-owner"
    assert settings.FORGE_LANE_CONTROL_SECRET.get_secret_value() == "fixture-control-secret"
