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


def _run(root) -> dict[str, dict[str, str]]:
    (root / ledger.LEDGER).parent.mkdir(parents=True, exist_ok=True)
    assert ledger.main(["--root", str(root)]) == 0
    with (root / ledger.LEDGER).open(encoding="utf-8", newline="") as handle:
        return {row["file"]: row for row in csv.DictReader(handle)}


def _curate(root, file: str, **values: str) -> None:
    path = root / ledger.LEDGER
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        if row["file"] == file:
            row.update(values)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ledger.FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def test_default_dispositions(tmp_path):
    rows = _run(write_tree(tmp_path, TREE))
    assert rows["tests/test_core.py"]["disposition"] == "keep"
    assert rows["tests/test_only.py"]["disposition"] == "drop-with-code"
    assert rows["tests/test_only.py"]["phase"] == "P1"
    assert rows["tests/test_only.py"]["tests"] == "1"


def test_planned_deletions_drive_drop_phase_and_deleted_by(tmp_path):
    tree = {
        **TREE,
        "src/forge/main.py": "from forge import core\nfrom forge.integrations import azure\n",
        "src/forge/config.py": "",
        "src/forge/integrations/__init__.py": "",
        "src/forge/integrations/azure.py": "",
        "tests/test_azure.py": "from forge import config\nfrom forge.integrations import azure\n",
        "tests/test_mixed.py": "from forge import core\nfrom forge.integrations import azure\n",
    }
    rows = _run(write_tree(tmp_path, tree))
    azure = rows["tests/test_azure.py"]
    assert (azure["disposition"], azure["phase"], azure["deleted_by"]) == (
        "drop-with-code",
        "P1-11",
        "P1-11",
    )
    mixed = rows["tests/test_mixed.py"]
    assert (mixed["disposition"], mixed["deleted_by"]) == ("keep", "P1-11")
    assert (mixed["review"], mixed["note"]) == ("partly deleted", "")


def test_port_rules(tmp_path):
    tree = {
        **TREE,
        "src/forge/runs/__init__.py": "",
        "src/forge/runs/service.py": "",
        "src/forge/gitlab/__init__.py": "",
        "src/forge/gitlab/client.py": "",
        "src/forge/gitlab/schemas.py": "",
        "src/forge/main.py": (
            "from forge import core\nfrom forge.runs import service\n"
            "from forge.gitlab import client, schemas\n"
        ),
        "tests/test_service.py": "from forge.runs import service\n",
        "tests/test_client.py": "from forge.gitlab import client\n",
        "tests/test_schema.py": "from forge.gitlab import schemas\n",
        "tests/production_entry/test_trace.py": "from forge import core\n",
    }
    rows = _run(write_tree(tmp_path, tree))
    assert rows["tests/test_service.py"]["disposition"] == "port-engine"
    assert rows["tests/production_entry/test_trace.py"]["disposition"] == "port-engine"
    assert rows["tests/test_client.py"]["disposition"] == "port-adapter"
    assert rows["tests/test_schema.py"]["disposition"] == "keep"


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
        "tests/invariants/registry.py": 'INVARIANT_FILES = frozenset({"tests/test_only.py"})\n',
        "tests/invariants/test_suite.py": "from forge import only_tests\n",
        "docs/reference/edge-cases.md": "| EC-01 | a | b | c | `tests/test_pinned.py::test_x` |\n",
    }
    rows = _run(write_tree(tmp_path, tree))
    names = (
        "tests/test_doctor.py",
        "tests/test_cli.py",
        "tests/test_only.py",
        "tests/test_pinned.py",
        "tests/invariants/test_suite.py",
    )
    assert {name: rows[name]["disposition"] for name in names} == dict.fromkeys(names, "keep")
    assert rows["tests/test_only.py"]["protected"] == "invariant evidence"
    assert rows["tests/test_pinned.py"]["protected"] == "edge-case pin"
    assert rows["tests/invariants/test_suite.py"]["protected"] == "invariant suite"
    assert rows["tests/test_only.py"]["note"] == ""


def test_moved_modules_scripts_and_orphans_are_reviewed_not_guessed(tmp_path):
    tree = {
        **TREE,
        "src/forge/main.py": (
            "from forge import core\nfrom forge.orchestrator import orchestrator, project_config\n"
        ),
        "src/forge/orchestrator/__init__.py": "",
        "src/forge/orchestrator/orchestrator.py": "from forge.stores import conversation\n",
        "src/forge/orchestrator/project_config.py": "",
        "src/forge/stores/__init__.py": "",
        "src/forge/stores/conversation.py": "",
        "tests/test_config.py": "from forge.orchestrator import project_config\n",
        "tests/test_orchestrator.py": "from forge.orchestrator import orchestrator\n",
        "tests/test_store.py": "from forge.stores import conversation\n",
        "tests/test_gate.py": "from forge import core\nfrom scripts.run_live_qualification import main\n",
    }
    rows = _run(write_tree(tmp_path, tree))
    config = rows["tests/test_config.py"]
    assert (config["disposition"], config["deleted_by"]) == ("keep", "")
    orchestrator = rows["tests/test_orchestrator.py"]
    assert (orchestrator["disposition"], orchestrator["phase"]) == ("drop-with-code", "P1-12")
    assert rows["tests/test_store.py"]["review"] == "orphaned by Phase 1"
    gate = rows["tests/test_gate.py"]
    assert (gate["deleted_by"], gate["review"]) == ("P1-07", "partly deleted")


def test_curated_columns_survive_regeneration(tmp_path):
    root = write_tree(tmp_path, TREE)
    _run(root)
    _curate(root, "tests/test_core.py", disposition="port-engine", phase="P3", note="owner")
    kept = _run(root)["tests/test_core.py"]
    assert (kept["disposition"], kept["phase"], kept["note"]) == ("port-engine", "P3", "owner")


def test_warnings_for_vanished_protected_and_unknown_rows(tmp_path, capsys):
    tree = {
        **TREE,
        "tests/invariants/registry.py": 'INVARIANT_FILES = {"tests/test_only.py"}\n',
        "tests/test_misc.py": "from forge import core\n",
    }
    root = write_tree(tmp_path, tree)
    _run(root)
    _curate(root, "tests/test_only.py", disposition="drop-obsolete")
    _curate(root, "tests/test_core.py", note="owner decision")
    _curate(root, "tests/test_misc.py", disposition="maybe")
    (root / "tests/test_core.py").unlink()
    _run(root)
    err = capsys.readouterr().err
    assert "tests/test_only.py is invariant evidence but marked drop-obsolete" in err
    assert "tests/test_core.py vanished while marked keep (owner decision)" in err
    assert "tests/test_misc.py: unknown disposition 'maybe'" in err


def test_the_csv_uses_lf_line_endings(tmp_path):
    root = write_tree(tmp_path, TREE)
    _run(root)
    assert b"\r\n" not in (root / ledger.LEDGER).read_bytes()
