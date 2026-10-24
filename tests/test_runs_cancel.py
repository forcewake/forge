"""Cancellation revokes native publication and lane authority before cleanup."""

import pytest
from sqlalchemy import select

from forge.api.lane_auth_values import LaneAuthError
from forge.domain.states import State
from forge.store.models_execution import EngineCandidate, EngineStep
from forge.store.records import StaleLease
from forge_protocol import LaneReport
from tests.test_engine_commands import commands as commands, envelope
from tests.test_engine_delivery import delivery as delivery, completed
from tests.test_engine_lane_handlers import case as case, env as env, settings as settings, started


class TestCancelWithdrawsSteps:
    async def test_cancel_sets_flag_and_cancels_scheduled_steps(self, commands):
        env = commands["env"]
        run, _, _ = await started(commands)
        result = await commands["commands"].submit(envelope(commands, "cancel"))
        assert result.reason == "cancelled"
        current = await env.runs.load(run.id)
        assert current.cancel_requested and current.state == State.CANCELLED
        async with env.factory() as session:
            steps = list(
                await session.scalars(select(EngineStep).where(EngineStep.run_id == run.id))
            )
            assert any(step.state == "cancelled" for step in steps)
            assert all(step.state not in ("scheduled", "running") for step in steps)

    async def test_late_scheduled_execution_finds_nothing(self, commands):
        env = commands["env"]
        await started(commands)
        (old_lease,) = await env.steps.claim_due("late-worker", env.clock.now(), 60)
        await commands["commands"].submit(envelope(commands, "cancel"))
        assert await env.steps.claim_due("replacement", env.clock.now(), 60) == ()
        with pytest.raises(StaleLease):
            await env.steps.complete_and_enqueue(old_lease, "{}", now=env.clock.now())
        assert (await env.runner.tick()).claimed == 0


class TestPublicationGrant:
    async def test_cancel_mid_proposal_revokes_the_grant(self, commands, delivery, monkeypatch):
        env = commands["env"]
        run, _, pending = await started(delivery)
        await completed(delivery, pending)
        original = delivery["delivery_store"].validated

        async def cancel_after_validation(*args, **kwargs):
            await original(*args, **kwargs)
            await commands["commands"].submit(envelope(commands, "cancel"))

        monkeypatch.setattr(delivery["delivery_store"], "validated", cancel_after_validation)
        assert (await env.runner.tick()).lost == 1
        current = await env.runs.load(run.id)
        assert current.state == State.CANCELLED and current.cancel_requested
        assert not any(name == "commit" for name, _ in env.fake.calls)
        assert not env.fake.drafts
        async with env.factory() as session:
            candidate = await session.scalar(
                select(EngineCandidate).where(EngineCandidate.run_id == run.id)
            )
            assert candidate.state == "validated" and candidate.published_sha is None

    async def test_verified_harness_result_after_cancel_is_superseded(self, commands):
        env = commands["env"]
        run, _, pending = await started(commands)
        await commands["commands"].submit(envelope(commands, "cancel"))
        # Native CI may finish successfully after revocation. That source fact
        # never reacquires the cancelled run's submission/publication authority.
        key, (handle, status) = next(iter(env.fake.lanes.items()))
        success = status.model_copy(
            update={"status": "succeeded", "evidence": "native:late_success"}
        )
        env.fake.lanes[key] = (handle, success)
        assert (await env.fake.lane_status(handle)).status == "succeeded"
        report = LaneReport(
            **pending.identity.model_dump(),
            approved_digest=pending.dispatch.approved_digest,
            status="failed",
            classification="late_observation",
            driver=pending.dispatch.driver,
            model=pending.dispatch.model,
            terminal_reason="Late runner report",
        )
        with pytest.raises(LaneAuthError):
            await commands["api"].report(pending.identity, report)
        assert (await env.runs.load(run.id)).state == State.CANCELLED
        async with env.factory() as session:
            assert (
                await session.scalar(
                    select(EngineCandidate).where(EngineCandidate.run_id == run.id)
                )
                is None
            )
        assert not env.fake.drafts and not any(name == "commit" for name, _ in env.fake.calls)

    async def test_evaluate_waiting_harness_skips_cancelled_runs(self, commands):
        env = commands["env"]
        run, _, _ = await started(commands)
        await commands["commands"].submit(envelope(commands, "cancel"))
        before = sum(name == "lane_status" for name, _ in env.fake.calls)
        tick = await env.runner.tick()
        assert tick.claimed == tick.completed == 0
        assert sum(name == "lane_status" for name, _ in env.fake.calls) == before
        assert (await env.runs.load(run.id)).state == State.CANCELLED
