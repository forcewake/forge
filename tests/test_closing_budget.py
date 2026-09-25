"""Q39-06 (#325) — the closing budget: known spend separated from safe
reserves, the promised closing review PROTECTED.

The author-recorded traces (useful-WIP + combined-steering) both ended
``blocked (budget_exhausted)`` at the REVIEWER leg after the candidate
and independent CI succeeded: the guard was right, but nothing kept the
implementation phases from consuming the allowance the mandatory review
needed. These tests pin the closing contract:

- THE RESERVE: a profile-specific closing allowance
  (``FORGE_CLOSING_RESERVE_USD`` / a fraction of the cap, the default
  PROVISIONAL) that the CODER cannot consume — the coder's cap check
  sees ``cap - reserve`` as its ceiling.
- THE REPORT: exact / known subtotal / lower bound / reserved liability
  / unknown — five DISTINGUISHABLE fields.
- REVIEW-ONLY CONTINUATION: after an explicit budget decision, repeat
  ONLY the review of the SAME candidate/tested identity — zero coder
  dispatches, zero commits; a moved candidate (or tested identity)
  invalidates the shortcut with the typed ``review_shortcut_stale``.
- THE TOP-UP: an operator command with an amount AND a reason,
  recorded, replay-idempotent (the same command retried adds its amount
  exactly once).
- THE DURABLE MAPPING: ``usage_receipts`` rows fold into the report
  (the reviewer-leg decision's input).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from forge.adaptive.closing_budget import (
    DEFAULT_CLOSING_RESERVE_FRACTION,
    OBSERVABLE_CLOSING_RESERVE,
    OBSERVABLE_REVIEW_ONLY_RECOVERY,
    POLICY_CAP_ENV,
    POLICY_FRACTION_ENV,
    POLICY_RESERVE_ENV,
    REVIEW_SHORTCUT_STALE,
    AppliedTopUp,
    BudgetTopUp,
    CandidateBinding,
    ClosingReservePolicy,
    TopUpLedger,
    coder_cap_check,
    closing_budget_report,
    review_only_continuation,
    rows_from_durable_receipts,
)
from forge.adaptive.delivery_measurement import ProviderRoute
from forge.adaptive.usage_ingestion import (
    COST_BASIS_ESTIMATED,
    COST_BASIS_PROVIDER_REPORTED,
    IngestedUsageRow,
)


def row(
    receipt_id: str,
    *,
    cost=None,
    lower: float = 0.0,
    upper=None,
    final: bool = True,
    basis: str = "",
) -> IngestedUsageRow:
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


# ----------------------------------------------------------------------
# The policy — resolution and the provisional default
# ----------------------------------------------------------------------


def test_the_absolute_policy_field_wins_over_the_fraction():
    policy = ClosingReservePolicy.from_env(
        {POLICY_RESERVE_ENV: "0.60", POLICY_FRACTION_ENV: "0.9", POLICY_CAP_ENV: "10"}
    )
    assert policy.reserve_for() == pytest.approx(0.60)
    assert policy.source == POLICY_RESERVE_ENV


def test_the_fraction_record_resolves_against_the_cap():
    policy = ClosingReservePolicy.from_env({POLICY_FRACTION_ENV: "0.2", POLICY_CAP_ENV: "10"})
    assert policy.reserve_for() == pytest.approx(2.0)


def test_the_default_fraction_is_provisional_and_documented():
    policy = ClosingReservePolicy.from_env({POLICY_CAP_ENV: "10"})
    assert policy.fraction == DEFAULT_CLOSING_RESERVE_FRACTION
    assert policy.source == "default:provisional"
    assert policy.to_json()["provisional"] is True
    assert policy.reserve_for() == pytest.approx(10 * DEFAULT_CLOSING_RESERVE_FRACTION)


def test_no_cap_and_no_absolute_field_reserves_nothing_honestly():
    policy = ClosingReservePolicy.from_env({})
    assert policy.reserve_for() is None  # never a silent zero reserve


# ----------------------------------------------------------------------
# The reserve is un-consumable by the coder phases
# ----------------------------------------------------------------------


def test_the_coder_ceiling_withholds_the_closing_reserve():
    """The implementation phases see ``cap - reserve``: a projection
    that would eat the review's allowance is refused at the CODER
    ceiling even though the same projection fits the FULL cap."""
    policy = ClosingReservePolicy.from_env({POLICY_RESERVE_ENV: "0.60", POLICY_CAP_ENV: "1.00"})
    rows = [row("spent", cost=0.35, basis=COST_BASIS_PROVIDER_REPORTED)]
    coder = coder_cap_check(rows, cap_usd=1.00, policy=policy, projection_usd=0.10)
    assert coder["closing_reserve_withheld"] is True
    assert coder["effective_cap_usd"] == pytest.approx(0.40)
    # 0.35 known + 0.10 projection = 0.45 > the 0.40 coder ceiling
    assert coder["allowed"] is False
    assert any("un-consumable by implementation phases" in n for n in coder["notes"])
    # the same projection against the FULL cap fits — the reserve is
    # exactly the withheld difference
    full = closing_budget_report(rows, cap_usd=1.00, policy=policy, projection_usd=0.10)
    assert full.cap_check["allowed"] is True


def test_the_closing_review_fits_while_its_reserve_is_intact():
    policy = ClosingReservePolicy.from_env({POLICY_RESERVE_ENV: "0.60", POLICY_CAP_ENV: "1.00"})
    rows = [row("spent", cost=0.35, basis=COST_BASIS_PROVIDER_REPORTED)]
    report = closing_budget_report(rows, cap_usd=1.00, policy=policy)
    assert report.reserve_intact is True  # 0.35 <= 1.00 - 0.60
    assert report.closing_review_fits is True
    document = report.to_json()
    assert document["closing_reserve"] == pytest.approx(0.60)  # the observable
    assert document["coder_ceiling_usd"] == pytest.approx(0.40)


def test_a_consumed_reserve_does_not_cover_the_review():
    policy = ClosingReservePolicy.from_env({POLICY_RESERVE_ENV: "0.60", POLICY_CAP_ENV: "1.00"})
    # the coder spent INTO the reserve: 0.90 known > the 0.40 ceiling
    rows = [row("spent", cost=0.90, basis=COST_BASIS_PROVIDER_REPORTED)]
    report = closing_budget_report(rows, cap_usd=1.00, policy=policy)
    assert report.reserve_intact is False
    assert report.closing_review_fits is False
    assert report.to_json()["phase_exhaustion"] is True


# ----------------------------------------------------------------------
# The five-field report — every field distinguishable
# ----------------------------------------------------------------------


def test_the_five_report_fields_stay_distinguishable():
    policy = ClosingReservePolicy.from_env({POLICY_RESERVE_ENV: "1.00", POLICY_CAP_ENV: "10.0"})
    rows = [
        row("exact", cost=0.50, basis=COST_BASIS_PROVIDER_REPORTED),
        row("estimate", cost=0.25, basis=COST_BASIS_ESTIMATED),
        row("unknown", cost=None, lower=0.10, upper=0.40),
    ]
    report = closing_budget_report(rows, cap_usd=10.0, policy=policy)
    document = report.to_json()
    # exact: final provider-reported only (the estimate is NOT exact)
    assert document["exact_usd"] == pytest.approx(0.50)
    # known subtotal: every known figure, estimates included
    assert document["known_subtotal_usd"] == pytest.approx(0.75)
    # the unknown interval's lower bound and retained envelope
    assert document["lower_bound_usd"] == pytest.approx(0.10)
    assert document["reserved_liability_usd"] == pytest.approx(0.40)
    assert document["unknown_intervals"] == 1
    # five DIFFERENT answers to five different questions
    assert (
        len(
            {
                document["exact_usd"],
                document["known_subtotal_usd"],
                document["lower_bound_usd"],
                document["reserved_liability_usd"],
                document["unknown_intervals"],
            }
        )
        == 5
    )


def test_the_report_names_the_unresolved_upper_bound():
    """An unknown interval with no finite envelope surfaces the
    ``budget.unresolved_upper_bound`` observable — visible, never
    silently bounded by its lower bound."""
    policy = ClosingReservePolicy.from_env({POLICY_RESERVE_ENV: "1.00", POLICY_CAP_ENV: "10.0"})
    rows = [
        row("exact", cost=0.50, basis=COST_BASIS_PROVIDER_REPORTED),
        row("unbounded", cost=None, lower=0.10),  # NO upper bound anywhere
    ]
    report = closing_budget_report(rows, cap_usd=10.0, policy=policy)
    document = report.to_json()
    assert document["requires_bounded_policy"] is True
    assert document["unresolved_upper_bound"] == 1
    assert document["closing_reserve"] == pytest.approx(1.0)
    assert document[OBSERVABLE_CLOSING_RESERVE.split(".", 1)[1]] == pytest.approx(1.0)


# ----------------------------------------------------------------------
# Review-only continuation — zero dispatches, typed staleness
# ----------------------------------------------------------------------


def test_review_only_continuation_repeats_only_the_review():
    binding = CandidateBinding(candidate_sha="a" * 40, tested_identity="a" * 40)
    decision = review_only_continuation(
        budget_decision="budget_exhausted", recorded=binding, current=binding
    )
    assert decision.allowed is True
    assert decision.coder_dispatches == 0  # ZERO coder dispatches, always
    assert decision.commits == 0  # ZERO commits, always
    assert decision.observable == OBSERVABLE_REVIEW_ONLY_RECOVERY
    assert "zero coder dispatches" in decision.detail


def test_a_moved_candidate_invalidates_the_shortcut_typed():
    recorded = CandidateBinding(candidate_sha="a" * 40, tested_identity="a" * 40)
    moved_head = CandidateBinding(candidate_sha="b" * 40, tested_identity="a" * 40)
    decision = review_only_continuation(
        budget_decision="budget_exhausted", recorded=recorded, current=moved_head
    )
    assert decision.allowed is False
    assert decision.reason == REVIEW_SHORTCUT_STALE
    assert "verification reruns" in decision.detail
    assert decision.coder_dispatches == 0


def test_a_moved_tested_identity_invalidates_the_shortcut_typed():
    recorded = CandidateBinding(candidate_sha="a" * 40, tested_identity="a" * 40)
    retested = CandidateBinding(candidate_sha="a" * 40, tested_identity="c" * 40)
    decision = review_only_continuation(
        budget_decision="budget_exhausted", recorded=recorded, current=retested
    )
    assert decision.allowed is False
    assert decision.reason == REVIEW_SHORTCUT_STALE


def test_only_an_explicit_budget_decision_opens_the_shortcut():
    binding = CandidateBinding(candidate_sha="a" * 40, tested_identity="a" * 40)
    decision = review_only_continuation(
        budget_decision="review_failed: gateway 500", recorded=binding, current=binding
    )
    assert decision.allowed is False
    assert decision.reason == "not_a_budget_decision"


# ----------------------------------------------------------------------
# The explicit, auditable top-up — replay-idempotent
# ----------------------------------------------------------------------


def test_a_top_up_requires_an_amount_and_a_reason():
    with pytest.raises(ValueError, match="positive number"):
        BudgetTopUp(run_id="w", amount_usd=0.0, reason="closing review")
    with pytest.raises(ValueError, match="reason"):
        BudgetTopUp(run_id="w", amount_usd=0.50, reason="  ")


def test_a_replayed_top_up_adds_its_amount_exactly_once():
    ledger = TopUpLedger()
    top_up = BudgetTopUp(
        run_id="w",
        amount_usd=0.50,
        reason="close the review within its reserve",
        operator="human:pavel",
    )
    first = ledger.apply(top_up)
    assert first.applied is True
    assert first.total_added_usd == pytest.approx(0.50)
    # the SAME command retried (a double-entered operator line) replays
    replay = ledger.apply(top_up)
    assert isinstance(replay, AppliedTopUp)
    assert replay.applied is False
    assert replay.total_added_usd == pytest.approx(0.50)  # added ONCE
    # a DIFFERENT reason or amount is a different command, applied again
    bigger = ledger.apply(
        BudgetTopUp(
            run_id="w", amount_usd=0.50, reason="second explicit approval", operator="human:pavel"
        )
    )
    assert bigger.applied is True
    assert bigger.total_added_usd == pytest.approx(1.00)
    # the audit trail keeps every applied record (amount + reason + operator)
    records = ledger.records()
    assert len(records) == 2
    assert {r["operator"] for r in records} == {"human:pavel"}
    assert all(r["reason"] for r in records)


def test_the_ledger_rebuilds_from_recorded_evidence():
    """The service persists the applied records and rebuilds the ledger
    from them — a crash-retried command after a restart is still a
    replay."""
    first = TopUpLedger()
    top_up = BudgetTopUp(run_id="w", amount_usd=0.25, reason="auditable top-up")
    first.apply(top_up)
    rebuilt = TopUpLedger(first.records())
    replay = rebuilt.apply(top_up)
    assert replay.applied is False
    assert replay.total_added_usd == pytest.approx(0.25)


# ----------------------------------------------------------------------
# The durable mapping — usage_receipts rows feed the report
# ----------------------------------------------------------------------


@dataclass
class FakeReceipt:
    """The duck-typed durable ``usage_receipts`` row shape."""

    run_id: str = "w"
    attempt_id: str = "a1"
    receipt_id: str = "r1"
    source_namespace: str = "lane/.forge/usage.json"
    driver: str | None = "claude-sdk-lane"
    model: str | None = "m"
    cost_usd: float | None = None
    cost_basis: str | None = None
    final: bool = True
    completeness: str = "unknown"
    raw: dict | None = None


def test_durable_receipts_fold_into_the_report():
    receipts = [
        FakeReceipt(
            receipt_id="known",
            cost_usd=0.50,
            cost_basis=COST_BASIS_PROVIDER_REPORTED,
            completeness="aggregate",
        ),
        FakeReceipt(
            receipt_id="unknown",
            cost_usd=None,
            completeness="partial",
            raw={"cost_lower_bound_usd": 0.10, "cost_upper_bound_usd": 0.40},
        ),
    ]
    rows = rows_from_durable_receipts(receipts)
    assert len(rows) == 2
    policy = ClosingReservePolicy.from_env({POLICY_RESERVE_ENV: "1.00", POLICY_CAP_ENV: "10.0"})
    report = closing_budget_report(rows, cap_usd=10.0, policy=policy)
    document = report.to_json()
    assert document["exact_usd"] == pytest.approx(0.50)
    assert document["lower_bound_usd"] == pytest.approx(0.10)
    assert document["reserved_liability_usd"] == pytest.approx(0.40)
    assert document["unknown_intervals"] == 1
