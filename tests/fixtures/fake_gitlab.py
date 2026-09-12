"""Shared in-memory GitLab fake for the M1 run-loop tests.

No network, no live services: every method mirrors the real
:class:`forge.gitlab.client.GitLabClient` semantics the run loop relies on,
including the non-idempotent create_commit behaviour (a timeout *after* the
server executed the commit).
"""

from __future__ import annotations

from forge.gitlab.client import CommitOutcomeUnknown, GitLabAPIError
from forge.gitlab.schemas import Issue, MergeRequest, Pipeline


class FakeGitLab:
    """In-memory GitLab covering the M1 read/write surface."""

    def __init__(self) -> None:
        self.branches: dict[str, list[dict]] = {}  # branch -> commits (newest first)
        self.merge_requests: dict[int, dict] = {}
        self.pipelines: list[dict] = []
        self.notes: list[dict] = []
        self.issues: dict[int, dict] = {}
        self.calls: list[tuple[str, tuple]] = []
        # Knobs for failure injection:
        self.create_commit_timeout_drops: bool = False  # timeout, commit not applied
        self.create_commit_timeout_applies: bool = False  # timeout, commit applied
        self.raise_on_create_commit: GitLabAPIError | None = None
        self._next_id = 1

    # -- ids ---------------------------------------------------------------

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    # -- branches / commits -------------------------------------------------

    async def create_branch(self, project_id: int, branch_name: str, ref: str = "main") -> dict:
        self.calls.append(("create_branch", (project_id, branch_name, ref)))
        if branch_name in self.branches:
            raise GitLabAPIError(400, f"branch {branch_name} already exists")
        self.branches[branch_name] = []
        return {"name": branch_name, "commit": None}

    async def get_branch(self, project_id: int, branch_name: str) -> dict:
        self.calls.append(("get_branch", (project_id, branch_name)))
        if branch_name not in self.branches:
            raise GitLabAPIError(404, "branch not found")
        return {"name": branch_name, "commit": None}

    async def create_commit(
        self,
        project_id: int,
        branch: str,
        actions: list[dict],
        commit_message: str,
        start_branch: str | None = None,
    ) -> dict:
        self.calls.append(
            ("create_commit", (project_id, branch, actions, commit_message, start_branch))
        )
        if self.raise_on_create_commit is not None:
            raise self.raise_on_create_commit

        sha = f"sha-{self._id()}"
        record = {"sha": sha, "short_id": sha, "message": commit_message}

        if self.create_commit_timeout_applies or self.create_commit_timeout_drops:
            # Server-side outcome of the lost response:
            if self.create_commit_timeout_applies:
                self.branches.setdefault(branch, []).insert(0, record)
            raise CommitOutcomeUnknown("create_commit timed out; outcome unknown")

        self.branches.setdefault(branch, []).insert(0, record)
        return {"id": sha, "short_id": sha, "message": commit_message}

    async def list_commits(self, project_id: int, ref: str) -> list[dict]:
        self.calls.append(("list_commits", (project_id, ref)))
        commits = self.branches.get(ref, [])
        return [
            {"sha": c["sha"], "short_id": c["short_id"], "message": c["message"]} for c in commits
        ]

    def seed_commit(self, branch: str, sha: str, message: str = "seeded") -> None:
        """Pre-seed a commit on a branch (newest first), as if pushed before."""
        self.branches.setdefault(branch, []).insert(
            0, {"sha": sha, "short_id": sha, "message": message}
        )

    # -- pipelines -----------------------------------------------------------

    async def list_pipelines(
        self,
        project_id: int,
        ref: str | None = None,
        status: str | None = None,
        sha: str | None = None,
        per_page: int = 20,
    ) -> list[Pipeline]:
        self.calls.append(("list_pipelines", (project_id, ref, status, sha)))
        found = [
            p
            for p in self.pipelines
            if (sha is None or p.get("sha") == sha)
            and (ref is None or p.get("ref") == ref)
            and (status is None or p.get("status") == status)
        ]
        return [Pipeline.model_validate(p) for p in found]

    async def create_pipeline(self, project_id: int, ref: str) -> dict:
        self.calls.append(("create_pipeline", (project_id, ref)))
        pid = self._id()
        pipeline = {"id": pid, "ref": ref, "status": "pending", "sha": None}
        self.pipelines.append(pipeline)
        return dict(pipeline)

    def set_pipeline_status(self, pipeline_id: int, status: str, sha: str | None = None) -> None:
        for p in self.pipelines:
            if p["id"] == pipeline_id:
                p["status"] = status
                if sha is not None:
                    p["sha"] = sha

    # -- merge requests -------------------------------------------------------

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
    ) -> dict:
        self.calls.append(
            ("create_merge_request", (project_id, source_branch, target_branch, title))
        )
        iid = self._id()
        mr = {
            "id": iid,
            "iid": iid,
            "title": title,
            "description": description,
            "state": "opened",
            "source_branch": source_branch,
            "target_branch": target_branch,
            "web_url": f"https://gitlab.test/g/p/-/merge_requests/{iid}",
        }
        self.merge_requests[iid] = mr
        return dict(mr)

    async def get_merge_request(self, project_id: int, mr_iid: int) -> MergeRequest:
        self.calls.append(("get_merge_request", (project_id, mr_iid)))
        if mr_iid not in self.merge_requests:
            raise GitLabAPIError(404, "mr not found")
        return MergeRequest.model_validate(self.merge_requests[mr_iid])

    # -- issues / notes --------------------------------------------------------

    def seed_issue(self, iid: int, title: str, description: str = "") -> None:
        self.issues[iid] = {
            "id": iid,
            "iid": iid,
            "title": title,
            "description": description,
            "state": "opened",
        }

    async def get_issue(self, project_id: int, issue_iid: int) -> Issue:
        self.calls.append(("get_issue", (project_id, issue_iid)))
        if issue_iid not in self.issues:
            raise GitLabAPIError(404, "issue not found")
        return Issue.model_validate(self.issues[issue_iid])

    async def create_issue_note(self, project_id: int, issue_iid: int, body: str) -> dict:
        self.calls.append(("create_issue_note", (project_id, issue_iid, body)))
        note_id = self._id()
        note = {"id": note_id, "issue_iid": issue_iid, "body": body}
        self.notes.append(note)
        return dict(note)

    # -- helpers ----------------------------------------------------------------

    async def __aenter__(self) -> FakeGitLab:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def calls_of(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]

    def notes_containing(self, fragment: str) -> list[dict]:
        return [n for n in self.notes if fragment in n["body"]]


class FakeGitLabClientFactory:
    """Context-manager factory standing in for GitLabClient construction.

    ``execute_run_command`` builds its own GitLabClient per task; tests patch
    ``forge.runs.service.GitLabClient`` with an instance of this to hand the
    RunService a fake (optionally the same instance across constructions).
    """

    def __init__(self, shared: FakeGitLab | None = None) -> None:
        self.shared = shared
        self.created: list[FakeGitLab] = []

    def __call__(self, **_kwargs) -> FakeGitLab:
        instance = self.shared if self.shared is not None else FakeGitLab()
        self.created.append(instance)
        return instance
