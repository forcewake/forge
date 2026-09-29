"""scripts/ratchet.py: oversized code may only shrink; new code stays small."""

from __future__ import annotations

import json
from pathlib import Path

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


def test_small_code_needs_no_baseline_entry(tmp_path):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("f", 10)})
    _init(root)
    assert _baseline(root) == {"files": {}, "functions": {}}
    assert ratchet.main(["--root", str(root)]) == 0


def test_oversized_function_is_recorded_and_may_not_grow(tmp_path, capsys):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("big", 90)})
    _init(root)
    assert _baseline(root)["functions"] == {"src/forge/m.py::big": 90}
    (root / "src/forge/m.py").write_text(_function("big", 91))
    assert ratchet.main(["--root", str(root)]) == 1
    assert "src/forge/m.py::big grew: 91 lines (allowed 90)" in capsys.readouterr().out


def test_new_function_over_the_limit_fails(tmp_path, capsys):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("f", 10)})
    _init(root)
    (root / "src/forge/n.py").write_text(_function("huge", 81))
    assert ratchet.main(["--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "src/forge/n.py::huge exceeds the limit: 81 lines (allowed 80)" in out


def test_small_function_may_grow_up_to_the_limit(tmp_path):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("f", 10)})
    _init(root)
    (root / "src/forge/m.py").write_text(_function("f", 80))
    assert ratchet.main(["--root", str(root)]) == 0


def test_update_lowers_shrunk_debt_and_drops_repaid_entries(tmp_path):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("a", 120) + _function("b", 90)})
    _init(root)
    (root / "src/forge/m.py").write_text(_function("a", 100) + _function("b", 40))
    assert ratchet.main(["--root", str(root), "--update"]) == 0
    assert _baseline(root)["functions"] == {"src/forge/m.py::a": 100}


def test_accept_records_a_reviewed_move(tmp_path):
    root = write_tree(tmp_path, {"src/forge/old.py": _function("big", 90)})
    _init(root)
    (root / "src/forge/old.py").unlink()
    (root / "src/forge/new.py").write_text(_function("big", 90))
    assert ratchet.main(["--root", str(root)]) == 1
    assert ratchet.main(["--root", str(root), "--update", "--accept", "src/forge/new.py"]) == 0
    assert ratchet.main(["--root", str(root)]) == 0


def test_only_the_effects_layer_may_name_the_write_port(tmp_path, capsys):
    root = write_tree(
        tmp_path,
        {
            "src/forge/ports.py": "class WritePort: ...\n",
            "src/forge/engine/effects.py": "from forge.ports import WritePort\n",
            "src/forge/engine/handlers.py": "from forge.ports import WritePort\n",
        },
    )
    _init(root)
    assert ratchet.main(["--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "src/forge/engine/handlers.py names WritePort" in out
    assert "effects.py names WritePort" not in out
