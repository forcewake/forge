"""R32-21 — the bounded design-partner pilot kit (research topic 04).

These tests pin the kit's contract:

- the SPEC is versioned and validated: exactly three criteria each with
  metric + threshold + method + named end date, a non-empty
  unsupported-features list recorded before onboarding, a data boundary
  with a writable repo and NO production secrets / automatic merge /
  deploy, the four ladder stages in order, a baseline frozen before
  kickoff, the three-exit policy;
- the PLAN is bounded: 12-20 tasks, unique ids, EVERY scenario tag
  present at least once (the real adaptive control path included), and
  review groups staged in ladder order;
- the TRACKER folds the five metrics + reviewer load with hand-computed
  fixtures, counts steering as an intervention, keeps unaccepted
  attempts' spend in the cost denominator, and degrades unknown spend
  to unknown — never zero;
- the STOP CONDITIONS trigger on scope and authority violations (the
  stop PRESERVES the tracker snapshot verbatim and the pilot is never
  silently retried), the 2-of-3 criteria math expands on 2 met / stops
  on fewer, and extend-once is limited to one named gap;
- the ORDERING and freeze rules are enforced: onboarding strictly
  before the first task start, and the spec untouchable once a task is
  recorded (the J-curve pre-commitment);
- the REPORT lists unaccepted tasks and operator rescues explicitly,
  decides readiness per ladder stage, and is deterministic.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from forge.adaptive.pilot import (
    CRITERIA_REQUIRED,
    DECISION_CONTINUE,
    DECISION_EXPAND,
    DECISION_EXTEND_ONCE,
    DECISION_STOP,
    MAX_PILOT_TASKS,
    METRIC_DEFINITIONS,
    MIN_PILOT_TASKS,
    PLAN_SCHEMA,
    REPORT_SCHEMA,
    SCENARIO_TAGS,
    SPEC_SCHEMA,
    STAGES,
    VIOLATION_AUTO_MERGE,
    VIOLATION_PRODUCTION_SECRET,
    VIOLATION_SCOPE,
    AcceptanceCriterion,
    AttemptUsage,
    DataBoundary,
    ExitsPolicy,
    InterventionEvent,
    LadderStage,
    OnboardingRecord,
    PilotBaseline,
    PilotError,
    PilotPlan,
    PilotReport,
    PilotSpec,
    PilotTask,
    PilotTracker,
    TaskRecord,
    VerificationCheck,
    build_pilot_report,
    evaluate_stop,
    record_onboarding,
)
from forge.adaptive import pilot as pilot_module

REPO_ROOT = Path(__file__).resolve().parents[1]
PILOT_JSON = REPO_ROOT / "evaluation" / "pilot" / "pilot-v1.json"


# ---------------------------------------------------------------------------
# Fixtures — a valid spec, a valid task, a valid plan
# ---------------------------------------------------------------------------


def _spec(**overrides: object) -> PilotSpec:
    base = PilotSpec(
        pilot_id="pilot-test",
        platform="gitlab-ce",
        recipe_id="forge-recipe/gitlab-ci/claude-code",
        harness_id="gitlab-ci-lane",
        decision_owners=("Ada Chen (acme eng lead)", "Forge pilot lead"),
        data_boundary=DataBoundary(
            writable_repos=("acme/checkout",),
            neighbor_repos=("acme/billing",),
            secrets_scope="sentinel test credentials only",
        ),
        unsupported_features=(
            "Database migrations",
            "Deploy authority",
            "Confidential access beyond the named repos",
        ),
        ladder=(
            LadderStage(STAGES[0], "read-only planning", "read-only"),
            LadderStage(STAGES[1], "single-file fixes", "draft MR"),
            LadderStage(STAGES[2], "multi-file branch+PR", "human review"),
            LadderStage(STAGES[3], "full gated flow", "operator gate"),
        ),
        criteria=(
            AcceptanceCriterion(
                criterion_id="C1",
                statement="Autonomy rate at or above 50%",
                metric="autonomy_rate",
                op=">=",
                value=0.5,
                method="tracker fold over recorded tasks",
                end_date="2026-11-18",
            ),
            AcceptanceCriterion(
                criterion_id="C2",
                statement="Cycle time at or below baseline",
                metric="cycle_time_vs_baseline",
                op="<=",
                value=1.0,
                method="mean latency / frozen baseline",
                end_date="2026-11-18",
            ),
            AcceptanceCriterion(
                criterion_id="C3",
                statement="Intervention rate at or below 40%",
                metric="intervention_rate",
                op="<=",
                value=0.4,
                method="tasks with interventions / total",
                end_date="2026-11-18",
            ),
        ),
        window_start="2026-10-21",
        window_end="2026-11-18",
        baseline=PilotBaseline(
            window_start="2026-07-23",
            window_end="2026-10-20",
            cycle_time_minutes=140.0,
        ),
        review_groups=("G1", "G2", "G3", "G4"),
        review_cadence="weekly operator session per task group",
        exits=ExitsPolicy(),
    )
    return replace(base, **overrides) if overrides else base


def _task(
    n: int,
    scenario: str = "test_repair",
    group: str = "G1",
    stage: str = STAGES[0],
    task_id: str = "",
    **overrides: object,
) -> PilotTask:
    base = PilotTask(
        task_id=task_id or f"PT-{n:02d}",
        title=f"Pilot task {n}",
        owner="Ada Chen (acme eng lead)",
        expected_outcome=f"The specific documented outcome {n}",
        non_goals=("No scope creep",),
        budget={"max_spend_usd": 4.0, "wall_minutes": 45},
        verification=(VerificationCheck(name="partner-ci", method="partner CI exits 0"),),
        scenario=scenario,
        group=group,
        stage=stage,
    )
    return replace(base, **overrides) if overrides else base


def _tasks_covering_every_tag(extra: int = 3) -> tuple[PilotTask, ...]:
    """A valid task set: one task per scenario tag, then *extra* more.

    Staged so each review group maps to exactly one ladder stage, in
    ladder order.
    """
    group_stage = (("G1", STAGES[0]), ("G2", STAGES[1]), ("G3", STAGES[2]), ("G4", STAGES[3]))
    tasks = [
        _task(
            index + 1,
            scenario=tag,
            group=group_stage[index % 4][0],
            stage=group_stage[index % 4][1],
        )
        for index, tag in enumerate(SCENARIO_TAGS)
    ]
    for extra_index in range(extra):
        group, stage = group_stage[(len(SCENARIO_TAGS) + extra_index) % 4]
        tasks.append(_task(len(tasks) + 1, scenario="test_repair", group=group, stage=stage))
    return tuple(tasks)


def _plan(tasks: tuple[PilotTask, ...] | None = None, **overrides: object) -> PilotPlan:
    return PilotPlan(
        plan_id=overrides.pop("plan_id", "pilot-plan-test") if overrides else "pilot-plan-test",
        spec=overrides.pop("spec", None) or _spec(),
        tasks=tasks if tasks is not None else _tasks_covering_every_tag(),
        **overrides,
    )


def _record(task_id: str, started_at: str, **overrides: object) -> TaskRecord:
    base = TaskRecord(
        task_id=task_id,
        started_at=started_at,
        attempts=(AttemptUsage("a1", accepted=True, spend_usd=1.0),),
    )
    return replace(base, **overrides) if overrides else base


def _tracker_with_tasks(
    records: tuple[TaskRecord, ...],
    spec: PilotSpec | None = None,
) -> PilotTracker:
    tracker = PilotTracker(spec or _spec())
    tracker.record_onboarding(
        OnboardingRecord(
            recorded_at="2026-10-20T09:00:00+00:00",
            unsupported_features=tracker.spec.unsupported_features,
            spec_digest=tracker.spec_digest,
        )
    )
    for record in records:
        tracker.record_task(record)
    return tracker


# ---------------------------------------------------------------------------
# The spec — versioned and validated
# ---------------------------------------------------------------------------


def test_spec_validates_and_roundtrips_its_document() -> None:
    spec = _spec()
    spec.validate()
    assert spec.schema == SPEC_SCHEMA
    assert spec.frozen_digest == PilotSpec.from_document(spec.as_document()).frozen_digest
    restored = PilotSpec.from_document(json.loads(json.dumps(spec.as_document())))
    assert restored == spec


def test_spec_refused_without_all_criteria() -> None:
    with pytest.raises(PilotError, match="exactly 3 criteria"):
        _spec(criteria=_spec().criteria[:2]).validate()


def test_spec_refused_when_a_criterion_lacks_its_threshold() -> None:
    criteria = list(_spec().criteria)
    criteria[0] = replace(criteria[0], value=0.0, op="")
    with pytest.raises(PilotError, match="op must be"):
        _spec(criteria=tuple(criteria)).validate()


def test_spec_refused_when_a_criterion_lacks_its_end_date() -> None:
    criteria = list(_spec().criteria)
    criteria[0] = replace(criteria[0], end_date="")
    with pytest.raises(PilotError, match="ISO date"):
        _spec(criteria=tuple(criteria)).validate()


def test_spec_refused_when_a_criterion_lacks_its_measurement_method() -> None:
    criteria = list(_spec().criteria)
    criteria[0] = replace(criteria[0], method="  ")
    with pytest.raises(PilotError, match="measurement method"):
        _spec(criteria=tuple(criteria)).validate()


def test_spec_refused_on_liar_metrics() -> None:
    criterion = replace(_spec().criteria[0], metric="pr_acceptance_rate", op=">=", value=0.9)
    with pytest.raises(PilotError, match="closed vocabulary"):
        _spec(criteria=(criterion, *_spec().criteria[1:])).validate()


def test_spec_refused_without_unsupported_features() -> None:
    with pytest.raises(PilotError, match="unsupported features BEFORE onboarding"):
        _spec(unsupported_features=()).validate()


def test_spec_refused_without_a_data_boundary() -> None:
    with pytest.raises(PilotError, match="writable repo"):
        _spec(data_boundary=DataBoundary(secrets_scope="sentinel only")).validate()


def test_spec_refused_with_production_secrets() -> None:
    boundary = replace(_spec().data_boundary, production_secrets=True)
    with pytest.raises(PilotError, match="outside EVERY pilot boundary"):
        _spec(data_boundary=boundary).validate()


def test_spec_refused_with_automatic_merge_or_deploy() -> None:
    with pytest.raises(PilotError, match="bot never merges"):
        _spec(data_boundary=replace(_spec().data_boundary, automatic_merge=True)).validate()
    with pytest.raises(PilotError, match="automatic deploy"):
        _spec(data_boundary=replace(_spec().data_boundary, automatic_deploy=True)).validate()


def test_spec_refused_with_a_broken_ladder() -> None:
    with pytest.raises(PilotError, match="four stages in order"):
        _spec(ladder=_spec().ladder[:3]).validate()
    with pytest.raises(PilotError, match="four stages in order"):
        _spec(ladder=_spec().ladder[::-1]).validate()


def test_spec_refused_without_decision_owners_or_exits() -> None:
    with pytest.raises(PilotError, match="decision owners"):
        _spec(decision_owners=()).validate()
    with pytest.raises(PilotError, match="expand/extend_once/stop"):
        _spec(exits=ExitsPolicy(allowed=("expand", "stop"), extension_limit=1)).validate()


def test_spec_refused_when_the_baseline_was_not_frozen_before_kickoff() -> None:
    late = PilotBaseline(
        window_start="2026-08-01", window_end="2026-11-01", cycle_time_minutes=140.0
    )
    with pytest.raises(PilotError, match="frozen BEFORE kickoff"):
        _spec(baseline=late).validate()


def test_spec_refused_on_an_empty_or_backwards_window() -> None:
    with pytest.raises(PilotError, match="window must be non-empty"):
        _spec(window_start="2026-11-18", window_end="2026-10-21").validate()


# ---------------------------------------------------------------------------
# The plan — bounded, every scenario tag, staged groups
# ---------------------------------------------------------------------------


def test_plan_validates_the_bounded_task_range() -> None:
    nine_tags = len(SCENARIO_TAGS)
    assert MIN_PILOT_TASKS == 12 and MAX_PILOT_TASKS == 20
    with pytest.raises(PilotError, match="12-20 tasks"):
        _plan(_tasks_covering_every_tag(extra=MIN_PILOT_TASKS - 1 - nine_tags)).validate()
    with pytest.raises(PilotError, match="12-20 tasks"):
        _plan(_tasks_covering_every_tag(extra=MAX_PILOT_TASKS + 1 - nine_tags)).validate()


def test_plan_validates_the_range_boundaries_exactly() -> None:
    nine_tags = len(SCENARIO_TAGS)
    _plan(_tasks_covering_every_tag(extra=MIN_PILOT_TASKS - nine_tags)).validate()
    _plan(_tasks_covering_every_tag(extra=MAX_PILOT_TASKS - nine_tags)).validate()


def test_plan_refused_when_a_scenario_tag_is_missing() -> None:
    tasks = tuple(task for task in _tasks_covering_every_tag() if task.scenario != "runner_loss")
    dropped = _task(99, scenario="test_repair", group="G1", stage=STAGES[0])
    with pytest.raises(PilotError, match="runner_loss"):
        _plan((*tasks, dropped)).validate()


def test_plan_refused_when_groups_mix_stages_or_break_order() -> None:
    mixed = list(_tasks_covering_every_tag())
    mixed[0] = replace(mixed[0], stage=STAGES[2])
    with pytest.raises(PilotError, match="mixes ladder stages"):
        _plan(tuple(mixed)).validate()
    reordered_spec = _spec(review_groups=("G2", "G1", "G3", "G4"))
    with pytest.raises(PilotError, match="ladder order"):
        PilotPlan(plan_id="p", spec=reordered_spec, tasks=_tasks_covering_every_tag()).validate()


def test_plan_refused_on_undeclared_groups_or_unknown_scenarios() -> None:
    tasks = list(_tasks_covering_every_tag())
    tasks[0] = replace(tasks[0], group="GX")
    with pytest.raises(PilotError, match="not declared in the spec"):
        _plan(tuple(tasks)).validate()
    tasks[0] = replace(tasks[0], group="G1", scenario="demo_path")
    with pytest.raises(PilotError, match="closed vocabulary"):
        _plan(tuple(tasks)).validate()


def test_task_refused_without_owner_outcome_non_goals_or_verification() -> None:
    with pytest.raises(PilotError, match="owner"):
        _task(1, owner=" ").validate()
    with pytest.raises(PilotError, match="expected_outcome"):
        _task(1, expected_outcome=" ").validate()
    with pytest.raises(PilotError, match="non-goals"):
        _task(1, non_goals=()).validate()
    with pytest.raises(PilotError, match="verification contract"):
        _task(1, verification=()).validate()
    with pytest.raises(PilotError, match="max_spend_usd"):
        _task(1, budget={"wall_minutes": 30}).validate()


def test_shipped_pilot_v1_loads_validates_and_covers_every_tag() -> None:
    plan = PilotPlan.load(PILOT_JSON)
    assert plan.schema == PLAN_SCHEMA
    assert len(plan.tasks) == 14
    assert {task.scenario for task in plan.tasks} == set(SCENARIO_TAGS)
    # Staged groups: each group is exactly one ladder stage, in order.
    group_stages = {task.group: task.stage for task in plan.tasks}
    assert list(group_stages.values()) == list(STAGES)
    # The real adaptive control path is exercised at the gate stage.
    adaptive = [task for task in plan.tasks if task.scenario == "adaptive_control"]
    assert any(task.stage == "gate" for task in adaptive)
    # Roundtrip stability: the loaded plan re-serializes to the same spec digest.
    assert PilotPlan.from_document(plan.as_document()).spec.frozen_digest == (
        plan.spec.frozen_digest
    )


# ---------------------------------------------------------------------------
# The tracker — hand-computed metric fixtures
# ---------------------------------------------------------------------------


def _metrics_fixture_tracker() -> PilotTracker:
    """Three tasks designed for hand computation:

    - PT-01 accepted, no human code change, attempts 1.0 + 0.5 (the
      0.5 attempt was unaccepted), one steer, review 20, latency 90;
    - PT-02 accepted AFTER a human code change, spend 2.0, no
      interventions, review 30, latency 120, one defect;
    - PT-03 UNACCEPTED, spend 0.75, two interventions (pause + steer),
      review 10, latency never completed.
    Baseline cycle time in :func:`_spec` is 140 minutes.
    """
    records = (
        TaskRecord(
            task_id="PT-01",
            started_at="2026-10-21T10:00:00+00:00",
            accepted=True,
            attempts=(
                AttemptUsage("a1", accepted=False, spend_usd=0.5),
                AttemptUsage("a2", accepted=True, spend_usd=1.0),
            ),
            interventions=(InterventionEvent("steer", "narrow the search"),),
            review_minutes=20.0,
            completion_latency_minutes=90.0,
        ),
        TaskRecord(
            task_id="PT-02",
            started_at="2026-10-22T10:00:00+00:00",
            accepted=True,
            human_code_change=True,
            attempts=(AttemptUsage("a1", accepted=True, spend_usd=2.0),),
            review_minutes=30.0,
            completion_latency_minutes=120.0,
            defects=1,
        ),
        TaskRecord(
            task_id="PT-03",
            started_at="2026-10-23T10:00:00+00:00",
            accepted=False,
            attempts=(AttemptUsage("a1", accepted=False, spend_usd=0.75),),
            interventions=(
                InterventionEvent("pause", "inspect"),
                InterventionEvent("steer", "wrong file"),
            ),
            review_minutes=10.0,
            manual_rescues=1,
            rescue_notes=("operator re-ran the lane",),
        ),
    )
    return _tracker_with_tasks(records)


def test_tracker_metrics_match_hand_computed_values() -> None:
    metrics = _metrics_fixture_tracker().metrics()
    assert metrics.tasks_total == 3
    assert metrics.tasks_accepted == 2
    assert metrics.tasks_unaccepted == 1
    # autonomy: accepted-without-human-code-change = PT-01 only -> 1/3
    assert metrics.autonomy_rate == pytest.approx(1 / 3, abs=1e-4)
    # all-attempt spend: 0.5 + 1.0 + 2.0 + 0.75 = 4.25 over 2 accepted
    assert metrics.total_cost_usd == pytest.approx(4.25)
    assert metrics.cost_per_accepted_usd == pytest.approx(2.125)
    # defects + rollbacks: 1 over 2 accepted
    assert metrics.defect_rollback_rate == pytest.approx(0.5)
    # interventions: PT-01 and PT-03 -> 2/3; events 3/3
    assert metrics.intervention_rate == pytest.approx(2 / 3, abs=1e-4)
    assert metrics.interventions_per_task == pytest.approx(1.0)
    # cycle: (90 + 120)/2 = 105 over baseline 140 -> 0.75
    assert metrics.mean_cycle_time_minutes == pytest.approx(105.0)
    assert metrics.cycle_time_vs_baseline == pytest.approx(0.75)
    # reviewer load: (20 + 30 + 10)/3
    assert metrics.reviewer_load_minutes_per_task == pytest.approx(20.0)


def test_metrics_definitions_document_every_metric_in_the_report() -> None:
    metrics = _metrics_fixture_tracker().metrics().to_json()
    for name in METRIC_DEFINITIONS:
        assert name in metrics
    # Every rate states its numerator ÷ denominator; the reviewer-load
    # counterweight states its unit instead.
    for name in (
        "autonomy_rate",
        "cost_per_accepted_usd",
        "defect_rollback_rate",
        "intervention_rate",
        "cycle_time_vs_baseline",
    ):
        assert "÷" in METRIC_DEFINITIONS[name]
    assert "minutes" in METRIC_DEFINITIONS["reviewer_load_minutes_per_task"]
    assert "steering" in METRIC_DEFINITIONS["intervention_rate"]


def test_steering_counts_as_an_intervention() -> None:
    steered = TaskRecord(
        task_id="PT-S",
        started_at="2026-10-21T10:00:00+00:00",
        accepted=True,
        attempts=(AttemptUsage("a1", accepted=True, spend_usd=1.0),),
        interventions=(InterventionEvent("steer"),),
    )
    tracker = _tracker_with_tasks((steered,))
    metrics = tracker.metrics()
    assert metrics.intervention_rate == 1.0
    assert metrics.intervention_rate is not None


def test_intervention_kind_vocabulary_is_closed() -> None:
    with pytest.raises(PilotError, match="closed vocabulary"):
        InterventionEvent("nudge").validate()
    for kind in ("steer", "pause", "resume", "cancel", "takeover", "question_answer"):
        InterventionEvent(kind).validate()


def test_unaccepted_attempt_spend_stays_in_the_cost_denominator() -> None:
    record = TaskRecord(
        task_id="PT-U",
        started_at="2026-10-21T10:00:00+00:00",
        accepted=True,
        attempts=(
            AttemptUsage("a1", accepted=False, spend_usd=9.0),
            AttemptUsage("a2", accepted=True, spend_usd=1.0),
        ),
    )
    metrics = _tracker_with_tasks((record,)).metrics()
    assert metrics.total_cost_usd == pytest.approx(10.0)
    assert metrics.cost_per_accepted_usd == pytest.approx(10.0)


def test_missing_spend_degrades_cost_to_unknown_never_zero() -> None:
    record = TaskRecord(
        task_id="PT-M",
        started_at="2026-10-21T10:00:00+00:00",
        accepted=True,
        attempts=(
            AttemptUsage("a1", accepted=False, spend_usd=1.0),
            AttemptUsage("a2", accepted=True, spend_usd=None),
        ),
    )
    metrics = _tracker_with_tasks((record,)).metrics()
    assert metrics.total_cost_usd is None
    assert metrics.cost_per_accepted_usd is None
    assert any("never zero" in note for note in metrics.notes)


def test_empty_tracker_metrics_are_unknown_not_zero() -> None:
    tracker = PilotTracker(_spec())
    metrics = tracker.metrics()
    assert metrics.tasks_total == 0
    assert metrics.autonomy_rate is None
    assert metrics.reviewer_load_minutes_per_task is None
    assert any("no tasks recorded" in note for note in metrics.notes)


def test_record_task_validates_and_refuses_duplicates() -> None:
    tracker = _tracker_with_tasks(())
    good = _record("PT-01", "2026-10-21T10:00:00+00:00")
    tracker.record_task(good)
    with pytest.raises(PilotError, match="already recorded"):
        tracker.record_task(good)
    with pytest.raises(PilotError, match="ISO datetime"):
        tracker.record_task(_record("PT-02", "not-a-timestamp"))
    with pytest.raises(PilotError, match="notes"):
        tracker.record_task(
            _record(
                "PT-03",
                "2026-10-21T10:00:00+00:00",
                manual_rescues=0,
                rescue_notes=("one note too many",),
            )
        )


# ---------------------------------------------------------------------------
# Scope and authority violations
# ---------------------------------------------------------------------------


def test_task_touching_outside_the_boundary_records_a_scope_violation() -> None:
    record = _record(
        "PT-01",
        "2026-10-21T10:00:00+00:00",
        touched_repos=("acme/checkout", "acme/infrastructure"),
    )
    tracker = _tracker_with_tasks((record,))
    assert [v.kind for v in tracker.violations] == [VIOLATION_SCOPE]
    assert "acme/infrastructure" in tracker.violations[0].detail


def test_authority_violation_stops_and_preserves_diagnostics_with_no_retry() -> None:
    tracker = _tracker_with_tasks((_record("PT-01", "2026-10-21T10:00:00+00:00"),))
    tracker.record_authority_violation(
        VIOLATION_AUTO_MERGE, detail="lane scripted git push to main", task_id="PT-01"
    )
    decision = evaluate_stop(tracker, as_of="2026-10-22")
    assert decision.decision == DECISION_STOP
    assert decision.diagnostics_pointer
    assert tracker.diagnostics is not None
    # The snapshot is retained VERBATIM: the preserved bytes are the
    # records document, and re-preserving returns the same object.
    assert tracker.diagnostics.snapshot == tracker.snapshot_document()
    assert tracker.preserve_diagnostics("again").pointer == decision.diagnostics_pointer
    # NEVER silently retried: the stopped tracker refuses new work.
    with pytest.raises(PilotError, match="NEVER silently retried"):
        tracker.record_task(_record("PT-02", "2026-10-24T10:00:00+00:00"))
    with pytest.raises(PilotError, match="NEVER silently retried"):
        tracker.record_authority_violation(VIOLATION_PRODUCTION_SECRET, "again")


def test_production_secret_violation_also_stops_immediately() -> None:
    tracker = _tracker_with_tasks(())
    tracker.record_authority_violation(VIOLATION_PRODUCTION_SECRET, "used PROD_TOKEN")
    decision = evaluate_stop(tracker, as_of="2026-10-21")
    assert decision.decision == DECISION_STOP
    assert "production_secret_used" in decision.reasons[0]


def test_scope_violation_stops_before_any_criteria_math() -> None:
    tracker = _tracker_with_tasks(
        (
            _record(
                "PT-01",
                "2026-10-21T10:00:00+00:00",
                touched_repos=("acme/payments",),
            ),
        )
    )
    decision = evaluate_stop(tracker, as_of="2026-11-19")
    assert decision.decision == DECISION_STOP
    assert decision.reasons[0].startswith("scope violation")
    assert tracker.diagnostics is not None


def test_authority_violation_kind_vocabulary_is_closed() -> None:
    tracker = PilotTracker(_spec())
    with pytest.raises(PilotError, match="authority violation kind"):
        tracker.record_authority_violation(VIOLATION_SCOPE, "wrong channel")


# ---------------------------------------------------------------------------
# Criteria math: 2-of-3, extend-once, the J-curve window
# ---------------------------------------------------------------------------


def _criteria_spec(op_values: tuple[tuple[str, str, float], ...]) -> PilotSpec:
    spec = _spec()
    criteria = tuple(
        replace(criterion, op=op, value=value)
        for criterion, (metric, op, value) in zip(spec.criteria, op_values, strict=True)
    )
    return replace(spec, criteria=criteria)


def test_two_of_three_met_expands() -> None:
    # Fixture metrics: autonomy 1/3 (~0.33), cycle 0.75, intervention 2/3.
    # Criteria: autonomy >= 0.2 (met), cycle <= 1.0 (met), intervention <= 0.9 (met)
    # -> 3 met; shrink one threshold to make it exactly 2-of-3.
    spec = _criteria_spec(
        (
            ("autonomy_rate", ">=", 0.2),
            ("cycle_time_vs_baseline", "<=", 1.0),
            ("intervention_rate", "<=", 0.5),
        )
    )
    # intervention 2/3 = 0.6667 > 0.5 -> unmet; autonomy 0.3333 >= 0.2 met;
    # cycle 0.75 <= 1.0 met -> exactly 2 of 3.
    tracker = _tracker_with_tasks(
        _fixture_records_for_criteria(),
        spec=spec,
    )
    decision = evaluate_stop(tracker, spec, as_of="2026-11-18")
    assert decision.decision == DECISION_EXPAND
    assert decision.met_criteria == ("C1", "C2")
    assert decision.unmet_criteria == ("C3",)
    assert tracker.diagnostics is None


def test_one_of_three_met_stops_with_preserved_diagnostics() -> None:
    # Only cycle time met: autonomy >= 0.9 (unmet), cycle <= 1.0 (met),
    # intervention <= 0.2 (unmet).
    spec = _criteria_spec(
        (
            ("autonomy_rate", ">=", 0.9),
            ("cycle_time_vs_baseline", "<=", 1.0),
            ("intervention_rate", "<=", 0.2),
        )
    )
    tracker = _tracker_with_tasks(_fixture_records_for_criteria(), spec=spec)
    decision = evaluate_stop(tracker, spec, as_of="2026-11-18")
    assert decision.decision == DECISION_STOP
    assert decision.met_criteria == ("C2",)
    assert tracker.diagnostics is not None
    assert decision.diagnostics_pointer == tracker.diagnostics.pointer


def _fixture_records_for_criteria() -> tuple[TaskRecord, ...]:
    return (
        TaskRecord(
            task_id="PT-01",
            started_at="2026-10-21T10:00:00+00:00",
            accepted=True,
            attempts=(AttemptUsage("a1", accepted=True, spend_usd=1.0),),
            interventions=(InterventionEvent("steer"),),
            review_minutes=20.0,
            completion_latency_minutes=90.0,
        ),
        TaskRecord(
            task_id="PT-02",
            started_at="2026-10-22T10:00:00+00:00",
            accepted=True,
            human_code_change=True,
            attempts=(AttemptUsage("a1", accepted=True, spend_usd=2.0),),
            review_minutes=30.0,
            completion_latency_minutes=120.0,
        ),
        TaskRecord(
            task_id="PT-03",
            started_at="2026-10-23T10:00:00+00:00",
            accepted=False,
            attempts=(AttemptUsage("a1", accepted=False, spend_usd=0.75),),
            interventions=(InterventionEvent("pause"),),
            review_minutes=10.0,
        ),
    )


def test_unknown_metrics_count_as_unjudgeable_not_silent_failure() -> None:
    # No latency recorded anywhere -> cycle_time_vs_baseline unknown.
    spec = _criteria_spec(
        (
            ("autonomy_rate", ">=", 0.2),
            ("cycle_time_vs_baseline", "<=", 1.0),
            ("intervention_rate", "<=", 0.9),
        )
    )
    records = tuple(
        replace(record, completion_latency_minutes=None)
        for record in _fixture_records_for_criteria()
    )
    tracker = _tracker_with_tasks(records, spec=spec)
    decision = evaluate_stop(tracker, spec, as_of="2026-11-18")
    assert decision.decision == DECISION_EXPAND  # autonomy + intervention met
    assert decision.unjudgeable_criteria == ("C2",)


def test_extend_once_requires_a_named_gap_and_happens_once() -> None:
    # Nothing met: thresholds impossible for the fixture.
    spec = _criteria_spec(
        (
            ("autonomy_rate", ">=", 0.99),
            ("cycle_time_vs_baseline", "<=", 0.1),
            ("intervention_rate", "<=", 0.05),
        )
    )
    tracker = _tracker_with_tasks(_fixture_records_for_criteria(), spec=spec)
    # Without a named gap the exit is stop, not a silent extension.
    decision = evaluate_stop(tracker, spec, as_of="2026-11-18")
    assert decision.decision == DECISION_STOP

    tracker2 = _tracker_with_tasks(_fixture_records_for_criteria(), spec=spec)
    extended = evaluate_stop(tracker2, spec, as_of="2026-11-18", named_gap="runner loss recovery")
    assert extended.decision == DECISION_EXTEND_ONCE
    assert extended.gap == "runner loss recovery"
    assert tracker2.extensions_used == 1
    # The extension limit is one: a second named gap is a stop.
    stopped = evaluate_stop(tracker2, spec, as_of="2026-12-02", named_gap="something else")
    assert stopped.decision == DECISION_STOP
    assert tracker2.diagnostics is not None
    # And the tracker itself refuses to over-extend (on a tracker whose
    # one extension is used but which has NOT stopped).
    tracker3 = _tracker_with_tasks(_fixture_records_for_criteria(), spec=spec)
    evaluate_stop(tracker3, spec, as_of="2026-11-18", named_gap="runner loss recovery")
    with pytest.raises(PilotError, match="extend-once means once"):
        tracker3.grant_extension("a third try")
    with pytest.raises(PilotError, match="NAMED gap"):
        PilotTracker(spec).grant_extension("  ")


def test_no_judging_during_the_dip_before_the_named_end_date() -> None:
    tracker = _tracker_with_tasks(_fixture_records_for_criteria())
    decision = evaluate_stop(tracker, as_of="2026-10-28")
    assert decision.decision == DECISION_CONTINUE
    assert "not reached" in decision.reasons[0]
    assert tracker.diagnostics is None


def test_decision_date_is_the_latest_named_end_date() -> None:
    spec = _spec()
    criteria = list(spec.criteria)
    criteria[2] = replace(criteria[2], end_date="2026-12-02")
    spec = replace(spec, criteria=tuple(criteria))
    assert spec.decision_date == date(2026, 12, 2)
    tracker = _tracker_with_tasks(_fixture_records_for_criteria(), spec=spec)
    # Before the LATEST end date: continue, even past the other two.
    assert evaluate_stop(tracker, spec, as_of="2026-11-19").decision == DECISION_CONTINUE
    assert evaluate_stop(tracker, spec, as_of="2026-12-02").decision in (
        DECISION_EXPAND,
        DECISION_STOP,
        DECISION_EXTEND_ONCE,
    )


# ---------------------------------------------------------------------------
# Onboarding ordering and the frozen spec (J-curve pre-commitment)
# ---------------------------------------------------------------------------


def test_record_onboarding_snapshots_unsupported_features_pre_task() -> None:
    spec = _spec()
    onboarding = record_onboarding(spec, recorded_at="2026-10-20T09:00:00+00:00")
    assert onboarding.unsupported_features == spec.unsupported_features
    assert onboarding.spec_digest == spec.frozen_digest


def test_onboarding_must_be_recorded_before_any_task() -> None:
    tracker = PilotTracker(_spec())
    tracker.record_task(_record("PT-01", "2026-10-21T10:00:00+00:00"))
    with pytest.raises(PilotError, match="BEFORE any task"):
        tracker.record_onboarding(record_onboarding(_spec()))
    # And only once.
    tracker2 = PilotTracker(_spec())
    first = record_onboarding(_spec(), recorded_at="2026-10-20T09:00:00+00:00")
    tracker2.record_onboarding(first)
    with pytest.raises(PilotError, match="once"):
        tracker2.record_onboarding(first)


def test_onboarding_is_bound_to_this_spec() -> None:
    tracker = PilotTracker(_spec())
    other = record_onboarding(_spec(pilot_id="a-different-pilot"))
    with pytest.raises(PilotError, match="different spec"):
        tracker.record_onboarding(other)


def test_report_refuses_onboarding_recorded_after_the_first_task() -> None:
    plan = _plan()
    tracker = PilotTracker(plan.spec)
    late = OnboardingRecord(
        recorded_at="2026-10-22T10:00:00+00:00",  # AFTER the first task start
        unsupported_features=plan.spec.unsupported_features,
        spec_digest=plan.spec.frozen_digest,
    )
    tracker.record_onboarding(late)
    tracker.record_task(_record(plan.tasks[0].task_id, "2026-10-21T10:00:00+00:00"))
    with pytest.raises(PilotError, match="ordering violated"):
        build_pilot_report(plan, tracker)


def test_report_requires_an_onboarding_record() -> None:
    plan = _plan()
    tracker = PilotTracker(plan.spec)
    with pytest.raises(PilotError, match="pre-onboarding record"):
        build_pilot_report(plan, tracker)


def test_spec_start_date_cannot_be_edited_after_tasks_recorded() -> None:
    tracker = PilotTracker(_spec())
    # Before anything is recorded, retargeting is a legitimate setup move.
    tracker.retarget_spec(_spec(window_start="2026-10-28"))
    tracker.record_onboarding(
        record_onboarding(tracker.spec, recorded_at="2026-10-20T09:00:00+00:00")
    )
    tracker.record_task(_record("PT-01", "2026-10-29T10:00:00+00:00"))
    with pytest.raises(PilotError, match="measurement window was fixed pre-kickoff"):
        tracker.retarget_spec(_spec(window_start="2026-11-01"))


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _report_tracker(plan: PilotPlan) -> PilotTracker:
    tracker = PilotTracker(plan.spec)
    tracker.record_onboarding(record_onboarding(plan.spec, recorded_at="2026-10-20T09:00:00+00:00"))
    tracker.record_task(
        _record(
            plan.tasks[0].task_id,
            "2026-10-21T10:00:00+00:00",
            accepted=True,
            attempts=(
                AttemptUsage("a1", accepted=False, spend_usd=0.5),
                AttemptUsage("a2", accepted=True, spend_usd=1.0),
            ),
            review_minutes=20.0,
            completion_latency_minutes=90.0,
        )
    )
    # An unaccepted task with an operator rescue — it must appear
    # EXPLICITLY in the report.
    tracker.record_task(
        _record(
            plan.tasks[1].task_id,
            "2026-10-22T10:00:00+00:00",
            accepted=False,
            attempts=(AttemptUsage("a1", accepted=False, spend_usd=2.0),),
            manual_rescues=1,
            rescue_notes=("operator re-ran the lane by hand",),
            interventions=(InterventionEvent("steer", "wrong direction"),),
            review_minutes=15.0,
        )
    )
    return tracker


def test_report_lists_unaccepted_tasks_and_rescues_explicitly() -> None:
    plan = _plan()
    tracker = _report_tracker(plan)
    report = build_pilot_report(plan, tracker, as_of="2026-10-28")
    unaccepted_id = plan.tasks[1].task_id
    entry = report.document["tasks"][unaccepted_id]
    assert entry["unaccepted"] is True
    assert entry["accepted"] is False
    assert entry["manual_rescues"] == 1
    assert entry["rescue_notes"] == ["operator re-ran the lane by hand"]
    assert entry["recorded"] is True
    # The accepted task's UNACCEPTED first attempt keeps its spend.
    accepted_entry = report.document["tasks"][plan.tasks[0].task_id]
    assert accepted_entry["attempts"][0]["spend_usd"] == 0.5
    # A plan task with no record appears as a named gap, not silence.
    missing = report.document["tasks"][plan.tasks[2].task_id]
    assert missing["recorded"] is False
    assert "never silence" in missing["note"]


def test_report_carries_metrics_baseline_definitions_and_verdict() -> None:
    plan = _plan()
    tracker = _report_tracker(plan)
    report = build_pilot_report(plan, tracker, as_of="2026-11-18")
    assert report.schema == REPORT_SCHEMA
    metrics = report.document["metrics"]
    assert metrics["tasks_total"] == 2
    assert metrics["baseline_cycle_time_minutes"] == plan.spec.baseline.cycle_time_minutes
    assert metrics["definitions"] == dict(METRIC_DEFINITIONS)
    verdict = report.document["criteria_verdict"]
    assert verdict["rule"].startswith("2-of-3")
    assert verdict["verdict"] in {"met", "not_met"}
    assert len(report.document["criteria"]) == CRITERIA_REQUIRED
    # Readiness is per capability stage, honest about the unrecorded mass.
    readiness = report.document["readiness"]
    assert set(readiness) == set(STAGES)
    assert readiness[STAGES[0]]["label"] == "experimental"
    assert readiness[STAGES[0]]["reason"]
    # Provenance labels and the honest limitations ride every report.
    assert report.document["provenance_labels"] == list(plan.spec.provenance_labels)
    assert any("NOT a run pilot" in note for note in report.document["limitations"])


def test_report_groups_carry_per_group_review_records() -> None:
    plan = _plan()
    tracker = _report_tracker(plan)
    report = build_pilot_report(plan, tracker, as_of="2026-11-18")
    groups = report.document["groups"]
    assert set(groups) == set(plan.spec.review_groups)
    first = groups[plan.tasks[0].group]
    assert first["cadence"] == plan.spec.review_cadence
    assert first["review_record"]["tasks_recorded"] == 1
    assert first["review_record"]["tasks_accepted"] == 1


def test_report_is_deterministic_and_writable(tmp_path: Path) -> None:
    plan = _plan()
    tracker = _report_tracker(plan)
    first = build_pilot_report(plan, tracker, as_of="2026-11-18")
    second = build_pilot_report(plan, tracker, as_of="2026-11-18")
    assert first.document == second.document
    assert first.digest == second.digest
    target = tmp_path / "pilot-report.json"
    first.write(target)
    reread = PilotReport(document=json.loads(target.read_text(encoding="utf-8")))
    assert reread.digest == first.digest
    assert reread.document == first.document


def test_report_records_stop_reason_and_diagnostics_pointer() -> None:
    plan = _plan()
    tracker = _report_tracker(plan)
    tracker.record_authority_violation(VIOLATION_AUTO_MERGE, detail="scripted merge")
    report = build_pilot_report(plan, tracker, as_of="2026-10-28")
    stop = report.document["stop"]
    assert stop is not None
    assert "auto_merge_attempt" in stop["reason"]
    assert stop["diagnostics_pointer"] == tracker.diagnostics.pointer
    assert stop["retried"] is False
    assert report.document["decision"]["decision"] == DECISION_STOP


def test_report_refuses_a_tracker_bound_to_a_different_spec() -> None:
    plan = _plan()
    other = _plan(spec=_spec(pilot_id="someone-elses-pilot"))
    tracker = _report_tracker(other)
    with pytest.raises(PilotError, match="different spec"):
        build_pilot_report(plan, tracker)


# ---------------------------------------------------------------------------
# R36-16 — the recording helpers the lab-pilot runner needs
# ---------------------------------------------------------------------------


def test_blocked_record_roundtrips_and_can_never_be_accepted() -> None:
    with pytest.raises(PilotError, match="blocked task cannot be accepted"):
        TaskRecord(
            task_id="PT-blocked",
            started_at="2026-10-21T09:00:00+00:00",
            accepted=True,
            blocked="blocked: seam gap",
        ).validate()

    record = TaskRecord(
        task_id="PT-blocked",
        started_at="2026-10-21T09:00:00+00:00",
        blocked="blocked: seam gap",
    )
    document = pilot_module._task_record_document(record)
    assert document["blocked"] == "blocked: seam gap"
    rebuilt = pilot_module.task_record_from_document(document)
    assert rebuilt == record


def test_record_blocked_task_prefixes_the_reason_and_keeps_denominators() -> None:
    tracker = _tracker_with_tasks(())
    pilot_module.record_blocked_task(
        tracker, "PT-99", "2026-10-21T09:00:00+00:00", "seam gap: no driver"
    )
    (record,) = tracker.records
    assert record.blocked == "blocked: seam gap: no driver"
    assert record.accepted is False
    metrics = tracker.metrics()
    assert metrics.tasks_total == 1  # the denominator keeps it
    assert metrics.tasks_accepted == 0
    assert metrics.autonomy_rate == 0.0
    assert metrics.cycle_time_vs_baseline is None  # no completion latency


def test_task_record_from_document_roundtrips_every_field() -> None:
    record = _record(
        "PT-full",
        "2026-10-21T09:00:00+00:00",
        accepted=True,
        human_code_change=False,
        setup_minutes=3.5,
        plan_corrections=2,
        plan_correction_notes=("first", "second"),
        manual_rescues=1,
        rescue_notes=("operator fixed the branch",),
        completion_latency_minutes=12.25,
        attempts=(
            AttemptUsage("a1", accepted=False),
            AttemptUsage("a2", accepted=True, spend_usd=None),
        ),
        interventions=(InterventionEvent(kind="steer", note="tighten"),),
        review_minutes=6.0,
        defects=0,
        rollbacks=1,
        touched_repos=("acme/checkout", "acme/billing"),
        blocked="",
    )
    rebuilt = pilot_module.task_record_from_document(pilot_module._task_record_document(record))
    assert rebuilt == record


def test_tracker_from_snapshot_rebuilds_the_report_identically() -> None:
    plan = _plan()
    records = (
        _record("PT-01", "2026-10-21T09:00:00+00:00", completion_latency_minutes=100.0),
        _record(
            "PT-02",
            "2026-10-21T10:00:00+00:00",
            accepted=False,
            manual_rescues=1,
            rescue_notes=("rescue",),
            interventions=(InterventionEvent(kind="pause"),),
        ),
        TaskRecord(task_id="PT-03", started_at="2026-10-21T11:00:00+00:00", blocked="blocked: x"),
    )
    tracker = _tracker_with_tasks(records)
    snapshot = tracker.snapshot_document()
    rebuilt = pilot_module.tracker_from_snapshot(tracker.spec, snapshot)
    assert rebuilt.snapshot_document() == snapshot
    # The reports agree byte-for-byte (the determinism proof the
    # lab-pilot runner's --rebuild-from relies on).
    first = build_pilot_report(plan, tracker, as_of="2026-10-28")
    second = build_pilot_report(plan, rebuilt, as_of="2026-10-28")
    assert first.document == second.document


def test_tracker_from_snapshot_replays_a_stop_with_the_same_pointer() -> None:
    records = (
        _record(
            "PT-out",
            "2026-10-21T09:00:00+00:00",
            touched_repos=("someone/elses-repo",),
        ),
    )
    tracker = _tracker_with_tasks(records)
    decision = evaluate_stop(tracker, as_of="2026-11-18")
    assert decision.decision == DECISION_STOP
    snapshot = tracker.snapshot_document()
    rebuilt = pilot_module.tracker_from_snapshot(
        tracker.spec, snapshot, stop_reason=tracker.diagnostics.stop_reason
    )
    assert rebuilt.diagnostics is not None
    assert rebuilt.diagnostics.pointer == tracker.diagnostics.pointer
    assert rebuilt.diagnostics.snapshot == tracker.diagnostics.snapshot


def test_usage_ledger_rows_label_unknown_costs_never_zero() -> None:
    unknown = pilot_module.usage_ledger_document(
        "a1", source="lab-lane/vendor-wire", input_tokens=518, output_tokens=231, model_time_s=0.8
    )
    assert unknown["spend_usd"] is None
    assert unknown["cost_state"] == "unknown"
    assert "never zero" in unknown["cost_note"]
    assert unknown["tokens"] == {"input": 518, "output": 231, "total_known": 749}

    partial = pilot_module.usage_ledger_document("a2", source="s", input_tokens=10)
    assert partial["tokens"]["total_known"] is None  # one-sided tokens stay unknown

    known = pilot_module.usage_ledger_document("a3", source="s", spend_usd=1.5)
    assert known["cost_state"] == "known"

    with pytest.raises(PilotError, match="non-empty attempt_id"):
        pilot_module.usage_ledger_document("", source="s")
    with pytest.raises(PilotError, match="named source"):
        pilot_module.usage_ledger_document("a4", source=" ")


# ---------------------------------------------------------------------------
# R37-12 (#293): the budget-overrun stop rule + the all-attempt spend fold
# ---------------------------------------------------------------------------


def test_spend_total_usd_folds_every_attempt_and_degrades_to_unknown() -> None:
    tracker = _tracker_with_tasks(
        (
            _record(
                "PT-a",
                "2026-10-21T09:00:00+00:00",
                attempts=(
                    AttemptUsage("a1", accepted=True, spend_usd=1.0),
                    AttemptUsage("a2", accepted=False, spend_usd=0.5),
                ),
            ),
        )
    )
    # Unaccepted attempts' spend counts — the all-attempt fold.
    assert tracker.spend_total_usd == 1.5

    unknown = _tracker_with_tasks(
        (
            _record(
                "PT-b",
                "2026-10-21T09:30:00+00:00",
                attempts=(
                    AttemptUsage("a1", accepted=True, spend_usd=1.0),
                    AttemptUsage("a2", spend_usd=None),
                ),
            ),
        )
    )
    assert unknown.spend_total_usd is None  # unknown, never zero
    assert tracker.spend_total_usd is not None  # a full-known tracker stays known


def test_budget_overrun_stops_the_pilot_and_preserves_diagnostics() -> None:
    tracker = _tracker_with_tasks((_record("PT-c", "2026-10-21T09:00:00+00:00", accepted=False),))
    tracker.record_budget_overrun(
        detail="recorded spend 30.0 USD passed the agreed cap 25.0 USD", task_id="PT-c"
    )
    decision = evaluate_stop(tracker, as_of="2026-11-18")
    assert decision.decision == DECISION_STOP
    assert "budget overrun" in decision.reasons[0]
    assert tracker.diagnostics is not None
    assert decision.diagnostics_pointer == tracker.diagnostics.pointer
    # A stopped pilot refuses further recording — never silently retried.
    with pytest.raises(PilotError, match="cannot record task"):
        tracker.record_task(_record("PT-d", "2026-10-21T10:00:00+00:00"))


def test_budget_overrun_replays_from_the_snapshot_exactly() -> None:
    records = (
        _record(
            "PT-e",
            "2026-10-21T09:00:00+00:00",
            attempts=(AttemptUsage("a1", accepted=True, spend_usd=30.0),),
        ),
    )
    tracker = _tracker_with_tasks(records)
    tracker.record_budget_overrun("cap passed", task_id="PT-e")
    decision = evaluate_stop(tracker, as_of="2026-11-18")
    assert decision.decision == DECISION_STOP
    snapshot = tracker.snapshot_document()
    rebuilt = pilot_module.tracker_from_snapshot(
        tracker.spec, snapshot, stop_reason=tracker.diagnostics.stop_reason
    )
    assert [v.kind for v in rebuilt.violations] == [v.kind for v in tracker.violations]
    assert rebuilt.diagnostics is not None and tracker.diagnostics is not None
    assert rebuilt.diagnostics.pointer == tracker.diagnostics.pointer
