"""Usage receipt identity, normalization honesty and idempotent ingest (R23).

Covers the R23 contract end to end:

- identity: ``receipt_id`` is sha256 over (run, attempt, normalized usage
  JSON) — deterministic across re-reads of the same artifact, distinct per
  attempt, and recomputed identically by the control plane when the meta
  carries no lane-computed id;
- normalization honesty (docs/research/2026-09-17-actions-artifacts-usage.md § Usage
  normalization table): Anthropic-shaped counters are DISJOINT and get the
  total formula ``input + cache_read + cache_write + output``;
  OpenAI-shaped counters are INCLUSIVE and the cache is never added on
  top; unknown stays unknown — never zero;
- ingestion: ``ingest_usage_receipt`` inserts the identity with
  ``ON CONFLICT DO NOTHING`` against UNIQUE (run_id, attempt_id,
  receipt_id), so a duplicated artifact ingestion produces ONE receipt
  ledger entry, ONE ``llm_calls`` row and ONE budget move — while a new
  attempt (repair re-dispatch) legitimately costs again; a missing/invalid
  receipt records the unknown bucket, never a fabricated zero;
- migration 015 creates/drops the ``usage_receipts`` table exactly as the
  ORM declares it.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.durable import FlowRun, LLMCall, RunBudget, UsageReceipt
from forge.durable.budgets import budget_for_run, close_budget, ingest_usage_receipt, open_budget
from forge.models.base import Base
from forge.runs.candidate import (
    HarnessUsage,
    usage_document,
    usage_receipt_id,
)

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


def anthropic_usage(**overrides) -> HarnessUsage:
    """A claude-code receipt: DISJOINT counters (input excludes the cache)."""
    values = dict(
        driver="claude-code",
        model="glm-5.3-flash[1m]",
        input_tokens=100,
        cached_input_tokens=40,
        cache_write_tokens=25,
        output_tokens=50,
        reasoning_tokens=10,
        completeness="aggregate",
        source="stream-json",
        attempt_id="501:1",
    )
    values.update(overrides)
    return HarnessUsage(**values)


def openai_usage(**overrides) -> HarnessUsage:
    """A grok-build receipt: INCLUSIVE counters (cache inside the input)."""
    values = dict(
        driver="grok-build",
        input_tokens=100,
        cached_input_tokens=40,
        output_tokens=50,
        completeness="aggregate",
        source="stream-json",
        attempt_id="501:1",
    )
    values.update(overrides)
    return HarnessUsage(**values)


async def receipt_rows(db) -> list[UsageReceipt]:
    async with db() as session:
        rows = (
            (await session.execute(select(UsageReceipt).order_by(UsageReceipt.id))).scalars().all()
        )
        for row in rows:
            session.expunge(row)
        return rows


async def ledger_rows(db) -> list[LLMCall]:
    async with db() as session:
        rows = (await session.execute(select(LLMCall).order_by(LLMCall.id))).scalars().all()
        for row in rows:
            session.expunge(row)
        return rows


# ----------------------------------------------------------------------
# Identity
# ----------------------------------------------------------------------


class TestReceiptIdentity:
    def test_identity_is_deterministic_across_re_reads(self):
        """The exact double-count scenario: re-downloading the same
        artifact replays byte-identical identity."""
        first = usage_receipt_id(RUN_ID, "501:1", anthropic_usage())
        again = usage_receipt_id(RUN_ID, "501:1", anthropic_usage())
        assert first == again
        assert len(first) == 64  # sha256 hex

    def test_identity_separates_run_attempt_and_content(self):
        base = usage_receipt_id(RUN_ID, "501:1", anthropic_usage())
        assert base != usage_receipt_id("f" * 32, "501:1", anthropic_usage())
        assert base != usage_receipt_id(RUN_ID, "501:2", anthropic_usage())  # repair attempt
        assert base != usage_receipt_id(RUN_ID, "501:1", anthropic_usage(input_tokens=101))

    def test_normalized_document_excludes_the_raw_block(self):
        usage = anthropic_usage(raw={"input_tokens": 100, "extra": "verbatim"})
        document = usage_document(usage)
        assert "raw" not in document
        assert document["input_tokens"] == 100
        assert document["cache_write_tokens"] == 25

    def test_lane_provided_receipt_id_is_accepted_by_from_meta(self):
        """The lane may embed its emit-time id as usage.receipt_id — the
        parser carries it so ingest never needs to recompute."""
        usage = HarnessUsage.from_meta(
            {
                "input_tokens": 10,
                "receipt_id": "a" * 64,
                "attempt_id": "501:2",
                "cache_write_tokens": 5,
                "reasoning_tokens": 3,
            },
            driver="claude-code",
            attempt_id="501:1",
        )
        assert usage.receipt_id == "a" * 64
        assert usage.attempt_id == "501:2"  # in-block identity wins
        assert usage.cache_write_tokens == 5
        assert usage.reasoning_tokens == 3
        assert usage.raw == {
            "input_tokens": 10,
            "receipt_id": "a" * 64,
            "attempt_id": "501:2",
            "cache_write_tokens": 5,
            "reasoning_tokens": 3,
        }


# ----------------------------------------------------------------------
# Normalization honesty (the research table)
# ----------------------------------------------------------------------


class TestNormalization:
    def test_anthropic_disjoint_counters_get_the_total_formula(self):
        """input_tokens EXCLUDES the cache on Anthropic-compatible shapes:
        the spend total is input + cache_read + cache_write + output."""
        usage = anthropic_usage()
        assert usage.anthropic_shaped is True
        assert usage.total_known_tokens == 100 + 40 + 25 + 50

    def test_openai_inclusive_cache_is_never_added_on_top(self):
        """cached ⊂ prompt on OpenAI-compatible shapes: input + output,
        the cache breakdown stays informational."""
        usage = openai_usage()
        assert usage.anthropic_shaped is False
        assert usage.total_known_tokens == 150

    def test_unknown_parts_contribute_nothing_and_are_never_zeroed(self):
        usage = anthropic_usage(cache_write_tokens=None, reasoning_tokens=None)
        assert usage.total_known_tokens == 100 + 40 + 50

    def test_nothing_known_is_none_not_zero(self):
        assert HarnessUsage(completeness="unknown").total_known_tokens is None

    def test_cache_write_presence_alone_signals_the_anthropic_shape(self):
        """Field presence decides the shape — an unknown driver carrying the
        Anthropic-only write counter is still disjoint."""
        usage = HarnessUsage(input_tokens=10, cache_write_tokens=5)
        assert usage.anthropic_shaped is True
        assert usage.total_known_tokens == 15

    def test_from_meta_degrades_a_missing_receipt_to_unknown(self):
        usage = HarnessUsage.from_meta(None, driver="claude-code", attempt_id="501:1")
        assert usage.completeness == "unknown"
        assert usage.total_known_tokens is None
        assert usage.attempt_id == "501:1"
        assert usage.raw is None

    def test_from_meta_keeps_negative_junk_counters_unknown(self):
        usage = HarnessUsage.from_meta(
            {"input_tokens": -5, "output_tokens": "12", "cached_input_tokens": True},
            driver="grok-build",
        )
        assert usage.completeness == "unknown"
        assert usage.input_tokens is None
        assert usage.output_tokens is None
        assert usage.cached_input_tokens is None


# ----------------------------------------------------------------------
# Idempotent ingestion
# ----------------------------------------------------------------------


class TestIngestReceipt:
    async def test_first_ingest_records_ledger_and_moves_the_budget(self, db):
        await openb(db, max_calls=5, max_tokens=10000)
        async with db() as session:
            budget, created = await ingest_usage_receipt(
                session, run_id=RUN_ID, usage=anthropic_usage()
            )
            await session.commit()
        assert created is True
        assert budget is not None
        # Disjoint Anthropic counters: the FULL formula reaches the budget.
        assert budget.consumed_calls == 1
        assert budget.consumed_tokens == 215

        receipts = await receipt_rows(db)
        assert len(receipts) == 1
        assert receipts[0].run_id == RUN_ID
        assert receipts[0].attempt_id == "501:1"
        assert receipts[0].receipt_id == usage_receipt_id(RUN_ID, "501:1", anthropic_usage())
        assert receipts[0].input_tokens == 100
        assert receipts[0].cached_input_tokens == 40
        assert receipts[0].cache_write_tokens == 25
        assert receipts[0].output_tokens == 50
        assert receipts[0].completeness == "aggregate"

        (call,) = await ledger_rows(db)
        assert call.flow_run_id == RUN_ID
        assert call.provider == "ci_harness"
        assert call.input_tokens == 100
        assert call.cached_tokens == 40
        assert call.output_tokens == 50
        assert call.completeness == "aggregate"

    async def test_duplicate_artifact_ingestion_is_one_ledger_entry(self, db):
        """The R23 double-count scenario: the same artifact re-read by a
        repeated poll / crash-retry moves NOTHING the second time."""
        await openb(db, max_calls=5)
        usage = anthropic_usage()
        for round_no in range(3):
            async with db() as session:
                _, created = await ingest_usage_receipt(session, run_id=RUN_ID, usage=usage)
                await session.commit()
            assert created is (round_no == 0)

        budget = await get_budget(db)
        assert budget.consumed_calls == 1  # the attempt, once
        assert budget.consumed_tokens == 215
        assert len(await receipt_rows(db)) == 1  # ONE receipt ledger entry
        assert len(await ledger_rows(db)) == 1  # ONE llm_calls row

    async def test_a_new_attempt_costs_again(self, db):
        """A repair re-dispatch is a new attempt_id — a distinct receipt."""
        await openb(db, max_calls=5)
        async with db() as session:
            await ingest_usage_receipt(session, run_id=RUN_ID, usage=anthropic_usage())
            await session.commit()
        repair = anthropic_usage(
            input_tokens=10,
            cached_input_tokens=None,
            cache_write_tokens=None,
            output_tokens=5,
            attempt_id="501:2",
        )
        async with db() as session:
            budget, created = await ingest_usage_receipt(session, run_id=RUN_ID, usage=repair)
            await session.commit()
        assert created is True
        assert budget is not None
        assert budget.consumed_calls == 2
        assert budget.consumed_tokens == 215 + 15
        assert len(await receipt_rows(db)) == 2

    async def test_unknown_receipt_records_the_unknown_bucket_never_zero(self, db):
        """A missing/invalid receipt is recorded with completeness unknown
        and NULL counters — visibly unknown, never fabricated as 0 — and
        stops a hard token-limited budget instead of reading as headroom."""
        await openb(db, max_calls=5, max_tokens=10000)
        unknown = HarnessUsage(completeness="unknown", attempt_id="501:3")
        async with db() as session:
            budget, created = await ingest_usage_receipt(session, run_id=RUN_ID, usage=unknown)
            await session.commit()
        assert created is True
        assert budget is not None
        assert budget.status == "exhausted"  # unknown ≠ spendable headroom
        assert budget.consumed_tokens == 0  # nothing KNOWN was counted

        (receipt,) = await receipt_rows(db)
        assert receipt.completeness == "unknown"
        assert receipt.input_tokens is None
        assert receipt.output_tokens is None
        assert receipt.cached_input_tokens is None
        assert receipt.cache_write_tokens is None

        (call,) = await ledger_rows(db)
        assert call.completeness == "unknown"
        assert call.input_tokens is None

    async def test_ingest_without_a_budget_still_records_the_receipt(self, db):
        async with db() as session:
            budget, created = await ingest_usage_receipt(
                session, run_id=RUN_ID, usage=anthropic_usage()
            )
            await session.commit()
        assert budget is None
        assert created is True
        assert len(await receipt_rows(db)) == 1
        assert len(await ledger_rows(db)) == 1

    async def test_ingest_on_a_closed_budget_keeps_the_ledger_honest(self, db):
        budget = await openb(db, max_calls=5)
        async with db() as session:
            await session.get(RunBudget, budget.id)
            await close_budget(session, await budget_for_run(session, RUN_ID))
            _, created = await ingest_usage_receipt(session, run_id=RUN_ID, usage=anthropic_usage())
            await session.commit()
        assert created is True  # the receipt itself is recorded
        after = await get_budget(db)
        assert after.consumed_calls == 0  # …but a closed budget moves nothing

    async def test_openai_inclusive_receipt_never_double_counts_cache(self, db):
        await openb(db, max_calls=5, max_tokens=10000)
        async with db() as session:
            budget, _ = await ingest_usage_receipt(session, run_id=RUN_ID, usage=openai_usage())
            await session.commit()
        assert budget is not None
        assert budget.consumed_tokens == 150  # the cache rides inside the input

    async def test_caller_supplied_attempt_id_fills_the_identity(self, db):
        """A v1 meta without attempt identity: the caller's episode key
        becomes the attempt component of the identity."""
        await openb(db, max_calls=5)
        usage = openai_usage(attempt_id="")
        async with db() as session:
            await ingest_usage_receipt(
                session, run_id=RUN_ID, usage=usage, attempt_id="pipeline:77"
            )
            await session.commit()
        async with db() as session:
            _, created = await ingest_usage_receipt(
                session, run_id=RUN_ID, usage=usage, attempt_id="pipeline:77"
            )
            await session.commit()
        assert created is False  # same episode → no-op
        (receipt,) = await receipt_rows(db)
        assert receipt.attempt_id == "pipeline:77"


async def openb(db, **limits) -> RunBudget:
    async with db() as session:
        budget = await open_budget(session, run_id=RUN_ID, **limits)
        await session.commit()
        session.expunge(budget)
        return budget


async def get_budget(db) -> RunBudget:
    async with db() as session:
        budget = await budget_for_run(session, RUN_ID)
        assert budget is not None
        session.expunge(budget)
        return budget


# ----------------------------------------------------------------------
# Migration 015
# ----------------------------------------------------------------------


class TestMigration015:
    def _load_migration(self, module_name: str, filename: str) -> ModuleType:
        path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / filename
        spec = importlib.util.spec_from_file_location(module_name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_upgrade_creates_the_receipt_table_and_downgrade_drops_it(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import create_engine

        base = self._load_migration("migration_014", "014_publication_intents.py")
        head = self._load_migration("migration_015", "015_usage_receipts.py")
        engine = create_engine("sqlite:///:memory:")
        try:
            with engine.connect() as conn:
                ctx = MigrationContext.configure(conn)
                with Operations.context(ctx):
                    base.upgrade()
                    head.upgrade()

                inspector = inspect(conn)
                assert "usage_receipts" in inspector.get_table_names()
                columns = {c["name"] for c in inspector.get_columns("usage_receipts")}
                assert {
                    "id",
                    "run_id",
                    "attempt_id",
                    "receipt_id",
                    "driver",
                    "model",
                    "input_tokens",
                    "cached_input_tokens",
                    "cache_write_tokens",
                    "output_tokens",
                    "completeness",
                    "source",
                    "raw",
                    "created_at",
                } <= columns
                indexes = {ix["name"]: ix for ix in inspector.get_indexes("usage_receipts")}
                identity = indexes["uq_usage_receipt_identity"]
                assert identity["unique"]  # SQLite's inspector reports 1
                assert set(identity["column_names"]) == {
                    "run_id",
                    "attempt_id",
                    "receipt_id",
                }

                with Operations.context(ctx):
                    head.downgrade()
                # A fresh inspector: table names are cached per instance.
                after = inspect(conn)
                assert "usage_receipts" not in after.get_table_names()
        finally:
            engine.dispose()

    def test_models_match_the_migrated_columns(self):
        assert {c.name for c in UsageReceipt.__table__.columns} == {
            "id",
            "run_id",
            "attempt_id",
            "receipt_id",
            "driver",
            "model",
            "input_tokens",
            "cached_input_tokens",
            "cache_write_tokens",
            "output_tokens",
            "completeness",
            "source",
            "raw",
            "created_at",
        }
