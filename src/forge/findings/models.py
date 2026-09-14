"""forge-owned security findings store (v0.7, ADR-0021 §2).

On GitLab CE the native vulnerability store, the MR security widget and the
``/vulnerabilities`` state APIs are Ultimate — the ``gl-*-report.json``
artifacts are the only free surface, so forge parses them itself and owns
all triage state externally (docs/research/ci-security-surface.md §3.1,
§4.1, §7). On GitHub the alert APIs are read for free-tier-eligible repos
and forge mirrors them into the same table so the triage flow is
provider-neutral.

The dedupe key is ``uq_finding_per_source_scope_fingerprint`` over
(``source``, ``scope``, ``fingerprint``):

- **GitLab** (both scanners): *forge computes* the fingerprint — sha256
  over ``category`` + primary ``identifiers[].value`` + location hash
  (research §4.1: the report schema's ``id`` is scanner-assigned per scan
  and not stable across runs; CE users get none of GitLab Ultimate's
  internal location/track fingerprints, and the report JSON carries no
  fingerprint column).
- **GitHub**: the alert number per repository (research §3.2) — stable,
  server-assigned, and the exact handle the PATCH-dismiss endpoints take.

Absence semantics (research §4.1 + mission contract): a finding missing
from a later scan is NEVER auto-marked ``fixed`` — one scan's silence is
not evidence of a fix (branch-scoped scans, expired artifacts and scanner
outages all look identical to a fix). Only ``first_seen``/``last_seen``
move; ``status`` changes exclusively through triage or an external state
transition (e.g. a GitHub alert reporting ``fixed``/``dismissed``).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge.models.base import Base

#: Scanner/feed provenance. GitLab sources come from job-artifact reports
#: (CE free tier: SAST + Secret Detection run in Free; the report JSON of
#: the paid scanners never exists to parse), GitHub sources from the alert
#: REST APIs.
FINDING_SOURCES: tuple[str, ...] = (
    "gitlab_sast",
    "gitlab_secret",
    "github_code_scanning",
    "github_secret",
    "github_dependabot",
)

#: Normalized severity — GitLab's Title-Case enum (``Info, Unknown, Low,
#: Medium, High, Critical``, research §3.1) is lower-cased at ingest and
#: ``Unknown`` maps to ``info`` (the enum's floor); GitHub's
#: ``security_severity_level`` already uses this vocabulary.
FINDING_SEVERITIES: tuple[str, ...] = ("info", "low", "medium", "high", "critical")

#: Triage lifecycle. ``fixed`` and ``dismissed`` are set only by external
#: evidence (GitHub alert state) or an operator; the agent can move a
#: finding between ``open`` → ``triaged`` (confirmed real) and ``open`` →
#: ``false_positive``.
FINDING_STATUSES: tuple[str, ...] = (
    "open",
    "triaged",
    "fixed",
    "false_positive",
    "dismissed",
)

_INCOMING_SEVERITY_MAP: dict[str, str] = {
    "info": "info",
    "unknown": "info",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "critical": "critical",
    # GitHub code scanning also emits these rule-level values; the alert's
    # security_severity_level (mapped before this table) takes precedence.
    "warning": "low",
    "note": "info",
    "error": "high",
}


def normalize_severity(raw: str | None) -> str:
    """Map any provider severity string onto the closed forge enum."""
    return _INCOMING_SEVERITY_MAP.get((raw or "").strip().lower(), "info")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _status_check(name: str, values: tuple[str, ...]) -> CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"status IN ({allowed})", name=name)


class SecurityFinding(Base):
    """One deduplicated security finding with forge-owned triage state.

    ``scope`` carries the scan context's repo/project identity — the GitLab
    numeric project id as a string, or the GitHub ``owner/repo`` full name —
    so the unique index implements "per repo" for both providers with one
    column. ``ref``/``sha`` record where the finding was last observed;
    ``run_id`` optionally links the ingestion to a FlowRun (NULL for plain
    artifact scans, which happen outside any run).
    """

    __tablename__ = "security_findings"
    __table_args__ = (
        _status_check("ck_security_findings_status", FINDING_STATUSES),
        # The dedupe key (research §3/§4): one row per (source, repo, fingerprint).
        Index(
            "uq_finding_per_source_scope_fingerprint",
            "source",
            "scope",
            "fingerprint",
            unique=True,
        ),
        Index("ix_security_findings_scope_status", "scope", "status"),
        Index("ix_security_findings_provider", "provider"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    #: Optional FlowRun provenance — the run whose scan ingested the finding.
    run_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("flow_runs.id"),
        nullable=True,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    #: GitLab numeric project id (string) or GitHub ``owner/repo``.
    scope: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Ref/SHA the finding was last observed at (scan context).
    ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sha: Mapped[str | None] = mapped_column(String(40), nullable=True)

    source: Mapped[str] = mapped_column(String(40), nullable=False)
    #: sha256 hex (GitLab, forge-computed) or the GitHub alert number as a
    #: string — always stable within (source, scope).
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    severity: Mapped[str] = mapped_column(String(10), nullable=False, default="info")
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Provider identifiers (GitLab ``identifiers[]`` CVE/CWE entries, GitHub
    #: rule ids / GHSA ids / secret types) — display + fingerprint inputs.
    identifiers: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    triage_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
