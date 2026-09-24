#!/usr/bin/env python3
"""The fake native server of the production-entry layer (Q35-09).

A REAL local HTTP server (stdlib only) modeling the GitHub Actions /
GitHub REST semantics the PRODUCTION transport uses — the real
:class:`forge.integrations.github.GitHubClient` is pointed at it with
``base_url=http://127.0.0.1:<port>``, so provider WRITES travel real
HTTP, exactly as against api.github.com.

It runs as a SEPARATE PROCESS (spawned by the fixture), so its state
SURVIVES WORKER DEATH by construction: a control-plane worker that
dies and comes back with a fresh session factory still reads the same
accepted native jobs, the same dispatch ledger, the same run statuses.

Native API semantics modeled:

- ``POST /repos/{o}/{r}/actions/workflows/{wf}/dispatches`` — accepts
  a ``workflow_dispatch``, MINTS a native run (id, branch, inputs)
  recorded in the dispatch ledger, answers ``202 {"run_id": N}``. The
  run's state is controllable: it starts ``in_progress`` and stays
  running until the test marks it terminal — a job that outlives
  every local cancellation.
- ``GET  /repos/{o}/{r}/actions/workflows/{wf}/runs`` — the
  ``workflow_dispatch`` runs list (``branch`` / ``head_sha`` filters,
  newest first) — the correlation primitive the reconciler probe uses.
- ``GET  /repos/{o}/{r}/actions/runs/{id}`` — one run's status.
- ``POST /repos/{o}/{r}/actions/runs/{id}/cancel`` — controllable:
  ``ok`` (202) or ``fail`` (500 after logging — the cancel that never
  lands, AT-05).
- the repo/issue surface the dispatch legs touch: refs (branch head /
  create branch, 422 when the ref exists), contents (404 for
  ``.forge.yml`` — a provider-confirmed absence), issues, issue
  comments, pulls (none by default).

Test control surface (``/__ctl/…``, plain JSON):

- ``GET  /__ctl/state`` — the full state document (dispatches with
  their INPUTS, runs, comments, unknown-path ledger);
- ``POST /__ctl/config`` — ``{"dispatch_response": "ok"|"server_error"``,
  ``"cancel_mode": "ok"|"fail"`` — the failure injectors;
- ``POST /__ctl/mark_terminal`` — ``{"run_id": N}`` moves a native run
  to ``completed/success``.

``dispatch_response=server_error`` is AT-06's lost-start-response
shape: the server ACCEPTS the job (the run is created, the ledger
records it) and then answers 500 — the client's call fails while the
native job lives on. Every unknown path is recorded and 404s loudly so
a missing endpoint surfaces in the trace instead of passing silently.

Stdlib only; runs as ``python fake_native_server.py --ready-file F``.

## The GitLab mode (``--api gitlab``, issue #268 / R36-09)

The same PROCESS discipline pointed at the GitLab CE REST v4 surface the
PRODUCTION transport uses — the real :class:`forge.gitlab.client.GitLabClient`
is pointed at it with ``base_url=http://127.0.0.1:<port>`` (its ``/api/v4``
prefix rides along), so provider reads AND writes travel real HTTP, exactly
as against a GitLab CE instance. ``--repo`` is the NUMERIC project id.

Native API semantics modeled (GitLab shapes):

- ``POST /projects/{id}/pipeline`` — the harness DISPATCH: mints a
  pipeline on the ref with the given VARIABLES recorded in the dispatch
  ledger, plus its ``forge-agent-*`` job (``running`` until the test
  marks it terminal — a job that outlives local cancellation).
- ``GET  /projects/{id}/pipelines`` (``ref``/``sha``/``status`` filters,
  newest first), ``GET /pipelines/{pid}``, ``GET /pipelines/{pid}/jobs``.
- ``POST /projects/{id}/jobs/{jid}/cancel`` — the RUNNER-LOSS primitive:
  job → ``canceled`` (recorded), its pipeline → ``canceled``.
- ``GET  /projects/{id}/jobs/{jid}/trace`` and
  ``GET  /projects/{id}/jobs/{jid}/artifacts/{path}`` — the candidate
  contract (ADR-0016) the backend downloads.
- the repository surface the writer and publisher touch: branches
  (get/create, 400 when the branch exists), commits (create — moving the
  branch head — and list), files (base64, 404 = provider-confirmed
  absence, so ``.forge.yml`` earns the default profile).
- issues + issue notes, merge requests (create/update/get/list by
  source branch) and MR notes.

Test control surface (``/__ctl/…``): ``state``, ``seed_issue``,
``seed_file``, ``seed_pipeline`` (independent verification pipelines with
their own jobs), ``seed_artifact``, ``mark_terminal`` (job + pipeline
status) and ``cancel_job``. Every unknown path is recorded and 404s
loudly, so a trace asserting ``unknown_paths == []`` proves the real
client never fell off the modeled API.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# --------------------------------------------------------------------- state
#
# The shared GitHub-mode state lives below (unchanged); the GitLab-mode
# state (``GitLabState``) follows the same one-lock-survives-the-worker
# discipline with GitLab-shaped documents.

#: The dispatch-input identity fields the server fingerprints per job
#: (R36-13 / issue #272 — the executor-input capture). MUST stay in sync
#: with ``forge.adaptive.revisions.EXECUTOR_INPUT_FIELDS`` and its
#: canonicalization (field-sorted JSON, comma separators): the trace
#: asserts the server's fingerprint equals the ``revision.executor_digest``
#: evidence the dispatch recorded, so any drift fails loudly.
EXECUTOR_INPUT_FIELDS = (
    "run_id",
    "plan_digest",
    "envelope_digest",
    "spec_digest",
    "lane_resume_mode",
)


def executor_input_fingerprint(inputs: dict) -> str:
    """The server-side fingerprint of one dispatch's executor-input identity."""
    canonical = json.dumps(
        {name: str(inputs.get(name) or "") for name in EXECUTOR_INPUT_FIELDS},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class NativeState:
    """All server state, guarded by one lock (the whole point: it survives)."""

    def __init__(self, repo_full_name: str, base_branch: str, base_sha: str) -> None:
        self.lock = threading.Lock()
        self.repo = repo_full_name
        self.branches: dict[str, str] = {base_branch: base_sha}
        self.issues: dict[int, dict] = {}
        self.comments: list[dict] = []
        self.next_comment_id = 9000
        self.runs: list[dict] = []
        self.next_run_id = 500
        self.dispatches: list[dict] = []
        self.cancels: list[dict] = []
        self.unknown_paths: list[str] = []
        # failure injectors
        self.dispatch_response = "ok"  # ok | server_error
        self.cancel_mode = "ok"  # ok | fail

    def mint_run(self, workflow: str, ref: str, inputs: dict) -> dict:
        with self.lock:
            self.next_run_id += 1
            run = {
                "id": self.next_run_id,
                "name": workflow,
                "event": "workflow_dispatch",
                "status": "in_progress",
                "conclusion": None,
                "head_branch": ref,
                "head_sha": self.branches.get(ref, "0" * 40),
                "created_at": "2026-09-23T00:00:00Z",
                "run_attempt": 1,
            }
            self.runs.append(run)
            self.dispatches.append(
                {
                    "run_id": run["id"],
                    "workflow": workflow,
                    "ref": ref,
                    "inputs": dict(inputs),
                    # R36-13 (#272): the SERVER-side fingerprint of the
                    # dispatched executor-input identity — computed from
                    # what the production client ACTUALLY sent, never from
                    # anything the test told the server to say.
                    "executor_input_digest": executor_input_fingerprint(inputs),
                }
            )
            return dict(run)

    def document(self) -> dict:
        with self.lock:
            return {
                "repo": self.repo,
                "branches": dict(self.branches),
                "runs": [dict(run) for run in self.runs],
                "dispatches": [dict(entry) for entry in self.dispatches],
                "cancels": [dict(entry) for entry in self.cancels],
                "comments": [dict(entry) for entry in self.comments],
                "dispatch_response": self.dispatch_response,
                "cancel_mode": self.cancel_mode,
                "unknown_paths": list(self.unknown_paths),
            }


# ------------------------------------------------------------------- handler


def make_handler(state: NativeState) -> type[BaseHTTPRequestHandler]:
    repo_owner, repo_name = state.repo.split("/", 1)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:  # silence
            pass

        # -- plumbing -------------------------------------------------------

        def _send_json(self, status: int, body: object | None = None) -> None:
            payload = json.dumps(body if body is not None else {}).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                parsed = json.loads(raw)
            except ValueError:
                return {}
            return parsed if isinstance(parsed, dict) else {}

        def _segments(self) -> list[str]:
            path = unquote(urlparse(self.path).path)
            return [segment for segment in path.split("/") if segment]

        def _query(self) -> dict[str, str]:
            parsed = parse_qs(urlparse(self.path).query)
            return {key: values[0] for key, values in parsed.items() if values}

        # -- GET ------------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 — http.server spelling
            segments = self._segments()
            query = self._query()
            if segments[:2] == ["__ctl", "state"]:
                self._send_json(200, state.document())
                return
            if (
                len(segments) >= 5
                and segments[:2] == ["repos", repo_owner]
                and segments[2] == repo_name
            ):
                tail = segments[3:]
                if tail[:2] == ["git", "refs"] and len(tail) == 4 and tail[2] == "heads":
                    sha = state.branches.get(tail[3])
                    if sha is None:
                        self._send_json(404, {"message": "Not Found"})
                        return
                    self._send_json(200, {"ref": f"refs/heads/{tail[3]}", "object": {"sha": sha}})
                    return
                if tail[0] == "commits" and len(tail) == 2:
                    # get_branch_head's fallback shape: GET /commits/{ref}.
                    sha = state.branches.get(tail[1])
                    if sha is None:
                        self._send_json(404, {"message": "Not Found"})
                        return
                    self._send_json(200, {"sha": sha, "commit": {"tree": {"sha": sha}}})
                    return
                if tail == ["contents", ".forge.yml"]:
                    # A provider-CONFIRMED absence: the config read earns the
                    # default profile, never an "unreadable" park.
                    self._send_json(404, {"message": "Not Found"})
                    return
                if tail[0] == "contents":
                    self._send_json(404, {"message": "Not Found"})
                    return
                if tail[0] == "issues" and len(tail) == 2 and tail[1].isdigit():
                    issue = state.issues.get(int(tail[1]))
                    if issue is None:
                        self._send_json(404, {"message": "Not Found"})
                        return
                    self._send_json(200, issue)
                    return
                if tail == ["pulls"]:
                    self._send_json(200, [])
                    return
                if tail[:2] == ["actions", "workflows"] and len(tail) == 4 and tail[-1] == "runs":
                    workflow = tail[2]
                    with state.lock:
                        runs = [
                            dict(run)
                            for run in reversed(state.runs)
                            if run["name"] == workflow and run["event"] == "workflow_dispatch"
                        ]
                    if "branch" in query:
                        runs = [run for run in runs if run["head_branch"] == query["branch"]]
                    if "head_sha" in query:
                        runs = [run for run in runs if run["head_sha"] == query["head_sha"]]
                    self._send_json(
                        200,
                        {
                            "total_count": len(runs),
                            "workflow_runs": runs[: int(query.get("per_page") or 100)],
                        },
                    )
                    return
                if tail[:2] == ["actions", "runs"] and len(tail) == 3 and tail[2].isdigit():
                    with state.lock:
                        run = next(
                            (dict(item) for item in state.runs if item["id"] == int(tail[2])), None
                        )
                    if run is None:
                        self._send_json(404, {"message": "Not Found"})
                        return
                    self._send_json(200, run)
                    return
            with state.lock:
                state.unknown_paths.append(f"GET {self.path}")
            self._send_json(
                404, {"message": f"fake native server has no route for GET {self.path}"}
            )

        # -- POST / PATCH ----------------------------------------------------

        def do_POST(self) -> None:  # noqa: N802 — http.server spelling
            segments = self._segments()
            body = self._body()
            if segments[:2] == ["__ctl", "config"]:
                with state.lock:
                    if "dispatch_response" in body:
                        state.dispatch_response = str(body["dispatch_response"])
                    if "cancel_mode" in body:
                        state.cancel_mode = str(body["cancel_mode"])
                self._send_json(200, state.document())
                return
            if segments[:2] == ["__ctl", "mark_terminal"]:
                run_id = int(body.get("run_id") or 0)
                with state.lock:
                    for run in state.runs:
                        if run["id"] == run_id:
                            run["status"] = "completed"
                            run["conclusion"] = str(body.get("conclusion") or "success")
                self._send_json(200, {"run_id": run_id})
                return
            if segments[:2] == ["__ctl", "seed_issue"]:
                number = int(body.get("number") or 0)
                state.issues[number] = {
                    "id": 100000 + number,
                    "number": number,
                    "title": str(body.get("title") or "issue"),
                    "body": str(body.get("body") or ""),
                    "state": "open",
                    "user": {"login": "alice", "id": 1},
                    "labels": [],
                    "html_url": f"https://github.test/{state.repo}/issues/{number}",
                }
                self._send_json(200, state.issues[number])
                return
            if (
                len(segments) >= 5
                and segments[:2] == ["repos", repo_owner]
                and segments[2] == repo_name
            ):
                tail = segments[3:]
                if tail[:2] == ["actions", "workflows"] and tail[-1] == "dispatches":
                    workflow = "/".join(tail[2:-1])
                    ref = str(body.get("ref") or "")
                    inputs = dict(body.get("inputs") or {})
                    run = state.mint_run(workflow, ref, inputs)  # ACCEPTED first
                    with state.lock:
                        mode = state.dispatch_response
                    if mode == "server_error":
                        # AT-06: the job lives; only the RESPONSE is lost.
                        self._send_json(500, {"message": "injected dispatch-response failure"})
                        return
                    self._send_json(202, {"run_id": run["id"]})
                    return
                if tail[:2] == ["actions", "runs"] and len(tail) == 4 and tail[3] == "cancel":
                    run_id = int(tail[2])
                    with state.lock:
                        state.cancels.append({"run_id": run_id})
                        mode = state.cancel_mode
                    if mode == "fail":
                        self._send_json(500, {"message": "injected cancel failure"})
                        return
                    with state.lock:
                        for run in state.runs:
                            if run["id"] == run_id:
                                run["status"] = "completed"
                                run["conclusion"] = "cancelled"
                    self._send_json(202, {})
                    return
                if tail == ["git", "refs"]:
                    ref = str(body.get("ref") or "")
                    sha = str(body.get("sha") or "")
                    name = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
                    with state.lock:
                        if name in state.branches:
                            self._send_json(422, {"message": "Reference already exists"})
                            return
                        state.branches[name] = sha
                    self._send_json(201, {"ref": ref, "object": {"sha": sha, "type": "commit"}})
                    return
                if tail[0] == "issues" and len(tail) == 3 and tail[2] == "comments":
                    number = int(tail[1])
                    with state.lock:
                        state.next_comment_id += 1
                        comment = {
                            "id": state.next_comment_id,
                            "issue_number": number,
                            "body": str(body.get("body") or ""),
                        }
                        state.comments.append(comment)
                    self._send_json(201, comment)
                    return
            with state.lock:
                state.unknown_paths.append(f"POST {self.path}")
            self._send_json(
                404, {"message": f"fake native server has no route for POST {self.path}"}
            )

        def do_PATCH(self) -> None:  # noqa: N802 — http.server spelling
            segments = self._segments()
            body = self._body()
            if (
                len(segments) >= 5
                and segments[:2] == ["repos", repo_owner]
                and segments[2] == repo_name
            ):
                tail = segments[3:]
                if tail[:2] == ["issues", "comments"] and len(tail) == 4:
                    comment_id = int(tail[2])
                    with state.lock:
                        for comment in state.comments:
                            if comment["id"] == comment_id:
                                comment["body"] = str(body.get("body") or comment["body"])
                                self._send_json(200, dict(comment))
                                return
                    self._send_json(404, {"message": "Not Found"})
                    return
            with state.lock:
                state.unknown_paths.append(f"PATCH {self.path}")
            self._send_json(
                404, {"message": f"fake native server has no route for PATCH {self.path}"}
            )

    return Handler


# =====================================================================
# The GitLab mode (issue #268 / R36-09): GitLab CE REST v4 semantics
# for the real forge.gitlab.client.GitLabClient.
# =====================================================================


def _gitlab_commit_doc(sha: str, message: str, parent_ids: list[str]) -> dict:
    return {
        "id": sha,
        "short_id": sha[:8],
        "message": message,
        "parent_ids": list(parent_ids),
    }


class GitLabState:
    """All GitLab-mode server state under one lock (it survives workers)."""

    def __init__(self, project_id: int, base_branch: str, base_sha: str) -> None:
        self.lock = threading.Lock()
        self.project_id = project_id
        # branch -> commits (newest first); each commit is the GitLab shape
        # the client/writer read (id/short_id/message/parent_ids).
        self.branches: dict[str, list[dict]] = {
            base_branch: [_gitlab_commit_doc(base_sha, "frozen base", [])]
        }
        self.issues: dict[int, dict] = {}
        self.notes: list[dict] = []  # issue notes
        self.mr_notes: list[dict] = []  # merge-request notes
        self.merge_requests: dict[int, dict] = {}  # iid -> MR document
        self.pipelines: list[dict] = []
        self.pipeline_jobs: dict[int, list[dict]] = {}
        self.job_logs: dict[int, str] = {}
        self.job_artifacts: dict[int, dict[str, bytes]] = {}
        self.dispatches: list[dict] = []  # create_pipeline calls (with variables)
        self.job_cancels: list[dict] = []
        self.unknown_paths: list[str] = []
        self.files: dict[str, str] = {}  # path -> text (ref-independent snapshot)
        self._next_id = 1000

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    # -- native mutations (called under the handler) -----------------------

    def branch_head(self, branch: str) -> str | None:
        commits = self.branches.get(branch)
        return str(commits[0]["id"]) if commits else None

    def create_pipeline(self, ref: str, variables: list[dict]) -> dict:
        """The harness dispatch: mint pipeline + its forge-agent job."""
        pipeline_id = self._id()
        sha = self.branch_head(ref) or ("0" * 40)
        pipeline = {
            "id": pipeline_id,
            "ref": ref,
            "sha": sha,
            "status": "running",
            "source": "api",
            "web_url": f"https://gitlab.test/project-{self.project_id}/-/pipelines/{pipeline_id}",
            "created_at": "2026-09-23T00:00:00Z",
        }
        self.pipelines.append(pipeline)
        job = {
            "id": self._id(),
            "name": "forge-agent-claude-code",
            "stage": "harness",
            "status": "running",
            "web_url": f"{pipeline['web_url']}/jobs",
            "failure_reason": None,
        }
        self.pipeline_jobs[pipeline_id] = [job]
        self.dispatches.append(
            {
                "pipeline_id": pipeline_id,
                "ref": ref,
                "sha": sha,
                "job_id": job["id"],
                "variables": [dict(v) for v in (variables or [])],
            }
        )
        return dict(pipeline)

    def set_job_status(
        self, job_id: int, status: str, *, pipeline_status: str | None = None
    ) -> None:
        """Move one job (and its pipeline) to a terminal/active status."""
        for jobs in self.pipeline_jobs.values():
            for job in jobs:
                if job["id"] == job_id:
                    job["status"] = status
                    job["failure_reason"] = "job_failure" if status == "failed" else None
        for pipeline in self.pipelines:
            jobs = self.pipeline_jobs.get(pipeline["id"]) or []
            if any(job["id"] == job_id for job in jobs):
                pipeline["status"] = pipeline_status or status

    def document(self) -> dict:
        with self.lock:
            return {
                "api": "gitlab",
                "project_id": self.project_id,
                "branches": {
                    name: [dict(c) for c in commits] for name, commits in self.branches.items()
                },
                "pipelines": [dict(p) for p in self.pipelines],
                "jobs": {
                    str(pid): [dict(j) for j in jobs] for pid, jobs in self.pipeline_jobs.items()
                },
                "dispatches": [dict(d) for d in self.dispatches],
                "job_cancels": [dict(c) for c in self.job_cancels],
                "merge_requests": {str(iid): dict(mr) for iid, mr in self.merge_requests.items()},
                "mr_notes": [dict(n) for n in self.mr_notes],
                "notes": [dict(n) for n in self.notes],
                "issues": {str(iid): dict(issue) for iid, issue in self.issues.items()},
                "files": dict(self.files),
                "unknown_paths": list(self.unknown_paths),
            }


def _repo_file_doc(path: str, content: str, ref: str) -> dict:
    raw = content.encode("utf-8", errors="surrogateescape")
    return {
        "file_name": path.rsplit("/", 1)[-1],
        "file_path": path,
        "size": len(raw),
        "encoding": "base64",
        "content": base64.b64encode(raw).decode("ascii"),
        "ref": ref,
    }


def make_gitlab_handler(state: GitLabState) -> type[BaseHTTPRequestHandler]:
    """The GitLab REST v4 surface (paths the production client builds)."""

    project = str(state.project_id)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:  # silence
            pass

        # -- plumbing -------------------------------------------------------

        def _send_json(self, status: int, body: object | None = None) -> None:
            payload = json.dumps(body if body is not None else {}).encode("utf-8")
            self._send(status, payload)

        def _send(
            self, status: int, payload: bytes, content_type: str = "application/json"
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                parsed = json.loads(raw)
            except ValueError:
                return {}
            return parsed if isinstance(parsed, dict) else {}

        def _segments(self) -> list[str]:
            # Per-segment unquoting: GitLab clients encode "/" inside a
            # branch/file path as %2F, and the server must treat that as
            # ONE segment (unquoting the whole path first would split
            # ``factory%2F38%2F…`` into three).
            path = urlparse(self.path).path
            return [unquote(segment) for segment in path.split("/") if segment]

        def _query(self) -> dict[str, str]:
            parsed = parse_qs(urlparse(self.path).query)
            return {key: values[0] for key, values in parsed.items() if values}

        def _unknown(self) -> None:
            with state.lock:
                state.unknown_paths.append(f"{self.command} {self.path}")
            self._send_json(
                404, {"message": f"fake gitlab server has no route for {self.command} {self.path}"}
            )

        def _project_tail(self, segments: list[str]) -> list[str] | None:
            """``['api','v4','projects',<id>, ...]`` → the tail after the id."""
            if (
                len(segments) >= 5
                and segments[:2] == ["api", "v4"]
                and segments[2] == "projects"
                and segments[3] == project
            ):
                return segments[4:]
            return None

        # -- GET ------------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 — http.server spelling
            segments = self._segments()
            query = self._query()
            if segments[:2] == ["__ctl", "state"]:
                self._send_json(200, state.document())
                return
            tail = self._project_tail(segments)
            if tail is None:
                self._unknown()
                return
            with state.lock:
                if tail[:1] == ["repository"]:
                    repo = tail[1:]
                    if repo[:1] == ["branches"] and len(repo) == 2:
                        # get_branch (URL-encoded name; unquoted in _segments)
                        commits = state.branches.get(repo[1])
                        if commits is None:
                            self._send_json(404, {"message": "404 Branch Not Found"})
                            return
                        head = dict(commits[0])
                        self._send_json(200, {"name": repo[1], "commit": head})
                        return
                    if repo[:1] == ["commits"] and len(repo) == 1:
                        ref = query.get("ref_name") or "HEAD"
                        commits = state.branches.get(ref) or []
                        self._send_json(200, [dict(c) for c in commits])
                        return
                    if repo[:1] == ["files"] and len(repo) == 2:
                        # the client quote()d the path (safe="") — one
                        # segment once per-segment unquoting collapsed %2F
                        path = repo[1]
                        content = state.files.get(path)
                        if content is None:
                            # provider-CONFIRMED absence (the .forge.yml read
                            # earns the default profile, never a park)
                            self._send_json(404, {"message": "404 File Not Found"})
                            return
                        self._send_json(
                            200, _repo_file_doc(path, content, query.get("ref") or "HEAD")
                        )
                        return
                if tail[:1] == ["pipelines"]:
                    if len(tail) == 1:
                        pipelines = list(reversed(state.pipelines))
                        if "ref" in query:
                            pipelines = [p for p in pipelines if p["ref"] == query["ref"]]
                        if "sha" in query:
                            pipelines = [p for p in pipelines if p["sha"] == query["sha"]]
                        if "status" in query:
                            pipelines = [p for p in pipelines if p["status"] == query["status"]]
                        self._send_json(200, pipelines[: int(query.get("per_page") or 20)])
                        return
                    if len(tail) == 2 and tail[1].isdigit():
                        pid = int(tail[1])
                        found = next((dict(p) for p in state.pipelines if p["id"] == pid), None)
                        if found is None:
                            self._send_json(404, {"message": "404 Pipeline Not Found"})
                            return
                        self._send_json(200, found)
                        return
                    if len(tail) == 3 and tail[2] == "jobs" and tail[1].isdigit():
                        pid = int(tail[1])
                        jobs = state.pipeline_jobs.get(pid)
                        if jobs is None:
                            self._send_json(404, {"message": "404 Pipeline Not Found"})
                            return
                        self._send_json(200, [dict(j) for j in jobs])
                        return
                if tail[:1] == ["jobs"]:
                    if len(tail) == 3 and tail[2] == "trace" and tail[1].isdigit():
                        jid = int(tail[1])
                        self._send(
                            200,
                            state.job_logs.get(jid, "").encode("utf-8"),
                            "text/plain; charset=utf-8",
                        )
                        return
                    if len(tail) >= 4 and tail[2] == "artifacts" and tail[1].isdigit():
                        # the archive path rides one quote(safe="")-encoded
                        # segment (collapsed back by the handler's unquote)
                        jid = int(tail[1])
                        artifact_path = "/".join(tail[3:])
                        artifact = state.job_artifacts.get(jid, {}).get(artifact_path)
                        if artifact is None:
                            self._send_json(404, {"message": "404 artifact not found"})
                            return
                        self._send(200, artifact, "application/octet-stream")
                        return
                if tail[:1] == ["issues"] and len(tail) == 2 and tail[1].isdigit():
                    issue = state.issues.get(int(tail[1]))
                    if issue is None:
                        self._send_json(404, {"message": "404 Issue Not Found"})
                        return
                    self._send_json(200, issue)
                    return
                if tail[:1] == ["merge_requests"]:
                    if len(tail) == 1:
                        opened = [
                            dict(mr)
                            for mr in state.merge_requests.values()
                            if mr.get("state") == query.get("state", "opened")
                        ]
                        if "source_branch" in query:
                            opened = [
                                mr for mr in opened if mr["source_branch"] == query["source_branch"]
                            ]
                        self._send_json(200, opened)
                        return
                    if len(tail) == 2 and tail[1].isdigit():
                        mr = state.merge_requests.get(int(tail[1]))
                        if mr is None:
                            self._send_json(404, {"message": "404 Merge Request Not Found"})
                            return
                        self._send_json(200, mr)
                        return
            self._unknown()

        # -- POST / PUT ------------------------------------------------------

        def do_POST(self) -> None:  # noqa: N802 — http.server spelling
            segments = self._segments()
            body = self._body()
            if segments[:2] == ["__ctl", "seed_issue"]:
                iid = int(body.get("iid") or 0)
                state.issues[iid] = {
                    "id": 100000 + iid,
                    "iid": iid,
                    "title": str(body.get("title") or "issue"),
                    "description": str(body.get("description") or ""),
                    "state": "opened",
                    "labels": [],
                    "web_url": f"https://gitlab.test/project-{state.project_id}/-/issues/{iid}",
                }
                self._send_json(200, state.issues[iid])
                return
            if segments[:2] == ["__ctl", "seed_file"]:
                state.files[str(body.get("path") or "")] = str(body.get("content") or "")
                self._send_json(200, {"path": body.get("path")})
                return
            if segments[:2] == ["__ctl", "seed_commit"]:
                with state.lock:
                    branch = str(body.get("branch") or "main")
                    state.branches.setdefault(branch, []).insert(
                        0,
                        _gitlab_commit_doc(
                            str(body.get("sha") or ""),
                            str(body.get("message") or "seeded"),
                            list(body.get("parent_ids") or []),
                        ),
                    )
                self._send_json(200, {"branch": body.get("branch"), "sha": body.get("sha")})
                return
            if segments[:2] == ["__ctl", "seed_pipeline"]:
                with state.lock:
                    pipeline_id = state._id()
                    pipeline = {
                        "id": pipeline_id,
                        "ref": str(body.get("ref") or "main"),
                        "sha": str(body.get("sha") or ""),
                        "status": str(body.get("status") or "success"),
                        "source": str(body.get("source") or "push"),
                        "web_url": (
                            f"https://gitlab.test/project-{state.project_id}"
                            f"/-/pipelines/{pipeline_id}"
                        ),
                    }
                    state.pipelines.append(pipeline)
                    jobs = [
                        {
                            "id": state._id(),
                            "name": str(job.get("name") or "verify"),
                            "stage": "test",
                            "status": str(job.get("status") or "success"),
                            "web_url": pipeline["web_url"] + "/jobs",
                            "failure_reason": None,
                        }
                        for job in (body.get("jobs") or [])
                    ]
                    state.pipeline_jobs[pipeline["id"]] = jobs
                self._send_json(200, pipeline)
                return
            if segments[:2] == ["__ctl", "seed_artifact"]:
                job_id = int(body.get("job_id") or 0)
                content = body.get("content_b64")
                payload = (
                    base64.b64decode(content)
                    if isinstance(content, str)
                    else str(body.get("content") or "").encode("utf-8")
                )
                state.job_artifacts.setdefault(job_id, {})[str(body.get("path") or "")] = payload
                self._send_json(200, {"job_id": job_id, "path": body.get("path")})
                return
            if segments[:2] == ["__ctl", "mark_terminal"]:
                with state.lock:
                    job_id = int(body.get("job_id") or 0)
                    status = str(body.get("status") or "success")
                    state.set_job_status(
                        job_id, status, pipeline_status=str(body.get("pipeline_status") or status)
                    )
                self._send_json(200, {"job_id": job_id, "status": status})
                return
            if segments[:2] == ["__ctl", "cancel_job"]:
                with state.lock:
                    job_id = int(body.get("job_id") or 0)
                    state.job_cancels.append({"job_id": job_id})
                    state.set_job_status(job_id, "canceled", pipeline_status="canceled")
                self._send_json(200, {"job_id": job_id})
                return
            tail = self._project_tail(segments)
            if tail is None:
                self._unknown()
                return
            with state.lock:
                if tail == ["pipeline"]:
                    pipeline = state.create_pipeline(
                        str(body.get("ref") or ""), list(body.get("variables") or [])
                    )
                    self._send_json(201, pipeline)
                    return
                if tail[:1] == ["jobs"] and len(tail) == 3 and tail[2] == "cancel":
                    job_id = int(tail[1])
                    state.job_cancels.append({"job_id": job_id})
                    state.set_job_status(job_id, "canceled", pipeline_status="canceled")
                    job = next(
                        (
                            dict(j)
                            for jobs in state.pipeline_jobs.values()
                            for j in jobs
                            if j["id"] == job_id
                        ),
                        None,
                    )
                    if job is None:
                        self._send_json(404, {"message": "404 Job Not Found"})
                        return
                    self._send_json(200, job)
                    return
                if tail[:2] == ["repository", "branches"]:
                    branch = str(body.get("branch") or "")
                    ref = str(body.get("ref") or "main")
                    if branch in state.branches:
                        self._send_json(400, {"message": "Branch already exists"})
                        return
                    # real GitLab resolves *ref*: a branch name → that
                    # branch's head; a SHA → the commit with that id.
                    commits = state.branches.get(ref)
                    if commits is None:
                        commits = next(
                            (
                                found
                                for found in state.branches.values()
                                if found and found[0]["id"] == ref
                            ),
                            None,
                        )
                    state.branches[branch] = [dict(commits[0])] if commits else []
                    self._send_json(
                        201, {"name": branch, "commit": dict(commits[0]) if commits else None}
                    )
                    return
                if tail[:2] == ["repository", "commits"] and len(tail) == 2:
                    branch = str(body.get("branch") or "")
                    actions = list(body.get("actions") or [])
                    message = str(body.get("commit_message") or "")
                    parent = state.branch_head(branch)
                    sha = f"gl-sha-{state._id():08d}"
                    record = _gitlab_commit_doc(sha, message, [parent] if parent else [])
                    state.branches.setdefault(branch, []).insert(0, record)
                    # materialize file actions into the snapshot (the
                    # publisher's authoritative base reads follow the head)
                    for action in actions:
                        path = str(action.get("file_path") or "")
                        if not path:
                            continue
                        if str(action.get("action") or "create") == "delete":
                            state.files.pop(path, None)
                        else:
                            state.files[path] = str(action.get("content") or "")
                    self._send_json(201, {"id": sha, "short_id": sha[:8], "message": message})
                    return
                if tail[:1] == ["issues"] and len(tail) == 3 and tail[2] == "notes":
                    iid = int(tail[1])
                    note = {
                        "id": state._id(),
                        "body": str(body.get("body") or ""),
                        "system": False,
                    }
                    state.notes.append({"id": note["id"], "issue_iid": iid, "body": note["body"]})
                    self._send_json(201, note)
                    return
                if tail[:1] == ["merge_requests"] and len(tail) == 1:
                    iid = state._id()
                    mr = {
                        "id": iid,
                        "iid": iid,
                        "title": str(body.get("title") or ""),
                        "description": str(body.get("description") or ""),
                        "state": "opened",
                        "source_branch": str(body.get("source_branch") or ""),
                        "target_branch": str(body.get("target_branch") or "main"),
                        "web_url": (
                            f"https://gitlab.test/project-{state.project_id}/-/merge_requests/{iid}"
                        ),
                        "draft": str(body.get("title") or "").startswith("Draft:"),
                        "sha": state.branch_head(str(body.get("source_branch") or "")),
                    }
                    state.merge_requests[iid] = mr
                    self._send_json(201, mr)
                    return
                if (
                    tail[:1] == ["merge_requests"]
                    and len(tail) == 3
                    and tail[2] == "notes"
                    and tail[1].isdigit()
                ):
                    mr_iid = int(tail[1])
                    note = {"id": state._id(), "body": str(body.get("body") or "")}
                    state.mr_notes.append(
                        {"id": note["id"], "mr_iid": mr_iid, "body": note["body"]}
                    )
                    self._send_json(201, note)
                    return
            self._unknown()

        def do_PUT(self) -> None:  # noqa: N802 — http.server spelling
            segments = self._segments()
            body = self._body()
            tail = self._project_tail(segments)
            if tail is None:
                self._unknown()
                return
            with state.lock:
                if tail[:1] == ["merge_requests"] and len(tail) == 2 and tail[1].isdigit():
                    mr = state.merge_requests.get(int(tail[1]))
                    if mr is None:
                        self._send_json(404, {"message": "404 Merge Request Not Found"})
                        return
                    if "description" in body:
                        mr["description"] = str(body["description"])
                    if "title" in body:
                        mr["title"] = str(body["title"])
                    self._send_json(200, dict(mr))
                    return
            self._unknown()

    return Handler


# ---------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The production-entry fake native server")
    parser.add_argument("--ready-file", required=True, help="where the chosen port is written")
    parser.add_argument(
        "--api",
        choices=["github", "gitlab"],
        default="github",
        help="which native surface to model (default: github)",
    )
    parser.add_argument("--repo", default="acme/forge-pe", help="owner/name (github) or project id")
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--base-sha", default="1" * 40)
    args = parser.parse_args(argv)

    if args.api == "gitlab":
        state = GitLabState(int(args.repo), args.base_branch, args.base_sha)
        handler = make_gitlab_handler(state)
    else:
        state = NativeState(args.repo, args.base_branch, args.base_sha)
        handler = make_handler(state)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    port = server.server_address[1]
    Path(args.ready_file).write_text(json.dumps({"port": port, "api": args.api}), encoding="utf-8")
    try:
        server.serve_forever(poll_interval=0.05)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
