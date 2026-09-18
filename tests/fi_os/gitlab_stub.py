"""An in-test fake GitLab HTTP server for the OS-process FI suite.

The worker subprocesses build the real :class:`forge.gitlab.client.GitLabClient`
against ``GITLAB_URL``; this stdlib ``http.server`` implements the REST v4
surface the run loop exercises (issues, files, tree, branches, commits,
merge requests, notes, pipelines, compare) with GitLab-shaped JSON and status
codes, including the semantics recovery depends on:

- a missing file/branch is a real 404 (the R14 "provider-confirmed absence");
- a second open MR for the same source branch is refused (real GitLab
  behaviour — a latent duplicate-MR recovery bug fails loudly instead of
  silently doubling);
- ``POST .../repository/commits`` can be armed to APPLY the commit and then
  ``hold`` the response forever (the caller is SIGKILLed waiting) or ``drop``
  the connection before responding — the ambiguous remote effect R11's
  identity probe exists for. It can also be armed (``arm_commit_delay``) to
  ACCEPT the request but DELAY the application itself: the commit lands only
  after the arming window elapses, so a recovery probe racing the delay sees
  the OLD head — the A12 delayed-apply window in which a negative probe
  proves nothing about an in-flight effect. Applied effects stay in the
  stub's state, so the probe can find exactly one ``(forge-op:<key>)``
  commit afterwards.

State lives in the test process; the suite drives the "external world" (CI
pipelines) by mutating it directly, exactly like the coroutine suite drives
``FakeGitLab``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HOLD_SECONDS = 300  # a held response outlives every suite timeout

_BASE_SHA = hashlib.sha1(b"forge-os-fi-base").hexdigest()


class _QuietServer(ThreadingHTTPServer):
    """A SIGKILLed worker resets its sockets mid-request — that is the drill,
    not an error worth a traceback on stderr."""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        exc_type = sys.exc_info()[0]
        if exc_type in (ConnectionResetError, BrokenPipeError, TimeoutError):
            return
        super().handle_error(request, client_address)


def base_sha() -> str:
    """The seeded head of ``main`` (a realistic 40-hex SHA)."""
    return _BASE_SHA


def _commit(sha: str, message: str, parents: list[str]) -> dict:
    return {
        "id": sha,
        "short_id": sha[:8],
        "message": message,
        "parent_ids": list(parents),
    }


class GitLabStub:
    """Fake GitLab CE REST v4 surface over 127.0.0.1, with failure knobs."""

    def __init__(self, project_id: int = 4242) -> None:
        self.project_id = project_id
        self._lock = threading.Lock()
        self.branches: dict[str, list[dict]] = {}  # branch -> commits, newest first
        self.files: dict[str, str] = {}  # path -> text (ref-independent snapshot)
        self.merge_requests: dict[int, dict] = {}
        self.notes: list[dict] = []  # issue notes
        self.pipelines: list[dict] = []
        # The acceptance ledger: every counted remote effect.
        self.commit_posts = 0  # create_commit arrivals
        self.commits_applied = 0
        self.commit_responses_served = 0
        self.commit_holds = 0
        self.commit_drops = 0
        self.delayed_apply_pending = 0  # accepted, application still delayed
        self.delayed_applies = 0  # delayed applications completed
        self.mr_posts = 0
        self.mrs_created = 0
        self.note_posts = 0
        self.notes_created = 0
        self.request_log: list[str] = []
        self._commit_effect: str | None = None  # None | "hold" | "drop" (one-shot)
        self._commit_delay_seconds: float | None = None  # one-shot
        self._note_hold_fragment: str | None = None  # one-shot
        self._ids = 100

        self.seed_commit("main", base_sha(), "seed: base of the OS-FI lab repo")
        self.files["README.md"] = "# OS-FI lab repo\n"

        self._server = _QuietServer(("127.0.0.1", 0), self._make_handler())
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread: threading.Thread | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # -- knobs (one-shot, like the coroutine suite's CrashInjector) ----------

    def arm_commit_effect(self, effect: str) -> None:
        """The NEXT create_commit is applied, then held / dropped."""
        assert effect in ("hold", "drop"), effect
        with self._lock:
            self._commit_effect = effect

    def arm_commit_delay(self, seconds: float) -> None:
        """The NEXT create_commit is ACCEPTED but APPLIED only after *seconds*.

        The A12 delayed-apply window: the request's effect is in flight
        while every branch read still shows the old head, so a recovery
        probe racing the delay is NEGATIVE without proving absence. Combine
        with ``arm_commit_effect("hold")`` so the poster never sees an
        outcome (the ambiguous shape recovery exists for).
        """
        with self._lock:
            self._commit_delay_seconds = float(seconds)

    # -- effect execution (stub-owned: the delayed timer runs on the stub) ----

    def _apply_commit(
        self, branch: str, sha: str, message: str, head: str, actions: list[dict]
    ) -> None:
        """Execute the accepted write: file actions + the branch move."""
        with self._lock:
            for action in actions:
                path = str(action.get("file_path") or "")
                if action.get("action") == "delete":
                    self.files.pop(path, None)
                elif path:
                    self.files[path] = str(action.get("content") or "")
            self.branches[branch].insert(0, _commit(sha, message, [head]))
            self.commits_applied += 1

    def _apply_delayed_commit(
        self, branch: str, sha: str, message: str, head: str, actions: list[dict]
    ) -> None:
        """The delayed half of an accepted A12 write: apply it now."""
        self._apply_commit(branch, sha, message, head, actions)
        with self._lock:
            self.delayed_apply_pending -= 1
            self.delayed_applies += 1

    def arm_note_hold(self, fragment: str) -> None:
        """The NEXT issue note whose body contains *fragment* is held."""
        with self._lock:
            self._note_hold_fragment = fragment

    # -- external-world helpers (the test plays GitLab's other users) ---------

    def seed_commit(self, branch: str, sha: str, message: str) -> None:
        with self._lock:
            self.branches.setdefault(branch, []).insert(0, _commit(sha, message, []))

    def set_pipeline_success(self, sha: str, ref: str) -> None:
        """The target project's CI finishing green on the candidate."""
        with self._lock:
            self._ids += 1
            self.pipelines.append(
                {
                    "id": self._ids,
                    "status": "success",
                    "ref": ref,
                    "sha": sha,
                    "web_url": f"{self.url}/c/p/-/pipelines/{self._ids}",
                }
            )

    # -- assertion helpers -----------------------------------------------------

    def forge_commits_on(self, branch: str, base: str = _BASE_SHA) -> list[dict]:
        """Commits on *branch* that are not the seeded base."""
        with self._lock:
            return [c for c in self.branches.get(branch, []) if c["id"] != base]

    def notes_with(self, fragment: str) -> list[dict]:
        with self._lock:
            return [n for n in self.notes if fragment in n["body"]]

    def snapshot_counts(self) -> dict[str, int]:
        with self._lock:
            return {
                "commit_posts": self.commit_posts,
                "commits_applied": self.commits_applied,
                "commit_responses_served": self.commit_responses_served,
                "delayed_apply_pending": self.delayed_apply_pending,
                "delayed_applies": self.delayed_applies,
                "mr_posts": self.mr_posts,
                "mrs_created": self.mrs_created,
                "note_posts": self.note_posts,
                "notes_created": self.notes_created,
            }

    # -- request handling --------------------------------------------------------

    def _make_handler(self):
        stub = self

        def parsed_path(parts: list[str]) -> str:
            return "/api/v4/" + "/".join(parts)

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _reply(self, status: int, payload) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _not_found(self, what: str) -> None:
                self._reply(404, {"message": f"404 {what} Not Found"})

            def _read_body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    return json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return {}

            def _route(self, method: str) -> None:
                parsed = urlparse(self.path)
                # Split the RAW path, then unquote: GitLabClient quotes "/"
                # inside branch and file names as %2F, so each segment is
                # exactly one path component.
                parts = [unquote(p) for p in parsed.path.split("/") if p]
                query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
                with stub._lock:
                    stub.request_log.append(f"{method} {parsed.path}")
                try:
                    self._dispatch(parts, query)
                except Exception as exc:  # noqa: BLE001 — the stub must answer
                    self._reply(500, {"message": f"stub error: {exc!r}"})

            def do_GET(self) -> None:  # noqa: N802 — stdlib naming
                self._route("GET")

            def do_POST(self) -> None:  # noqa: N802 — stdlib naming
                self._route("POST")

            def do_PUT(self) -> None:  # noqa: N802 — stdlib naming
                self._route("PUT")

            def log_message(self, *args) -> None:
                return

            def _dispatch(self, parts: list[str], query: dict) -> None:
                if parts[:3] != ["api", "v4", "projects"]:
                    self._not_found(parsed_path(parts))
                    return
                rest = parts[3:]
                if not rest or rest[0] != str(stub.project_id):
                    self._not_found(parsed_path(parts))
                    return
                tail = rest[1:]
                head = tail[0] if tail else ""
                if head == "issues":
                    self._issues(tail)
                elif head == "repository":
                    self._repository(tail[1:], query)
                elif head == "merge_requests":
                    self._merge_requests(tail[1:])
                elif head == "pipelines" and len(tail) >= 3 and tail[2] == "jobs":
                    self._reply(200, [])  # no jobs — an empty verification profile
                elif head == "pipelines" and len(tail) >= 2 and self.command == "GET":
                    self._one_pipeline(int(tail[1]))
                elif head == "pipelines":
                    self._pipelines(query)
                else:
                    self._not_found(parsed_path(parts))

            # -- issues ------------------------------------------------------------

            def _issues(self, tail: list[str]) -> None:
                if (
                    len(tail) >= 3
                    and tail[1].isdigit()
                    and tail[2] == "notes"
                    and self.command == "POST"
                ):
                    self._issue_notes(int(tail[1]))
                    return
                if len(tail) >= 2 and tail[1].isdigit() and self.command == "GET":
                    self._reply(
                        200,
                        {
                            "id": int(tail[1]),
                            "iid": int(tail[1]),
                            "title": "Add a widget",
                            "description": "Make widgets real.",
                            "state": "opened",
                        },
                    )
                    return
                self._not_found("issue")

            def _issue_notes(self, issue_iid: int) -> None:
                body = str(self._read_body().get("body") or "")
                with stub._lock:
                    stub.note_posts += 1
                    hold = stub._note_hold_fragment is not None and stub._note_hold_fragment in body
                    if hold:
                        stub._note_hold_fragment = None  # one-shot, before creating
                if hold:
                    time.sleep(HOLD_SECONDS)
                    return
                with stub._lock:
                    stub._ids += 1
                    note = {"id": stub._ids, "issue_iid": issue_iid, "body": body}
                    stub.notes.append(note)
                    stub.notes_created += 1
                self._reply(201, note)

            # -- repository ---------------------------------------------------------

            def _repository(self, tail: list[str], query: dict) -> None:
                head = tail[0] if tail else ""
                if head == "files" and len(tail) >= 2 and self.command == "GET":
                    self._get_file("/".join(tail[1:]), query.get("ref", "HEAD"))
                elif head == "tree" and self.command == "GET":
                    with stub._lock:
                        entries = [
                            {"name": p.rsplit("/", 1)[-1], "type": "blob", "path": p}
                            for p in sorted(stub.files)
                        ]
                    self._reply(200, entries)
                elif head == "branches" and len(tail) >= 2 and self.command == "GET":
                    self._get_branch("/".join(tail[1:]))
                elif head == "branches" and self.command == "POST":
                    body = self._read_body()
                    self._create_branch(str(body.get("branch") or ""), str(body.get("ref") or ""))
                elif head == "commits" and self.command == "GET":
                    self._list_commits(query.get("ref_name", "main"))
                elif head == "commits" and self.command == "POST":
                    self._create_commit(self._read_body())
                elif head == "compare" and self.command == "GET":
                    self._reply(200, {"diffs": []})
                else:
                    self._not_found("repository route")

            def _get_file(self, path: str, ref: str) -> None:
                with stub._lock:
                    text = stub.files.get(path)
                if text is None:
                    self._not_found("file")  # the R14 provider-confirmed absence
                    return
                content = text.encode("utf-8")
                self._reply(
                    200,
                    {
                        "file_name": path.rsplit("/", 1)[-1],
                        "file_path": path,
                        "size": len(content),
                        "encoding": "base64",
                        "content": base64.b64encode(content).decode("ascii"),
                        "ref": ref,
                    },
                )

            def _branch_head_locked(self, branch: str) -> str | None:
                commits = stub.branches.get(branch)
                return commits[0]["id"] if commits else None

            def _get_branch(self, branch: str) -> None:
                with stub._lock:
                    head = self._branch_head_locked(branch)
                    message = stub.branches[branch][0]["message"] if head else ""
                if head is None:
                    self._not_found("branch")
                    return
                self._reply(200, {"name": branch, "commit": {"id": head, "message": message}})

            def _create_branch(self, branch: str, ref: str) -> None:
                with stub._lock:
                    if branch in stub.branches:
                        self._reply(400, {"message": f"branch {branch} already exists"})
                        return
                    sha = None
                    if ref in stub.branches:
                        sha = self._branch_head_locked(ref)
                    else:
                        for commits in stub.branches.values():
                            if any(c["id"] == ref for c in commits):
                                sha = ref
                                break
                    if sha is None:
                        self._not_found("ref")
                        return
                    stub.branches[branch] = [_commit(sha, f"branch {branch}", [])]
                self._reply(201, {"name": branch, "commit": {"id": sha}})

            def _list_commits(self, ref: str) -> None:
                with stub._lock:
                    commits = stub.branches.get(ref)
                    listing = [
                        {
                            "id": c["id"],
                            "short_id": c["short_id"],
                            "message": c["message"],
                            "parent_ids": list(c["parent_ids"]),
                        }
                        for c in (commits or [])
                    ]
                if commits is None:
                    self._not_found("ref")
                    return
                self._reply(200, listing)

            def _create_commit(self, body: dict) -> None:
                """The ONE write the R11 identity probe resolves.

                The commit is applied FIRST (the ambiguous-effect contract:
                the provider may have executed it even though no response
                arrived), then the armed effect holds or drops the response.
                With a delay armed (A12) the commit is ACCEPTED but its
                application is scheduled for later — branch reads keep
                showing the old head until the delay elapses.
                """
                branch = str(body.get("branch") or "")
                message = str(body.get("commit_message") or "")
                with stub._lock:
                    stub.commit_posts += 1
                    head = self._branch_head_locked(branch)
                    if head is None:
                        self._reply(400, {"message": f"branch {branch} not found"})
                        return
                    sha = hashlib.sha1(
                        f"{branch}:{message}:{stub.commit_posts}".encode()
                    ).hexdigest()
                    actions = [
                        action
                        for action in (body.get("actions") or [])
                        if str(action.get("file_path") or "")
                    ]
                    delay = stub._commit_delay_seconds
                    stub._commit_delay_seconds = None  # one-shot
                    effect = stub._commit_effect
                    stub._commit_effect = None  # one-shot
                    if delay is not None:
                        # Accepted-but-not-applied: the parent is pinned by
                        # the head at accept time (exactly what the intent's
                        # expected parent recorded).
                        stub.delayed_apply_pending += 1
                        timer = threading.Timer(
                            delay,
                            stub._apply_delayed_commit,
                            args=(branch, sha, message, head, actions),
                        )
                        timer.daemon = True
                        timer.start()
                    else:
                        stub._apply_commit(branch, sha, message, head, actions)
                    if effect == "hold":
                        stub.commit_holds += 1
                    elif effect == "drop":
                        stub.commit_drops += 1
                    else:
                        stub.commit_responses_served += 1

                if effect == "drop":
                    # Applied, then the wire vanishes before any response: the
                    # exact ambiguous outcome R11's probe was built for.
                    self.close_connection = True
                    return
                if effect == "hold":
                    time.sleep(HOLD_SECONDS)  # the caller is SIGKILLed waiting
                self._reply(201, {"id": sha, "short_id": sha[:8], "message": message})

            # -- merge requests --------------------------------------------------------

            def _merge_requests(self, tail: list[str]) -> None:
                if not tail:
                    self._create_mr()
                    return
                iid = int(tail[0]) if tail[0].isdigit() else None
                mr = stub.merge_requests.get(iid) if iid is not None else None
                if mr is None:
                    self._not_found("merge request")
                    return
                if self.command == "GET":
                    self._reply(200, dict(mr))
                    return
                if self.command == "PUT":
                    body = self._read_body()
                    if body.get("description") is not None:
                        mr["description"] = body["description"]
                    self._reply(200, dict(mr))
                    return
                self._not_found("merge request route")

            def _create_mr(self) -> None:
                body = self._read_body()
                source = str(body.get("source_branch") or "")
                with stub._lock:
                    stub.mr_posts += 1
                    for mr in stub.merge_requests.values():
                        if mr["source_branch"] == source and mr["state"] == "opened":
                            # Real GitLab refuses a second open MR for the
                            # branch: a duplicate-MR recovery bug must fail
                            # loudly here, never silently double.
                            self._reply(
                                409,
                                {
                                    "message": "Another open merge request already "
                                    f"exists for this source branch: {source}"
                                },
                            )
                            return
                    stub._ids += 1
                    iid = stub._ids
                    mr = {
                        "id": iid,
                        "iid": iid,
                        "title": str(body.get("title") or ""),
                        "description": str(body.get("description") or ""),
                        "state": "opened",
                        "source_branch": source,
                        "target_branch": str(body.get("target_branch") or "main"),
                        "web_url": f"{stub.url}/c/p/-/merge_requests/{iid}",
                    }
                    stub.merge_requests[iid] = mr
                    stub.mrs_created += 1
                self._reply(201, dict(mr))

            # -- pipelines -----------------------------------------------------------

            def _one_pipeline(self, pipeline_id: int) -> None:
                with stub._lock:
                    found = [dict(p) for p in stub.pipelines if p["id"] == pipeline_id]
                if not found:
                    self._not_found("pipeline")
                    return
                self._reply(200, found[0])

            def _pipelines(self, query: dict) -> None:
                with stub._lock:
                    found = [
                        dict(p)
                        for p in stub.pipelines
                        if (query.get("sha") is None or p["sha"] == query["sha"])
                        and (query.get("ref") is None or p["ref"] == query["ref"])
                        and (query.get("status") is None or p["status"] == query["status"])
                    ]
                self._reply(200, found)

        return Handler
