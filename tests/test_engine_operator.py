"""Operator retries retain original authority; paused WIP resumes a new generation."""

import hashlib

import pytest
from forge_protocol import CheckpointEntry, CheckpointManifest, LaneReport
from sqlalchemy import select

from forge.domain.states import Handler, State
from forge.engine.operator_actions import OperatorActions
from forge.store.command_runs import CommandRuns
from forge.store.controls import ControlStore
from forge.store.models_execution import EngineStep
from forge.store.resume import ResumeStore
from forge.store.retry import RetryStore
from tests.test_engine_commands import commands as commands, envelope
from tests.test_engine_delivery import delivery as delivery, completed, published, check
from tests.test_engine_lane_handlers import (
    case as case,
    env as env,
    settings as settings,
    started,
    report,
)


@pytest.fixture
def operator(commands):
    env = commands["env"]
    actions = OperatorActions(
        CommandRuns(env.factory),
        RetryStore(env.factory),
        ControlStore(env.factory, {"test": frozenset({"interrupt", "steer"})}),
        ResumeStore(env.factory),
        env.fake,
        env.clock,
    )
    commands["commands"].actions.update(actions.actions)
    return commands


async def test_retry_planning_reclaims_original_step_with_same_payload_and_identity(operator):
    env = operator["env"]
    run = await env.start()
    assert (await env.runner.tick()).completed == 1
    original = env.runner.handlers[Handler.PLAN]

    async def fail(context):
        raise RuntimeError("synthetic planning failure")

    env.runner.handlers[Handler.PLAN] = fail
    assert (await env.runner.tick()).parked == 1
    async with env.factory() as session:
        step = await session.scalar(
            select(EngineStep).where(EngineStep.run_id == run.id, EngineStep.state == "failed")
        )
        before = (step.id, step.payload_json, step.attempt, step.generation, step.result_json)
    env.runner.handlers[Handler.PLAN] = original
    value = envelope(operator, "retry")
    result = await operator["commands"].submit(value)
    assert result.reason == "retry_scheduled"
    assert await operator["commands"].submit(value) == result
    current = await env.runs.load(run.id)
    assert current.state == State.PLANNING and (current.attempt, current.generation) == (1, 1)
    async with env.factory() as session:
        step = await session.get(EngineStep, before[0])
        assert (
            step.id,
            step.payload_json,
            step.attempt,
            step.generation,
            step.result_json,
        ) == before
        assert step.state == "scheduled" and step.lease_epoch == 1
    assert (await env.runner.tick()).completed == 1
    assert (await env.runs.load(run.id)).state == State.AWAITING_APPROVAL


async def test_retry_lane_grants_one_new_attempt_and_keeps_the_original_approval(operator):
    env = operator["env"]
    run, frozen, pending = await started(operator)
    await report(
        operator, pending, status="failed", classification="driver_failure", reason="fatal failure"
    )
    assert (await env.runner.tick()).parked == 1
    result = await operator["commands"].submit(envelope(operator, "retry"))
    assert result.reason == "retry_scheduled"
    current = await env.runs.load(run.id)
    assert current.state == State.RUNNING and (current.attempt, current.generation) == (2, 2)
    assert (await env.approvals.load(run.id)).approved == frozen.approved
    assert (await env.runner.tick()).completed == 1
    assert (await env.runner.tick()).completed == 1
    assert sum(name == "dispatch_lane" for name, _ in env.fake.calls) == 2


async def test_retry_refuses_corrupt_frozen_policy_without_new_execution(operator):
    from forge.store.models_runs import EngineRunSpec

    env = operator["env"]
    run, _, pending = await started(operator)
    await report(
        operator, pending, status="failed", classification="driver_failure", reason="fatal failure"
    )
    assert (await env.runner.tick()).parked == 1
    before = await env.runs.load(run.id)
    dispatches = sum(name == "dispatch_lane" for name, _ in env.fake.calls)
    async with env.factory() as session, session.begin():
        row = await session.scalar(select(EngineRunSpec).where(EngineRunSpec.run_id == run.id))
        row.digest = "0" * 64
    result = await operator["commands"].submit(envelope(operator, "retry"))
    assert result.status == "refused" and result.reason == "command_conflict"
    after = await env.runs.load(run.id)
    assert (after.state, after.attempt, after.generation) == (
        before.state,
        before.attempt,
        before.generation,
    )
    assert sum(name == "dispatch_lane" for name, _ in env.fake.calls) == dispatches


async def test_controls_are_capability_checked_atomic_and_replay_idempotent(operator):
    env = operator["env"]
    run, _, pending = await started(operator)
    pause = envelope(operator, "pause", native_id="pause")
    assert (await operator["commands"].submit(pause)).reason == "control_queued"
    await operator["commands"].submit(pause)
    assert (
        await operator["commands"].submit(
            envelope(operator, "steer", native_id="steer", argument="Keep the public API.")
        )
    ).reason == "control_queued"
    controls = await operator["api"].controls(pending.identity, 0)
    assert [(c.action, c.sequence) for c in controls] == [("pause", 1), ("steer", 2)]
    assert controls[1].text == "Keep the public API."
    operator["commands"].actions["steer"] = OperatorActions(
        CommandRuns(env.factory),
        RetryStore(env.factory),
        ControlStore(env.factory, {"test": frozenset({"interrupt"})}),
        ResumeStore(env.factory),
        env.fake,
        env.clock,
    ).control
    result = await operator["commands"].submit(
        envelope(operator, "steer", native_id="unsupported", argument="change")
    )
    assert result.reason == "capability_unavailable"
    assert len(await operator["api"].controls(pending.identity, 0)) == 2
    assert (await env.runs.load(run.id)).state == State.RUNNING


async def paused(operator):
    run, frozen, pending = await started(operator)
    raw = b"exact work in progress\n"
    key = hashlib.sha256(raw).hexdigest()

    async def chunks():
        yield raw

    await operator["api"].blobs.put(key, chunks())
    await operator["api"].record_blob(pending.identity, key, len(raw))
    checkpoint = CheckpointManifest(
        **pending.identity.model_dump(),
        base_sha=pending.dispatch.execution_base_sha,
        approved_digest=pending.dispatch.approved_digest,
        sequence=1,
        driver=pending.dispatch.driver,
        entries=(
            CheckpointEntry(
                path="work.py", mode="100644", sha256=key, state="new", size_bytes=len(raw)
            ),
        ),
    )
    await operator["api"].checkpoint(pending.identity, checkpoint.checkpoint_id, checkpoint)
    value = LaneReport(
        **pending.identity.model_dump(),
        approved_digest=pending.dispatch.approved_digest,
        status="paused",
        classification="paused",
        driver=pending.dispatch.driver,
        model=pending.dispatch.model,
        terminal_reason="Paused at operator request",
        checkpoint_id=checkpoint.checkpoint_id,
    )
    await operator["api"].report(pending.identity, value)
    assert (await operator["env"].runner.tick()).parked == 1
    return run, frozen, pending, checkpoint, key, raw


async def test_resume_uses_exact_named_checkpoint_without_resetting_attempt_or_budget(operator):
    env = operator["env"]
    run, frozen, pending, checkpoint, key, raw = await paused(operator)
    pool = await operator["budgets"].load(run.id, "implementation")
    value = envelope(operator, "resume")
    result = await operator["commands"].submit(value)
    assert result.reason == "resume_scheduled"
    assert await operator["commands"].submit(value) == result
    current = await env.runs.load(run.id)
    assert (current.attempt, current.generation) == (1, 2)
    assert (await operator["budgets"].load(run.id, "implementation")).capacity == pool.capacity
    assert (await env.approvals.load(run.id)).approved == frozen.approved
    assert (await env.runner.tick()).completed == 1
    assert (await env.runner.tick()).completed == 1
    identity = pending.identity.model_copy(update={"generation": 2})
    resumed = await operator["auth"].store.load(identity)
    assert resumed.dispatch.checkpoint_id == checkpoint.checkpoint_id
    assert await operator["api"].latest_checkpoint(identity) == checkpoint
    assert await operator["api"].read_blob(identity, key) == raw
    assert sum(name == "ensure_branch" for name, _ in env.fake.calls) == 1


async def test_resume_refuses_branch_drift_and_keeps_paused_identity(operator):
    env = operator["env"]
    run, _, pending, _, _, _ = await paused(operator)
    from forge.providers.fake import Blob
    from forge.providers.fake.state import commit_value

    repo = env.intent.task.subject.repo
    human = commit_value({"a.py": Blob(b"human\n")}, pending.dispatch.base_sha, "Human change")
    env.fake.repositories[repo.identity.digest].commits[human.sha] = human
    env.fake.repositories[repo.identity.digest].branches["forge/" + run.id] = human.sha
    result = await operator["commands"].submit(envelope(operator, "resume"))
    assert result.reason == "branch_drift"
    current = await env.runs.load(run.id)
    assert current.state == State.BLOCKED and current.generation == 1


@pytest.mark.parametrize("failure", ["commit", "post_review"])
async def test_retry_publication_or_review_reuses_exact_original_step_after_durable_write(
    operator, delivery, failure
):
    env = operator["env"]
    handler = Handler.PUBLISH if failure == "commit" else Handler.REVIEW
    if failure == "commit":
        run, _, pending = await started(delivery)
        await completed(delivery, pending)
    else:
        run, _, _, sha = await published(delivery)
        check(delivery, sha)
        assert (await env.runner.tick()).completed == 1

    def crash(point, effect):
        if point == "after_t3" and effect.kind == failure:
            raise RuntimeError("synthetic crash after durable receipt")

    env.effects.hook = crash
    assert (await env.runner.tick()).parked == 1
    env.effects.hook = None
    async with env.factory() as session:
        step = await session.scalar(
            select(EngineStep).where(
                EngineStep.run_id == run.id, EngineStep.handler == handler.value
            )
        )
        before = (step.id, step.payload_json, step.result_json, step.attempt, step.generation)
    result = await operator["commands"].submit(envelope(operator, "retry"))
    assert result.reason == "retry_scheduled"
    current = await env.runs.load(run.id)
    assert current.state == (State.RUNNING if failure == "commit" else State.REVIEWING)
    assert (current.attempt, current.generation) == (1, 1)
    async with env.factory() as session:
        step = await session.get(EngineStep, before[0])
        assert (
            step.id,
            step.payload_json,
            step.result_json,
            step.attempt,
            step.generation,
        ) == before
    assert (await env.runner.tick()).completed == 1
    assert sum(name == failure for name, _ in env.fake.calls) == 1
    if failure == "post_review":
        assert len(delivery["reviewer"].calls) == 1
        assert (await env.runner.tick()).completed == 1
        assert (await env.runs.load(run.id)).state == State.READY_FOR_HUMAN


@pytest.mark.parametrize("missing", ["checkpoint", "report", "retained"])
async def test_resume_missing_required_checkpoint_is_typed_refusal_without_fresh_restart(
    operator, missing
):
    from forge.store.models_lane import EngineCheckpoint, EngineLaneSession
    import json

    run, _, _, checkpoint, _, _ = await paused(operator)
    async with operator["env"].factory() as session, session.begin():
        row = await session.get(EngineCheckpoint, checkpoint.checkpoint_id)
        if missing == "checkpoint":
            await session.delete(row)
        elif missing == "retained":
            row.retained = False
        else:
            lane = await session.scalar(
                select(EngineLaneSession).where(EngineLaneSession.run_id == run.id)
            )
            raw = json.loads(lane.api_state_json)
            raw.pop("report")
            lane.api_state_json = json.dumps(raw)
    result = await operator["commands"].submit(envelope(operator, "resume"))
    assert result.reason == "checkpoint_unavailable"
    current = await operator["env"].runs.load(run.id)
    assert current.generation == 1 and current.state == State.BLOCKED
    assert sum(name == "dispatch_lane" for name, _ in operator["env"].fake.calls) == 1


async def test_resume_recovery_refuses_a_missing_checkpoint_binding(operator):
    import json
    from forge.engine.runner import LeaseHolder, StepContext
    from forge.store.models_execution import EngineEffect
    from forge.store.models_lane import EngineLaneSession
    from tests.test_engine_lane_handlers import advance

    env = operator["env"]
    run, _, _, _, _, _ = await paused(operator)
    assert (
        await operator["commands"].submit(envelope(operator, "resume"))
    ).reason == "resume_scheduled"
    assert (await env.runner.tick()).completed == 1

    class PowerLoss(BaseException):
        pass

    def crash(point, effect):
        if point == "after_t1" and effect.kind == "dispatch_lane":
            raise PowerLoss()

    env.effects.hook = crash
    (lease,) = await env.steps.claim_due("crashed-resume", env.clock.now(), 60)
    context = StepContext(LeaseHolder(lease), await env.steps.load(lease.step_id), env.effects)
    with pytest.raises(PowerLoss):
        await env.runner.handlers[Handler.DISPATCH](context)
    env.effects.hook = None
    async with env.factory() as session, session.begin():
        row = await session.scalar(
            select(EngineEffect).where(EngineEffect.key == f"{run.id}:1:2:lane:dispatch")
        )
        raw = json.loads(row.payload_json)
        dispatch = json.loads(raw["dispatch_json"])
        dispatch["checkpoint_id"] = None
        raw["dispatch_json"] = json.dumps(dispatch)
        row.payload_json = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        lane = await session.scalar(
            select(EngineLaneSession).where(
                EngineLaneSession.run_id == run.id, EngineLaneSession.generation == 2
            )
        )
        if lane is not None:
            lane.dispatch_json = json.dumps(dispatch)
    advance(operator, 61)
    assert (await env.runner.tick()).parked == 1
    assert sum(name == "dispatch_lane" for name, _ in env.fake.calls) == 1
