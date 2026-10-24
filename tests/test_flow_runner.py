"""Retained durable execution lessons run against current SQL command/step owners."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from forge.command_values import CommandEnvelope, CommandResult
from forge.domain.states import Handler, State
from forge.engine.runner import Retryable, RetryPolicy, StepRunner
from forge.ingress_values import Command
from forge.port_values import Actor, RepoRef, SubjectRef
from forge.domain.plan import RepositoryIdentity
from forge.store.commands import CommandStore
from forge.store.models_commands import EngineCommand
from forge.store.models_execution import EngineEffect
from forge.store.records import RunRequest, StaleLease, StepRequest
from forge.store.runs import RunStore
from forge.store.steps import StepStore
from tests.native_store_fixture import native_store as native_store


@pytest.fixture
def db(native_store):
    return native_store


class Clock:
    def __init__(self):
        self.instant = datetime.now(timezone.utc)

    def now(self):
        return self.instant


async def accepted(db, note=7, kind="status", *, now=None):
    repo = RepoRef(
        identity=RepositoryIdentity(provider="github", connection_id="test", repository_id="42"),
        full_name="owner/project",
    )
    envelope = CommandEnvelope(
        source="webhook",
        native_id=str(note),
        actor=Actor(
            provider="github", connection_id="test", native_id="7", login="human", is_bot=False
        ),
        target=SubjectRef(repo=repo, number=1),
        command=Command(kind=kind),
    )
    return await CommandStore(db).accept(envelope, now=now or datetime.now(timezone.utc))


async def execution(db, clock, *, max_attempts=3):
    run = await RunStore(db).admit(RunRequest(uuid4().hex, "github", "test", "42", "issue", "1"))
    step = await StepStore(db).enqueue(
        StepRequest(
            run.id, Handler.PLAN, f"{run.id}:plan", "{}", clock.now(), max_attempts=max_attempts
        )
    )
    return run, step


async def test_a_committed_command_needs_no_redis_wakeup_to_be_claimed(db):
    receipt = await accepted(db)
    fresh = CommandStore(db)
    lease = await fresh.claim("fresh-worker", now=datetime.now(timezone.utc), command_id=receipt.id)
    assert (
        lease is not None and (await fresh.load(lease.command_id)).envelope.command.kind == "status"
    )


async def test_redelivery_keeps_one_inbox_and_one_scheduled_step(db):
    # The native receipt now is the durable execution owner, rather than two tables.
    first, second = await accepted(db), await accepted(db)
    assert first.id == second.id
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(EngineCommand)) == 1
        assert await session.scalar(select(func.count()).select_from(EngineEffect)) == 0


async def test_a_second_owner_cannot_claim_the_running_command(db):
    receipt = await accepted(db)
    store, now = CommandStore(db), datetime.now(timezone.utc)
    assert await store.claim("first", now=now, command_id=receipt.id) is not None
    assert await store.claim("second", now=now, command_id=receipt.id) is None


async def test_a_wrong_fence_cannot_complete_the_running_command(db):
    receipt = await accepted(db)
    store, now = CommandStore(db), datetime.now(timezone.utc)
    lease = await store.claim("worker", now=now, command_id=receipt.id)
    await store.resolve(lease, CommandResult(status="observed", reason="status_observed"), now=now)
    with pytest.raises(StaleLease):
        await store.complete(replace(lease, epoch=lease.epoch + 1), now=now)
    assert (await store.load(receipt.id)).state == "resolved"
    await store.complete(lease, now=now)
    assert (await store.load(receipt.id)).state == "completed"


async def test_completed_and_missing_commands_are_inert(db):
    receipt = await accepted(db)
    store, now = CommandStore(db), datetime.now(timezone.utc)
    lease = await store.claim("worker", now=now, command_id=receipt.id)
    await store.resolve(lease, CommandResult(status="observed", reason="status_observed"), now=now)
    await store.complete(lease, now=now)
    assert await store.claim("later", now=now, command_id=receipt.id) is None
    assert await store.claim("later", now=now, command_id="unknown") is None


async def test_exhausted_failure_preserves_the_error_without_claiming_success(db):
    clock = Clock()
    run, step = await execution(db, clock, max_attempts=1)

    async def fail(context):
        raise Retryable("explicit_failure")

    worker = StepRunner(
        StepStore(db), {Handler.PLAN: fail}, effects=None, clock=clock, owner="worker"
    )
    assert (await worker.tick()).parked == 1
    saved, current = await StepStore(db).load(step.id), await RunStore(db).load(run.id)
    assert saved.state == "dead" and current.state == State.BLOCKED
    assert json.loads(saved.result_json) == {"reason": "explicit_failure", "retryable": True}
    assert current.reason == "retry_exhausted:explicit_failure"


async def test_retry_is_bounded_and_due_in_the_future(db):
    clock = Clock()
    _, step = await execution(db, clock)

    async def fail(context):
        raise Retryable("temporary_outage")

    worker = StepRunner(
        StepStore(db),
        {Handler.PLAN: fail},
        effects=None,
        clock=clock,
        owner="worker",
        retry=RetryPolicy(base_seconds=10, cap_seconds=10),
    )
    assert (await worker.tick()).rescheduled == 1
    assert (await worker.tick()).claimed == 0
    saved = await StepStore(db).load(step.id)
    assert saved.attempts == 1 and saved.due_at == clock.now() + timedelta(seconds=10)
    assert json.loads(saved.result_json) == {"reason": "temporary_outage", "retryable": True}


async def test_two_wakeups_execute_the_committed_handler_once(db):
    from forge.engine.runner import StepResult

    clock = Clock()
    _, step = await execution(db, clock)
    calls = []

    async def handle(context):
        calls.append(context.lease.step_id)
        return StepResult('{"observed":"done"}')

    first = StepRunner(
        StepStore(db), {Handler.PLAN: handle}, effects=None, clock=clock, owner="first"
    )
    second = StepRunner(
        StepStore(db), {Handler.PLAN: handle}, effects=None, clock=clock, owner="second"
    )
    assert (await first.tick()).completed == 1
    assert (await second.tick()).claimed == 0
    assert calls == [step.id] and (await StepStore(db).load(step.id)).state == "succeeded"


async def test_retry_observation_cannot_replace_original_failed_handler_evidence(db):
    from forge.store.retry import RetryStore

    clock = Clock()
    run, step = await execution(db, clock)
    (lease,) = await StepStore(db).claim_due("first", clock.now(), 60)
    original = '{"actual_outcome":{"candidate":"preserved"}}'
    await StepStore(db).complete_and_enqueue(
        lease,
        original,
        now=clock.now(),
        transition_to=State.BLOCKED,
        reason="failed_before_completion",
        outcome="failed",
    )
    receipt = await accepted(db, kind="retry", now=clock.now())
    command_lease = await CommandStore(db).claim("operator", now=clock.now(), command_id=receipt.id)
    assert (
        await RetryStore(db).retry(command_lease, run.id, None, now=clock.now())
    ).reason == "retry_scheduled"

    async def temporarily_unavailable(context):
        raise Retryable("new_transient_observation")

    worker = StepRunner(
        StepStore(db),
        {Handler.PLAN: temporarily_unavailable},
        effects=None,
        clock=clock,
        owner="retry",
    )
    assert (await worker.tick()).rescheduled == 1
    assert (await StepStore(db).load(step.id)).result_json == original


@pytest.mark.parametrize(
    "observation", ['{ "not":"canonical"}', '{"message":"' + "x" * 65536 + '"}']
)
async def test_invalid_or_oversize_observation_leaves_current_step_unchanged(db, observation):
    clock = Clock()
    _, step = await execution(db, clock)
    (lease,) = await StepStore(db).claim_due("current", clock.now(), 60)
    with pytest.raises(ValueError):
        await StepStore(db).reschedule(
            lease,
            due_at=clock.now() + timedelta(seconds=1),
            now=clock.now(),
            observation_json=observation,
        )
    saved = await StepStore(db).load(step.id)
    assert saved.state == "running" and saved.result_json is None


async def test_stale_retry_cannot_record_an_observation(db):
    clock = Clock()
    _, step = await execution(db, clock)
    (lease,) = await StepStore(db).claim_due("current", clock.now(), 60)
    with pytest.raises(StaleLease):
        await StepStore(db).reschedule(
            replace(lease, epoch=lease.epoch + 1),
            due_at=clock.now() + timedelta(seconds=1),
            now=clock.now(),
            observation_json='{"reason":"stale"}',
        )
    assert (await StepStore(db).load(step.id)).result_json is None
