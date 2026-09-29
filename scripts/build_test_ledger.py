"""Generate the test disposition ledger (ROADMAP P0-13).

One row per test file under ``tests/``. The measured columns are recomputed
on every run; the curated columns (``disposition``, ``phase``, ``note``)
are preserved for files already in the ledger.

The default disposition never drops a file that is invariant evidence
(``tests/invariants/registry.py``) or an edge-case pin
(``docs/reference/edge-cases.md``), nor a test of tooling that stays
(``forge.doctor`` and the ``[project.scripts]`` commands).

Usage::

    uv run python scripts/build_test_ledger.py      # writes docs/roadmap/2026-09-29-simplification/test-ledger.csv
"""

from __future__ import annotations

import argparse
import ast
import csv
import re
import sys
from pathlib import Path

import _codebase as cb

LEDGER = Path("docs/roadmap/2026-09-29-simplification/test-ledger.csv")
FIELDS = (
    "file",
    "tests",
    "loc",
    "imports_runtime",
    "imports_tests_only",
    "area",
    "disposition",
    "phase",
    "note",
)
SERVICES = ("forge.runs.service", "forge.runs.github_service", "forge.runs.azure_service")
PROVIDER_CLIENTS = ("forge.gitlab", "forge.integrations", "forge.repository.writer")
CURATED = ("disposition", "phase", "note")
KEPT_TOOLING_ROOTS = ("forge.doctor",)
INVARIANT_REGISTRY = Path("tests/invariants/registry.py")
EDGE_CASE_REGISTER = Path("docs/reference/edge-cases.md")
PINNED_FILE = re.compile(r"`(tests/[^`:]+\.py)(?:::[^`]*)?`")


def count_tests(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test")
    )


def area(rel: str) -> str:
    parts = Path(rel).parts
    if len(parts) > 2:
        return parts[1]
    stem = Path(rel).stem.removeprefix("test_")
    return stem.split("_", 1)[0]


def protected_files(root: Path) -> dict[str, str]:
    """Test files that are invariant evidence or edge-case pins, with the reason."""
    found: dict[str, str] = {}
    registry = root / INVARIANT_REGISTRY
    if registry.is_file():
        tree = ast.parse(registry.read_text(encoding="utf-8"), filename=str(registry))
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            for node in ast.walk(call):
                value = node.value if isinstance(node, ast.Constant) else None
                if isinstance(value, str) and value.startswith("tests/") and value.endswith(".py"):
                    found.setdefault(value, "invariant evidence")
    register = root / EDGE_CASE_REGISTER
    if register.is_file():
        for rel in PINNED_FILE.findall(register.read_text(encoding="utf-8")):
            found.setdefault(rel, "edge-case pin")
    return found


def default_disposition(rel: str, imported: set[str], droppable: set[str]) -> tuple[str, str]:
    """A starting point for the owner to curate — never the final word.

    ``imported`` holds leaf modules only (packages are excluded), so a test
    that imports nothing but code that is going away is recognised.
    """
    if imported and imported <= droppable:
        return "drop-with-code", "P1"
    if any(name.startswith(SERVICES) for name in imported) or "production_entry" in rel:
        return "port-engine", "P3"
    if any(name.startswith(PROVIDER_CLIENTS) for name in imported):
        return "port-adapter", "P3"
    return "keep", "-"


def build_rows(root: Path, previous: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    modules = cb.discover_modules(root)
    reach = cb.classify(root, modules)
    kept_roots = {*KEPT_TOOLING_ROOTS, *cb.console_script_modules(root)}
    kept = cb.reachable(kept_roots, cb.import_graph(modules))
    droppable = reach.tests_only | (reach.tooling_only - kept)
    protected = protected_files(root)
    rows = []
    for path in sorted((root / "tests").rglob("test*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        imported = {
            m
            for m in cb.imports_of(path, modules)
            if m.startswith("forge") and modules[m].path.name != "__init__.py"
        }
        disposition, phase = default_disposition(rel, imported, droppable)
        note = protected.get(rel, "")
        if note and disposition.startswith("drop"):
            disposition, phase = "keep", "-"
        row = {
            "file": rel,
            "tests": str(count_tests(path)),
            "loc": str(cb.count_lines(path)),
            "imports_runtime": str(bool(imported & reach.runtime)).lower(),
            "imports_tests_only": str(bool(imported & reach.tests_only)).lower(),
            "area": area(rel),
            "disposition": disposition,
            "phase": phase,
            "note": note,
        }
        if rel in previous:
            row.update({key: previous[rel][key] for key in CURATED})
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=cb.repo_root())
    args = parser.parse_args(argv)
    target = args.root / LEDGER
    previous: dict[str, dict[str, str]] = {}
    if target.exists():
        with target.open(encoding="utf-8", newline="") as handle:
            previous = {row["file"]: row for row in csv.DictReader(handle)}
    rows = build_rows(args.root, previous)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["disposition"]] = counts.get(row["disposition"], 0) + 1
    print(f"test-ledger: {len(rows)} files → {LEDGER} {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
