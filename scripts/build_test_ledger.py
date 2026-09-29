"""Generate the test disposition ledger (ROADMAP P0-13).

One row per test file under ``tests/``. Measured columns are recomputed on
every run; the curated columns (``disposition``, ``phase``, ``note``)
survive regeneration.

Default dispositions, a starting point for the owner:

- ``drop-with-code``: everything the test is about (its imports minus shared
  models and config) is deleted in Phase 1 (``PLANNED_DELETIONS``) or is
  reached only by tests or by tooling that goes; ``phase`` names the tasks;
- ``port-engine``: the test drives a lifecycle service or a production entry;
- ``port-adapter``: the test drives a provider client;
- ``keep``: everything else.

Invariant evidence and edge-case pins are never ``drop-*`` by default and
carry a note. ``deleted_by`` lists the Phase 1 tasks whose deletions a test
imports; a row that imports deleted code without defaulting to drop gets the
note "partly deleted: review". Warnings on stderr name
curated rows whose file vanished and protected rows marked ``drop-*``.

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
    "area",
    "disposition",
    "phase",
    "note",
)
CURATED = ("disposition", "phase", "note")
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
)
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
INVARIANT_REGISTRY = Path("tests/invariants/registry.py")
EDGE_CASE_REGISTER = Path("docs/reference/edge-cases.md")
PIN_ROW = re.compile(r"^\| EC-\d{2} \|(?P<rest>.*)\|$")
PIN = re.compile(r"`([^`]+)`")


def _within(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def deleted_by(name: str) -> str | None:
    """The Phase 1 task that deletes ``name``, if any."""
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
    """Test files that are invariant evidence or edge-case pins, with the reasons."""
    reasons: dict[str, list[str]] = {}
    registry = root / INVARIANT_REGISTRY
    if registry.is_file():
        for rel in runpy.run_path(str(registry)).get("INVARIANT_FILES", ()):
            reasons.setdefault(rel, []).append("invariant evidence")
    register = root / EDGE_CASE_REGISTER
    if register.is_file():
        for line in register.read_text(encoding="utf-8").splitlines():
            if match := PIN_ROW.match(line.rstrip()):
                for pin in PIN.findall(match["rest"].rsplit("|", 1)[-1]):
                    reasons.setdefault(pin.split("::", 1)[0], []).append("edge-case pin")
    return {rel: "; ".join(dict.fromkeys(why)) for rel, why in reasons.items()}


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


def _droppable(root: Path, modules: dict[str, cb.Module], reach: cb.Reachability) -> set[str]:
    planned = {name for name in modules if deleted_by(name)}
    kept_roots = {*KEPT_TOOLING_ROOTS, *cb.console_script_modules(root)}
    kept = cb.reachable(kept_roots, cb.import_graph(modules)) - planned
    return planned | reach.tests_only | (reach.tooling_only - kept)


def build_rows(
    root: Path, previous: dict[str, dict[str, str]]
) -> tuple[list[dict[str, str]], list[str]]:
    modules = cb.discover_modules(root)
    reach = cb.classify(root, modules)
    droppable = _droppable(root, modules, reach)
    protected = protected_files(root)
    rows, warnings = [], []
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
        reason = protected.get(rel, "")
        if reason and disposition.startswith("drop"):
            disposition, phase = "keep", "-"
        deleted = ",".join(sorted({t for m in imported if (t := deleted_by(m))}))
        review = "partly deleted: review" if deleted and not disposition.startswith("drop") else ""
        row = {
            "file": rel,
            "tests": str(count_tests(path)),
            "loc": str(cb.count_lines(path)),
            "imports_runtime": str(bool(imported & reach.runtime)).lower(),
            "imports_tests_only": str(bool(imported & reach.tests_only)).lower(),
            "deleted_by": deleted,
            "area": area(rel),
            "disposition": disposition,
            "phase": phase,
            "note": "; ".join(filter(None, (reason, review))),
        }
        if rel in previous:
            row.update({key: previous[rel][key] for key in CURATED})
            row["note"] = row["note"] or "; ".join(filter(None, (reason, review)))
        if reason and row["disposition"].startswith("drop"):
            warnings.append(f"{rel} is {reason} but marked {row['disposition']}")
        rows.append(row)
    current = {row["file"] for row in rows}
    for rel, old in sorted(previous.items()):
        if rel not in current:
            warnings.append(
                f"{rel} vanished; its row ({old['disposition']}: {old['note']}) is dropped"
            )
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
