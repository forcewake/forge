"""Real operator commands reserve exact increments before a journal-owned cap update."""

from datetime import timedelta
from decimal import Decimal
import json

import httpx
import pytest
from sqlalchemy import select

from forge.budget.command_values import parse_amendment
from forge.command_values import CommandEnvelope
from forge.domain.budget import Resources
from forge.engine.budget_actions import BudgetActions
from forge.ingress_values import Command
from forge.store.budget_commands import BudgetCommands
from forge.store.models_budget import EngineBudgetAmendment
from forge.store.models_commands import EngineCommand
from forge.store.models_execution import EngineEffect
from forge.store.records import StoreConflict
from tests.native_budget_lesson_fixture import operator_commands
from tests import test_budget_engine as budget_engine
from tests.test_budget_engine import NativeAdmin, prepare
from tests.test_engine_lane_handlers import case as case, env as env, settings as settings, started
from tests.test_engine_delivery import delivery as delivery

budget_case = budget_engine.case


def envelope(
    case,
    run_id,
    *,
    native_id="amend-one",
    purpose="implementation",
    amount="usd=1 calls=2 tokens=100",
    **changes,
):
    return CommandEnvelope(
        source="api",
        native_id=native_id,
        actor=case["env"].intent.actor,
        target=case["env"].intent.task.subject,
        command=Command(
            kind="amend", argument=f"{run_id} {purpose} {amount} reason=continue bounded work"
        ),
        **changes,
    )


def commands_for(case):
    commands = operator_commands(case)
    action = BudgetActions(
        BudgetCommands(case["env"].factory),
        case["env"].effects,
        commands.authority,
        case["env"].clock,
    )
    commands.actions["amend"], commands.finishers["amend"] = action.amend, action.finish
    return commands


async def rows(case, model):
    async with case["env"].factory() as session:
        return list((await session.scalars(select(model))).all())


async def test_operator_amendment_is_fenced_reserved_before_http_and_applied_once(
    case, monkeypatch
):
    run, frozen, pending = await started(case)
    commands = commands_for(case)
    before = await case["budgets"].load(run.id, "implementation")
    original = NativeAdmin.__call__

    async def observe(self, request):
        if request.url.path == "/key/update":
            (row,) = await rows(case, EngineBudgetAmendment)
            (command,) = await rows(case, EngineCommand)
            effect = [item for item in await rows(case, EngineEffect) if item.kind == "gw_update"][
                0
            ]
            pool = await case["budgets"].load(run.id, "implementation")
            assert command.state == "resolved" and row.state == "authorized"
            assert (
                effect.command_id == command.id
                and effect.step_id is None
                and effect.state == "dispatched"
            )
            assert pool.balance.available == before.balance.available
            assert pool.reservations[0].allowance == Resources(
                usd=Decimal("3"), calls=6, tokens=2100
            )
        return original(self, request)

    monkeypatch.setattr(NativeAdmin, "__call__", observe)
    item = envelope(case, run.id)
    result = await commands.submit(item)
    assert result.status == "accepted" and result.reason == "budget_amendment_authorized"
    assert await commands.submit(item) == result
    (row,) = await rows(case, EngineBudgetAmendment)
    (command,) = await rows(case, EngineCommand)
    assert row.state == "applied" and command.state == "completed"
    assert case["native"].writes.count("/key/update") == 1
    assert [name for name, _ in case["env"].fake.calls].count("commit") == 0


async def test_lost_update_response_replays_exact_probe_without_reauthorizing_or_second_post(
    case, monkeypatch
):
    run, _, _ = await started(case)
    commands = commands_for(case)
    original = NativeAdmin.__call__
    lost = True

    def drop(self, request):
        nonlocal lost
        response = original(self, request)
        if request.url.path == "/key/update" and lost:
            lost = False
            self.unavailable = True
            raise httpx.ReadTimeout("synthetic response lost", request=request)
        return response

    monkeypatch.setattr(NativeAdmin, "__call__", drop)
    item = envelope(case, run.id)
    result = await commands.submit(item)
    (row,) = await rows(case, EngineBudgetAmendment)
    (command,) = await rows(case, EngineCommand)
    assert row.state == "authorized" and command.state == "resolved"
    target = row.target_json
    pool = await case["budgets"].load(run.id, "implementation")
    assert pool.balance.held.usd == 3
    case["native"].unavailable = False
    case["env"].clock.instant += timedelta(seconds=121)
    assert await commands.tick() == 1
    (row,) = await rows(case, EngineBudgetAmendment)
    (command,) = await rows(case, EngineCommand)
    assert row.state == "applied" and row.target_json == target and command.state == "completed"
    assert await commands.submit(item) == result
    assert case["native"].writes.count("/key/update") == 1
    effect = next(item for item in await rows(case, EngineEffect) if item.kind == "gw_update")
    assert effect.state == "adopted"


async def test_cancellation_after_acceptance_prevents_cap_raise_without_fake_step(case):
    run, _, _ = await started(case)
    commands = commands_for(case)

    def crash(point, record):
        if point == "after_t1" and record.kind == "gw_update":
            raise SystemExit("crash after exact command intent")

    case["env"].effects.hook = crash
    with pytest.raises(SystemExit):
        await commands.submit(envelope(case, run.id))
    case["env"].effects.hook = None
    await commands.submit(
        CommandEnvelope(
            source="api",
            native_id="cancel-one",
            actor=case["env"].intent.actor,
            target=case["env"].intent.task.subject,
            command=Command(kind="cancel", argument=run.id),
        )
    )
    case["env"].clock.instant += timedelta(seconds=121)
    assert await commands.tick() == 1
    (row,) = await rows(case, EngineBudgetAmendment)
    assert row.state == "refused" and case["native"].writes.count("/key/update") == 0
    pool = await case["budgets"].load(run.id, "implementation")
    assert pool.amendments == () and pool.reservations[0].allowance.usd == 2
    effect = next(item for item in await rows(case, EngineEffect) if item.kind == "gw_update")
    assert effect.step_id is None and effect.state == "requested"


@pytest.mark.parametrize(
    "axis",
    [
        "usd=-1 calls=0 tokens=0",
        "usd=0 calls=0 tokens=0",
        "usd=0.0000000000000000001 calls=0 tokens=0",
        "usd=1 calls=1.5 tokens=0",
        "usd=1 calls=9223372036854775808 tokens=0",
    ],
)
async def test_malformed_axes_are_persisted_refusals_without_gateway_writes(case, axis):
    run, _, _ = await started(case)
    result = await commands_for(case).submit(envelope(case, run.id, amount=axis))
    assert result.status == "refused" and result.reason == "malformed_command"
    (command,) = await rows(case, EngineCommand)
    assert command.state == "completed" and await rows(case, EngineBudgetAmendment) == []
    assert case["native"].writes.count("/key/update") == 0


async def test_approver_scope_and_closed_or_foreign_grants_cannot_authorize_updates(case):
    run, _, _ = await started(case)
    commands = commands_for(case)
    case["env"].fake.approvers.clear()
    result = await commands.submit(envelope(case, run.id))
    assert result.reason == "actor_not_authorized"
    assert await rows(case, EngineBudgetAmendment) == []
    assert case["native"].writes.count("/key/update") == 0


async def test_distinct_native_commands_add_once_but_changed_receipt_is_refused(case):
    run, _, _ = await started(case)
    commands = commands_for(case)
    item = envelope(case, run.id)
    await commands.submit(item)
    await commands.submit(envelope(case, run.id, native_id="amend-two"))
    with pytest.raises(StoreConflict, match="changed content"):
        await commands.submit(envelope(case, run.id, amount="usd=2 calls=2 tokens=100"))
    assert len(await rows(case, EngineBudgetAmendment)) == 2
    pool = await case["budgets"].load(run.id, "implementation")
    assert pool.reservations[0].allowance == Resources(usd=Decimal("4"), calls=8, tokens=2200)
    assert case["native"].writes.count("/key/update") == 2


async def test_forged_command_update_cannot_select_another_alias_or_amount(case):
    run, _, _ = await started(case)
    commands = commands_for(case)
    item = envelope(case, run.id)
    saved = await commands.store.accept(item, now=case["env"].clock.now())
    lease = await commands.store.claim(
        "proof", now=case["env"].clock.now(), seconds=120, command_id=saved.id
    )
    store = BudgetCommands(case["env"].factory)
    await store.authorize(
        lease, parse_amendment(item.command.argument), now=case["env"].clock.now()
    )
    payload = json.loads(await store.pending(lease, now=case["env"].clock.now()))
    for changes in (
        {"alias": "foreign-key"},
        {"allowance_json": Resources(usd=Decimal("999")).model_dump_json()},
    ):
        with pytest.raises(StoreConflict, match="persisted amendment"):
            await commands.authority.request_once(
                lease,
                key=f"command:{saved.id}:budget-update",
                kind="gw_update",
                payload_json=json.dumps(
                    {**payload, **changes}, sort_keys=True, separators=(",", ":")
                ),
            )
    assert case["native"].writes.count("/key/update") == 0


async def test_known_gateway_refusal_is_a_visible_terminal_amendment_state(case, monkeypatch):
    run, _, _ = await started(case)
    original = NativeAdmin.__call__

    def refuse(self, request):
        return (
            httpx.Response(403, json={"error": "refused"})
            if request.url.path == "/key/update"
            else original(self, request)
        )

    monkeypatch.setattr(NativeAdmin, "__call__", refuse)
    await commands_for(case).submit(envelope(case, run.id))
    (row,) = await rows(case, EngineBudgetAmendment)
    (command,) = await rows(case, EngineCommand)
    assert row.state == "refused" and command.state == "completed"
    from forge.store.command_runs import CommandRuns

    snapshot = json.loads(
        await CommandRuns(case["env"].factory).snapshot(await case["env"].runs.load(run.id))
    )
    assert snapshot["budget_amendments"]["latest"][0]["state"] == "refused"


async def test_closed_grant_cannot_be_reopened_by_a_new_amendment(case):
    run, _, _ = await started(case)
    commands = commands_for(case)
    await commands.submit(
        CommandEnvelope(
            source="api",
            native_id="cancel-before-amend",
            actor=case["env"].intent.actor,
            target=case["env"].intent.task.subject,
            command=Command(kind="cancel", argument=run.id),
        )
    )
    result = await commands.submit(envelope(case, run.id))
    assert result.status == "refused" and result.reason == "amendment_grant_inactive"
    assert case["native"].writes.count("/key/update") == 0


async def test_closing_increment_uses_the_current_readonly_grant_without_recalling_review(delivery):
    import asyncio
    from forge.domain.states import Handler
    from forge.port_values import ReviewVerdict
    from tests.test_engine_delivery import published, check

    run, frozen, pending, sha = await published(delivery)
    check(delivery, sha)
    assert (await delivery["env"].runner.tick()).completed == 1
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    class Waiting:
        async def review(
            self, diff, approved, *, candidate_sha, checks_passed=(), repair_context=""
        ):
            calls.append((diff, candidate_sha))
            entered.set()
            await release.wait()
            return ReviewVerdict(
                candidate_sha=candidate_sha,
                decision="approve",
                body="Exact candidate reviewed.",
                evidence="owned",
            )

    delivery["env"].runner.handlers[Handler.REVIEW].reviewer = lambda current, alias: Waiting()
    task = asyncio.create_task(delivery["env"].runner.tick())
    try:
        await asyncio.wait_for(entered.wait(), 10)
        result = await commands_for(delivery).submit(envelope(delivery, run.id, purpose="closing"))
        assert result.status == "accepted"
        pool = await delivery["budgets"].load(run.id, "closing")
        assert pool.reservations[0].allowance == Resources(usd=Decimal("3"), calls=4, tokens=1100)
        assert len(calls) == 1
    finally:
        release.set()
        await task


async def test_expired_command_owner_cannot_stage_or_dispatch_an_increment(case):
    from forge.store.records import StaleLease

    run, _, _ = await started(case)
    commands = commands_for(case)
    item = envelope(case, run.id)
    saved = await commands.store.accept(item, now=case["env"].clock.now())
    lease = await commands.store.claim(
        "expiring", now=case["env"].clock.now(), seconds=1, command_id=saved.id
    )
    case["env"].clock.instant += timedelta(seconds=2)
    with pytest.raises(StaleLease):
        await BudgetCommands(case["env"].factory).authorize(
            lease, parse_amendment(item.command.argument), now=case["env"].clock.now()
        )
    assert await rows(case, EngineBudgetAmendment) == []
    assert case["native"].writes.count("/key/update") == 0


async def test_concurrent_delivery_of_one_operator_command_updates_native_cap_once(case):
    import asyncio

    run, _, _ = await started(case)
    commands = commands_for(case)
    item = envelope(case, run.id)
    results = await asyncio.gather(commands.submit(item), commands.submit(item))
    assert any(result.reason == "budget_amendment_authorized" for result in results)
    assert (await commands.submit(item)).reason == "budget_amendment_authorized"
    assert len(await rows(case, EngineBudgetAmendment)) == 1
    assert case["native"].writes.count("/key/update") == 1


async def test_concurrent_distinct_operator_increments_never_overlap_an_unresolved_target(case):
    import asyncio

    run, _, _ = await started(case)
    commands = commands_for(case)
    results = await asyncio.gather(
        commands.submit(envelope(case, run.id, native_id="one")),
        commands.submit(envelope(case, run.id, native_id="two")),
    )
    accepted = sum(result.status == "accepted" for result in results)
    assert accepted in (1, 2)
    assert all(
        result.status == "accepted" or result.reason == "amendment_grant_inactive"
        for result in results
    )
    pool = await case["budgets"].load(run.id, "implementation")
    assert pool.reservations[0].allowance.usd == Decimal(2 + accepted)
    assert len(pool.amendments) == accepted == case["native"].writes.count("/key/update")


async def test_known_refusal_restores_only_the_unspent_staged_increment(case, monkeypatch):
    run, _, _ = await started(case)
    commands = commands_for(case)
    original = NativeAdmin.__call__

    def refuse(self, request):
        return (
            httpx.Response(403, json={"error": "refused"})
            if request.url.path == "/key/update"
            else original(self, request)
        )

    monkeypatch.setattr(NativeAdmin, "__call__", refuse)
    await commands.submit(envelope(case, run.id))
    pool = await case["budgets"].load(run.id, "implementation")
    assert pool.reservations[0].allowance == Resources(usd=Decimal("2"), calls=4, tokens=2000)
    assert pool.amendments == ()
    monkeypatch.setattr(NativeAdmin, "__call__", original)
    await commands.submit(envelope(case, run.id, native_id="explicit-new-decision"))
    assert len((await case["budgets"].load(run.id, "implementation")).amendments) == 1
    assert case["native"].writes.count("/key/update") == 1


async def test_internal_amendment_uses_the_same_exact_refusal_rollback(budget_case, monkeypatch):
    from forge.port_values import Applied, Refused

    case = budget_case
    await prepare(case)
    original = NativeAdmin.__call__

    def refuse(self, request):
        return (
            httpx.Response(403, json={"error": "refused"})
            if request.url.path == "/key/update"
            else original(self, request)
        )

    monkeypatch.setattr(NativeAdmin, "__call__", refuse)
    arguments = (
        case["lease"],
        "implementation",
        "exact-refusal",
        Resources(calls=1),
        "owner",
        "explicit",
    )
    assert isinstance(await case["service"].amend(*arguments), Refused)
    assert isinstance(await case["service"].amend(*arguments), Refused)
    pool = await case["store"].load(case["run"].id, "implementation")
    assert pool.amendments == () and pool.reservations[0].allowance.calls == 4
    monkeypatch.setattr(NativeAdmin, "__call__", original)
    assert isinstance(
        await case["service"].amend(
            case["lease"],
            "implementation",
            "new-explicit-decision",
            Resources(calls=1),
            "owner",
            "explicit",
        ),
        Applied,
    )
