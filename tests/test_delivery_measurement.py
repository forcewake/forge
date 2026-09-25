"""R36-17 — the identity-linked delivery measurement path, checked.

The review's demands, pinned as tests (issue #276 / backlog R36-17 /
trace AT-12):

- JOIN CHAIN: records link by work id → execution attempt id →
  model-call/receipt identity → provider route, and the ledger keeps
  every attempt — accepted, rejected, cancelled, superseded, abandoned —
  with its receipts, spans and known/unknown usage.
- HONEST UNKNOWNS: an unknown EARLIER receipt stays unknown after a
  later known one; totals are exact or unknown with a lower bound and a
  coverage fraction; cache reads are never double-counted in inclusive
  input; reasoning counters carry their provider conventions.
- POPULATION SEPARATION: latency spans are typed and origin-scoped, every
  aggregate names its population, and output ÷ total-request latency can
  never be labeled decode throughput (the rate-label guard).
- PROGRAMME VS ACCEPTED-UNIT: rejected spend stays in the programme
  denominator; the accepted unit is priced by its OWN attempt chain.
- ORDER INVARIANCE: shuffled inputs produce a byte-identical ledger
  document and report.
- NEGATIVE FIXTURES: duplicate receipts, duplicated call ids across
  attempts, counter resets, absent vendor telemetry, zero/one-task
  reports — no fabricated rates, no silent averaging.
- AT-12: the trace walks a task's cost total to every included receipt
  and every excluded/unknown record; the replay reproduces the
  aggregation byte-identically from the stored document.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from forge.adaptive.delivery_measurement import (
    LATENCY_SPAN_TYPES,
    MEASUREMENT_SCHEMA,
    RATE_LABEL_SPACE,
    DeliveryLedger,
    MeasurementLinker,
    RateLabelError,
    attempt_records_from_evidence,
    billing_comparison,
    build_report,
    ledger_records_from_delivery_metrics,
    ledger_records_from_ingested_usage,
    measured_rate,
    replay_report,
    trace_accepted_task,
)
from forge.adaptive.delivery_metrics import reconcile_delivery

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "docs" / "evaluation" / "2026-09-23-delivery-measurement"


def _receipt(
    attempt_id: str,
    *,
    work_id: str = "w-acc",
    receipt_id: str | None = None,
    source: str = "usage",
    driver: str = "grok-build",
    model: str = "gpt-5",
    cost_usd: float | None = 0.5,
    completeness: str = "exact",
    **counters: int,
) -> dict:
    record: dict = {
        "work_id": work_id,
        "attempt_id": attempt_id,
        "source": source,
        "driver": driver,
        "model": model,
        "completeness": completeness,
    }
    if receipt_id is not None:
        record["receipt_id"] = receipt_id
    if cost_usd is not None:
        record["total_cost_usd"] = cost_usd
    record.update(counters)
    return record


def _call(
    call_id: str,
    *,
    work_id: str = "w-acc",
    attempt_id: str | None = None,
    provider: str = "zai",
    duration_ms: int = 1_500,
    input_tokens: int = 800,
    output_tokens: int = 120,
) -> dict:
    return {
        "id": call_id,
        "flow_run_id": work_id,
        "attempt_id": attempt_id,
        "provider": provider,
        "model": "glm-5",
        "duration_ms": duration_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _span(
    attempt_id: str,
    span_type: str,
    seconds: float | None = 30.0,
    *,
    work_id: str = "w-acc",
    origin: str | None = None,
) -> dict:
    record: dict = {
        "work_id": work_id,
        "attempt_id": attempt_id,
        "span_type": span_type,
        "seconds": seconds,
    }
    if origin is not None:
        record["origin"] = origin
    return record


def _link(**kwargs) -> DeliveryLedger:
    return MeasurementLinker().link(**kwargs)


def _canonical(document: dict) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


# ----------------------------------------------------------------------
# The join chain
# ----------------------------------------------------------------------


class TestJoinChain:
    def test_work_attempt_receipt_call_and_route_join(self):
        """The happy path: one accepted work, two attempts, receipts with
        token counters, an llm_calls row riding the second attempt, typed
        spans — everything lands on the identity it belongs to."""
        ledger = _link(
            works=[
                {
                    "work_id": "w-acc",
                    "outcome": "accepted",
                    "model_version": "glm-5.3",
                    "harness_version": "forge-v0.36.0",
                    "acceptance_contract": "forge.delivery-cohort/1",
                }
            ],
            attempts=[
                {"work_id": "w-acc", "attempt_id": "w-acc:1", "outcome": "superseded"},
                {"work_id": "w-acc", "attempt_id": "w-acc:2", "outcome": "accepted"},
            ],
            receipts=[
                _receipt("w-acc:1", receipt_id="r-1", cost_usd=0.40),
                _receipt(
                    "w-acc:2",
                    receipt_id="r-2",
                    cost_usd=1.10,
                    input_tokens=1000,
                    cached_input_tokens=600,
                    output_tokens=200,
                    reasoning_tokens=50,
                ),
            ],
            calls=[_call("c-1", attempt_id="w-acc:2")],
            spans=[
                _span("w-acc:1", "queue", 45.0),
                _span("w-acc:2", "model", 60.0, origin="planner"),
                _span("w-acc:2", "tool", 12.0),
                _span("w-acc:2", "human_wait", 600.0),
            ],
        )

        assert ledger.schema == MEASUREMENT_SCHEMA == "forge.delivery.measurement/1"
        work = ledger.work_by_id("w-acc")
        assert work is not None
        assert work.outcome == "accepted"
        assert work.model_version == "glm-5.3"
        assert work.harness_version == "forge-v0.36.0"
        assert work.acceptance_contract == "forge.delivery-cohort/1"
        assert [attempt.attempt_id for attempt in work.attempts] == ["w-acc:1", "w-acc:2"]
        assert [attempt.outcome for attempt in work.attempts] == ["superseded", "accepted"]

        first, second = work.attempts
        assert [row.receipt_id for row in first.receipts] == ["r-1"]
        assert [row.receipt_id for row in second.receipts] == ["r-2"]
        # The provider route — the join chain's leaf — is recorded per receipt.
        assert second.receipts[0].route.key == "grok-build/gpt-5"
        # The llm_calls row joined its attempt and became a model@harness span.
        assert second.call_ids == ("c-1",)
        assert ("model", "harness") in {(span.span_type, span.origin) for span in second.spans}
        assert ("model", "planner") in {(span.span_type, span.origin) for span in second.spans}

        report = build_report(ledger)
        assert report["cohort"] == {
            "works": 1,
            "by_outcome": {"accepted": 1},
            "accepted_outcomes": 1,
            "attempts": 2,
        }
        assert report["usage"]["exact_coverage"] == 1.0
        assert report["usage"]["exact_programme_cost_usd"] == pytest.approx(1.50)
        assert report["delivery"]["accepted_unit_costs_usd"] == {"w-acc": pytest.approx(1.50)}
        assert work.fully_linked

    def test_outcomes_that_never_earn_accepted_unit_status(self):
        """Rejected/cancelled/superseded/abandoned attempts stay in the
        ledger with their outcome typed — never silently dropped."""
        ledger = _link(
            works=[{"work_id": "w-x", "outcome": "cancelled"}],
            attempts=[
                {"work_id": "w-x", "attempt_id": "a1", "outcome": "abandoned"},
                {"work_id": "w-x", "attempt_id": "a2", "outcome": "cancelled"},
            ],
        )
        work = ledger.work_by_id("w-x")
        assert work is not None
        assert [attempt.outcome for attempt in work.attempts] == ["abandoned", "cancelled"]

    def test_connected_path_from_delivery_metrics(self):
        """The connected measurement path: reconcile_delivery's honest
        output feeds the linker without re-reading anything."""
        metrics = reconcile_delivery(
            attempts=[
                {
                    "attempt_id": "run:1",
                    "usage": {"total_cost_usd": 0.40, "input_tokens": 900},
                    "episode": {"turn_s": 65.0},
                    "tool_call_count": 12,
                    "dispatched_at": "2026-09-23T10:00:00+00:00",
                    "started_at": "2026-09-23T10:01:30+00:00",
                    "finished_at": "2026-09-23T10:40:00+00:00",
                    "reviewed_at": "2026-09-23T11:40:00+00:00",
                }
            ],
            accepted=True,
            run_id="run-1",
        )
        records = ledger_records_from_delivery_metrics(metrics)
        ledger = _link(**records)
        report = build_report(ledger)

        work = ledger.work_by_id("run-1")
        assert work is not None and work.outcome == "accepted"
        assert report["usage"]["exact_programme_cost_usd"] == pytest.approx(0.40)
        populations = {
            (row["span_type"], row["origin"]): row["total_seconds"]
            for row in report["latency"]["populations"]
        }
        assert populations[("model", "planner")] == pytest.approx(65.0)
        assert populations[("queue", "harness")] == pytest.approx(90.0)
        assert populations[("human_wait", "operator")] == pytest.approx(3600.0)

    def test_evidence_extraction_reads_the_attempts_contract(self):
        evidence = {
            "attempts": [{"attempt_id": "run:1"}, "not-a-mapping"],
            "harness": {"dispatched_at": "2026-09-23T10:00:00+00:00"},
        }
        assert attempt_records_from_evidence(evidence) == [{"attempt_id": "run:1"}]
        assert attempt_records_from_evidence({"attempts": "nope"}) == []
        assert attempt_records_from_evidence({}) == []


# ----------------------------------------------------------------------
# Honest unknowns
# ----------------------------------------------------------------------


class TestHonestUnknowns:
    def test_an_unknown_earlier_receipt_stays_unknown_after_a_later_known_one(self):
        """THE ordering rule: attempt 1 has no receipt, attempt 2 does —
        the total is UNKNOWN with attempt 2's cost as the lower bound; the
        later known receipt never heals the earlier gap into a complete
        total."""
        ledger = _link(
            works=[{"work_id": "w-acc", "outcome": "accepted"}],
            attempts=[
                {"work_id": "w-acc", "attempt_id": "w-acc:1"},
                {"work_id": "w-acc", "attempt_id": "w-acc:2"},
            ],
            receipts=[_receipt("w-acc:2", receipt_id="r-2", cost_usd=1.10)],
        )
        report = build_report(ledger)
        assert report["usage"]["exact_programme_cost_usd"] is None
        assert report["usage"]["known_cost_lower_bound_usd"] == pytest.approx(1.10)
        assert report["usage"]["exact_coverage"] == 0.5
        work = ledger.work_by_id("w-acc")
        assert work is not None
        assert work.own_usage.coverage == 0.5
        assert any("never zero" in note for note in work.attempts[0].usage.notes)

    def test_coverage_and_lower_bound_math(self):
        ledger = _link(
            works=[{"work_id": "w-acc", "outcome": "accepted"}],
            attempts=[{"work_id": "w-acc", "attempt_id": f"w-acc:{n}"} for n in (1, 2, 3, 4)],
            receipts=[
                _receipt("w-acc:1", receipt_id="r-1", cost_usd=0.10),
                _receipt("w-acc:3", receipt_id="r-3", cost_usd=0.30),
                _receipt("w-acc:4", receipt_id="r-4", cost_usd=0.40),
            ],
        )
        report = build_report(ledger)
        assert report["usage"]["exact_coverage"] == pytest.approx(0.75)
        assert report["usage"]["receipt_coverage"] == pytest.approx(0.75)
        assert report["usage"]["unknown_receipt_attempts"] == 1
        assert report["usage"]["known_cost_lower_bound_usd"] == pytest.approx(0.80)
        assert report["usage"]["exact_programme_cost_usd"] is None

    def test_absent_vendor_telemetry_is_unknown_not_zero(self):
        ledger = _link(
            works=[{"work_id": "w-acc", "outcome": "accepted"}],
            attempts=[{"work_id": "w-acc", "attempt_id": "w-acc:1"}],
        )
        report = build_report(ledger)
        assert report["usage"]["exact_programme_cost_usd"] is None
        assert report["usage"]["known_cost_lower_bound_usd"] == 0.0
        assert report["usage"]["exact_coverage"] == 0.0
        work = ledger.work_by_id("w-acc")
        assert work is not None
        assert work.own_usage.cost_usd is None  # unknown — never a zero total

    def test_token_only_call_telemetry_is_a_lower_bound_not_a_total(self):
        """No receipt at all: llm_calls rows keep a known lower bound and
        say so — they never promote themselves to an exact total."""
        ledger = _link(
            works=[{"work_id": "w-acc", "outcome": "accepted"}],
            attempts=[{"work_id": "w-acc", "attempt_id": "w-acc:1"}],
            calls=[_call("c-1", attempt_id="w-acc:1"), _call("c-2", attempt_id="w-acc:1")],
        )
        work = ledger.work_by_id("w-acc")
        assert work is not None
        usage = work.attempts[0].usage
        assert usage.call_derived
        assert usage.input_tokens_inclusive_lower_bound == 1600
        assert usage.output_tokens_lower_bound == 240
        assert usage.cost_usd is None
        assert any("lower bound" in note for note in usage.notes)


class TestCacheAndConventionRules:
    def test_cache_reads_are_never_double_counted_in_inclusive_input(self):
        """OpenAI shape: cached rides INSIDE input (1000 stays 1000).
        Anthropic shape: disjoint counters sum (400+300+100 = 800)."""
        openai_shaped = _receipt(
            "a1",
            receipt_id="r-openai",
            input_tokens=1000,
            cached_input_tokens=600,
            output_tokens=200,
        )
        anthropic_shaped = _receipt(
            "a2",
            work_id="w-ant",
            receipt_id="r-anthropic",
            driver="claude-code",
            input_tokens=400,
            cached_input_tokens=300,
            cache_write_tokens=100,
            output_tokens=200,
        )
        ledger = _link(
            works=[{"work_id": "w-acc"}, {"work_id": "w-ant"}],
            attempts=[
                {"work_id": "w-acc", "attempt_id": "a1"},
                {"work_id": "w-ant", "attempt_id": "a2"},
            ],
            receipts=[openai_shaped, anthropic_shaped],
        )
        acc = ledger.work_by_id("w-acc")
        ant = ledger.work_by_id("w-ant")
        assert acc is not None and ant is not None
        assert acc.own_usage.input_tokens_inclusive == 1000  # NOT 1600
        assert ant.own_usage.input_tokens_inclusive == 800
        # Reasoning is recorded as a breakdown inside output, never added.
        reasoning = _receipt(
            "a3",
            work_id="w-r",
            receipt_id="r-reasoning",
            input_tokens=100,
            output_tokens=200,
            reasoning_tokens=150,
        )
        ledger_r = _link(
            works=[{"work_id": "w-r"}],
            attempts=[{"work_id": "w-r", "attempt_id": "a3"}],
            receipts=[reasoning],
        )
        work_r = ledger_r.work_by_id("w-r")
        assert work_r is not None
        assert work_r.own_usage.output_tokens == 200  # NOT 350
        assert work_r.own_usage.reasoning_tokens == 150

    def test_the_report_carries_the_counter_conventions(self):
        report = build_report(_link(works=[{"work_id": "w-acc", "outcome": "accepted"}]))
        conventions = " ".join(report["usage"]["conventions"])
        assert "INSIDE input_tokens" in conventions
        assert "never added on top" in conventions
        assert "reasoning_tokens" in conventions


# ----------------------------------------------------------------------
# Population separation and the decode-label guard
# ----------------------------------------------------------------------


class TestLatencyPopulations:
    def test_all_six_span_types_stay_separate_with_named_populations(self):
        spans = [
            _span("a1", "model", 60.0, origin="planner"),
            _span("a1", "model", 25.0),  # harness origin by default
            _span("a1", "tool", 12.0),
            _span("a1", "queue", 45.0),
            _span("a1", "verification", 300.0),
            _span("a1", "human_wait", 600.0),
            _span("a1", "restore_collection", 8.0),
        ]
        ledger = _link(
            works=[{"work_id": "w-acc", "outcome": "accepted"}],
            attempts=[{"work_id": "w-acc", "attempt_id": "a1"}],
            spans=spans,
        )
        report = build_report(ledger)
        populations = report["latency"]["populations"]
        keys = [(row["span_type"], row["origin"]) for row in populations]
        assert ("model", "planner") in keys and ("model", "harness") in keys
        for span_type in LATENCY_SPAN_TYPES:
            assert span_type in {row["span_type"] for row in populations}
        # Every aggregate names its population; planner-only time and
        # harness-wide time never share a row.
        for row in populations:
            identity = row["population_identity"]
            assert identity.startswith("latency.")
            assert f"{row['span_type']}:{row['origin']}" in identity
        by_key = {(row["span_type"], row["origin"]): row for row in populations}
        assert by_key[("model", "planner")]["total_seconds"] == 60.0
        assert by_key[("model", "harness")]["total_seconds"] == 25.0
        assert report["latency"]["latency.population_identity"] == [
            row["population_identity"] for row in populations
        ]

    def test_an_unmeasurable_span_degrades_its_population_not_others(self):
        ledger = _link(
            works=[{"work_id": "w-acc", "outcome": "accepted"}],
            attempts=[{"work_id": "w-acc", "attempt_id": "a1"}],
            spans=[_span("a1", "model", None), _span("a1", "queue", 45.0)],
        )
        report = build_report(ledger)
        by_type = {row["span_type"]: row for row in report["latency"]["populations"]}
        assert by_type["model"]["total_seconds"] is None
        assert by_type["model"]["unknown_spans"] == 1
        assert by_type["model"]["total_seconds_lower_bound"] == 0.0
        assert by_type["queue"]["total_seconds"] == 45.0

    def test_the_label_space_forbids_mixed_population_decode_throughput(self):
        """THE guard (AT-12): output ÷ total-request latency can never be
        labeled decode throughput — neither by label nor by population."""
        assert set(RATE_LABEL_SPACE) == {"decode_output_tokens_per_second"}
        allowed = RATE_LABEL_SPACE["decode_output_tokens_per_second"]
        assert allowed == frozenset({"model:harness", "model:planner"})

        ok = measured_rate(
            "decode_output_tokens_per_second",
            tokens=200,
            seconds=60.0,
            population_identity="latency.model:planner:works=abc",
        )
        assert ok["tokens_per_second"] == pytest.approx(200 / 60)

        with pytest.raises(RateLabelError, match="never decode throughput"):
            measured_rate(
                "decode_output_tokens_per_second",
                tokens=200,
                seconds=3600.0,  # model + queue + tool + verification + human wait
                population_identity="latency.total_request:works=abc",
            )
        with pytest.raises(RateLabelError):
            measured_rate(
                "decode_output_tokens_per_second",
                tokens=200,
                seconds=90.0,
                population_identity="latency.model:harness+queue:harness:works=abc",
            )
        with pytest.raises(RateLabelError, match="outside the sanctioned"):
            measured_rate(
                "total_tokens_per_second",  # not in the space at all
                tokens=1200,
                seconds=60.0,
                population_identity="latency.model:harness:works=abc",
            )

    def test_span_records_without_timestamps_or_seconds_stay_unknown(self):
        ledger = _link(
            works=[{"work_id": "w-acc", "outcome": "accepted"}],
            attempts=[{"work_id": "w-acc", "attempt_id": "a1"}],
            spans=[
                {
                    "work_id": "w-acc",
                    "attempt_id": "a1",
                    "span_type": "verification",
                    "began_at": "2026-09-23T10:00:00Z",
                    "ended_at": "garbage",
                }
            ],
        )
        report = build_report(ledger)
        verification = next(
            row for row in report["latency"]["populations"] if row["span_type"] == "verification"
        )
        assert verification["total_seconds"] is None
        assert verification["unknown_spans"] == 1


# ----------------------------------------------------------------------
# Programme vs accepted-unit economics
# ----------------------------------------------------------------------


class TestProgrammeEconomics:
    def test_a_rejected_tasks_budget_stays_in_the_programme_denominator(self):
        """THE R36-17 rule: the rejected work's spend stays in programme
        cost AND in the per-accepted divisor; the accepted unit is priced
        by its own attempt chain only."""
        ledger = _link(
            works=[
                {"work_id": "w-acc", "outcome": "accepted"},
                {"work_id": "w-rej", "outcome": "rejected"},
            ],
            attempts=[
                {"work_id": "w-acc", "attempt_id": "w-acc:1"},
                {"work_id": "w-acc", "attempt_id": "w-acc:2"},
                {"work_id": "w-rej", "attempt_id": "w-rej:1"},
            ],
            receipts=[
                _receipt("w-acc:1", receipt_id="r-a1", cost_usd=0.40),
                _receipt("w-acc:2", receipt_id="r-a2", cost_usd=1.10),
                _receipt("w-rej:1", work_id="w-rej", receipt_id="r-r1", cost_usd=0.30),
            ],
        )
        report = build_report(ledger)
        delivery = report["delivery"]
        assert delivery["programme_cost_usd"] == pytest.approx(1.80)
        # ONE accepted outcome: the per-accepted figure carries the whole
        # programme, rejected spend included — not just the accepted unit.
        assert delivery["programme_cost_per_accepted_usd"] == pytest.approx(1.80)
        assert delivery["accepted_unit_costs_usd"] == {"w-acc": pytest.approx(1.50)}
        distribution = delivery["delivery.accepted_cost_distribution"]
        assert distribution is not None
        assert distribution["count"] == 1
        assert distribution["min"] == distribution["max"] == pytest.approx(1.50)
        assert any("stays in the programme" in note for note in delivery["notes"])

    def test_every_terminal_outcome_keeps_its_spend_in_programme_cost(self):
        for outcome in ("rejected", "cancelled", "superseded", "abandoned"):
            ledger = _link(
                works=[
                    {"work_id": "w-acc", "outcome": "accepted"},
                    {"work_id": "w-x", "outcome": outcome},
                ],
                attempts=[{"work_id": "w-x", "attempt_id": "x:1"}],
                receipts=[_receipt("x:1", work_id="w-x", receipt_id="r-x", cost_usd=0.25)],
            )
            report = build_report(ledger)
            assert report["usage"]["known_cost_lower_bound_usd"] == pytest.approx(0.25), outcome
            assert report["delivery"]["programme_cost_per_accepted_lower_bound_usd"] == (
                pytest.approx(0.25)
            ), outcome

    def test_zero_and_one_task_reports_never_fabricate_rates(self):
        empty = build_report(_link())
        assert empty["cohort"] == {
            "works": 0,
            "by_outcome": {},
            "accepted_outcomes": 0,
            "attempts": 0,
        }
        assert empty["usage"]["exact_coverage"] is None
        assert empty["usage"]["exact_programme_cost_usd"] is None
        assert empty["delivery"]["programme_cost_usd"] is None
        assert empty["delivery"]["programme_cost_per_accepted_usd"] is None
        assert empty["delivery"]["delivery.accepted_cost_distribution"] is None
        assert empty["capacity"] is None
        assert any("undefined, never zero" in note for note in empty["delivery"]["notes"])
        # No rate-like field exists anywhere in the empty report.
        assert "rates" not in empty and "throughput" not in empty

        one = build_report(
            _link(
                works=[{"work_id": "solo", "outcome": "accepted"}],
                attempts=[{"work_id": "solo", "attempt_id": "solo:1"}],
                receipts=[_receipt("solo:1", work_id="solo", receipt_id="r-solo", cost_usd=0.70)],
            )
        )
        distribution = one["delivery"]["delivery.accepted_cost_distribution"]
        assert distribution == {"count": 1, "min": 0.7, "median": 0.7, "max": 0.7}
        assert one["delivery"]["programme_cost_per_accepted_usd"] == pytest.approx(0.70)

    def test_capacity_appears_only_after_a_fully_linked_task(self):
        unlinked = _link(
            works=[{"work_id": "w-acc", "outcome": "accepted"}],
            attempts=[{"work_id": "w-acc", "attempt_id": "a1"}],
            spans=[_span("a1", "model", 60.0)],
        )
        assert build_report(unlinked)["capacity"] is None  # no receipt → not fully linked

        linked = _link(
            works=[{"work_id": "w-acc", "outcome": "accepted"}],
            attempts=[{"work_id": "w-acc", "attempt_id": "a1"}],
            receipts=[
                _receipt("a1", receipt_id="r-1", cost_usd=0.5, input_tokens=100, output_tokens=20)
            ],
            spans=[_span("a1", "model", 60.0, origin="planner"), _span("a1", "queue", 45.0)],
        )
        capacity = build_report(linked)["capacity"]
        assert capacity is not None
        assert capacity["bound_kind"] == "upper"
        assert capacity["model_service_seconds_per_task"] == pytest.approx(60.0)
        assert capacity["serial_upper_bound_tasks_per_day"] == pytest.approx(1440.0)
        assert "not a forecast" in capacity["note"]


# ----------------------------------------------------------------------
# Order invariance
# ----------------------------------------------------------------------


class TestOrderInvariance:
    def test_shuffled_inputs_produce_identical_ledger_and_report(self):
        works = [
            {"work_id": "w-acc", "outcome": "accepted"},
            {"work_id": "w-rej", "outcome": "rejected"},
        ]
        attempts = [
            {"work_id": "w-acc", "attempt_id": "w-acc:1"},
            {"work_id": "w-acc", "attempt_id": "w-acc:2"},
            {"work_id": "w-rej", "attempt_id": "w-rej:1"},
        ]
        receipts = [
            _receipt("w-acc:1", receipt_id="r-a1", cost_usd=0.40, input_tokens=900),
            _receipt("w-acc:2", receipt_id="r-a2", cost_usd=1.10, input_tokens=1200),
            _receipt("w-rej:1", work_id="w-rej", receipt_id="r-r1", cost_usd=0.30),
        ]
        calls = [
            _call("c-1", attempt_id="w-acc:1"),
            _call("c-2", work_id="w-rej"),  # a planner-side call
        ]
        spans = [
            _span("w-acc:1", "model", 60.0, origin="planner"),
            _span("w-acc:2", "model", 25.0),
            _span("w-acc:2", "queue", 45.0),
            _span("w-rej:1", "human_wait", 300.0),
        ]
        baseline_ledger = _link(
            works=works, attempts=attempts, receipts=receipts, calls=calls, spans=spans
        )
        baseline_document = _canonical(baseline_ledger.to_document())
        baseline_report = _canonical(build_report(baseline_ledger))

        rng = random.Random(20260923)
        for _round in range(10):
            shuffled_works, shuffled_attempts = list(works), list(attempts)
            shuffled_receipts, shuffled_calls, shuffled_spans = (
                list(receipts),
                list(calls),
                list(spans),
            )
            for group in (
                shuffled_works,
                shuffled_attempts,
                shuffled_receipts,
                shuffled_calls,
                shuffled_spans,
            ):
                rng.shuffle(group)
            ledger = _link(
                works=shuffled_works,
                attempts=shuffled_attempts,
                receipts=shuffled_receipts,
                calls=shuffled_calls,
                spans=shuffled_spans,
            )
            assert _canonical(ledger.to_document()) == baseline_document
            assert _canonical(build_report(ledger)) == baseline_report

    def test_reordered_attempt_replays_within_one_source_collapse_content_keyed(self):
        """A same-source cumulative replay collapses by CONTENT (the
        greatest cumulative claim), so delivery order cannot change the
        aggregate — the arrival-order trap the review warned about."""
        older = _receipt("a1", receipt_id="r-old", cost_usd=0.25, input_tokens=500)
        newer = _receipt("a1", receipt_id="r-new", cost_usd=0.60, input_tokens=1200)
        first = _link(
            works=[{"work_id": "w-acc", "outcome": "accepted"}],
            attempts=[{"work_id": "w-acc", "attempt_id": "a1"}],
            receipts=[older, newer],
        )
        second = _link(
            works=[{"work_id": "w-acc", "outcome": "accepted"}],
            attempts=[{"work_id": "w-acc", "attempt_id": "a1"}],
            receipts=[newer, older],
        )
        assert _canonical(build_report(first)) == _canonical(build_report(second))
        work = first.work_by_id("w-acc")
        assert work is not None
        assert work.own_usage.cost_usd == pytest.approx(0.60)  # the cumulative claim
        assert work.own_usage.input_tokens_inclusive == 1200  # never 500 + 1200


# ----------------------------------------------------------------------
# Negative fixtures: duplicates, resets, absent telemetry
# ----------------------------------------------------------------------


class TestDuplicatesAndConflicts:
    def test_repeated_receipt_delivery_collapses_by_identity(self):
        receipt = _receipt("a1", receipt_id="r-1", cost_usd=0.50, input_tokens=1000)
        ledger = _link(
            works=[{"work_id": "w-acc", "outcome": "accepted"}],
            attempts=[{"work_id": "w-acc", "attempt_id": "a1"}],
            receipts=[receipt, dict(receipt), dict(receipt)],
        )
        work = ledger.work_by_id("w-acc")
        assert work is not None
        assert work.own_usage.cost_usd == pytest.approx(0.50)  # counted ONCE
        assert work.own_usage.input_tokens_inclusive == 1000
        assert not ledger.conflicts

    def test_disagreeing_sources_conflict_and_are_never_averaged(self):
        ledger = _link(
            works=[{"work_id": "w-acc", "outcome": "accepted"}],
            attempts=[{"work_id": "w-acc", "attempt_id": "a1"}],
            receipts=[
                _receipt("a1", receipt_id="r-a", source="usage", cost_usd=0.50),
                _receipt("a1", receipt_id="r-b", source="billing-export", cost_usd=0.90),
            ],
        )
        work = ledger.work_by_id("w-acc")
        assert work is not None
        assert work.own_usage.cost_usd is None  # unknown, never (0.50+0.90)/2
        assert work.own_usage.known_cost_lower_bound_usd == pytest.approx(0.50)  # the min
        assert ledger.conflicts and ledger.conflicts[0].identity_kind == "receipt"
        assert ledger.conflicts[0].source_a == "billing-export"
        assert "cost_usd" in ledger.conflicts[0].fields

    def test_duplicated_call_ids_across_attempts_collapse_and_surface(self):
        """Same call id delivered under two attempts: counted once,
        attributed deterministically, and SURFACED — never counted twice
        and never averaged."""
        ledger = _link(
            works=[{"work_id": "w-acc", "outcome": "accepted"}],
            attempts=[
                {"work_id": "w-acc", "attempt_id": "a1"},
                {"work_id": "w-acc", "attempt_id": "a2"},
            ],
            receipts=[
                _receipt("a1", receipt_id="r-1", cost_usd=0.10),
                _receipt("a2", receipt_id="r-2", cost_usd=0.20),
            ],
            calls=[
                _call("c-dup", attempt_id="a1", input_tokens=800, output_tokens=120),
                _call("c-dup", attempt_id="a2", input_tokens=800, output_tokens=120),
            ],
        )
        assert ledger.duplicate_calls and ledger.duplicate_calls[0].call_id == "c-dup"
        assert set(ledger.duplicate_calls[0].attributions) == {"w-acc/a1", "w-acc/a2"}
        # The call joined exactly one attempt (a1, the deterministic pick).
        work = ledger.work_by_id("w-acc")
        assert work is not None
        with_call = [attempt for attempt in work.attempts if "c-dup" in attempt.call_ids]
        assert len(with_call) == 1

    def test_conflicting_call_rows_are_excluded_from_exact_totals(self):
        ledger = _link(
            works=[{"work_id": "w-acc", "outcome": "accepted"}],
            attempts=[{"work_id": "w-acc", "attempt_id": "a1"}],
            calls=[
                _call("c-x", attempt_id="a1", duration_ms=1_500, output_tokens=120),
                _call("c-x", attempt_id="a1", duration_ms=9_000, output_tokens=400),
            ],
        )
        assert any(
            row.identity == "c-x" and row.identity_kind == "call" for row in ledger.conflicts
        )
        work = ledger.work_by_id("w-acc")
        assert work is not None
        assert all("c-x" not in attempt.call_ids for attempt in work.attempts)

    def test_a_counter_reset_is_surfaced_and_degrades_the_exact_total(self):
        """Same source, cumulative counters that DECREASE on one field:
        a reset — not a negative delta, not silence."""
        ledger = _link(
            works=[{"work_id": "w-acc", "outcome": "accepted"}],
            attempts=[{"work_id": "w-acc", "attempt_id": "a1"}],
            receipts=[
                _receipt(
                    "a1",
                    receipt_id="r-before",
                    cost_usd=0.50,
                    input_tokens=1000,
                    output_tokens=200,
                ),
                _receipt(
                    "a1",
                    receipt_id="r-after",
                    cost_usd=0.30,
                    input_tokens=500,
                    output_tokens=300,
                ),
            ],
        )
        assert ledger.counter_resets
        assert any(reset.field == "output_tokens" for reset in ledger.counter_resets)
        work = ledger.work_by_id("w-acc")
        assert work is not None
        assert work.own_usage.cost_usd is None  # exact unknown after a reset
        assert work.own_usage.known_cost_lower_bound_usd == pytest.approx(0.50)
        assert "counter reset" in ledger.counter_resets[0].note

    def test_orphan_receipts_are_kept_as_lower_bounds_never_dropped(self):
        ledger = _link(
            works=[{"work_id": "w-acc", "outcome": "accepted"}],
            attempts=[{"work_id": "w-acc", "attempt_id": "a1"}],
            receipts=[
                _receipt("a1", receipt_id="r-1", cost_usd=0.40),
                _receipt("a9", receipt_id="r-orphan", cost_usd=0.15),
            ],
        )
        work = ledger.work_by_id("w-acc")
        assert work is not None
        assert work.orphan_receipt_ids == ("r-orphan",)
        assert work.own_usage.known_cost_lower_bound_usd == pytest.approx(0.55)
        assert work.own_usage.cost_usd is None  # the orphan blocks exactness


# ----------------------------------------------------------------------
# The AT-12 trace and the replay
# ----------------------------------------------------------------------


class TestTraceAndReplay:
    def _accepted_ledger(self, *, drop_one_receipt: bool = False) -> DeliveryLedger:
        receipts = [
            _receipt("w-acc:1", receipt_id="r-1", cost_usd=0.40, input_tokens=900),
            _receipt("w-acc:2", receipt_id="r-2", cost_usd=1.10, input_tokens=1200),
            _receipt(
                "w-rej:1",
                work_id="w-rej",
                receipt_id="r-rej",
                cost_usd=0.30,
                input_tokens=300,
            ),
        ]
        if drop_one_receipt:
            receipts = receipts[:1] + receipts[2:]  # r-2 removed
        return _link(
            works=[
                {"work_id": "w-acc", "outcome": "accepted"},
                {"work_id": "w-rej", "outcome": "rejected"},
            ],
            attempts=[
                {"work_id": "w-acc", "attempt_id": "w-acc:1"},
                {"work_id": "w-acc", "attempt_id": "w-acc:2"},
                {"work_id": "w-rej", "attempt_id": "w-rej:1"},
            ],
            receipts=receipts,
        )

    def test_the_trace_walks_the_total_down_to_every_receipt(self):
        trace = trace_accepted_task(self._accepted_ledger(), "w-acc")
        assert trace["schema"] == MEASUREMENT_SCHEMA
        assert trace["outcome"] == "accepted"
        assert trace["cost_total_usd"] == pytest.approx(1.50)
        assert trace["total_is_complete"] is True
        assert {row["receipt_id"] for row in trace["included_receipts"]} == {"r-1", "r-2"}
        # The included receipts SUM to the total — the trace is complete.
        assert trace["included_receipts_sum_usd"] == pytest.approx(1.50)
        assert trace["excluded_or_unknown"] == []

    def test_removing_one_receipt_moves_it_to_excluded_and_keeps_the_bound(self):
        """AT-12's replay scenario: remove one usage receipt and replay —
        the unknown remains visible and the lower bound is preserved."""
        ledger = self._accepted_ledger(drop_one_receipt=True)
        trace = trace_accepted_task(ledger, "w-acc")
        assert trace["cost_total_usd"] is None
        assert trace["total_is_complete"] is False
        assert trace["known_cost_lower_bound_usd"] == pytest.approx(0.40)
        excluded_records = " ".join(row["record"] for row in trace["excluded_or_unknown"])
        assert "w-acc:2" in excluded_records
        assert any("never zero" in row["reason"] for row in trace["excluded_or_unknown"])

    def test_a_rejected_task_traces_with_programme_inclusion(self):
        trace = trace_accepted_task(self._accepted_ledger(), "w-rej")
        assert trace["outcome"] == "rejected"
        assert trace["cost_total_usd"] == pytest.approx(0.30)
        assert "programme" in trace["programme_inclusion"]
        report = build_report(self._accepted_ledger())
        assert report["delivery"]["programme_cost_usd"] == pytest.approx(1.80)

    def test_trace_names_conflicts_and_duplicate_calls_as_excluded(self):
        ledger = _link(
            works=[{"work_id": "w-acc", "outcome": "accepted"}],
            attempts=[{"work_id": "w-acc", "attempt_id": "a1"}],
            receipts=[
                _receipt("a1", receipt_id="r-a", source="usage", cost_usd=0.50),
                _receipt("a1", receipt_id="r-b", source="billing", cost_usd=0.90),
            ],
        )
        trace = trace_accepted_task(ledger, "w-acc")
        reasons = " ".join(row["reason"] for row in trace["excluded_or_unknown"])
        assert "conflicting claims" in reasons
        assert "never averaged" in reasons

    def test_trace_of_an_unknown_work_is_a_typed_error(self):
        with pytest.raises(KeyError, match="w-nope"):
            trace_accepted_task(_link(), "w-nope")

    def test_replay_report_is_byte_identical_from_the_stored_document(self):
        ledger = self._accepted_ledger()
        document = ledger.to_document()
        assert document["schema"] == MEASUREMENT_SCHEMA
        replayed = replay_report(document)
        assert _canonical(replayed) == _canonical(build_report(ledger))
        # Round-tripping AGAIN through the rebuilt ledger changes nothing.
        rebuilt = DeliveryLedger.from_document(DeliveryLedger.from_document(document).to_document())
        assert _canonical(replay_report(rebuilt)) == _canonical(build_report(ledger))

    def test_replay_rejects_a_foreign_schema_stamp(self):
        with pytest.raises(ValueError, match="schema"):
            replay_report({"schema": "forge.delivery.measurement/0", "works": []})

    def test_billing_comparison_states_unresolvable_differences(self):
        ledger = self._accepted_ledger()
        report = build_report(ledger)
        reconciled = billing_comparison(
            report, [{"route": "grok-build/gpt-5", "billed_usd": 1.801}]
        )
        assert reconciled["rows"][0]["status"] == "reconciled"
        assert reconciled["unresolvable"] == 0

        incomplete = build_report(self._accepted_ledger(drop_one_receipt=True))
        gap = billing_comparison(incomplete, [{"route": "grok-build/gpt-5", "billed_usd": 1.80}])
        assert gap["rows"][0]["status"] == "unresolvable"
        assert "report side incomplete" in gap["rows"][0]["reason"]
        assert gap["unresolvable"] == 1

        mismatch = billing_comparison(
            report,
            [
                {"route": "grok-build/gpt-5", "billed_usd": 9.99},
                {"route": "zai/glm-5", "billed_usd": None},
            ],
        )
        assert all(row["status"] == "unresolvable" for row in mismatch["rows"])
        assert mismatch["unresolvable"] == 2


# ----------------------------------------------------------------------
# The committed example fixture (authored data, honestly labeled)
# ----------------------------------------------------------------------


class TestCommittedFixture:
    def test_the_fixture_ledger_replays_the_committed_report_byte_identically(self):
        document = json.loads((FIXTURE_DIR / "ledger.json").read_text(encoding="utf-8"))
        assert document["schema"] == MEASUREMENT_SCHEMA
        replayed = replay_report(document)
        committed = json.loads((FIXTURE_DIR / "report.json").read_text(encoding="utf-8"))
        assert _canonical(replayed) == _canonical(committed)

    def test_the_fixture_is_labeled_as_authored_example_data(self):
        readme = (FIXTURE_DIR / "README.md").read_text(encoding="utf-8")
        assert "fixture" in readme.lower()
        assert "authored" in readme.lower()
        assert "not" in readme.lower() and "measured" in readme.lower()


# ----------------------------------------------------------------------
# R38-09 — ingested usage rows as a receipt SOURCE beside the evidence
# ----------------------------------------------------------------------


def _ingested_row(work_id: str, attempt_id: str, **overrides) -> dict:
    row: dict = {
        "work_id": work_id,
        "attempt_id": attempt_id,
        "receipt_id": f"ing:{work_id}:{attempt_id}",
        "source": "lane/.forge/usage.json",
        "provider": "claude-sdk-lane",
        "model": "glm-5.3-flash",
        "counters": {
            "input_tokens": 19163,
            "cached_input_tokens": 119936,
            "output_tokens": 2463,
            "anthropic_shaped": True,
        },
        "cost_usd": 0.1498992,
        "cost_basis": "provider-reported",
        "segment": "segment:live1",
        "completeness": "aggregate",
        "final": True,
    }
    row.update(overrides)
    return row


class TestIngestedUsageJoin:
    def test_ingested_rows_join_beside_evidence_receipts(self):
        records = ledger_records_from_ingested_usage(
            [
                _ingested_row("w-acc", "w-acc/a2"),
                # the planner ledger row: work-level, its OWN population
                {
                    "work_id": "w-acc",
                    "attempt_id": "",
                    "receipt_id": "w-acc:planner:1",
                    "source": "planner/llm_calls",
                    "provider": "zai",
                    "counters": {"input_tokens": 900, "output_tokens": 90},
                    "completeness": "aggregate",
                },
            ],
            works=[{"work_id": "w-acc", "outcome": "accepted"}],
            attempts=[{"work_id": "w-acc", "attempt_id": "w-acc/a2"}],
        )
        ledger = MeasurementLinker().link(**records)
        work = ledger.work_by_id("w-acc")
        # the lane row landed as the ATTEMPT's receipt with its attribution
        attempt = work.attempts[0]
        claim = attempt.receipts[0]
        assert claim.source == "lane/.forge/usage.json"
        assert claim.cost_basis == "provider-reported"
        assert claim.segment == "segment:live1"
        assert claim.cost_usd == pytest.approx(0.1498992)
        # the disjoint (Anthropic-shaped) counters sum into the inclusive input
        assert claim.input_tokens_inclusive == 19163 + 119936
        # the planner row kept the planner population — never an attempt
        assert work.planner_call_ids == ("w-acc:planner:1",)
        assert all(row.attempt_id != "planner" for row in work.attempts)

    def test_ingested_join_is_order_invariant_and_replayable(self):
        rows = [
            _ingested_row("w-acc", "w-acc/a1", cost_usd=0.30),
            _ingested_row("w-acc", "w-acc/a2", cost_usd=0.70),
        ]
        works = [{"work_id": "w-acc", "outcome": "accepted"}]
        attempts = [
            {"work_id": "w-acc", "attempt_id": "w-acc/a1"},
            {"work_id": "w-acc", "attempt_id": "w-acc/a2"},
        ]
        first = MeasurementLinker().link(
            **ledger_records_from_ingested_usage(rows, works=works, attempts=attempts)
        )
        shuffled = MeasurementLinker().link(
            **ledger_records_from_ingested_usage(
                list(reversed(rows)), works=list(reversed(works)), attempts=list(reversed(attempts))
            )
        )
        assert _canonical(first.to_document()) == _canonical(shuffled.to_document())
        # the stored document replays byte-identically (cost_basis/segment
        # survive the round trip)
        replayed = replay_report(first.to_document())
        assert _canonical(replayed) == _canonical(build_report(first))
        doc_claim = first.to_document()["works"][0]["attempts"][0]["receipts"][0]
        assert doc_claim["cost_basis"] == "provider-reported"
        assert (
            DeliveryLedger.from_document(first.to_document())
            .works[0]
            .attempts[0]
            .receipts[0]
            .cost_basis
            == "provider-reported"
        )

    def test_a_streamed_partial_keeps_the_attempt_lower_bound_only(self):
        records = ledger_records_from_ingested_usage(
            [_ingested_row("w-acc", "w-acc/a1", completeness="partial")],
            works=[{"work_id": "w-acc", "outcome": ""}],
            attempts=[{"work_id": "w-acc", "attempt_id": "w-acc/a1"}],
        )
        ledger = MeasurementLinker().link(**records)
        usage = ledger.work_by_id("w-acc").attempts[0].usage
        assert usage.exact is False
        assert usage.cost_usd is None
        assert usage.known_cost_lower_bound_usd == pytest.approx(0.1498992)
