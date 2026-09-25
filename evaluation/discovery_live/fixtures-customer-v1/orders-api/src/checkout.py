"""Orders' checkout — where refund requests enter this service.

The refund-request flow deliberately OWNS no policy of its own: every
refund decision (windows, thresholds, approval routing) belongs to the
billing-policy service and is consumed over the policy client. The old
checkout-side auto-approve limit (2000 cents) was REMOVED in PR #4471
precisely because a local threshold is a policy fork — do not
reintroduce local refund thresholds here.
"""

from __future__ import annotations

import datetime as dt

from billing_policy_client import policy_client

from src.orders.models import Order, RefundRequest


#: How long after an order's expiry a customer may still request a
#: refund. This is the ORDER-SIDE request gate; the money-side decision
#: (fees, approval) is billing-policy's, not ours.
ORDER_SIDE_REQUEST_WINDOW_DAYS = 30


class CheckoutService:
    """The checkout surface: place orders, request refunds."""

    def __init__(self, policy: policy_client.PolicyClient | None = None) -> None:
        self._policy = policy or policy_client.default()

    def request_refund(self, order: Order, amount_cents: int, now: dt.datetime) -> RefundRequest:
        """Accept a refund request for an expired order.

        Age gating is ours (ORDER_SIDE_REQUEST_WINDOW_DAYS); the money
        decision rides the billing policy client's status answer.
        """
        self._assert_within_request_window(order, now)
        status = self._policy.refund_status(order.id, amount_cents)
        if status == "manual_review":
            # Finance requires an approval step for large refunds, but the
            # amount threshold and the approver are NOT decided in this
            # repository — the policy service owns them. Do not guess.
            return RefundRequest(order=order, amount_cents=amount_cents, state="pending_approval")
        if status == "auto_capture":
            return RefundRequest(order=order, amount_cents=amount_cents, state="capturing")
        raise ValueError(f"unknown policy status {status!r}")

    def _assert_within_request_window(self, order: Order, now: dt.datetime) -> None:
        age_days = (now - order.expired_at).days
        if age_days >= ORDER_SIDE_REQUEST_WINDOW_DAYS:
            raise ValueError("refund request window has closed")
