"""R37-13 — accepted-delivery economics over real identities, checked.

The review's demands, pinned as tests (issue #294 / backlog R37-13):

- JOIN CHAIN: ledger works join acceptance decisions by stable work id,
  attempts by attempt id, receipts by receipt identity — the acceptance
  record is the outcome authority, the ledger the spend authority, and
  an attempt the acceptance names but the ledger never saw stays in the
  coverage denominator.
- RETENTION: a failed/superseded attempt stays in the accepted item's
  OWN total and in the programme-per-accepted denominator; the lab's
  unknown costs render as known lower bound + coverage, never zero.
- EVIDENCE CLASSES: receipts carry ``live-model`` vs
  ``synthetic-vendor-counter``; a scripted counter inside a throughput
  comparison is REFUSED, and no decode label rides a request-latency
  population (the #276 rule extended).
- PRICE ASSUMPTIONS: the rate card is versioned; every priced entry
  labels billing vs estimate; the columns never mix.
- OPERATOR SURFACE: the summary is redacted (no prompt/tool content),
  built from the ledger's report — no second truth store; the budget
  reconciliation states discrepancies instead of absorbing them.
- ORDER INVARIANCE + IDENTITY: shuffled inputs produce a byte-identical
  document; duplicate receipts collapse by identity; two runs sharing a
  receipt label never cross-join.
- The committed ``evaluation/economics/lab-economics-v1.json`` report
  (the REAL lab-pilot-v1 ledgers) honours the same invariants.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from forge.adaptive.delivery_economics import (
    ECONOMICS_SCHEMA,
    EVIDENCE_LIVE_MODEL,
    EVIDENCE_SYNTHETIC_COUNTER,
    EconomicsLinker,
    EconomicsReport,
    EvidenceClassError,
    ModelRate,
    RateCard,
    assert_latency_guards,
    decode_throughput,
    operator_summary,
    reconcile_with_budget,
    redact_for_operator,
    throughput_comparison,
)
from forge.adaptive.delivery_measurement import (
    MEASUREMENT_SCHEMA,
    MeasurementLinker,
    RateLabelError,
    ledger_records_from_ingested_usage,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMITTED_REPORT = REPO_ROOT / "evaluation" / "economics" / "lab-economics-v1.json"

CARD = RateCard(
    version="card-v1",
    rates=[
        ModelRate(provider="zai", model="glm-5", input_per_mtok_usd=2.0, output_per_mtok_usd=8.0),
        ModelRate(
            provider="lab-lane/vendor-wire",
            model="",
            input_per_mtok_usd=2.5,
            output_per_mtok_usd=10.0,
        ),
    ],
)


def _ledger(**overrides):
    """A three-work cohort: accepted (2 attempts), accepted (1), rejected (1)."""
    records = {
        "works": [
            {"work_id": "w-acc", "outcome": "accepted"},
            {"work_id": "w-one", "outcome": "accepted"},
            {"work_id": "w-rej", "outcome": "rejected"},
        ],
        "attempts": [
            {"work_id": "w-acc", "attempt_id": "w-acc/a1"},
            {"work_id": "w-acc", "attempt_id": "w-acc/a2"},
            {"work_id": "w-one", "attempt_id": "w-one/a1"},
            {"work_id": "w-rej", "attempt_id": "w-rej/a1"},
        ],
        "receipts": [
            {
                "work_id": "w-acc",
                "attempt_id": "w-acc/a1",
                "receipt_id": "r-acc-1",
                "source": "usage",
                "provider": "zai",
                "model": "glm-5",
                "input_tokens": 800,
                "output_tokens": 120,
                "total_cost_usd": 0.40,
                "completeness": "exact",
            },
            {
                "work_id": "w-acc",
                "attempt_id": "w-acc/a2",
                "receipt_id": "r-acc-2",
                "source": "usage",
                "provider": "zai",
                "model": "glm-5",
                "input_tokens": 900,
                "output_tokens": 150,
                "total_cost_usd": 0.60,
                "completeness": "exact",
            },
            {
                "work_id": "w-one",
                "attempt_id": "w-one/a1",
                "receipt_id": "r-one-1",
                "source": "usage",
                "provider": "zai",
                "model": "glm-5",
                "input_tokens": 500,
                "output_tokens": 80,
                "total_cost_usd": 0.20,
                "completeness": "exact",
            },
            {
                "work_id": "w-rej",
                "attempt_id": "w-rej/a1",
                "receipt_id": "r-rej-1",
                "source": "usage",
                "provider": "zai",
                "model": "glm-5",
                "input_tokens": 600,
                "output_tokens": 90,
                "total_cost_usd": 0.30,
                "completeness": "exact",
            },
        ],
        "calls": [],
        "spans": [
            {
                "work_id": "w-acc",
                "attempt_id": "w-acc/a1",
                "span_type": "model",
                "origin": "harness",
                "seconds": 12.5,
            },
            {
                "work_id": "w-acc",
                "attempt_id": "w-acc/a2",
                "span_type": "model",
                "origin": "harness",
                "seconds": 7.5,
            },
            {
                "work_id": "w-rej",
                "attempt_id": "w-rej/a1",
                "span_type": "human_wait",
                "origin": "operator",
                "seconds": 240.0,
            },
        ],
    }
    records.update(overrides)
    return MeasurementLinker().link(**records)


def _acceptance():
    return [
        {
            "work_id": "w-acc",
            "accepted": True,
            "decided_by": "human:operator",
            "decided_at": "2026-09-24T00:00:00+00:00",
            "acceptance_contract": "verification:independent-check+diff-applies",
            "attempt_outcomes": {"w-acc/a1": "rejected", "w-acc/a2": "accepted"},
            "profile_version": "profile@gitlab-ce-v1@0.36.0",
            "harness_version": "lane-subprocess@1",
        },
        {
            "work_id": "w-one",
            "accepted": True,
            "decided_by": "human:operator",
            "attempt_outcomes": {"w-one/a1": "accepted"},
            "profile_version": "profile@gitlab-ce-v1@0.36.0",
            "harness_version": "lane-subprocess@1",
        },
        {
            "work_id": "w-rej",
            "accepted": False,
            "decided_by": "human:operator",
            "attempt_outcomes": {"w-rej/a1": "rejected"},
            "profile_version": "profile@gitlab-ce-v1@0.36.0",
            "harness_version": "lane-subprocess@1",
        },
    ]


def _receipt_rows():
    return [
        {
            "work_id": "w-acc",
            "attempt_id": "w-acc/a1",
            "receipt_id": "r-acc-1",
            "source": "usage",
            "provider": "zai",
            "model": "glm-5",
            "input_tokens": 800,
            "output_tokens": 120,
            "total_cost_usd": 0.40,
            "completeness": "exact",
        },
        {
            "work_id": "w-acc",
            "attempt_id": "w-acc/a2",
            "receipt_id": "r-acc-2",
            "source": "usage",
            "provider": "zai",
            "model": "glm-5",
            "input_tokens": 900,
            "output_tokens": 150,
            "total_cost_usd": 0.60,
            "completeness": "exact",
        },
        {
            "work_id": "w-one",
            "attempt_id": "w-one/a1",
            "receipt_id": "r-one-1",
            "source": "usage",
            "provider": "zai",
            "model": "glm-5",
            "input_tokens": 500,
            "output_tokens": 80,
            "total_cost_usd": 0.20,
            "completeness": "exact",
        },
        {
            "work_id": "w-rej",
            "attempt_id": "w-rej/a1",
            "receipt_id": "r-rej-1",
            "source": "usage",
            "provider": "zai",
            "model": "glm-5",
            "input_tokens": 600,
            "output_tokens": 90,
            "total_cost_usd": 0.30,
            "completeness": "exact",
        },
    ]


def _report(**linker_overrides):
    kwargs = {
        "ledger": _ledger(),
        "acceptance_records": _acceptance(),
        "rate_card": CARD,
        "pilot": {"pilot_id": "test-pilot", "profile_version": "profile@gitlab-ce-v1@0.36.0"},
    }
    kwargs.update(linker_overrides)
    return EconomicsLinker().link(**kwargs)


# ----------------------------------------------------------------------
# The join chain
# ----------------------------------------------------------------------


def test_join_chain_links_works_attempts_receipts_and_acceptance_by_stable_ids():
    report = _report()
    document = report.to_document()
    works = {work["work_id"]: work for work in document["works"]}
    # work → acceptance decision
    assert works["w-acc"]["accepted"] is True
    assert works["w-acc"]["decided_by"] == "human:operator"
    assert works["w-rej"]["accepted"] is False
    # work → attempts (every attempt kept, joined to its outcome)
    acc_attempts = {a["attempt_id"]: a for a in works["w-acc"]["attempts"]}
    assert set(acc_attempts) == {"w-acc/a1", "w-acc/a2"}
    assert acc_attempts["w-acc/a1"]["outcome"] == "rejected"
    assert acc_attempts["w-acc/a2"]["outcome"] == "accepted"
    assert acc_attempts["w-acc/a1"]["joined_to_acceptance"] is True
    # attempt → receipt (identity preserved end to end)
    receipts = acc_attempts["w-acc/a1"]["receipts"]
    assert [entry["receipt_id"] for entry in receipts] == ["r-acc-1"]
    assert receipts[0]["billed_usd"] == 0.40
    assert receipts[0]["basis"] == "billing"
    # the acceptance contract and exact profile versions ride the work
    assert works["w-acc"]["acceptance_contract"].startswith("verification:")
    assert works["w-acc"]["profile_version"] == "profile@gitlab-ce-v1@0.36.0"
    assert document["schema"] == ECONOMICS_SCHEMA


def test_acceptance_record_is_the_outcome_authority_on_conflict():
    ledger = _ledger(
        works=[
            {"work_id": "w-acc", "outcome": "accepted"},
            {"work_id": "w-one", "outcome": "accepted"},
            {"work_id": "w-rej", "outcome": "accepted"},  # the ledger is wrong
        ]
    )
    report = EconomicsLinker().link(ledger, acceptance_records=_acceptance(), rate_card=CARD)
    document = report.to_document()
    works = {work["work_id"]: work for work in document["works"]}
    assert works["w-rej"]["outcome"] == "rejected"
    assert works["w-rej"]["ledger_outcome"] == "accepted"
    conflicts = [row for row in document["conflicts"] if row["kind"] == "outcome"]
    assert len(conflicts) == 1 and conflicts[0]["identity"] == "w-rej"


def test_unobserved_attempt_stays_in_the_coverage_denominator():
    acceptance = _acceptance()
    acceptance[1]["attempt_outcomes"]["w-one/a9"] = "rejected"  # ledger never saw it
    report = EconomicsLinker().link(_ledger(), acceptance_records=acceptance, rate_card=CARD)
    document = report.to_document()
    works = {work["work_id"]: work for work in document["works"]}
    assert works["w-one"]["unobserved_attempt_ids"] == ["w-one/a9"]
    programme = document["costs"]["programme"]
    assert programme["unobserved_attempts"] == 1
    coverage = document["costs"]["coverage"]["programme"]
    assert coverage["receipts_expected"] == 5  # 4 observed + 1 unobserved
    assert coverage["receipts_received"] == 4
    assert coverage["receipt_coverage"] == pytest.approx(4 / 5)
    assert programme["billed_usd"] is None  # a gap is never an exact total
    assert programme["billed_known_lower_bound_usd"] == pytest.approx(1.5)


def test_acceptance_record_without_a_ledger_work_is_noted_not_joined():
    acceptance = [
        *_acceptance(),
        {"work_id": "w-ghost", "accepted": True, "decided_by": "human:operator"},
    ]
    report = EconomicsLinker().link(_ledger(), acceptance_records=acceptance, rate_card=CARD)
    document = report.to_document()
    assert report.work_by_id("w-ghost") is None
    assert any("w-ghost" in note for note in document["notes"])


# ----------------------------------------------------------------------
# Retention and totals
# ----------------------------------------------------------------------


def test_failed_attempt_stays_in_the_accepted_items_own_total():
    document = _report().to_document()
    item = document["costs"]["accepted_item_totals"]["w-acc"]
    # BOTH attempts priced — the failed first attempt is not dropped
    assert item["attempts"] == 2
    assert item["failed_or_superseded_attempts_kept"] == 1
    assert item["billed_usd"] == pytest.approx(1.0)  # 0.40 + 0.60
    assert item["receipt_ids"] == ["r-acc-1", "r-acc-2"]
    assert document["observability"]["cost.accepted_item_total"]["w-acc"] == pytest.approx(1.0)


def test_programme_per_accepted_item_includes_rejected_spend():
    document = _report().to_document()
    programme = document["costs"]["programme"]
    assert programme["billed_usd"] == pytest.approx(1.5)  # 1.0 + 0.2 + 0.3 (rej kept)
    per = document["costs"]["programme_per_accepted_item"]
    assert per["programme_billed_per_accepted_usd"] == pytest.approx(1.5 / 2)
    assert document["observability"]["cost.programme_per_accepted_item"] == pytest.approx(0.75)


def test_no_accepted_items_leaves_per_undefined_never_zero():
    acceptance = [
        {
            "work_id": "w-acc",
            "accepted": False,
            "decided_by": "human:operator",
            "attempt_outcomes": {"w-acc/a1": "rejected", "w-acc/a2": "rejected"},
        },
        {
            "work_id": "w-one",
            "accepted": False,
            "decided_by": "human:operator",
            "attempt_outcomes": {"w-one/a1": "rejected"},
        },
        {
            "work_id": "w-rej",
            "accepted": False,
            "decided_by": "human:operator",
            "attempt_outcomes": {"w-rej/a1": "rejected"},
        },
    ]
    document = (
        EconomicsLinker()
        .link(_ledger(), acceptance_records=acceptance, rate_card=CARD)
        .to_document()
    )
    per = document["costs"]["programme_per_accepted_item"]
    assert per["programme_billed_per_accepted_usd"] is None
    assert per["programme_billed_lower_bound_per_accepted_usd"] is None
    assert document["costs"]["accepted_item_totals"] == {}
    assert any("never zero" in note for note in document["notes"])


# ----------------------------------------------------------------------
# Honest unknowns: the lab shape (cost_state unknown)
# ----------------------------------------------------------------------


def _lab_ledger():
    return MeasurementLinker().link(
        works=[{"work_id": "t-01", "outcome": "accepted"}],
        attempts=[
            {"work_id": "t-01", "attempt_id": "t-01/a1"},
            {"work_id": "t-01", "attempt_id": "t-01/a2"},
        ],
        receipts=[
            {
                "work_id": "t-01",
                "attempt_id": "t-01/a1",
                "receipt_id": "lab-r1",
                "source": "lab-lane/vendor-wire",
                "provider": "lab-lane/vendor-wire",
                "input_tokens": 518,
                "output_tokens": 231,
            },
            {
                "work_id": "t-01",
                "attempt_id": "t-01/a2",
                "receipt_id": "lab-r2",
                "source": "lab-lane/vendor-wire",
                "provider": "lab-lane/vendor-wire",
                "input_tokens": 518,
                "output_tokens": 231,
            },
        ],
        spans=[
            {"work_id": "t-01", "attempt_id": "t-01/a1", "span_type": "model", "seconds": 0.832},
            {"work_id": "t-01", "attempt_id": "t-01/a2", "span_type": "model", "seconds": 0.849},
        ],
    )


def test_lab_unknown_costs_render_lower_bound_and_coverage_never_zero():
    report = EconomicsLinker().link(
        _lab_ledger(),
        acceptance_records=[
            {
                "work_id": "t-01",
                "accepted": True,
                "decided_by": "human:lab-operator",
                "attempt_outcomes": {"t-01/a1": "rejected", "t-01/a2": "accepted"},
                "profile_version": "forge-recipe/lab-offline/codex-app/scripted-vendor@4116f29b",
            }
        ],
        rate_card=CARD,
    )
    document = report.to_document()
    programme = document["costs"]["programme"]
    # the lab's costs are unknown by construction — exact stays None
    assert programme["billed_usd"] is None
    assert programme["billed_exact"] is False
    # coverage is measured over the SAME population the claims describe
    coverage = document["costs"]["coverage"]["programme"]
    assert coverage["receipts_expected"] == 2
    assert coverage["receipts_received"] == 2
    assert coverage["receipt_coverage"] == 1.0
    assert coverage["cost_known_receipts"] == 0
    assert coverage["unknown_cost_receipts"] == 2
    assert coverage["cost_coverage"] == 0.0
    assert coverage["unknown_costs_rendered_as"] == "known-lower-bound + coverage, never zero"
    # the accepted item keeps both attempts with an unknown, not zero, total
    item = document["costs"]["accepted_item_totals"]["t-01"]
    assert item["billed_usd"] is None
    assert item["attempts"] == 2
    assert item["failed_or_superseded_attempts_kept"] == 1
    # the estimate column exists, labelled, and never masquerades as spend
    entries = document["works"][0]["attempts"][0]["receipts"]
    assert entries[0]["basis"] == "estimate"
    assert entries[0]["evidence_class"] == EVIDENCE_SYNTHETIC_COUNTER
    assert entries[0]["rate_card_version"] == "card-v1"
    per_receipt_estimate = 518 / 1_000_000 * 2.5 + 231 / 1_000_000 * 10.0
    assert document["costs"]["programme"]["estimate_usd"] == pytest.approx(
        per_receipt_estimate * 2, rel=1e-9
    )


# ----------------------------------------------------------------------
# Evidence classes and the throughput guards
# ----------------------------------------------------------------------


def test_receipts_are_labelled_by_evidence_class():
    document = _report().to_document()
    classes = document["evidence"]["classes"]
    assert classes == {EVIDENCE_LIVE_MODEL: 4}
    lab_document = (
        EconomicsLinker()
        .link(
            _lab_ledger(),
            acceptance_records=[
                {"work_id": "t-01", "accepted": True, "decided_by": "human:lab-operator"}
            ],
        )
        .to_document()
    )
    assert lab_document["evidence"]["classes"] == {EVIDENCE_SYNTHETIC_COUNTER: 2}
    assert lab_document["evidence"]["synthetic_counters_labelled"] == 2
    assert "throughput comparisons" in lab_document["evidence"]["synthetic_excluded_from"]


def test_synthetic_counter_in_a_throughput_comparison_is_refused():
    with pytest.raises(EvidenceClassError, match="synthetic-vendor-counter"):
        throughput_comparison(
            label="decode_output_tokens_per_second",
            tokens=231,
            seconds=0.832,
            population_identity="latency.model:harness:works=t-01",
            evidence_classes=[EVIDENCE_SYNTHETIC_COUNTER],
        )
    # a mixed comparison (scripted trace beside real billing) is refused too
    with pytest.raises(EvidenceClassError, match="only live-model"):
        throughput_comparison(
            label="decode_output_tokens_per_second",
            tokens=231,
            seconds=0.832,
            population_identity="latency.model:harness:works=t-01",
            evidence_classes=[EVIDENCE_LIVE_MODEL, EVIDENCE_SYNTHETIC_COUNTER],
        )
    # unclassified evidence never sneaks in as live
    with pytest.raises(EvidenceClassError):
        decode_throughput(
            tokens=10,
            seconds=1.0,
            population_identity="latency.model:harness:works=x",
            evidence_classes=["unknown"],
        )


def test_decode_rate_over_live_model_evidence_passes_both_guards():
    row = decode_throughput(
        tokens=120,
        seconds=12.5,
        population_identity="latency.model:harness:works=w-acc",
        evidence_classes=[EVIDENCE_LIVE_MODEL],
    )
    assert row["label"] == "decode_output_tokens_per_second"
    assert row["tokens_per_second"] == pytest.approx(9.6)
    assert row["evidence_class"] == EVIDENCE_LIVE_MODEL


def test_no_decode_label_on_request_latency_populations():
    # output over a mixed request-latency population (model + queue + human wait)
    with pytest.raises(RateLabelError, match="never decode throughput"):
        decode_throughput(
            tokens=120,
            seconds=260.0,
            population_identity="latency.queue:harness:works=w-acc",
            evidence_classes=[EVIDENCE_LIVE_MODEL],
        )
    # an unsanctioned label is refused outright by the comparison surface
    with pytest.raises(RateLabelError, match="outside the sanctioned"):
        throughput_comparison(
            label="tokens_per_wall_second",
            tokens=120,
            seconds=260.0,
            population_identity="latency.model:harness:works=w-acc",
            evidence_classes=[EVIDENCE_LIVE_MODEL],
        )
    # and the document-level guard refuses tampered throughput rows
    document = _report().to_document()
    assert_latency_guards(document)  # clean document passes
    tampered = json.loads(json.dumps(document))
    tampered["latency"]["throughput"] = [
        {
            "label": "decode_output_tokens_per_second",
            "population_identity": "latency.human_wait:operator:works=w-rej",
            "evidence_class": EVIDENCE_LIVE_MODEL,
        }
    ]
    with pytest.raises(RateLabelError, match="no decode label"):
        assert_latency_guards(tampered)
    synthetic = json.loads(json.dumps(document))
    synthetic["latency"]["throughput"] = [
        {
            "label": "decode_output_tokens_per_second",
            "population_identity": "latency.model:harness:works=t-01",
            "evidence_class": EVIDENCE_SYNTHETIC_COUNTER,
        }
    ]
    with pytest.raises(EvidenceClassError, match="synthetic counters are excluded"):
        assert_latency_guards(synthetic)


def test_latency_stage_seconds_separate_populations_and_keep_unknowns_honest():
    document = _report().to_document()
    stages = document["latency"]["stage_seconds"]
    # measured stages carry exact totals over their one population
    assert stages["model"]["stage_total_seconds"] == pytest.approx(20.0)
    assert stages["model"]["populations"]["model:harness"]["total_seconds"] == pytest.approx(20.0)
    assert stages["human_wait"]["stage_total_seconds"] == pytest.approx(240.0)
    # unmeasured stages are named unknowns, never zero
    for stage in ("tool", "queue", "verification"):
        assert stages[stage]["measured"] is False
        assert stages[stage]["stage_total_seconds"] is None
        assert stages[stage]["stage_lower_bound_seconds"] is None
        assert "never zero" in stages[stage]["note"]
    assert set(stages) >= {"model", "tool", "queue", "verification", "human_wait"}
    assert document["observability"]["latency.stage_seconds"]["model"] == pytest.approx(20.0)
    # a stage spanning two origins withholds the total (populations never mix)
    mixed = MeasurementLinker().link(
        works=[{"work_id": "m", "outcome": "accepted"}],
        attempts=[{"work_id": "m", "attempt_id": "m/a1"}],
        spans=[
            {
                "work_id": "m",
                "attempt_id": "m/a1",
                "span_type": "model",
                "origin": "harness",
                "seconds": 10.0,
            },
            {
                "work_id": "m",
                "attempt_id": "m/a1",
                "span_type": "model",
                "origin": "planner",
                "seconds": 5.0,
            },
        ],
    )
    mixed_document = (
        EconomicsLinker()
        .link(
            mixed,
            acceptance_records=[{"work_id": "m", "accepted": True, "decided_by": "human:operator"}],
        )
        .to_document()
    )
    model_stage = mixed_document["latency"]["stage_seconds"]["model"]
    assert model_stage["stage_total_seconds"] is None
    assert model_stage["stage_lower_bound_seconds"] == pytest.approx(15.0)
    assert "never mix" in model_stage["note"]
    # an unknown window degrades its stage to a lower bound
    unknown_span = MeasurementLinker().link(
        works=[{"work_id": "u", "outcome": "accepted"}],
        attempts=[{"work_id": "u", "attempt_id": "u/a1"}],
        spans=[
            {"work_id": "u", "attempt_id": "u/a1", "span_type": "model", "seconds": 3.0},
            {"work_id": "u", "attempt_id": "u/a1", "span_type": "model", "seconds": None},
        ],
    )
    unknown_document = (
        EconomicsLinker()
        .link(
            unknown_span,
            acceptance_records=[{"work_id": "u", "accepted": True, "decided_by": "human:operator"}],
        )
        .to_document()
    )
    assert unknown_document["latency"]["stage_seconds"]["model"]["stage_total_seconds"] is None
    assert unknown_document["latency"]["stage_seconds"]["model"]["stage_lower_bound_seconds"] == 3.0


# ----------------------------------------------------------------------
# Versioned price assumptions
# ----------------------------------------------------------------------


def test_rate_card_versioning_and_billing_vs_estimate_labels():
    document = _report().to_document()
    assert document["rate_card"]["version"] == "card-v1"
    # billing-labelled entries are exactly the receipts that carry a figure
    entries = [
        entry
        for work in document["works"]
        for attempt in work["attempts"]
        for entry in attempt["receipts"]
    ]
    assert all(entry["basis"] == "billing" for entry in entries)
    assert all(entry["rate_card_version"] == "card-v1" for entry in entries)
    # a priced-from-the-card entry is an estimate, labelled with the version
    lab_report = EconomicsLinker().link(
        _lab_ledger(),
        acceptance_records=[
            {"work_id": "t-01", "accepted": True, "decided_by": "human:lab-operator"}
        ],
        rate_card=CARD,
    )
    lab_entries = [
        entry
        for work in lab_report.to_document()["works"]
        for attempt in work["attempts"]
        for entry in attempt["receipts"]
    ]
    assert all(entry["basis"] == "estimate" for entry in lab_entries)
    assert all(entry["billed_usd"] is None for entry in lab_entries)
    # changing the card version changes the estimate labels, never the billing
    card_v2 = RateCard(
        version="card-v2",
        rates=[
            ModelRate(
                provider="lab-lane/vendor-wire",
                model="",
                input_per_mtok_usd=5.0,
                output_per_mtok_usd=20.0,
            )
        ],
    )
    lab_acceptance = [{"work_id": "t-01", "accepted": True, "decided_by": "human:lab-operator"}]
    lab_v1_estimate = (
        EconomicsLinker()
        .link(_lab_ledger(), acceptance_records=lab_acceptance, rate_card=CARD)
        .to_document()["costs"]["programme"]["estimate_usd"]
    )
    lab_v2 = (
        EconomicsLinker()
        .link(_lab_ledger(), acceptance_records=lab_acceptance, rate_card=card_v2)
        .to_document()
    )
    assert lab_v2["costs"]["programme"]["estimate_rate_card_version"] == "card-v2"
    assert lab_v2["costs"]["programme"]["estimate_usd"] == pytest.approx(
        lab_v1_estimate * 2, rel=1e-9
    )
    assert lab_v2["costs"]["programme"]["billed_usd"] is None
    # a receipt with no card rate carries no estimate (never a zero price)
    card_empty = RateCard(version="card-empty", rates=[])
    no_rate = (
        EconomicsLinker()
        .link(
            _lab_ledger(),
            acceptance_records=[
                {"work_id": "t-01", "accepted": True, "decided_by": "human:lab-operator"}
            ],
            rate_card=card_empty,
        )
        .to_document()
    )
    assert no_rate["costs"]["programme"]["estimate_usd"] is None
    # the JSON round trip keeps the card honest
    parsed = RateCard.from_json(CARD.to_json())
    assert parsed.version == "card-v1"
    assert [rate.key for rate in parsed.rates] == ["zai/glm-5", "lab-lane/vendor-wire"]


def test_anthropic_disjoint_counters_price_without_double_counting():
    ledger = MeasurementLinker().link(
        works=[{"work_id": "w", "outcome": "accepted"}],
        attempts=[{"work_id": "w", "attempt_id": "w/a1"}],
        receipts=[
            {
                "work_id": "w",
                "attempt_id": "w/a1",
                "receipt_id": "r-claude",
                "source": "usage",
                "provider": "claude-code",
                "model": "claude-x",
                "input_tokens": 1_000_000,
                "cached_input_tokens": 1_000_000,
                "cache_write_tokens": 500_000,
                "output_tokens": 1_000_000,
            },
        ],
    )
    card = RateCard(
        version="card-claude",
        rates=[
            ModelRate(
                provider="claude-code",
                model="claude-x",
                input_per_mtok_usd=3.0,
                cached_input_per_mtok_usd=0.3,
                cache_write_per_mtok_usd=3.75,
                output_per_mtok_usd=15.0,
            )
        ],
    )
    report = EconomicsLinker().link(
        ledger,
        acceptance_records=[{"work_id": "w", "accepted": True, "decided_by": "human:operator"}],
        rate_card=card,
    )
    entry = report.to_document()["works"][0]["attempts"][0]["receipts"][0]
    # disjoint counters priced separately, then summed once
    assert entry["estimate_usd"] == pytest.approx(3.0 + 0.3 + 3.75 / 2 + 15.0)
    assert entry["input_tokens_inclusive"] == 2_500_000  # the inclusive convention


# ----------------------------------------------------------------------
# The operator surface
# ----------------------------------------------------------------------


def test_operator_summary_is_compact_redacted_and_ledger_derived():
    document = _report().to_document()
    summary = operator_summary(document)
    assert summary["schema"] == ECONOMICS_SCHEMA
    assert summary["join"]["works"] == 3
    assert summary["join"]["accepted_items"] == 2
    assert summary["costs"]["programme_billed_usd"] == pytest.approx(1.5)
    assert summary["costs"]["per_accepted_item"]["programme_billed_per_accepted_usd"] == (
        pytest.approx(0.75)
    )
    assert summary["costs"]["repeated_work_attempts"] == 1
    assert summary["latency_stage_seconds"]["model"]["total"] == pytest.approx(20.0)
    assert summary["evidence_classes"] == {EVIDENCE_LIVE_MODEL: 4}
    # no prompt/tool/task content anywhere in the summary
    flattened = json.dumps(summary)
    for marker in ("prompt", "brief", "snippet", "tool_payload", "SECRET"):
        assert marker not in flattened
    # task-level content (the acceptance contract's check names) never enters
    assert "verification:independent-check" not in flattened
    # the summary is derived from the report — not a second truth store
    assert operator_summary(_report()) == operator_summary(_report().to_document())


def test_redact_for_operator_drops_sensitive_keys_at_every_depth():
    noisy = {
        "prompt_text": "SECRET PROMPT",
        "tool_payload": {"content": "shell command"},
        "nested": {"diff": "...", "answer": 42, "count": 7},
        "items": [{"snippet": "code", "id": "r-1"}],
        "keep": 3,
    }
    redacted = redact_for_operator(noisy)
    assert redacted["prompt_text"] == "[redacted]"
    assert redacted["tool_payload"] == "[redacted]"
    assert redacted["nested"] == {"diff": "[redacted]", "answer": "[redacted]", "count": 7}
    assert redacted["items"] == [{"snippet": "[redacted]", "id": "r-1"}]
    assert redacted["keep"] == 3


def test_reconcile_with_budget_states_within_over_and_unreconcilable():
    document = _report().to_document()
    within = reconcile_with_budget(document, {"cap_usd": 2.0})
    billed_line = within["lines"][0]
    assert billed_line["scope"] == "programme.billed"
    assert billed_line["status"] == "within"
    assert billed_line["reported_usd"] == pytest.approx(1.5)
    assert billed_line["delta_usd"] == pytest.approx(0.5)
    over = reconcile_with_budget(document, {"cap_usd": 1.0})
    assert over["lines"][0]["status"] == "over"
    assert over["lines"][0]["reason"].startswith("exact billed total")
    # the lab's unknown spend can never certify a cap
    lab_report = EconomicsLinker().link(
        _lab_ledger(),
        acceptance_records=[
            {"work_id": "t-01", "accepted": True, "decided_by": "human:lab-operator"}
        ],
        rate_card=CARD,
    )
    lab_recon = reconcile_with_budget(lab_report.to_document(), {"cap_usd": 25.0})
    statuses = {line["scope"]: line["status"] for line in lab_recon["lines"]}
    assert statuses == {
        "programme.billed": "unreconcilable",
        "programme.lower_bound": "unreconcilable",
        "programme.estimate": "unreconcilable",
    }
    assert lab_recon["unreconcilable"] == 3
    billed_reason = lab_recon["lines"][0]["reason"]
    assert "never zero" in billed_reason
    estimate_reason = lab_recon["lines"][2]["reason"]
    assert "assumption, not spend" in estimate_reason
    # a capless budget is stated, not guessed
    capless = reconcile_with_budget(document, {"currency": "usd"})
    assert all(line["status"] == "unreconcilable" for line in capless["lines"])


# ----------------------------------------------------------------------
# Order invariance and identity
# ----------------------------------------------------------------------


def test_reordering_every_input_changes_no_byte():
    reference = _report().to_document()
    random.seed(294)
    for _ in range(5):
        acceptance = _acceptance()
        works = [
            {"work_id": "w-acc", "outcome": "accepted"},
            {"work_id": "w-one", "outcome": "accepted"},
            {"work_id": "w-rej", "outcome": "rejected"},
        ]
        attempts = [
            {"work_id": "w-acc", "attempt_id": "w-acc/a1"},
            {"work_id": "w-acc", "attempt_id": "w-acc/a2"},
            {"work_id": "w-one", "attempt_id": "w-one/a1"},
            {"work_id": "w-rej", "attempt_id": "w-rej/a1"},
        ]
        receipt_rows = _receipt_rows()
        spans = [
            {
                "work_id": "w-acc",
                "attempt_id": "w-acc/a1",
                "span_type": "model",
                "origin": "harness",
                "seconds": 12.5,
            },
            {
                "work_id": "w-acc",
                "attempt_id": "w-acc/a2",
                "span_type": "model",
                "origin": "harness",
                "seconds": 7.5,
            },
            {
                "work_id": "w-rej",
                "attempt_id": "w-rej/a1",
                "span_type": "human_wait",
                "origin": "operator",
                "seconds": 240.0,
            },
        ]
        random.shuffle(works)
        random.shuffle(attempts)
        random.shuffle(receipt_rows)
        random.shuffle(spans)
        random.shuffle(acceptance)
        shuffled = (
            EconomicsLinker()
            .link(
                MeasurementLinker().link(
                    works=works, attempts=attempts, receipts=receipt_rows, spans=spans
                ),
                acceptance_records=acceptance,
                rate_card=CARD,
                pilot={"pilot_id": "test-pilot", "profile_version": "profile@gitlab-ce-v1@0.36.0"},
            )
            .to_document()
        )
        assert json.dumps(shuffled, sort_keys=True) == json.dumps(reference, sort_keys=True)


def test_duplicate_receipt_delivery_collapses_by_identity():
    receipts = [
        {
            "work_id": "w-acc",
            "attempt_id": "w-acc/a1",
            "receipt_id": "r-acc-1",
            "source": "usage",
            "provider": "zai",
            "model": "glm-5",
            "input_tokens": 800,
            "output_tokens": 120,
            "total_cost_usd": 0.40,
            "completeness": "exact",
        },
        {
            "work_id": "w-acc",
            "attempt_id": "w-acc/a1",
            "receipt_id": "r-acc-1",
            "source": "usage",
            "provider": "zai",
            "model": "glm-5",
            "input_tokens": 800,
            "output_tokens": 120,
            "total_cost_usd": 0.40,
            "completeness": "exact",
        },
        {
            "work_id": "w-acc",
            "attempt_id": "w-acc/a2",
            "receipt_id": "r-acc-2",
            "source": "usage",
            "provider": "zai",
            "model": "glm-5",
            "input_tokens": 900,
            "output_tokens": 150,
            "total_cost_usd": 0.60,
            "completeness": "exact",
        },
    ]
    ledger = MeasurementLinker().link(
        works=[{"work_id": "w-acc", "outcome": "accepted"}],
        attempts=[
            {"work_id": "w-acc", "attempt_id": "w-acc/a1"},
            {"work_id": "w-acc", "attempt_id": "w-acc/a2"},
        ],
        receipts=receipts,
    )
    document = (
        EconomicsLinker()
        .link(
            ledger,
            acceptance_records=[
                {
                    "work_id": "w-acc",
                    "accepted": True,
                    "decided_by": "human:operator",
                    "attempt_outcomes": {"w-acc/a1": "rejected", "w-acc/a2": "accepted"},
                }
            ],
            rate_card=CARD,
        )
        .to_document()
    )
    item = document["costs"]["accepted_item_totals"]["w-acc"]
    assert item["billed_usd"] == pytest.approx(1.0)  # 0.40 counted once
    assert item["receipt_ids"] == ["r-acc-1", "r-acc-2"]
    assert document["costs"]["programme"]["receipts"] == 2


def test_two_runs_sharing_a_receipt_label_never_cross_join():
    shared_rows = [
        {
            "work_id": "run-a",
            "attempt_id": "run-a/a1",
            "receipt_id": "shared-r",
            "source": "usage",
            "provider": "zai",
            "model": "glm-5",
            "input_tokens": 800,
            "output_tokens": 120,
            "total_cost_usd": 0.40,
            "completeness": "exact",
        },
        {
            "work_id": "run-b",
            "attempt_id": "run-b/a1",
            "receipt_id": "shared-r",
            "source": "usage",
            "provider": "zai",
            "model": "glm-5",
            "input_tokens": 800,
            "output_tokens": 120,
            "total_cost_usd": 0.40,
            "completeness": "exact",
        },
    ]
    ledger = MeasurementLinker().link(
        works=[
            {"work_id": "run-a", "outcome": "accepted"},
            {"work_id": "run-b", "outcome": "accepted"},
        ],
        attempts=[
            {"work_id": "run-a", "attempt_id": "run-a/a1"},
            {"work_id": "run-b", "attempt_id": "run-b/a1"},
        ],
        receipts=shared_rows,
    )
    document = (
        EconomicsLinker()
        .link(
            ledger,
            acceptance_records=[
                {
                    "work_id": "run-a",
                    "accepted": True,
                    "decided_by": "human:operator",
                    "attempt_outcomes": {"run-a/a1": "accepted"},
                },
                {
                    "work_id": "run-b",
                    "accepted": True,
                    "decided_by": "human:operator",
                    "attempt_outcomes": {"run-b/a1": "accepted"},
                },
            ],
            rate_card=CARD,
        )
        .to_document()
    )
    # the cross-join is surfaced and refused
    assert len(document["cross_joins"]) == 1
    cross = document["cross_joins"][0]
    assert cross["receipt_id"] == "shared-r"
    assert cross["kept_attribution"] == "run-a/run-a/a1"
    assert set(cross["attributions"]) == {"run-a/run-a/a1", "run-b/run-b/a1"}
    # the spend is counted ONCE: kept for run-a, refused for run-b
    totals = document["costs"]["accepted_item_totals"]
    assert totals["run-a"]["billed_usd"] is None  # exactness degraded globally
    assert totals["run-a"]["billed_known_lower_bound_usd"] == pytest.approx(0.40)
    assert totals["run-b"]["billed_known_lower_bound_usd"] == 0.0
    works = {work["work_id"]: work for work in document["works"]}
    run_b_entries = works["run-b"]["attempts"][0]["receipts"]
    assert run_b_entries[0]["basis"] == "refused"
    assert run_b_entries[0]["billed_usd"] is None
    programme = document["costs"]["programme"]
    assert programme["billed_usd"] is None
    assert programme["billed_known_lower_bound_usd"] == pytest.approx(0.40)
    assert any("no cross-run join" in note for note in document["notes"])
    # reordering the two runs keeps the same single attribution
    reordered = (
        EconomicsLinker()
        .link(
            MeasurementLinker().link(
                works=[
                    {"work_id": "run-b", "outcome": "accepted"},
                    {"work_id": "run-a", "outcome": "accepted"},
                ],
                attempts=[
                    {"work_id": "run-b", "attempt_id": "run-b/a1"},
                    {"work_id": "run-a", "attempt_id": "run-a/a1"},
                ],
                receipts=list(reversed(shared_rows)),
            ),
            acceptance_records=[
                {
                    "work_id": "run-b",
                    "accepted": True,
                    "decided_by": "human:operator",
                    "attempt_outcomes": {"run-b/a1": "accepted"},
                },
                {
                    "work_id": "run-a",
                    "accepted": True,
                    "decided_by": "human:operator",
                    "attempt_outcomes": {"run-a/a1": "accepted"},
                },
            ],
            rate_card=CARD,
        )
        .to_document()
    )
    assert reordered["costs"]["programme"]["billed_known_lower_bound_usd"] == pytest.approx(0.40)
    assert reordered["cross_joins"][0]["kept_attribution"] == "run-a/run-a/a1"


def test_document_round_trip_replays_the_aggregation():
    reference = _report().to_document()
    replayed = EconomicsReport.from_document(reference).to_document()
    assert json.dumps(repalyed_doc := replayed, sort_keys=True) == json.dumps(
        reference, sort_keys=True
    )
    assert repalyed_doc["schema"] == ECONOMICS_SCHEMA
    bad = dict(reference)
    bad["schema"] = MEASUREMENT_SCHEMA
    with pytest.raises(ValueError, match="expected 'forge.delivery.economics/1'"):
        EconomicsReport.from_document(bad)


# ----------------------------------------------------------------------
# The committed real-data report
# ----------------------------------------------------------------------


def test_committed_lab_economics_report_honours_the_contracts():
    if not COMMITTED_REPORT.exists():
        pytest.skip("the executed economics report has not been committed yet")
    document = json.loads(COMMITTED_REPORT.read_text())
    assert document["schema"] == ECONOMICS_SCHEMA
    pilot = document["pilot"]
    assert pilot["pilot_id"] == "lab-pilot-v1"
    # every receipt in the lab pilot is a labelled synthetic counter
    assert document["evidence"]["classes"].get(EVIDENCE_SYNTHETIC_COUNTER, 0) > 0
    assert EVIDENCE_LIVE_MODEL not in document["evidence"]["classes"]
    # unknown spend is never an exact zero
    programme = document["costs"]["programme"]
    assert programme["billed_usd"] is None
    assert programme["billed_exact"] is False
    coverage = document["costs"]["coverage"]["programme"]
    assert coverage["unknown_cost_receipts"] == coverage["receipts_received"]
    # latency stages separated with unknown stages named
    stages = document["latency"]["stage_seconds"]
    assert stages["model"]["measured"] is True
    for stage in ("tool", "queue", "verification"):
        assert stages[stage]["measured"] is False
        assert stages[stage]["stage_total_seconds"] is None
    # the ledger document the report was built from rides along
    assert document["measurement_ledger"]["schema"] == MEASUREMENT_SCHEMA
    assert document["measurement_ledger"]["works"]
    # the operator surface and the reconciliation fold cleanly
    summary = operator_summary(document)
    assert summary["join"]["works"] == 12
    assert summary["join"]["accepted_items"] == 12
    assert "prompt" not in json.dumps(summary)
    recon = reconcile_with_budget(document, {"cap_usd": 25.0})
    assert recon["unreconcilable"] == 3


def test_committed_discovery_live_population_is_separate_and_honest():
    """The sibling's landed captures join as their OWN population.

    The three live attempts are real gateway receipts (``live-model``) —
    the only live evidence in-tree — but they are RESEARCH captures with
    mechanical grades, so no accepted items are claimed, their spend
    never enters the pilot's aggregates, and their costs stay card
    estimates matching the capture's own recorded arithmetic.
    """
    if not COMMITTED_REPORT.exists():
        pytest.skip("the executed economics report has not been committed yet")
    document = json.loads(COMMITTED_REPORT.read_text())
    populations = document.get("populations") or {}
    discovery = populations.get("discovery_live")
    assert discovery is not None, "the discovery-live captures have landed — include them"
    programme = discovery["costs"]["programme"]
    # one run with three attempts (truncated, malformed-response, completed)
    # plus the separate scripted run — every attempt kept
    assert programme["works"] == 2
    assert programme["attempts"] == 4
    assert programme["receipts"] == 4
    # the evidence census separates the classes inside the population
    assert discovery["evidence"]["classes"] == {
        EVIDENCE_LIVE_MODEL: 3,
        EVIDENCE_SYNTHETIC_COUNTER: 1,
    }
    # no acceptance decisions exist for research captures — none claimed
    assert discovery["costs"]["accepted_items"]["works"] == 0
    assert (
        discovery["costs"]["programme_per_accepted_item"]["programme_billed_per_accepted_usd"]
        is None
    )
    # costs stay estimates (never billing) and reproduce the capture's own
    # recorded arithmetic: 0.070568 + 0.131920 + 0.113852
    assert programme["billed_usd"] is None
    assert programme["estimate_usd"] == pytest.approx(0.31634, abs=1e-6)
    entries = [
        entry
        for work in discovery["works"]
        for attempt in work["attempts"]
        for entry in attempt["receipts"]
    ]
    assert all(entry["basis"] == "estimate" for entry in entries)
    live = [entry for entry in entries if entry["evidence_class"] == EVIDENCE_LIVE_MODEL]
    assert sorted(entry["estimate_usd"] for entry in live) == [
        pytest.approx(0.070568, abs=1e-6),
        pytest.approx(0.113852, abs=1e-6),
        pytest.approx(0.131920, abs=1e-6),
    ]
    # the discovery spend never leaked into the pilot's aggregates
    assert document["evidence"]["classes"] == {EVIDENCE_SYNTHETIC_COUNTER: 19}
    assert document["costs"]["programme"]["works"] == 12
    assert any("own population" in note.lower() for note in document["notes"])


# ----------------------------------------------------------------------
# R38-09 — ingested lane rows feeding the economics measures
# ----------------------------------------------------------------------


def _ingested_ledger():
    """A ledger built from INGESTED lane receipts + planner ledger rows."""
    rows = [
        {
            "work_id": "w-acc",
            "attempt_id": "w-acc/a1",
            "receipt_id": "ing:w-acc:a1",
            "source": "lane/.forge/usage.json",
            "provider": "claude-sdk-lane",
            "model": "glm-5.3-flash",
            "counters": {"input_tokens": 1000, "output_tokens": 100},
            "cost_usd": 0.30,
            "cost_basis": "provider-reported",
            "segment": "segment:v1",
            "completeness": "aggregate",
        },
        {
            "work_id": "w-acc",
            "attempt_id": "w-acc/a2",
            "receipt_id": "ing:w-acc:a2",
            "source": "lane/.forge/usage.json",
            "provider": "claude-sdk-lane",
            "model": "glm-5.3-flash",
            "counters": {"input_tokens": 900, "output_tokens": 90},
            "cost_usd": 0.70,
            "cost_basis": "provider-reported",
            "segment": "segment:v1",
            "completeness": "aggregate",
        },
        {
            "work_id": "w-rej",
            "attempt_id": "w-rej/a1",
            "receipt_id": "ing:w-rej:a1",
            "source": "lane/.forge/usage.json",
            "provider": "codex-sdk-lane",
            "model": "gpt-5",
            "counters": {"input_tokens": 600, "output_tokens": 60},
            "cost_usd": 0.20,
            "cost_basis": "provider-reported",
            "segment": "segment:v2",
            "completeness": "aggregate",
        },
        # the planner ledger row: work-level, its own population
        {
            "work_id": "w-acc",
            "attempt_id": "",
            "receipt_id": "w-acc:planner:1",
            "source": "planner/llm_calls",
            "provider": "zai",
            "counters": {"input_tokens": 400, "output_tokens": 40},
            "cost_usd": 0.05,
            "cost_basis": "provider-reported",
            "completeness": "aggregate",
        },
    ]
    records = ledger_records_from_ingested_usage(
        rows,
        works=[
            {"work_id": "w-acc", "outcome": "accepted"},
            {"work_id": "w-rej", "outcome": "rejected"},
        ],
        attempts=[
            {"work_id": "w-acc", "attempt_id": "w-acc/a1", "outcome": "rejected"},
            {"work_id": "w-acc", "attempt_id": "w-acc/a2", "outcome": "accepted"},
            {"work_id": "w-rej", "attempt_id": "w-rej/a1", "outcome": "rejected"},
        ],
    )
    return MeasurementLinker().link(**records)


def test_three_measures_from_ingested_rows_stay_distinct():
    report = EconomicsLinker().link(
        _ingested_ledger(),
        acceptance_records=[
            {
                "work_id": "w-acc",
                "accepted": True,
                "decided_by": "human:operator",
                "attempt_outcomes": {"w-acc/a1": "rejected", "w-acc/a2": "accepted"},
            },
            {"work_id": "w-rej", "accepted": False, "decided_by": "human:operator"},
        ],
    )
    document = report.to_document()
    costs = document["costs"]
    # 1) the SUCCESSFUL attempt's own receipts (a2 only)
    assert costs["successful_attempt_costs"]["w-acc"]["w-acc/a2"]["cost_usd"] == pytest.approx(0.70)
    # 2) the accepted item's ALL-ATTEMPT total (a1 + a2)
    assert costs["accepted_item_totals"]["w-acc"]["billed_usd"] == pytest.approx(1.00)
    # 3) programme per accepted ((0.30 + 0.70 + 0.20) / 1)
    assert costs["programme_per_accepted_item"]["programme_billed_per_accepted_usd"] == (
        pytest.approx(1.20)
    )
    # the provider-reported column is named, separated from estimates
    assert costs["programme"]["provider_reported_usd"] == pytest.approx(1.20)
    census = costs["cost_basis_census"]["bases"]
    assert census["provider-reported"]["receipts"] == 3
    assert census["provider-reported"]["usd"] == pytest.approx(1.20)
    # attribution segments: two routes — two segments, both kept
    segments = {(row["segment"], row["route"]) for row in costs["attribution_segments"]}
    assert segments == {
        ("segment:v1", "claude-sdk-lane/glm-5.3-flash"),
        ("segment:v2", "codex-sdk-lane/gpt-5"),
    }


def test_committed_live_single_writer_population_is_honest():
    """The re-executed report's live population — the REAL captured receipts.

    Coverage BEFORE the join: the durable ``usage_receipts`` table was
    EMPTY at capture (the recorded gap). AFTER: the four SDK receipt
    artifacts the evidence carries are ingested idempotently — the lane
    population is measured, the killed job's spend stays unknown, and the
    pilot's own aggregates are untouched.
    """
    if not COMMITTED_REPORT.exists():
        pytest.skip("the executed economics report has not been committed yet")
    document = json.loads(COMMITTED_REPORT.read_text())
    populations = document.get("populations") or {}
    live = populations.get("live_single_writer")
    assert live is not None, "the live single-writer captures have landed — include them"
    # the recorded gap, quoted in the population's own pilot block
    assert live["pilot"]["durable_usage_receipts_rows_at_capture"] == 0
    ingested = live["ingestion"]["ingested_receipt_ids"]
    assert sorted(ingested) == [
        "live-evidence:job723",
        "live-evidence:job726",
        "live-evidence:job731",
        "live-evidence:job732",
    ]
    programme = live["costs"]["programme"]
    # 3 runs, 5 lane attempts, 4 receipts — the killed job730 keeps its
    # attempt with NO receipt: spend unknown, coverage 0.8, never zero
    assert programme["works"] == 3
    assert programme["attempts"] == 5
    assert programme["receipts"] == 4
    assert programme["receipt_coverage"] == pytest.approx(0.8)
    assert programme["billed_exact"] is False
    assert programme["billed_usd"] is None
    # the SDK-reported lane spend, as a provider-reported LOWER BOUND:
    # 0.2353712 + 0.2179536 + 0.2005248 + 0.1498992
    assert programme["billed_known_lower_bound_usd"] == pytest.approx(0.803749, abs=1e-6)
    assert programme["provider_reported_usd"] == pytest.approx(0.803749, abs=1e-6)
    # every lane receipt is live-model evidence (a real SDK metered them)
    assert live["evidence"]["classes"] == {EVIDENCE_LIVE_MODEL: 4}
    # the successful lane attempt's OWN cost — the "$0.2180" figure, a
    # DIFFERENT measure from all-attempt and per-accepted (both undefined
    # here: qualification runs carry no acceptance decisions)
    successful = live["costs"]["successful_attempt_costs"]["60b9de7f"]["job726"]
    assert successful["cost_usd"] == pytest.approx(0.217954, abs=1e-6)
    assert live["costs"]["accepted_items"]["works"] == 0
    assert live["costs"]["programme_per_accepted_item"]["programme_billed_per_accepted_usd"] is None
    # the spend-cap reservation consults the ingested totals
    cap = live["ingestion"]["spend_cap_check"]
    assert cap["allowed"] is True
    assert cap["cap_usd"] == pytest.approx(2.0)
    assert cap["reserved_usd"] == pytest.approx(0.8037488, abs=1e-6)
    # the trace names the review window as an unknown gap, never zero
    trace = live["trace_green_task"]
    assert any("review" in row["record"] for row in trace["excluded_or_unknown"])
    # planning counters stay SEPARATE (never ingested as spend rows)
    assert live["planning_counters"]["run_budgets_durable"]["60b9de7f"] == {
        "calls": 3,
        "tokens": 179494,
    }
    # the pilot's own aggregates are untouched by the lane population
    assert document["evidence"]["classes"] == {EVIDENCE_SYNTHETIC_COUNTER: 19}
    assert document["costs"]["programme"]["works"] == 12
    assert document["costs"]["programme"]["billed_known_lower_bound_usd"] == 0.0
