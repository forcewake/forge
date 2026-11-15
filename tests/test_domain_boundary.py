"""The domain cannot acquire an adapter, filesystem, process or clock dependency."""

import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PURE = {
    "__future__",
    "collections",
    "dataclasses",
    "datetime",
    "decimal",
    "enum",
    "base64",
    "fnmatch",
    "hashlib",
    "json",
    "math",
    "posixpath",
    "re",
    "zlib",
    "types",
    "typing",
    "pydantic",
    "forge_protocol",
}


def violations(source: str, package="forge.domain") -> list[str]:
    failures = []
    for node in ast.walk(ast.parse(source)):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [resolve_name("." * node.level + (node.module or ""), package)]
        for name in names:
            if name != "forge.domain" and not name.startswith("forge.domain."):
                if name.split(".")[0] not in PURE:
                    failures.append(f"forbidden domain dependency: {name}")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in {
                "open",
                "eval",
                "exec",
                "__import__",
            }:
                failures.append(f"forbidden domain operation: {node.func.id}")
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "now",
                "utcnow",
                "today",
            }:
                failures.append(f"domain reads the clock: {node.func.attr}")
    return failures


def test_domain_imports_only_pure_values_and_never_reads_the_clock():
    failures = []
    for path in (ROOT / "src/forge/domain").rglob("*.py"):
        package = ".".join(path.parent.relative_to(ROOT / "src").parts)
        failures.extend(
            f"{path.name}: {failure}" for failure in violations(path.read_text(), package)
        )
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize(
    "source",
    [
        "from forge.runs.spec import ExecutableRunSpec",
        "from ..config import Settings",
        "import sqlalchemy",
        "from pathlib import Path",
        "import httpx",
        "import os",
        "import subprocess",
        "from datetime import datetime as clock\nclock.now()",
        "open('config')",
        "__import__('os')",
    ],
)
def test_domain_boundary_rejects_real_dependency_and_io_mutations(source):
    assert violations(source)
