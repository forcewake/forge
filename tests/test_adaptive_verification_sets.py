"""VER epic core: verification execution separate from the coding agent.

These tests pin the separation: verification selects its own trusted
lane (never the agent's privileged environment), focuses on the
impacted changed set over baselines, flags contracts that need
consumer/provider verification, upgrades from a data-bearing baseline,
binds integration environments to the exact CandidateSet — every member
pinned (baselines included), every launched service resolved to a
recorded exact artifact or flagged unresolved, the whole bound to the
tested-world digest — selects checks by IDENTITY, decays stale
evidence, and reviews claims only through verified evidence —
implementation claims are never their own proof.
"""

from __future__ import annotations

import dataclasses

import pytest

from forge.adaptive.models import CandidateSet, CandidateSetMember
from forge.adaptive.verification_sets import (
    RECIPE_SCHEMA,
    VerificationLane,
    VerificationSelector,
    async_failure_scenarios,
    contract_checks,
    db_upgrade_plan,
    environment_compose,
    evidence_aware_review,
    focused_recipe,
    freshness,
    select_lane,
    selector_matches,
)

# aliased: the real name starts with "test" and pytest would collect the
# imported FUNCTION as a test of its own.
from forge.adaptive.workpackage import tested_world_digest as world_digest

WORK_CONTRACT_DIGEST = "2e703bcbb99b1d534aedc8e0b22956d9107190a6f1fe635e7438b5a80da87c73"


def _member(
    repository_id: str, role: str, *, candidate_oid: str | None = None
) -> CandidateSetMember:
    suffix = "3" if role == "changed" else "2"
    return CandidateSetMember(
        repository_id=repository_id,
        base_oid="1" * 40,
        candidate_oid=candidate_oid or suffix * 40,
        image_digest=f"sha256:{'e' * 64}",
        role=role,
    )


def _candidate_set() -> CandidateSet:
    # orders changed, billing changed, catalog baseline — the shape a
    # focused recipe has to split.
    return CandidateSet(
        work_id="wp-demo-1",
        plan_revision=1,
        work_contract_digest=WORK_CONTRACT_DIGEST,
        members=[
            _member("orders", "changed", candidate_oid="a" * 40),
            _member("billing", "changed", candidate_oid="b" * 40),
            _member("catalog", "baseline"),
        ],
        environment_profile_digest="c" * 64,
    )


class TestVerificationLane:
    def test_defaults_to_the_trusted_integration_profile(self):
        lane = VerificationLane(lane_id="lane-1")
        assert lane.profile == "trusted-integration-v1"
        assert lane.runs_code is True

    def test_the_lane_is_frozen(self):
        lane = VerificationLane(lane_id="lane-1")
        with pytest.raises(dataclasses.FrozenInstanceError):
            lane.runs_code = False


class TestSelectLane:
    def test_db_probe_selects_the_integration_profile(self):
        lane = select_lane(["check-1"], has_db=True, has_broker=False)
        assert lane.profile == "trusted-integration-v1"
        assert lane.runs_code is True

    def test_broker_probe_selects_the_integration_profile(self):
        lane = select_lane(["check-1"], has_db=False, has_broker=True)
        assert lane.profile == "trusted-integration-v1"
        assert lane.runs_code is True

    def test_pure_contract_checks_select_the_contract_only_lane(self):
        lane = select_lane(["check-1"], has_db=False, has_broker=False)
        assert lane.profile == "contract-only-v1"
        assert lane.runs_code is False

    def test_the_lane_identity_is_stable_for_the_same_checks(self):
        first = select_lane(["a", "b"], has_db=True, has_broker=False)
        second = select_lane(["b", "a"], has_db=True, has_broker=False)  # order-independent
        assert first.lane_id == second.lane_id

    def test_different_checks_get_different_lanes(self):
        assert (
            select_lane(["a"], has_db=False, has_broker=False).lane_id
            != select_lane(["b"], has_db=False, has_broker=False).lane_id
        )


class TestFocusedRecipe:
    def test_focused_runs_only_the_impacted_changed_set(self):
        recipe = focused_recipe(_candidate_set(), impacted=["orders", "not-in-set"])
        assert recipe["schema"] == RECIPE_SCHEMA
        assert recipe["changed"] == ["orders", "billing"]
        assert recipe["baselines"] == ["catalog"]
        assert recipe["focused"] == ["orders"]  # not-in-set is not a changed member
        assert recipe["skipped_unrelated"] == ["billing"]

    def test_no_impact_names_skips_every_changed_repo(self):
        recipe = focused_recipe(_candidate_set(), impacted=[])
        assert recipe["focused"] == []
        assert recipe["skipped_unrelated"] == ["billing", "orders"]

    def test_full_impact_leaves_nothing_skipped(self):
        recipe = focused_recipe(_candidate_set(), impacted=["billing", "orders"])
        assert recipe["focused"] == ["billing", "orders"]
        assert recipe["skipped_unrelated"] == []

    def test_focused_is_sorted_even_for_unsorted_impact(self):
        recipe = focused_recipe(_candidate_set(), impacted=["billing", "orders"])
        assert recipe["focused"] == ["billing", "orders"]


class TestContractChecks:
    def test_http_entry_with_expected_status_is_machine_checkable(self):
        checks = contract_checks(
            {"http": [{"method": "GET", "path": "/health", "request": {}, "expected_status": 200}]}
        )
        (check,) = checks
        assert check["kind"] == "http"
        assert check["method"] == "GET"
        assert check["path"] == "/health"
        assert check["expected_status"] == 200
        assert "must_verify" not in check

    def test_http_entry_without_expected_status_must_be_verified(self):
        checks = contract_checks({"http": [{"method": "GET", "path": "/report", "request": {}}]})
        (check,) = checks
        assert check["must_verify"] is True

    def test_message_contracts_carry_only_shape_and_must_be_verified(self):
        checks = contract_checks(
            {"messages": [{"topic": "orders.created", "payload_schema": "{}"}]}
        )
        (check,) = checks
        assert check["kind"] == "message"
        assert check["topic"] == "orders.created"
        assert check["payload_schema"] == "{}"
        assert check["must_verify"] is True

    def test_ids_are_deterministic_and_distinct(self):
        spec = {
            "http": [{"method": "GET", "path": "/a", "request": {}, "expected_status": 200}],
            "messages": [{"topic": "t", "payload_schema": "{}"}],
        }
        first = contract_checks(spec)
        second = contract_checks(spec)
        assert [c["id"] for c in first] == [c["id"] for c in second]
        assert len({c["id"] for c in first}) == len(first)

    def test_an_empty_spec_yields_no_checks(self):
        assert contract_checks({}) == []


class TestDbUpgradePlan:
    def test_synthetic_data_upgrade_verifies_the_surviving_data(self):
        plan = db_upgrade_plan("v1", "v2", synthetic_data=True)
        assert plan["steps"] == ["snapshot_baseline", "apply_migrations", "verify_synthetic_data"]
        assert plan["baseline"] == "v1"
        assert plan["target"] == "v2"

    def test_without_synthetic_data_the_upgrade_states_it_verifies_empty(self):
        plan = db_upgrade_plan("v1", "v2", synthetic_data=False)
        assert plan["steps"] == ["snapshot_baseline", "apply_migrations", "verify_empty"]

    def test_the_plan_always_starts_from_the_data_bearing_baseline(self):
        for synthetic in (True, False):
            plan = db_upgrade_plan("v1", "v2", synthetic_data=synthetic)
            assert plan["steps"][0] == "snapshot_baseline"


class TestAsyncFailureScenarios:
    def test_the_catalog_is_the_fixed_three(self):
        assert async_failure_scenarios() == [
            {"name": "crash_between_commit_and_ack"},
            {"name": "redelivery_duplicate"},
            {"name": "out_of_order_events"},
        ]


class TestEnvironmentCompose:
    def test_changed_members_pin_their_oid_and_exact_image(self):
        compose = environment_compose(_candidate_set(), services=[])
        assert compose["members"]["orders"] == {
            "candidate_oid": "a" * 40,
            "image_digest": f"sha256:{'e' * 64}",
            "role": "changed",
        }

    def test_baseline_members_are_pinned_too(self):
        # NXT-25: a drifted baseline is a different system under test —
        # the compose must pin its identity like anyone else's.
        compose = environment_compose(_candidate_set(), services=[])
        assert compose["members"]["catalog"] == {
            "candidate_oid": "2" * 40,
            "image_digest": f"sha256:{'e' * 64}",
            "role": "baseline",
        }

    def test_all_members_are_pinned_sorted_for_deterministic_output(self):
        compose = environment_compose(_candidate_set(), services=[])
        assert list(compose["members"]) == ["billing", "catalog", "orders"]

    def test_the_profile_digest_stays_bound(self):
        compose = environment_compose(_candidate_set(), services=[])
        assert compose["environment_profile_digest"] == "c" * 64

    def test_a_member_service_resolves_to_its_recorded_artifact(self):
        compose = environment_compose(_candidate_set(), services=["orders"])
        assert compose["services"] == [
            {"service": "orders", "artifact_digest": f"sha256:{'e' * 64}", "source": "member"}
        ]

    def test_an_external_service_resolves_only_through_an_exact_pin(self):
        pin = f"sha256:{'d' * 64}"
        compose = environment_compose(
            _candidate_set(), services=["postgres"], service_pins={"postgres": pin}
        )
        assert compose["services"] == [
            {"service": "postgres", "artifact_digest": pin, "source": "pin"}
        ]

    def test_an_unpinned_service_is_flagged_unresolved_never_a_tag(self):
        compose = environment_compose(_candidate_set(), services=["postgres", "kafka"])
        assert compose["services"] == [
            {"service": "postgres", "artifact_digest": None, "unresolved": True},
            {"service": "kafka", "artifact_digest": None, "unresolved": True},
        ]
        # a mutable tag like "latest" must never be recorded as identity
        assert not any(
            str(svc.get("artifact_digest")).startswith("latest") for svc in compose["services"]
        )

    def test_a_pin_contradicting_a_member_artifact_is_refused(self):
        with pytest.raises(ValueError, match="one launched thing, one recorded artifact"):
            environment_compose(
                _candidate_set(),
                services=["orders"],
                service_pins={"orders": f"sha256:{'d' * 64}"},
            )

    def test_the_compose_carries_the_tested_world_digest(self):
        pins = {"postgres": f"sha256:{'d' * 64}"}
        compose = environment_compose(
            _candidate_set(), services=["postgres"], service_pins=pins, policy_refs=["compat/1"]
        )
        assert compose["tested_world_digest"] == world_digest(
            _candidate_set(), environment=pins, policy_refs=["compat/1"]
        )

    def test_a_changed_dependency_pin_changes_the_bound_world(self):
        old_pins = {"postgres": f"sha256:{'d' * 64}"}
        new_pins = {"postgres": f"sha256:{'f' * 64}"}
        old = environment_compose(_candidate_set(), services=["postgres"], service_pins=old_pins)
        new = environment_compose(_candidate_set(), services=["postgres"], service_pins=new_pins)
        assert old["tested_world_digest"] != new["tested_world_digest"]


class TestVerificationSelector:
    def test_a_selector_matches_when_every_filled_field_is_equal(self):
        selector = VerificationSelector(
            check_id="chk-1", workflow_identity="wf-9", event="push", ref="refs/heads/main"
        )
        observed = {
            "check_id": "chk-1",
            "workflow_identity": "wf-9",
            "event": "push",
            "ref": "refs/heads/main",
            "job_identity": "job-77",  # not pinned by the selector → wildcard
        }
        assert selector_matches(selector, observed) is True

    def test_a_workflow_identity_mismatch_never_matches(self):
        selector = VerificationSelector(check_id="chk-1", workflow_identity="wf-9")
        assert (
            selector_matches(selector, {"check_id": "chk-1", "workflow_identity": "wf-10"}) is False
        )

    def test_a_missing_observed_identity_is_no_match(self):
        selector = VerificationSelector(check_id="chk-1", tested_revision="abc123")
        assert selector_matches(selector, {"check_id": "chk-1"}) is False

    def test_a_check_id_mismatch_never_matches(self):
        selector = VerificationSelector(check_id="chk-1")
        assert selector_matches(selector, {"check_id": "chk-2"}) is False

    def test_empty_fields_are_wildcards(self):
        selector = VerificationSelector(check_id="chk-1")
        assert selector_matches(selector, {"check_id": "chk-1"}) is True

    def test_the_selector_is_frozen(self):
        selector = VerificationSelector(check_id="chk-1")
        with pytest.raises(dataclasses.FrozenInstanceError):
            selector.ref = "refs/heads/main"


class TestFreshness:
    def test_recent_evidence_is_fresh(self):
        assert freshness("2026-09-21T10:00:00+00:00", "2026-09-21T10:30:00+00:00") == "fresh"

    def test_evidence_past_the_max_age_is_stale(self):
        assert (
            freshness("2026-09-21T08:00:00+00:00", "2026-09-21T10:00:00+00:00", max_age_s=3600)
            == "stale"
        )

    def test_exactly_max_age_is_still_fresh(self):
        assert (
            freshness("2026-09-21T09:00:00+00:00", "2026-09-21T10:00:00+00:00", max_age_s=3600)
            == "fresh"
        )

    def test_unparseable_timestamps_are_unknown_never_an_exception(self):
        assert freshness("not-a-timestamp", "2026-09-21T10:00:00+00:00") == "unknown"
        assert freshness("2026-09-21T10:00:00+00:00", "yesterday-ish") == "unknown"

    def test_mixed_aware_and_naive_timestamps_are_unknown(self):
        assert freshness("2026-09-21T10:00:00", "2026-09-21T10:30:00+00:00") == "unknown"

    def test_evidence_observed_in_the_future_is_unknown(self):
        assert freshness("2026-09-21T11:00:00+00:00", "2026-09-21T10:00:00+00:00") == "unknown"

    def test_z_suffix_timestamps_parse(self):
        assert freshness("2026-09-21T10:00:00Z", "2026-09-21T10:00:30Z") == "fresh"


class TestEvidenceAwareReview:
    def test_a_claim_with_all_evidence_verified_is_supported(self):
        review = evidence_aware_review(
            claims=[
                {
                    "claim_id": "c1",
                    "text": "endpoint returns 201",
                    "evidence_ids": ["e1", "e2"],
                }
            ],
            evidence=[
                {"evidence_id": "e1", "verified": True},
                {"evidence_id": "e2", "verified": True},
            ],
        )
        assert review == {"supported": ["c1"], "unsupported": [], "unbacked": []}

    def test_one_unverified_piece_makes_the_claim_unsupported(self):
        review = evidence_aware_review(
            claims=[{"claim_id": "c1", "text": "t", "evidence_ids": ["e1", "e2"]}],
            evidence=[
                {"evidence_id": "e1", "verified": True},
                {"evidence_id": "e2", "verified": False},
            ],
        )
        assert review["supported"] == []
        assert review["unsupported"] == ["c1"]
        assert review["unbacked"] == []

    def test_a_referenced_but_unrecorded_evidence_id_is_unsupported(self):
        review = evidence_aware_review(
            claims=[{"claim_id": "c1", "text": "t", "evidence_ids": ["ghost"]}],
            evidence=[{"evidence_id": "e1", "verified": True}],
        )
        assert review["unsupported"] == ["c1"]

    def test_a_claim_without_evidence_ids_is_unbacked_never_supported(self):
        review = evidence_aware_review(
            claims=[{"claim_id": "c1", "text": "it works, trust me", "evidence_ids": []}],
            evidence=[],
        )
        assert review == {"supported": [], "unsupported": [], "unbacked": ["c1"]}

    def test_the_review_separates_all_three_buckets(self):
        review = evidence_aware_review(
            claims=[
                {"claim_id": "ok", "text": "t", "evidence_ids": ["e1"]},
                {"claim_id": "bad", "text": "t", "evidence_ids": ["e1", "e2"]},
                {"claim_id": "bare", "text": "t", "evidence_ids": []},
            ],
            evidence=[
                {"evidence_id": "e1", "verified": True},
                {"evidence_id": "e2", "verified": False},
            ],
        )
        assert review == {"supported": ["ok"], "unsupported": ["bad"], "unbacked": ["bare"]}
