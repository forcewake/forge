"""Durable-run MCP tools (ADR-0021 §4, R3 §8.1) — the read surface.

These tools expose the FACTORY STATE (flow runs, frozen RunSpecs, evidence)
over MCP. They read durable Postgres state through the app's session
factory and NEVER touch a provider token — killing the legacy
platform-token passthrough for this surface (a token grants scopes over
forge's state, not forge's GitLab/GitHub identity).

Enforcement is per-call: each tool resolves the principal stashed by
:class:`forge.mcp_server.auth.MCPAuthMiddleware` and requires its scope
before any query runs. Denials return explicit ``FORBIDDEN`` text (the
model-actionable error channel), not exceptions-with-traces.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp import Context

from forge.mcp_server.auth import (
    McpAuthzError,
    audit,
    require_scope,
    session_factory_from_request,
)

#: Session scope: RFC-2119 "defensively read-only" — these tools only SELECT.
_MAX_LIST = 50


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
            query = select(FlowRun).order_by(FlowRun.updated_at.desc()).limit(limit)
            if status:
                query = query.where(FlowRun.status == status)
            if provider:
                query = query.where(FlowRun.provider == provider)
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
        if run is None:
            return f"NOT FOUND: no run {run_id!r}"
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
        from forge.durable.models import RunSpec

        async with _session_factory(ctx)() as session:
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
            if run is None:
                return f"NOT FOUND: no run {run_id!r}"
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
                    "finished_at": (
                        step.finished_at.isoformat() if step.finished_at else None
                    ),
                }
                for step in steps
            ],
        }
        audit(principal, "run_evidence_get", run.id, "ok")
        return json.dumps(payload, indent=2, default=str)
