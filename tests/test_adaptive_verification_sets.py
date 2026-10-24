"""Retained verification lessons on native checks and immutable current policy.

The multi-repository tested-world planner, environment composer, selective
invalidation ledger and claim rubrics are retired. Their complete 98-case
disposition is recorded in PORTS-verification-sets.csv; this file does not
recreate those services or claim to execute target database/broker recipes.
"""

from datetime import datetime, timedelta, timezone
import json

from pydantic import ValidationError
import pytest

from forge.domain.run_spec import RunSpec2, VerificationContract
from forge.port_values import CheckResult
from forge.verification.checks import verdict
from tests.test_domain_approved_input import spec

SHA = "a" * 40
NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)


def observed(**changes):
    return CheckResult(
        **{
            "name": "unit",
            "tested_sha": SHA,
            "native_id": "job-1",
            "native_run_id": "run-1",
            "workflow_path": ".github/workflows/ci.yml",
            "attempt": 1,
            "role": "verification",
            "status": "passed",
            "observed_at": NOW,
            "evidence": "native:opaque",
            **changes,
        }
    )


def decision(*checks, required=("unit",)):
    return verdict(checks, SHA, required, now=NOW, max_age_seconds=3600)


class TestVerificationLane:
    def test_defaults_to_the_trusted_integration_profile(self):
        # No generated integration profile grants execution authority. A named
        # native verification contract is mandatory in the frozen policy.
        with pytest.raises(ValidationError):
            VerificationContract.model_validate({})
        assert spec().verification.required_checks == ("unit", "lint")

    def test_the_lane_is_frozen(self):
        contract = spec().verification
        with pytest.raises(ValidationError):
            contract.required_checks = ("optional",)


class TestSelectLane:
    @pytest.mark.parametrize("field", ("has_db", "has_broker", "integration_profile"))
    def test_db_probe_selects_the_integration_profile(self, field):
        with pytest.raises(ValidationError):
            VerificationContract.model_validate({"required_checks": ["unit"], field: True})

    def test_broker_probe_selects_the_integration_profile(self):
        assert decision(observed(role="lane")).status == "pending"

    def test_pure_contract_checks_select_the_contract_only_lane(self):
        # A coding lane result cannot certify a required independent check.
        result = decision(observed(role="lane"), observed(name="optional"))
        assert result.successful == () and result.status == "pending"

    def test_the_lane_identity_is_stable_for_the_same_checks(self):
        a, b = observed(), observed(name="lint", native_id="job-2")
        first = decision(a, b, required=("unit", "lint"))
        second = decision(b, a, required=("lint", "unit"))
        assert first.verified_for(SHA, ("lint", "unit"), now=NOW, max_age_seconds=3600)
        assert second.verified_for(SHA, ("unit", "lint"), now=NOW, max_age_seconds=3600)
        assert first.successful == second.successful == ("lint", "unit")

    def test_different_checks_get_different_lanes(self):
        result = decision(observed())
        assert not result.verified_for(SHA, ("different",), now=NOW, max_age_seconds=3600)


class TestVerificationSelector:
    def test_a_selector_matches_when_every_filled_field_is_equal(self):
        check = observed()
        result = decision(check)
        assert result.status == "passed"
        assert json.loads(result.evidence)["checks"] == [check.model_dump(mode="json")]

    @pytest.mark.parametrize(
        "field,value", [("workflow_path", "other.yml"), ("native_run_id", "other")]
    )
    def test_a_workflow_identity_mismatch_never_matches(self, field, value):
        first = observed(status="failed", failure_kind="code")
        decoy = observed(**{field: value, "attempt": 2, "native_id": "job-2"})
        assert decision(first, decoy).status == "unknown"

    @pytest.mark.parametrize("field", ("workflow_path", "native_run_id", "native_id", "tested_sha"))
    def test_a_missing_observed_identity_is_no_match(self, field):
        value = observed().model_dump(mode="json")
        value.pop(field)
        with pytest.raises(ValidationError):
            CheckResult.model_validate_json(json.dumps(value))

    def test_a_check_id_mismatch_never_matches(self):
        assert decision(observed(), observed(native_id="another-job")).status == "unknown"

    @pytest.mark.parametrize("field", ("native_id", "native_run_id", "workflow_path", "name"))
    def test_empty_fields_are_wildcards(self, field):
        # Empty selectors no longer act as wildcards at the authority boundary.
        with pytest.raises(ValidationError):
            observed(**{field: ""})

    def test_the_selector_is_frozen(self):
        check = observed()
        with pytest.raises(ValidationError):
            check.workflow_path = "different.yml"


class TestFreshness:
    def test_recent_evidence_is_fresh(self):
        assert decision(observed(observed_at=NOW - timedelta(minutes=30))).status == "passed"

    def test_evidence_past_the_max_age_is_stale(self):
        assert decision(observed(observed_at=NOW - timedelta(seconds=3601))).status == "stale"

    def test_exactly_max_age_is_still_fresh(self):
        assert decision(observed(observed_at=NOW - timedelta(seconds=3600))).status == "passed"

    @pytest.mark.parametrize("timestamp", ("not-a-timestamp", "yesterday-ish"))
    def test_unparseable_timestamps_are_unknown_never_an_exception(self, timestamp):
        with pytest.raises(ValidationError):
            observed(observed_at=timestamp)

    def test_mixed_aware_and_naive_timestamps_are_unknown(self):
        with pytest.raises(ValidationError):
            observed(observed_at=NOW.replace(tzinfo=None))

    def test_evidence_observed_in_the_future_is_unknown(self):
        assert decision(observed(observed_at=NOW + timedelta(seconds=1))).status == "stale"

    def test_z_suffix_timestamps_parse(self):
        value = observed().model_dump(mode="json")
        value["observed_at"] = "2026-10-24T12:00:00Z"
        check = CheckResult.model_validate_json(json.dumps(value))
        assert check.observed_at == NOW and decision(check).status == "passed"


class TestEvidenceAwareReview:
    def test_a_claim_with_all_evidence_verified_is_supported(self):
        result = decision(observed(), observed(name="lint"), required=("unit", "lint"))
        assert result.status == "passed" and result.successful == ("lint", "unit")

    def test_one_unverified_piece_makes_the_claim_unsupported(self):
        assert decision(observed(status="unknown")).status == "unknown"

    def test_a_referenced_but_unrecorded_evidence_id_is_unsupported(self):
        assert decision(observed(), required=("unit", "missing")).status == "pending"

    def test_a_claim_without_evidence_ids_is_unbacked_never_supported(self):
        with pytest.raises(ValidationError):
            decision(required=())

    def test_the_review_separates_all_three_buckets(self):
        cases = [
            ((), "pending"),
            ((observed(status="unknown"),), "unknown"),
            ((observed(),), "passed"),
        ]
        for checks, expected in cases:
            assert decision(*checks).status == expected


@pytest.mark.parametrize(
    "field",
    [
        "focused_recipe",
        "environment_compose",
        "candidate_set",
        "tested_world_digest",
        "applicability_digest",
        "environment_profile_digest",
        "dependency_identities",
        "test_bundle_digest",
        "baseline_drift",
        "upgrade_plan",
        "async_failure_scenarios",
        "evidence_ledger",
        "claim_review",
        "verification_selector",
    ],
)
def test_retired_world_or_recipe_fields_cannot_activate_an_execution_path(field):
    value = spec().model_dump(mode="json")
    value[field] = {"declared_verified": True, "members": ["untrusted"], "waive_missing": True}
    with pytest.raises(ValidationError):
        RunSpec2.model_validate_json(json.dumps(value))


@pytest.mark.parametrize("status", ("failed", "unknown", "skipped", "cancelled", "pending"))
def test_no_claim_or_report_text_can_override_required_native_outcomes(status):
    check = observed(status=status, evidence='{"verified":true,"supported":["all"]}')
    result = decision(check)
    assert result.status != "passed"
    assert not result.verified_for(SHA, ("unit",), now=NOW, max_age_seconds=3600)


def test_current_evidence_cannot_be_reactivated_under_a_different_candidate():
    result = decision(observed())
    assert not result.verified_for("b" * 40, ("unit",), now=NOW, max_age_seconds=3600)
    assert result.verified_for(SHA, ("unit",), now=NOW, max_age_seconds=3600)
    assert json.loads(result.evidence)["checks"][0]["tested_sha"] == SHA


@pytest.mark.parametrize("names", [[], [""], [" "], ["\t\n"], ["unit", "unit"]])
def test_unusable_required_check_identity_refuses_during_policy_validation(names):
    with pytest.raises(ValidationError):
        VerificationContract.model_validate_json(json.dumps({"required_checks": names}))
