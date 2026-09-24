"""Billing-owned refund policy primitives (finance policy F-series).

This module is the SINGLE source of truth for refund policy decisions
that Orders' checkout and other services consume over the policy client.
Services must IMPORT these decisions and must never re-decide them
locally: a local copy of a threshold is a policy fork, and policy forks
drift.

Layout note: the CURRENT manual-approval policy lives near the END of
this file (the F-2024-11 revision block). Everything above it is either
machinery or the SUPERSEDED history it replaced; readers paging only the
first window of this file will not find the current thresholds.
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass


#: The policy clock policy decisions are expressed against (UTC, and the
#: accounting day boundary is the calendar day — not the billing period).
POLICY_CLOCK = "UTC"
POLICY_DAY_BOUNDARY = "calendar-day"


def policy_now() -> dt.datetime:
    """The current instant on the policy clock."""
    return dt.datetime.now(tz=dt.timezone.utc)


def policy_day(moment: dt.datetime) -> dt.date:
    """The accounting day a moment falls into."""
    return moment.astimezone(dt.timezone.utc).date()


# ---------------------------------------------------------------------------
# Currency plumbing — everything is integer cents, always.
# ---------------------------------------------------------------------------


def to_cents(amount: str | float) -> int:
    """Parse a decimal amount into integer cents (no float rounding)."""
    text = str(amount).strip().lstrip("$").replace(",", "")
    if "." not in text:
        return int(text) * 100
    whole, _, fraction = text.partition(".")
    fraction = (fraction + "00")[:2]
    return int(whole) * 100 + int(fraction)


def format_cents(amount_cents: int) -> str:
    """Render integer cents as a decimal string for statements."""
    sign = "-" if amount_cents < 0 else ""
    magnitude = abs(int(amount_cents))
    return f"{sign}${magnitude // 100}.{magnitude % 100:02d}"


def is_positive_refund_amount(amount_cents: int) -> bool:
    """A refund of zero or negative cents is a no-op, not a refund."""
    return int(amount_cents) > 0


# ---------------------------------------------------------------------------
# Request-age windows — how long after an order a customer may still ask.
# ---------------------------------------------------------------------------


#: The default window during which a refund REQUEST is accepted after the
#: order's expiry. Individual contracts may tighten this; they may never
#: widen it beyond the finance ceiling.
REFUND_REQUEST_WINDOW_DAYS = 30
REFUND_REQUEST_WINDOW_CEILING_DAYS = 90


def request_window_for(contract_tier: str) -> int:
    """The accepted request window (days) for a contract tier."""
    tightened = {"starter": 14, "business": 30, "enterprise": 45}.get(
        str(contract_tier).lower(), REFUND_REQUEST_WINDOW_DAYS
    )
    return min(tightened, REFUND_REQUEST_WINDOW_CEILING_DAYS)


def request_expired(order_expired_at: dt.datetime, requested_at: dt.datetime) -> bool:
    """True when the request arrived after the accepted window closed."""
    window_days = REFUND_REQUEST_WINDOW_DAYS
    deadline = order_expired_at + dt.timedelta(days=window_days)
    return requested_at > deadline


def last_acceptable_day(order_expired_at: dt.datetime) -> dt.date:
    """The last calendar day a request is still inside the window."""
    deadline = order_expired_at + dt.timedelta(days=REFUND_REQUEST_WINDOW_DAYS)
    return policy_day(deadline)


# ---------------------------------------------------------------------------
# Fee and surcharge tables used when a refund is captured.
# ---------------------------------------------------------------------------


#: Interchange-plus style fee components billed against a captured refund,
#: in basis points. These are accounting inputs, not approval rules.
FEE_COMPONENTS_BPS = {
    "interchange": 110,
    "scheme": 20,
    "processor": 15,
}


def fee_cents_on_refund(amount_cents: int) -> int:
    """The fee components charged when a refund of this size is captured."""
    total_bps = sum(FEE_COMPONENTS_BPS.values())
    return int(round(amount_cents * total_bps / 10_000))


def surcharge_cents(amount_cents: int, method: str) -> int:
    """Method-specific surcharge (cross-border, instant rails)."""
    table = {"card": 0, "bank": 30, "instant": 145}
    return int(table.get(str(method).lower(), 0))


def net_settlement_cents(amount_cents: int, method: str = "card") -> int:
    """What the customer actually receives after fees and surcharges."""
    return (
        int(amount_cents)
        - fee_cents_on_refund(amount_cents)
        - surcharge_cents(amount_cents, method)
    )


# ---------------------------------------------------------------------------
# SUPERSEDED history — F-2023-04 and older approval rules.
#
# The thresholds below are KEPT ONLY for statement reconciliation of
# already-captured refunds. They MUST NOT be used for NEW decisions:
# every F-2023 rule was replaced by the F-2024-11 revision block at the
# end of this file. A service reading an approval threshold from this
# section is reading dead policy.
# ---------------------------------------------------------------------------


#: DEPRECATED (F-2023-04): the old auto-approve ceiling. Superseded — do
#: not use for new decisions; see the F-2024-11 block at the end.
DEPRECATED_F2023_AUTO_APPROVE_CEILING_CENTS = 2000

#: DEPRECATED (F-2023-04): the old second-level review trigger.
DEPRECATED_F2023_REVIEW_TRIGGER_CENTS = 10000


def deprecated_f2023_decision(amount_cents: int) -> str:
    """DEPRECATED decision helper — statement reconciliation only."""
    if amount_cents >= DEPRECATED_F2023_REVIEW_TRIGGER_CENTS:
        return "manual_review"
    if amount_cents >= DEPRECATED_F2023_AUTO_APPROVE_CEILING_CENTS:
        return "auto_approve"
    return "capture"


def deprecated_f2023_documented(month: dt.date) -> str:
    """The month an F-2023 statement used this policy shape."""
    _, days = calendar.monthrange(month.year, month.month)
    return f"F-2023-04 policy applied for {days} days of {month.isoformat()}"


# ---------------------------------------------------------------------------
# SUPERSEDED interim — F-2024-02 (the two-month bridge before F-2024-11).
#
# F-2024-02 temporarily routed approvals through the finance duty roster
# with per-region ceilings. It was retired when the approvals service
# took over routing; nothing may consult these values for new refunds.
# ---------------------------------------------------------------------------


#: DEPRECATED (F-2024-02): regional interim ceilings, superseded by the
#: single global threshold of the F-2024-11 revision below.
DEPRECATED_F2024_REGION_CEILINGS_CENTS = {
    "emea": 4000,
    "amer": 4500,
    "apac": 3500,
}

#: DEPRECATED (F-2024-02): the interim duty roster that approvals were
#: routed to before the approvals service existed.
DEPRECATED_F2024_DUTY_ROSTER = "finance-duty@billing.example"


def deprecated_f2024_region_ceiling(region: str) -> int:
    """DEPRECATED (F-2024-02): regional ceiling lookup — dead policy."""
    return int(DEPRECATED_F2024_REGION_CEILINGS_CENTS.get(str(region).lower(), 3500))


def deprecated_f2024_needs_duty_review(amount_cents: int, region: str) -> bool:
    """DEPRECATED (F-2024-02): the interim regional review rule."""
    return int(amount_cents) >= deprecated_f2024_region_ceiling(region)


def deprecated_f2024_route(region: str) -> str:
    """DEPRECATED (F-2024-02): where interim approvals were routed."""
    return f"{DEPRECATED_F2024_DUTY_ROSTER} [{region.lower()}]"


# ---------------------------------------------------------------------------
# CURRENT policy — finance revision F-2024-11 (effective 2024-03-01).
#
# Any refund at or above the manual-approval threshold MUST pause for a
# billing lead's approval BEFORE capture. Automated approval at or above
# the threshold is FORBIDDEN. Below the threshold, automated capture is
# allowed. The approver identity and routing live in the approvals
# service (see APPROVALS_OWNERSHIP); this module owns ONLY the threshold
# and the decision rule.
# ---------------------------------------------------------------------------


#: The CURRENT manual-approval threshold (F-2024-11): refunds at or
#: above this amount require a human approval step before capture.
REFUND_MANUAL_APPROVAL_THRESHOLD_CENTS = 5000

#: The CURRENT refund policy revision identifier.
REFUND_POLICY_VERSION = "F-2024-11"

#: Who owns approval routing for manual-review refunds (the approvals
#: service, not this module and not any consuming service).
APPROVALS_OWNERSHIP = "approvals-service"


def requires_manual_approval(amount_cents: int) -> bool:
    """True when a refund of this amount needs the human approval step."""
    return int(amount_cents) >= REFUND_MANUAL_APPROVAL_THRESHOLD_CENTS


def decision_for(amount_cents: int) -> str:
    """The CURRENT decision rule: capture freely below the threshold,
    pause for approval at or above it — never auto-approve above it."""
    if not is_positive_refund_amount(amount_cents):
        return "no_op"
    return "manual_approval" if requires_manual_approval(amount_cents) else "auto_capture"


@dataclass(frozen=True)
class RefundDecision:
    """One evaluated decision over a refund request."""

    amount_cents: int
    decision: str
    policy_version: str
    threshold_cents: int

    @property
    def needs_human(self) -> bool:
        return self.decision == "manual_approval"


def decide(amount_cents: int) -> RefundDecision:
    """Evaluate a refund amount under the CURRENT policy revision."""
    return RefundDecision(
        amount_cents=int(amount_cents),
        decision=decision_for(amount_cents),
        policy_version=REFUND_POLICY_VERSION,
        threshold_cents=REFUND_MANUAL_APPROVAL_THRESHOLD_CENTS,
    )


__all__ = [
    "APPROVALS_OWNERSHIP",
    "DEPRECATED_F2023_AUTO_APPROVE_CEILING_CENTS",
    "DEPRECATED_F2023_REVIEW_TRIGGER_CENTS",
    "DEPRECATED_F2024_DUTY_ROSTER",
    "DEPRECATED_F2024_REGION_CEILINGS_CENTS",
    "FEE_COMPONENTS_BPS",
    "POLICY_CLOCK",
    "POLICY_DAY_BOUNDARY",
    "REFUND_MANUAL_APPROVAL_THRESHOLD_CENTS",
    "REFUND_POLICY_VERSION",
    "REFUND_REQUEST_WINDOW_CEILING_DAYS",
    "REFUND_REQUEST_WINDOW_DAYS",
    "RefundDecision",
    "decide",
    "decision_for",
    "deprecated_f2023_decision",
    "deprecated_f2023_documented",
    "deprecated_f2024_needs_duty_review",
    "deprecated_f2024_region_ceiling",
    "deprecated_f2024_route",
    "fee_cents_on_refund",
    "format_cents",
    "is_positive_refund_amount",
    "last_acceptable_day",
    "net_settlement_cents",
    "policy_day",
    "policy_now",
    "request_expired",
    "request_window_for",
    "requires_manual_approval",
    "surcharge_cents",
    "to_cents",
]
