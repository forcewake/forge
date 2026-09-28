#!/usr/bin/env python3
"""R42-06 (#379) — the budget calibration report from the EXISTING traces.

Measured BEFORE any default changes (the issue's scope 5): context
expansion, redundant reads, repair counts and successful-correction cost on
the #364/#377 live captures (each lane attempt's SDK receipt — input /
cached / output / cost — plus every lane job's trace window) and the #368
ledger v2 (the review-loop lineage's all-attempt fold, usage coverage and
the unreceipted liability bound).

No model runs, no clocks, no fabrication — the artifact is
byte-deterministic on rerun (sorted keys, no build timestamp, rounding at
emit). The honesty rules this pins:

- usage coverage names the unreceipted attempts (never zero-filled): the
  #364 window folds 7 SDK receipts over 8 attempts (0.875) and BOUNDS the
  $0.223636 residual to the unreceipted wrong-branch child — a bound,
  never a cost column;
- failed attempts and human corrections are NAMED rows with their own
  spend where the SDK receipted them (the calibration is not just the
  successful lane cost);
- the token dimensions keep the provider conventions (#339/#368):
  the claude-sdk-lane receipts are Anthropic-shaped DISJOINT counters, so
  the inclusive input is ``input + cached`` and the cache is added exactly
  once — never double-added on top of an inclusive input;
- the exhaustion event and the 200k→600k amendment are recorded AS
  EVIDENCE (one observed task shape), not promoted to a universal default:
  the artifact carries the recorded profiles with their origin.

Usage::

    uv run python scripts/build_budget_calibration.py \
        --out evaluation/economics/budget-calibration-v1.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCHEMA = "forge.budget.calibration/1"

REVIEW_LOOP_DIR = REPO_ROOT / "docs" / "evaluation" / "2026-09-27-review-loop"
BUILD_ONCE_DIR = REPO_ROOT / "docs" / "evaluation" / "2026-09-28-r4204-build-once"
LEDGER_V2 = REPO_ROOT / "evaluation" / "economics" / "accepted-task-ledger-v2.json"

#: The recorded budget-profile versions this calibration measures under
#: (history never rewritten — a later profile writes NEW evidence):
#: - the runbook default posture (standard 200k) as shipped;
#: - the live lab's receipted amendment, standard 200k→600k, recorded as
#:   ``forge.budget-profiles/1@2026-09-27-align`` (the #364 exhaustion
#:   event's operator resolution — an OBSERVATION about ONE task shape,
#:   never proof every task needs 600k).
RECORDED_PROFILES = {
    "runbook-default@forge-0.42.0": {
        "trivial": {"max_calls": 8, "max_tokens": 40000, "wallclock_s": 900},
        "standard": {"max_calls": 40, "max_tokens": 200000, "wallclock_s": 3600},
        "heavy": {"max_calls": 120, "max_tokens": 600000, "wallclock_s": 10800},
        "origin": (
            "docs/operations/lab-alignment-runbook.md — the shipped default "
            "posture (the 600k token axis exists only on the heavy class)"
        ),
    },
    "forge.budget-profiles/1@2026-09-27-align": {
        "trivial": {"max_calls": 8, "max_tokens": 40000, "wallclock_s": 900},
        "standard": {"max_calls": 40, "max_tokens": 600000, "wallclock_s": 3600},
        "heavy": {"max_calls": 120, "max_tokens": 600000, "wallclock_s": 10800},
        "origin": (
            "the #364 live window's receipted amendment: run fbe62ad5 "
            "exhausted standard@200k (196312/200000 consumed) with the "
            "closing review stood down; the operator raised the deployment's "
            "standard profile to 600k — STILL finite, numerical, enforced. "
            "One observed task shape, not a universal requirement; the "
            "runbook default keeps standard@200k."
        ),
    },
}

_LINE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _r6(value: float) -> float:
    return round(value, 6)


def _trace_seconds(trace_dir: Path, job_id: int) -> float | None:
    """The lane job's wall window from its recorded trace (first→last
    timestamped line) — the honest TIME dimension, a lower bound of the
    job (the runner's own bookkeeping outside the window is not in the
    log)."""
    matches = sorted(trace_dir.glob(f"*job{job_id}.log"))
    if not matches:
        return None
    stamps: list[datetime] = []
    for line in matches[0].read_text(errors="replace").splitlines():
        found = _LINE_TS.match(line)
        if found:
            stamps.append(datetime.fromisoformat(found.group(1).replace("Z", "+00:00")))
    if len(stamps) < 2:
        return None
    return round((stamps[-1] - stamps[0]).total_seconds(), 3)


def _receipt_row(
    *,
    capture: str,
    phase: str,
    run_id: str,
    job_id: int | None,
    usage: dict[str, Any],
    trace_dir: Path | None,
) -> dict[str, Any] | None:
    """One SDK lane receipt under the #339/#368 conventions.

    The claude-sdk-lane receipts are Anthropic-shaped (DISJOINT counters):
    ``input_tokens`` already EXCLUDES the cache, so the spend-bearing
    inclusive input is ``input + cached`` — the cache contributes exactly
    once, never double-added. ``redundant_read_fraction`` is the share of
    the served context that was a re-read of cached conversation
    (``cached / inclusive``) — the context-expansion measure.
    """
    model_usage = usage.get("model_usage") or {}
    model = next(iter(model_usage), "") if isinstance(model_usage, dict) else ""
    entry = model_usage.get(model, {}) if isinstance(model_usage, dict) else {}
    input_tokens = usage.get("input_tokens")
    cached = usage.get("cached_input_tokens")
    cache_write = entry.get("cacheCreationInputTokens", usage.get("cache_write_tokens"))
    output = usage.get("output_tokens")
    if not isinstance(input_tokens, int) and not isinstance(cached, int):
        return None
    inclusive = sum(part for part in (input_tokens, cached, cache_write) if isinstance(part, int))
    total = inclusive + output if isinstance(output, int) else None
    return {
        "capture": capture,
        "phase": phase,
        "run_id": run_id,
        "job_id": job_id,
        "route": str(usage.get("driver") or "claude-sdk-lane"),
        "model": model,
        # the disjoint counters, as the SDK reported them
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_tokens": cache_write if isinstance(cache_write, int) else None,
        "output_tokens": output,
        # the ONE inclusive fold (cache added exactly once)
        "input_tokens_inclusive": inclusive,
        "total_known_tokens": total,
        "redundant_read_fraction": (
            _r6(cached / inclusive) if isinstance(cached, int) and inclusive else None
        ),
        "cost_usd": _r6(float(usage["total_cost_usd"])) if usage.get("total_cost_usd") else None,
        "cost_basis": "sdk-total_cost_usd",
        "completeness": str(usage.get("completeness") or "aggregate"),
        "lane_job_wall_seconds": _trace_seconds(trace_dir, job_id) if job_id else None,
        "anthropic_shaped": True,
    }


def _capture_receipts(capture: str, evidence_path: Path) -> list[dict[str, Any]]:
    evidence = json.loads(evidence_path.read_text())
    trace_dir = evidence_path.parent / "traces"
    rows: list[dict[str, Any]] = []
    phases = evidence.get("phases", {})
    for phase in sorted(phases):
        block = phases[phase]
        items = block if isinstance(block, list) else [block]
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            usage = None
            job_id = None
            run_id = str(item.get("run_id") or "")
            phase_label = phase if isinstance(block, dict) else f"{phase}[{index}]"
            if isinstance(item.get("lane_meta_spend"), dict):
                usage = (item["lane_meta_spend"] or {}).get("usage")
                lane = item.get("lane") or {}
                job_id = lane.get("job_id") if isinstance(lane, dict) else None
            elif isinstance(item.get("usage"), dict):
                # the failed-attempt spelling: the usage block rides directly
                # (the budget-fence attempt's own SDK figure).
                usage = item["usage"]
            if not isinstance(usage, dict):
                continue
            row = _receipt_row(
                capture=capture,
                phase=phase_label,
                run_id=run_id,
                job_id=job_id if isinstance(job_id, int) else None,
                usage=usage,
                trace_dir=trace_dir,
            )
            if row is not None:
                rows.append(row)
    return rows


def _failed_and_correction_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The NAMED failed attempts and human corrections (never only the
    successful lane cost). Sources: the two captures' phase blocks + the
    ledger v2's root-task fold."""
    ledger = json.loads(LEDGER_V2.read_text())
    increments = ledger["observability"]["review_round.incremental_cost"][
        "7319478e10b74f3c90d0e0ea43a9dc3f"
    ]
    corrections: list[dict[str, Any]] = []
    round_outcomes = {
        "2": "oracle green on the corrected candidate; the closing reviewer resolved",
        "3": "worker killed mid-round; the #358 recovery adopted the round's own effect",
        "4": "BLOCKED harness_no_changes — the /fix premise had expired (the honest negative)",
    }
    for round_no in sorted(increments, key=int):
        entry = increments[round_no]
        corrections.append(
            {
                "kind": "human_correction",
                "capture": "2026-09-27-review-loop",
                "root_run_id": "7319478e10b74f3c90d0e0ea43a9dc3f",
                "round": int(round_no),
                "outcome": round_outcomes.get(round_no, ""),
                "incremental_cost_usd": entry.get("provider_reported_usd", {}).get("usd"),
                "basis": "the ledger v2 review_round.incremental_cost fold",
            }
        )
    corrections.append(
        {
            "kind": "human_correction",
            "capture": "2026-09-28-r4204-build-once",
            "root_run_id": "37f94098fabc48cebf2c73f42b56e202",
            "round": 2,
            "outcome": "oracle green on the new candidate; ready_for_human",
            "incremental_cost_usd": 0.140366,
            "basis": "the build-once capture's round2 SDK receipt",
        }
    )
    corrections.append(
        {
            "kind": "human_correction",
            "capture": "2026-09-28-r4204-build-once",
            "root_run_id": "37f94098fabc48cebf2c73f42b56e202",
            "round": 3,
            "outcome": "worker killed mid-round, #358 recovery adopted the round's own effect",
            "incremental_cost_usd": 0.188016,
            "basis": "the build-once capture's round3 SDK receipt",
        }
    )
    failed = [
        {
            "kind": "failed_attempt",
            "capture": "2026-09-27-review-loop",
            "run_id": "165dd1edab724b20b42c916991c53069",
            "outcome": "blocked harness_infrastructure (protected-variable bootstrap fence)",
            "cost_usd": 0.0,
            "cost_basis": "zero model calls — recorded, never inferred",
            "human_repair": (
                "operator re-provisioned the carrier variable masked (the "
                "alignment receipt names the axis)"
            ),
        },
        {
            "kind": "failed_attempt",
            "capture": "2026-09-27-review-loop",
            "run_id": "fbe62ad5ecf946eb8f12caac95692904",
            "outcome": (
                "budget_exhausted 196312/200000 tokens; the closing review "
                "stood down (zero reviewer spend) — the designed fence"
            ),
            "cost_usd": 0.222881,
            "cost_basis": "sdk-total_cost_usd",
            "human_repair": (
                "the receipted amendment forge.budget-profiles/1@2026-09-27-align "
                "(standard.max_tokens 200000→600000) + a fresh run"
            ),
        },
        {
            "kind": "failed_attempt",
            "capture": "2026-09-27-review-loop",
            "run_id": "8be14a80312d47a8b274efaa4d6a0a73",
            "outcome": (
                "ready_for_human but the first /fix classified material_change "
                "(empty allowed_paths — the fail-closed scope classification)"
            ),
            "cost_usd": 0.1876,
            "cost_basis": "sdk-total_cost_usd",
            "human_repair": (
                "seed .forge.yml implement.paths (the repo-side scope declaration) + a fresh run"
            ),
        },
        {
            "kind": "failed_attempt",
            "capture": "2026-09-27-review-loop",
            "run_id": "6e0fdf3448ee4860b2bc34822db9489f",
            "outcome": "blocked external_change (the #361 wrong-branch publisher defect)",
            "cost_usd": None,
            "cost_basis": "unreceipted — its SDK figure never reached the record",
            "human_repair": (
                "the live-found defect was patched in-tree (publish_candidate "
                "branch threading); the spend stays a bounded residual"
            ),
        },
        {
            "kind": "failed_attempt",
            "capture": "2026-09-27-review-loop",
            "run_id": "2396987be7b64895b7b73af273211c91",
            "outcome": (
                "harness_no_changes — the round-4 /fix premise had expired "
                "(the candidate already carried the asked change); zero candidate commits"
            ),
            "cost_usd": 0.239445,
            "cost_basis": "sdk-total_cost_usd",
            "human_repair": "none — the model honestly produced zero changes; recorded",
        },
        {
            "kind": "failed_attempt",
            "capture": "2026-09-28-r4204-build-once",
            "run_id": "fed57de4",
            "outcome": (
                "candidate_rejected: branch_drift — a conflicting human commit "
                "landed mid-lane; the child parked blocked, the human head preserved"
            ),
            "cost_usd": 0.198261,
            "cost_basis": "sdk-total_cost_usd",
            "human_repair": "none — the typed drift fence held; recorded",
        },
    ]
    return failed, corrections


def build_document() -> dict[str, Any]:
    review_loop = _capture_receipts(
        "2026-09-27-review-loop", REVIEW_LOOP_DIR / "live-run-evidence.json"
    )
    build_once = _capture_receipts(
        "2026-09-28-r4204-build-once", BUILD_ONCE_DIR / "live-run-evidence.json"
    )
    receipts = sorted(
        review_loop + build_once,
        key=lambda row: (row["capture"], str(row["job_id"]), row["run_id"], row["phase"]),
    )

    cached_total = sum(row["cached_input_tokens"] or 0 for row in receipts)
    input_total = sum(row["input_tokens"] or 0 for row in receipts)
    inclusive_total = sum(row["input_tokens_inclusive"] or 0 for row in receipts)
    output_total = sum(row["output_tokens"] or 0 for row in receipts)
    cost_total = sum(row["cost_usd"] or 0.0 for row in receipts)
    times = [row["lane_job_wall_seconds"] for row in receipts if row["lane_job_wall_seconds"]]

    ledger = json.loads(LEDGER_V2.read_text())
    coverage = ledger["costs"]["programme"]["coverage"]
    window = ledger["lineage"]["root_tasks"]["7319478e10b74f3c90d0e0ea43a9dc3f"]
    reconciliation = ledger["lineage"]["window_reconciliation"]
    round_budgets = window["budget_window"]["round_budgets"]

    reserve_rows = [
        {
            "run_id": budget["run_id"],
            "round": budget["round_number"],
            "max_tokens": budget["max_tokens"],
            "closing_reserved_tokens": budget["closing_reserved_tokens"],
            "closing_reserved_calls": budget["closing_reserved_calls"],
            "partition_policy": budget["closing_partition_policy"] or None,
            "status": budget["status"],
            "note": (
                "the recorded capture carries limits/status only — no "
                "in-window consumption figure, so the reserve_remaining is "
                "the FROZEN share, never a measured remainder"
                if budget["closing_reserved_tokens"]
                else "unpartitioned (the pre-#340 posture on this window)"
            ),
        }
        for budget in round_budgets
    ]

    failed, corrections = _failed_and_correction_rows()

    return {
        "schema": SCHEMA,
        "inputs": {
            "review_loop_capture": {
                "path": str(REVIEW_LOOP_DIR.relative_to(REPO_ROOT)),
                "sha256": _sha256(REVIEW_LOOP_DIR / "live-run-evidence.json"),
            },
            "build_once_capture": {
                "path": str(BUILD_ONCE_DIR.relative_to(REPO_ROOT)),
                "sha256": _sha256(BUILD_ONCE_DIR / "live-run-evidence.json"),
            },
            "ledger_v2": {
                "path": str(LEDGER_V2.relative_to(REPO_ROOT)),
                "sha256": _sha256(LEDGER_V2),
            },
        },
        "measured": {
            "receipts": receipts,
            "receipt_count": len(receipts),
            "route": "claude-sdk-lane@glm-5.3-flash (Anthropic-shaped DISJOINT counters)",
            "totals": {
                "input_tokens": input_total,
                "cached_input_tokens": cached_total,
                "input_tokens_inclusive": inclusive_total,
                "output_tokens": output_total,
                "cost_usd": _r6(cost_total),
                "lane_job_wall_seconds_lower_bound": round(sum(times), 3) if times else None,
                "lane_job_wall_seconds_mean": _r6(sum(times) / len(times)) if times else None,
            },
            "context_expansion": {
                "redundant_read_fraction_overall": (
                    _r6(cached_total / inclusive_total) if inclusive_total else None
                ),
                "redundant_read_fraction_min": _r6(
                    min(
                        row["redundant_read_fraction"]
                        for row in receipts
                        if row["redundant_read_fraction"] is not None
                    )
                ),
                "redundant_read_fraction_max": _r6(
                    max(
                        row["redundant_read_fraction"]
                        for row in receipts
                        if row["redundant_read_fraction"] is not None
                    )
                ),
                "note": (
                    "every SDK receipt re-served the majority of its context "
                    "from cache (the restored conversation) — the harness "
                    "fills a ~200k-token context regardless of task size; "
                    "the token axis therefore measures CONTEXT, not task "
                    "weight, and a class mapping that ignores it starves "
                    "the closing review (the fbe62ad5 fence)"
                ),
            },
            "usage_coverage": {
                "attempts": coverage["attempts"],
                "receipts": coverage["receipts_received"],
                "coverage": coverage["receipt_coverage"],
                "unresolved_liability_usd": _r6(reconciliation["unreceipted_residual_usd"]),
                "unresolved_liability_attributed_to": reconciliation["attributed_to_runs"],
                "note": (
                    "coverage and the bounded residual come from the ledger "
                    "v2 fold (the same honesty the #368 artifact pins): a "
                    "figure the SDK never reported is a bound, never a "
                    "cost column"
                ),
            },
            "accepted_task_all_attempt_cost": {
                "root_run_id": "7319478e10b74f3c90d0e0ea43a9dc3f",
                "attempts": window["attempts"],
                "provider_reported_usd_exact": 1.256064,
                "basis": "the ledger v2's accepted_work.total_cost_lower_bound fold",
                "human_decision": "pending (draft MR !4)",
            },
            "failed_attempts_and_repairs": failed,
            "human_corrections": corrections,
            "closing_reserve_window": reserve_rows,
        },
        "recorded_profiles": RECORDED_PROFILES,
        "exhaustion_event": {
            "run_id": "fbe62ad5ecf946eb8f12caac95692904",
            "profile": "standard@200000",
            "consumed_tokens": 196312,
            "sdk_receipt_tokens": 194789,
            "gateway_turn_tokens": 1523,
            "note": (
                "the SDK receipt (input 33061 + cached 158784 + output 2944 "
                "= 194789, cache added exactly once under the disjoint "
                "convention) plus 1523 gateway planner/reviewer tokens; the "
                "window carried NO closing partition, so the coder consumed "
                "the whole axis and the closing review stood down with zero "
                "reviewer spend — the designed fence, and the recorded "
                "basis for the 600k amendment"
            ),
            "resolution_amendment": "forge.budget-profiles/1@2026-09-27-align",
        },
        "dimensions": {
            "routes": [
                {
                    "route": "claude-sdk-lane",
                    "measured_on_these_traces": True,
                    "token_convention": (
                        "anthropic-shaped DISJOINT — input excludes the "
                        "cache; spend total = input + cache_read + "
                        "cache_write + output (cache added exactly once)"
                    ),
                    "dimensions_reported": [
                        "input_tokens",
                        "cached_input_tokens",
                        "cache_write_tokens",
                        "output_tokens",
                        "per-model usage (costUSD, contextWindow)",
                        "calls (per attempt: 1 lane dispatch)",
                        "cost_usd (the SDK's own meter)",
                    ],
                    "time_dimension": (
                        "lane job wall window from the recorded trace "
                        "(lower bound); FORGE_LANE_BUDGET_SECONDS enforces "
                        "the lane's own clock"
                    ),
                },
                {
                    "route": "gateway (planner/reviewer, litellm)",
                    "measured_on_these_traces": True,
                    "token_convention": (
                        "the durable run_budgets counters are INCLUSIVE "
                        "exposure (consumed + reserved + unresolved); the "
                        "gateway turns ride the SAME run budget as the lane "
                        "receipts (1523 tokens on the exhausted window)"
                    ),
                    "dimensions_reported": [
                        "calls",
                        "tokens (inclusive exposure)",
                        "wallclock_s (anchored deadline)",
                    ],
                    "time_dimension": "the run budget's anchored wall clock",
                },
                {
                    "route": "claude-code (batch)",
                    "measured_on_these_traces": False,
                    "token_convention": (
                        "anthropic-shaped DISJOINT (same tells: the driver "
                        "always talks to an Anthropic-compatible endpoint)"
                    ),
                    "dimensions_reported": [
                        "input_tokens",
                        "cached_input_tokens",
                        "cache_write_tokens",
                        "output_tokens",
                        "cost_usd",
                    ],
                    "time_dimension": "harness episode wall clock",
                    "note": (
                        "the #289 single-writer capture measured this route "
                        "(36125 in / 136768 cached / 2305 out, $0.2180); "
                        "not re-measured here — the conventions table is "
                        "the stable part"
                    ),
                },
                {
                    "route": "grok-build / opencode / copilot (batch, OpenAI-compatible)",
                    "measured_on_these_traces": False,
                    "token_convention": (
                        "OpenAI-shaped INCLUSIVE — the cache rides INSIDE "
                        "input_tokens; a cached column is a breakdown, "
                        "NEVER added on top (the double-count guard)"
                    ),
                    "dimensions_reported": [
                        "input_tokens (inclusive)",
                        "cached_tokens (breakdown)",
                        "output_tokens",
                        "reasoning/thinking tokens (output breakdown)",
                    ],
                    "time_dimension": "harness episode wall clock",
                },
            ],
            "note": (
                "provider-specific inclusive/disjoint conventions preserved "
                "(#339/#368): the two shapes never mix into one fold — "
                "normalize_counters decides the shape per receipt and the "
                "inclusive input is computed under the receipt's OWN "
                "convention"
            ),
        },
        "conclusion": (
            "measured BEFORE changing defaults: 9 SDK lane receipts across "
            "the #364/#377 captures show every attempt serving a majority-"
            "cached ~190k-token context regardless of task size, so the "
            "token axis measures context, not task weight. The 200k "
            "standard profile exhausted once (fbe62ad5) with the closing "
            "review stood down; the 600k standard profile is the RECORDED "
            "amended posture of that deployment (finite, enforced), not a "
            "universal requirement. Calibration moves through the "
            "structured assessment (budget_class) + the recorded "
            "profile-selection reason + the operator amendment — never a "
            "model-granted ceiling"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "evaluation" / "economics" / "budget-calibration-v1.json",
    )
    args = parser.parse_args(argv)

    document = build_document()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        wrote = args.out.relative_to(REPO_ROOT)
    except ValueError:
        wrote = args.out
    print(f"wrote {wrote}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
