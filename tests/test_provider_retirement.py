"""Retired providers have no runtime adapter, settings or webhook surface."""

from pathlib import Path
import importlib.util
import re

import pytest

from forge.runtime.settings import RuntimeSettings
from tests.runtime_fixture import configuration
from pydantic import ValidationError
import json
from forge.runtime.app import create_app

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
    try:
        found = importlib.util.find_spec(module)
    except ModuleNotFoundError:
        found = None
    assert found is None


def test_retired_provider_has_no_settings(tmp_path):
    config, _ = configuration(tmp_path)
    assert not any(name.startswith("FORGE_AZDO_") for name in RuntimeSettings.model_fields)
    value = config.model_dump(mode="json", by_alias=True)
    value["connections"][0]["provider"] = "azure_devops"
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate_json(json.dumps(value))


def test_retired_provider_has_no_ingress(tmp_path):
    config, _ = configuration(tmp_path)
    routes = {getattr(route, "path", "") for route in create_app(config).routes}
    assert not any("azure" in route for route in routes)


def test_supported_authorization_settings_are_not_retired_with_the_provider(tmp_path):
    config, _ = configuration(tmp_path)
    connection = config.connections[0]
    assert connection.repositories[0].approver_ids == ("42",)
    assert config.secret_key_env == "SYNTHETIC_SECRET"
