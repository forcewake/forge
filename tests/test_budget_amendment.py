"""Amendments change actual per-call enforcement, stay exact and recover by journal probes."""

import asyncio
from datetime import timedelta
from decimal import Decimal
import os

import pytest
from pydantic import ValidationError

from forge.domain.budget import BudgetConflict, BudgetExhausted, Resources, partition
from forge.port_values import Applied
from forge.store.budget_calls import ModelCallStore
from forge.store.budgets import BudgetStore
from forge.store.models_budget import EngineBudgetAmendment, EngineUsage
from forge.store.records import StaleLease, StoreConflict
from tests.native_amendment_fixture import (
    amend,
    call,
    closing_grant,
    model_client,
    postgres_case,
    rows,
)
from tests.native_budget_lesson_fixture import resources
from tests.test_budget_engine import case as case, prepare
from tests.test_domain_approved_input import spec


class TestAmendmentOpensTheRealGuard:
    async def test_a_calls_amendment_permits_exactly_one_more_reviewer_call(self, case):
        grant = await closing_grant(case)
        async with model_client(case, grant.value.alias) as (client, requests):
            await call(client, grant.value.alias)
            with pytest.raises(BudgetExhausted):
                await call(client, grant.value.alias)
            assert len(requests) == 1
            assert isinstance(await amend(case, purpose="closing"), Applied)
            await call(client, grant.value.alias)
            with pytest.raises(BudgetExhausted):
                await call(client, grant.value.alias)
            assert len(requests) == 2

    async def test_an_unchanged_usd_cap_does_not_silently_permit_calls(self, case):
        grant = await closing_grant(case)
        async with model_client(case, grant.value.alias) as (client, requests):
            await call(client, grant.value.alias)
            await amend(case, amount=Resources(usd=Decimal("1")), purpose="closing")
            with pytest.raises(BudgetExhausted):
                await call(client, grant.value.alias)
            assert len(requests) == 1
            latest = await case["keys"].get(grant.value.alias)
            assert latest.reference.allowance.usd == 3 and latest.reference.allowance.calls == 1

    async def test_a_tokens_amendment_opens_the_token_axis_only(self, case):
        grant = await closing_grant(case, calls=2, tokens=515)
        async with model_client(case, grant.value.alias) as (client, requests):
            await call(client, grant.value.alias)
            with pytest.raises(BudgetExhausted):
                await call(client, grant.value.alias)
            await amend(case, amount=Resources(tokens=14), purpose="closing")
            await call(client, grant.value.alias)
            latest = await case["keys"].get(grant.value.alias)
            assert latest.reference.allowance == resources(2, 529, "2")
            assert len(requests) == 2


class TestCommandIdentity:
    async def test_the_durable_row_carries_the_limit_history(self, case):
        await prepare(case)
        await amend(case)
        (row,) = await rows(case, EngineBudgetAmendment)
        assert row.command_key == "native:42:1" and row.actor == "human:alice"
        assert row.reason == "authorized review" and row.state == "applied"
        assert Resources.model_validate_json(row.amount_json) == Resources(calls=1)
        assert Resources.model_validate_json(row.target_json) == resources(5, 2000, "2")

    async def test_a_redelivery_of_one_command_applies_once(self, case):
        await prepare(case)
        first, second = await amend(case), await amend(case)
        assert first == second
        assert len(await rows(case, EngineBudgetAmendment)) == 1
        assert case["native"].writes.count("/key/update") == 1

    async def test_two_identical_amount_reason_commands_are_two_decisions(self, case):
        await prepare(case)
        await amend(case, "native:42:1")
        await amend(case, "native:42:2")
        assert len(await rows(case, EngineBudgetAmendment)) == 2
        assert (await case["store"].load(case["run"].id, "implementation")).reservations[
            0
        ].allowance.calls == 6

    @pytest.mark.parametrize("command", ["", " ", "x\n", "x" * 301])
    async def test_an_amendment_requires_its_command_identity(self, case, command):
        await prepare(case)
        with pytest.raises(ValueError, match="command identity"):
            await amend(case, command)
        assert await rows(case, EngineBudgetAmendment) == []
        assert case["native"].writes == ["/key/generate"]

    async def test_the_axis_is_named_and_never_converted(self, case):
        await prepare(case)
        await amend(case, amount=Resources(tokens=3))
        amount = Resources.model_validate_json(
            (await rows(case, EngineBudgetAmendment))[0].amount_json
        )
        assert amount == Resources(tokens=3) and amount.usd == 0 and amount.calls == 0
        with pytest.raises(ValidationError):
            Resources(calls=1.5)

    async def test_the_usd_amendment_does_not_need_a_guard_row(self, case):
        await prepare(case)
        await amend(case, amount=Resources(usd=Decimal(".5")))
        # One native gateway reference is the enforcing authority; no retired guard row exists.
        (row,) = await rows(case, EngineBudgetAmendment)
        assert row.state == "applied" and Resources.model_validate_json(
            row.target_json
        ).usd == Decimal("2.5")


class TestRefusedAmendments:
    async def test_a_closed_budget_refuses_and_is_recorded(self, case):
        await prepare(case)
        await case["service"].finish(case["lease"], "implementation")
        with pytest.raises(StoreConflict, match="open gateway reservation"):
            await amend(case)
        assert await rows(case, EngineBudgetAmendment) == []
        assert case["native"].writes == ["/key/generate", "/key/block"]

    async def test_an_unlimited_axis_refuses_naming_the_axis(self, case):
        # The new contract has finite integer axes. Unlimited/null input is rejected at freeze.
        with pytest.raises(ValidationError):
            Resources.model_validate_json('{"usd":"1","calls":null,"tokens":1}')
        assert case["native"].writes == []

    async def test_a_wallclock_extension_still_in_the_past_refuses(self, case):
        await prepare(case)
        case["clock"].instant += timedelta(seconds=601)
        with pytest.raises(StaleLease):
            await amend(case)
        with pytest.raises(ValidationError):
            Resources.model_validate_json('{"wallclock_seconds":100}')

    async def test_a_redelivery_replays_the_recorded_refusal(self, case):
        await prepare(case)
        await case["service"].finish(case["lease"], "implementation")
        for _ in range(2):
            with pytest.raises(StoreConflict):
                await amend(case)
        pool = await case["store"].load(case["run"].id, "implementation")
        assert not pool.amendments and case["native"].writes.count("/key/update") == 0

    async def test_refused_usd_amendments_never_count(self, case):
        await prepare(case)
        with pytest.raises(ValueError):
            await amend(case, amount=Resources(usd=Decimal("1")), reason=" ")
        assert (await case["store"].load(case["run"].id, "implementation")).amendments == ()


class TestClosingPartition:
    async def test_the_coder_cannot_reserve_the_protected_share(self, case):
        await case["store"].initialize(case["lease"], case["frozen"], now=case["clock"].now())
        capacity = partition(case["frozen"].limits, case["frozen"].closing_reserve).implementation
        await case["store"].reserve(
            case["lease"], "implementation", "coding", capacity, capacity, now=case["clock"].now()
        )
        with pytest.raises(BudgetExhausted):
            await case["store"].reserve(
                case["lease"],
                "implementation",
                "over",
                resources(1, 1, ".01"),
                resources(1, 1, ".01"),
                now=case["clock"].now(),
            )
        assert (await case["store"].load(case["run"].id, "closing")).balance.available == case[
            "frozen"
        ].closing_reserve

    async def test_the_token_share_is_partitioned_too(self, case):
        await case["store"].initialize(case["lease"], case["frozen"], now=case["clock"].now())
        closing = await case["store"].load(case["run"].id, "closing")
        coding = await case["store"].load(case["run"].id, "implementation")
        assert closing.capacity.tokens == 1000 and coding.capacity.tokens == 9000

    async def test_no_partition_keeps_the_shared_axes(self, case):
        zero = spec(closing_reserve=Resources())
        result = partition(zero.limits, zero.closing_reserve)
        assert result.implementation.usd == 10 and result.implementation.calls == 20
        assert result.closing == Resources()


class TestReceiptCostColumns:
    async def test_a_legacy_raw_cost_is_classified_explicitly(self, case):
        grant = await closing_grant(case)
        source = await ModelCallStore(case["factory"]).admit(
            case["lease"], grant.value.alias, "test-model", 100, now=case["clock"].now()
        )
        await ModelCallStore(case["factory"]).observe(source, None)
        (row,) = await rows(case, EngineUsage)
        assert row.completeness == "unknown" and row.usd is None and not row.final

    async def test_the_canonical_lineage_lands(self, case):
        grant = await closing_grant(case)
        async with model_client(case, grant.value.alias) as (client, requests):
            await call(client, grant.value.alias)
        (row,) = await rows(case, EngineUsage)
        assert (row.run_id, row.attempt, row.generation, row.alias) == (
            case["run"].id,
            1,
            1,
            grant.value.alias,
        )
        assert row.completeness == "exact" and row.final and len(requests) == 1

    async def test_a_garbage_basis_never_lands(self, case):
        grant = await closing_grant(case)
        source = await ModelCallStore(case["factory"]).admit(
            case["lease"], grant.value.alias, "test-model", 100, now=case["clock"].now()
        )
        with pytest.raises(AttributeError):
            await ModelCallStore(case["factory"]).observe(source, {"cost_basis": "invented"})
        (row,) = await rows(case, EngineUsage)
        assert row.completeness == "unknown" and row.usd is None

    async def test_the_counters_and_the_cost_describe_one_observation(self, case):
        grant = await closing_grant(case)
        async with model_client(case, grant.value.alias) as (client, requests):
            reply = await call(client, grant.value.alias)
        (row,) = await rows(case, EngineUsage)
        assert row.usd == reply.usage.usd == Decimal(".00003")
        assert row.calls == reply.usage.calls == 1 and row.tokens == reply.usage.tokens == 14


class TestRecovery:
    async def test_a_restart_loses_neither_the_amendment_nor_runs_the_review_twice(self, case):
        grant = await closing_grant(case)
        await amend(case, purpose="closing")
        rebuilt = BudgetStore(case["factory"])
        assert (await rebuilt.load(case["run"].id, "closing")).reservations[0].allowance.calls == 2
        async with model_client(case, grant.value.alias) as (client, requests):
            await call(client, grant.value.alias)
        assert len(requests) == 1 and len(await rows(case, EngineBudgetAmendment)) == 1
        assert await amend(case, purpose="closing")
        assert case["native"].writes.count("/key/update") == 1

    async def test_a_lost_review_response_is_not_a_new_amendment(self, case):
        grant = await closing_grant(case)
        async with model_client(case, grant.value.alias, fail=True) as (client, requests):
            with pytest.raises(ValueError):
                await call(client, grant.value.alias)
        assert len(requests) == 1 and await rows(case, EngineBudgetAmendment) == []
        (row,) = await rows(case, EngineUsage)
        assert row.usd is None and not row.final
        assert (await case["store"].load(case["run"].id, "closing")).balance.held.usd == 2


class TestRealPostgresRaces:
    @pytest.mark.skipif(
        not os.environ.get("FORGE_PG_TEST_URL"),
        reason="requires explicit disposable PostgreSQL URL",
    )
    async def test_two_amendments_and_a_reservation_raced_across_sessions(self, case):
        async with postgres_case(os.environ["FORGE_PG_TEST_URL"]) as factory:
            from tests.native_amendment_pg import move_case

            await move_case(case, factory)
            await prepare(case)
            outcomes = await asyncio.gather(
                amend(case, "one", Resources(calls=2)),
                amend(case, "two", Resources(calls=3)),
                case["store"].reserve(
                    case["lease"],
                    "implementation",
                    "other",
                    resources(1, 1, ".1"),
                    resources(1, 1, ".1"),
                    now=case["clock"].now(),
                ),
                return_exceptions=True,
            )
            for index, result in enumerate(outcomes[:2]):
                if isinstance(result, (StoreConflict, BudgetConflict)):
                    await amend(case, ("one", "two")[index], Resources(calls=(2, 3)[index]))
                else:
                    assert isinstance(result, Applied), result
            assert not isinstance(outcomes[2], BaseException), outcomes
            pool = await case["store"].load(case["run"].id, "implementation")
            assert (
                len(pool.amendments) == 2 and sum(row.amount.calls for row in pool.amendments) == 5
            )
            assert pool.balance.shortfall == Resources()
            assert len(await rows(case, EngineBudgetAmendment)) == 2

    @pytest.mark.skipif(
        not os.environ.get("FORGE_PG_TEST_URL"),
        reason="requires explicit disposable PostgreSQL URL",
    )
    async def test_one_command_raced_from_two_sessions_applies_once(self, case):
        async with postgres_case(os.environ["FORGE_PG_TEST_URL"]) as factory:
            from tests.native_amendment_pg import move_case

            await move_case(case, factory)
            await prepare(case)
            outcomes = await asyncio.gather(amend(case), amend(case), return_exceptions=True)
            assert any(isinstance(row, Applied) for row in outcomes), outcomes
            assert isinstance(await amend(case), Applied)
            pool = await case["store"].load(case["run"].id, "implementation")
            assert len(pool.amendments) == len(await rows(case, EngineBudgetAmendment)) == 1
            assert case["native"].writes.count("/key/update") == 1
