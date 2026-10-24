"""Mandatory PostgreSQL crash/restart evidence over independent current worker pools.

Set FORGE_ENGINE_TEST_DATABASE_URL to an explicitly owned disposable PostgreSQL
database. An absent profile skips this capability; a configured non-PostgreSQL
profile fails. SQLite mechanics never count as PostgreSQL evidence. Provider state is an authoritative
fake shared across workers; every run, claim, approval and effect lives in SQL.
"""

import asyncio
import json
import os

import pytest
from sqlalchemy import func, select

from forge.domain.states import Handler, State
from forge.store.approval import ApprovalStore
from forge.store.models_execution import EngineCandidate, EngineEffect, EngineStep
from forge.store.models_runs import EngineApprovedInput, EngineGate, EngineRun
from forge.store.records import StoreConflict
from forge_protocol import RunIdentity
from tests.pg_engine_cluster import Cluster, WorkerDeath
from tests.test_engine_commands import commands as commands, envelope
from tests.test_engine_delivery import delivery as delivery, check, completed
from tests.test_engine_lane_handlers import advance, case as case, env as env, settings as settings


@pytest.fixture
async def cluster(delivery, settings):
    if not os.environ.get("FORGE_ENGINE_TEST_DATABASE_URL"):
        pytest.skip("PostgreSQL failure-injection profile is not configured")
    engine = delivery["env"].factory.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.fail(
            "failure injection requires the owned FORGE_ENGINE_TEST_DATABASE_URL PostgreSQL profile"
        )
    value = Cluster(delivery, settings)
    try:
        yield value
    finally:
        await value.close()


async def planned(cluster, run):
    env = cluster.case["env"]
    while (await env.runs.load(run.id)).state in {State.QUEUED, State.PLANNING}:
        assert (await env.runner.tick()).completed == 1
    frozen = await ApprovalStore(env.factory).load(run.id)
    assert frozen is not None and frozen.state == "open"
    return frozen


async def approve(cluster, run, frozen):
    env = cluster.case["env"]
    await env.approve(run.id, await env.claim(run, frozen), command_key="go")
    assert (await env.runner.tick()).completed == 1


async def lane(cluster, run):
    env = cluster.case["env"]
    assert (await env.runner.tick()).completed == 1
    assert (await env.runner.tick()).completed == 1
    return await cluster.case["store"].pending(
        RunIdentity(forge_run_id=run.id, attempt=1, generation=1)
    )


async def publication(cluster, pending):
    await completed(cluster.case, pending)
    assert (await cluster.case["env"].runner.tick()).completed == 1


async def finish(cluster, run):
    env = cluster.case["env"]
    sha = await env.fake.head(env.intent.task.subject.repo, "forge/" + run.id)
    check(cluster.case, sha)
    for _ in range(3):
        assert (await env.runner.tick()).completed == 1
    current = await env.runs.load(run.id)
    assert current.state == State.READY_FOR_HUMAN
    assert len(env.fake.drafts) == 1
    assert sum(name == "commit" for name, _ in env.fake.calls) == 1
    assert sum(name == "ensure_draft" for name, _ in env.fake.calls) == 1
    assert sum(name == "post_review" for name, _ in env.fake.calls) == 1


async def ready_from_ingress(cluster, run):
    frozen = await planned(cluster, run)
    await approve(cluster, run, frozen)
    await publication(cluster, await lane(cluster, run))
    await finish(cluster, run)


class TestS1CrashBetweenIngressAndClaim:
    async def test_s1_scheduled_step_survives_and_survivor_completes_the_run(self, cluster):
        env = cluster.case["env"]
        victim = cluster.worker("victim")
        run = await env.start()
        await victim.close()
        survivor = cluster.worker("survivor")
        assert survivor.engine.pool is not victim.engine.pool
        cluster.use(survivor)
        await ready_from_ingress(cluster, run)
        assert sum(name == "plan" for name, _ in env.planner.calls) == 1


class TestS2CrashMidCommandStep:
    async def test_s2b_crash_after_claim_before_effect_recovers_via_lease_expiry(self, cluster):
        env = cluster.case["env"]
        victim, survivor = cluster.worker("victim"), cluster.worker("survivor")
        run = await env.start()
        (lease,) = await victim.runner.store.claim_due("victim", env.clock.now(), 60)
        await victim.close()
        assert (await survivor.runner.tick()).claimed == 0
        advance(cluster.case, 61)
        cluster.use(survivor)
        await ready_from_ingress(cluster, run)
        original = await survivor.runner.store.load(lease.step_id)
        assert original.lease_epoch == 2 and original.state == "succeeded"

    async def test_s2c_crash_after_gate_consumption_recovers_without_recompute_side_effects(
        self, cluster
    ):
        env = cluster.case["env"]
        victim = cluster.worker("victim", after_completion=Handler.APPROVE)
        cluster.use(victim)
        run = await env.start()
        frozen = await planned(cluster, run)
        with pytest.raises(WorkerDeath):
            await approve(cluster, run, frozen)
        async with victim.factory() as session:
            assert await session.scalar(select(func.count()).select_from(EngineApprovedInput)) == 1
            gate = await session.scalar(select(EngineGate))
            assert gate.state == "consumed"
        survivor = cluster.worker("survivor")
        cluster.use(survivor)
        await publication(cluster, await lane(cluster, run))
        await finish(cluster, run)
        assert sum(name == "plan" for name, _ in env.planner.calls) == 1


class TestS3CrashAfterCommit:
    async def test_s3_recovery_adopts_the_existing_commit_and_never_commits_twice(self, cluster):
        env = cluster.case["env"]

        def kill(point, effect):
            if point == "after_t3" and effect.kind == "commit":
                raise WorkerDeath("native commit and outcome exist; step completion does not")

        victim = cluster.worker("victim", hook=kill)
        cluster.use(victim)
        run = await env.start()
        await approve(cluster, run, await planned(cluster, run))
        await completed(cluster.case, await lane(cluster, run))
        with pytest.raises(WorkerDeath):
            await victim.runner.tick()
        key = f"{run.id}:1:1:publish:commit"
        effect = await victim.runner.effects.store.get(key)
        original_sha = json.loads(effect.outcome_json)["sha"]
        assert effect.state == "committed"
        advance(cluster.case, 61)
        survivor = cluster.worker("survivor")
        cluster.use(survivor)
        assert (await survivor.runner.tick()).completed == 1
        assert (await survivor.runner.effects.store.get(key)).outcome_json == effect.outcome_json
        assert await env.fake.head(env.intent.task.subject.repo, "forge/" + run.id) == original_sha
        await finish(cluster, run)


class TestS4CrashInPublishingLeg:
    async def test_s4e_crash_after_mr_creation_adopts_the_journaled_mr(self, cluster):
        env = cluster.case["env"]

        def kill(point, effect):
            if point == "after_t3" and effect.kind == "ensure_draft":
                raise WorkerDeath("draft exists; publication step is incomplete")

        victim = cluster.worker("victim", hook=kill)
        cluster.use(victim)
        run = await env.start()
        await approve(cluster, run, await planned(cluster, run))
        await completed(cluster.case, await lane(cluster, run))
        with pytest.raises(WorkerDeath):
            await victim.runner.tick()
        assert len(env.fake.drafts) == 1
        advance(cluster.case, 61)
        survivor = cluster.worker("survivor")
        cluster.use(survivor)
        assert (await survivor.runner.tick()).completed == 1
        await finish(cluster, run)

    async def test_s4f_crash_after_ready_before_evidence_note_recovers_exactly_one_note(
        self, cluster
    ):
        # Closing evidence is now published before readiness, removing the old gap.
        env = cluster.case["env"]
        victim = cluster.worker("victim", after_completion=Handler.READY)
        cluster.use(victim)
        run = await env.start()
        await approve(cluster, run, await planned(cluster, run))
        await publication(cluster, await lane(cluster, run))
        sha = await env.fake.head(env.intent.task.subject.repo, "forge/" + run.id)
        check(cluster.case, sha)
        assert (await victim.runner.tick()).completed == 1
        assert (await victim.runner.tick()).completed == 1
        with pytest.raises(WorkerDeath):
            await victim.runner.tick()
        assert (await env.runs.load(run.id)).state == State.READY_FOR_HUMAN
        survivor = cluster.worker("survivor")
        assert (await survivor.runner.tick()).claimed == 0
        assert sum(name == "post_review" for name, _ in env.fake.calls) == 1
        async with survivor.factory() as session:
            row = await session.scalar(
                select(EngineEffect).where(EngineEffect.kind == "post_review")
            )
            assert row.state == "committed" and row.outcome_json is not None


class TestS5CancelRacesInFlightProposal:
    async def test_s5_cancelled_run_never_publishes(self, cluster, commands, monkeypatch):
        env = cluster.case["env"]
        victim, survivor = cluster.worker("victim"), cluster.worker("survivor")
        cluster.use(victim)
        run = await env.start()
        await approve(cluster, run, await planned(cluster, run))
        await completed(cluster.case, await lane(cluster, run))
        store = victim.runner.handlers[Handler.PUBLISH].store
        original = store.validated

        async def cancel_before_native_write(*args, **kwargs):
            await original(*args, **kwargs)
            await commands["commands"].submit(envelope(commands, "cancel"))

        monkeypatch.setattr(store, "validated", cancel_before_native_write)
        assert (await victim.runner.tick()).lost == 1
        assert (await survivor.runner.tick()).claimed == 0
        assert (await env.runs.load(run.id)).state == State.CANCELLED
        assert not env.fake.drafts and not any(name == "commit" for name, _ in env.fake.calls)
        async with survivor.factory() as session:
            candidate = await session.scalar(select(EngineCandidate))
            assert candidate.published_sha is None


class TestS6ConcurrentDuplicateImplement:
    async def test_s6_ten_concurrent_ingresses_yield_exactly_one_active_run(self, cluster):
        env = cluster.case["env"]
        outcomes = await asyncio.gather(*(env.start() for _ in range(10)), return_exceptions=True)
        failures = [value for value in outcomes if isinstance(value, Exception)]
        assert len(failures) == 9 and all(isinstance(value, StoreConflict) for value in failures)
        (run,) = [value for value in outcomes if not isinstance(value, Exception)]
        survivor = cluster.worker("survivor")
        cluster.use(survivor)
        async with survivor.factory() as session:
            assert await session.scalar(select(func.count()).select_from(EngineRun)) == 1
            assert await session.scalar(select(func.count()).select_from(EngineStep)) == 1
        await ready_from_ingress(cluster, run)
