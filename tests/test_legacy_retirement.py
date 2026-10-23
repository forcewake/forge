"""The retired execution engine cannot return through imports or app wiring."""

import importlib.util
from pathlib import Path
import pytest
from forge.config import Settings
from forge.main import create_app


@pytest.mark.parametrize(
    "name", ["orchestrator", "flows", "agents", "llm", "mcp_client", "context", "stores"]
)
def test_legacy_package_is_absent(name):
    assert importlib.util.find_spec("forge." + name) is None


def test_application_does_not_construct_the_legacy_execution_engine():
    root = Path(__file__).resolve().parents[1]
    for path in ("src/forge/main.py", "src/forge/worker/app.py", "src/forge/gateway/router.py"):
        text = (root / path).read_text()
        assert all(
            name not in text
            for name in (
                "AgentRegistry",
                "FlowLoader",
                "FlowRunner",
                "Orchestrator",
                "MCPConnectionManager",
            )
        )
    app = create_app(Settings(_env_file=None, FORGE_GITLAB_ENABLED=False))
    assert not any(getattr(route, "path", "") == "/mcp-tools" for route in app.routes)
    assert not (root / "agents").exists()
