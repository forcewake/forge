"""The Invariant Suite registry cannot lose its evidence silently."""

from __future__ import annotations

import ast
from pathlib import Path

from tests.invariants.registry import INVARIANTS

REPO = Path(__file__).resolve().parents[2]
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def collected_tests(path: Path) -> list[str]:
    """What pytest collects from a file: ``test*`` functions and ``Test*`` class methods."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bodies = [tree.body] + [
        node.body
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test")
    ]
    return [
        node.name
        for body in bodies
        for node in body
        if isinstance(node, FUNCTIONS) and node.name.startswith("test")
    ]


def test_i1_to_i8_and_the_self_trigger_rule_are_registered():
    required = {f"I{n}" for n in range(1, 9)} | {"SELF-TRIGGER"}
    assert required <= {inv.ident for inv in INVARIANTS}


def test_every_invariant_names_evidence():
    assert all(inv.files for inv in INVARIANTS)


def test_registered_paths_are_clean_and_repo_relative():
    paths = [f for inv in INVARIANTS for f in inv.files]
    unclean = [
        f
        for f in paths
        if f != Path(f).as_posix() or not f.startswith("tests/") or ".." in Path(f).parts
    ]
    assert unclean == []


def test_every_registered_file_exists():
    missing = [f for inv in INVARIANTS for f in inv.files if not (REPO / f).is_file()]
    assert missing == []


def test_every_registered_file_defines_tests():
    empty = [f for inv in INVARIANTS for f in inv.files if not collected_tests(REPO / f)]
    assert empty == []


def test_a_file_without_collectable_tests_is_caught(tmp_path):
    path = tmp_path / "test_empty.py"
    path.write_text(
        "def helper():\n    pass\n\n\nclass Helper:\n    def test_x(self):\n        pass\n"
    )
    assert collected_tests(path) == []
    path.write_text("class TestX:\n    async def test_x(self):\n        pass\n")
    assert collected_tests(path) == ["test_x"]
