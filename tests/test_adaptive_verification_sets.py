"""VER epic core: verification execution separate from the coding agent.

These tests pin the separation: verification selects its own trusted
lane (never the agent's privileged environment), focuses on the
impacted changed set over baselines, flags contracts that need
consumer/provider verification, upgrades from a data-bearing baseline,
binds integration environments to the exact CandidateSet — every member
pinned (baselines included), every launched service resolved to a
recorded exact artifact or flagged unresolved, the whole bound to the
tested-world digest, PERSISTED AT FREEZE TIME (NXT-22: results bind to
the digest at persistence, not call time) — selects checks by IDENTITY,
decays stale evidence, and reviews claims only through verified
evidence. The NXT-21 core: evidence records claim the exact
dependency identities they cover, and ONE member's change invalidates
only the records covering it — a plan-revision bump or an unrelated
member's move never nukes the ledger.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest

from forge.adaptive.models import CandidateSet, CandidateSetMember
from forge.adaptive.verification_sets import (
    RECIPE_SCHEMA,
    DependencyIdentity,
    EnvironmentPinChange,
    EvidenceLedger,
    MemberChange,
    TestBundleChange,
    VerificationLane,
    VerificationSelector,
    async_failure_scenarios,
    bound_applicability_digest,
    bound_tested_world_digest,
    contract_checks,
    db_upgrade_plan,
    environment_compose,
    evidence_aware_review,
    focused_recipe,
    freeze_verified_world,
    freshness,
    member_identity,
    record_evidence,
    select_lane,
    selector_matches,
    world_identities,
)

# aliased: the real name starts with "test" and pytest would collect the
# imported FUNCTION as a test of its own.
from forge.adaptive.workpackage import applicability_digest as app_digest
from forge.adaptive.workpackage import tested_world_digest as world_digest

WORK_CONTRACT_DIGEST = "2e703bcbb99b1d534aedc8e0b22956d9107190a6f1fe635e7438b5a80da87c73"

POSTGRES_PIN = f"sha256:{'d' * 64}"
POSTGRES_MOVED = f"sha256:{'f' * 64}"


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


def _with_orders_moved(candidate_set: CandidateSet, *, image: str | None = None) -> CandidateSet:
    """The same world with the ORDERS member's identity moved."""
    members = []
    for member in candidate_set.members:
        if member.repository_id == "orders":
            members.append(
                CandidateSetMember(
                    repository_id="orders",
                    base_oid=member.base_oid,
                    candidate_oid="9" * 40 if image is None else member.candidate_oid,
                    image_digest=image or member.image_digest,
                    role=member.role,
                )
            )
        else:
            members.append(member)
    return candidate_set.model_copy(update={"members": tuple(members)})


def _whole_plan_digest(candidate_set: CandidateSet) -> str:
    """The OLD behaviour's shape: one digest over EVERYTHING, numbering included.

    This is what NXT-21 defects against — comparing prior evidence to a
    whole-plan digest that moves when the revision number moves, even
    for steps whose dependencies never changed.
    """
    canonical = json.dumps(candidate_set.model_dump(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


class TestFreezeVerifiedWorld:
    """NXT-22: results bind to the digest AT PERSISTENCE, not call time."""

    def test_freezing_persists_both_digests_and_the_inputs_they_cover(self):
        frozen = freeze_verified_world(
            _candidate_set(), environment_pins={"postgres": POSTGRES_PIN}, policy_refs=["compat/1"]
        )
        assert frozen.environment_pins == (("postgres", POSTGRES_PIN),)
        assert frozen.policy_refs == ("compat/1",)
        assert bound_tested_world_digest(frozen) == world_digest(
            _candidate_set(), environment={"postgres": POSTGRES_PIN}, policy_refs=["compat/1"]
        )
        assert bound_applicability_digest(frozen) == app_digest(
            _candidate_set(), environment={"postgres": POSTGRES_PIN}, policy_refs=["compat/1"]
        )

    def test_the_persisted_digest_is_not_the_naive_call_time_recompute(self):
        # a later call that "forgets" the pins computes a DIFFERENT digest:
        # that difference is exactly why the binding must be persisted.
        frozen = freeze_verified_world(
            _candidate_set(), environment_pins={"postgres": POSTGRES_PIN}
        )
        assert bound_tested_world_digest(frozen) != world_digest(frozen)
        # ...and recomputing WITH the same pins reproduces it exactly
        assert bound_tested_world_digest(frozen) == world_digest(
            frozen, environment={"postgres": POSTGRES_PIN}
        )

    def test_refreezing_with_different_pins_is_refused(self):
        frozen = freeze_verified_world(
            _candidate_set(), environment_pins={"postgres": POSTGRES_PIN}
        )
        with pytest.raises(ValueError, match="one frozen set records one world"):
            freeze_verified_world(frozen, environment_pins={"postgres": POSTGRES_MOVED})
        with pytest.raises(ValueError, match="one frozen set records one world"):
            freeze_verified_world(frozen, policy_refs=["compat/9"])

    def test_refreezing_with_the_same_inputs_is_idempotent(self):
        frozen = freeze_verified_world(
            _candidate_set(),
            environment_pins={"postgres": POSTGRES_PIN},
            policy_refs=["compat/1", "compat/2"],
        )
        again = freeze_verified_world(
            frozen,
            environment_pins={"postgres": POSTGRES_PIN},
            policy_refs=["compat/2", "compat/1"],  # order-free, like the digest
        )
        assert again == frozen

    def test_freezing_refuses_a_mutable_tag_pin(self):
        with pytest.raises(ValueError, match="mutable tags and bare names"):
            freeze_verified_world(_candidate_set(), environment_pins={"postgres": "latest"})

    def test_bound_digests_refuse_an_unfrozen_set(self):
        with pytest.raises(ValueError, match="freeze_verified_world first"):
            bound_tested_world_digest(_candidate_set())
        with pytest.raises(ValueError, match="freeze_verified_world first"):
            bound_applicability_digest(_candidate_set())


class TestEnvironmentComposePersistence:
    """NXT-22 meets compose: a frozen world composes against its own pins."""

    def test_a_frozen_set_composes_against_its_persisted_pins(self):
        frozen = freeze_verified_world(
            _candidate_set(), environment_pins={"postgres": POSTGRES_PIN}
        )
        compose = environment_compose(frozen, services=["postgres"])
        assert compose["services"] == [
            {"service": "postgres", "artifact_digest": POSTGRES_PIN, "source": "pin"}
        ]
        assert compose["tested_world_digest"] == bound_tested_world_digest(frozen)

    def test_composing_a_frozen_set_against_different_pins_is_refused(self):
        frozen = freeze_verified_world(
            _candidate_set(), environment_pins={"postgres": POSTGRES_PIN}
        )
        with pytest.raises(ValueError, match="one frozen set records one world"):
            environment_compose(
                frozen, services=["postgres"], service_pins={"postgres": POSTGRES_MOVED}
            )
        with pytest.raises(ValueError, match="one frozen set records one world"):
            environment_compose(frozen, services=[], policy_refs=["compat/9"])

    def test_composing_with_the_same_pins_binds_to_the_persisted_digest(self):
        frozen = freeze_verified_world(
            _candidate_set(),
            environment_pins={"postgres": POSTGRES_PIN},
            policy_refs=["compat/1"],
        )
        compose = environment_compose(
            frozen,
            services=["postgres"],
            service_pins={"postgres": POSTGRES_PIN},
            policy_refs=["compat/1"],
        )
        assert compose["tested_world_digest"] == frozen.tested_world_digest

    def test_a_mutable_tag_pin_is_refused_at_compose_too(self):
        with pytest.raises(ValueError, match="mutable tags and bare names"):
            environment_compose(
                _candidate_set(), services=["postgres"], service_pins={"postgres": "latest"}
            )

    def test_an_unresolved_member_composes_flagged_never_faked(self):
        unresolved = CandidateSet(
            work_id="wp-demo-1",
            plan_revision=1,
            work_contract_digest=WORK_CONTRACT_DIGEST,
            members=[
                CandidateSetMember(
                    repository_id="orders",
                    base_oid="1" * 40,
                    candidate_oid="a" * 40,
                    image_digest="unresolved",
                    role="changed",
                )
            ],
        )
        compose = environment_compose(unresolved, services=["orders"])
        assert compose["services"] == [
            {"service": "orders", "artifact_digest": None, "source": "member", "unresolved": True}
        ]


class TestWorldIdentities:
    def test_every_member_has_an_identity_keyed_by_repository(self):
        identities = world_identities(_candidate_set())
        assert set(identities) == {"orders", "billing", "catalog"}
        orders = identities["orders"]
        assert isinstance(orders, DependencyIdentity)
        assert (orders.candidate_oid, orders.role) == ("a" * 40, "changed")

    def test_the_identity_carries_no_whole_plan_numbers(self):
        # repository, candidate oid, image artifact, role — and NOTHING
        # else: revision and work id are provenance, not dependency.
        assert {field.name for field in dataclasses.fields(DependencyIdentity)} == {
            "repository_id",
            "candidate_oid",
            "image_digest",
            "role",
        }


class TestPerDependencyInvalidation:
    """NXT-21: invalidate by RELEVANT dependency identity, not whole-plan numbers."""

    def _ledger(self) -> tuple[CandidateSet, EvidenceLedger]:
        candidate_set = _candidate_set()
        return candidate_set, EvidenceLedger(
            records=(
                record_evidence("ev-orders", candidate_set, covers=["orders", "catalog"]),
                record_evidence("ev-billing", candidate_set, covers=["billing"]),
            )
        )

    @staticmethod
    def _orders_change(old_world: CandidateSet, new_world: CandidateSet) -> MemberChange:
        return MemberChange(
            "orders",
            previous=member_identity(old_world.members[0]),
            current=member_identity(new_world.members[0]),
        )

    def test_one_members_candidate_moving_invalidates_only_its_covering_evidence(self):
        old_world, ledger = self._ledger()
        new_world = _with_orders_moved(old_world)
        assert ledger.invalidated_by(self._orders_change(old_world, new_world)) == {"ev-orders"}

    def test_a_rebuilt_image_for_one_member_is_equally_precise(self):
        old_world, ledger = self._ledger()
        new_world = _with_orders_moved(old_world, image=f"sha256:{'9' * 64}")
        assert ledger.invalidated_by(self._orders_change(old_world, new_world)) == {"ev-orders"}

    def test_an_unrelated_member_change_never_nukes_the_ledger(self):
        # the acceptance criterion: unrelated code does not force every
        # expensive integration test to rerun — ev-billing survives.
        old_world, ledger = self._ledger()
        new_world = _with_orders_moved(old_world)
        applied = ledger.apply(self._orders_change(old_world, new_world))
        assert applied.applicable_to(new_world) == {"ev-billing"}

    def test_a_revision_bump_preserves_applicability_where_the_whole_plan_digest_moves(self):
        # the pinned distinction (NXT-21): a whole-plan digest — the old
        # behaviour — includes the revision number, so a renumbered plan
        # invalidated EVERYTHING (same shape as revisions.plan_digest).
        # Per-dependency applicability does not even see the number.
        old_world, ledger = self._ledger()
        renumbered = old_world.model_copy(update={"plan_revision": 99})
        assert _whole_plan_digest(old_world) != _whole_plan_digest(renumbered)
        assert ledger.applicable_to(renumbered) == {"ev-orders", "ev-billing"}

    def test_member_order_is_not_part_of_any_identity(self):
        # reordering independent unchanged members preserves applicable
        # verification — order is spelling, not identity.
        old_world, ledger = self._ledger()
        reordered = old_world.model_copy(update={"members": tuple(reversed(old_world.members))})
        assert ledger.applicable_to(reordered) == {"ev-orders", "ev-billing"}

    def test_a_work_id_bump_is_provenance_not_applicability(self):
        old_world, ledger = self._ledger()
        other_work = old_world.model_copy(update={"work_id": "wp-other-9"})
        assert ledger.applicable_to(other_work) == {"ev-orders", "ev-billing"}

    def test_one_shared_dependency_moving_invalidates_every_consumer(self):
        # the negative-test demand: catalog (a shared contract repo,
        # baseline) moves → EVERY record covering it invalidates, and
        # only those.
        old_world, ledger = self._ledger()
        moved_catalog = CandidateSetMember(
            repository_id="catalog",
            base_oid="1" * 40,
            candidate_oid="7" * 40,
            image_digest=f"sha256:{'e' * 64}",
            role="baseline",
        )
        previous = [member_identity(m) for m in old_world.members if m.repository_id == "catalog"][
            0
        ]
        change = MemberChange("catalog", previous=previous, current=member_identity(moved_catalog))
        assert ledger.invalidated_by(change) == {"ev-orders"}
        with_second_consumer = EvidenceLedger(
            records=ledger.records
            + (record_evidence("ev-second-consumer", old_world, covers=["billing", "catalog"]),)
        )
        assert with_second_consumer.invalidated_by(change) == {
            "ev-orders",
            "ev-second-consumer",
        }

    def test_a_removal_invalidates_the_records_covering_the_removed_member(self):
        old_world, ledger = self._ledger()
        previous = member_identity(old_world.members[0])
        assert ledger.invalidated_by(MemberChange("orders", previous=previous, current=None)) == {
            "ev-orders"
        }

    def test_an_appearance_invalidates_nothing_by_itself(self):
        # a NEW member has no prior identity any record could have
        # claimed; whether old records apply to the new world is the
        # state-based applicable_to question, not this event's.
        old_world, ledger = self._ledger()
        appearing = member_identity(old_world.members[0])
        assert (
            ledger.invalidated_by(MemberChange("extra", previous=None, current=appearing)) == set()
        )

    def test_a_test_bundle_change_invalidates_records_judged_under_the_old_bundle(self):
        old_world, ledger = self._ledger()
        # the set carries no bundle → records claim "judged under none";
        # a bundle APPEARING breaks that claim for every record.
        assert ledger.invalidated_by(TestBundleChange(previous=None, current="t" * 64)) == {
            "ev-orders",
            "ev-billing",
        }
        # and precision holds across a bundle move: only the records
        # judged under the OLD bundle break — one recorded after the
        # move (judged under the new bundle) is untouched.
        judged_old = record_evidence(
            "ev-old-bundle",
            old_world.model_copy(update={"test_bundle_digest": "t" * 64}),
            covers=["orders"],
        )
        judged_new = record_evidence(
            "ev-new-bundle",
            old_world.model_copy(update={"test_bundle_digest": "u" * 64}),
            covers=["orders"],
        )
        ledger2 = EvidenceLedger(records=(judged_old, judged_new))
        assert ledger2.invalidated_by(TestBundleChange("t" * 64, "u" * 64)) == {"ev-old-bundle"}

    def test_an_environment_pin_change_is_precise_per_service(self):
        old_world = _candidate_set()
        frozen = freeze_verified_world(old_world, environment_pins={"postgres": POSTGRES_PIN})
        pinned = record_evidence("ev-pinned", frozen, covers=["orders"])
        unpinned = record_evidence("ev-billing", old_world, covers=["billing"])
        ledger = EvidenceLedger(records=(pinned, unpinned))
        change = EnvironmentPinChange("postgres", previous=POSTGRES_PIN, current=POSTGRES_MOVED)
        # only the record that EXPLICITLY pinned postgres breaks
        assert ledger.invalidated_by(change) == {"ev-pinned"}
        # a record pinned to the NEW value (recorded after the move) is untouched
        refrozen = freeze_verified_world(
            _with_orders_moved(old_world), environment_pins={"postgres": POSTGRES_MOVED}
        )
        after = record_evidence("ev-after", refrozen, covers=["orders"])
        ledger_after = EvidenceLedger(records=(after, unpinned))
        assert ledger_after.invalidated_by(change) == set()

    def test_records_bind_pins_at_persistence_time(self):
        # an evidence record from a FROZEN set carries the persisted
        # pins — so a later pin move is judged against what the record
        # actually claimed, not what the caller remembers.
        old_world = _candidate_set()
        frozen = freeze_verified_world(old_world, environment_pins={"postgres": POSTGRES_PIN})
        record = record_evidence("ev", frozen, covers=["orders"])
        assert record.environment_pins == (("postgres", POSTGRES_PIN),)

    def test_a_record_claiming_an_unknown_repository_is_refused(self):
        with pytest.raises(ValueError, match="outside the set"):
            record_evidence("ev-ghost", _candidate_set(), covers=["orders", "ghost"])

    def test_invalidated_by_returns_a_set_of_ids(self):
        old_world, ledger = self._ledger()
        new_world = _with_orders_moved(old_world)
        invalidated = ledger.invalidated_by(self._orders_change(old_world, new_world))
        assert isinstance(invalidated, set)
        assert all(isinstance(evidence_id, str) for evidence_id in invalidated)


class TestSupersedeAndHistory:
    """Supersede without delete; stale callbacks can never masquerade as current."""

    def _applied(self) -> tuple[CandidateSet, CandidateSet, EvidenceLedger]:
        old_world, ledger = TestPerDependencyInvalidation()._ledger()
        new_world = _with_orders_moved(old_world)
        change = TestPerDependencyInvalidation()._orders_change(old_world, new_world)
        return old_world, new_world, ledger.apply(change)

    def test_superseded_records_stay_inspectable_with_their_reason(self):
        _, _, applied = self._applied()
        superseded = {record.evidence_id: record for record in applied.records}
        assert set(superseded) == {"ev-orders", "ev-billing"}  # history is never deleted
        assert superseded["ev-orders"].superseded is True
        assert "orders" in superseded["ev-orders"].superseded_reason
        assert superseded["ev-billing"].superseded is False  # untouched stays current

    def test_a_superseded_record_never_masquerades_as_current(self):
        # the stale-callback fence: even when the world still matches the
        # record's exact fingerprint, a superseded record is NOT
        # applicable — a late "verified" callback cannot requalify it.
        old_world, _, applied = self._applied()
        assert "ev-orders" not in applied.applicable_to(old_world)
        assert applied.applicable_to(old_world) == {"ev-billing"}

    def test_superseding_an_unknown_id_is_refused(self):
        _, _, ledger = self._applied()
        with pytest.raises(ValueError, match="no evidence record"):
            ledger.supersede(["ghost"], "typo")

    def test_double_superseding_keeps_the_first_reason(self):
        _, _, applied = self._applied()
        doubled = applied.supersede(["ev-orders"], "a second, later reason")
        record = {r.evidence_id: r for r in doubled.records}["ev-orders"]
        assert record.superseded_reason.startswith("member orders")

    def test_a_reversion_requires_an_explicit_applicability_check(self):
        # A -> B superseded the A-evidence; B -> A reverts the world.
        # The reversion must NOT blindly reactivate the old record...
        old_world, new_world, applied = self._applied()
        reversion = MemberChange(
            "orders",
            previous=member_identity(new_world.members[0]),
            current=member_identity(old_world.members[0]),
        )
        assert applied.invalidated_by(reversion) == set()
        assert "ev-orders" not in applied.applicable_to(old_world)
        # ...re-qualification is an EXPLICIT decision that re-checks the
        # record's complete fingerprint against the current world.
        reactivated = applied.reactivate("ev-orders", old_world)
        assert reactivated.applicable_to(old_world) == {"ev-orders", "ev-billing"}

    def test_reactivation_refuses_a_mismatched_world(self):
        _, new_world, applied = self._applied()
        with pytest.raises(ValueError, match="explicit applicability check"):
            applied.reactivate("ev-orders", new_world)

    def test_reactivating_a_current_record_is_refused(self):
        old_world, ledger = TestPerDependencyInvalidation()._ledger()
        with pytest.raises(ValueError, match="not superseded"):
            ledger.reactivate("ev-orders", old_world)

    def test_applicability_requires_the_complete_fingerprint(self):
        # a record applies only when EVERY claimed dependency sits in
        # the world at the exact claimed identity — one moved member
        # anywhere in the claim disqualifies the whole record.
        old_world, ledger = TestPerDependencyInvalidation()._ledger()
        catalog_moved = old_world.model_copy(
            update={
                "members": tuple(
                    CandidateSetMember(
                        repository_id="catalog",
                        base_oid="1" * 40,
                        candidate_oid="7" * 40,
                        image_digest=f"sha256:{'e' * 64}",
                        role="baseline",
                    )
                    if m.repository_id == "catalog"
                    else m
                    for m in old_world.members
                )
            }
        )
        assert ledger.applicable_to(catalog_moved) == {"ev-billing"}

    def test_applicability_binds_the_judging_environment_too(self):
        # test bundle and environment profile are part of the claimed
        # fingerprint: judged under a different bundle/profile, the
        # record does not apply — no indefinite reuse by SHA.
        old_world, ledger = TestPerDependencyInvalidation()._ledger()
        rebundled = old_world.model_copy(update={"test_bundle_digest": "t" * 64})
        reprofiled = old_world.model_copy(update={"environment_profile_digest": "9" * 64})
        assert ledger.applicable_to(rebundled) == set()
        assert ledger.applicable_to(reprofiled) == set()

    def test_the_ledger_and_records_are_frozen_values(self):
        record = record_evidence("ev", _candidate_set(), covers=["orders"])
        with pytest.raises(dataclasses.FrozenInstanceError):
            record.superseded = True  # type: ignore[misc]
        ledger = EvidenceLedger(records=(record,))
        with pytest.raises(dataclasses.FrozenInstanceError):
            ledger.records = ()  # type: ignore[misc]

    def test_duplicate_evidence_ids_are_refused(self):
        record = record_evidence("ev", _candidate_set(), covers=["orders"])
        with pytest.raises(ValueError, match="duplicate evidence id"):
            EvidenceLedger().record(record, record)
