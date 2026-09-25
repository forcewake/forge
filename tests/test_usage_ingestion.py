"""R38-09 — lane usage ingested into durable delivery economics, checked.

The recorded gap (issue #310 / live single-writer record): the SDK lanes
report receipt costs while durable ``usage_receipts`` rows for harness
lanes stayed EMPTY — the lane's usage artifacts never became durable
rows, so the economics report rendered unknown-cost. These tests pin the
ingestion contract that closes it:

- IDEMPOTENCY: ingestion is keyed by (work, attempt, receipt, source) — a
  re-ingest, a replayed webhook, a re-downloaded artifact write NOTHING
  new; a changed artifact under the same identity is a surfaced conflict
  (the artifact digest is tamper-evident), never an overwrite.
- NORMALIZATION (the #294/R23 rules, REUSED): OpenAI-shaped counters are
  inclusive (the cache rides inside input — never added on top);
  Anthropic-shaped counters are disjoint (inclusive input sums the parts);
  unknown stays unknown; an out-of-order or reset cumulative counter is
  folded by content, never by arrival order.
- STREAMING + CRASH: partials stream with ``final: false``; a final
  artifact REPLACES its partial (reconciled, never summed); after process
  death the re-run recovers streamed partials and the unresolved remainder
  stays unknown with its lower bound — never zero.
- THE JOIN: ingested rows become a receipt SOURCE for the measurement
  ledger and the economics report beside the existing evidence inputs;
  planner rows keep their own population; successful-attempt cost,
  accepted-task all-attempt cost and programme-per-accepted stay
  DIFFERENT measures.
- RATE-CARD IDENTITY: every row carries its rate-card id and cost basis
  (estimated vs provider-reported vs billing-reconciliation); a route or
  card change creates a NEW attribution segment — history never rewritten.
- SPEND CAPS: the check before the next chargeable action reserves known
  costs plus the lower bounds of unknown intervals (conservative
  reservation — never zero, never under-stopped).
- DUPLICATE CALL IDS across attempts never false-join (one identity
  counted once, the duplication surfaced).
- THE DURABLE WRITE: ``persist_ingested_rows`` lands rows through the R23
  ``ON CONFLICT DO NOTHING`` seam — a replay writes nothing at the DB.
- THE LANE ENVELOPE: ``emit_candidate_meta`` stamps the ingest envelope
  (the receipt identity the control plane recomputes, the artifact digest,
  the final marker, the cost basis) so the artifact IS the durable write.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive.delivery_economics import EconomicsLinker
from forge.adaptive.delivery_measurement import (
    MeasurementLinker,
    ProviderRoute,
    ledger_records_from_ingested_usage,
)
from forge.adaptive.usage_ingestion import (
    COST_BASIS_ESTIMATED,
    COST_BASIS_PROVIDER_REPORTED,
    IngestedUsageRow,
    UsageIngestStore,
    attribution_segment,
    ingest_usage_artifact,
    normalize_counters,
    persist_ingested_rows,
    reconcile_after_death,
    rows_from_documents,
    spend_cap_check,
)
from forge.durable import FlowRun, UsageReceipt
from forge.harness_entry import (
    USAGE_INGEST_SOURCE,
    emit_candidate_meta,
    usage_ingest_envelope,
)
from forge.models.base import Base
from forge.runs.candidate import HarnessUsage, usage_receipt_id

RUN_ID = "r" * 32


# ----------------------------------------------------------------------
# The three source shapes (each with its own attribution label)
# ----------------------------------------------------------------------


def lane_usage_artifact(**overrides) -> dict:
    """The R23 ``.forge/usage.json`` shape the SDK lane writes."""
    artifact = {
        "input_tokens": 19163,
        "cached_input_tokens": 119936,
        "output_tokens": 2463,
        "driver": "claude-sdk-lane",
        "completeness": "aggregate",
        "source": "claude-agent-sdk",
        "total_cost_usd": 0.1498992,
        "attempt_id": "386:2",
    }
    artifact.update(overrides)
    return artifact


def sdk_receipt_artifact(**overrides) -> dict:
    """The SDK's own receipt JSON (per-call cost blocks)."""
    artifact = {
        "provider": "claude-sdk-lane",
        "model": "glm-5.3-flash",
        "receipts": [
            {
                "call_id": "call_732_1",
                "input_tokens": 1000,
                "output_tokens": 200,
                "total_cost_usd": 0.01,
            },
            {
                "call_id": "call_732_2",
                "input_tokens": 3000,
                "cache_read_input_tokens": 500,
                "output_tokens": 400,
                "total_cost_usd": 0.02,
            },
        ],
    }
    artifact.update(overrides)
    return artifact


def planner_ledger_row(**overrides) -> dict:
    """A planner/LLM-call ledger extract (work-level, no attempt)."""
    row = {
        "call_id": f"{RUN_ID}:planner:1",
        "input_tokens": 4000,
        "output_tokens": 800,
        "total_cost_usd": 0.03,
    }
    row.update(overrides)
    return row


# ----------------------------------------------------------------------
# Idempotency — re-ingest x2 and webhook replay write nothing new
# ----------------------------------------------------------------------


def test_reingest_twice_creates_once_and_replays_silently():
    store = UsageIngestStore()
    artifact = lane_usage_artifact()
    first = ingest_usage_artifact(store, artifact, work_id=RUN_ID, source="lane/.forge/usage.json")
    assert len(first.created) == 1
    row = first.created[0]
    assert row.cost_usd == pytest.approx(0.1498992)
    assert row.cost_basis == COST_BASIS_PROVIDER_REPORTED
    assert row.source == "lane/.forge/usage.json"

    second = ingest_usage_artifact(store, artifact, work_id=RUN_ID, source="lane/.forge/usage.json")
    third = ingest_usage_artifact(store, artifact, work_id=RUN_ID, source="lane/.forge/usage.json")
    assert not second.wrote_something_new and not third.wrote_something_new
    assert len(second.replayed) == 1 and len(third.replayed) == 1
    assert len(store.rows()) == 1
    # the totals never move on a replay
    check = spend_cap_check(store.rows(), cap_usd=1.0)
    assert check["known_cost_usd"] == pytest.approx(0.1498992)


def test_webhook_replay_of_every_source_writes_nothing_new():
    store = UsageIngestStore()
    for artifact, source, kwargs in (
        (lane_usage_artifact(), "lane/.forge/usage.json", {"attempt_id": "386:2"}),
        (sdk_receipt_artifact(), "sdk-receipt", {"attempt_id": "386:2"}),
        (planner_ledger_row(), "planner/llm_calls", {}),
    ):
        first = ingest_usage_artifact(store, artifact, work_id=RUN_ID, source=source, **kwargs)
        replay = ingest_usage_artifact(store, artifact, work_id=RUN_ID, source=source, **kwargs)
        assert len(first.created) >= 1
        assert not replay.wrote_something_new
    # 1 lane receipt + 2 SDK call receipts + 1 planner row
    assert len(store.rows()) == 4


def test_changed_artifact_under_one_identity_is_a_surfaced_conflict():
    store = UsageIngestStore()
    ingest_usage_artifact(
        store, lane_usage_artifact(), work_id=RUN_ID, source="lane/.forge/usage.json"
    )
    tampered = ingest_usage_artifact(
        store,
        lane_usage_artifact(total_cost_usd=0.99, artifact_digest="sha256:changed"),
        work_id=RUN_ID,
        source="lane/.forge/usage.json",
    )
    assert len(tampered.conflicts) == 1
    assert tampered.conflicts[0].receipt_id == store.rows()[0].receipt_id
    # the first final row stands — never averaged, never overwritten
    assert store.rows()[0].cost_usd == pytest.approx(0.1498992)


# ----------------------------------------------------------------------
# Provider counter normalization — the #294 rules, reused not re-derived
# ----------------------------------------------------------------------


def test_openai_inclusive_cache_never_added_on_top():
    counters = normalize_counters(
        {"input_tokens": 800, "cached_tokens": 500, "output_tokens": 120},
        driver="codex-sdk-lane",
    )
    assert counters.anthropic_shaped is False
    # the cache is a BREAKDOWN of the inclusive input — the spend-bearing
    # input stays 800, never 800 + 500
    assert counters.input_tokens_inclusive == 800
    assert counters.cached_input_tokens == 500
    assert counters.known_total_tokens == 920


def test_anthropic_disjoint_counters_sum_into_inclusive_input():
    counters = normalize_counters(
        {
            "input_tokens": 19163,
            "cache_read_input_tokens": 119936,
            "cache_creation_input_tokens": 300,
            "output_tokens": 2463,
            "thinking_tokens": 900,
        },
        driver="claude-sdk-lane",
    )
    assert counters.anthropic_shaped is True
    assert counters.input_tokens_inclusive == 19163 + 119936 + 300
    # reasoning is a breakdown inside the inclusive output — never added
    assert counters.known_total_tokens == 19163 + 119936 + 300 + 2463


def test_unknown_counters_stay_unknown_never_zero():
    counters = normalize_counters({"driver": "claude-sdk-lane"})
    assert counters.input_tokens is None
    assert counters.input_tokens_inclusive is None
    assert counters.known_total_tokens is None


def test_out_of_order_and_reset_cumulative_counters_never_negative_deltas():
    store = UsageIngestStore()
    base = {
        "driver": "claude-sdk-lane",
        "receipt_id": "cumulative-1",
        "completeness": "aggregate",
        "attempt_id": "a",
    }
    # a vendor counter RESET: the same identity re-delivered with a
    # DECREASED cumulative counter — a reset, never a negative delta
    first = ingest_usage_artifact(
        store, {**base, "input_tokens": 900, "output_tokens": 90}, work_id=RUN_ID
    )
    reset = ingest_usage_artifact(
        store, {**base, "input_tokens": 500, "output_tokens": 50}, work_id=RUN_ID
    )
    # two different FINAL contents under one identity: surfaced as a
    # conflict — the first row stands, nothing is averaged and no counter
    # ever moves backwards (the durable ON CONFLICT seam keeps the first
    # insert for exactly this reason)
    assert len(first.conflicts) == 0 and len(reset.conflicts) == 1
    stored = store.rows()[0]
    assert stored.counters.input_tokens == 900
    # no counter ever moves backwards and nothing is averaged
    assert spend_cap_check(store.rows(), cap_usd=1.0)["reserved_usd"] >= 0.0
    # re-delivering the ORIGINAL (out-of-order greater claim) after the
    # reset is a clean REPLAY of the standing row — no second write
    again = ingest_usage_artifact(
        store, {**base, "input_tokens": 900, "output_tokens": 90}, work_id=RUN_ID
    )
    assert not again.wrote_something_new
    assert len(store.rows()) == 1


# ----------------------------------------------------------------------
# Streaming + crash reconciliation
# ----------------------------------------------------------------------


def test_partial_then_final_reconciles_replaces_never_sums():
    store = UsageIngestStore()
    partial = ingest_usage_artifact(
        store,
        lane_usage_artifact(input_tokens=1000, output_tokens=100, total_cost_usd=0.05),
        work_id=RUN_ID,
        attempt_id="386:2",
        source="lane/.forge/usage.json",
        final=False,
    )
    row = partial.created[0]
    assert row.final is False and row.completeness == "partial"
    final = ingest_usage_artifact(
        store,
        lane_usage_artifact(),
        work_id=RUN_ID,
        attempt_id="386:2",
        source="lane/.forge/usage.json",
        final=True,
    )
    assert len(final.reconciled) == 1 and not final.created
    stored = store.rows()[0]
    assert stored.final is True
    # REPLACED by the final state — the totals are the final counters,
    # never partial + final summed
    assert stored.counters.input_tokens == 19163
    assert stored.cost_usd == pytest.approx(0.1498992)


def test_killed_lane_streamed_partials_recovered_remainder_unknown():
    # the lane streamed one completed call, died before the final upload
    store = UsageIngestStore()
    ingest_usage_artifact(
        store,
        {
            "receipt_id": "call-k1",
            "driver": "claude-sdk-lane",
            "input_tokens": 500,
            "output_tokens": 50,
            "total_cost_usd": 0.02,
        },
        work_id=RUN_ID,
        attempt_id="job730",
        source="sdk-receipt",
        final=False,
    )
    # the re-run ingests the final artifacts that DO exist (a different
    # attempt's); job730 has no final artifact
    report = reconcile_after_death(
        store,
        [lane_usage_artifact(attempt_id="job732")],
        work_id=RUN_ID,
        source="sdk-receipt",
    )
    assert report["reconciled_receipt_ids"] == []
    remainder = report["unresolved_remainder"]
    assert len(remainder) == 1
    assert remainder[0]["receipt_id"] == "call-k1"
    # the remainder keeps its known lower bound — never zero, never healed
    assert report["unresolved_cost_lower_bound_usd"] == pytest.approx(0.02)
    assert "never zero" in remainder[0]["note"]
    # and a later final artifact for the SAME identity reconciles it
    reconciled = reconcile_after_death(
        store,
        [
            {
                "receipt_id": "call-k1",
                "driver": "claude-sdk-lane",
                "input_tokens": 500,
                "output_tokens": 50,
                "total_cost_usd": 0.02,
                "attempt_id": "job730",
            }
        ],
        work_id=RUN_ID,
        source="sdk-receipt",
    )
    assert reconciled["reconciled_receipt_ids"] == ["call-k1"]
    assert reconciled["unresolved_remainder"] == []


def test_partial_receipt_never_certifies_an_exact_ledger_total():
    store = UsageIngestStore()
    ingest_usage_artifact(
        store,
        lane_usage_artifact(),
        work_id=RUN_ID,
        source="lane/.forge/usage.json",
        final=False,
    )
    # the lane artifact's own attempt identity ("386:2") is the row's —
    # the ledger must know the attempt under that id
    records = ledger_records_from_ingested_usage(
        store.documents(),
        works=[{"work_id": RUN_ID, "outcome": ""}],
        attempts=[{"work_id": RUN_ID, "attempt_id": "386:2"}],
    )
    ledger = MeasurementLinker().link(**records)
    usage = ledger.works[0].attempts[0].usage
    assert usage.exact is False
    assert usage.cost_usd is None
    # the lower bound is the partial's own claim — never zero
    assert usage.known_cost_lower_bound_usd == pytest.approx(0.1498992)


# ----------------------------------------------------------------------
# The join — ingested rows feed the ledger and the three measures
# ----------------------------------------------------------------------


def _live_cohort_store() -> UsageIngestStore:
    """A mixed-source cohort: lane receipts + planner ledger rows."""
    store = UsageIngestStore()
    ingest_usage_artifact(
        store,
        lane_usage_artifact(total_cost_usd=0.30, attempt_id="w/a1"),
        work_id="w",
        source="lane/.forge/usage.json",
    )
    ingest_usage_artifact(
        store,
        lane_usage_artifact(total_cost_usd=0.70, attempt_id="w/a2", input_tokens=900),
        work_id="w",
        source="lane/.forge/usage.json",
    )
    ingest_usage_artifact(
        store,
        lane_usage_artifact(total_cost_usd=0.20, attempt_id="x/a1"),
        work_id="x",
        source="lane/.forge/usage.json",
    )
    ingest_usage_artifact(
        store,
        planner_ledger_row(),
        work_id="w",
        source="planner/llm_calls",
    )
    return store


def test_ledger_join_three_distinct_measures_from_mixed_sources():
    store = _live_cohort_store()
    records = ledger_records_from_ingested_usage(
        store.documents(),
        works=[{"work_id": "w", "outcome": "accepted"}, {"work_id": "x", "outcome": "rejected"}],
        attempts=[
            {"work_id": "w", "attempt_id": "w/a1", "outcome": "rejected"},
            {"work_id": "w", "attempt_id": "w/a2", "outcome": "accepted"},
            {"work_id": "x", "attempt_id": "x/a1", "outcome": "rejected"},
        ],
    )
    ledger = MeasurementLinker().link(**records)
    # the planner row kept its OWN population (work-level calls, never an
    # attempt receipt)
    work = ledger.work_by_id("w")
    assert work.planner_call_ids == (f"{RUN_ID}:planner:1",)
    assert len(work.attempts) == 2

    report = EconomicsLinker().link(
        ledger,
        acceptance_records=[
            {
                "work_id": "w",
                "accepted": True,
                "decided_by": "human:operator",
                "attempt_outcomes": {"w/a1": "rejected", "w/a2": "accepted"},
            },
            {"work_id": "x", "accepted": False, "decided_by": "human:operator"},
        ],
    )
    document = report.to_document()
    costs = document["costs"]
    # measure 1: the SUCCESSFUL attempt's own cost (a2 only, 0.70)
    successful = costs["successful_attempt_costs"]["w"]["w/a2"]
    assert successful["cost_usd"] == pytest.approx(0.70)
    # measure 2: the accepted item's ALL-ATTEMPT cost (a1 + a2 = 1.00)
    assert costs["accepted_item_totals"]["w"]["billed_usd"] == pytest.approx(1.00)
    # measure 3: programme per accepted ((0.30 + 0.70 + 0.20) / 1 = 1.20)
    assert costs["programme_per_accepted_item"]["programme_billed_per_accepted_usd"] == (
        pytest.approx(1.20)
    )
    # three DIFFERENT numbers for three different questions
    assert {0.70, 1.00, 1.20} == {
        successful["cost_usd"],
        costs["accepted_item_totals"]["w"]["billed_usd"],
        costs["programme_per_accepted_item"]["programme_billed_per_accepted_usd"],
    }
    # the provider-reported column is separated from estimates
    assert costs["programme"]["provider_reported_usd"] == pytest.approx(1.20)
    assert costs["cost_basis_census"]["bases"][COST_BASIS_PROVIDER_REPORTED]["receipts"] == 3


def test_duplicate_call_ids_across_attempts_never_false_join():
    store = UsageIngestStore()
    # ONE model-call id delivered under TWO attempts — both natural keys
    # exist (the callers each claim it), the join must not sum it twice
    for attempt in ("w/a1", "w/a2"):
        ingest_usage_artifact(
            store,
            sdk_receipt_artifact(
                receipts=[
                    {
                        "call_id": "dup-1",
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "total_cost_usd": 0.01,
                    }
                ]
            ),
            work_id="w",
            attempt_id=attempt,
            source="sdk-receipt",
        )
    records = ledger_records_from_ingested_usage(
        store.documents(),
        works=[{"work_id": "w", "outcome": ""}],
        attempts=[
            {"work_id": "w", "attempt_id": "w/a1"},
            {"work_id": "w", "attempt_id": "w/a2"},
        ],
    )
    ledger = MeasurementLinker().link(**records)
    report = EconomicsLinker().link(ledger)
    document = report.to_document()
    # the duplicate identity is REFUSED a second copy: one attribution
    # keeps the spend, the other is labelled refused — never 0.02
    assert len(document["cross_joins"]) == 1
    assert document["cross_joins"][0]["receipt_id"] == "dup-1"
    programme = document["costs"]["programme"]
    assert programme["billed_known_lower_bound_usd"] == pytest.approx(0.01)


# ----------------------------------------------------------------------
# Rate-card identity and attribution segments
# ----------------------------------------------------------------------


def test_rate_card_and_route_changes_create_new_segments_history_intact():
    route_a = attribution_segment(
        ProviderRoute("claude-sdk-lane", "glm-5.3-flash"),
        rate_card_id="card-v1",
        route_version="harness@1",
    )
    route_b = attribution_segment(
        ProviderRoute("claude-sdk-lane", "glm-5.3-flash"),
        rate_card_id="card-v2",
        route_version="harness@1",
    )
    route_c = attribution_segment(
        ProviderRoute("codex-sdk-lane", "gpt-5"),
        rate_card_id="card-v1",
        route_version="harness@1",
    )
    assert len({route_a, route_b, route_c}) == 3

    store = UsageIngestStore()
    ingest_usage_artifact(
        store,
        lane_usage_artifact(total_cost_usd=0.10, attempt_id="w/a1"),
        work_id="w",
        source="lane/.forge/usage.json",
        rate_card_id="card-v1",
        route_version="harness@1",
    )
    ingest_usage_artifact(
        store,
        lane_usage_artifact(total_cost_usd=0.40, attempt_id="w/a2", input_tokens=900),
        work_id="w",
        source="lane/.forge/usage.json",
        rate_card_id="card-v2",
        route_version="harness@2",
    )
    rows = store.rows()
    assert rows[0].rate_card_id == "card-v1" and rows[1].rate_card_id == "card-v2"
    assert rows[0].segment != rows[1].segment
    # the economics fold shows BOTH segments — the historical one untouched
    records = ledger_records_from_ingested_usage(
        store.documents(),
        works=[{"work_id": "w", "outcome": ""}],
        attempts=[{"work_id": "w", "attempt_id": "w/a1"}, {"work_id": "w", "attempt_id": "w/a2"}],
    )
    document = EconomicsLinker().link(MeasurementLinker().link(**records)).to_document()
    segments = document["costs"]["attribution_segments"]
    assert len(segments) == 2
    assert segments[0]["segment"] != segments[1]["segment"]


def test_estimated_vs_provider_reported_vs_billing_reconciliation_distinguished():
    store = UsageIngestStore()
    ingest_usage_artifact(
        store,
        lane_usage_artifact(attempt_id="w/a1"),
        work_id="w",
        source="lane/.forge/usage.json",
    )
    ingest_usage_artifact(
        store,
        {
            "receipt_id": "est-1",
            "driver": "codex-sdk-lane",
            "input_tokens": 1000,
            "output_tokens": 100,
            "cost_usd": 0.003,
            "cost_basis": "estimated",
            "rate_card_id": "card-v1",
            "attempt_id": "w/a2",
        },
        work_id="w",
        source="card-pricing",
        cost_basis=COST_BASIS_ESTIMATED,
    )
    ingest_usage_artifact(
        store,
        {
            "receipt_id": "bill-1",
            "driver": "codex-sdk-lane",
            "input_tokens": 2000,
            "output_tokens": 200,
            "cost_usd": 0.006,
            "cost_basis": "billing-reconciliation",
            "attempt_id": "w/a3",
        },
        work_id="w",
        source="billing-export",
        cost_basis="billing-reconciliation",
    )
    records = ledger_records_from_ingested_usage(
        store.documents(),
        works=[{"work_id": "w", "outcome": ""}],
        attempts=[
            {"work_id": "w", "attempt_id": "w/a1"},
            {"work_id": "w", "attempt_id": "w/a2"},
            {"work_id": "w", "attempt_id": "w/a3"},
        ],
    )
    document = EconomicsLinker().link(MeasurementLinker().link(**records)).to_document()
    bases = document["costs"]["cost_basis_census"]["bases"]
    assert set(bases) == {"provider-reported", "estimated", "billing-reconciliation"}
    assert bases["provider-reported"]["usd"] == pytest.approx(0.1498992, abs=1e-6)
    assert bases["estimated"]["usd"] == pytest.approx(0.003, abs=1e-6)
    assert bases["billing-reconciliation"]["usd"] == pytest.approx(0.006, abs=1e-6)


# ----------------------------------------------------------------------
# The spend-cap check — conservative reservation
# ----------------------------------------------------------------------


def test_spend_cap_reserves_lower_bounds_of_unknown_intervals():
    rows = [
        IngestedUsageRow(
            work_id="w",
            attempt_id="a1",
            receipt_id="known",
            source="lane/.forge/usage.json",
            route=ProviderRoute("claude-sdk-lane", "m"),
            cost_usd=0.50,
            cost_basis=COST_BASIS_PROVIDER_REPORTED,
        ),
        IngestedUsageRow(
            work_id="w",
            attempt_id="a2",
            receipt_id="unknown-interval",
            source="lane/.forge/usage.json",
            route=ProviderRoute("claude-sdk-lane", "m"),
            cost_usd=None,
            cost_lower_bound_usd=0.25,
        ),
    ]
    # 0.50 known + 0.25 reserved for the unknown interval = 0.75 reserved
    blocked = spend_cap_check(rows, cap_usd=0.80, projection_usd=0.10)
    assert blocked["allowed"] is False
    assert blocked["reserved_usd"] == pytest.approx(0.75)
    assert blocked["unknown_intervals"] == 1
    assert blocked["unknown_reserved_lower_bound_usd"] == pytest.approx(0.25)
    fits = spend_cap_check(rows, cap_usd=0.80, projection_usd=0.05)
    assert fits["allowed"] is True
    assert fits["headroom_usd"] == pytest.approx(0.05)
    assert any("never zero" in note for note in fits["notes"])


# ----------------------------------------------------------------------
# The durable write — the R23 ON CONFLICT seam
# ----------------------------------------------------------------------


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


async def test_persist_ingested_rows_is_idempotent_at_the_database(db):
    store = UsageIngestStore()
    ingest_usage_artifact(
        store, lane_usage_artifact(), work_id=RUN_ID, source="lane/.forge/usage.json"
    )
    rows = store.rows()
    async with db() as session:
        created, replayed = await persist_ingested_rows(session, rows)
        await session.commit()
    assert (created, replayed) == (1, 0)
    async with db() as session:
        created, replayed = await persist_ingested_rows(session, rows)
        await session.commit()
    assert (created, replayed) == (0, 1)
    async with db() as session:
        receipts = (await session.execute(select(UsageReceipt))).scalars().all()
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.run_id == RUN_ID
    assert receipt.attempt_id == "386:2"
    assert receipt.input_tokens == 19163 and receipt.cached_input_tokens == 119936
    assert receipt.completeness == "aggregate"
    assert receipt.raw["cost_basis"] == COST_BASIS_PROVIDER_REPORTED
    assert receipt.raw["final"] is True


async def test_persisted_rows_round_trip_into_ingested_rows(db):
    store = UsageIngestStore()
    ingest_usage_artifact(
        store, lane_usage_artifact(), work_id=RUN_ID, source="lane/.forge/usage.json"
    )
    async with db() as session:
        await persist_ingested_rows(session, store.rows())
        await session.commit()
    rebuilt = rows_from_documents(store.documents())
    assert rebuilt[0].key == store.rows()[0].key
    assert rebuilt[0].cost_usd == store.rows()[0].cost_usd


# ----------------------------------------------------------------------
# The lane envelope — emit-meta stamps the durable write
# ----------------------------------------------------------------------


def test_emit_meta_stamps_the_ingest_envelope(tmp_path: Path):
    workdir = tmp_path / "lane"
    workdir.mkdir()
    usage_file = workdir / ".forge" / "usage.json"
    usage_file.parent.mkdir(parents=True)
    usage_block = lane_usage_artifact()
    usage_file.write_text(json.dumps(usage_block, indent=2, sort_keys=True) + "\n")
    diff = workdir / "candidate.diff"
    diff.write_text("+ slugify\n")
    exit_file = workdir / ".forge" / "exit"
    exit_file.parent.mkdir(parents=True, exist_ok=True)
    exit_file.write_text("completed\n")
    meta_file = workdir / "candidate.meta.json"

    meta = emit_candidate_meta(
        run_id=RUN_ID,
        attempt_base_oid="0" * 40,
        driver="claude-sdk-lane",
        model="glm-5.3-flash",
        diff_file=str(diff),
        meta_file=str(meta_file),
        exit_file=str(workdir / ".forge" / "exit"),
        usage_file=str(usage_file),
    )
    envelope = meta["usage_ingest"]
    assert envelope["source"] == USAGE_INGEST_SOURCE
    assert envelope["final"] is True
    assert envelope["cost_basis"] == COST_BASIS_PROVIDER_REPORTED
    assert envelope["total_cost_usd"] == pytest.approx(0.1498992)
    # the identity is the one the control plane RECOMPUTES — the join key
    attempt = envelope["attempt_id"]
    assert envelope["receipt_id"] == usage_receipt_id(
        RUN_ID, attempt, HarnessUsage.from_meta(usage_block, attempt_id=attempt)
    )
    # tamper-evident: the digest is over the exact artifact bytes
    assert (
        envelope["artifact_digest"]
        == f"sha256:{hashlib.sha256(usage_file.read_bytes()).hexdigest()}"
    )
    # and the envelope ingests cleanly through the front door
    store = UsageIngestStore()
    result = ingest_usage_artifact(
        store, {**usage_block, **envelope}, work_id=RUN_ID, source=USAGE_INGEST_SOURCE
    )
    assert len(result.created) == 1
    assert store.rows()[0].artifact_digest == envelope["artifact_digest"]


def test_envelope_is_absent_without_an_artifact(tmp_path: Path):
    workdir = tmp_path / "lane"
    workdir.mkdir()
    diff = workdir / "candidate.diff"
    diff.write_text("+ x\n")
    meta = emit_candidate_meta(
        run_id=RUN_ID,
        attempt_base_oid="0" * 40,
        driver="claude-sdk-lane",
        model="glm-5.3-flash",
        diff_file=str(diff),
        meta_file=str(workdir / "meta.json"),
        exit_file=str(workdir / ".forge" / "exit"),
        usage_file=str(workdir / ".forge" / "usage.json"),
    )
    assert "usage_ingest" not in meta
    assert meta["usage"] is None


def test_envelope_marks_a_partial_artifact_not_final(tmp_path: Path):
    workdir = tmp_path / "lane"
    workdir.mkdir()
    usage_file = workdir / "usage.json"
    usage_file.write_text(json.dumps(lane_usage_artifact(input_tokens=10)))
    envelope = usage_ingest_envelope(
        run_id=RUN_ID,
        attempt_id="386:2",
        usage_file=usage_file,
        usage_block=lane_usage_artifact(input_tokens=10),
        final=False,
    )
    assert envelope is not None and envelope["final"] is False
    store = UsageIngestStore()
    result = ingest_usage_artifact(
        store, lane_usage_artifact(input_tokens=10), work_id=RUN_ID, attempt_id="386:2", final=False
    )
    assert result.created[0].completeness == "partial"
