#!/usr/bin/env python3
"""R37-13 (#294) + R38-09 (#310) — build the delivery-economics report from REAL ledgers.

This reads the RECORDED pilot artifacts in-tree — the per-task usage
ledgers (the #276 shape with ``cost_state: unknown``), the operator
acceptance decisions, the exact profile versions — joins them through
``MeasurementLinker`` (#276) and ``EconomicsLinker`` (#294), and writes
the stamped economics report. No model runs, no clocks, no fabrication:
the scripted vendor's wire counters are labelled
``synthetic-vendor-counter`` and excluded from throughput; unknown costs
render as known lower bound + coverage, never zero.

R38-09 (#310) adds the LIVE SINGLE-WRITER population: the SDK lane
receipts the 2026-09-24 qualification run captured — the four
provider-reported cost figures the live record carries ($0.2353712 /
$0.2179536 / $0.2005248 / $0.1498992, total $0.8037488) that stayed OUT
of the durable ``usage_receipts`` table (0 rows at capture — the recorded
gap) — ingested idempotently through the usage-ingestion front door and
joined as their OWN population, never folded into the pilot's
aggregates. The killed lane job's spend stays unknown with a zero
receipt (never zero-cost), and no acceptance decisions exist for
qualification runs, so no accepted items are claimed.

The estimate column is priced from a VERSIONED rate card (an authored
assumption file, never a bill) and labelled ``estimate`` per entry.

Usage::

    uv run python scripts/build_economics_report.py \
        --pilot evaluation/pilot/lab-pilot-v1 \
        --rates evaluation/economics/rate-card-lab-v1.json \
        --out evaluation/economics/lab-economics-v1.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from forge.adaptive.delivery_economics import (  # noqa: E402
    EconomicsLinker,
    RateCard,
    operator_summary,
    reconcile_with_budget,
)
from forge.adaptive.delivery_measurement import (  # noqa: E402
    MeasurementLinker,
    ledger_records_from_ingested_usage,
    trace_accepted_task,
)
from forge.adaptive.usage_ingestion import (  # noqa: E402
    UsageIngestStore,
    ingest_usage_artifact,
    spend_cap_check,
)

DISCOVERY_LIVE_DIR = REPO_ROOT / "evaluation" / "discovery_live"
LIVE_SINGLE_WRITER_EVIDENCE = (
    REPO_ROOT / "docs" / "evaluation" / "2026-09-24-live-single-writer" / "live-run-evidence.json"
)


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _nn_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number >= 0 else None


def _profile_version(spec: dict[str, Any], report: dict[str, Any]) -> str:
    """The exact profile identity: recipe id pinned to the spec digest."""
    digest = ""
    spec_block = report.get("spec")
    if isinstance(spec_block, dict):
        digest = str(spec_block.get("digest") or "")
    recipe = str(spec.get("recipe_id") or "unknown-recipe")
    return f"{recipe}@{digest[:12]}" if digest else recipe


def _acceptance_contract(evidence: dict[str, Any]) -> str:
    """The bar the operator applied — the contracted verification names."""
    task = evidence.get("task") if isinstance(evidence.get("task"), dict) else {}
    names = [str(row.get("name")) for row in task.get("verification") or [] if row]
    if names:
        return "verification:" + "+".join(sorted(names))
    return "acceptance:operator-decision"


def pilot_records(pilot_dir: Path) -> dict[str, Any]:
    """The pilot's REAL recorded artifacts, read as-is."""
    spec = _load_json(pilot_dir / "spec.json")
    report = _load_json(pilot_dir / "report.json")
    tasks_report = report.get("tasks") if isinstance(report.get("tasks"), dict) else {}
    evidences: list[dict[str, Any]] = []
    for path in sorted((pilot_dir / "records").glob("*/task-evidence.json")):
        evidences.append(_load_json(path))
    return {"spec": spec, "report": report, "tasks_report": tasks_report, "evidences": evidences}


def linker_inputs(records: dict[str, Any], *, pilot_id: str) -> dict[str, list[dict[str, Any]]]:
    """The #276 linker input records — one row per RECORDED fact.

    Every attempt of every task is kept (accepted, failed, superseded);
    a usage row without token counters still contributes its attempt and
    its gap. No cost is ever emitted for the scripted vendor (spend is
    unknown by construction — ``cost_state: unknown`` in the source).
    """
    spec = records["spec"]
    profile = _profile_version(spec, records["report"])
    harness = str(spec.get("harness_id") or "")
    works: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    spans: list[dict[str, Any]] = []
    for evidence in records["evidences"]:
        task_id = str(evidence.get("task_id"))
        record = evidence.get("record") if isinstance(evidence.get("record"), dict) else {}
        accepted = bool(record.get("accepted"))
        works.append(
            {
                "work_id": task_id,
                "outcome": "accepted" if accepted else "rejected",
                "model_version": profile,
                "harness_version": harness,
                "acceptance_contract": _acceptance_contract(evidence),
            }
        )
        usage_ledger = evidence.get("usage_ledger") or []
        for row in usage_ledger:
            attempt_id = str(row.get("attempt_id"))
            attempts.append({"work_id": task_id, "attempt_id": attempt_id})
            tokens = row.get("tokens") if isinstance(row.get("tokens"), dict) else {}
            input_tokens = tokens.get("input")
            output_tokens = tokens.get("output")
            receipt: dict[str, Any] = {
                "receipt_id": f"{pilot_id}:{task_id}:{attempt_id}:usage",
                "work_id": task_id,
                "attempt_id": attempt_id,
                "source": str(row.get("source") or "lab-lane/vendor-wire"),
                "provider": str(row.get("source") or "lab-lane/vendor-wire"),
                "model": "",
                "completeness": (
                    "aggregate"
                    if input_tokens is not None or output_tokens is not None
                    else "unknown"
                ),
            }
            if input_tokens is not None:
                receipt["input_tokens"] = int(input_tokens)
            if output_tokens is not None:
                receipt["output_tokens"] = int(output_tokens)
            receipts.append(receipt)
            model_time = _nn_float(row.get("model_time_s"))
            if model_time is not None:
                spans.append(
                    {
                        "span_id": f"{pilot_id}:{task_id}:{attempt_id}:model",
                        "work_id": task_id,
                        "attempt_id": attempt_id,
                        "span_type": "model",
                        "origin": "harness",
                        "seconds": model_time,
                    }
                )
        # The reviewer's effort is a TASK-level operator window — attached
        # to the accepting attempt, named so its scope stays explicit.
        review_minutes = _nn_float(record.get("review_minutes"))
        accepting = [
            str(a.get("attempt_id")) for a in record.get("attempts") or [] if a.get("accepted")
        ]
        anchor = (
            accepting[-1]
            if accepting
            else (str(record["attempts"][-1]["attempt_id"]) if record.get("attempts") else "")
        )
        if review_minutes is not None and anchor:
            spans.append(
                {
                    "span_id": f"{pilot_id}:{task_id}:review",
                    "work_id": task_id,
                    "attempt_id": anchor,
                    "span_type": "human_wait",
                    "origin": "operator",
                    "seconds": review_minutes * 60.0,
                }
            )
    return {"works": works, "attempts": attempts, "receipts": receipts, "spans": spans}


def acceptance_records(records: dict[str, Any]) -> list[dict[str, Any]]:
    """The operator's acceptance decisions — the outcome authority."""
    spec = records["spec"]
    profile = _profile_version(spec, records["report"])
    harness = str(spec.get("harness_id") or "")
    rows: list[dict[str, Any]] = []
    for evidence in records["evidences"]:
        task_id = str(evidence.get("task_id"))
        record = evidence.get("record") if isinstance(evidence.get("record"), dict) else {}
        decided = ""
        decided_at = ""
        for decision in evidence.get("operator_decisions") or []:
            if (
                isinstance(decision, dict)
                and decision.get("state") == "DECIDED"
                and decision.get("point") == "acceptance"
            ):
                decided = str(decision.get("decided_by") or "")
                decided_at = str(decision.get("at") or "")
                break
        rows.append(
            {
                "work_id": task_id,
                "accepted": bool(record.get("accepted")),
                "decided_by": decided or "human:lab-operator",
                "decided_at": decided_at,
                "acceptance_contract": _acceptance_contract(evidence),
                "attempt_outcomes": {
                    str(attempt.get("attempt_id")): (
                        "accepted" if attempt.get("accepted") else "rejected"
                    )
                    for attempt in record.get("attempts") or []
                },
                "profile_version": profile,
                "harness_version": harness,
            }
        )
    return rows


#: The live single-writer run's LANE JOBS, from the recorded evidence:
#: job id → (work/run id, attempt outcome, the recorded fact the outcome
#: rests on). Every row cites its evidence block — nothing is guessed.
_LIVE_LANE_JOBS: dict[int, tuple[str, str, str]] = {
    723: (
        "ca78cda2",
        "rejected",
        "blocked harness_artifact_missing — v0.36.0 template exit bug (flow_attempts.2)",
    ),
    726: (
        "60b9de7f",
        "accepted",
        "the successful lane attempt: candidate published, verification green,"
        " review raised concerns — task acceptance stays a human decision (flow.mr,"
        " collect-flow evidence)",
    ),
    730: (
        "905194f0",
        "cancelled",
        "killed by the pause fence mid-turn — NO usage receipt captured, spend"
        " unknown (interrupt.job_cancel)",
    ),
    731: (
        "905194f0",
        "rejected",
        "resumed turn completed, candidate diff EMPTY — repair_no_effect"
        " (interrupt.honest_verdict)",
    ),
    732: (
        "905194f0",
        "rejected",
        "third retry also repair_no_effect — final run state blocked (interrupt.attempt2_job)",
    ),
}


def live_single_writer_population() -> dict[str, Any] | None:
    """R38-09 (#310): the live single-writer run's REAL SDK receipts, ingested.

    The recorded gap: the run's SDK receipts report $0.8037488 total
    ($0.2179536 for the successful lane attempt) while the durable
    ``usage_receipts`` table stayed EMPTY (0 rows at capture — the
    evidence's own ``spend.coverage_gap``). This ingests the captured
    artifacts through the idempotent front door
    (:func:`forge.adaptive.usage_ingestion.ingest_usage_artifact`) and
    joins them as their OWN population — qualification runs with NO
    acceptance decisions, so no accepted items are claimed and the spend
    never enters the pilot's aggregates. The killed job's attempt is kept
    with NO receipt (spend unknown, never zero); planning-side budget
    counters are recorded separately (their per-dimension split is
    unknown, so they are not ingested as spend rows).
    """
    if not LIVE_SINGLE_WRITER_EVIDENCE.is_file():
        return None
    evidence = _load_json(LIVE_SINGLE_WRITER_EVIDENCE)
    phases = evidence.get("phases") if isinstance(evidence.get("phases"), dict) else {}
    spend = evidence.get("spend") if isinstance(evidence.get("spend"), dict) else {}
    receipts_usd = spend.get("lane_usage_receipts_usd") or {}
    if not isinstance(receipts_usd, dict) or not receipts_usd:
        return None

    # The full counters the evidence captured for job 732 (the only lane
    # job whose token block reached the record) and the partial counters
    # the honest verdict states for job 731 (input + cache only).
    interrupt = phases.get("interrupt") if isinstance(phases.get("interrupt"), dict) else {}
    attempt2 = (
        interrupt.get("attempt2_job") if isinstance(interrupt.get("attempt2_job"), dict) else {}
    )
    job732_usage = attempt2.get("usage") if isinstance(attempt2.get("usage"), dict) else {}

    store = UsageIngestStore()
    ingested_receipts: list[str] = []
    for job_key, cost in sorted(receipts_usd.items()):
        job_id = int("".join(ch for ch in str(job_key).split("_", 1)[0] if ch.isdigit()) or 0)
        job = _LIVE_LANE_JOBS.get(job_id)
        if job is None:
            continue
        work_id, _outcome, _fact = job
        artifact: dict[str, Any] = {
            "receipt_id": f"live-evidence:job{job_id}",
            "driver": "claude-sdk-lane",
            "model": "glm-5.3-flash",
            "total_cost_usd": float(cost),
            "cost_basis": "provider-reported",
            "attempt_id": f"job{job_id}",
        }
        if job_id == 732 and job732_usage:
            artifact.update(
                {
                    "input_tokens": job732_usage.get("input_tokens"),
                    "cached_input_tokens": job732_usage.get("cached_input_tokens"),
                    "output_tokens": job732_usage.get("output_tokens"),
                }
            )
        if job_id == 731:
            # recorded in the honest verdict's mechanics line: input 32344
            # + cached 140544 — the output counter never reached the record
            artifact.update({"input_tokens": 32344, "cached_input_tokens": 140544})
        result = ingest_usage_artifact(
            store,
            artifact,
            work_id=work_id,
            source="live-evidence/sdk-receipt",
            route_version="claude-code@2.1.273",
        )
        ingested_receipts.extend(row.receipt_id for row in result.created)

    works: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    for job_id in sorted(_LIVE_LANE_JOBS):
        work_id, outcome, fact = _LIVE_LANE_JOBS[job_id]
        if not any(work["work_id"] == work_id for work in works):
            works.append(
                {
                    "work_id": work_id,
                    # no acceptance decisions exist for qualification runs —
                    # the work outcome stays unknown, spend stays programme-side
                    "outcome": "",
                    "model_version": "glm-5.3-flash",
                    "harness_version": "claude-sdk-lane@2.1.273",
                    "acceptance_contract": "",
                }
            )
        attempts.append(
            {
                "work_id": work_id,
                "attempt_id": f"job{job_id}",
                "outcome": outcome,
                "recorded_fact": fact,
            }
        )
    records = ledger_records_from_ingested_usage(store.documents(), works=works, attempts=attempts)
    # The green task's REVIEW window: the review happened (findings + a
    # "concerns" verdict are recorded) but its duration never reached the
    # record — an UNKNOWN operator-side window, named as a gap (never
    # zero-filled, never re-typed).
    records["spans"] = [
        {
            "span_id": "live-evidence:job726:review",
            "work_id": "60b9de7f",
            "attempt_id": "job726",
            "span_type": "human_wait",
            "origin": "operator",
            "seconds": None,
        }
    ]
    ledger = MeasurementLinker().link(**records)
    report = EconomicsLinker().link(
        ledger,
        pilot={
            "population": "live_single_writer",
            "issue": "R38-09 / #310 (the recorded usage-ingestion gap, closed by ingest)",
            "recorded_cap_usd": spend.get("cap_usd"),
            "durable_usage_receipts_rows_at_capture": len(
                ((phases.get("collect-flow") or {}).get("durable_state") or {}).get(
                    "usage_receipts"
                )
                or []
            ),
        },
    )
    document = report.to_document()
    document["measurement_ledger"] = ledger.to_document()
    # Planning and review stay VISIBLE beside the lane spend: the durable
    # run-budget counters are call/token TOTALS whose per-dimension split
    # and cost are unknown (never ingested as spend, never estimated), and
    # the recorded coverage gap is quoted verbatim.
    document["planning_counters"] = {
        "run_budgets_durable": spend.get("run_budgets_durable") or {},
        "coverage_gap_at_capture": spend.get("coverage_gap") or "",
        "note": (
            "planning-side durable counters, recorded separately from the lane"
            " receipts — consumed_calls/consumed_tokens are run totals whose"
            " input/output split and cost are unknown; 905194f0's row is STALE"
            " (updated 12:34, before the resumed turns whose receipts never"
            " reconciled) — the ingestion join, not the budget counter, is the"
            " spend truth here"
        ),
    }
    # the ONE live task traced: planning (budget counters, separate) + the
    # lane attempt + the review window — every receipt and every gap named
    document["trace_green_task"] = trace_accepted_task(ledger, "60b9de7f")
    document["ingestion"] = {
        "ingested_receipt_ids": sorted(ingested_receipts),
        "replayed_or_reconciled": len(store.rows()) - len(ingested_receipts),
        "spend_cap_check": spend_cap_check(
            store.rows(),
            cap_usd=float(spend.get("cap_usd") or 0.0),
        ),
        "note": (
            "the captured SDK receipt artifacts ingested idempotently by"
            " (work, attempt, receipt, source) — re-running this report over"
            " the same evidence writes nothing new; the killed job730 kept"
            " its attempt with NO receipt: spend unknown, never zero"
        ),
    }
    document["budget_reconciliation"] = reconcile_with_budget(
        document,
        {
            "cap_usd": spend.get("cap_usd"),
            "currency": "usd",
            "label": "the qualification run's recorded hard cap",
        },
    )
    return {"document": document, "store": store, "evidence": evidence}


def discovery_live_captures() -> dict[str, Any] | None:
    """The sibling's landed discovery-live captures, read as-is (or None).

    Each capture file is one ATTEMPT of its run (the three live files share
    one run id: the truncated attempt, the malformed-response retry and the
    completed run). The per-call gateway receipts aggregate to one usage
    receipt per attempt — their own ``usd_estimated`` figures are
    cap-enforcement estimates, so cost is priced from the rate card (which
    carries their recorded prices), never claimed as billing. These are
    RESEARCH captures with mechanical grades, not accepted delivery items:
    no acceptance decision exists, so the works carry unknown outcomes and
    stay programme-side spend in their own population.
    """
    runs_dir = DISCOVERY_LIVE_DIR / "runs"
    if not runs_dir.is_dir():
        return None
    captures = [(_load_json(path), path) for path in sorted(runs_dir.glob("*.json"))]
    if not captures:
        return None
    works: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    provenance: dict[str, str] = {}
    caps: list[float] = []
    for capture, path in captures:
        run_id = str(capture.get("run_id"))
        attempt_id = path.stem
        mode = str(capture.get("mode") or "live")
        source = f"discovery-live/{mode}"
        provenance.setdefault(source, str((capture.get("capture") or {}).get("provenance") or ""))
        works.append(
            {
                "work_id": run_id,
                "outcome": "",
                "model_version": str((capture.get("capture") or {}).get("model_identity") or ""),
                "harness_version": str((capture.get("capture") or {}).get("harness") or ""),
                "acceptance_contract": "",
            }
        )
        attempts.append({"work_id": run_id, "attempt_id": attempt_id})
        cost = capture.get("cost") if isinstance(capture.get("cost"), dict) else {}
        call_receipts = cost.get("receipts") or []
        input_tokens = sum(int(row.get("input_tokens") or 0) for row in call_receipts)
        output_tokens = sum(int(row.get("output_tokens") or 0) for row in call_receipts)
        receipts.append(
            {
                "receipt_id": f"discovery-live:{run_id}:{attempt_id}:usage",
                "work_id": run_id,
                "attempt_id": attempt_id,
                "source": source,
                "provider": source,
                "model": str((capture.get("capture") or {}).get("model_identity") or ""),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "completeness": "aggregate" if call_receipts else "unknown",
            }
        )
        cap = _nn_float(cost.get("cap_usd"))
        if cap is not None:
            caps.append(cap)
    return {
        "records": {"works": works, "attempts": attempts, "receipts": receipts},
        "provenance_by_source": provenance,
        "cap_usd": min(caps) if caps else None,
        "captures": len(captures),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", required=True, type=Path, help="the pilot directory")
    parser.add_argument("--rates", required=True, type=Path, help="the versioned rate card JSON")
    parser.add_argument("--out", required=True, type=Path, help="the report output path")
    parser.add_argument(
        "--budget-cap-usd",
        type=float,
        default=None,
        help="an explicit programme cap; default is the pilot's summed per-task budget caps",
    )
    args = parser.parse_args(argv)

    pilot_dir = args.pilot if args.pilot.is_absolute() else REPO_ROOT / args.pilot
    rates_path = args.rates if args.rates.is_absolute() else REPO_ROOT / args.rates
    out_path = args.out if args.out.is_absolute() else REPO_ROOT / args.out

    records = pilot_records(pilot_dir)
    pilot_id = str(records["spec"].get("pilot_id") or pilot_dir.name)
    card = RateCard.from_json(_load_json(rates_path))

    inputs = linker_inputs(records, pilot_id=pilot_id)
    ledger = MeasurementLinker().link(**inputs)
    report = EconomicsLinker().link(
        ledger,
        acceptance_records=acceptance_records(records),
        rate_card=card,
        pilot={
            "pilot_id": pilot_id,
            "platform": str(records["spec"].get("platform") or ""),
            "recipe_id": str(records["spec"].get("recipe_id") or ""),
            "harness_id": str(records["spec"].get("harness_id") or ""),
            "profile_version": _profile_version(records["spec"], records["report"]),
        },
    )
    document = report.to_document()

    # The agreed cap: the pilot's own summed per-task budget ceilings (the
    # learning contract's spend boundary), unless overridden explicitly.
    cap = args.budget_cap_usd
    if cap is None:
        kit_tasks = (_load_json(pilot_dir / "tasks.json").get("tasks")) or []
        caps = [_nn_float((task.get("budget") or {}).get("max_spend_usd")) for task in kit_tasks]
        cap = sum(value for value in caps if value is not None) or None

    document["pilot"]["agreed_cap_usd"] = cap
    document["budget_reconciliation"] = reconcile_with_budget(
        document, {"cap_usd": cap, "currency": "usd", "label": "summed per-task budget ceilings"}
    )
    document["operator_summary"] = operator_summary(document)
    document["measurement_ledger"] = ledger.to_document()

    # The sibling's discovery-live captures — a SEPARATE population, never
    # folded into the pilot's aggregates (different profiles, different
    # evidence classes, no acceptance decisions). Included only when the
    # captures have landed with receipts; noted otherwise.
    discovery_notes: list[str] = []
    captures = discovery_live_captures()
    if captures is None:
        discovery_notes.append("discovery-live: no captured runs have landed — nothing to include")
    else:
        discovery_ledger = MeasurementLinker().link(**captures["records"])
        discovery_report = EconomicsLinker().link(
            discovery_ledger,
            rate_card=card,
            provenance_by_source=captures["provenance_by_source"],
            pilot={
                "population": "discovery_live",
                "issue": "R37-09 / #290 (sibling capture, read-only join)",
                "recorded_cap_usd": captures["cap_usd"],
            },
        )
        discovery_document = discovery_report.to_document()
        discovery_document["budget_reconciliation"] = reconcile_with_budget(
            discovery_document,
            {"cap_usd": captures["cap_usd"], "currency": "usd", "label": "recorded per-run cap"},
        )
        discovery_document["measurement_ledger"] = discovery_ledger.to_document()
        document["populations"] = {"discovery_live": discovery_document}
        discovery_notes = [
            (
                f"discovery-live: {captures['captures']} captured attempts joined as"
                " their OWN population under populations.discovery_live — the"
                " captures carry mechanical grades, not acceptance decisions, so"
                " no accepted items are claimed and their spend never enters the"
                " pilot's aggregates"
            ),
            (
                "discovery-live estimates are priced from the same versioned card"
                " (which carries the capture's own recorded cap-enforcement"
                " prices) — never billing"
            ),
        ]

    # R38-09 (#310): the live single-writer population — the REAL SDK lane
    # receipts the run captured, ingested through the front door and joined
    # as their OWN population. Coverage before: durable usage_receipts rows
    # EMPTY at capture; after: the captured artifacts ingested (idempotent —
    # re-running writes nothing new).
    live_notes: list[str] = []
    live = live_single_writer_population()
    if live is None:
        live_notes.append(
            "live-single-writer: no captured SDK receipts have landed — nothing to include"
        )
    else:
        live_document = live["document"]
        document.setdefault("populations", {})["live_single_writer"] = live_document
        programme = live_document["costs"]["programme"]
        successful = live_document["costs"]["successful_attempt_costs"]
        successful_usd = next(
            (
                attempt["cost_usd"]
                for attempts in successful.values()
                for attempt in attempts.values()
                if attempt["cost_usd"] is not None
            ),
            None,
        )
        live_notes = [
            (
                "live-single-writer: the 2026-09-24 qualification run's SDK lane"
                " receipts joined under populations.live_single_writer — the"
                f" durable usage_receipts table was EMPTY at capture"
                f" ({live_document['pilot']['durable_usage_receipts_rows_at_capture']}"
                " rows); the captured artifacts now ingest idempotently and the"
                f" provider-reported lane spend is {programme['billed_known_lower_bound_usd']}"
                " usd (lower bound — the killed job730's spend is unknown, never zero)"
            ),
            (
                "live-single-writer keeps qualification runs programme-side: no"
                " acceptance decisions exist, so no accepted items are claimed and"
                " the lane spend never enters the pilot's aggregates; the"
                f" successful lane attempt's own cost is {successful_usd} usd — a"
                " DIFFERENT measure from an accepted-item all-attempt total and"
                " from programme spend per accepted item"
            ),
            (
                "live-single-writer planning counters stay separate: the run_budgets"
                " consumed_calls/consumed_tokens figures are recorded beside the"
                " population, not ingested as spend rows (their per-dimension"
                " split and cost are unknown — never zero, never estimated as spend)"
            ),
        ]
    document["notes"] = sorted(
        set(
            [
                *document["notes"],
                *discovery_notes,
                *live_notes,
                (
                    "setup_minutes and completion_latency_minutes are whole-task"
                    " windows over mixed populations — excluded from stage_seconds"
                    " by construction, never re-typed"
                ),
                (
                    "the vendor is scripted: token counters are real wire"
                    " observations, spend is unknown by construction and every"
                    " cost figure here is a labelled estimate or an honest unknown"
                ),
            ]
        )
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8")

    programme = document["costs"]["programme"]
    coverage = document["costs"]["coverage"]["programme"]
    stages = document["latency"]["stage_seconds"]
    print(f"wrote {out_path}")
    print(f"  pilot:              {pilot_id} (profile {document['pilot']['profile_version']})")
    print(f"  works/attempts:     {programme['works']} / {programme['attempts']}")
    print(f"  accepted items:     {document['costs']['accepted_items']['works']}")
    print(f"  receipt coverage:   {coverage['receipt_coverage']}")
    print(f"  unknown-cost rcpts: {coverage['unknown_cost_receipts']}")
    print(f"  billed (exact):     {programme['billed_usd']}")
    print(f"  billed lower bound: {programme['billed_known_lower_bound_usd']}")
    print(f"  estimate (card {card.version}): {programme['estimate_usd']}")
    print(f"  evidence classes:   {document['evidence']['classes']}")
    print(f"  model stage s:      {stages['model']['stage_total_seconds']}")
    print(f"  human_wait stage s: {stages['human_wait']['stage_total_seconds']}")
    print(f"  cross-run joins:    {len(document['cross_joins'])}")
    print(
        f"  budget cap:         {cap} -> {document['budget_reconciliation']['lines'][0]['status']}"
    )
    if "populations" in document:
        discovery = document["populations"]["discovery_live"]
        print("  discovery-live population (separate, never folded in):")
        print(
            f"    works/attempts:     {discovery['costs']['programme']['works']}"
            f" / {discovery['costs']['programme']['attempts']}"
        )
        print(f"    evidence classes:   {discovery['evidence']['classes']}")
        print(f"    estimate (card):    {discovery['costs']['programme']['estimate_usd']}")
        print(
            f"    accepted items:     {discovery['costs']['accepted_items']['works']}"
            " (no acceptance decisions — none claimed)"
        )
    live = document.get("populations", {}).get("live_single_writer")
    if live is not None:
        programme = live["costs"]["programme"]
        coverage = live["costs"]["coverage"]["programme"]
        successful = live["costs"]["successful_attempt_costs"]
        successful_usd = next(
            (
                attempt["cost_usd"]
                for attempts in successful.values()
                for attempt in attempts.values()
                if attempt["cost_usd"] is not None
            ),
            None,
        )
        cap_check = live["ingestion"]["spend_cap_check"]
        print("  live-single-writer population (R38-09 — the REAL SDK receipts):")
        print(
            f"    works/attempts:     {programme['works']} / {programme['attempts']}"
            f" (receipt coverage {coverage['receipt_coverage']}, the killed job730"
            " kept with no receipt — spend unknown, never zero)"
        )
        print(
            "    coverage before:    durable usage_receipts rows at capture =="
            f" {live['pilot']['durable_usage_receipts_rows_at_capture']}"
            " (the recorded gap); after =="
            f" {len(live['ingestion']['ingested_receipt_ids'])} artifacts ingested"
            " idempotently"
        )
        print(
            "    provider-reported:  "
            f"{programme['billed_known_lower_bound_usd']} usd (lower bound;"
            f" exact withheld — billed_exact={programme['billed_exact']})"
        )
        print(
            f"    successful attempt: {successful_usd} usd (its OWN receipts — a"
            " different measure from all-attempt and per-accepted)"
        )
        print(
            f"    accepted items:     {live['costs']['accepted_items']['works']}"
            " (qualification runs — no acceptance decisions, none claimed;"
            " programme-per-accepted undefined, never zero)"
        )
        print(
            f"    spend cap:          {cap_check['cap_usd']} usd — reserved"
            f" {round(cap_check['reserved_usd'], 7)} usd, headroom"
            f" {round(cap_check['headroom_usd'], 7)} usd, allowed={cap_check['allowed']}"
            " (unknown intervals reserved at their lower bound)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
