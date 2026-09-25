"""Mobile checkout flow states (mobile-checkout)."""

import enum


class CheckoutStep(enum.Enum):
    """The native checkout funnel steps, in order."""

    CART = "cart"
    PAYMENT = "payment"
    CONFIRM = "confirm"
    DONE = "done"


FUNNEL_ORDER = [step.value for step in CheckoutStep]

#: The orders-api endpoint every money action posts to; the shell never
#: decides refunds locally.
ORDERS_ENDPOINT = "https://orders-api.internal.example/v1"


def next_step(current: str) -> str:
    """The step after *current* ("" at the end of the funnel)."""
    if current not in FUNNEL_ORDER:
        return ""
    index = FUNNEL_ORDER.index(current)
    return FUNNEL_ORDER[index + 1] if index + 1 < len(FUNNEL_ORDER) else ""
