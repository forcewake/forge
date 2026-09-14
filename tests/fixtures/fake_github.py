"""Shared in-memory GitHub fake for the slice tests.

No network, no live services: every method mirrors the PARSED semantics of
:class:`forge.integrations.github.GitHubClient` (the same level the flow
consumes), including the branch-wide CAS of ``create_commit_on_branch`` —
a moved head surfaces as :class:`GitHubStaleBranchError`, never as a silent
overwrite — and the base64 blob handling of the contents API.
"""

from __future__ import annotations

import base64
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
        # sha -> list of check-run dicts
        self.check_runs: dict[str, list[dict]] = {}
        # list of workflow-run dicts (head_sha / name keyed filtering)
        self.workflow_runs: list[dict] = []
        self.calls: list[tuple[str, tuple]] = []
        self._next_number = 100
        self._next_oid = 1

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

    def prs_for(self, full_name: str, head_branch: str) -> list[dict]:
        return [
            pr for pr in self.pull_requests.get(full_name, []) if pr["head"]["ref"] == head_branch
        ]

    def seed_pr_files(self, pr_number: int, files: list[dict]) -> None:
        """Seed the changed-file entries the reviewer reads for a PR."""
        self.pr_files[pr_number] = files

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
        return {"id": self._number(), "body": body}

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
