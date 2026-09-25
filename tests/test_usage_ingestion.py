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
- SPEND CAPS (Q39-06/#325): the check before the next chargeable action
  keeps THREE quantities separated — known spend, the unknown intervals'
  lower bound, and their worst-case reserved LIABILITY (the upper
  envelope) — and the hard cap consults ``known + reserved_liability +
  projection``. A lower bound can never bound spend from above (the P03
  probe); an unknown interval with no finite upper bound BLOCKS the next
  chargeable action until an explicit bounded policy ceiling is supplied
  or the interval reconciles (releasing its envelope exactly once).
- DUPLICATE CALL IDS across attempts never false-join (one identity
  counted once, the duplication surfaced).
- THE DURABLE WRITE: ``persist_ingested_rows`` lands rows through the R23
  ``ON CONFLICT DO NOTHING`` seam — a replay writes nothing at the DB.
- THE LANE ENVELOPE: ``emit_candidate_meta`` stamps the ingest envelope
  (the receipt identity the control plane recomputes, the artifact digest,
  the final marker, the cost basis) so the artifact IS the durable write.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive.delivery_economics import EconomicsLinker
from forge.adaptive.delivery_measurement import (
    MeasurementLinker,
    ProviderRoute,
    ledger_records_from_ingested_usage,
)
from forge.adaptive.usage_ingestion import (
    ATTRIBUTION_REFUSED,
    COST_BASIS_ESTIMATED,
    COST_BASIS_PROVIDER_REPORTED,
    IDENTITY_REJECTED,
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
from forge.durable import FlowRun, UsageIngestionConflict, UsageReceipt
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
# The spend-cap check — three separated quantities, an UPPER envelope
# (Q39-06/#325)
# ----------------------------------------------------------------------


def _cap_row(receipt_id: str, *, cost=None, lower=0.0, upper=None, final=True, basis=""):
    return IngestedUsageRow(
        work_id="w",
        attempt_id="a1",
        receipt_id=receipt_id,
        source="lane/.forge/usage.json",
        route=ProviderRoute("claude-sdk-lane", "m"),
        cost_usd=cost,
        cost_basis=basis,
        cost_lower_bound_usd=lower,
        cost_upper_bound_usd=upper,
        final=final,
    )


def test_the_p03_counterexample_refuses_a_lower_bound_reservation():
    """P03: cap 10, known 8, unknown interval with lower bound 0.5,
    projection 1. The old helper reserved the LOWER bound and "allowed"
    9.5 — while the interval may settle at 3 for an actual 12. A lower
    bound cannot bound spend from above, so without a finite envelope
    the check BLOCKS (requires_bounded_policy), and WITH a bounded
    policy covering the honest worst case the hard cap REFUSES."""
    rows = [
        _cap_row("known", cost=8.0, basis=COST_BASIS_PROVIDER_REPORTED),
        _cap_row("unknown", cost=None, lower=0.5),
    ]
    # no finite upper bound exists -> the next chargeable action blocks
    unbounded = spend_cap_check(rows, cap_usd=10.0, projection_usd=1.0)
    assert unbounded["allowed"] is False
    assert unbounded["requires_bounded_policy"] is True
    assert unbounded["unbounded_intervals"] == 1
    # the explicit bounded policy: the interval's honest worst case is 3
    bounded = spend_cap_check(
        rows, cap_usd=10.0, projection_usd=1.0, unknown_interval_ceiling_usd=3.0
    )
    # 8 known + 3 reserved liability + 1 projection = 12 > 10 -> REFUSED
    assert bounded["allowed"] is False
    assert bounded["requires_bounded_policy"] is False
    assert bounded["reserved_liability"] == pytest.approx(3.0)
    assert bounded["known_spend"] == pytest.approx(8.0)
    # and a bounded policy whose envelope genuinely fits may allow
    fits = spend_cap_check(rows, cap_usd=10.0, projection_usd=1.0, unknown_interval_ceiling_usd=0.5)
    assert fits["allowed"] is True  # 8 + 0.5 + 1 = 9.5 <= 10


def test_three_quantities_stay_separated_in_the_check():
    rows = [
        _cap_row("known-reported", cost=0.50, basis=COST_BASIS_PROVIDER_REPORTED),
        _cap_row("known-estimate", cost=0.10, basis=COST_BASIS_ESTIMATED),
        _cap_row("own-envelope", cost=None, lower=0.25, upper=0.90),
        _cap_row("policy-envelope", cost=None, lower=0.05),
    ]
    check = spend_cap_check(
        rows, cap_usd=5.0, projection_usd=0.10, unknown_interval_ceiling_usd=0.40
    )
    assert check["known_spend"] == pytest.approx(0.60)
    assert check["unknown_lower_bound"] == pytest.approx(0.30)
    assert check["reserved_liability"] == pytest.approx(1.30)  # 0.90 own + 0.40 policy
    assert check["unknown_intervals"] == 2
    assert check["reserved_usd"] == pytest.approx(1.90)
    assert check["headroom_usd"] == pytest.approx(5.0 - 1.90)
    assert check["allowed"] is True
    # the three quantities are DIFFERENT numbers, never folded into one
    assert (
        len(
            {
                round(check["known_spend"], 6),
                round(check["unknown_lower_bound"], 6),
                round(check["reserved_liability"], 6),
            }
        )
        == 3
    )
    assert any("never reserved as zero" in note for note in check["notes"])


def test_no_finite_bound_blocks_the_next_chargeable_action():
    """An unknown interval with neither its own upper bound nor a policy
    ceiling cannot be bounded from above — the explicit bounded-policy
    arm: block, or supply the policy field."""
    rows = [_cap_row("unknown", cost=None, lower=0.25)]
    check = spend_cap_check(rows, cap_usd=100.0, projection_usd=0.0)
    assert check["allowed"] is False
    assert check["requires_bounded_policy"] is True
    assert check["reserved_liability"] == pytest.approx(0.0)
    assert check["unknown_lower_bound"] == pytest.approx(0.25)  # still reported
    assert any("bounded policy" in note for note in check["notes"])
    # the row's OWN finite bound satisfies the policy arm on its own
    own = spend_cap_check(
        [_cap_row("unknown", cost=None, lower=0.25, upper=2.0)], cap_usd=3.0, projection_usd=0.5
    )
    assert own["allowed"] is True  # 0 + 2.0 + 0.5 <= 3
    assert own["requires_bounded_policy"] is False


def test_reconciliation_releases_a_reserve_exactly_once():
    """A partial with an unknown envelope holds its worst-case reserve;
    the final reconciles it to an exact cost (the reserve is RELEASED);
    re-delivering the same final is a replay — released ONCE, never
    twice, and the exact cost joins the known spend."""
    store = UsageIngestStore()
    ingest_usage_artifact(
        store,
        lane_usage_artifact(total_cost_usd=None, cost_upper_bound_usd=0.50, attempt_id="w/a1"),
        work_id="w",
        attempt_id="w/a1",
        source="lane/.forge/usage.json",
        final=False,
    )
    held = spend_cap_check(store.rows(), cap_usd=10.0, unknown_interval_ceiling_usd=1.0)
    assert held["requires_bounded_policy"] is False
    assert held["reserved_liability"] == pytest.approx(0.50)  # its own envelope
    assert held["unknown_intervals"] == 1

    # the final state reconciles the partial: cost KNOWN (0.1498992),
    # the envelope released
    final = ingest_usage_artifact(
        store,
        lane_usage_artifact(attempt_id="w/a1"),
        work_id="w",
        attempt_id="w/a1",
        source="lane/.forge/usage.json",
        final=True,
    )
    assert len(final.reconciled) == 1
    released = spend_cap_check(store.rows(), cap_usd=10.0, unknown_interval_ceiling_usd=1.0)
    assert released["unknown_intervals"] == 0
    assert released["reserved_liability"] == pytest.approx(0.0)
    assert released["known_spend"] == pytest.approx(0.1498992)

    # a replayed identical final writes NOTHING — the release happened
    # exactly once (a second "release" would double-count headroom)
    replay = ingest_usage_artifact(
        store,
        lane_usage_artifact(attempt_id="w/a1"),
        work_id="w",
        attempt_id="w/a1",
        source="lane/.forge/usage.json",
        final=True,
    )
    assert not replay.wrote_something_new
    once = spend_cap_check(store.rows(), cap_usd=10.0, unknown_interval_ceiling_usd=1.0)
    assert once["known_spend"] == released["known_spend"]
    assert once["reserved_liability"] == released["reserved_liability"]
    assert once["unknown_intervals"] == 0


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
        outcome = await persist_ingested_rows(session, rows)
        await session.commit()
    assert (outcome.created, outcome.replayed) == (1, 0)
    async with db() as session:
        outcome = await persist_ingested_rows(session, rows)
        await session.commit()
    assert (outcome.created, outcome.replayed) == (0, 1)
    async with db() as session:
        receipts = (await session.execute(select(UsageReceipt))).scalars().all()
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.run_id == RUN_ID
    assert receipt.attempt_id == "386:2"
    assert receipt.input_tokens == 19163 and receipt.cached_input_tokens == 119936
    assert receipt.completeness == "aggregate"
    assert receipt.source_namespace == "lane/.forge/usage.json"
    assert receipt.final is True
    assert receipt.cost_usd == pytest.approx(0.1498992)
    assert receipt.cost_basis == COST_BASIS_PROVIDER_REPORTED
    assert receipt.artifact_digest
    assert receipt.raw["final"] is True
    # the canonical identity digest — stable across replays
    assert receipt.identity_digest.startswith("identity:")


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


# ----------------------------------------------------------------------
# Q39-05 (#324) — ONE durable identity and reconciliation contract
# ----------------------------------------------------------------------


def _ingested(**overrides):
    """One ingested row built directly (the durable path's unit)."""
    base = dict(
        work_id=RUN_ID,
        attempt_id="386:2",
        receipt_id="receipt-1",
        source="lane/.forge/usage.json",
        route=ProviderRoute("claude-sdk-lane", "glm-5.3-flash"),
        counters=normalize_counters(
            {"input_tokens": 1000, "output_tokens": 100, "driver": "claude-sdk-lane"}
        ),
        cost_usd=0.20,
        cost_basis=COST_BASIS_PROVIDER_REPORTED,
    )
    base.update(overrides)
    return IngestedUsageRow(**base)


async def test_the_p02_shape_partial_reconciles_to_final_at_the_database(db):
    """P02's exact counterexample: partial 0.20/final=False stored, then
    the 1.20 final arrives — ONE logical receipt at 1.20, final. The old
    ``ON CONFLICT DO NOTHING`` seam left the partial standing FOREVER."""
    async with db() as session:
        outcome = await persist_ingested_rows(session, [_ingested(final=False, cost_usd=0.20)])
        await session.commit()
    assert outcome.created == 1
    async with db() as session:
        outcome = await persist_ingested_rows(
            session,
            [
                _ingested(
                    final=True,
                    cost_usd=1.20,
                    counters=normalize_counters(
                        {"input_tokens": 19163, "output_tokens": 2463, "driver": "claude-sdk-lane"}
                    ),
                )
            ],
        )
        await session.commit()
    assert outcome.reconciled == 1
    async with db() as session:
        receipts = (await session.execute(select(UsageReceipt))).scalars().all()
    assert len(receipts) == 1  # ONE logical receipt — never summed
    assert receipts[0].cost_usd == pytest.approx(1.20)
    assert receipts[0].final is True
    assert receipts[0].input_tokens == 19163


async def test_a_late_partial_never_downgrades_a_final(db):
    async with db() as session:
        await persist_ingested_rows(session, [_ingested(final=True, cost_usd=1.20)])
        await session.commit()
    async with db() as session:
        outcome = await persist_ingested_rows(session, [_ingested(final=False, cost_usd=0.20)])
        await session.commit()
    # not reconciled, not created — the final stands untouched
    assert outcome.reconciled == 0 and outcome.created == 0
    async with db() as session:
        receipts = (await session.execute(select(UsageReceipt))).scalars().all()
    assert receipts[0].final is True and receipts[0].cost_usd == pytest.approx(1.20)


async def test_a_repeated_identical_final_is_a_no_op(db):
    async with db() as session:
        await persist_ingested_rows(session, [_ingested(final=True)])
        await session.commit()
    async with db() as session:
        outcome = await persist_ingested_rows(session, [_ingested(final=True)])
        await session.commit()
    assert (outcome.created, outcome.reconciled, outcome.replayed) == (0, 0, 1)
    async with db() as session:
        conflicts = (await session.execute(select(UsageIngestionConflict))).scalars().all()
    assert conflicts == []


async def test_a_conflicting_final_is_recorded_never_merged_or_dropped(db):
    async with db() as session:
        await persist_ingested_rows(session, [_ingested(final=True, cost_usd=1.20)])
        await session.commit()
    async with db() as session:
        outcome = await persist_ingested_rows(session, [_ingested(final=True, cost_usd=0.99)])
        await session.commit()
    assert outcome.conflicts == 1
    async with db() as session:
        receipts = (await session.execute(select(UsageReceipt))).scalars().all()
        conflicts = (await session.execute(select(UsageIngestionConflict))).scalars().all()
    # the standing 1.20 final never moved
    assert receipts[0].cost_usd == pytest.approx(1.20)
    assert len(conflicts) == 1
    assert conflicts[0].kind == "conflicting-final"
    assert conflicts[0].detail["standing"]["cost_usd"] == pytest.approx(1.20)
    assert conflicts[0].detail["delivered"]["cost_usd"] == pytest.approx(0.99)
    # replaying the SAME conflicting delivery records ONE diagnostic, not N
    async with db() as session:
        outcome = await persist_ingested_rows(session, [_ingested(final=True, cost_usd=0.99)])
        await session.commit()
    assert outcome.conflicts == 1
    async with db() as session:
        conflicts = (await session.execute(select(UsageIngestionConflict))).scalars().all()
    assert len(conflicts) == 1


async def test_two_source_namespaces_same_label_stay_two_rows(db):
    async with db() as session:
        outcome = await persist_ingested_rows(
            session,
            [
                _ingested(source="lane/.forge/usage.json"),
                _ingested(source="sdk-receipt"),
            ],
        )
        await session.commit()
    assert outcome.created == 2  # the old 3-column identity collapsed these
    async with db() as session:
        receipts = (await session.execute(select(UsageReceipt))).scalars().all()
    assert sorted(receipt.source_namespace for receipt in receipts) == [
        "lane/.forge/usage.json",
        "sdk-receipt",
    ]


async def test_the_attribution_refused_arm_writes_no_row_to_the_claimed_work(db):
    """The payload claims work-B; the trusted transport is work-A: no B
    row anywhere, the refusal preserved as a durable diagnostic."""
    store = UsageIngestStore()
    result = ingest_usage_artifact(
        store,
        lane_usage_artifact(work_id="b" * 32),
        work_id=RUN_ID,
        source="lane/.forge/usage.json",
    )
    assert result.created == () and len(result.refused) == 1
    refusal = result.refused[0]
    assert refusal.kind == ATTRIBUTION_REFUSED
    assert refusal.trusted_work_id == RUN_ID and refusal.claimed_work_id == "b" * 32
    assert store.rows() == []  # nothing ingested in memory either

    async with db() as session:
        outcome = await persist_ingested_rows(session, [], refusals=result.refused)
        await session.commit()
    assert outcome.refusals == 1
    async with db() as session:
        receipts = (await session.execute(select(UsageReceipt))).scalars().all()
        conflicts = (await session.execute(select(UsageIngestionConflict))).scalars().all()
    assert receipts == []  # no row to work-B AND no row to work-A
    assert len(conflicts) == 1
    assert conflicts[0].kind == ATTRIBUTION_REFUSED
    assert conflicts[0].run_id == RUN_ID  # keyed by the TRUSTED work
    assert conflicts[0].detail["claimed_work_id"] == "b" * 32


def test_a_payload_attempt_conflicting_with_the_trusted_attempt_is_refused():
    store = UsageIngestStore()
    result = ingest_usage_artifact(
        store,
        lane_usage_artifact(attempt_id="999:9"),
        work_id=RUN_ID,
        attempt_id="386:2",
        source="lane/.forge/usage.json",
    )
    assert result.created == () and len(result.refused) == 1
    assert result.refused[0].claimed_attempt_id == "999:9"
    assert result.refused[0].attempt_id == "386:2"
    # and without a trusted attempt claim the payload's own stands
    loose = ingest_usage_artifact(store, lane_usage_artifact(attempt_id="999:9"), work_id=RUN_ID)
    assert len(loose.created) == 1 and loose.created[0].attempt_id == "999:9"


async def test_overlength_identities_hash_never_silently_truncate(db):
    """Two receipt ids sharing their first 64 characters stay DISTINCT
    (the old ``[:64]`` truncation collapsed them); the full originals
    ride ``raw``; a work id wider than the run id column is REJECTED."""
    shared = "r" * 64
    first = _ingested(receipt_id=shared + "-a")
    second = _ingested(receipt_id=shared + "-b")
    async with db() as session:
        outcome = await persist_ingested_rows(session, [first, second])
        await session.commit()
    assert outcome.created == 2
    async with db() as session:
        receipts = (await session.execute(select(UsageReceipt))).scalars().all()
    assert len(receipts) == 2
    assert len({receipt.receipt_id for receipt in receipts}) == 2
    for receipt in receipts:
        assert receipt.receipt_id.startswith("sha256:")
    assert sorted(receipt.raw["identity_overlength"]["receipt_id"] for receipt in receipts) == [
        shared + "-a",
        shared + "-b",
    ]

    # an overlength attempt id hashes the same documented way
    async with db() as session:
        outcome = await persist_ingested_rows(
            session, [_ingested(receipt_id="fitting", attempt_id="a" * 120)]
        )
        await session.commit()
    assert outcome.created == 1

    # a work id the run-id column cannot hold names NO run row — rejected
    async with db() as session:
        outcome = await persist_ingested_rows(
            session, [_ingested(work_id="w" * 40, receipt_id="wide-work")]
        )
        await session.commit()
    assert outcome.refusals == 1 and outcome.created == 0
    async with db() as session:
        conflicts = (await session.execute(select(UsageIngestionConflict))).scalars().all()
    assert conflicts[-1].kind == IDENTITY_REJECTED
    assert conflicts[-1].detail["full_work_id"] == "w" * 40


async def test_array_receipts_without_ids_never_collapse(db):
    """Missing per-call IDs key on POSITION — an array of N id-less calls
    is N distinct receipts, never one aggregate key."""
    store = UsageIngestStore()
    result = ingest_usage_artifact(
        store,
        {
            "provider": "claude-sdk-lane",
            "receipts": [
                {"input_tokens": 10, "output_tokens": 1, "total_cost_usd": 0.01},
                {"input_tokens": 20, "output_tokens": 2, "total_cost_usd": 0.02},
                {"input_tokens": 30, "output_tokens": 3, "total_cost_usd": 0.03},
            ],
        },
        work_id=RUN_ID,
        attempt_id="job800",
        source="sdk-receipt",
    )
    assert len(result.created) == 3
    assert len({row.receipt_id for row in result.created}) == 3
    async with db() as session:
        outcome = await persist_ingested_rows(session, store.rows())
        await session.commit()
    assert outcome.created == 3
    async with db() as session:
        receipts = (await session.execute(select(UsageReceipt))).scalars().all()
    assert len(receipts) == 3


async def test_reordered_and_repeated_delivery_yields_the_same_ledger(db):
    """AC-05: reorder, repeat and resume — the final ledger is identical."""
    final_rows = [
        _ingested(receipt_id="a", cost_usd=0.10),
        _ingested(receipt_id="b", cost_usd=0.20),
        _ingested(receipt_id="c", final=False, cost_usd=0.05),
    ]
    async with db() as session:
        await persist_ingested_rows(session, list(reversed(final_rows)))
        await session.commit()
    async with db() as session:
        # the partial c reconciles late; everything re-delivered once
        await persist_ingested_rows(session, final_rows)
        await session.commit()
    async with db() as session:
        await persist_ingested_rows(session, [_ingested(receipt_id="c", final=True, cost_usd=0.50)])
        await session.commit()

    first = await _ledger_snapshot(db)

    # a full replay of everything, reversed again — byte-identical ledger
    async with db() as session:
        await persist_ingested_rows(
            session,
            [
                _ingested(receipt_id="c", final=True, cost_usd=0.50),
                _ingested(receipt_id="b", cost_usd=0.20),
                _ingested(receipt_id="a", cost_usd=0.10),
            ],
        )
        await session.commit()
    async with db() as session:
        receipts = (await session.execute(select(UsageReceipt))).scalars().all()
    by_receipt = {receipt.receipt_id: receipt for receipt in receipts}
    assert len(receipts) == 3
    assert by_receipt["a"].cost_usd == pytest.approx(0.10)
    assert by_receipt["b"].cost_usd == pytest.approx(0.20)
    assert by_receipt["c"].cost_usd == pytest.approx(0.50) and by_receipt["c"].final is True
    assert first == {  # the replay moved nothing
        receipt.receipt_id: (receipt.cost_usd, receipt.final) for receipt in receipts
    }


async def _ledger_snapshot(db):
    async with db() as session:
        receipts = (await session.execute(select(UsageReceipt))).scalars().all()
    return {receipt.receipt_id: (receipt.cost_usd, receipt.final) for receipt in receipts}


async def test_two_independent_sessions_partial_and_final_concurrently(tmp_path):
    """Two ENGINES over one sqlite FILE (real independent sessions, a
    barrier at the start): one side delivers the partial, the other the
    final — the final ledger holds ONE final receipt whichever write the
    arbiter serialized first."""
    url = f"sqlite+aiosqlite:///{tmp_path}/two-sessions.db"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory_a = async_sessionmaker(engine, expire_on_commit=False)
    factory_b = async_sessionmaker(engine, expire_on_commit=False)
    async with factory_a() as session:
        session.add(FlowRun(id=RUN_ID, project_id=1))
        await session.commit()

    barrier = asyncio.Barrier(2)

    async def deliver(factory, row):
        async with factory() as session:
            await barrier.wait()  # both sessions hold open transactions
            outcome = await persist_ingested_rows(session, [row])
            await session.commit()
            return outcome

    partial = _ingested(final=False, cost_usd=0.20)
    final = _ingested(final=True, cost_usd=1.20)
    outcomes = await asyncio.gather(deliver(factory_a, partial), deliver(factory_b, final))
    # whichever write the arbiter serialized first, exactly ONE logical
    # receipt was landed or reconciled by the pair — partial-then-final
    # (created + reconciled = 2) or final-first (created = 1, the late
    # partial classifies as a replay against the standing final).
    landed = sum(outcome.created + outcome.reconciled for outcome in outcomes)
    assert landed in (1, 2)

    async with factory_a() as session:
        receipts = (await session.execute(select(UsageReceipt))).scalars().all()
    assert len(receipts) == 1
    assert receipts[0].final is True and receipts[0].cost_usd == pytest.approx(1.20)

    # a resumed delivery after "process death" reconciles whatever stood
    async with factory_b() as session:
        outcome = await persist_ingested_rows(session, [final])
        await session.commit()
    assert outcome.replayed == 1
    await engine.dispose()


class TestQ3905RealPostgres:
    """The PG-gated isolation variants (the podman disposable-DB pattern —
    same skipif convention as the checkpoint gate): REAL row-level
    isolation, two independent sessions, a barrier between them."""

    @pytest.mark.skipif(
        not os.environ.get("FORGE_PG_TEST_URL"),
        reason=(
            "FORGE_PG_TEST_URL not set — the real-PostgreSQL isolation proofs "
            "run only against a disposable real Postgres"
        ),
    )
    async def test_concurrent_partial_and_final_reconcile_under_real_isolation(self):
        engine = create_async_engine(os.environ["FORGE_PG_TEST_URL"])
        try:
            from forge.durable.models import CredentialRedemption

            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(delete(UsageReceipt))
                await conn.execute(delete(UsageIngestionConflict))
                # the shared disposable database may hold ledger rows for
                # this run id from a sibling PG-gated proof — clear them
                # before the run row itself (the FK would refuse).
                await conn.execute(
                    delete(CredentialRedemption).where(CredentialRedemption.work_id == RUN_ID)
                )
                await conn.execute(delete(FlowRun).where(FlowRun.id == RUN_ID))
            factory = async_sessionmaker(engine, expire_on_commit=False)
            # the run row through the ORM: PG carries no server defaults
            # for status/created_at, so a raw INSERT would violate NOT NULL
            async with factory() as session:
                session.add(FlowRun(id=RUN_ID, project_id=1))
                await session.commit()
            barrier = asyncio.Barrier(2)

            async def deliver(row):
                async with factory() as session:
                    await barrier.wait()
                    outcome = await persist_ingested_rows(session, [row])
                    await session.commit()
                    return outcome

            outcomes = await asyncio.gather(
                deliver(_ingested(final=False, cost_usd=0.20)),
                deliver(_ingested(final=True, cost_usd=1.20)),
            )
            async with factory() as session:
                receipts = (await session.execute(select(UsageReceipt))).scalars().all()
            assert len(receipts) == 1  # one logical receipt
            assert receipts[0].final is True
            assert receipts[0].cost_usd == pytest.approx(1.20)
            assert sum(o.created + o.reconciled for o in outcomes) in (1, 2)
        finally:
            await engine.dispose()
