"""Durable-run MCP tools (ADR-0021 §4, R3 §8.1) — the read surface.

These tools expose the FACTORY STATE (flow runs, frozen RunSpecs, evidence)
over MCP. They read durable Postgres state through the app's session
factory and NEVER touch a provider token — killing the legacy
platform-token passthrough for this surface (a token grants scopes over
forge's state, not forge's GitLab/GitHub identity).

Enforcement is per-call and two-layered: each tool resolves the principal
stashed by :class:`forge.mcp_server.auth.MCPAuthMiddleware`, requires its
scope, AND — A06, closing the object-level gap the R19 guard left on this
surface — checks the RUN's canonical subject against the principal's repo
allowlist (:func:`forge.mcp_server.auth.repo_target_allowed`). A token
allowlisted for repo A must not read (or even discover, via ``run_list``)
runs, plans or evidence of repo B. The checked target is the run's OWN
subject resolved from durable state, never a caller-supplied filter —
``provider``/``status`` arguments narrow, never widen. Denials return the
exact same NOT FOUND text a missing run produces (forbidden vs absent is
indistinguishable from the caller's seat) and log a WARNING audit line;
``run_list`` filters at the SQL level so its limit applies to the allowed
set (a filtered page stays a valid page).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp import Context
from sqlalchemy import ColumnElement

from forge.mcp_server.auth import (
    McpAuthzError,
    McpPrincipal,
    audit,
    audit_denied,
    repo_target_allowed,
    require_scope,
    session_factory_from_request,
)

if TYPE_CHECKING:
    from forge.durable.models import FlowRun

#: Session scope: RFC-2119 "defensively read-only" — these tools only SELECT.
_MAX_LIST = 50

#: The subject check rides ``forge:read`` — the audit label mirrors the
#: repo-allowlist suffix :func:`forge.mcp_server.auth.guard_fn` uses, so
#: operators grep one shape across surfaces.
_SUBJECT_SCOPE = "forge:read (repo allowlist)"


def _request(ctx: Context) -> Any:
    """The starlette request behind this tool call (streamable HTTP)."""
    request = getattr(ctx.request_context, "request", None)
    if request is None:
        raise McpAuthzError("forge:read", "no-http-context")
    return request


def _session_factory(ctx: Context) -> Any:
    factory = session_factory_from_request(_request(ctx))
    if factory is None:
        raise McpAuthzError("forge:read", "no-database")
    return factory


def _deny(exc: McpAuthzError) -> str:
    return f"FORBIDDEN: {exc}"


def _run_not_found(run_id: str) -> str:
    """The one not-found shape — shared by missing AND forbidden runs (A06)."""
    return f"NOT FOUND: no run {run_id!r}"


def _run_target_ref(run: FlowRun) -> str:
    """The canonical repo/project ref a run's subject authorizes against (A06).

    GitHub runs carry their subject path directly (``github_repo_full_name``),
    so path globs like ``allowed/*`` match exactly as on the classic surface.
    GitLab/AzDO rows keep only the provider's numeric subject id — matching
    R19 semantics, a numeric ref passes only against an explicitly digit
    pattern (``"42"``), never a path glob, so a restricted principal cannot
    widen its allowlist through IDs. A github row with no resolvable full
    name yields ``""`` — matched by no non-empty pattern, i.e. denied.
    """
    if run.provider == "github":
        return str(run.github_repo_full_name or "")
    return str(run.project_id)


def _subject_allowed(principal: McpPrincipal, run: FlowRun, tool: str, run_id: str) -> bool:
    """Object-level check: may *principal* read this run's subject? (A06)

    A denied read is audited (WARNING, ``forge.mcp_server.audit``) and the
    caller gets the same not-found text a missing run produces.
    """
    if repo_target_allowed(principal, _run_target_ref(run)):
        return True
    audit_denied(principal, tool, run_id, _SUBJECT_SCOPE)
    return False


def _allowed_subjects_clause(
    principal: McpPrincipal,
    subjects: Iterable[Any],
) -> ColumnElement[bool]:
    """SQL filter admitting only rows whose canonical subject is allowed (A06).

    *subjects* are the DISTINCT (provider, project_id, github_repo_full_name)
    tuples in the table; each is checked with the SAME
    :func:`forge.mcp_server.auth.repo_target_allowed` the per-object path
    uses, so the query-level and row-level decisions cannot drift. The
    result is an exact disjunction over the allowed subjects — filtered in
    SQL, so ``run_list``'s limit applies to the allowed set and a filtered
    page remains a valid page. Restricted principals with zero allowed
    subjects get a never-true clause (empty list, not an error).
    """
    from sqlalchemy import and_, false, or_

    from forge.durable.models import FlowRun

    github_refs: set[str] = set()
    numeric_ids: set[int] = set()
    for row in subjects:
        if str(row.provider) == "github":
            ref = str(row.github_repo_full_name or "")
            if ref and repo_target_allowed(principal, ref):
                github_refs.add(ref)
        elif repo_target_allowed(principal, str(row.project_id)):
            numeric_ids.add(int(row.project_id))
    clauses: list[ColumnElement[bool]] = []
    if github_refs:
        clauses.append(
            and_(
                FlowRun.provider == "github",
                FlowRun.github_repo_full_name.in_(sorted(github_refs)),
            )
        )
    if numeric_ids:
        clauses.append(
            and_(FlowRun.provider != "github", FlowRun.project_id.in_(sorted(numeric_ids)))
        )
    if not clauses:
        return false()
    return or_(*clauses)


def register_run_tools(mcp: FastMCP) -> None:
    """Register the durable-run tools on *mcp*."""

    @mcp.tool(
        name="run_list",
        description="List forge factory runs (durable work packages), newest first.",
    )
    async def run_list(
        ctx: Context,
        status: str = "",
        provider: str = "",
        limit: int = 20,
    ) -> str:
        try:
            principal = require_scope(_request(ctx), "forge:read")
        except McpAuthzError as exc:
            return _deny(exc)
        from sqlalchemy import select

        from forge.durable.models import FlowRun

        limit = max(1, min(int(limit), _MAX_LIST))
        async with _session_factory(ctx)() as session:
            query = select(FlowRun).order_by(FlowRun.updated_at.desc())
            if status:
                query = query.where(FlowRun.status == status)
            if provider:
                query = query.where(FlowRun.provider == provider)
            if principal.repo_patterns is not None:
                # A06: object-level scope, applied BEFORE limit — the caller
                # must not discover foreign subjects by listing. Subject
                # resolution reads the table's distinct subjects; the
                # provider/status arguments above only narrow further.
                distinct_subjects = (
                    await session.execute(
                        select(
                            FlowRun.provider,
                            FlowRun.project_id,
                            FlowRun.github_repo_full_name,
                        ).distinct()
                    )
                ).all()
                query = query.where(_allowed_subjects_clause(principal, distinct_subjects))
            query = query.limit(limit)
            runs = (await session.execute(query)).scalars().all()

        payload = [
            {
                "run_id": run.id,
                "provider": run.provider,
                "subject": (
                    f"{run.github_repo_full_name}#{run.github_issue_number}"
                    if run.provider == "github"
                    else f"project {run.project_id} issue {run.issue_iid}"
                ),
                "status": run.status,
                "status_reason": run.status_reason,
                "cancel_requested": run.cancel_requested,
                "updated_at": run.updated_at.isoformat(),
            }
            for run in runs
        ]
        audit(principal, "run_list", f"limit={limit}", f"ok({len(payload)})")
        return json.dumps(payload, indent=2)

    @mcp.tool(
        name="run_get",
        description="Get one forge run: status, subject identity, plan/MR links, cancel flag.",
    )
    async def run_get(ctx: Context, run_id: str) -> str:
        try:
            principal = require_scope(_request(ctx), "forge:read")
        except McpAuthzError as exc:
            return _deny(exc)
        from forge.durable.models import FlowRun

        async with _session_factory(ctx)() as session:
            run = await session.get(FlowRun, run_id)
        if run is None or not _subject_allowed(principal, run, "run_get", run_id):
            # A06: forbidden reads share the missing-run shape — no
            # existence oracle for subjects the principal cannot see.
            return _run_not_found(run_id)
        payload = {
            "run_id": run.id,
            "provider": run.provider,
            "subject": (
                f"{run.github_repo_full_name}#{run.github_issue_number}"
                if run.provider == "github"
                else f"project {run.project_id} issue {run.issue_iid}"
            ),
            "status": run.status,
            "status_reason": run.status_reason,
            "mr_iid": run.mr_iid,
            "base_sha": run.base_sha,
            "plan_digest": run.plan_digest,
            "spec_digest": run.spec_digest,
            "commit_cycle": run.commit_cycle,
            "cancel_requested": run.cancel_requested,
            "created_at": run.created_at.isoformat(),
            "updated_at": run.updated_at.isoformat(),
        }
        audit(principal, "run_get", run.id, "ok")
        return json.dumps(payload, indent=2)

    @mcp.tool(
        name="plan_get",
        description="Get the frozen RunSpec (the approved plan document) for a run.",
    )
    async def plan_get(ctx: Context, run_id: str) -> str:
        try:
            principal = require_scope(_request(ctx), "forge:read")
        except McpAuthzError as exc:
            return _deny(exc)
        from forge.durable.models import FlowRun, RunSpec

        async with _session_factory(ctx)() as session:
            # A06: the owner subject is resolved and checked BEFORE the
            # frozen document is read, let alone serialized.
            run = await session.get(FlowRun, run_id)
            if run is None or not _subject_allowed(principal, run, "plan_get", run_id):
                return _run_not_found(run_id)
            spec = (
                await session.execute(
                    RunSpec.__table__.select()
                    .where(RunSpec.run_id == run_id)
                    .order_by(RunSpec.created_at.desc())
                    .limit(1)
                )
            ).first()
        if spec is None:
            return f"NOT FOUND: no RunSpec frozen for run {run_id!r}"
        document = spec.document if isinstance(spec.document, dict) else {}
        audit(principal, "plan_get", run_id, "ok")
        return json.dumps(
            {"spec_id": spec.id, "digest": spec.digest, "document": document},
            indent=2,
            default=str,
        )

    @mcp.tool(
        name="run_evidence_get",
        description="Get a run's evidence bundle (plan/review/CI proof accumulated so far).",
    )
    async def run_evidence_get(ctx: Context, run_id: str) -> str:
        try:
            principal = require_scope(_request(ctx), "forge:read")
        except McpAuthzError as exc:
            return _deny(exc)
        from forge.durable.models import FlowRun, StepRun

        async with _session_factory(ctx)() as session:
            run = await session.get(FlowRun, run_id)
            if run is None or not _subject_allowed(principal, run, "run_evidence_get", run_id):
                # A06: missing and forbidden runs share one not-found shape.
                return _run_not_found(run_id)
            steps = (
                await session.execute(
                    StepRun.__table__.select()
                    .where(StepRun.flow_run_id == run_id)
                    .order_by(StepRun.id)
                )
            ).all()
        payload = {
            "run_id": run.id,
            "status": run.status,
            "evidence": run.evidence or {},
            "steps": [
                {
                    "name": step.step_name,
                    "status": step.status,
                    "attempt": step.attempt,
                    "finished_at": (step.finished_at.isoformat() if step.finished_at else None),
                }
                for step in steps
            ],
        }
        audit(principal, "run_evidence_get", run.id, "ok")
        return json.dumps(payload, indent=2, default=str)
