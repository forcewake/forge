"""Default server installations retain their actual runtime capabilities."""

from pathlib import Path
import tomllib

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]


def requirements(path):
    return {
        Requirement(value).name.lower().replace("_", "-"): Requirement(value)
        for value in tomllib.loads((ROOT / path).read_text())["project"]["dependencies"]
    }


def test_retired_implementation_dependencies_are_absent():
    document = tomllib.loads((ROOT / "pyproject.toml").read_text())
    declared = requirements("pyproject.toml")
    assert not {"agno", "litellm", "mcp", "tiktoken"} & declared.keys()
    assert "mysql" not in document["project"].get("optional-dependencies", {})


def test_default_server_has_the_runtime_stack_and_not_redis():
    declared = requirements("pyproject.toml")
    assert {
        "fastapi",
        "uvicorn",
        "httpx",
        "pydantic",
        "sqlalchemy",
        "alembic",
        "cryptography",
        "pyjwt",
    } <= declared.keys()
    assert "redis" not in declared
    assert "aiosqlite" not in declared
    assert "crypto" in declared["pyjwt"].extras


def test_each_distribution_declares_the_types_it_imports():
    for path in (
        "pyproject.toml",
        "packages/forge-protocol/pyproject.toml",
        "packages/forge-lane/pyproject.toml",
    ):
        assert "pydantic" in requirements(path)
