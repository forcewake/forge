"""Shared in-memory GitHub fake for the slice tests.

No network, no live services: every method mirrors the PARSED semantics of
:class:`forge.integrations.github.GitHubClient` (the same level the flow
consumes), including the branch-wide CAS of ``create_commit_on_branch`` —
a moved head surfaces as :class:`GitHubStaleBranchError`, never as a silent
overwrite — and the base64 blob handling of the contents API. The Actions
surface (E3b) mirrors the parsed shapes of the workflow-run / artifact
endpoints, including the 2026 dispatch run-id response and the legacy
empty-202 fallback.
"""

from __future__ import annotations

import base64
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

from forge.gitlab.schemas import Issue, RepositoryFile, TreeEntry
from forge.integrations.github import GitHubAPIError, GitHubStaleBranchError


class FakeGitHub:
    """In-memory GitHub covering the publish-flow and reader surface."""

    def __init__(self) -> None:
        # full_name -> branch -> head sha
        self.heads: dict[str, dict[str, str]] = {}
        # full_name -> path -> text content (single snapshot, like FakeGitLab)
        self.files: dict[str, dict[str, str]] = {}
        # full_name -> issue number -> issue dict
        self.issues: dict[str, dict[int, dict]] = {}
        # full_name -> list of PR dicts
        self.pull_requests: dict[str, list[dict]] = {}
        # pr number -> list of PR file dicts (filename / patch), the reviewer's
        # diff surface
        self.pr_files: dict[int, list[dict]] = {}
        # pr number -> list of review dicts, chronological (the incremental
        # anchor: forge's own review commit_ids)
        self.reviews: dict[int, list[dict]] = {}
        # (before, after) -> compare dict {"files": [...]} — the synchronize
        # delta surface
        self.compares: dict[tuple[str, str], dict] = {}
        # full_name -> issue/PR number -> list of comment dicts (the sticky
        # progress-comment surface)
        self.issue_comments: dict[str, dict[int, list[dict]]] = {}
        # identity reviews/comments are authored with (the App's bot login)
        self.reviewer_login: str = "forge-app[bot]"
        # sha -> list of check-run dicts
        self.check_runs: dict[str, list[dict]] = {}
        # list of workflow-run dicts (head_sha / name keyed filtering)
        self.workflow_runs: list[dict] = []
        # Actions state (E3b): dispatch mode ("run_id" | "legacy"),
        # workflow-run dicts, per-run artifacts, per-job logs, cancellations
        self.dispatch_mode: str = "run_id"
        self.dispatch_inputs: list[dict] = []
        self.actions_runs: list[dict] = []
        self.actions_artifacts: dict[int, list[dict]] = {}
        self.artifact_zips: dict[int, bytes] = {}
        self.job_logs: dict[int, str] = {}
        self.actions_jobs: dict[int, list[dict]] = {}
        self.cancelled_runs: list[int] = []
        # Security alert surfaces (findings ingestion, ci-security-surface
        # §3/§4): full_name -> list of alert dicts; the *_disabled knobs
        # raise the documented 403 "feature not enabled" per repo.
        self.code_scanning_alerts: dict[str, list[dict]] = {}
        self.secret_scanning_alerts: dict[str, list[dict]] = {}
        self.dependabot_alerts: dict[str, list[dict]] = {}
        self.code_scanning_disabled: bool = False
        self.secret_scanning_disabled: bool = False
        self.calls: list[tuple[str, tuple]] = []
        self._next_number = 100
        self._next_oid = 1
        self._next_actions_run = 500
        self._next_artifact = 900

    # -- ids ----------------------------------------------------------------

    def _oid(self) -> str:
        self._next_oid += 1
        return f"{self._next_oid:040x}"

    def _number(self) -> int:
        self._next_number += 1
        return self._next_number

    # -- seeding --------------------------------------------------------------

    def seed_repo(self, full_name: str, files: dict[str, str] | None = None) -> None:
        """Seed a repository whose default branch sits at one root commit."""
        root = "0" * 40
        self.heads.setdefault(full_name, {})["main"] = root
        self.files.setdefault(full_name, {}).update(files or {})

    def seed_issue(self, full_name: str, number: int, title: str, body: str = "") -> None:
        self.issues.setdefault(full_name, {})[number] = {
            "id": number,
            "number": number,
            "title": title,
            "body": body,
            "state": "open",
        }

    def seed_check_runs(self, sha: str, runs: list[dict]) -> None:
        self.check_runs[sha] = runs

    def seed_workflow_runs(self, runs: list[dict]) -> None:
        self.workflow_runs.extend(runs)

    # -- refs / branches --------------------------------------------------------

    async def get_branch_head(self, owner: str, repo: str, branch: str) -> str:
        self.calls.append(("get_branch_head", (owner, repo, branch)))
        full = f"{owner}/{repo}"
        head = self.heads.get(full, {}).get(branch)
        if head is None:
            raise GitHubAPIError(404, f"branch {branch!r} not found")
        return head

    async def create_branch(self, owner: str, repo: str, branch: str, sha: str) -> dict:
        self.calls.append(("create_branch", (owner, repo, branch, sha)))
        full = f"{owner}/{repo}"
        branches = self.heads.setdefault(full, {})
        if branch in branches:
            raise GitHubAPIError(422, f"Reference already exists: refs/heads/{branch}")
        branches[branch] = sha
        return {"ref": f"refs/heads/{branch}", "object": {"sha": sha, "type": "commit"}}

    # -- GraphQL commit ------------------------------------------------------------

    async def create_commit_on_branch(
        self,
        owner: str,
        repo: str,
        branch: str,
        *,
        headline: str,
        body: str | None = None,
        additions: list[tuple[str, str]] | None = None,
        deletions: list[str] | None = None,
        expected_head_oid: str,
        client_mutation_id: str = "",
    ) -> dict:
        self.calls.append(
            (
                "create_commit_on_branch",
                (owner, repo, branch, tuple(p for p, _ in additions or []), tuple(deletions or [])),
            )
        )
        full = f"{owner}/{repo}"
        head = self.heads.get(full, {}).get(branch)
        if head != expected_head_oid:
            raise GitHubStaleBranchError(
                branch,
                expected_head_oid,
                f'Expected branch to point to "{expected_head_oid}" but it did not.',
            )
        for path, content in additions or []:
            self.files.setdefault(full, {})[path] = content
        for path in deletions or []:
            self.files.setdefault(full, {}).pop(path, None)
        new_oid = self._oid()
        self.heads.setdefault(full, {})[branch] = new_oid
        return {
            "oid": new_oid,
            "url": f"https://github.test/{full}/commit/{new_oid}",
            "client_mutation_id": client_mutation_id,
        }

    # -- pull requests ----------------------------------------------------------------

    async def create_draft_pr(
        self, owner: str, repo: str, head: str, base: str, title: str, body: str = ""
    ) -> dict:
        self.calls.append(("create_draft_pr", (owner, repo, head, base, title)))
        full = f"{owner}/{repo}"
        number = self._number()
        pr = {
            "number": number,
            "id": number,
            "title": title,
            "body": body,
            "draft": True,
            "state": "open",
            "head": {"ref": head, "label": f"{owner}:{head}", "sha": self.heads[full][head]},
            "base": {"ref": base},
            "html_url": f"https://github.test/{full}/pull/{number}",
        }
        self.pull_requests.setdefault(full, []).append(pr)
        return dict(pr)

    async def get_pr_by_head(
        self, owner: str, repo: str, head_branch: str, base: str | None = None
    ) -> dict | None:
        self.calls.append(("get_pr_by_head", (owner, repo, head_branch, base)))
        full = f"{owner}/{repo}"
        for pr in self.pull_requests.get(full, []):
            if pr["state"] == "open" and pr["head"]["ref"] == head_branch:
                if base is not None and pr["base"]["ref"] != base:
                    continue
                return dict(pr)
        return None

    async def close_pull_request(self, owner: str, repo: str, number: int) -> dict:
        self.calls.append(("close_pull_request", (owner, repo, number)))
        full = f"{owner}/{repo}"
        for pr in self.pull_requests.get(full, []):
            if pr["number"] == number:
                pr["state"] = "closed"
                return dict(pr)
        raise GitHubAPIError(404, f"PR #{number} not found")

    def prs_for(self, full_name: str, head_branch: str) -> list[dict]:
        return [
            pr for pr in self.pull_requests.get(full_name, []) if pr["head"]["ref"] == head_branch
        ]

    def seed_pr_files(self, pr_number: int, files: list[dict]) -> None:
        """Seed the changed-file entries the reviewer reads for a PR."""
        self.pr_files[pr_number] = files

    def seed_compare(self, before: str, after: str, files: list[dict]) -> dict:
        """Seed a ``before...after`` comparison (the synchronize delta)."""
        comparison = {"files": [dict(f) for f in files], "total_commits": 1}
        self.compares[(before, after)] = comparison
        return dict(comparison)

    async def get_compare(self, owner: str, repo: str, before: str, after: str) -> dict:
        self.calls.append(("get_compare", (owner, repo, before, after)))
        comparison = self.compares.get((before, after))
        if comparison is None:
            raise GitHubAPIError(404, f"comparison {before[:8]}...{after[:8]} not found")
        return json.loads(json.dumps(comparison))  # deep copy, dict-shape

    # -- reviews (the reactive review engine surface, v0.7) -----------------

    def seed_review(
        self,
        pr_number: int,
        *,
        commit_id: str,
        login: str | None = None,
        state: str = "COMMENT",
        body: str = "",
    ) -> dict:
        """Seed an existing review (the incremental anchor of a prior run)."""
        review = {
            "id": self._number(),
            "user": {"login": login or self.reviewer_login, "type": "Bot"},
            "state": state,
            "commit_id": commit_id,
            "body": body,
        }
        self.reviews.setdefault(pr_number, []).append(review)
        return dict(review)

    async def list_reviews(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        self.calls.append(("list_reviews", (owner, repo, pr_number)))
        return [dict(review) for review in self.reviews.get(pr_number, [])]

    async def create_review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
        comments: list[dict] | None = None,
        event: str | None = None,
        commit_id: str | None = None,
    ) -> dict:
        self.calls.append(("create_review", (owner, repo, pr_number, event, commit_id)))
        review = {
            "id": self._number(),
            "user": {"login": self.reviewer_login, "type": "Bot"},
            "state": event or "PENDING",
            "commit_id": commit_id,
            "body": body,
            "comments": [dict(comment) for comment in comments or []],
        }
        self.reviews.setdefault(pr_number, []).append(review)
        return dict(review)

    async def get_pr_files(self, owner: str, repo: str, number: int) -> list[dict]:
        self.calls.append(("get_pr_files", (owner, repo, number)))
        return [dict(entry) for entry in self.pr_files.get(number, [])]

    # -- verification reads -------------------------------------------------------------

    async def list_check_runs_for_sha(self, owner: str, repo: str, sha: str) -> list[dict]:
        self.calls.append(("list_check_runs_for_sha", (owner, repo, sha)))
        return [dict(run) for run in self.check_runs.get(sha, [])]

    async def list_workflow_runs_for_sha(
        self, owner: str, repo: str, sha: str, workflow_name: str | None = None
    ) -> list[dict]:
        self.calls.append(("list_workflow_runs_for_sha", (owner, repo, sha, workflow_name)))
        found = [run for run in self.workflow_runs if run.get("head_sha") == sha]
        if workflow_name is not None:
            found = [run for run in found if run.get("name") == workflow_name]
        return [dict(run) for run in found]

    async def list_pull_requests_for_commit(self, owner: str, repo: str, sha: str) -> list[dict]:
        self.calls.append(("list_pull_requests_for_commit", (owner, repo, sha)))
        full = f"{owner}/{repo}"
        return [
            dict(pr)
            for pr in self.pull_requests.get(full, [])
            if pr.get("head", {}).get("sha") == sha
        ]

    # -- issues ---------------------------------------------------------------------------

    async def get_issue(self, owner: str, repo: str, number: int) -> Issue:
        self.calls.append(("get_issue", (owner, repo, number)))
        issue = self.issues.get(f"{owner}/{repo}", {}).get(number)
        if issue is None:
            raise GitHubAPIError(404, f"issue #{number} not found")
        return Issue.model_validate(
            {
                "id": issue["id"],
                "iid": issue["number"],
                "title": issue["title"],
                "description": issue.get("body"),
                "state": issue.get("state") or "open",
            }
        )

    async def create_issue_comment(self, owner: str, repo: str, number: int, body: str) -> dict:
        self.calls.append(("create_issue_comment", (owner, repo, number, body)))
        comment = {
            "id": self._number(),
            "body": body,
            "user": {"login": self.reviewer_login, "type": "Bot"},
        }
        self.issue_comments.setdefault(f"{owner}/{repo}", {}).setdefault(number, []).append(comment)
        return dict(comment)

    async def get_issue_comments(self, owner: str, repo: str, number: int) -> list[dict]:
        self.calls.append(("get_issue_comments", (owner, repo, number)))
        return [
            dict(comment)
            for comment in self.issue_comments.get(f"{owner}/{repo}", {}).get(number, [])
        ]

    async def update_issue_comment(self, owner: str, repo: str, comment_id: int, body: str) -> dict:
        self.calls.append(("update_issue_comment", (owner, repo, comment_id)))
        for comments in self.issue_comments.get(f"{owner}/{repo}", {}).values():
            for comment in comments:
                if comment["id"] == comment_id:
                    comment["body"] = body
                    return dict(comment)
        raise GitHubAPIError(404, f"comment {comment_id} not found")

    # -- reader surface (contents API semantics) --------------------------------------------

    async def get_file(self, project_id: int, file_path: str, ref: str = "HEAD") -> RepositoryFile:
        """Duck-typed :class:`GitHubRepositoryReader` shape for direct use."""
        self.calls.append(("get_file", (project_id, file_path, ref)))
        # The fake keeps one snapshot; the reader resolves the same content
        # whatever the ref (matching a frozen single-commit base in tests).
        files = next(iter(self.files.values()), {})
        if file_path not in files:
            raise GitHubAPIError(404, f"file {file_path!r} not found")
        content = files[file_path]
        return RepositoryFile.model_validate(
            {
                "file_name": file_path.rsplit("/", 1)[-1],
                "file_path": file_path,
                "size": len(content.encode("utf-8")),
                "encoding": "base64",
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "ref": ref,
            }
        )

    async def read_text(self, file_path: str, ref: str = "HEAD") -> str:
        """Duck-typed :meth:`GitHubRepositoryReader.read_text`."""
        repo_file = await self.get_file(0, file_path, ref)
        return base64.b64decode(repo_file.content).decode("utf-8")

    async def get_tree(
        self,
        project_id: int,
        path: str = "",
        ref: str = "HEAD",
        recursive: bool = False,
    ) -> list[TreeEntry]:
        self.calls.append(("get_tree", (project_id, path, ref, recursive)))
        files = next(iter(self.files.values()), {})
        entries = []
        for file_path in sorted(files):
            if path and not file_path.startswith(path):
                continue
            entries.append(
                TreeEntry.model_validate(
                    {
                        "id": self._oid()[:40] if recursive else None,
                        "name": file_path.rsplit("/", 1)[-1],
                        "type": "blob",
                        "path": file_path,
                    }
                )
            )
        return entries

    # -- Actions: the E3b execution surface -----------------------------------
    #
    # Mirrors the PARSED semantics of the new client methods: the 2026
    # dispatch response carries the run id (dispatch_mode="run_id"), the
    # legacy one answers empty (dispatch_mode="legacy") and the run is only
    # discoverable via the workflow_dispatch runs listing.

    def seed_actions_run(
        self,
        *,
        run_id: int,
        head_branch: str,
        head_sha: str,
        status: str = "completed",
        conclusion: str | None = None,
        event: str = "workflow_dispatch",
        created_at: datetime | None = None,
        workflow_name: str = "forge-harness",
    ) -> dict:
        run = {
            "id": run_id,
            "name": workflow_name,
            "event": event,
            "status": status,
            "conclusion": conclusion,
            "head_branch": head_branch,
            "head_sha": head_sha,
            "created_at": (created_at or datetime.now(timezone.utc)).isoformat(),
        }
        self.actions_runs.append(run)
        return dict(run)

    def seed_actions_artifact(self, run_id: int, name: str, files: dict[str, bytes]) -> dict:
        """Upload an artifact: a real in-memory zip (download returns bytes)."""
        self._next_artifact += 1
        artifact_id = self._next_artifact
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for path, payload in files.items():
                archive.writestr(path, payload)
        self.artifact_zips[artifact_id] = buffer.getvalue()
        artifact = {"id": artifact_id, "name": name, "run_id": run_id}
        self.actions_artifacts.setdefault(run_id, []).append(artifact)
        return dict(artifact)

    def seed_candidate_artifact(
        self,
        run_id: int,
        *,
        name: str,
        diff_text: str,
        meta: dict,
    ) -> dict:
        """The candidate contract zip: candidate.diff + candidate.meta.json."""
        return self.seed_actions_artifact(
            run_id,
            name,
            {
                "forge/candidate.diff": diff_text.encode("utf-8"),
                "forge/candidate.meta.json": json.dumps(meta).encode("utf-8"),
            },
        )

    def seed_job_log(self, job_id: int, log: str) -> None:
        self.job_logs[job_id] = log

    def seed_actions_jobs(self, run_id: int, jobs: list[dict]) -> None:
        self.actions_jobs[run_id] = [dict(job) for job in jobs]

    async def dispatch_workflow(
        self,
        owner: str,
        repo: str,
        workflow_filename: str,
        ref: str,
        inputs: dict[str, str] | None = None,
    ) -> dict:
        self.calls.append(("dispatch_workflow", (owner, repo, workflow_filename, ref)))
        self.dispatch_inputs.append(
            {"workflow": workflow_filename, "ref": ref, "inputs": dict(inputs or {})}
        )
        if self.dispatch_mode == "legacy":
            return {}  # legacy empty 202 — discovery must find the run
        self._next_actions_run += 1
        run_id = self._next_actions_run
        self.seed_actions_run(
            run_id=run_id,
            head_branch=ref,
            head_sha=str((inputs or {}).get("attempt_base_oid") or ""),
            status="queued",
            conclusion=None,
        )
        return {"run_id": run_id}

    async def list_workflow_dispatch_runs(
        self,
        owner: str,
        repo: str,
        workflow_filename: str,
        *,
        head_branch: str | None = None,
        head_sha: str | None = None,
        created_after: datetime | None = None,
    ) -> list[dict]:
        self.calls.append(
            ("list_workflow_dispatch_runs", (owner, repo, workflow_filename, head_branch))
        )
        found = [
            run
            for run in self.actions_runs
            if run["event"] == "workflow_dispatch"
            and (head_branch is None or run["head_branch"] == head_branch)
            and (head_sha is None or run["head_sha"] == head_sha)
            and (
                created_after is None or datetime.fromisoformat(run["created_at"]) >= created_after
            )
        ]
        # newest first (the API's default ordering)
        found.sort(key=lambda run: run["created_at"], reverse=True)
        return [dict(run) for run in found]

    async def get_workflow_run(self, owner: str, repo: str, run_id: int) -> dict:
        self.calls.append(("get_workflow_run", (owner, repo, run_id)))
        for run in self.actions_runs:
            if run["id"] == run_id:
                return dict(run)
        raise GitHubAPIError(404, f"workflow run {run_id} not found")

    async def get_workflow_run_jobs(self, owner: str, repo: str, run_id: int) -> list[dict]:
        self.calls.append(("get_workflow_run_jobs", (owner, repo, run_id)))
        return [dict(job) for job in self.actions_jobs.get(run_id, [])]

    async def list_workflow_run_artifacts(self, owner: str, repo: str, run_id: int) -> list[dict]:
        self.calls.append(("list_workflow_run_artifacts", (owner, repo, run_id)))
        return [dict(a) for a in self.actions_artifacts.get(run_id, [])]

    async def download_artifact_zip(self, owner: str, repo: str, artifact_id: int) -> bytes:
        self.calls.append(("download_artifact_zip", (owner, repo, artifact_id)))
        payload = self.artifact_zips.get(artifact_id)
        if payload is None:
            raise GitHubAPIError(404, f"artifact {artifact_id} not found")
        return payload

    async def get_job_log(self, owner: str, repo: str, job_id: int) -> str:
        self.calls.append(("get_job_log", (owner, repo, job_id)))
        return self.job_logs.get(job_id, "")

    async def cancel_workflow_run(self, owner: str, repo: str, run_id: int) -> None:
        self.calls.append(("cancel_workflow_run", (owner, repo, run_id)))
        if run_id in {run["id"] for run in self.actions_runs if run["status"] == "completed"}:
            raise GitHubAPIError(409, "cannot cancel a completed run")
        self.cancelled_runs.append(run_id)

    # -- security alerts (findings ingestion; ci-security-surface §3/§4) -------
    #
    # Mirrors the parsed alert-list endpoints and the PATCH-dismiss enums:
    # invalid reasons raise 422-shaped GitHubAPIErrors exactly like GitHub.

    async def list_code_scanning_alerts(
        self, owner: str, repo: str, *, state: str = "open", ref: str | None = None
    ) -> list[dict]:
        self.calls.append(("list_code_scanning_alerts", (owner, repo, state, ref)))
        if self.code_scanning_disabled:
            raise GitHubAPIError(403, "code scanning is not enabled")
        alerts = self.code_scanning_alerts.get(f"{owner}/{repo}", [])
        return [dict(a) for a in alerts if a.get("state", "open") == state]

    async def dismiss_code_scanning_alert(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        dismissed_reason: str,
        dismissed_comment: str | None = None,
    ) -> dict:
        self.calls.append(
            (
                "dismiss_code_scanning_alert",
                (owner, repo, number, dismissed_reason, dismissed_comment),
            )
        )
        # GitHub requires the reason and validates the space-separated enum.
        if dismissed_reason not in {"false positive", "won't fix", "used in tests"}:
            raise GitHubAPIError(422, f"invalid dismissed_reason {dismissed_reason!r}")
        alert = self._find_alert(
            self.code_scanning_alerts.setdefault(f"{owner}/{repo}", []), number
        )
        alert.update(
            {
                "state": "dismissed",
                "dismissed_reason": dismissed_reason,
                "dismissed_comment": dismissed_comment,
            }
        )
        return dict(alert)

    async def list_secret_scanning_alerts(
        self, owner: str, repo: str, *, state: str = "open"
    ) -> list[dict]:
        self.calls.append(("list_secret_scanning_alerts", (owner, repo, state)))
        if self.secret_scanning_disabled:
            raise GitHubAPIError(403, "secret scanning is not enabled")
        alerts = self.secret_scanning_alerts.get(f"{owner}/{repo}", [])
        return [dict(a) for a in alerts if a.get("state", "open") == state]

    async def resolve_secret_scanning_alert(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        resolution: str,
        resolution_comment: str | None = None,
    ) -> dict:
        self.calls.append(
            (
                "resolve_secret_scanning_alert",
                (owner, repo, number, resolution, resolution_comment),
            )
        )
        # Underscore enum (research §4.2) — space variants are rejected.
        if resolution not in {"false_positive", "wont_fix", "revoked", "used_in_tests"}:
            raise GitHubAPIError(422, f"invalid resolution {resolution!r}")
        alert = self._find_alert(
            self.secret_scanning_alerts.setdefault(f"{owner}/{repo}", []), number
        )
        alert.update(
            {
                "state": "resolved",
                "resolution": resolution,
                "resolution_comment": resolution_comment,
            }
        )
        return dict(alert)

    async def list_dependabot_alerts(
        self, owner: str, repo: str, *, state: str = "open"
    ) -> list[dict]:
        self.calls.append(("list_dependabot_alerts", (owner, repo, state)))
        alerts = self.dependabot_alerts.get(f"{owner}/{repo}", [])
        return [dict(a) for a in alerts if a.get("state", "open") == state]

    async def dismiss_dependabot_alert(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        dismissed_reason: str,
        dismissed_comment: str | None = None,
    ) -> dict:
        self.calls.append(
            (
                "dismiss_dependabot_alert",
                (owner, repo, number, dismissed_reason, dismissed_comment),
            )
        )
        if dismissed_reason not in {
            "fix_started",
            "inaccurate",
            "no_bandwidth",
            "not_used",
            "tolerable_risk",
        }:
            raise GitHubAPIError(422, f"invalid dismissed_reason {dismissed_reason!r}")
        alert = self._find_alert(self.dependabot_alerts.setdefault(f"{owner}/{repo}", []), number)
        alert.update(
            {
                "state": "dismissed",
                "dismissed_reason": dismissed_reason,
                "dismissed_comment": dismissed_comment,
            }
        )
        return dict(alert)

    @staticmethod
    def _find_alert(alerts: list[dict], number: int) -> dict:
        for alert in alerts:
            if alert.get("number") == number:
                return alert
        raise GitHubAPIError(404, f"alert #{number} not found")

    # -- helpers ------------------------------------------------------------------------

    def calls_of(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]

    def token_stub(self, token: str = "ghs_fake") -> object:
        """A TokenProvider stub so the fake can back a real GitHubClient."""

        class _Stub:
            async def token(self) -> str:
                return token

            async def invalidate(self) -> None:
                return None

        return _Stub()


def sample_installation_token(
    token: str = "ghs_fake_token", expires_in_seconds: int = 3600
) -> dict:
    """A mint-response body shaped like POST .../access_tokens (research §1.2)."""
    expires = (datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return {
        "token": token,
        "expires_at": expires,
        "permissions": {"contents": "write", "pull_requests": "write", "issues": "write"},
        "repositories": [],
    }
