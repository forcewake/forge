"""Q39-03 (#322) — the append-only credential-redemption audit ledger.

The recorded defect (review 6df4020, probe P04): the redemption endpoint
read the ENTIRE ``FlowRun.evidence`` document, appended the receipt and
wrote the whole JSON back — an unversioned full-document overwrite. A
concurrent native-handle / checkpoint / continuation write between the
read and the write was LOST (the SQLite schedule: dispatch wrote a new
attempt, the audit wrote back the stale ``harness`` copy), and the
last-50 cap on the embedded list was the ONLY audit history. This module
replaces that with two clean layers:

- **The ledger** — the ``credential_redemptions`` table (migration 028):
  INSERT-only, complete history, refs and metadata ONLY (no value slot,
  no value digest, nothing from which the credential could be
  reconstructed). A re-delivered receipt id bumps ``retry_count`` on the
  SAME logical row (a lost-response retry never inflates logical totals
  while each retry observation stays inspectable); a duplicate carrying
  CONFLICTING identity facts is a typed :class:`RedemptionConflict`,
  never a merge. ``grant_id`` is the join key to the operation grant
  (#Q39-01's grant type) — a plain string the grant owner populates;
  backfilled legacy rows carry it EMPTY with the fact labelled (a grant
  identity is never invented).

- **The projection** — the bounded ``run.evidence["credential_redemptions"]``
  list (the last 50, byte-compatible with the pre-table shape so every
  existing reader keeps working) rewritten through an OPTIMISTIC
  compare-and-swap over the stored evidence text: the writer re-reads on
  every conflict and retries, so a concurrent evidence writer can never
  be lost to the projection and the projection can never be lost to a
  CAS-participating writer. The decision — kept as a versioned
  projection rather than dropped in favor of a read-time query only —
  is deliberate: the operator surfaces (and the production-entry
  qualification) read the embedded list today, so the summary stays
  readable during migration while the TABLE is the authority; the
  read-time adapter :func:`recent_redemptions` serves the full history
  (no cap) to new readers. ``run.evidence["credential_redemptions_summary"]``
  carries the projection's observables — the TOTAL receipt count (the
  projection version, bumped by construction on every new receipt) and
  the cap — so ``credential.audit_backlog`` and
  ``run.evidence_projection_version`` have a durable home.

Retention is defined SEPARATELY from the summary cap
(:func:`retention_holds`, :func:`prune_expired_redemptions`): a receipt
row is deletable only when NO active attempt or investigation still
references its work/grant — an active run (non-terminal status) holds
its receipts forever, and an investigation hold (the run's evidence
carrying ``credential_investigation_open``) holds them regardless of
status. :func:`credential_audit_health` is the doctor-style check that
reports the backlog (un-backfilled embedded entries), held works and
prunable counts.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import bindparam, func, select, update

from forge.durable.models import CredentialRedemption, FlowRun

__all__ = [
    "AUDIT_WRITE_CONFLICT_EVENT",
    "AuditProjectionConflict",
    "BackfillReport",
    "CREDENTIAL_AUDIT_BACKLOG_EVENT",
    "LEGACY_EMBEDDED_PROVENANCE",
    "LIVE_PROVENANCE",
    "PROJECTION_LIMIT",
    "PROJECTION_CAS_ATTEMPTS",
    "RedemptionConflict",
    "RedemptionReceipt",
    "RetentionHolds",
    "backfill_embedded_redemptions",
    "credential_audit_health",
    "prune_expired_redemptions",
    "recent_redemptions",
    "record_redemption",
    "retention_holds",
]

logger = logging.getLogger(__name__)

#: The bounded projection's cap (the pre-table summary size — unchanged).
PROJECTION_LIMIT: int = 50
#: How many CAS rounds the projection attempts before refusing (a busy
#: evidence document under continuous concurrent writes; the ledger row
#: is already committed, the RESPONSE is what refuses — fail-closed).
PROJECTION_CAS_ATTEMPTS: int = 5

#: Observability labels (the backlog's names, surfaced in logs/doctor).
AUDIT_WRITE_CONFLICT_EVENT = "credential.audit_write_conflict"
CREDENTIAL_AUDIT_BACKLOG_EVENT = "credential.audit_backlog"

LIVE_PROVENANCE = "live"
LEGACY_EMBEDDED_PROVENANCE = "legacy-embedded"

#: The evidence key the investigation hold is recorded under (the
#: operator/IR surface sets it while an investigation is open).
INVESTIGATION_HOLD_EVIDENCE_KEY = "credential_investigation_open"

#: Keys a receipt document may NEVER carry — value material in any
#: spelling is refused, not scrubbed (the broker-audit doctrine: the
#: audit is centrally auditable OR the redemption does not happen).
_FORBIDDEN_DETAIL_KEYS = frozenset(
    {"value", "secret", "token", "password", "api_key", "credential_value", "staged_env"}
)


class RedemptionConflict(RuntimeError):
    """The same receipt id re-delivered with CONFLICTING identity facts.

    The standing row is never merged, never averaged and never rewritten:
    the duplicate is refused loudly (the caller turns it into a 5xx) and
    the FIRST logical redemption stands untouched.
    """


class AuditProjectionConflict(RuntimeError):
    """The evidence projection could not win its CAS within the budget.

    Raised only after the LEDGER row has committed (the audit fact is
    durable); what refuses is the RESPONSE — fail-closed: the projection
    and the value release share one all-or-nothing contract, so a busy
    evidence document can never yield a redemption whose summary write
    silently lost a concurrent writer's evidence.
    """


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _digest(material: str) -> str:
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


# ----------------------------------------------------------------------
# The receipt — refs and metadata ONLY
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class RedemptionReceipt:
    """One value-free redemption audit record.

    The typed fields are the queryable identity (work, grant, attempt
    generation, route, resolver lineage, outcome); ``audit`` carries the
    VERBATIM audit document the endpoint built (the exact pre-table
    entry shape, so the evidence projection stays byte-compatible with
    the legacy readers). There is deliberately NO field for the
    credential value, its digest, or any staged environment mapping.
    """

    receipt_id: str
    work_id: str
    audit: Mapping[str, Any] = field(default_factory=dict)
    grant_id: str = ""
    attempt_generation: int | None = None
    route: str = ""
    credential_ref: str = ""
    resolver: str = ""
    subject: str = ""
    provider: str = ""
    binding_revision: int | None = None
    outcome: str = "redeemed"
    retry_of: str | None = None
    expires_at: datetime | None = None
    broker_receipt_id: str = ""
    resolved_version_kind: str = ""
    credential_policy: str = ""
    provenance: str = LIVE_PROVENANCE
    created_at: datetime = field(default_factory=_utcnow)

    @classmethod
    def from_audit_entry(cls, work_id: str, entry: Mapping[str, Any]) -> RedemptionReceipt:
        """Build the receipt from the endpoint's audit entry (the R38-02
        shape: ``redemption_id`` / ``subject`` / ``provider`` /
        ``credential_ref`` / ``binding_revision`` / ``resolver`` /
        ``attempt_generation`` / ``expires_at`` / ``broker_receipt_id`` /
        ``resolved_version_kind`` / ``credential_policy`` / ``at``).

        The entry is stored verbatim as the projection document; the
        typed columns are derived from it. A grant id may ride the entry
        under ``grant_id`` (the Q39-01 grant owner mints it) — absent
        stays EMPTY (never invented).
        """
        expires_raw = str(entry.get("expires_at") or "")
        try:
            expires_at = datetime.fromisoformat(expires_raw) if expires_raw else None
        except ValueError:
            expires_at = None
        generation = entry.get("attempt_generation")
        binding = entry.get("binding_revision")
        return cls(
            receipt_id=str(entry.get("redemption_id") or ""),
            work_id=work_id,
            audit=dict(entry),
            grant_id=str(entry.get("grant_id") or ""),
            attempt_generation=int(generation) if isinstance(generation, int) else None,
            route=str(entry.get("provider") or ""),
            credential_ref=str(entry.get("credential_ref") or ""),
            resolver=str(entry.get("resolver") or ""),
            subject=str(entry.get("subject") or ""),
            provider=str(entry.get("provider") or ""),
            binding_revision=int(binding) if isinstance(binding, int) else None,
            outcome="redeemed",
            expires_at=expires_at,
            broker_receipt_id=str(entry.get("broker_receipt_id") or ""),
            resolved_version_kind=str(entry.get("resolved_version_kind") or ""),
            credential_policy=str(entry.get("credential_policy") or ""),
        )

    def validate(self) -> None:
        """Refuse malformed or value-bearing receipts BEFORE any write."""
        if not self.receipt_id or not self.work_id:
            raise ValueError("a redemption receipt needs a receipt_id and a work_id")
        if len(self.receipt_id) > 64 or len(self.work_id) > 32:
            raise ValueError(
                "a redemption receipt identity exceeds its durable width "
                "(receipt_id<=64, work_id<=32) — reject, never truncate"
            )
        lowered = {str(key).strip().lower(): key for key in self.audit}
        clash = sorted(lowered[key] for key in _FORBIDDEN_DETAIL_KEYS if key in lowered)
        if clash:
            raise ValueError(
                f"the redemption audit document carries value material under {clash} — "
                "the audit is refs and metadata ONLY; the redemption is refused"
            )


# ----------------------------------------------------------------------
# The ledger write — INSERT-only, retries counted never inflated
# ----------------------------------------------------------------------


def _identity_facts(receipt: RedemptionReceipt) -> tuple[Any, ...]:
    """The immutable logical-identity facts a retry must reproduce."""
    return (
        receipt.work_id,
        receipt.grant_id,
        receipt.route,
        receipt.credential_ref,
        receipt.resolver,
        receipt.subject,
        receipt.provider,
        receipt.binding_revision,
        receipt.outcome,
    )


def _standing_facts(row: CredentialRedemption) -> tuple[Any, ...]:
    return (
        row.work_id,
        row.grant_id,
        row.route,
        row.credential_ref,
        row.resolver,
        row.subject,
        row.provider,
        row.binding_revision,
        row.outcome,
    )


async def record_redemption(session_factory: Any, receipt: RedemptionReceipt) -> str:
    """Append ONE redemption receipt; return its receipt id.

    The ledger insert commits BEFORE this returns — the endpoint releases
    the credential bytes only afterwards (the audit-durable-before-value
    doctrine; a failure here refuses the redemption outright). A
    re-delivery of the SAME receipt id (a lost-response retry):

    - identical identity facts → the SAME logical row is referenced: an
      atomic ``retry_count + 1`` (plus ``last_retry_at``) — one
      redemption, N observable deliveries;
    - conflicting identity facts → :class:`RedemptionConflict` (never a
      merge, never a rewrite of history).

    After the ledger commit the bounded evidence projection is refreshed
    through the optimistic CAS (:func:`_project_summary`) — a projection
    failure refuses the response (:class:`AuditProjectionConflict`) even
    though the ledger row stands: the projection and the value release
    share the all-or-nothing contract.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    receipt.validate()
    inserted = await _insert_receipt(session_factory, receipt, pg_insert)
    if not inserted:
        await _record_retry_observation(session_factory, receipt)
    await _project_summary(session_factory, receipt.work_id)
    return receipt.receipt_id


async def _insert_receipt(session_factory: Any, receipt: RedemptionReceipt, pg_insert: Any) -> bool:
    async with session_factory() as session:
        result = await session.execute(
            pg_insert(CredentialRedemption)
            .values(
                receipt_id=receipt.receipt_id,
                work_id=receipt.work_id,
                grant_id=receipt.grant_id,
                attempt_generation=receipt.attempt_generation,
                route=receipt.route,
                credential_ref=receipt.credential_ref,
                resolver=receipt.resolver,
                subject=receipt.subject,
                provider=receipt.provider,
                binding_revision=receipt.binding_revision,
                outcome=receipt.outcome,
                retry_of=receipt.retry_of,
                retry_count=0,
                expires_at=receipt.expires_at,
                broker_receipt_id=receipt.broker_receipt_id,
                resolved_version_kind=receipt.resolved_version_kind,
                credential_policy=receipt.credential_policy,
                provenance=receipt.provenance,
                details=_details_of(receipt),
                created_at=receipt.created_at,
            )
            # INSERT-only: a duplicate delivery never rewrites the row —
            # the retry observation is a separate, conditional counter
            # bump. Portable across PostgreSQL and SQLite (tests).
            .on_conflict_do_nothing(index_elements=[CredentialRedemption.receipt_id])
        )
        await session.commit()
        return result.rowcount == 1  # type: ignore[attr-defined]


def _details_of(receipt: RedemptionReceipt) -> dict[str, Any]:
    details: dict[str, Any] = {"audit": dict(receipt.audit)}
    if not receipt.grant_id:
        # No grant identity existed for this redemption (pre-Q39-01 or a
        # backfilled legacy row): the absence is LABELLED, never invented.
        details["grant"] = "unattributed — no operation grant identity was recorded"
    return details


async def _record_retry_observation(session_factory: Any, receipt: RedemptionReceipt) -> None:
    """The lost-response retry path: count the delivery on the SAME row.

    The compare of the immutable identity facts happens INSIDE the
    conditional UPDATE's read; a conflicting duplicate raises
    :class:`RedemptionConflict` and nothing moves.
    """
    async with session_factory() as session:
        standing = await session.get(CredentialRedemption, receipt.receipt_id)
        if standing is None:  # pragma: no cover — the insert said duplicate
            raise RedemptionConflict(
                f"receipt {receipt.receipt_id!r} vanished between insert and retry read"
            )
        if _standing_facts(standing) != _identity_facts(receipt):
            raise RedemptionConflict(
                f"receipt {receipt.receipt_id!r} was re-delivered with conflicting "
                f"identity facts (standing work={standing.work_id!r} "
                f"ref={standing.credential_ref!r} resolver={standing.resolver!r}) — "
                "the first logical redemption stands; the duplicate is refused"
            )
        bumped = await session.execute(
            update(CredentialRedemption)
            .where(CredentialRedemption.receipt_id == receipt.receipt_id)
            .values(retry_count=CredentialRedemption.retry_count + 1, last_retry_at=_utcnow())
            .execution_options(synchronize_session=False)
        )
        if bumped.rowcount != 1:  # type: ignore[attr-defined]
            raise RedemptionConflict(
                f"receipt {receipt.receipt_id!r} could not record its retry observation"
            )
        await session.commit()
        logger.info(
            "credential redemption %s re-delivered (retry observation #%s) — "
            "the logical redemption is not re-counted",
            receipt.receipt_id,
            standing.retry_count + 1,
        )


# ----------------------------------------------------------------------
# The projection — a versioned, CAS-guarded evidence summary
# ----------------------------------------------------------------------


async def _project_summary(
    session_factory: Any, work_id: str, *, limit: int = PROJECTION_LIMIT
) -> None:
    """Rewrite the bounded evidence summary through an optimistic CAS.

    The summary document (``credential_redemptions`` = the last *limit*
    audit documents, ``credential_redemptions_summary`` = the projection
    observables) is written only when the stored evidence TEXT still
    equals the text this writer read — a concurrent evidence writer
    (native handle, checkpoint, continuation) makes the compare fail and
    the projection re-reads and retries. Nobody's evidence is ever lost
    to the summary write; a summary that cannot win within
    :data:`PROJECTION_CAS_ATTEMPTS` rounds refuses the response
    (:class:`AuditProjectionConflict`, logged as
    ``credential.audit_write_conflict``).
    """
    from sqlalchemy import cast, String

    for attempt in range(1, PROJECTION_CAS_ATTEMPTS + 1):
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(FlowRun.id, cast(FlowRun.evidence, String).label("raw")).where(
                        FlowRun.id == work_id
                    )
                )
            ).first()
            if row is None:
                raise AuditProjectionConflict(
                    f"work {work_id!r} disappeared before the audit projection"
                )
            old_raw: str | None = row.raw
            evidence: dict[str, Any] = (
                json.loads(old_raw) if isinstance(old_raw, str) and old_raw else {}
            )
            if not isinstance(evidence, dict):  # pragma: no cover — corrupt document
                raise AuditProjectionConflict(
                    f"work {work_id!r} carries a non-object evidence document — "
                    "the projection refuses to overwrite it"
                )
            total, entries = await _summary_rows(session, work_id, limit=limit)
            projected = dict(evidence)
            projected["credential_redemptions"] = entries
            projected["credential_redemptions_summary"] = {
                "total": total,
                "version": total,
                "capped_to": limit,
                "authority": "credential_redemptions",
            }
            guard = (
                cast(FlowRun.evidence, String) == bindparam("raw_text", old_raw)
                if old_raw is not None
                else FlowRun.evidence.is_(None)
            )
            won = await session.execute(
                update(FlowRun)
                .where(FlowRun.id == work_id, guard)
                .values(evidence=projected)
                .execution_options(synchronize_session=False)
            )
            if won.rowcount == 1:  # type: ignore[attr-defined]
                await session.commit()
                return
            await session.rollback()
            logger.warning(
                "%s: the evidence projection for work %s lost round %s/%s to a "
                "concurrent evidence writer — re-reading and retrying",
                AUDIT_WRITE_CONFLICT_EVENT,
                work_id,
                attempt,
                PROJECTION_CAS_ATTEMPTS,
            )
    raise AuditProjectionConflict(
        f"the evidence projection for work {work_id!r} could not win its CAS within "
        f"{PROJECTION_CAS_ATTEMPTS} rounds — a concurrent writer kept the document "
        "busy; the redemption is refused (the ledger row stands)"
    )


async def _summary_rows(
    session: Any, work_id: str, *, limit: int
) -> tuple[int, list[dict[str, Any]]]:
    """The full-history count plus the bounded, oldest→newest tail."""
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(CredentialRedemption)
            .where(CredentialRedemption.work_id == work_id)
        )
        or 0
    )
    tail = (
        (
            await session.execute(
                select(CredentialRedemption)
                .where(CredentialRedemption.work_id == work_id)
                .order_by(
                    CredentialRedemption.created_at.desc(),
                    CredentialRedemption.receipt_id.desc(),
                )
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    entries: list[dict[str, Any]] = []
    for row in reversed(tail):
        document = dict(row.details or {}).get("audit")
        entry = dict(document) if isinstance(document, Mapping) else {}
        entry.setdefault("redemption_id", row.receipt_id)
        if row.retry_count:
            entry.setdefault("delivery_count", row.retry_count + 1)
        entries.append(entry)
    return total, entries


# ----------------------------------------------------------------------
# The read adapter — the full history, no cap
# ----------------------------------------------------------------------


async def recent_redemptions(
    session_factory: Any, work_id: str, *, limit: int = PROJECTION_LIMIT
) -> list[dict[str, Any]]:
    """The work's most recent redemptions, newest first — the read-time
    adapter over the ledger (the operator surface's query; ``limit`` is a
    READ parameter, never a retention rule)."""
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(CredentialRedemption)
                    .where(CredentialRedemption.work_id == work_id)
                    .order_by(
                        CredentialRedemption.created_at.desc(),
                        CredentialRedemption.receipt_id.desc(),
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    records: list[dict[str, Any]] = []
    for row in rows:
        document = dict(row.details or {}).get("audit")
        record = dict(document) if isinstance(document, Mapping) else {}
        record.setdefault("redemption_id", row.receipt_id)
        record.update(
            {
                "receipt_id": row.receipt_id,
                "work_id": row.work_id,
                "grant_id": row.grant_id,
                "retry_count": row.retry_count,
                "retry_of": row.retry_of,
                "outcome": row.outcome,
                "provenance": row.provenance,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        )
        records.append(record)
    return records


# ----------------------------------------------------------------------
# Retention — deletable only when nothing active references the grant
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class RetentionHolds:
    """Why a work's receipts are NOT deletable right now.

    ``active_attempt`` — the run is in a NON-TERMINAL status (an active
    attempt may still need its redemption evidence for recovery or
    re-authorization); ``investigation`` — the operator/IR hold is open
    (``run.evidence["credential_investigation_open"]`` truthy). An empty
    ``reasons`` means the work's receipts are prunable.
    """

    work_id: str
    reasons: tuple[str, ...] = ()

    @property
    def held(self) -> bool:
        return bool(self.reasons)


async def retention_holds(session_factory: Any, work_id: str) -> RetentionHolds:
    """The retention decision for ONE work's receipts.

    The rule: a receipt row is deletable only when NO active attempt or
    investigation references its grant. Grants are work-scoped until the
    Q39-01 grant type lands its own lifecycle, so the work's run status
    plus the investigation hold ARE the reference check today; the
    grant-scoped refinement (an open grant row holding its receipts)
    slots into the same decision when the sibling's table exists.
    """
    async with session_factory() as session:
        run = await session.get(FlowRun, work_id)
    if run is None:
        return RetentionHolds(work_id=work_id, reasons=("unknown-work",))
    reasons: list[str] = []
    if run.status not in ("ready_for_human", "blocked", "failed", "cancelled"):
        reasons.append("active_attempt")
    evidence = run.evidence if isinstance(run.evidence, dict) else {}
    if evidence.get(INVESTIGATION_HOLD_EVIDENCE_KEY):
        reasons.append("investigation")
    return RetentionHolds(work_id=work_id, reasons=tuple(reasons))


async def prune_expired_redemptions(
    session_factory: Any,
    *,
    now: datetime | None = None,
    protected: Sequence[str] = (),
) -> dict[str, Any]:
    """Delete EXPIRED receipts whose work is under no retention hold.

    Only rows past ``expires_at`` AND whose work's
    :func:`retention_holds` decision is empty are deleted; every refusal
    is counted and reported (a cleanup never removes evidence an active
    attempt or investigation still requires). Returns the counts.
    """
    from sqlalchemy import delete

    moment = now or _utcnow()
    shielded = set(protected)
    deleted = 0
    held: dict[str, list[str]] = {}
    # Every hold decision is made BEFORE any delete executes: a refusal
    # never depends on a half-pruned state, and the read sessions close
    # before the write session opens its transaction.
    prunable: list[str] = []
    async with session_factory() as session:
        work_ids = (
            (
                await session.execute(
                    select(CredentialRedemption.work_id)
                    .where(CredentialRedemption.expires_at.is_not(None))
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
    for work_id in work_ids:
        if work_id in shielded:
            held.setdefault(work_id, []).append("operator-protected")
            continue
        decision = await retention_holds(session_factory, work_id)
        if decision.held:
            held.setdefault(work_id, []).extend(decision.reasons)
            continue
        prunable.append(work_id)
    if prunable:
        from sqlalchemy import delete

        async with session_factory() as session:
            for work_id in prunable:
                result = await session.execute(
                    delete(CredentialRedemption).where(
                        CredentialRedemption.work_id == work_id,
                        CredentialRedemption.expires_at.is_not(None),
                        CredentialRedemption.expires_at <= moment,
                    )
                )
                deleted += int(result.rowcount or 0)  # type: ignore[attr-defined]
            await session.commit()
    return {"deleted": deleted, "held_works": held, "now": moment.isoformat()}


# ----------------------------------------------------------------------
# The doctor-style check
# ----------------------------------------------------------------------


async def credential_audit_health(session_factory: Any) -> dict[str, Any]:
    """The operator's audit-health check (the doctor posture).

    Reports the legacy backlog — embedded ``credential_redemptions``
    evidence entries not yet backfilled into the ledger (the
    ``credential.audit_backlog`` observable, per work with counts) —
    beside the ledger totals and the backfill provenance. The evidence
    documents are read whole and counted in Python: the embedded lists
    are small by construction (the pre-table cap was 50) and a portable
    JSON path expression buys nothing here.
    """
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(FlowRun.id, FlowRun.evidence).where(FlowRun.evidence.is_not(None))
            )
        ).all()
        ledger_by_work = dict(
            (
                await session.execute(
                    select(CredentialRedemption.work_id, func.count()).group_by(
                        CredentialRedemption.work_id
                    )
                )
            ).all()
        )
        # The backlog is the count of embedded entries with NO ledger row
        # behind them yet (a live redemption's projection also writes the
        # embedded summary — those ARE backed by the ledger and are not a
        # backlog; the projection's cap means the embedded list can even
        # trail the ledger, so the backlog never goes negative).
        backlog: dict[str, int] = {}
        for work_id, evidence in rows:
            entries = (evidence or {}).get("credential_redemptions")
            if isinstance(entries, list) and entries:
                unbacked = max(0, len(entries) - int(ledger_by_work.get(str(work_id), 0)))
                if unbacked:
                    backlog[str(work_id)] = unbacked
        ledger_total = int(
            await session.scalar(select(func.count()).select_from(CredentialRedemption)) or 0
        )
        per_work_backfilled = (
            await session.execute(
                select(CredentialRedemption.work_id, func.count())
                .where(CredentialRedemption.provenance == LEGACY_EMBEDDED_PROVENANCE)
                .group_by(CredentialRedemption.work_id)
            )
        ).all()
    if backlog:
        logger.info(
            "%s: %s work(s) still carry embedded redemption evidence not yet "
            "backfilled into the ledger",
            CREDENTIAL_AUDIT_BACKLOG_EVENT,
            len(backlog),
        )
    return {
        "ledger_receipts": ledger_total,
        "embedded_backlog": backlog,
        "backfilled": {str(work): int(count) for work, count in per_work_backfilled},
    }


# ----------------------------------------------------------------------
# The legacy backfill — explicit provenance, no invented grants
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class BackfillReport:
    """What one backfill pass did — created, already-present, skipped."""

    works_scanned: int = 0
    created: int = 0
    already_present: int = 0
    skipped_entries: tuple[str, ...] = ()
    grant_ids_invented: int = 0  # always 0 — the invariant, reported

    def to_json(self) -> dict[str, Any]:
        return {
            "works_scanned": self.works_scanned,
            "created": self.created,
            "already_present": self.already_present,
            "skipped_entries": list(self.skipped_entries),
            "grant_ids_invented": self.grant_ids_invented,
        }


async def backfill_embedded_redemptions(session_factory: Any) -> BackfillReport:
    """Backfill the pre-table embedded evidence into the ledger.

    Every entry under ``run.evidence["credential_redemptions"]`` becomes
    a ledger row with ``provenance='legacy-embedded'``:

    - ``redemption_id`` present → the receipt id (idempotent re-runs
      re-read the same identity);
    - absent → a STABLE content digest over (work, position, content) —
      deterministic across re-runs, never a random id;
    - ``grant_id`` stays EMPTY with the fact labelled in ``details``
      (a grant identity is NEVER invented — the count is reported as 0
      by construction and asserted so).

    The embedded evidence itself is LEFT IN PLACE (the rollout contract:
    the old summary stays readable during migration; readers move to the
    ledger at their own pace). Value-bearing keys in a legacy entry skip
    the entry (counted in ``skipped_entries``) rather than being
    scrubbed into the ledger.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    created = already = 0
    skipped: list[str] = []
    works = 0
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(FlowRun.id, FlowRun.evidence).where(FlowRun.evidence.is_not(None))
            )
        ).all()
        for work_id, evidence in rows:
            entries = (evidence or {}).get("credential_redemptions")
            if not isinstance(entries, list) or not entries:
                continue
            works += 1
            for position, entry in enumerate(entries):
                if not isinstance(entry, Mapping):
                    skipped.append(f"{work_id}[{position}]:not-a-document")
                    continue
                receipt_id = str(entry.get("redemption_id") or "") or (
                    "legacy:" + _digest(f"{work_id}:{position}:{_canonical(dict(entry))}")
                )
                lowered = {str(key).strip().lower() for key in entry}
                if lowered & _FORBIDDEN_DETAIL_KEYS:
                    skipped.append(f"{work_id}[{position}]:value-material")
                    continue
                created_raw = str(entry.get("at") or "")
                try:
                    created_at = datetime.fromisoformat(created_raw) if created_raw else _utcnow()
                except ValueError:
                    created_at = _utcnow()
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                generation = entry.get("attempt_generation")
                binding = entry.get("binding_revision")
                result = await session.execute(
                    pg_insert(CredentialRedemption)
                    .values(
                        receipt_id=receipt_id[:64],
                        work_id=str(work_id),
                        grant_id="",
                        attempt_generation=int(generation) if isinstance(generation, int) else None,
                        route=str(entry.get("provider") or ""),
                        credential_ref=str(entry.get("credential_ref") or ""),
                        resolver=str(entry.get("resolver") or ""),
                        subject=str(entry.get("subject") or ""),
                        provider=str(entry.get("provider") or ""),
                        binding_revision=int(binding) if isinstance(binding, int) else None,
                        outcome="redeemed",
                        expires_at=None,
                        broker_receipt_id=str(entry.get("broker_receipt_id") or ""),
                        resolved_version_kind=str(entry.get("resolved_version_kind") or ""),
                        credential_policy=str(entry.get("credential_policy") or ""),
                        provenance=LEGACY_EMBEDDED_PROVENANCE,
                        details={
                            "audit": dict(entry),
                            "grant": (
                                "unattributed — legacy embedded evidence carries no "
                                "operation grant identity; none is invented"
                            ),
                            "backfilled_from": "flow_runs.evidence.credential_redemptions",
                            "backfill_position": position,
                        },
                        created_at=created_at,
                    )
                    .on_conflict_do_nothing(index_elements=[CredentialRedemption.receipt_id])
                )
                if result.rowcount == 1:  # type: ignore[attr-defined]
                    created += 1
                else:
                    already += 1
        await session.commit()
    return BackfillReport(
        works_scanned=works,
        created=created,
        already_present=already,
        skipped_entries=tuple(skipped),
    )
