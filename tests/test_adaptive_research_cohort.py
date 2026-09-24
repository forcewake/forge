"""R32-14 — the task-specific research-quality cohort (topics 3 §4 + 4 §4).

These tests pin the cohort's contract:

- the SPEC is versioned and validated: all four archetypes present (and
  present among promotable tasks), at least one held-back task, unique
  ids, archetype-specific ground truth, and snapshot digests that ARE
  ``repo_set_digest`` over the recorded bytes;
- the RUNNER is a pure replay over recorded artifacts (offline, no LLM,
  no network) with re-validated bindings: tampered snapshots, missing
  runs, missing cost coverage, budget mismatches and unresolvable plan
  citations are integrity issues that force HOLD;
- the GRADING is semantic, not syntactic: a valid file:line whose bytes
  do not support the claim earns nothing, minor claims stay out of the
  accuracy denominator, spurious irrelevant-repo surface penalizes
  recall, an invented default scores worse than a specific question,
  and the recorded correction estimate is published beside — never
  blended into — the overall;
- HONEST semantics are structural: exhaustion keeps ``complete: false``
  and its ``stopped_reason`` into the report, failed attempts appear
  WITH cost and without grades, unknown usage stays a lower bound;
- the VERDICT is three-valued (PASS / HOLD-with-expiry / ROLLBACK),
  held-back tasks are reported separately and can never move it, and
  the mutation + injection hooks behave as adversarial probes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from forge.adaptive.discovery_stage import repo_set_digest
from forge.adaptive.research_cohort import (
    ARCHETYPE_AMBIGUOUS_REQUIREMENT,
    ARCHETYPE_IRRELEVANT_REPOSITORY,
    ARCHETYPE_NEIGHBOR_DEPENDENCY,
    ARCHETYPES,
    ASSUMPTION_WEIGHT_SATURATION,
    BASELINE_MODE,
    CANDIDATE_MODE,
    COHORT_SCHEMA,
    COHORT_MODES,
    EVIDENCE_CLASS_FIXTURE_REPLAY,
    MIN_COHORT_TASKS,
    PROVENANCE_LIVE_MODEL,
    PROVENANCE_OFFLINE_SCRIPTED,
    RECORD_SCHEMA,
    REPORT_SCHEMA,
    REVIEW_PENDING,
    VERDICT_HOLD,
    VERDICT_PASS,
    VERDICT_ROLLBACK,
    CohortRunner,
    CohortSpec,
    CohortSpecError,
    CohortTask,
    Preregistration,
    PreregTaskBinding,
    PromotionPolicy,
    RecordedRun,
    Rubric,
    Snapshot,
    SurfaceRef,
    apply_contract_mutation,
    check_claim_support,
    grade_run,
    inject_valid_but_irrelevant_citation,
    plan_digest,
    promotion_verdict,
)
from forge.adaptive.research_planner import ToolObservation

REPO_ROOT = Path(__file__).resolve().parents[1]
COHORT_DIR = REPO_ROOT / "evaluation" / "research_cohort"
SNAPSHOTS_DIR = COHORT_DIR / "snapshots"
RECORDED_DIR = COHORT_DIR / "recorded"

BUDGET = {"max_calls": 10, "wall_seconds": 90.0}

_EVENTS_FILE = (
    'ORDER_EVENT = "order.expired.v2"\n'
    'PAYLOAD_FIELDS = ("order_id",)\n'
    "\n"
    "\n"
    "def validate(payload):\n"
    "    return True\n"
    "\n"
    "# design review checklist (content that supports nothing)\n"
    "# do not touch the header art\n"
)


@pytest.fixture(scope="module")
def shipped_runner() -> CohortRunner:
    return CohortRunner.from_directory(COHORT_DIR / "cohort-v1.json", SNAPSHOTS_DIR, RECORDED_DIR)


@pytest.fixture(scope="module")
def shipped_report(shipped_runner):
    return shipped_runner.run().document


def make_snapshot(files: dict[str, str], *, repo: str = "orders") -> Snapshot:
    return Snapshot.from_document(
        {
            "repos": {
                repo: {
                    "repository_id": "gitlab-1",
                    "source_oid": "a" * 40,
                    "files": files,
                }
            }
        }
    )


def make_task(
    *,
    snapshot: Snapshot,
    archetype: str = ARCHETYPE_NEIGHBOR_DEPENDENCY,
    rubric: Rubric | None = None,
    expected: tuple[tuple[str, str], ...] = (("orders", "src/events.py"),),
    missing_decision: str = "",
    terms: tuple[str, ...] = (),
    irrelevant: tuple[str, ...] = (),
) -> CohortTask:
    return CohortTask(
        task_id="T-1",
        archetype=archetype,
        statement="do the thing",
        snapshot_digest=snapshot.digest,
        snapshot_ref="snap.json",
        rubric=rubric or Rubric(),
        held_back=False,
        expected_surface=tuple(SurfaceRef(repo, path) for repo, path in expected),
        missing_decision=missing_decision,
        missing_decision_terms=terms,
        irrelevant_repos=irrelevant,
    )


def make_run(
    *,
    snapshot: Snapshot,
    plan: dict | None,
    mode: str = "research",
    research: dict | None = None,
    attempts: list[dict] | None = None,
    reviewer: dict | None = None,
    task_id: str = "T-1",
    budget: dict | None = None,
) -> RecordedRun:
    return RecordedRun(
        task_id=task_id,
        mode=mode,
        snapshot_digest=snapshot.digest,
        budget=dict(budget or BUDGET),
        attempts=tuple(
            attempts
            if attempts is not None
            else [
                {
                    "attempt": 1,
                    "stopped_reason": "",
                    "cost": {
                        "calls_proposed": 0,
                        "calls_executed": 0,
                        "wall_seconds_used": 0.0,
                        "tokens": {
                            "input": 0,
                            "output": 0,
                            "input_lower_bound": 0,
                            "output_lower_bound": 0,
                            "unknown_usage_calls": 0,
                        },
                    },
                }
            ]
        ),
        research_document=research,
        observations=(),
        plan=plan,
        reviewer=dict(reviewer or {}),
    )


def a_claim(
    line: int,
    asserted: str,
    *,
    repo: str = "orders",
    path: str = "src/events.py",
    importance: str = "important",
) -> dict:
    return {
        "claim_id": "c1",
        "text": asserted,
        "repo": repo,
        "path": path,
        "line": line,
        "asserted_content": asserted,
        "importance": importance,
    }


# ---------------------------------------------------------------------------
# Spec versioning + validation
# ---------------------------------------------------------------------------


def test_shipped_spec_loads_and_validates():
    spec = CohortSpec.load(COHORT_DIR / "cohort-v1.json")
    spec.validate()
    assert spec.schema == COHORT_SCHEMA
    assert len(spec.tasks) >= MIN_COHORT_TASKS
    assert len(spec.held_back_tasks) >= 1
    assert {task.archetype for task in spec.tasks} == set(ARCHETYPES)
    # all four archetypes also present among the PROMOTABLE tasks
    assert {task.archetype for task in spec.promotable_tasks} == set(ARCHETYPES)
    assert len(spec.promotable_tasks) >= 4


def test_shipped_snapshot_digest_is_repo_set_digest():
    """The binding is the SAME authorization identity the stage uses."""
    for path in sorted(SNAPSHOTS_DIR.glob("*.json")):
        snapshot = Snapshot.load(path)
        files_by_repo = {key: dict(entry["files"]) for key, entry in snapshot.repos.items()}
        assert snapshot.digest == repo_set_digest(files_by_repo)


def test_shipped_task_digests_bind_to_recorded_snapshots():
    spec = CohortSpec.load(COHORT_DIR / "cohort-v1.json")
    digests = {Snapshot.load(path).digest for path in SNAPSHOTS_DIR.glob("*.json")}
    assert {task.snapshot_digest for task in spec.tasks} <= digests


def _shipped_spec_doc() -> dict:
    return json.loads((COHORT_DIR / "cohort-v1.json").read_text(encoding="utf-8"))


def test_spec_rejects_wrong_schema():
    doc = _shipped_spec_doc()
    doc["schema"] = "forge.research.cohort/0"
    with pytest.raises(CohortSpecError, match="schema"):
        CohortSpec.from_document(doc).validate()


def test_spec_rejects_missing_archetype():
    doc = _shipped_spec_doc()
    doc["tasks"] = [t for t in doc["tasks"] if t["archetype"] != "irrelevant_repository"]
    with pytest.raises(CohortSpecError, match="archetypes without a task"):
        CohortSpec.from_document(doc).validate()


def test_spec_rejects_archetype_present_only_held_back():
    doc = _shipped_spec_doc()
    for task in doc["tasks"]:
        if task["archetype"] == "irrelevant_repository":
            task["held_back"] = True
    with pytest.raises(CohortSpecError, match="never tested"):
        CohortSpec.from_document(doc).validate()


def test_spec_rejects_no_held_back_task():
    doc = _shipped_spec_doc()
    for task in doc["tasks"]:
        task["held_back"] = False
    with pytest.raises(CohortSpecError, match="hold back"):
        CohortSpec.from_document(doc).validate()


def test_spec_rejects_duplicate_task_ids():
    doc = _shipped_spec_doc()
    doc["tasks"].append(json.loads(json.dumps(doc["tasks"][0])))
    with pytest.raises(CohortSpecError, match="duplicate task id"):
        CohortSpec.from_document(doc).validate()


def test_ambiguous_task_requires_its_missing_decision():
    doc = _shipped_spec_doc()
    for task in doc["tasks"]:
        if task["task_id"] == "RC-03-ambiguous-refund-window":
            task.pop("missing_decision", None)
            task.pop("missing_decision_terms", None)
    with pytest.raises(CohortSpecError, match="missing decision"):
        CohortSpec.from_document(doc).validate()


def test_rubric_weights_must_sum_to_one():
    with pytest.raises(CohortSpecError, match="sum to 1.0"):
        Rubric(accuracy_weight=0.5, recall_weight=0.5, assumptions_weight=0.2).validate()


def test_promotion_policy_requires_named_owner_and_expiry():
    policy = PromotionPolicy(
        owner="",
        hold_expires="2026-10-07",
        margin=0.05,
        accuracy_floor=0.8,
        budget=BUDGET,
    )
    with pytest.raises(CohortSpecError, match="owner"):
        policy.validate()
    policy = PromotionPolicy(
        owner="lead", hold_expires="soon", margin=0.05, accuracy_floor=0.8, budget=BUDGET
    )
    with pytest.raises(CohortSpecError, match="ISO date"):
        policy.validate()


# ---------------------------------------------------------------------------
# Grading — semantic vs syntactic, and the merged dimensions
# ---------------------------------------------------------------------------


def test_claim_support_semantic_at_the_real_line():
    snapshot = make_snapshot({"src/events.py": _EVENTS_FILE})
    claim = a_claim(1, 'ORDER_EVENT = "order.expired.v2"')
    assert check_claim_support(claim, snapshot) == "semantic_match"


def test_claim_support_syntactic_only_at_a_valid_wrong_line():
    """A resolving file:line whose bytes do not carry the claim earns NOTHING."""
    snapshot = make_snapshot({"src/events.py": _EVENTS_FILE})
    claim = a_claim(8, 'ORDER_EVENT = "order.expired.v2"')  # line 8 is the art comment
    assert snapshot.resolves("orders", "src/events.py", 8)  # syntactically valid...
    assert check_claim_support(claim, snapshot) == "syntactic_only"  # ...semantically dead


def test_claim_support_invalid_and_unverified():
    snapshot = make_snapshot({"src/events.py": _EVENTS_FILE})
    assert check_claim_support(a_claim(1, "whatever", path="src/absent.py"), snapshot) == "invalid"
    assert check_claim_support(a_claim(999, "whatever"), snapshot) == "invalid"
    assert check_claim_support(a_claim(1, "whatever"), Snapshot()) == "unverified"


def test_accuracy_denominator_is_important_claims_only():
    snapshot = make_snapshot({"src/events.py": _EVENTS_FILE})
    task = make_task(snapshot=snapshot)
    plan = {
        "steps": [],
        "surface": [],
        "claims": [
            a_claim(1, 'ORDER_EVENT = "order.expired.v2"', importance="important"),
            a_claim(8, 'ORDER_EVENT = "order.expired.v2"', importance="minor"),
            a_claim(9, 'ORDER_EVENT = "order.expired.v2"', importance="minor"),
        ],
        "questions": [],
        "assumptions": [],
    }
    grade = grade_run(task, make_run(snapshot=snapshot, plan=plan), snapshot)
    assert grade["evidence_accuracy"]["important_claims"] == 1
    assert grade["evidence_accuracy"]["score"] == 1.0  # the minors never entered


def test_surface_recall_found_missed_and_spurious_penalty():
    snapshot = make_snapshot({"src/events.py": _EVENTS_FILE, "src/other.py": "x = 1\n"})
    task = make_task(
        snapshot=snapshot,
        archetype=ARCHETYPE_IRRELEVANT_REPOSITORY,
        expected=(("orders", "src/events.py"), ("orders", "src/other.py")),
        irrelevant=("docs-site",),
    )
    plan = {
        "steps": [],
        "surface": [
            {"repo": "orders", "path": "src/events.py", "why": ""},
            {"repo": "docs-site", "path": "index.md", "why": "trap"},
        ],
        "claims": [a_claim(1, 'ORDER_EVENT = "order.expired.v2"')],
        "questions": [],
        "assumptions": [],
    }
    grade = grade_run(task, make_run(snapshot=snapshot, plan=plan), snapshot)
    recall = grade["impacted_surface_recall"]
    assert recall["recall"] == 0.5
    assert recall["missed"] == [{"repo": "orders", "path": "src/other.py"}]
    assert recall["spurious"] == [{"repo": "docs-site", "path": "index.md"}]
    assert recall["score"] == 0.25  # 0.5 recall minus one spurious penalty


def test_ambiguous_task_specific_question_beats_invented_default():
    snapshot = make_snapshot({"src/events.py": _EVENTS_FILE})
    task = make_task(
        snapshot=snapshot,
        archetype=ARCHETYPE_AMBIGUOUS_REQUIREMENT,
        missing_decision="the grace-window length (7 or 30 days)",
        terms=("grace window", "7", "30", "days"),
    )
    inventor = make_run(
        snapshot=snapshot,
        plan={
            "steps": [],
            "surface": [{"repo": "orders", "path": "src/events.py", "why": ""}],
            "claims": [a_claim(1, 'ORDER_EVENT = "order.expired.v2"')],
            "questions": [],
            "assumptions": [
                {"text": "grace window is 7 days", "severity": "high", "invented_default": True}
            ],
        },
    )
    asker = make_run(
        snapshot=snapshot,
        plan={
            "steps": [],
            "surface": [{"repo": "orders", "path": "src/events.py", "why": ""}],
            "claims": [a_claim(1, 'ORDER_EVENT = "order.expired.v2"')],
            "questions": [
                {
                    "question_id": "q1",
                    "text": "Is the grace window 7 or 30 days after expiry?",
                    "specific": True,
                }
            ],
            "assumptions": [],
        },
    )
    inventor_grade = grade_run(task, inventor, snapshot)
    asker_grade = grade_run(task, asker, snapshot)
    assert inventor_grade["question_quality"]["missing_decision_addressed"] is False
    assert inventor_grade["question_quality"]["score"] == 0.0
    assert asker_grade["question_quality"]["missing_decision_addressed"] is True
    assert asker_grade["question_quality"]["score"] == 1.0
    assert (
        inventor_grade["unjustified_assumptions"]["weight"]
        == 3.0 + 2.0  # high severity + invented-default penalty
    )
    assert asker_grade["overall"] > inventor_grade["overall"]


def test_generic_question_scores_zero_on_ambiguous_task():
    snapshot = make_snapshot({"src/events.py": _EVENTS_FILE})
    task = make_task(
        snapshot=snapshot,
        archetype=ARCHETYPE_AMBIGUOUS_REQUIREMENT,
        missing_decision="the grace-window length",
        terms=("grace window",),
    )
    grade = grade_run(
        task,
        make_run(
            snapshot=snapshot,
            plan={
                "steps": [],
                "surface": [],
                "claims": [],
                "questions": [
                    {"question_id": "q1", "text": "what are the rules?", "specific": False}
                ],
                "assumptions": [],
            },
        ),
        snapshot,
    )
    assert grade["question_quality"]["score"] == 0.0


def test_correction_effort_is_recorded_beside_not_blended():
    snapshot = make_snapshot({"src/events.py": _EVENTS_FILE})
    task = make_task(snapshot=snapshot)
    plan = {
        "steps": [],
        "surface": [{"repo": "orders", "path": "src/events.py", "why": ""}],
        "claims": [a_claim(1, 'ORDER_EVENT = "order.expired.v2"')],
        "questions": [],
        "assumptions": [],
    }
    quick = grade_run(
        task, make_run(snapshot=snapshot, plan=plan, reviewer={"correction_minutes": 2}), snapshot
    )
    slow = grade_run(
        task, make_run(snapshot=snapshot, plan=plan, reviewer={"correction_minutes": 90}), snapshot
    )
    assert quick["human_plan_correction_effort"]["minutes"] == 2
    assert slow["human_plan_correction_effort"]["minutes"] == 90
    assert quick["overall"] == slow["overall"]  # the counterweight never enters the score


def test_overall_is_the_rubric_weighted_sum():
    snapshot = make_snapshot({"src/events.py": _EVENTS_FILE})
    rubric = Rubric(
        accuracy_weight=0.4, recall_weight=0.3, assumptions_weight=0.2, questions_weight=0.1
    )
    task = make_task(snapshot=snapshot, rubric=rubric, expected=(("orders", "src/events.py"),))
    plan = {
        "steps": [],
        "surface": [{"repo": "orders", "path": "src/events.py", "why": ""}],
        "claims": [a_claim(1, 'ORDER_EVENT = "order.expired.v2"')],
        "questions": [],
        "assumptions": [{"text": "x", "severity": "low", "invented_default": False}],
    }
    grade = grade_run(task, make_run(snapshot=snapshot, plan=plan), snapshot)
    expected = (
        0.4 * 1.0  # accuracy
        + 0.3 * 1.0  # recall
        + 0.2 * (1.0 - 1.0 / ASSUMPTION_WEIGHT_SATURATION)  # one low assumption
        + 0.1 * 1.0  # no questions on a decided task
    )
    assert grade["overall"] == pytest.approx(expected, abs=1e-4)


# ---------------------------------------------------------------------------
# Honest semantics — exhaustion, failures, unknown usage
# ---------------------------------------------------------------------------


def test_exhausted_research_never_relabelled_complete(shipped_report):
    grade = shipped_report["tasks"]["RC-05-irrelevant-repo-lifecycle"]["modes"]["research"]
    research = grade["research"]
    assert research["present"] is True
    assert research["complete"] is False  # the plan existing changes nothing
    assert research["stopped_reason"] == "max_calls"
    assert research["partial_retained_as_partial"] is True
    assert grade["outcome"] == "graded"  # ...but it IS still graded honestly


def test_failed_attempt_reported_with_cost_not_graded(shipped_report):
    grade = shipped_report["held_back"]["tasks"]["RC-07-holdout-deep-retry-handler"]["modes"][
        "research"
    ]
    assert grade["outcome"] == "failed"
    assert "evidence_accuracy" not in grade  # no plan, no fabricated dimensions
    assert grade["cost"]["coverage"] == "recorded"  # ...but the spend stays
    assert grade["reason"] == "gateway_error: RateLimitError"


def test_earlier_failed_attempt_keeps_its_cost(shipped_report):
    grade = shipped_report["tasks"]["RC-04-deep-file-evidence"]["modes"]["research"]
    assert grade["attempts"] == [
        {"attempt": 1, "stopped_reason": "gateway_error: APIConnectionError"},
        {"attempt": 2, "stopped_reason": ""},
    ]
    assert grade["attempts_stopped"] == 1
    assert grade["cost"]["attempts"] == 2  # both attempts' spend is in


def test_unknown_usage_stays_a_lower_bound(shipped_report):
    tokens = shipped_report["aggregate"]["research"]["cost"]["tokens"]
    assert tokens["input"] is None  # RC-04 attempt 1 had unknown usage
    assert tokens["output"] is None
    assert tokens["unknown_usage_calls"] >= 1
    assert tokens["input_lower_bound"] > 0


def test_recorded_research_document_schema_is_enforced():
    doc = {
        "schema": RECORD_SCHEMA,
        "task_id": "T-1",
        "mode": "research",
        "research_document": {"schema": "forge.discovery.research/0", "complete": True},
        "plan": {"steps": []},
    }
    with pytest.raises(CohortSpecError, match="research document schema"):
        RecordedRun.from_document(doc)


def test_observations_rebuild_as_real_toolobservations(shipped_runner):
    run = shipped_runner.runs["RC-01-neighbor-expiry-event"]["research"]
    assert run.observations
    assert all(isinstance(obs, ToolObservation) for obs in run.observations)
    assert any(obs.truncated for obs in run.observations) is False
    exhausted = shipped_runner.runs["RC-05-irrelevant-repo-lifecycle"]["research"]
    assert any(obs.truncated for obs in exhausted.observations)


# ---------------------------------------------------------------------------
# Runner replay over the recorded artifacts
# ---------------------------------------------------------------------------


def test_replay_shipped_artifacts_offline(shipped_report):
    assert shipped_report["schema"] == REPORT_SCHEMA
    assert shipped_report["replay"] == {"pure_over_recorded": True, "live_calls": 0}
    assert shipped_report["cohort_id"] == "research-cohort-v1"
    assert shipped_report["mode_spellings"] == {
        "none": "none",
        "lexical": "lexical",
        "research": "research-harness",
    }
    # every promotable task graded under every mode
    for section in shipped_report["tasks"].values():
        assert set(section["modes"]) == set(COHORT_MODES)
        for grade in section["modes"].values():
            assert grade["cost"]["coverage"] == "recorded"


def test_runner_is_a_pure_function_of_the_recording(shipped_runner):
    first = shipped_runner.run().document
    second = shipped_runner.run().document
    assert first == second


def test_shipped_verdict_is_pass_with_the_agreed_margin(shipped_report):
    promotion = shipped_report["promotion"]
    assert promotion["verdict"] == VERDICT_PASS
    assert promotion["candidate_mode"] == CANDIDATE_MODE
    assert promotion["baseline_mode"] == BASELINE_MODE
    delta = (
        promotion["compared"][CANDIDATE_MODE]["overall_mean"]
        - promotion["compared"][BASELINE_MODE]["overall_mean"]
    )
    assert delta >= shipped_report["promotion_policy"]["margin"]
    assert (
        shipped_report["aggregate"][CANDIDATE_MODE]["evidence_accuracy_mean"]
        >= (shipped_report["promotion_policy"]["accuracy_floor"])
    )


def test_held_back_reported_separately_and_never_in_aggregates(shipped_report):
    held = shipped_report["held_back"]["tasks"]
    assert set(held) == {
        "RC-02-neighbor-retry-policy",
        "RC-06-holdout-ambiguous-retention",
        "RC-07-holdout-deep-retry-handler",
    }
    assert not (set(held) & set(shipped_report["tasks"]))
    for mode in COHORT_MODES:
        # aggregates count ONLY the four promotable tasks
        assert shipped_report["aggregate"][mode]["tasks_total"] == 4
    # the held-back research FAILURE (RC-07) did not block the PASS verdict
    assert shipped_report["promotion"]["verdict"] == VERDICT_PASS


def test_cost_coverage_present_for_all_modes(shipped_report):
    for mode in COHORT_MODES:
        cost = shipped_report["aggregate"][mode]["cost"]
        assert cost["coverage"] == "complete"
        assert cost["tasks_with_cost"] == shipped_report["aggregate"][mode]["tasks_total"]
        assert cost["calls_proposed_total"] >= 0


def test_tampered_snapshot_forces_hold(shipped_runner):
    spec = shipped_runner.spec
    task = spec.promotable_tasks[0]
    tampered = make_snapshot({"src/rewritten.py": "gone = True\n"})
    snapshots = dict(shipped_runner.snapshots)
    snapshots[task.snapshot_digest] = tampered  # same key, different bytes
    runner = CohortRunner(spec, snapshots, shipped_runner.runs, shipped_runner.mutations)
    report = runner.run().document
    assert report["integrity"]["binding_violations"]
    assert report["promotion"]["verdict"] == VERDICT_HOLD


def test_missing_run_forces_hold(shipped_runner):
    runs = {task_id: dict(mode_runs) for task_id, mode_runs in shipped_runner.runs.items()}
    del runs["RC-04-deep-file-evidence"][CANDIDATE_MODE]
    runner = CohortRunner(shipped_runner.spec, shipped_runner.snapshots, runs, {})
    report = runner.run().document
    assert report["integrity"]["missing_runs"] == ["RC-04-deep-file-evidence/research"]
    assert report["promotion"]["verdict"] == VERDICT_HOLD


def test_budget_mismatch_breaks_comparability_and_forces_hold(shipped_runner):
    spec = shipped_runner.spec
    task = spec.promotable_tasks[0]
    original = shipped_runner.runs[task.task_id][CANDIDATE_MODE]
    richer = RecordedRun(**{**original.__dict__, "budget": {"max_calls": 50, "wall_seconds": 90.0}})
    runs = {
        task_id: (
            {**dict(mode_runs), CANDIDATE_MODE: richer}
            if task_id == task.task_id
            else dict(mode_runs)
        )
        for task_id, mode_runs in shipped_runner.runs.items()
    }
    runner = CohortRunner(spec, shipped_runner.snapshots, runs, shipped_runner.mutations)
    report = runner.run().document
    assert report["integrity"]["budget_mismatches"]
    assert report["promotion"]["verdict"] == VERDICT_HOLD


def test_unresolvable_plan_citation_is_an_integrity_issue(shipped_runner):
    """A research plan citing evidence ids its own document never minted."""
    snapshot = make_snapshot({"src/events.py": _EVENTS_FILE})
    research = {
        "schema": "forge.discovery.research/1",
        "complete": True,
        "stopped_reason": "",
        "findings": [
            {"evidence_id": "r1", "repo_key": "orders", "path": "src/events.py", "line": 1}
        ],
    }
    plan = {
        "steps": [
            {
                "step_id": "s1",
                "objective": "act",
                "evidence_refs": ["evidence:r1", "evidence:ghost"],
            }
        ],
        "surface": [],
        "claims": [],
        "questions": [],
        "assumptions": [],
    }
    run = make_run(snapshot=snapshot, plan=plan, research=research)
    task = make_task(snapshot=snapshot)
    runner = CohortRunner(
        _spec_with_task(task),
        {snapshot.digest: snapshot},
        {task.task_id: {mode: run for mode in COHORT_MODES}},
        {},
    )
    report = runner.run().document
    assert report["integrity"]["citation_violations"]
    assert report["promotion"]["verdict"] == VERDICT_HOLD


def _spec_with_task(task: CohortTask) -> CohortSpec:
    """A minimal VALID spec wrapping one promotable task plus filler tasks
    covering the other three archetypes (validation requires all four)."""

    def filler(task_id: str, archetype: str) -> CohortTask:
        return CohortTask(
            task_id=task_id,
            archetype=archetype,
            statement="filler",
            snapshot_digest=task.snapshot_digest,
            snapshot_ref="snap.json",
            rubric=Rubric(),
            expected_surface=(SurfaceRef("orders", "src/events.py"),),
            missing_decision="x or y" if archetype == ARCHETYPE_AMBIGUOUS_REQUIREMENT else "",
            irrelevant_repos=("docs-site",) if archetype == ARCHETYPE_IRRELEVANT_REPOSITORY else (),
        )

    others = [a for a in ARCHETYPES if a != task.archetype]
    tasks = (
        task,
        *(filler(f"F-{index}", archetype) for index, archetype in enumerate(others, start=1)),
        CohortTask(
            task_id="F-held",
            archetype=task.archetype,
            statement="held reserve",
            snapshot_digest=task.snapshot_digest,
            snapshot_ref="snap.json",
            rubric=Rubric(),
            held_back=True,
            expected_surface=(SurfaceRef("orders", "src/events.py"),),
        ),
    )
    return CohortSpec(
        cohort_id="test-cohort",
        recorded_at="2026-09-23T00:00:00+00:00",
        promotion=PromotionPolicy(
            owner="test owner",
            hold_expires="2026-10-07",
            margin=0.05,
            accuracy_floor=0.8,
            budget=BUDGET,
        ),
        tasks=tasks,
    )


def test_mutation_arm_tracks_plan_change_and_substantive_reason(shipped_report):
    mutation = shipped_report["tasks"]["RC-01-neighbor-expiry-event"]["mutation"]
    modes = mutation["modes"]
    assert modes[BASELINE_MODE]["plan_changed"] is False  # lexical never read the contract
    assert modes[CANDIDATE_MODE]["plan_changed"] is True  # research reacted to the move
    assert modes[CANDIDATE_MODE]["change_reason_substantive"] is True  # reviewer-recorded
    assert modes[CANDIDATE_MODE]["digest_matches_reapplied_mutation"] is True


# ---------------------------------------------------------------------------
# The three-value verdict logic (synthetic aggregates)
# ---------------------------------------------------------------------------


def _policy(margin: float = 0.05) -> PromotionPolicy:
    return PromotionPolicy(
        owner="review lead",
        hold_expires="2026-10-07",
        margin=margin,
        accuracy_floor=0.8,
        budget=BUDGET,
    )


def _agg(overall: float, accuracy: float = 1.0, invented: int = 0) -> dict:
    return {
        "overall_mean": overall,
        "evidence_accuracy_mean": accuracy,
        "invented_defaults_total": invented,
    }


def _aggregate(baseline: dict, candidate: dict) -> dict:
    return {
        "none": {"overall_mean": 0.0},
        BASELINE_MODE: baseline,
        CANDIDATE_MODE: candidate,
    }


def test_verdict_pass_on_margin_improvement():
    verdict = promotion_verdict(_policy(), _aggregate(_agg(0.60), _agg(0.90)), {})
    assert verdict["verdict"] == VERDICT_PASS


def test_verdict_rollback_on_overall_regression():
    verdict = promotion_verdict(_policy(), _aggregate(_agg(0.80), _agg(0.70)), {})
    assert verdict["verdict"] == VERDICT_ROLLBACK
    assert any("did not improve" in reason for reason in verdict["reasons"])


def test_verdict_rollback_under_accuracy_floor():
    verdict = promotion_verdict(_policy(), _aggregate(_agg(0.60), _agg(0.90, accuracy=0.5)), {})
    assert verdict["verdict"] == VERDICT_ROLLBACK
    assert any("accuracy" in reason for reason in verdict["reasons"])


def test_verdict_rollback_on_invented_defaults_regression():
    verdict = promotion_verdict(
        _policy(), _aggregate(_agg(0.60, invented=0), _agg(0.90, invented=2)), {}
    )
    assert verdict["verdict"] == VERDICT_ROLLBACK


def test_verdict_hold_when_improvement_below_margin():
    verdict = promotion_verdict(_policy(margin=0.05), _aggregate(_agg(0.60), _agg(0.63)), {})
    assert verdict["verdict"] == VERDICT_HOLD
    assert "hold_owner" in verdict and verdict["hold_owner"] == "review lead"
    assert verdict["hold_expires"] == "2026-10-07"


def test_verdict_hold_routes_evidence_gaps_to_the_named_owner():
    issues = {"missing_runs": ["T-1/research"]}
    verdict = promotion_verdict(_policy(), _aggregate(_agg(0.6), _agg(0.9)), issues)
    assert verdict["verdict"] == VERDICT_HOLD
    assert verdict["hold_owner"] == "review lead"
    assert verdict["hold_expires"] == "2026-10-07"
    assert any("evidence incomplete" in reason for reason in verdict["reasons"])


# ---------------------------------------------------------------------------
# The mutation + injection hooks
# ---------------------------------------------------------------------------


def test_contract_mutation_moves_the_neighbor_contract():
    snapshot = Snapshot.from_document(
        {
            "repos": {
                "billing": {
                    "repository_id": "g-2",
                    "source_oid": "b" * 40,
                    "files": {"src/events.py": 'ORDER_EXPIRED_EVENT = "order.expired.v2"\n'},
                },
                "orders": {
                    "repository_id": "g-1",
                    "source_oid": "a" * 40,
                    "files": {"src/timer.py": 'EXPIRY_EVENT = "order.expired.v2"\n'},
                },
            }
        }
    )
    mutated = apply_contract_mutation(snapshot, "RC-01-neighbor-expiry-event")
    assert "order.expired.v3" in mutated.files_of("billing")["src/events.py"]
    assert "order.expired.v2" in snapshot.files_of("billing")["src/events.py"]  # untouched
    assert mutated.digest != snapshot.digest
    # the ORDERS side is untouched: the contract lives in the neighbor
    assert mutated.files_of("orders") == snapshot.files_of("orders")


def test_contract_mutation_generic_fallback_and_refusal():
    versioned = make_snapshot({"src/contract.py": 'EVENT = "order.placed.v1"\n'})
    mutated = apply_contract_mutation(versioned, "any-other-task")
    assert 'EVENT = "order.placed.v2"' in mutated.files_of("orders")["src/contract.py"]
    unversioned = make_snapshot({"src/plain.py": "value = 1\n"})
    with pytest.raises(ValueError, match="no cross-service contract token"):
        apply_contract_mutation(unversioned, "any-other-task")


def test_injected_citation_is_valid_but_semantically_worthless():
    snapshot = make_snapshot({"src/events.py": _EVENTS_FILE})
    doc = {
        "schema": "forge.discovery.research/1",
        "summary": 'the shared contract ORDER_EVENT = "order.expired.v2" sits in events',
        "findings": [
            {
                "evidence_id": "r1",
                "repo_key": "orders",
                "repository_id": "gitlab-1",
                "source_oid": "a" * 40,
                "path": "src/events.py",
                "line": 1,
                "kind": "research_read",
                "detail": "read window",
                "text": _EVENTS_FILE,
                "content": _EVENTS_FILE,
            }
        ],
    }
    injected = inject_valid_but_irrelevant_citation(doc)
    decoy = injected["findings"][-1]
    assert decoy["kind"] == "injected_decoy"
    # syntactically VALID: the anchor resolves inside the snapshot...
    assert snapshot.resolves("orders", decoy["path"], decoy["line"])
    # ...but a claim leaning on it is syntactic-only support:
    leaning = a_claim(decoy["line"], 'ORDER_EVENT = "order.expired.v2"')
    assert check_claim_support(leaning, snapshot) == "syntactic_only"
    # and the accuracy dimension therefore cannot be inflated by it:
    task = make_task(snapshot=snapshot)
    honest = grade_run(
        task,
        make_run(
            snapshot=snapshot,
            plan={
                "steps": [],
                "surface": [{"repo": "orders", "path": "src/events.py", "why": ""}],
                "claims": [a_claim(1, 'ORDER_EVENT = "order.expired.v2"')],
                "questions": [],
                "assumptions": [],
            },
        ),
        snapshot,
    )
    gaming = grade_run(
        task,
        make_run(
            snapshot=snapshot,
            plan={
                "steps": [],
                "surface": [{"repo": "orders", "path": "src/events.py", "why": ""}],
                "claims": [leaning],
                "questions": [],
                "assumptions": [],
            },
        ),
        snapshot,
    )
    assert honest["evidence_accuracy"]["score"] == 1.0
    assert gaming["evidence_accuracy"]["score"] == 0.0


def test_injection_never_mutates_the_recorded_document():
    doc = {
        "schema": "forge.discovery.research/1",
        "summary": "summary tokens",
        "findings": [
            {
                "evidence_id": "r1",
                "repo_key": "orders",
                "repository_id": "g",
                "source_oid": "a" * 40,
                "path": "src/x.py",
                "line": 1,
                "kind": "research_read",
                "content": "one\ntwo\nthree\n",
            }
        ],
    }
    before = json.loads(json.dumps(doc))
    inject_valid_but_irrelevant_citation(doc)
    assert doc == before
    with pytest.raises(ValueError, match="no findings"):
        inject_valid_but_irrelevant_citation(
            {"schema": "forge.discovery.research/1", "findings": []}
        )


def test_plan_digest_tracks_content_changes():
    a = {"steps": [{"step_id": "s1", "objective": "same"}]}
    b = {"steps": [{"step_id": "s1", "objective": "same"}]}
    c = {"steps": [{"step_id": "s1", "objective": "changed"}]}
    assert plan_digest(a) == plan_digest(b)
    assert plan_digest(a) != plan_digest(c)


def test_evaluation_artifacts_are_replayable_from_a_clean_path():
    """The offline replay needs nothing but the checked-in files."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    runner = CohortRunner.from_directory(COHORT_DIR / "cohort-v1.json", SNAPSHOTS_DIR, RECORDED_DIR)
    report = runner.run()
    assert report.schema == REPORT_SCHEMA
    assert report.verdict in {VERDICT_PASS, VERDICT_HOLD, VERDICT_ROLLBACK}


# ---------------------------------------------------------------------------
# R36-12 — pre-registration, provenance, review states on the report
# ---------------------------------------------------------------------------


def _prereg() -> Preregistration:
    return Preregistration(
        cohort_id="test-live-cohort",
        registered_at="2026-09-24T00:00:00+00:00",
        task_bindings=(
            PreregTaskBinding(
                task_id="T-1",
                archetype=ARCHETYPE_NEIGHBOR_DEPENDENCY,
                snapshot_digest="a" * 64,
                acceptance_criteria=("the decisive claim cites the neighbor's bytes",),
            ),
        ),
        eligibility={"reviewer_role": "code owner"},
        budget=BUDGET,
        arms=COHORT_MODES,
        review_procedure={
            "blind": True,
            "counterbalanced": True,
            "ordering_seed": 20260924,
            "correction_effort_procedure": "one procedure for every mode",
        },
        promotion_criteria={
            "margin": 0.05,
            "accuracy_floor": 0.8,
            "cost_bound": {"max_calls_total_per_mode": 60},
        },
        prompt_policy_digest="b" * 64,
    )


def test_preregistration_digest_is_over_the_frozen_content():
    prereg = _prereg()
    assert prereg.digest == Preregistration(**prereg.__dict__).digest
    moved = Preregistration(
        **{**prereg.__dict__, "budget": {"max_calls": 20, "wall_seconds": 90.0}}
    )
    assert moved.digest != prereg.digest  # the budget is frozen INTO the hash
    document = prereg.as_document()
    reloaded = Preregistration.from_document(document)
    assert reloaded.digest == prereg.digest
    document["budget"]["max_calls"] = 20  # tamper after the fact…
    with pytest.raises(CohortSpecError, match="digest mismatch"):
        Preregistration.from_document(document)


def test_preregistration_revision_records_its_generation():
    prereg = _prereg()
    revised = prereg.revise(
        "the research system prompt changed between iterations",
        prompt_policy_digest="c" * 64,
    )
    assert revised.generation == 2
    assert revised.supersedes == prereg.digest
    assert revised.change_reason.startswith("the research system prompt")
    with pytest.raises(CohortSpecError, match="change reason"):
        prereg.revise("   ")
    with pytest.raises(CohortSpecError, match="supersedes"):
        Preregistration(**{**prereg.__dict__, "generation": 2}).validate()


def test_preregistration_validation_pins_the_contract():
    with pytest.raises(CohortSpecError, match="arms"):
        Preregistration(**{**_prereg().__dict__, "arms": ("none",)}).validate()
    with pytest.raises(CohortSpecError, match="cost_bound"):
        Preregistration(
            **{
                **_prereg().__dict__,
                "promotion_criteria": {"margin": 0.05, "accuracy_floor": 0.8},
            }
        ).validate()
    with pytest.raises(CohortSpecError, match="correction-effort"):
        Preregistration(
            **{
                **_prereg().__dict__,
                "review_procedure": {"blind": True, "counterbalanced": True, "ordering_seed": 1},
            }
        ).validate()


def test_recorded_run_round_trips_the_capture_block():
    snapshot = make_snapshot({"src/events.py": _EVENTS_FILE})
    run = make_run(snapshot=snapshot, plan={"steps": []}, mode="research")
    captured = RecordedRun(
        **{
            **run.__dict__,
            "capture": {
                "provenance": PROVENANCE_OFFLINE_SCRIPTED,
                "preregistration": {"digest": "d" * 64, "generation": 1},
            },
        }
    )
    document = {
        "schema": RECORD_SCHEMA,
        "task_id": "T-1",
        "mode": "research",
        "snapshot_digest": snapshot.digest,
        "attempts": [{"attempt": 1, "stopped_reason": "", "cost": {}}],
        "plan": {"steps": []},
        "reviewer": {"correction_minutes": 3},
        "capture": dict(captured.capture),
    }
    reloaded = RecordedRun.from_document(document)
    assert reloaded.provenance == PROVENANCE_OFFLINE_SCRIPTED
    assert reloaded.preregistration_digest == "d" * 64
    assert reloaded.preregistration_generation == 1
    assert reloaded.review_pending is False  # a preregistered capture WITH grades
    assert captured.review_pending is True  # …and without grades it is pending


def test_pending_review_keeps_reviewer_graded_dimensions_unknown():
    """A preregistered capture without reviewer grades: accuracy, assumptions,
    question quality and the overall stay UNKNOWN — never vacuous scores."""
    snapshot = make_snapshot({"src/events.py": _EVENTS_FILE})
    task = make_task(snapshot=snapshot)
    plan = {
        "steps": [{"step_id": "s1", "objective": "act", "evidence_refs": ["ev-1"]}],
        "surface": [{"repo": "orders", "path": "src/events.py", "why": "read"}],
        "claims": [a_claim(1, 'ORDER_EVENT = "order.expired.v2"')],
        "questions": [{"question_id": "q1", "text": "which contract version?"}],
        "assumptions": [{"text": "the neighbor still consumes v2"}],
    }
    run = RecordedRun(
        **{
            **make_run(snapshot=snapshot, plan=plan).__dict__,
            "capture": {
                "provenance": PROVENANCE_LIVE_MODEL,
                "preregistration": {"digest": "d" * 64},
            },
        }
    )
    grade = grade_run(task, run, snapshot)
    assert grade["review"]["state"] == REVIEW_PENDING
    assert grade["review"]["pending_fields"] == [
        "claim_importance",
        "assumption_severity",
        "correction_minutes",
    ]
    assert grade["evidence_accuracy"]["score"] is None
    assert grade["evidence_accuracy"]["unreviewed_claims"] == 1
    # the MECHANICAL verdict still computes — support is not a reviewer grade
    assert grade["evidence_accuracy"]["claims"][0]["verdict"] == "semantic_match"
    assert grade["impacted_surface_recall"]["score"] == 1.0
    assert grade["unjustified_assumptions"]["score"] is None
    assert grade["question_quality"]["score"] is None
    assert grade["overall"] is None
    assert grade["human_plan_correction_effort"]["state"] == REVIEW_PENDING


def test_means_skip_unknown_values_instead_of_zeroing_them():
    from forge.adaptive.research_cohort import aggregate_mode

    snapshot = make_snapshot({"src/events.py": _EVENTS_FILE})
    task = make_task(snapshot=snapshot)
    plan = {
        "steps": [],
        "surface": [{"repo": "orders", "path": "src/events.py", "why": ""}],
        "claims": [a_claim(1, 'ORDER_EVENT = "order.expired.v2"')],
        "questions": [],
        "assumptions": [],
    }
    pending = RecordedRun(
        **{
            **make_run(snapshot=snapshot, plan=plan).__dict__,
            "capture": {
                "provenance": PROVENANCE_LIVE_MODEL,
                "preregistration": {"digest": "d" * 64},
            },
        }
    )
    grade = grade_run(task, pending, snapshot)
    aggregate = aggregate_mode([grade], [pending])
    assert aggregate["overall_mean"] is None
    assert aggregate["evidence_accuracy_mean"] is None
    assert aggregate["correction_minutes_mean"] is None  # unknown, never estimated
    assert aggregate["correction_minutes_known_tasks"] == 0
    assert aggregate["review_pending_tasks"] == 1


def test_shipped_report_carries_provenance_and_the_fixture_evidence_class(shipped_report):
    """The v1 fixture report names its evidence class honestly: an
    authored-fixture replay, never a live performance claim."""
    assert shipped_report["promotion"]["evidence_class"] == EVIDENCE_CLASS_FIXTURE_REPLAY
    assert shipped_report["promotion"]["human_promotion_decision"]["required"] is False
    assert shipped_report["promotion"]["sample"][CANDIDATE_MODE]["tasks_graded"] == 4
    for mode in COHORT_MODES:
        summary = shipped_report["provenance_summary"][mode]
        assert summary["unlabeled"] == 4  # authored fixtures carry no capture block
        assert summary["runs_total"] == 4
    # the v1 reviewer grades are recorded: the correction mean stays over
    # the KNOWN reviewers only, and every run is known
    assert shipped_report["aggregate"]["research"]["correction_minutes_known_tasks"] == 4
    assert "preregistration" not in shipped_report  # the fixture cohort has none
    assert shipped_report["promotion"]["verdict"] == VERDICT_PASS  # unchanged
