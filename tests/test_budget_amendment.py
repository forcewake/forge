"""R40-04 (#340) — budget amendments effective in the ACTUAL enforcing
guard.

The recorded defect: ``continue_review_only`` recorded amount/reason/
operator into run EVIDENCE, released the marker and re-ran the review
against the SAME ``RunBudget`` limits and the same ``BudgetGuard`` — a
dollar-only annotation cannot reopen an exhausted call or token budget.
These tests pin the fix:

- **AT-04 (the review's acceptance bar)**: a REAL ``LLMClient`` over a
  recording transport with the ACTUAL ``BudgetGuard`` — exhaust the real
  guard, a permitted amendment changes the appropriate enforcement
  capacity, EXACTLY ONE reviewer call becomes possible, and an unchanged
  USD cap does NOT silently permit more calls/tokens (refused with the
  limiting axis named).
- **Command identity**: the amendment rides the ORIGINATING NATIVE
  COMMAND identity — two identical amount/reason commands are two
  decisions; a redelivery of ONE command applies once.
- **Refusals**: closed budgets, unlimited axes and past-deadline
  wallclock extensions refuse with the limiting axis named and stay
  recorded (``status='refused'``) for audit.
- **The closing partition**: the implementation reservation purpose
  cannot enter the protected share; the closing purpose can — through
  the real :func:`reserve` admission path, on the columns frozen at open
  time.
- **The receipt front door**: ``ingest_usage_receipt`` persists the
  canonical cost/finality columns — a legacy receipt with a raw cost but
  no new columns is classified explicitly, never silently zero.
- **Recovery**: a restart between the amendment commit and the review
  dispatch loses neither the top-up nor runs the review twice; a lost
  review response is never treated as a new amendment.
- **Real-PostgreSQL races** (``FORGE_PG_TEST_URL``-gated): two
  amendments and a reservation raced across independent sessions.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import Settings
from forge.durable import (
    AXIS_CALLS,
    AXIS_TOKENS,
    AXIS_USD,
    AXIS_WALLCLOCK,
    RESERVE_CLOSING,
    RESERVE_IMPLEMENTATION,
    AppliedBudgetAmendment,
    BudgetAmendmentCommand,
    FlowRun,
    apply_budget_amendment,
    budget_amendments_for_run,
    budget_for_run,
    close_budget,
    ingest_usage_receipt,
    limiting_axis,
    load_budget_guard,
    open_budget,
    reserve,
    usd_amendment_total,
)
from forge.durable.models import BudgetAmendment
from forge.factory.llm import LLMClient, LLMError
from forge.models.base import Base

RUN_ID = "r" * 32
#: The native command identity an operator note carries (the inbox
#: ``source_event_id`` shape — ``run:<command>:<project>:<note id>``).
CMD_A = "run:continue_review:42:9101"
CMD_B = "run:continue_review:42:9102"
CMD_C = "run:continue_review:42:9103"


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


def _settings() -> Settings:
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


async def openb(db, **limits) -> None:
    async with db() as session:
        await open_budget(session, run_id=RUN_ID, **limits)
        await session.commit()


async def budget_row(db):
    async with db() as session:
        row = await budget_for_run(session, RUN_ID)
        assert row is not None
        session.expunge(row)
        return row


async def amendment_rows(db) -> list[BudgetAmendment]:
    async with db() as session:
        rows = await budget_amendments_for_run(session, RUN_ID)
        for row in rows:
            session.expunge(row)
        return rows


def amendment(**kwargs) -> BudgetAmendmentCommand:
    values = {
        "run_id": RUN_ID,
        "command_id": CMD_A,
        "axis": AXIS_CALLS,
        "amount": 1,
        "reason": "close the promised review",
        "operator": "human:alice",
    }
    values.update(kwargs)
    return BudgetAmendmentCommand(**values)


async def apply(db, command: BudgetAmendmentCommand) -> AppliedBudgetAmendment:
    async with db() as session:
        applied = await apply_budget_amendment(session, command)
        await session.commit()
        return applied


def reviewer_call(client_kwargs: dict | None = None) -> dict:
    """The reviewer's real call shape (factory/reviewer.py: one call, one
    JSON-mode completion against the reviewer tier)."""
    kwargs = {
        "tier": "strong",
        "system": "s",
        "user": "u",
        "role": "reviewer",
        "flow_run_id": RUN_ID,
        "json_mode": True,
        "max_tokens": 4096,
    }
    kwargs.update(client_kwargs or {})
    return kwargs


# ----------------------------------------------------------------------
# AT-04 — the real LLMClient + the ACTUAL BudgetGuard
# ----------------------------------------------------------------------


class TestAmendmentOpensTheRealGuard:
    """The acceptance bar: exhaust the REAL guard through the REAL client
    (a recording transport proves what reached the provider), then a
    permitted amendment changes the enforcement capacity and EXACTLY ONE
    reviewer call becomes possible."""

    def make_client(self, db, guard) -> LLMClient:
        return LLMClient(settings=_settings(), session_factory=db, budget=guard)

    async def test_a_calls_amendment_permits_exactly_one_more_reviewer_call(self, db, httpx_mock):
        await openb(db, max_calls=2, max_tokens=1_000_000)
        guard = await load_budget_guard(db, RUN_ID)
        closing_guard = await load_budget_guard(db, RUN_ID, purpose=RESERVE_CLOSING)
        assert guard is not None and closing_guard is not None
        client = self.make_client(db, closing_guard)
        try:
            # Two real calls exhaust the guard (the recording transport
            # proves both reached the provider).
            for _ in range(2):
                httpx_mock.add_response(json=completion("{}", prompt_tokens=10))
                await client.complete(**reviewer_call())

            # The third call is refused BEFORE the provider: no response
            # is registered, so any request would fail the test.
            with pytest.raises(LLMError, match="budget_exhausted"):
                await client.complete(**reviewer_call())
            assert httpx_mock.get_requests().__len__() == 2  # nothing new hit the wire
            row = await budget_row(db)
            assert row.status == "exhausted"  # durably exhausted
            async with db() as session:
                assert await limiting_axis(
                    session, RUN_ID, calls=1, tokens=4096, purpose=RESERVE_CLOSING
                ) in (AXIS_CALLS, "status")

            # A permitted CALLS-axis amendment (the axis that actually
            # refuses) raises the enforcing limit atomically and re-opens
            # the exhausted guard.
            applied = await apply(db, amendment(axis=AXIS_CALLS, amount=1))
            assert applied.applied is True
            assert applied.limit_before is not None
            assert applied.limit_before["max_calls"] == 2  # history kept
            assert applied.limit_after is not None
            assert applied.limit_after["max_calls"] == 3
            row = await budget_row(db)
            assert row.status == "open"
            async with db() as session:
                assert (
                    await limiting_axis(
                        session, RUN_ID, calls=1, tokens=4096, purpose=RESERVE_CLOSING
                    )
                    is None
                )

            # EXACTLY ONE more reviewer call becomes possible.
            httpx_mock.add_response(json=completion("{}", prompt_tokens=10))
            result = await client.complete(**reviewer_call())
            assert result.text == "{}"
            assert len(httpx_mock.get_requests()) == 3

            # The one-after-the-amendment call is refused again — the
            # amendment bought exactly one call, not an open lane.
            with pytest.raises(LLMError, match="budget_exhausted"):
                await client.complete(**reviewer_call())
            assert len(httpx_mock.get_requests()) == 3
        finally:
            await client.close()

    async def test_an_unchanged_usd_cap_does_not_silently_permit_calls(self, db, httpx_mock):
        """A usd-axis amendment raises the closing gate's effective cap —
        it must NOT reopen the exhausted CALLS axis: the review is refused
        with the limiting axis named."""
        await openb(db, max_calls=1, max_tokens=1_000_000)
        closing_guard = await load_budget_guard(db, RUN_ID, purpose=RESERVE_CLOSING)
        assert closing_guard is not None
        client = self.make_client(db, closing_guard)
        try:
            httpx_mock.add_response(json=completion("{}", prompt_tokens=10))
            await client.complete(**reviewer_call())  # the single allowed call
            with pytest.raises(LLMError, match="budget_exhausted"):
                await client.complete(**reviewer_call())
            assert len(httpx_mock.get_requests()) == 1

            # The usd amendment applies — the closing gate's cap rises —
            # but the calls axis still refuses the reviewer reservation.
            applied = await apply(db, amendment(axis=AXIS_USD, amount=0.75, command_id=CMD_B))
            assert applied.applied is True
            async with db() as session:
                assert await usd_amendment_total(session, RUN_ID) == pytest.approx(0.75)
            async with db() as session:
                limiting = await limiting_axis(
                    session, RUN_ID, calls=1, tokens=4096, purpose=RESERVE_CLOSING
                )
            assert limiting == AXIS_CALLS  # the limiting axis is NAMED

            # And the real client agrees: no request reaches the provider.
            with pytest.raises(LLMError, match="budget_exhausted"):
                await client.complete(**reviewer_call())
            assert len(httpx_mock.get_requests()) == 1  # transport untouched
        finally:
            await client.close()

    async def test_a_tokens_amendment_opens_the_token_axis_only(self, db, httpx_mock):
        await openb(db, max_calls=100, max_tokens=4096)
        closing_guard = await load_budget_guard(db, RUN_ID, purpose=RESERVE_CLOSING)
        assert closing_guard is not None
        client = self.make_client(db, closing_guard)
        try:
            httpx_mock.add_response(json=completion("{}", prompt_tokens=10))
            await client.complete(**reviewer_call())  # 10 <= 4096 fits
            # The next 4096-token estimate does not fit the 4096 cap.
            with pytest.raises(LLMError, match="budget_exhausted"):
                await client.complete(**reviewer_call())
            assert len(httpx_mock.get_requests()) == 1

            applied = await apply(db, amendment(axis=AXIS_TOKENS, amount=4096))
            assert applied.applied is True
            httpx_mock.add_response(json=completion("{}", prompt_tokens=10))
            await client.complete(**reviewer_call())  # exactly one more
            assert len(httpx_mock.get_requests()) == 2
        finally:
            await client.close()


# ----------------------------------------------------------------------
# Command identity — two decisions vs one redelivery
# ----------------------------------------------------------------------


class TestCommandIdentity:
    async def test_the_durable_row_carries_the_limit_history(self, db):
        """R40-15 live finding: the RESULT carried limit_before/after but the
        ROW did not (applied rows held NULL) — the row is what replays and
        audits read. Stamped at every applied site; the replay carries it."""
        await openb(db, max_calls=2, max_tokens=100)
        applied = await apply(db, amendment())
        assert applied.applied
        (row,) = await amendment_rows(db)
        assert row.status == "applied"
        assert row.limit_before is not None and row.limit_after is not None
        assert row.limit_before != row.limit_after
        assert row.limit_before.get("max_calls") == 2
        assert row.limit_after.get("max_calls") == 3
        replay = await apply(db, amendment())
        assert replay.replayed and not replay.applied
        assert replay.limit_after == row.limit_after

    async def test_a_redelivery_of_one_command_applies_once(self, db):
        await openb(db, max_calls=2)
        command = amendment(axis=AXIS_CALLS, amount=2)
        first = await apply(db, command)
        assert first.applied is True and first.replayed is False
        replay = await apply(db, command)  # the SAME command redelivered
        assert replay.applied is False and replay.replayed is True
        row = await budget_row(db)
        assert row.max_calls == 4  # raised ONCE by 2
        rows = await amendment_rows(db)
        assert len(rows) == 1  # one durable amendment row

    async def test_two_identical_amount_reason_commands_are_two_decisions(self, db):
        await openb(db, max_calls=2)
        first = await apply(db, amendment(axis=AXIS_CALLS, amount=2, command_id=CMD_A))
        second = await apply(db, amendment(axis=AXIS_CALLS, amount=2, command_id=CMD_B))
        assert first.applied is True
        assert second.applied is True and second.replayed is False
        row = await budget_row(db)
        assert row.max_calls == 6  # BOTH decisions moved the limit
        assert len(await amendment_rows(db)) == 2

    async def test_an_amendment_requires_its_command_identity(self):
        with pytest.raises(ValueError, match="command identity"):
            amendment(command_id="   ")

    async def test_the_axis_is_named_and_never_converted(self):
        with pytest.raises(ValueError, match="distinct"):
            amendment(axis="dollars")
        with pytest.raises(ValueError, match="reason"):
            amendment(reason="  ")
        with pytest.raises(ValueError, match="positive"):
            amendment(amount=0)
        with pytest.raises(ValueError, match="whole number"):
            amendment(axis=AXIS_CALLS, amount=1.5)

    async def test_the_usd_amendment_does_not_need_a_guard_row(self, db):
        """The usd axis is enforced at the closing gate over receipts —
        its durable APPLIED row is the enforcement record."""
        applied = await apply(db, amendment(axis=AXIS_USD, amount=1.25, command_id=CMD_C))
        assert applied.applied is True
        async with db() as session:
            assert await usd_amendment_total(session, RUN_ID) == pytest.approx(1.25)


# ----------------------------------------------------------------------
# Refusals — typed, recorded, replayed
# ----------------------------------------------------------------------


class TestRefusedAmendments:
    async def test_a_closed_budget_refuses_and_is_recorded(self, db):
        await openb(db, max_calls=2)
        async with db() as session:
            await close_budget(session, await budget_for_run(session, RUN_ID))
            await session.commit()
        applied = await apply(db, amendment(axis=AXIS_CALLS, amount=2))
        assert applied.applied is False and applied.replayed is False
        assert applied.refusal_reason is not None
        assert applied.refusal_reason.startswith("calls:")
        assert "closed" in applied.refusal_reason
        (row,) = await amendment_rows(db)
        assert row.status == "refused"
        assert row.refusal_reason == applied.refusal_reason

    async def test_an_unlimited_axis_refuses_naming_the_axis(self, db):
        await openb(db, max_tokens=100)  # calls axis unlimited
        applied = await apply(db, amendment(axis=AXIS_CALLS, amount=2))
        assert applied.applied is False
        assert applied.refusal_reason is not None
        assert applied.refusal_reason.startswith("calls:")
        assert "unlimited" in applied.refusal_reason

    async def test_a_wallclock_extension_still_in_the_past_refuses(self, db):
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import update

        from forge.durable.models import RunBudget

        # A budget whose anchored deadline is two hours past.
        async with db() as session:
            await open_budget(session, run_id=RUN_ID, wallclock_s=3600)
            await session.commit()
        async with db() as session:
            await session.execute(
                update(RunBudget)
                .where(RunBudget.run_id == RUN_ID)
                .values(created_at=datetime.now(timezone.utc) - timedelta(seconds=7200))
            )
            await session.commit()
        applied = await apply(db, amendment(axis=AXIS_WALLCLOCK, amount=60))
        assert applied.applied is False
        assert applied.refusal_reason is not None and "wallclock" in applied.refusal_reason
        row = await budget_row(db)
        assert row.status == "exhausted"  # still exhausted, honestly

        # The wallclock axis CAN re-open when the extension reaches now:
        # the deadline is now ~1h past; +7200s of window covers it.
        healed = await apply(db, amendment(axis=AXIS_WALLCLOCK, amount=7200, command_id=CMD_B))
        assert healed.applied is True
        row = await budget_row(db)
        assert row.status == "open"

    async def test_a_redelivery_replays_the_recorded_refusal(self, db):
        await openb(db, max_tokens=100)
        command = amendment(axis=AXIS_CALLS, amount=2)
        first = await apply(db, command)
        assert first.applied is False
        replay = await apply(db, command)
        assert replay.applied is False and replay.replayed is True
        assert replay.refusal_reason == first.refusal_reason
        assert len(await amendment_rows(db)) == 1

    async def test_refused_usd_amendments_never_count(self, db):
        # A usd amendment always applies (the closing gate reads the
        # total); the count-axis refusal path is what must never leak in.
        await apply(db, amendment(axis=AXIS_USD, amount=2.0, command_id=CMD_A))
        async with db() as session:
            assert await usd_amendment_total(session, RUN_ID) == pytest.approx(2.0)


# ----------------------------------------------------------------------
# The closing partition — the real admission path
# ----------------------------------------------------------------------


class TestClosingPartition:
    async def test_the_coder_cannot_reserve_the_protected_share(self, db):
        """max_calls=4 with a closing share of 1: the implementation
        purpose tops out at 3; the closing purpose can use the 4th."""
        await openb(
            db,
            max_calls=4,
            closing_reserved_calls=1,
            closing_partition_policy="closing-partition/1",
        )
        async with db() as session:
            budget = await budget_for_run(session, RUN_ID)
            assert budget is not None
            for _ in range(3):
                assert await reserve(session, budget, calls=1, tokens=0) is not None
            # The 4th implementation reservation enters the share — refused.
            assert await reserve(session, budget, calls=1, tokens=0) is None
            assert (
                await limiting_axis(session, RUN_ID, calls=1, purpose=RESERVE_IMPLEMENTATION)
                == AXIS_CALLS
            )
            # The closing purpose still sees the FULL limit.
            assert (
                await reserve(session, budget, calls=1, tokens=0, purpose=RESERVE_CLOSING)
                is not None
            )
            await session.rollback()

    async def test_the_token_share_is_partitioned_too(self, db):
        await openb(
            db,
            max_tokens=1000,
            closing_reserved_tokens=300,
            closing_partition_policy="closing-partition/1",
        )
        async with db() as session:
            budget = await budget_for_run(session, RUN_ID)
            assert budget is not None
            assert await reserve(session, budget, calls=1, tokens=699) is not None
            # 699 + 2 > 1000 - 300: the implementation purpose refuses
            # (it cannot enter the protected 300-token share).
            assert await reserve(session, budget, calls=1, tokens=2) is None
            # The closing purpose may spend the share: 699 + 300 == 1000.
            assert (
                await reserve(session, budget, calls=1, tokens=300, purpose=RESERVE_CLOSING)
                is not None
            )
            # ...and no more: the full limit is now exposure.
            assert await reserve(session, budget, calls=1, tokens=1) is None
            await session.rollback()

    async def test_no_partition_keeps_the_shared_axes(self, db):
        await openb(db, max_calls=2)
        async with db() as session:
            budget = await budget_for_run(session, RUN_ID)
            assert budget is not None
            for _ in range(2):
                assert await reserve(session, budget, calls=1) is not None
            await session.rollback()


# ----------------------------------------------------------------------
# The receipt front door — canonical cost/finality columns
# ----------------------------------------------------------------------


@dataclass
class SimpleUsage:
    attempt_id: str = "att-1"
    receipt_id: str = ""
    driver: str = ""
    model: str = ""
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    completeness: str = "aggregate"
    source: str = ""
    raw: dict | None = None
    cost_usd: float | None = None
    cost_basis: str | None = None
    final: bool | None = None


async def receipt_row(db):
    from forge.durable import UsageReceipt

    async with db() as session:
        row = (
            (await session.execute(select(UsageReceipt).where(UsageReceipt.run_id == RUN_ID)))
            .scalars()
            .first()
        )
        session.expunge(row)
        return row


class TestReceiptCostColumns:
    async def test_a_legacy_raw_cost_is_classified_explicitly(self, db):
        """The legacy receipt: a raw block carrying a cost but none of the
        new columns — the figure LANDS (never silently zero) with an
        honest unknown basis."""
        usage = SimpleUsage(
            input_tokens=100,
            output_tokens=50,
            raw={"cost_usd": 0.42, "unexpected": "shape"},
        )
        async with db() as session:
            await ingest_usage_receipt(session, run_id=RUN_ID, usage=usage)
            await session.commit()
        row = await receipt_row(db)
        assert row.cost_usd == pytest.approx(0.42)  # never silently zero
        assert row.cost_basis is None  # unknown lineage stays unknown
        assert row.final is True  # the R23 front door default

    async def test_the_canonical_lineage_lands(self, db):
        usage = SimpleUsage(
            raw={
                "cost_usd": 0.5,
                "cost_basis": "provider-reported",
                "rate_card_id": "card-7",
                "route_version": "v3",
                "final": False,
            }
        )
        async with db() as session:
            await ingest_usage_receipt(session, run_id=RUN_ID, usage=usage)
            await session.commit()
        row = await receipt_row(db)
        assert row.cost_usd == pytest.approx(0.5)
        assert row.cost_basis == "provider-reported"
        assert row.rate_card_id == "card-7"
        assert row.route_version == "v3"
        assert row.final is False  # the streamed partial's own finality

    async def test_a_garbage_basis_never_lands(self, db):
        usage = SimpleUsage(raw={"cost_usd": 1.0, "cost_basis": "trust-me"})
        async with db() as session:
            await ingest_usage_receipt(session, run_id=RUN_ID, usage=usage)
            await session.commit()
        row = await receipt_row(db)
        assert row.cost_usd == pytest.approx(1.0)
        assert row.cost_basis is None  # outside the vocabulary → unknown

    async def test_the_counters_and_the_cost_describe_one_observation(self, db):
        """The same receipt feeds the guard's counters AND the USD
        projection — one durable row, both honest."""
        usage = SimpleUsage(
            input_tokens=120,
            output_tokens=30,
            raw={"cost_usd": 0.3, "cost_basis": "provider-reported"},
        )
        async with db() as session:
            await open_budget(session, run_id=RUN_ID, max_calls=10, max_tokens=10_000)
            await session.commit()
        async with db() as session:
            budget, created = await ingest_usage_receipt(session, run_id=RUN_ID, usage=usage)
            await session.commit()
        assert created is True
        assert budget is not None
        assert budget.consumed_calls == 1  # the live counter
        row = await receipt_row(db)
        assert row.cost_usd == pytest.approx(0.3)  # the projection


# ----------------------------------------------------------------------
# Recovery — restart between commit and dispatch; lost responses
# ----------------------------------------------------------------------


class TestRecovery:
    async def test_a_restart_loses_neither_the_amendment_nor_runs_the_review_twice(
        self, db, httpx_mock
    ):
        """The amendment commits in its OWN transaction; a fresh session
        (a restarted process) sees the raised limit; exactly ONE reviewer
        call runs; the redelivered command replays."""
        await openb(db, max_calls=1, max_tokens=1_000_000)
        closing_guard = await load_budget_guard(db, RUN_ID, purpose=RESERVE_CLOSING)
        assert closing_guard is not None
        # Session A (the pre-crash process) applies the amendment.
        applied = await apply(db, amendment(axis=AXIS_CALLS, amount=1))
        assert applied.applied is True

        # Session B — a fresh process after the restart: the guard reads
        # the durable limits, and the review runs ONCE.
        guard_b = await load_budget_guard(db, RUN_ID, purpose=RESERVE_CLOSING)
        assert guard_b is not None
        client = LLMClient(settings=_settings(), session_factory=db, budget=guard_b)
        try:
            httpx_mock.add_response(json=completion("{}", prompt_tokens=5))
            httpx_mock.add_response(json=completion("{}", prompt_tokens=5))
            await client.complete(**reviewer_call())  # the original capacity
            await client.complete(**reviewer_call())  # the amended capacity
            with pytest.raises(LLMError, match="budget_exhausted"):
                await client.complete(**reviewer_call())
            assert len(httpx_mock.get_requests()) == 2  # exactly two, ever

            # The redelivered command adds nothing after the restart.
            replay = await apply(db, amendment(axis=AXIS_CALLS, amount=1))
            assert replay.replayed is True
            assert len(await amendment_rows(db)) == 1
        finally:
            await client.close()

    async def test_a_lost_review_response_is_not_a_new_amendment(self, db, httpx_mock):
        """The transport loses the review's response: the call is
        consumed, the token actuals stay unknown (unresolved liability —
        never zero), and the recovery is NOT a second amendment — the
        SAME command replays; a NEW command is a new decision."""
        await openb(db, max_calls=5, max_tokens=1_000_000)
        closing_guard = await load_budget_guard(db, RUN_ID, purpose=RESERVE_CLOSING)
        assert closing_guard is not None
        client = LLMClient(settings=_settings(), session_factory=db, budget=closing_guard)
        try:
            await apply(db, amendment(axis=AXIS_CALLS, amount=4, command_id=CMD_A))
            # The response is lost (a 500): the reservation reconciles to
            # unknown actuals.
            httpx_mock.add_response(status_code=500, text="lost")
            with pytest.raises(LLMError):
                await client.complete(**reviewer_call())
        finally:
            await client.close()
        row = await budget_row(db)
        assert row.consumed_calls == 1  # the call counts
        assert row.unresolved_tokens == 4096  # unknown ≠ zero: liability parked

        # The SAME command redelivered: a replay, nothing added.
        replay = await apply(db, amendment(axis=AXIS_CALLS, amount=4, command_id=CMD_A))
        assert replay.replayed is True
        # A DIFFERENT command with the identical amount/reason: a new
        # decision (two notes are two approvals).
        second = await apply(db, amendment(axis=AXIS_CALLS, amount=4, command_id=CMD_B))
        assert second.applied is True
        assert (await budget_row(db)).max_calls == 5 + 4 + 4


# ----------------------------------------------------------------------
# The PG-gated variant — real independent sessions, real isolation
# ----------------------------------------------------------------------


class TestRealPostgresRaces:
    @pytest.mark.skipif(
        not os.environ.get("FORGE_PG_TEST_URL"),
        reason=(
            "FORGE_PG_TEST_URL not set — the two-amendments-plus-reservation"
            " race proof runs only against a disposable real Postgres"
        ),
    )
    async def test_two_amendments_and_a_reservation_raced_across_sessions(self):
        """Two amendments (two command identities) and a reservation, all
        fired concurrently from independent PostgreSQL sessions: each
        amendment applies exactly once, the limit ends at original + both
        amounts, and the reservation never overshoots the live limit."""
        engine = create_async_engine(os.environ["FORGE_PG_TEST_URL"])
        try:
            from forge.durable.models import BudgetReservation, RunBudget

            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                # the shared disposable database may hold rows for this run
                # id from sibling PG-gated proofs — clear them FK-first.
                await conn.execute(
                    BudgetAmendment.__table__.delete().where(BudgetAmendment.run_id == RUN_ID)
                )
                await conn.execute(
                    BudgetReservation.__table__.delete().where(
                        BudgetReservation.run_budget_id.in_(
                            select(RunBudget.id).where(RunBudget.run_id == RUN_ID)
                        )
                    )
                )
                await conn.execute(RunBudget.__table__.delete().where(RunBudget.run_id == RUN_ID))
                await conn.execute(FlowRun.__table__.delete().where(FlowRun.id == RUN_ID))
                await conn.execute(
                    FlowRun.__table__.insert().values(
                        [
                            {
                                "id": RUN_ID,
                                "project_id": 1,
                                "provider": "gitlab",
                                "status": "reviewing",
                            }
                        ]
                    )
                )
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                await open_budget(session, run_id=RUN_ID, max_calls=10)
                await session.commit()

            async def amend(command_id: str, amount: int) -> AppliedBudgetAmendment:
                async with factory() as session:
                    applied = await apply_budget_amendment(
                        session,
                        BudgetAmendmentCommand(
                            run_id=RUN_ID,
                            command_id=command_id,
                            axis=AXIS_CALLS,
                            amount=amount,
                            reason="race proof",
                            operator="human:race",
                        ),
                    )
                    await session.commit()
                    return applied

            async def try_reserve() -> bool:
                async with factory() as session:
                    budget = await budget_for_run(session, RUN_ID)
                    assert budget is not None
                    granted = await reserve(session, budget, calls=1, purpose=RESERVE_CLOSING)
                    await session.commit()
                    return granted is not None

            results = await asyncio.gather(
                amend("race:cmd:1", 2),
                amend("race:cmd:2", 3),
                try_reserve(),
                try_reserve(),
                return_exceptions=True,
            )
            amendments = [r for r in results[:2] if not isinstance(r, BaseException)]
            reserves = [r for r in results[2:] if not isinstance(r, BaseException)]
            assert len(amendments) == 2, results
            assert all(a.applied for a in amendments)  # both decisions landed
            assert all(a.replayed is False for a in amendments)

            async with factory() as session:
                budget = await budget_for_run(session, RUN_ID)
                assert budget is not None
                rows = await budget_amendments_for_run(session, RUN_ID)
                # the limit ends at 10 + 2 + 3, never more
                assert budget.max_calls == 15
                assert len(rows) == 2
                # the reservations never overshoot the live limit: calls
                # exposure (reserved + consumed + unresolved) <= 15.
                exposure = budget.reserved_calls + budget.consumed_calls + budget.unresolved_calls
                assert exposure <= 15
                assert len(reserves) == 2 and all(reserves)
        finally:
            await engine.dispose()

    @pytest.mark.skipif(
        not os.environ.get("FORGE_PG_TEST_URL"),
        reason="FORGE_PG_TEST_URL not set — the redelivery race runs only on real Postgres",
    )
    async def test_one_command_raced_from_two_sessions_applies_once(self):
        engine = create_async_engine(os.environ["FORGE_PG_TEST_URL"])
        try:
            from forge.durable.models import BudgetReservation, RunBudget

            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(
                    BudgetAmendment.__table__.delete().where(BudgetAmendment.run_id == RUN_ID)
                )
                await conn.execute(
                    BudgetReservation.__table__.delete().where(
                        BudgetReservation.run_budget_id.in_(
                            select(RunBudget.id).where(RunBudget.run_id == RUN_ID)
                        )
                    )
                )
                await conn.execute(RunBudget.__table__.delete().where(RunBudget.run_id == RUN_ID))
                await conn.execute(FlowRun.__table__.delete().where(FlowRun.id == RUN_ID))
                await conn.execute(
                    FlowRun.__table__.insert().values(
                        [
                            {
                                "id": RUN_ID,
                                "project_id": 1,
                                "provider": "gitlab",
                                "status": "reviewing",
                            }
                        ]
                    )
                )
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                await open_budget(session, run_id=RUN_ID, max_calls=10)
                await session.commit()

            command = BudgetAmendmentCommand(
                run_id=RUN_ID,
                command_id="race:redelivery:1",
                axis=AXIS_CALLS,
                amount=5,
                reason="one decision, many deliveries",
                operator="human:race",
            )

            async def deliver() -> AppliedBudgetAmendment:
                async with factory() as session:
                    applied = await apply_budget_amendment(session, command)
                    await session.commit()
                    return applied

            first, second = await asyncio.gather(deliver(), deliver())
            applied_count = [first.applied, second.applied]
            assert applied_count.count(True) == 1  # EXACTLY ONE applied
            assert applied_count.count(False) == 1  # the other replayed
            async with factory() as session:
                budget = await budget_for_run(session, RUN_ID)
                assert budget is not None
                assert budget.max_calls == 15  # raised once by 5
                assert len(await budget_amendments_for_run(session, RUN_ID)) == 1
        finally:
            await engine.dispose()
