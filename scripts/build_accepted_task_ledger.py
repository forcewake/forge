#!/usr/bin/env python3
"""Q39-11 (#330) + R41-13 (#368) — build the accepted-task ledgers from REAL captures.

This reads the RECORDED evidence in-tree — the useful-WIP resume trace
(``docs/evaluation/2026-09-25-useful-wip-resume/``, the #306 drill: the
driver's ``live-run-evidence.json`` bundle, the validated record and the
alignment receipts) plus the #310 population (the live single-writer run's
SDK lane receipts, ingested idempotently through the usage-ingestion
front door exactly as ``scripts/build_economics_report.py`` does) — and
joins each population into an :class:`AcceptedTaskLedger`: run → attempt
→ model-call receipt → native job → candidate → verification → human
decision, one complete evidence chain per task.

``--population review-loop`` (R41-13 / #368) builds the v2 artifact over
the #364 review-and-correction capture
(``docs/evaluation/2026-09-27-review-loop/`` — the richest real capture:
delivery 1 + correction rounds 2/3 with their OWN finite budgets and
closing partitions + a blocked round 4 + three honestly-failed attempts
+ a foreign-lineage blocked child, every lane job's SDK receipt and
trace): the correction rounds fold under ONE root task through the #359
``root_run_id``, the rounds keep INDEPENDENT candidate outcomes, the
budget window carries the #340 amendment (limit_before/after history)
and the #339 exposure quantities under an EXPLICIT versioned policy,
and the recorded window totals ($0.8455 qualifying / $1.4797
all-attempt) reconcile against the folded receipts — the unreceipted
residual stays a bound, never a column.

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

    uv run python scripts/build_accepted_task_ledger.py \
        --population review-loop \
        --out evaluation/economics/accepted-task-ledger-v2.json
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
    TIME_MEASURES,
    AcceptedTaskLedgerBuilder,
    EconomicsLinker,
    EconomicsReport,
    ModelRate,
    RateCard,
    accepted_ledger_v2_document,
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

REVIEW_LOOP_DIR = REPO_ROOT / "docs" / "evaluation" / "2026-09-27-review-loop"
REVIEW_LOOP_EVIDENCE = REVIEW_LOOP_DIR / "live-run-evidence.json"
REVIEW_LOOP_TRACES = REVIEW_LOOP_DIR / "traces"
REVIEW_LOOP_RECORD = REPO_ROOT / "qualification" / "records" / "review-loop-2026-09-27.json"

#: The #364 review-loop lineage, by the identities the capture records.
_ROOT_RUN = "7319478e10b74f3c90d0e0ea43a9dc3f"
_ROUND2_RUN = "bbd8d0d4885341fe858dafb3908207ed"
_ROUND3_RUN = "17645ea9c6a247ef9b456ed614eb5c2a"
_ROUND4_RUN = "2396987be7b64895b7b73af273211c91"
_FAILED_BOOTSTRAP_RUN = "165dd1edab724b20b42c916991c53069"
_FAILED_BUDGET_RUN = "fbe62ad5ecf946eb8f12caac95692904"
_FAILED_SCOPE_RUN = "8be14a80312d47a8b274efaa4d6a0a73"
_FOREIGN_CHILD_RUN = "6e0fdf3448ee4860b2bc34822db9489f"
_FOREIGN_PARENT_RUN = "76a1088a3221486d8dac9ab7de54ad3b"
_FAILED_BUDGET_ATTEMPT = "run-fbe62ad5ecf946eb"

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


def _trace_envelope(job_id: int) -> tuple[str, str]:
    """A lane job's trace envelope — the first and last trace-line stamps."""
    candidates = sorted(REVIEW_LOOP_TRACES.glob(f"*job{job_id}.log"))
    if not candidates:
        return "", ""
    lines = candidates[0].read_text(encoding="utf-8", errors="replace").splitlines()
    stamps = [line.split()[0] for line in lines if line.split()]
    if len(stamps) < 2:
        return "", ""
    return stamps[0], stamps[-1]


def _block(parent: Any, key: str) -> dict[str, Any]:
    """The dict child of a JSON block — {} when absent (never None)."""
    child = parent.get(key) if isinstance(parent, dict) else None
    return child if isinstance(child, dict) else {}


def _rows(parent: Any, key: str) -> list[Any]:
    """The list child of a JSON block — [] when absent (never None)."""
    child = parent.get(key) if isinstance(parent, dict) else None
    return child if isinstance(child, list) else []


def review_loop_population() -> dict[str, Any] | None:
    """R41-13 (#368): the #364 review-loop capture → the v2 lineage document.

    Every row cites the evidence block it was read from — nothing is
    guessed, no clock runs, no figure is invented. The population is the
    capture's OWN spend frame: the root lineage (delivery 1 + rounds 2/3
    + the blocked round 4 + the three honestly-failed delivery attempts)
    plus the foreign-lineage blocked child whose lane spend never
    reached the record as a figure (bounded by the window
    reconciliation, never zero, never a column entry).
    """
    if not REVIEW_LOOP_EVIDENCE.is_file():
        return None
    evidence = _load_json(REVIEW_LOOP_EVIDENCE)
    phases = _block(evidence, "phases")
    delivery = _block(phases, "delivery")
    round2 = _block(phases, "round2")
    round3 = _block(phases, "round3")
    negative = _block(phases, "negative")
    align = _block(phases, "align")
    setup = _block(phases, "setup")
    preflight = _block(phases, "preflight")
    failed_attempts = _rows(phases, "delivery_attempts_failed")
    failed_by_run = {
        str(row.get("run_id")): row for row in failed_attempts if isinstance(row, dict)
    }
    spend = _block(_block(phases, "collect"), "spend")
    qualifying = _block(spend, "qualifying_breakdown")

    # -- the SDK receipts (the evidence's own usage blocks) -------------
    def sdk_receipt(
        phase_key: str,
        work_id: str,
        attempt_id: str,
        receipt_id: str,
        *,
        zero_spend_note: str = "",
        cost_only_note: str = "",
    ) -> dict[str, Any]:
        usage = _block(_block(qualifying, phase_key), "usage")
        row: dict[str, Any] = {
            "receipt_id": receipt_id,
            "work_id": work_id,
            "attempt_id": attempt_id,
            "source": str(usage.get("source") or "claude-agent-sdk"),
            "provider": "claude-sdk-lane",
            "model": "glm-5.3-flash",
            "cost_basis": "provider-reported",
            "completeness": "aggregate",
            "recorded_in": f"spend.qualifying_breakdown.{phase_key}.usage"
            if phase_key
            else zero_spend_note or cost_only_note,
        }
        for name in ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens"):
            value = usage.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                row[name] = value
        cost = usage.get("total_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            row["total_cost_usd"] = float(cost)
        return row

    budget_failed = failed_by_run.get(_FAILED_BUDGET_RUN, {}).get("budget") or {}
    budget_failed_usage = failed_by_run.get(_FAILED_BUDGET_RUN, {}).get("usage") or {}

    works: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []

    def add_work(work_id: str, harness_version: str) -> None:
        works.append(
            {
                "work_id": work_id,
                "outcome": "",
                "model_version": "glm-5.3-flash",
                "harness_version": harness_version,
                "acceptance_contract": (
                    "verification:precommitted-oracle (tests/test_text_utils.py +"
                    " the round-2 contract, committed before any run)"
                ),
            }
        )

    def add_attempt(work_id: str, attempt_id: str, outcome: str) -> None:
        attempts.append({"work_id": work_id, "attempt_id": attempt_id, "outcome": outcome})

    wheel_qualifying = "claude-sdk-lane@forge-0.41.0(wheel-223d0f25)"
    wheel_early = "claude-sdk-lane@forge-0.41.0(wheel-2616d221)"
    for work_id in (
        _ROOT_RUN,
        _ROUND2_RUN,
        _ROUND3_RUN,
        _ROUND4_RUN,
        _FAILED_BUDGET_RUN,
        _FOREIGN_CHILD_RUN,
    ):
        add_work(work_id, wheel_qualifying)
    add_work(_FAILED_BOOTSTRAP_RUN, "claude-sdk-lane@forge-0.41.0")
    add_work(_FAILED_SCOPE_RUN, wheel_early)

    # the qualifying rounds + the blocked round 4 (the negative arm's
    # lane job ran and its SDK receipt is real spend inside the window)
    for phase_key, work_id, attempt_id, outcome in (
        ("delivery", _ROOT_RUN, "job1157", "superseded"),
        ("round2", _ROUND2_RUN, "job1162", "superseded"),
        ("round3", _ROUND3_RUN, "job1166", "accepted"),
        ("negative", _ROUND4_RUN, "job1169", "rejected"),
    ):
        add_attempt(work_id, attempt_id, outcome)
        receipts.append(sdk_receipt(phase_key, work_id, attempt_id, f"review-loop:{attempt_id}"))
    # the honestly-failed delivery attempts (the capture's own frame)
    add_attempt(_FAILED_BOOTSTRAP_RUN, "job1135", "rejected")
    receipts.append(
        {
            "receipt_id": "review-loop:job1135",
            "work_id": _FAILED_BOOTSTRAP_RUN,
            "attempt_id": "job1135",
            "source": "claude-agent-sdk",
            "provider": "claude-sdk-lane",
            "model": "glm-5.3-flash",
            "cost_basis": "provider-reported",
            "completeness": "aggregate",
            "total_cost_usd": 0.0,
            "recorded_in": (
                "phases.delivery_attempts_failed[0].note — typed"
                " FORGE_BOOTSTRAP_FAILED, zero model calls, zero spend (a"
                " RECORDED zero, never an inferred one)"
            ),
        }
    )
    add_attempt(_FAILED_BUDGET_RUN, _FAILED_BUDGET_ATTEMPT, "rejected")
    budget_receipt: dict[str, Any] = {
        "receipt_id": f"review-loop:{_FAILED_BUDGET_ATTEMPT}",
        "work_id": _FAILED_BUDGET_RUN,
        "attempt_id": _FAILED_BUDGET_ATTEMPT,
        "source": "claude-agent-sdk",
        "provider": "claude-sdk-lane",
        "model": "glm-5.3-flash",
        "cost_basis": "provider-reported",
        "completeness": "aggregate",
        "recorded_in": "phases.delivery_attempts_failed[1].usage",
    }
    for name in ("input_tokens", "cached_input_tokens", "output_tokens"):
        value = budget_failed_usage.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            budget_receipt[name] = value
    cost = budget_failed_usage.get("total_cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        budget_receipt["total_cost_usd"] = float(cost)
    receipts.append(budget_receipt)
    add_attempt(_FAILED_SCOPE_RUN, "job1141", "superseded")
    receipts.append(
        {
            "receipt_id": "review-loop:job1141",
            "work_id": _FAILED_SCOPE_RUN,
            "attempt_id": "job1141",
            "source": "claude-agent-sdk",
            "provider": "claude-sdk-lane",
            "model": "glm-5.3-flash",
            "cost_basis": "provider-reported",
            "completeness": "aggregate",
            "total_cost_usd": 0.1876,
            "recorded_in": (
                "phases.delivery_attempts_failed[2].note — the lane/oracle/review"
                " legs stand as evidence ($0.1876, recorded at 4dp; no token"
                " counters reached the record)"
            ),
        }
    )
    # the foreign-lineage blocked child: the lane ran, its SDK figure
    # never reached the record — NO receipt (unknown spend, never zero)
    add_attempt(_FOREIGN_CHILD_RUN, "job1154", "rejected")

    # -- native jobs (the GitLab lane jobs the attempts ran as) ---------
    native_jobs: list[dict[str, Any]] = []
    for job_id, work_id, attempt_id, pipeline_id, status, recorded_in in (
        (
            1135,
            _FAILED_BOOTSTRAP_RUN,
            "job1135",
            "789",
            "failed",
            "phases.delivery_attempts_failed[0] (blocked at the bootstrap fence)",
        ),
        (
            1141,
            _FAILED_SCOPE_RUN,
            "job1141",
            "",
            "success",
            "traces/delivery1-job1141.log (resolved_work_id + Job succeeded)",
        ),
        (
            1157,
            _ROOT_RUN,
            "job1157",
            "811",
            "success",
            "phases.delivery.lane + traces/delivery1-job1157.log",
        ),
        (
            1162,
            _ROUND2_RUN,
            "job1162",
            "816",
            "success",
            "phases.round2.lane + traces/round2-job1162.log",
        ),
        (
            1166,
            _ROUND3_RUN,
            "job1166",
            "820",
            "success",
            "phases.round3.lane + traces/round3-job1166.log",
        ),
        (
            1169,
            _ROUND4_RUN,
            "job1169",
            "823",
            "success",
            "phases.negative.lane + traces/round4-negative-job1169.log",
        ),
        (
            1154,
            _FOREIGN_CHILD_RUN,
            "job1154",
            "808",
            "",
            "phases.round2_attempts_failed[0].live_found_defect.evidence_ids"
            " (the lane turn ran; the job's own status never reached the record)",
        ),
    ):
        began, ended = _trace_envelope(job_id)
        native_jobs.append(
            {
                "job_id": f"job{job_id}",
                "work_id": work_id,
                "attempt_id": attempt_id,
                "pipeline_id": pipeline_id,
                "status": status,
                "began_at": began,
                "ended_at": ended,
                "recorded_in": recorded_in,
            }
        )

    # -- candidates + the precommitted oracle verifications -------------
    candidates = [
        {
            "candidate_sha": "6e973202f1e6de4268d9363db2dbeb3c7c500e6c",
            "work_id": _ROOT_RUN,
            "attempt_id": "job1157",
            "identity_state": "exact",
            "recorded_in": "phases.delivery.candidate_sha + oracle",
        },
        {
            "candidate_sha": "f72223ce698c5f4c3137ab7f72185c492bf47876",
            "work_id": _ROUND2_RUN,
            "attempt_id": "job1162",
            "identity_state": "exact",
            "recorded_in": "phases.round2.candidate_sha + oracle",
        },
        {
            "candidate_sha": "7008c940b2d8a6f50f60847c93a9e601ce9e3bf9",
            "work_id": _ROUND3_RUN,
            "attempt_id": "job1166",
            "identity_state": "exact",
            "recorded_in": "phases.round3.candidate_sha + oracle",
        },
    ]
    verifications = [
        {
            "verification_id": "review-loop:oracle-814",
            "work_id": _ROOT_RUN,
            "candidate_sha": "6e973202f1e6de4268d9363db2dbeb3c7c500e6c",
            "producer": "gitlab-pipeline:precommitted-oracle",
            "status": "success",
            "tested_oid": "6e973202f1e6de4268d9363db2dbeb3c7c500e6c",
            "recorded_in": "phases.delivery.oracle",
        },
        {
            "verification_id": "review-loop:oracle-819",
            "work_id": _ROUND2_RUN,
            "candidate_sha": "f72223ce698c5f4c3137ab7f72185c492bf47876",
            "producer": "gitlab-pipeline:precommitted-oracle",
            "status": "success",
            "tested_oid": "f72223ce698c5f4c3137ab7f72185c492bf47876",
            "recorded_in": "phases.round2.oracle",
        },
        {
            "verification_id": "review-loop:oracle-822",
            "work_id": _ROUND3_RUN,
            "candidate_sha": "7008c940b2d8a6f50f60847c93a9e601ce9e3bf9",
            "producer": "gitlab-pipeline:precommitted-oracle",
            "status": "success",
            "tested_oid": "7008c940b2d8a6f50f60847c93a9e601ce9e3bf9",
            "recorded_in": "phases.round3.oracle",
        },
    ]

    # -- the human decision point: PENDING, named ------------------------
    human_decisions = [
        {
            "work_id": _ROOT_RUN,
            "state": "pending",
            "channel": "draft-mr-awaiting-human",
            "note": (
                "Draft MR !4 on the lineage — a human merges, forge never does."
                " The reviewer discussions WERE resolved through the API"
                " surface (resolved_by forcewake, returncode 200) and the"
                " closing reviews returned verdict ok — those are model/CI"
                " decisions over the CANDIDATE, never the human acceptance;"
                " the HUMAN acceptance of the lineage stays pending, named"
            ),
        }
    ]

    # -- the budget refusal (the designed fence) -------------------------
    budget_events = [
        {
            "work_id": _FAILED_BUDGET_RUN,
            "kind": "budget_refusal",
            "at": "",
            "reason": (
                f"budget_exhausted: {budget_failed.get('consumed_tokens')}/"
                f"{budget_failed.get('max_tokens')} tokens — the finite budget"
                " EXHAUSTED at the token axis"
            ),
            "detail": (
                "the closing review stood down (zero reviewer spend, the"
                " standing block names the closing reserve; exactly the"
                " designed fence); the operator resolution: the receipted"
                " profile adjustment to 600k (the class stays finite) + a"
                " fresh run"
            ),
            "observable": "budget.phase_exhaustion",
        }
    ]

    # -- time windows, from the recorded timestamps only ------------------
    time_windows: list[dict[str, Any]] = []
    for job_id, work_id, attempt_id in (
        (1141, _FAILED_SCOPE_RUN, "job1141"),
        (1157, _ROOT_RUN, "job1157"),
        (1162, _ROUND2_RUN, "job1162"),
        (1166, _ROUND3_RUN, "job1166"),
        (1169, _ROUND4_RUN, "job1169"),
    ):
        began, ended = _trace_envelope(job_id)
        time_windows.append(
            _window(
                f"review-loop:job{job_id}:ci-runtime",
                work_id,
                attempt_id,
                "ci_runtime",
                "lane-job:trace-envelope",
                _seconds(began, ended),
                began,
                ended,
                "the runner trace's first → last line (the native runtime wall)",
            )
        )
    for window_id, work_id, attempt_id, note in (
        (
            "review-loop:job1135:ci-runtime",
            _FAILED_BOOTSTRAP_RUN,
            "job1135",
            "the bootstrap fence blocked before any lane turn — no trace exists; unknown, never zero",
        ),
        (
            f"review-loop:{_FAILED_BUDGET_ATTEMPT}:ci-runtime",
            _FAILED_BUDGET_RUN,
            _FAILED_BUDGET_ATTEMPT,
            "no lane job id or trace reached the record for this attempt — unknown, never zero",
        ),
        (
            "review-loop:job1154:ci-runtime",
            _FOREIGN_CHILD_RUN,
            "job1154",
            "the wrong-branch run's trace never reached the record — unknown, never zero",
        ),
    ):
        time_windows.append(
            _window(
                window_id,
                work_id,
                attempt_id,
                "ci_runtime",
                "lane-job:trace-envelope",
                None,
                note=note,
            )
        )
    # the round-2 dispatch anchor: the round-admitted reply observation,
    # selected by CONTENT (never by list position — the evidence's order
    # is not a fact)
    round2_reply_at = ""
    for row in sorted(
        (row for row in _rows(round2, "failures") if isinstance(row, dict)),
        key=lambda row: str(row.get("at") or ""),
    ):
        if "round-admitted reply" in str(row.get("message") or ""):
            round2_reply_at = str(row.get("at") or "")
            break
    time_windows.extend(
        [
            _window(
                "review-loop:delivery1:operator-wait",
                _ROOT_RUN,
                "job1157",
                "operator_wait",
                "review-loop:dispatch-observation→terminal",
                _seconds(delivery.get("started_at"), (delivery.get("terminal") or {}).get("at")),
                delivery.get("started_at"),
                (delivery.get("terminal") or {}).get("at"),
                "the delivery phase's recorded start → the ready_for_human terminal",
            ),
            _window(
                "review-loop:round2:operator-wait",
                _ROUND2_RUN,
                "job1162",
                "operator_wait",
                "review-loop:dispatch-observation→terminal",
                _seconds(round2_reply_at, (round2.get("terminal") or {}).get("at")),
                round2_reply_at,
                (round2.get("terminal") or {}).get("at"),
                (
                    "the reviewer-resolution wait: the /fix dispatch's own stamp"
                    " never reached the record — the window runs from the"
                    " round-admitted reply observation to the corrected"
                    " candidate's ready terminal"
                ),
            ),
            _window(
                "review-loop:round3:operator-wait",
                _ROUND3_RUN,
                "job1166",
                "operator_wait",
                "review-loop:dispatch-observation→terminal",
                _seconds(round3.get("started_at"), (round3.get("terminal") or {}).get("at")),
                round3.get("started_at"),
                (round3.get("terminal") or {}).get("at"),
                "the round-3 phase's recorded start → the ready terminal (spans the kill/recovery)",
            ),
            _window(
                "review-loop:round4:operator-wait",
                _ROUND4_RUN,
                "job1169",
                "operator_wait",
                "review-loop:dispatch-observation→terminal",
                _seconds(negative.get("started_at"), negative.get("finished_at")),
                negative.get("started_at"),
                negative.get("finished_at"),
                "the blocked round: the wait ended in the typed conflict, never a ready terminal",
            ),
        ]
    )
    kill = _block(round3, "kill")
    time_windows.append(
        _window(
            "review-loop:round3:manual-rescue",
            _ROUND3_RUN,
            "job1166",
            "manual_rescue",
            "review-loop:kill→recovered-ready",
            _seconds(kill.get("killed_at"), (round3.get("terminal") or {}).get("at")),
            kill.get("killed_at"),
            (round3.get("terminal") or {}).get("at"),
            (
                "the #358 worker-failure recovery: the child was killed at"
                " ensure_draft_mr one second after committing the candidate;"
                " the restart's own stamp never reached the record — the"
                " window runs from the kill observation to the recovered"
                " ready terminal"
            ),
        )
    )
    for window_id, work_id, attempt_id, note in (
        (
            "review-loop:delivery1:reviewer-effort",
            _ROOT_RUN,
            "job1157",
            "the run-level closing reviewer ran (verdict ok on the exact candidate) — its duration never reached the record; unknown, never zero",
        ),
        (
            "review-loop:round2:reviewer-effort",
            _ROUND2_RUN,
            "job1162",
            (
                "the round-2 review verified the obligation digest (verdict ok;"
                " the digest refusals were the driver's recomputation) and the"
                " discussions were resolved through the API surface — a"
                " model/CI decision, never the human acceptance; the reviewer"
                " duration is unrecorded, never zero"
            ),
        ),
        (
            "review-loop:round2:human-review-effort",
            _ROUND2_RUN,
            "job1162",
            (
                "the HUMAN reviewer's correction-contract commit (3878456c,"
                " tests/test_round2_contract.py — red until corrected) is"
                " recorded; its authoring duration never reached the record —"
                " unknown, never zero"
            ),
        ),
        (
            "review-loop:round3:reviewer-effort",
            _ROUND3_RUN,
            "job1166",
            "the round-3 review legs ran (the readiness gate held, the discussion resolved) — durations unrecorded, never zero",
        ),
    ):
        time_windows.append(
            _window(
                window_id,
                work_id,
                attempt_id,
                "reviewer_effort",
                "review-loop:reviewer-leg",
                None,
                note=note,
            )
        )
    for window_id, began_key, ended_key, population, note in (
        (
            "review-loop:align:setup",
            align,
            align,
            "lab:align",
            "the receipted budget-profile alignment (the 200k→600k pin) before any qualifying run",
        ),
        (
            "review-loop:project-setup",
            setup,
            setup,
            "lab:project-setup",
            "the project/bot/variable setup phase (its recorded failures are the live-found fence findings)",
        ),
        (
            "review-loop:preflight",
            preflight,
            preflight,
            "lab:preflight",
            "the doctor/health preflight before delivery 1",
        ),
    ):
        time_windows.append(
            _window(
                window_id,
                "",
                "",
                "setup_effort",
                population,
                _seconds(began_key.get("started_at"), ended_key.get("finished_at")),
                began_key.get("started_at"),
                ended_key.get("finished_at"),
                note,
            )
        )

    # -- the lineage rows (the #359 root_run_id join) ---------------------
    lineage_runs = [
        {
            "run_id": _ROOT_RUN,
            "relation": "root",
            "root_run_id": _ROOT_RUN,
            "round_number": 1,
            "root_join_basis": "the delivery run itself (issue 5, MR !4's first delivery)",
            "round_outcome": "ready_for_human",
            "outcome_detail": (
                "ordinary classic parent (active_plan_absent true); terminal"
                " ready_for_human; the closing review verdict ok; the"
                " candidate superseded by round 2's correction"
            ),
        },
        {
            "run_id": _ROUND2_RUN,
            "relation": "round",
            "root_run_id": _ROOT_RUN,
            "parent_run_id": _ROOT_RUN,
            "round_number": 2,
            "root_join_basis": "round_rows[7fdaaa9da91b4db8896e949ae4166324].root_run_id",
            "round_outcome": "ready_for_human",
            "outcome_detail": (
                "the corrected output visibly addressed the /fix"
                " (slugify_parts_present, the oracle file untouched); the"
                " reviewer obligation digest verified; the candidate"
                " superseded by round 3's correction"
            ),
        },
        {
            "run_id": _ROUND3_RUN,
            "relation": "round",
            "root_run_id": _ROOT_RUN,
            "parent_run_id": _ROUND2_RUN,
            "round_number": 3,
            "root_join_basis": "round_rows[0e463732c7ed46aba2186c89d99e2e7c].root_run_id",
            "round_outcome": "ready_for_human",
            "outcome_detail": (
                "the #358 kill/restart recovery mid ensure_draft_mr; exactly"
                " one provider commit for the round; terminal ready_for_human"
            ),
        },
        {
            "run_id": _ROUND4_RUN,
            "relation": "round",
            "root_run_id": _ROOT_RUN,
            "round_number": 4,
            "root_join_basis": (
                "the lineage branch factory/5/7319478e named in the typed"
                " conflict — no round row for round 4 reached the evidence"
                " (the parent run id is unrecorded, never guessed)"
            ),
            "round_outcome": "blocked",
            "outcome_detail": (
                "candidate_rejected: branch_drift — the human's conflicting"
                " commit moved the head mid-round (expected 7008c940…, actual"
                " f9e17e25…); the child run blocked, the human commit"
                " preserved (human_commit_is_head true)"
            ),
        },
        {
            "run_id": _FAILED_BOOTSTRAP_RUN,
            "relation": "prior_attempt",
            "root_run_id": _ROOT_RUN,
            "round_number": 0,
            "root_join_basis": (
                "the capture's own delivery_attempts_failed attribution (the"
                " task's failed delivery attempt — no round row exists, the"
                " attribution is the evidence's, never a guess)"
            ),
            "round_outcome": "failed",
            "outcome_detail": (
                "blocked harness_infrastructure: the carrier variable was"
                " PROTECTED; the lane failed CLOSED at the bootstrap fence"
                " (job 1135, typed FORGE_BOOTSTRAP_FAILED, zero model calls,"
                " zero spend — a recorded zero, never an inferred one)"
            ),
        },
        {
            "run_id": _FAILED_BUDGET_RUN,
            "relation": "prior_attempt",
            "root_run_id": _ROOT_RUN,
            "round_number": 0,
            "root_join_basis": "the capture's own delivery_attempts_failed attribution",
            "round_outcome": "failed",
            "outcome_detail": (
                "the finite budget EXHAUSTED at the token axis"
                f" ({budget_failed.get('consumed_tokens')}/{budget_failed.get('max_tokens')});"
                " parked in reviewing under the reviewer-leg budget decision;"
                " the resolution: the receipted 600k profile adjustment + a"
                " fresh run"
            ),
        },
        {
            "run_id": _FAILED_SCOPE_RUN,
            "relation": "prior_attempt",
            "root_run_id": _ROOT_RUN,
            "round_number": 0,
            "root_join_basis": "the capture's own delivery_attempts_failed attribution",
            "round_outcome": "superseded",
            "outcome_detail": (
                "ready_for_human, but the FIRST /fix (note 1472) classified"
                " material_change — the frozen spec's allowed_paths was EMPTY"
                " (the repo carried no .forge.yml); the honest material reply"
                " landed (MR note 1474); the resolution: seed implement.paths"
                " + a fresh run"
            ),
        },
        {
            "run_id": _FOREIGN_CHILD_RUN,
            "relation": "programme_side",
            "parent_run_id": _FOREIGN_PARENT_RUN,
            "round_number": 0,
            "root_join_basis": (
                "parent_run_id 76a1088a (issue 4's lineage — a DIFFERENT"
                " root) + the capture's failed_attempts_counted; the lane ran"
                " and committed to the wrong branch (the #361 publisher"
                " defect), its SDK figure never reached the record"
            ),
            "round_outcome": "blocked",
            "outcome_detail": (
                "blocked external_change: the candidate landed on"
                " factory/4/6e0fdf34 beside the lineage surface"
                " factory/4/76a1088a — the drift check correctly blocked the"
                " run; the wrong-branch commit stands as evidence, never"
                " merged, never the MR head"
            ),
        },
    ]

    # -- the budget window: rounds' own budgets, the amendment, exposure --
    def round_budget(
        block: dict[str, Any], run_id: str, round_number: int, recorded_in: str
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "round_number": round_number,
            "max_calls": block.get("max_calls"),
            "max_tokens": block.get("max_tokens"),
            "wallclock_s": block.get("wallclock_s"),
            "status": block.get("status"),
            "closing_partition_policy": block.get("closing_partition_policy") or "",
            "closing_reserved_calls": block.get("closing_reserved_calls"),
            "closing_reserved_tokens": block.get("closing_reserved_tokens"),
            "recorded_in": recorded_in,
        }

    round_budgets = [
        round_budget(delivery.get("budget") or {}, _ROOT_RUN, 1, "phases.delivery.budget"),
        round_budget(round2.get("budget") or {}, _ROUND2_RUN, 2, "phases.round2.budget"),
        round_budget(round3.get("budget") or {}, _ROUND3_RUN, 3, "phases.round3.budget"),
        {
            "run_id": _FAILED_BUDGET_RUN,
            "round_number": 0,
            "max_calls": budget_failed.get("max_calls"),
            "max_tokens": budget_failed.get("max_tokens"),
            "status": budget_failed.get("status"),
            "consumed_tokens": budget_failed.get("consumed_tokens"),
            "recorded_in": "phases.delivery_attempts_failed[1].budget",
        },
    ]
    align_receipts = _block(align, "receipts")
    pins = _block(align_receipts, "pins")
    profiles_pin = str(pins.get("FORGE_BUDGET_PROFILES") or "{}")
    try:
        profiles = json.loads(profiles_pin)
    except ValueError:
        profiles = {}
    align_generated_at = str(align_receipts.get("generated_at") or "")
    policy_version = "forge.budget-profiles/1@2026-09-27-align"
    budget_amendments = [
        {
            "amendment_id": "review-loop:amend-standard-tokens-600k",
            "run_id": _FAILED_BUDGET_RUN,
            "scope": "profile:standard",
            "command_id": "align:FORGE_BUDGET_PROFILES:standard.max_tokens",
            "axis": "tokens",
            "amount_tokens": 400000,
            "reason": (
                "the finite budget exhausted at the token axis on run"
                " fbe62ad5 (196312/200000); the operator resolution — the"
                " receipted profile adjustment, the class stays finite"
            ),
            "operator": "forcewake",
            "status": "applied",
            "limit_before": {"max_calls": 40, "max_tokens": 200000, "wallclock_s": 3600},
            "limit_after": {"max_calls": 40, "max_tokens": 600000, "wallclock_s": 3600},
            "applied_at": align_generated_at,
            "policy_version": policy_version,
            "recorded_in": (
                "phases.align.receipts.pins.FORGE_BUDGET_PROFILES (the"
                " receipted adjustment — both consumers recreated from the"
                " same image with these pins) +"
                " phases.delivery_attempts_failed[1].note"
            ),
        }
    ]
    exposure_rows = [
        {
            "run_id": _FAILED_BUDGET_RUN,
            "axis": "tokens",
            "consumed": budget_failed.get("consumed_tokens"),
            "limit": budget_failed.get("max_tokens"),
            "exhausted": True,
            "closing_review_stood_down": True,
            "note": (
                "the designed fence observed live: exposure at the token axis"
                " exhausted the budget; the closing review stood down with"
                " zero reviewer spend, the standing block naming the closing"
                " reserve"
            ),
            "recorded_in": "phases.delivery_attempts_failed[1]",
        },
        {
            "run_id": _ROOT_RUN,
            "axis": "tokens",
            "consumed": None,
            "limit": (delivery.get("budget") or {}).get("max_tokens"),
            "exhausted": False,
            "note": "consumption counters never reached the record (status open)",
            "recorded_in": "phases.delivery.budget",
        },
        {
            "run_id": _ROUND2_RUN,
            "axis": "tokens",
            "consumed": None,
            "limit": (round2.get("budget") or {}).get("max_tokens"),
            "exhausted": False,
            "note": (
                "the round's OWN finite budget with the closing partition"
                " reserving 90000 tokens / 6 calls for the review"
            ),
            "recorded_in": "phases.round2.budget",
        },
        {
            "run_id": _ROUND3_RUN,
            "axis": "tokens",
            "consumed": None,
            "limit": (round3.get("budget") or {}).get("max_tokens"),
            "exhausted": False,
            "note": (
                "the round's OWN finite budget with the closing partition"
                " reserving 90000 tokens / 6 calls for the review"
            ),
            "recorded_in": "phases.round3.budget",
        },
    ]
    collaboration_labels = [
        {
            "root_run_id": _ROOT_RUN,
            "provider": "gitlab",
            "project_ref": str((setup.get("project") or {}).get("id") or "160"),
            "issue_iid": (delivery.get("issue") or {}).get("iid"),
            "source_branch": "factory/5/7319478e",
            "mr_iid": (delivery.get("mr") or {}).get("iid"),
            "mr_state": (delivery.get("mr") or {}).get("state"),
            "mr_draft": (delivery.get("mr") or {}).get("draft"),
            "recorded_in": (
                "phases.delivery.mr + phases.negative.typed_conflict (the"
                " branch names the root) — the lineage's ONE MR, never moved"
            ),
        }
    ]
    cap = spend.get("cap_usd") if isinstance(spend.get("cap_usd"), (int, float)) else None
    recorded_totals = {
        "qualifying_lane_spend_usd": spend.get("qualifying_lane_spend_usd"),
        "all_attempt_total_usd": spend.get("all_attempt_total_usd"),
        "failed_attempt_lane_spend_usd": spend.get("failed_attempt_lane_spend_usd"),
        "cap_usd": cap,
    }
    budget_policy = {
        "version": policy_version,
        "profiles": profiles,
        "closing_partition": {
            "version": "closing-partition/1",
            "reserved_calls": 6,
            "reserved_tokens": 90000,
        },
        "estimate_policy": (
            "no rate card is armed for this capture — every priced entry is"
            " provider-reported (the SDK's own meter); an estimate would"
            " require a NEW versioned card (versions are additive, history"
            " never rewritten)"
        ),
        "recorded_in": "phases.align.receipts.pins + phases.round2.budget",
    }

    records = {"works": works, "attempts": attempts, "receipts": receipts, "calls": [], "spans": []}
    measurement = MeasurementLinker().link(**records)
    report = EconomicsLinker().link(
        measurement,
        pilot={
            "population": "review_loop_2026_09_27",
            "issue": "R41-13 / #368 (the #364 review-loop live capture)",
            "evidence": "docs/evaluation/2026-09-27-review-loop/",
            "profile_version": "claude-sdk-lane@forge-0.41.0 / glm-5.3-flash",
        },
    )
    ledger = AcceptedTaskLedgerBuilder().build(
        report,
        native_jobs=native_jobs,
        candidates=candidates,
        verifications=verifications,
        human_decisions=human_decisions,
        budget_events=budget_events,
        time_windows=time_windows,
        measurement_ledger=measurement,
    )
    document = accepted_ledger_v2_document(
        ledger,
        lineage_runs=lineage_runs,
        round_budgets=round_budgets,
        budget_amendments=budget_amendments,
        exposure_rows=exposure_rows,
        collaboration_labels=collaboration_labels,
        budget_policy=budget_policy,
        recorded_totals=recorded_totals,
        cap_usd=float(cap) if cap is not None else None,
        pilot={
            "population": "review_loop_2026_09_27",
            "issue": "R41-13 / #368 (the #364 review-loop live capture)",
            "evidence": (
                "docs/evaluation/2026-09-27-review-loop/ (live-run-evidence.json"
                " + traces) + qualification/records/review-loop-2026-09-27.json"
            ),
            "recorded_cap_usd": cap,
        },
    )
    document["measurement_ledger"] = measurement.to_document()
    # the in-window foreign-parent lane job (issue 4's delivery, job 1148)
    # ran but the capture's own spend population excludes it — noted,
    # never silently dropped and never folded into this window's totals
    notes = list(document.get("notes") or [])
    notes.append(
        "run 76a1088a (issue 4's delivery, lane job 1148 — wheel 2616d221)"
        " also ran inside this window (its trace is recorded); the capture's"
        " own spend population EXCLUDES it — its economics ride its own"
        " lineage, never this window's totals"
    )
    document["notes"] = sorted(set(notes))
    return document


def _print_review_loop_summary(document: dict[str, Any]) -> None:
    lineage = document["lineage"]
    programme = document["costs"]["programme"]
    columns = programme["columns"]
    coverage = programme["coverage"]
    measures = document["time_measures"]
    print(f"wrote artifact:        {document['pilot'].get('artifact')}")
    print(f"  schema:             {document['schema']}")
    print(f"  identity chain:     {' → '.join(document['identity_chain'])}")
    print(f"  works/attempts:     {programme['works']} / {coverage['attempts']}")
    print(f"  receipt coverage:   {coverage['receipt_coverage']}")
    print(
        f"  provider-reported:  {columns['provider_reported_usd']['known_lower_bound_usd']}"
        f" usd lower bound (exact={columns['provider_reported_usd']['exact']})"
    )
    for root_id, root_task in sorted(lineage["root_tasks"].items()):
        all_attempt = root_task["all_attempt"]["columns"]["provider_reported_usd"]
        print(
            f"  root task {root_id[:8]}:    {root_task['attempts']} attempts"
            f" ({len(root_task['prior_failed_attempts'])} prior failed +"
            f" {len(root_task['rounds'])} rounds), all-attempt provider-reported"
            f" {all_attempt['usd']} usd (exact={all_attempt['exact']})"
        )
        for round_row in root_task["rounds"]:
            cost = round_row["incremental_cost"]["provider_reported_usd"]
            label = (
                "delivery 1"
                if round_row["relation"] == "root"
                else f"round {round_row['round_number']}"
            )
            print(
                f"    {label:<11} {round_row['round_outcome']:<16}"
                f" {cost['usd']} usd (candidate {(round_row['candidate_sha'] or 'none')[:8]})"
            )
        window = root_task["budget_window"]
        print(
            f"    budget cap:       {window['cap_usd']} usd, policy {window['policy'].get('version')}"
        )
        for amendment in window["amendments"]:
            print(
                f"    amendment:        {amendment['axis']} {amendment['command_id']}"
                f" {amendment['limit_before'].get('max_tokens')}→{amendment['limit_after'].get('max_tokens')}"
                f" ({amendment['status']})"
            )
        closing = window["exposure"]["closing_budget"]
        print(
            f"    exposure:         settled {closing['settled_usd']} / accrued-unsettled"
            f" {closing['accrued_unsettled_usd']} / retained {closing['retained_liability_usd']}"
            f" usd; unknown intervals {closing['unknown_intervals']}"
        )
        no_double = root_task["no_double_count"]
        print(
            f"    no double count:  receipts {no_double['receipt_ids']['distinct']}/"
            f"{no_double['receipt_ids']['total']} unique, jobs"
            f" {no_double['native_job_ids']['distinct']}/{no_double['native_job_ids']['total']},"
            f" labels {no_double['delivery_labels_per_lineage']}"
        )
    for run_id, row in sorted(lineage["programme_side"].items()):
        print(
            f"  programme side {run_id[:8]}: {row['round_outcome']},"
            f" receipts {row['coverage']['receipts_received']} — spend unknown, bounded, never zero"
        )
    reconciliation = lineage["window_reconciliation"]
    print(f"  recorded totals:    {reconciliation['recorded_totals']}")
    print(
        f"  unreceipted resid:  {reconciliation.get('unreceipted_residual_usd')} usd"
        f" → {reconciliation.get('attributed_to_runs')}"
    )
    for measure in TIME_MEASURES:
        row = measures[measure]
        total = row["stage_total_seconds"]
        lower = row["stage_lower_bound_seconds"]
        shape = f"total {total}" if total is not None else f"lower bound {lower}"
        print(f"  {measure:<16} {shape} s (measured={row['measured']})")
    observability = document["observability"]
    print(
        f"  human review+rescue:{observability['human.review_and_rescue_minutes']['minutes']}"
        f" min (rescue {observability['human.review_and_rescue_minutes']['manual_rescue_minutes']},"
        f" reviewer {observability['human.review_and_rescue_minutes']['reviewer_effort_minutes']})"
    )
    print(f"  identity gaps:      {observability['identity.gaps']}")
    print(
        f"  throughput rows:    {len(document['throughput']['rows'])} (durations and receipts never join → null)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="the artifact output path")
    parser.add_argument(
        "--population",
        choices=("useful-wip", "review-loop"),
        default="useful-wip",
        help="which recorded capture to fold (review-loop emits the v2 lineage document)",
    )
    args = parser.parse_args(argv)
    out_path = args.out if args.out.is_absolute() else REPO_ROOT / args.out

    if args.population == "review-loop":
        document = review_loop_population()
        if document is None:
            print(
                "the #364 review-loop evidence has not landed — nothing to build", file=sys.stderr
            )
            return 1
        document["pilot"]["artifact"] = str(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8"
        )
        _print_review_loop_summary(document)
        return 0

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
    for measure in TIME_MEASURES:
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
