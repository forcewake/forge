"""R37-12 (issue #293) — the staged design-partner pilot LADDER.

These tests pin the ladder's contract:

- the staged contract: gate predicates between stages, ordering, and
  the FAIL-CLOSED partner prerequisites (a pending customer blocks
  supervised-batch; the machinery cannot fabricate a customer);
- the customer baseline: separate typed axes, provenance
  observed|pending only — an AUTHORED baseline is refused outright,
  and a pending axis never advances the ladder;
- the stop rules: a tripped rule preserves diagnostics verbatim, halts
  the ladder, and no stage may start while it is tripped; the budget
  stop rule trips on recorded spend past the cap;
- the decision record: continue/narrow/redesign/stop with reasons and
  retained evidence, engineering feasibility and adoption willingness
  as SEPARATE typed verdicts — unknown-pending-partner is forced while
  the customer is pending, and the report ships the UNFILLED template;
- the stage runner's preflight arms: aligned / misaligned /
  caps-absent / gateway-down each produce a pending-lab stage record
  with the exact unmet preconditions;
- the report's honest-status rendering and the ladder state's replay
  determinism (byte-identical round trip).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from forge.adaptive import pilot_ladder as pl
from forge.adaptive.pilot_ladder import (
    ADOPTION_UNKNOWN_PENDING_PARTNER,
    ADOPTION_WILLING,
    BASELINE_AXES,
    CUSTOMER_PENDING,
    DECISIONS,
    DECISION_STOP,
    FEASIBILITY_DEMONSTRATED,
    FEASIBILITY_VERDICTS,
    LADDER_STAGES,
    PROVENANCE_OBSERVED,
    STATUS_EXECUTED,
    STATUS_PENDING_LAB,
    STATUS_PENDING_PARTNER,
    STAGE_CONTRACT,
    STAGE_ONE_OBSERVED_TASK,
    STAGE_OPERATIONAL_SAMPLE,
    STAGE_SUPERVISED_BATCH,
    STOP_AUTHORITY_VIOLATION,
    STOP_BUDGET_OVERRUN,
    STOP_RULE_OUTCOME,
    BaselineMeasure,
    Customer,
    CustomerBaseline,
    DecisionReview,
    LearningContract,
    PilotLadder,
    PilotLadderError,
    Precondition,
    StageRecord,
    UsageReceipt,
    build_partner_pilot_report,
    customer_baseline_from_pilot_baseline,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PARTNER_DIR = REPO_ROOT / "evaluation" / "pilot" / "partner-pilot-v1"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _observed(value: float, source: str = "time-tracking export") -> BaselineMeasure:
    return BaselineMeasure(value_minutes=value, provenance=PROVENANCE_OBSERVED, source=source)


def _pending() -> BaselineMeasure:
    return BaselineMeasure(value_minutes=None, provenance="pending", source="")


def _baseline(all_observed: bool = True) -> CustomerBaseline:
    if not all_observed:
        return CustomerBaseline(method="pending — not yet measured")
    return CustomerBaseline(
        assisted_coding_minutes=_observed(95.0, "the customer's issue tracker + calendar"),
        reviewer_minutes=_observed(40.0, "review timestamps on the last 30 changes"),
        ops_intervention_minutes=_observed(15.0, "the ops on-call log"),
        waiting_minutes=_observed(210.0, "issue-open to merge-ready, excluding coding"),
        window_start="2026-06-01",
        window_end="2026-08-31",
        method="per-axis measurement over the customer's own tools, 60-90 day window",
    )


def _contract(
    *,
    customer: Customer | None = None,
    baseline: CustomerBaseline | None = None,
    task_set_approval: str = "pending-code-owner-approval",
    spend_cap_usd: float = 10.0,
) -> LearningContract:
    return LearningContract(
        contract_id="ladder-test",
        customer=customer or Customer(),
        task_eligibility=("one small change per task, owner-approved",),
        acceptance=("the independent oracle decides — never PR creation",),
        available_data=("the one writable repo", "read-only neighbor evidence"),
        prohibited_operations=("automatic merge", "production secrets"),
        stop_rules=(STOP_AUTHORITY_VIOLATION, STOP_BUDGET_OVERRUN, STOP_RULE_OUTCOME),
        spend_cap_usd=spend_cap_usd,
        stage_spend_caps_usd={STAGE_ONE_OBSERVED_TASK: 1.0},
        baseline=baseline if baseline is not None else CustomerBaseline(),
        task_set_approval=task_set_approval,
    )


def _partner_contract() -> LearningContract:
    return _contract(
        customer=Customer(name="Ada Chen", organization="Acme", code_owner="Ada Chen"),
        baseline=_baseline(),
        task_set_approval="Ada Chen (code owner), 2026-10-01",
    )


def _executed_record(
    stage: str,
    *,
    accepted: bool = True,
    spend: float | None = 0.25,
    task_refs: tuple[str, ...] = ("obs-01",),
    complete: bool = True,
    manual_rescues: int = 0,
    abandoned: int = 0,
    review_minutes: float = 0.0,
) -> StageRecord:
    receipts = (
        (UsageReceipt(attempt_id="obs-01/live-1", source="test", spend_usd=spend),)
        if spend is not None
        else (UsageReceipt(attempt_id="obs-01/live-1", source="test", spend_usd=None),)
    )
    return StageRecord(
        stage=stage,
        status=STATUS_EXECUTED,
        recorded_at="2026-09-24T12:00:00+00:00",
        task_refs=task_refs,
        acceptance={
            "accepted": accepted,
            "method": "independent-oracle",
            "pr_creation_recorded_but_not_acceptance": True,
        },
        attempts=({"attempt_id": "obs-01/live-1", "outcome": "ready_for_human"},),
        manual_rescues=manual_rescues,
        abandoned_tasks=abandoned,
        timings={"setup_minutes": 1.0, "wait_minutes": 2.0, "review_minutes": review_minutes},
        usage_receipts=receipts,
        stage_complete=complete,
    )


def _ladder_ready_for_stage_one() -> PilotLadder:
    ladder = PilotLadder(_contract())
    ladder.record_stage(
        StageRecord(
            stage=STAGE_CONTRACT,
            status=STATUS_EXECUTED,
            recorded_at="2026-09-24T11:00:00+00:00",
            acceptance={"accepted": True, "method": "contract-validation"},
            stage_complete=True,
        )
    )
    return ladder


def _load_runner():
    if "run_pilot_stage" in sys.modules:
        return sys.modules["run_pilot_stage"]
    spec = importlib.util.spec_from_file_location(
        "run_pilot_stage", REPO_ROOT / "scripts" / "run_pilot_stage.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_pilot_stage"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# The learning contract and its fail-closed partner gate
# ---------------------------------------------------------------------------


class TestLearningContract:
    def test_a_pending_customer_is_a_valid_but_honestly_labeled_contract(self) -> None:
        contract = _contract()
        contract.validate()
        assert contract.customer.is_pending
        assert contract.customer.as_document()["state"] == CUSTOMER_PENDING

    def test_the_shipped_contract_validates_with_customer_pending(self) -> None:
        contract = LearningContract.load(PARTNER_DIR / "contract.json")
        assert contract.customer.is_pending
        assert contract.stage_spend_caps_usd[STAGE_ONE_OBSERVED_TASK] == 1.0

    def test_stage_two_fails_closed_without_a_named_customer(self) -> None:
        contract = _contract()
        with pytest.raises(PilotLadderError, match="customer-named"):
            contract.validate_for_stage(STAGE_SUPERVISED_BATCH)

    def test_the_partner_prerequisites_are_named_not_anonymous(self) -> None:
        contract = _contract()
        unmet = [
            entry.check
            for entry in contract.stage_prerequisites(STAGE_SUPERVISED_BATCH)
            if not entry.met
        ]
        assert unmet == ["customer-named", "task-set-approved", "baseline-observed"]

    def test_a_complete_partner_contract_passes_the_partner_gate(self) -> None:
        contract = _partner_contract()
        unmet = [
            entry.check
            for entry in contract.stage_prerequisites(STAGE_SUPERVISED_BATCH)
            if not entry.met
        ]
        assert unmet == []
        contract.validate_for_stage(STAGE_SUPERVISED_BATCH)

    def test_the_observed_task_stage_needs_only_a_bounded_cap(self) -> None:
        contract = _contract()
        unmet = [
            entry.check
            for entry in contract.stage_prerequisites(STAGE_ONE_OBSERVED_TASK)
            if not entry.met
        ]
        assert unmet == []
        no_cap = replace(contract, stage_spend_caps_usd={})
        assert [
            entry.check
            for entry in no_cap.stage_prerequisites(STAGE_ONE_OBSERVED_TASK)
            if not entry.met
        ] == ["stage-spend-cap-configured"]

    def test_a_stage_cap_above_the_contract_cap_is_refused(self) -> None:
        contract = replace(_contract(), stage_spend_caps_usd={STAGE_ONE_OBSERVED_TASK: 99.0})
        with pytest.raises(PilotLadderError, match="within the contract cap"):
            contract.validate()

    def test_unknown_stop_rules_are_refused(self) -> None:
        contract = replace(_contract(), stop_rules=("vibes",))
        with pytest.raises(PilotLadderError, match="closed vocabulary"):
            contract.validate()


# ---------------------------------------------------------------------------
# The customer baseline: four typed axes, observed or pending, never authored
# ---------------------------------------------------------------------------


class TestCustomerBaseline:
    def test_every_axis_is_a_separate_typed_field(self) -> None:
        baseline = CustomerBaseline(
            assisted_coding_minutes=_observed(95.0),
            reviewer_minutes=_observed(40.0),
            ops_intervention_minutes=_observed(15.0),
            waiting_minutes=_observed(210.0),
            window_start="2026-06-01",
            window_end="2026-08-31",
            method="measured",
        )
        baseline.validate()
        assert sorted(baseline.as_document()["axes"]) == sorted(BASELINE_AXES)
        assert baseline.all_observed

    def test_an_authored_baseline_is_refused_outright(self) -> None:
        authored = BaselineMeasure(value_minutes=30.0, provenance="authored", source="a guess")
        with pytest.raises(PilotLadderError, match="AUTHORED baseline is refused"):
            authored.validate("assisted_coding_minutes")

    def test_the_shipped_contract_carries_no_authored_baseline(self) -> None:
        contract = LearningContract.load(PARTNER_DIR / "contract.json")
        assert contract.baseline.pending_axes == BASELINE_AXES
        assert not contract.baseline.all_observed

    def test_a_pending_axis_never_advances_the_ladder(self) -> None:
        mostly_observed = CustomerBaseline(
            assisted_coding_minutes=_observed(95.0),
            reviewer_minutes=_observed(40.0),
            ops_intervention_minutes=_observed(15.0),
            waiting_minutes=_pending(),
            window_start="2026-06-01",
            window_end="2026-08-31",
            method="three of four axes measured",
        )
        mostly_observed.validate()
        assert mostly_observed.pending_axes == ("waiting_minutes",)
        contract = replace(
            _partner_contract(),
            baseline=mostly_observed,
        )
        assert "baseline-observed" in [
            entry.check
            for entry in contract.stage_prerequisites(STAGE_SUPERVISED_BATCH)
            if not entry.met
        ]

    def test_an_observed_measure_needs_a_positive_value_and_a_source(self) -> None:
        with pytest.raises(PilotLadderError, match="positive value"):
            BaselineMeasure(
                value_minutes=0.0, provenance=PROVENANCE_OBSERVED, source="tracked"
            ).validate("waiting_minutes")
        with pytest.raises(PilotLadderError, match="needs its source"):
            BaselineMeasure(value_minutes=5.0, provenance=PROVENANCE_OBSERVED, source="").validate(
                "waiting_minutes"
            )

    def test_a_pending_measure_carries_no_value(self) -> None:
        with pytest.raises(PilotLadderError, match="pending is pending"):
            BaselineMeasure(value_minutes=5.0, provenance="pending", source="").validate(
                "waiting_minutes"
            )

    def test_the_kit_pilot_baseline_adapts_to_all_pending_never_observed(self) -> None:
        from forge.adaptive.pilot import PilotBaseline

        kit_baseline = PilotBaseline(
            window_start="2026-08-01", window_end="2026-09-22", cycle_time_minutes=30.0
        )
        adapted = customer_baseline_from_pilot_baseline(kit_baseline)
        adapted.validate()
        assert adapted.pending_axes == BASELINE_AXES
        assert not adapted.all_observed


# ---------------------------------------------------------------------------
# The ladder: ordering, gates, stop rules, budget
# ---------------------------------------------------------------------------


class TestLadderGating:
    def test_the_ladder_advances_in_order(self) -> None:
        ladder = PilotLadder(_contract())
        assert [entry.check for entry in ladder.gate(STAGE_ONE_OBSERVED_TASK).unmet] == [
            "stage-contract-complete"
        ]
        assert not ladder.gate(STAGE_ONE_OBSERVED_TASK).allowed

    def test_stage_two_is_blocked_without_a_non_pending_customer(self) -> None:
        ladder = _ladder_ready_for_stage_one()
        ladder.record_stage(_executed_record(STAGE_ONE_OBSERVED_TASK))
        gate = ladder.gate(STAGE_SUPERVISED_BATCH)
        assert not gate.allowed
        assert [entry.check for entry in gate.unmet] == [
            "customer-named",
            "task-set-approved",
            "baseline-observed",
        ]
        with pytest.raises(PilotLadderError, match="customer-named"):
            ladder.record_stage(_executed_record(STAGE_SUPERVISED_BATCH))

    def test_a_partner_ladder_admits_stage_two(self) -> None:
        ladder = PilotLadder(_partner_contract())
        ladder.record_stage(
            StageRecord(
                stage=STAGE_CONTRACT,
                status=STATUS_EXECUTED,
                recorded_at="2026-09-24T11:00:00+00:00",
                acceptance={"accepted": True},
                stage_complete=True,
            )
        )
        ladder.record_stage(_executed_record(STAGE_ONE_OBSERVED_TASK))
        assert ladder.gate(STAGE_SUPERVISED_BATCH).allowed

    def test_a_pending_record_never_completes_a_stage(self) -> None:
        ladder = _ladder_ready_for_stage_one()
        ladder.record_stage(
            StageRecord(
                stage=STAGE_ONE_OBSERVED_TASK,
                status=STATUS_PENDING_LAB,
                recorded_at="2026-09-24T12:00:00+00:00",
                unmet_preconditions=(
                    Precondition(check="lab-aligned", met=False, reason="0.28.0"),
                ),
            )
        )
        assert ladder.stage_status(STAGE_ONE_OBSERVED_TASK) == STATUS_PENDING_LAB
        assert ladder.current_stage == STAGE_ONE_OBSERVED_TASK

    def test_a_pending_record_without_preconditions_is_refused(self) -> None:
        ladder = _ladder_ready_for_stage_one()
        with pytest.raises(PilotLadderError, match="unmet preconditions"):
            ladder.record_stage(
                StageRecord(
                    stage=STAGE_ONE_OBSERVED_TASK,
                    status=STATUS_PENDING_LAB,
                    recorded_at="2026-09-24T12:00:00+00:00",
                )
            )


class TestStopRules:
    def test_a_tripped_rule_preserves_diagnostics_and_halts(self) -> None:
        ladder = _ladder_ready_for_stage_one()
        ladder.record_stage(
            _executed_record(STAGE_ONE_OBSERVED_TASK, accepted=False, complete=False)
        )
        stop = ladder.trip_stop(STOP_AUTHORITY_VIOLATION, "the lane attempted an automatic merge")
        assert "authority_violation" in ladder.stop_reason
        assert stop.diagnostics_digest
        assert stop.preserved_state["ladder_id"] == ladder.ladder_id
        gate = ladder.gate(STAGE_SUPERVISED_BATCH)
        assert not gate.allowed
        assert gate.stop_reason == ladder.stop_reason
        assert [entry.check for entry in gate.unmet] == ["stop-rules-clear"]

    def test_no_stage_may_start_while_a_rule_is_tripped(self) -> None:
        ladder = PilotLadder(_contract())
        ladder.trip_stop(STOP_RULE_OUTCOME, "the observed task's stop rule outcome")
        for stage in LADDER_STAGES:
            assert not ladder.gate(stage).allowed

    def test_an_executed_record_on_a_stopped_ladder_is_refused(self) -> None:
        ladder = _ladder_ready_for_stage_one()
        ladder.trip_stop(STOP_BUDGET_OVERRUN, "the cap was passed")
        with pytest.raises(PilotLadderError, match="never silently retried"):
            ladder.record_stage(_executed_record(STAGE_ONE_OBSERVED_TASK))

    def test_the_first_trip_wins_and_is_retained(self) -> None:
        ladder = PilotLadder(_contract())
        first = ladder.trip_stop(STOP_BUDGET_OVERRUN, "first")
        second = ladder.trip_stop(STOP_AUTHORITY_VIOLATION, "second")
        assert second is first
        assert "first" in ladder.stop_reason

    def test_a_trip_needs_its_reason(self) -> None:
        ladder = PilotLadder(_contract())
        with pytest.raises(PilotLadderError, match="needs its reason"):
            ladder.trip_stop(STOP_AUTHORITY_VIOLATION, "  ")


class TestBudget:
    def test_recorded_spend_folds_across_records(self) -> None:
        ladder = _ladder_ready_for_stage_one()
        ladder.record_stage(_executed_record(STAGE_ONE_OBSERVED_TASK, spend=0.25))
        assert ladder.spend_usd == 0.25
        assert ladder.budget_remaining_usd == pytest.approx(9.75)

    def test_unknown_spend_is_unknown_never_zero(self) -> None:
        ladder = _ladder_ready_for_stage_one()
        ladder.record_stage(_executed_record(STAGE_ONE_OBSERVED_TASK, spend=None))
        # Unknown spend is NOT folded into the known total (the cap check
        # stays honest: unknown spend never silently reads as zero spend).
        assert ladder.spend_usd == 0.0
        record = ladder.stage_records(STAGE_ONE_OBSERVED_TASK)[-1]
        assert record.spend_usd is None

    def test_spend_past_the_cap_trips_the_budget_stop_rule(self) -> None:
        contract = replace(
            _contract(spend_cap_usd=0.5),
            stage_spend_caps_usd={STAGE_ONE_OBSERVED_TASK: 0.5},
        )
        ladder = PilotLadder(contract)
        ladder.record_stage(
            StageRecord(
                stage=STAGE_CONTRACT,
                status=STATUS_EXECUTED,
                recorded_at="2026-09-24T11:00:00+00:00",
                acceptance={"accepted": True},
                stage_complete=True,
            )
        )
        ladder.record_stage(_executed_record(STAGE_ONE_OBSERVED_TASK, spend=0.75))
        assert "budget_overrun" in ladder.stop_reason
        assert not ladder.gate(STAGE_SUPERVISED_BATCH).allowed

    def test_a_reached_cap_blocks_the_next_paid_batch(self) -> None:
        ladder = PilotLadder(_contract(spend_cap_usd=10.0))
        ladder.record_stage(
            StageRecord(
                stage=STAGE_CONTRACT,
                status=STATUS_EXECUTED,
                recorded_at="2026-09-24T11:00:00+00:00",
                acceptance={"accepted": True},
                stage_complete=True,
            )
        )
        ladder.record_stage(_executed_record(STAGE_ONE_OBSERVED_TASK, spend=10.0))
        gate = ladder.gate(STAGE_SUPERVISED_BATCH)
        assert "budget-remaining" in [entry.check for entry in gate.unmet]


# ---------------------------------------------------------------------------
# The decision record
# ---------------------------------------------------------------------------


class TestDecisionReview:
    def test_the_template_ships_unfilled(self) -> None:
        template = DecisionReview.unfilled_template()
        template.validate()
        assert template.as_document()["filled"] is False

    def test_a_filled_review_separates_feasibility_from_adoption(self) -> None:
        review = DecisionReview(
            decision=DECISION_STOP,
            reasons=("the oracle rejected the sample",),
            evidence_pointers=("evaluation/pilot/partner-pilot-v1/stages/",),
            feasibility=pl.EngineeringFeasibility(
                verdict=FEASIBILITY_DEMONSTRATED,
                statement="the workflow completed tasks within cap",
                evidence_pointers=("stages/one-observed-task.json",),
            ),
            adoption=pl.AdoptionAssessment(
                verdict=ADOPTION_WILLING,
                statement="the customer said they would continue",
                evidence_pointers=("stages/supervised-batch.json",),
            ),
            decided_by="Ada Chen (customer) + the maintainer",
        )
        review.validate(customer_pending=False)
        document = review.as_document()
        assert set(document["engineering_feasibility"]) == {
            "verdict",
            "statement",
            "evidence_pointers",
        }
        assert document["adoption_willingness"]["verdict"] == ADOPTION_WILLING

    def test_adoption_is_forced_unknown_while_the_customer_is_pending(self) -> None:
        review = DecisionReview(
            decision=DECISION_STOP,
            reasons=("stop",),
            feasibility=pl.EngineeringFeasibility(
                verdict=FEASIBILITY_DEMONSTRATED, statement="it works"
            ),
            adoption=pl.AdoptionAssessment(verdict=ADOPTION_WILLING, statement="we love it"),
            decided_by="the maintainer",
        )
        with pytest.raises(PilotLadderError, match="nobody has been asked"):
            review.validate(customer_pending=True)

    def test_a_named_customer_cannot_stay_unknown(self) -> None:
        review = DecisionReview(
            decision=DECISION_STOP,
            reasons=("stop",),
            feasibility=pl.EngineeringFeasibility(
                verdict=FEASIBILITY_DEMONSTRATED, statement="it works"
            ),
            adoption=pl.AdoptionAssessment(
                verdict=ADOPTION_UNKNOWN_PENDING_PARTNER, statement="nobody asked"
            ),
            decided_by="the maintainer",
        )
        with pytest.raises(PilotLadderError, match="no longer honest"):
            review.validate(customer_pending=False)

    def test_the_verdict_vocabularies_are_closed(self) -> None:
        assert DECISIONS == ("continue", "narrow", "redesign", "stop")
        assert FEASIBILITY_VERDICTS == ("demonstrated", "partial", "not-demonstrated")
        with pytest.raises(PilotLadderError, match="not in"):
            DecisionReview(
                decision="expand",
                reasons=("x",),
                feasibility=pl.EngineeringFeasibility(verdict="ok", statement="s"),
                adoption=pl.AdoptionAssessment(
                    verdict=ADOPTION_UNKNOWN_PENDING_PARTNER, statement="s"
                ),
                decided_by="x",
            ).validate()


# ---------------------------------------------------------------------------
# The report + observability + replay
# ---------------------------------------------------------------------------


class TestReportAndReplay:
    def test_the_report_renders_the_honest_pending_lab_status(self) -> None:
        ladder = _ladder_ready_for_stage_one()
        ladder.record_stage(
            StageRecord(
                stage=STAGE_ONE_OBSERVED_TASK,
                status=STATUS_PENDING_LAB,
                recorded_at="2026-09-24T12:00:00+00:00",
                task_refs=("obs-01",),
                unmet_preconditions=(
                    Precondition(
                        check="lab-aligned",
                        met=False,
                        reason="compatibility verdict 'misaligned'",
                    ),
                ),
            )
        )
        report = build_partner_pilot_report(ladder)
        status = report["honest_status"]
        assert status["stage_one_status"] == "stage-1-pending-lab"
        assert "lab-aligned" in status["stage_one_detail"]
        assert "pending-recruitment" in status["external_partner_prerequisite"]
        assert report["decision_review"]["filled"] is False

    def test_the_report_states_the_external_partner_prerequisite_verbatim(self) -> None:
        report = build_partner_pilot_report(PilotLadder(_contract()))
        assert (
            report["honest_status"]["external_partner_prerequisite"]
            == pl.EXTERNAL_PARTNER_PREREQUISITE
        )
        assert report["honest_status"]["customer_state"] == CUSTOMER_PENDING

    def test_the_report_separates_feasibility_from_adoption_when_filled(self) -> None:
        ladder = PilotLadder(_contract())
        ladder.record_decision_review(DecisionReview.unfilled_template())
        report = build_partner_pilot_report(ladder)
        review = report["decision_review"]
        assert review["filled"] is False
        assert set(review) >= {
            "decision",
            "reasons",
            "evidence_pointers",
            "engineering_feasibility",
            "adoption_willingness",
            "decided_by",
        }

    def test_the_gauges_are_exactly_the_issue_s_observability(self) -> None:
        ladder = _ladder_ready_for_stage_one()
        ladder.record_stage(
            _executed_record(STAGE_ONE_OBSERVED_TASK, manual_rescues=1, abandoned=1)
        )
        gauges = ladder.observability_gauges()
        assert set(gauges) == {
            "pilot.accepted_tasks",
            "pilot.manual_rescue_count",
            "pilot.customer_review_minutes",
            "pilot.abandoned_tasks",
            "pilot.stop_reason",
            "pilot.continuation_decision",
        }
        assert gauges["pilot.accepted_tasks"] == 1
        assert gauges["pilot.manual_rescue_count"] == 1
        assert gauges["pilot.abandoned_tasks"] == 1
        # No partner exists: customer review minutes are unknown — never zero.
        assert gauges["pilot.customer_review_minutes"] is None
        assert gauges["pilot.continuation_decision"] == "pending-review"

    def test_a_rejected_task_stays_in_the_denominator(self) -> None:
        ladder = _ladder_ready_for_stage_one()
        ladder.record_stage(
            _executed_record(STAGE_ONE_OBSERVED_TASK, accepted=False, complete=False)
        )
        gauges = ladder.observability_gauges()
        assert gauges["pilot.accepted_tasks"] == 0
        assert gauges["pilot.abandoned_tasks"] == 0  # rejected is not abandoned

    def test_replay_is_byte_identical(self) -> None:
        ladder = _ladder_ready_for_stage_one()
        ladder.record_stage(
            StageRecord(
                stage=STAGE_ONE_OBSERVED_TASK,
                status=STATUS_PENDING_LAB,
                recorded_at="2026-09-24T12:00:00+00:00",
                task_refs=("obs-01",),
                unmet_preconditions=(Precondition(check="lab-aligned", met=False, reason="x"),),
            )
        )
        document = ladder.state_document()
        replayed = PilotLadder.from_document(document)
        assert json.dumps(replayed.state_document(), sort_keys=True) == json.dumps(
            document, sort_keys=True
        )

    def test_replay_preserves_a_tripped_stop_s_diagnostics(self) -> None:
        ladder = _ladder_ready_for_stage_one()
        ladder.record_stage(_executed_record(STAGE_ONE_OBSERVED_TASK, spend=0.9))
        ladder.trip_stop(STOP_BUDGET_OVERRUN, "cap passed")
        document = ladder.state_document()
        replayed = PilotLadder.from_document(document)
        assert replayed.stop_reason == ladder.stop_reason
        assert replayed.stop is not None and ladder.stop is not None
        assert replayed.stop.diagnostics_digest == ladder.stop.diagnostics_digest
        assert json.dumps(replayed.state_document(), sort_keys=True) == json.dumps(
            document, sort_keys=True
        )

    def test_replay_refuses_a_state_bound_to_a_different_contract(self) -> None:
        ladder = PilotLadder(_contract())
        document = ladder.state_document()
        document["contract_digest"] = "0" * 64
        with pytest.raises(PilotLadderError, match="different contract"):
            PilotLadder.from_document(document)


# ---------------------------------------------------------------------------
# The stage runner: the shipped artifacts and the preflight arms
# ---------------------------------------------------------------------------


class TestRunnerArtifacts:
    def test_the_shipped_task_set_is_staged_and_bounded(self) -> None:
        runner = _load_runner()
        task_set = runner.load_task_set(PARTNER_DIR / "tasks.json")
        stages: dict[str, int] = {}
        for entry in task_set["tasks"]:
            stages[str(entry["stage"])] = stages.get(str(entry["stage"]), 0) + 1
        assert stages[STAGE_ONE_OBSERVED_TASK] == 1
        assert 12 <= stages[STAGE_OPERATIONAL_SAMPLE] <= 20
        assert stages[STAGE_SUPERVISED_BATCH] >= 4

    def test_the_shipped_task_set_covers_the_issue_s_scenario_mix(self) -> None:
        runner = _load_runner()
        task_set = runner.load_task_set(PARTNER_DIR / "tasks.json")
        kinds = {str(entry["kind"]) for entry in task_set["tasks"]}
        assert {"ordinary", "ambiguity", "neighbor", "intervention", "infra-failure"} <= kinds
        # the human-rejection task: a green candidate the customer still rejects
        assert "human-rejection" in kinds


_TEMPLATE_CONTENT = "include:\n  - project: /forge/v0.36.0/ci/templates/lane.yml\n    ref: main\n"


class _FakeProbe:
    """A fake inventory probe: podman-free, offline, fully scripted."""

    def __init__(
        self,
        *,
        version: str = "0.36.0",
        image_digest: str = "sha256:" + "a" * 64,
        schema: str = "027",
        template_content: str = _TEMPLATE_CONTENT,
        caps: bool = True,
        gateway_ok: bool = True,
    ) -> None:
        self.app_health_url = "http://fake:8420/health"
        self.version = version
        self.image_digest = image_digest
        self.schema = schema
        self.template_content = template_content
        self.caps = caps
        self.gateway_ok = gateway_ok

    def http_get_json(self, url: str, headers: Any = None, params: Any = None) -> Any:
        if "gateway" in url:
            if not self.gateway_ok:
                raise RuntimeError("gateway down")
            return {"status": "ok"}
        if url.endswith("/health"):
            return {"status": "ok", "version": self.version}
        if "/repository/files/" in str(url):
            import base64

            return {
                "content": base64.b64encode(self.template_content.encode()).decode(),
                "content_sha256": "0" * 64,
                "last_commit_id": "0" * 40,
            }
        if "/runners" in str(url):
            return []
        raise RuntimeError(f"unexpected url {url}")

    def podman(self, *args: str) -> str:
        joined = " ".join(args)
        if "ImageName" in joined:
            return f"ghcr.io/forcewake/forge {self.image_digest}"
        if ".Config.Env" in joined:
            cap_entries = (
                ['FORGE_BUDGET_PROFILES={"p": {"max_calls": 50}}', "FORGE_LANE_BUDGET_SECONDS=600"]
                if self.caps
                else []
            )
            return json.dumps(["PATH=/usr/bin", *cap_entries])
        if "psql" in joined:
            return f" {self.schema} \n"
        raise RuntimeError(f"unexpected podman {joined}")


def _inventory_root(tmp_path: Path) -> Path:
    """A minimal repo-shaped root for the inventory's intended-profile read."""
    versions = tmp_path / "alembic" / "versions"
    versions.mkdir(parents=True)
    (versions / "026_x.py").write_text(
        'revision = "026"\ndown_revision = "025"\n', encoding="utf-8"
    )
    (versions / "027_y.py").write_text(
        'revision = "027"\ndown_revision = "026"\n', encoding="utf-8"
    )
    evidence = tmp_path / "docs" / "releases" / "evidence" / "v0.36.0"
    evidence.mkdir(parents=True)
    (evidence / "promotion.json").write_text(
        json.dumps(
            {
                "version": "0.36.0",
                "image_digest": "sha256:" + "a" * 64,
                "wheel_sha256": "b" * 64,
                "wheel_url": "https://example/forge.whl",
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def _fake_lane_venv(tmp_path: Path, version: str = "0.36.0") -> Path:
    site = tmp_path / "lane-venv" / "lib" / "python3.13" / "site-packages"
    site.mkdir(parents=True)
    (site / f"forge-{version}.dist-info").mkdir()
    return tmp_path / "lane-venv"


@pytest.fixture
def gitlab_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The inventory's lane stage reads GitLab settings via forge.config."""
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.example")
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "test-secret")


class TestPreflightArms:
    def test_an_aligned_lab_with_caps_and_gateway_passes_every_arm(
        self, tmp_path: Path, gitlab_settings_env: None
    ) -> None:
        runner = _load_runner()
        preconditions, _observations = runner.lab_preflight(
            _inventory_root(tmp_path),
            _contract(),
            STAGE_ONE_OBSERVED_TASK,
            max_spend_usd=1.0,
            probe=_FakeProbe(),
            gateway_url="http://gateway/health",
            lane_venv=_fake_lane_venv(tmp_path),
            remaining_budget_usd=10.0,
        )
        assert [(entry.check, entry.met) for entry in preconditions] == [
            ("lab-aligned", True),
            ("gateway-reachable", True),
            ("caps-configured", True),
            ("stage-spend-cap-bounded", True),
        ]

    def test_a_misaligned_lab_is_pending_lab_with_the_exact_reasons(
        self, tmp_path: Path, gitlab_settings_env: None
    ) -> None:
        runner = _load_runner()
        preconditions, _observations = runner.lab_preflight(
            _inventory_root(tmp_path),
            _contract(),
            STAGE_ONE_OBSERVED_TASK,
            max_spend_usd=1.0,
            probe=_FakeProbe(version="0.28.0"),
            gateway_url="http://gateway/health",
            lane_venv=_fake_lane_venv(tmp_path),
        )
        aligned = next(entry for entry in preconditions if entry.check == "lab-aligned")
        assert not aligned.met
        assert "misaligned" in aligned.reason
        assert "runbook" in aligned.resolution.lower()

    def test_absent_caps_are_their_own_arm(self, tmp_path: Path, gitlab_settings_env: None) -> None:
        runner = _load_runner()
        preconditions, _observations = runner.lab_preflight(
            _inventory_root(tmp_path),
            _contract(),
            STAGE_ONE_OBSERVED_TASK,
            max_spend_usd=1.0,
            probe=_FakeProbe(caps=False),
            gateway_url="http://gateway/health",
            lane_venv=_fake_lane_venv(tmp_path),
        )
        caps = next(entry for entry in preconditions if entry.check == "caps-configured")
        assert not caps.met
        assert "FORGE_BUDGET_PROFILES" in caps.reason

    def test_a_down_gateway_is_pending_lab_even_when_aligned(
        self, tmp_path: Path, gitlab_settings_env: None
    ) -> None:
        runner = _load_runner()
        preconditions, _observations = runner.lab_preflight(
            _inventory_root(tmp_path),
            _contract(),
            STAGE_ONE_OBSERVED_TASK,
            max_spend_usd=1.0,
            probe=_FakeProbe(gateway_ok=False),
            gateway_url="http://gateway/health",
            lane_venv=_fake_lane_venv(tmp_path),
        )
        gateway = next(entry for entry in preconditions if entry.check == "gateway-reachable")
        assert not gateway.met
        assert "unreachable" in gateway.reason

    def test_a_bound_above_the_stage_cap_is_refused(
        self, tmp_path: Path, gitlab_settings_env: None
    ) -> None:
        runner = _load_runner()
        preconditions, _observations = runner.lab_preflight(
            _inventory_root(tmp_path),
            _contract(),
            STAGE_ONE_OBSERVED_TASK,
            max_spend_usd=5.0,
            probe=_FakeProbe(),
            gateway_url="http://gateway/health",
            lane_venv=_fake_lane_venv(tmp_path),
        )
        bound = next(entry for entry in preconditions if entry.check == "stage-spend-cap-bounded")
        assert not bound.met

    def test_the_partner_stage_records_pending_partner_through_the_cli(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runner = _load_runner()
        out = tmp_path / "ladder"
        out.mkdir()
        args = [
            "--stage",
            STAGE_SUPERVISED_BATCH,
            "--out",
            str(out),
            "--contract",
            str(PARTNER_DIR / "contract.json"),
            "--tasks",
            str(PARTNER_DIR / "tasks.json"),
        ]
        assert runner.main(args) == 0
        captured = capsys.readouterr().out
        assert "PENDING-PARTNER" in captured
        assert "customer-named" in captured
        state = json.loads((out / "ladder-state.json").read_text(encoding="utf-8"))
        record = state["stages"][STAGE_SUPERVISED_BATCH]["records"][-1]
        assert record["status"] == STATUS_PENDING_PARTNER
        assert [entry["check"] for entry in record["unmet_preconditions"]] == [
            "stage-contract-complete",
            "stage-one-observed-task-complete",
            "customer-named",
            "task-set-approved",
            "baseline-observed",
        ]

    def test_the_contract_then_observed_stage_flow_through_the_cli(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runner = _load_runner()
        out = tmp_path / "ladder"
        out.mkdir()
        base = [
            "--out",
            str(out),
            "--contract",
            str(PARTNER_DIR / "contract.json"),
            "--tasks",
            str(PARTNER_DIR / "tasks.json"),
        ]
        assert runner.main(["--stage", STAGE_CONTRACT, *base]) == 0
        # The observed stage runs its REAL read-only preflight against the lab.
        # The gateway URL points at a guaranteed-dead port so the runner can
        # never EXECUTE a paid task from a test: the preflight fails closed
        # (gateway arm) and the record is honestly pending-lab — the read-only
        # inventory probes still exercise the real arms.
        dead_gateway = ["--gateway-url", "http://127.0.0.1:9/health"]
        code = runner.main(["--stage", STAGE_ONE_OBSERVED_TASK, *dead_gateway, *base])
        captured = capsys.readouterr().out
        # Either outcome is honest; the record must exist and name its state.
        assert code == 0
        assert "one-observed-task" in captured
        state = json.loads((out / "ladder-state.json").read_text(encoding="utf-8"))
        records = state["stages"][STAGE_ONE_OBSERVED_TASK]["records"]
        assert records, "the observed stage must leave its record"
        assert records[-1]["status"] in {
            STATUS_PENDING_LAB,
            STATUS_EXECUTED,
            STATUS_PENDING_PARTNER,
        }
        if records[-1]["status"] == STATUS_PENDING_LAB:
            checks = [entry["check"] for entry in records[-1]["unmet_preconditions"]]
            assert any(
                check in {"lab-aligned", "gateway-reachable", "caps-configured"} for check in checks
            )
