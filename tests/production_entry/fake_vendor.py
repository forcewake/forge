#!/usr/bin/env python3
"""The controlled vendor of the production-entry layer (Q35-09).

ONE tiny executable playing the agent on the real lane, two ways:

- ``fake_vendor.py app-server`` — the codex App Server wire (JSON-RPC
  2.0 JSONL over stdio, one frame per line, no ``"jsonrpc"`` header —
  the exact protocol :class:`forge.adaptive.drivers.codex_app.
  StdioTransport` speaks). The REAL lane
  (``python -m forge.lane_driver --driver codex``) spawns it with
  ``CODEX_BINARY`` and drives it like the real vendor: on
  ``turn/start`` the vendor performs REAL file edits in its working
  directory (the lane chdir'd into the workspace generation, so the
  edits land exactly where the real agent's would) and answers with
  the ``turn/started`` / ``turn/completed`` notifications the lane
  classifies. ``turn/steer`` and ``turn/interrupt`` are recorded as
  controlled events.

- ``fake_vendor.py --once`` — perform the file edits immediately and
  exit (the pre-checkpoint WIP leg of the AT-01 trace: the vendor
  worked in the original checkout before the pause captured it).

Controlled behavior comes from the environment (never argv — the
app-server spelling owns argv):

- ``FAKE_VENDOR_ACTIONS`` — a JSON list of file operations applied per
  turn: ``{"op": "write", "path": ..., "content": ...}``,
  ``{"op": "delete", "path": ...}``, ``{"op": "chmod", "path": ...,
  "mode": 493}`` (paths are RELATIVE to the process cwd — the lane's
  active workspace generation, never an absolute escape).
- ``FAKE_VENDOR_EVENTLOG`` — an absolute path; every vendor-visible
  event (process start, thread/turn lifecycle, steer, interrupt,
  failure) is appended as one JSON line. This is how the traces prove
  ZERO vendor calls: a lane that never started a vendor session
  leaves the log EMPTY.
- ``FAKE_VENDOR_MODE`` — ``complete`` (default) | ``fail`` (the turn
  ends ``failed`` with a controlled error message).

Stdlib only; no forge imports (it runs as the lane's subprocess).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_MODE_COMPLETE = "complete"
_MODE_FAIL = "fail"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def _log_event(kind: str, **details: object) -> None:
    target = os.environ.get("FAKE_VENDOR_EVENTLOG", "").strip()
    if not target:
        return
    entry = {"at": _now(), "pid": os.getpid(), "kind": kind, **details}
    with open(target, "a", encoding="utf-8") as handle:  # noqa: SIM115 - short append
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def _actions() -> list[dict]:
    raw = os.environ.get("FAKE_VENDOR_ACTIONS", "").strip()
    if not raw:
        return []
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, list) else []


def _apply_actions(cwd: Path) -> list[str]:
    """Apply the controlled file edits INSIDE *cwd*; return the touched paths."""
    touched: list[str] = []
    for action in _actions():
        rel = str(action.get("path") or "").strip()
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            continue  # never an absolute or escaping path
        target = cwd / rel
        op = str(action.get("op") or "write")
        if op == "write":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(action.get("content") or ""), encoding="utf-8")
        elif op == "delete":
            target.unlink(missing_ok=True)
        elif op == "chmod":
            target.chmod(int(action.get("mode") or 0o644))
        else:
            continue
        touched.append(rel)
    return touched


def _mode() -> str:
    raw = os.environ.get("FAKE_VENDOR_MODE", "").strip().lower()
    return raw if raw in {_MODE_COMPLETE, _MODE_FAIL} else _MODE_COMPLETE


def run_once() -> int:
    """The immediate-edit spelling (the pre-checkpoint WIP leg)."""
    _log_event("vendor_once", cwd=os.getcwd())
    touched = _apply_actions(Path.cwd())
    _log_event("vendor_edits", touched=touched)
    return 0


def run_app_server() -> int:
    """The codex App Server wire: JSONL frames on stdin, frames + notifications out."""
    _log_event("vendor_process_started", cwd=os.getcwd())
    thread_id = ""
    turn_counter = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if not isinstance(frame, dict):
            continue
        method = str(frame.get("method") or "")
        request_id = frame.get("id")
        if method == "initialize":
            _emit({"id": request_id, "result": {"serverInfo": {"name": "fake-vendor"}}})
        elif method == "initialized":
            pass  # notification, nothing to answer
        elif method == "thread/start":
            thread_id = f"vendor-thread-{int(time.time() * 1000) % 100000}"
            _emit({"id": request_id, "result": {"thread": {"id": thread_id}}})
            _log_event("thread_started", thread_id=thread_id)
        elif method == "turn/start":
            turn_counter += 1
            turn_id = f"turn-{turn_counter}"
            _emit({"id": request_id, "result": {"turn": {"id": turn_id}}})
            _emit(
                {
                    "method": "turn/started",
                    "params": {"threadId": thread_id, "turn": {"id": turn_id}},
                }
            )
            # The controlled vendor's REAL work: file edits in the cwd the
            # lane spawned it under (the restored workspace generation).
            touched = _apply_actions(Path.cwd())
            _log_event("turn_started", thread_id=thread_id, turn_id=turn_id, touched=touched)
            _emit(
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": thread_id,
                        "inputTokens": 518,
                        "outputTokens": 231,
                    },
                }
            )
            if _mode() == _MODE_FAIL:
                _emit(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": thread_id,
                            "turn": {
                                "id": turn_id,
                                "status": "failed",
                                "error": {"message": "fake-vendor controlled failure"},
                            },
                        },
                    }
                )
                _log_event("turn_completed", thread_id=thread_id, turn_id=turn_id, status="failed")
            else:
                _emit(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": thread_id,
                            "turn": {"id": turn_id, "status": "completed"},
                        },
                    }
                )
                _log_event(
                    "turn_completed", thread_id=thread_id, turn_id=turn_id, status="completed"
                )
        elif method == "turn/steer":
            _emit({"id": request_id, "result": {}})
            _log_event("turn_steered", thread_id=thread_id)
        elif method == "turn/interrupt":
            _emit({"id": request_id, "result": {}})
            _emit(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread_id,
                        "turn": {"id": f"turn-{turn_counter}", "status": "interrupted"},
                    },
                }
            )
            _log_event("turn_interrupted", thread_id=thread_id)
        elif request_id is not None:
            # An unknown request still gets an answer — an unanswered frame
            # would hang the real client's request future.
            _emit({"id": request_id, "result": {}})
    _log_event("vendor_process_exit", thread_id=thread_id)
    return 0


def _emit(frame: dict) -> None:
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    if argv and argv[0] == "app-server":
        return run_app_server()
    if "--once" in argv:
        return run_once()
    sys.stderr.write("usage: fake_vendor.py app-server | --once\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
