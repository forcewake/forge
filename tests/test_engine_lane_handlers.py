"""Actual approval, SQL effects, budget admission and current lane API receipts."""

from datetime import timedelta
from decimal import Decimal
import hashlib
import json

import httpx
import pytest
from pydantic import SecretStr

from forge.api.lane_auth import LaneAuthService
from forge.api.lane_v1_blobs import LaneBlobStore
from forge.api.lane_v1_service import LaneV1Service
from forge.budget.credentials import CredentialVault
from forge.budget.gateway import LiteLLMGateway
from forge.budget.service import BudgetService
from forge.budget.settlement import Settlement
from forge.domain.budget import Resources
from forge.domain.states import Handler, State
from forge.engine.await_lane import AwaitLaneHandler
from forge.engine.dispatch import DispatchHandler, lane_key
from forge.engine.effects import Effects
from forge.engine.lane_values import LaneSettings
from forge.port_values import Refused
from forge.providers.fake import Fault
from forge.store.budgets import BudgetStore
from forge.store.effects import EffectStore
from forge.store.gateway_keys import GatewayKeyStore
from forge.store.inbox_models import EventInbox
from forge.store.lane_auth import EngineLaneAuthStore
from forge.store.lane_v1 import EngineLaneV1Store
from forge.store.lane_work import LaneWorkStore
from forge_protocol import CandidateBundle, LaneReport, RunIdentity
from tests.test_budget_engine import NativeAdmin
from tests.test_engine_approval import env as env
from tests.test_lane_auth import settings as settings


class LocalAdmission:
    """Test-only admission result; this does not qualify a deployed gateway."""

    refusal = None

    async def readiness(self, model):
        return self.refusal


@pytest.fixture
async def case(env, settings, tmp_path):
    async with env.factory() as session, session.begin():
        await session.run_sync(
            lambda sync: EventInbox.__table__.create(sync.connection(), checkfirst=True)
        )
    native = NativeAdmin(env.clock)
    keys = GatewayKeyStore(env.factory, CredentialVault(b"synthetic-server-secret-32-bytes!!"))
    gateway = LiteLLMGateway(
        "https://gateway.invalid",
        SecretStr("synthetic-admin"),
        keys,
        env.clock,
        transport=httpx.MockTransport(native),
    )
    effects = Effects(
        EffectStore(env.factory),
        env.fake,
        env.fake,
        env.fake,
        env.fake,
        env.fake,
        gateway,
        env.clock,
    )
    admission = LocalAdmission()
    budgets = BudgetStore(env.factory)
    budget = BudgetService(
        budgets, keys, effects, Settlement(gateway, budgets, window=0), env.clock, admission
    )
    store = LaneWorkStore(env.factory)
    auth = LaneAuthService(EngineLaneAuthStore(env.factory), settings)
    api = LaneV1Service(auth, EngineLaneV1Store(env.factory), LaneBlobStore(tmp_path / "blobs"))
    policy = LaneSettings(
        server_url="https://forge.example",
        requested=Resources(usd=Decimal("2"), calls=4, tokens=2000),
        minimum=Resources(usd=Decimal("0.01"), calls=1, tokens=1),
        poll_seconds=1,
        discovery_attempts=3,
        revive_backoff_seconds=1,
    )
    env.effects = env.runner.effects = effects
    env.runner.handlers.update(
        {
            Handler.DISPATCH: DispatchHandler(store, auth, budget, env.clock, policy),
            Handler.AWAIT_LANE: AwaitLaneHandler(store, budget, env.fake, env.clock),
        }
    )
    try:
        yield dict(
            env=env,
            native=native,
            keys=keys,
            budget=budget,
            budgets=budgets,
            store=store,
            auth=auth,
            api=api,
            admission=admission,
            policy=policy,
        )
    finally:
        await gateway.aclose()


async def approved(case):
    env = case["env"]
    run, frozen = await env.planned()
    await env.approve(run.id, await env.claim(run, frozen), command_key="go")
    assert (await env.runner.tick()).completed == 1
    assert (await env.runs.load(run.id)).state == State.RUNNING
    return run, frozen


async def started(case):
    run, frozen = await approved(case)
    assert (await case["env"].runner.tick()).completed == 1  # frozen dispatch work
    tick = await case["env"].runner.tick()
    assert tick.completed == 1, (await case["env"].runs.load(run.id)).reason
    identity = RunIdentity(forge_run_id=run.id, attempt=1, generation=1)
    return run, frozen, await case["store"].pending(identity)


def advance(case, seconds=1):
    env = case["env"]
    env.clock.instant += timedelta(seconds=seconds)
    env.fake.now = env.clock.now()


async def report(
    case,
    pending,
    *,
    status="failed",
    reason="runner startup failure",
    classification="infrastructure",
    candidate=None,
):
    value = LaneReport(
        **pending.identity.model_dump(),
        approved_digest=pending.dispatch.approved_digest,
        status=status,
        classification=classification,
        driver=pending.dispatch.driver,
        model=pending.dispatch.model,
        terminal_reason=reason,
        candidate_diff_sha256=candidate,
    )
    await case["api"].report(pending.identity, value)
    return value


async def test_dispatch_lost_response_recovers_same_nonce_and_native_job(case):
    env = case["env"]
    run, frozen = await approved(case)
    assert (await env.runner.tick()).completed == 1
    env.fake.fail_next("dispatch_lane", Fault(unknown_after_success=True))
    assert (await env.runner.tick()).rescheduled == 1
    effect = await env.effects.store.get(f"{run.id}:1:1:lane:dispatch")
    nonce = json.loads(json.loads(effect.payload_json)["dispatch_json"])["nonce"]
    advance(case)
    assert (await env.runner.tick()).completed == 1
    pending = await case["store"].pending(RunIdentity(forge_run_id=run.id, attempt=1, generation=1))
    assert pending.dispatch.nonce == nonce and len(nonce) >= 32
    assert pending.brief == frozen.approved.to_brief(pending.identity, frozen.spec)
    assert pending.repository_id == "42"
    assert len(env.fake.lanes) == 1
    assert sum(call[0] == "dispatch_lane" for call in env.fake.calls) == 1
    assert case["native"].writes.count("/key/generate") == 1
    assert (await env.effects.store.get(effect.key)).state == "adopted"
    if env.fake.provider == "gitlab":
        assert pending.gitlab_pipeline_id == next(iter(env.fake.lanes.values()))[0].native_id
    else:
        assert pending.github_workflow_ref.endswith("@refs/heads/forge/" + run.id)


async def test_never_started_lane_is_bounded_without_a_second_native_dispatch(case):
    env = case["env"]
    run, _ = await approved(case)
    await env.runner.tick()
    env.fake.fail_next("dispatch_lane", Fault(http_5xx=True))
    assert (await env.runner.tick()).rescheduled == 1
    for _ in range(2):
        advance(case)
        tick = await env.runner.tick()
    assert tick.parked == 1
    current = await env.runs.load(run.id)
    assert current.state == State.BLOCKED and current.reason == "lane_discovery_exhausted"
    assert not env.fake.lanes
    assert (await case["keys"].get(f"forge:{run.id}:1:1")).state == "purged"
    assert sum(call[0] == "dispatch_lane" for call in env.fake.calls) == 1
    assert (await case["budgets"].load(run.id, "implementation")).balance.held.usd == Decimal("2")


async def test_budget_refusal_never_dispatches_or_serves_a_credential(case):
    env = case["env"]
    case["admission"].refusal = Refused(
        reason="strict_gateway_unavailable", evidence="test:unavailable"
    )
    run, _ = await approved(case)
    await env.runner.tick()
    assert (await env.runner.tick()).parked == 1
    assert (await env.runs.load(run.id)).reason == "strict_gateway_unavailable"
    assert not env.fake.lanes and not case["native"].writes
    assert await case["keys"].get(f"forge:{run.id}:1:1") is None


async def test_completed_report_consumes_only_the_current_accepted_candidate(case):
    env = case["env"]
    run, _, pending = await started(case)
    body = b"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
    digest = hashlib.sha256(body).hexdigest()

    async def chunks():
        yield body

    await case["api"].blobs.put(digest, chunks())
    await case["api"].record_blob(pending.identity, digest, len(body))
    candidate = CandidateBundle(
        **pending.identity.model_dump(),
        diff_sha256=digest,
        diff_size_bytes=len(body),
        base_sha=pending.dispatch.base_sha,
        approved_digest=pending.dispatch.approved_digest,
        driver=pending.dispatch.driver,
        model=pending.dispatch.model,
        terminal_reason="completed",
    )
    await case["api"].candidate(pending.identity, candidate)
    await report(
        case,
        pending,
        status="completed",
        reason="completed",
        classification="completed",
        candidate=digest,
    )
    assert (await env.runner.tick()).completed == 1
    current = await env.runs.load(run.id)
    assert current.state == State.PUBLISHING
    step = await env.steps.get(lane_key(pending.identity) + ":publish")
    assert candidate.diff_sha256 in step.payload_json and step.handler == Handler.PUBLISH
    assert not any(call[0] == "commit" for call in env.fake.calls)
    assert (await case["budgets"].load(run.id, "implementation")).balance.held.usd == Decimal("2")


async def test_transient_failure_atomically_advances_identity_and_reserves_again(case):
    env = case["env"]
    run, _, pending = await started(case)
    await report(case, pending)
    assert (await env.runner.tick()).completed == 1
    current = await env.runs.load(run.id)
    assert (current.state, current.attempt, current.generation) == (State.RUNNING, 2, 2)
    old_nonce = pending.dispatch.nonce
    advance(case)
    assert (await env.runner.tick()).completed == 1
    assert (await env.runner.tick()).completed == 1
    second = await case["store"].pending(RunIdentity(forge_run_id=run.id, attempt=2, generation=2))
    assert second.dispatch.nonce != old_nonce
    assert len(env.fake.lanes) == 2 and case["native"].writes.count("/key/generate") == 2
    pool = await case["budgets"].load(run.id, "implementation")
    assert len(pool.reservations) == 2 and pool.balance.held.usd == Decimal("4")


async def test_fatal_failure_preserves_reason_without_revival(case):
    env = case["env"]
    run, _, pending = await started(case)
    terminal = await report(
        case, pending, reason="driver binary is incompatible", classification="driver_failed"
    )
    assert (await env.runner.tick()).parked == 1
    current = await env.runs.load(run.id)
    assert (current.state, current.attempt, current.generation) == (State.BLOCKED, 1, 1)
    assert terminal.terminal_reason in current.reason
    assert len(env.fake.lanes) == 1


async def test_pool_too_small_for_another_attempt_is_budget_exhausted(case):
    env = case["env"]
    handler = env.runner.handlers[Handler.DISPATCH]
    handler.settings = case["policy"].model_copy(
        update={"requested": Resources(usd=Decimal("8"), calls=8, tokens=9000)}
    )
    run, _, pending = await started(case)
    await report(case, pending)
    assert (await env.runner.tick()).completed == 1
    advance(case)
    assert (await env.runner.tick()).completed == 1
    assert (await env.runner.tick()).parked == 1
    current = await env.runs.load(run.id)
    assert current.reason == "budget_exhausted" and current.state == State.BLOCKED
    assert len(env.fake.lanes) == 1
    assert (await case["budgets"].load(run.id, "implementation")).balance.held.usd == Decimal("8")


async def test_native_success_without_candidate_remains_waiting(case):
    env = case["env"]
    run, _, pending = await started(case)
    key, (handle, status) = next(iter(env.fake.lanes.items()))
    env.fake.lanes[key] = (handle, status.model_copy(update={"status": "succeeded"}))
    assert (await env.runner.tick()).rescheduled == 1
    assert (await env.runs.load(run.id)).state == State.RUNNING
    assert await env.steps.get(lane_key(pending.identity) + ":publish") is None
    assert (await case["store"].receipt(pending.identity)).candidate is None


async def test_transient_revival_rolls_back_identity_and_successor_together(case, monkeypatch):
    import forge.store.steps as step_module

    env = case["env"]
    run, _, pending = await started(case)
    await report(case, pending)
    original = step_module.enqueue_in

    async def crash_after_insert(session, row, request):
        result = await original(session, row, request)
        if request.handler == Handler.DISPATCH:
            raise RuntimeError("synthetic crash after successor insertion")
        return result

    with monkeypatch.context() as change:
        change.setattr(step_module, "enqueue_in", crash_after_insert)
        with pytest.raises(RuntimeError, match="synthetic crash"):
            await env.runner.tick()
    current = await env.runs.load(run.id)
    assert (current.state, current.attempt, current.generation) == (State.RUNNING, 1, 1)
    assert await env.steps.get(f"{run.id}:2:2:revive") is None
    parent = await env.steps.get(lane_key(pending.identity) + ":await")
    assert parent.state == "running" and parent.result_json is None
    advance(case, 61)
    assert (await env.runner.tick()).completed == 1
    assert (await env.runs.load(run.id)).generation == 2
    assert (await env.steps.get(f"{run.id}:2:2:revive")).state == "scheduled"
    assert len(env.fake.lanes) == 1


async def test_cancelled_epoch_cannot_commit_a_prepared_revival(case):
    from forge.engine.runner import LeaseHolder, StepContext
    from forge.store.records import StaleLease

    env = case["env"]
    run, _, pending = await started(case)
    await report(case, pending)
    (lease,) = await env.steps.claim_due("old-worker", env.clock.now(), 60)
    step = await env.steps.load(lease.step_id)
    result = await env.runner.handlers[Handler.AWAIT_LANE](
        StepContext(LeaseHolder(lease), step, env.effects)
    )
    assert result.restart_lane is not None
    current = await env.runs.load(run.id)
    await env.runs.request_cancel(run.id, expected_version=current.version)
    with pytest.raises(StaleLease):
        await env.steps.complete_and_enqueue(
            lease,
            result.result_json,
            result.successors,
            restart_lane=result.restart_lane,
            now=env.clock.now(),
        )
    current = await env.runs.load(run.id)
    assert current.cancel_requested and (current.attempt, current.generation) == (1, 1)
    assert await env.steps.get(f"{run.id}:2:2:revive") is None


async def test_automatic_revival_has_a_frozen_finite_limit(case):
    env = case["env"]
    env.runner.handlers[Handler.DISPATCH].settings = case["policy"].model_copy(
        update={"requested": Resources(usd=Decimal("1"), calls=1, tokens=1000)}
    )
    run, _, pending = await started(case)
    for cycle in range(3):
        await report(case, pending)
        tick = await env.runner.tick()
        if cycle == 2:
            assert tick.parked == 1
            break
        assert tick.completed == 1
        advance(case, 2)
        assert (await env.runner.tick()).completed == 1
        assert (await env.runner.tick()).completed == 1
        pending = await case["store"].pending(
            RunIdentity(forge_run_id=run.id, attempt=cycle + 2, generation=cycle + 2)
        )
    current = await env.runs.load(run.id)
    assert current.state == State.BLOCKED and (current.attempt, current.generation) == (3, 3)
    assert len(env.fake.lanes) == 3 and "runner startup failure" in current.reason


async def test_copilot_exception_is_explicit_unmetered_without_gateway_key(case):
    import json
    from sqlalchemy import select
    from forge.domain.run_spec import RunPolicy
    from forge.store.models_budget import EngineUsage

    env = case["env"]
    raw = env.intent.policy.model_dump(mode="json")
    raw.update(driver="copilot_cli", metering="copilot-scoped-unmetered")
    policy = RunPolicy.model_validate_json(json.dumps(raw))
    env.intent = env.intent.model_copy(update={"policy": policy})
    run, _, pending = await started(case)
    assert pending.dispatch.driver == "copilot_cli"
    assert not case["native"].writes
    assert await case["keys"].get(f"forge:{run.id}:1:1") is None
    async with env.factory() as session:
        usage = await session.scalar(select(EngineUsage).where(EngineUsage.run_id == run.id))
        assert usage.completeness == "unmetered" and usage.usd is None
        assert usage.calls is None and usage.tokens is None


async def test_session_pending_is_only_current_durable_dispatch_and_never_an_auth_bypass(
    case, monkeypatch
):
    from fastapi import FastAPI
    from forge.api.lane_v1 import router

    env = case["env"]
    app = FastAPI()
    app.state.lane_v1_service = case["api"]
    app.include_router(router)
    statuses = []
    original = env.fake.dispatch_lane
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://forge.example"
    ) as client:

        async def race(repo, dispatch):
            body = {
                **dispatch.model_dump(include={"forge_run_id", "attempt", "generation"}),
                "provider": dispatch.provider,
                "oidc_token": "invalid-synthetic-token",
            }
            statuses.append((await client.post("/lane/v1/session", json=body)).status_code)
            stale = {**body, "generation": dispatch.generation + 1}
            assert (await client.post("/lane/v1/session", json=stale)).status_code == 401
            unknown = {**body, "forge_run_id": "f" * 32}
            assert (await client.post("/lane/v1/session", json=unknown)).status_code == 401
            return await original(repo, dispatch)

        monkeypatch.setattr(env.fake, "dispatch_lane", race)
        _, _, pending = await started(case)
        body = {
            **pending.identity.model_dump(),
            "provider": pending.dispatch.provider,
            "oidc_token": "invalid-synthetic-token",
        }
        assert (await client.post("/lane/v1/session", json=body)).status_code == 401
    assert statuses == ([503] if env.fake.provider == "gitlab" else [401])


@pytest.mark.parametrize("response_status", [401, 403, 503])
async def test_lane_client_retries_only_pending_and_keeps_a_startup_deadline(
    case, response_status, monkeypatch
):
    import asyncio
    from forge_lane.client import LaneClient
    from forge_lane.errors import ExitCode, LaneFailure
    from forge_protocol import SessionToken

    _, _, pending = await started(case)
    calls = []

    async def no_delay(seconds):
        assert seconds == 1

    monkeypatch.setattr(asyncio, "sleep", no_delay)

    def respond(request):
        calls.append(request)
        if response_status != 503 or len(calls) <= 2:
            return httpx.Response(response_status)
        return httpx.Response(
            200,
            json=SessionToken(
                **pending.identity.model_dump(),
                access_token="synthetic-bearer",
                expires_at=case["env"].clock.now() + timedelta(seconds=60),
            ).model_dump(mode="json"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = LaneClient(pending.dispatch, http)
        if response_status == 503:
            await client.exchange("synthetic-oidc")
            assert len(calls) == 3 and client.token.access_token == "synthetic-bearer"
        else:
            with pytest.raises(LaneFailure) as caught:
                await client.exchange("synthetic-oidc")
            assert caught.value.code == ExitCode.AUTHENTICATION
            assert len(calls) == 1 and client.token is None


async def test_lane_client_pending_retry_stops_at_startup_deadline(case):
    from forge_lane.client import LaneClient
    from forge_lane.errors import ExitCode, LaneFailure

    _, _, pending = await started(case)
    dispatch = pending.dispatch.model_copy(update={"timeout_seconds": 1})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    ) as http:
        client = LaneClient(dispatch, http)
        with pytest.raises(LaneFailure) as caught:
            await client.exchange("synthetic-oidc")
        assert caught.value.code == ExitCode.TRANSPORT
        assert caught.value.classification == "session_startup_timeout" and client.token is None
