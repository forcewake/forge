"""scripts/metrics.py: the simplification-program measurements."""

from __future__ import annotations

import json

from tests._scripts import load_script, write_tree

metrics = load_script("metrics")

TREE = {
    "pyproject.toml": '[project]\nname = "x"\ndependencies = ["a", "b"]\n',
    "src/forge/__init__.py": "",
    "src/forge/main.py": "from forge import core\n",
    "src/forge/core.py": "def f():\n    return 1\n",
    "src/forge/doctor.py": "from forge import diag\n",
    "src/forge/diag.py": "X = 1\n",
    "src/forge/orphan.py": "Y = 2\n",
    "tests/test_orphan.py": "from forge import orphan\n",
    "docs/a.md": "one two three\n",
}


def test_reachability_splits_runtime_tooling_and_test_only_code(tmp_path):
    snapshot = metrics.measure(write_tree(tmp_path, TREE))
    assert snapshot["modules"] == 6
    assert snapshot["runtime_lines"] == 3  # forge/__init__ (0) + main (1) + core (2)
    assert snapshot["tooling_only_lines"] == 2  # doctor + diag
    assert snapshot["tests_only_lines"] == 1  # orphan
    assert snapshot["direct_dependencies"] == 2
    assert snapshot["docs_words"] == 3


def test_who_imports_lists_direct_importers(tmp_path):
    assert metrics.who_imports(write_tree(tmp_path, TREE), "forge.core") == ["forge.main"]


def test_write_appends_history_and_regenerates_one_block(tmp_path):
    root = write_tree(tmp_path, {metrics.METRICS_FILE.as_posix(): "# Metrics\n\nTargets.\n"})
    metrics.write(root, {"date": "2026-09-30", "sha": "abc1234", "src_lines": 10})
    metrics.write(root, {"date": "2026-10-07", "sha": "def5678", "src_lines": 8})
    text = (root / metrics.METRICS_FILE).read_text()
    assert text.count(metrics.BEGIN) == 1
    assert "| `src/forge` lines | 10 | 8 |" in text
    assert len(json.loads((root / metrics.HISTORY_FILE).read_text())) == 2
