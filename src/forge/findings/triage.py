"""The ``/security`` command — provider-neutral durable triage step (v0.7).

Wired into the step runtime like every other command (ADR-0017): the
gateway persists a ``security_triage`` step in the ingress transaction,
the worker claims it and :func:`execute_security_command` runs the leg:

1. **Refresh** (best effort): latest successful GitLab pipeline artifacts
   and/or the GitHub alert surfaces are re-ingested, so triage reasons
   over current data. Ingest errors (403 on a repo without Code Security,
   missing artifacts, …) degrade to notes — never fail the command; the
   pass's :class:`~forge.findings.models.ScanExecution` rows record the
   scan's completeness (R26).
2. **Pull + pin**: open findings for the (connection, scope) from
   ``security_findings``, severity-sorted, capped at :data:`TRIAGE_BATCH`
   (50) per call. Each row's ``version`` is captured NOW — this is the
   optimistic-binding snapshot (R25).
3. **Triage**: the existing ``security-triage`` agent classifies each
   finding. The agent contract (:class:`~forge.agents.models.
   SecurityTriageResult`) keys findings by ``id`` — forge feeds it the
   stable **fingerprint** as the id so verdicts join back to rows.
4. **Write back SUGGESTIONS**: the AI verdict is recorded on
   ``suggested_verdict``/``suggested_at``/``suggested_by`` — the
   authoritative ``status`` is NOT moved by the model (R25). Each
   suggestion applies as a compare-and-set on the pinned version, so a
   manual status change during the LLM call is never overwritten (the
   stale batch misses and journals a skip). Only when the operator has
   explicitly opted in via ``FORGE_SECURITY_AUTO_ACCEPT`` (default OFF)
   does the same pass promote accepted suggestions into ``status``. The
   privileged actor path is :func:`confirm_finding_verdict` (gated by
   ``FORGE_SECURITY_TRIAGERS``). Every applied/skipped change journals a
   :class:`~forge.findings.models.SecurityFindingAction` row.
5. **Remote dismissal** (``FORGE_SECURITY_REMOTE_DISMISS``, GitHub only):
   a separate privileged action — only findings whose AUTHORITATIVE
   status is already ``false_positive`` (confirmed, never a bare
   suggestion) are dismissed remotely, each with an intent journal row
   written strictly BEFORE the provider call (research §4.2 enum quirks,
   justification as the audit-trail comment).
6. **Post** the triage comment: grouped by severity, every finding with
   its fingerprint and the SUGGESTED action.

No admission gate (unlike /implement): triage is a read + one bounded
model call + comments, mirroring the legacy reactive ``/security``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, cast

from sqlalchemy import CursorResult, select, update
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
from forge.findings.models import (
    SecurityFinding,
    SecurityFindingAction,
    normalize_severity,
)
from forge.gitlab.schemas import Pipeline

logger = logging.getLogger(__name__)

#: Bounded agent calls: at most this many findings per /security run,
#: severity-sorted (critical first) so the cap never hides the worst.
TRIAGE_BATCH = 50

_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

#: The actor recorded on AI suggestions.
AI_TRIAGE_ACTOR = "ai:security-triage"

#: TriageRunner: findings rows → the agent's structured verdict.
TriageRunner = Callable[[list[SecurityFinding]], Awaitable[SecurityTriageResult]]

#: The optimistic-binding snapshot (R25): fingerprint → (row id, version)
#: read at triage start; verdicts apply as compare-and-set on the version.
Observation = dict[str, tuple[str, int]]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def authorized_triagers(triagers_config: str) -> frozenset[str]:
    """The comma-separated triager allowlist (``FORGE_SECURITY_TRIAGERS``).

    Empty — NOBODY may confirm or reject a suggestion: the default is that
    only an explicit human grant governs authoritative status changes.
    """
    return frozenset({name.strip() for name in (triagers_config or "").split(",") if name.strip()})


def resolve_connection_id(metadata: dict[str, Any], provider: str, scope: str) -> str:
    """The findings-scope connection for a triage pass.

    The GitHub ingress always supplies ``connection_id``
    (``github:{installation}:{repo}``); the fallback derives from the repo
    full name — the SAME derivation :func:`forge.findings.ingest.
    ingest_github_alerts` uses when called without an explicit connection,
    so the refresh leg and the triage queries always agree on the scope
    key. GitLab is a single-connection deployment today ("" = default
    connection).
    """
    explicit = str(metadata.get("connection_id") or "")
    if explicit:
        return explicit
    if provider == "github":
        return f"github:{scope}"
    return ""


@dataclass
class TriageOutcome:
    """What one /security run did (the step's output record)."""

    scope: str = ""
    considered: int = 0
    #: Suggestions by kind (the AI's proposal, not authoritative writes).
    triaged: int = 0
    false_positives: int = 0
    #: R25 governance counters.
    suggested: int = 0
    confirmed: int = 0
    superseded: int = 0
    out_of_batch: int = 0
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
            "suggested": self.suggested,
            "confirmed": self.confirmed,
            "superseded": self.superseded,
            "out_of_batch": self.out_of_batch,
            "refresh_created": self.refresh_created,
            "refresh_errors": self.refresh_errors,
            "comment_posted": self.comment_posted,
            "remote_dismissed": self.remote_dismissed,
        }


@dataclass
class AppliedVerdicts:
    """The result of a verdict write-back pass (R25 counters)."""

    applied: list[str] = field(default_factory=list)
    superseded: list[str] = field(default_factory=list)
    out_of_batch: list[str] = field(default_factory=list)
    confirmed: int = 0


# ----------------------------------------------------------------------
# DB access helpers
# ----------------------------------------------------------------------


async def open_findings_for_scope(
    session_factory: async_sessionmaker[AsyncSession],
    scope: str,
    *,
    connection_id: str = "",
    limit: int = TRIAGE_BATCH,
) -> list[SecurityFinding]:
    """Open findings for (connection, scope), severity-sorted, batch-capped."""
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(SecurityFinding)
                    .where(
                        SecurityFinding.connection_id == connection_id,
                        SecurityFinding.scope == scope,
                        SecurityFinding.status == "open",
                    )
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


def observe_batch(rows: list[SecurityFinding]) -> Observation:
    """Pin (row id, version) per fingerprint — the triage-start snapshot.

    Compare-and-set target for :func:`apply_triage_verdicts`: any
    authoritative change between this snapshot and the write-back bumps
    ``version`` and makes the stale verdict miss instead of overwrite.
    Fingerprints are stable within (connection, scope, source); a
    duplicate fingerprint in one batch keeps its FIRST occurrence.
    """
    observed: Observation = {}
    for row in rows:
        observed.setdefault(row.fingerprint, (row.id, row.version))
    return observed


def _journal(
    session: AsyncSession,
    *,
    finding_id: str,
    action: str,
    actor: str,
    justification: str = "",
    before_status: str | None = None,
    after_status: str | None = None,
    payload: dict[str, Any] | None = None,
    outcome: str = "applied",
) -> SecurityFindingAction:
    """Append one governance journal row (intent/applied record)."""
    entry = SecurityFindingAction(
        finding_id=finding_id,
        action=action,
        actor=actor,
        justification=justification,
        before_status=before_status,
        after_status=after_status,
        payload=payload or {},
        outcome=outcome,
    )
    session.add(entry)
    return entry


async def apply_triage_verdicts(
    session_factory: async_sessionmaker[AsyncSession],
    scope: str,
    result: SecurityTriageResult,
    *,
    connection_id: str = "",
    observed: Observation | None = None,
    auto_accept: bool = False,
    actor: str = AI_TRIAGE_ACTOR,
) -> AppliedVerdicts:
    """Record the agent's verdicts as SUGGESTIONS on the finding rows.

    R25 governance:

    - **Separate from status**: the verdict lands on
      ``suggested_verdict``/``suggested_at``/``suggested_by`` with the
      justification as ``triage_note``. The authoritative ``status``
      moves ONLY when *auto_accept* is set (the explicit opt-in config)
      — otherwise an authorized actor confirms via
      :func:`confirm_finding_verdict`.
    - **Optimistic binding**: with *observed* (the snapshot taken at
      triage start), each suggestion applies as ``UPDATE … WHERE id=…
      AND version=…`` — a manual status change during the LLM call bumps
      the version, the write MISSES, and the manual decision stands
      (journaled as ``skipped``). Verdicts whose fingerprint is not in
      the snapshot (hallucinated, or a suppressed/out-of-batch row) are
      NEVER applied.

    Returns the applied fingerprints and the governance counters.
    """
    applied = AppliedVerdicts()
    if not result.findings:
        return applied

    if observed is None:
        # Legacy best-effort path: pin whatever the rows carry NOW (no
        # LLM-call-race protection — callers should pass a snapshot).
        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(SecurityFinding).where(
                            SecurityFinding.connection_id == connection_id,
                            SecurityFinding.scope == scope,
                        )
                    )
                )
                .scalars()
                .all()
            )
        observed = observe_batch(list(rows))

    now = utcnow()
    async with session_factory() as session:
        async with session.begin():
            for verdict in result.findings:
                pinned = observed.get(verdict.id)
                if pinned is None:
                    # Out of batch (hallucinated / suppressed / not pulled
                    # this pass): never applied, never journaled against a
                    # row that may not exist.
                    applied.out_of_batch.append(verdict.id)
                    continue
                finding_id, pinned_version = pinned
                new_status = "false_positive" if verdict.is_false_positive else "triaged"
                note = verdict.justification or verdict.remediation or ""
                values: dict[str, Any] = {
                    "suggested_verdict": new_status,
                    "suggested_at": now,
                    "suggested_by": actor,
                    "triage_note": note,
                    "version": SecurityFinding.version + 1,
                }
                if auto_accept:
                    values["status"] = new_status
                db_result = await session.execute(
                    update(SecurityFinding)
                    .where(
                        SecurityFinding.id == finding_id,
                        SecurityFinding.version == pinned_version,
                        SecurityFinding.scope == scope,
                        SecurityFinding.connection_id == connection_id,
                    )
                    .values(**values)
                )
                if cast("CursorResult[Any]", db_result).rowcount != 1:
                    # The row changed since the snapshot (a manual status
                    # change won the race) — the stale verdict must not
                    # overwrite it.
                    applied.superseded.append(verdict.id)
                    _journal(
                        session,
                        finding_id=finding_id,
                        action="suggest_verdict",
                        actor=actor,
                        justification=note,
                        payload={
                            "verdict": new_status,
                            "observed_version": pinned_version,
                            "reason": "version_changed_since_snapshot",
                        },
                        outcome="skipped",
                    )
                    continue

                applied.applied.append(verdict.id)
                _journal(
                    session,
                    finding_id=finding_id,
                    action="suggest_verdict",
                    actor=actor,
                    justification=note,
                    before_status="open",
                    after_status=new_status if auto_accept else "open",
                    payload={"verdict": new_status, "auto_accept": auto_accept},
                )
                if auto_accept:
                    applied.confirmed += 1
                    _journal(
                        session,
                        finding_id=finding_id,
                        action="confirm_verdict",
                        actor="auto_accept",
                        justification=note,
                        before_status="open",
                        after_status=new_status,
                        payload={"verdict": new_status},
                    )
    return applied


async def confirm_finding_verdict(
    session_factory: async_sessionmaker[AsyncSession],
    finding_id: str,
    actor: str,
    *,
    accept: bool = True,
    justification: str = "",
    triagers: str = "",
) -> dict[str, Any]:
    """Authoritative confirmation of a suggested verdict (R25).

    *actor* must be on the ``FORGE_SECURITY_TRIAGERS`` allowlist (empty
    allowlist = nobody is authorized). ``accept=True`` promotes the
    existing suggestion to ``status``; ``accept=False`` rejects it (the
    suggestion clears, the finding stays ``open``). Both bump ``version``
    and journal the decision with the actor's justification.
    """
    allowed = authorized_triagers(triagers)
    if actor not in allowed:
        raise PermissionError(f"actor {actor!r} is not an authorized security triager")
    async with session_factory() as session:
        async with session.begin():
            row = (
                await session.execute(
                    select(SecurityFinding).where(SecurityFinding.id == finding_id)
                )
            ).scalar_one_or_none()
            if row is None:
                raise LookupError(f"finding {finding_id} not found")
            if row.suggested_verdict is None:
                raise RuntimeError(f"finding {finding_id} carries no suggested verdict to confirm")
            note = justification or row.triage_note or ""
            if accept:
                new_status = row.suggested_verdict
                row.status = new_status
                row.version += 1
                _journal(
                    session,
                    finding_id=finding_id,
                    action="confirm_verdict",
                    actor=actor,
                    justification=note,
                    before_status="open",
                    after_status=new_status,
                    payload={"verdict": new_status},
                )
            else:
                new_status = row.status
                row.suggested_verdict = None
                row.suggested_at = None
                row.suggested_by = None
                row.version += 1
                _journal(
                    session,
                    finding_id=finding_id,
                    action="reject_verdict",
                    actor=actor,
                    justification=note,
                    before_status=row.status,
                    after_status=row.status,
                    payload={"rejected": True},
                )
            return {
                "id": finding_id,
                "status": row.status,
                "suggested_verdict": row.suggested_verdict,
            }


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
    connection_id = resolve_connection_id(metadata, provider, scope)

    # 1. Best-effort refresh so triage reasons over current data.
    outcome.refresh_created = await _refresh(
        settings,
        session_factory,
        metadata,
        provider=provider,
        scope=scope,
        connection_id=connection_id,
        gitlab=gitlab,
        github_client=github_client,
        outcome=outcome,
    )

    # 2. Pull the open batch and pin its versions (optimistic binding).
    rows = await open_findings_for_scope(session_factory, scope, connection_id=connection_id)
    outcome.considered = len(rows)

    if not rows:
        body = _empty_comment(scope)
        await _post_comment(metadata, gitlab=gitlab, github_client=github_client, body=body)
        outcome.comment_posted = True
        return outcome.as_dict()

    observed = observe_batch(rows)

    # 3. Triage (bounded batch, severity-sorted).
    if triage_runner is None:

        async def runner(batch: list[SecurityFinding]) -> SecurityTriageResult:
            return await _default_triage_runner(settings, forge_config, batch, project_path=scope)

        triage_runner = runner
    result = await triage_runner(rows)
    verdicts = {verdict.id: verdict for verdict in result.findings}

    # 4. Write verdicts back as SUGGESTIONS (status moves only on the
    # explicit auto-accept opt-in — default OFF, R25).
    auto_accept = bool(getattr(settings, "FORGE_SECURITY_AUTO_ACCEPT", False))
    applied = await apply_triage_verdicts(
        session_factory,
        scope,
        result,
        connection_id=connection_id,
        observed=observed,
        auto_accept=auto_accept,
    )
    outcome.suggested = len(applied.applied)
    outcome.confirmed = applied.confirmed
    outcome.superseded = len(applied.superseded)
    outcome.out_of_batch = len(applied.out_of_batch)
    outcome.triaged = sum(
        1
        for fingerprint in applied.applied
        if not (verdicts.get(fingerprint) and verdicts[fingerprint].is_false_positive)
    )
    outcome.false_positives = len(applied.applied) - outcome.triaged

    # 4b. Privileged remote dismissal (GitHub only, operator opt-in):
    # confirmed false positives only — never a bare suggestion (R25).
    if bool(getattr(settings, "FORGE_SECURITY_REMOTE_DISMISS", False)) and provider == "github":
        outcome.remote_dismissed = await _remote_dismiss(
            github_client,
            owner,
            repo,
            session_factory,
            scope=scope,
            connection_id=connection_id,
            fingerprints=set(applied.applied),
            verdicts=verdicts,
        )

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
    connection_id: str,
    gitlab: Any | None,
    github_client: Any | None,
    outcome: TriageOutcome,
) -> int:
    """Re-ingest the provider surfaces; degrade errors to outcome notes."""
    created = 0
    if provider == "github" and github_client is not None:
        owner, repo = scope.split("/", 1)
        try:
            result = await ingest_github_alerts(
                github_client,
                session_factory,
                owner,
                repo,
                connection_id=connection_id,
            )
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
                    connection_id=connection_id,
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
        owner, repo = full.split("/", 1)
        return owner, repo
    return "", ""


# ----------------------------------------------------------------------
# Remote dismissal — separate privileged action (research §4.2 enum quirks)
# ----------------------------------------------------------------------


async def _remote_dismiss(
    github_client: Any,
    owner: str,
    repo: str,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    scope: str,
    connection_id: str,
    fingerprints: set[str],
    verdicts: dict[str, SecurityFindingResult],
) -> int:
    """Dismiss CONFIRMED false-positive GitHub alerts, journal-first.

    R25: this is a privileged action, gated by the operator's
    ``FORGE_SECURITY_REMOTE_DISMISS`` grant (checked by the caller) — and
    it only ever targets findings whose AUTHORITATIVE status is already
    ``false_positive``. A model suggestion alone can never close a remote
    alert. Each dismissal writes an intent journal row (``requested``)
    BEFORE the provider call and completes it (``succeeded``/``failed``)
    after; a success also moves the local mirror to ``dismissed``.
    """
    if not fingerprints:
        return 0
    async with session_factory() as session:
        candidates = (
            (
                await session.execute(
                    select(SecurityFinding).where(
                        SecurityFinding.connection_id == connection_id,
                        SecurityFinding.scope == scope,
                        SecurityFinding.status == "false_positive",
                        SecurityFinding.fingerprint.in_(fingerprints),
                    )
                )
            )
            .scalars()
            .all()
        )
        targets = [(row.id, row.source, row.fingerprint) for row in candidates]

    dismissed = 0
    for finding_id, source, fingerprint in targets:
        verdict = verdicts.get(fingerprint)
        rationale = f"forge triage ({fingerprint[:12]}): {verdict.justification if verdict else ''}"
        # Intent FIRST, strictly before the remote effect (ADR-0005 shape).
        async with session_factory() as session:
            async with session.begin():
                entry = _journal(
                    session,
                    finding_id=finding_id,
                    action="remote_dismiss",
                    actor="config:FORGE_SECURITY_REMOTE_DISMISS",
                    justification=verdict.justification if verdict else "",
                    before_status="false_positive",
                    payload={"source": source, "alert": fingerprint},
                    outcome="requested",
                )
                await session.flush()  # assign the journal row's id
                entry_id = entry.id
        try:
            await _dismiss_remote_alert(github_client, owner, repo, source, fingerprint, rationale)
        except Exception as exc:
            logger.warning("Remote dismissal failed for %s: %s", fingerprint[:12], exc)
            async with session_factory() as session:
                async with session.begin():
                    session.add(
                        SecurityFindingAction(
                            finding_id=finding_id,
                            action="remote_dismiss",
                            actor="config:FORGE_SECURITY_REMOTE_DISMISS",
                            justification=verdict.justification if verdict else "",
                            payload={"source": source, "alert": fingerprint, "error": str(exc)},
                            outcome="failed",
                        )
                    )
            continue

        dismissed += 1
        async with session_factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(SecurityFinding).where(SecurityFinding.id == finding_id)
                    )
                ).scalar_one()
                row.status = "dismissed"
                row.version += 1
                session.add(
                    SecurityFindingAction(
                        finding_id=finding_id,
                        action="remote_dismiss",
                        actor="config:FORGE_SECURITY_REMOTE_DISMISS",
                        justification=verdict.justification if verdict else "",
                        before_status="false_positive",
                        after_status="dismissed",
                        payload={"source": source, "alert": fingerprint, "intent_id": entry_id},
                        outcome="succeeded",
                    )
                )
    return dismissed


async def _dismiss_remote_alert(
    github_client: Any,
    owner: str,
    repo: str,
    source: str,
    fingerprint: str,
    rationale: str,
) -> None:
    """The provider PATCH, with the research §4.2 enum quirks per surface."""
    if source == "github_code_scanning":
        await github_client.dismiss_code_scanning_alert(
            owner,
            repo,
            int(fingerprint),
            dismissed_reason="false positive",  # space enum (§4.2)
            dismissed_comment=rationale,
        )
    elif source == "github_secret":
        await github_client.resolve_secret_scanning_alert(
            owner,
            repo,
            int(fingerprint),
            resolution="false_positive",  # underscore enum (§4.2)
            resolution_comment=rationale,
        )
    elif source == "github_dependabot":
        await github_client.dismiss_dependabot_alert(
            owner,
            repo,
            int(fingerprint),
            dismissed_reason="inaccurate",  # closest enum to false positive
            dismissed_comment=rationale,
        )
    else:
        raise ValueError(f"source {source} has no remote-dismiss surface")


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
    triaged-but-unverdicted rows still show), with the agent's SUGGESTED
    action attached where one exists — suggestions await an authorized
    confirmation before any status moves (R25).
    """
    confirmed = [v for v in result.findings if not v.is_false_positive]
    false_pos = [v for v in result.findings if v.is_false_positive]
    lines: list[str] = [
        "## \U0001f6e1\ufe0f Forge Security Triage",
        "",
        f"**Risk level:** {result.risk_level.upper()} — "
        f"{len(confirmed)} suggested confirmed, {len(false_pos)} suggested false positive "
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
        "across scans; verdicts are recorded as forge SUGGESTIONS (an authorized "
        "confirm moves the status), never inferred from scan silence." + unverdicted_note
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
            f"  \u2705 **Suggested: false positive** \u2014 {verdict.justification} "
            "*(awaiting confirmation)*",
        ]
    action = verdict.remediation or verdict.justification or "review required"
    return [
        f"- **{row.title or 'Unnamed finding'}** — `{location}` "
        f"\u00b7 {row.source} \u00b7 fingerprint {fingerprint}",
        f"  \U0001f534 **Suggested: confirmed** ({verdict.severity}) \u2014 action: {action} "
        "*(awaiting confirmation)*",
    ]


def _empty_comment(scope: str) -> str:
    return (
        "## \U0001f6e1\ufe0f Forge Security Triage\n\n"
        f"\u2705 No open security findings on `{scope}`.\n\n"
        "*Findings are ingested from CI security scans and provider alert APIs; "
        "re-run `/security` after a new scan to re-check.*"
    )


__all__ = [
    "AI_TRIAGE_ACTOR",
    "TRIAGE_BATCH",
    "AppliedVerdicts",
    "Observation",
    "TriageRunner",
    "apply_triage_verdicts",
    "authorized_triagers",
    "confirm_finding_verdict",
    "execute_security_command",
    "format_triage_comment",
    "normalize_severity",
    "observe_batch",
    "open_findings_for_scope",
    "resolve_connection_id",
]
