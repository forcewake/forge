"""HTTP handlers for refund requests (Orders' public surface)."""

from __future__ import annotations

import datetime as dt

from src.checkout import CheckoutService
from src.orders.models import Order, RefundRequest


def handle_refund_request(
    checkout: CheckoutService, order: Order, amount_cents: int
) -> dict[str, str | int]:
    """POST /orders/{id}/refunds — accept and route a refund request."""
    try:
        request: RefundRequest = checkout.request_refund(
            order, amount_cents, now=dt.datetime.now(tz=dt.timezone.utc)
        )
    except ValueError as exc:
        return {"status": "rejected", "reason": str(exc)}
    return {"status": request.state, "order_id": order.id, "amount_cents": request.amount_cents}
