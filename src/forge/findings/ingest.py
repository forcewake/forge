"""Security findings ingestion: GitLab CE artifacts + GitHub alert APIs.

Both adapters normalize into :class:`forge.findings.models.SecurityFinding`
rows, deduped by (source, scope, fingerprint). Upsert semantics:

- **new** fingerprint → row created with ``status='open'``;
- **seen** fingerprint → scan-time fields (severity, title, path, line,
  identifiers, ref/sha, ``last_seen``) refresh, but ``status``,
  ``triage_note`` and ``first_seen`` are NEVER touched by ingestion —
  triage state is forge's, and one scan's silence is not evidence of a
  fix (a finding absent from the latest scan keeps its status; only
  present findings get ``last_seen`` updates). External evidence moves
  status: a GitHub alert listing answers ``state=open`` filtered, so
  mirrored alerts that GitHub itself marks ``fixed``/``dismissed`` are
  simply absent — again NOT promoted to ``fixed`` by forge.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.context.security_report import parse_gitlab_security_report
from forge.findings.fingerprints import gitlab_fingerprint, github_fingerprint
from forge.findings.models import SecurityFinding, normalize_severity

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

    def merge(self, other: IngestResult) -> IngestResult:
        return IngestResult(
            created=self.created + other.created,
            updated=self.updated + other.updated,
            parsed=self.parsed + other.parsed,
            errors=self.errors + other.errors,
        )


# ----------------------------------------------------------------------
# Core upsert (the dedupe authority)
# ----------------------------------------------------------------------


async def upsert_findings(
    session: AsyncSession,
    findings: list[NormalizedFinding],
    *,
    provider: str,
    scope: str,
    ref: str | None = None,
    sha: str | None = None,
    run_id: str | None = None,
    seen_at: datetime | None = None,
) -> IngestResult:
    """Upsert normalized findings for (provider, scope); NEVER auto-fix.

    Runs inside the caller's transaction. Existing rows keep their
    ``status``/``triage_note``/``first_seen`` — a scan can only refresh
    observability fields, never overturn a triage verdict (research §4.1
    step 1: forge-side triage state keyed by the stable fingerprint).
    """
    seen_at = seen_at or datetime.now(timezone.utc)
    result = IngestResult(parsed=len(findings))
    cache: dict[tuple[str, str], SecurityFinding | None] = {}

    for finding in findings:
        key = (finding.source, finding.fingerprint)
        if key not in cache:
            row = (
                await session.execute(
                    select(SecurityFinding).where(
                        SecurityFinding.source == finding.source,
                        SecurityFinding.scope == scope,
                        SecurityFinding.fingerprint == finding.fingerprint,
                    )
                )
            ).scalar_one_or_none()
            cache[key] = row

        row = cache[key]
        if row is None:
            row = SecurityFinding(
                source=finding.source,
                scope=scope,
                fingerprint=finding.fingerprint,
                provider=provider,
                severity=finding.severity,
                title=finding.title,
                path=finding.path,
                line=finding.line,
                identifiers=finding.identifiers,
                status="open",
                ref=ref,
                sha=sha,
                run_id=run_id,
                first_seen=seen_at,
                last_seen=seen_at,
            )
            session.add(row)
            cache[key] = row
            result.created += 1
            continue

        # Seen before: refresh observability only. status/triage_note/
        # first_seen stay — absence from a scan is never a fix (module doc).
        row.severity = finding.severity
        row.title = finding.title
        row.path = finding.path
        row.line = finding.line
        row.identifiers = finding.identifiers
        row.ref = ref
        row.sha = sha
        row.last_seen = seen_at
        result.updated += 1

    return result


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


async def ingest_gitlab_pipeline(
    client: _GitLabTransport,
    session_factory: async_sessionmaker[AsyncSession],
    project_id: int,
    pipeline_id: int,
    *,
    ref: str | None = None,
    sha: str | None = None,
    run_id: str | None = None,
) -> IngestResult:
    """Parse ``gl-sast-report.json`` / ``gl-secret-detection-report.json``
    job artifacts from *pipeline_id* and upsert them for the project.

    Jobs are found by name pattern (``sast``, ``secret_detection``,
    ``semgrep-sast``, …); a job without the report artifact (scan failed,
    artifact expired, empty ruleset) is skipped with a logged note — its
    silence must not look like fixes, so nothing else happens.
    """
    jobs = await client.list_pipeline_jobs(project_id, pipeline_id)
    matched = gitlab_report_jobs(jobs)
    scope = str(project_id)
    now = datetime.now(timezone.utc)

    normalized: list[NormalizedFinding] = []
    result = IngestResult()
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

    if not normalized:
        return result

    async with session_factory() as session:
        async with session.begin():
            upsert = await upsert_findings(
                session,
                normalized,
                provider="gitlab",
                scope=scope,
                ref=ref,
                sha=sha,
                run_id=run_id,
                seen_at=now,
            )
    result.created, result.updated = upsert.created, upsert.updated
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
) -> IngestResult:
    """Pull all three alert surfaces (state=open) and upsert them.

    Graceful degradation (research §7): code/secret scanning answer 403
    when the repo lacks Code Security / Secret Protection — the surface is
    recorded as an ingest error and the remaining surfaces still ingest.
    Dependabot alerts are free for all repositories.
    """
    scope = f"{owner}/{repo}"
    now = datetime.now(timezone.utc)
    result = IngestResult()

    normalized: list[NormalizedFinding] = []
    for fetch, normalizer, label in (
        (client.list_code_scanning_alerts, normalize_code_scanning_alert, "code-scanning"),
        (client.list_secret_scanning_alerts, normalize_secret_scanning_alert, "secret-scanning"),
        (client.list_dependabot_alerts, normalize_dependabot_alert, "dependabot"),
    ):
        try:
            alerts = await fetch(owner, repo, state="open")
        except Exception as exc:
            note = f"{label}: {exc}"
            logger.info("GitHub alert surface unavailable — %s", note)
            result.errors.append(note)
            continue
        result.parsed += len(alerts)
        normalized.extend(normalizer(dict(alert)) for alert in alerts)

    if not normalized:
        return result

    async with session_factory() as session:
        async with session.begin():
            upsert = await upsert_findings(
                session,
                normalized,
                provider="github",
                scope=scope,
                ref=ref,
                sha=sha,
                run_id=run_id,
                seen_at=now,
            )
    result.created, result.updated = upsert.created, upsert.updated
    return result
