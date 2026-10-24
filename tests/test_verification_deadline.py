"""The immutable publication deadline fences native reads and late successful CI."""

import json

import pytest
from sqlalchemy import select

from forge.domain.states import State
from forge.store.models_execution import EngineCandidate, EngineStep
from tests.test_engine_delivery import (
    advance,
    case as case,
    check,
    delivery as delivery,
    env as env,
    published,
    settings as settings,
)


@pytest.mark.parametrize("phase", ("before_read", "during_read"))
async def test_expired_or_late_native_success_cannot_record_a_pass_or_start_closing(
    delivery, monkeypatch, phase
):
    env = delivery["env"]
    run, _, _, sha = await published(delivery)
    check(delivery, sha)
    observed = []
    original = env.fake.checks_for
    if phase == "before_read":
        advance(delivery, 60)
    else:
        advance(delivery, 10)

    async def read(*args):
        observed.append(args)
        result = await original(*args)
        advance(delivery, 55)
        return result

    monkeypatch.setattr(env.fake, "checks_for", read)
    native_before = len(env.fake.calls)
    budget_before = await delivery["budgets"].load(run.id, "implementation")
    assert (await env.runner.tick()).parked == 1
    current = await env.runs.load(run.id)
    assert current.state == State.BLOCKED and current.reason == "verification_timeout"
    assert len(observed) == (1 if phase == "during_read" else 0)
    if phase == "before_read":
        assert len(env.fake.calls) == native_before
    assert (current.attempt, current.generation) == (1, 1)
    assert await delivery["budgets"].load(run.id, "implementation") == budget_before
    assert not delivery["reviewer"].calls and len(env.fake.lanes) == 1
    async with env.factory() as session:
        candidate = await session.scalar(
            select(EngineCandidate).where(EngineCandidate.run_id == run.id)
        )
        assert "verification" not in json.loads(candidate.validation_json)
        assert (
            await session.scalar(
                select(EngineStep).where(
                    EngineStep.run_id == run.id, EngineStep.handler == "review"
                )
            )
            is None
        )
