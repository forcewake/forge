"""Retired execution code cannot return through application construction."""

from importlib.util import find_spec
from pathlib import Path

import pytest

from forge.runtime.app import create_app
from tests.runtime_fixture import configuration


@pytest.mark.parametrize(
    "name", ["orchestrator", "flows", "agents", "llm", "mcp_client", "context", "stores"]
)
def test_legacy_package_is_absent(name):
    assert find_spec("forge." + name) is None


def test_application_does_not_construct_the_legacy_execution_engine(tmp_path):
    root = Path(__file__).resolve().parents[1]
    for name in ("app.py", "assembly.py", "handlers.py", "worker.py"):
        text = (root / "src/forge/runtime" / name).read_text()
        assert all(
            symbol not in text
            for symbol in (
                "AgentRegistry",
                "FlowLoader",
                "FlowRunner",
                "Orchestrator",
                "MCPConnectionManager",
            )
        )
    config, _ = configuration(tmp_path)
    assert not any(
        getattr(route, "path", "") == "/mcp-tools" for route in create_app(config).routes
    )
    assert not (root / "agents").exists()
