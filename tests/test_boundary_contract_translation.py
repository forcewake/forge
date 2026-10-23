"""Authority import contracts preserve exact module scope and reviewed callers."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OWNERS = {
    "repository_identity_checkpoint_lifecycle": (
        "forge.adaptive.checkpoint_repository",
        "forge.api_checkpoint_channel",
    ),
    "continuation_authorization": ("forge.adaptive.continuation",),
    "native_occupancy": ("forge.adaptive.admission", "forge.runs.admission"),
    "candidate_publication": ("forge.adaptive.publication_saga", "forge.adaptive.saga_durable"),
    "verification_applicability": (
        "forge.adaptive.verification_binding",
        "forge.adaptive.verification_sets",
        "forge.runs.usecases",
    ),
    "operator_projection": ("forge.adaptive.operator_view", "forge.adaptive.operator_snapshot"),
    "execution_delivery_spec": ("forge.adaptive.execution_spec",),
    "budget_amendment_application": ("forge.durable.budgets",),
    "feedback_admission": ("forge.gateway.feedback",),
    "command_acceptance_dedup": ("forge.gateway.durable_ingress",),
    "review_round_admission": ("forge.runs.round_admission",),
}


def _module_path(module):
    base = ROOT / "src" / Path(*module.split("."))
    return base.with_suffix(".py") if base.with_suffix(".py").exists() else base / "__init__.py"


def _contracts():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return config["tool"]["importlinter"]["contracts"]


def _authorities():
    return {
        contract["id"].removeprefix("authority-"): contract
        for contract in _contracts()
        if contract.get("id", "").startswith("authority-")
    }


@pytest.mark.parametrize("name", OWNERS)
def test_every_surviving_authority_has_an_import_contract(name):
    owners = {module for module in OWNERS[name] if _module_path(module).exists()}
    if not owners:
        assert name not in _authorities(), "deleted owners must leave no stale contract"
        return
    assert name in _authorities()
    assert set(_authorities()[name]["protected_modules"]) == owners


def test_authority_contracts_protect_exact_modules():
    contracts = _authorities()
    assert contracts
    for contract in contracts.values():
        assert contract["type"] == "protected"
        assert contract["as_packages"] is False
        assert not contract.get("ignore_imports")


def test_registered_authority_importers_are_not_stale():
    contracts = _authorities()
    assert contracts
    for contract in contracts.values():
        owners = set(contract["protected_modules"])
        for importer in set(contract["allowed_importers"]) - owners:
            path = _module_path(importer)
            assert path.exists(), importer
            imported = set()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    imported.add(node.module)
                    imported.update(f"{node.module}.{alias.name}" for alias in node.names)
            assert imported & owners, f"{importer} no longer imports any declared owner"


def test_translation_adds_no_ignored_imports():
    ignored = {entry for contract in _contracts() for entry in contract.get("ignore_imports", [])}
    assert ignored <= {
        "forge.durable.budgets -> forge.adaptive.usage_ingestion",
        "forge.durable.budgets -> forge.runs.candidate",
    }


def test_production_registry_is_retired():
    assert not (ROOT / "src/forge/adaptive/boundary_registry.py").exists()
