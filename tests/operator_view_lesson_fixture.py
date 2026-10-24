"""Actual state and receipt inputs for retained view lessons; no writable view DTO."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from forge.domain.approved_input import ApprovedInput
from forge.domain.budget import Pool, Resources
from forge.domain.states import Handler, State
from forge.store.models_budget import EngineBudgetPool
from forge.store.models_runs import EngineApprovedInput, EngineGate
from forge.store.runs import RunStore
from tests.operator_surface_fixture import detail, headers, seed_run
from tests.test_domain_approved_input import spec


async def view(case, run):
    response = await detail(case, run)
    assert response.status_code == 200, response.text
    return response.json()


async def command(case, kind, *, argument="", name="project", request_id=None, **changes):
    response = await case.client.post(
        "/api/v1/commands",
        headers=headers(name),
        json={
            "repository": "project",
            "number": 7,
            "request_id": request_id or uuid4().hex,
            "command": {"kind": kind, "argument": argument},
            **changes,
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


async def closed_hint(case, run, field, value):
    before = await view(case, run)
    response = await case.client.post(
        "/api/v1/commands",
        headers=headers(),
        json={
            "repository": "project",
            "number": 7,
            "request_id": uuid4().hex,
            "command": {"kind": "status", "argument": run.id},
            field: value,
        },
    )
    assert response.status_code == 422
    assert await view(case, run) == before


async def approval(case, run, *, gate_state="open"):
    frozen = spec()
    approved = ApprovedInput.from_spec(frozen)
    await RunStore(case.service.factory).put_spec_once(
        run.id, document_json=frozen.spec_json, digest=frozen.digest
    )
    gate = EngineGate(
        id=uuid4().hex,
        run_id=run.id,
        decision_key=uuid4().hex,
        state=gate_state,
        approved_digest=approved.approved_digest,
        actor_scope_json='["42"]',
        expires_at=case.service.budget.clock.now() + timedelta(minutes=5),
    )
    row = EngineApprovedInput(
        id=uuid4().hex,
        run_id=run.id,
        revision=1,
        digest=approved.approved_digest,
        spec_digest=frozen.digest,
        base_sha=approved.base_sha,
        document_json=approved.model_dump_json(),
    )
    async with case.service.factory() as session, session.begin():
        session.add_all((row, gate))
    return approved, gate


async def pool(case, run, *, observed=None, final=False):
    capacity = Resources(usd=Decimal("2"), calls=2, tokens=100)
    value = Pool(identity=run.id + ":closing", capacity=capacity).reserve(
        "closing:1:1", capacity, Resources(calls=1)
    )
    if observed is not None or final:
        value = value.observe("closing:1:1", observed, final=final)
    async with case.service.factory() as session, session.begin():
        session.add(
            EngineBudgetPool(
                id=uuid4().hex,
                run_id=run.id,
                purpose="closing",
                usd=capacity.usd,
                calls=capacity.calls,
                tokens=capacity.tokens,
                state_json=value.model_dump_json(),
            )
        )
    return value


async def admitted(case):
    result = await command(case, "implement")
    assert result["reason"] == "implementation_queued"
    return await RunStore(case.service.factory).load(result["run_id"])


async def failed_plan(case):
    run = await admitted(case)
    return await RunStore(case.service.factory).transition(
        run.id,
        expected_version=run.version,
        target=State.FAILED,
        failed_handler=Handler.PLAN,
        reason="synthetic infrastructure",
    )


async def basic(case, state="queued", **changes):
    run = await seed_run(case, state=state, **changes)
    return run, await view(case, run)
