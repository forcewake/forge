"""Consume-once human gates bind exact approved bytes and one atomic dispatch."""

from dataclasses import replace
from datetime import timedelta
import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from forge.domain.states import Handler, State
from forge.engine.runner import LeaseHolder, StepContext
from forge.store.models_runs import EngineApprovedInput, EngineGate
from forge.store.models_execution import EngineStep
from forge.store.records import StoreConflict
from tests.test_engine_approval import env as env


async def consume(env, run, frozen, *, claim=None, command=None):
    claim = claim or await env.claim(run, frozen)
    step = await env.approve(run.id, claim, command_key=command or uuid4().hex)
    leases = await env.steps.claim_due("gate-worker", env.clock.now(), 60, 100)
    lease = next(value for value in leases if value.step_id == step.id)
    result = await env.approve_handler(
        StepContext(LeaseHolder(lease), await env.steps.load(step.id), env.effects)
    )
    saved = await env.steps.complete_and_enqueue(
        lease,
        result.result_json,
        result.successors,
        now=env.clock.now(),
        transition_to=result.next_state,
        approval=result.approval,
    )
    return saved


class TestRecordApproval:
    async def test_persists_all_gate_bindings(self, env):
        run, frozen = await env.planned()
        async with env.factory() as session:
            gate = await session.get(EngineGate, frozen.gate_id)
            assert gate.run_id == run.id and gate.state == "open"
            assert gate.approved_digest == frozen.approved.approved_digest
            assert json.loads(gate.actor_scope_json) == [
                run.provider,
                run.connection_id,
                run.repository_id,
                "issue",
                "1",
            ]
            assert gate.consumed_at is None and gate.consumed_step_id is None
            assert frozen.expires_at > env.clock.now()
        assert frozen.spec.digest == frozen.approved.spec_digest
        assert frozen.spec.plan.digest == frozen.approved.plan_digest
        assert frozen.spec.base_sha == frozen.approved.base_sha
        assert frozen.spec.spec_json in frozen.comment
        assert frozen.approved.approved_digest in frozen.comment

    async def test_unknown_run_cannot_prepare_a_gate(self, env):
        await env.start()
        assert (await env.runner.tick()).completed == 1
        (lease,) = await env.steps.claim_due("plan-worker", env.clock.now(), 60)
        plan = env.planner.script[0][1]
        with pytest.raises(StoreConflict, match="run does not exist"):
            await env.approvals.prepare(
                replace(lease, run_id=uuid4().hex),
                env.intent.freeze(plan),
                ttl_seconds=60,
                now=env.clock.now(),
            )
        assert await env.count(EngineGate) == 0


class TestGateValidity:
    async def test_fresh_gate_is_valid(self, env):
        run, frozen = await env.planned()
        saved = await consume(env, run, frozen)
        assert "refused" not in json.loads(saved.result_json)
        assert (await env.runs.load(run.id)).state == State.RUNNING
        assert await env.count(EngineApprovedInput) == 1

    async def test_expired_gate_is_invalid(self, env):
        run, frozen = await env.planned()
        env.clock.instant = frozen.expires_at + timedelta(seconds=1)
        saved = await consume(env, run, frozen)
        assert json.loads(saved.result_json) == {"refused": "approval_expired"}
        assert await env.count(EngineApprovedInput) == 0
        assert (await env.runs.load(run.id)).state == State.BLOCKED
        assert (await env.approvals.load(run.id)).state == "expired"

    async def test_consumed_gate_is_invalid(self, env):
        run, frozen = await env.planned()
        await consume(env, run, frozen)
        saved = await consume(env, run, frozen)
        assert json.loads(saved.result_json) == {"refused": "gate_not_open"}
        assert await env.count(EngineApprovedInput) == 1

    @pytest.mark.parametrize(
        "field,value",
        (
            ("plan_digest", "a" * 64),
            ("spec_digest", "b" * 64),
            ("base_sha", "c" * 40),
            ("revision", 2),
            ("approved_digest", "d" * 64),
        ),
    )
    async def test_changed_expectations_invalidate_gate(self, env, field, value):
        run, frozen = await env.planned()
        claim = (await env.claim(run, frozen)).model_copy(update={field: value})
        saved = await consume(env, run, frozen, claim=claim)
        assert json.loads(saved.result_json) == {"refused": "approval_input_mismatch"}
        assert await env.count(EngineApprovedInput) == 0
        assert (await env.approvals.load(run.id)).state == "open"
        assert (await env.runs.load(run.id)).state == State.AWAITING_APPROVAL


class TestConsumeApproval:
    async def test_consume_sets_consumed_at_exactly_once(self, env):
        run, frozen = await env.planned()
        saved = await consume(env, run, frozen)
        async with env.factory() as session:
            gate = await session.get(EngineGate, frozen.gate_id)
            original = (gate.consumed_at, gate.consumed_by, gate.consumed_step_id)
            assert gate.consumed_step_id == saved.id
            assert gate.consumed_by == env.intent.actor.digest
            assert gate.state == "consumed" and gate.consumed_at is not None
        await consume(env, run, frozen)
        async with env.factory() as session:
            gate = await session.get(EngineGate, frozen.gate_id)
            assert (gate.consumed_at, gate.consumed_by, gate.consumed_step_id) == original
            count = await session.scalar(
                select(func.count())
                .select_from(EngineStep)
                .where(EngineStep.run_id == run.id, EngineStep.handler == Handler.DISPATCH.value)
            )
            assert count == 1

    async def test_second_consumption_is_refused(self, env):
        run, frozen = await env.planned()
        first = await consume(env, run, frozen)
        second = await consume(env, run, frozen)
        assert first.id != second.id
        assert json.loads(second.result_json)["refused"] == "gate_not_open"
        assert await env.count(EngineApprovedInput) == 1

    async def test_consume_missing_gate_is_refused(self, env):
        run, frozen = await env.planned()
        claim = (await env.claim(run, frozen)).model_copy(update={"gate_id": uuid4().hex})
        saved = await consume(env, run, frozen, claim=claim)
        assert json.loads(saved.result_json) == {"refused": "gate_scope_mismatch"}
        assert await env.count(EngineApprovedInput) == 0
        assert (await env.approvals.load(run.id)).state == "open"
