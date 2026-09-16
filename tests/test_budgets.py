"""Tests for run budgets: reserve before dispatch, reconcile actuals (F22).

Covers the ADR-0018 §5 contract on top of ADR-0013's reserve-then-reconcile:

- ``open_budget`` is idempotent per run and reads limits from the RunSpec's
  ``budgets`` block;
- ``reserve`` refuses (and marks ``exhausted``) with a single conditional
  UPDATE — the live row counters decide, never an ORM snapshot, and spent
  actuals consume the limit just like outstanding holds do;
- ``reconcile_actual`` moves reserved→consumed exactly once, records
  under-reserved actuals, and books a hold whose usage stayed unknown as
  unresolved liability instead of reopening headroom as zero spend;
- the LLM client raises ``budget_exhausted`` BEFORE the provider is contacted
  and reconciles real usage afterwards (failures still consume the call);
- harness usage receipts reconcile the run budget (aggregate = 1 call +
  reported tokens; unknown leaves tokens untouched).
"""

import asyncio
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import inspect, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from forge.durable import FlowRun, RunBudget
from forge.durable.budgets import (
    BudgetGuard,
    budget_for_run,
    budget_limits_from_spec,
    close_budget,
    load_budget_guard,
    open_budget,
    open_budget_from_spec,
    reconcile_actual,
    reconcile_harness_receipt,
    reserve,
)
from forge.durable.models import BudgetReservation
from forge.factory.llm import LLMClient, LLMError, LLMResponseError
from forge.models.base import Base

RUN_ID = "r" * 32


@pytest.fixture()
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(FlowRun(id=RUN_ID, project_id=1))
        await session.commit()
    yield factory
    await engine.dispose()


def _settings():
    from forge.config import Settings

    return Settings(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        LITELLM_URL="http://litellm.test",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
    )


def completion(text: str, prompt_tokens=None, completion_tokens=None) -> dict:
    usage = {}
    if prompt_tokens is not None:
        usage["prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        usage["completion_tokens"] = completion_tokens
    body = {"choices": [{"message": {"content": text}}]}
    if usage:
        body["usage"] = usage
    return body


async def get_budget(db) -> RunBudget:
    async with db() as session:
        budget = await budget_for_run(session, RUN_ID)
        assert budget is not None
        session.expunge(budget)
        return budget


async def openb(db, **limits) -> RunBudget:
    async with db() as session:
        budget = await open_budget(session, run_id=RUN_ID, **limits)
        await session.commit()
        session.expunge(budget)
        return budget


async def reservations(db) -> list[BudgetReservation]:
    async with db() as session:
        rows = (
            (await session.execute(select(BudgetReservation).order_by(BudgetReservation.id)))
            .scalars()
            .all()
        )
        for row in rows:
            session.expunge(row)
        return rows


class TestOpenBudget:
    async def test_open_is_idempotent_per_run(self, db):
        first = await openb(db, spec_digest="d1", max_calls=5, max_tokens=100)
        again = await openb(db, spec_digest="OTHER", max_calls=99, max_tokens=999)

        assert again.id == first.id
        # The first-open limits stand; a re-open never silently moves them.
        assert again.max_calls == 5
        assert again.max_tokens == 100
        assert again.spec_digest == "d1"
        assert again.status == "open"
        assert again.reserved_calls == 0 and again.consumed_calls == 0
        budgets = await get_budget(db)
        assert budgets.run_id == RUN_ID

    async def test_open_requires_an_existing_run(self, db):
        async with db() as session:
            from forge.durable import RunNotFound

            with pytest.raises(RunNotFound):
                await open_budget(session, run_id="nope" * 8)

    async def test_spec_without_budget_dimensions_opens_nothing(self, db):
        # The spec shape RunService writes today: lifecycle limits only.
        document = {"budgets": {"commit_cycles": 3, "harness_timeout": 1800}}
        assert budget_limits_from_spec(document) is None
        async with db() as session:
            assert (
                await open_budget_from_spec(session, run_id=RUN_ID, spec_document=document) is None
            )
        assert await get_budget_or_none(db) is None

    async def test_spec_budgets_freeze_limits_and_digest(self, db):
        document = {
            "budgets": {
                "commit_cycles": 3,
                "wallclock_s": 1800,
                "max_calls": 40,
                "max_tokens": 500000,
            }
        }
        limits = budget_limits_from_spec(document)
        assert limits == budget_limits_from_spec(document)  # deterministic
        async with db() as session:
            budget = await open_budget_from_spec(
                session,
                run_id=RUN_ID,
                spec_document=document,
                spec_digest="specdigest",
            )
            await session.commit()
        assert budget.max_calls == 40
        assert budget.max_tokens == 500000
        assert budget.wallclock_s == 1800
        assert budget.spec_digest == "specdigest"

    async def test_spec_garbage_budget_block_opens_nothing(self, db):
        assert budget_limits_from_spec(None) is None
        assert budget_limits_from_spec({"budgets": "yes"}) is None
        assert budget_limits_from_spec({"budgets": {"max_calls": "lots"}}) is None
        assert budget_limits_from_spec({"budgets": {"max_calls": True}}) is None
        assert budget_limits_from_spec({"budgets": {"max_calls": -5}}) is None


async def get_budget_or_none(db) -> RunBudget | None:
    async with db() as session:
        budget = await budget_for_run(session, RUN_ID)
        if budget is not None:
            session.expunge(budget)
        return budget


class TestReserve:
    async def test_reserve_refuses_over_limit_and_exhausts(self, db):
        budget = await openb(db, max_calls=2)

        first = await session_reserve(db, budget, calls=1, tokens=10)
        second = await session_reserve(db, budget, calls=1, tokens=10)
        assert first is not None and second is not None
        refused = await session_reserve(db, budget, calls=1, tokens=10)
        assert refused is None

        after = await get_budget(db)
        # The refusal durably exhausts the budget: it cannot serve the
        # standard reservation shape, so it must stop accepting work.
        assert after.status == "exhausted"
        assert after.reserved_calls == 2
        assert after.consumed_calls == 0

        rows = await reservations(db)
        assert len(rows) == 2  # the refused attempt wrote no audit row
        assert all(not row.released for row in rows)

    async def test_reserve_refuses_on_token_limit(self, db):
        budget = await openb(db, max_tokens=500)
        assert await session_reserve(db, budget, calls=1, tokens=400) is not None
        assert await session_reserve(db, budget, calls=1, tokens=200) is None
        assert (await get_budget(db)).status == "exhausted"

    async def test_spent_actuals_consume_the_token_limit(self, db):
        """90 spent + 0 reserved + 20 requested at limit 100 is refused (R12).

        Consumption is exposure, not just the outstanding holds: a budget
        whose actuals nearly fill it cannot grant a request that fits only if
        the spend were ignored.
        """
        budget = await openb(db, max_tokens=100)
        hold = await session_reserve(db, budget, calls=1, tokens=90)
        assert hold is not None
        async with db() as session:
            settled = await reconcile_actual(session, hold, actual_calls=1, actual_tokens=90)
            await session.commit()
        assert settled.consumed_tokens == 90 and settled.reserved_tokens == 0

        assert await session_reserve(db, budget, calls=1, tokens=20) is None
        after = await get_budget(db)
        assert after.status == "exhausted"
        assert after.consumed_tokens == 90  # the spend stands, unexposed
        assert after.reserved_tokens == 0 and after.unresolved_tokens == 0

    async def test_reserve_grants_the_exact_remaining_exposure(self, db):
        budget = await openb(db, max_tokens=100)
        hold = await session_reserve(db, budget, calls=1, tokens=90)
        assert hold is not None
        async with db() as session:
            await reconcile_actual(session, hold, actual_calls=1, actual_tokens=90)
            await session.commit()
        # 90 spent + 10 requested == 100: the limit is still met exactly.
        assert await session_reserve(db, budget, calls=1, tokens=10) is not None

    async def test_spent_actuals_consume_the_call_limit(self, db):
        budget = await openb(db, max_calls=2)
        hold = await session_reserve(db, budget, calls=1, tokens=0)
        assert hold is not None
        async with db() as session:
            await reconcile_actual(session, hold, actual_calls=1)
            await session.commit()

        assert await session_reserve(db, budget, calls=1) is not None  # 1+1 == 2
        assert await session_reserve(db, budget, calls=1) is None  # 1+1+1 > 2
        assert (await get_budget(db)).status == "exhausted"

    async def test_reserve_rejects_moves_that_are_not_non_negative_ints(self, db):
        """A negative or fractional move would *credit* capacity or corrupt
        the predicate arithmetic — rejected before any row is touched."""
        budget = await openb(db, max_calls=5, max_tokens=1000)
        for kwargs in ({"calls": -1}, {"tokens": -10}, {"calls": 1.5}, {"tokens": True}):
            with pytest.raises(ValueError):
                await session_reserve(db, budget, **kwargs)

        after = await get_budget(db)
        assert after.status == "open"
        assert after.reserved_calls == 0 and after.reserved_tokens == 0
        assert await reservations(db) == []  # no audit rows for garbage moves

    async def test_reserve_reads_live_counters_not_the_snapshot(self, db):
        """The conditional UPDATE, not the ORM object, enforces the limit."""
        await openb(db, max_calls=1)
        # A stale snapshot loaded BEFORE another session exhausts the budget.
        async with db() as session:
            stale = await budget_for_run(session, RUN_ID)
            assert stale.reserved_calls == 0

        other = await openb(db, max_calls=1)  # fresh session grants the call
        assert await session_reserve(db, other, calls=1, tokens=0) is not None

        async with db() as session:
            # The stale snapshot still says reserved_calls == 0...
            assert stale.reserved_calls == 0
            # ...but the grant must refuse: the row's live counters say 1.
            assert await reserve(session, stale, calls=1, tokens=0) is None
            await session.commit()
        assert (await get_budget(db)).status == "exhausted"

    async def test_unlimited_dimensions_never_refuse(self, db):
        budget = await openb(db)  # no limits at all
        for _ in range(5):
            assert await session_reserve(db, budget, calls=1, tokens=10**9) is not None
        after = await get_budget(db)
        assert after.status == "open"
        assert after.reserved_calls == 5

    async def test_reserve_on_closed_budget_refuses_without_flip(self, db):
        budget = await openb(db, max_calls=1)
        async with db() as session:
            assert await close_budget(session, budget) is True
            await session.commit()
            assert await reserve(session, budget, calls=1, tokens=1) is None
        after = await get_budget(db)
        assert after.status == "closed"  # a refusal never resurrects a budget

    async def test_close_budget_is_idempotent(self, db):
        budget = await openb(db)
        async with db() as session:
            assert await close_budget(session, budget) is True
            assert await close_budget(session, budget) is False
            await session.commit()
        assert (await get_budget(db)).status == "closed"


async def session_reserve(db, budget, *, calls: int, tokens: int = 0):
    async with db() as session:
        reservation = await reserve(session, budget, calls=calls, tokens=tokens)
        await session.commit()
        return reservation


class TestReconcile:
    async def test_reconcile_moves_reserved_to_consumed(self, db):
        budget = await openb(db, max_calls=3, max_tokens=1000)
        reservation = await session_reserve(db, budget, calls=1, tokens=400)
        assert reservation is not None

        async with db() as session:
            settled = await reconcile_actual(
                session, reservation, actual_calls=1, actual_tokens=300
            )
            await session.commit()
        assert settled.consumed_calls == 1
        assert settled.consumed_tokens == 300
        assert settled.reserved_calls == 0
        assert settled.reserved_tokens == 0
        assert settled.status == "open"  # 300 <= 1000: headroom remains

        (row,) = await reservations(db)
        assert row.released is True
        assert row.reserved_tokens == 400  # audit keeps the original hold

    async def test_under_reservation_records_actuals_and_exhausts(self, db):
        budget = await openb(db, max_tokens=500)
        reservation = await session_reserve(db, budget, calls=1, tokens=400)
        async with db() as session:
            settled = await reconcile_actual(
                session, reservation, actual_calls=1, actual_tokens=600
            )
            await session.commit()
        # The full actual is recorded (never clipped to the hold) and the
        # overshoot durably exhausts the budget.
        assert settled.consumed_tokens == 600
        assert settled.status == "exhausted"

    async def test_unknown_actuals_move_the_hold_into_unresolved_liability(self, db):
        """An unknown receipt never reopens hard-budget capacity as zero spend."""
        budget = await openb(db, max_calls=2, max_tokens=1000)
        reservation = await session_reserve(db, budget, calls=1, tokens=400)
        async with db() as session:
            settled = await reconcile_actual(
                session, reservation, actual_calls=1, actual_tokens=None
            )
            await session.commit()
        # Unknown tokens are never counted as zero — the hold moves into the
        # unresolved liability instead of returning as spendable headroom.
        assert settled.consumed_tokens == 0
        assert settled.reserved_tokens == 0
        assert settled.unresolved_tokens == 400
        assert settled.status == "open"  # exposed, not spent: headroom shrunk

        # The liability keeps counting against the limit: 400 + 600 fits...
        assert await session_reserve(db, budget, calls=1, tokens=600) is not None
        # ...but 400 + 700 does not, even though nothing was ever consumed.
        assert await session_reserve(db, budget, calls=1, tokens=700) is None

    async def test_unknown_call_actual_keeps_the_call_hold_as_liability(self, db):
        budget = await openb(db, max_calls=2)
        reservation = await session_reserve(db, budget, calls=1, tokens=0)
        async with db() as session:
            settled = await reconcile_actual(session, reservation, actual_calls=None)
            await session.commit()
        assert settled.consumed_calls == 0
        assert settled.reserved_calls == 0
        assert settled.unresolved_calls == 1

        assert await session_reserve(db, budget, calls=1) is not None  # 1+1 == 2
        assert await session_reserve(db, budget, calls=1) is None  # 1+1+1 > 2

    async def test_reconcile_is_exactly_once(self, db):
        budget = await openb(db, max_calls=5)
        reservation = await session_reserve(db, budget, calls=1, tokens=0)
        async with db() as session:
            await reconcile_actual(session, reservation, actual_calls=1, actual_tokens=10)
            again = await reconcile_actual(session, reservation, actual_calls=1, actual_tokens=10)
            await session.commit()
        # A crash-retry (released already flipped) cannot double-apply.
        assert again.consumed_calls == 1
        assert again.consumed_tokens == 10

    async def test_reconcile_rejects_negative_actuals_before_touching_a_row(self, db):
        budget = await openb(db, max_calls=5, max_tokens=1000)
        reservation = await session_reserve(db, budget, calls=1, tokens=400)
        async with db() as session:
            with pytest.raises(ValueError):
                await reconcile_actual(session, reservation, actual_calls=-1)
            with pytest.raises(ValueError):
                await reconcile_actual(session, reservation, actual_tokens=-5)
            await session.commit()
        # Nothing moved: the hold is still outstanding and settles exactly once.
        after = await get_budget(db)
        assert after.consumed_calls == 0 and after.consumed_tokens == 0
        assert after.reserved_calls == 1 and after.reserved_tokens == 400
        (row,) = await reservations(db)
        assert row.released is False

    async def test_settlement_never_drives_reserved_negative(self, db):
        budget = await openb(db, max_calls=5, max_tokens=1000)
        reservation = await session_reserve(db, budget, calls=1, tokens=400)
        async with db() as session:
            # The holds are already gone from the row (a crash-retry race):
            # the defensive clamp holds the counters at zero, never below.
            await session.execute(
                update(RunBudget)
                .where(RunBudget.id == budget.id)
                .values(reserved_calls=0, reserved_tokens=0)
            )
            await session.commit()
        async with db() as session:
            settled = await reconcile_actual(
                session, reservation, actual_calls=1, actual_tokens=100
            )
            await session.commit()
        assert settled.reserved_calls == 0 and settled.reserved_tokens == 0
        assert settled.unresolved_calls == 0 and settled.unresolved_tokens == 0
        assert settled.consumed_calls == 1
        assert settled.consumed_tokens == 100

    async def test_call_limit_exhausts_on_reconcile(self, db):
        budget = await openb(db, max_calls=1)
        reservation = await session_reserve(db, budget, calls=1, tokens=0)
        async with db() as session:
            settled = await reconcile_actual(session, reservation, actual_calls=1)
            await session.commit()
        assert settled.consumed_calls == 1
        assert settled.status == "exhausted"
        assert settled.reserved_calls == 0


class TestLLMBudgetEnforcement:
    def make_client(self, db, guard: BudgetGuard | None) -> LLMClient:
        return LLMClient(settings=_settings(), session_factory=db, budget=guard)

    async def test_refusal_raises_before_the_provider_is_called(self, db, httpx_mock):
        await openb(db, max_calls=1)
        guard = await load_budget_guard(db, RUN_ID)
        assert guard is not None
        # Burn the single granted call outside the client.
        hold = await guard.reserve(calls=1, tokens=100)
        assert hold is not None

        # No httpx response is registered: pytest-httpx fails the test if any
        # request reaches the transport.
        client = self.make_client(db, guard)
        with pytest.raises(LLMError, match="budget_exhausted"):
            await client.complete(
                tier="strong",
                system="s",
                user="u",
                role="planner",
                flow_run_id=RUN_ID,
                max_tokens=4096,
            )
        await client.close()

        assert httpx_mock.get_requests() == []
        after = await guard.refresh()
        assert after is not None and after.status == "exhausted"
        assert after.consumed_calls == 0  # nothing was spent

        # The refusal is journaled for the ledger, with unknown usage.
        async with db() as session:
            from forge.durable import LLMCall

            (row,) = (await session.execute(select(LLMCall))).scalars().all()
        assert row.status == "failed"
        assert row.error == "budget_exhausted"
        assert row.input_tokens is None and row.output_tokens is None

    async def test_spent_actuals_refuse_before_the_provider_is_called(self, db, httpx_mock):
        """90 of 100 tokens spent, 0 held, 20 requested → refused (R12)."""
        budget = await openb(db, max_calls=10, max_tokens=100)
        hold = await session_reserve(db, budget, calls=1, tokens=90)
        assert hold is not None
        async with db() as session:
            await reconcile_actual(session, hold, actual_calls=1, actual_tokens=90)
            await session.commit()

        guard = await load_budget_guard(db, RUN_ID)
        assert guard is not None
        # No httpx response is registered: pytest-httpx fails the test if any
        # request reaches the transport.
        client = self.make_client(db, guard)
        with pytest.raises(LLMError, match="budget_exhausted"):
            await client.complete(
                tier="strong",
                system="s",
                user="u",
                role="planner",
                flow_run_id=RUN_ID,
                max_tokens=20,
            )
        await client.close()

        assert httpx_mock.get_requests() == []
        after = await guard.refresh()
        assert after is not None and after.status == "exhausted"
        assert after.consumed_tokens == 90  # the recorded spend is untouched

    async def test_successful_call_reserves_then_reconciles_real_usage(self, db, httpx_mock):
        await openb(db, max_calls=5, max_tokens=10000)
        guard = await load_budget_guard(db, RUN_ID)
        httpx_mock.add_response(json=completion("ok", prompt_tokens=12, completion_tokens=34))
        client = self.make_client(db, guard)
        result = await client.complete(
            tier="strong",
            system="s",
            user="u",
            role="planner",
            flow_run_id=RUN_ID,
            max_tokens=4096,
        )
        await client.close()

        assert result.text == "ok"
        after = await guard.refresh()
        assert after is not None
        # The hold (1 call + the 4096 estimate) was replaced by the actuals.
        assert after.reserved_calls == 0 and after.reserved_tokens == 0
        assert after.consumed_calls == 1
        assert after.consumed_tokens == 46
        assert after.status == "open"

        (row,) = await reservations(db)
        assert row.released is True
        assert row.reserved_tokens == 4096  # the estimate was the hold

    async def test_http_failure_still_consumes_the_call(self, db, httpx_mock):
        await openb(db, max_calls=2, max_tokens=1000)
        guard = await load_budget_guard(db, RUN_ID)
        httpx_mock.add_response(status_code=500, text="boom")
        client = self.make_client(db, guard)
        with pytest.raises(LLMError):
            await client.complete(
                tier="code",
                system="s",
                user="u",
                role="implementer",
                flow_run_id=RUN_ID,
                max_tokens=400,
            )
        await client.close()

        after = await guard.refresh()
        assert after is not None
        # The provider may have processed the failed request: the call counts,
        # the token actuals stay unknown (never zero), and the hold moves into
        # the unresolved liability instead of reopening headroom.
        assert after.consumed_calls == 1
        assert after.consumed_tokens == 0
        assert after.reserved_tokens == 0
        assert after.unresolved_tokens == 400
        assert after.status == "open"

    async def test_invalid_json_reconciles_the_known_usage(self, db, httpx_mock):
        await openb(db, max_calls=5, max_tokens=10000)
        guard = await load_budget_guard(db, RUN_ID)
        httpx_mock.add_response(json=completion("not json", prompt_tokens=7))
        client = self.make_client(db, guard)
        with pytest.raises(LLMResponseError):
            await client.complete(
                tier="strong",
                system="s",
                user="u",
                role="planner",
                flow_run_id=RUN_ID,
                json_mode=True,
            )
        await client.close()

        after = await guard.refresh()
        assert after is not None
        assert after.consumed_calls == 1
        assert after.consumed_tokens == 7  # the known part, not the estimate


async def test_load_budget_guard_returns_none_when_unbudgeted(db):
    assert await load_budget_guard(db, RUN_ID) is None
    await openb(db)
    guard = await load_budget_guard(db, RUN_ID)
    assert guard is not None
    async with db() as session:
        await close_budget(session, await budget_for_run(session, RUN_ID))
        await session.commit()
    assert await load_budget_guard(db, RUN_ID) is None  # closed ⇒ no guard


class TestHarnessReceipt:
    async def test_aggregate_receipt_counts_one_call_and_reported_tokens(self, db):
        await openb(db, max_calls=3, max_tokens=10000)
        usage = SimpleUsage(input_tokens=100, output_tokens=50, completeness="aggregate")
        async with db() as session:
            settled = await reconcile_harness_receipt(session, RUN_ID, usage)
            await session.commit()
        assert settled is not None
        assert settled.consumed_calls == 1
        assert settled.consumed_tokens == 150
        assert settled.status == "open"

    async def test_unknown_completeness_keeps_tokens_unchanged(self, db):
        await openb(db, max_calls=3, max_tokens=10000)
        unknown = SimpleUsage(input_tokens=None, output_tokens=None, completeness="unknown")
        async with db() as session:
            settled = await reconcile_harness_receipt(session, RUN_ID, unknown)
            await session.commit()
        assert settled is not None
        # The call counts; the tokens are unknown — unchanged, never zeroed
        # into a fabricated 0 that would falsify the total.
        assert settled.consumed_calls == 1
        assert settled.consumed_tokens == 0

    async def test_cached_tokens_are_never_added_on_top(self, db):
        await openb(db, max_calls=3, max_tokens=10000)
        usage = SimpleUsage(
            input_tokens=100,
            cached_input_tokens=40,
            output_tokens=50,
            completeness="aggregate",
        )
        async with db() as session:
            settled = await reconcile_harness_receipt(session, RUN_ID, usage)
            await session.commit()
        assert settled is not None
        assert settled.consumed_tokens == 150  # input is the inclusive figure

    async def test_receipt_exhausts_an_overshooting_budget(self, db):
        await openb(db, max_tokens=100)
        usage = SimpleUsage(input_tokens=60, output_tokens=60, completeness="aggregate")
        async with db() as session:
            settled = await reconcile_harness_receipt(session, RUN_ID, usage)
            await session.commit()
        assert settled is not None
        # Actuals are recorded even though the budget is now exhausted.
        assert settled.consumed_tokens == 120
        assert settled.status == "exhausted"

    async def test_receipt_on_closed_budget_is_ignored(self, db):
        budget = await openb(db, max_calls=3)
        async with db() as session:
            await close_budget(session, budget)
            settled = await reconcile_harness_receipt(
                session, RUN_ID, SimpleUsage(input_tokens=10, completeness="aggregate")
            )
            await session.commit()
        assert settled is None
        after = await get_budget(db)
        assert after.consumed_calls == 0

    async def test_receipt_without_a_budget_is_a_noop(self, db):
        async with db() as session:
            assert (
                await reconcile_harness_receipt(session, RUN_ID, SimpleUsage(input_tokens=10))
                is None
            )

    async def test_negative_receipt_figures_are_garbage_not_credits(self, db):
        """A parsed receipt never credits headroom through a negative figure."""
        await openb(db, max_calls=3, max_tokens=10000)
        async with db() as session:
            mixed = await reconcile_harness_receipt(
                session, RUN_ID, SimpleUsage(input_tokens=-5, output_tokens=60)
            )
            await session.commit()
        assert mixed is not None
        # The negative part is dropped; only the valid 60 counts.
        assert mixed.consumed_calls == 1
        assert mixed.consumed_tokens == 60

        async with db() as session:
            garbage = await reconcile_harness_receipt(
                session, RUN_ID, SimpleUsage(input_tokens=-5, output_tokens=-10)
            )
            await session.commit()
        assert garbage is not None
        # Nothing known remains → the tokens stay unknown, never negative.
        assert garbage.consumed_calls == 2
        assert garbage.consumed_tokens == 60  # unchanged


class TestConcurrentExposure:
    """Parallel reserve/settle sessions cannot overshoot the reservable exposure.

    Every counter move is one conditional UPDATE against the live row, so the
    grants that land can never total more than the limit allows: SQLite
    serializes these short transactions exactly the way Postgres row-locks
    the same UPDATE on ``run_budgets``.
    """

    async def test_parallel_reserves_and_settlements_stay_within_the_limits(
        self, tmp_path: Path
    ):
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'budgets.db'}",
            connect_args={"timeout": 30},  # wait out write-lock contention
            poolclass=NullPool,
        )
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                session.add(FlowRun(id=RUN_ID, project_id=1))
                await session.commit()
                budget = await open_budget(
                    session, run_id=RUN_ID, max_calls=3, max_tokens=3000
                )
                await session.commit()
                budget_id = budget.id

            async def dispatch() -> bool:
                async with factory() as session:
                    row = await session.get(RunBudget, budget_id)
                    assert row is not None
                    hold = await reserve(session, row, calls=1, tokens=1000)
                    await session.commit()
                    if hold is None:
                        return False
                    await reconcile_actual(session, hold, actual_calls=1, actual_tokens=1000)
                    await session.commit()
                    return True

            granted = await asyncio.gather(*(dispatch() for _ in range(8)))
            # Exactly the limit's worth of holds was ever granted — never one
            # more, whatever the interleaving.
            assert sum(granted) == 3

            async with factory() as session:
                after = await session.get(RunBudget, budget_id)
                assert after is not None
                # Every grant settled: 3 calls / 3000 tokens, nothing left
                # outstanding or unresolved.
                assert after.consumed_calls == 3
                assert after.consumed_tokens == 3000
                assert after.reserved_calls == 0 and after.reserved_tokens == 0
                assert after.unresolved_calls == 0 and after.unresolved_tokens == 0
                assert after.status == "exhausted"  # fully spent
        finally:
            await engine.dispose()


class SimpleUsage:
    """Duck-typed stand-in for candidate.HarnessUsage in budget tests."""

    def __init__(
        self,
        *,
        input_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        output_tokens: int | None = None,
        completeness: str = "unknown",
    ) -> None:
        self.input_tokens = input_tokens
        self.cached_input_tokens = cached_input_tokens
        self.output_tokens = output_tokens
        self.completeness = completeness


class TestBudgetMigrations:
    def _load_migration(self, filename: str):
        path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / filename
        spec = importlib.util.spec_from_file_location(path.stem, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_upgrade_creates_budget_tables_and_downgrade_drops_them(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import create_engine

        module = self._load_migration("009_run_budgets.py")
        engine = create_engine("sqlite:///:memory:")
        try:
            with engine.connect() as conn:
                ctx = MigrationContext.configure(conn)
                with Operations.context(ctx):
                    module.upgrade()

                inspector = inspect(conn)
                assert "run_budgets" in inspector.get_table_names()
                assert "budget_reservations" in inspector.get_table_names()
                budget_columns = {c["name"] for c in inspector.get_columns("run_budgets")}
                assert {
                    "id",
                    "run_id",
                    "spec_digest",
                    "wallclock_s",
                    "max_calls",
                    "max_tokens",
                    "reserved_calls",
                    "reserved_tokens",
                    "consumed_calls",
                    "consumed_tokens",
                    "status",
                    "created_at",
                    "updated_at",
                } <= budget_columns
                reservation_columns = {
                    c["name"] for c in inspector.get_columns("budget_reservations")
                }
                assert {
                    "id",
                    "run_budget_id",
                    "attempt_id",
                    "reserved_calls",
                    "reserved_tokens",
                    "released",
                } <= reservation_columns
                budget_indexes = {ix["name"] for ix in inspector.get_indexes("run_budgets")}
                assert "ix_run_budgets_run_id" in budget_indexes

                with Operations.context(ctx):
                    module.downgrade()
                # A fresh inspector: table names are cached per instance.
                after = inspect(conn)
                assert "run_budgets" not in after.get_table_names()
                assert "budget_reservations" not in after.get_table_names()
        finally:
            engine.dispose()

    def test_liability_migration_adds_and_drops_the_unresolved_columns(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import create_engine

        m009 = self._load_migration("009_run_budgets.py")
        m011 = self._load_migration("011_budget_unresolved_liability.py")
        engine = create_engine("sqlite:///:memory:")
        try:
            with engine.connect() as conn:
                ctx = MigrationContext.configure(conn)
                with Operations.context(ctx):
                    m009.upgrade()
                    m011.upgrade()

                inspector = inspect(conn)
                columns = {c["name"] for c in inspector.get_columns("run_budgets")}
                assert {"unresolved_calls", "unresolved_tokens"} <= columns

                with Operations.context(ctx):
                    m011.downgrade()
                after = inspect(conn)
                remaining = {c["name"] for c in after.get_columns("run_budgets")}
                assert not {"unresolved_calls", "unresolved_tokens"} & remaining
        finally:
            engine.dispose()

    def test_models_match_the_migrated_columns(self):
        """The ORM tables declare exactly the columns the migrations create."""
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import create_engine

        m009 = self._load_migration("009_run_budgets.py")
        m011 = self._load_migration("011_budget_unresolved_liability.py")
        engine = create_engine("sqlite:///:memory:")
        try:
            with engine.connect() as conn:
                ctx = MigrationContext.configure(conn)
                with Operations.context(ctx):
                    m009.upgrade()
                    m011.upgrade()

                inspector = inspect(conn)
                budget_columns = {c["name"] for c in inspector.get_columns("run_budgets")}
                reservation_columns = {
                    c["name"] for c in inspector.get_columns("budget_reservations")
                }
        finally:
            engine.dispose()

        from forge.durable.models import BudgetReservation, RunBudget

        assert {c.name for c in RunBudget.__table__.columns} == budget_columns
        assert {c.name for c in BudgetReservation.__table__.columns} == reservation_columns
