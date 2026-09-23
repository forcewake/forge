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
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# --------------------------------------------------------------------- state


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


# ---------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The production-entry fake native server")
    parser.add_argument("--ready-file", required=True, help="where the chosen port is written")
    parser.add_argument("--repo", default="acme/forge-pe")
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--base-sha", default="1" * 40)
    args = parser.parse_args(argv)

    state = NativeState(args.repo, args.base_branch, args.base_sha)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    server.daemon_threads = True
    port = server.server_address[1]
    Path(args.ready_file).write_text(json.dumps({"port": port}), encoding="utf-8")
    try:
        server.serve_forever(poll_interval=0.05)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
