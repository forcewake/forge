from __future__ import annotations

from typing import TYPE_CHECKING

from forge.gitlab.client import GitLabAPIError
from forge.mcp_server.auth import guarded_tool

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

#: Classic-surface scope mapping (R19; closed set in auth.MCP_SCOPES).
#: Read-only helpers (list/get/search/tree) need ``forge:read``; anything
#: that mutates GitLab — comments, issue creation — needs the write scope.
#: Approve/cancel actions would map to ``forge:approvals:write``; none
#: exist on the classic surface yet.
READ_SCOPE = "forge:read"
WRITE_SCOPE = "forge:runs:write"


def register_tools(mcp: FastMCP) -> None:
    """Register all GitLab tools on *mcp* (each behind its required scope)."""

    # Import here to avoid circular refs; helpers close over the mcp instance.
    from forge.mcp_server.server import (
        gitlab_client_from,
        resolve_project_id,
    )

    settings = mcp._forge_settings  # type: ignore[attr-defined]

    def _redis():  # type: ignore[no-untyped-def]
        return mcp._forge_redis  # type: ignore[attr-defined]

    @guarded_tool(mcp, READ_SCOPE, repo_arg="project")
    async def list_merge_requests(
        project: str,
        state: str = "opened",
        max_results: int = 10,
    ) -> str:
        """List merge requests for a GitLab project.

        Args:
            project: Project path (e.g. "mygroup/myproject") or numeric ID.
            state: Filter by state: opened, closed, merged, all.
            max_results: Maximum number of results to return.
        """
        try:
            async with gitlab_client_from(settings) as gl:
                pid = await resolve_project_id(gl, project, _redis())
                mrs = await gl.list_merge_requests(pid, state=state, per_page=max_results)
            lines = [f"Merge requests ({state}) for {project}:\n"]
            for mr in mrs[:max_results]:
                draft = " [DRAFT]" if mr.draft else ""
                lines.append(f"  !{mr.iid} — {mr.title}{draft}  ({mr.state})")
                if mr.web_url:
                    lines.append(f"         {mr.web_url}")
            return "\n".join(lines) if len(lines) > 1 else "No merge requests found."
        except GitLabAPIError as exc:
            return f"Error: {exc.message}"

    @guarded_tool(mcp, READ_SCOPE, repo_arg="project")
    async def get_merge_request(project: str, mr_iid: int) -> str:
        """Get details of a specific merge request.

        Args:
            project: Project path or numeric ID.
            mr_iid: Merge request IID (the project-scoped number).
        """
        try:
            async with gitlab_client_from(settings) as gl:
                pid = await resolve_project_id(gl, project, _redis())
                mr = await gl.get_merge_request(pid, mr_iid)
            parts = [
                f"!{mr.iid} — {mr.title}",
                f"State: {mr.state}",
                f"Source: {mr.source_branch} → Target: {mr.target_branch}",
            ]
            if mr.description:
                parts.append(f"Description:\n{mr.description}")
            if mr.labels:
                parts.append(f"Labels: {', '.join(mr.labels)}")
            if mr.web_url:
                parts.append(f"URL: {mr.web_url}")
            return "\n".join(parts)
        except GitLabAPIError as exc:
            return f"Error: {exc.message}"

    @guarded_tool(mcp, READ_SCOPE, repo_arg="project")
    async def get_merge_request_diff(project: str, mr_iid: int) -> str:
        """Get the unified diff for a merge request.

        Args:
            project: Project path or numeric ID.
            mr_iid: Merge request IID.
        """
        try:
            async with gitlab_client_from(settings) as gl:
                pid = await resolve_project_id(gl, project, _redis())
                diff = await gl.get_merge_request_raw_diff(pid, mr_iid)
            return diff or "No changes in this merge request."
        except GitLabAPIError as exc:
            return f"Error: {exc.message}"

    @guarded_tool(mcp, WRITE_SCOPE, repo_arg="project")
    async def post_merge_request_comment(project: str, mr_iid: int, body: str) -> str:
        """Post a comment on a merge request.

        Args:
            project: Project path or numeric ID.
            mr_iid: Merge request IID.
            body: Comment text (Markdown supported).
        """
        try:
            async with gitlab_client_from(settings) as gl:
                pid = await resolve_project_id(gl, project, _redis())
                note = await gl.create_mr_note(pid, mr_iid, body)
            return f"Comment posted (note #{note.id})."
        except GitLabAPIError as exc:
            return f"Error: {exc.message}"

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------

    @guarded_tool(mcp, READ_SCOPE, repo_arg="project")
    async def list_issues(
        project: str,
        state: str = "opened",
        labels: str = "",
        max_results: int = 10,
    ) -> str:
        """List issues for a GitLab project.

        Args:
            project: Project path or numeric ID.
            state: Filter by state: opened, closed, all.
            labels: Comma-separated label names to filter by.
            max_results: Maximum number of results.
        """
        try:
            async with gitlab_client_from(settings) as gl:
                pid = await resolve_project_id(gl, project, _redis())
                issues = await gl.list_issues(
                    pid,
                    state=state,
                    labels=labels or None,
                    per_page=max_results,
                )
            lines = [f"Issues ({state}) for {project}:\n"]
            for issue in issues[:max_results]:
                lbl = f"  [{', '.join(issue.labels)}]" if issue.labels else ""
                lines.append(f"  #{issue.iid} — {issue.title}  ({issue.state}){lbl}")
            return "\n".join(lines) if len(lines) > 1 else "No issues found."
        except GitLabAPIError as exc:
            return f"Error: {exc.message}"

    @guarded_tool(mcp, READ_SCOPE, repo_arg="project")
    async def get_issue(project: str, issue_iid: int) -> str:
        """Get details of a specific issue.

        Args:
            project: Project path or numeric ID.
            issue_iid: Issue IID (project-scoped number).
        """
        try:
            async with gitlab_client_from(settings) as gl:
                pid = await resolve_project_id(gl, project, _redis())
                issue = await gl.get_issue(pid, issue_iid)
            parts = [
                f"#{issue.iid} — {issue.title}",
                f"State: {issue.state}",
            ]
            if issue.description:
                parts.append(f"Description:\n{issue.description}")
            if issue.labels:
                parts.append(f"Labels: {', '.join(issue.labels)}")
            if issue.web_url:
                parts.append(f"URL: {issue.web_url}")
            return "\n".join(parts)
        except GitLabAPIError as exc:
            return f"Error: {exc.message}"

    @guarded_tool(mcp, WRITE_SCOPE, repo_arg="project")
    async def create_issue(
        project: str,
        title: str,
        description: str = "",
        labels: str = "",
    ) -> str:
        """Create a new issue in a GitLab project.

        Args:
            project: Project path or numeric ID.
            title: Issue title.
            description: Issue description (Markdown).
            labels: Comma-separated label names.
        """
        try:
            label_list = [s.strip() for s in labels.split(",") if s.strip()] if labels else None
            async with gitlab_client_from(settings) as gl:
                pid = await resolve_project_id(gl, project, _redis())
                issue = await gl.create_issue(
                    pid, title, description=description, labels=label_list
                )
            return f"Issue created: #{issue.iid} — {issue.title}\n{issue.web_url or ''}"
        except GitLabAPIError as exc:
            return f"Error: {exc.message}"

    @guarded_tool(mcp, READ_SCOPE, repo_arg="project")
    async def get_file_content(project: str, file_path: str, ref: str = "HEAD") -> str:
        """Get the content of a file from the repository.

        Args:
            project: Project path or numeric ID.
            file_path: Path to the file in the repository.
            ref: Branch, tag, or commit SHA (default HEAD).
        """
        try:
            async with gitlab_client_from(settings) as gl:
                pid = await resolve_project_id(gl, project, _redis())
                f = await gl.get_file(pid, file_path, ref=ref)
            import base64

            if f.encoding == "base64":
                return base64.b64decode(f.content).decode("utf-8", errors="replace")
            return f.content
        except GitLabAPIError as exc:
            return f"Error: {exc.message}"

    @guarded_tool(mcp, READ_SCOPE, repo_arg="project")
    async def get_repository_tree(
        project: str,
        path: str = "",
        ref: str = "HEAD",
    ) -> str:
        """List files and directories in the repository.

        Args:
            project: Project path or numeric ID.
            path: Subdirectory path (empty for root).
            ref: Branch, tag, or commit SHA.
        """
        try:
            async with gitlab_client_from(settings) as gl:
                pid = await resolve_project_id(gl, project, _redis())
                entries = await gl.get_tree(pid, path=path, ref=ref)
            lines = [f"Repository tree for {project} (ref={ref}, path={path or '/'}):\n"]
            for e in entries:
                icon = "📁" if e.type == "tree" else "📄"
                lines.append(f"  {icon} {e.path}")
            return "\n".join(lines) if len(lines) > 1 else "Empty directory."
        except GitLabAPIError as exc:
            return f"Error: {exc.message}"

    @guarded_tool(mcp, READ_SCOPE, repo_arg="project")
    async def search_code(project: str, query: str) -> str:
        """Search for code in a repository using GitLab's search API.

        Args:
            project: Project path or numeric ID.
            query: Search query string.
        """
        try:
            async with gitlab_client_from(settings) as gl:
                pid = await resolve_project_id(gl, project, _redis())
                results = await gl.search_code(pid, query)
            if not results:
                return f"No results for '{query}'."
            lines = [f"Code search results for '{query}':\n"]
            for r in results[:20]:
                lines.append(f"  {r.get('filename', '?')} (ref: {r.get('ref', '?')})")
                data = r.get("data", "")
                if data:
                    # Show first 200 chars of matched content
                    preview = data[:200].replace("\n", "\n    ")
                    lines.append(f"    {preview}")
            return "\n".join(lines)
        except GitLabAPIError as exc:
            return f"Error: {exc.message}"

    @guarded_tool(mcp, READ_SCOPE, repo_arg="project")
    async def get_pipeline_status(project: str, ref: str = "main") -> str:
        """Get the latest pipeline status for a branch.

        Args:
            project: Project path or numeric ID.
            ref: Branch name (default "main").
        """
        try:
            async with gitlab_client_from(settings) as gl:
                pid = await resolve_project_id(gl, project, _redis())
                pipelines = await gl.list_pipelines(pid, ref=ref, per_page=1)
            if not pipelines:
                return f"No pipelines found for ref '{ref}'."
            p = pipelines[0]
            parts = [
                f"Pipeline #{p.id} for ref '{ref}'",
                f"Status: {p.status}",
            ]
            if p.web_url:
                parts.append(f"URL: {p.web_url}")
            return "\n".join(parts)
        except GitLabAPIError as exc:
            return f"Error: {exc.message}"

    @guarded_tool(mcp, READ_SCOPE, repo_arg="project")
    async def get_failed_pipeline_logs(project: str, pipeline_id: int) -> str:
        """Get logs from failed jobs in a pipeline.

        Args:
            project: Project path or numeric ID.
            pipeline_id: Pipeline ID.
        """
        try:
            async with gitlab_client_from(settings) as gl:
                pid = await resolve_project_id(gl, project, _redis())
                jobs = await gl.list_pipeline_jobs(pid, pipeline_id)
                failed = [j for j in jobs if j.status == "failed"]
                if not failed:
                    return f"No failed jobs in pipeline #{pipeline_id}."
                parts = [f"Failed jobs in pipeline #{pipeline_id}:\n"]
                for job in failed:
                    log = await gl.get_job_log(pid, job.id)
                    # Truncate long logs to last 2000 chars
                    if len(log) > 2000:
                        log = "... (truncated)\n" + log[-2000:]
                    parts.append(f"--- Job: {job.name} (stage: {job.stage}) ---")
                    if job.failure_reason:
                        parts.append(f"Failure reason: {job.failure_reason}")
                    parts.append(log)
            return "\n".join(parts)
        except GitLabAPIError as exc:
            return f"Error: {exc.message}"

    @guarded_tool(mcp, READ_SCOPE, repo_arg="project")
    async def list_project_labels(project: str) -> str:
        """List all labels for a project.

        Args:
            project: Project path or numeric ID.
        """
        try:
            async with gitlab_client_from(settings) as gl:
                pid = await resolve_project_id(gl, project, _redis())
                labels = await gl.list_project_labels(pid)
            if not labels:
                return "No labels found."
            lines = [f"Labels for {project}:\n"]
            for lb in labels:
                desc = f" — {lb.description}" if lb.description else ""
                lines.append(f"  {lb.name}{desc}")
            return "\n".join(lines)
        except GitLabAPIError as exc:
            return f"Error: {exc.message}"
