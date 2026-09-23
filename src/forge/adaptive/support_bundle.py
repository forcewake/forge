"""The exportable support bundle (R32-23) — one run's whole honest story.

When an operator (or an auditor, or a second responder) picks up a stuck
run, they need EVERYTHING the projection saw — not just the current
state. The support bundle is that evidence pack: the attempts history
with failed attempts PRESERVED (recovery never resets history — a
retried run still shows what failed and when), the control commands and
their deliveries, the checkpoints (ids and digests — never the blobs),
the verification records, the publication intents, and the projection
state the bundle was built under.

Two invariants, both pinned by tests:

- **explicit coverage** — every section carries a
  ``present | missing | unknown`` mark in the coverage map: a section the
  collector never looked at is ``unknown``, a section observed empty is
  ``missing``, and neither is ever filled or assumed. An outcome forge
  cannot prove (an attempt with no recorded outcome, a verification that
  came back inconclusive) is exported as ``unknown``, never normalized
  into a guess.
- **no credentials** — every section passes the audit-export redaction
  guard (imported, not copied); the digest covers the CONTENT (sections
  + projection state + source digest) and never the generation
  timestamp, so two builds from the same rows digest identically.

The bundle is a VALUE: :meth:`SupportBundle.build` derives it purely from
the same durable-row shapes :mod:`forge.adaptive.operator_view` reads, in
the same module version stamp style as the audit trail
(``forge.support.bundle/1``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from forge.adaptive.audit_export import _redact as redact
from forge.adaptive.operator_view import (
    OperatorProjection,
    WEDGED_AFTER,
    _first,
    _iso,
    _norm,
    initial_projection,
    render,
)

__all__ = [
    "BUNDLE_SCHEMA",
    "COVERAGE_SECTIONS",
    "SupportBundle",
    "bundle_digest",
]

#: The schema discriminator every bundle carries (versioned: a breaking
#: change to the bundle contract bumps the tag).
BUNDLE_SCHEMA: Final = "forge.support.bundle/1"

#: The row sections a bundle may carry, in coverage-map order. A section
#: key ABSENT from the rows is ``unknown`` (never observed); present but
#: empty is ``missing`` (observed, found nothing); non-empty is
#: ``present``. Missing data stays explicit, never filled.
COVERAGE_SECTIONS: Final[tuple[str, ...]] = (
    "attempts",
    "commands",
    "deliveries",
    "checkpoints",
    "verifications",
    "publications",
    "approvals",
    "questions",
)

#: The closed coverage vocabulary.
Coverage = str  # "present" | "missing" | "unknown"


def _attempt_document(row: Mapping[str, Any]) -> dict[str, Any]:
    """One attempt — outcome preserved verbatim; NO outcome → ``unknown``
    (an attempt whose ending forge cannot prove is exported as unknown,
    never guessed into failed or succeeded)."""
    status = _first(row, "status", "state", "outcome")
    return {
        "attempt_id": str(_first(row, "attempt_id", "id") or ""),
        "outcome": str(status) if status else "unknown",
        "started_at": _iso(row.get("started_at")),
        "updated_at": _iso(row.get("updated_at")),
        "generation": row.get("generation"),
    }


def _command_document(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "command_id": str(_first(row, "command_id", "id") or ""),
        "kind": str(row.get("kind") or ""),
        "status": str(_first(row, "status") or "") or "unknown",
        "actor_ref": str(_first(row, "actor_ref", "actor") or ""),
        "actor_origin": str(row.get("actor_origin") or ""),
        "sequence": row.get("sequence"),
        "created_at": _iso(row.get("created_at")),
        "applied_at": _iso(row.get("applied_at")),
    }


def _delivery_document(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "command_id": str(_first(row, "command_id") or ""),
        "recipient": str(row.get("recipient") or ""),
        "status": str(_first(row, "status") or "") or "unknown",
        "created_at": _iso(row.get("created_at")),
    }


def _checkpoint_document(row: Mapping[str, Any]) -> dict[str, Any]:
    """One checkpoint — the ID and the DIGEST, never the blob payload:
    the bundle points at the content-addressed store, it does not carry
    the workspace."""
    return {
        "checkpoint_id": str(_first(row, "checkpoint_id", "id") or ""),
        "digest": str(_first(row, "digest", "artifact_id") or ""),
        "committed_at": _iso(row.get("committed_at")),
        "activated_at": _iso(row.get("activated_at")),
        "fence": str(row.get("fence") or ""),
        "sequence": row.get("sequence"),
    }


def _verification_document(row: Mapping[str, Any]) -> dict[str, Any]:
    result = _first(row, "result", "outcome")
    return {
        "verification_id": str(_first(row, "verification_id", "id") or ""),
        "result": str(result) if result else "unknown",
        "candidate_sha": str(row.get("candidate_sha") or ""),
        "at": _iso(row.get("at")),
    }


def _publication_document(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "operation_key": str(_first(row, "operation_key", "id") or ""),
        "status": str(_first(row, "status") or "") or "unknown",
        "operation": str(row.get("operation") or ""),
        "target_ref": str(row.get("target_ref") or ""),
        "at": _iso(row.get("at")),
    }


def _approval_document(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "approved_by": str(_first(row, "approved_by", "approver") or ""),
        "generation": row.get("generation"),
        "at": _iso(row.get("at")),
        "consumed_at": _iso(row.get("consumed_at")),
    }


def _question_document(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "question_id": str(_first(row, "question_id", "id") or ""),
        "resolved": bool(row.get("resolved")),
        "at": _iso(row.get("at")),
    }


_SECTION_DOCUMENTS: Final[Mapping[str, Any]] = {
    "attempts": _attempt_document,
    "commands": _command_document,
    "deliveries": _delivery_document,
    "checkpoints": _checkpoint_document,
    "verifications": _verification_document,
    "publications": _publication_document,
    "approvals": _approval_document,
    "questions": _question_document,
}


def _section_rows(rows: Mapping[str, Any], section: str) -> list[Any]:
    """The section's rows as a list — an absent or null section is empty
    (which section it was stays the coverage map's fact, not this list's)."""
    if section not in rows:
        return []
    value = rows[section]
    return list(value) if isinstance(value, (list, tuple)) else []


def bundle_digest(run_id: str, rows: Mapping[str, Any], projection: OperatorProjection) -> str:
    """The content digest of a bundle: sections + projection state + the
    rows' source digest — NEVER the generation timestamp, so the same
    rows always digest identically whatever clock built the bundle."""
    content = {
        "schema": BUNDLE_SCHEMA,
        "run_id": run_id,
        "coverage": coverage_map(rows),
        **{
            section: [
                _SECTION_DOCUMENTS[section](_norm(row)) for row in _section_rows(rows, section)
            ]
            for section in COVERAGE_SECTIONS
            if section in rows
        },
        "projection": {
            "state": projection.state,
            "underlying_state": projection.underlying_state,
            "source_digest": projection.source_digest,
        },
    }
    canonical = json.dumps(redact(content), sort_keys=True, default=str, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def coverage_map(rows: Mapping[str, Any]) -> dict[str, str]:
    """The explicit evidence-coverage map: ``present`` / ``missing`` /
    ``unknown`` per section — a section the rows never carried is
    UNKNOWN (the collector never observed that source), an observed
    empty one is MISSING; nothing is ever filled or assumed."""
    coverage: dict[str, str] = {}
    for section in COVERAGE_SECTIONS:
        if section not in rows:
            coverage[section] = "unknown"
        elif rows[section]:
            coverage[section] = "present"
        else:
            coverage[section] = "missing"
    return coverage


@dataclass(frozen=True)
class SupportBundle:
    """One run's exportable evidence pack (``forge.support.bundle/1``)."""

    schema: str
    run_id: str
    generated_at: str
    digest: str
    coverage: dict[str, str]
    projection: dict[str, Any]
    attempts: tuple[dict[str, Any], ...]
    commands: tuple[dict[str, Any], ...]
    deliveries: tuple[dict[str, Any], ...]
    checkpoints: tuple[dict[str, Any], ...]
    verifications: tuple[dict[str, Any], ...]
    publications: tuple[dict[str, Any], ...]
    approvals: tuple[dict[str, Any], ...]
    questions: tuple[dict[str, Any], ...]

    @classmethod
    def build(
        cls,
        run_id: str,
        rows: Mapping[str, Any],
        *,
        now: datetime | str | None = None,
        wedged_after: timedelta = WEDGED_AFTER,
    ) -> SupportBundle:
        """Build the bundle for *run_id* from the durable-row shapes the
        operator view reads (see :mod:`forge.adaptive.operator_view`).

        The bundle carries ALL attempts — failed ones included: recovery
        never resets history. Every section is redacted; the coverage map
        keeps missing and unknown data explicit; the digest covers
        content, never the clock.
        """
        run = rows.get("run")
        run_view = _norm(run) if run is not None else {}
        if not run_view:
            raise ValueError("no run row — a bundle without a run is nothing")
        row_run_id = str(_first(run_view, "id", "run_id") or "")
        if row_run_id != run_id:
            raise ValueError(
                f"rows are for run {row_run_id!r}, not {run_id!r} — a bundle never mixes runs"
            )
        projection = initial_projection(rows, now, wedged_after=wedged_after)
        sections = {
            section: tuple(
                redact(_SECTION_DOCUMENTS[section](_norm(row)))
                for row in _section_rows(rows, section)
            )
            for section in COVERAGE_SECTIONS
            if section in rows
        }
        return cls(
            schema=BUNDLE_SCHEMA,
            run_id=run_id,
            generated_at=projection.computed_at,
            digest=bundle_digest(run_id, rows, projection),
            coverage=coverage_map(rows),
            projection=render(projection),
            attempts=sections.get("attempts", ()),
            commands=sections.get("commands", ()),
            deliveries=sections.get("deliveries", ()),
            checkpoints=sections.get("checkpoints", ()),
            verifications=sections.get("verifications", ()),
            publications=sections.get("publications", ()),
            approvals=sections.get("approvals", ()),
            questions=sections.get("questions", ()),
        )

    def as_document(self) -> dict[str, Any]:
        """The exportable JSON document (redaction already applied per
        section at build; assembled here in coverage-map order)."""
        return {
            "schema": self.schema,
            "run_id": self.run_id,
            "generated_at": self.generated_at,
            "digest": self.digest,
            "coverage": dict(self.coverage),
            "projection": dict(self.projection),
            "attempts": [dict(row) for row in self.attempts],
            "commands": [dict(row) for row in self.commands],
            "deliveries": [dict(row) for row in self.deliveries],
            "checkpoints": [dict(row) for row in self.checkpoints],
            "verifications": [dict(row) for row in self.verifications],
            "publications": [dict(row) for row in self.publications],
            "approvals": [dict(row) for row in self.approvals],
            "questions": [dict(row) for row in self.questions],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_document(), indent=2, sort_keys=True) + "\n"
