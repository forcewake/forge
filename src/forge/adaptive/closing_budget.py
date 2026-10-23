"""Reserve bounded delivery liability before authorizing another chargeable action."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from forge.adaptive.usage_types import ProviderRoute
from forge.adaptive.usage_ingestion import (
    COST_BASIS_BILLING,
    COST_BASIS_PROVIDER_REPORTED,
    INGESTION_SCHEMA,
    IngestedUsageRow,
    spend_cap_check,
)
from forge.durable.budgets import BudgetLimits

__all__ = [
    "CLOSING_PARTITION_POLICY_VERSION",
    "DEFAULT_CLOSING_RESERVE_FRACTION",
    "DEFAULT_REVIEW_AUTHORITY_TTL_SECONDS",
    "OBSERVABLE_ACCRUED_UNSETTLED",
    "OBSERVABLE_AMENDMENT_APPLIED",
    "OBSERVABLE_AMENDMENT_REPLAYED",
    "OBSERVABLE_CLOSING_CAPACITY",
    "OBSERVABLE_CLOSING_RESERVE",
    "OBSERVABLE_PHASE_EXHAUSTION",
    "OBSERVABLE_REFUSED_AXIS",
    "OBSERVABLE_RETAINED_LIABILITY",
    "OBSERVABLE_REVIEW_ONLY_CALLS",
    "OBSERVABLE_REVIEW_ONLY_RECOVERY",
    "OBSERVABLE_SETTLEMENT_RELEASE",
    "OBSERVABLE_UNBOUNDED_INTERVALS",
    "OBSERVABLE_UNRESOLVED_UPPER_BOUND",
    "POLICY_AUTHORITY_TTL_ENV",
    "POLICY_CAP_ENV",
    "POLICY_FRACTION_ENV",
    "POLICY_RESERVE_ENV",
    "REVIEW_SHORTCUT_AUTHORITY_EXPIRED",
    "REVIEW_SHORTCUT_STALE",
    "REVIEW_SHORTCUT_UNVERIFIED",
    "AppliedTopUp",
    "BudgetTopUp",
    "CandidateBinding",
    "ClosingBudgetReport",
    "ClosingPartition",
    "ClosingReservePolicy",
    "TopUpLedger",
    "amendment_ledger_document",
    "closing_partition",
    "coder_cap_check",
    "closing_budget_report",
    "review_only_continuation",
    "rows_from_durable_receipts",
]

#: The policy fields (environment-resolved — a deployment's profile knobs, deliberately not
#: new model columns): the absolute closing allowance, its fraction-of-cap spelling, and the
#: spend cap the fraction resolves against.
POLICY_RESERVE_ENV = "FORGE_CLOSING_RESERVE_USD"
POLICY_FRACTION_ENV = "FORGE_CLOSING_RESERVE_FRACTION"
POLICY_CAP_ENV = "FORGE_SPEND_CAP_USD"

#: The PROVISIONAL default: 15% of the spend cap, sized from the observed live workloads
#: (the delivery-economics record's lane receipts run $0.15–$0.80 per attempt; a closing
#: review + final evidence has consistently cost a fraction of one implementation attempt).
DEFAULT_CLOSING_RESERVE_FRACTION = 0.15

#: The typed staleness marker: the review-only shortcut's candidate or tested identity moved
#: — the recorded decision no longer binds the current candidate and the required
#: verification must rerun.
REVIEW_SHORTCUT_STALE = "review_shortcut_stale"

#: the review-only shortcut's AUTHORITY expired — the recorded reviewer-leg budget decision
#: is no longer a fresh operator surface (the TTL below); the run re-enters its normal gate
#: instead of replaying a stale authorization.
REVIEW_SHORTCUT_AUTHORITY_EXPIRED = "review_shortcut_authority_expired"

#: the shortcut has no verification bound to the tested candidate — the required
#: verification is missing, and a paid review must not ride a shortcut that cannot prove
#: what it is reviewing.
REVIEW_SHORTCUT_UNVERIFIED = "review_shortcut_unverified"

#: how long the recorded reviewer-leg budget decision stays a valid authority for the
#: review-only continuation.
POLICY_AUTHORITY_TTL_ENV = "FORGE_REVIEW_AUTHORITY_TTL_SECONDS"
DEFAULT_REVIEW_AUTHORITY_TTL_SECONDS = 24 * 3600

#: the VERSIONED policy that sizes the closing share of the guard's numeric axes. v1's
#: stated rule: the closing share of each limited axis is ``ceil(axis limit × closing
#: fraction)`` — the same fraction record the USD reserve resolves from
#: (class:`ClosingReservePolicy`), applied per-axis because the axes are DISTINCT (a USD
#: allowance cannot be silently converted into calls); wall-clock is NOT partitioned under
#: v1 (the closing review rides the run's own clock — the deadline is anchored at open time
#: and an extension is an explicit wallclock-axis amendment).
CLOSING_PARTITION_POLICY_VERSION = "closing-partition/1"

#: The observables (issue 's observability scope, extended by /'s finality model and
#: /'s amendment scope).
OBSERVABLE_CLOSING_RESERVE = "budget.closing_reserve"
OBSERVABLE_UNRESOLVED_UPPER_BOUND = "budget.unresolved_upper_bound"
OBSERVABLE_REVIEW_ONLY_RECOVERY = "delivery.review_only_recovery"
OBSERVABLE_PHASE_EXHAUSTION = "budget.phase_exhaustion"
#: the finality-separated quantities as report keys — the provisional subtotals streamed by
#: partial receipts, the envelope still fenced for them, the headroom settlements returned,
#: and how many intervals have no finite envelope at all.
OBSERVABLE_ACCRUED_UNSETTLED = "budget.accrued_unsettled_usd"
OBSERVABLE_RETAINED_LIABILITY = "budget.retained_liability_usd"
OBSERVABLE_SETTLEMENT_RELEASE = "budget.settlement_release_usd"
OBSERVABLE_UNBOUNDED_INTERVALS = "budget.unbounded_intervals"
#: the amendment + review-only scope — applied/replayed amendments, the refused limiting
#: axis, the review-only call count and the closing share's capacity per axis.
OBSERVABLE_AMENDMENT_APPLIED = "budget.amendment_applied"
OBSERVABLE_AMENDMENT_REPLAYED = "budget.amendment_replayed"
OBSERVABLE_REFUSED_AXIS = "budget.refused_axis"
OBSERVABLE_REVIEW_ONLY_CALLS = "delivery.review_only_calls"
OBSERVABLE_CLOSING_CAPACITY = "budget.closing_capacity"

#: The cost bases that make a known figure EXACT (a final figure the provider itself
#: reported, or one a billing reconciliation joined).
_EXACT_BASES = (COST_BASIS_PROVIDER_REPORTED, COST_BASIS_BILLING)


def _env_float(name: str, env: Mapping[str, str] | None) -> float | None:
    raw = (env or os.environ).get(name)
    if raw is None or not str(raw).strip():
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        return None
    if value < 0 or math.isnan(value) or math.isinf(value):
        return None
    return value


def _aware_utc(moment: Any) -> datetime | None:
    """A timezone-aware datetime, or ``None`` for anything unparsable."""
    if not isinstance(moment, datetime):
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


# The closing reserve policy
#


@dataclass(frozen=True)
class ClosingReservePolicy:
    """The profile-specific closing allowance: how much of the cap is RESERVED for the mandatory closing review + final evidence."""

    reserve_usd: float | None = None
    fraction: float | None = None
    cap_usd: float | None = None
    source: str = "default"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ClosingReservePolicy:
        """Resolve the policy from the environment (the profile fields)."""
        reserve = _env_float(POLICY_RESERVE_ENV, env)
        fraction = _env_float(POLICY_FRACTION_ENV, env)
        cap = _env_float(POLICY_CAP_ENV, env)
        if reserve is not None:
            return cls(
                reserve_usd=reserve, fraction=fraction, cap_usd=cap, source=POLICY_RESERVE_ENV
            )
        if fraction is not None:
            return cls(
                reserve_usd=None,
                fraction=fraction,
                cap_usd=cap,
                source=POLICY_FRACTION_ENV,
            )
        return cls(
            reserve_usd=None,
            fraction=DEFAULT_CLOSING_RESERVE_FRACTION,
            cap_usd=cap,
            source="default:provisional",
        )

    def reserve_for(self, cap_usd: float | None = None) -> float | None:
        """The reserve amount under this policy, or ``None`` (unresolvable)."""
        bound = cap_usd if cap_usd is not None else self.cap_usd
        if self.reserve_usd is not None:
            reserve = self.reserve_usd
        else:
            fraction = (
                self.fraction if self.fraction is not None else DEFAULT_CLOSING_RESERVE_FRACTION
            )
            if bound is None or bound <= 0:
                return None
            reserve = bound * fraction
        if reserve <= 0:
            return None
        if bound is not None and reserve > bound:
            return bound
        return reserve

    def to_json(self) -> dict[str, Any]:
        return {
            "reserve_usd": self.reserve_usd,
            "fraction": self.fraction,
            "cap_usd": self.cap_usd,
            "default_fraction": DEFAULT_CLOSING_RESERVE_FRACTION,
            "source": self.source,
            "provisional": self.source == "default:provisional",
        }

    def fraction_record(self) -> float:
        """The effective fraction — an explicit record or the provisional default."""
        return self.fraction if self.fraction is not None else DEFAULT_CLOSING_RESERVE_FRACTION

    def authority_ttl_seconds(self, env: Mapping[str, str] | None = None) -> int:
        """How long a recorded reviewer-leg decision stays a valid shortcut authority."""
        raw = (env or os.environ).get(POLICY_AUTHORITY_TTL_ENV)
        if raw is not None:
            try:
                value = int(str(raw).strip())
            except ValueError:
                value = 0
            if value > 0:
                return value
        return DEFAULT_REVIEW_AUTHORITY_TTL_SECONDS


# the closing share of the guard's numeric axes — the VERSIONED partition policy
#


@dataclass(frozen=True)
class ClosingPartition:
    """The closing share of the guard's numeric axes, frozen at budget open time by:func:`closing_partition` under:data:`CLOSING_PARTITION_POLICY_VERSION`."""

    calls: int | None
    tokens: int | None
    policy_version: str = CLOSING_PARTITION_POLICY_VERSION

    def to_json(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "closing_reserved_calls": self.calls,
            "closing_reserved_tokens": self.tokens,
            "wallclock_partitioned": False,
        }


def closing_partition(
    limits: BudgetLimits, policy: ClosingReservePolicy
) -> ClosingPartition | None:
    """Size the closing share of the guard's numeric axes (v1 rule)."""
    fraction = policy.fraction_record()

    def _share(limit: int | None) -> int | None:
        if limit is None or limit <= 1:
            # An unlimited axis has no share to partition; a one-unit axis cannot give the
            # review a share without forbidding ALL implementation (the same contract the
            # USD reserve states: protection is not prohibition) — v1 leaves it
            # un-partitioned.
            return None
        share = max(math.ceil(limit * fraction), 1)
        return min(share, limit - 1)  # never the whole axis

    calls = _share(limits.max_calls)
    tokens = _share(limits.max_tokens)
    if calls is None and tokens is None:
        return None
    return ClosingPartition(
        calls=calls, tokens=tokens, policy_version=CLOSING_PARTITION_POLICY_VERSION
    )


# Durable receipts → ingested rows (the report's input)
#


def rows_from_durable_receipts(receipts: Iterable[Any]) -> list[IngestedUsageRow]:
    """Map durable ``usage_receipts`` rows into:class:`IngestedUsageRow`."""
    rows: list[IngestedUsageRow] = []
    for receipt in receipts:
        raw = getattr(receipt, "raw", None)
        raw = raw if isinstance(raw, Mapping) else {}
        cost = getattr(receipt, "cost_usd", None)
        lower = raw.get("cost_lower_bound_usd")
        if not isinstance(lower, (int, float)) or isinstance(lower, bool):
            lower = cost if cost is not None else 0.0
        upper = raw.get("cost_upper_bound_usd")
        if not isinstance(upper, (int, float)) or isinstance(upper, bool):
            upper = None
        rows.append(
            IngestedUsageRow(
                work_id=str(getattr(receipt, "run_id", "") or ""),
                attempt_id=str(getattr(receipt, "attempt_id", "") or ""),
                receipt_id=str(getattr(receipt, "receipt_id", "") or ""),
                source=str(getattr(receipt, "source_namespace", "") or ""),
                route=ProviderRoute(
                    provider=str(getattr(receipt, "driver", "") or ""),
                    model=str(getattr(receipt, "model", "") or ""),
                ),
                cost_usd=float(cost)
                if isinstance(cost, (int, float)) and not isinstance(cost, bool)
                else None,
                cost_basis=str(getattr(receipt, "cost_basis", "") or ""),
                cost_lower_bound_usd=float(lower),
                cost_upper_bound_usd=float(upper) if upper is not None else None,
                rate_card_id=str(raw.get("rate_card_id") or ""),
                route_version=str(raw.get("route_version") or ""),
                final=bool(getattr(receipt, "final", True)),
                completeness=str(getattr(receipt, "completeness", "") or "unknown"),
            )
        )
    return rows


# The closing budget report — five distinguishable cost fields
#


@dataclass(frozen=True)
class ClosingBudgetReport:
    """The budget report the reviewer-leg decision records."""

    cap_usd: float | None
    reserve_usd: float | None
    exact_usd: float
    known_subtotal_usd: float
    lower_bound_usd: float
    reserved_liability_usd: float
    unknown_intervals: int
    closing_review_fits: bool
    reserve_intact: bool | None
    requires_bounded_policy: bool
    settled_usd: float = 0.0
    accrued_unsettled_usd: float = 0.0
    retained_liability_usd: float = 0.0
    settlement_release_usd: float = 0.0
    unbounded_intervals: int = 0
    cap_check: dict[str, Any] = field(default_factory=dict)
    policy: ClosingReservePolicy = field(default_factory=ClosingReservePolicy)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": INGESTION_SCHEMA,
            "cap_usd": self.cap_usd,
            "closing_reserve_usd": self.reserve_usd,
            "coder_ceiling_usd": (
                round(self.cap_usd - self.reserve_usd, 6)
                if self.cap_usd is not None and self.reserve_usd is not None
                else None
            ),
            # the five distinguishable fields
            "exact_usd": round(self.exact_usd, 6),
            "known_subtotal_usd": round(self.known_subtotal_usd, 6),
            "lower_bound_usd": round(self.lower_bound_usd, 6),
            "reserved_liability_usd": round(self.reserved_liability_usd, 6),
            "unknown_intervals": self.unknown_intervals,
            # the finality split () — exact settled spend vs provisional accrued vs
            # remaining liability
            "settled_usd": round(self.settled_usd, 6),
            "accrued_unsettled_usd": round(self.accrued_unsettled_usd, 6),
            "retained_liability_usd": round(self.retained_liability_usd, 6),
            "settlement_release_usd": round(self.settlement_release_usd, 6),
            # the decision + observables
            "closing_review_fits": self.closing_review_fits,
            "reserve_intact": self.reserve_intact,
            "closing_reserve": self.reserve_usd,
            "unresolved_upper_bound": self.cap_check.get("unbounded_intervals", 0),
            "unbounded_intervals": self.unbounded_intervals,
            "phase_exhaustion": (self.reserve_intact is False or not self.closing_review_fits),
            "requires_bounded_policy": self.requires_bounded_policy,
            "cap_check": dict(self.cap_check),
            "policy": self.policy.to_json(),
        }


def closing_budget_report(
    rows: Sequence[IngestedUsageRow],
    *,
    cap_usd: float | None,
    policy: ClosingReservePolicy,
    projection_usd: float = 0.0,
    unknown_interval_ceiling_usd: float | None = None,
) -> ClosingBudgetReport:
    """Fold the run's ingested spend into the closing budget report."""
    exact = math.fsum(
        row.cost_usd
        for row in rows
        if row.cost_usd is not None and row.cost_basis in _EXACT_BASES and row.final
    )
    known_subtotal = math.fsum(row.cost_usd for row in rows if row.cost_usd is not None)
    reserve = policy.reserve_for(cap_usd)
    effective_cap = cap_usd if cap_usd is not None else policy.cap_usd
    # The fold runs against an infinite cap when none is configured — the separated
    # quantities are cap-independent facts; only the ALLOWED decision needs a finite cap
    # (reported as None there).
    check: dict[str, Any] = spend_cap_check(
        rows,
        cap_usd=effective_cap if effective_cap is not None else math.inf,
        projection_usd=projection_usd,
        unknown_interval_ceiling_usd=unknown_interval_ceiling_usd,
    )
    if effective_cap is None:
        check["cap_usd"] = None
        check["allowed"] = False
        check["headroom_usd"] = None
    exposure = float(check["known_spend"]) + float(check["reserved_liability"])
    reserve_intact: bool | None
    fits: bool
    if effective_cap is None or reserve is None:
        # No closing policy resolved — the decision cannot claim the reserve covers anything
        # (an honest unknown, never a pass).
        reserve_intact = None
        fits = False
    else:
        reserve_intact = exposure <= (effective_cap - reserve) + 1e-9
        # The closing review fits when the exposure leaves the reserve (its own allowance)
        # un-consumed under the FULL cap.
        fits = reserve_intact and exposure + projection_usd <= effective_cap + 1e-9
    return ClosingBudgetReport(
        cap_usd=effective_cap,
        reserve_usd=reserve,
        exact_usd=exact,
        known_subtotal_usd=known_subtotal,
        lower_bound_usd=float(check["unknown_lower_bound"]),
        reserved_liability_usd=float(check["reserved_liability"]),
        unknown_intervals=int(check["unknown_intervals"]),
        closing_review_fits=fits,
        reserve_intact=reserve_intact,
        requires_bounded_policy=bool(check.get("requires_bounded_policy")),
        settled_usd=float(check["settled_usd"]),
        accrued_unsettled_usd=float(check["accrued_unsettled_usd"]),
        retained_liability_usd=float(check["retained_liability_usd"]),
        settlement_release_usd=float(check["settlement_release_usd"]),
        unbounded_intervals=int(check["unbounded_intervals"]),
        cap_check=check,
        policy=policy,
    )


def coder_cap_check(
    rows: Sequence[IngestedUsageRow],
    *,
    cap_usd: float,
    policy: ClosingReservePolicy,
    projection_usd: float = 0.0,
    unknown_interval_ceiling_usd: float | None = None,
) -> dict[str, Any]:
    """The IMPLEMENTATION phases' cap check: ``cap - closing_reserve``."""
    reserve = policy.reserve_for(cap_usd)
    if reserve is None:
        # No resolvable reserve: nothing to withhold (the honest un-bounded posture — never
        # a fabricated reserve of zero that silently grants the whole cap as "protected").
        check = spend_cap_check(
            rows,
            cap_usd=cap_usd,
            projection_usd=projection_usd,
            unknown_interval_ceiling_usd=unknown_interval_ceiling_usd,
        )
        check["effective_cap_usd"] = cap_usd
        check["closing_reserve_usd"] = None
        check["closing_reserve_withheld"] = False
        return check
    effective = max(cap_usd - reserve, 0.0)
    check = spend_cap_check(
        rows,
        cap_usd=effective,
        projection_usd=projection_usd,
        unknown_interval_ceiling_usd=unknown_interval_ceiling_usd,
    )
    check["effective_cap_usd"] = effective
    check["closing_reserve_usd"] = reserve
    check["closing_reserve_withheld"] = True
    check["notes"] = list(check["notes"]) + [
        f"coder ceiling is cap - closing reserve ({round(reserve, 6)} usd"
        " withheld for the mandatory closing review — un-consumable by"
        " implementation phases)"
    ]
    return check


# Review-only continuation — the guarded shortcut
#


@dataclass(frozen=True)
class CandidateBinding:
    """What the review-only shortcut is bound to: the candidate sha the
    CI tested and the verification's tested identity (the provider-verified
    oid / subject the checks verdict names)."""

    candidate_sha: str
    tested_identity: str

    def to_json(self) -> dict[str, str]:
        return {"candidate_sha": self.candidate_sha, "tested_identity": self.tested_identity}


@dataclass(frozen=True)
class ReviewContinuation:
    """The guarded decision: repeat ONLY the review, or refuse loudly."""

    allowed: bool
    reason: str
    detail: str = ""
    candidate: CandidateBinding | None = None
    coder_dispatches: int = 0
    commits: int = 0
    observable: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "detail": self.detail,
            "candidate": self.candidate.to_json() if self.candidate else None,
            "coder_dispatches": self.coder_dispatches,
            "commits": self.commits,
            "observable": self.observable,
        }


def review_only_continuation(
    *,
    budget_decision: str,
    recorded: CandidateBinding,
    current: CandidateBinding,
    authority_expires_at: Any = None,
    now: Any = None,
    verified: bool = True,
) -> ReviewContinuation:
    """Whether the review-only shortcut may repeat the SAME review."""
    decision = str(budget_decision or "").strip()
    if "budget" not in decision:
        return ReviewContinuation(
            allowed=False,
            reason="not_a_budget_decision",
            detail=(
                f"the recorded decision {decision!r} is not an explicit budget"
                " decision — the review-only shortcut does not open"
            ),
            candidate=recorded,
        )
    if recorded.candidate_sha != current.candidate_sha or (
        recorded.tested_identity != current.tested_identity
    ):
        return ReviewContinuation(
            allowed=False,
            reason=REVIEW_SHORTCUT_STALE,
            detail=(
                "the review-only shortcut is invalidated: the recorded binding"
                f" (candidate {recorded.candidate_sha[:8] or '?'}, tested"
                f" {recorded.tested_identity[:8] or '?'}) no longer names the"
                f" current candidate (candidate {current.candidate_sha[:8] or '?'},"
                f" tested {current.tested_identity[:8] or '?'}) — the required"
                " verification reruns"
            ),
            candidate=current,
            observable=OBSERVABLE_REVIEW_ONLY_RECOVERY,
        )
    if authority_expires_at is not None:
        deadline = _aware_utc(authority_expires_at)
        moment = _aware_utc(now) if now is not None else datetime.now(timezone.utc)
        if deadline is not None and moment is not None and moment > deadline:
            return ReviewContinuation(
                allowed=False,
                reason=REVIEW_SHORTCUT_AUTHORITY_EXPIRED,
                detail=(
                    "the review-only shortcut's authority expired at"
                    f" {authority_expires_at} — the recorded reviewer-leg budget"
                    " decision is no longer a fresh operator surface; the run"
                    " re-enters its normal gate"
                ),
                candidate=current,
                observable=OBSERVABLE_REVIEW_ONLY_RECOVERY,
            )
    if not verified:
        return ReviewContinuation(
            allowed=False,
            reason=REVIEW_SHORTCUT_UNVERIFIED,
            detail=(
                "the review-only shortcut has no verification bound to the"
                " tested candidate — the required verification is missing and"
                " a paid review must not ride the shortcut"
            ),
            candidate=current,
            observable=OBSERVABLE_REVIEW_ONLY_RECOVERY,
        )
    return ReviewContinuation(
        allowed=True,
        reason="",
        detail=(
            "review-only continuation on the unchanged, tested candidate —"
            " zero coder dispatches, zero commits"
        ),
        candidate=current,
        coder_dispatches=0,
        commits=0,
        observable=OBSERVABLE_REVIEW_ONLY_RECOVERY,
    )


# The amendment ledger's ONE projection
#


def amendment_ledger_document(rows: Sequence[Any]) -> dict[str, Any]:
    """The recorded decision's amendment ledger — the ONE projection of the durable ``budget_amendments`` rows every reviewer-leg decision composes."""
    return {
        "amendments": [
            {
                "command_id": row.command_id,
                "axis": row.axis,
                "amount_usd": row.amount_usd,
                "amount_calls": row.amount_calls,
                "amount_tokens": row.amount_tokens,
                "amount_wallclock_s": row.amount_wallclock_s,
                "reason": row.reason,
                "operator": row.operator,
                "status": row.status,
                "refusal_reason": row.refusal_reason,
            }
            for row in rows
        ],
        "top_ups": [
            {
                "run_id": row.run_id,
                "amount_usd": row.amount_usd,
                "reason": row.reason,
                "operator": row.operator,
                "idempotency_key": row.command_id,
            }
            for row in rows
            if str(row.axis or "") == "usd" and str(row.status or "") == "applied"
        ],
        "top_up_total_usd": round(
            math.fsum(
                float(row.amount_usd)
                for row in rows
                if str(row.status or "") == "applied" and row.amount_usd is not None
            ),
            6,
        ),
    }


# The explicit, auditable top-up
#


@dataclass(frozen=True)
class BudgetTopUp:
    """One operator top-up: an amount AND a reason, both required."""

    run_id: str
    amount_usd: float
    reason: str
    operator: str = ""
    key: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.amount_usd, (int, float)) or isinstance(self.amount_usd, bool):
            raise ValueError("budget top-up amount must be a number")
        if not math.isfinite(float(self.amount_usd)) or float(self.amount_usd) <= 0:
            raise ValueError("budget top-up amount must be a positive number")
        if not str(self.reason).strip():
            raise ValueError("budget top-up requires an explicit reason (auditable)")

    @property
    def idempotency_key(self) -> str:
        if self.key:
            return self.key
        material = json.dumps(
            [
                self.run_id,
                round(float(self.amount_usd), 6),
                str(self.reason).strip(),
                self.operator,
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        return "topup:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "amount_usd": round(float(self.amount_usd), 6),
            "reason": str(self.reason).strip(),
            "operator": self.operator,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class AppliedTopUp:
    """What one apply did: ``applied=False`` means the same key had
    already been applied — a replay, nothing added."""

    top_up: BudgetTopUp
    applied: bool
    total_added_usd: float

    def to_json(self) -> dict[str, Any]:
        return {
            **self.top_up.to_json(),
            "applied": self.applied,
            "total_added_usd": round(self.total_added_usd, 6),
        }


class TopUpLedger:
    """The auditable top-up ledger, replay-idempotent by key."""

    def __init__(self, records: Sequence[Mapping[str, Any]] = ()) -> None:
        self._applied: dict[str, BudgetTopUp] = {}
        self._order: list[str] = []
        for record in records:
            key = str(record.get("idempotency_key") or "")
            if not key:
                continue
            if key in self._applied:
                continue
            try:
                top_up = BudgetTopUp(
                    run_id=str(record.get("run_id") or ""),
                    amount_usd=float(record.get("amount_usd") or 0.0),
                    reason=str(record.get("reason") or "recorded"),
                    operator=str(record.get("operator") or ""),
                    key=key,
                )
            except ValueError:
                continue
            self._applied[key] = top_up
            self._order.append(key)

    def apply(self, top_up: BudgetTopUp) -> AppliedTopUp:
        """Apply *top_up* once; a same-key replay adds nothing."""
        key = top_up.idempotency_key
        if key in self._applied:
            return AppliedTopUp(
                top_up=self._applied[key],
                applied=False,
                total_added_usd=self.total_added_usd(),
            )
        self._applied[key] = top_up
        self._order.append(key)
        return AppliedTopUp(
            top_up=top_up,
            applied=True,
            total_added_usd=self.total_added_usd(),
        )

    def total_added_usd(self) -> float:
        return math.fsum(float(top_up.amount_usd) for top_up in self._applied.values())

    def records(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._applied[key].to_json() for key in self._order)
