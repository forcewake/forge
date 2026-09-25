#!/usr/bin/env python3
"""Q39-11 (#330) — build the accepted-task ledger from the REAL captures.

This reads the RECORDED evidence in-tree — the useful-WIP resume trace
(``docs/evaluation/2026-09-25-useful-wip-resume/``, the #306 drill: the
driver's ``live-run-evidence.json`` bundle, the validated record and the
alignment receipts) plus the #310 population (the live single-writer run's
SDK lane receipts, ingested idempotently through the usage-ingestion
front door exactly as ``scripts/build_economics_report.py`` does) — and
joins each population into an :class:`AcceptedTaskLedger`: run → attempt
→ model-call receipt → native job → candidate → verification → human
decision, one complete evidence chain per task.

No model runs, no clocks, no fabrication. The honesty rules the module
pins show up in the artifact's real numbers:

- the useful-WIP task's SDK receipts are provider-reported (the SDK's
  own meter) — the price-card column stays empty beside them (no
  receipt lacked a figure to estimate) and the billing-reconciliation
  column stays ``null`` (no billing export exists to reconcile against);
- the human decision point is LABELLED ``pending`` — the delivery and
  the independent verification stand, the MR is a Draft awaiting its
  human, so the accepted measures are undefined, never zero, and every
  work's all-attempt totals stand beside them with coverage;
- the run's terminal budget refusal (``budget_exhausted: reviewer
  refused``) is recorded as a budget event, and the #325 review-only
  continuation is EVALUATED against the recorded candidate binding: the
  candidate head and tested identity are unchanged, so the recovery
  would repeat ONLY the review — zero coder dispatches, zero commits;
- time is separated into its measures from the recorded timestamps only
  (lane-job windows, operator decision gaps, the alignment window);
  model/tool/CI-queue time and the reviewer's effort have NO recorded
  windows and render as named unknowns — never zero, never a rate.

Usage::

    uv run python scripts/build_accepted_task_ledger.py \
        --out evaluation/economics/accepted-task-ledger-v1.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

from forge.adaptive.delivery_economics import (  # noqa: E402
    AcceptedTaskLedgerBuilder,
    EconomicsLinker,
    EconomicsReport,
    ModelRate,
    RateCard,
)
from forge.adaptive.delivery_measurement import MeasurementLinker  # noqa: E402

from build_economics_report import (  # noqa: E402
    _LIVE_LANE_JOBS,
    live_single_writer_population,
)

USEFUL_WIP_DIR = REPO_ROOT / "docs" / "evaluation" / "2026-09-25-useful-wip-resume"
EVIDENCE_PATH = USEFUL_WIP_DIR / "live-run-evidence.json"
RECORD_PATH = USEFUL_WIP_DIR / "useful-wip-resume-2026-09-25.json"
ALIGNMENT_PATH = USEFUL_WIP_DIR / "alignment-receipts.json"

#: The useful-WIP interrupted arm's run (issue #2) and its accidental
#: uninterrupted sibling (issue #1) — the two REAL works of the drill.
RUN_ID = "04bca389d69e4d4c9ef0ee223a0ed65e"
SIBLING_RUN_ID = "d58082a2ee9a4973a3cf567bc93c18d8"


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _trace_stamp(line: str) -> str | None:
    """The leading timestamp of a GitLab trace line (``…Z 01O <body>``)."""
    head = line.split(" 01O ", 1)[0].strip()
    return head if _parse_dt(head) is not None else None


def _seconds(began: Any, ended: Any) -> float | None:
    start = _parse_dt(began)
    end = _parse_dt(ended)
    if start is None or end is None or end < start:
        return None
    return (end - start).total_seconds()


def _window(
    window_id: str,
    work_id: str,
    attempt_id: str,
    measure: str,
    population: str,
    seconds: float | None,
    began_at: Any = "",
    ended_at: Any = "",
    note: str = "",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "window_id": window_id,
        "work_id": work_id,
        "attempt_id": attempt_id,
        "measure": measure,
        "population": population,
        "seconds": None if seconds is None else round(seconds, 6),
    }
    if began_at:
        row["began_at"] = str(began_at)
    if ended_at:
        row["ended_at"] = str(ended_at)
    if note:
        row["note"] = note
    return row


def useful_wip_population() -> dict[str, Any] | None:
    """The useful-WIP resume trace → the ledger's primary population.

    Every row cites the evidence block it was read from (``recorded_in``)
    — nothing is guessed. The four SDK receipts are the raw usage blocks
    the dispatches carry (the record's ``spend`` block rounds them and
    zeroes the token columns; the raw blocks carry the real counters).
    """
    if not (EVIDENCE_PATH.is_file() and RECORD_PATH.is_file()):
        return None
    evidence = _load_json(EVIDENCE_PATH)
    record = _load_json(RECORD_PATH)
    phases = evidence.get("phases") if isinstance(evidence.get("phases"), dict) else {}
    interrupt = phases.get("interrupt") if isinstance(phases.get("interrupt"), dict) else {}
    state = (
        phases.get("interrupt-state", {}).get("04bca389")
        if isinstance(phases.get("interrupt-state"), dict)
        else {}
    )
    run = state.get("run") if isinstance(state.get("run"), dict) else {}
    full = state.get("evidence_full") if isinstance(state.get("evidence_full"), dict) else {}
    verification = full.get("verification") if isinstance(full.get("verification"), dict) else {}
    candidate = (
        full.get("published_candidate") if isinstance(full.get("published_candidate"), dict) else {}
    )
    continuation = full.get("continuation") if isinstance(full.get("continuation"), dict) else {}
    decisions = (
        continuation.get("decisions") if isinstance(continuation.get("decisions"), dict) else {}
    )
    uninterrupted = (
        phases.get("uninterrupted-attempt-1")
        if isinstance(phases.get("uninterrupted-attempt-1"), dict)
        else {}
    )
    dispatches = (
        interrupt.get("dispatches") if isinstance(interrupt.get("dispatches"), list) else []
    )
    ladder = (
        interrupt.get("probe_ladder") if isinstance(interrupt.get("probe_ladder"), list) else []
    )

    def dispatch_for(job_id: int) -> dict[str, Any]:
        for row in dispatches:
            if row.get("lane_job_id") == job_id:
                return row if isinstance(row, dict) else {}
        return {}

    def usage_of(job_id: int) -> dict[str, Any]:
        row = dispatch_for(job_id)
        usage = row.get("usage_receipt") if isinstance(row.get("usage_receipt"), dict) else {}
        if usage:
            return usage
        meta = row.get("candidate_meta") if isinstance(row.get("candidate_meta"), dict) else {}
        return meta.get("usage") if isinstance(meta.get("usage"), dict) else {}

    def receipt(job_id: int) -> dict[str, Any]:
        usage = usage_of(job_id)
        row: dict[str, Any] = {
            "receipt_id": f"useful-wip:job{job_id}",
            "work_id": RUN_ID,
            "attempt_id": f"job{job_id}",
            "source": str(usage.get("source") or "claude-sdk-lane/usage-receipt"),
            "provider": "claude-sdk-lane",
            "model": "glm-5.3-flash",
            "cost_basis": "provider-reported",
            "completeness": "aggregate",
            "recorded_in": f"phases.interrupt.dispatches[lane_job_id={job_id}]",
        }
        for name in (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_tokens",
            "output_tokens",
        ):
            value = usage.get(name)
            # the record's spend block zeroes absent counters; only a
            # counter the RAW usage block actually names is a counter —
            # never a zero-fill.
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                row[name] = value
        cost = usage.get("total_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            row["total_cost_usd"] = float(cost)
        return row

    job762_usage = (
        uninterrupted.get("dispatches")[0].get("usage_receipt")
        if uninterrupted.get("dispatches")
        else {}
    )
    works = [
        {
            "work_id": RUN_ID,
            "outcome": "",
            "model_version": "glm-5.3-flash",
            "harness_version": "claude-sdk-lane@59ba869",
            "acceptance_contract": "verification:smoke-oracle (precommitted before any run)",
        },
        {
            "work_id": SIBLING_RUN_ID,
            "outcome": "",
            "model_version": "glm-5.3-flash",
            "harness_version": "claude-sdk-lane@59ba869",
            "acceptance_contract": "verification:smoke-oracle (precommitted before any run)",
        },
    ]
    attempts = [
        {"work_id": RUN_ID, "attempt_id": "job767", "outcome": "superseded"},
        {"work_id": RUN_ID, "attempt_id": "job768", "outcome": "superseded"},
        {"work_id": RUN_ID, "attempt_id": "job769", "outcome": "accepted"},
        {"work_id": SIBLING_RUN_ID, "attempt_id": "job762", "outcome": "accepted"},
    ]
    receipts = [
        receipt(767),
        receipt(768),
        receipt(769),
        {
            "receipt_id": "useful-wip:job762",
            "work_id": SIBLING_RUN_ID,
            "attempt_id": "job762",
            "source": "claude-sdk-lane/usage-receipt",
            "provider": "claude-sdk-lane",
            "model": "glm-5.3-flash",
            "cost_basis": "provider-reported",
            "completeness": "aggregate",
            "recorded_in": "phases.uninterrupted-attempt-1.dispatches[0].usage_receipt",
            **{
                name: int(job762_usage[name])
                for name in ("input_tokens", "output_tokens")
                if isinstance(job762_usage.get(name), int)
            },
            "total_cost_usd": float(job762_usage.get("total_cost_usd")),
        },
    ]

    # -- native jobs (the GitLab lane jobs the attempts ran as) ----------
    native_jobs = [
        {
            "job_id": "job767",
            "work_id": RUN_ID,
            "attempt_id": "job767",
            "pipeline_id": "421",
            "status": "failed",
            "began_at": (ladder[0] or {}).get("driver_phase_anchor", ""),
            "ended_at": (interrupt.get("job_cancel") or {}).get("attempted_at", ""),
            "recorded_in": "phases.interrupt.probe_ladder[0] + job_cancel",
        },
        {
            "job_id": "job768",
            "work_id": RUN_ID,
            "attempt_id": "job768",
            "pipeline_id": "422",
            "status": "failed",
            "began_at": (ladder[1] or {}).get("driver_phase_anchor", ""),
            "ended_at": _trace_stamp(
                ((dispatch_for(768).get("collector_lines") or [""])[0]),
            )
            or "",
            "recorded_in": "phases.interrupt.probe_ladder[1] + dispatches[job768].collector_lines",
        },
        {
            "job_id": "job769",
            "work_id": RUN_ID,
            "attempt_id": "job769",
            "pipeline_id": "423",
            "status": "success",
            "began_at": _handle_started_at(full),
            "ended_at": _trace_stamp(((dispatch_for(769).get("collector_lines") or [""])[0])) or "",
            "recorded_in": "evidence_full.harness.handle.started_at + dispatches[job769].collector_lines",
        },
        {
            "job_id": "job762",
            "work_id": SIBLING_RUN_ID,
            "attempt_id": "job762",
            "pipeline_id": "416",
            "status": "success",
            "recorded_in": "phases.uninterrupted-attempt-1.dispatches[0]",
        },
    ]

    # -- candidates + verification ---------------------------------------
    candidate_sha = str(candidate.get("sha") or "")
    candidates = [
        {
            "candidate_sha": candidate_sha,
            "work_id": RUN_ID,
            "attempt_id": "job769",
            "base_sha": str(candidate.get("attempt_base") or ""),
            "entries": int(candidate.get("entries") or 0),
            "identity_state": "exact",
            "recorded_in": "evidence_full.published_candidate",
        },
        {
            # the sibling's candidate sha reached the record as a PREFIX
            # inside the failure ledger's prose — kept partial, never padded
            "candidate_sha": "62906e1f",
            "work_id": SIBLING_RUN_ID,
            "attempt_id": "job762",
            "identity_state": "partial",
            "recorded_in": "record.failures[2] (sha prefix only)",
        },
    ]
    verifications = [
        {
            "verification_id": "useful-wip:pipeline-425",
            "work_id": RUN_ID,
            "candidate_sha": str(verification.get("tested_oid") or candidate_sha),
            "producer": str(verification.get("producer") or "gitlab-pipeline"),
            "status": str(verification.get("status") or ""),
            "tested_oid": str(verification.get("tested_oid") or ""),
            "observed_at": str(verification.get("observed_at") or ""),
            "recorded_in": "evidence_full.verification + record.oracle",
        },
        {
            "verification_id": "useful-wip:issue1-oracle",
            "work_id": SIBLING_RUN_ID,
            "candidate_sha": "62906e1f",
            "producer": "gitlab-pipeline",
            "status": "passed",
            "tested_oid": "62906e1f",
            "recorded_in": "record.failures[2] (oracle pipeline green on the prefix)",
        },
    ]

    # -- the human decision points (both works still await their human) --
    human_decisions = [
        {
            "work_id": RUN_ID,
            "state": "pending",
            "channel": "draft-mr-awaiting-human",
            "note": (
                "Draft MR !2 on the verified candidate — a human merges, forge"
                " never does; the run-level reviewer was refused by the budget"
                " before any review ran"
            ),
        },
        {
            "work_id": SIBLING_RUN_ID,
            "state": "pending",
            "channel": "draft-mr-awaiting-human",
            "note": "Draft MR !1 delivered end-to-end by the shipped finalization",
        },
    ]

    # -- budget events + the review-only recovery evaluation -------------
    status_reason = str(run.get("status_reason") or "")
    budget_rows = state.get("run_budgets") if isinstance(state.get("run_budgets"), list) else []
    budget_row = budget_rows[0] if budget_rows else {}
    budget_events = [
        {
            "work_id": RUN_ID,
            "kind": "budget_refusal",
            "at": str(budget_row.get("updated_at") or ""),
            "reason": status_reason or "budget_exhausted",
            "detail": (
                f"run budget {budget_row.get('status')}:"
                f" {budget_row.get('consumed_calls')}/{budget_row.get('max_calls')}"
                f" calls, {budget_row.get('consumed_tokens')}/"
                f"{budget_row.get('max_tokens')} tokens consumed — the run-level"
                " reviewer could not run"
            ),
            "observable": "budget.phase_exhaustion",
        }
    ]
    review_recoveries = [
        {
            "work_id": RUN_ID,
            "budget_decision": status_reason,
            "candidate_sha": candidate_sha,
            "tested_identity": str(verification.get("tested_oid") or candidate_sha),
            # the current binding: the draft MR still names the same
            # candidate and the same tested identity — unchanged
            "current_candidate_sha": candidate_sha,
            "current_tested_identity": str(verification.get("tested_oid") or candidate_sha),
        }
    ]

    # -- time windows, from the recorded timestamps only ------------------
    job767_end = (interrupt.get("job_cancel") or {}).get("attempted_at", "")
    # leg 1's honest refusal observation (the driver's own refused block —
    # the one tied to job 768, never the earlier no-anchor refusal)
    leg1_observed = (interrupt.get("refused") or {}).get("at", "")
    decision_ids = {
        "7ff0c6b2": "7ff0c6b2b79019b2372a381087447712578cff923b70356c10354310aceb8164",
        "6251bbd5": "6251bbd5672af48b722c27f41475b99f4b992e8d5359a093603993e625e1e3b2",
    }
    leg1_decision = (decisions.get(decision_ids["7ff0c6b2"]) or {}).get("decided_at", "")
    leg2_decision = (decisions.get(decision_ids["6251bbd5"]) or {}).get("decided_at", "")
    alignment_runs = []
    if ALIGNMENT_PATH.is_file():
        alignment = _load_json(ALIGNMENT_PATH)
        alignment_runs = alignment.get("runs") if isinstance(alignment.get("runs"), list) else []
    time_windows = [
        _window(
            "useful-wip:job767:ci-runtime",
            RUN_ID,
            "job767",
            "ci_runtime",
            "lane-job:driver-anchor→failed-observed",
            _seconds((ladder[0] or {}).get("driver_phase_anchor"), job767_end),
            (ladder[0] or {}).get("driver_phase_anchor"),
            job767_end,
            "driver-phase anchor → the job-level cancel observation (status already failed)",
        ),
        _window(
            "useful-wip:job768:ci-runtime",
            RUN_ID,
            "job768",
            "ci_runtime",
            "lane-job:driver-envelope→collector",
            _seconds(
                (ladder[1] or {}).get("driver_phase_anchor"),
                _trace_stamp(((dispatch_for(768).get("collector_lines") or [""])[0])),
            ),
            (ladder[1] or {}).get("driver_phase_anchor"),
            _trace_stamp(((dispatch_for(768).get("collector_lines") or [""])[0])),
            "resume leg 1: driver envelope echo → collector outcome line",
        ),
        _window(
            "useful-wip:job769:ci-runtime",
            RUN_ID,
            "job769",
            "ci_runtime",
            "lane-job:job-start→collector",
            _seconds(
                _handle_started_at(full),
                _trace_stamp(((dispatch_for(769).get("collector_lines") or [""])[0])),
            ),
            _handle_started_at(full),
            _trace_stamp(((dispatch_for(769).get("collector_lines") or [""])[0])),
            "resume leg 2: the harness handle's own job start → collector outcome line",
        ),
        _window(
            "useful-wip:job762:ci-runtime",
            SIBLING_RUN_ID,
            "job762",
            "ci_runtime",
            "lane-job:unrecorded",
            None,
            note=(
                "the uninterrupted turn measured 87.3 s in the drill README's"
                " prose — no trace window reached the evidence, so the runtime"
                " is unknown here, never the prose figure"
            ),
        ),
        _window(
            "useful-wip:leg1-decision:operator-wait",
            RUN_ID,
            "job767",
            "operator_wait",
            "operator:failure-observed→continuation-decision",
            _seconds(job767_end, leg1_decision),
            job767_end,
            leg1_decision,
        ),
        _window(
            "useful-wip:leg2-decision:operator-wait",
            RUN_ID,
            "job768",
            "operator_wait",
            "operator:failure-observed→continuation-decision",
            _seconds(leg1_observed, leg2_decision),
            leg1_observed,
            leg2_decision,
            "the /retry gap between leg 1's honest refusal and leg 2's continuation decision",
        ),
        _window(
            "useful-wip:reviewer-effort",
            RUN_ID,
            "job769",
            "reviewer_effort",
            "operator:run-reviewer",
            None,
            note=(
                "the run-level reviewer could not run — the budget refused its"
                " call (budget_exhausted); reviewer effort is unknown, never zero"
            ),
        ),
    ]
    if alignment_runs and isinstance(alignment_runs[0], dict):
        first = alignment_runs[0]
        time_windows.append(
            _window(
                "useful-wip:alignment:setup",
                "",
                "",
                "setup_effort",
                "operator:lab-alignment",
                _seconds(first.get("started_at"), first.get("finished_at")),
                first.get("started_at"),
                first.get("finished_at"),
                "the lab alignment before any paid call (cohort-level window)",
            )
        )

    cap = (record.get("spend") or {}).get("cap_usd")
    records = {
        "works": works,
        "attempts": attempts,
        "receipts": receipts,
        "calls": [],
        "spans": [],
    }
    ledger = MeasurementLinker().link(**records)
    report = EconomicsLinker().link(
        ledger,
        # the drill's own recorded price class (run_useful_wip_resume.py's
        # FALLBACK_PRICE_PER_MTOK) — a VERSIONED card, armed for any receipt
        # the SDK meter did not price. No receipt lacked a figure in this
        # trace, so the estimate column stays empty; the card rides the
        # document so its basis is traceable, never guessed.
        rate_card=RateCard(
            version="useful-wip-priceclass-v1",
            rates=[
                ModelRate(
                    provider="claude-sdk-lane",
                    model="glm-5.3-flash",
                    input_per_mtok_usd=0.60,
                    cached_input_per_mtok_usd=0.07,
                    output_per_mtok_usd=2.20,
                )
            ],
        ),
        pilot={
            "population": "useful_wip_resume",
            "issue": "Q39-11 / #330 (the #306 useful-WIP resume trace)",
            "recorded_cap_usd": cap,
            "profile_version": "claude-sdk-lane@59ba869 / glm-5.3-flash",
            "evidence": "docs/evaluation/2026-09-25-useful-wip-resume/",
        },
    )
    document = (
        AcceptedTaskLedgerBuilder()
        .build(
            report,
            native_jobs=native_jobs,
            candidates=candidates,
            verifications=verifications,
            human_decisions=human_decisions,
            budget_events=budget_events,
            review_recoveries=review_recoveries,
            time_windows=time_windows,
            budgets={RUN_ID: {"cap_usd": cap}, SIBLING_RUN_ID: {"cap_usd": cap}},
            measurement_ledger=ledger,
            pilot={
                "population": "useful_wip_resume",
                "issue": "Q39-11 / #330 (the #306 useful-WIP resume trace)",
                "recorded_cap_usd": cap,
            },
        )
        .to_document()
    )
    document["measurement_ledger"] = ledger.to_document()
    return document


def _handle_started_at(full: dict[str, Any]) -> str:
    """The harness handle's own job-start timestamp (a JSON string)."""
    harness = full.get("harness") if isinstance(full.get("harness"), dict) else {}
    handle = harness.get("handle")
    try:
        parsed = json.loads(handle) if isinstance(handle, str) else {}
    except ValueError:
        return ""
    return str(parsed.get("started_at") or "") if isinstance(parsed, dict) else ""


def live_population() -> dict[str, Any] | None:
    """The #310 live single-writer SDK receipts — their OWN population."""
    live = live_single_writer_population()
    if live is None:
        return None
    economics_document = live["document"]
    report = EconomicsReport.from_document(economics_document)
    measurement_ledger = economics_document.get("measurement_ledger") or {}
    native_jobs = [
        {
            "job_id": f"job{job_id}",
            "work_id": work_id,
            "attempt_id": f"job{job_id}",
            "status": outcome,
            "recorded_in": "scripts/build_economics_report.py::_LIVE_LANE_JOBS",
        }
        for job_id in sorted(_LIVE_LANE_JOBS)
        for work_id, outcome, _fact in [_LIVE_LANE_JOBS[job_id]]
    ]
    time_windows = [
        {
            "window_id": "live-evidence:job726:review",
            "work_id": "60b9de7f",
            "attempt_id": "job726",
            "measure": "reviewer_effort",
            "population": "operator:review",
            "seconds": None,
            "note": (
                "the review happened (findings + a 'concerns' verdict are"
                " recorded) but its duration never reached the record — an"
                " unknown operator-side window, never zero"
            ),
        }
    ]
    spend_cap = (live["evidence"].get("spend") or {}).get("cap_usd")
    return (
        AcceptedTaskLedgerBuilder()
        .build(
            report,
            native_jobs=native_jobs,
            time_windows=time_windows,
            budgets={
                work_id: {"cap_usd": spend_cap}
                for work_id in sorted({row[0] for row in _LIVE_LANE_JOBS.values()})
            },
            measurement_ledger=measurement_ledger,
            pilot={
                "population": "live_single_writer",
                "issue": "R38-09 / #310 (the SDK receipts, read-only join)",
                "recorded_cap_usd": spend_cap,
            },
        )
        .to_document()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="the artifact output path")
    args = parser.parse_args(argv)
    out_path = args.out if args.out.is_absolute() else REPO_ROOT / args.out

    document = useful_wip_population()
    if document is None:
        print("the useful-WIP evidence has not landed — nothing to build", file=sys.stderr)
        return 1
    notes = list(document.get("notes") or [])
    live = live_population()
    if live is None:
        notes.append("live-single-writer: no captured SDK receipts — nothing to include")
    else:
        document["populations"] = {"live_single_writer": live}
        programme = live["costs"]["programme"]
        notes.append(
            "live-single-writer (the #310 SDK receipts) joined as their OWN"
            f" population: {programme['works']} works /"
            f" {programme['coverage']['attempts']} attempts,"
            f" provider-reported lower bound"
            f" {programme['columns']['provider_reported_usd']['known_lower_bound_usd']}"
            " usd — qualification runs, no human decision points, never"
            " folded into the useful-WIP aggregates"
        )
    notes.append(
        "combined-steering (the #313 trace) is NOT folded in: its five lane"
        " jobs are a different drill's population — noted, never blended"
    )
    document["notes"] = sorted(set(notes))
    document["pilot"]["artifact"] = "evaluation/economics/accepted-task-ledger-v1.json"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8")

    programme = document["costs"]["programme"]
    columns = programme["columns"]
    coverage = programme["coverage"]
    measures = document["time_measures"]
    print(f"wrote {out_path}")
    print(f"  schema:             {document['schema']}")
    print(f"  identity chain:     {' → '.join(document['identity_chain'])}")
    print(f"  works/attempts:     {programme['works']} / {coverage['attempts']}")
    print(
        f"  human decisions:    {sorted(state['state'] for state in document['human_decision_points'].values())}"
    )
    print(f"  receipt coverage:   {coverage['receipt_coverage']}")
    print(
        f"  provider-reported:  {columns['provider_reported_usd']['known_lower_bound_usd']} usd (exact={columns['provider_reported_usd']['exact']})"
    )
    print(
        f"  price-card est:     {columns['price_card_estimate_usd']['known_lower_bound_usd']} usd (column receipts: {columns['price_card_estimate_usd']['receipts']})"
    )
    print(
        f"  billing-recon:      {columns['billing_reconciliation_usd']['known_lower_bound_usd']} usd (no billing export exists)"
    )
    print(
        f"  accepted items:     {document['costs']['accepted_items']['works']} (undefined per-accepted, never zero)"
    )
    for work_id, row in sorted(document["costs"]["all_attempt_totals"].items()):
        spent = row["columns"]["provider_reported_usd"]["known_lower_bound_usd"]
        print(
            f"  all-attempt {work_id[:8]}: {row['attempts']} attempts"
            f" ({row['failed_or_superseded_attempts_kept']} kept failed/superseded),"
            f" provider-reported {spent} usd, decision {row['outcome']}"
        )
    for measure in (
        "model_time",
        "tool_time",
        "ci_queue",
        "ci_runtime",
        "operator_wait",
        "reviewer_effort",
        "setup_effort",
    ):
        row = measures[measure]
        total = row["stage_total_seconds"]
        lower = row["stage_lower_bound_seconds"]
        shape = f"total {total}" if total is not None else f"lower bound {lower}"
        print(f"  {measure:<16} {shape} s (measured={row['measured']})")
    budget = document["budget"][RUN_ID]
    closing = budget["closing_budget"]
    print(
        f"  closing reserve:    {closing['closing_reserve_usd']} usd of cap {closing['cap_usd']} (coder ceiling {closing['coder_ceiling_usd']})"
    )
    print(
        f"  budget refusals:    {len(budget['budget_refusals'])} recorded ({budget['budget_refusals'][0]['reason'][:60]})"
    )
    recovery = budget["review_only_recovery"]
    print(
        f"  review-only recov:  evaluated={recovery['evaluated']}"
        f" allowed={recovery.get('allowed')} dispatches={recovery.get('coder_dispatches')}"
        f" commits={recovery.get('commits')}"
    )
    print(f"  identity gaps:      {len(document['identity_gaps'])}")
    print(
        f"  human minutes:      {document['observability']['delivery.human_minutes']} (reviewer refused — unknown, never zero)"
    )
    if live is not None:
        live_programme = live["costs"]["programme"]
        print(
            "  live-single-writer population (separate, never folded in):"
            f" {live_programme['works']} works /"
            f" {live_programme['coverage']['attempts']}"
            " attempts, provider-reported lower bound"
            f" {live_programme['columns']['provider_reported_usd']['known_lower_bound_usd']} usd,"
            f" receipt coverage {live_programme['coverage']['receipt_coverage']}"
            " (the killed job730 kept with no receipt — spend unknown, never zero)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
