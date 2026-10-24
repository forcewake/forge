"""Immutable execution and exact published SHA replace the retired world model.

This suite claims no environment/image identity or multi-service qualification.
"""

import json

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from forge.domain.approved_input import ApprovedInput
from forge.domain.run_spec import RunSpec2, VerificationContract
from forge.domain.states import State
from forge.domain.verdicts import CheckVerdict
from forge.port_values import CheckResult
from forge.store.command_runs import CommandRuns
from forge.store.models_execution import EngineCandidate
from forge_protocol import RunIdentity
from tests.test_engine_delivery import (
    case as case,
    delivery as delivery,
    env as env,
    settings as settings,
)
from tests.test_verification_binding import candidate, ready_input
from tests.test_runs_verification import NOW, SHA, evaluate, observation


@pytest.mark.parametrize("field", ("base_sha", "revision", "task_title", "model", "verification"))
async def test_every_changed_execution_input_changes_approval_authority(env, field):
    _, frozen = await env.planned()
    original = frozen.spec
    changes = {
        "base_sha": "9" * 40,
        "revision": original.revision + 1,
        "task_title": "An editorial change still needs a new frozen approval",
        "model": "another-explicit-model",
        "verification": VerificationContract(required_checks=("unit", "secondary")),
    }
    moved = RunSpec2.model_validate_json(
        original.model_copy(update={field: changes[field]}).canonical_bytes
    )
    assert moved.digest != original.digest
    before, after = ApprovedInput.from_spec(original), ApprovedInput.from_spec(moved)
    assert before.approved_digest != after.approved_digest
    with pytest.raises(ValueError, match="frozen specification"):
        before.to_brief(RunIdentity(forge_run_id="a" * 32, attempt=1, generation=1), moved)


async def test_unchanged_approved_identity_round_trips_deterministically_and_is_immutable(env):
    _, frozen = await env.planned()
    original = frozen.spec
    assert RunSpec2.model_validate_json(original.spec_json) == original
    assert ApprovedInput.model_validate_json(frozen.approved.canonical_bytes) == frozen.approved
    assert ApprovedInput.from_spec(original) == frozen.approved
    with pytest.raises(ValidationError):
        original.base_sha = "9" * 40


@pytest.mark.parametrize("field", tuple(ApprovedInput.model_fields))
async def test_tampered_serialized_approval_is_refused_never_repaired_into_a_pass(env, field):
    _, frozen = await env.planned()
    document = json.loads(frozen.approved.canonical_bytes)
    document[field] = (
        999 if field == "revision" else "9" * 40 if field == "base_sha" else "tampered"
    )
    with pytest.raises(ValidationError):
        ApprovedInput.model_validate_json(json.dumps(document))
    document.pop(field)
    with pytest.raises(ValidationError):
        ApprovedInput.model_validate_json(json.dumps(document))


@pytest.mark.parametrize(
    "field", ("tested_sha", "native_id", "attempt", "observed_at", "role", "evidence")
)
def test_missing_native_identity_cannot_claim_a_successful_check(field):
    document = observation().model_dump(mode="json")
    document.pop(field)
    with pytest.raises(ValidationError):
        CheckResult.model_validate_json(json.dumps(document))


@pytest.mark.parametrize("status", ("pending", "skipped", "cancelled", "failed", "unknown"))
def test_a_required_check_must_have_run_successfully(status):
    result = evaluate((observation(status=status),))
    assert not result.verified_for(SHA, ("unit",), now=NOW, max_age_seconds=60)


@pytest.mark.parametrize("missing", ("tested_sha", "required", "observed_at", "evidence"))
def test_missing_or_foreign_verdict_shape_has_no_legacy_success_fallback(missing):
    document = evaluate((observation(),)).model_dump(mode="json")
    document.pop(missing)
    document["subject_identity"] = {"schema": "forge.verification.subject/1", "source_oid": SHA}
    with pytest.raises(ValidationError):
        CheckVerdict.model_validate_json(json.dumps(document))


async def test_refused_current_readiness_keeps_historical_candidate_and_operator_state_consistent(
    delivery,
):
    env = delivery["env"]
    run, _, _, sha = await ready_input(delivery)
    async with env.factory.begin() as session:
        row = await candidate(session, run.id)
        history = json.loads(row.validation_json)
        history["verification"]["tested_sha"] = "f" * 40
        row.validation_json = json.dumps(history)
    assert (await env.runner.tick()).parked == 1
    current = await env.runs.load(run.id)
    assert current.state == State.BLOCKED
    view = json.loads(await CommandRuns(env.factory).snapshot(current))
    assert view["state"] == "blocked" and view["reason"] == "readiness_evidence_stale"
    assert view["candidates"][0]["sha"] == sha
    async with env.factory() as session:
        row = await session.scalar(select(EngineCandidate).where(EngineCandidate.run_id == run.id))
        assert row.published_sha == sha and row.validation_json is not None
        assert json.loads(row.validation_json)["review"]["candidate_sha"] == sha
