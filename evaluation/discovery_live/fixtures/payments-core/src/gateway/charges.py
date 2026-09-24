"""Payments' charge gateway — charge capture and reversal primitives."""

from __future__ import annotations

from dataclasses import dataclass


#: The rails a charge (or its reversal) may travel.
SUPPORTED_RAILS = ("card", "bank", "instant")


@dataclass(frozen=True)
class Charge:
    """One captured charge."""

    charge_id: str
    amount_cents: int
    rail: str


def reverse_charge(charge: Charge) -> dict[str, str | int]:
    """Reverse a captured charge (the rail-side half of a refund).

    Payments reverses whatever the policy layer approved; the decision
    WHETHER a refund needs approval is not made here.
    """
    if charge.rail not in SUPPORTED_RAILS:
        raise ValueError(f"unsupported rail {charge.rail!r}")
    return {"reversed_charge_id": charge.charge_id, "amount_cents": charge.amount_cents}
