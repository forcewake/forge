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
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

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
    arm_inferrability_scan,
    blind_review_package,
    grade_run,
    merge_review_grades,
    unblind_package,
)
from forge.adaptive.research_cohort_live import (
    FORGE_RESEARCH_LIVE_GATEWAY_MODEL_ENV,
    FORGE_RESEARCH_LIVE_GATEWAY_URL_ENV,
    REACTIVE_DEEP_READ,
    ScriptedInvestigation,
    capture_arm,
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


if __name__ == "__main__":  # pragma: no cover
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    write_live_cohort()
    print(f"live cohort written to {LIVE_DIR}")
