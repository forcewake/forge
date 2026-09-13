from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

import httpx

from forge.gitlab.schemas import (
    Diff,
    Discussion,
    Issue,
    Job,
    Label,
    MergeRequest,
    MRVersion,
    Note,
    Pipeline,
    Project,
    RepositoryFile,
    TreeEntry,
)

logger = logging.getLogger(__name__)

_RETRYABLE_STATUSES = {429, 500, 502, 503}
_MAX_RETRIES = 3
_BACKOFF_BASE = 1  # seconds
_MAX_PAGES = 50


class GitLabAPIError(Exception):
    """Raised when a GitLab API request fails."""

    def __init__(
        self,
        status_code: int,
        message: str,
        response: httpx.Response | None = None,
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.response = response
        super().__init__(f"GitLab API error {status_code}: {message}")


class CommitOutcomeUnknown(Exception):
    """``create_commit`` timed out and the server-side outcome is unknown (ADR-0005).

    Carries no assumption about whether the commit was created; callers must
    reconcile (list the branch commits) before retrying — never blind-retry.
    """


class GitLabClient:
    """Async client for the GitLab CE REST API v4."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 30.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/api/v4",
            headers={"PRIVATE-TOKEN": token},
            timeout=timeout,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GitLabClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        retry: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make a request with retry logic.

        Retries on 429/500/502/503 with exponential backoff unless *retry* is
        ``False`` — non-idempotent writes (e.g. create-commit) must bypass the
        generic retry so a single HTTP error cannot duplicate side effects.
        Raises immediately on 401/403/404.
        """
        max_attempts = _MAX_RETRIES if retry else 1
        for attempt in range(max_attempts):
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                if attempt < max_attempts - 1:
                    delay = _BACKOFF_BASE * (2**attempt)
                    logger.warning(
                        "HTTP error on %s %s (attempt %d): %s — retrying in %ds",
                        method,
                        path,
                        attempt + 1,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

            if response.status_code < 400:
                return response

            if response.status_code not in _RETRYABLE_STATUSES:
                raise GitLabAPIError(
                    response.status_code,
                    response.text,
                    response,
                )

            # Retryable status
            if attempt < max_attempts - 1:
                if response.status_code == 429:
                    delay = int(response.headers.get("Retry-After", "5"))
                else:
                    delay = _BACKOFF_BASE * (2**attempt)
                logger.warning(
                    "%s %s returned %d (attempt %d) — retrying in %ds",
                    method,
                    path,
                    response.status_code,
                    attempt + 1,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                raise GitLabAPIError(
                    response.status_code,
                    response.text,
                    response,
                )

        # Should not reach here, but just in case
        raise GitLabAPIError(0, "Max retries exhausted")  # pragma: no cover

    async def _get(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._request("GET", path, **kwargs)

    async def _post(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._request("POST", path, **kwargs)

    async def _put(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._request("PUT", path, **kwargs)

    async def _delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._request("DELETE", path, **kwargs)

    async def _paginated(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all pages of a paginated GitLab API endpoint."""
        params = dict(params or {})
        params.setdefault("per_page", 100)

        results: list[dict[str, Any]] = []

        for _ in range(_MAX_PAGES):
            response = await self._get(path, params=params)
            data = response.json()
            if isinstance(data, list):
                results.extend(data)
            else:
                results.append(data)

            next_page = response.headers.get("x-next-page", "").strip()
            if not next_page:
                break
            params["page"] = int(next_page)

        return results

    async def get_merge_request(self, project_id: int, mr_iid: int) -> MergeRequest:
        resp = await self._get(f"/projects/{project_id}/merge_requests/{mr_iid}")
        return MergeRequest.model_validate(resp.json())

    async def get_merge_request_diffs(self, project_id: int, mr_iid: int) -> list[Diff]:
        raw = await self._paginated(f"/projects/{project_id}/merge_requests/{mr_iid}/diffs")
        return [Diff.model_validate(d) for d in raw]

    async def get_merge_request_raw_diff(self, project_id: int, mr_iid: int) -> str:
        resp = await self._get(
            f"/projects/{project_id}/merge_requests/{mr_iid}/diffs",
            headers={"Accept": "text/plain"},
        )
        return resp.text

    async def get_merge_request_versions(self, project_id: int, mr_iid: int) -> list[MRVersion]:
        raw = await self._paginated(f"/projects/{project_id}/merge_requests/{mr_iid}/versions")
        return [MRVersion.model_validate(v) for v in raw]

    async def list_discussions(self, project_id: int, mr_iid: int) -> list[Discussion]:
        raw = await self._paginated(f"/projects/{project_id}/merge_requests/{mr_iid}/discussions")
        return [Discussion.model_validate(d) for d in raw]

    async def create_mr_note(self, project_id: int, mr_iid: int, body: str) -> Note:
        resp = await self._post(
            f"/projects/{project_id}/merge_requests/{mr_iid}/notes",
            json={"body": body},
        )
        return Note.model_validate(resp.json())

    async def create_mr_discussion(
        self,
        project_id: int,
        mr_iid: int,
        body: str,
        position: dict[str, Any] | None = None,
    ) -> Discussion:
        payload: dict[str, Any] = {"body": body}
        if position:
            payload["position"] = position
        resp = await self._post(
            f"/projects/{project_id}/merge_requests/{mr_iid}/discussions",
            json=payload,
        )
        return Discussion.model_validate(resp.json())

    async def reply_to_discussion(
        self,
        project_id: int,
        mr_iid: int,
        discussion_id: str,
        body: str,
    ) -> Note:
        resp = await self._post(
            f"/projects/{project_id}/merge_requests/{mr_iid}/discussions/{discussion_id}/notes",
            json={"body": body},
        )
        return Note.model_validate(resp.json())

    async def resolve_discussion(
        self,
        project_id: int,
        mr_iid: int,
        discussion_id: str,
        resolved: bool = True,
    ) -> Discussion:
        resp = await self._put(
            f"/projects/{project_id}/merge_requests/{mr_iid}/discussions/{discussion_id}",
            json={"resolved": resolved},
        )
        return Discussion.model_validate(resp.json())

    async def get_file(self, project_id: int, file_path: str, ref: str = "HEAD") -> RepositoryFile:
        encoded = quote(file_path, safe="")
        resp = await self._get(
            f"/projects/{project_id}/repository/files/{encoded}",
            params={"ref": ref},
        )
        return RepositoryFile.model_validate(resp.json())

    async def get_tree(
        self,
        project_id: int,
        path: str = "",
        ref: str = "HEAD",
        recursive: bool = False,
    ) -> list[TreeEntry]:
        params: dict[str, Any] = {"ref": ref, "recursive": recursive}
        if path:
            params["path"] = path
        raw = await self._paginated(f"/projects/{project_id}/repository/tree", params=params)
        return [TreeEntry.model_validate(e) for e in raw]

    async def get_pipeline(self, project_id: int, pipeline_id: int) -> Pipeline:
        resp = await self._get(f"/projects/{project_id}/pipelines/{pipeline_id}")
        return Pipeline.model_validate(resp.json())

    async def list_pipeline_jobs(self, project_id: int, pipeline_id: int) -> list[Job]:
        raw = await self._paginated(f"/projects/{project_id}/pipelines/{pipeline_id}/jobs")
        return [Job.model_validate(j) for j in raw]

    async def get_job_log(self, project_id: int, job_id: int, tail: int | None = None) -> str:
        """Fetch a job's raw trace; with *tail*, only its last *tail* chars."""
        resp = await self._get(f"/projects/{project_id}/jobs/{job_id}/trace")
        text = resp.text
        if tail is not None and len(text) > tail:
            return text[-tail:]
        return text

    async def get_job_artifacts_file(
        self, project_id: int, job_id: int, artifact_path: str
    ) -> bytes:
        """Download a single file from a job's artifacts archive."""
        encoded_path = quote(artifact_path, safe="")
        resp = await self._get(f"/projects/{project_id}/jobs/{job_id}/artifacts/{encoded_path}")
        return resp.content

    async def add_mr_labels(self, project_id: int, mr_iid: int, labels: list[str]) -> MergeRequest:
        resp = await self._put(
            f"/projects/{project_id}/merge_requests/{mr_iid}",
            json={"add_labels": ",".join(labels)},
        )
        return MergeRequest.model_validate(resp.json())

    async def remove_mr_labels(
        self, project_id: int, mr_iid: int, labels: list[str]
    ) -> MergeRequest:
        resp = await self._put(
            f"/projects/{project_id}/merge_requests/{mr_iid}",
            json={"remove_labels": ",".join(labels)},
        )
        return MergeRequest.model_validate(resp.json())

    async def ensure_label_exists(
        self,
        project_id: int,
        name: str,
        color: str,
        description: str = "",
    ) -> dict[str, Any]:
        try:
            resp = await self._post(
                f"/projects/{project_id}/labels",
                json={
                    "name": name,
                    "color": color,
                    "description": description,
                },
            )
            return resp.json()
        except GitLabAPIError as exc:
            if exc.status_code == 409:
                return {"name": name, "exists": True}
            raise

    async def create_commit_comment(self, project_id: int, sha: str, body: str) -> dict[str, Any]:
        """Post a comment on a commit."""
        resp = await self._post(
            f"/projects/{project_id}/repository/commits/{sha}/comments",
            json={"note": body},
        )
        return resp.json()

    async def list_project_hooks(self, project_id: int) -> list[dict[str, Any]]:
        return await self._paginated(f"/projects/{project_id}/hooks")

    async def create_project_hook(
        self,
        project_id: int,
        url: str,
        token: str,
        *,
        push_events: bool = True,
        merge_requests_events: bool = True,
        note_events: bool = True,
        pipeline_events: bool = True,
        job_events: bool = True,
        issues_events: bool = False,
    ) -> dict[str, Any]:
        resp = await self._post(
            f"/projects/{project_id}/hooks",
            json={
                "url": url,
                "token": token,
                "push_events": push_events,
                "merge_requests_events": merge_requests_events,
                "note_events": note_events,
                "pipeline_events": pipeline_events,
                "job_events": job_events,
                "issues_events": issues_events,
            },
        )
        return resp.json()

    async def delete_project_hook(self, project_id: int, hook_id: int) -> None:
        await self._delete(f"/projects/{project_id}/hooks/{hook_id}")

    async def compare_commits(
        self,
        project_id: int,
        from_sha: str,
        to_sha: str,
    ) -> dict[str, Any]:
        """Compare two commits via ``GET /projects/:id/repository/compare``."""
        resp = await self._get(
            f"/projects/{project_id}/repository/compare",
            params={"from": from_sha, "to": to_sha},
        )
        return resp.json()

    async def compare_commits_raw_diff(
        self,
        project_id: int,
        from_sha: str,
        to_sha: str,
    ) -> str:
        """Return unified diff text between two commits."""
        data = await self.compare_commits(project_id, from_sha, to_sha)
        parts: list[str] = []
        for d in data.get("diffs", []):
            old_path = d.get("old_path", "")
            new_path = d.get("new_path", "")
            diff_text = d.get("diff", "")
            if diff_text:
                parts.append(f"diff --git a/{old_path} b/{new_path}")
                parts.append(diff_text)
        return "\n".join(parts)

    async def create_branch(
        self,
        project_id: int,
        branch_name: str,
        ref: str = "main",
    ) -> dict[str, Any]:
        """Create a new branch from a ref."""
        resp = await self._post(
            f"/projects/{project_id}/repository/branches",
            json={"branch": branch_name, "ref": ref},
        )
        return resp.json()

    async def get_branch(self, project_id: int, branch_name: str) -> dict[str, Any]:
        """Fetch a single branch (``GET /projects/:id/repository/branches/:branch``)."""
        encoded = quote(branch_name, safe="")
        resp = await self._get(f"/projects/{project_id}/repository/branches/{encoded}")
        return resp.json()

    async def create_commit(
        self,
        project_id: int,
        branch: str,
        actions: list[dict[str, Any]],
        commit_message: str,
        start_branch: str | None = None,
    ) -> dict[str, Any]:
        """Create a commit via the Commits API.

        ``POST /projects/:id/repository/commits`` with ``branch``,
        ``commit_message``, ``actions`` (``{action, file_path, content}``) and
        optional ``start_branch``.

        This call is **never auto-retried** (non-idempotent): a lost response
        must not duplicate a commit. On ``httpx.TimeoutException`` raises
        :class:`CommitOutcomeUnknown` — the caller must reconcile (compare the
        branch commits against *commit_message*) before any retry (ADR-0005).
        """
        payload: dict[str, Any] = {
            "branch": branch,
            "commit_message": commit_message,
            "actions": actions,
        }
        if start_branch is not None:
            payload["start_branch"] = start_branch
        try:
            resp = await self._request(
                "POST",
                f"/projects/{project_id}/repository/commits",
                retry=False,
                json=payload,
            )
        except httpx.TimeoutException as exc:
            raise CommitOutcomeUnknown(
                f"create_commit on branch {branch!r} timed out; outcome unknown"
            ) from exc
        return resp.json()

    async def list_commits(self, project_id: int, ref: str) -> list[dict[str, str]]:
        """List commits on a ref, newest first (``GET /repository/commits``).

        Returns ``{"sha", "short_id", "message"}`` dicts — used for outcome
        reconciliation after an unknown create_commit (match by message) and
        for branch-head drift checks.
        """
        raw = await self._paginated(
            f"/projects/{project_id}/repository/commits",
            params={"ref_name": ref},
        )
        return [
            {
                "sha": c.get("id", ""),
                "short_id": c.get("short_id", ""),
                "message": c.get("message", ""),
            }
            for c in raw
        ]

    async def create_pipeline(
        self,
        project_id: int,
        ref: str,
        variables: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Create a pipeline for a ref (``POST /projects/:id/pipeline``).

        *variables* is an optional list of ``{"key", "value"}`` dicts passed
        as pipeline variables — the ci_harness backend uses it to hand the
        harness job its task brief (ADR-0015).
        """
        payload: dict[str, Any] = {"ref": ref}
        if variables:
            payload["variables"] = [
                {"key": str(v["key"]), "value": str(v["value"])} for v in variables
            ]
        resp = await self._post(f"/projects/{project_id}/pipeline", json=payload)
        return resp.json()

    async def create_merge_request(
        self,
        project_id: int,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str = "",
        *,
        assignee_id: int | None = None,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new merge request."""
        payload: dict[str, Any] = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
        }
        if assignee_id is not None:
            payload["assignee_id"] = assignee_id
        if labels:
            payload["labels"] = ",".join(labels)
        resp = await self._post(
            f"/projects/{project_id}/merge_requests",
            json=payload,
        )
        return resp.json()

    async def update_merge_request(
        self,
        project_id: int,
        mr_iid: int,
        description: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Update an existing merge request (``PUT /projects/:id/merge_requests/:iid``)."""
        payload: dict[str, Any] = {}
        if description is not None:
            payload["description"] = description
        if title is not None:
            payload["title"] = title
        resp = await self._put(
            f"/projects/{project_id}/merge_requests/{mr_iid}",
            json=payload,
        )
        return resp.json()

    async def create_issue_note(
        self,
        project_id: int,
        issue_iid: int,
        body: str,
    ) -> dict[str, Any]:
        """Post a note/comment on an issue."""
        resp = await self._post(
            f"/projects/{project_id}/issues/{issue_iid}/notes",
            json={"body": body},
        )
        return resp.json()

    async def list_group_projects(self, group_id: int) -> list[dict[str, Any]]:
        return await self._paginated(
            f"/groups/{group_id}/projects",
            params={"include_subgroups": True},
        )

    async def get_project(self, project_id_or_path: int | str) -> Project:
        """Get a project by numeric ID or URL-encoded path (e.g. ``group/project``)."""
        if isinstance(project_id_or_path, str) and not project_id_or_path.isdigit():
            encoded = quote(project_id_or_path, safe="")
        else:
            encoded = str(project_id_or_path)
        resp = await self._get(f"/projects/{encoded}")
        return Project.model_validate(resp.json())

    async def list_merge_requests(
        self,
        project_id: int,
        state: str = "opened",
        per_page: int = 20,
    ) -> list[MergeRequest]:
        raw = await self._paginated(
            f"/projects/{project_id}/merge_requests",
            params={"state": state, "per_page": per_page},
        )
        return [MergeRequest.model_validate(mr) for mr in raw]

    async def get_issue(self, project_id: int, issue_iid: int) -> Issue:
        resp = await self._get(f"/projects/{project_id}/issues/{issue_iid}")
        return Issue.model_validate(resp.json())

    async def list_issues(
        self,
        project_id: int,
        state: str = "opened",
        labels: str | None = None,
        per_page: int = 20,
    ) -> list[Issue]:
        params: dict[str, Any] = {"state": state, "per_page": per_page}
        if labels:
            params["labels"] = labels
        raw = await self._paginated(f"/projects/{project_id}/issues", params=params)
        return [Issue.model_validate(i) for i in raw]

    async def create_issue(
        self,
        project_id: int,
        title: str,
        description: str = "",
        labels: list[str] | None = None,
    ) -> Issue:
        payload: dict[str, Any] = {"title": title, "description": description}
        if labels:
            payload["labels"] = ",".join(labels)
        resp = await self._post(f"/projects/{project_id}/issues", json=payload)
        return Issue.model_validate(resp.json())

    async def search_code(self, project_id: int, query: str) -> list[dict[str, Any]]:
        """Search for code in a project using GitLab's search API."""
        return await self._paginated(
            f"/projects/{project_id}/search",
            params={"scope": "blobs", "search": query},
        )

    async def list_project_labels(self, project_id: int) -> list[Label]:
        raw = await self._paginated(f"/projects/{project_id}/labels")
        return [Label.model_validate(lb) for lb in raw]

    async def list_pipelines(
        self,
        project_id: int,
        ref: str | None = None,
        status: str | None = None,
        sha: str | None = None,
        per_page: int = 20,
    ) -> list[Pipeline]:
        params: dict[str, Any] = {
            "per_page": per_page,
            "order_by": "id",
            "sort": "desc",
        }
        if ref:
            params["ref"] = ref
        if status:
            params["status"] = status
        if sha:
            params["sha"] = sha
        raw = await self._paginated(f"/projects/{project_id}/pipelines", params=params)
        return [Pipeline.model_validate(p) for p in raw]
