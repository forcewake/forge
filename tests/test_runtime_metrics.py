"""Scoped metrics preserve observed duration, unknown liability and effect history."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from forge.domain.budget import Observation, Pool, Resources
from forge.domain.states import Handler, State
from forge.engine.runner import StepResult, StepRunner
from forge.port_values import SubjectRef, Unknown
from forge.runtime.metrics import Metrics
from forge.store.effects import EffectStore
from forge.store.models_budget import EngineBudgetPool, EngineUsage
from forge.store.models_execution import EngineEffect, EngineStep
from forge.store.models_runs import EngineRun
from forge.store.models_timing import EngineStepTiming
from forge.store.records import RunRequest, StaleLease, StepRequest
from forge.store.runs import RunStore
from forge.store.steps import StepStore
from tests import gitlab_engine_fixture as native
from tests.fakes.ports import FixedClock
from tests.native_store_fixture import native_store as native_store
from tests.runtime_fixture import configuration, native_server
from tests.test_runtime_app import application, auth, command

SCOPE = (("github", "main", "123"),)


async def seeded(factory, *, connection="main", subject="7"):
    run = await RunStore(factory).admit(
        RunRequest(uuid4().hex, "github", connection, "123", "issue", subject)
    )
    now = datetime.now(timezone.utc)
    step = await StepStore(factory).enqueue(
        StepRequest(run.id, Handler.PLAN, run.id + ":plan", "{}", now - timedelta(days=1))
    )
    return run, step, now


async def test_execution_duration_excludes_queue_and_missing_observations(native_store):
    run, step, now = await seeded(native_store)
    async with native_store.begin() as session:
        row = await session.get(EngineStep, step.id)
        row.created_at = now - timedelta(days=1)

    async def handler(context):
        return StepResult("{}")

    result = await StepRunner(
        StepStore(native_store),
        {Handler.PLAN: handler},
        effects=None,
        clock=FixedClock(now),
        owner="metrics",
    ).tick()
    assert result.completed == 1
    async with native_store() as session:
        (timing,) = (await session.scalars(select(EngineStepTiming))).all()
        assert timing.run_id == run.id and timing.lease_epoch == 1
        assert timing.outcome == "completed" and 0 <= timing.seconds < 30
    # A manually completed step has no measured execution interval.
    _, _, other_now = await seeded(native_store, subject="8")
    (lease,) = await StepStore(native_store).claim_due("manual", other_now, 60)
    await StepStore(native_store).complete_and_enqueue(lease, "{}", now=other_now)
    rendered = await Metrics(native_store, SCOPE).render()
    assert 'forge_step_seconds_count{handler="plan"} 1\n' in rendered
    assert "queue time and missing observations are excluded" in rendered


@pytest.mark.parametrize("seconds", [-1.0, float("inf"), float("nan"), 86401.0])
async def test_invalid_observation_rolls_back_step_and_successor(native_store, seconds):
    run, step, now = await seeded(native_store)
    (lease,) = await StepStore(native_store).claim_due("metrics", now, 60)
    with pytest.raises(ValueError, match="finite bound"):
        await StepStore(native_store).complete_and_enqueue(
            lease,
            "{}",
            (StepRequest(run.id, Handler.APPROVE, "next", "{}", now),),
            now=now,
            execution_seconds=seconds,
        )
    assert (await StepStore(native_store).load(step.id)).state == "running"
    assert await StepStore(native_store).get("next") is None
    async with native_store() as session:
        assert await session.scalar(select(func.count()).select_from(EngineStepTiming)) == 0


async def test_stale_completion_does_not_create_execution_evidence(native_store):
    run, _, now = await seeded(native_store)
    (lease,) = await StepStore(native_store).claim_due("metrics", now, 60)
    await RunStore(native_store).request_cancel(run.id, expected_version=run.version)
    with pytest.raises(StaleLease):
        await StepStore(native_store).complete_and_enqueue(
            lease,
            "{}",
            now=now,
            execution_seconds=0.25,
        )
    assert "forge_step_seconds_count{" not in await Metrics(native_store, SCOPE).render()


async def test_reschedule_records_each_executed_epoch_once(native_store):
    _, _, now = await seeded(native_store)
    store = StepStore(native_store)
    (first,) = await store.claim_due("first", now, 60)
    await store.reschedule(first, due_at=now, now=now, execution_seconds=0.25)
    (second,) = await store.claim_due("second", now, 60)
    await store.complete_and_enqueue(second, "{}", now=now, execution_seconds=0.5)
    with pytest.raises(StaleLease):
        await store.complete_and_enqueue(second, "{}", now=now, execution_seconds=0.5)
    rendered = await Metrics(native_store, SCOPE).render()
    assert 'forge_step_seconds_sum{handler="plan"} 0.75\n' in rendered
    assert 'forge_step_seconds_count{handler="plan"} 2\n' in rendered


async def test_native_lost_response_adoption_preserves_counter_history_and_one_post(tmp_path):
    with native_server(tmp_path) as (peer, url, ca):
        config, values = configuration(tmp_path, native_url=url, ca=ca)

        def lost_response(request):
            body = json.loads(request.content)["body"]
            peer.routes["issues/7/notes"].append(native.note(ident=51, body=body))
            return httpx.Response(504, json={"message": "lost response"})

        peer.mutations[("POST", "issues/7/notes")] = lost_response
        async with application(config, values, peer) as (app, client, _):
            result = (await client.post("/api/v1/commands", headers=auth(), json=command())).json()
            service = app.state.runtime
            (lease,) = await StepStore(service.factory).claim_due(
                "metrics", service.budget.clock.now(), 60
            )
            repo = service.connections.registry.named("project").ref
            marker = "<!-- forge:metrics-owned-proof -->"
            outcome = await service.effects.post_comment(
                lease,
                result["run_id"] + ":metrics",
                SubjectRef(repo=repo, number=7),
                "Owned synthetic note",
                marker,
            )
            assert isinstance(outcome, Unknown)
            before = (await client.get("/metrics", headers=auth())).text
            assert 'forge_effects_total{kind="post_comment",outcome="unknown"} 1\n' in before
            assert "forge_unknown_effects 1\n" in before
            effect = await EffectStore(service.factory).get(result["run_id"] + ":metrics")
            recovered = await service.effects.recover(effect)
            assert recovered.state == "adopted", recovered.observations
            after = (await client.get("/metrics", headers=auth())).text
            assert 'forge_effects_total{kind="post_comment",outcome="unknown"} 1\n' in after
            assert 'forge_effects_total{kind="post_comment",outcome="adopted"} 1\n' in after
            assert "forge_unknown_effects 0\n" in after
            assert len([request for request in peer.calls if request.method == "POST"]) == 1


async def test_scoped_spend_reports_only_settled_cost_and_explicit_unknowns(native_store):
    visible, _, _ = await seeded(native_store)
    hidden, _, _ = await seeded(native_store, connection="private")
    capacity = Resources(usd=Decimal("20"), calls=20, tokens=200)
    allowance = Resources(usd=Decimal("2"), calls=2, tokens=20)
    pool = Pool(identity="owned", capacity=capacity)
    for key in ("planning:known", "closing:partial", "implementation:unknown"):
        pool = pool.reserve(key, allowance, Resources())
    pool = pool.observe(
        "planning:known", Observation(usd=Decimal("0.125"), calls=1, tokens=10), final=True
    ).observe("closing:partial", Observation(usd=None, calls=1, tokens=10), final=False)
    async with native_store.begin() as session:
        for run, state in ((visible, pool), (hidden, pool)):
            session.add(
                EngineBudgetPool(
                    id=uuid4().hex,
                    run_id=run.id,
                    purpose="implementation",
                    usd=capacity.usd,
                    calls=capacity.calls,
                    tokens=capacity.tokens,
                    state_json=state.model_dump_json(),
                )
            )
            session.add(
                EngineUsage(
                    id=uuid4().hex,
                    run_id=run.id,
                    source_key=run.id + ":unmetered",
                    attempt=1,
                    generation=1,
                    completeness="unmetered",
                    final=True,
                    receipt_json="{}",
                )
            )
    rendered = await Metrics(native_store, SCOPE).render()
    assert 'forge_spend_usd_total{kind="planning"} 0.125\n' in rendered
    assert 'forge_spend_usd_total{kind="closing"}' not in rendered
    assert 'forge_spend_usd_total{kind="implementation"}' not in rendered
    for kind in ("closing", "implementation"):
        assert f'forge_held_usd{{kind="{kind}"}} 2\n' in rendered
        assert f'forge_unknown_spend_reservations{{kind="{kind}"}} 1\n' in rendered
    assert "forge_unmetered_usage 1\n" in rendered
    assert 'forge_runs{provider="github",connection="main",state="queued"} 1\n' in rendered
    assert "private" not in rendered


async def test_metrics_count_complete_history_beyond_a_thousand_effects(native_store):
    run, step, _ = await seeded(native_store)
    async with native_store.begin() as session:
        for index in range(1001):
            session.add(
                EngineEffect(
                    id=uuid4().hex,
                    key="history:" + str(index),
                    kind="post_comment",
                    run_id=run.id,
                    step_id=step.id,
                    lease_epoch=1,
                    payload_json="{}",
                    payload_digest="a" * 64,
                    state="unknown",
                    observations_json=json.dumps([json.dumps({"state": "unknown"})]),
                )
            )
    rendered = await Metrics(native_store, SCOPE).render()
    assert 'forge_effects_total{kind="post_comment",outcome="unknown"} 1001\n' in rendered
    assert "forge_unknown_effects 1001\n" in rendered


async def test_metrics_deadline_refuses_incomplete_series(native_store, monkeypatch):
    import forge.runtime.metrics as metrics

    class Stalled(Metrics):
        async def timings(self, session):
            await asyncio.Future()

    monkeypatch.setattr(metrics, "QUERY_SECONDS", 0.01)
    with pytest.raises(TimeoutError):
        await Stalled(native_store, SCOPE).render()


@pytest.mark.parametrize("state", list(State))
async def test_projection_state_reason_snapshots_exclude_private_effect_documents(tmp_path, state):
    with native_server(tmp_path) as (peer, url, ca):
        config, values = configuration(tmp_path, native_url=url, ca=ca)
        async with application(config, values, peer) as (app, client, _):
            result = (await client.post("/api/v1/commands", headers=auth(), json=command())).json()
            service = app.state.runtime
            async with service.factory.begin() as session:
                row = await session.get(EngineRun, result["run_id"])
                row.state = state.value
                row.reason = (
                    "gateway_unavailable" if state in (State.BLOCKED, State.FAILED) else None
                )
                row.failed_handler = "review" if row.reason else None
                step = await session.scalar(select(EngineStep))
                session.add(
                    EngineEffect(
                        id=uuid4().hex,
                        key="public-key",
                        kind="post_comment",
                        run_id=row.id,
                        step_id=step.id,
                        lease_epoch=1,
                        payload_digest="a" * 64,
                        payload_json='{"token":"private-never-echo"}',
                        state="unknown",
                        outcome_json='{"credential":"private-never-echo"}',
                    )
                )
            response = await client.get("/api/v1/runs/" + result["run_id"], headers=auth())
            assert response.status_code == 200
            view = response.json()
            assert view["state"] == view["evidence"]["state"] == state.value
            expected = "gateway_unavailable" if state in (State.BLOCKED, State.FAILED) else None
            assert view["reason"] == view["evidence"]["reason"] == expected
            assert view["evidence"]["effects"] == [
                {"key": "public-key", "kind": "post_comment", "state": "unknown"}
            ]
            assert "private-never-echo" not in response.text
