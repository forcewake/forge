#!/usr/bin/env python3
"""R40-12 (#348) — the cold-install kit's RESTORE REHEARSAL.

One data-bearing restore drill, on DISPOSABLE data only, verifying
work / checkpoint / pin ("approved resume spec" — the native intent
artifact) consistency BEFORE dispatch resumes. This is the machinery
the second engineer repeats after any restore; it references the
existing drill implementations in :mod:`forge.adaptive.ops_drills`
verbatim — nothing here re-implements a backup, a restore or a gate:

- ``drill_backup_restore`` — backups and restores metadata AND blobs
  together on a seeded disposable fixture; the SELECTED active
  checkpoint of each work and the PINNED checkpoint recover with a
  VERIFIED read (every blob re-hashed on the way out); the mismatched
  halves (t2 metadata + t1 blobs) are DETECTED before any restore and
  the restore REFUSES.
- ``drill_mismatched_restore_preflight`` — the restore preflight's
  ordering on disposable installations: the mismatched snapshot and the
  wrong-schema installation both refuse TYPED with ZERO model turns
  spent; only the consistent snapshot at the frozen profile's schema
  head restores, and only then may the resume dispatch (the first new
  model turn) run.
- ``drill_workflow_restore`` (R40-15 / #351, extended here because the
  SCHEMA now carries the selected workflow's own rows) — the
  data-bearing restore over the REAL workflow shape: a ready parent, a
  #338 ``review_rounds`` row with its child run, an applied #340
  ``budget_amendments`` row, a #341/#343 operation grant with its JOINED
  redemption receipt, an open draining lease and pinned checkpoints.
  Work + checkpoint + NATIVE-INTENT consistency — the round and
  amendment rows included — is VERIFIED before the dispatch gate may
  open; a restore that drops the round rows refuses with ZERO model
  turns.

The profile binding row is built from the MANIFEST'S RECORDED
executed-lab observation — the cold-install kit installs from THIS
manifest, so the manifest is the normative source here. A LIVE
deployment bind (read-only podman inspect of the running consumers) is
``scripts/run_deployment_ops.py``'s domain and is deliberately not
repeated by this rehearsal (this window the shared lab is another
issue's; this script touches NOTHING shared).

Usage (from the repository root):

    uv run python scripts/cold_install_restore_rehearsal.py --report /tmp/restore.json

Exit codes: 0 the drills passed · 1 a drill failed or a precondition
refused.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from cold_install_check import (  # noqa: E402
    CheckRefused,
    revision_two_behind,
    load_manifest,
)
from forge.adaptive.ops_drills import (  # noqa: E402
    drill_mismatched_restore_preflight,
    drill_workflow_restore,
    profile_binding_row,
    run_drill,
)

REPORT_SCHEMA = "forge.cold.restore-rehearsal/1"


async def _rehearse(manifest: dict[str, Any], work_root: Path) -> dict[str, Any]:
    head = str(manifest["control_plane"]["schema_revision"]["head"])
    n2 = revision_two_behind(manifest)
    # The binding row: the manifest's RECORDED executed-lab observation
    # (named as such — never presented as a live probe).
    executed = dict(manifest["control_plane"]["executed_lab"])
    observed = {
        "image_name": executed.get("image_name", ""),
        "image_id": executed.get("image_id", ""),
        "image_digest": executed.get("image_digest", ""),
        "schema_head": executed.get("deployed_schema_head", ""),
        "reported_version": executed.get("reported_version", ""),
    }
    binding = profile_binding_row(manifest, observed)
    documents = []
    backup_doc = await run_drill("backup_restore", work_root / "backup-restore")
    documents.append(backup_doc.as_document())
    preflight_doc = await drill_mismatched_restore_preflight(
        work_root / "preflight",
        profile_binding=binding,
        expected_schema_head=head,
        mismatched_schema_head=n2,
    )
    documents.append(preflight_doc.as_document())
    # R40-15 (#351): the workflow-shaped rows the schema now carries —
    # review_rounds (031), budget_amendments, operation_grants and
    # credential_redemptions — survive the SAME restore discipline.
    workflow_doc = await drill_workflow_restore(
        work_root / "workflow-restore",
        profile_binding=binding,
        expected_schema_head=head,
        mismatched_schema_head=n2,
    )
    documents.append(workflow_doc.as_document())
    for document in documents:
        for objective in document["achieved_objectives"]:
            print(f"   [ok] {objective}")
        for violation in document["violations"]:
            print(f"   [VIOLATION] {violation}")
        print(f"   {document['drill']}: {document['outcome']}")
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "manifest_digest": manifest["manifest_digest"],
        "expected_schema_head": head,
        "mismatched_schema_head": n2,
        "profile_binding_source": (
            "the manifest's RECORDED executed-lab observation (the kit installs from this "
            "manifest; a live deployment bind is run_deployment_ops.py's domain, not repeated)"
        ),
        "profile_binding": binding,
        "drills": documents,
        "restore_gate": dict(
            next(
                document["signals"]["preflight.restore_gate"]
                for document in documents
                if "preflight.restore_gate" in document.get("signals", {})
            )
        ),
        "workflow_restore_gate": dict(
            documents[-1].get("signals", {}).get("recovery.rto_observed", {})
        ),
        "summary": {
            "drills_run": len(documents),
            "passed": sum(1 for d in documents if d["outcome"] == "pass"),
            "failed": sum(1 for d in documents if d["outcome"] == "fail"),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/cold_install_restore_rehearsal.py",
        description=(
            "R40-12 (#348): the cold-install kit's data-bearing restore rehearsal on "
            "DISPOSABLE data — works/checkpoints/pins consistency, the mismatched-halves "
            "detection, and the restore preflight's zero-model-turn ordering before the "
            "dispatch gate opens."
        ),
    )
    parser.add_argument(
        "--report", type=Path, default=None, help="write the JSON report to this path"
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep the disposable work directory (print its path)"
    )
    args = parser.parse_args(argv)
    work_root = Path(tempfile.mkdtemp(prefix="forge-cold-restore-"))
    try:
        manifest = load_manifest()
        report = asyncio.run(_rehearse(manifest, work_root))
    except CheckRefused as error:
        print(f"cold_install_restore_rehearsal: REFUSED: {error}", file=sys.stderr)
        return 1
    finally:
        if args.keep:
            print(f"work directory kept: {work_root}")
        else:
            import shutil

            shutil.rmtree(work_root, ignore_errors=True)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"cold_install_restore_rehearsal: report written to {args.report}")
    print(
        f"summary: {report['summary']['passed']}/{report['summary']['drills_run']} drills passed "
        f"({report['summary']['failed']} failed)"
    )
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
