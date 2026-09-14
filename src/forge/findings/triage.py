"""The ``/security`` command — provider-neutral durable triage step (v0.7).

Wired into the step runtime like every other command (ADR-0017): the
gateway persists a ``security_triage`` step in the ingress transaction,
the worker claims it and :func:`execute_security_command` runs the leg:

1. **Refresh** (best effort): latest successful GitLab pipeline artifacts
   and/or the GitHub alert surfaces are re-ingested, so triage reasons
   over current data. Ingest errors (403 on a repo without Code Security,
   missing artifacts, …) degrade to notes — never fail the command.
2. **Pull**: open findings for the scope from ``security_findings``,
   severity-sorted, capped at :data:`TRIAGE_BATCH` (50) per call.
3. **Triage**: the existing ``security-triage`` agent classifies each
   finding. The agent contract (:class:`~forge.agents.models.
   SecurityTriageResult`) keys findings by ``id`` — forge feeds it the
   stable **fingerprint** as the id so verdicts join back to rows.
4. **Write back**: ``is_false_positive`` → ``status='false_positive'``,
   confirmed → ``status='triaged'``, with the justification as
   ``triage_note``. Optionally (``FORGE_SECURITY_REMOTE_DISMISS``) the
   false positives are also dismissed on GitHub with the research §4.2
   enum quirks and the justification as the audit-trail comment.
5. **Post** the triage comment: grouped by severity, every finding with
   its fingerprint and suggested action.

No admission gate (unlike /implement): triage is a read + one bounded
model call + comments, mirroring the legacy reactive ``/security``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.agents.models import SecurityFindingResult, SecurityTriageResult
from forge.context.engine import AgentContext
from forge.context.security_report import (
    SecurityFinding as ReportFinding,
)
from forge.context.security_report import (
    SecurityReport,
)
from forge.findings.ingest import ingest_github_alerts, ingest_gitlab_pipeline
from forge.findings.models import SecurityFinding, normalize_severity
from forge.gitlab.schemas import Pipeline

logger = logging.getLogger(__name__)

#: Bounded agent calls: at most this many findings per /security run,
#: severity-sorted (critical first) so the cap never hides the worst.
TRIAGE_BATCH = 50

_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

#: TriageRunner: findings rows → the agent's structured verdict.
TriageRunner = Callable[[list[SecurityFinding]], Awaitable[SecurityTriageResult]]


@dataclass
class TriageOutcome:
    """What one /security run did (the step's output record)."""

    scope: str = ""
    considered: int = 0
    triaged: int = 0
    false_positives: int = 0
    refresh_created: int = 0
    refresh_errors: list[str] = field(default_factory=list)
    comment_posted: bool = False
    remote_dismissed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "considered": self.considered,
            "triaged": self.triaged,
            "false_positives": self.false_positives,
            "refresh_created": self.refresh_created,
            "refresh_errors": self.refresh_errors,
            "comment_posted": self.comment_posted,
            "remote_dismissed": self.remote_dismissed,
        }


# ----------------------------------------------------------------------
# DB access helpers
# ----------------------------------------------------------------------


async def open_findings_for_scope(
    session_factory: async_sessionmaker[AsyncSession],
    scope: str,
    *,
    limit: int = TRIAGE_BATCH,
) -> list[SecurityFinding]:
    """Open findings for *scope*, severity-sorted, batch-capped."""
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(SecurityFinding)
                    .where(SecurityFinding.scope == scope, SecurityFinding.status == "open")
                    .order_by(SecurityFinding.first_seen, SecurityFinding.id)
                )
            )
            .scalars()
            .all()
        )
    ordered = sorted(
        rows,
        key=lambda row: (_SEVERITY_RANK.get(row.severity, 9), row.first_seen, row.fingerprint),
    )
    return ordered[:limit]


async def apply_triage_verdicts(
    session_factory: async_sessionmaker[AsyncSession],
    scope: str,
    result: SecurityTriageResult,
) -> list[str]:
    """Write the agent's verdicts back onto the finding rows.

    Joins on the fingerprint (the id the agent was fed); unknown ids are
    ignored (the model hallucinated one). Returns the applied fingerprints.
    """
    if not result.findings:
        return []
    applied: list[str] = []
    async with session_factory() as session:
        async with session.begin():
            rows = (
                (
                    await session.execute(
                        select(SecurityFinding).where(SecurityFinding.scope == scope)
                    )
                )
                .scalars()
                .all()
            )
            by_fingerprint = {row.fingerprint: row for row in rows}
            for verdict in result.findings:
                row = by_fingerprint.get(verdict.id)
                if row is None:
                    continue
                row.status = "false_positive" if verdict.is_false_positive else "triaged"
                row.triage_note = verdict.justification or verdict.remediation or ""
                applied.append(verdict.id)
    return applied


# ----------------------------------------------------------------------
# Default agent runner (real LLM; tests inject triage_runner stubs)
# ----------------------------------------------------------------------


def _rows_to_report(rows: list[SecurityFinding]) -> SecurityReport:
    """Pack the batch into the SecurityReport shape the agent prompts from."""
    findings = [
        ReportFinding(
            id=row.fingerprint,
            name=row.title or "Unnamed finding",
            description=row.title or "",
            severity=(row.severity or "info").capitalize(),
            scanner=row.source,
            file=row.path or "",
            start_line=row.line,
            identifiers=list(row.identifiers or []),
        )
        for row in rows
    ]
    return SecurityReport(findings=findings, scan_type="mixed", scanner_name="forge")


async def _default_triage_runner(
    settings: Any,
    forge_config: Any,
    rows: list[SecurityFinding],
    *,
    project_path: str,
) -> SecurityTriageResult:
    """Run the real security-triage agent over the batch."""
    from forge.agents.registry import AgentRegistry
    from forge.llm.provider import get_model

    registry = AgentRegistry(getattr(settings, "FORGE_AGENTS_DIR", "agents"))
    registry.load()
    definition = registry.get("security-triage")
    if definition is None:
        raise RuntimeError("security-triage agent definition not found in FORGE_AGENTS_DIR")
    model = get_model(definition.model_alias, forge_config, settings)
    from forge.agents.security_triage import SecurityTriageAgent

    context = AgentContext(
        event_type="security_triage",
        project_id=0,
        project_path=project_path,
        security_reports=[_rows_to_report(rows)],
    )
    agent = SecurityTriageAgent(
        definition=definition,
        model=model,
        context=context,
        project_config=_project_config_stub(),
        gitlab=None,  # type: ignore[arg-type]
    )
    outcome = await agent.run()
    if not outcome.success or outcome.security_triage is None:
        raise RuntimeError(f"security-triage agent failed: {outcome.error or 'no output'}")
    return outcome.security_triage


def _project_config_stub() -> Any:
    from forge.orchestrator.project_config import ProjectConfig

    return ProjectConfig()


# ----------------------------------------------------------------------
# The durable step executor
# ----------------------------------------------------------------------


async def execute_security_command(
    settings: Any,
    forge_config: Any,
    session_factory: async_sessionmaker[AsyncSession],
    metadata: dict[str, Any],
    *,
    gitlab: Any | None = None,
    github_client: Any | None = None,
    triage_runner: TriageRunner | None = None,
) -> dict[str, Any]:
    """Execute a ``security_triage`` command step (both providers).

    *gitlab* / *github_client* are the provider transports (real clients
    in production — built here when absent — fakes in tests);
    *triage_runner* replaces the agent (tests). Returns the outcome dict
    recorded by the step runtime.
    """
    provider = str(metadata.get("provider") or "gitlab")
    owner, repo = "", ""
    owned_client = False
    if provider == "github" and github_client is None:
        github_client = _build_github_client(settings)
        owned_client = True
    if provider == "github":
        owner, repo = _github_subject(metadata)
        scope = f"{owner}/{repo}"
    else:
        scope = str(metadata.get("project_id") or "0")
    outcome = TriageOutcome(scope=scope)

    try:
        return await _execute_security_command_inner(
            settings,
            forge_config,
            session_factory,
            metadata,
            outcome=outcome,
            provider=provider,
            scope=scope,
            owner=owner,
            repo=repo,
            gitlab=gitlab,
            github_client=github_client,
            triage_runner=triage_runner,
        )
    finally:
        if owned_client:
            aclose = getattr(github_client, "aclose", None)
            if aclose is not None:
                await aclose()


async def _execute_security_command_inner(
    settings: Any,
    forge_config: Any,
    session_factory: async_sessionmaker[AsyncSession],
    metadata: dict[str, Any],
    *,
    outcome: TriageOutcome,
    provider: str,
    scope: str,
    owner: str,
    repo: str,
    gitlab: Any | None,
    github_client: Any | None,
    triage_runner: TriageRunner | None,
) -> dict[str, Any]:
    # 1. Best-effort refresh so triage reasons over current data.
    outcome.refresh_created = await _refresh(
        settings,
        session_factory,
        metadata,
        provider=provider,
        scope=scope,
        gitlab=gitlab,
        github_client=github_client,
        outcome=outcome,
    )

    # 2. Pull the open batch.
    rows = await open_findings_for_scope(session_factory, scope)
    outcome.considered = len(rows)

    if not rows:
        body = _empty_comment(scope)
        await _post_comment(metadata, gitlab=gitlab, github_client=github_client, body=body)
        outcome.comment_posted = True
        return outcome.as_dict()

    # 3. Triage (bounded batch, severity-sorted).
    if triage_runner is None:

        async def runner(batch: list[SecurityFinding]) -> SecurityTriageResult:
            return await _default_triage_runner(settings, forge_config, batch, project_path=scope)

        triage_runner = runner
    result = await triage_runner(rows)
    verdicts = {verdict.id: verdict for verdict in result.findings}

    # 4. Write triage status back.
    applied = await apply_triage_verdicts(session_factory, scope, result)
    outcome.triaged = sum(
        1
        for fingerprint in applied
        if not (verdicts.get(fingerprint) and verdicts[fingerprint].is_false_positive)
    )
    outcome.false_positives = len(applied) - outcome.triaged

    # 4b. Optional remote dismissal (GitHub only, operator opt-in).
    if bool(getattr(settings, "FORGE_SECURITY_REMOTE_DISMISS", False)) and provider == "github":
        outcome.remote_dismissed = await _remote_dismiss(github_client, owner, repo, rows, verdicts)

    # 5. Comment.
    body = format_triage_comment(rows, verdicts, result, scope)
    await _post_comment(metadata, gitlab=gitlab, github_client=github_client, body=body)
    outcome.comment_posted = True
    return outcome.as_dict()


def _build_github_client(settings: Any) -> Any:
    """The production GitHub transport (mirrors build_github_agents)."""
    from forge.integrations.github import GitHubClient
    from forge.integrations.github_flow import credentials_from_settings

    return GitHubClient(
        base_url=getattr(settings, "FORGE_GITHUB_API_URL", "https://api.github.com"),
        token_provider=credentials_from_settings(settings),
    )


async def _refresh(
    settings: Any,
    session_factory: async_sessionmaker[AsyncSession],
    metadata: dict[str, Any],
    *,
    provider: str,
    scope: str,
    gitlab: Any | None,
    github_client: Any | None,
    outcome: TriageOutcome,
) -> int:
    """Re-ingest the provider surfaces; degrade errors to outcome notes."""
    created = 0
    if provider == "github" and github_client is not None:
        owner, repo = scope.split("/", 1)
        try:
            result = await ingest_github_alerts(github_client, session_factory, owner, repo)
            created += result.created
            outcome.refresh_errors.extend(result.errors)
        except Exception as exc:  # a broken surface must not kill the step
            outcome.refresh_errors.append(f"github ingest: {exc}")
        return created

    if provider != "github" and gitlab is not None:
        project_id = int(metadata.get("project_id") or 0)
        try:
            pipeline = await _latest_finished_pipeline(gitlab, project_id)
            if pipeline is not None:
                result = await ingest_gitlab_pipeline(
                    gitlab,
                    session_factory,
                    project_id,
                    int(pipeline.id),
                    ref=pipeline.ref,
                    sha=pipeline.sha,
                )
                created += result.created
                outcome.refresh_errors.extend(result.errors)
        except Exception as exc:
            outcome.refresh_errors.append(f"gitlab ingest: {exc}")
    return created


async def _latest_finished_pipeline(gitlab: Any, project_id: int) -> Pipeline | None:
    """The most recent successful pipeline carrying security jobs."""
    pipelines = await gitlab.list_pipelines(project_id, status="success", per_page=5)
    return pipelines[0] if pipelines else None


def _github_subject(metadata: dict[str, Any]) -> tuple[str, str]:
    full = str(metadata.get("repo_full_name") or "")
    if "/" in full:
        return full.split("/", 1)
    return "", ""


# ----------------------------------------------------------------------
# Remote dismissal (research §4.2 enum quirks)
# ----------------------------------------------------------------------


async def _remote_dismiss(
    github_client: Any,
    owner: str,
    repo: str,
    rows: list[SecurityFinding],
    verdicts: dict[str, SecurityFindingResult],
) -> int:
    """Dismiss the false-positive GitHub alerts with a machine rationale."""
    dismissed = 0
    for row in rows:
        verdict = verdicts.get(row.fingerprint)
        if verdict is None or not verdict.is_false_positive:
            continue
        rationale = f"forge triage ({row.fingerprint[:12]}): {verdict.justification}"
        try:
            if row.source == "github_code_scanning":
                await github_client.dismiss_code_scanning_alert(
                    owner,
                    repo,
                    int(row.fingerprint),
                    dismissed_reason="false positive",  # space enum (§4.2)
                    dismissed_comment=rationale,
                )
            elif row.source == "github_secret":
                await github_client.resolve_secret_scanning_alert(
                    owner,
                    repo,
                    int(row.fingerprint),
                    resolution="false_positive",  # underscore enum (§4.2)
                    resolution_comment=rationale,
                )
            elif row.source == "github_dependabot":
                await github_client.dismiss_dependabot_alert(
                    owner,
                    repo,
                    int(row.fingerprint),
                    dismissed_reason="inaccurate",  # closest enum to false positive
                    dismissed_comment=rationale,
                )
            else:
                continue
            dismissed += 1
        except Exception as exc:
            logger.warning("Remote dismissal failed for %s: %s", row.fingerprint[:12], exc)
    return dismissed


# ----------------------------------------------------------------------
# Comment posting
# ----------------------------------------------------------------------


async def _post_comment(
    metadata: dict[str, Any],
    *,
    gitlab: Any | None,
    github_client: Any | None,
    body: str,
) -> None:
    """Post the triage comment on the MR/issue/PR that asked for it."""
    if metadata.get("provider") == "github" and github_client is not None:
        owner, repo = _github_subject(metadata)
        number = int(metadata.get("issue_number") or 0)
        if owner and number:
            await github_client.create_issue_comment(owner, repo, number, body)
            return
        logger.warning("/security produced no comment target — dropping")
        return

    if gitlab is None:
        logger.warning("/security produced no comment target (no transport) — dropping")
        return
    project_id = int(metadata.get("project_id") or 0)
    mr_iid = metadata.get("mr_iid")
    issue_iid = metadata.get("issue_iid")
    if mr_iid:
        await gitlab.create_mr_note(project_id, int(mr_iid), body)
    elif issue_iid:
        await gitlab.create_issue_note(project_id, int(issue_iid), body)
    else:
        logger.warning("/security produced no comment target — dropping")


# ----------------------------------------------------------------------
# Comment formatting (pure)
# ----------------------------------------------------------------------

_SEVERITY_BADGE = {
    "critical": "\U0001f6a8 Critical",
    "high": "\U0001f534 High",
    "medium": "\U0001f7e0 Medium",
    "low": "\U0001f7e1 Low",
    "info": "\U0001f4a1 Info",
}


def format_triage_comment(
    rows: list[SecurityFinding],
    verdicts: dict[str, SecurityFindingResult],
    result: SecurityTriageResult,
    scope: str,
) -> str:
    """The grouped triage comment: severity sections, fingerprints, actions.

    Grouping is by the STORED severity of every considered finding (so
    triaged-but-unverdicted rows still show), with the agent's verdict and
    suggested action attached where one exists.
    """
    confirmed = [v for v in result.findings if not v.is_false_positive]
    false_pos = [v for v in result.findings if v.is_false_positive]
    lines: list[str] = [
        "## \U0001f6e1\ufe0f Forge Security Triage",
        "",
        f"**Risk level:** {result.risk_level.upper()} — "
        f"{len(confirmed)} confirmed, {len(false_pos)} likely false positive "
        f"(of {len(rows)} open finding(s) on `{scope}`).",
        "",
    ]
    if result.summary:
        lines += [result.summary, ""]

    for severity in ("critical", "high", "medium", "low", "info"):
        group = [row for row in rows if (row.severity or "info") == severity]
        if not group:
            continue
        lines += [f"### {_SEVERITY_BADGE[severity]}", ""]
        for row in group:
            lines.extend(_finding_lines(row, verdicts.get(row.fingerprint)))
        lines.append("")

    unverdicted_note = ""
    missing = len(rows) - len(result.findings)
    if missing > 0:
        unverdicted_note = (
            f"\n> {missing} finding(s) received no explicit verdict this pass — "
            "they stay **open**; re-run `/security` to re-triage.\n"
        )
    lines += [
        "Triaged by forge \u00b7 security-triage v1.0 \u2014 fingerprints are stable "
        "across scans; verdicts are recorded in forge, never inferred from scan "
        "silence." + unverdicted_note
    ]
    return "\n".join(lines).rstrip() + "\n"


def _finding_lines(row: SecurityFinding, verdict: SecurityFindingResult | None) -> list[str]:
    location = row.path or "unknown location"
    if row.line:
        location += f":{row.line}"
    fingerprint = f"`{row.fingerprint[:12]}`"
    if verdict is None:
        return [
            f"- **{row.title or 'Unnamed finding'}** — `{location}` "
            f"\u00b7 {row.source} \u00b7 fingerprint {fingerprint} \u2014 *no verdict, stays open*"
        ]
    if verdict.is_false_positive:
        return [
            f"- **{row.title or 'Unnamed finding'}** — `{location}` "
            f"\u00b7 {row.source} \u00b7 fingerprint {fingerprint}",
            f"  \u2705 **False positive** \u2014 {verdict.justification}",
        ]
    action = verdict.remediation or verdict.justification or "review required"
    return [
        f"- **{row.title or 'Unnamed finding'}** — `{location}` "
        f"\u00b7 {row.source} \u00b7 fingerprint {fingerprint}",
        f"  \U0001f534 **Confirmed** ({verdict.severity}) \u2014 action: {action}",
    ]


def _empty_comment(scope: str) -> str:
    return (
        "## \U0001f6e1\ufe0f Forge Security Triage\n\n"
        f"\u2705 No open security findings on `{scope}`.\n\n"
        "*Findings are ingested from CI security scans and provider alert APIs; "
        "re-run `/security` after a new scan to re-check.*"
    )


__all__ = [
    "TRIAGE_BATCH",
    "TriageRunner",
    "apply_triage_verdicts",
    "execute_security_command",
    "format_triage_comment",
    "normalize_severity",
    "open_findings_for_scope",
]
