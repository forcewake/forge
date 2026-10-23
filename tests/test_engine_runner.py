"""Durable runner recovery against real SQLite and optional owned PostgreSQL."""

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from forge.domain.states import Handler, State
from forge.engine.reconciler import Reconciler
from forge.engine.runner import Park, Retryable, RetryPolicy, StepResult, StepRunner
from forge.engine.wakeups import Wakeups
from forge.engine.worker import EngineWorker
from forge.store.effects import EffectStore
from forge.store.records import StepRequest, StoreConflict
from forge.store.recovery import RecoveryStore
from forge.store.runs import RunStore
from forge.store.steps import StepStore
from forge.store.transactions import notify_steps
from tests.test_engine_store import intent, request as run_request, store as store


@dataclass
class ManualClock:
    current: datetime

    def now(self):
        return self.current

    def advance(self, seconds):
        self.current += timedelta(seconds=seconds)


class RealClock:
    def now(self):
        return datetime.now(timezone.utc)


async def seeded(factory, clock, *, max_attempts=3):
    run = await RunStore(factory).admit(run_request())
    step = await StepStore(factory).enqueue(
        StepRequest(
            run.id, Handler.PLAN, f"{run.id}:plan", "{}", clock.now(), max_attempts=max_attempts
        )
    )
    return run, step


def runner(factory, clock, handler, *, owner="one", **kwargs):
    return StepRunner(
        StepStore(factory),
        {Handler.PLAN: handler},
        effects=None,
        clock=clock,
        owner=owner,
        **kwargs,
    )


async def test_competing_runners_execute_one_handler_and_one_atomic_successor(store):
    clock = RealClock()
    run, step = await seeded(store, clock)
    calls = []

    async def handler(ctx):
        calls.append(ctx.lease.epoch)
        return StepResult(
            '{"finished":true}',
            (StepRequest(run.id, Handler.APPROVE, "next", "{}", clock.now()),),
            next_state=State.PLANNING,
        )

    ticks = await asyncio.gather(
        runner(store, clock, handler).tick(), runner(store, clock, handler, owner="two").tick()
    )
    assert sum(t.claimed for t in ticks) == sum(t.completed for t in ticks) == 1
    assert calls == [1]
    assert (await StepStore(store).load(step.id)).result_json == '{"finished":true}'
    assert (await StepStore(store).get("next")).state == "scheduled"
    assert (await RunStore(store).load(run.id)).state == State.PLANNING


async def test_lease_expiring_mid_handler_refuses_result_and_successors(store):
    clock = ManualClock(datetime.now(timezone.utc))
    run, step = await seeded(store, clock)

    async def handler(ctx):
        clock.advance(61)
        return StepResult(
            "{}", (StepRequest(run.id, Handler.APPROVE, "forbidden", "{}", clock.now()),)
        )

    tick = await runner(store, clock, handler).tick()
    assert tick.lost == 1 and tick.completed == 0
    assert (await StepStore(store).load(step.id)).result_json is None
    assert await StepStore(store).get("forbidden") is None


async def test_renewal_updates_context_lease_for_effects_and_final_commit(store):
    clock = RealClock()
    _, step = await seeded(store, clock)
    observed = []

    async def handler(ctx):
        observed.append(ctx.lease.expires_at)
        await asyncio.sleep(0.14)
        observed.append(ctx.lease.expires_at)
        return StepResult("{}")

    tick = await runner(store, clock, handler, lease_seconds=0.12, renew_seconds=0.02).tick()
    assert tick.completed == 1 and observed[1] > observed[0]
    assert (await StepStore(store).load(step.id)).lease_epoch == 1


async def test_cancellation_between_handler_and_result_creates_no_successor(store):
    clock = RealClock()
    run, step = await seeded(store, clock)

    async def handler(ctx):
        await RunStore(store).request_cancel(run.id, expected_version=run.version)
        return StepResult(
            "{}", (StepRequest(run.id, Handler.APPROVE, "forbidden", "{}", clock.now()),)
        )

    tick = await runner(store, clock, handler).tick()
    assert tick.lost == 1 and (await StepStore(store).load(step.id)).result_json is None
    assert await StepStore(store).get("forbidden") is None


async def test_retryable_has_bounded_due_backoff_and_atomic_exhaustion(store):
    clock = ManualClock(datetime.now(timezone.utc))
    run, step = await seeded(store, clock)

    async def handler(ctx):
        raise Retryable("provider_unavailable")

    worker = runner(store, clock, handler)
    assert (await worker.tick()).rescheduled == 1
    assert (await StepStore(store).load(step.id)).due_at == clock.now() + timedelta(seconds=60)
    clock.advance(59)
    assert (await worker.tick()).claimed == 0
    clock.advance(1)
    assert (await worker.tick()).rescheduled == 1
    assert (await StepStore(store).load(step.id)).due_at == clock.now() + timedelta(seconds=120)
    clock.advance(120)
    assert (await worker.tick()).parked == 1
    assert (await StepStore(store).load(step.id)).state == "dead"
    parked = await RunStore(store).load(run.id)
    assert parked.state == State.BLOCKED and parked.failed_handler == Handler.PLAN
    assert parked.reason == "retry_exhausted:provider_unavailable"
    assert RetryPolicy().delay(99999) == 900


async def test_unknown_effect_is_a_terminal_park_not_an_automatic_retry(store):
    clock = RealClock()
    run, step = await seeded(store, clock)

    async def handler(ctx):
        return StepResult('{"effect":"unknown"}', park=Park("effect_unknown"))

    worker = runner(store, clock, handler)
    assert (await worker.tick()).parked == 1
    assert (await worker.tick()).claimed == 0
    assert (await StepStore(store).load(step.id)).state == "succeeded"
    parked = await RunStore(store).load(run.id)
    assert (
        parked.state == State.BLOCKED
        and parked.failed_handler == Handler.PLAN
        and parked.reason == "effect_unknown"
    )


async def test_handler_errors_park_with_nonsecret_reason_and_never_retry(store):
    clock = RealClock()
    run, step = await seeded(store, clock)

    async def handler(ctx):
        raise ValueError("secret-sentinel-never-persist")

    assert (await runner(store, clock, handler).tick()).parked == 1
    receipt = await StepStore(store).load(step.id)
    parked = await RunStore(store).load(run.id)
    assert receipt.state == "failed" and "secret-sentinel" not in receipt.result_json
    assert parked.reason == "handler_error:ValueError"


async def test_worker_cancellation_leaves_reclaimable_step_without_false_result(store):
    clock = RealClock()
    _, step = await seeded(store, clock)
    entered = asyncio.Event()

    async def handler(ctx):
        entered.set()
        await asyncio.Event().wait()
        return StepResult("{}")

    task = asyncio.create_task(runner(store, clock, handler).tick())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    persisted = await StepStore(store).load(step.id)
    assert persisted.state == "running" and persisted.result_json is None


async def test_crash_during_successor_insert_rolls_back_result_and_all_children(store):
    clock = RealClock()
    run, step = await seeded(store, clock)

    class InjectedCrash(BaseException):
        pass

    async def handler(ctx):
        return StepResult(
            '{"done":true}',
            (StepRequest(run.id, Handler.APPROVE, "crash-child", "{}", clock.now()),),
        )

    engine = store.kw["bind"].sync_engine

    def crash(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO eng_steps") and "crash-child" in str(parameters):
            raise InjectedCrash()

    event.listen(engine, "before_cursor_execute", crash)
    try:
        with pytest.raises(InjectedCrash):
            await runner(store, clock, handler).tick()
    finally:
        event.remove(engine, "before_cursor_execute", crash)
    persisted = await StepStore(store).load(step.id)
    assert persisted.state == "running" and persisted.result_json is None
    assert await StepStore(store).get("crash-child") is None


async def test_reconciler_recovers_original_requested_step_after_t1_crash(store):
    clock = ManualClock(datetime.now(timezone.utc))
    _, step = await seeded(store, clock)
    (old,) = await StepStore(store).claim_due("crashed", clock.now(), 60)
    effect = await intent(store, old)
    clock.advance(61)
    probe_calls = []

    async def probe(record):
        probe_calls.append(record.id)
        return record

    reconciler = Reconciler(EffectStore(store), RecoveryStore(store), probe=probe, clock=clock)
    result = await reconciler.pass_once()
    assert result.requeued == 1 and not probe_calls
    assert (await StepStore(store).load(step.id)).lease_epoch == 1

    async def handler(ctx):
        same = await intent(store, ctx.lease)
        assert same.id == effect.id and ctx.lease.step_id == old.step_id and ctx.lease.epoch == 2
        dispatched = await EffectStore(store).mark_dispatched(
            same.id, ctx.lease, expected_version=same.version
        )
        await EffectStore(store).record_observation(
            dispatched.id, state="committed", outcome_json="{}"
        )
        return StepResult("{}")

    assert (await runner(store, clock, handler).tick()).completed == 1
    assert (await EffectStore(store).get(effect.key)).state == "committed"


async def test_probe_absence_handoff_requeues_same_step_and_respects_cas(store):
    clock = ManualClock(datetime.now(timezone.utc))
    _, step = await seeded(store, clock)
    (old,) = await StepStore(store).claim_due("crashed", clock.now(), 60)
    effect = await intent(store, old)
    dispatched = await EffectStore(store).mark_dispatched(
        effect.id, old, expected_version=effect.version
    )
    clock.advance(61)

    async def probe(record):
        assert record.id == dispatched.id and clock.now() > record.dispatch_deadline_at
        return await EffectStore(store).resolve_probe(
            record.id,
            expected_version=record.version,
            state="requested",
            evidence_json='{"definitely_absent":true}',
        )

    reconciler = Reconciler(EffectStore(store), RecoveryStore(store), probe=probe, clock=clock)
    result = await reconciler.pass_once()
    assert result.requeued == 1
    assert (await StepStore(store).load(step.id)).id == old.step_id
    with pytest.raises(StoreConflict):
        await RecoveryStore(store).requeue_original(dispatched, now=clock.now())


async def test_recovery_refuses_live_leases_parked_runs_and_new_identities(store):
    clock = ManualClock(datetime.now(timezone.utc))
    run, _ = await seeded(store, clock)
    (old,) = await StepStore(store).claim_due("live", clock.now(), 60)
    effect = await intent(store, old)
    assert not await RecoveryStore(store).requeue_original(effect, now=clock.now())
    await RunStore(store).request_cancel(run.id, expected_version=run.version)
    clock.advance(61)
    assert not await RecoveryStore(store).requeue_original(effect, now=clock.now())
    cancelled = await RunStore(store).load(run.id)
    await RunStore(store).transition(
        run.id, expected_version=cancelled.version, target=State.CANCELLED
    )
    other, _ = await seeded(store, clock)
    (prior,) = await StepStore(store).claim_due("prior-generation", clock.now(), 60)
    previous = await intent(store, prior, key="prior-generation-effect")
    blocked = await RunStore(store).transition(
        other.id, expected_version=other.version, target=State.BLOCKED, failed_handler=Handler.PLAN
    )
    assert not await RecoveryStore(store).requeue_original(previous, now=clock.now())
    resumed = await RunStore(store).resume(
        other.id, expected_version=blocked.version, handler=Handler.PLAN
    )
    assert resumed.generation > prior.generation
    clock.advance(61)
    assert not await RecoveryStore(store).requeue_original(previous, now=clock.now())


async def test_polling_failures_and_foreign_recovery_do_not_claim_success(store):
    clock = ManualClock(datetime.now(timezone.utc))
    _, _ = await seeded(store, clock)
    (old,) = await StepStore(store).claim_due("live", clock.now(), 60)
    effect = await intent(store, old)
    await EffectStore(store).mark_dispatched(effect.id, old, expected_version=effect.version)

    async def probe(record):
        return replace(record, id="foreign")

    calls = []

    async def okay():
        calls.append("poll")

    async def unavailable():
        raise OSError("hidden detail")

    result = await Reconciler(
        EffectStore(store),
        RecoveryStore(store),
        probe=probe,
        clock=clock,
        pollers=(okay, unavailable),
    ).pass_once()
    assert result.conflicts == result.unavailable == result.polled == 1 and calls == ["poll"]


async def test_postgres_notice_is_after_commit_and_poll_fallback_always_wakes(store):
    engine = store.kw["bind"]
    wake = Wakeups(0.3 if engine.dialect.name == "postgresql" else 0.03)
    async with wake.listen(engine.url.render_as_string(hide_password=False)):
        if engine.dialect.name == "postgresql":
            waiting = asyncio.create_task(wake.wait())
            async with store.begin() as session:
                await notify_steps(session)
                await asyncio.sleep(0.005)
                assert not waiting.done()
            await asyncio.wait_for(waiting, 0.5)
        else:
            await asyncio.wait_for(wake.wait(), 0.1)
    wake.signal()
    await asyncio.wait_for(wake.wait(), 0.1)


async def test_worker_serves_new_engine_steps_and_stops_without_redis(store):
    clock = RealClock()
    _, step = await seeded(store, clock)
    stop = asyncio.Event()

    async def handler(ctx):
        stop.set()
        return StepResult("{}")

    async def probe(record):
        return record

    worker = EngineWorker(
        runner(store, clock, handler),
        Reconciler(EffectStore(store), RecoveryStore(store), probe=probe, clock=clock),
        Wakeups(0.01),
    )
    await asyncio.wait_for(worker.serve(stop), 1)
    assert (await StepStore(store).load(step.id)).state == "succeeded"


async def test_repeated_worker_crashes_exhaust_into_durable_owner_park(store):
    clock = ManualClock(datetime.now(timezone.utc))
    run, step = await seeded(store, clock, max_attempts=1)
    await StepStore(store).claim_due("crashed", clock.now(), 60)
    clock.advance(61)

    async def handler(ctx):
        pytest.fail("exhausted step must never run again")

    assert (await runner(store, clock, handler).tick()).claimed == 0
    assert (await StepStore(store).load(step.id)).state == "dead"
    parked = await RunStore(store).load(run.id)
    assert parked.state == State.BLOCKED and parked.failed_handler == Handler.PLAN
    assert parked.reason == "step_attempts_exhausted"


async def test_lost_lease_renewal_cancels_handler_and_records_no_result(store):
    clock = RealClock()
    run, step = await seeded(store, clock)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def handler(ctx):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return StepResult("{}")

    task = asyncio.create_task(
        runner(store, clock, handler, lease_seconds=0.3, renew_seconds=0.01).tick()
    )
    await entered.wait()
    await RunStore(store).request_cancel(run.id, expected_version=run.version)
    tick = await asyncio.wait_for(task, 1)
    assert tick.lost == 1 and cancelled.is_set()
    assert (await StepStore(store).load(step.id)).result_json is None
