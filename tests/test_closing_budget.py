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
    CLOSING_PARTITION_POLICY_VERSION,
    DEFAULT_CLOSING_RESERVE_FRACTION,
    OBSERVABLE_ACCRUED_UNSETTLED,
    OBSERVABLE_AMENDMENT_APPLIED,
    OBSERVABLE_AMENDMENT_REPLAYED,
    OBSERVABLE_CLOSING_CAPACITY,
    OBSERVABLE_CLOSING_RESERVE,
    OBSERVABLE_REFUSED_AXIS,
    OBSERVABLE_RETAINED_LIABILITY,
    OBSERVABLE_REVIEW_ONLY_CALLS,
    OBSERVABLE_REVIEW_ONLY_RECOVERY,
    OBSERVABLE_SETTLEMENT_RELEASE,
    OBSERVABLE_UNBOUNDED_INTERVALS,
    POLICY_AUTHORITY_TTL_ENV,
    POLICY_CAP_ENV,
    POLICY_FRACTION_ENV,
    POLICY_RESERVE_ENV,
    REVIEW_SHORTCUT_AUTHORITY_EXPIRED,
    REVIEW_SHORTCUT_STALE,
    REVIEW_SHORTCUT_UNVERIFIED,
    AppliedTopUp,
    BudgetTopUp,
    CandidateBinding,
    ClosingPartition,
    ClosingReservePolicy,
    TopUpLedger,
    amendment_ledger_document,
    coder_cap_check,
    closing_budget_report,
    closing_partition,
    review_only_continuation,
    rows_from_durable_receipts,
)
from forge.adaptive.delivery_measurement import ProviderRoute
from forge.adaptive.usage_ingestion import (
    COST_BASIS_ESTIMATED,
    COST_BASIS_PROVIDER_REPORTED,
    IngestedUsageRow,
)
from forge.durable.budgets import BudgetLimits


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
# R40-03 (#339) — finality settles: settled / accrued-unsettled /
# retained liability separated in the honest report
# ----------------------------------------------------------------------


def test_the_report_separates_settled_accrued_and_retained_liability():
    """The user-facing report distinguishes exact settled spend,
    provisional accrued subtotals, and the remaining liability — and
    renders the new ``budget.*`` observability names as report keys."""
    policy = ClosingReservePolicy.from_env({POLICY_RESERVE_ENV: "1.00", POLICY_CAP_ENV: "10.0"})
    rows = [
        row("exact", cost=0.50, basis=COST_BASIS_PROVIDER_REPORTED),
        row("partial", cost=0.25, lower=0.25, upper=2.00, final=False),
        row("unknown", cost=None, lower=0.10, upper=0.40),
    ]
    report = closing_budget_report(rows, cap_usd=10.0, policy=policy)
    document = report.to_json()
    # settled: FINAL rows' costs only — the partial's 0.25 is NOT settled
    assert document["settled_usd"] == pytest.approx(0.50)
    # accrued-unsettled: the streamed subtotal, provisional
    assert document["accrued_unsettled_usd"] == pytest.approx(0.25)
    # retained liability: the envelopes (2.00 + 0.40) — the 0.25 rides
    # INSIDE its envelope, never added on top
    assert document["retained_liability_usd"] == pytest.approx(2.40)
    assert document["settlement_release_usd"] == pytest.approx(0.0)
    assert document["unbounded_intervals"] == 0
    # the known subtotal stays every known figure (settled + accrued)
    assert document["known_subtotal_usd"] == pytest.approx(0.75)
    # the budget.* observability names as report keys
    assert document[OBSERVABLE_ACCRUED_UNSETTLED.split(".", 1)[1]] == pytest.approx(0.25)
    assert document[OBSERVABLE_RETAINED_LIABILITY.split(".", 1)[1]] == pytest.approx(2.40)
    assert document[OBSERVABLE_SETTLEMENT_RELEASE.split(".", 1)[1]] == pytest.approx(0.0)
    assert document[OBSERVABLE_UNBOUNDED_INTERVALS.split(".", 1)[1]] == 0


def test_the_p01_partial_never_releases_the_envelope_in_the_closing_report():
    """The P01 counterexample through the closing decision: exposure 11
    against a cap of 10 — the review does not fit while the partial
    holds its envelope; the settlement at 0.5 releases 2.5 exactly once
    and the same projection then fits."""
    policy = ClosingReservePolicy.from_env({POLICY_RESERVE_ENV: "0.60", POLICY_CAP_ENV: "10.0"})
    held_rows = [
        row("settled", cost=8.0, basis=COST_BASIS_PROVIDER_REPORTED),
        row("partial", cost=0.5, lower=0.5, upper=3.0, final=False),
    ]
    held = closing_budget_report(held_rows, cap_usd=10.0, policy=policy, projection_usd=1.0)
    assert held.cap_check["allowed"] is False  # 8 + 3 + 1 = 12 > 10
    assert held.retained_liability_usd == pytest.approx(3.0)
    assert held.accrued_unsettled_usd == pytest.approx(0.5)
    assert held.reserve_intact is False  # exposure 11 ate the reserve
    assert held.closing_review_fits is False

    settled_rows = [
        row("settled", cost=8.0, basis=COST_BASIS_PROVIDER_REPORTED),
        row("partial", cost=0.5, lower=0.5, upper=3.0, final=True),
    ]
    settled = closing_budget_report(settled_rows, cap_usd=10.0, policy=policy, projection_usd=1.0)
    assert settled.cap_check["allowed"] is True  # 8.5 + 1 = 9.5 <= 10
    assert settled.settled_usd == pytest.approx(8.5)
    assert settled.retained_liability_usd == pytest.approx(0.0)
    assert settled.settlement_release_usd == pytest.approx(2.5)
    document = settled.to_json()
    assert document[OBSERVABLE_SETTLEMENT_RELEASE.split(".", 1)[1]] == pytest.approx(2.5)
    assert settled.reserve_intact is True  # 8.5 <= 10 - 0.6
    assert settled.closing_review_fits is True


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
# R40-17 (#353) — the ONE amendment-ledger projection both provider
# legs compose (the duplicated decision removed)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class _AmendmentRow:
    """The durable ``budget_amendments`` row's shape (duck-typed — the
    projection reads exactly these attributes)."""

    command_id: str
    axis: str
    amount_usd: float | None = None
    amount_calls: int | None = None
    amount_tokens: int | None = None
    amount_wallclock_s: int | None = None
    reason: str = "recorded"
    operator: str = ""
    status: str = "applied"
    refusal_reason: str | None = None
    run_id: str = "w"


def test_the_amendment_ledger_projects_both_views_of_one_table():
    """The full audit view carries every command (applied AND refused);
    the pinned #325 usd view carries only the APPLIED usd rows; the
    total is the applied usd total — refused never counts."""
    rows = [
        _AmendmentRow(
            command_id="run:continue_review:42:9101",
            axis="usd",
            amount_usd=0.50,
            reason="close the review",
            operator="human:alice",
        ),
        _AmendmentRow(
            command_id="run:continue_review:42:9102",
            axis="calls",
            amount_calls=1,
            reason="reopen the guard",
            operator="human:alice",
        ),
        _AmendmentRow(
            command_id="run:continue_review:42:9103",
            axis="usd",
            amount_usd=9.0,
            status="refused",
            refusal_reason="usd: ...",
        ),
    ]
    document = amendment_ledger_document(rows)
    assert len(document["amendments"]) == 3  # the audit keeps refusals
    assert document["amendments"][0]["refusal_reason"] is None
    assert [t["idempotency_key"] for t in document["top_ups"]] == [
        "run:continue_review:42:9101"
    ]  # only APPLIED usd rows ride the legacy view
    assert document["top_up_total_usd"] == pytest.approx(0.50)  # refused never counts


def test_the_amendment_ledger_over_no_rows_is_the_empty_ledger():
    document = amendment_ledger_document([])
    assert document == {
        "amendments": [],
        "top_ups": [],
        "top_up_total_usd": 0.0,
    }


# ----------------------------------------------------------------------
# R40-04 (#340) — the versioned closing partition + the pre-paid-review
# shortcut guards (authority expiry, missing verification)
# ----------------------------------------------------------------------


def test_the_partition_sizes_each_numeric_axis_under_the_versioned_rule():
    """v1: ceil(limit × fraction) per LIMITED axis; wall-clock is not
    partitioned; the share is never the whole axis."""
    policy = ClosingReservePolicy.from_env({POLICY_RESERVE_ENV: "0.60", POLICY_CAP_ENV: "2.00"})
    partition = closing_partition(
        BudgetLimits(max_calls=40, max_tokens=100_000, wallclock_s=1800), policy
    )
    assert partition is not None
    assert partition.policy_version == CLOSING_PARTITION_POLICY_VERSION
    assert partition.calls == 6  # ceil(40 * 0.15) — the provisional default
    assert partition.tokens == 15_000  # ceil(100_000 * 0.15)
    document = partition.to_json()
    assert document["wallclock_partitioned"] is False  # the stated v1 rule
    assert document["policy_version"] == CLOSING_PARTITION_POLICY_VERSION


def test_the_partition_never_takes_the_whole_axis_or_partitions_units():
    policy = ClosingReservePolicy.from_env({POLICY_FRACTION_ENV: "0.9", POLICY_CAP_ENV: "10"})
    tiny = closing_partition(BudgetLimits(max_calls=4, max_tokens=1), policy)
    assert tiny is not None
    assert tiny.calls == 3  # min(ceil(4*0.9), 4-1) — never the whole axis
    assert tiny.tokens is None  # a one-unit axis cannot share
    unlimited = closing_partition(BudgetLimits(wallclock_s=60), policy)
    assert unlimited is None  # nothing limited to partition — honest none


def test_the_authority_ttl_degrades_to_the_default_never_to_forever():
    default_env: dict[str, str] = {}
    policy = ClosingReservePolicy.from_env(default_env)
    assert policy.authority_ttl_seconds(default_env) > 0  # a documented default
    tuned_env = {POLICY_AUTHORITY_TTL_ENV: "3600"}
    tuned = ClosingReservePolicy.from_env(tuned_env)
    assert tuned.authority_ttl_seconds(tuned_env) == 3600
    for garbage in ("0", "-5", "not-a-number"):
        env = {POLICY_AUTHORITY_TTL_ENV: garbage}
        assert ClosingReservePolicy.from_env(env).authority_ttl_seconds(
            env
        ) == policy.authority_ttl_seconds(default_env)


def test_an_expired_authority_invalidates_the_shortcut_before_paid_review():
    from datetime import datetime, timedelta, timezone

    binding = CandidateBinding(candidate_sha="a" * 40, tested_identity="a" * 40)
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    decision = review_only_continuation(
        budget_decision="budget_exhausted",
        recorded=binding,
        current=binding,
        authority_expires_at=past,
    )
    assert decision.allowed is False
    assert decision.reason == REVIEW_SHORTCUT_AUTHORITY_EXPIRED
    assert decision.coder_dispatches == 0
    # a future deadline (and no deadline at all) keeps the shortcut open
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert (
        review_only_continuation(
            budget_decision="budget_exhausted",
            recorded=binding,
            current=binding,
            authority_expires_at=future,
        ).allowed
        is True
    )
    assert (
        review_only_continuation(
            budget_decision="budget_exhausted", recorded=binding, current=binding
        ).allowed
        is True
    )


def test_missing_verification_invalidates_the_shortcut_before_paid_review():
    binding = CandidateBinding(candidate_sha="a" * 40, tested_identity="a" * 40)
    decision = review_only_continuation(
        budget_decision="budget_exhausted", recorded=binding, current=binding, verified=False
    )
    assert decision.allowed is False
    assert decision.reason == REVIEW_SHORTCUT_UNVERIFIED
    assert "paid review" in decision.detail


def test_the_r40_04_observability_names_exist():
    """The amendment/review-only scope's observables are declared
    constants — the evidence keys the service renders."""
    assert OBSERVABLE_AMENDMENT_APPLIED == "budget.amendment_applied"
    assert OBSERVABLE_AMENDMENT_REPLAYED == "budget.amendment_replayed"
    assert OBSERVABLE_REFUSED_AXIS == "budget.refused_axis"
    assert OBSERVABLE_REVIEW_ONLY_CALLS == "delivery.review_only_calls"
    assert OBSERVABLE_CLOSING_CAPACITY == "budget.closing_capacity"
    assert ClosingPartition(calls=1, tokens=None).policy_version == (
        CLOSING_PARTITION_POLICY_VERSION
    )


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
