"""Tests for run budgets: reserve before dispatch, reconcile actuals (F22).

Covers the ADR-0018 §5 contract on top of ADR-0013's reserve-then-reconcile:

- ``open_budget`` is idempotent per run and reads limits from the RunSpec's
  ``budgets`` block;
- ``reserve`` enforces *exposure* — consumed + unresolved liability +
  outstanding holds + requested — with a single conditional UPDATE: the live
  row counters decide, never an ORM snapshot, and spent budget is never
  re-lent;
- ``reconcile_actual`` moves reserved→consumed exactly once, records
  under-reserved actuals in full, and re-books a hold whose actual stayed
  unknown as unresolved liability (unknown is never counted as zero, and it
  never opens hard-budget capacity);
- the LLM client raises ``budget_exhausted`` BEFORE the provider is contacted
  and reconciles real usage afterwards (failures still consume the call);
- harness usage receipts reconcile the run budget (aggregate = 1 call +
  reported tokens; unknown exhausts a hard token budget instead of leaving
  it open at zero spend).
"""

import asyncio
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from forge.durable import FlowRun, RunBudget
from forge.durable.budgets import (
    BudgetGuard,
    Reservation,
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

    async def test_reserve_counts_consumed_against_the_limit(self, db):
        """R12: spent budget is gone even with no hold outstanding.

        90 spent + 0 reserved + 20 requested at limit 100 used to pass the
        predicate (reserved + requested only) and lend the spent 90 a second
        time; the refusal now lands before any dispatch.
        """
        budget = await openb(db, max_calls=100, max_tokens=100)
        hold = await session_reserve(db, budget, calls=1, tokens=90)
        assert hold is not None
        async with db() as session:
            settled = await reconcile_actual(session, hold, actual_calls=1, actual_tokens=90)
            await session.commit()
        assert settled is not None
        assert settled.status == "open"  # 90 of 100: headroom remains
        assert settled.reserved_tokens == 0 and settled.consumed_tokens == 90

        assert await session_reserve(db, budget, calls=1, tokens=20) is None
        after = await get_budget(db)
        assert after.status == "exhausted"
        assert after.consumed_tokens == 90  # the refusal itself spent nothing

    async def test_reserve_counts_outstanding_holds_against_the_limit(self, db):
        """The reserved half of the exposure formula, isolated."""
        budget = await openb(db, max_tokens=100)
        assert await session_reserve(db, budget, calls=1, tokens=60) is not None
        # 0 spent + 60 held + 40 requested fits exactly; 61 does not.
        assert await session_reserve(db, budget, calls=1, tokens=41) is None
        assert (await get_budget(db)).status == "exhausted"

    async def test_reserve_counts_unresolved_liability_against_the_limit(self, db):
        """A hold whose actual stayed unknown keeps occupying the budget."""
        budget = await openb(db, max_tokens=500)
        hold = await session_reserve(db, budget, calls=1, tokens=400)
        assert hold is not None
        async with db() as session:
            settled = await reconcile_actual(session, hold, actual_calls=1, actual_tokens=None)
            await session.commit()
        assert settled is not None
        assert settled.reserved_tokens == 0
        assert settled.unresolved_tokens == 400  # released, not forgotten

        # Only 500 - 400 of headroom remains: 100 fits, 200 does not. (The
        # grantable request comes first — a refusal durably exhausts.)
        assert await session_reserve(db, budget, calls=1, tokens=100) is not None
        assert await session_reserve(db, budget, calls=1, tokens=200) is None

    async def test_reserve_rejects_requests_that_mint_capacity(self, db):
        """Negative/non-integer requests would move counters backwards."""
        budget = await openb(db, max_calls=5, max_tokens=100)
        async with db() as session:
            with pytest.raises(ValueError):
                await reserve(session, budget, calls=-1)
            with pytest.raises(ValueError):
                await reserve(session, budget, tokens=-5)
            with pytest.raises(ValueError):
                await reserve(session, budget, calls=True)
        after = await get_budget(db)
        assert after.status == "open"  # a caller bug never exhausts a budget
        assert after.reserved_calls == 0 and after.reserved_tokens == 0
        assert await reservations(db) == []  # no audit row for a rejected hold

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


async def session_reserve(db, budget, *, calls: int, tokens: int):
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

    async def test_unknown_actuals_become_liability_never_zero(self, db):
        budget = await openb(db, max_calls=2, max_tokens=1000)
        reservation = await session_reserve(db, budget, calls=1, tokens=400)
        async with db() as session:
            settled = await reconcile_actual(
                session, reservation, actual_calls=1, actual_tokens=None
            )
            await session.commit()
        # Unknown tokens are never counted as zero: consumed does not move.
        # The hold IS released, but re-booked as unresolved liability — the
        # dispatch agreed to that bound, so it keeps occupying the budget.
        assert settled.consumed_tokens == 0
        assert settled.reserved_tokens == 0
        assert settled.unresolved_tokens == 400
        assert settled.status == "open"  # 400 exposed of 1000: headroom remains

    async def test_known_actuals_release_their_hold_entirely(self, db):
        """A receipt under the estimate restores the headroom it over-held."""
        budget = await openb(db, max_tokens=1000)
        reservation = await session_reserve(db, budget, calls=1, tokens=800)
        async with db() as session:
            settled = await reconcile_actual(
                session, reservation, actual_calls=1, actual_tokens=100
            )
            await session.commit()
        assert settled.consumed_tokens == 100
        assert settled.reserved_tokens == 0
        assert settled.unresolved_tokens == 0  # nothing unknown, nothing held

    async def test_reconcile_rejects_actuals_that_mint_capacity(self, db):
        budget = await openb(db, max_calls=5, max_tokens=100)
        reservation = await session_reserve(db, budget, calls=1, tokens=40)
        assert reservation is not None
        async with db() as session:
            with pytest.raises(ValueError):
                await reconcile_actual(session, reservation, actual_calls=-1)
            with pytest.raises(ValueError):
                await reconcile_actual(session, reservation, actual_tokens=-10)
            await session.commit()
        after = await get_budget(db)
        assert after.reserved_tokens == 40  # the hold survives the bad call
        assert after.consumed_tokens == 0 and after.unresolved_tokens == 0
        (row,) = await reservations(db)
        assert row.released is False  # still settleable with a real receipt

    async def test_reconcile_is_exactly_once(self, db):
        budget = await openb(db, max_calls=5, max_tokens=100)
        reservation = await session_reserve(db, budget, calls=1, tokens=40)
        async with db() as session:
            await reconcile_actual(session, reservation, actual_calls=1, actual_tokens=10)
            again = await reconcile_actual(session, reservation, actual_calls=1, actual_tokens=10)
            await session.commit()
        # A crash-retry (released already flipped) cannot double-apply.
        assert again.consumed_calls == 1
        assert again.consumed_tokens == 10
        assert again.reserved_tokens == 0
        assert again.unresolved_tokens == 0

    async def test_late_settlement_after_exhaustion_counts_once(self, db):
        """A straggler receipt lands once and never drives a hold negative."""
        budget = await openb(db, max_calls=1, max_tokens=100)
        hold = await session_reserve(db, budget, calls=1, tokens=40)
        assert hold is not None
        # The last call is gone while the first dispatch is in flight: the
        # competing attempt is refused and durably exhausts the budget.
        assert await session_reserve(db, budget, calls=1, tokens=60) is None
        assert (await get_budget(db)).status == "exhausted"

        async with db() as session:
            settled = await reconcile_actual(session, hold, actual_calls=1, actual_tokens=30)
            await session.commit()
        assert settled is not None
        assert settled.status == "exhausted"  # a late receipt never revives it
        assert settled.consumed_tokens == 30  # recorded exactly once
        assert settled.reserved_tokens == 0  # released, not negative

    async def test_call_limit_exhausts_on_reconcile(self, db):
        budget = await openb(db, max_calls=1)
        reservation = await session_reserve(db, budget, calls=1, tokens=0)
        async with db() as session:
            settled = await reconcile_actual(session, reservation, actual_calls=1)
            await session.commit()
        assert settled.consumed_calls == 1
        assert settled.status == "exhausted"
        assert settled.reserved_calls == 0


class TestExposureUnderConcurrency:
    """Parallel reserves and settlements can never pass the reservable exposure.

    The ``db`` fixture's StaticPool funnels everything through one connection;
    these tests use a temp-file database so every session gets its own
    connection and the holds genuinely race. Each task opens its session with
    the counter move itself — no read beforehand, a detached id-only handle
    instead of a loaded row — so, exactly as against Postgres, the one
    conditional UPDATE decides every grant over the row's live counters,
    whichever order the tasks run in.
    """

    @pytest.fixture()
    async def raced_db(self, tmp_path):
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'budgets.db'}",
            connect_args={"check_same_thread": False, "timeout": 30},
            poolclass=NullPool,
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add(FlowRun(id=RUN_ID, project_id=1))
            await session.commit()
        yield factory
        await engine.dispose()

    async def test_parallel_reserves_never_overshoot_the_limit(self, raced_db):
        limit, size, attempts = 10, 2, 10
        budget = await openb(raced_db, max_tokens=limit)

        async def attempt() -> Reservation | None:
            async with raced_db() as session:
                hold = await reserve(session, RunBudget(id=budget.id), calls=1, tokens=size)
                await session.commit()
                return hold

        holds = await asyncio.gather(*[attempt() for _ in range(attempts)])
        granted = [hold for hold in holds if hold is not None]
        # Ten tokens of headroom at two per hold: exactly five grants fit, no
        # matter how the ten attempts interleave.
        assert len(granted) == limit // size

        after = await get_budget(raced_db)
        assert after.reserved_tokens + after.unresolved_tokens + after.consumed_tokens == limit
        assert after.reserved_calls == len(granted)
        assert len(await reservations(raced_db)) == len(granted)

    async def test_interleaved_settlements_keep_the_counters_exact(self, raced_db):
        budget = await openb(raced_db, max_calls=50, max_tokens=200)

        async def attempt(index: int) -> Reservation | None:
            async with raced_db() as session:
                hold = await reserve(session, RunBudget(id=budget.id), calls=1, tokens=5)
                await session.commit()
            if hold is None:
                return None
            async with raced_db() as session:
                if index % 2:
                    await reconcile_actual(session, hold)  # unknown → liability
                else:
                    await reconcile_actual(session, hold, actual_calls=1, actual_tokens=5)
                await session.commit()
            return hold

        # Twenty holds of five tokens against a 200 limit: none may be refused,
        # and every settlement must land exactly once.
        settled = await asyncio.gather(*[attempt(i) for i in range(20)])
        assert all(hold is not None for hold in settled)

        after = await get_budget(raced_db)
        # Twenty settled holds: the ten known receipts in consumed, the ten
        # unknown ones as liability, nothing left reserved, nothing lost.
        assert after.reserved_calls == 0 and after.reserved_tokens == 0
        assert after.consumed_calls == 10 and after.consumed_tokens == 50
        assert after.unresolved_calls == 10 and after.unresolved_tokens == 50
        assert after.status == "open"  # exposure 100 of 200: headroom remains


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

    async def test_spent_budget_refuses_before_the_provider_is_called(self, db, httpx_mock):
        """R12 acceptance: 90 spent + 0 reserved + 20 requested at limit 100."""
        await openb(db, max_calls=100, max_tokens=100)
        guard = await load_budget_guard(db, RUN_ID)
        assert guard is not None
        hold = await guard.reserve(calls=1, tokens=90)
        assert hold is not None
        await guard.reconcile(hold, actual_calls=1, actual_tokens=90)

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
        assert after is not None
        assert after.status == "exhausted"
        assert after.consumed_tokens == 90  # the refusal spent nothing

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
        # the token actuals stay unknown (never zero), the hold is released.
        assert after.consumed_calls == 1
        assert after.consumed_tokens == 0
        assert after.reserved_tokens == 0
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

    async def test_unknown_completeness_exhausts_a_hard_token_budget(self, db):
        """An uncosted receipt on a hard budget never reads as zero spend.

        The harness path holds no reservation, so an unknown token figure has
        no bound to book as liability — the honest state is "headroom
        unprovable", not a budget that is open at full capacity.
        """
        await openb(db, max_calls=3, max_tokens=10000)
        unknown = SimpleUsage(input_tokens=None, output_tokens=None, completeness="unknown")
        async with db() as session:
            settled = await reconcile_harness_receipt(session, RUN_ID, unknown)
            await session.commit()
        assert settled is not None
        # The call counts; the tokens stay unknown — never zeroed into a
        # fabricated 0 that would falsify the total.
        assert settled.consumed_calls == 1
        assert settled.consumed_tokens == 0
        assert settled.unresolved_tokens == 0  # no figure is invented either
        assert settled.status == "exhausted"

    async def test_unknown_completeness_leaves_an_unlimited_budget_open(self, db):
        await openb(db, max_calls=3)  # no token limit: unknown cannot breach it
        unknown = SimpleUsage(input_tokens=None, output_tokens=None, completeness="unknown")
        async with db() as session:
            settled = await reconcile_harness_receipt(session, RUN_ID, unknown)
            await session.commit()
        assert settled is not None
        assert settled.consumed_calls == 1
        assert settled.status == "open"

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

    async def test_negative_token_figures_are_not_known_usage(self, db):
        await openb(db, max_calls=3, max_tokens=10000)
        weird = SimpleUsage(input_tokens=-50, output_tokens=100, completeness="aggregate")
        async with db() as session:
            settled = await reconcile_harness_receipt(session, RUN_ID, weird)
            await session.commit()
        assert settled is not None
        # A negative count is not a receipt: summing it would mint capacity,
        # so only the real figure is recorded.
        assert settled.consumed_tokens == 100
        assert settled.status == "open"

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


class TestMigration009:
    def _load_migration(self, name: str = "009_run_budgets.py"):
        path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / name
        spec = importlib.util.spec_from_file_location(f"migration_{name[:-3]}", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_upgrade_creates_budget_tables_and_downgrade_drops_them(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import create_engine

        module = self._load_migration()
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

    def test_models_match_the_migrated_columns(self):
        """The ORM tables declare exactly the columns the migrations create."""
        from forge.durable.models import BudgetReservation, RunBudget

        assert {c.name for c in RunBudget.__table__.columns} == {
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
            "unresolved_calls",
            "unresolved_tokens",
            "status",
            "created_at",
            "updated_at",
        }
        assert {c.name for c in BudgetReservation.__table__.columns} == {
            "id",
            "run_budget_id",
            "attempt_id",
            "reserved_calls",
            "reserved_tokens",
            "released",
        }


class TestMigration011:
    """The unresolved-liability columns the reserve predicate counts."""

    _CHAIN = ("009_run_budgets.py", "010_security_findings.py", "011_run_budget_unresolved.py")

    def _run(self, name: str, conn, *, direction: str = "upgrade") -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        module = TestMigration009()._load_migration(name)
        with Operations.context(MigrationContext.configure(conn)):
            getattr(module, direction)()

    def test_upgrade_adds_the_unresolved_columns_and_downgrade_drops_them(self):
        from sqlalchemy import create_engine

        engine = create_engine("sqlite:///:memory:")
        try:
            with engine.connect() as conn:
                for name in self._CHAIN:
                    self._run(name, conn)

                columns = {c["name"] for c in inspect(conn).get_columns("run_budgets")}
                assert {"unresolved_calls", "unresolved_tokens"} <= columns

                self._run("011_run_budget_unresolved.py", conn, direction="downgrade")
                after = {c["name"] for c in inspect(conn).get_columns("run_budgets")}
                assert "unresolved_calls" not in after and "unresolved_tokens" not in after
        finally:
            engine.dispose()

