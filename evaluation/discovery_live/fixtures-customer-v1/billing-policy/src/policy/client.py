"""The billing-policy client other services consume."""

from __future__ import annotations

from src.policy import refunds


class PolicyClient:
    """Read-only access to Billing's refund policy decisions."""

    def refund_status(self, order_id: str, amount_cents: int) -> str:
        """The policy status for a refund of this amount.

        Maps the CURRENT policy revision's decision onto the wire
        statuses consumers route on: ``manual_review`` or
        ``auto_capture``. The threshold itself never leaves this
        service as a number — consumers route on the status.
        """
        decision = refunds.decide(amount_cents)
        return {"manual_approval": "manual_review", "auto_capture": "auto_capture"}.get(
            decision.decision, "auto_capture"
        )


def default() -> PolicyClient:  # pragma: no cover - wiring
    return PolicyClient()
