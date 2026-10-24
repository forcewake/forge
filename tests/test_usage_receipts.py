"""Retained receipt lessons on immutable lane telemetry and authoritative accounting."""

from decimal import Decimal
import json

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, select

from forge.api.lane_v1_values import LaneConflict
from forge.domain.budget import BudgetExhausted, Observation, Resources
from forge.store.budget_calls import ModelCallStore
from forge.store.models_budget import EngineUsage
from forge.store.models_lane import EngineLaneSession
from forge.store.records import StoreConflict
from forge.store.runs import RunStore
from forge_protocol import LaneReport, Usage, canonical_json, digest
from tests.lane_settings_fixture import settings as settings
from tests.native_budget_lesson_fixture import (
    claude_usage,
    cold_schema,
    observation,
    resources,
    usage_rows,
    operator_commands,
)
from tests.test_budget_engine import case as case, prepare
from tests.test_engine_commands import envelope
from tests.test_engine_approval import env as env
from tests import test_engine_lane_handlers as lane_handlers
from tests.test_engine_lane_handlers import started
from tests.test_server_models import server, wire


lane_case = lane_handlers.case


def report(pending, usage=None):
    return LaneReport(
        **pending.identity.model_dump(),
        approved_digest=pending.dispatch.approved_digest,
        status="failed",
        classification="infrastructure",
        driver=pending.dispatch.driver,
        model=pending.dispatch.model,
        terminal_reason="synthetic worker stopped",
        usage=usage,
    )


def anthropic_document():
    return {
        "input_tokens": 100,
        "cache_read_input_tokens": 40,
        "cache_creation_input_tokens": 25,
        "output_tokens": 50,
    }


def inclusive_wire():
    return wire(
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
    )


class TestReceiptIdentity:
    async def test_identity_is_deterministic_across_re_reads(self, lane_case):
        _, _, pending = await started(lane_case)
        value = report(pending, Usage(input_tokens=165, output_tokens=50))
        assert await lane_case["api"].report(pending.identity, value) is True
        reloaded = (await lane_case["store"].receipt(pending.identity)).report
        assert digest(reloaded) == digest(value) and len(digest(value)) == 64
        assert canonical_json(reloaded) == canonical_json(value)

    async def test_identity_separates_run_attempt_and_content(self, lane_case):
        _, _, pending = await started(lane_case)
        value = report(pending, Usage(input_tokens=165, output_tokens=50))
        assert digest(value) != digest(value.model_copy(update={"attempt": value.attempt + 1}))
        assert digest(value) != digest(value.model_copy(update={"forge_run_id": "foreign-run"}))
        assert digest(value) != digest(
            value.model_copy(update={"usage": Usage(input_tokens=166, output_tokens=50)})
        )
        with pytest.raises(LaneConflict, match="identity"):
            await lane_case["api"].report(
                pending.identity, value.model_copy(update={"attempt": value.attempt + 1})
            )
        assert (await lane_case["store"].receipt(pending.identity)).report is None

    def test_normalized_document_excludes_the_raw_block(self):
        usage = Usage(input_tokens=165, output_tokens=50)
        assert set(usage.model_dump()) == {"protocol", "input_tokens", "output_tokens", "cost_usd"}
        with pytest.raises(ValidationError):
            Usage.model_validate({**usage.model_dump(), "raw": {"untrusted": "opaque"}})
        assert usage.cost_usd is None

    def test_lane_provided_receipt_id_is_accepted_by_from_meta(self):
        """The current typed wire refuses caller IDs; identity is the bound dispatch."""
        with pytest.raises(ValidationError):
            Usage.model_validate({"input_tokens": 10, "output_tokens": 5, "receipt_id": "a" * 64})
        value = Usage(input_tokens=10, output_tokens=5)
        assert digest(value) == digest(Usage.model_validate_json(value.model_dump_json()))


class TestNormalization:
    async def test_anthropic_disjoint_counters_get_the_total_formula(self, tmp_path):
        usage = await claude_usage(tmp_path, anthropic_document())
        assert usage.input_tokens == 100 + 40 + 25
        assert usage.input_tokens + usage.output_tokens == 215

    async def test_openai_inclusive_cache_is_never_added_on_top(self, case):
        async with server(case, lambda request: inclusive_wire()) as (_, client, args):
            assert (await client.complete(**args)).usage.tokens == 150
        assert (await usage_rows(case))[0].tokens == 150

    async def test_unknown_parts_contribute_nothing_and_are_never_zeroed(self, case):
        async with server(
            case, lambda request: wire(headers={"x-litellm-response-cost": "unknown"})
        ) as (_, client, args):
            reply = await client.complete(**args)
        assert reply.usage == Observation(usd=None, calls=1, tokens=14)
        (row,) = await usage_rows(case)
        assert row.usd is None and not row.final and row.tokens == 14

    async def test_nothing_known_is_none_not_zero(self, tmp_path):
        assert await claude_usage(tmp_path, None, cost=None) is None

    async def test_cache_write_presence_alone_signals_the_anthropic_shape(self, tmp_path):
        usage = await claude_usage(
            tmp_path, {"input_tokens": 10, "cache_creation_input_tokens": 5, "output_tokens": 0}
        )
        assert usage.input_tokens == 15 and usage.output_tokens == 0

    async def test_from_meta_degrades_a_missing_receipt_to_unknown(self, lane_case):
        _, _, pending = await started(lane_case)
        assert await lane_case["api"].report(pending.identity, report(pending))
        assert (await lane_case["store"].receipt(pending.identity)).report.usage is None
        value = await lane_case["budgets"].load(pending.identity.forge_run_id, "implementation")
        assert value.balance.held.tokens == 2000 and value.balance.settled == Resources()

    @pytest.mark.parametrize("counter", [-5, "12", True])
    def test_from_meta_keeps_negative_junk_counters_unknown(self, counter):
        with pytest.raises(ValidationError):
            Usage(input_tokens=counter, output_tokens=1)
        assert Observation(usd=None, calls=1, tokens=None).tokens is None


class TestIngestReceipt:
    async def test_first_ingest_records_ledger_and_moves_the_budget(self, lane_case):
        """Lane data is a source fact; independent stable gateway reads settle."""
        env = lane_case["env"]
        run, _, pending = await started(lane_case)
        value = report(pending, Usage(input_tokens=165, output_tokens=50, cost_usd=Decimal("0.1")))
        assert await lane_case["api"].report(pending.identity, value)
        assert (await lane_case["store"].receipt(pending.identity)).report == value
        before = await lane_case["budgets"].load(run.id, "implementation")
        assert before.balance.held.tokens == 2000 and before.balance.settled == Resources()
        reads = []

        async def native(alias):
            reads.append(alias)
            return observation(tokens=215)

        lane_case["budget"].settlement.gateway.read_spend = native
        (lease,) = await env.steps.claim_due("observer", env.clock.now(), 600)
        assert await lane_case["budget"].finish(lease, "implementation") == "settled"
        after = await lane_case["budgets"].load(run.id, "implementation")
        assert (
            after.balance.settled == resources(1, 215, "0.1") and after.balance.held == Resources()
        )
        assert len(reads) == 2 and lane_case["native"].writes[-1] == "/key/block"

    async def test_duplicate_artifact_ingestion_is_one_ledger_entry(self, lane_case):
        _, _, pending = await started(lane_case)
        value = report(pending, Usage(input_tokens=165, output_tokens=50))
        for index in range(3):
            assert await lane_case["api"].report(pending.identity, value) is (index == 0)
        async with lane_case["env"].factory() as session:
            rows = list((await session.scalars(select(EngineLaneSession))).all())
        assert (
            len(rows) == 1
            and json.loads(rows[0].api_state_json)["report"]["usage"]["input_tokens"] == 165
        )
        value = await lane_case["budgets"].load(pending.identity.forge_run_id, "implementation")
        assert len(value.reservations) == 1 and value.balance.held.tokens == 2000

    async def test_a_new_attempt_costs_again(self, lane_case):
        env = lane_case["env"]
        run, _, pending = await started(lane_case)
        await lane_case["api"].report(pending.identity, report(pending))
        assert (await env.runner.tick()).parked == 1
        first = await lane_case["budgets"].load(run.id, "implementation")
        assert first.balance.held == resources(4, 2000, "2")
        service = operator_commands(lane_case)
        assert (await service.submit(envelope(lane_case, "retry"))).reason == "retry_scheduled"
        assert (await env.runner.tick()).completed == 1
        assert (await env.runner.tick()).completed == 1
        renewed = await env.runs.load(run.id)
        assert (renewed.attempt, renewed.generation) == (2, 2)
        after = await lane_case["budgets"].load(run.id, "implementation")
        assert len(after.reservations) == 2 and after.balance.held == resources(8, 4000, "4")
        async with env.factory() as session:
            rows = list((await session.scalars(select(EngineLaneSession))).all())
        assert {(row.attempt, row.generation) for row in rows} == {(1, 1), (2, 2)}
        assert lane_case["native"].writes.count("/key/generate") == 2
        assert lane_case["native"].writes.count("/key/block") == 1

    async def test_unknown_receipt_records_the_unknown_bucket_never_zero(self, case):
        prepared = await prepare(case, "planning")
        calls = ModelCallStore(case["factory"])
        source = await calls.admit(
            case["lease"], prepared.value.alias, "test-model", 1000, now=case["clock"].now()
        )
        await calls.observe(source, None)
        (row,) = await usage_rows(case)
        assert not row.final and row.completeness == "unknown"
        assert row.calls is row.tokens is row.usd is None
        with pytest.raises(BudgetExhausted, match="cumulative allowance"):
            await calls.admit(
                case["lease"], prepared.value.alias, "test-model", 1500, now=case["clock"].now()
            )

    async def test_ingest_without_a_budget_still_records_the_receipt(self, lane_case):
        """Telemetry has its own immutable native receipt; it creates no model-call row."""
        _, _, pending = await started(lane_case)
        value = report(pending, Usage(input_tokens=10, output_tokens=5))
        assert await lane_case["api"].report(pending.identity, value)
        assert (await lane_case["store"].receipt(pending.identity)).report.usage == value.usage
        async with lane_case["env"].factory() as session:
            assert list((await session.scalars(select(EngineUsage))).all()) == []

    async def test_ingest_on_a_closed_budget_keeps_the_ledger_honest(self, case):
        prepared = await prepare(case, "planning")
        calls = ModelCallStore(case["factory"])
        source = await calls.admit(
            case["lease"], prepared.value.alias, "test-model", 100, now=case["clock"].now()
        )
        await RunStore(case["factory"]).request_cancel(case["run"].id, expected_version=1)
        await calls.observe(source, observation())
        (row,) = await usage_rows(case)
        assert row.final and row.tokens == 100
        with pytest.raises(StoreConflict):
            await calls.admit(
                case["lease"], prepared.value.alias, "test-model", 100, now=case["clock"].now()
            )
        assert (await RunStore(case["factory"]).load(case["run"].id)).cancel_requested

    async def test_openai_inclusive_receipt_never_double_counts_cache(self, case):
        async with server(case, lambda request: inclusive_wire()) as (_, client, args):
            assert (await client.complete(**args)).usage.tokens == 150
        (row,) = await usage_rows(case)
        calls = ModelCallStore(case["factory"])
        await calls.observe(row.source_key, observation(tokens=150, usd="0.00003"))
        assert (await usage_rows(case))[0].tokens == 150

    async def test_caller_supplied_attempt_id_fills_the_identity(self, lane_case):
        _, _, pending = await started(lane_case)
        value = report(pending, Usage(input_tokens=10, output_tokens=5))
        assert await lane_case["api"].report(pending.identity, value)
        with pytest.raises(LaneConflict):
            await lane_case["api"].report(
                pending.identity, value.model_copy(update={"generation": value.generation + 1})
            )
        assert (await lane_case["store"].receipt(pending.identity)).report == value


class TestMigration015:
    def test_upgrade_creates_the_receipt_table_and_downgrade_drops_it(self):
        with cold_schema() as installed:
            inspector = inspect(installed.connection)
            assert inspector.has_table("usage") and inspector.has_table("lane_sessions")
            unique = inspector.get_unique_constraints("usage")
            assert any(item["column_names"] == ["source_key"] for item in unique)
            unique = inspector.get_unique_constraints("lane_sessions")
            assert any(
                set(item["column_names"]) == {"run_id", "attempt", "generation"} for item in unique
            )
            with pytest.raises(RuntimeError, match="destructive downgrade"):
                installed.module.downgrade()
            assert inspect(installed.connection).has_table("usage")

    def test_models_match_the_migrated_columns(self):
        with cold_schema() as installed:
            inspector = inspect(installed.connection)
            for model in (EngineUsage, EngineLaneSession):
                assert {item["name"] for item in inspector.get_columns(model.__tablename__)} == {
                    item.name for item in model.__table__.columns
                }
            columns = {item["name"]: item for item in inspector.get_columns("usage")}
            assert all(columns[name]["nullable"] for name in ("usd", "calls", "tokens"))


async def test_persistence_uses_the_frozen_raw_usage_snapshot(lane_case):
    _, _, pending = await started(lane_case)
    raw = {"input_tokens": 10, "output_tokens": 5, "cost_usd": "0.5"}
    usage = Usage.model_validate_json(json.dumps(raw))
    value = report(pending, usage)
    raw["input_tokens"], raw["cost_usd"] = 999, "999"
    decoded = usage.model_dump()
    decoded["input_tokens"] = 999
    assert await lane_case["api"].report(pending.identity, value)
    saved = (await lane_case["store"].receipt(pending.identity)).report
    assert saved.usage == Usage(input_tokens=10, output_tokens=5, cost_usd=Decimal("0.5"))
    with pytest.raises(LaneConflict):
        await lane_case["api"].report(
            pending.identity, report(pending, Usage(input_tokens=999, output_tokens=5))
        )
    assert (await lane_case["store"].receipt(pending.identity)).report == saved
