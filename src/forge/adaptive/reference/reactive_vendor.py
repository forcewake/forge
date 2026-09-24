#!/usr/bin/env python3
"""The REACTIVE SCRIPTED VENDOR (``scripted-causal``) — the executable
half of the steering-causality proof harness (R37-10 #291 → R37-19 #300).

**This module is REFERENCE material** (label:
:data:`REFERENCE_PACKAGE_LABEL` — spelled literally here, no import): a
tiny stdlib-only executable speaking the real codex app-server JSONL
wire (the same contract ``tests/production_entry/fake_vendor.py``
speaks) that, mid-turn, POLLS the control plane's real HTTP surface
(``/lane/controls`` with the lane's work-scoped token — the same
endpoint the lane's own LaneControlChannel drains) for the operator's
steer, consumes it and APPLIES the instruction's transformation as its
NEXT edit. Causal by construction: the script reads the durable
guidance mid-turn and its subsequent edit demonstrably depends on what
it read. With no steer inside the bounded window it deterministically
performs the task's default follow-up — the counterfactual trajectory.
It is a SCRIPT, never a model: every run it backs is labelled
:data:`SCRIPTED_CAUSAL_PROVENANCE`, never presented as a real-model
result.

It was extracted from ``steering_causality.py`` (whose PURE GRADER
stays runtime) so the runtime contract module no longer hosts the
scenario executable. The extraction is SELF-CONTAINED BY DESIGN —
stdlib only, no forge import — because this file runs in TWO loading
contexts:

- inside the venv, as ``forge.adaptive.reference.reactive_vendor`` (the
  compat re-export on ``steering_causality`` keeps the old attribute
  paths working);
- SPAWNED, loaded BY PATH from ``steering_causality.py``'s ``__main__``
  (the lane subprocess's ``CODEX_BINARY`` — ``python <that-file>
  app-server`` under ``/usr/bin/env python3``, where no forge import is
  guaranteed to resolve).

The frozen demonstration task, the checkable-instruction grammar and
the executable live HERE together (one decision): the grader's arm 3
consumes the same grammar through
:func:`steering_causality.grade_causality`.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "ACTIONS_ENV",
    "BASE_POLICY_CONTENT",
    "DEFAULT_ACTIONS_ENV",
    "DEFAULT_FIRST_EDIT",
    "DEFAULT_FOLLOWUP_CONTENT",
    "FOLLOWUP_PATH",
    "POLICY_PATH",
    "POLL_MODE_ENV",
    "SCRIPTED_CAUSAL_PROVENANCE",
    "STEER_POLL_INTERVAL_S_ENV",
    "STEER_WAIT_S_ENV",
    "STEERING_DISABLED_ENV",
    "STEERING_TASK_BRIEF",
    "STEERING_TASK_INSTRUCTION",
    "EVENTLOG_ENV",
    "SemanticTarget",
    "apply_instruction",
    "apply_rename",
    "parse_instruction",
    "run_app_server",
    "run_once",
    "vendor_main",
]


# ---------------------------------------------------------------------------
# The frozen demonstration task (shared by the vendor, the trace, the script)
# ---------------------------------------------------------------------------

#: The one file the demonstration task lives in (relative to the checkout).
POLICY_PATH = "policy.py"

#: The task's starting bytes: a tiny policy module whose knob is named
#: ``refund_limit`` — the name the operator's steer will rename.
BASE_POLICY_CONTENT = (
    "refund_limit = 100\n\n\ndef allows(amount):\n    return amount <= refund_limit\n"
)

#: The DEFAULT (unsteered) trajectory: the vendor first lands the task's
#: ordinary change — a clamp helper over the existing knob — then, when
#: no steer arrives inside the window, its follow-up edit documents the
#: knob in a notes file. The steer REPLACES that follow-up with the
#: rename; the two trajectories therefore differ in both bytes and files.
DEFAULT_FIRST_EDIT = (
    BASE_POLICY_CONTENT
    + "\n"
    + "\n"
    + "def clamp(amount):\n"
    + "    return max(0, min(amount, refund_limit))\n"
)

#: The follow-up file the unsteered trajectory writes (naming the OLD
#: knob — the counterfactual keeps ``refund_limit`` everywhere).
FOLLOWUP_PATH = "notes.md"
DEFAULT_FOLLOWUP_CONTENT = (
    "# Policy notes\n\nThe refund gate is `refund_limit` (100).\n"
    "Orders at or below it are allowed without review.\n"
)

#: The task brief the lane hands the vendor (``.forge/brief.md``).
STEERING_TASK_BRIEF = (
    "# Implement the purchase policy helper\n\n"
    f"In `{POLICY_PATH}`, add a `clamp(amount)` helper that bounds an\n"
    "amount to the existing policy knob, then finish the task with a\n"
    "short follow-up change of your choice consistent with the brief.\n"
)

#: The operator's mid-turn instruction — a CHECKABLE transformation
#: (arm 3 grades against exactly this sentence).
STEERING_TASK_INSTRUCTION = f"rename refund_limit to approval_threshold in {POLICY_PATH}"

#: The provenance label every scripted-causal run carries — a script,
#: never a model result.
SCRIPTED_CAUSAL_PROVENANCE = "scripted-causal"


# ---------------------------------------------------------------------------
# The instruction grammar + the transformation (pure)
# ---------------------------------------------------------------------------

_RENAME_RE = re.compile(
    r"^\s*rename\s+(?P<old>[A-Za-z_][A-Za-z0-9_]*)\s+to\s+"
    r"(?P<new>[A-Za-z_][A-Za-z0-9_]*)\s+in\s+(?P<path>[A-Za-z0-9_./-]+)\s*\.?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SemanticTarget:
    """One instruction's checkable transformation.

    Only ``rename`` exists today — a deliberately tiny, mechanically
    gradable vocabulary. An instruction outside it parses to ``None``
    and arm 3 refuses (an uncheckable target is never a passed arm).
    """

    kind: str  # "rename"
    old: str
    new: str
    path: str

    def as_document(self) -> dict[str, str]:
        return {"kind": self.kind, "old": self.old, "new": self.new, "path": self.path}


def parse_instruction(text: str) -> SemanticTarget | None:
    """Parse ``rename X to Y in path``; anything else is uncheckable."""
    match = _RENAME_RE.match(str(text or ""))
    if match is None:
        return None
    return SemanticTarget(
        kind="rename",
        old=match.group("old"),
        new=match.group("new"),
        path=match.group("path"),
    )


def apply_rename(content: str, old: str, new: str) -> str:
    """Replace every WORD-BOUNDED ``old`` identifier with ``new``.

    ``refund_limit`` must not corrupt ``refund_limit_cents`` — the
    transformation is identifier-scoped, the same discipline a rename
    refactor applies.
    """
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(old)}(?![A-Za-z0-9_])")
    return pattern.sub(new, content)


def apply_instruction(text: str, workspace: Mapping[str, str]) -> dict[str, str]:
    """Apply one instruction's transformation to a path->content workspace.

    Returns the NEW workspace mapping (the input is never mutated). An
    unparseable instruction, a missing file or a no-op rename returns
    the workspace UNCHANGED — the vendor only lands transformations the
    grader can check.
    """
    target = parse_instruction(text)
    if target is None or target.kind != "rename":
        return dict(workspace)
    if target.path not in workspace or target.old == target.new:
        return dict(workspace)
    updated = dict(workspace)
    updated[target.path] = apply_rename(workspace[target.path], target.old, target.new)
    return updated


# ---------------------------------------------------------------------------
# The reactive executable — stdlib-only, spawned or imported
# ---------------------------------------------------------------------------
#
# Speaks the codex app-server wire (JSONL frames on stdin, frames +
# notifications out) exactly like the production-entry fake vendor, but
# its turn does not finish before the poll cadence: after its first
# (pre-instruction) edit it enters a BOUNDED mid-turn window in which it
# polls the control plane's real HTTP surface for the operator's steer
# and answers wire frames (``turn/steer`` / ``turn/interrupt``) the
# lane's own steering drain delivers. Whichever arrives first is the
# causally consumed guidance:
#
# - ``control-plane-poll`` — the vendor read the DURABLE steer row from
#   ``GET /lane/controls`` itself (the real /steer path's store);
# - ``turn-steer-wire`` — the lane's steering session drained the row
#   and delivered ``turn/steer`` (the real /steer path's delivery leg).
#
# Both consume the same durable command; both are logged ``steer_consumed``
# with the command id and the text. The NEXT edit applies the parsed
# transformation and is logged ``vendor_edits_after_steer``.

#: Knobs (all env; the lane owns argv): the bounded steer window, the
#: poll cadence, the pre-instruction edit / default follow-up (JSON op
#: lists), the event log, and the off-switch for the counterfactual arm.
STEER_WAIT_S_ENV = "REACTIVE_VENDOR_STEER_WAIT_S"
STEER_POLL_INTERVAL_S_ENV = "REACTIVE_VENDOR_STEER_POLL_S"
ACTIONS_ENV = "REACTIVE_VENDOR_ACTIONS"
DEFAULT_ACTIONS_ENV = "REACTIVE_VENDOR_DEFAULT_ACTIONS"
EVENTLOG_ENV = "REACTIVE_VENDOR_EVENTLOG"
STEERING_DISABLED_ENV = "REACTIVE_VENDOR_STEERING_DISABLED"
#: The window's leg selection: ``control-plane+wire`` (default — the
#: vendor polls the durable rows itself AND answers the lane's wire
#: deliveries) or ``wire-only`` (a vendor whose "tool call" is slow: it
#: answers wire frames but does NOT poll — the interleaving scenario,
#: where the urgent pause must win the race against queued guidance).
POLL_MODE_ENV = "REACTIVE_VENDOR_POLL_MODE"

_DEFAULT_STEER_WAIT_S = 20.0
_DEFAULT_POLL_S = 0.05
_STDIN_BUFFER = ""


def _now() -> str:
    return (
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        + f".{int(time.time() * 1000) % 1000:03d}Z"
    )


def _log_event(kind: str, **details: Any) -> None:
    target = os.environ.get(EVENTLOG_ENV, "").strip()
    if not target:
        return
    entry = {"at": _now(), "pid": os.getpid(), "kind": kind, **details}
    with open(target, "a", encoding="utf-8") as handle:  # noqa: SIM115 - short append
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def _actions_of(env_name: str) -> list[dict[str, Any]]:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


def _apply_actions(cwd: str, actions: list[dict[str, Any]]) -> list[str]:
    """Apply the controlled file edits inside *cwd*; the touched paths."""
    from pathlib import Path  # local: stdlib, kept local like the grader's datetime

    touched: list[str] = []
    for action in actions:
        rel = str(action.get("path") or "").strip()
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            continue
        target = Path(cwd) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(action.get("content") or ""), encoding="utf-8")
        touched.append(rel)
    return touched


def _read_workspace_file(cwd: str, rel: str) -> str:
    from pathlib import Path

    path = Path(cwd) / rel
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _poll_lane_controls(url: str, token: str, work_id: str) -> list[dict[str, Any]]:
    """GET the control plane's pending-control surface (read-only, no ack).

    The SAME durable rows and the SAME endpoint the lane's
    LaneControlChannel drains — the vendor only READS, so the lane's
    session stays the single consumer of the ladder.
    """
    endpoint = f"{url.rstrip('/')}/lane/controls?work_id={work_id}"
    request = urllib.request.Request(  # noqa: S310 - the lab loopback control plane
        endpoint, headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:  # noqa: S310
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return []
    commands = body.get("commands") if isinstance(body, dict) else None
    return [entry for entry in (commands or []) if isinstance(entry, dict)]


def _drain_stdin_nonblocking() -> list[dict[str, Any]]:
    """Answer/collect any buffered wire frames without blocking the turn.

    ``select`` over stdin: every complete JSON line is handled the same
    way the (blocked) main loop would have handled it — requests get
    their result frame, ``turn/steer`` frames are returned to the caller
    as consumed guidance, ``turn/interrupt`` completes the turn.
    """
    global _STDIN_BUFFER
    import select

    frames: list[dict[str, Any]] = []
    while True:
        readable, _, _ = select.select([sys.stdin], [], [], 0)
        if not readable:
            break
        chunk = os.read(sys.stdin.fileno(), 65536)
        if not chunk:
            break
        _STDIN_BUFFER += chunk.decode("utf-8", errors="replace")
        while "\n" in _STDIN_BUFFER:
            line, _STDIN_BUFFER = _STDIN_BUFFER.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if isinstance(frame, dict):
                frames.append(frame)
    return frames


def _emit(frame: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def _consume_steer_from_frames(frames: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Extract steer guidance the LANE delivered over the wire, if any."""
    for frame in frames:
        if str(frame.get("method") or "") != "turn/steer":
            continue
        params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
        text = str(params.get("text") or params.get("input") or "")
        if text.strip():
            return {"text": text, "source": "turn-steer-wire", "command_id": ""}
    return None


def _interrupt_in_frames(frames: list[dict[str, Any]]) -> bool:
    return any(str(frame.get("method") or "") == "turn/interrupt" for frame in frames)


@dataclass(frozen=True)
class _WindowOutcome:
    """How the mid-turn steer window ended.

    ``consumed`` — the guidance the vendor read (``text`` / ``source`` /
    ``command_id``) and will apply as its NEXT edit; ``interrupted`` —
    the lane's urgent pause reached the vendor inside the window (the
    turn ends interrupted, no further edits); ``expired`` / ``disabled``
    — no steer arrived inside the bound (or this arm runs steering
    disabled): the vendor performs the task's deterministic default
    follow-up, the counterfactual trajectory.
    """

    status: str
    guidance: dict[str, Any] = field(default_factory=dict)


def _steer_window(cwd: str) -> _WindowOutcome:  # noqa: ARG001 — cwd symmetry with siblings
    """The bounded mid-turn window: poll the durable plane, answer the wire.

    Reads the control plane's real HTTP surface and the lane's own wire
    deliveries until the steer arrives, the bound elapses, or an urgent
    interrupt suspends the turn — whichever comes first.
    """
    if os.environ.get(STEERING_DISABLED_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        _log_event("steer_window_expired", reason="steering disabled on this arm")
        return _WindowOutcome("disabled")
    url = os.environ.get("FORGE_LANE_CONTROL_URL", "").strip()
    token = os.environ.get("FORGE_LANE_CONTROL_TOKEN", "").strip()
    work_id = (os.environ.get("FORGE_WORK_ID") or os.environ.get("FORGE_RUN_ID") or "").strip()
    try:
        wait_s = float(os.environ.get(STEER_WAIT_S_ENV, "") or _DEFAULT_STEER_WAIT_S)
        poll_s = float(os.environ.get(STEER_POLL_INTERVAL_S_ENV, "") or _DEFAULT_POLL_S)
    except ValueError:
        wait_s, poll_s = _DEFAULT_STEER_WAIT_S, _DEFAULT_POLL_S
    if wait_s <= 0:
        _log_event("steer_window_expired", reason="window is zero on this arm")
        return _WindowOutcome("expired")
    # No control-plane URL/token: the window still listens on the WIRE
    # (the lane's own delivery leg) — only the HTTP poll leg is absent.
    # ``wire-only`` models a vendor mid-tool-call: reachable over the
    # wire (the interrupt), never racing the lane for the durable rows.
    mode = os.environ.get(POLL_MODE_ENV, "").strip().lower()
    can_poll = bool(url and token and work_id) and mode != "wire-only"
    if not can_poll:
        _log_event("steer_window_note", reason="no control-plane URL/token/work id — wire only")

    deadline = time.monotonic() + wait_s
    polls = 0
    while time.monotonic() < deadline:
        frames = _drain_stdin_nonblocking()
        for frame in frames:
            if frame.get("id") is not None:
                _emit({"id": frame.get("id"), "result": {}})
        if _interrupt_in_frames(frames):
            _log_event("turn_interrupted", phase="mid-turn-window")
            return _WindowOutcome("interrupted")
        wire = _consume_steer_from_frames(frames)
        if wire is not None:
            _log_event("steer_consumed", **wire)
            return _WindowOutcome("consumed", guidance=wire)
        polls += 1
        if can_poll:
            for command in _poll_lane_controls(url, token, work_id):
                if str(command.get("kind") or "") != "steer":
                    continue
                text = str((command.get("payload") or {}).get("text") or "")
                if not text.strip():
                    continue
                consumed = {
                    "text": text,
                    "source": "control-plane-poll",
                    "command_id": str(command.get("command_id") or ""),
                }
                _log_event("steer_poll", polls=polls, found=consumed["command_id"] or True)
                _log_event("steer_consumed", **consumed)
                return _WindowOutcome("consumed", guidance=consumed)
        time.sleep(poll_s)
    _log_event("steer_window_expired", reason="no steer arrived inside the bounded window")
    return _WindowOutcome("expired")


def _apply_consumed_instruction(cwd: str, guidance: Mapping[str, Any]) -> list[str]:
    """The vendor's NEXT edit: the parsed transformation of the workspace."""
    text = str(guidance.get("text") or "")
    workspace = {POLICY_PATH: _read_workspace_file(cwd, POLICY_PATH)}
    updated = apply_instruction(text, workspace)
    if updated == workspace:
        _log_event(
            "vendor_noop_after_steer",
            applied="nothing-checkable",
            command_id=str(guidance.get("command_id") or ""),
        )
        return []
    touched = _apply_actions(
        cwd, [{"op": "write", "path": POLICY_PATH, "content": updated[POLICY_PATH]}]
    )
    _log_event(
        "vendor_edits_after_steer",
        touched=touched,
        applied=str(guidance.get("source") or ""),
        command_id=str(guidance.get("command_id") or ""),
    )
    return touched


def run_app_server() -> int:
    """The codex App Server wire (JSONL frames on stdin, frames out)."""
    _log_event("vendor_process_started", cwd=os.getcwd(), provenance=SCRIPTED_CAUSAL_PROVENANCE)
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
            _emit({"id": request_id, "result": {"serverInfo": {"name": "reactive-vendor"}}})
        elif method == "initialized":
            pass
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
            # The pre-instruction trajectory: the task's ordinary first edit.
            first = _apply_actions(cwd=os.getcwd(), actions=_actions_of(ACTIONS_ENV))
            _log_event("turn_started", thread_id=thread_id, turn_id=turn_id)
            _log_event("vendor_edits", touched=first, phase="pre-instruction")
            # THE MID-TURN WINDOW: the bounded poll for the operator's steer.
            window = _steer_window(cwd=os.getcwd())
            turn_status = "completed"
            if window.status == "consumed":
                _apply_consumed_instruction(os.getcwd(), window.guidance)
            elif window.status == "interrupted":
                # The urgent pause won the race inside the window: the turn
                # ends interrupted — no default follow-up, no further edits.
                turn_status = "interrupted"
            else:
                followup = _apply_actions(os.getcwd(), _actions_of(DEFAULT_ACTIONS_ENV))
                _log_event("vendor_edits", touched=followup, phase="default-followup")
            _emit(
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {"threadId": thread_id, "inputTokens": 518, "outputTokens": 231},
                }
            )
            _emit(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread_id,
                        "turn": {"id": turn_id, "status": turn_status},
                    },
                }
            )
            _log_event("turn_completed", thread_id=thread_id, turn_id=turn_id, status=turn_status)
        elif method == "turn/steer":
            _emit({"id": request_id, "result": {}})
            _log_event(
                "turn_steered",
                thread_id=thread_id,
                note="steer frame reached the main loop (outside the mid-turn window)",
            )
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
            _log_event("turn_interrupted")
        elif request_id is not None:
            _emit({"id": request_id, "result": {}})
    _log_event("vendor_process_exit", thread_id=thread_id)
    return 0


def run_once() -> int:
    """The immediate-edit spelling (a pre-positioned WIP leg)."""
    _log_event("vendor_once", cwd=os.getcwd(), provenance=SCRIPTED_CAUSAL_PROVENANCE)
    touched = _apply_actions(os.getcwd(), _actions_of(ACTIONS_ENV))
    _log_event("vendor_edits", touched=touched)
    return 0


def vendor_main(argv: list[str]) -> int:
    """The executable entry (``python <this file> app-server | --once``)."""
    if argv and argv[0] == "app-server":
        return run_app_server()
    if "--once" in argv:
        return run_once()
    sys.stderr.write("usage: reactive_vendor.py app-server | --once\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(vendor_main(sys.argv[1:]))
