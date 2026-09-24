"""The exportable project audit trail (R28-25) — the SIEM-ready surface.

Enterprise onboarding needs an audit trail a customer can EXPORT and
retain: every pause, resume, unknown effect and human approval for a
project's runs, in order, with per-entry actor, action, timestamp and
outcome. This module READS the run history — it derives the trail from
the journals forge already keeps durably, it never adds a second
bookkeeping path that could drift from the truth:

- ``flow_runs`` — the run lifecycle itself (status, evidence, the
  requesting actor journaled at creation, R28-23);
- ``outbox`` (``flow.transition`` events) — the ordered status ladder;
- ``gate_approvals`` — the human decisions, recorded and consumed;
- ``control_commands`` — the steering journal (pause/resume/steer/…,
  actor-scoped, with the row's append-only journal inlined);
- ``publication_intents`` — the remote publication effects, including
  the honest ``unknown`` outcomes;
- ``action_log`` — the external-write journal (requested → succeeded /
  failed / unknown_outcome).

:func:`audit_trail_for_project` assembles a chronological
:class:`AuditTrail`; ``as_document()``/``to_json()`` render the
exportable JSON. Two invariants the tests pin:

- NO CREDENTIAL VALUES: every detail dict passes a redaction guard that
  drops token/secret/password/key-looking VALUES (issue 183's
  secret-scanning canary) — refs and names survive, values never do;
  NEXT-19 (#207) adds the SCHEMA-BASED allowlist above the redaction:
  a credential binding/proof/receipt document serializes ONLY its
  declared fields, and every dropped field is named as a
  ``credential.export_field_dropped`` finding at the trail level;
- honest unknowns: an outcome forge cannot prove (publication
  ``unknown``, action ``unknown_outcome``) is EXPORTED as unknown,
  never normalized to success.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.adaptive.mailbox_db import ControlCommandRow
from forge.durable import ActionLog, FlowRun, GateApproval, Outbox, PublicationIntent

__all__ = [
    "AUDIT_SCHEMA",
    "CREDENTIAL_DOCUMENT_FIELDS",
    "EXPORT_FIELD_DROPPED",
    "AuditEntry",
    "AuditTrail",
    "audit_trail_for_project",
]

#: The schema discriminator every exported trail carries (versioned: a
#: breaking change to the export contract bumps the tag).
AUDIT_SCHEMA = "forge.project.audit-trail/1"

#: Keys whose VALUES are credential material by convention — dropped by
#: the redaction guard regardless of what they contain. The guard also
#: drops string values that LOOK like pasted secrets.
_REDACTED_KEY_RE = re.compile(
    r"(token|secret|password|passwd|api[-_]?key|credential|auth)$", re.IGNORECASE
)
_SECRET_VALUE_RE = re.compile(r"(Bearer\s+\S|sk-[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{8,}|glpat-)")

#: The named finding every dropped field leaves behind (NEXT-19/#207).
EXPORT_FIELD_DROPPED = "credential.export_field_dropped"

#: The per-schema EXPORT ALLOWLISTS (NEXT-19/#207): a document carrying
#: one of these schema discriminators serializes ONLY its declared
#: fields — an unknown or nested secret-shaped field is DROPPED with the
#: named finding, never rescued by a regex. This is the schema-based
#: half of the export guard; :func:`_redact` stays beneath it as the
#: value-shaped backstop (a sentinel smuggled into a DECLARED field —
#: e.g. a receipt whose ``resolved_version`` carries a pasted key — is
#: still redacted there).
CREDENTIAL_DOCUMENT_FIELDS: dict[str, frozenset[str]] = {
    "forge.project.credential-binding/1": frozenset(
        {
            "schema",
            "project_id",
            "provider",
            "credential_ref",
            "env_var",
            "bound_at",
            "bound_by",
            "revoked_at",
        }
    ),
    "forge.project.credential-binding/2": frozenset(
        {
            "schema",
            "subject",
            "provider",
            "credential_ref",
            "env_var",
            "bound_at",
            "bound_by",
            "revision",
            "project_id",
            "revoked_at",
        }
    ),
    "forge.project.dispatch-credential-proof/1": frozenset(
        {
            "schema",
            "project_id",
            "provider",
            "credential_ref",
            "env_var",
            "resolved_at",
            "bound_at",
            "bound_by",
        }
    ),
    "forge.project.dispatch-credential-proof/2": frozenset(
        {
            "schema",
            "subject",
            "provider",
            "credential_ref",
            "env_var",
            "binding_revision",
            "resolved_at",
            "bound_at",
            "bound_by",
            "resolver_identity",
            "resolved_version",
            "grant",
            "receipt",
            "attempt_generation",
        }
    ),
    "forge.credential.broker-receipt/1": frozenset(
        {
            "schema",
            "resolver_identity",
            "provider_route",
            "credential_ref",
            "env_var",
            "env_present",
            "resolved_version",
            "resolved_at",
        }
    ),
}

#: Fields the allowlisted documents promote to ``unknown`` when absent —
#: unresolved attribution stays unknown in exports, never promoted to a
#: claim (and never silently omitted either).
_UNKNOWN_PROMOTIONS: dict[str, tuple[str, ...]] = {
    "forge.project.dispatch-credential-proof/2": ("resolver_identity", "resolved_version"),
    "forge.credential.broker-receipt/1": (
        "resolver_identity",
        "resolved_version",
        "provider_route",
    ),
}


def _apply_document_allowlist(value: Any, findings: list[str]) -> Any:
    """The schema-based export guard (recursive, copy-on-write).

    A dict whose ``schema`` key names an allowlisted document keeps ONLY
    its declared fields — every other key (an injected sentinel under an
    unexpected nested name, an error payload smuggled beside the record,
    a value-shaped surprise inside the receipt itself) is DROPPED and
    named as a ``credential.export_field_dropped`` finding. Absent
    attribution fields are promoted to ``unknown``. Documents without a
    known schema discriminator pass through untouched (the redaction
    backstop below still applies to them).
    """
    if isinstance(value, dict):
        schema = str(value.get("schema") or "")
        declared = CREDENTIAL_DOCUMENT_FIELDS.get(schema)
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if declared is not None and str(key) not in declared:
                findings.append(f"{EXPORT_FIELD_DROPPED}:{schema}:{key}")
                continue
            cleaned[key] = _apply_document_allowlist(item, findings)
        if declared is not None:
            for field in _UNKNOWN_PROMOTIONS.get(schema, ()):
                if not str(cleaned.get(field) or "").strip():
                    cleaned[field] = "unknown"
        return cleaned
    if isinstance(value, list):
        return [_apply_document_allowlist(item, findings) for item in value]
    return value


def _export_detail(detail: dict[str, Any], findings: list[str] | None = None) -> dict[str, Any]:
    """One entry's detail as exportable: allowlist first, redaction second."""
    collected: list[str] = findings if findings is not None else []
    return _redact(_apply_document_allowlist(detail, collected))


def _is_credential_document(value: Any) -> bool:
    """Whether *value* is an ALREADY-ALLOWLISTED schema-discriminated
    credential document — a known schema discriminator AND no keys
    beyond the declared set (:func:`_export_detail` guarantees that
    upstream; a raw doc smuggled to :func:`_redact` from another caller
    fails this check and falls to the ordinary key regex, conservative
    exactly as before)."""
    if not isinstance(value, dict):
        return False
    declared = CREDENTIAL_DOCUMENT_FIELDS.get(str(value.get("schema") or ""))
    return declared is not None and set(value) <= set(declared)


def _redact(value: Any) -> Any:
    """The export-side credential guard (recursive, copy-on-write).

    NEXT-19 (#207): a schema-discriminated credential document is
    refs/metadata by construction and was already trimmed to its
    DECLARED fields by the allowlist — the KEY regex does not apply to
    it (its keys are declared field names like ``credential_ref``, and a
    parent key like ``dispatch_credential`` must not erase the whole
    audit record), while the VALUE regex still runs on every string it
    carries: a sentinel smuggled into a declared field is redacted
    exactly like anywhere else.
    """
    if isinstance(value, dict) and _is_credential_document(value):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, dict):
        return {
            key: (
                "[redacted]"
                if _REDACTED_KEY_RE.search(str(key)) and not _is_credential_document(item)
                else _redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        return "[redacted]"
    return value


def _iso(moment: datetime | None) -> str:
    """Normalized UTC ISO-8601 — naive DB datetimes read as UTC (sqlite
    stores what forge wrote: UTC)."""
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


#: The SYSTEM actor for rows no human authored (transitions, publication
#: legs, external-write attempts). Human surfaces (gate approvals,
#: control commands, the requesting actor) name their human.
SYSTEM_ACTOR = "system:forge"


@dataclass(frozen=True)
class AuditEntry:
    """One chronological audit record: WHO did WHAT, WHEN, with WHAT
    outcome — plus the run it belongs to and the journal it was derived
    from (provenance beats paraphrase)."""

    timestamp: str
    actor: str
    action: str
    outcome: str
    run_id: str
    source: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_document(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "actor": self.actor,
            "action": self.action,
            "outcome": self.outcome,
            "run_id": self.run_id,
            "source": self.source,
            "detail": _export_detail(self.detail),
        }


@dataclass(frozen=True)
class AuditTrail:
    """The exportable audit trail for one project."""

    project_id: int
    entries: tuple[AuditEntry, ...]
    generated_at: str

    def as_document(self) -> dict[str, Any]:
        findings: list[str] = []
        entries = []
        for entry in self.entries:
            entry_findings: list[str] = []
            document = {
                "timestamp": entry.timestamp,
                "actor": entry.actor,
                "action": entry.action,
                "outcome": entry.outcome,
                "run_id": entry.run_id,
                "source": entry.source,
                "detail": _export_detail(entry.detail, entry_findings),
            }
            findings.extend(entry_findings)
            entries.append(document)
        return {
            "schema": AUDIT_SCHEMA,
            "project_id": self.project_id,
            "generated_at": self.generated_at,
            "entry_count": len(self.entries),
            "entries": entries,
            # NEXT-19 (#207): the named export findings — every field the
            # schema allowlist dropped (``credential.export_field_
            # dropped:<schema>:<field>``). Names only: a finding never
            # carries the dropped content.
            "findings": sorted(set(findings)),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_document(), indent=2, sort_keys=True) + "\n"

    def actions(self) -> list[str]:
        return [entry.action for entry in self.entries]


def _run_requested_by(run: FlowRun) -> str:
    actor = str((run.evidence or {}).get("requested_by") or "").strip()
    return actor or SYSTEM_ACTOR


async def _collect(session: AsyncSession, project_id: int) -> list[AuditEntry]:
    """Read every journaled surface for the project's runs (one pass per
    table, Python-side assembly — export volume is report-sized, not
    OLTP-sized)."""
    runs: list[FlowRun] = list(
        (
            (await session.execute(select(FlowRun).where(FlowRun.project_id == project_id)))
            .scalars()
            .all()
        )
    )
    run_ids = {run.id for run in runs}
    if not run_ids:
        return []

    entries: list[AuditEntry] = []

    for run in runs:
        entries.append(
            AuditEntry(
                timestamp=_iso(run.created_at),
                actor=_run_requested_by(run),
                action="run.recorded",
                outcome=run.status,
                run_id=run.id,
                source="flow_runs",
                detail={
                    "issue_iid": run.issue_iid,
                    "provider": run.provider,
                    "status_reason": run.status_reason,
                    "commit_cycle": run.commit_cycle,
                    "spec_digest": run.spec_digest,
                    "plan_digest": run.plan_digest,
                    "evidence": dict(run.evidence or {}),
                },
            )
        )

    transitions = (
        (
            await session.execute(
                select(Outbox)
                .where(Outbox.flow_run_id.in_(run_ids), Outbox.event_type == "flow.transition")
                .order_by(Outbox.id)
            )
        )
        .scalars()
        .all()
    )
    for row in transitions:
        payload = dict(row.payload or {})
        entries.append(
            AuditEntry(
                timestamp=_iso(row.created_at),
                actor=SYSTEM_ACTOR,
                action="run.transition",
                outcome=str(payload.get("to") or ""),
                run_id=str(row.flow_run_id or payload.get("flow_run_id") or ""),
                source="outbox",
                detail={"from": payload.get("from"), "reason": payload.get("reason")},
            )
        )

    approvals = (
        (
            await session.execute(
                select(GateApproval)
                .where(GateApproval.flow_run_id.in_(run_ids))
                .order_by(GateApproval.id)
            )
        )
        .scalars()
        .all()
    )
    for row in approvals:
        entries.append(
            AuditEntry(
                timestamp=_iso(row.created_at),
                actor=f"approver:{row.approver_user_id}",
                action="gate.approval_recorded",
                outcome="consumed" if row.consumed_at else "pending",
                run_id=row.flow_run_id,
                source="gate_approvals",
                detail={
                    "generation": row.generation,
                    "plan_digest": row.plan_digest,
                    "spec_digest": row.spec_digest,
                    "consumed_at": _iso(row.consumed_at),
                },
            )
        )
        if row.consumed_at is not None:
            entries.append(
                AuditEntry(
                    timestamp=_iso(row.consumed_at),
                    actor=f"approver:{row.approver_user_id}",
                    action="gate.approval_consumed",
                    outcome="consumed",
                    run_id=row.flow_run_id,
                    source="gate_approvals",
                    detail={"generation": row.generation},
                )
            )

    commands = (
        (
            await session.execute(
                select(ControlCommandRow)
                .where(ControlCommandRow.run_id.in_(run_ids))
                .order_by(ControlCommandRow.sequence)
            )
        )
        .scalars()
        .all()
    )
    for row in commands:
        entries.append(
            AuditEntry(
                timestamp=_iso(row.created_at),
                actor=row.actor_ref,
                action=f"control.{row.kind}",
                outcome=row.status,
                run_id=str(row.run_id or ""),
                source="control_commands",
                detail={
                    "command_id": row.id,
                    "work_id": row.work_id,
                    "sequence": row.sequence,
                    "actor_origin": row.actor_origin,
                    "applied_at": _iso(row.applied_at),
                    "journal": list(row.journal or []),
                },
            )
        )

    intents = (
        (
            await session.execute(
                select(PublicationIntent)
                .where(PublicationIntent.run_id.in_(run_ids))
                .order_by(PublicationIntent.created_at)
            )
        )
        .scalars()
        .all()
    )
    for row in intents:
        entries.append(
            AuditEntry(
                timestamp=_iso(row.created_at),
                actor=SYSTEM_ACTOR,
                action="publication.intent",
                outcome=row.status,  # unknown stays unknown, never success
                run_id=row.run_id,
                source="publication_intents",
                detail={
                    "operation": row.operation,
                    "target_ref": row.target_ref,
                    "operation_key": row.operation_key,
                    "commit_cycle": row.commit_cycle,
                    "attempt_count": row.attempt_count,
                    "provider_object_id": row.provider_object_id,
                },
            )
        )

    actions = (
        (
            await session.execute(
                select(ActionLog).where(ActionLog.flow_run_id.in_(run_ids)).order_by(ActionLog.id)
            )
        )
        .scalars()
        .all()
    )
    for row in actions:
        entries.append(
            AuditEntry(
                timestamp=_iso(row.created_at),
                actor=SYSTEM_ACTOR,
                action=f"action.{row.action_kind}",
                outcome=row.status,  # unknown_outcome exported verbatim
                run_id=str(row.flow_run_id or ""),
                source="action_log",
                detail={"correlation_id": row.correlation_id},
            )
        )

    return entries


async def audit_trail_for_project(
    project_id: int, session_factory: async_sessionmaker[AsyncSession]
) -> AuditTrail:
    """Assemble the chronological, exportable audit trail for one project.

    Reads only (no writes, no locks): open one session, collect every
    journaled surface scoped to the project's runs, sort by timestamp
    (stable — same-timestamp entries keep their journal order), and
    stamp the generation time. The result is a value: render it with
    ``as_document()``/``to_json()``.
    """
    async with session_factory() as session:
        entries = await _collect(session, project_id)
    ordered = sorted(entries, key=lambda entry: entry.timestamp)
    return AuditTrail(
        project_id=project_id,
        entries=tuple(ordered),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
