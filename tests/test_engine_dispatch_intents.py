"""Nonce discovery reads the single effect journal without a lane session."""

import json

import pytest
from sqlalchemy import delete, select

from forge.store.dispatch_intents import nonce_digest
from forge.store.effects import EffectStore
from forge.store.models_execution import EngineEffect, EngineStep
from forge.store.models_lane import EngineLaneSession
from forge.store.models_runs import EngineRun
from forge.store.records import StoreConflict
from tests.test_engine_lane_handlers import case as case, env as env, settings as settings, started


async def dispatch(case):
    run, _, pending = await started(case)
    return run, pending, EffectStore(case["env"].factory)


async def test_indexed_nonce_survives_missing_session_and_run_revival(case):
    run, pending, effects = await dispatch(case)
    repo = case["env"].intent.task.subject.repo
    async with case["env"].factory() as session, session.begin():
        await session.execute(delete(EngineLaneSession))
        row = await session.get(EngineRun, run.id)
        row.attempt, row.generation, row.cancel_requested = 2, 2, True
    value = await effects.dispatch_for_nonce(repo, pending.dispatch.nonce)
    assert value == pending.dispatch
    assert await effects.dispatch_for_nonce(repo, "unrecorded-nonce") is None
    foreign = repo.model_copy(
        update={"identity": repo.identity.model_copy(update={"repository_id": "999"})}
    )
    with pytest.raises(StoreConflict, match="exact committed intent"):
        await effects.dispatch_for_nonce(foreign, pending.dispatch.nonce)


@pytest.mark.parametrize("change", ["payload", "owner"])
async def test_nonce_refuses_corrupt_payload_or_original_step_identity(case, change):
    _, pending, effects = await dispatch(case)
    nonce = pending.dispatch.nonce
    async with case["env"].factory() as session, session.begin():
        row = await session.scalar(
            select(EngineEffect).where(EngineEffect.dispatch_nonce_digest == nonce_digest(nonce))
        )
        if change == "payload":
            raw = json.loads(row.payload_json)
            raw["correlation_id"] = "changed"
            row.payload_json = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        else:
            step = await session.get(EngineStep, row.step_id)
            step.generation += 1
    with pytest.raises(StoreConflict):
        await effects.dispatch_for_nonce(case["env"].intent.task.subject.repo, nonce)


async def test_dispatch_nonce_has_one_owner_on_the_effect_journal(case):
    _, pending, effects = await dispatch(case)
    async with case["env"].factory() as session:
        row = await session.scalar(select(EngineEffect).where(EngineEffect.kind == "dispatch_lane"))
        assert row.dispatch_nonce_digest == nonce_digest(pending.dispatch.nonce)
        indexes = {index.name: index for index in EngineEffect.__table__.indexes}
        assert indexes["uq_effect_dispatch_nonce"].unique
