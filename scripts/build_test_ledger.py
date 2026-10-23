"""Generate the test disposition ledger (ROADMAP P0-13).

One row per test file under ``tests/``. Measured columns are recomputed on
every run; the curated columns (``disposition``, ``phase``, ``note``)
survive regeneration and belong to the owner.

Default dispositions, a starting point for the owner:

- ``drop-with-code``: everything the test is about (its imports minus shared
  models and config) is deleted in Phase 1 (``PLANNED_DELETIONS``) or is
  reached only by tests or by tooling that goes; ``phase`` names the tasks;
- ``port-engine``: the test drives a lifecycle service or a production entry;
- ``port-adapter``: the test drives a provider client;
- ``keep``: everything else.

Measured columns that guide the review: ``deleted_by`` (Phase 1 tasks whose
deletions the test imports, including scripts that P1-07 deletes),
``protected`` (invariant suite, invariant evidence or edge-case pin; never
``drop-*`` by default) and ``review`` ("partly deleted" when the test
imports deleted code without defaulting to drop; "orphaned by Phase 1" when
it imports runtime code that loses its last importer). Warnings on stderr
name unknown dispositions, protected rows marked ``drop-*`` and non-drop
rows whose file vanished.

Usage::

    uv run python scripts/build_test_ledger.py
"""

from __future__ import annotations

import argparse
import ast
import csv
import re
import runpy
import sys
from dataclasses import dataclass
from pathlib import Path

import _codebase as cb

LEDGER = Path("docs/roadmap/2026-09-29-simplification/test-ledger.csv")
FIELDS = (
    "file",
    "tests",
    "loc",
    "imports_runtime",
    "imports_tests_only",
    "deleted_by",
    "protected",
    "review",
    "area",
    "disposition",
    "phase",
    "note",
)
CURATED = ("disposition", "phase", "note")
DISPOSITIONS = frozenset({"keep", "port-engine", "port-adapter", "drop-with-code", "drop-obsolete"})
SERVICES = ("forge.runs.service", "forge.runs.github_service")
PROVIDER_CLIENTS = (
    "forge.gitlab.client",
    "forge.integrations.github",
    "forge.integrations.github_flow",
    "forge.repository.writer",
)
# Imported by many tests without being what they test.
SHARED = (
    "forge.config",
    "forge.database",
    "forge.models",
    "forge.durable.models",
    "forge.adaptive.models",
    "forge.runs.spec",
    "forge.gitlab.schemas",
    "forge.gitlab.events",
    "forge.utils",
    "forge.project_config",
)
# Moved by P1-01 rather than deleted with its package.
MOVED = ("forge.orchestrator.project_config",)
KEPT_TOOLING_ROOTS = ("forge.doctor",)
# Module (or package) -> the ROADMAP Phase 1 task that deletes it.
PLANNED_DELETIONS: dict[str, str] = {
    **dict.fromkeys(
        (
            "forge.adaptive.publication_saga",
            "forge.adaptive.saga_durable",
            "forge.adaptive.saga_native",
            "forge.adaptive.two_writer_qualification",
        ),
        "P1-02",
    ),
    **dict.fromkeys(
        (
            "forge.adaptive.workpackage_service",
            "forge.adaptive.runtime",
            "forge.adaptive.compat",
            "forge.adaptive.compat_fixtures",
            "forge.adaptive.dedup",
            "forge.adaptive.snapshots",
            "forge.adaptive.impact",
            "forge.adaptive.project_map",
            "forge.adaptive.read_guards",
            "forge.adaptive.benchmark",
            "forge.adaptive.system_manifest",
            "forge.adaptive.system_verification",
            "forge.adaptive.executed_evidence",
            "forge.adaptive.independent_checks",
            "forge.adaptive.verification_executor",
            "forge.adaptive.dotnet_recipe",
            "forge.runs.conformance",
            "forge.agents.classifier",
        ),
        "P1-03",
    ),
    "forge.adaptive.reference": "P1-04",
    "forge.adaptive.boundary_registry": "P1-05",
    "forge.adaptive.closure_matrix": "P1-06",
    **dict.fromkeys(
        (
            "forge.release_manifest",
            "forge.release_promotion",
            "forge.profile_qualification",
            "forge.capability_manifest",
            "forge.adaptive.ops_drills",
            "forge.adaptive.delivery_economics",
            "forge.adaptive.research_cohort",
            "forge.adaptive.research_cohort_live",
            "forge.adaptive.pilot",
            "forge.adaptive.pilot_ladder",
            "forge.adaptive.checkpoint_migration",
            "forge.adaptive.steering_causality",
            "forge.adaptive.discovery_profile",
            "forge.adaptive.discovery_authority",
            "forge.adaptive.support_matrix",
        ),
        "P1-07",
    ),
    "forge.factory.implementer": "P1-10",
    **dict.fromkeys(
        (
            "forge.runs.azure_service",
            "forge.integrations.azure",
            "forge.gateway.azure_webhook",
            "forge.reactive.azure_review",
            "forge.reactive.azure_ci_debug",
            "forge.execution.azure_pipelines",
        ),
        "P1-11",
    ),
    **dict.fromkeys(
        (
            "forge.orchestrator",
            "forge.flows",
            "forge.agents",
            "forge.llm",
            "forge.mcp_client",
            "forge.context.engine",
            "forge.context.diff_parser",
            "forge.context.log_parser",
            "forge.context.security_report",
            "forge.context.token_counter",
        ),
        "P1-12",
    ),
    **dict.fromkeys(("forge.reactive.ci_debug", "forge.findings", "forge.mcp_server"), "P1-13"),
    **dict.fromkeys(
        (
            "forge.adaptive.support_bundle",
            "forge.adaptive.audit_export",
            "forge.adaptive.delivery_measurement",
            "forge.adaptive.delivery_metrics",
            "forge.adaptive.qualification",
            "forge.adaptive.ops_limits",
        ),
        "P1-15",
    ),
}
# Scripts that P1-07 deletes; the program's own scripts stay.
PLANNED_SCRIPT_DELETIONS = re.compile(
    r"run_\w+|build_(?:accepted_task_ledger|budget_calibration|economics_report|lane_closure)"
    r"|freeze_supported_profile|generate_template_pins|gate_conformance|qualify_gitlab_ce"
    r"|cold_install_\w+"
)
INVARIANT_REGISTRY = Path("tests/invariants/registry.py")
EDGE_CASE_REGISTER = Path("docs/reference/edge-cases.md")
PIN_ROW = re.compile(r"^\| EC-\d{2} \|(?P<rest>.*)\|$")
PIN = re.compile(r"`([^`]+)`")


def _within(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def deleted_by(name: str) -> str | None:
    """The Phase 1 task that deletes ``name``, if any."""
    if _within(name, MOVED):
        return None
    owners = [p for p in PLANNED_DELETIONS if name == p or name.startswith(p + ".")]
    return PLANNED_DELETIONS[max(owners, key=len)] if owners else None


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
    """Test files the invariant suite or the edge-case register depend on, with reasons."""
    reasons: dict[str, list[str]] = {}
    for path in sorted((root / "tests" / "invariants").glob("test*.py")):
        reasons.setdefault(path.relative_to(root).as_posix(), []).append("invariant suite")
    registry = root / INVARIANT_REGISTRY
    if registry.is_file():
        for rel in runpy.run_path(str(registry))["INVARIANT_FILES"]:
            reasons.setdefault(rel, []).append("invariant evidence")
    register = root / EDGE_CASE_REGISTER
    if register.is_file():
        for line in register.read_text(encoding="utf-8").splitlines():
            if match := PIN_ROW.match(line.rstrip()):
                for pin in PIN.findall(match["rest"].rsplit("|", 1)[-1]):
                    reasons.setdefault(pin.split("::", 1)[0], []).append("edge-case pin")
    return {rel: "; ".join(dict.fromkeys(why)) for rel, why in reasons.items()}


def loads_deleted_script(tree: ast.Module) -> bool:
    """True when a test imports, or names the file of, a script that P1-07 deletes."""
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.endswith(".py") and "/" not in node.value:
                names = ["scripts." + node.value.removesuffix(".py")]
        for name in names:
            head, _, script = name.partition(".")
            if head == "scripts" and PLANNED_SCRIPT_DELETIONS.fullmatch(script):
                return True
    return False


def default_disposition(rel: str, imported: set[str], droppable: set[str]) -> tuple[str, str]:
    """A starting point for the owner to curate — never the final word."""
    about = {m for m in imported if not _within(m, SHARED)} or imported
    if about and about <= droppable:
        tasks = sorted({task for m in about if (task := deleted_by(m))})
        return "drop-with-code", ",".join(tasks) or "P1"
    if any(_within(m, SERVICES) for m in imported) or "production_entry" in rel:
        return "port-engine", "P3"
    if any(_within(m, PROVIDER_CLIENTS) for m in imported):
        return "port-adapter", "P3"
    return "keep", "-"


@dataclass(frozen=True)
class Context:
    modules: dict[str, cb.Module]
    reach: cb.Reachability
    droppable: set[str]
    orphaned: set[str]
    protected: dict[str, str]


def analyse(root: Path) -> Context:
    modules = cb.discover_modules(root)
    reach = cb.classify(root, modules)
    graph = cb.import_graph(modules)
    planned = {name for name in modules if deleted_by(name)}
    kept_roots = {*KEPT_TOOLING_ROOTS, *cb.console_script_modules(root)}
    kept = cb.reachable(kept_roots, graph) - planned
    pruned = {name: deps - planned for name, deps in graph.items() if name not in planned}
    roots = (*cb.CONTROL_PLANE_ROOTS, *cb.LANE_ROOTS)
    orphaned = reach.runtime - planned - cb.reachable(roots, pruned)
    droppable = planned | reach.tests_only | (reach.tooling_only - kept)
    return Context(modules, reach, droppable, orphaned, protected_files(root))


def measure(root: Path, path: Path, ctx: Context) -> dict[str, str]:
    rel = path.relative_to(root).as_posix()
    imported = {
        m
        for m in cb.imports_of(path, ctx.modules)
        if m.startswith("forge") and ctx.modules[m].path.name != "__init__.py"
    }
    tasks = {task for m in imported if (task := deleted_by(m))}
    if loads_deleted_script(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        tasks.add("P1-07")
    disposition, phase = default_disposition(rel, imported, ctx.droppable)
    protected = ctx.protected.get(rel, "")
    if protected and disposition.startswith("drop"):
        disposition, phase = "keep", "-"
    review = []
    if tasks and not disposition.startswith("drop"):
        review.append("partly deleted")
    if imported & ctx.orphaned:
        review.append("orphaned by Phase 1")
    return {
        "file": rel,
        "tests": str(count_tests(path)),
        "loc": str(cb.count_lines(path)),
        "imports_runtime": str(bool(imported & ctx.reach.runtime)).lower(),
        "imports_tests_only": str(bool(imported & ctx.reach.tests_only)).lower(),
        "deleted_by": ",".join(sorted(tasks)),
        "protected": protected,
        "review": ", ".join(review),
        "area": area(rel),
        "disposition": disposition,
        "phase": phase,
        "note": "",
    }


def build_rows(
    root: Path, previous: dict[str, dict[str, str]]
) -> tuple[list[dict[str, str]], list[str]]:
    ctx = analyse(root)
    rows, warnings = [], []
    for path in sorted((root / "tests").rglob("test*.py")):
        if "__pycache__" in path.parts:
            continue
        row = measure(root, path, ctx)
        if row["file"] in previous:
            row.update({key: previous[row["file"]][key] for key in CURATED})
        if row["disposition"] not in DISPOSITIONS:
            warnings.append(f"{row['file']}: unknown disposition {row['disposition']!r}")
        if row["protected"] and row["disposition"].startswith("drop"):
            warnings.append(f"{row['file']} is {row['protected']} but marked {row['disposition']}")
        rows.append(row)
    current = {row["file"] for row in rows}
    for rel, old in sorted(previous.items()):
        if rel not in current and not old["disposition"].startswith("drop"):
            warnings.append(f"{rel} vanished while marked {old['disposition']} ({old['note']})")
    return rows, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=cb.repo_root())
    args = parser.parse_args(argv)
    target = args.root / LEDGER
    previous: dict[str, dict[str, str]] = {}
    if target.exists():
        with target.open(encoding="utf-8", newline="") as handle:
            previous = {row["file"]: row for row in csv.DictReader(handle)}
    rows, warnings = build_rows(args.root, previous)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    for warning in warnings:
        print(f"test-ledger: warning: {warning}", file=sys.stderr)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["disposition"]] = counts.get(row["disposition"], 0) + 1
    print(f"test-ledger: {len(rows)} files → {LEDGER} {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
