"""DSC-08: bounded impact slices, retrieval coverage, specialist planning.

Discovery follows the CHANGE IMPACT, not repository size: a depth-bounded
walk finds direct producers/consumers (required) and deeper context
(optional), stores what the cap excluded instead of dropping it, records
whether retrieval actually gathered the slice, and splits separable
investigations into specialists while one root planner stays in charge.
"""

from __future__ import annotations

import pytest

from forge.adaptive.impact import impact_slice, plan_specialists, retrieval_coverage
from forge.adaptive.system_manifest import DependencyEdge, ServiceEntry, SystemManifest


def _manifest(*edges: tuple[str, str, str]) -> SystemManifest:
    service_ids = {endpoint for edge in edges for endpoint in edge[:2]}
    services = [
        ServiceEntry.model_validate({"service_id": sid, "repositories": [f"core/{sid}-api"]})
        for sid in sorted(service_ids)
    ]
    edge_models = [
        DependencyEdge.model_validate(
            {"source": source, "target": target, "kind": kind, "provenance": "declared"}
        )
        for source, target, kind in edges
    ]
    return SystemManifest.model_validate(
        {"manifest_id": "test", "services": services, "edges": edge_models}
    )


class TestImpactSlice:
    def test_chain_orders_billing_notifications(self):
        manifest = _manifest(
            ("orders", "billing", "api"),
            ("billing", "notifications", "event"),
        )
        slice_ = impact_slice(manifest, ["orders"])
        assert slice_["schema"] == "forge.impact.slice/1"
        assert slice_["required"] == ["billing"]  # direct consumer at depth 1
        assert slice_["optional"] == ["notifications"]  # depth 2 exploration
        assert slice_["depth_capped"] is False
        assert slice_["omitted"] == []

    def test_changed_services_are_seeds_not_impact(self):
        manifest = _manifest(("orders", "billing", "api"))
        slice_ = impact_slice(manifest, ["orders"])
        assert "orders" not in slice_["required"]
        assert "orders" not in slice_["optional"]

    def test_event_consumer_is_found_from_the_reverse_direction(self):
        # notifications consumes orders' event: the edge points AT the
        # changed service, and the walk must still find the consumer —
        # impact flows both ways along an edge.
        manifest = _manifest(("notifications", "orders", "event"))
        slice_ = impact_slice(manifest, ["orders"])
        assert slice_["required"] == ["notifications"]

    def test_provider_is_context(self):
        # orders depends on billing's API: changing billing pulls orders in
        # as context (a provider I depend on).
        manifest = _manifest(("orders", "billing", "api"))
        slice_ = impact_slice(manifest, ["billing"])
        assert slice_["required"] == ["orders"]

    def test_depth_cap_sets_depth_capped_and_fills_omitted(self):
        manifest = _manifest(
            ("a", "b", "api"),
            ("b", "c", "event"),
            ("c", "d", "schema"),
            ("d", "e", "shared_lib"),
        )
        slice_ = impact_slice(manifest, ["a"], max_depth=2)
        assert slice_["required"] == ["b"]
        assert slice_["optional"] == ["c"]
        assert slice_["depth_capped"] is True
        assert slice_["omitted"] == ["d"]  # the excluded ring, stored not dropped

    def test_reaching_max_depth_without_a_frontier_is_not_capped(self):
        manifest = _manifest(
            ("a", "b", "api"),
            ("b", "c", "event"),
            ("c", "d", "schema"),
            ("d", "e", "shared_lib"),
        )
        slice_ = impact_slice(manifest, ["a"], max_depth=4)
        assert slice_["optional"] == ["c", "d", "e"]
        assert slice_["depth_capped"] is False
        assert slice_["omitted"] == []

    def test_disconnected_services_stay_out_of_the_slice(self):
        manifest = _manifest(("orders", "billing", "api"), ("auth", "audit", "event"))
        slice_ = impact_slice(manifest, ["orders"])
        assert "auth" not in slice_["required"] + slice_["optional"]
        assert "audit" not in slice_["required"] + slice_["optional"]

    def test_unknown_changed_service_is_refused(self):
        manifest = _manifest(("orders", "billing", "api"))
        with pytest.raises(ValueError, match="not registered"):
            impact_slice(manifest, ["typo-service"])


class TestRetrievalCoverage:
    def _slice(self) -> dict:
        return {"required": ["billing"], "optional": ["notifications"]}

    def test_missing_required_is_computed(self):
        coverage = retrieval_coverage(self._slice(), gathered=set())
        assert coverage["covered"] == []
        assert coverage["missing_required"] == ["billing"]
        assert coverage["coverage_ratio"] == 0.0

    def test_partial_gathering_records_ratio_and_missing(self):
        coverage = retrieval_coverage(self._slice(), gathered={"billing"})
        assert coverage["covered"] == ["billing"]
        assert coverage["missing_required"] == []
        assert coverage["coverage_ratio"] == pytest.approx(0.5)

    def test_full_gathering_covers_the_universe(self):
        coverage = retrieval_coverage(self._slice(), gathered={"billing", "notifications"})
        assert coverage["missing_required"] == []
        assert coverage["coverage_ratio"] == 1.0

    def test_gathered_material_outside_the_slice_is_not_coverage(self):
        coverage = retrieval_coverage(self._slice(), gathered={"billing", "unrelated-repo"})
        assert coverage["covered"] == ["billing"]

    def test_empty_universe_is_fully_covered(self):
        coverage = retrieval_coverage({"required": [], "optional": []}, gathered=set())
        assert coverage["coverage_ratio"] == 1.0


class TestPlanSpecialists:
    QUESTIONS = [
        "[billing] where are invoices calculated?",
        "[billing] which events does billing emit?",
        "[repo:orders] where is order.created published?",
        "what tests cover the orders api?",
    ]

    def test_split_by_tag_with_untagged_questions_standalone(self):
        # Three groups: the billing tag keeps both of its questions, the
        # repo:orders tag gets one, and the untagged question stands alone.
        plan = plan_specialists(self.QUESTIONS, max_specialists=4)
        assert len(plan) == 3
        assert plan[0]["scope"] == "billing"
        assert plan[0]["questions"] == self.QUESTIONS[0:2]
        assert plan[1]["scope"] == "repo:orders"
        assert plan[2]["scope"] == "adhoc"  # untagged: its own specialist
        for specialist in plan:
            assert set(specialist) == {"specialist_id", "scope", "questions"}

    def test_cap_merges_the_tail_into_root(self):
        plan = plan_specialists(self.QUESTIONS, max_specialists=2)
        assert len(plan) == 2
        assert plan[0]["scope"] == "billing"
        root = plan[1]
        assert root["specialist_id"] == "root"
        assert root["scope"] == "root"
        assert root["questions"] == self.QUESTIONS[2:]

    def test_cap_of_one_is_the_root_planner_alone(self):
        plan = plan_specialists(self.QUESTIONS, max_specialists=1)
        assert len(plan) == 1
        assert plan[0]["specialist_id"] == "root"
        assert plan[0]["questions"] == self.QUESTIONS

    def test_under_the_cap_nothing_merges(self):
        plan = plan_specialists(["[billing] q1", "[billing] q2"], max_specialists=4)
        assert len(plan) == 1
        assert plan[0]["scope"] == "billing"
        assert len(plan[0]["questions"]) == 2
