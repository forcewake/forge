"""Tests for run budgets: reserve before dispatch, reconcile actuals (F22).

Covers the ADR-0018 §5 contract on top of ADR-0013's reserve-then-reconcile:

- ``open_budget`` is idempotent per run and reads limits from the RunSpec's
  ``budgets`` block;
- ``reserve`` refuses (and marks ``exhausted``) with a single conditional
  UPDATE — the live row counters decide, never an ORM snapshot, and the
  exposure it checks is ``consumed + reserved + unresolved``: spent and
  unreported budget is never grantable again;
- ``reconcile_actual`` moves reserved→consumed exactly once, records
  under-reserved actuals, and parks unknown usage as unresolved liability
  instead of counting it as zero;
- the LLM client raises ``budget_exhausted`` BEFORE the provider is contacted
  and reconciles real usage afterwards (failures still consume the call);
- harness usage receipts reconcile the run budget (aggregate = 1 call +
  reported tokens; unknown leaves tokens untouched and stops a hard-limited
  budget rather than reading as capacity).
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.durable import FlowRun, RunBudget
from forge.durable.budgets import (
    BudgetGuard,
    budget_block_reason,
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
        """R12: spent budget is not grantable — exposure includes consumed."""
        budget = await openb(db, max_calls=100, max_tokens=100)
        spent = await session_reserve(db, budget, calls=1, tokens=90)
        assert spent is not None
        async with db() as session:
            settled = await reconcile_actual(session, spent, actual_calls=1, actual_tokens=90)
            await session.commit()
        assert settled.status == "open"  # 90 spent, 10 of provable headroom
        assert settled.consumed_tokens == 90

        # 90 spent + 0 reserved + 20 requested > 100: denied before the
        # provider is contacted, even though reserved was 0.
        assert await session_reserve(db, budget, calls=1, tokens=20) is None
        after = await get_budget(db)
        assert after.status == "exhausted"
        assert after.consumed_tokens == 90  # the refusal spent nothing

    async def test_reserve_rejects_amounts_that_would_create_budget(self, db):
        """Counter moves are ``counter = counter + delta``: a negative delta
        would mint capacity, a zero-call hold under-reserves the dispatch."""
        budget = await openb(db, max_calls=5, max_tokens=100)
        for kwargs in (
            {"calls": 0, "tokens": 10},
            {"calls": -1, "tokens": 0},
            {"calls": 1, "tokens": -5},
        ):
            with pytest.raises(ValueError):
                await session_reserve(db, budget, **kwargs)
        after = await get_budget(db)
        assert after.status == "open"
        assert after.reserved_calls == 0 and after.reserved_tokens == 0
        assert await reservations(db) == []  # no audit row for a bad shape

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

    async def test_unknown_actuals_become_liability_not_zero_spend(self, db):
        budget = await openb(db, max_calls=2, max_tokens=1000)
        reservation = await session_reserve(db, budget, calls=1, tokens=400)
        async with db() as session:
            settled = await reconcile_actual(
                session, reservation, actual_calls=1, actual_tokens=None
            )
            await session.commit()
        # Unknown tokens are never counted as zero — and never released as
        # spendable: the hold parks in the unresolved counters, so the
        # fenced capacity stays fenced for the rest of the run.
        assert settled.consumed_tokens == 0
        assert settled.reserved_tokens == 0
        assert settled.unresolved_tokens == 400
        assert settled.status == "open"  # 400 liability, 600 provable headroom

        assert await session_reserve(db, budget, calls=1, tokens=600) is not None
        assert await session_reserve(db, budget, calls=1, tokens=100) is None

    async def test_fully_unknown_receipt_parks_the_whole_hold(self, db):
        """A cancelled dispatch reports nothing: the whole hold stays fenced."""
        budget = await openb(db, max_calls=1, max_tokens=500)
        reservation = await session_reserve(db, budget, calls=1, tokens=400)
        async with db() as session:
            settled = await reconcile_actual(session, reservation)
            await session.commit()
        assert settled.consumed_calls == 0 and settled.consumed_tokens == 0
        assert settled.reserved_calls == 0 and settled.reserved_tokens == 0
        assert settled.unresolved_calls == 1 and settled.unresolved_tokens == 400
        # Exposure (1 call + 400 tokens) is unchanged: nothing reopened, and
        # the liability alone exhausts the single-call budget.
        assert settled.status == "exhausted"

    async def test_reconcile_rejects_negative_actuals(self, db):
        budget = await openb(db, max_calls=3, max_tokens=1000)
        reservation = await session_reserve(db, budget, calls=1, tokens=100)
        async with db() as session:
            with pytest.raises(ValueError):
                await reconcile_actual(session, reservation, actual_calls=1, actual_tokens=-5)
            await session.rollback()
        after = await get_budget(db)
        assert after.consumed_tokens == 0
        assert after.reserved_tokens == 100  # the hold is intact

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
        assert again.reserved_calls == 0  # never driven negative by a retry
        assert again.unresolved_calls == 0 and again.unresolved_tokens == 0

    async def test_interleaved_moves_never_overshoot_exposure(self, db):
        """Every granted move keeps consumed + reserved + unresolved <= limit."""
        budget = await openb(db, max_calls=4, max_tokens=1000)
        for index in range(6):
            hold = await session_reserve(db, budget, calls=1, tokens=300)
            if hold is None:
                break
            after = await get_budget(db)
            assert after.consumed_calls + after.reserved_calls + after.unresolved_calls <= 4
            assert after.consumed_tokens + after.reserved_tokens + after.unresolved_tokens <= 1000
            if index % 2 == 0:  # settle half the holds with a smaller actual
                async with db() as session:
                    await reconcile_actual(session, hold, actual_calls=1, actual_tokens=150)
                    await session.commit()
        final = await get_budget(db)
        assert final.consumed_calls + final.reserved_calls + final.unresolved_calls <= 4
        assert final.consumed_tokens + final.reserved_tokens + final.unresolved_tokens <= 1000

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

    async def test_unknown_completeness_stops_a_hard_limited_budget(self, db):
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
        # This path holds no reservation, so there is no liability figure to
        # park: against a hard token limit the budget stops instead of
        # reading the unknown as spendable headroom.
        assert settled.status == "exhausted"

    async def test_unknown_completeness_on_an_unlimited_token_axis_stays_open(self, db):
        await openb(db, max_calls=3)
        unknown = SimpleUsage(completeness="unknown")
        async with db() as session:
            settled = await reconcile_harness_receipt(session, RUN_ID, unknown)
            await session.commit()
        assert settled is not None
        assert settled.consumed_calls == 1
        assert settled.status == "open"  # no hard token limit to protect

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


async def age_budget(db, budget_id: str, *, seconds: int) -> None:
    """Rewind a budget's created_at — the durable wall-clock anchor."""
    from datetime import datetime, timedelta, timezone

    async with db() as session:
        budget = await session.get(RunBudget, budget_id)
        assert budget is not None
        budget.created_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        await session.commit()


class TestWallclockEnforcement:
    """R13: wallclock_s is ENFORCED at reservation time, not just recorded."""

    async def test_expired_wallclock_refuses_and_exhausts(self, db):
        budget = await openb(db, wallclock_s=100)
        await age_budget(db, budget.id, seconds=200)

        # A fresh snapshot (the guard reloads per reserve in real flows).
        async with db() as session:
            fresh = await budget_for_run(session, RUN_ID)
            assert fresh is not None
            assert await reserve(session, fresh, calls=1, tokens=0) is None
            await session.commit()
        after = await get_budget(db)
        assert after.status == "exhausted"  # the stop is durable
        assert after.consumed_calls == 0

    async def test_live_wallclock_still_grants(self, db):
        budget = await openb(db, wallclock_s=3600)
        assert await session_reserve(db, budget, calls=1, tokens=0) is not None

    async def test_exhausted_budget_refuses_further_reserves(self, db):
        """The episode gate exhausted it; reserve must refuse cleanly too."""
        budget = await openb(db, max_calls=1)
        hold = await session_reserve(db, budget, calls=1, tokens=0)
        assert hold is not None
        async with db() as session:
            fresh = await budget_for_run(session, RUN_ID)
            assert fresh is not None
            await reconcile_actual(session, hold, actual_calls=1)
            await session.commit()
        again = await openb(db, max_calls=1)  # re-load the exhausted row
        assert again.status == "exhausted"
        assert await session_reserve(db, again, calls=1, tokens=0) is None

    async def test_guard_refuses_expired_wallclock_before_any_http(self, db, httpx_mock):
        await openb(db, wallclock_s=10)
        await age_budget(db, (await get_budget(db)).id, seconds=20)
        guard = await load_budget_guard(db, RUN_ID)
        assert guard is not None

        client = LLMClient(settings=_settings(), session_factory=db, budget=guard)
        with pytest.raises(LLMError, match="budget_exhausted"):
            await client.complete(
                tier="strong", system="s", user="u", role="planner", flow_run_id=RUN_ID
            )
        await client.close()
        # The provider was never contacted.
        assert httpx_mock.get_requests() == []
        after = await guard.refresh()
        assert after is not None and after.status == "exhausted"


class TestBudgetBlockReason:
    async def test_no_budget_and_open_budget_allow_work(self, db):
        async with db() as session:
            assert await budget_block_reason(session, RUN_ID) is None
        await openb(db)
        async with db() as session:
            fresh = await budget_for_run(session, RUN_ID)
            assert fresh is not None
            assert await budget_block_reason(session, RUN_ID) is None

    async def test_exhausted_budget_names_the_block(self, db):
        budget = await openb(db, max_calls=1)
        hold = await session_reserve(db, budget, calls=1, tokens=0)
        assert hold is not None
        async with db() as session:
            fresh = await budget_for_run(session, RUN_ID)
            assert fresh is not None
            await reconcile_actual(session, hold, actual_calls=1)
            await session.commit()
            reason = await budget_block_reason(session, RUN_ID)
        assert reason is not None and reason.startswith("budget_exhausted")

    async def test_spent_wallclock_blocks_and_exhausts_durably(self, db):
        await openb(db, wallclock_s=50)
        await age_budget(db, (await get_budget(db)).id, seconds=100)
        async with db() as session:
            reason = await budget_block_reason(session, RUN_ID)
            await session.commit()
        assert reason is not None and "wall clock" in reason
        assert (await get_budget(db)).status == "exhausted"

    async def test_closed_budget_blocks_nothing(self, db):
        """Closed = terminal-run bookkeeping; the gate has nothing to add."""
        await openb(db, max_calls=1)
        async with db() as session:
            fresh = await budget_for_run(session, RUN_ID)
            assert fresh is not None
            await close_budget(session, fresh)
            await session.commit()
            assert await budget_block_reason(session, RUN_ID) is None


class TestReceiptDedupe:
    async def test_same_key_reconciles_once(self, db):
        await openb(db, max_calls=5)
        usage = SimpleUsage(input_tokens=21, output_tokens=7, completeness="aggregate")
        async with db() as session:
            first = await reconcile_harness_receipt(
                session, RUN_ID, usage, dedupe_key="episode-1"
            )
            assert first is not None
            assert first.consumed_calls == 1
            again = await reconcile_harness_receipt(
                session, RUN_ID, usage, dedupe_key="episode-1"
            )
            await session.commit()
        assert again is not None
        assert again.consumed_calls == 1  # the repeat was a no-op
        assert again.consumed_tokens == 28

    async def test_different_keys_reconcile_separately(self, db):
        await openb(db, max_calls=5)
        usage = SimpleUsage(input_tokens=10, completeness="aggregate")
        async with db() as session:
            await reconcile_harness_receipt(session, RUN_ID, usage, dedupe_key="ep-1")
            settled = await reconcile_harness_receipt(session, RUN_ID, usage, dedupe_key="ep-2")
            await session.commit()
        assert settled is not None
        assert settled.consumed_calls == 2

    async def test_no_key_keeps_the_legacy_count_always_behavior(self, db):
        await openb(db, max_calls=5)
        usage = SimpleUsage(input_tokens=10, completeness="aggregate")
        async with db() as session:
            await reconcile_harness_receipt(session, RUN_ID, usage)
            settled = await reconcile_harness_receipt(session, RUN_ID, usage)
            await session.commit()
        assert settled is not None
        assert settled.consumed_calls == 2  # unkeyed callers keep today's shape

    async def test_claims_survive_a_closed_then_reconciled_race(self, db):
        """A claim consumed on a budget that is exhausted afterwards still
        records actuals (the F22 posture), but never revives capacity."""
        await openb(db, max_calls=1)
        usage = SimpleUsage(input_tokens=10, completeness="aggregate")
        async with db() as session:
            await reconcile_harness_receipt(session, RUN_ID, usage, dedupe_key="ep-1")
            settled = await reconcile_harness_receipt(session, RUN_ID, usage, dedupe_key="ep-1")
            await session.commit()
        assert settled is not None
        assert settled.status == "exhausted"  # 1 of 1 calls spent
        assert settled.consumed_calls == 1


class TestMigration009:
    def _load_migration(self):
        path = (
            Path(__file__).resolve().parent.parent / "alembic" / "versions" / "009_run_budgets.py"
        )
        spec = importlib.util.spec_from_file_location("migration_009", path)
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
            # 011: unresolved usage liability (unknown ≠ zero, never spendable).
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
    def _load_migration(self, module_name: str, filename: str) -> ModuleType:
        path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / filename
        spec = importlib.util.spec_from_file_location(module_name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_upgrade_adds_the_unresolved_counters_and_downgrade_drops_them(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import create_engine

        base = self._load_migration("migration_009", "009_run_budgets.py")
        head = self._load_migration("migration_011", "011_budget_unresolved_liability.py")
        engine = create_engine("sqlite:///:memory:")
        try:
            with engine.connect() as conn:
                ctx = MigrationContext.configure(conn)
                with Operations.context(ctx):
                    base.upgrade()
                    head.upgrade()

                inspector = inspect(conn)
                budget_columns = {c["name"] for c in inspector.get_columns("run_budgets")}
                assert {"unresolved_calls", "unresolved_tokens"} <= budget_columns

                with Operations.context(ctx):
                    head.downgrade()
                # A fresh inspector: table names are cached per instance.
                after = inspect(conn)
                after_columns = {c["name"] for c in after.get_columns("run_budgets")}
                assert "unresolved_calls" not in after_columns
                assert "unresolved_tokens" not in after_columns
        finally:
            engine.dispose()
