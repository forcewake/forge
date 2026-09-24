#!/usr/bin/env python3
"""The R36-21 operational-drill runner (issue #280).

What this runner proves, and refuses to prove:

- Each drill from :mod:`forge.adaptive.ops_drills` EXECUTED against a
  fresh disposable fixture (real sqlite by default, or a real
  PostgreSQL through ``--db-url``) — the invariants of capacity,
  bounded overload, lock-wait bounds, control responsiveness, degraded
  mode, backup/restore and the override audit, with per-drill
  achieved objectives, tested limits and signals in the report.
- Nothing is generalized: the report carries the drill scope sentence
  (disposable single-database fixtures, synthetic bounded workloads,
  the exact N/budget/lock-wait recorded per drill) — it is NOT a fleet
  throughput claim and does not substitute for measuring a real
  deployment.

Usage:

    uv run python scripts/run_ops_drills.py --drill all --report /tmp/ops-drills.json
    uv run python scripts/run_ops_drills.py --drill native_start_load,backup_restore

The PostgreSQL variant (the checkpoint index in ``checkpoint_metadata``
through the real postgres repository) — point ``--db-url`` at a
DISPOSABLE database; the runner creates the schema itself:

    podman exec forge-postgres psql -U forge -d forge \\
        -c 'CREATE DATABASE forge_ops_drills'
    uv run python scripts/run_ops_drills.py --drill all \\
        --db-url postgresql+asyncpg://forge:forge@127.0.0.1:5432/forge_ops_drills \\
        --authority postgres --report /tmp/ops-drills-pg.json

Exit codes: 0 all selected drills passed · 1 a drill failed (or the
drill name was unknown).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from forge.adaptive.ops_drills import (  # noqa: E402
    DRILLS,
    DRILL_SCOPE,
    run_drill,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_ops_drills",
        description="Run the R36-21 operational drills against disposable fixtures.",
    )
    parser.add_argument(
        "--drill",
        default="all",
        help=(f"comma-separated drill names (or 'all'): {', '.join(sorted(DRILLS))}"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="write the JSON report to this path (required for the operating proof)",
    )
    parser.add_argument(
        "--db-url",
        default="",
        help="a DISPOSABLE database URL (default: file-backed sqlite in a temp dir)",
    )
    parser.add_argument(
        "--authority",
        default="filesystem",
        choices=("filesystem", "postgres"),
        help="which checkpoint authority the checkpoint drills compose",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        default=False,
        help="run the CI-sized N per drill (default: each drill's operator sizes)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        default=False,
        help="keep the disposable work directory (print its path; default: clean up)",
    )
    return parser.parse_args(argv)


async def _run_selected(names: list[str], args: argparse.Namespace) -> dict:
    work_root = Path(tempfile.mkdtemp(prefix="forge-ops-drills-"))
    documents = []
    try:
        for name in names:
            print(f"== drill: {name} (fixture {work_root / name})")
            outcome = await run_drill(
                name,
                work_root / name,
                db_url=args.db_url,
                authority=args.authority,
                fast=args.fast,
            )
            document = outcome.as_document()
            documents.append(document)
            for objective in document["achieved_objectives"]:
                print(f"   [ok] {objective}")
            for violation in document["violations"]:
                print(f"   [VIOLATION] {violation}")
            print(f"   outcome: {document['outcome']}")
    finally:
        if args.keep:
            print(f"work directory kept: {work_root}")
        else:
            import shutil

            shutil.rmtree(work_root, ignore_errors=True)
    return {
        "schema": "forge.ops.drills/1",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "drills": documents,
        "database": args.db_url.split("://")[0] if args.db_url else "sqlite",
        "authority": args.authority,
        "profile": "ci-fast" if args.fast else "operator",
        "scope": DRILL_SCOPE,
        "summary": {
            "drills_run": len(documents),
            "passed": sum(1 for d in documents if d["outcome"] == "pass"),
            "failed": sum(1 for d in documents if d["outcome"] == "fail"),
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.drill.strip().lower() == "all":
        names = sorted(DRILLS)
    else:
        names = [name.strip() for name in args.drill.split(",") if name.strip()]
        unknown = [name for name in names if name not in DRILLS]
        if unknown:
            print(f"unknown drill(s): {', '.join(unknown)}", file=sys.stderr)
            print(f"known drills: {', '.join(sorted(DRILLS))}", file=sys.stderr)
            return 1
    report = asyncio.run(_run_selected(names, args))
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(f"report: {args.report}")
    print(
        f"summary: {report['summary']['passed']}/{report['summary']['drills_run']} passed "
        f"({report['summary']['failed']} failed)"
    )
    print(f"scope: {report['scope']}")
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
