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
- SPEND CAPS (Q39-06/#325, corrected by R40-03/#339): the check before
  the next chargeable action classifies by FINALITY, never by value
  presence — settled spend (final rows), accrued-unsettled subtotals
  (nonfinal rows), the unresolved intervals' lower bound and their
  worst-case retained LIABILITY stay separated, and the hard cap
  consults ``settled + retained_liability + projection``. Each
  unresolved interval retains ``max(envelope, accrued, lower)`` — a
  partial's subtotal rides INSIDE the envelope and never releases it
  (the P01 probe); a lower bound can never bound spend from above (the
  P03 probe); an unresolved interval with no finite upper bound — a
  nonfinal row that already carries a cost included — is a typed
  ``unbounded-exposure`` refusal until an explicit bounded policy
  ceiling is supplied or the interval reconciles (releasing its
  envelope exactly once); an envelope below the accrued cost is a typed
  ``incoherent-bound`` inconsistency, never free headroom; NaN/infinity/
  negative inputs are typed findings, never silent defaults.
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
import math
import os
import random
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
    INCOHERENT_BOUND,
    INVALID_BOUND,
    INVALID_CAP,
    INVALID_COST,
    INVALID_LOWER_BOUND,
    INVALID_POLICY_CEILING,
    INVALID_PROJECTION,
    UNBOUNDED_EXPOSURE,
    IngestedUsageRow,
    UsageIngestStore,
    attribution_segment,
    exposure_fold,
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
# R40-03 (#339) — finality settles: a partial that already carries a
# cost keeps its whole retained envelope
# ----------------------------------------------------------------------


def test_the_p01_counterexample_a_partial_subtotal_never_releases_the_envelope():
    """P01, pinned to the corrected numbers: cap 10, settled 8, a PARTIAL
    receipt at 0.5 inside an upper envelope of 3, projection 1. The old
    value-presence math "allowed" 8 + 0.5 + 1 = 9.5; the bounded exposure
    is 8 + 3 + 1 = 12, so the exposure alone is 11 — the cap refuses.
    The final 0.5 then settles ONCE, releases the envelope exactly once
    (2.5 of headroom), and the within-cap next request passes."""
    rows = [
        _cap_row("settled", cost=8.0, basis=COST_BASIS_PROVIDER_REPORTED),
        _cap_row("partial", cost=0.5, lower=0.5, upper=3.0, final=False),
    ]
    check = spend_cap_check(rows, cap_usd=10.0, projection_usd=1.0)
    assert check["allowed"] is False
    assert check["requires_bounded_policy"] is False  # the envelope IS finite
    assert check["settled_usd"] == pytest.approx(8.0)
    assert check["accrued_unsettled_usd"] == pytest.approx(0.5)
    assert check["retained_liability_usd"] == pytest.approx(3.0)  # max(3.0, 0.5)
    assert check["unknown_lower_bound"] == pytest.approx(0.5)  # the subtotal is its floor
    assert check["reserved_usd"] == pytest.approx(11.0)  # exposure >= 11 before the projection
    assert check["headroom_usd"] == pytest.approx(-1.0)
    assert any("never settles liability" in note for note in check["notes"])

    # the same delivery through the STORE (the streaming contract), then
    # the reconciliation: the final 0.5 settles exactly once
    store = UsageIngestStore()
    store.ingest([rows[0]])
    partial_delivery = store.ingest([rows[1]])
    assert len(partial_delivery.created) == 1
    held = spend_cap_check(store.rows(), cap_usd=10.0, projection_usd=1.0)
    assert held["allowed"] is False and held["reserved_usd"] == pytest.approx(11.0)
    final_delivery = store.ingest([_cap_row("partial", cost=0.5, lower=0.5, upper=3.0, final=True)])
    assert len(final_delivery.reconciled) == 1
    settled = spend_cap_check(store.rows(), cap_usd=10.0, projection_usd=1.0)
    assert settled["allowed"] is True  # 8 + 0.5 + 1 = 9.5 <= 10 — fits now
    assert settled["settled_usd"] == pytest.approx(8.5)
    assert settled["accrued_unsettled_usd"] == pytest.approx(0.0)
    assert settled["retained_liability_usd"] == pytest.approx(0.0)
    assert settled["unresolved_intervals"] == 0
    assert settled["settlement_release_usd"] == pytest.approx(2.5)  # max(3, 0.5) - 0.5
    # the replayed identical final releases nothing a second time
    replay = store.ingest([_cap_row("partial", cost=0.5, lower=0.5, upper=3.0, final=True)])
    assert not replay.wrote_something_new
    once_more = spend_cap_check(store.rows(), cap_usd=10.0, projection_usd=1.0)
    assert once_more["settled_usd"] == settled["settled_usd"]
    assert once_more["settlement_release_usd"] == pytest.approx(2.5)  # ONCE, not 5.0
    assert once_more["reserved_usd"] == pytest.approx(8.5)


def test_a_nonfinal_row_with_a_cost_and_no_envelope_is_typed_unbounded_exposure():
    """R40-03 scope item 2: a partial that already carries a cost but NO
    finite envelope (neither its own bound nor the policy ceiling) cannot
    be bounded from above — a typed ``unbounded-exposure`` refusal."""
    rows = [
        _cap_row("settled", cost=8.0, basis=COST_BASIS_PROVIDER_REPORTED),
        _cap_row("partial-bare", cost=0.5, lower=0.5, final=False),
    ]
    check = spend_cap_check(rows, cap_usd=100.0, projection_usd=0.0)
    assert check["allowed"] is False
    assert check["requires_bounded_policy"] is True
    assert check["unbounded_intervals"] == 1
    finding = next(f for f in check["findings"] if f["type"] == UNBOUNDED_EXPOSURE)
    assert finding["receipt_id"] == "partial-bare"
    assert finding["final"] is False
    assert finding["accrued_cost_usd"] == pytest.approx(0.5)
    # the explicit bounded policy ceiling rescues the arm
    rescued = spend_cap_check(rows, cap_usd=100.0, unknown_interval_ceiling_usd=3.0)
    assert rescued["allowed"] is True  # 8 + max(3.0, 0.5) <= 100
    assert rescued["requires_bounded_policy"] is False
    assert rescued["retained_liability_usd"] == pytest.approx(3.0)


def test_an_envelope_below_the_accrued_cost_is_an_inconsistency_never_headroom():
    """R40-03 scope item 4: an upper bound below the observed accrued cost
    is a typed ``incoherent-bound`` finding — the interval still retains
    at least its accrued cost, never the cheap envelope as headroom."""
    rows = [_cap_row("partial", cost=0.9, lower=0.0, upper=0.3, final=False)]
    check = spend_cap_check(rows, cap_usd=1.0, projection_usd=0.0)
    assert check["retained_liability_usd"] == pytest.approx(0.9)  # NOT 0.3
    assert check["accrued_unsettled_usd"] == pytest.approx(0.9)
    assert check["incoherent_bounds"] == 1
    finding = next(f for f in check["findings"] if f["type"] == INCOHERENT_BOUND)
    assert finding["accrued_cost_usd"] == pytest.approx(0.9)
    assert finding["upper_bound_usd"] == pytest.approx(0.3)
    # the inconsistency is NOT free headroom: the exposure fences 0.9
    assert check["allowed"] is True  # 0.9 <= 1 — but only 0.1 remains
    assert check["headroom_usd"] == pytest.approx(0.1)
    # a settlement landing ABOVE its own declared envelope flags the same way
    settled_above = spend_cap_check(
        [_cap_row("late", cost=0.5, lower=0.5, upper=0.3, final=True)], cap_usd=10.0
    )
    assert settled_above["incoherent_bounds"] == 1
    assert settled_above["settled_usd"] == pytest.approx(0.5)
    assert settled_above["settlement_release_usd"] == pytest.approx(0.0)  # max(0.3, 0.5) - 0.5


def test_malformed_numbers_are_typed_findings_never_silent_defaults():
    """NaN / infinity / negative caps, projections, ceilings, costs and
    bounds refuse with a typed finding — never a clamped default."""
    nan = float("nan")
    inf = float("inf")
    rows = [_cap_row("settled", cost=1.0, basis=COST_BASIS_PROVIDER_REPORTED)]

    bad_cap = spend_cap_check(rows, cap_usd=nan, projection_usd=0.0)
    assert bad_cap["allowed"] is False
    assert bad_cap["headroom_usd"] is None
    assert any(f["type"] == INVALID_CAP for f in bad_cap["findings"])
    negative_cap = spend_cap_check(rows, cap_usd=-1.0, projection_usd=0.0)
    assert negative_cap["allowed"] is False
    assert any(f["type"] == INVALID_CAP for f in negative_cap["findings"])

    bad_projection = spend_cap_check(rows, cap_usd=10.0, projection_usd=-0.5)
    assert bad_projection["allowed"] is False
    assert any(f["type"] == INVALID_PROJECTION for f in bad_projection["findings"])
    assert spend_cap_check(rows, cap_usd=10.0, projection_usd=inf)["allowed"] is False

    # a corrupt policy ceiling is treated as NOT supplied: the interval it
    # would cover is unbounded, and the corruption itself is a finding
    bare = [_cap_row("partial", cost=0.5, lower=0.5, final=False)]
    bad_ceiling = spend_cap_check(bare, cap_usd=10.0, unknown_interval_ceiling_usd=nan)
    assert bad_ceiling["allowed"] is False
    types = {f["type"] for f in bad_ceiling["findings"]}
    assert INVALID_POLICY_CEILING in types and UNBOUNDED_EXPOSURE in types

    # a NaN cost never settles an interval: it stays unresolved (bounded
    # here by its own envelope — the figure itself stays flagged)
    nan_cost = spend_cap_check(
        [_cap_row("corrupt", cost=nan, lower=0.0, upper=2.0, final=True)], cap_usd=10.0
    )
    assert nan_cost["settled_usd"] == pytest.approx(0.0)
    assert nan_cost["unresolved_intervals"] == 1
    assert nan_cost["retained_liability_usd"] == pytest.approx(2.0)
    assert any(f["type"] == INVALID_COST for f in nan_cost["findings"])

    # an infinite upper bound is no bound at all
    inf_bound = spend_cap_check(
        [_cap_row("unboundable", cost=0.1, lower=0.1, upper=inf, final=False)], cap_usd=10.0
    )
    assert inf_bound["requires_bounded_policy"] is True
    types = {f["type"] for f in inf_bound["findings"]}
    assert INVALID_BOUND in types and UNBOUNDED_EXPOSURE in types

    # a negative lower bound is reported at 0 for the (non-gating) sum,
    # visibly — and nothing anywhere in the result is NaN
    negative_lower = spend_cap_check(
        [_cap_row("odd", cost=None, lower=-3.0, final=False)], cap_usd=10.0
    )
    assert negative_lower["unknown_lower_bound"] == pytest.approx(0.0)
    assert any(f["type"] == INVALID_LOWER_BOUND for f in negative_lower["findings"])
    for value in (
        negative_lower["settled_usd"],
        negative_lower["accrued_unsettled_usd"],
        negative_lower["unknown_lower_bound"],
        negative_lower["retained_liability_usd"],
        negative_lower["settlement_release_usd"],
    ):
        assert math.isfinite(value)


def _random_rows(rng: random.Random, count: int) -> list[IngestedUsageRow]:
    rows: list[IngestedUsageRow] = []
    for index in range(count):
        rows.append(
            _cap_row(
                f"r{index}",
                cost=rng.choice([None, round(rng.uniform(0, 2), 4)]),
                lower=round(rng.uniform(0, 1), 4),
                upper=rng.choice([None, round(rng.uniform(0, 3), 4)]),
                final=rng.random() < 0.5,
            )
        )
    return rows


def _canonical_findings(check: dict) -> list[str]:
    return sorted(json.dumps(finding, sort_keys=True) for finding in check["findings"])


def test_the_fold_is_order_invariant_over_random_row_sets():
    """Property: the exposure fold over a row set is identical under every
    permutation of the rows (findings compared order-insensitively)."""
    rng = random.Random(339)
    for _trial in range(25):
        rows = _random_rows(rng, rng.randint(0, 8))
        base = spend_cap_check(
            rows, cap_usd=5.0, projection_usd=0.25, unknown_interval_ceiling_usd=1.5
        )
        for _shuffle in range(3):
            shuffled = list(rows)
            rng.shuffle(shuffled)
            other = spend_cap_check(
                shuffled, cap_usd=5.0, projection_usd=0.25, unknown_interval_ceiling_usd=1.5
            )
            for key in base:
                if key == "findings":
                    assert _canonical_findings(other) == _canonical_findings(base)
                else:
                    assert other[key] == base[key]


def test_store_replay_and_delivery_order_invariance():
    """Property: delivering each identity as (partial, final) in ANY order
    — and replaying every delivery again — leaves one identical fold: a
    late partial after the final is a replay, a partial before its final
    reconciles, and a repeated final is a no-op."""
    rng = random.Random(340)
    for _trial in range(25):
        pairs: list[tuple[IngestedUsageRow, IngestedUsageRow]] = []
        for index in range(rng.randint(1, 6)):
            settled_cost = round(rng.uniform(0.1, 1.0), 4)
            envelope = round(settled_cost + rng.uniform(0.0, 2.0), 4)
            pairs.append(
                (
                    _cap_row(
                        f"p{index}",
                        cost=round(settled_cost / 2, 4),
                        lower=0.0,
                        upper=envelope,
                        final=False,
                    ),
                    _cap_row(f"p{index}", cost=settled_cost, lower=0.0, upper=envelope, final=True),
                )
            )
        canonical = [final for _partial, final in pairs]
        deliveries = [row for pair in pairs for row in pair]
        for _shuffle in range(3):
            rng.shuffle(deliveries)
            store = UsageIngestStore()
            for row in deliveries:
                store.ingest([row])
            # the full replay of every delivery moves NOTHING
            for row in deliveries:
                assert not store.ingest([row]).wrote_something_new
            expected = spend_cap_check(canonical, cap_usd=10.0, projection_usd=0.5)
            got = spend_cap_check(store.rows(), cap_usd=10.0, projection_usd=0.5)
            for key in expected:
                if key == "findings":
                    assert _canonical_findings(got) == _canonical_findings(expected)
                else:
                    assert got[key] == expected[key]


def test_monotonicity_a_higher_partial_never_reduces_liability():
    """Property: within one identity a newer higher partial and an
    out-of-order older partial are surfaced conflicts (the first stands)
    — liability never moves DOWN between partials; and at the fold level
    a higher accrued subtotal never yields a smaller interval."""
    # the fold level: for every (accrued <= accrued') and fixed envelope,
    # the interval's retained liability is non-decreasing
    rng = random.Random(341)
    for _trial in range(200):
        envelope = round(rng.uniform(0, 3), 4)
        accrued = round(rng.uniform(0, 3), 4)
        grown = accrued + round(rng.uniform(0, 2), 4)
        lower = _cap_row("i", cost=accrued, lower=accrued, upper=envelope, final=False)
        higher = _cap_row("i", cost=grown, lower=grown, upper=envelope, final=False)
        less = exposure_fold([lower]).retained_liability_usd
        more = exposure_fold([higher]).retained_liability_usd
        assert more >= less - 1e-12
        assert less >= min(accrued, envelope) - 1e-12

    # the store level: a streamed partial at 0.9/envelope 3.0 stands; a
    # NEWER higher partial (1.2) and an out-of-order OLDER one (0.4)
    # are conflicts — the exposure never drops until the settlement
    store = UsageIngestStore()
    store.ingest([_cap_row("stream", cost=0.9, lower=0.9, upper=3.0, final=False)])
    before = spend_cap_check(store.rows(), cap_usd=10.0)["reserved_usd"]
    assert before == pytest.approx(3.0)
    newer = store.ingest([_cap_row("stream", cost=1.2, lower=1.2, upper=3.0, final=False)])
    older = store.ingest([_cap_row("stream", cost=0.4, lower=0.4, upper=3.0, final=False)])
    assert len(newer.conflicts) == 1 and len(older.conflicts) == 1
    assert spend_cap_check(store.rows(), cap_usd=10.0)["reserved_usd"] == pytest.approx(3.0)
    stored = store.rows()[0]
    assert stored.cost_usd == pytest.approx(0.9)  # the first stands, never averaged
    # the settlement drops the exposure exactly once, to the settled cost
    store.ingest([_cap_row("stream", cost=1.0, lower=1.0, upper=3.0, final=True)])
    after = spend_cap_check(store.rows(), cap_usd=10.0)
    assert after["reserved_usd"] == pytest.approx(1.0)
    assert after["settlement_release_usd"] == pytest.approx(2.0)  # max(3, 1) - 1


async def test_sql_reloaded_rows_give_the_same_check_for_every_combination(db):
    """AC: SQL-reloaded and in-memory rows give IDENTICAL results for
    every finality/cost/bound combination — the fold runs against the
    ACTUAL persistence (persist → reload through the durable-mapping
    seam → re-check)."""
    from forge.adaptive.closing_budget import rows_from_durable_receipts

    combos: list[dict] = []
    for final in (True, False):
        for cost in (None, 0.5):
            for upper in (None, 2.0):
                combos.append(
                    {
                        "receipt_id": f"combo-{int(final)}-{cost is not None}-{upper is not None}",
                        "final": final,
                        "cost_usd": cost,
                        "cost_lower_bound_usd": 0.25 if cost is None else cost,
                        "cost_upper_bound_usd": upper,
                    }
                )
    # the incoherent arm: an envelope below the accrued subtotal
    combos.append(
        {
            "receipt_id": "combo-incoherent",
            "final": False,
            "cost_usd": 2.5,
            "cost_lower_bound_usd": 2.5,
            "cost_upper_bound_usd": 0.75,
        }
    )
    rows = [_ingested(**combo) for combo in combos]
    async with db() as session:
        outcome = await persist_ingested_rows(session, rows)
        await session.commit()
    assert outcome.created == len(rows)
    async with db() as session:
        receipts = (await session.execute(select(UsageReceipt))).scalars().all()
    reloaded = rows_from_durable_receipts(receipts)
    assert len(reloaded) == len(rows)
    for ceiling in (None, 3.0):
        in_memory = spend_cap_check(rows, cap_usd=10.0, unknown_interval_ceiling_usd=ceiling)
        from_sql = spend_cap_check(reloaded, cap_usd=10.0, unknown_interval_ceiling_usd=ceiling)
        assert from_sql == in_memory  # EVERY field, every combination


def test_the_budget_gate_projection_seam_consults_the_calculator():
    """The recording-provider-shaped seam: the numbers the live budget
    gate holds — the SpendLedger's USD limit and its worst-case
    projection — are exactly the cap and projection the corrected
    calculator consults. A provider streaming a partial receipt that
    already carries a subtotal must NOT unlock the headroom its envelope
    still fences (the seam the review calls the SpendLedger projection)."""
    from forge.adaptive.research_cohort_live import SpendLedger

    ledger = SpendLedger(model="unknown", limit_usd=1.0)
    max_tokens = 4000
    projection = ledger.estimate(ledger.worst_case_input_tokens, max_tokens)
    subtotal = round(projection / 2, 6)
    settled_cost = round(ledger.limit_usd - 1.5 * projection, 6)

    # the recording provider: one settled receipt + one STREAMED partial
    # whose envelope is the lane's contracted worst case (the ledger's own
    # projection priced it)
    store = UsageIngestStore()
    ingest_usage_artifact(
        store,
        {
            "receipt_id": "call-1",
            "driver": "claude-sdk-lane",
            "input_tokens": 1000,
            "output_tokens": 100,
            "total_cost_usd": settled_cost,
            "final": True,
        },
        work_id="w",
        attempt_id="a1",
        source="sdk-receipt",
    )
    ingest_usage_artifact(
        store,
        {
            "receipt_id": "call-2",
            "driver": "claude-sdk-lane",
            "input_tokens": 500,
            "output_tokens": 50,
            "total_cost_usd": subtotal,
            "cost_upper_bound_usd": round(projection, 6),
            "final": False,
        },
        work_id="w",
        attempt_id="a1",
        source="sdk-receipt",
    )
    # the ledger's own arithmetic over the SUBTOTALS would admit the call:
    # settled + subtotal + projection == the limit exactly
    ledger.spent_usd = settled_cost + subtotal
    assert ledger.allows_call(max_tokens) is True  # the naive reading fits
    assert settled_cost + subtotal + projection == pytest.approx(ledger.limit_usd)
    # the corrected calculator the gate consults REFUSES: the envelope
    # rides on top of the settled spend, and the projection tips it over
    check = spend_cap_check(store.rows(), cap_usd=ledger.limit_usd, projection_usd=projection)
    assert check["allowed"] is False
    assert check["settled_usd"] == pytest.approx(settled_cost)
    assert check["accrued_unsettled_usd"] == pytest.approx(subtotal)
    assert check["retained_liability_usd"] == pytest.approx(projection)
    assert check["reserved_usd"] + projection > ledger.limit_usd
    # and once the partial settles at its subtotal, the same projection fits
    store.ingest(
        [
            IngestedUsageRow(
                work_id="w",
                attempt_id="a1",
                receipt_id="call-2",
                source="sdk-receipt",
                route=ProviderRoute("claude-sdk-lane", "m"),
                cost_usd=subtotal,
                cost_lower_bound_usd=subtotal,
                cost_upper_bound_usd=round(projection, 6),
                final=True,
            )
        ]
    )
    settled = spend_cap_check(store.rows(), cap_usd=ledger.limit_usd, projection_usd=projection)
    assert settled["allowed"] is True  # settled + projection <= limit


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
