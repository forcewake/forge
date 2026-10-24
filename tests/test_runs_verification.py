"""Positive native proof, an immutable required inventory and honest non-success."""

from datetime import datetime, timedelta, timezone
from itertools import permutations
import json

import pytest
from pydantic import ValidationError

from forge.domain.run_spec import VerificationContract
from forge.domain.verdicts import CheckVerdict
from forge.port_values import CheckResult
from forge.verification.checks import code_failure, verdict
from tests.test_engine_delivery import (
    advance,
    case as case,
    check,
    delivery as delivery,
    env as env,
    published,
    settings as settings,
)

SHA, OTHER = "a" * 40, "b" * 40
NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)


def observation(name="unit", status="passed", **changes):
    values = dict(
        name=name,
        status=status,
        tested_sha=SHA,
        native_id="native-1",
        attempt=1,
        role="verification",
        observed_at=NOW,
        evidence="native:job",
    )
    values.update(changes)
    return CheckResult(**values)


def evaluate(checks, required=("unit",), *, final=False):
    return verdict(
        tuple(checks), SHA, required, now=NOW, max_age_seconds=60, missing_is_final=final
    )


def test_required_contract_is_nonempty_unique_frozen_and_canonical():
    contract = VerificationContract(required_checks=("unit", "lint"))
    assert VerificationContract.model_validate_json(contract.canonical_bytes) == contract
    assert contract.digest != VerificationContract(required_checks=("unit",)).digest
    with pytest.raises(ValidationError):
        contract.required_checks = ("other",)
    for required in ((), ("unit", "unit"), ("",), (" ",)):
        with pytest.raises(ValidationError):
            VerificationContract(required_checks=required)
    with pytest.raises(ValidationError):
        evaluate((), ())


def test_green_optional_job_cannot_substitute_for_the_named_required_check():
    value = evaluate((observation("documentation"),))
    assert value.status == "pending" and not value.successful
    assert evaluate((observation("documentation"),), final=True).status == "not_configured"
    assert not value.verified_for(SHA, ("unit",), now=NOW, max_age_seconds=60)


@pytest.mark.parametrize("status", ("pending", "skipped", "cancelled", "unknown", "failed"))
def test_non_success_never_becomes_positive_proof_or_an_implicit_waiver(status):
    value = evaluate((observation(status=status),))
    assert value.status == (
        "pending" if status == "pending" else "failed" if status == "failed" else "unknown"
    )
    assert not value.verified_for(SHA, ("unit",), now=NOW, max_age_seconds=60)


@pytest.mark.parametrize(
    "changes",
    (
        {"tested_sha": OTHER},
        {"observed_at": NOW - timedelta(seconds=61)},
        {"observed_at": NOW + timedelta(seconds=1)},
    ),
)
def test_wrong_candidate_stale_and_future_observations_remain_unverified(changes):
    value = evaluate((observation(**changes),))
    assert value.status == "stale"
    assert not value.verified_for(SHA, ("unit",), now=NOW, max_age_seconds=60)


def test_pass_names_exact_native_subject_inventory_and_full_observed_surface():
    value = evaluate((observation(), observation("lint", native_id="native-2")), ("unit", "lint"))
    assert value.status == "passed" and value.tested_sha == SHA and value.observed_at == NOW
    assert value.verified_for(SHA, ("lint", "unit"), now=NOW, max_age_seconds=60)
    assert not value.verified_for(OTHER, ("unit", "lint"), now=NOW, max_age_seconds=60)
    assert not value.verified_for(SHA, ("unit",), now=NOW, max_age_seconds=60)
    assert not value.verified_for(
        SHA, ("unit", "lint"), now=NOW + timedelta(seconds=61), max_age_seconds=60
    )
    recorded = json.loads(value.evidence)["checks"]
    assert {row["native_id"] for row in recorded} == {"native-1", "native-2"}
    assert all(row["tested_sha"] == SHA and row["role"] == "verification" for row in recorded)
    assert CheckVerdict.model_validate_json(value.canonical_bytes) == value


@pytest.mark.parametrize("newest_status", ("passed", "failed", "pending"))
def test_attempt_ten_decides_over_attempt_nine_in_every_api_order(newest_status):
    old = observation(attempt=9, native_id="old")
    new = observation(status=newest_status, attempt=10, native_id="new")
    for ordered in permutations((old, new)):
        result = evaluate(ordered)
        assert result.status == newest_status
        assert [row["native_id"] for row in json.loads(result.evidence)["checks"]] == ["new"]


@pytest.mark.parametrize("status", ("passed", "failed", "skipped"))
def test_duplicate_execution_identity_with_conflicting_native_receipts_is_unknown(status):
    first, second = observation(), observation(status=status, native_id="another-native-job")
    for ordered in permutations((first, second)):
        assert evaluate(ordered).status == "unknown"
    assert evaluate((first, first)).status == "passed"


@pytest.mark.parametrize("failure_kind", ("unknown", "infrastructure", "configuration", "code"))
def test_only_explicit_native_code_failure_can_authorize_repair(failure_kind):
    checks = (
        observation(status="failed", failure_kind=failure_kind, evidence="arbitrary runner text"),
    )
    result = evaluate(checks)
    assert code_failure(checks, result) == (failure_kind == "code")
    assert not code_failure((observation(),), evaluate((observation(),)))


async def test_empty_native_surface_never_reaches_ready_or_spends_closing_budget(delivery):
    env = delivery["env"]
    run, _, _, _ = await published(delivery)
    assert (await env.runner.tick()).rescheduled == 1
    advance(delivery, 2)
    assert (await env.runner.tick()).parked == 1
    current = await env.runs.load(run.id)
    assert current.reason == "verification_not_configured"
    assert not delivery["reviewer"].calls
    assert not any(name == "post_review" for name, _ in env.fake.calls)


async def test_complete_independent_contract_reaches_ready_with_exact_durable_evidence(delivery):
    env = delivery["env"]
    run, _, _, sha = await published(delivery)
    check(delivery, sha)
    for _ in range(3):
        assert (await env.runner.tick()).completed == 1
    from forge.domain.states import State
    from forge.store.models_execution import EngineCandidate
    from sqlalchemy import select

    assert (await env.runs.load(run.id)).state == State.READY_FOR_HUMAN
    async with env.factory() as session:
        candidate = await session.scalar(
            select(EngineCandidate).where(EngineCandidate.run_id == run.id)
        )
        result = CheckVerdict.model_validate_json(
            json.dumps(json.loads(candidate.validation_json)["verification"])
        )
        assert result.verified_for(sha, ("unit",), now=env.clock.now(), max_age_seconds=3600)
        assert json.loads(result.evidence)["checks"][0]["native_id"] == "check-1"
