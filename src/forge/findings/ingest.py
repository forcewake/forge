"""Security findings ingestion: GitLab CE artifacts + GitHub alert APIs.

Both adapters normalize into :class:`forge.findings.models.SecurityFinding`
rows, deduped by (connection, source, scope, fingerprint). Upsert semantics
(R26): the dedupe is DB-native — a single ``INSERT … ON CONFLICT DO
UPDATE`` against the unique index, so two concurrent ingests of the same
finding collapse to one row at the database instead of racing a
SELECT+INSERT pair:

- **new** fingerprint → row created with ``status='open'``;
- **seen** fingerprint → scan-time fields (severity, title, path, line,
  identifiers, ref/sha, ``last_seen``) refresh — ``last_seen`` moves
  monotonically forward (never backwards under a late/reordered scan) —
  but ``status``, ``triage_note``, ``first_seen`` and the concurrency
  ``version`` are NEVER touched by ingestion — triage state is forge's,
  and one scan's silence is not evidence of a fix (a finding absent from
  the latest scan keeps its status; only present findings get
  ``last_seen`` updates). External evidence moves status: a GitHub alert
  listing answers ``state=open`` filtered, so mirrored alerts that GitHub
  itself marks ``fixed``/``dismissed`` are simply absent — again NOT
  promoted to ``fixed`` by forge.

Every pass also records :class:`~forge.findings.models.ScanExecution`
rows (one per surface) so a partial scan — a 403 surface, an expired
artifact, an undecodable report, a pipeline with no report jobs at all —
is visible as ``completeness='incomplete'`` and never looks like a clean
scan (R26). A surface that read COMPLETELY drives presence tracking:
findings its source did not report are flagged ``absent_in_last_scan``,
and a SUPPRESSED finding that reappears after being absent is reopened
for a fresh evidence assessment (R25 §4) — its old suppression is never
silently restored.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import case, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.context.security_report import parse_gitlab_security_report
from forge.findings.fingerprints import gitlab_fingerprint, github_fingerprint
from forge.findings.models import (
    INGEST_VERSION,
    SUPPRESSED_STATUSES,
    ScanExecution,
    SecurityFinding,
    SecurityFindingAction,
    normalize_severity,
    retention_from,
)

logger = logging.getLogger(__name__)

#: GitLab job-name → (source, report artifact). Free/CE tier only (research
#: §3.1 tier matrix): SAST and Secret Detection run in Free; the paid
#: scanners' reports never exist as artifacts to parse.
_GITLAB_SAST_RE = re.compile(r"(^|[-_])sast$", re.IGNORECASE)
_GITLAB_SECRET_RE = re.compile(r"secret[-_]detection", re.IGNORECASE)
_GITLAB_REPORTS: tuple[tuple[Any, str, str], ...] = (
    (_GITLAB_SAST_RE, "gitlab_sast", "gl-sast-report.json"),
    (_GITLAB_SECRET_RE, "gitlab_secret", "gl-secret-detection-report.json"),
)


class _GitLabTransport(Protocol):
    """The slice of GitLabClient the artifact ingestion needs."""

    async def list_pipeline_jobs(self, project_id: int, pipeline_id: int) -> list[Any]: ...

    async def get_job_artifacts_file(
        self, project_id: int, job_id: int, artifact_path: str
    ) -> bytes: ...


class _GitHubTransport(Protocol):
    """The slice of GitHubClient the alert ingestion needs."""

    async def list_code_scanning_alerts(
        self, owner: str, repo: str, *, state: str = "open"
    ) -> list[dict[str, Any]]: ...

    async def list_secret_scanning_alerts(
        self, owner: str, repo: str, *, state: str = "open"
    ) -> list[dict[str, Any]]: ...

    async def list_dependabot_alerts(
        self, owner: str, repo: str, *, state: str = "open"
    ) -> list[dict[str, Any]]: ...


@dataclass
class NormalizedFinding:
    """Provider-neutral finding ready for the upsert."""

    source: str
    fingerprint: str
    severity: str
    title: str
    path: str | None = None
    line: int | None = None
    identifiers: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class IngestResult:
    """What one ingestion pass did (per-source breakdown included)."""

    created: int = 0
    updated: int = 0
    parsed: int = 0
    errors: list[str] = field(default_factory=list)
    #: R26: False when ANY attempted surface could not be fully read — a
    #: partial scan must never look clean.
    scan_complete: bool = True
    #: Per-source counts for the ScanExecution rows.
    created_by_source: dict[str, int] = field(default_factory=dict)
    updated_by_source: dict[str, int] = field(default_factory=dict)
    #: Sources whose surface read COMPLETELY this pass (presence tracking
    #: only drives from fully-read surfaces).
    complete_sources: list[str] = field(default_factory=list)

    def merge(self, other: IngestResult) -> IngestResult:
        return IngestResult(
            created=self.created + other.created,
            updated=self.updated + other.updated,
            parsed=self.parsed + other.parsed,
            errors=self.errors + other.errors,
        )


# ----------------------------------------------------------------------
# Core upsert (the dedupe authority — DB-native, R26)
# ----------------------------------------------------------------------


def _conflict_insert(session: AsyncSession) -> Any:
    """The dialect insert() with ON CONFLICT support for *session*'s bind.

    Both production (Postgres) and dev/test (SQLite) dialects implement
    the standard ``ON CONFLICT DO UPDATE`` clause; anything else refuses
    loudly rather than degrading to a racy SELECT+INSERT.
    """
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        return pg_insert
    if dialect == "sqlite":
        return sqlite_insert
    raise RuntimeError(f"findings upsert: no ON CONFLICT support for dialect {dialect!r}")


def _dedupe_in_batch(findings: list[NormalizedFinding]) -> list[NormalizedFinding]:
    """Collapse duplicate fingerprints within one batch (last one wins).

    A single INSERT … ON CONFLICT may not touch the same row twice
    (Postgres rejects it outright), and the previous cache-based loop also
    let the last occurrence win — identical behavior, one statement.
    """
    deduped: dict[tuple[str, str], NormalizedFinding] = {}
    for finding in findings:
        deduped[(finding.source, finding.fingerprint)] = finding
    return list(deduped.values())


async def upsert_findings(
    session: AsyncSession,
    findings: list[NormalizedFinding],
    *,
    provider: str,
    scope: str,
    connection_id: str = "",
    ref: str | None = None,
    sha: str | None = None,
    run_id: str | None = None,
    seen_at: datetime | None = None,
) -> IngestResult:
    """Upsert normalized findings for (connection, provider, scope).

    Runs inside the caller's transaction and issues ONE
    ``INSERT … ON CONFLICT DO UPDATE`` statement: the unique index
    ``(connection_id, source, scope, fingerprint)`` is the dedupe
    authority, so concurrent ingests of the same finding collapse to a
    single row without any SELECT+INSERT race (R26). Existing rows keep
    their ``status``/``triage_note``/``first_seen``/``version`` — a scan
    can only refresh observability fields (with a monotonic ``last_seen``),
    never overturn or stale-out a triage verdict.
    """
    seen_at = seen_at or datetime.now(timezone.utc)
    result = IngestResult(parsed=len(findings))
    batch = _dedupe_in_batch(findings)
    if not batch:
        return result

    # Advisory classification (counts only — correctness comes from the
    # unique index + ON CONFLICT, never from this read).
    keys = {(finding.source, finding.fingerprint) for finding in batch}
    existing = (
        await session.execute(
            select(SecurityFinding.source, SecurityFinding.fingerprint).where(
                SecurityFinding.connection_id == connection_id,
                SecurityFinding.scope == scope,
            )
        )
    ).all()
    existing_keys = {(source, fingerprint) for source, fingerprint in existing}
    for key in keys:
        if key in existing_keys:
            result.updated += 1
            result.updated_by_source[key[0]] = result.updated_by_source.get(key[0], 0) + 1
        else:
            result.created += 1
            result.created_by_source[key[0]] = result.created_by_source.get(key[0], 0) + 1

    insert = _conflict_insert(session)
    stmt = insert(SecurityFinding).values(
        [
            {
                "provider": provider,
                "connection_id": connection_id,
                "scope": scope,
                "source": finding.source,
                "fingerprint": finding.fingerprint,
                "severity": finding.severity,
                "title": finding.title,
                "path": finding.path,
                "line": finding.line,
                "identifiers": finding.identifiers,
                "status": "open",
                "ref": ref,
                "sha": sha,
                "run_id": run_id,
                "first_seen": seen_at,
                "last_seen": seen_at,
            }
            for finding in batch
        ]
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            SecurityFinding.connection_id,
            SecurityFinding.source,
            SecurityFinding.scope,
            SecurityFinding.fingerprint,
        ],
        # Observability refresh only — and last_seen moves monotonically
        # forward (a late/reordered older scan never rolls it back).
        set_={
            "severity": stmt.excluded.severity,
            "title": stmt.excluded.title,
            "path": stmt.excluded.path,
            "line": stmt.excluded.line,
            "identifiers": stmt.excluded.identifiers,
            "ref": stmt.excluded.ref,
            "sha": stmt.excluded.sha,
            "last_seen": case(
                (stmt.excluded.last_seen > SecurityFinding.last_seen, stmt.excluded.last_seen),
                else_=SecurityFinding.last_seen,
            ),
        },
    )
    await session.execute(stmt)
    return result


# ----------------------------------------------------------------------
# Presence tracking (R25 §4 reappearance rule)
# ----------------------------------------------------------------------


async def mark_scan_presence(
    session: AsyncSession,
    scope: str,
    connection_id: str,
    present_by_source: dict[str, set[str]],
    *,
    seen_at: datetime | None = None,
) -> int:
    """Track per-source presence and reopen reappearing suppressed rows.

    Call AFTER :func:`upsert_findings` inside the same transaction, passing
    the fingerprints observed by each COMPLETE surface. Three effects:

    1. findings of a covered source NOT in this scan →
       ``absent_in_last_scan = True`` (status untouched — silence is never
       a fix);
    2. suppressed findings (``false_positive``/``dismissed``/``fixed``)
       that WERE absent last scan and reappear now → reopened
       (``status='open'``, stale suggestion cleared, ``version`` bumped) —
       a new evidence assessment, never a silent restore of the old
       suppression (R25 §4);
    3. everything observed → ``absent_in_last_scan = False``.

    Returns the number of reopened rows.
    """
    seen_at = seen_at or datetime.now(timezone.utc)
    reopened = 0
    for source, present in present_by_source.items():
        rows = (
            (
                await session.execute(
                    select(SecurityFinding).where(
                        SecurityFinding.connection_id == connection_id,
                        SecurityFinding.scope == scope,
                        SecurityFinding.source == source,
                    )
                )
            )
            .scalars()
            .all()
        )

        reappearing = [
            row
            for row in rows
            if row.fingerprint in present
            and row.absent_in_last_scan
            and row.status in SUPPRESSED_STATUSES
        ]
        for row in reappearing:
            session.add(
                SecurityFindingAction(
                    finding_id=row.id,
                    action="reopen",
                    actor="ingest:reappearance",
                    justification=(
                        "finding reappeared in a scan after being absent — "
                        "previous suppression is not restored; fresh "
                        "assessment required (R25 §4)"
                    ),
                    before_status=row.status,
                    after_status="open",
                    payload={"source": source, "observed_at": seen_at.isoformat()},
                    outcome="applied",
                )
            )
            row.status = "open"
            row.suggested_verdict = None
            row.suggested_at = None
            row.suggested_by = None
            row.version += 1
            row.absent_in_last_scan = False
            reopened += 1

        for row in rows:
            row.absent_in_last_scan = row.fingerprint not in present
    return reopened


# ----------------------------------------------------------------------
# Scan execution records (R26: a partial scan never looks clean)
# ----------------------------------------------------------------------


def record_scan_execution(
    session: AsyncSession,
    *,
    provider: str,
    scope: str,
    source: str,
    completeness: str,
    connection_id: str = "",
    external_id: str | None = None,
    ref: str | None = None,
    sha: str | None = None,
    scanner_version: str | None = None,
    parsed: int = 0,
    created: int = 0,
    updated: int = 0,
    errors: list[str] | None = None,
    run_id: str | None = None,
    observed_at: datetime | None = None,
) -> ScanExecution:
    """Append one :class:`ScanExecution` row inside the caller's transaction."""
    observed_at = observed_at or datetime.now(timezone.utc)
    row = ScanExecution(
        provider=provider,
        connection_id=connection_id,
        scope=scope,
        source=source,
        completeness=completeness,
        external_id=external_id,
        ref=ref,
        sha=sha,
        scanner_version=scanner_version,
        ingest_version=INGEST_VERSION,
        parsed=parsed,
        created=created,
        updated=updated,
        errors=errors or [],
        run_id=run_id,
        observed_at=observed_at,
        retention_until=retention_from(observed_at),
    )
    session.add(row)
    return row


# ----------------------------------------------------------------------
# GitLab CE: pipeline job artifacts (gl-*-report.json)
# ----------------------------------------------------------------------


def gitlab_report_jobs(jobs: list[Any]) -> list[tuple[Any, str, str]]:
    """Match pipeline jobs to their report artifacts by name (research §3.1).

    Returns (job, source, artifact_path) triples; a job yields at most one
    report (the first pattern that matches its name wins — a job cannot be
    both SAST and secret detection).
    """
    matched: list[tuple[Any, str, str]] = []
    for job in jobs:
        name = str(getattr(job, "name", "") or "")
        for pattern, source, artifact_path in _GITLAB_REPORTS:
            if pattern.search(name):
                matched.append((job, source, artifact_path))
                break
    return matched


def _scanner_version_of(report: Any) -> str | None:
    """The report's scanner version, when the parsed report carried one."""
    scan = getattr(report, "raw", None) or {}
    scanner = (scan.get("scan") or {}).get("scanner") if isinstance(scan, dict) else None
    version = (scanner or {}).get("version") if isinstance(scanner, dict) else None
    return str(version) if version else None


async def ingest_gitlab_pipeline(
    client: _GitLabTransport,
    session_factory: async_sessionmaker[AsyncSession],
    project_id: int,
    pipeline_id: int,
    *,
    ref: str | None = None,
    sha: str | None = None,
    run_id: str | None = None,
    connection_id: str = "",
) -> IngestResult:
    """Parse ``gl-sast-report.json`` / ``gl-secret-detection-report.json``
    job artifacts from *pipeline_id* and upsert them for the project.

    Jobs are found by name pattern (``sast``, ``secret_detection``,
    ``semgrep-sast``, …); a job without the report artifact (scan failed,
    artifact expired, empty ruleset) is skipped with a logged note — its
    silence must not look like fixes, so nothing else happens. Every
    attempted surface records a :class:`ScanExecution` row: a pipeline
    with NO report jobs, or an unreadable artifact, marks the scan
    INCOMPLETE (R26) — it must never look like a clean scan.
    """
    jobs = await client.list_pipeline_jobs(project_id, pipeline_id)
    matched = gitlab_report_jobs(jobs)
    scope = str(project_id)
    now = datetime.now(timezone.utc)

    normalized: list[NormalizedFinding] = []
    result = IngestResult()
    #: (source, completeness, parsed, scanner_version, errors) per surface.
    surfaces: list[tuple[str, str, int, str | None, list[str]]] = []
    for job, source, artifact_path in matched:
        job_id = int(getattr(job, "id", 0) or 0)
        try:
            raw_bytes = await client.get_job_artifacts_file(project_id, job_id, artifact_path)
            report = parse_gitlab_security_report(
                json.loads(raw_bytes), scan_type=source.removeprefix("gitlab_")
            )
        except Exception as exc:  # 404 artifact / undecodable JSON / schema drift
            note = f"job {job_id} ({artifact_path}): {exc}"
            logger.debug("GitLab security report unavailable — %s", note)
            result.errors.append(note)
            surfaces.append((source, "incomplete", 0, None, [note]))
            continue

        result.parsed += len(report.findings)
        for finding in report.findings:
            vuln = finding.raw
            normalized.append(
                NormalizedFinding(
                    source=source,
                    fingerprint=gitlab_fingerprint(
                        category=str(vuln.get("category") or source),
                        identifiers=finding.identifiers,
                        location=vuln.get("location"),
                        fallback_key=finding.name,
                    ),
                    severity=normalize_severity(finding.severity),
                    title=finding.name,
                    path=finding.file or None,
                    line=finding.start_line,
                    identifiers=list(finding.identifiers),
                )
            )
        surfaces.append((source, "complete", len(report.findings), _scanner_version_of(report), []))

    if not matched:
        # No security jobs at all: no evidence, never a clean scan (R26).
        note = f"pipeline {pipeline_id}: no security report jobs found"
        result.errors.append(note)
        surfaces.append(("gitlab_pipeline", "incomplete", 0, None, [note]))

    result.scan_complete = all(completeness == "complete" for _, completeness, *_ in surfaces)

    async with session_factory() as session:
        async with session.begin():
            if normalized:
                upsert = await upsert_findings(
                    session,
                    normalized,
                    provider="gitlab",
                    scope=scope,
                    connection_id=connection_id,
                    ref=ref,
                    sha=sha,
                    run_id=run_id,
                    seen_at=now,
                )
                result.created, result.updated = upsert.created, upsert.updated
                result.created_by_source, result.updated_by_source = (
                    upsert.created_by_source,
                    upsert.updated_by_source,
                )

            for source, completeness, parsed, scanner_version, errors in surfaces:
                record_scan_execution(
                    session,
                    provider="gitlab",
                    scope=scope,
                    source=source,
                    completeness=completeness,
                    connection_id=connection_id,
                    external_id=str(pipeline_id),
                    ref=ref,
                    sha=sha,
                    scanner_version=scanner_version,
                    parsed=parsed,
                    created=result.created_by_source.get(source, 0),
                    updated=result.updated_by_source.get(source, 0),
                    errors=errors,
                    run_id=run_id,
                    observed_at=now,
                )

            # Presence tracking drives only from fully-read surfaces.
            present_by_source = {
                source: {finding.fingerprint for finding in normalized if finding.source == source}
                for source, completeness, *_ in surfaces
                if completeness == "complete"
            }
            if present_by_source:
                await mark_scan_presence(
                    session, scope, connection_id, present_by_source, seen_at=now
                )
    result.complete_sources = list(present_by_source)
    return result


# ----------------------------------------------------------------------
# GitHub: code scanning / secret scanning / dependabot alert APIs
# ----------------------------------------------------------------------


def normalize_code_scanning_alert(alert: dict[str, Any]) -> NormalizedFinding:
    """Alert JSON → NormalizedFinding (research §3.2 field set).

    Severity: the alert carries ``rule.security_severity_level``
    (``critical|high|medium|low``) with the rule's ``severity``
    (``error|warning|note``) as the fallback — both are normalized onto
    the forge enum.
    """
    rule = alert.get("rule") or {}
    instance = alert.get("most_recent_instance") or {}
    location = instance.get("location") or {}
    identifiers = [
        {"type": "rule", "value": rule.get("id") or ""},
        {"type": "rule_name", "value": rule.get("name") or ""},
    ]
    return NormalizedFinding(
        source="github_code_scanning",
        fingerprint=github_fingerprint(alert.get("number") or 0),
        severity=normalize_severity(rule.get("security_severity_level") or rule.get("severity")),
        title=str(rule.get("description") or rule.get("name") or f"alert {alert.get('number')}"),
        path=str(location.get("path") or "") or None,
        line=location.get("start_line"),
        identifiers=identifiers,
    )


def normalize_secret_scanning_alert(alert: dict[str, Any]) -> NormalizedFinding:
    """Secret scanning alert → NormalizedFinding.

    GitHub assigns secret alerts no severity; forge maps ``validity ==
    'active''`` (the secret still works) to ``critical`` and everything
    else to ``high`` — a present secret is never below high.
    """
    identifiers = [
        {"type": "secret_type", "value": alert.get("secret_type_display_name") or ""},
    ]
    return NormalizedFinding(
        source="github_secret",
        fingerprint=github_fingerprint(alert.get("number") or 0),
        severity="critical" if alert.get("validity") == "active" else "high",
        title=str(
            alert.get("secret_type_display_name")
            or alert.get("secret_type")
            or f"secret alert {alert.get('number')}"
        ),
        identifiers=identifiers,
    )


def normalize_dependabot_alert(alert: dict[str, Any]) -> NormalizedFinding:
    """Dependabot alert → NormalizedFinding (research §3.2 field set)."""
    dependency = alert.get("dependency") or {}
    advisory = alert.get("security_advisory") or {}
    manifest = str(dependency.get("manifest_path") or "") or None
    identifiers = [
        {"type": "ghsa", "value": advisory.get("ghsa_id") or ""},
        {"type": "cve", "value": advisory.get("cve_id") or ""},
        {"type": "package", "value": (dependency.get("package") or {}).get("name") or ""},
    ]
    return NormalizedFinding(
        source="github_dependabot",
        fingerprint=github_fingerprint(alert.get("number") or 0),
        severity=normalize_severity(advisory.get("severity")),
        title=str(advisory.get("summary") or f"alert {alert.get('number')}"),
        path=manifest,
        line=None,
        identifiers=identifiers,
    )


async def ingest_github_alerts(
    client: _GitHubTransport,
    session_factory: async_sessionmaker[AsyncSession],
    owner: str,
    repo: str,
    *,
    ref: str | None = None,
    sha: str | None = None,
    run_id: str | None = None,
    connection_id: str | None = None,
) -> IngestResult:
    """Pull all three alert surfaces (state=open) and upsert them.

    Graceful degradation (research §7): code/secret scanning answer 403
    when the repo lacks Code Security / Secret Protection — the surface is
    recorded as an ingest error AND an INCOMPLETE :class:`ScanExecution`
    row (R26: a 403 surface must never make the scan look clean); the
    remaining surfaces still ingest. Dependabot alerts are free for all
    repositories. *connection_id* scopes the rows to the GitHub
    connection (installation); when omitted it derives from the repo full
    name — the same fallback the triage executor uses, so both legs always
    agree on the scope key.
    """
    scope = f"{owner}/{repo}"
    if connection_id is None:
        connection_id = f"github:{scope}"
    now = datetime.now(timezone.utc)
    result = IngestResult()

    normalized: list[NormalizedFinding] = []
    #: (source, completeness, parsed, errors) per surface.
    surfaces: list[tuple[str, str, int, list[str]]] = []
    for fetch, normalizer, source, label in (
        (
            client.list_code_scanning_alerts,
            normalize_code_scanning_alert,
            "github_code_scanning",
            "code-scanning",
        ),
        (
            client.list_secret_scanning_alerts,
            normalize_secret_scanning_alert,
            "github_secret",
            "secret-scanning",
        ),
        (
            client.list_dependabot_alerts,
            normalize_dependabot_alert,
            "github_dependabot",
            "dependabot",
        ),
    ):
        try:
            alerts = await fetch(owner, repo, state="open")
        except Exception as exc:
            note = f"{label}: {exc}"
            logger.info("GitHub alert surface unavailable — %s", note)
            result.errors.append(note)
            surfaces.append((source, "incomplete", 0, [note]))
            continue
        result.parsed += len(alerts)
        normalized.extend(normalizer(dict(alert)) for alert in alerts)
        surfaces.append((source, "complete", len(alerts), []))

    result.scan_complete = all(completeness == "complete" for _, completeness, *_ in surfaces)

    async with session_factory() as session:
        async with session.begin():
            if normalized:
                upsert = await upsert_findings(
                    session,
                    normalized,
                    provider="github",
                    scope=scope,
                    connection_id=connection_id,
                    ref=ref,
                    sha=sha,
                    run_id=run_id,
                    seen_at=now,
                )
                result.created, result.updated = upsert.created, upsert.updated
                result.created_by_source, result.updated_by_source = (
                    upsert.created_by_source,
                    upsert.updated_by_source,
                )

            for source, completeness, parsed, errors in surfaces:
                record_scan_execution(
                    session,
                    provider="github",
                    scope=scope,
                    source=source,
                    completeness=completeness,
                    connection_id=connection_id,
                    parsed=parsed,
                    created=result.created_by_source.get(source, 0),
                    updated=result.updated_by_source.get(source, 0),
                    errors=errors,
                    run_id=run_id,
                    observed_at=now,
                )

            present_by_source = {
                source: {finding.fingerprint for finding in normalized if finding.source == source}
                for source, completeness, *_ in surfaces
                if completeness == "complete"
            }
            if present_by_source:
                await mark_scan_presence(
                    session, scope, connection_id, present_by_source, seen_at=now
                )
    result.complete_sources = list(present_by_source)
    return result
