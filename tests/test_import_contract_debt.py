"""The import-contract debt (``ignore_imports``) may only shrink (ROADMAP P0-07)."""

from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
# Lower this when an entry is removed. Raising it needs an ADR.
MAX_IGNORED_IMPORTS = 2


def test_ignored_imports_only_shrink():
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    contracts = config["tool"]["importlinter"]["contracts"]
    ignored = [entry for contract in contracts for entry in contract.get("ignore_imports", [])]
    assert len(ignored) <= MAX_IGNORED_IMPORTS, ignored
