"""scripts/metrics.py: the simplification-program measurements."""

from __future__ import annotations

import json

import pytest

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


def test_breakdown_adds_up_and_the_lane_package_is_counted_separately(tmp_path):
    tree = {**TREE, "packages/forge-lane/src/forge_lane/__init__.py": "A = 1\nB = 2\n"}
    snapshot = metrics.measure(write_tree(tmp_path, tree))
    parts = ("runtime_lines", "tooling_only_lines", "tests_only_lines", "unreached_lines")
    assert sum(snapshot[key] for key in parts) == snapshot["src_lines"]
    assert snapshot["forge_lane_lines"] == 2


def test_who_imports_leaves_out_the_target_package_itself(tmp_path):
    tree = {
        **TREE,
        "src/forge/pkg/__init__.py": "from forge.pkg import a\n",
        "src/forge/pkg/a.py": "",
        "src/forge/user.py": "from forge import pkg\n",
    }
    assert metrics.who_imports(write_tree(tmp_path, tree), "forge.pkg") == ["forge.user"]


def test_write_is_idempotent_per_commit_and_refuses_broken_markers(tmp_path):
    root = write_tree(tmp_path, {metrics.METRICS_FILE.as_posix(): "# Metrics\n"})
    metrics.write(root, {"date": "2026-09-30", "sha": "abc1234", "src_lines": 10})
    metrics.write(root, {"date": "2026-09-30", "sha": "abc1234", "src_lines": 9})
    assert len(json.loads((root / metrics.HISTORY_FILE).read_text())) == 1
    assert "| `src/forge` lines | 9 |" in (root / metrics.METRICS_FILE).read_text()
    (root / metrics.METRICS_FILE).write_text(f"# Metrics\n{metrics.BEGIN}\n")
    with pytest.raises(ValueError):
        metrics.write(root, {"date": "2026-10-01", "sha": "def5678", "src_lines": 8})
    assert len(json.loads((root / metrics.HISTORY_FILE).read_text())) == 1


def test_the_sha_is_unknown_without_git(tmp_path, monkeypatch):
    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(metrics.subprocess, "run", no_git)
    assert metrics._git_sha(tmp_path) == "unknown"


def test_suite_seconds_reads_a_pytest_junit_report(tmp_path):
    report = tmp_path / "report.xml"
    report.write_text(
        '<testsuites name="pytest tests"><testsuite name="pytest" time="12.5"/></testsuites>'
    )
    assert metrics._suite_seconds(report) == 12.5
