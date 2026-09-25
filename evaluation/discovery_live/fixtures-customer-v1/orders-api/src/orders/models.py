"""Order and refund-request models."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


ORDER_STATUSES = ("pending", "paid", "expired", "refunded", "cancelled")


@dataclass(frozen=True)
class Order:
    """One customer order."""

    id: str
    customer_id: str
    total_cents: int
    status: str
    expired_at: dt.datetime


@dataclass(frozen=True)
class RefundRequest:
    """One refund request against an expired order."""

    order: Order
    amount_cents: int
    state: str = "received"
    requested_at: dt.datetime | None = None
    events: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class RefundReceipt:
    """What a captured refund actually settled for."""

    request: RefundRequest
    settled_cents: int
    policy_version: str = ""
