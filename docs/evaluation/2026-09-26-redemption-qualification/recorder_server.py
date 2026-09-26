"""The recording model endpoint for the R40-07 (#343) redemption qualification.

THE INSTRUMENT (issue #343 acceptance 2/3): the lane's model route is
pointed at THIS endpoint instead of a real provider, so the trace can
prove WHICH credential the actual model consumer presented — the
BROKER-SELECTED sentinel (delivered by the lane's startup redemption)
or the competing AMBIENT one (the project CI variable in the lane's
ordinary environment). A real-model pass is NOT required for identity
proof; the choice is recorded honestly in the qualification record.

Behavior (deliberately minimal, zero dependencies):

- ``GET /health`` answers 200 — the reach probe the driver runs from a
  scratch runner job before any dispatch.
- EVERY other request is captured to the JSONL log (timestamp, method,
  path, the RAW ``Authorization`` / ``x-api-key`` headers and the body's
  ``model`` member) and answered with a 400 Anthropic-shaped error, so
  the vendor client fails fast without retries. The RAW header values
  land ONLY in the maintainer-private capture log under ``data/``
  (gitignored); the published evidence carries sha256 digests, never
  the values themselves.

Run (podman, on the lab host; the runner reaches the host LAN IP):

    podman run -d --name forge-recorder \\
      -p 8480:8480 \\
      -v "$PWD/data/redemption-recorder:/recorder" \\
      -v "$PWD/docs/evaluation/2026-09-26-redemption-qualification/recorder_server.py:/server.py:ro" \\
      python:3.13-alpine python /server.py
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG_PATH = Path("/recorder/capture.jsonl")
BIND_HOST = "0.0.0.0"
BIND_PORT = 8480

_LOCK = threading.Lock()


def _capture(record: dict) -> None:
    line = json.dumps(record, sort_keys=True) + "\n"
    with _LOCK:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _record(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length else b""
        body_model = ""
        try:
            parsed = json.loads(raw_body.decode("utf-8", "replace"))
            if isinstance(parsed, dict):
                body_model = str(parsed.get("model") or "")
        except ValueError:
            body_model = ""
        authorization = self.headers.get("Authorization") or ""
        x_api_key = self.headers.get("x-api-key") or ""
        _capture(
            {
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "method": method,
                "path": self.path,
                "authorization": authorization,
                "authorization_sha256": hashlib.sha256(authorization.encode()).hexdigest(),
                "x_api_key_sha256": hashlib.sha256(x_api_key.encode()).hexdigest(),
                "anthropic_version": self.headers.get("anthropic-version") or "",
                "user_agent": self.headers.get("User-Agent") or "",
                "body_model": body_model,
            }
        )

    def do_GET(self) -> None:  # noqa: N802 — http.server naming
        if self.path == "/health":
            self._reply(200, b"forge redemption-qualification recorder\n", "text/plain")
            return
        self._record("GET")
        self._reply(400, b"{}\n", "application/json")

    def do_POST(self) -> None:  # noqa: N802 — http.server naming
        self._record("POST")
        payload = (
            json.dumps(
                {
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "forge redemption-qualification recorder: not a model endpoint",
                    },
                }
            )
            + "\n"
        ).encode()
        self._reply(400, payload, "application/json")

    def log_message(self, *args: object) -> None:  # silence stdout noise
        del args


if __name__ == "__main__":
    ThreadingHTTPServer((BIND_HOST, BIND_PORT), Handler).serve_forever()
