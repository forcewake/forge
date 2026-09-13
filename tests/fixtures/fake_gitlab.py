"""Shared in-memory GitLab fake for the run-loop tests.

No network, no live services: every method mirrors the real
:class:`forge.gitlab.client.GitLabClient` semantics the run loop relies on,
including the non-idempotent create_commit behaviour (a timeout *after* the
server executed the commit) and base64-encoded repository files.
"""

from __future__ import annotations

import base64

from forge.gitlab.client import CommitOutcomeUnknown, GitLabAPIError
from forge.gitlab.schemas import Issue, Job, MergeRequest, Note, Pipeline, RepositoryFile, TreeEntry


class FakeGitLab:
    """In-memory GitLab covering the run-loop read/write surface."""

    def __init__(self) -> None:
        self.branches: dict[str, list[dict]] = {}  # branch -> commits (newest first)
        self.merge_requests: dict[int, dict] = {}
        self.pipelines: list[dict] = []
        self.pipeline_jobs: dict[int, list[dict]] = {}  # pipeline id -> job dicts
        self.pipeline_variables: list[dict] = []  # journal of create_pipeline variables
        self.job_logs: dict[int, str] = {}  # job id -> raw log text
        self.job_artifacts: dict[int, dict[str, bytes]] = {}  # job id -> path -> bytes
        self.notes: list[dict] = []  # issue notes
        self.mr_notes: list[dict] = []  # merge-request notes
        self.issues: dict[int, dict] = {}
        self.files: dict[str, str] = {}  # path -> text content (all refs)
        self.mr_updates: list[dict] = []  # journal of update_merge_request calls
        self.calls: list[tuple[str, tuple]] = []
        # Knobs for failure injection:
        self.create_commit_timeout_drops: bool = False  # timeout, commit not applied
        self.create_commit_timeout_applies: bool = False  # timeout, commit applied
        self.raise_on_create_commit: GitLabAPIError | None = None
        # Knob for compare_commits: None -> empty diff; dict -> returned as-is.
        self.compare_result: dict | None = None
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
        # Real GitLab resolves *ref* to a commit: a branch name → that
        # branch's head; a SHA → the commit with that sha.
        if self.branches.get(ref):
            seed = self.branches[ref][0]
        else:
            seed = next((c for cs in self.branches.values() for c in cs if c["sha"] == ref), None)
        self.branches[branch_name] = [dict(seed)] if seed else []
        return {"name": branch_name, "commit": dict(seed) if seed else None}

    async def get_branch(self, project_id: int, branch_name: str) -> dict:
        self.calls.append(("get_branch", (project_id, branch_name)))
        if branch_name not in self.branches:
            raise GitLabAPIError(404, "branch not found")
        commits = self.branches[branch_name]
        head = (
            {
                "id": commits[0]["sha"],
                "short_id": commits[0]["short_id"],
                "message": commits[0].get("message", ""),
            }
            if commits
            else None
        )
        return {"name": branch_name, "commit": head}

    async def get_branch_head(self, project_id: int, branch_name: str) -> str:
        """Head commit SHA — one lookup, never a paginated history (F28)."""
        self.calls.append(("get_branch_head", (project_id, branch_name)))
        commits = self.branches.get(branch_name)
        if not commits:
            raise GitLabAPIError(404, "branch not found")
        return commits[0]["sha"]

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
        head = self.branches.get(branch, [])
        record = {
            "sha": sha,
            "short_id": sha,
            "message": commit_message,
            "parent_ids": [head[0]["sha"]] if head else [],
        }

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
            {
                "sha": c["sha"],
                "short_id": c["short_id"],
                "message": c["message"],
                "parent_ids": list(c.get("parent_ids", [])),
            }
            for c in commits
        ]

    def seed_commit(
        self, branch: str, sha: str, message: str = "seeded", parents: list[str] | None = None
    ) -> None:
        """Pre-seed a commit on a branch (newest first), as if pushed before."""
        self.branches.setdefault(branch, []).insert(
            0,
            {
                "sha": sha,
                "short_id": sha,
                "message": message,
                "parent_ids": list(parents or []),
            },
        )

    # -- repository files / tree ---------------------------------------------

    def seed_file(self, path: str, content: str) -> None:
        """Seed a file in the (ref-independent) repository snapshot."""
        self.files[path] = content

    async def get_file(self, project_id: int, file_path: str, ref: str = "HEAD") -> RepositoryFile:
        self.calls.append(("get_file", (project_id, file_path, ref)))
        if file_path not in self.files:
            raise GitLabAPIError(404, f"file {file_path} not found")
        content = self.files[file_path]
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
        entries = []
        for file_path in sorted(self.files):
            if path and not file_path.startswith(path):
                continue
            entries.append(
                TreeEntry.model_validate(
                    {
                        "name": file_path.rsplit("/", 1)[-1],
                        "type": "blob",
                        "path": file_path,
                    }
                )
            )
        return entries

    async def compare_commits(self, project_id: int, from_sha: str, to_sha: str) -> dict:
        self.calls.append(("compare_commits", (project_id, from_sha, to_sha)))
        if self.compare_result is not None:
            return self.compare_result
        return {"diffs": []}

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

    async def create_pipeline(
        self, project_id: int, ref: str, variables: list[dict] | None = None
    ) -> dict:
        self.calls.append(("create_pipeline", (project_id, ref)))
        pid = self._id()
        pipeline = {
            "id": pid,
            "ref": ref,
            "status": "pending",
            "sha": None,
            "variables": [dict(v) for v in (variables or [])],
        }
        self.pipelines.append(pipeline)
        self.pipeline_variables.append(
            {"project_id": project_id, "ref": ref, "variables": variables or []}
        )
        return dict(pipeline)

    def set_pipeline_status(self, pipeline_id: int, status: str, sha: str | None = None) -> None:
        for p in self.pipelines:
            if p["id"] == pipeline_id:
                p["status"] = status
                if sha is not None:
                    p["sha"] = sha

    async def list_pipeline_jobs(self, project_id: int, pipeline_id: int) -> list[Job]:
        self.calls.append(("list_pipeline_jobs", (project_id, pipeline_id)))
        return [Job.model_validate(job) for job in self.pipeline_jobs.get(pipeline_id, [])]

    def set_pipeline_jobs(self, pipeline_id: int, jobs: list[dict]) -> None:
        """Seed jobs for a pipeline (dicts shaped like the GitLab Job schema)."""
        self.pipeline_jobs[pipeline_id] = jobs

    async def get_job_log(self, project_id: int, job_id: int, tail: int | None = None) -> str:
        self.calls.append(("get_job_log", (project_id, job_id)))
        log = self.job_logs.get(job_id, "")
        if tail is not None and len(log) > tail:
            return log[-tail:]
        return log

    def set_job_log(self, job_id: int, log: str) -> None:
        self.job_logs[job_id] = log

    # -- job artifacts (ADR-0016 candidate bundle) ----------------------------

    def seed_job_artifact(self, job_id: int, path: str, content: str | bytes) -> None:
        """Seed a single artifact file for a job (archive-path keyed)."""
        self.job_artifacts.setdefault(job_id, {})[path] = (
            content.encode("utf-8") if isinstance(content, str) else content
        )

    async def get_job_artifacts_file(
        self, project_id: int, job_id: int, artifact_path: str
    ) -> bytes:
        self.calls.append(("get_job_artifacts_file", (project_id, job_id, artifact_path)))
        artifact = self.job_artifacts.get(job_id, {}).get(artifact_path)
        if artifact is None:
            raise GitLabAPIError(404, f"artifact {artifact_path} not found")
        return artifact

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

    async def update_merge_request(
        self,
        project_id: int,
        mr_iid: int,
        description: str | None = None,
        title: str | None = None,
    ) -> dict:
        self.calls.append(("update_merge_request", (project_id, mr_iid)))
        if mr_iid not in self.merge_requests:
            raise GitLabAPIError(404, "mr not found")
        if description is not None:
            self.merge_requests[mr_iid]["description"] = description
        if title is not None:
            self.merge_requests[mr_iid]["title"] = title
        self.mr_updates.append({"mr_iid": mr_iid, "description": description, "title": title})
        return dict(self.merge_requests[mr_iid])

    async def get_merge_request(self, project_id: int, mr_iid: int) -> MergeRequest:
        self.calls.append(("get_merge_request", (project_id, mr_iid)))
        if mr_iid not in self.merge_requests:
            raise GitLabAPIError(404, "mr not found")
        return MergeRequest.model_validate(self.merge_requests[mr_iid])

    async def create_mr_note(self, project_id: int, mr_iid: int, body: str) -> Note:
        self.calls.append(("create_mr_note", (project_id, mr_iid, body)))
        if mr_iid not in self.merge_requests:
            raise GitLabAPIError(404, "mr not found")
        note_id = self._id()
        self.mr_notes.append({"id": note_id, "mr_iid": mr_iid, "body": body})
        return Note.model_validate({"id": note_id, "body": body})

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

    def mr_notes_containing(self, fragment: str) -> list[dict]:
        return [n for n in self.mr_notes if fragment in n["body"]]


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
