"""R36-12 / #271 — the LIVE research cohort: capture, blinding, replay.

The live cohort reuses the replay evaluator's grading machinery over
runs that were CAPTURED (not authored) under a frozen
:class:`~forge.adaptive.research_cohort.Preregistration`:

- PRE-REGISTRATION — the contract is frozen and hash-recorded before
  capture; a tampered document refuses to load, a policy change is a NEW
  recorded generation, and :meth:`capture_arm` refuses to start without
  the contract, outside its frozen task set / arms / digests;
- PROVENANCE — a capture with no gateway env is
  ``offline-scripted-model`` (a reactive script over the REAL
  ``run_research_pass``/``_execute_call`` loop, the RC-08 recipe), a
  gateway/injected completion is ``live-model`` with its route identity;
  an unlabelled live run (gateway URL without a model) is refused;
- BLINDING — the review package is anonymized and counterbalanced, the
  arm-inferrability scan finds NOTHING to unblind from, the same seed
  reproduces the same package, and reviewer grades land as structured
  records (a missing reviewer leaves correction unknown);
- HONEST DENOMINATORS — failed arms stay in the outcome denominator
  with their spend, unknown usage stays a lower bound, mixed provenance
  and mixed generations never pool;
- REPLAY — the shipped cohort round-trips (loader → grading → report
  with ``review_pending``), re-captures byte-identically, and the
  report replays deterministically with the human promotion decision
  still pending.

R37-11 / #292 adds the gen-2 LIVE cohort (``live-cohort-v2``): the same
machinery with the REAL model in EVERY arm — none/lexical/research
differ only in what their discovery phase established, so the captured
comparison is planner-MODE, not model.  Every model call rides a hard
spend ledger (per-arm cap + a ≤$3 cohort cap, worst-case projection
before the call, receipt after it); failed, truncated and capped
attempts are PRESERVED with their receipts; the shipped cohort's
report stays HOLD — pending-human — until real reviewers fill the blind
package's unfilled forms.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge.adaptive.research_cohort import (
    ARCHETYPE_AMBIGUOUS_REQUIREMENT,
    ARCHETYPE_DEEP_FILE_EVIDENCE,
    ARCHETYPE_IRRELEVANT_REPOSITORY,
    ARCHETYPE_NEIGHBOR_DEPENDENCY,
    BASELINE_MODE,
    BLIND_PACKAGE_SCHEMA,
    CANDIDATE_MODE,
    COHORT_MODES,
    COHORT_SCHEMA,
    DEFAULT_RUBRICS,
    MODE_NONE,
    PROVENANCE_LIVE_MODEL,
    PROVENANCE_OFFLINE_SCRIPTED,
    PREREGISTRATION_SCHEMA,
    REVIEW_PENDING,
    REVIEW_RECORDED,
    VERDICT_HOLD,
    CohortRunner,
    CohortSpec,
    CohortSpecError,
    CohortTask,
    Preregistration,
    PreregTaskBinding,
    PromotionPolicy,
    RecordedRun,
    Snapshot,
    SurfaceRef,
    aggregate_mode,
    arm_inferrability_scan,
    blind_review_package,
    grade_run,
    merge_review_grades,
    unblind_package,
)
from forge.adaptive.research_cohort_live import (
    FORGE_RESEARCH_LIVE_GATEWAY_MODEL_ENV,
    FORGE_RESEARCH_LIVE_GATEWAY_URL_ENV,
    PLAN_SYNTHESIS_POLICY,
    PRICES_USD_PER_MTOK,
    REACTIVE_DEEP_READ,
    ScriptedInvestigation,
    SpendCapReached,
    SpendLedger,
    capture_arm,
    capped_completion,
    model_plan_document,
    plan_synthesis_prompt,
    prompt_policy_digest,
    resolve_live_gateway,
)
from forge.adaptive.research_planner import ToolObservation

REPO_ROOT = Path(__file__).resolve().parents[1]
COHORT_DIR = REPO_ROOT / "evaluation" / "research_cohort"
SNAPSHOTS_DIR = COHORT_DIR / "snapshots"
LIVE_DIR = COHORT_DIR / "live"
RECORDED_DIR = LIVE_DIR / "recorded"

LIVE_BUDGET = {"max_calls": 10, "wall_seconds": 90.0}
ORDERING_SEED = 20260924
CAPTURED_AT = "2026-09-24"

RC_08 = "RC-08-offline-neighbor-deadline-policy"
RC_09 = "RC-09-live-ambiguous-refund-grace"
RC_10 = "RC-10-live-irrelevant-docs-site"
RC_11 = "RC-11-live-deep-lapse-handler"
RC_12 = "RC-12-live-holdout-neighbor-retry"


# ---------------------------------------------------------------------------
# The frozen cohort definition (task set + criteria + scripts)
# ---------------------------------------------------------------------------


def _task(
    task_id: str,
    archetype: str,
    snapshot_ref: str,
    statement: str,
    surface: tuple[tuple[str, str], ...],
    *,
    held_back: bool = False,
    missing_decision: str = "",
    terms: tuple[str, ...] = (),
    irrelevant: tuple[str, ...] = (),
) -> CohortTask:
    return CohortTask(
        task_id=task_id,
        archetype=archetype,
        statement=statement,
        snapshot_digest=Snapshot.load(SNAPSHOTS_DIR / snapshot_ref).digest,
        snapshot_ref=snapshot_ref,
        rubric=DEFAULT_RUBRICS[archetype],
        held_back=held_back,
        expected_surface=tuple(SurfaceRef(repo, path) for repo, path in surface),
        missing_decision=missing_decision,
        missing_decision_terms=terms,
        irrelevant_repos=irrelevant,
    )


def live_tasks() -> tuple[CohortTask, ...]:
    """The live cohort's task set — the SAME frozen snapshots the v1
    fixtures own, re-captured live under the preregistration."""
    return (
        _task(
            RC_08,
            ARCHETYPE_NEIGHBOR_DEPENDENCY,
            "snap-offline-neighbor.json",
            "Orders' dispatch must enforce the courier deadline window Billing owns. "
            "The deadline policy is defined in the neighbor repository only.",
            (("billing", "src/courier.py"), ("orders", "src/dispatch.py")),
        ),
        _task(
            RC_09,
            ARCHETYPE_AMBIGUOUS_REQUIREMENT,
            "snap-checkout.json",
            "Support refund requests within the grace window after order expiry.",
            (("orders", "src/checkout.py"), ("billing", "src/subscriptions.py")),
            missing_decision="the grace-window length (7 or 30 days) and who absorbs "
            "the payment fee",
            terms=("grace window", "grace", "7", "30", "fee", "days"),
        ),
        _task(
            RC_10,
            ARCHETYPE_IRRELEVANT_REPOSITORY,
            "snap-checkout.json",
            "Stop publishing the expiry event for orders already cancelled at expiry "
            "time. The authorized repository set includes a docs site; it holds "
            "nothing either service reads.",
            (("orders", "src/timer.py"), ("orders", "src/handlers/expiry.py")),
            irrelevant=("docs-site",),
        ),
        _task(
            RC_11,
            ARCHETYPE_DEEP_FILE_EVIDENCE,
            "snap-checkout.json",
            "When the expiry timer fires for a pending order, the lapse path runs "
            "through a handler deep inside the handlers module — find the exact "
            "handler that performs the lapse and the notification it emits.",
            (("orders", "src/handlers/expiry.py"), ("orders", "src/timer.py")),
        ),
        _task(
            RC_12,
            ARCHETYPE_NEIGHBOR_DEPENDENCY,
            "snap-dispatch.json",
            "Orders' dispatch must apply the retry policy Billing owns. The backoff "
            "contract is defined in the neighbor repository, not in Orders.",
            (("orders", "src/dispatch.py"), ("billing", "src/retry.py")),
            held_back=True,
        ),
    )


ACCEPTANCE_CRITERIA: dict[str, tuple[str, ...]] = {
    RC_08: (
        "the plan's decisive claim cites Billing's courier policy at its actual "
        "bytes (a non-initial window of the neighbor file)",
        "the plan keeps the policy OWNED by Billing (imported, never redefined in Orders)",
    ),
    RC_09: (
        "the plan asks a SPECIFIC question naming the grace-window decision and the "
        "fee owner — an invented default fails the criterion",
    ),
    RC_10: (
        "the plan's impacted surface stays inside orders; dragging the docs site in expands scope",
    ),
    RC_11: ("the plan's decisive claim cites the deep handler window, not the file head",),
    RC_12: ("the plan's decisive claim cites Billing's retry policy bytes in the neighbor",),
}

#: The reactive offline investigations (the RC-08 recipe): fixed first-turn
#: calls, a REACTIVE deep read that pages around what the tools actually
#: returned, then an honest done turn.  Token counts are recorded per turn.
_SCRIPTS: dict[str, dict] = {
    RC_08: {
        "turns": (
            {
                "calls": [
                    {"tool": "grep", "repo": "billing", "args": {"pattern": "DEADLINE_POLICY"}},
                    {
                        "tool": "read_file",
                        "repo": "orders",
                        "args": {"path": "src/dispatch.py", "offset": 0, "length": 600},
                    },
                ]
            },
            REACTIVE_DEEP_READ,
            {
                "done": True,
                "summary": (
                    "Orders' dispatch imports COURIER_DEADLINE_POLICY from Billing "
                    "(src/dispatch.py); Billing defines it in src/courier.py at a "
                    "non-initial window — window_seconds 900, grace_seconds 60, "
                    "version v3. The decisive contract lives only in the neighbor."
                ),
                "assumptions": [],
                "contradictions": [],
            },
        ),
        "deep": ("billing", "src/courier.py", "COURIER_DEADLINE_POLICY"),
        "input_tokens": (640, 830, 410),
        "output_tokens": (96, 44, 130),
    },
    RC_09: {
        "turns": (
            {
                "calls": [
                    {
                        "tool": "read_file",
                        "repo": "orders",
                        "args": {"path": "src/checkout.py", "offset": 0, "length": 400},
                    },
                    {
                        "tool": "read_file",
                        "repo": "billing",
                        "args": {"path": "src/subscriptions.py", "offset": 0, "length": 400},
                    },
                ]
            },
            {
                "done": True,
                "summary": (
                    "Orders' checkout (src/checkout.py) leaves the grace-window rules "
                    "explicitly undecided, and Billing's subscriptions module "
                    "(src/subscriptions.py) lapses on the shared expiry event without "
                    "answering them either. Is the grace window 7 or 30 days after "
                    "expiry, and who absorbs the payment fee?"
                ),
                "assumptions": [],
                "contradictions": [],
            },
        ),
        "deep": None,
        "input_tokens": (700, 520),
        "output_tokens": (150, 210),
    },
    RC_10: {
        "turns": (
            {
                "calls": [
                    {
                        "tool": "read_file",
                        "repo": "docs-site",
                        "args": {"path": "index.md", "offset": 0, "length": 300},
                    },
                    {
                        "tool": "grep",
                        "repo": "orders",
                        "args": {"pattern": "handle_cancellation"},
                    },
                ]
            },
            REACTIVE_DEEP_READ,
            {
                "done": True,
                "summary": (
                    "Cancellation is marked by the handler deep in "
                    "orders/src/handlers/expiry.py (handle_cancellation) and the timer "
                    "publishes the expiry event from orders/src/timer.py; the docs "
                    "site describes the OLD lifecycle diagram and holds nothing either "
                    "service reads."
                ),
                "assumptions": [],
                "contradictions": [],
            },
        ),
        "deep": ("orders", "src/handlers/expiry.py", "handle_cancellation"),
        "input_tokens": (600, 880, 430),
        "output_tokens": (140, 60, 200),
    },
    RC_11: {
        "turns": (
            {
                "calls": [
                    {"tool": "find_symbol", "repo": "orders", "args": {"name": "handle_expiry"}},
                    {
                        "tool": "read_file",
                        "repo": "orders",
                        "args": {"path": "src/timer.py", "offset": 0, "length": 400},
                    },
                ]
            },
            REACTIVE_DEEP_READ,
            {
                "done": True,
                "summary": (
                    "The lapse is performed by handle_expiry deep in "
                    "orders/src/handlers/expiry.py — it notifies by email and lapses "
                    "the pending order; the expiry timer that fires it publishes from "
                    "orders/src/timer.py."
                ),
                "assumptions": [],
                "contradictions": [],
            },
        ),
        "deep": ("orders", "src/handlers/expiry.py", "handle_expiry"),
        "input_tokens": (610, 900, 460),
        "output_tokens": (120, 88, 240),
    },
    RC_12: {
        "turns": (
            {
                "calls": [
                    {"tool": "grep", "repo": "billing", "args": {"pattern": "RETRY_POLICY"}},
                    {
                        "tool": "read_file",
                        "repo": "orders",
                        "args": {"path": "src/dispatch.py", "offset": 0, "length": 700},
                    },
                ]
            },
            REACTIVE_DEEP_READ,
            {
                "done": True,
                "summary": (
                    "Billing owns the retry contract: RETRY_POLICY (base_seconds 2, "
                    "max_attempts 5, jitter full, version v2) is defined in "
                    "billing/src/retry.py; Orders' dispatch imports and applies it "
                    "between attempts."
                ),
                "assumptions": [],
                "contradictions": [],
            },
        ),
        "deep": ("billing", "src/retry.py", "RETRY_POLICY"),
        "input_tokens": (655, 845, 420),
        "output_tokens": (100, 90, 175),
    },
}


def scripted_model(task_id: str, snapshot: Snapshot) -> ScriptedInvestigation:
    spec = _SCRIPTS[task_id]
    return ScriptedInvestigation(
        snapshot,
        turns=spec["turns"],
        deep=spec["deep"],
        input_tokens=spec["input_tokens"],
        output_tokens=spec["output_tokens"],
    )


def build_preregistration() -> Preregistration:
    return Preregistration(
        cohort_id="research-cohort-live-v1",
        registered_at="2026-09-24T00:00:00+00:00",
        task_bindings=tuple(
            PreregTaskBinding(
                task_id=task.task_id,
                archetype=task.archetype,
                snapshot_digest=task.snapshot_digest,
                acceptance_criteria=ACCEPTANCE_CRITERIA[task.task_id],
            )
            for task in live_tasks()
        ),
        eligibility={
            "reviewer_role": "code owner (adaptive)",
            "independent_of_capture": True,
            "capture_author_ineligible": True,
        },
        budget=dict(LIVE_BUDGET),
        arms=COHORT_MODES,
        review_procedure={
            "blind": True,
            "counterbalanced": True,
            "ordering_seed": ORDERING_SEED,
            "correction_effort_procedure": (
                "the reviewer edits each plan to acceptance and records wall-clock "
                "minutes; the procedure is identical for every mode; a missing "
                "reviewer leaves correction unknown, never estimated"
            ),
            "rubric_forms": "task-specific acceptance criteria frozen in this preregistration",
            "evidence_checker_role": (
                "consistency check only — token overlap is not semantic entailment"
            ),
        },
        promotion_criteria={
            "margin": 0.05,
            "accuracy_floor": 0.8,
            "max_invented_default_delta": 0,
            "cost_bound": {"max_calls_total_per_mode": 60},
        },
        prompt_policy_digest=prompt_policy_digest(),
    )


def build_spec() -> CohortSpec:
    return CohortSpec(
        cohort_id="research-cohort-live-v1",
        recorded_at="2026-09-24T00:00:00+00:00",
        promotion=PromotionPolicy(
            owner="R36 review lead (adaptive)",
            hold_expires="2026-10-08",
            margin=0.05,
            accuracy_floor=0.8,
            budget=dict(LIVE_BUDGET),
        ),
        tasks=live_tasks(),
    )


async def capture_live_cohort() -> dict[str, dict[str, dict]]:
    """Re-capture the whole live cohort deterministically (no gateway)."""
    prereg = build_preregistration()
    captured: dict[str, dict[str, dict]] = {}
    for task in live_tasks():
        snapshot = Snapshot.load(SNAPSHOTS_DIR / task.snapshot_ref)
        for mode in COHORT_MODES:
            document = await capture_arm(
                prereg,
                task,
                mode,
                snapshot=snapshot,
                scripted_model=scripted_model(task.task_id, snapshot)
                if mode == CANDIDATE_MODE
                else None,
                captured_at=CAPTURED_AT,
            )
            captured.setdefault(task.task_id, {})[mode] = document
    return captured


def write_live_cohort(target: Path = LIVE_DIR) -> None:
    """Generate every shipped live-cohort artifact (the one-off writer)."""
    import asyncio

    prereg = build_preregistration()
    captured = asyncio.run(capture_live_cohort())
    (target / "recorded").mkdir(parents=True, exist_ok=True)
    (target / "preregistration.json").write_text(
        json.dumps(prereg.as_document(), indent=2) + "\n", encoding="utf-8"
    )
    (target / "cohort-live-v1.json").write_text(
        json.dumps(build_spec().as_document(), indent=2) + "\n", encoding="utf-8"
    )
    for task_id, mode_docs in captured.items():
        task_dir = target / "recorded" / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        for mode, document in mode_docs.items():
            (task_dir / f"{mode}.json").write_text(
                json.dumps(document, indent=2) + "\n", encoding="utf-8"
            )
    runner = live_runner()
    runner.run().write(target / "report.json")
    (target / "review-package.json").write_text(
        json.dumps(build_review_package(), indent=2) + "\n", encoding="utf-8"
    )


def live_runner() -> CohortRunner:
    return CohortRunner.from_directory(
        LIVE_DIR / "cohort-live-v1.json", SNAPSHOTS_DIR, RECORDED_DIR
    )


def build_review_package() -> dict:
    runner = CohortRunner.from_directory(
        LIVE_DIR / "cohort-live-v1.json", SNAPSHOTS_DIR, RECORDED_DIR
    )
    return blind_review_package(
        runner.spec,
        runner.runs,
        seed=ORDERING_SEED,
        package_id="research-cohort-live-v1-blind-review",
        acceptance_criteria=ACCEPTANCE_CRITERIA,
    )


def _checked_in_run(task_id: str, mode: str) -> dict:
    return json.loads((RECORDED_DIR / task_id / f"{mode}.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# R37-11 / #292 — the gen-2 LIVE cohort: the REAL model in every arm
# ---------------------------------------------------------------------------

LIVE_V2_DIR = COHORT_DIR / "live-cohort-v2"
LIVE_V2_SNAPSHOTS_DIR = LIVE_V2_DIR / "snapshots"
LIVE_V2_RECORDED_DIR = LIVE_V2_DIR / "recorded"
LIVE_V2_SNAPSHOT_REF = "snap-live2-refunds.json"
#: The sibling discovery-live fixture repositories, used READ-ONLY as the
#: frozen bytes of the gen-2 snapshot (R37-09 froze them; gen-2 re-uses
#: the same trees plus one NEW ambiguity task).
DISCOVERY_LIVE_FIXTURES = REPO_ROOT / "evaluation" / "discovery_live" / "fixtures"

#: The gen-2 budget: the call/wall caps of the frozen cohort PLUS the
#: R37-11 hard dollar caps (per-arm slice, cohort total ≤ $3) and the
#: per-call token caps the live capture rides under.
V2_BUDGET: dict[str, object] = {
    "max_calls": 12,
    "wall_seconds": 300.0,
    "max_tokens_per_call": 3000,
    "plan_max_tokens": 8000,
    "max_usd_per_arm": 0.40,
    "max_usd_total": 3.0,
}
V2_ORDERING_SEED = 20260925
V2_CAPTURED_AT = "2026-09-24"
V2_ISSUE = "R37-11 / #292"

RC_13 = "RC-13-live2-neighbor-refund-approval"
RC_14 = "RC-14-live2-ambiguous-approver"
RC_15 = "RC-15-live2-irrelevant-docs-site"
RC_16 = "RC-16-live2-deep-superseded-policy"
RC_17 = "RC-17-live2-holdout-neighbor-tier-window"


def _fixture_files(fixture: str) -> dict[str, str]:
    """The frozen bytes of one discovery-live fixture repository (read-only)."""
    root = DISCOVERY_LIVE_FIXTURES / fixture
    return {
        str(path.relative_to(root)): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _fixture_oid(files: Mapping[str, str]) -> str:
    """The frozen content identity (the discovery-live OID scheme)."""
    return hashlib.sha1(
        json.dumps(dict(files), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_v2_snapshot() -> Snapshot:
    """The gen-2 frozen snapshot: the three AUTHORIZED discovery-live
    repositories (writable orders-api, decisive neighbor billing-policy,
    irrelevant docs-site) — bytes re-derived from the fixtures at build
    time and shipped as ``snapshots/snap-live2-refunds.json``."""
    repos: dict[str, dict[str, Any]] = {}
    for key in ("orders-api", "billing-policy", "docs-site"):
        files = _fixture_files(key)
        repos[key] = {
            "repository_id": f"gitlab:{key}",
            "source_oid": _fixture_oid(files),
            "files": files,
        }
    return Snapshot(repos=repos)


def v2_snapshot() -> Snapshot:
    """The shipped gen-2 snapshot (loads off disk; falls back to build)."""
    path = LIVE_V2_SNAPSHOTS_DIR / LIVE_V2_SNAPSHOT_REF
    if path.is_file():
        return Snapshot.load(path)
    return build_v2_snapshot()


def _v2_task(
    task_id: str,
    archetype: str,
    statement: str,
    surface: tuple[tuple[str, str], ...],
    *,
    held_back: bool = False,
    missing_decision: str = "",
    terms: tuple[str, ...] = (),
    irrelevant: tuple[str, ...] = (),
) -> CohortTask:
    return CohortTask(
        task_id=task_id,
        archetype=archetype,
        statement=statement,
        snapshot_digest=v2_snapshot().digest,
        snapshot_ref=LIVE_V2_SNAPSHOT_REF,
        rubric=DEFAULT_RUBRICS[archetype],
        held_back=held_back,
        expected_surface=tuple(SurfaceRef(repo, path) for repo, path in surface),
        missing_decision=missing_decision,
        missing_decision_terms=terms,
        irrelevant_repos=irrelevant,
    )


def v2_tasks() -> tuple[CohortTask, ...]:
    """The gen-2 task set over the REAL frozen discovery-live snapshots.

    Four promotable archetypes plus one held-back neighbor task — the
    same structural contract as gen-1, over the sibling R37-09 froze
    for real (plus RC-14, the NEW ambiguity task: the approver for
    orders-api's manual-approval step is stated NOWHERE the planner can
    read — a specific question is the graded behavior, an invented
    default fails it).
    """
    return (
        _v2_task(
            RC_13,
            ARCHETYPE_NEIGHBOR_DEPENDENCY,
            "Support refund requests in Orders' checkout for orders that expired "
            "less than 30 days ago, wiring the request through to Billing's policy "
            "client. Finance said large refunds need an extra approval step before "
            "capture, but the issue does not say from which amount.",
            (("billing-policy", "src/policy/refunds.py"), ("orders-api", "src/checkout.py")),
        ),
        _v2_task(
            RC_14,
            ARCHETYPE_AMBIGUOUS_REQUIREMENT,
            "Route refund requests that need the manual approval step to whoever "
            "performs approvals for orders-api. The routing and its owner are not "
            "stated in this issue, and no repository the planner may read names the "
            "approver for orders-api — decide nothing silently.",
            (("orders-api", "src/checkout.py"), ("billing-policy", "src/policy/client.py")),
            missing_decision="which approvals queue/owner performs the manual "
            "approval step for orders-api refunds",
            terms=("approver", "approves", "approval owner", "approvals queue", "routing"),
        ),
        _v2_task(
            RC_15,
            ARCHETYPE_IRRELEVANT_REPOSITORY,
            "Surface Billing's manual-review decision verbatim in Orders' refund "
            "handlers. The authorized repository set also contains a docs site with "
            "a help-center refunds page; it is marketing content and holds nothing "
            "either service reads.",
            (
                ("orders-api", "src/orders/handlers/refunds.py"),
                ("orders-api", "src/checkout.py"),
            ),
            irrelevant=("docs-site",),
        ),
        _v2_task(
            RC_16,
            ARCHETYPE_DEEP_FILE_EVIDENCE,
            "Confirm the manual-approval threshold that applies to refunds today. "
            "The refunds policy file opens with superseded F-2023 and F-2024-02 "
            "revisions — the CURRENT rule is the F-2024-11 finance revision; cite "
            "its exact window, not the superseded history near the file head.",
            (("billing-policy", "src/policy/refunds.py"),),
        ),
        _v2_task(
            RC_17,
            ARCHETYPE_NEIGHBOR_DEPENDENCY,
            "Orders' checkout request gate must apply Billing's contract-tier "
            "request window instead of the order-side constant. The tier windows "
            "are defined in the neighbor policy module only.",
            (("billing-policy", "src/policy/refunds.py"), ("orders-api", "src/checkout.py")),
            held_back=True,
        ),
    )


V2_ACCEPTANCE_CRITERIA: dict[str, tuple[str, ...]] = {
    RC_13: (
        "the plan's decisive claim cites the neighbor policy module's CURRENT "
        "threshold window at its actual bytes (a non-initial window of the file)",
        "the plan keeps the approval policy OWNED by Billing (consumed over the "
        "policy client, never redefined in Orders)",
    ),
    RC_14: (
        "the plan asks a SPECIFIC question naming the approver/routing decision "
        "for orders-api — an invented default (auto-approval, approval-skipping) "
        "fails the criterion",
    ),
    RC_15: (
        "the plan's impacted surface stays inside orders-api/billing-policy; "
        "dragging the docs site in expands scope",
    ),
    RC_16: (
        "the plan's decisive claim cites the deep F-2024-11 window, not the "
        "superseded revisions near the file head",
    ),
    RC_17: ("the plan's decisive claim cites the neighbor's tier-window policy bytes",),
}


def build_v2_preregistration() -> Preregistration:
    """Generation 2: supersedes the SHIPPED gen-1 contract (its recorded
    digest), re-freezing the task set, the live budget caps and the
    changed plan-synthesis policy BEFORE any gen-2 capture ran."""
    gen1 = Preregistration.load(LIVE_DIR / "preregistration.json")
    return gen1.revise(
        "R37-11: gen-1 captured every arm offline-scripted with deterministic "
        "plan synthesis; gen-2 re-captures with the REAL model synthesizing "
        "every arm's plan (planner-mode comparison, not model) over the real "
        "frozen discovery-live snapshots, under hard dollar/token caps",
        task_bindings=tuple(
            PreregTaskBinding(
                task_id=task.task_id,
                archetype=task.archetype,
                snapshot_digest=task.snapshot_digest,
                acceptance_criteria=V2_ACCEPTANCE_CRITERIA[task.task_id],
            )
            for task in v2_tasks()
        ),
        budget=dict(V2_BUDGET),
        prompt_policy_digest=prompt_policy_digest(plan_synthesis=PLAN_SYNTHESIS_POLICY),
        promotion_criteria={
            "margin": 0.05,
            "accuracy_floor": 0.8,
            "max_invented_default_delta": 0,
            "cost_bound": {"max_calls_total_per_mode": 60},
        },
        review_procedure={
            "blind": True,
            "counterbalanced": True,
            "ordering_seed": V2_ORDERING_SEED,
            "correction_effort_procedure": (
                "the reviewer edits each plan to acceptance and records wall-clock "
                "minutes; the procedure is identical for every arm; a missing "
                "reviewer leaves correction unknown, never estimated"
            ),
            "rubric_forms": "task-specific acceptance criteria frozen in this preregistration",
            "evidence_checker_role": (
                "consistency check only — token overlap is not semantic entailment"
            ),
            "reviewer_identity_required": (
                "filled forms carry the reviewer's identity and timestamp; the "
                "shipped package is review_status=pending-human with every form "
                "unfilled — no fixture grades exist"
            ),
        },
        registered_at="2026-09-24T00:00:00+00:00",
    )


def build_v2_spec() -> CohortSpec:
    budget = dict(V2_BUDGET)
    return CohortSpec(
        cohort_id="research-cohort-live-v2",
        recorded_at="2026-09-24T00:00:00+00:00",
        promotion=PromotionPolicy(
            owner="R37 review lead (adaptive)",
            hold_expires="2026-10-22",
            margin=0.05,
            accuracy_floor=0.8,
            budget=budget,
        ),
        tasks=v2_tasks(),
    )


def v2_runner() -> CohortRunner:
    return CohortRunner.from_directory(
        LIVE_V2_DIR / "cohort-live-v2.json", LIVE_V2_SNAPSHOTS_DIR, LIVE_V2_RECORDED_DIR
    )


def v2_review_package() -> dict:
    """The shipped blind package: anonymized, counterbalanced, UNFILLED —
    and honestly marked pending-human (no reviewer identities/timestamps
    are fabricated; the procedure rides along for the humans who come)."""
    runner = v2_runner()
    package = blind_review_package(
        runner.spec,
        runner.runs,
        seed=V2_ORDERING_SEED,
        package_id="research-cohort-live-v2-blind-review",
        acceptance_criteria=V2_ACCEPTANCE_CRITERIA,
    )
    package["review_status"] = "pending-human"
    package["review_procedure"] = dict(runner.preregistration.review_procedure)  # type: ignore[arg-type]
    return package


async def capture_v2_cohort(
    *,
    env: Mapping[str, str] | None = None,
    completion: Any = None,
    completion_identity: str = "",
    captured_at: str = V2_CAPTURED_AT,
    per_arm_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    on_arm: Any = None,
) -> tuple[dict[str, dict[str, dict]], dict[str, Any]]:
    """Capture the WHOLE gen-2 cohort (5 tasks x 3 arms) through the REAL
    planner path with the REAL model, under ONE shared spend ledger.

    *completion* injects a fake gateway (the dry-run path); *env* rides
    the lab gateway (the live path).  Every arm is written through and
    returned — failures preserved, never retried.
    """
    prereg = build_v2_preregistration()
    ledger = SpendLedger(
        model=(completion_identity or "gateway").removeprefix("live:"),
        limit_usd=float(V2_BUDGET["max_usd_total"]),
    )
    snapshot = v2_snapshot()
    captured: dict[str, dict[str, dict]] = {}
    log: dict[str, Any] = {
        "schema": "forge.research.cohort.capture-log/1",
        "issue": V2_ISSUE,
        "captured_at": captured_at,
        "caps": dict(V2_BUDGET),
        "model_identity": completion_identity or "gateway",
        "arms": [],
    }
    for task in v2_tasks():
        for mode in COHORT_MODES:
            overrides = dict((per_arm_overrides or {}).get(f"{task.task_id}/{mode}") or {})
            document = await capture_arm(
                prereg,
                task,
                mode,
                snapshot=snapshot,
                env=env,
                completion=completion,
                completion_identity=completion_identity,
                captured_at=captured_at,
                spend=ledger,
                issue=V2_ISSUE,
                **overrides,
            )
            captured.setdefault(task.task_id, {})[mode] = document
            if on_arm is not None:
                # evidence is written AS IT LANDS — a later crash can never
                # lose an arm that already spent money
                on_arm(task.task_id, mode, document)
            run = RecordedRun.from_document(document)
            log["arms"].append(
                {
                    "task_id": task.task_id,
                    "mode": mode,
                    "outcome": run.outcome,
                    "stopped_reason": run.stopped_reason,
                    "plan_failure": str((document.get("capture") or {}).get("plan_failure") or ""),
                    "provenance": run.provenance,
                    "usd_estimated": document["attempts"][0]["cost"].get("usd_estimated"),
                    "receipts": document["attempts"][0]["cost"].get("receipts", []),
                }
            )
    log["totals"] = {
        "arms_captured": len(log["arms"]),
        "arms_graded": sum(1 for entry in log["arms"] if entry["outcome"] == "graded"),
        "arms_failed": sum(1 for entry in log["arms"] if entry["outcome"] == "failed"),
        "usd_estimated_total": round(ledger.spent_usd, 6),
        "cap_usd_total": ledger.limit_usd,
        "cap_exhausted": ledger.exhausted,
        "usd_by_mode": {
            mode: round(
                sum(
                    float(entry.get("usd_estimated") or 0)
                    for entry in log["arms"]
                    if entry["mode"] == mode
                ),
                6,
            )
            for mode in COHORT_MODES
        },
    }
    return captured, log


def write_v2_static_artifacts() -> None:
    """Write the static gen-2 artifacts (snapshot, preregistration, spec).

    Called BEFORE the live capture — the contract is frozen first, the
    captures land under it, then the report/package are derived.
    """
    LIVE_V2_SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    (LIVE_V2_SNAPSHOTS_DIR / LIVE_V2_SNAPSHOT_REF).write_text(
        json.dumps({"repos": build_v2_snapshot().repos}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    snapshot = v2_snapshot()
    for task in v2_tasks():  # the tasks must bind to the SHIPPED bytes
        assert task.snapshot_digest == snapshot.digest
    (LIVE_V2_DIR / "preregistration.json").write_text(
        json.dumps(build_v2_preregistration().as_document(), indent=2) + "\n", encoding="utf-8"
    )
    (LIVE_V2_DIR / "cohort-live-v2.json").write_text(
        json.dumps(build_v2_spec().as_document(), indent=2) + "\n", encoding="utf-8"
    )


def write_v2_recorded(captured: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> None:
    for task_id, mode_docs in captured.items():
        task_dir = LIVE_V2_RECORDED_DIR / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        for mode, document in mode_docs.items():
            (task_dir / f"{mode}.json").write_text(
                json.dumps(document, indent=2) + "\n", encoding="utf-8"
            )


def write_v2_report_and_package(log: Mapping[str, Any]) -> None:
    (LIVE_V2_DIR / "capture-log.json").write_text(
        json.dumps(log, indent=2) + "\n", encoding="utf-8"
    )
    v2_runner().run().write(LIVE_V2_DIR / "report.json")
    package = v2_review_package()
    assert arm_inferrability_scan(package) == [], "the shipped package must be scan-clean"
    (LIVE_V2_DIR / "review-package.json").write_text(
        json.dumps(package, indent=2) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Pre-registration: immutability, generations, capture refusal
# ---------------------------------------------------------------------------


class TestPreregistration:
    def test_the_shipped_preregistration_loads_and_validates(self):
        prereg = Preregistration.load(LIVE_DIR / "preregistration.json")
        assert prereg.schema == PREREGISTRATION_SCHEMA
        assert prereg.generation == 1
        assert prereg.supersedes == ""
        assert prereg.digest == build_preregistration().digest
        assert prereg.prompt_policy_digest == prompt_policy_digest()
        assert {b.task_id for b in prereg.task_bindings} == {task.task_id for task in live_tasks()}

    def test_a_tampered_preregistration_refuses_to_load(self):
        doc = json.loads((LIVE_DIR / "preregistration.json").read_text(encoding="utf-8"))
        doc["promotion_criteria"]["margin"] = 0.0  # quietly lower the bar…
        with pytest.raises(CohortSpecError, match="digest mismatch"):
            Preregistration.from_document(doc)

    def test_a_policy_change_is_a_new_recorded_generation(self):
        prereg = build_preregistration()
        revised = prereg.revise(
            "tightened the promotion margin before any verdict",
            promotion_criteria={**prereg.promotion_criteria, "margin": 0.10},
        )
        assert revised.generation == prereg.generation + 1
        assert revised.supersedes == prereg.digest
        assert revised.change_reason.startswith("tightened")
        assert revised.digest != prereg.digest
        with pytest.raises(CohortSpecError, match="change reason"):
            prereg.revise("")

    def test_preregistration_validation_refuses_gaps(self):
        good = build_preregistration()

        def _validated(**overrides: object) -> None:
            Preregistration(**{**good.__dict__, **overrides}).validate()

        with pytest.raises(CohortSpecError, match="arms"):
            _validated(arms=("none", "research"))
        with pytest.raises(CohortSpecError, match="acceptance criteria"):
            _validated(
                task_bindings=(
                    PreregTaskBinding(
                        task_id=RC_08,
                        archetype=ARCHETYPE_NEIGHBOR_DEPENDENCY,
                        snapshot_digest=good.task_bindings[0].snapshot_digest,
                        acceptance_criteria=(),
                    ),
                )
            )
        with pytest.raises(CohortSpecError, match="blind"):
            _validated(review_procedure={"blind": False})
        with pytest.raises(CohortSpecError, match="prompt_policy_digest"):
            _validated(prompt_policy_digest="not-a-digest")

    async def test_capture_refuses_to_start_without_a_preregistration(self):
        task = live_tasks()[0]
        snapshot = Snapshot.load(SNAPSHOTS_DIR / task.snapshot_ref)
        with pytest.raises(CohortSpecError, match="pre-registered"):
            await capture_arm(None, task, CANDIDATE_MODE, snapshot=snapshot)

    async def test_capture_refuses_tasks_and_modes_outside_the_contract(self):
        prereg = build_preregistration()
        task = live_tasks()[0]
        snapshot = Snapshot.load(SNAPSHOTS_DIR / task.snapshot_ref)
        stranger = CohortTask(**{**task.__dict__, "snapshot_digest": "0" * 64})
        with pytest.raises(CohortSpecError, match="not the frozen"):
            await capture_arm(prereg, stranger, CANDIDATE_MODE, snapshot=snapshot)
        with pytest.raises(CohortSpecError, match="pre-registered arms"):
            await capture_arm(prereg, task, "agentic-free-run", snapshot=snapshot)
        with pytest.raises(CohortSpecError, match="re-derives"):
            await capture_arm(
                prereg,
                live_tasks()[1],  # bound to a DIFFERENT snapshot
                CANDIDATE_MODE,
                snapshot=snapshot,
            )


# ---------------------------------------------------------------------------
# Provenance: offline-scripted vs live-model, never mislabelled
# ---------------------------------------------------------------------------


class TestProvenance:
    async def test_offline_capture_is_labelled_offline_scripted(self):
        prereg = build_preregistration()
        task = live_tasks()[0]
        snapshot = Snapshot.load(SNAPSHOTS_DIR / task.snapshot_ref)
        document = await capture_arm(
            prereg,
            task,
            CANDIDATE_MODE,
            snapshot=snapshot,
            scripted_model=scripted_model(task.task_id, snapshot),
            captured_at=CAPTURED_AT,
            env={},  # no gateway configured
        )
        capture = document["capture"]
        assert capture["provenance"] == PROVENANCE_OFFLINE_SCRIPTED
        assert capture["live_provider"] is False
        assert capture["model_identity"].startswith("scripted:")
        assert capture["preregistration"] == {
            "digest": prereg.digest,
            "generation": prereg.generation,
        }
        assert capture["budget"] == dict(prereg.budget)
        assert capture["budget_spent"]["calls_executed"] == 3

    async def test_an_injected_completion_is_labelled_live_model(self):
        prereg = build_preregistration()
        task = live_tasks()[0]
        snapshot = Snapshot.load(SNAPSHOTS_DIR / task.snapshot_ref)
        document = await capture_arm(
            prereg,
            task,
            CANDIDATE_MODE,
            snapshot=snapshot,
            completion=scripted_model(task.task_id, snapshot),
            completion_identity="lab-glm-5.3",
            captured_at=CAPTURED_AT,
        )
        capture = document["capture"]
        assert capture["provenance"] == PROVENANCE_LIVE_MODEL
        assert capture["live_provider"] is True
        assert capture["model_identity"] == "live:lab-glm-5.3"
        assert capture["route"] == "injected-live-completion"

    async def test_offline_research_needs_a_script(self):
        prereg = build_preregistration()
        task = live_tasks()[0]
        snapshot = Snapshot.load(SNAPSHOTS_DIR / task.snapshot_ref)
        with pytest.raises(CohortSpecError, match="no live gateway"):
            await capture_arm(prereg, task, CANDIDATE_MODE, snapshot=snapshot, env={})

    def test_a_gateway_url_without_a_model_identity_is_refused(self):
        with pytest.raises(CohortSpecError, match="model identity"):
            resolve_live_gateway({FORGE_RESEARCH_LIVE_GATEWAY_URL_ENV: "http://litellm:4000"})

    def test_the_gateway_resolves_with_both_identities(self):
        gateway = resolve_live_gateway(
            {
                FORGE_RESEARCH_LIVE_GATEWAY_URL_ENV: "http://litellm:4000/",
                FORGE_RESEARCH_LIVE_GATEWAY_MODEL_ENV: "glm-5.3",
            }
        )
        assert gateway is not None
        assert gateway.base_url == "http://litellm:4000"
        assert gateway.model == "glm-5.3"
        assert gateway.route == "litellm-gateway:planner"

    async def test_every_shipped_run_carries_the_offline_label_and_the_digest(self):
        prereg = Preregistration.load(LIVE_DIR / "preregistration.json")
        for task in live_tasks():
            for mode in COHORT_MODES:
                run = RecordedRun.from_document(_checked_in_run(task.task_id, mode))
                assert run.provenance == PROVENANCE_OFFLINE_SCRIPTED
                assert run.preregistration_digest == prereg.digest
                assert run.preregistration_generation == 1
                assert run.review_pending is True


# ---------------------------------------------------------------------------
# Blinding: anonymized, counterbalanced, scan-checked
# ---------------------------------------------------------------------------


class TestBlinding:
    @pytest.fixture(scope="class")
    def package(self) -> dict:
        return json.loads((LIVE_DIR / "review-package.json").read_text(encoding="utf-8"))

    def test_the_package_is_anonymized_and_scan_clean(self, package):
        assert package["schema"] == BLIND_PACKAGE_SCHEMA
        assert package["ordering"]["counterbalanced"] is True
        assert package["ordering"]["seed"] == ORDERING_SEED
        # a reviewer CANNOT infer the mode from the package: the scan finds
        # no arm-correlated keys, values or machinery substrings anywhere
        assert arm_inferrability_scan(package) == []
        assert len(package["units"]) == 4 * len(COHORT_MODES)  # promotable tasks only

    def test_every_form_is_unfilled_review_pending(self, package):
        for unit in package["units"]:
            form = unit["form"]
            assert form["correction_minutes"] is None
            assert form["correction_severity"] is None
            assert all(entry["grade"] is None for entry in form["claim_importance"])
            assert all(entry["severity"] is None for entry in form["assumption_grades"])
            assert all(entry["specific"] is None for entry in form["question_specificity"])
            assert form["acceptance_criteria"]  # the frozen per-task criteria ride along

    def test_counterbalance_is_deterministic_and_rotated(self, package):
        again = blind_review_package(
            build_spec(),
            live_runner().runs,
            seed=ORDERING_SEED,
            acceptance_criteria=ACCEPTANCE_CRITERIA,
        )
        assert again == package  # same seed, same package
        # unblinding re-derives the SAME mapping, and each arm appears at
        # every within-block position across the task blocks (positions
        # are not arm-correlated — that is the counterbalance)
        mapping = unblind_package(package, build_spec())
        assert len(mapping) == len(package["units"])
        positions_by_mode: dict[str, set[int]] = {}
        for index, unit in enumerate(package["units"], start=1):
            _, mode = mapping[unit["review_id"]]
            positions_by_mode.setdefault(mode, set()).add(index % len(COHORT_MODES))
        for mode in COHORT_MODES:
            assert positions_by_mode[mode] == {0, 1, 2}

    def test_a_different_seed_reorders_the_package(self, package):
        other = blind_review_package(
            build_spec(),
            live_runner().runs,
            seed=ORDERING_SEED + 1,
            acceptance_criteria=ACCEPTANCE_CRITERIA,
        )
        assert other != package

    def test_reviewer_grades_land_as_structured_records(self, package):
        runner = live_runner()
        spec = runner.spec
        run_documents = {
            task.task_id: {
                mode: json.loads(
                    (RECORDED_DIR / task.task_id / f"{mode}.json").read_text(encoding="utf-8")
                )
                for mode in COHORT_MODES
            }
            for task in spec.promotable_tasks
        }
        # one filled form: the research unit of the FIRST task block
        mapping = unblind_package(package, spec)
        target_id = next(
            review_id for review_id, (_, mode) in mapping.items() if mode == CANDIDATE_MODE
        )
        filled = {
            target_id: {
                "claim_importance": [
                    {"claim_id": claim["claim_id"], "grade": "important"}
                    for claim in package["units"][int(target_id.split("-")[1]) - 1]["plan"][
                        "claims"
                    ]
                ],
                "assumption_grades": [],
                "question_specificity": [],
                "correction_minutes": 12,
                "correction_severity": "minor",
            }
        }
        updated = merge_review_grades(run_documents, package, spec, filled)
        task_id, mode = mapping[target_id]
        graded = RecordedRun.from_document(updated[task_id][mode])
        assert graded.review_pending is False
        assert graded.reviewer["correction_minutes"] == 12
        assert all(claim.get("importance") == "important" for claim in graded.plan["claims"])
        grade = grade_run(
            next(t for t in spec.tasks if t.task_id == task_id),
            graded,
            runner.snapshots[graded.snapshot_digest],
        )
        assert grade["review"]["state"] == REVIEW_RECORDED
        assert grade["human_plan_correction_effort"]["minutes"] == 12
        # a form WITHOUT correction minutes leaves the run pending — the
        # effort stays unknown, never estimated
        partial = merge_review_grades(
            run_documents, package, spec, {target_id: {"claim_importance": []}}
        )
        assert RecordedRun.from_document(partial[task_id][mode]).review_pending is True

    def test_merge_refuses_unknown_ids(self, package):
        runner = live_runner()
        with pytest.raises(CohortSpecError, match="unknown review id"):
            merge_review_grades(
                {t.task_id: {} for t in runner.spec.promotable_tasks},
                package,
                runner.spec,
                {"BR-999": {}},
            )


# ---------------------------------------------------------------------------
# Honest denominators: failures, unknown usage, no pooling
# ---------------------------------------------------------------------------


class TestHonestDenominators:
    async def test_a_failed_research_arm_stays_in_the_denominator_with_its_spend(self):
        prereg = build_preregistration()

        async def _dying_model(system: str, user: str) -> SimpleNamespace:
            raise RuntimeError("gateway exploded")

        task = live_tasks()[0]
        snapshot = Snapshot.load(SNAPSHOTS_DIR / task.snapshot_ref)
        document = await capture_arm(
            prereg, task, CANDIDATE_MODE, snapshot=snapshot, scripted_model=_dying_model
        )
        run = RecordedRun.from_document(document)
        assert run.outcome == "failed"
        assert run.stopped_reason == "gateway_error: RuntimeError"
        assert run.plan is None
        assert run.attempts[0]["cost"]["calls_proposed"] == 0  # spend kept, not hidden
        grade = grade_run(task, run, snapshot)
        assert grade["outcome"] == "failed"
        assert grade["cost"]["coverage"] == "recorded"
        assert "evidence_accuracy" not in grade  # no plan, no fabricated scores

    async def test_unknown_usage_stays_a_lower_bound(self):
        prereg = build_preregistration()
        task = live_tasks()[0]
        snapshot = Snapshot.load(SNAPSHOTS_DIR / task.snapshot_ref)
        spec = _SCRIPTS[task.task_id]
        # only the FIRST turn carries recorded usage — the rest is unknown
        starved = ScriptedInvestigation(
            snapshot,
            turns=spec["turns"],
            deep=spec["deep"],
            input_tokens=(640,),
            output_tokens=(96,),
        )
        document = await capture_arm(
            prereg, task, CANDIDATE_MODE, snapshot=snapshot, scripted_model=starved
        )
        tokens = document["attempts"][0]["cost"]["tokens"]
        assert tokens["input"] is None  # NOT silently re-totalled
        assert tokens["input_lower_bound"] == 640
        assert tokens["unknown_usage_calls"] >= 1

    async def test_an_attempt_without_cost_is_coverage_missing(self):
        prereg = build_preregistration()
        task = live_tasks()[0]
        snapshot = Snapshot.load(SNAPSHOTS_DIR / task.snapshot_ref)
        document = await capture_arm(
            prereg,
            task,
            BASELINE_MODE,
            snapshot=snapshot,
        )
        del document["attempts"][0]["cost"]
        run = RecordedRun.from_document(document)
        assert run.cost_complete() is False

    async def test_mixed_provenance_never_pools_into_one_score(self):
        prereg = build_preregistration()
        task = live_tasks()[0]
        snapshot = Snapshot.load(SNAPSHOTS_DIR / task.snapshot_ref)
        offline = await capture_arm(
            prereg,
            task,
            CANDIDATE_MODE,
            snapshot=snapshot,
            scripted_model=scripted_model(task.task_id, snapshot),
        )
        live_run = await capture_arm(
            prereg,
            task,
            CANDIDATE_MODE,
            snapshot=snapshot,
            completion=scripted_model(task.task_id, snapshot),
            completion_identity="lab-glm-5.3",
        )
        runs: dict[str, dict[str, RecordedRun]] = {
            task.task_id: {CANDIDATE_MODE: RecordedRun.from_document(offline)}
            if task.task_id == RC_08
            else {}
            for task in build_spec().tasks
        }
        # swap in the live run for a DIFFERENT task's research arm → the
        # mode's runs mix provenance labels inside one cohort
        spec = build_spec()
        other = next(t for t in spec.tasks if t.task_id == RC_09)
        runs[RC_09] = {
            CANDIDATE_MODE: RecordedRun.from_document(
                {**live_run, "task_id": RC_09, "snapshot_digest": other.snapshot_digest}
            )
        }
        from forge.adaptive.research_cohort import integrity_issues

        snapshots = {
            t.snapshot_digest: Snapshot.load(SNAPSHOTS_DIR / t.snapshot_ref) for t in spec.tasks
        }
        issues = integrity_issues(spec, runs, snapshots, None, prereg)
        assert any("MIXED provenance" in entry for entry in issues["provenance_violations"])

    async def test_a_generation_mismatch_is_an_integrity_violation(self):
        prereg = build_preregistration()
        task = live_tasks()[0]
        snapshot = Snapshot.load(SNAPSHOTS_DIR / task.snapshot_ref)
        document = await capture_arm(
            prereg,
            task,
            CANDIDATE_MODE,
            snapshot=snapshot,
            scripted_model=scripted_model(task.task_id, snapshot),
        )
        revised = prereg.revise("prompt policy moved between iterations")
        runs = {t.task_id: {} for t in build_spec().tasks}
        runs[task.task_id][CANDIDATE_MODE] = RecordedRun.from_document(document)
        from forge.adaptive.research_cohort import integrity_issues

        spec = build_spec()
        snapshots = {
            t.snapshot_digest: Snapshot.load(SNAPSHOTS_DIR / t.snapshot_ref) for t in spec.tasks
        }
        issues = integrity_issues(spec, runs, snapshots, None, revised)
        assert any(
            "captured under preregistration" in entry for entry in issues["provenance_violations"]
        )

    async def test_the_cost_bound_is_enforced(self):
        prereg = build_preregistration()
        tight = prereg.revise(
            "tightened the cost bound",
            promotion_criteria={
                **prereg.promotion_criteria,
                "cost_bound": {"max_calls_total_per_mode": 1},
            },
        )
        spec = CohortSpec(
            cohort_id=tight.cohort_id,
            recorded_at=tight.registered_at,
            promotion=PromotionPolicy(
                owner="R36 review lead (adaptive)",
                hold_expires="2026-10-08",
                margin=0.05,
                accuracy_floor=0.8,
                budget=dict(tight.budget),
            ),
            tasks=live_tasks(),
        )
        runs: dict[str, dict[str, RecordedRun]] = {t.task_id: {} for t in spec.tasks}
        snapshots = {}
        for task in spec.tasks:
            snapshot = Snapshot.load(SNAPSHOTS_DIR / task.snapshot_ref)
            snapshots[task.snapshot_digest] = snapshot
            runs[task.task_id] = {
                mode: RecordedRun.from_document(_checked_in_run(task.task_id, mode))
                for mode in COHORT_MODES
            }
        from forge.adaptive.research_cohort import integrity_issues

        issues = integrity_issues(spec, runs, snapshots, None, tight)
        assert any(
            "exceeds the pre-registered cost bound" in entry
            for entry in issues["preregistration_violations"]
        )


# ---------------------------------------------------------------------------
# The shipped cohort: regeneration, round-trip, replay determinism
# ---------------------------------------------------------------------------


class TestShippedLiveCohort:
    async def test_re_capturing_reproduces_the_checked_in_artifacts(self):
        """The recordings are REAL: re-running the deterministic offline
        capture over the frozen snapshots reproduces every checked-in
        run document byte-for-byte (canonical JSON)."""
        captured = await capture_live_cohort()
        for task_id, mode_docs in captured.items():
            for mode, document in mode_docs.items():
                checked_in = _checked_in_run(task_id, mode)
                assert json.dumps(document, sort_keys=True) == json.dumps(
                    checked_in, sort_keys=True
                ), f"{task_id}/{mode} drifted"

    def test_the_loader_round_trips_into_a_review_pending_report(self):
        runner = live_runner()
        report = runner.run().document
        checked_in = json.loads((LIVE_DIR / "report.json").read_text(encoding="utf-8"))
        assert report == checked_in  # the shipped report IS the replay

        assert report["schema"] == "forge.research.cohort.report/1"
        assert report["preregistration"]["generation"] == 1
        assert report["preregistration"]["digest"] == build_preregistration().digest
        # provenance separated, never pooled
        summary = report["provenance_summary"]
        for mode in COHORT_MODES:
            assert summary[mode] == {
                "live-model": 0,
                "offline-scripted-model": 4,
                "unlabeled": 0,
                "runs_total": 4,
            }
        # review pending everywhere: the reviewer-graded dimensions stay
        # unknown, the correction effort stays unknown
        for mode in COHORT_MODES:
            aggregate = report["aggregate"][mode]
            assert aggregate["review_pending_tasks"] == 4
            assert aggregate["correction_minutes_known_tasks"] == 0
            assert aggregate["correction_minutes_mean"] is None
            assert aggregate["overall_mean"] is None
            assert aggregate["evidence_accuracy_mean"] is None
        for section in report["tasks"].values():
            for grade in section["modes"].values():
                assert grade["review"]["state"] == REVIEW_PENDING
                assert grade["overall"] is None
        assert report["review"]["pending"]  # named task/mode entries

    def test_the_verdict_is_hold_until_the_human_decision_lands(self):
        promotion = live_runner().run().document["promotion"]
        assert promotion["verdict"] == VERDICT_HOLD
        assert promotion["evidence_class"] == "live-cohort:offline-scripted-model"
        assert promotion["human_promotion_decision"] == {
            "required": True,
            "recorded": False,
            "decision": None,
            "decided_by": None,
            "recorded_at": None,
        }
        assert any("review" in reason for reason in promotion["reasons"])

    def test_replay_is_deterministic_and_offline(self):
        runner = live_runner()
        first = runner.run().document
        second = runner.run().document
        assert first == second
        assert first["replay"] == {"pure_over_recorded": True, "live_calls": 0}

    def test_the_spec_loads_and_binds_to_the_preregistration(self):
        spec = CohortSpec.load(LIVE_DIR / "cohort-live-v1.json")
        assert spec.schema == COHORT_SCHEMA
        prereg = Preregistration.load(LIVE_DIR / "preregistration.json")
        for task in spec.tasks:
            assert prereg.binds(task)

    def test_research_observations_are_real_toolobservations(self):
        runner = live_runner()
        run = runner.runs[RC_08][CANDIDATE_MODE]
        assert run.observations
        assert all(isinstance(observation, ToolObservation) for observation in run.observations)
        assert all(observation.error == "" for observation in run.observations)
        deep = [o for o in run.observations if o.tool == "read_file" and o.repo_key == "billing"]
        assert deep, "the neighbor read rode the real tool loop"
        # the neighbor read paged into a NON-INITIAL window
        offset = int(deep[0].call.split("offset ")[1].split()[0])
        assert offset > 0

    def test_the_ambiguous_task_asks_and_the_irrelevant_task_leaks_scope(self):
        runner = live_runner()
        report = runner.run().document
        ambiguous = report["tasks"][RC_09]["modes"][CANDIDATE_MODE]
        assert ambiguous["review"]["state"] == REVIEW_PENDING
        questions = runner.runs[RC_09][CANDIDATE_MODE].plan["questions"]
        assert any("grace window" in str(q.get("text")) for q in questions)
        # the irrelevant-repository task: the run read the docs site, the
        # plan dragged it in, and the spurious surface is reported as such
        irrelevant = report["tasks"][RC_10]["modes"][CANDIDATE_MODE]["impacted_surface_recall"]
        assert irrelevant["spurious"] == [{"repo": "docs-site", "path": "index.md"}]

    def test_the_held_back_task_is_graded_but_separate(self):
        report = live_runner().run().document
        assert set(report["held_back"]["tasks"]) == {RC_12}
        for mode in COHORT_MODES:
            assert report["aggregate"][mode]["tasks_total"] == 4


# ---------------------------------------------------------------------------
# R37-11: the fake-gateway machinery (deterministic, no network, no spend)
# ---------------------------------------------------------------------------


class FakeGateway:
    """A deterministic injected completion — the dry-run/test double.

    Branches on the SYSTEM prompt (exactly like the real driver: the
    research loop and the plan synthesis ride the same seam): research
    turns answer with scripted tool calls and an honest done summary;
    the plan-synthesis turn answers with a plan grounded in the evidence
    the prompt actually carries.  Records every (system, user) pair so
    tests can assert WHAT each arm's planner was shown.
    """

    def __init__(
        self,
        *,
        plan_json: str | None = None,
        fail_plan: bool = False,
        raise_on_call: bool = False,
    ) -> None:
        self.prompts: list[tuple[str, str]] = []
        self.calls = 0
        self.research_turns = 0
        self._plan_json = plan_json
        self._fail_plan = fail_plan
        self._raise_on_call = raise_on_call

    def _plan_text(self, user: str) -> str:
        if self._plan_json is not None:
            return self._plan_json
        line = 218
        match = re.search(r"billing-policy src/policy/refunds\.py lines (\d+)\.", user)
        if match:
            line = int(match.group(1))
        return json.dumps(
            {
                "steps": [
                    {"step_id": "s1", "objective": "apply the neighbor threshold policy"},
                    {"step_id": "s2", "objective": "route manual-review refunds for decision"},
                ],
                "claims": [
                    {
                        "claim_id": "c1",
                        "text": "the manual-approval threshold is 5000 cents (F-2024-11)",
                        "repo": "billing-policy",
                        "path": "src/policy/refunds.py",
                        "line": line,
                        "asserted_content": ("REFUND_MANUAL_APPROVAL_THRESHOLD_CENTS = 5000"),
                    }
                ],
                "questions": ["Who performs the manual approval step for orders-api refunds?"],
                "assumptions": [],
            }
        )

    async def __call__(self, system: str, user: str) -> SimpleNamespace:
        self.prompts.append((system, user))
        self.calls += 1
        if self._raise_on_call:
            raise RuntimeError("gateway exploded")
        if system == plan_synthesis_prompt(with_evidence_refs=True) or system.startswith(
            "You are the planning stage"
        ):
            text = "not json at all" if self._fail_plan else self._plan_text(user)
            return SimpleNamespace(text=text, input_tokens=900, output_tokens=400)
        # research turns: one targeted grep, then done with an honest summary
        # (keyed on the RESEARCH turn index — the seam is shared with plan
        # synthesis, so a global counter would break under arm reordering)
        self.research_turns += 1
        if self.research_turns == 1:
            return SimpleNamespace(
                text=json.dumps(
                    {
                        "calls": [
                            {
                                "tool": "grep",
                                "repo": "billing-policy",
                                "args": {"pattern": "REFUND_MANUAL_APPROVAL_THRESHOLD"},
                            }
                        ]
                    }
                ),
                input_tokens=800,
                output_tokens=90,
            )
        return SimpleNamespace(
            text=json.dumps(
                {
                    "done": True,
                    "summary": (
                        "The current manual-approval threshold (F-2024-11, 5000 cents) "
                        "is defined only in billing-policy/src/policy/refunds.py. Who "
                        "performs the approval step for orders-api remains undecided."
                    ),
                    "assumptions": [],
                    "contradictions": [],
                }
            ),
            input_tokens=1600,
            output_tokens=180,
        )


# ---------------------------------------------------------------------------
# R37-11: pre-registration generation 2
# ---------------------------------------------------------------------------


def _v2_prereg_shipped() -> bool:
    return (LIVE_V2_DIR / "preregistration.json").is_file()


class TestPreregistrationGen2:
    def test_the_shipped_gen2_loads_validates_and_supersedes_gen1(self):
        if not _v2_prereg_shipped():
            pytest.skip("the gen-2 preregistration has not been frozen yet")
        prereg = Preregistration.load(LIVE_V2_DIR / "preregistration.json")
        gen1 = Preregistration.load(LIVE_DIR / "preregistration.json")
        assert prereg.generation == 2
        assert prereg.supersedes == gen1.digest
        assert "R37-11" in prereg.change_reason
        assert prereg.digest == build_v2_preregistration().digest
        assert dict(prereg.budget) == dict(V2_BUDGET)
        assert {b.task_id for b in prereg.task_bindings} == {t.task_id for t in v2_tasks()}
        assert all(prereg.binds(task) for task in v2_tasks())
        # the spec IS the frozen contract
        spec = CohortSpec.load(LIVE_V2_DIR / "cohort-live-v2.json")
        assert dict(spec.promotion.budget) == dict(prereg.budget)

    def test_the_gen2_policy_digest_differs_from_gen1(self):
        # the plan-synthesis policy changed (the model now writes the plan):
        # a changed policy MUST move the digest — a silent equal digest would
        # hide the change the new generation is supposed to record
        assert (
            build_v2_preregistration().prompt_policy_digest
            != build_preregistration().prompt_policy_digest
        )

    def test_gen2_validation_enforces_the_dollar_and_token_caps(self):
        good = build_v2_preregistration()

        def _validated(**overrides: object) -> None:
            Preregistration(**{**good.__dict__, **overrides}).validate()

        with pytest.raises(CohortSpecError, match="max_usd_total"):
            _validated(budget={**good.budget, "max_usd_total": 5.0})
        with pytest.raises(CohortSpecError, match="max_usd_per_arm may not exceed"):
            _validated(budget={**good.budget, "max_usd_total": 1.0, "max_usd_per_arm": 2.0})
        with pytest.raises(CohortSpecError, match="plan_max_tokens"):
            _validated(budget={**good.budget, "plan_max_tokens": 0})

    def test_a_tampered_gen2_preregistration_refuses_to_load(self):
        if not _v2_prereg_shipped():
            pytest.skip("the gen-2 preregistration has not been frozen yet")
        doc = json.loads((LIVE_V2_DIR / "preregistration.json").read_text(encoding="utf-8"))
        doc["budget"]["max_usd_total"] = 99.0  # quietly raise the ceiling…
        with pytest.raises(CohortSpecError, match="digest mismatch"):
            Preregistration.from_document(doc)


# ---------------------------------------------------------------------------
# R37-11: live capture provenance, caps, preserved failures
# ---------------------------------------------------------------------------


class TestLiveCaptureV2:
    @pytest.fixture()
    def v2_prereg(self) -> Preregistration:
        return build_v2_preregistration()

    async def test_every_arm_synthesizes_its_plan_through_the_model(self, v2_prereg):
        task = v2_tasks()[0]  # RC-13
        snapshot = v2_snapshot()
        fake = FakeGateway()
        ledger = SpendLedger(model="fake", limit_usd=3.0, prices=dict(PRICES_USD_PER_MTOK))
        documents = {
            mode: await capture_arm(
                v2_prereg,
                task,
                mode,
                snapshot=snapshot,
                completion=fake,
                completion_identity="dry-run-fake",
                captured_at=V2_CAPTURED_AT,
                spend=ledger,
                issue=V2_ISSUE,
            )
            for mode in COHORT_MODES
        }
        for mode, document in documents.items():
            capture = document["capture"]
            assert capture["provenance"] == PROVENANCE_LIVE_MODEL
            assert capture["live_provider"] is True
            assert capture["plan_synthesis"] == PLAN_SYNTHESIS_POLICY
            assert capture["issue"] == V2_ISSUE
            assert capture["preregistration"]["generation"] == 2
            cost = document["attempts"][0]["cost"]
            assert cost["usd_coverage"] == "receipted"
            assert cost["usd_estimated"] > 0
            assert cost["receipts"]
        # the plans are MODEL plans (synthesis marker), grounded per arm
        for mode, document in documents.items():
            assert isinstance(document["plan"], Mapping)
            assert document["plan"]["synthesis"] == "live-model/v1"
        # the research arm spent the research loop + the synthesis; the
        # none/lexical arms spent exactly the one synthesis call
        assert len(documents[CANDIDATE_MODE]["attempts"][0]["cost"]["receipts"]) >= 3
        for mode in (MODE_NONE, BASELINE_MODE):
            assert len(documents[mode]["attempts"][0]["cost"]["receipts"]) == 1
        # the shared ledger carries every arm's spend
        assert len(ledger.receipts) == sum(
            len(documents[mode]["attempts"][0]["cost"]["receipts"]) for mode in COHORT_MODES
        )

    async def test_the_arms_differ_only_in_what_discovery_established(self, v2_prereg):
        """Planner-MODE comparison, not model: the three arms ride the SAME
        completion seam; only the evidence block in the plan prompt
        differs (none: statement only; lexical: probe hits; research:
        findings + observations)."""
        task = v2_tasks()[0]
        snapshot = v2_snapshot()
        prompts: dict[str, str] = {}
        for mode in COHORT_MODES:
            fake = FakeGateway()
            await capture_arm(
                v2_prereg,
                task,
                mode,
                snapshot=snapshot,
                completion=fake,
                completion_identity="dry-run-fake",
                captured_at=V2_CAPTURED_AT,
            )
            plan_prompt = next(
                user
                for system, user in fake.prompts
                if system.startswith("You are the planning stage")
            )
            prompts[mode] = plan_prompt
        assert "REFUND_MANUAL_APPROVAL_THRESHOLD" not in prompts[MODE_NONE]
        assert "DISCOVERY EVIDENCE" in prompts[MODE_NONE]
        assert "LEXICAL PROBE EVIDENCE" in prompts[BASELINE_MODE]
        assert "RESEARCH EVIDENCE" in prompts[CANDIDATE_MODE]
        assert "ev-1" in prompts[CANDIDATE_MODE]  # the citable evidence ids rode along

    async def test_the_hard_spend_cap_stops_the_arm_before_the_call(self, v2_prereg):
        task = v2_tasks()[0]
        snapshot = v2_snapshot()
        # a total cap so small the FIRST research call's worst-case
        # projection already refuses — the arm stops honestly, spend kept
        ledger = SpendLedger(model="fake", limit_usd=0.001)
        document = await capture_arm(
            v2_prereg,
            task,
            CANDIDATE_MODE,
            snapshot=snapshot,
            completion=FakeGateway(),
            completion_identity="dry-run-fake",
            captured_at=V2_CAPTURED_AT,
            spend=ledger,
        )
        run = RecordedRun.from_document(document)
        assert run.outcome == "failed"
        assert run.stopped_reason == "spend_cap"
        assert run.plan is None
        assert document["attempts"][0]["cost"]["usd_coverage"] == "none-recorded"
        assert document["capture"]["plan_failure"]
        assert ledger.receipts == []  # refused BEFORE the provider was contacted

    async def test_the_per_arm_cap_bounds_each_arms_slice(self, v2_prereg):
        task = v2_tasks()[0]
        snapshot = v2_snapshot()
        # shared ledger already deep into a tiny per-arm allowance: the
        # plan synthesis must refuse rather than bust the arm's slice
        ledger = SpendLedger(model="fake", limit_usd=3.0)
        ledger.spent_usd = 2.95
        tight = v2_prereg.revise(
            "tightened the per-arm allowance for the cap test",
            budget={**v2_prereg.budget, "max_usd_per_arm": 0.02},
        )
        document = await capture_arm(
            tight,
            task,
            MODE_NONE,
            snapshot=snapshot,
            completion=FakeGateway(),
            completion_identity="dry-run-fake",
            captured_at=V2_CAPTURED_AT,
            spend=ledger,
        )
        run = RecordedRun.from_document(document)
        assert run.outcome == "failed"
        assert "spend_cap" in run.stopped_reason
        assert document["capture"]["plan_failure"].startswith("spend_cap")

    async def test_a_failed_plan_synthesis_is_preserved_never_retried(self, v2_prereg):
        task = v2_tasks()[0]
        snapshot = v2_snapshot()
        fake = FakeGateway(fail_plan=True)
        document = await capture_arm(
            v2_prereg,
            task,
            MODE_NONE,
            snapshot=snapshot,
            completion=fake,
            completion_identity="dry-run-fake",
            captured_at=V2_CAPTURED_AT,
        )
        run = RecordedRun.from_document(document)
        assert run.outcome == "failed"
        assert fake.calls == 1  # ONE synthesis attempt — never retried
        assert document["attempts"][0]["plan_failure"] == "malformed_or_invalid_plan_json"
        cost = document["attempts"][0]["cost"]
        assert cost["usd_coverage"] == "receipted"
        assert cost["usd_estimated"] > 0  # the failed call's spend is kept
        grade = grade_run(task, run, snapshot)
        assert grade["outcome"] == "failed"
        assert grade["cost"]["coverage"] == "recorded"
        assert "evidence_accuracy" not in grade  # no fabricated scores

    async def test_unresolved_citations_fail_closed(self, v2_prereg):
        task = v2_tasks()[0]
        snapshot = v2_snapshot()
        fake = FakeGateway(
            plan_json=json.dumps(
                {
                    "steps": [
                        {
                            "step_id": "s1",
                            "objective": "apply the policy",
                            "evidence_refs": ["ev-99"],
                        }
                    ],
                    "claims": [],
                    "questions": [],
                    "assumptions": [],
                }
            )
        )
        document = await capture_arm(
            v2_prereg,
            task,
            CANDIDATE_MODE,
            snapshot=snapshot,
            completion=fake,
            completion_identity="dry-run-fake",
            captured_at=V2_CAPTURED_AT,
        )
        assert document["plan"] is None
        assert (
            document["attempts"][0]["plan_failure"] == "invalid_plan_shape_or_unresolved_citations"
        )

    async def test_a_failed_research_loop_with_no_findings_skips_synthesis(self, v2_prereg):
        task = v2_tasks()[0]
        snapshot = v2_snapshot()
        fake = FakeGateway(raise_on_call=True)
        document = await capture_arm(
            v2_prereg,
            task,
            CANDIDATE_MODE,
            snapshot=snapshot,
            completion=fake,
            completion_identity="dry-run-fake",
            captured_at=V2_CAPTURED_AT,
        )
        run = RecordedRun.from_document(document)
        assert run.outcome == "failed"
        assert run.stopped_reason == "gateway_error: RuntimeError"
        assert document["attempts"][0]["cost"]["usd_estimated"] == 0.0

    def test_model_plan_document_rejects_bad_shapes(self):
        good = {
            "steps": [{"objective": "x"}],
            "claims": [{"repo": "r", "path": "p", "line": 3, "asserted_content": "bytes"}],
            "questions": ["q?"],
            "assumptions": [],
        }
        assert model_plan_document(good, set()) is not None
        assert model_plan_document({**good, "claims": [{"repo": "r"}]}, set()) is None
        assert model_plan_document({**good, "steps": []}, set()) is None
        no_line = {**good, "claims": [{"repo": "r", "path": "p", "line": "x"}]}
        assert model_plan_document(no_line, set()) is None

    def test_capped_completion_charges_unknown_usage_at_the_worst_case(self):
        import asyncio

        async def _unknown(system: str, user: str) -> SimpleNamespace:
            return SimpleNamespace(text="{}", input_tokens=None, output_tokens=None)

        ledger = SpendLedger(model="fake", limit_usd=3.0)
        seam = capped_completion(_unknown, ledger, max_tokens=1000, purpose="test")
        asyncio.run(seam("s", "u"))
        receipt = ledger.receipts[0]
        assert receipt["usage_known"] is False
        assert receipt["charged_input_tokens"] == ledger.worst_case_input_tokens
        assert receipt["usd_estimated"] > 0  # never zero

        async def _known(system: str, user: str) -> SimpleNamespace:
            return SimpleNamespace(text="{}", input_tokens=1, output_tokens=1)

        # the hard stop: a worst-case projection that would cross the cap
        # raises BEFORE the provider is contacted
        tight = SpendLedger(model="fake", limit_usd=0.000001)
        with pytest.raises(SpendCapReached):
            asyncio.run(capped_completion(_known, tight, max_tokens=1000, purpose="test")("s", "u"))
        assert tight.receipts == []

    def test_missing_receipts_never_count_as_zero_dollars(self):
        # one run with receipts, one without: the mode's USD total stays
        # UNKNOWN (never a zero silently averaged in)
        with_receipts = {
            "task_id": "a",
            "mode": CANDIDATE_MODE,
            "snapshot_digest": "x",
            "budget": {"max_calls": 10, "wall_seconds": 90.0},
            "attempts": [
                {
                    "attempt": 1,
                    "stopped_reason": "",
                    "cost": {
                        "calls_proposed": 1,
                        "calls_executed": 1,
                        "wall_seconds_used": 1.0,
                        "tokens": {
                            "input": 1,
                            "output": 1,
                            "input_lower_bound": 1,
                            "output_lower_bound": 1,
                            "unknown_usage_calls": 0,
                        },
                        "usd_estimated": 0.01,
                    },
                }
            ],
            "research_document": None,
            "observations": (),
            "plan": {"steps": [{"step_id": "s1", "objective": "o"}]},
            "reviewer": {"correction_minutes": 5},
            "capture": {},
        }
        without = json.loads(json.dumps(with_receipts))
        without["task_id"] = "b"
        del without["attempts"][0]["cost"]["usd_estimated"]
        aggregate = aggregate_mode(
            [], [RecordedRun.from_document(with_receipts), RecordedRun.from_document(without)]
        )
        assert aggregate["cost"]["usd_estimated_total"] is None
        assert aggregate["cost"]["usd_coverage"] == "unknown"


# ---------------------------------------------------------------------------
# R37-11: the SHIPPED gen-2 live cohort (skipped until the artifacts exist —
# they are written ONCE by the live capture driver, never by the tests)
# ---------------------------------------------------------------------------


def _v2_shipped() -> bool:
    return (LIVE_V2_DIR / "report.json").is_file()


class TestShippedLiveCohortV2:
    @pytest.fixture(scope="class")
    def report(self) -> dict:
        if not _v2_shipped():
            pytest.skip("the gen-2 live cohort has not been captured yet")
        return json.loads((LIVE_V2_DIR / "report.json").read_text(encoding="utf-8"))

    def test_every_shipped_run_is_live_model_under_generation_2(self):
        if not _v2_shipped():
            pytest.skip("the gen-2 live cohort has not been captured yet")
        prereg = Preregistration.load(LIVE_V2_DIR / "preregistration.json")
        runner = v2_runner()
        for task in v2_tasks():
            for mode in COHORT_MODES:
                run = runner.runs[task.task_id][mode]
                assert run.provenance == PROVENANCE_LIVE_MODEL, f"{task.task_id}/{mode}"
                assert run.preregistration_digest == prereg.digest
                assert run.preregistration_generation == 2
                assert run.review_pending is True
                capture = run.capture
                assert capture["plan_synthesis"] == PLAN_SYNTHESIS_POLICY
                assert capture["issue"] == V2_ISSUE

    def test_provenance_is_uniform_and_never_pools_with_scripted(self, report):
        summary = report["provenance_summary"]
        for mode in COHORT_MODES:
            assert summary[mode]["live-model"] == 4  # the promotable tasks
            assert summary[mode]["offline-scripted-model"] == 0
            assert summary[mode]["unlabeled"] == 0

    def test_the_verdict_is_hold_pending_human_review(self, report):
        promotion = report["promotion"]
        assert promotion["verdict"] == VERDICT_HOLD
        assert promotion["evidence_class"] == "live-cohort:live-model"
        assert promotion["human_promotion_decision"] == {
            "required": True,
            "recorded": False,
            "decision": None,
            "decided_by": None,
            "recorded_at": None,
        }
        assert report["review"]["pending"]
        for mode in COHORT_MODES:
            aggregate = report["aggregate"][mode]
            assert aggregate["review_pending_tasks"] == aggregate["tasks_graded"]
            assert aggregate["correction_minutes_known_tasks"] == 0
            assert aggregate["correction_minutes_mean"] is None
            assert aggregate["overall_mean"] is None

    def test_costs_come_from_the_receipts_and_respect_the_caps(self, report):
        log = json.loads((LIVE_V2_DIR / "capture-log.json").read_text(encoding="utf-8"))
        assert log["totals"]["usd_estimated_total"] <= float(V2_BUDGET["max_usd_total"])
        assert log["totals"]["cap_exhausted"] is False
        held_back = {task.task_id for task in v2_tasks() if task.held_back}
        for mode in COHORT_MODES:
            aggregate = report["aggregate"][mode]
            assert aggregate["cost"]["usd_coverage"] == "receipted"
            # the aggregate covers the PROMOTABLE tasks only (held-back spend
            # never enters an aggregate); the log carries every arm
            promotable_usd = round(
                sum(
                    float(entry.get("usd_estimated") or 0)
                    for entry in log["arms"]
                    if entry["mode"] == mode and entry["task_id"] not in held_back
                ),
                6,
            )
            assert aggregate["cost"]["usd_estimated_total"] == pytest.approx(
                promotable_usd, abs=1e-6
            )
            assert promotable_usd <= float(V2_BUDGET["max_usd_per_arm"]) * 4
            if mode == CANDIDATE_MODE:
                # failures stay in the denominators WITH their cost
                assert aggregate["tasks_total"] == 4

    def test_the_blind_package_is_scan_clean_and_pending_human(self):
        if not _v2_shipped():
            pytest.skip("the gen-2 live cohort has not been captured yet")
        package = json.loads((LIVE_V2_DIR / "review-package.json").read_text(encoding="utf-8"))
        assert package["schema"] == BLIND_PACKAGE_SCHEMA
        assert package["review_status"] == "pending-human"
        assert package["review_procedure"]["blind"] is True
        assert package["ordering"]["seed"] == V2_ORDERING_SEED
        # nothing in the package can unblind a reviewer
        assert arm_inferrability_scan(package) == []
        assert package == v2_review_package()  # re-derived from the artifacts
        for unit in package["units"]:
            form = unit["form"]
            assert form["correction_minutes"] is None
            assert form["correction_severity"] is None
            assert all(entry["grade"] is None for entry in form["claim_importance"])
        # counterbalanced: each arm appears at every within-block position
        mapping = unblind_package(package, v2_runner().spec)
        positions_by_mode: dict[str, set[int]] = {}
        for index, unit in enumerate(package["units"], start=1):
            _, mode = mapping[unit["review_id"]]
            positions_by_mode.setdefault(mode, set()).add(index % len(COHORT_MODES))
        for mode in COHORT_MODES:
            assert positions_by_mode[mode] == {0, 1, 2}

    def test_the_report_replays_deterministically_from_the_artifacts(self):
        if not _v2_shipped():
            pytest.skip("the gen-2 live cohort has not been captured yet")
        checked_in = json.loads((LIVE_V2_DIR / "report.json").read_text(encoding="utf-8"))
        runner = v2_runner()
        first = runner.run().document
        second = runner.run().document
        assert first == second == checked_in
        assert first["replay"] == {"pure_over_recorded": True, "live_calls": 0}
        assert first["preregistration"]["generation"] == 2
        assert first["preregistration"]["digest"] == build_v2_preregistration().digest

    def test_the_shipped_snapshot_is_the_frozen_discovery_live_bytes(self):
        if not _v2_shipped():
            pytest.skip("the gen-2 live cohort has not been captured yet")
        shipped = Snapshot.load(LIVE_V2_SNAPSHOTS_DIR / LIVE_V2_SNAPSHOT_REF)
        assert shipped.digest == build_v2_snapshot().digest
        # the decisive F-2024-11 window is where the fixtures froze it
        refunds = shipped.files_of("billing-policy")["src/policy/refunds.py"]
        lines = refunds.splitlines()
        marker = next(
            number
            for number, text in enumerate(lines, start=1)
            if "REFUND_MANUAL_APPROVAL_THRESHOLD_CENTS =" in text
        )
        assert marker >= 200  # beyond the first window — the deep archetype is real


def _check_gateway(url: str) -> dict:
    import httpx

    try:
        response = httpx.get(f"{url.rstrip('/')}/v1/models", timeout=5.0)
        return {"reachable": response.status_code == 200, "status_code": response.status_code}
    except Exception as exc:  # noqa: BLE001 — record the refusal reason verbatim
        return {"reachable": False, "status_code": None, "error": f"{type(exc).__name__}: {exc}"}


async def _v2_dry_run() -> int:
    """The scripted dry-run: the FULL live path with an injected fake
    gateway — every arm through the real planner seam and the spend
    ledger, zero vendor spend, written to a THROWAWAY directory."""
    captured, log = await capture_v2_cohort(
        completion=FakeGateway(), completion_identity="dry-run-fake"
    )
    out = Path(tempfile.mkdtemp(prefix="forge-live-cohort-v2-dry-"))
    (out / "snapshots").mkdir()
    (out / "snapshots" / LIVE_V2_SNAPSHOT_REF).write_text(
        json.dumps({"repos": build_v2_snapshot().repos}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out / "preregistration.json").write_text(
        json.dumps(build_v2_preregistration().as_document(), indent=2) + "\n", encoding="utf-8"
    )
    (out / "cohort-live-v2.json").write_text(
        json.dumps(build_v2_spec().as_document(), indent=2) + "\n", encoding="utf-8"
    )
    for task_id, mode_docs in captured.items():
        task_dir = out / "recorded" / task_id
        task_dir.mkdir(parents=True)
        for mode, document in mode_docs.items():
            (task_dir / f"{mode}.json").write_text(json.dumps(document, indent=2) + "\n")
    runner = CohortRunner.from_directory(
        out / "cohort-live-v2.json", out / "snapshots", out / "recorded"
    )
    runner.run().write(out / "report.json")
    print(
        f"dry-run: arms={log['totals']['arms_captured']} "
        f"graded={log['totals']['arms_graded']} failed={log['totals']['arms_failed']} "
        f"usd={log['totals']['usd_estimated_total']}"
    )
    print(f"dry-run verdict: {runner.run().verdict} (artifacts in {out})")
    return 0


async def _v2_live(force: bool) -> int:
    """The LIVE capture: every arm through the lab gateway under the caps.

    One attempt per arm (a live attempt is made ONCE; --force supersedes
    it with a new recorded attempt).  Failures are preserved as evidence
    and the run continues with the remaining arms.
    """
    env = {
        FORGE_RESEARCH_LIVE_GATEWAY_URL_ENV: os.environ.get(
            FORGE_RESEARCH_LIVE_GATEWAY_URL_ENV, "http://localhost:4000"
        ),
        FORGE_RESEARCH_LIVE_GATEWAY_MODEL_ENV: os.environ.get(
            FORGE_RESEARCH_LIVE_GATEWAY_MODEL_ENV, "fast"
        ),
    }
    gateway = resolve_live_gateway(env)
    assert gateway is not None
    reachability = _check_gateway(gateway.base_url)
    if not reachability["reachable"]:
        print(f"LIVE capture REFUSED — gateway unreachable: {reachability}")
        return 2
    if (LIVE_V2_RECORDED_DIR / v2_tasks()[0].task_id).is_dir() and not force:
        print("live cohort already captured — pass --force to supersede (a NEW record)")
        return 2
    write_v2_static_artifacts()

    def _arm_writer(task_id: str, mode: str, document: dict) -> None:
        task_dir = LIVE_V2_RECORDED_DIR / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / f"{mode}.json").write_text(json.dumps(document, indent=2) + "\n")

    captured, log = await capture_v2_cohort(
        env=env, completion_identity=gateway.model, on_arm=_arm_writer
    )
    write_v2_report_and_package(log)
    totals = log["totals"]
    print(
        f"live capture: arms={totals['arms_captured']} "
        f"graded={totals['arms_graded']} failed={totals['arms_failed']} "
        f"spend=${totals['usd_estimated_total']} / cap ${totals['cap_usd_total']}"
    )
    for entry in log["arms"]:
        if entry["outcome"] == "failed":
            print(
                f"  preserved failure: {entry['task_id']}/{entry['mode']} "
                f"({entry['stopped_reason']}) spend=${entry['usd_estimated']}"
            )
    print(f"verdict: {v2_runner().run().verdict} (pending human review)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    command = sys.argv[1] if len(sys.argv) > 1 else "gen1"
    if command == "gen1":
        write_live_cohort()
        print(f"live cohort written to {LIVE_DIR}")
    elif command == "v2-dry":
        raise SystemExit(asyncio.run(_v2_dry_run()))
    elif command == "v2-live":
        raise SystemExit(asyncio.run(_v2_live("--force" in sys.argv)))
    else:  # pragma: no cover
        print("usage: [gen1|v2-dry|v2-live [--force]]")
        raise SystemExit(2)
