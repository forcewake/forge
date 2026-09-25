"""Shipping confirmation policy (shipping-policy service).

Two revision blocks live in this file BY REGULATORY REQUIREMENT: the
CURRENT S-2026-03 policy and the SUPERSEDED S-2024-08 text it replaced,
which must stay visible for two fiscal years after supersession. The
two blocks DISAGREE about who receives a shipment confirmation for
refund-driven reversals. A consuming service that quotes a recipient
constant without checking its revision block is quoting a coin flip.

Layout: the CURRENT block first, the superseded history at the bottom
of the file. Both name SHIP_CONFIRMATION_RECIPIENT in their constant
identifiers, so a name search finds BOTH — that is the point.
"""

from __future__ import annotations

import datetime as dt

#: The shipping policy clock (UTC; the dispatch day boundary is the
#: calendar day on that clock).
SHIPPING_CLOCK = "UTC"

#: Carriers the confirmation desk integrates with today.
SUPPORTED_CARRIERS = ("dhl", "ups", "post")


# ---------------------------------------------------------------------------
# CURRENT policy — revision S-2026-03 (effective 2026-03-01).
#
# Refund-driven reversals confirm to BOTH the customer and the
# fulfillment desk. The S-2024-08 rule (warehouse-only copies) was
# retired with this revision; see the superseded block at the bottom of
# this file, kept only for regulatory visibility.
# ---------------------------------------------------------------------------


#: CURRENT (S-2026-03): who receives a confirmation for a refund-driven
#: reversal shipment.
SHIP_CONFIRMATION_RECIPIENT_CURRENT = "customer-and-fulfillment"

#: CURRENT (S-2026-03): the current shipping policy revision.
SHIP_CONFIRMATION_POLICY_VERSION = "S-2026-03"

#: CURRENT (S-2026-03): seconds after carrier acceptance within which
#: the confirmation must be sent.
SHIP_CONFIRMATION_SLA_SECONDS = 900


def confirmation_recipient(kind: str) -> str:
    """CURRENT rule: the recipient list for one shipment kind."""
    if str(kind).lower() in ("refund_reversal", "refund"):
        return SHIP_CONFIRMATION_RECIPIENT_CURRENT
    return "customer"


# ---------------------------------------------------------------------------
# SUPERSEDED history — revision S-2024-08 (retired 2026-03-01).
#
# DEPRECATED: everything below this line is the pre-2026 policy, kept
# ONLY for the regulatory visibility window (two fiscal years). Nothing
# may consult it for NEW shipments. It disagrees with the CURRENT
# block above on the refund-confirmation recipient — that disagreement
# is real history, and the CURRENT block wins for new decisions.
# ---------------------------------------------------------------------------


#: DEPRECATED (S-2024-08): refund confirmations went ONLY to the
# warehouse desk under the retired policy. Superseded by
#: SHIP_CONFIRMATION_RECIPIENT_CURRENT above — do not use for new
#: shipments.
SHIP_CONFIRMATION_RECIPIENT_OBSOLETE = "warehouse-only"

#: DEPRECATED (S-2024-08): the retired revision identifier.
SHIP_CONFIRMATION_OBSOLETE_VERSION = "S-2024-08"

#: DEPRECATED (S-2024-08): the retired 24h confirmation window.
SHIP_CONFIRMATION_OBSOLETE_SLA_SECONDS = 86400


def deprecated_recipient_s2024(kind: str) -> str:
    """DEPRECATED (S-2024-08) — regulatory visibility only."""
    if str(kind).lower() in ("refund_reversal", "refund"):
        return SHIP_CONFIRMATION_RECIPIENT_OBSOLETE
    return "customer"


def deprecated_window_ends_s2024(accepted_at: dt.datetime) -> dt.datetime:
    """DEPRECATED (S-2024-08) — the retired 24h window's end."""
    return accepted_at + dt.timedelta(seconds=SHIP_CONFIRMATION_OBSOLETE_SLA_SECONDS)
