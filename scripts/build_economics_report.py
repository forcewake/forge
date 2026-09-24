#!/usr/bin/env python3
"""R37-13 (issue #294) — build the delivery-economics report from REAL ledgers.

This reads the RECORDED pilot artifacts in-tree — the per-task usage
ledgers (the #276 shape with ``cost_state: unknown``), the operator
acceptance decisions, the exact profile versions — joins them through
``MeasurementLinker`` (#276) and ``EconomicsLinker`` (this issue), and
writes the stamped economics report. No model runs, no clocks, no
fabrication: the scripted vendor's wire counters are labelled
``synthetic-vendor-counter`` and excluded from throughput; unknown
costs render as known lower bound + coverage, never zero.

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
from forge.adaptive.delivery_measurement import MeasurementLinker  # noqa: E402

DISCOVERY_LIVE_DIR = REPO_ROOT / "evaluation" / "discovery_live"


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
    document["notes"] = sorted(
        set(
            [
                *document["notes"],
                *discovery_notes,
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
