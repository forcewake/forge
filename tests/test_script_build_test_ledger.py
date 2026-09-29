"""scripts/build_test_ledger.py: every test file gets a curated disposition."""

from __future__ import annotations

import csv

from tests._scripts import load_script, write_tree

ledger = load_script("build_test_ledger")

TREE = {
    "src/forge/__init__.py": "",
    "src/forge/main.py": "from forge import core\n",
    "src/forge/core.py": "X = 1\n",
    "src/forge/only_tests.py": "Y = 1\n",
    "tests/test_core.py": "from forge import core\n\n\ndef test_a():\n    assert core.X\n",
    "tests/test_only.py": "from forge import only_tests\n\n\ndef test_b():\n    assert only_tests.Y\n",
}


def _rows(root):
    with (root / ledger.LEDGER).open(encoding="utf-8", newline="") as handle:
        return {row["file"]: row for row in csv.DictReader(handle)}


def test_default_dispositions(tmp_path):
    root = write_tree(tmp_path, TREE)
    (root / ledger.LEDGER).parent.mkdir(parents=True)
    assert ledger.main(["--root", str(root)]) == 0
    rows = _rows(root)
    assert rows["tests/test_core.py"]["disposition"] == "keep"
    assert rows["tests/test_only.py"]["disposition"] == "drop-with-code"
    assert rows["tests/test_only.py"]["tests"] == "1"


def test_curated_columns_survive_regeneration(tmp_path):
    root = write_tree(tmp_path, TREE)
    (root / ledger.LEDGER).parent.mkdir(parents=True)
    ledger.main(["--root", str(root)])
    rows = list(_rows(root).values())
    rows[0].update(disposition="port-engine", phase="P3", note="owner decision")
    with (root / ledger.LEDGER).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ledger.FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    ledger.main(["--root", str(root)])
    kept = _rows(root)[rows[0]["file"]]
    assert (kept["disposition"], kept["phase"], kept["note"]) == (
        "port-engine",
        "P3",
        "owner decision",
    )


def test_kept_tooling_and_protected_evidence_are_not_dropped(tmp_path):
    tree = {
        **TREE,
        "pyproject.toml": '[project]\nname = "x"\n[project.scripts]\nforge = "forge.cli:main"\n',
        "src/forge/doctor.py": "X = 1\n",
        "src/forge/cli.py": "Y = 1\n",
        "src/forge/pinned.py": "Z = 1\n",
        "tests/test_doctor.py": "from forge import doctor\n",
        "tests/test_cli.py": "from forge import cli\n",
        "tests/test_pinned.py": "from forge import pinned\n",
        "tests/invariants/registry.py": 'EVIDENCE = frozenset(("tests/test_only.py",))\n',
        "docs/reference/edge-cases.md": "| EC-01 | a | b | c | `tests/test_pinned.py::test_x` |\n",
    }
    root = write_tree(tmp_path, tree)
    (root / ledger.LEDGER).parent.mkdir(parents=True)
    assert ledger.main(["--root", str(root)]) == 0
    rows = _rows(root)
    names = (
        "tests/test_doctor.py",
        "tests/test_cli.py",
        "tests/test_only.py",
        "tests/test_pinned.py",
    )
    assert {name: rows[name]["disposition"] for name in names} == dict.fromkeys(names, "keep")
    assert rows["tests/test_only.py"]["note"] == "invariant evidence"
    assert rows["tests/test_pinned.py"]["note"] == "edge-case pin"
