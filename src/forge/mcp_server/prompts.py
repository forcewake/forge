from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register_prompts(mcp: FastMCP) -> None:
    """Register all prompt templates on *mcp*."""

    from forge.mcp_server.server import (
        gitlab_client_from,
        resolve_project_id,
    )

    settings = mcp._forge_settings  # type: ignore[attr-defined]

    def _redis():  # type: ignore[no-untyped-def]
        return mcp._forge_redis  # type: ignore[attr-defined]

    @mcp.prompt()
    async def review_merge_request(project: str, mr_iid: int) -> str:
        """Review a merge request for code quality, bugs, and security.

        Args:
            project: Project path or numeric ID.
            mr_iid: Merge request IID.
        """
        async with gitlab_client_from(settings) as gl:
            pid = await resolve_project_id(gl, project, _redis())
            mr = await gl.get_merge_request(pid, mr_iid)
            diff = await gl.get_merge_request_raw_diff(pid, mr_iid)
        return f"""Review this merge request:

Title: {mr.title}
Description: {mr.description or "(no description)"}
Source: {mr.source_branch} → Target: {mr.target_branch}
Labels: {", ".join(mr.labels) if mr.labels else "none"}

Diff:
```
{diff}
```

Look for bugs, security issues, performance problems, and style issues.
Provide specific, actionable feedback with file and line references."""

    @mcp.prompt()
    async def explain_pipeline_failure(project: str, pipeline_id: int) -> str:
        """Diagnose why a CI/CD pipeline failed.

        Args:
            project: Project path or numeric ID.
            pipeline_id: Pipeline ID.
        """
        async with gitlab_client_from(settings) as gl:
            pid = await resolve_project_id(gl, project, _redis())
            pipeline = await gl.get_pipeline(pid, pipeline_id)
            jobs = await gl.list_pipeline_jobs(pid, pipeline_id)
            failed = [j for j in jobs if j.status == "failed"]
            log_sections = []
            for job in failed:
                log = await gl.get_job_log(pid, job.id)
                if len(log) > 2000:
                    log = "... (truncated)\n" + log[-2000:]
                log_sections.append(
                    f"--- Job: {job.name} (stage: {job.stage}) ---\n"
                    f"Failure reason: {job.failure_reason or 'unknown'}\n"
                    f"{log}"
                )
        all_logs = "\n\n".join(log_sections) if log_sections else "No failed jobs found."
        return f"""Diagnose this pipeline failure:

Pipeline #{pipeline.id} — Status: {pipeline.status}
Ref: {pipeline.ref}

Failed job logs:
{all_logs}

Explain what went wrong and suggest how to fix it."""

    @mcp.prompt()
    async def summarize_issue(project: str, issue_iid: int) -> str:
        """Summarize an issue's description and current status.

        Args:
            project: Project path or numeric ID.
            issue_iid: Issue IID.
        """
        async with gitlab_client_from(settings) as gl:
            pid = await resolve_project_id(gl, project, _redis())
            issue = await gl.get_issue(pid, issue_iid)
        return f"""Summarize this GitLab issue:

#{issue.iid} — {issue.title}
State: {issue.state}
Labels: {", ".join(issue.labels) if issue.labels else "none"}

Description:
{issue.description or "(no description)"}

Provide a brief summary of the issue, its current status, and any next steps."""
