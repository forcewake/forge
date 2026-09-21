"""OPS: plan quality before model selection, honest lineage and operator views.

These tests pin the operator-facing projections: plan quality measured
BEFORE any model comparison (and the fast-path honesty marker), usage
lineage that itemizes every stage end-to-end instead of conflating
spend, budget headroom that stays signed, ONE status projection whose
summary names the ONE next action, retention that routes unknown policy
to review instead of deleting, and the capacity/error-budget controls.
"""

from __future__ import annotations

import pytest

from forge.adaptive.ops import (
    PLAN_QUALITY_SCHEMA,
    STAGES,
    admission_check,
    budget_report,
    error_budget,
    plan_quality,
    retention_decision,
    status_projection,
    usage_lineage,
)


def event(stage: str, input_tokens: int = 10, output_tokens: int = 5, **overrides):
    base = {
        "stage": stage,
        "call_id": f"call-{stage}",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": 0,
    }
    return {**base, **overrides}


class TestPlanQuality:
    def test_the_record_carries_the_versioned_schema_tag(self):
        record = plan_quality({"steps": []}, None)
        assert record["schema"] == PLAN_QUALITY_SCHEMA == "forge.plan.quality/1"

    def test_coverage_is_the_fraction_of_steps_with_evidence(self):
        plan = {
            "steps": [
                {"evidence_refs": ["blob-a"]},
                {"evidence_refs": []},
                {"evidence_refs": ["blob-b", "blob-c"]},
                {},
            ]
        }
        assert plan_quality(plan, None)["citation_coverage"] == 0.5

    def test_both_citation_spellings_count_as_evidence(self):
        plan = {
            "steps": [
                {"citations": ["blob-a"]},
                {"evidence_refs": ["blob-b"]},
            ]
        }
        assert plan_quality(plan, None)["citation_coverage"] == 1.0

    def test_a_plan_with_no_steps_is_fully_covered(self):
        assert plan_quality({"steps": []}, None)["citation_coverage"] == 1.0
        assert plan_quality({}, None)["citation_coverage"] == 1.0

    def test_open_questions_and_assumptions_are_counted(self):
        plan = {
            "steps": [],
            "questions": [{"id": "q1"}, {"id": "q2"}, {"id": "q3"}],
            "assumptions": ["the api is stable"],
        }
        record = plan_quality(plan, None)
        assert record["open_questions"] == 3
        assert record["assumptions"] == 1

    def test_missing_question_or_assumption_keys_count_zero(self):
        record = plan_quality({"steps": []}, None)
        assert record["open_questions"] == 0
        assert record["assumptions"] == 0

    def test_no_discovery_means_fast_path_and_not_researched(self):
        record = plan_quality({"steps": [{"evidence_refs": ["a"]}]}, None)
        assert record["repository_researched"] is False
        assert record["fast_path"] is True

    def test_an_empty_discovery_handshake_is_still_the_fast_path(self):
        record = plan_quality({"steps": []}, {"evidence": []})
        assert record["repository_researched"] is False
        assert record["fast_path"] is True

    @pytest.mark.parametrize("key", ["evidence", "findings", "repositories"])
    def test_discovery_with_evidence_marks_research(self, key):
        record = plan_quality({"steps": []}, {key: ["something real"]})
        assert record["repository_researched"] is True
        assert record["fast_path"] is False

    def test_researched_and_fast_path_plans_differ_on_the_marker(self):
        # the same plan shape, one researched and one not: quality is
        # identical, the honesty marker is what changes (OPS-01)
        plan = {"steps": [{"evidence_refs": ["blob-a"]}], "questions": []}
        fast = plan_quality(plan, None)
        researched = plan_quality(plan, {"evidence": ["discovery-1"]})
        assert fast["citation_coverage"] == researched["citation_coverage"]
        assert fast["repository_researched"] is False
        assert researched["repository_researched"] is True
        assert fast["fast_path"] != researched["fast_path"]


class TestUsageLineage:
    def test_every_stage_is_itemized_even_with_no_events(self):
        lineage = usage_lineage([])
        assert set(lineage) == {*STAGES, "total"}
        for stage in (*STAGES, "total"):
            assert lineage[stage] == {"calls": 0, "input": 0, "output": 0, "cached": 0}

    def test_per_stage_sums_are_never_conflated(self):
        lineage = usage_lineage(
            [
                event("discovery", input_tokens=100, output_tokens=50, cached_tokens=20),
                event("discovery", input_tokens=11, output_tokens=4),
                event("planning", input_tokens=200, output_tokens=80),
                event("review", input_tokens=1, output_tokens=1),
            ]
        )
        assert lineage["discovery"] == {"calls": 2, "input": 111, "output": 54, "cached": 20}
        assert lineage["planning"] == {"calls": 1, "input": 200, "output": 80, "cached": 0}
        assert lineage["implementation"] == {"calls": 0, "input": 0, "output": 0, "cached": 0}
        assert lineage["review"] == {"calls": 1, "input": 1, "output": 1, "cached": 0}

    def test_the_total_is_the_sum_of_the_stage_rows(self):
        events = [
            event("discovery", input_tokens=100, output_tokens=50, cached_tokens=20),
            event("implementation", input_tokens=300, output_tokens=150, cached_tokens=7),
            event("verification", input_tokens=10, output_tokens=2),
        ]
        lineage = usage_lineage(events)
        assert lineage["total"] == {"calls": 3, "input": 410, "output": 202, "cached": 27}

    def test_cached_tokens_defaults_to_zero(self):
        raw = {
            "stage": "planning",
            "call_id": "c1",
            "input_tokens": 5,
            "output_tokens": 2,
        }
        assert usage_lineage([raw])["planning"]["cached"] == 0

    def test_an_unknown_stage_fails_visibly(self):
        with pytest.raises(ValueError, match="unknown usage stage"):
            usage_lineage([event("deployment")])

    def test_discovery_spend_never_leaks_into_another_stage(self):
        lineage = usage_lineage([event("discovery", input_tokens=999)])
        for stage in STAGES[1:]:
            assert lineage[stage]["input"] == 0
        assert lineage["discovery"]["input"] == 999


class TestBudgetReport:
    def test_under_budget_with_signed_headroom(self):
        lineage = usage_lineage([event("discovery"), event("planning")])
        report = budget_report(lineage, {"max_calls": 5})
        assert report["total_calls"] == 2
        assert report["budget_calls"] == 5
        assert report["within_budget"] is True
        assert report["headroom"] == 3

    def test_spending_exactly_the_budget_is_within_it(self):
        lineage = usage_lineage([event("discovery"), event("planning")])
        report = budget_report(lineage, {"max_calls": 2})
        assert report["within_budget"] is True
        assert report["headroom"] == 0

    def test_over_budget_reports_negative_headroom_not_a_clamp(self):
        lineage = usage_lineage([event("discovery"), event("planning")])
        report = budget_report(lineage, {"max_calls": 1})
        assert report["within_budget"] is False
        assert report["headroom"] == -1

    def test_per_stage_carries_every_stage_but_not_the_total_row(self):
        lineage = usage_lineage([event("discovery"), event("review")])
        report = budget_report(lineage, {"max_calls": 10})
        assert set(report["per_stage"]) == set(STAGES)
        assert "total" not in report["per_stage"]
        assert report["per_stage"]["review"]["calls"] == 1

    def test_total_calls_is_derived_from_the_stage_rows(self):
        # a hand-built lineage without a stored total still reports honestly
        report = budget_report(
            {"discovery": {"calls": 2, "input": 0, "output": 0, "cached": 0}},
            {"max_calls": 2},
        )
        assert report["total_calls"] == 2
        assert report["within_budget"] is True


class TestStatusProjection:
    def test_unresolved_questions_make_the_run_wait_on_question(self):
        projection = status_projection(
            {"status": "planning"},
            [{"id": "q1", "resolved": False}],
            None,
        )
        assert projection["state"] == "planning"
        assert projection["waiting_on"] == "question"

    def test_resolved_questions_do_not_block(self):
        projection = status_projection(
            {"status": "planning"},
            [{"id": "q1", "resolved": True}],
            None,
        )
        assert projection["waiting_on"] is None

    def test_a_question_without_a_resolution_marker_counts_unresolved(self):
        projection = status_projection({"status": "planning"}, [{"id": "q1"}], None)
        assert projection["waiting_on"] == "question"

    def test_a_partially_published_saga_makes_the_run_wait_on_saga(self):
        projection = status_projection(
            {"status": "publishing"}, [], {"state": "partially_published"}
        )
        assert projection["waiting_on"] == "saga"

    def test_a_completed_saga_does_not_block(self):
        projection = status_projection({"status": "publishing"}, [], {"state": "published"})
        assert projection["waiting_on"] is None

    def test_a_question_outranks_a_partially_published_saga(self):
        projection = status_projection(
            {"status": "publishing"},
            [{"id": "q1", "resolved": False}],
            {"state": "partially_published"},
        )
        assert projection["waiting_on"] == "question"
        assert "question" in projection["summary"]

    def test_the_summary_names_answering_the_open_question(self):
        projection = status_projection(
            {"status": "planning"},
            [{"id": "q1", "resolved": False}],
            None,
        )
        assert projection["summary"] == "Answer 1 open question to unblock the run."

    def test_the_summary_pluralizes_multiple_questions(self):
        projection = status_projection(
            {"status": "planning"},
            [{"id": "q1", "resolved": False}, {"id": "q2", "resolved": False}],
            None,
        )
        assert projection["summary"] == "Answer 2 open questions to unblock the run."

    def test_the_summary_names_finishing_the_publication(self):
        projection = status_projection(
            {"status": "publishing"}, [], {"state": "partially_published"}
        )
        assert projection["waiting_on"] == "saga"
        assert "publish" in projection["summary"]

    def test_a_blocked_reason_surfaces_in_the_projection_and_summary(self):
        projection = status_projection(
            {"status": "blocked", "blocked_reason": "credential expired"}, [], None
        )
        assert projection["blocked_reason"] == "credential expired"
        assert projection["waiting_on"] is None
        assert "credential expired" in projection["summary"]

    def test_blocked_reason_defaults_to_empty_when_absent(self):
        projection = status_projection({"status": "running"}, [], None)
        assert projection["blocked_reason"] == ""

    def test_a_steady_run_needs_no_operator_action(self):
        projection = status_projection({"status": "running"}, [], None)
        assert projection["waiting_on"] is None
        assert (
            projection["summary"] == "Run is running; let it proceed — no operator action pending."
        )


class TestRetentionDecision:
    @pytest.mark.parametrize("residency", ["eu", "us", "any"])
    def test_under_min_days_keeps_fresh_evidence(self, residency):
        policy = {"min_days": 7, "max_days": 90, "residency": residency}
        assert retention_decision(6, policy) == "keep"

    @pytest.mark.parametrize("residency", ["eu", "us", "any"])
    def test_over_max_days_deletes(self, residency):
        policy = {"min_days": 7, "max_days": 90, "residency": residency}
        assert retention_decision(91, policy) == "delete"

    def test_between_min_and_max_goes_to_review(self):
        policy = {"min_days": 7, "max_days": 90, "residency": "eu"}
        assert retention_decision(45, policy) == "review"

    def test_the_boundaries_themselves_go_to_review(self):
        policy = {"min_days": 7, "max_days": 90, "residency": "eu"}
        assert retention_decision(7, policy) == "review"
        assert retention_decision(90, policy) == "review"

    def test_an_unknown_residency_routes_to_review_even_past_max(self):
        # never silently delete under a policy you do not understand
        policy = {"min_days": 7, "max_days": 90, "residency": "apac"}
        assert retention_decision(365, policy) == "review"

    def test_a_missing_residency_is_unknown_policy_not_a_default(self):
        policy = {"min_days": 7, "max_days": 90}
        assert retention_decision(365, policy) == "review"


class TestAdmissionCheck:
    def test_under_both_caps_is_admitted(self):
        assert admission_check(3, 10) == (True, "admitted")

    def test_at_the_active_cap_is_full_not_room_for_one_more(self):
        assert admission_check(4, 0) == (False, "capacity: active")

    def test_at_the_queue_cap_is_refused(self):
        assert admission_check(0, 32) == (False, "capacity: queue")

    def test_active_is_the_binding_constraint_when_both_are_over(self):
        assert admission_check(5, 40) == (False, "capacity: active")

    def test_caps_are_configurable(self):
        assert admission_check(2, 3, max_active=2, max_queue=3) == (False, "capacity: active")
        assert admission_check(1, 3, max_active=2, max_queue=3) == (False, "capacity: queue")
        assert admission_check(1, 2, max_active=2, max_queue=3) == (True, "admitted")


class TestErrorBudget:
    def test_meeting_the_slo_leaves_positive_budget(self):
        report = error_budget(failures=1, window_requests=100, slo=0.99)
        assert report["slo"] == 0.99
        assert report["observed_success_rate"] == pytest.approx(0.99)
        assert report["budget_remaining"] == pytest.approx(0.0)
        assert report["exhausted"] is False

    def test_below_the_slo_exhausts_the_budget(self):
        report = error_budget(failures=5, window_requests=100, slo=0.99)
        assert report["observed_success_rate"] == pytest.approx(0.95)
        assert report["budget_remaining"] == pytest.approx(-0.04)
        assert report["exhausted"] is True

    def test_headroom_above_the_slo_is_signed_positive(self):
        report = error_budget(failures=0, window_requests=100, slo=0.99)
        assert report["observed_success_rate"] == 1.0
        assert report["budget_remaining"] == pytest.approx(0.01)
        assert report["exhausted"] is False

    def test_an_empty_window_observes_a_perfect_rate(self):
        report = error_budget(failures=0, window_requests=0, slo=0.99)
        assert report["observed_success_rate"] == 1.0
        assert report["exhausted"] is False

    def test_a_clean_window_never_exhausts_any_slo(self):
        report = error_budget(failures=0, window_requests=50, slo=1.0)
        assert report["budget_remaining"] == pytest.approx(0.0)
        assert report["exhausted"] is False
