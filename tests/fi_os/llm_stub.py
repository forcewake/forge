"""A tiny canned LiteLLM-proxy stub the OS-process FI workers point at.

The real worker subprocesses build the real :class:`forge.factory.llm.LLMClient`
against ``LITELLM_URL``; this stdlib ``http.server`` stub answers
``POST /v1/chat/completions`` with the exact JSON shape that client parses.
It doubles as the crash-injection seam a real process affords:

- every request is COUNTED per role (planner / implementer / reviewer), so the
  suite can assert "the plan ran exactly once across two OS processes" by
  counting stub hits — the cross-process version of the coroutine suite's
  ``CountingPlanner``;
- a role can be ARMED with ``hold`` (accept the request, never respond — the
  caller blocks until it is SIGKILLed mid-call) or ``drop`` (accept the
  request, close the connection without responding). The arm fires ONCE;
  later requests of that role are served normally, which is what lets the
  recovery worker converge.

Roles are detected from the system prompt (the tiers collide: planner and
reviewer are both "strong").
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOLD_SECONDS = 300  # a held request outlives every suite timeout


class _QuietServer(ThreadingHTTPServer):
    """A SIGKILLed worker resets its sockets mid-request — that is the drill,
    not an error worth a traceback on stderr."""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        exc_type = sys.exc_info()[0]
        if exc_type in (ConnectionResetError, BrokenPipeError, TimeoutError):
            return
        super().handle_error(request, client_address)


_PLANNER_CANNED = json.dumps(
    {
        "summary": "Implement the OS-FI widget",
        "steps": ["create the demo file"],
        "risks": [],
        "files_hint": [],
    }
)

_IMPLEMENTER_CANNED = json.dumps(
    {
        "branch": "unused — the implementer pins the branch from the run identity",
        "commit_message": "unused — pinned by the implementer",
        "changes": [
            {
                "path": "forge-demo/os-fi.md",
                "operation": "create",
                "content": "# OS failure injection\n\nCreated by the stub implementer.\n",
            }
        ],
    }
)

_REVIEWER_CANNED = json.dumps(
    {"verdict": "ok", "summary": "Stub review: no concerns.", "findings": []}
)

_ROLES = ("planner", "implementer", "reviewer")


def _role_of(system: str) -> str:
    if "planning agent" in system:
        return "planner"
    if "implementation agent" in system:
        return "implementer"
    if "review agent" in system:
        return "reviewer"
    return "unknown"


class LLMStub:
    """Canned LiteLLM proxy: counts every model call, can hold or drop one."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests: dict[str, int] = {role: 0 for role in _ROLES + ("unknown",)}
        self.served: dict[str, int] = {role: 0 for role in _ROLES + ("unknown",)}
        self.held: dict[str, int] = {role: 0 for role in _ROLES + ("unknown",)}
        self.dropped: dict[str, int] = {role: 0 for role in _ROLES + ("unknown",)}
        self._armed: dict[str, str] = {}  # role -> "hold" | "drop" (one-shot)
        self._server = _QuietServer(("127.0.0.1", 0), self._make_handler())
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread: threading.Thread | None = None

    # -- control ------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def arm(self, role: str, effect: str) -> None:
        """The NEXT request of *role* is held (never answered) or dropped."""
        assert effect in ("hold", "drop"), effect
        with self._lock:
            self._armed[role] = effect

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {
                "requests": dict(self.requests),
                "served": dict(self.served),
                "held": dict(self.held),
                "dropped": dict(self.dropped),
            }

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # -- request handling ----------------------------------------------------

    def _canned_for(self, role: str) -> str:
        return {
            "planner": _PLANNER_CANNED,
            "implementer": _IMPLEMENTER_CANNED,
            "reviewer": _REVIEWER_CANNED,
        }.get(role, "{}")

    def _make_handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 — stdlib naming
                if self.path != "/v1/chat/completions":
                    self._send_json(404, {"error": f"unhandled stub path {self.path}"})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    self._send_json(400, {"error": "bad request body"})
                    return
                messages = body.get("messages") or []
                system = str(messages[0].get("content") or "") if messages else ""
                role = _role_of(system)

                effect = None
                with stub._lock:
                    stub.requests[role] = stub.requests.get(role, 0) + 1
                    effect = stub._armed.pop(role, None)
                    if effect == "hold":
                        stub.held[role] = stub.held.get(role, 0) + 1
                    elif effect == "drop":
                        stub.dropped[role] = stub.dropped.get(role, 0) + 1

                if effect == "drop":
                    # Accept the work, then vanish before responding: the
                    # client sees "server disconnected without a response".
                    self.close_connection = True
                    return
                if effect == "hold":
                    # Park the handler thread: the caller stays blocked on the
                    # response until it is SIGKILLed mid-call.
                    time.sleep(HOLD_SECONDS)
                    try:
                        self._send_json(200, self._envelope(stub._canned_for(role)))
                    except OSError:
                        pass  # the client died while we held the response
                    return

                with stub._lock:
                    stub.served[role] = stub.served.get(role, 0) + 1
                self._send_json(200, self._envelope(stub._canned_for(role)))

            @staticmethod
            def _envelope(content: str) -> dict:
                return {
                    "id": "stub-completion",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 11,
                        "total_tokens": 22,
                    },
                }

            def _send_json(self, status: int, payload: dict) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args) -> None:  # keep pytest output clean
                return

        return Handler
