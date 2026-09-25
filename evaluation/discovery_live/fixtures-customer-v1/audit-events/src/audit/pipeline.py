"""Audit-event pipeline primitives (audit-events service).

This module is the single owner of the event pipeline plumbing and of
the retention/emission RULES every emitting service must consult. The
rules live in the RETENTION AND EMISSION POLICY block below — readers
paging only the first screen of this file will see plumbing, not rules;
the policy block sits past it on purpose (the file is read in pages,
and the operative constants belong to the policy block, not to the
transport headers above it).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field

#: The audit clock and the stream naming convention (UTC; a stream is
#: per emitting service and per calendar day).
AUDIT_CLOCK = "UTC"
STREAM_NAME_TEMPLATE = "audit.{service}.{day:%Y%m%d}"

#: The canonical event envelope version this pipeline accepts.
ENVELOPE_VERSION = 2

#: Event kinds the pipeline currently distinguishes. Anything else is
#: rejected at ingestion (an unknown kind is a producer bug).
EVENT_KINDS = (
    "refund_requested",
    "refund_captured",
    "refund_rejected",
    "order_expired",
    "checkout_completed",
)

#: Maximum envelope size the ingester accepts in one put (bytes). The
#: pipeline never truncates an audit record silently — it refuses it.
MAX_ENVELOPE_BYTES = 32768


def stream_name(service: str, day: dt.date) -> str:
    """The stream an event lands in for *service* on *day*."""
    return STREAM_NAME_TEMPLATE.format(service=service.strip().lower(), day=day)


def envelope_digest(service: str, kind: str, payload: dict) -> str:
    """The content digest binding one event to its exact payload bytes."""
    canonical = json.dumps(
        {"service": service, "kind": kind, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuditEvent:
    """One accepted audit event, envelope and all."""

    service: str
    kind: str
    occurred_at: dt.datetime
    payload: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"unknown audit event kind {self.kind!r}")
        if self.occurred_at.tzinfo is None:
            raise ValueError("audit events must carry an aware timestamp")

    def envelope(self) -> dict:
        """The wire envelope the ingester expects."""
        return {
            "version": ENVELOPE_VERSION,
            "service": self.service,
            "kind": self.kind,
            "occurred_at": self.occurred_at.isoformat(),
            "payload": self.payload,
            "digest": envelope_digest(self.service, self.kind, self.payload),
        }


# ---------------------------------------------------------------------------
# RETENTION AND EMISSION POLICY (audit revision A-2026-02).
#
# The rules every EMITTING service must consult before it decides an
# event is optional. In particular refund flows: a refund request on an
# order that expired recently still owes an emission, and the window is
# measured in DAYS since order expiry, not since the request.
# ---------------------------------------------------------------------------


#: The CURRENT emission rule (A-2026-02): a refund event MUST be
#: emitted while the refund request is within this many days of the
#: ORDER's expiry; after that the trail is statement-only.
AUDIT_EMISSION_REQUIRED_AFTER_DAYS = 21

#: The current audit policy revision identifier.
AUDIT_POLICY_VERSION = "A-2026-02"

#: Whether late emissions (outside the required window) are accepted at
#: all — they are, flagged, because a late honest record beats none.
LATE_EMISSION_ACCEPTED = True


def emission_required(order_expired_at: dt.datetime, requested_at: dt.datetime) -> bool:
    """True while a refund event is inside the REQUIRED emission window."""
    age_days = (requested_at - order_expired_at).days
    return age_days <= AUDIT_EMISSION_REQUIRED_AFTER_DAYS


def emission_flag(order_expired_at: dt.datetime, requested_at: dt.datetime) -> str:
    """The required/late/optional marker an emitted event carries."""
    if emission_required(order_expired_at, requested_at):
        return "required"
    return "late" if LATE_EMISSION_ACCEPTED else "rejected"
