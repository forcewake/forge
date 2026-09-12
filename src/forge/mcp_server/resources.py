from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from forge.gitlab.client import GitLabAPIError

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register_resources(mcp: FastMCP) -> None:
    """Register all GitLab resources on *mcp*."""

    from forge.mcp_server.server import (
        gitlab_client_from,
        resolve_project_id,
    )

    settings = mcp._forge_settings  # type: ignore[attr-defined]

    def _redis():  # type: ignore[no-untyped-def]
        return mcp._forge_redis  # type: ignore[attr-defined]

    @mcp.resource("gitlab://project/{project_path}/readme")
    async def get_readme(project_path: str) -> str:
        """The project's README file content."""
        try:
            async with gitlab_client_from(settings) as gl:
                pid = await resolve_project_id(gl, project_path, _redis())
                f = await gl.get_file(pid, "README.md")
            if f.encoding == "base64":
                return base64.b64decode(f.content).decode("utf-8", errors="replace")
            return f.content
        except GitLabAPIError:
            return "README.md not found."

    @mcp.resource("gitlab://project/{project_path}/ci-config")
    async def get_ci_config(project_path: str) -> str:
        """The project's .gitlab-ci.yml configuration."""
        try:
            async with gitlab_client_from(settings) as gl:
                pid = await resolve_project_id(gl, project_path, _redis())
                f = await gl.get_file(pid, ".gitlab-ci.yml")
            if f.encoding == "base64":
                return base64.b64decode(f.content).decode("utf-8", errors="replace")
            return f.content
        except GitLabAPIError:
            return ".gitlab-ci.yml not found."

    @mcp.resource("gitlab://project/{project_path}/merge-requests/open")
    async def get_open_mrs(project_path: str) -> str:
        """Summary of all open merge requests."""
        try:
            async with gitlab_client_from(settings) as gl:
                pid = await resolve_project_id(gl, project_path, _redis())
                mrs = await gl.list_merge_requests(pid, state="opened")
            if not mrs:
                return "No open merge requests."
            lines = []
            for mr in mrs:
                draft = " [DRAFT]" if mr.draft else ""
                lines.append(f"!{mr.iid} — {mr.title}{draft}")
                lines.append(f"  {mr.source_branch} → {mr.target_branch}")
            return "\n".join(lines)
        except GitLabAPIError as exc:
            return f"Error: {exc.message}"

    @mcp.resource("gitlab://project/{project_path}/issues/open")
    async def get_open_issues(project_path: str) -> str:
        """Summary of all open issues."""
        try:
            async with gitlab_client_from(settings) as gl:
                pid = await resolve_project_id(gl, project_path, _redis())
                issues = await gl.list_issues(pid, state="opened")
            if not issues:
                return "No open issues."
            lines = []
            for issue in issues:
                lbl = f" [{', '.join(issue.labels)}]" if issue.labels else ""
                lines.append(f"#{issue.iid} — {issue.title}{lbl}")
            return "\n".join(lines)
        except GitLabAPIError as exc:
            return f"Error: {exc.message}"

    @mcp.resource("gitlab://project/{project_path}/pipelines/latest")
    async def get_latest_pipeline(project_path: str) -> str:
        """Latest pipeline status and job results."""
        try:
            async with gitlab_client_from(settings) as gl:
                pid = await resolve_project_id(gl, project_path, _redis())
                pipelines = await gl.list_pipelines(pid, per_page=1)
            if not pipelines:
                return "No pipelines found."
            p = pipelines[0]
            parts = [
                f"Pipeline #{p.id}",
                f"Status: {p.status}",
                f"Ref: {p.ref}",
            ]
            if p.web_url:
                parts.append(f"URL: {p.web_url}")
            return "\n".join(parts)
        except GitLabAPIError as exc:
            return f"Error: {exc.message}"
