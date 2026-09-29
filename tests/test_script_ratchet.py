"""scripts/ratchet.py: oversized code may only shrink, and shrinking is locked in."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._scripts import load_script, write_tree

ratchet = load_script("ratchet")


def _function(name: str, lines: int) -> str:
    """A function that is exactly ``lines`` lines long."""
    return f"def {name}():\n" + "    x = 1\n" * (lines - 2) + "    return x\n"


def _baseline(root: Path) -> dict:
    return json.loads((root / "scripts/ratchet-baseline.json").read_text())


def _init(root: Path) -> None:
    (root / "scripts").mkdir(exist_ok=True)
    assert ratchet.main(["--root", str(root), "--init"]) == 0


def _check(root: Path) -> int:
    return ratchet.main(["--root", str(root)])


def test_small_code_needs_no_baseline_entry(tmp_path):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("f", 10)})
    _init(root)
    assert _baseline(root) == {"files": {}, "functions": {}}
    assert _check(root) == 0


def test_oversized_function_is_recorded_and_may_not_grow(tmp_path, capsys):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("big", 90)})
    _init(root)
    assert _baseline(root)["functions"] == {"src/forge/m.py::big": 90}
    (root / "src/forge/m.py").write_text(_function("big", 91))
    assert _check(root) == 1
    out = capsys.readouterr().out
    assert "src/forge/m.py::big (line 1) grew to 91 lines (allowed 90)" in out
    assert "--accept" not in out


def test_new_function_over_the_limit_fails(tmp_path, capsys):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("f", 10)})
    _init(root)
    (root / "src/forge/n.py").write_text(_function("huge", 81))
    assert _check(root) == 1
    assert "src/forge/n.py::huge (line 1) has 81 lines (limit 80)" in capsys.readouterr().out


def test_new_file_over_the_limit_fails(tmp_path, capsys):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("f", 10)})
    _init(root)
    (root / "src/forge/big.py").write_text("x = 1\n" * 601)
    assert _check(root) == 1
    assert "file src/forge/big.py has 601 lines (limit 600)" in capsys.readouterr().out


def test_workspace_packages_are_checked(tmp_path, capsys):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("f", 10)})
    _init(root)
    lane = root / "packages/forge-lane/src/forge_lane/run.py"
    lane.parent.mkdir(parents=True)
    lane.write_text(_function("huge", 81))
    assert _check(root) == 1
    assert "packages/forge-lane/src/forge_lane/run.py::huge" in capsys.readouterr().out


def test_small_function_may_grow_up_to_the_limit(tmp_path):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("f", 10)})
    _init(root)
    (root / "src/forge/m.py").write_text(_function("f", 80))
    assert _check(root) == 0


def test_shrinking_is_locked_in(tmp_path, capsys):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("big", 120)})
    _init(root)
    (root / "src/forge/m.py").write_text(_function("big", 90))
    assert _check(root) == 1
    assert "the baseline is stale" in capsys.readouterr().out
    assert ratchet.main(["--root", str(root), "--update"]) == 0
    assert _check(root) == 0
    (root / "src/forge/m.py").write_text(_function("big", 120))
    assert _check(root) == 1


def test_update_lowers_shrunk_debt_and_drops_repaid_entries(tmp_path):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("a", 120) + _function("b", 90)})
    _init(root)
    (root / "src/forge/m.py").write_text(_function("a", 100) + _function("b", 40))
    assert ratchet.main(["--root", str(root), "--update"]) == 0
    assert _baseline(root)["functions"] == {"src/forge/m.py::a": 100}


def test_accept_records_a_reviewed_move(tmp_path, capsys):
    root = write_tree(tmp_path, {"src/forge/old.py": _function("big", 90)})
    _init(root)
    (root / "src/forge/old.py").unlink()
    (root / "src/forge/new.py").write_text(_function("big", 90))
    assert _check(root) == 1
    moved = str(root / "src/forge/new.py")
    assert ratchet.main(["--root", str(root), "--update", "--accept", moved]) == 0
    assert "accepted src/forge/new.py::big (90 lines)" in capsys.readouterr().out
    assert _baseline(root)["functions"] == {"src/forge/new.py::big": 90}
    assert _check(root) == 0


def test_accept_never_raises_existing_debt(tmp_path, capsys):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("big", 90)})
    _init(root)
    (root / "src/forge/m.py").write_text(_function("big", 150))
    assert ratchet.main(["--root", str(root), "--update", "--accept", "src/forge/m.py"]) == 2
    assert "nothing new to accept in src/forge/m.py" in capsys.readouterr().out
    assert _baseline(root)["functions"] == {"src/forge/m.py::big": 90}


def test_accept_requires_update(tmp_path):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("f", 10)})
    _init(root)
    with pytest.raises(SystemExit):
        ratchet.main(["--root", str(root), "--accept", "src/forge/m.py"])


def test_init_refuses_to_overwrite_the_baseline(tmp_path):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("big", 90)})
    _init(root)
    (root / "src/forge/m.py").write_text(_function("big", 150))
    assert ratchet.main(["--root", str(root), "--init"]) == 2
    assert _baseline(root)["functions"] == {"src/forge/m.py::big": 90}
    assert ratchet.main(["--root", str(root), "--init", "--force"]) == 0


def test_only_the_effects_layer_may_name_the_write_port(tmp_path, capsys):
    root = write_tree(
        tmp_path,
        {
            "src/forge/ports.py": "class WritePort: ...\n",
            "src/forge/engine/effects.py": "from forge.ports import WritePort\n",
            "src/forge/engine/handlers.py": "import os\nfrom forge.ports import WritePort\n",
        },
    )
    _init(root)
    assert _check(root) == 1
    out = capsys.readouterr().out
    assert "src/forge/engine/handlers.py:2 names WritePort" in out
    assert "effects.py:1 names WritePort" not in out
