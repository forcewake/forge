"""Security and boundary invariants ported from the deleted 0.42 suites.

The old tests named forge.adaptive, forge.durable and forge.runs. The same
failure cases now sit on the 1.0 import boundary and the lane dispatch
document.
"""

import ast
from importlib.util import find_spec
from pathlib import Path

import pytest
from forge_protocol import LaneDispatch

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = (ROOT / "src", ROOT / "packages")
RETIRED = (
    "adaptive",
    "durable",
    "runs",
    "migrate",
    "gateway",
    "config",
    "database",
)
CREDENTIAL_FIELDS = {
    "api_key",
    "credential",
    "credential_ref",
    "password",
    "secret",
    "token",
}


def _modules(tree: ast.AST) -> list[str]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            name = node.args[0].value
            called = node.func
            dynamic = (
                isinstance(called, ast.Name) and called.id in {"__import__", "import_module"}
            ) or (isinstance(called, ast.Attribute) and called.attr == "import_module")
            if dynamic and isinstance(name, str):
                found.append(name)
    return found


def _retired(module: str) -> bool:
    parts = module.split(".")
    if parts[0] == "tests":
        return True
    return len(parts) >= 2 and parts[0] == "forge" and parts[1] in RETIRED


@pytest.mark.parametrize("name", RETIRED)
def test_retired_runtime_packages_stay_unimportable(name):
    assert find_spec("forge." + name) is None


def test_production_tree_does_not_import_retired_runtime_or_tests():
    offenders = []
    for root in PRODUCTION:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for module in _modules(tree):
                if _retired(module):
                    offenders.append(f"{path.relative_to(ROOT)} imports {module}")
    assert offenders == []


def test_native_dispatch_document_carries_no_credential_material():
    assert CREDENTIAL_FIELDS.isdisjoint(LaneDispatch.model_fields)
    for relative in (".github/workflows/lane.yml", "ci/lane.gitlab-ci.yml"):
        text = (ROOT / relative).read_text()
        assert "credential_ref" not in text
        assert "FORGE_MODEL" not in text
        assert "ANTHROPIC_API_KEY" not in text
    workflow = (ROOT / ".github/workflows/lane.yml").read_text()
    inputs = workflow.split("secrets:", 1)[0]
    assert "token" not in inputs
    gitlab = (ROOT / "ci/lane.gitlab-ci.yml").read_text()
    assert "FORGE_OIDC_TOKEN:" in gitlab
    assert "FORGE_LANE_DISPATCH" in gitlab
