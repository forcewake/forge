"""forge-owned security findings store (v0.7, ADR-0021 §2; R25/R26 governance).

On GitLab CE the native vulnerability store, the MR security widget and the
``/vulnerabilities`` state APIs are Ultimate — the ``gl-*-report.json``
artifacts are the only free surface, so forge parses them itself and owns
all triage state externally (docs/research/2026-09-14-ci-security-surface.md §3.1,
§4.1, §7). On GitHub the alert APIs are read for free-tier-eligible repos
and forge mirrors them into the same table so the triage flow is
provider-neutral.

The dedupe key is ``uq_finding_per_connection_source_scope_fingerprint`` over
(``connection_id``, ``source``, ``scope``, ``fingerprint``) — R26: the
*connection* leads the key because numeric subject ids are only unique
WITHIN a connection (two GitLab/GitHub connections can carry the same
project id; their findings must never mix, mirroring
``uq_active_run_per_issue``'s provider-led key):

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

Triage governance (R25) — the AI verdict is a SUGGESTION, never a status:

- ``suggested_verdict``/``suggested_at``/``suggested_by`` record what the
  security-triage agent proposed; the authoritative ``status`` moves only
  when an authorized actor confirms (``FORGE_SECURITY_TRIAGERS``) or the
  explicit opt-in auto-accept config (``FORGE_SECURITY_AUTO_ACCEPT``,
  default OFF) confirms on the operator's behalf.
- ``version`` is the optimistic-concurrency counter for compare-and-set
  verdict application: a manual status change during the LLM call bumps
  the version, so the stale batch's write-back misses and the manual
  decision wins.
- ``absent_in_last_scan`` feeds the reappearance rule: a suppressed
  (``false_positive``/``dismissed``/``fixed``) finding that reappears
  after having been absent gets a NEW evidence assessment — its old
  suppression is never silently restored (a finding suppressed, gone from
  the scans, then reintroduced must not stay hidden forever).

Every triage/reopen/remote-dismiss state change is journaled in
:class:`SecurityFindingAction` (intent first, outcome second — ADR-0005
shape for the findings surface); remote dismissal additionally requires
the ``FORGE_SECURITY_REMOTE_DISMISS`` grant and an authoritatively
confirmed false positive — a model suggestion alone can never close a
remote alert. Each ingestion pass records a :class:`ScanExecution` row so
a partial scan (403 surface, expired artifact, truncated pagination) is
visible as incomplete and never looks like a clean scan.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    JSON,
    Boolean,
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
#: evidence (GitHub alert state) or an operator; a confirmed AI verdict
#: moves ``open`` → ``triaged``/``false_positive`` — and only after the
#: suggestion was recorded separately (R25).
FINDING_STATUSES: tuple[str, ...] = (
    "open",
    "triaged",
    "fixed",
    "false_positive",
    "dismissed",
)

#: The verdicts the AI triage may SUGGEST (R25) — a strict subset of the
#: lifecycle that never includes terminal states.
SUGGESTED_VERDICTS: tuple[str, ...] = ("triaged", "false_positive")

#: Statuses a confirmed verdict may resolve FROM — the suppression states
#: whose reappearance triggers a fresh assessment instead of a silent
#: restore (R25 §4).
SUPPRESSED_STATUSES: tuple[str, ...] = ("false_positive", "dismissed", "fixed")

#: Findings governance journal actions (R25): every authoritative change
#: plus the AI suggestions is journaled with actor + justification.
FINDING_ACTIONS: tuple[str, ...] = (
    "suggest_verdict",
    "confirm_verdict",
    "reject_verdict",
    "reopen",
    "remote_dismiss",
)

#: Outcomes for journal rows — local changes land as ``applied``; the
#: privileged remote dismissal follows the intent-first pattern
#: (``requested`` → ``succeeded``/``failed``), and a compare-and-set miss
#: (a manual change won the race) journals as ``skipped``.
FINDING_ACTION_OUTCOMES: tuple[str, ...] = (
    "applied",
    "requested",
    "succeeded",
    "failed",
    "skipped",
)

#: Scan completeness (R26): ``complete`` = the surface produced a readable
#: report (even with zero findings — a clean report is evidence);
#: ``incomplete`` = the surface could not be read (403, expired artifact,
#: undecodable JSON, no report jobs at all); ``partial`` is reserved for a
#: surface read that is known to be truncated.
SCAN_COMPLETENESS: tuple[str, ...] = ("complete", "incomplete", "partial")

#: How long scan execution records are retained before they may be purged
#: (R26 "retention"): scan rows are observability metadata, not history.
SCAN_RETENTION_DAYS = 90

#: Bump when the ingest normalization contract changes — recorded on every
#: ScanExecution row so old rows can be re-derived or audited.
INGEST_VERSION = "1"

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


def _status_check(name: str, values: tuple[str, ...], column: str = "status") -> CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=name)


class SecurityFinding(Base):
    """One deduplicated security finding with forge-owned triage state.

    ``connection_id`` + ``scope`` carry the scan context's identity — the
    connection (e.g. ``github:{installation}:{repo}``, or "" for the
    deployment's single GitLab connection) plus the GitLab numeric project
    id as a string or the GitHub ``owner/repo`` full name — so the unique
    index implements "per connection and repo" for every provider with two
    columns (R26: same project id on different connections never mixes).
    ``ref``/``sha`` record where the finding was last observed;
    ``run_id`` optionally links the ingestion to a FlowRun (NULL for plain
    artifact scans, which happen outside any run).
    """

    __tablename__ = "security_findings"
    __table_args__ = (
        _status_check("ck_security_findings_status", FINDING_STATUSES),
        # R25: the AI verdict is a suggestion (NULL = none yet), never a
        # silent status write.
        CheckConstraint(
            "suggested_verdict IS NULL OR suggested_verdict IN ('triaged', 'false_positive')",
            name="ck_security_findings_suggested_verdict",
        ),
        # The dedupe key (research §3/§4 + R26): one row per connection,
        # source, repo and fingerprint.
        Index(
            "uq_finding_per_connection_source_scope_fingerprint",
            "connection_id",
            "source",
            "scope",
            "fingerprint",
            unique=True,
        ),
        Index("ix_security_findings_scope_status", "scope", "status"),
        Index("ix_security_findings_provider", "provider"),
        Index("ix_security_findings_connection_scope", "connection_id", "scope"),
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
    #: The provider connection the finding came from ("" = the deployment's
    #: single GitLab connection). Leads the dedupe key: two connections
    #: carrying the same project id never mix findings (R26).
    connection_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
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

    #: The AUTHORITATIVE triage state — moved only by a confirmed verdict,
    #: an operator, or external provider evidence; never by ingestion and
    #: never directly by the AI triage (R25).
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    #: The AI triage's SUGGESTION, recorded separately from ``status``
    #: (R25): NULL = no suggestion yet, otherwise 'triaged' or
    #: 'false_positive'. An authorized actor (FORGE_SECURITY_TRIAGERS) or
    #: the explicit auto-accept opt-in (FORGE_SECURITY_AUTO_ACCEPT, default
    #: OFF) promotes it to ``status``.
    suggested_verdict: Mapped[str | None] = mapped_column(String(20), nullable=True)
    suggested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Who made the suggestion (e.g. "ai:security-triage").
    suggested_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    triage_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Optimistic-concurrency counter (R25): bumped by every authoritative
    #: change (confirm/reject/reopen/remote dismiss). Verdict write-back
    #: pins the version observed at triage start and applies as
    #: compare-and-set, so a manual change during the LLM call can never be
    #: overwritten. Ingestion observability refreshes do NOT bump it.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: Presence tracking for the reappearance rule (R25 §4): True when the
    #: latest COMPLETE scan surface for this finding's source did not
    #: observe it. A suppressed finding flipping absent → present again is
    #: reopened for a fresh assessment instead of keeping its old
    #: suppression.
    absent_in_last_scan: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class ScanExecution(Base):
    """One ingestion pass over one provider surface, with its completeness.

    R26: a scan that could not fully read its surface (403 on a repo
    without Code Security, expired/missing artifact, undecodable report,
    truncated pagination) records ``completeness='incomplete'`` — a partial
    scan must never be mistaken for a clean one. One row per (pass,
    source): GitLab writes one per report job it attempted (and one
    ``gitlab_pipeline`` row when the pipeline carried no report jobs at
    all), GitHub one per alert surface.
    """

    __tablename__ = "security_scan_executions"
    __table_args__ = (
        _status_check("ck_scan_executions_completeness", SCAN_COMPLETENESS, column="completeness"),
        Index("ix_scan_executions_scope_observed", "connection_id", "scope", "observed_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    connection_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    scope: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The surface this pass covered (a FINDING_SOURCES entry, or the
    #: pass-level pseudo-surface ``gitlab_pipeline`` when no report jobs
    #: matched).
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    #: Provider scan identifier — the GitLab pipeline id, where applicable.
    external_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: Scanner version when the report carries one; ``ingest_version`` is
    #: forge's own normalization contract version (INGEST_VERSION).
    scanner_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ingest_version: Mapped[str] = mapped_column(String(10), nullable=False, default=INGEST_VERSION)

    completeness: Mapped[str] = mapped_column(String(20), nullable=False, default="complete")
    parsed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    run_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("flow_runs.id"),
        nullable=True,
        index=True,
    )

    #: When the scan was observed and how long this record may be kept
    #: (retention: observability metadata, not history).
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class SecurityFindingAction(Base):
    """Findings governance journal (R25): intent first, outcome second.

    Every authoritative change to a finding — suggestion, confirmation,
    rejection, reappearance reopen, remote dismissal — lands here with its
    actor and justification BEFORE (for remote effects) or with the change.
    Remote dismissal rows follow the ADR-0005 two-phase shape:
    ``requested`` is written strictly before the provider call, then
    completed with ``succeeded``/``failed`` — a model suggestion alone can
    never close a remote alert, and a lost outcome stays visible.
    """

    __tablename__ = "security_finding_actions"
    __table_args__ = (
        _status_check("ck_finding_actions_action", FINDING_ACTIONS, column="action"),
        _status_check("ck_finding_actions_outcome", FINDING_ACTION_OUTCOMES, column="outcome"),
        Index("ix_finding_actions_finding", "finding_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    finding_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("security_findings.id"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    #: Who acted: "ai:security-triage", a confirmed triager's username, or
    #: "auto_accept" (the explicit opt-in config speaking for the operator).
    actor: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    justification: Mapped[str] = mapped_column(Text, nullable=False, default="")
    before_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    after_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    #: Verdict snapshot / provider response / skip reason.
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, default="applied")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


def retention_from(observed_at: datetime) -> datetime:
    """The retention deadline for a scan observed at *observed_at*."""
    return observed_at + timedelta(days=SCAN_RETENTION_DAYS)
