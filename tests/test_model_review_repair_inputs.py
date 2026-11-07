"""A repaired closing review retains original approval and exact prior publication."""

import json

import pytest
from sqlalchemy import select

from forge_protocol import CandidateBundle, RunIdentity, digest
from forge.domain.states import State
from forge.store.model_inputs import ModelInputStore
from forge.store.models_execution import EngineCandidate, EngineEffect
from forge.store.models_lane import EngineLaneSession
from forge.store.records import StoreConflict
from tests.test_engine_delivery import (
    case as case,
    check,
    completed,
    delivery as delivery,
    env as env,
    published,
    settings as settings,
)


@pytest.fixture
async def repaired_review(delivery):
    current = delivery["env"]
    run, frozen, _, first_sha = await published(delivery)
    check(delivery, first_sha, status="failed", failure_kind="code")
    assert (await current.runner.tick()).completed == 1
    for _ in range(2):
        assert (await current.runner.tick()).completed == 1
    identity = RunIdentity(forge_run_id=run.id, attempt=2, generation=2)
    pending = await delivery["store"].pending(identity)
    await completed(delivery, pending, old=b"new\n", new=b"repaired\n")
    assert (await current.runner.tick()).completed == 1
    sha = await current.fake.head(current.intent.task.subject.repo, "forge/" + run.id)
    check(delivery, sha)
    assert (await current.runner.tick()).completed == 1
    assert (await current.runs.load(run.id)).state == State.REVIEWING
    (lease,) = await current.steps.claim_due("review-proof", current.clock.now(), 600)
    raw = await delivery["api"].blobs.read(
        (await delivery["store"].receipt(identity)).candidate.diff_sha256
    )
    return current, lease, frozen, raw, sha, first_sha


async def test_repaired_review_uses_exact_execution_base_without_reapproving_original_spec(
    repaired_review,
):
    current, lease, frozen, raw, sha, first_sha = repaired_review
    spec = await ModelInputStore(current.factory).review(
        lease, frozen.approved, raw, sha, now=current.clock.now()
    )
    assert spec == frozen.spec and spec.base_sha != first_sha
    async with current.factory() as session:
        candidate = await session.scalar(
            select(EngineCandidate).where(EngineCandidate.attempt == 2)
        )
        assert candidate.base_sha == first_sha
        assert candidate.approved_digest == frozen.approved.approved_digest


async def corrupt(current, lease, tamper):
    async with current.factory.begin() as session:
        lane = await session.scalar(select(EngineLaneSession).where(EngineLaneSession.attempt == 2))
        candidate = await session.scalar(
            select(EngineCandidate).where(EngineCandidate.attempt == 2)
        )
        prior = await session.scalar(
            select(EngineEffect).where(EngineEffect.key == f"{lease.run_id}:1:1:publish:commit")
        )
        if tamper == "missing_session":
            await session.delete(lane)
        elif tamper == "session_repo":
            binding = json.loads(lane.provider_binding_json)
            binding["repository_id"] = "987"
            lane.provider_binding_json = json.dumps(binding)
        elif tamper == "session_cancel":
            lane.cancellation_generation += 1
        elif tamper == "session_nonce":
            lane.nonce_hash = "f" * 64
        elif tamper == "session_brief":
            brief = json.loads(lane.brief_json)
            brief.pop("repair")
            lane.brief_json = json.dumps(brief)
        elif tamper in {"dispatch_base", "dispatch_run"}:
            dispatch = json.loads(lane.dispatch_json)
            dispatch["base_sha" if tamper == "dispatch_base" else "forge_run_id"] = "f" * 40
            lane.dispatch_json = json.dumps(dispatch)
        elif tamper == "missing_prior_receipt":
            await session.delete(prior)
        elif tamper == "prior_source_step":
            prior.step_id = lease.step_id
        elif tamper == "prior_source_repo":
            payload = json.loads(prior.payload_json)
            payload["repo"]["identity"]["repository_id"] = "987"
            prior.payload_json, prior.payload_digest = json.dumps(payload), digest(payload)
        elif tamper == "prior_candidate":
            row = await session.scalar(select(EngineCandidate).where(EngineCandidate.attempt == 1))
            row.approved_digest = "f" * 64
        elif tamper == "candidate_base":
            candidate.base_sha = "f" * 40
        else:
            wire = CandidateBundle.model_validate_json(candidate.manifest_json).model_copy(
                update={"base_sha": "f" * 40}
                if tamper == "wire_base"
                else {"diff_sha256": "f" * 64}
            )
            candidate.manifest_json, candidate.digest = wire.model_dump_json(), digest(wire)


@pytest.mark.parametrize(
    "tamper",
    [
        "missing_session",
        "session_repo",
        "session_cancel",
        "session_nonce",
        "session_brief",
        "dispatch_base",
        "dispatch_run",
        "missing_prior_receipt",
        "prior_source_step",
        "prior_source_repo",
        "prior_candidate",
        "candidate_base",
        "wire_base",
        "wire_diff",
    ],
)
async def test_repaired_review_refuses_changed_execution_authority_before_model(
    repaired_review, tamper
):
    current, lease, frozen, raw, sha, _ = repaired_review
    await corrupt(current, lease, tamper)
    with pytest.raises((StoreConflict, ValueError)):
        await ModelInputStore(current.factory).review(
            lease, frozen.approved, raw, sha, now=current.clock.now()
        )
