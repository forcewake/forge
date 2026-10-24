"""Retained cumulative-budget lessons on native pools, receipts and paid-call fences."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError, PendingRollbackError

from forge.budget.model_client import BudgetedModelClient
from forge.budget.server_models import ModelUnavailable
from forge.domain.budget import BudgetExhausted, BudgetLimits, Resources
from forge.domain.run_spec import RunPolicy
from forge.domain.states import Handler, State
from forge.store.budget_calls import ModelCallStore
from forge.store.models_budget import EngineBudgetPool, EngineBudgetReservation
from forge.store.records import StaleLease, StepRequest, StoreConflict
from forge.store.runs import RunStore
from forge.store.steps import StepStore
from tests.native_budget_lesson_fixture import (
    claude_usage,
    cold_schema,
    current,
    hold,
    initialize,
    observation,
    observe,
    pool,
    resources,
    usage_rows,
)
from tests.test_budget_engine import case as case, prepare
from tests.test_domain_approved_input import spec
from tests.test_server_models import server, wire


class TestOpenBudget:
    async def test_open_is_idempotent_per_run(self, case):
        await hold(case)
        before = await current(case)
        await initialize(case)
        assert await current(case) == before
        async with case["factory"]() as session:
            rows = list((await session.scalars(select(EngineBudgetPool))).all())
        assert len(rows) == 2 and {row.purpose for row in rows} == {"implementation", "closing"}
        assert before.capacity == resources(18, 9000, "8")

    async def test_open_requires_an_existing_run(self, case):
        case["lease"] = replace(case["lease"], run_id=uuid4().hex)
        with pytest.raises(StoreConflict, match="run does not exist"):
            await initialize(case)
        async with case["factory"]() as session:
            assert list((await session.scalars(select(EngineBudgetPool))).all()) == []

    async def test_spec_without_budget_dimensions_opens_nothing(self, case):
        document = json.loads(case["frozen"].model_dump_json())
        del document["limits"]
        with pytest.raises(ValidationError):
            RunPolicy.model_validate(document)
        async with case["factory"]() as session:
            assert list((await session.scalars(select(EngineBudgetPool))).all()) == []

    async def test_spec_budgets_freeze_limits_and_digest(self, case):
        await initialize(case)
        assert (await current(case)).capacity == resources(18, 9000, "8")
        original = case["frozen"]
        case["frozen"] = spec(
            limits=BudgetLimits(usd=Decimal("20"), calls=40, tokens=20000, wallclock_seconds=600)
        )
        assert case["frozen"].digest != original.digest
        with pytest.raises(StoreConflict, match="exact persisted policy"):
            await initialize(case)
        assert (await current(case)).capacity == resources(18, 9000, "8")

    @pytest.mark.parametrize("value", [None, "yes", {"calls": "lots"}, {"tokens": -5}])
    async def test_spec_garbage_budget_block_opens_nothing(self, case, value):
        document = json.loads(case["frozen"].model_dump_json())
        document["limits"] = value
        with pytest.raises(ValidationError):
            RunPolicy.model_validate(document)
        async with case["factory"]() as session:
            assert list((await session.scalars(select(EngineBudgetPool))).all()) == []


class TestConcurrentOpen:
    async def test_two_concurrent_opens_end_on_one_row(self, case):
        await asyncio.gather(initialize(case), initialize(case))
        async with case["factory"]() as session:
            before = {
                row.purpose: row.id for row in (await session.scalars(select(EngineBudgetPool)))
            }
        await asyncio.gather(hold(case, "a"), hold(case, "b"))
        async with case["factory"]() as session:
            after = {
                row.purpose: row.id for row in (await session.scalars(select(EngineBudgetPool)))
            }
        assert before == after and len(after) == 2
        assert len((await current(case)).reservations) == 2

    async def test_lost_race_adopts_the_winner_and_stays_usable(self, case):
        await initialize(case)
        winner = await current(case)
        await asyncio.gather(initialize(case), initialize(case))
        reservation = await hold(case, "neighbor")
        assert (await current(case)).identity == winner.identity
        assert reservation.allowance == resources()
        assert len((await current(case)).reservations) == 1

    async def test_add_before_savepoint_is_the_regressed_shape(self, case):
        await initialize(case)
        async with case["factory"]() as session:
            row = await session.scalar(select(EngineBudgetPool))
            session.add(
                EngineBudgetPool(
                    id=uuid4().hex,
                    run_id=row.run_id,
                    purpose=row.purpose,
                    usd=row.usd,
                    calls=row.calls,
                    tokens=row.tokens,
                    state_json=row.state_json,
                )
            )
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await session.flush()
            with pytest.raises(PendingRollbackError):
                await session.scalar(select(EngineBudgetPool))
            await session.rollback()
        assert len((await current(case)).reservations) == 0
        assert (await hold(case)).allowance == resources()


class TestReserve:
    async def test_reserve_refuses_over_limit_and_exhausts(self, case):
        await hold(case, "a", resources(18, 100, "1"))
        with pytest.raises(BudgetExhausted):
            await hold(case, "b", resources(1, 1, "0.1"))
        after = await current(case)
        assert after.balance.available.calls == 0 and len(after.reservations) == 1
        assert after.balance.settled == Resources()

    async def test_reserve_refuses_on_token_limit(self, case):
        await hold(case, "a", resources(1, 8900, "1"))
        with pytest.raises(BudgetExhausted):
            await hold(case, "b", resources(1, 200, "1"))
        assert (await current(case)).balance.held.tokens == 8900

    async def test_reserve_counts_consumed_against_the_limit(self, case):
        await hold(case, requested=resources(1, 8900, "1"))
        await observe(case, usage=observation(tokens=8900), final=True)
        with pytest.raises(BudgetExhausted):
            await hold(case, "second", resources(1, 200, "1"))
        assert (await current(case)).balance.settled.tokens == 8900

    @pytest.mark.parametrize("changes", [{"calls": -1}, {"tokens": -5}, {"usd": "-0.1"}])
    async def test_reserve_rejects_amounts_that_would_create_budget(self, case, changes):
        with pytest.raises(ValidationError):
            Resources(**changes)
        await initialize(case)
        with pytest.raises(BudgetExhausted):
            await hold(case, requested=Resources(), minimum=Resources(calls=1))
        assert (await current(case)).reservations == ()

    async def test_reserve_reads_live_counters_not_the_snapshot(self, case):
        await initialize(case)
        stale = await current(case)
        await hold(case, "a", resources(18, 100, "1"))
        assert stale.balance.available.calls == 18
        with pytest.raises(BudgetExhausted):
            await hold(case, "b", resources(1, 1, "0.1"))
        assert (await current(case)).balance.held.calls == 18

    def test_unlimited_dimensions_never_refuse(self):
        """Finite explicit ceilings replace implicit unlimited dimensions."""
        unlimited_shape = resources((1 << 63) - 1, (1 << 63) - 1, "99999999999999999999")
        value = pool(unlimited_shape.calls, unlimited_shape.tokens, str(unlimited_shape.usd))
        for index in range(5):
            value = value.reserve(str(index), resources(1, 10**9, "1"), Resources(calls=1))
        assert value.balance.held.tokens == 5 * 10**9

    async def test_reserve_on_closed_budget_refuses_without_flip(self, case):
        await initialize(case)
        cancelled = await RunStore(case["factory"]).request_cancel(
            case["run"].id, expected_version=case["run"].version
        )
        with pytest.raises(StaleLease):
            await hold(case)
        assert (await RunStore(case["factory"]).load(cancelled.id)).cancel_requested
        assert (await current(case)).reservations == ()

    async def test_close_budget_is_idempotent(self, case):
        await prepare(case)
        assert await case["service"].finish(case["lease"], "implementation") == "accrued"
        first = await current(case)
        assert await case["service"].finish(case["lease"], "implementation") == "accrued"
        assert await current(case) == first
        assert case["native"].writes.count("/key/block") == 1


class TestReconcile:
    async def test_reconcile_moves_reserved_to_consumed(self, case):
        await hold(case)
        settled = await observe(case, usage=observation(), final=True)
        assert settled.balance.held == Resources()
        assert settled.balance.settled == resources(1, 100, "0.1")
        assert settled.reservations[0].allowance == resources()
        async with case["factory"]() as session:
            row = await session.scalar(select(EngineBudgetReservation))
        assert row.state == "settled" and row.observed_tokens == 100

    async def test_under_reservation_records_actuals_and_exhausts(self, case):
        await hold(case, requested=resources(1, 100, "1"))
        settled = await observe(case, usage=observation(tokens=10000, usd="9"), final=True)
        assert settled.balance.settled.tokens == 10000
        assert settled.balance.shortfall == resources(0, 1000, "1")
        with pytest.raises(BudgetExhausted):
            await hold(case, "second")

    async def test_unknown_actuals_become_liability_not_zero_spend(self, case):
        await hold(case)
        value = await observe(case, usage=observation(tokens=None), final=False)
        assert value.balance.held == resources()
        assert value.reservations[0].observed.tokens is None
        assert value.balance.available.tokens == 8000
        await hold(case, "second", resources(1, 8000, "1"))
        with pytest.raises(BudgetExhausted):
            await hold(case, "third", resources(1, 1, "0.1"))

    async def test_fully_unknown_receipt_parks_the_whole_hold(self, case):
        await hold(case, requested=resources(18, 9000, "8"))
        value = await observe(case)
        assert value.reservations[0].status == "unknown"
        assert value.reservations[0].observed is None
        assert value.balance.available == Resources()
        with pytest.raises(BudgetExhausted):
            await hold(case, "second")

    async def test_reconcile_rejects_negative_actuals(self, case):
        await hold(case)
        with pytest.raises(ValidationError):
            observation(tokens=-1)
        assert (await current(case)).balance.held == resources()

    async def test_reconcile_is_exactly_once(self, case):
        await hold(case)
        first = await observe(case, usage=observation(), final=True)
        assert await observe(case, usage=observation(), final=True) == first
        with pytest.raises(ValueError, match="cannot be overwritten"):
            await observe(case, usage=observation(tokens=101), final=True)
        assert await current(case) == first

    async def test_interleaved_moves_never_overshoot_exposure(self, case):
        for index in range(6):
            try:
                await hold(case, str(index), resources(3, 1800, "1"))
            except BudgetExhausted:
                break
            if index % 2 == 0:
                await observe(case, str(index), observation(tokens=900), final=True)
            value = await current(case)
            assert value.capacity.covers(value.balance.held + value.balance.settled)
        assert (await current(case)).balance.shortfall == Resources()

    async def test_call_limit_exhausts_on_reconcile(self, case):
        await hold(case, requested=resources(18, 100, "1"))
        value = await observe(case, usage=observation(calls=18), final=True)
        assert value.balance.settled.calls == 18 and value.balance.available.calls == 0
        with pytest.raises(BudgetExhausted):
            await hold(case, "second", resources(1, 1, "0.1"))


class TestLLMBudgetEnforcement:
    async def test_refusal_raises_before_the_provider_is_called(self, case):
        requests = []
        async with server(case, lambda request: requests.append(request) or wire()) as (
            _,
            client,
            args,
        ):
            for _ in range(3):
                await client.complete(**args)
            with pytest.raises(BudgetExhausted):
                await client.complete(**args)
        assert len(requests) == 3 and len(await usage_rows(case)) == 3

    async def test_successful_call_reserves_then_reconciles_real_usage(self, case):
        async def native(request):
            (row,) = await usage_rows(case)
            assert not row.final and row.tokens is row.usd is row.calls is None
            assert json.loads(json.loads(row.receipt_json)["reserved_json"])["tokens"] > 100
            return wire()

        async with server(case, native) as (_, client, args):
            reply = await client.complete(**args)
        (row,) = await usage_rows(case)
        assert row.final and row.calls == 1 and row.tokens == reply.usage.tokens == 14
        assert row.usd == Decimal("0.00003")

    async def test_http_failure_still_consumes_the_call(self, case):
        async with server(case, lambda request: wire(status=500)) as (_, client, args):
            with pytest.raises(ModelUnavailable):
                await client.complete(**args)
        (row,) = await usage_rows(case)
        assert not row.final and row.calls is row.tokens is row.usd is None
        assert json.loads(json.loads(row.receipt_json)["reserved_json"])["calls"] == 1

    async def test_invalid_json_reconciles_the_known_usage(self, case):
        async with server(
            case,
            lambda request: wire(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "not json"},
                        }
                    ],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
                }
            ),
        ) as (_, client, args):
            reply = await client.complete(**args)
            with pytest.raises(json.JSONDecodeError):
                json.loads(reply.text)
        (row,) = await usage_rows(case)
        assert row.final and row.tokens == 9 and row.calls == 1


async def test_load_budget_guard_returns_none_when_unbudgeted(case):
    class Unexpected:
        async def complete(self, **kwargs):
            raise AssertionError("ungranted model must never be contacted")

    client = BudgetedModelClient(
        Unexpected(),
        ModelCallStore(case["factory"]),
        lambda: case["lease"],
        case["clock"],
        case["admission"],
    )
    with pytest.raises(StoreConflict, match="active planning or closing"):
        await client.complete(
            model="test-model",
            system="s",
            prompt="u",
            credential_alias="missing",
            max_output_tokens=10,
        )
    assert await usage_rows(case) == []


class TestHarnessReceipt:
    async def test_aggregate_receipt_counts_one_call_and_reported_tokens(self, case):
        await hold(case)
        value = await observe(case, usage=observation(tokens=150), final=True)
        assert value.balance.settled.calls == 1 and value.balance.settled.tokens == 150

    async def test_unknown_completeness_stops_a_hard_limited_budget(self, case):
        await hold(case, requested=resources(18, 9000, "8"))
        value = await observe(case, usage=observation(tokens=None), final=False)
        assert value.reservations[0].observed.tokens is None
        assert value.balance.available == Resources()
        with pytest.raises(BudgetExhausted):
            await hold(case, "new")

    def test_unknown_completeness_on_an_unlimited_token_axis_stays_open(self):
        value = pool(calls=10, tokens=(1 << 63) - 1, usd="10").reserve(
            "one", resources(), Resources(calls=1)
        )
        value = value.observe("one", observation(tokens=None), final=False)
        assert value.balance.available.tokens == (1 << 63) - 1 - 1000
        assert value.reserve("two", resources(), Resources(calls=1)).balance.held.tokens == 2000

    async def test_cached_tokens_are_never_added_on_top(self, case):
        async with server(
            case,
            lambda request: wire(
                {
                    "choices": [
                        {"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
                    ],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 50,
                        "total_tokens": 150,
                        "prompt_tokens_details": {"cached_tokens": 40},
                    },
                }
            ),
        ) as (_, client, args):
            assert (await client.complete(**args)).usage.tokens == 150
        assert (await usage_rows(case))[0].tokens == 150

    async def test_receipt_exhausts_an_overshooting_budget(self, case):
        await hold(case, requested=resources(1, 100, "1"))
        value = await observe(case, usage=observation(tokens=10000), final=True)
        assert value.balance.settled.tokens == 10000 and value.balance.shortfall.tokens == 1000

    async def test_receipt_on_closed_budget_is_ignored(self, case):
        """Terminal authority closes work; late source facts remain honest and held."""
        await hold(case)
        await RunStore(case["factory"]).request_cancel(
            case["run"].id, expected_version=case["run"].version
        )
        value = await observe(case, usage=observation(tokens=10), final=False)
        assert value.balance.held == resources()
        assert (await RunStore(case["factory"]).load(case["run"].id)).cancel_requested

    async def test_receipt_without_a_budget_is_a_noop(self, case):
        with pytest.raises(StoreConflict, match="not initialized"):
            await observe(case, usage=observation())
        assert await usage_rows(case) == []


class TestWallclockEnforcement:
    async def test_expired_wallclock_refuses_and_exhausts(self, case):
        steps = StepStore(case["factory"])
        await steps.complete_and_enqueue(case["lease"], result_json='{"reason":"completed"}')
        now = case["clock"].now()
        await steps.enqueue(
            StepRequest(
                case["run"].id,
                Handler.PLAN,
                "deadline",
                "{}",
                now,
                deadline_at=now + timedelta(seconds=1),
            )
        )
        case["clock"].instant += timedelta(seconds=2)
        assert await steps.claim_due("late", case["clock"].now(), 60) == ()
        run = await RunStore(case["factory"]).load(case["run"].id)
        assert run.state == State.BLOCKED and run.reason == "step_deadline_expired"
        assert case["native"].writes == []

    async def test_live_wallclock_still_grants(self, case):
        assert (await prepare(case)).kind == "applied"
        assert case["native"].writes == ["/key/generate"]

    async def test_exhausted_budget_refuses_further_reserves(self, case):
        await hold(case, requested=resources(18, 9000, "8"))
        await observe(case, usage=observation(calls=18, tokens=9000, usd="8"), final=True)
        await initialize(case)
        with pytest.raises(BudgetExhausted):
            await hold(case, "second")
        assert (await current(case)).balance.available == Resources()

    async def test_guard_refuses_expired_wallclock_before_any_http(self, case):
        requests = []
        async with server(case, lambda request: requests.append(request) or wire()) as (
            _,
            client,
            args,
        ):
            case["clock"].instant += timedelta(seconds=301)
            with pytest.raises(StoreConflict):
                await client.complete(**args)
        assert requests == [] and await usage_rows(case) == []


class TestBudgetBlockReason:
    async def test_no_budget_and_open_budget_allow_work(self, case):
        """Explicit initialization is mandatory; an initialized pool then grants work."""
        with pytest.raises(StoreConflict, match="not initialized"):
            await current(case)
        assert (await hold(case)).allowance == resources()

    async def test_exhausted_budget_names_the_block(self, case):
        await hold(case, requested=resources(18, 9000, "8"))
        with pytest.raises(BudgetExhausted, match="budget_exhausted"):
            await hold(case, "second")
        assert (await current(case)).balance.available == Resources()

    async def test_spent_wallclock_blocks_and_exhausts_durably(self, case):
        await TestWallclockEnforcement().test_expired_wallclock_refuses_and_exhausts(case)
        assert (
            await RunStore(case["factory"]).load(case["run"].id)
        ).reason == "step_deadline_expired"

    async def test_closed_budget_blocks_nothing(self, case):
        await hold(case)
        await RunStore(case["factory"]).request_cancel(
            case["run"].id, expected_version=case["run"].version
        )
        value = await observe(case, usage=observation(), final=True)
        assert value.balance.settled == resources(1, 100, "0.1")
        assert (await RunStore(case["factory"]).load(case["run"].id)).cancel_requested


class TestReceiptNormalization:
    async def test_anthropic_disjoint_counters_reach_the_budget_in_full(self, case, tmp_path):
        usage = await claude_usage(
            tmp_path,
            {
                "input_tokens": 100,
                "cache_read_input_tokens": 40,
                "cache_creation_input_tokens": 25,
                "output_tokens": 50,
            },
        )
        assert usage.input_tokens == 165 and usage.output_tokens == 50
        await hold(case)
        value = await observe(
            case, usage=observation(tokens=usage.input_tokens + usage.output_tokens), final=True
        )
        assert value.balance.settled.tokens == 215

    async def test_openai_inclusive_cache_is_still_never_added_on_top(self, case):
        await TestHarnessReceipt().test_cached_tokens_are_never_added_on_top(case)


class TestReceiptDedupe:
    async def test_same_key_reconciles_once(self, case):
        await hold(case)
        await hold(case)
        await observe(case, usage=observation(), final=True)
        value = await observe(case, usage=observation(), final=True)
        assert len(value.reservations) == 1 and value.balance.settled.calls == 1

    async def test_different_keys_reconcile_separately(self, case):
        await hold(case, "a")
        await hold(case, "b")
        await observe(case, "a", observation(), final=True)
        value = await observe(case, "b", observation(), final=True)
        assert len(value.reservations) == 2 and value.balance.settled.calls == 2

    async def test_no_key_keeps_the_legacy_count_always_behavior(self, case):
        """New paid calls each get a durable source identity before sending."""
        async with server(case, lambda request: wire()) as (_, client, args):
            await client.complete(**args)
            await client.complete(**args)
        rows = await usage_rows(case)
        assert len(rows) == len({row.source_key for row in rows}) == 2
        assert sum(row.calls for row in rows) == 2

    async def test_claims_survive_a_closed_then_reconciled_race(self, case):
        await hold(case)
        await RunStore(case["factory"]).request_cancel(
            case["run"].id, expected_version=case["run"].version
        )
        await observe(case, usage=observation(), final=True)
        value = await observe(case, usage=observation(), final=True)
        assert value.balance.settled.calls == 1
        with pytest.raises(StaleLease):
            await hold(case, "new")


class TestMigration009:
    def test_upgrade_creates_budget_tables_and_downgrade_drops_them(self):
        """The single cold baseline creates accounting; downgrade refuses deletion."""
        with cold_schema() as installed:
            inspector = inspect(installed.connection)
            assert {"budget_pools", "budget_reservations", "usage"} <= set(
                inspector.get_table_names()
            )
            with pytest.raises(RuntimeError, match="destructive downgrade"):
                installed.module.downgrade()
            assert inspect(installed.connection).has_table("budget_pools")

    def test_models_match_the_migrated_columns(self):
        with cold_schema() as installed:
            inspector = inspect(installed.connection)
            for model in (EngineBudgetPool, EngineBudgetReservation):
                assert {
                    column["name"] for column in inspector.get_columns(model.__tablename__)
                } == {column.name for column in model.__table__.columns}
            indexes = inspector.get_unique_constraints("budget_reservations")
            assert any(
                set(item["column_names"]) == {"pool_id", "reservation_key"} for item in indexes
            )


class TestMigration011:
    def test_upgrade_adds_the_unresolved_counters_and_downgrade_drops_them(self):
        """Nullable observations and held state preserve unresolved liability at baseline."""
        with cold_schema() as installed:
            columns = {
                item["name"]: item
                for item in inspect(installed.connection).get_columns("budget_reservations")
            }
            assert all(
                columns[name]["nullable"]
                for name in ("observed_usd", "observed_calls", "observed_tokens")
            )
            with pytest.raises(RuntimeError, match="destructive downgrade"):
                installed.module.downgrade()
            assert inspect(installed.connection).has_table("budget_reservations")
