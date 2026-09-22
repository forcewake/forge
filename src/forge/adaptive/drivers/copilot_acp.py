"""The REAL GitHub Copilot CLI ACP client for the execution lane.

Speaks JSON-RPC 2.0 NDJSON over the stdio of a spawned ``copilot --acp
--stdio`` process — the Agent Client Protocol server the CLI has shipped
since the 2026-01-28 public preview. No vendor package exists (there is no
Copilot equivalent of ``claude-agent-sdk``): ACP IS the first-party
surface, and the wire is plain enough for this pure-asyncio-stdlib client
(the same doctrine as :mod:`forge.adaptive.drivers.codex_app`). Wire facts
and section references are
``docs/research/2026-09-23-copilot-sdk-lane.md``:

- §3.1: handshake is ONE ``initialize`` request (``protocolVersion: 1``,
  ``clientCapabilities``, ``clientInfo``) whose response carries
  ``agentCapabilities`` / ``agentInfo`` / ``authMethods`` — ACP v1 has NO
  ``initialized`` notification (unlike the codex app-server), and the
  ``agentInfo`` string is recorded for provenance. Server-start options
  apply to EVERY session the server later creates: the spawn line IS the
  lane's posture (``--allow-all-tools`` plus the mechanical
  ``--deny-tool shell(git ...)`` denies — deny beats allow beats
  allow-all, the documented CLI rule).
- §3.2: ``session/new {cwd, mcpServers}`` → ``{sessionId}`` — the id
  exists BEFORE any turn (better than ``-p`` JSONL mode and than the
  opencode lane's id-at-completion).
- §4: ``session/prompt {sessionId, prompt}`` blocks until the turn
  resolves; ``session/update`` notifications stream in between
  (``agent_message_chunk`` / ``agent_thought_chunk`` / ``tool_call`` /
  ``tool_call_update`` / ``plan`` / ``user_message_chunk``); the response
  carries ``stopReason``. ACP v1 defines no steer method and reference
  clients refuse a second prompt while one is in flight
  (``ErrTurnInFlight``) — input lands as NEXT-TURN prompts, and this
  client raises :class:`TurnInProgressError` instead of queueing blindly.
- §4/#4561: ``session/cancel`` is a NOTIFICATION that genuinely
  interrupts (26 ms measured), but the interrupted turn answers
  ``stopReason: "end_turn"`` — indistinguishable from completion on the
  wire. The lane therefore keeps its own :class:`CancelLedger` (the
  side-channel): a turn whose response resolves after a cancel recorded
  since its prompt was issued classifies ``interrupted_by_ledger``, never
  ``completed`` — the exact watchdog guidance the GitHub issue gives.
- §3.3/§6: the client must answer server-initiated
  ``session/request_permission`` requests — ALWAYS with an allow outcome,
  never a denial (a denial ends the turn with zero agent output, Hermes
  #17284); the mechanical commit/push denies belong in the server-start
  flags, not in the responder.
- §8: no per-turn usage crosses the ACP wire (no ``usage_update``
  emission has ever been observed) — the lane's receipt stays
  ``completeness: unknown``; nothing here fabricates tokens.

Auth needs no knob: the child inherits the environment and reads
``COPILOT_GITHUB_TOKEN`` > ``GH_TOKEN`` > ``GITHUB_TOKEN`` > stored login
(a fine-grained PAT with the "Copilot Requests" permission; classic
``ghp_`` tokens fail silently — §7). The transport is a seam
(:class:`Transport`) so tests drive an in-memory fake while production
spawns the subprocess.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import itertools
import json
import os
import signal
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "ACP_PROTOCOL_VERSION",
    "CopilotACPDriverClient",
    "CopilotACPError",
    "CopilotACPTimeoutError",
    "CopilotConnectionClosedError",
    "OUTCOME_CANCELLED",
    "OUTCOME_COMPLETED",
    "OUTCOME_INTERRUPTED_BY_LEDGER",
    "StdioTransport",
    "Transport",
    "TurnInProgressError",
    "copilot_acp_client_from_env",
    "compute_outcome",
]

#: §3.1: the ACP v1 protocol version the handshake declares.
ACP_PROTOCOL_VERSION = 1

#: Terminal-turn outcomes the ledger-aware classifier can produce. The two
#: spellings that matter for issue #4561: a truthful ``cancelled`` wire,
#: and the lane's own verdict when the wire LIES (``end_turn`` after a
#: recorded cancel).
OUTCOME_COMPLETED = "completed"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_INTERRUPTED_BY_LEDGER = "interrupted_by_ledger"

#: The only ``request_permission`` outcomes this client will ever answer
#: with (§6 / Hermes #17284: a mid-turn DENIAL ends the turn with zero
#: agent output — the lane's tool posture belongs in the server-start
#: flags, never in the responder).
_ALLOWING_OUTCOMES = frozenset({"allow_once", "allow_always"})

#: JSON-RPC error code for a server request this client cannot answer.
_METHOD_NOT_FOUND = -32601


def compute_outcome(stop_reason: str, *, cancelled_by_ledger: bool) -> str:
    """The honest outcome for one terminal ``stopReason`` (#4561).

    ``end_turn`` is ``completed`` ONLY when the ledger has no cancel
    recorded since the prompt was issued — otherwise the wire is lying
    (the interrupted turn answers ``end_turn`` on 1.0.80+; GitHub issue
    #4561) and the outcome is :data:`OUTCOME_INTERRUPTED_BY_LEDGER`.
    ``cancelled`` is taken at face value (the wire being truthful), and
    every other spec spelling (``refusal`` / ``max_tokens`` /
    ``max_turns``) rides through verbatim as the outcome — never guessed,
    never folded into failed-generic.
    """
    reason = str(stop_reason or "").strip()
    if reason == "end_turn":
        return OUTCOME_INTERRUPTED_BY_LEDGER if cancelled_by_ledger else OUTCOME_COMPLETED
    if reason == "cancelled":
        return OUTCOME_CANCELLED
    return reason or "stop_reason_missing"


class CopilotACPError(Exception):
    """A failure of the Copilot ACP conversation.

    ``code`` is the JSON-RPC error code when one crossed the wire;
    ``method`` names the call the failure belongs to, for lane diagnostics.
    """

    def __init__(self, method: str | None, code: int | None, message: str) -> None:
        super().__init__(message)
        self.method = method
        self.code = code
        self.message = message


class CopilotACPTimeoutError(CopilotACPError):
    """A call went unanswered within the request budget."""


class CopilotConnectionClosedError(CopilotACPError):
    """The server side of the pipe ended with calls still in flight."""


class TurnInProgressError(CopilotACPError):
    """``session/prompt`` attempted while a turn is still active.

    ACP v1 models ONE in-flight prompt per session and has no steer
    method (§5) — new input on a busy session is a refusal, never a
    queued prompt the server may or may not execute.
    """


class Transport(Protocol):
    """The byte seam: one JSON-RPC message per line, both directions.

    Kept separate from the protocol logic so tests inject in-memory
    queues while production spawns a subprocess (the codex-app pattern).
    ``receive_frame`` returns ``None`` at EOF.
    """

    async def send_frame(self, frame: str) -> None: ...
    async def receive_frame(self) -> str | None: ...
    async def close(self) -> None: ...


class StdioTransport:
    """The production transport: a spawned ``copilot --acp --stdio`` child.

    §3.1: stdio is the recommended subprocess transport (the TCP listener
    exists but its bind scope is disputed — §12.6 — and a lane never needs
    it). One JSON-RPC message per line; stderr is inherited so server
    diagnostics stay visible in the lane log (the Sortie lesson: silent
    auth failures are diagnosable only via stderr). The spawn line IS the
    lane's posture — server-start options apply to EVERY session (§3.1):
    ``--allow-all-tools`` with the mechanical commit/push denies unioned
    in (deny beats allow beats allow-all; whether ``--deny-tool`` fully
    binds in ACP mode is live-check pending, and the lane's real boundary
    is architectural — no write credentials, push FORBIDDEN at the remote).
    """

    #: The mechanical denies — fixed, never removable by operator env.
    DENY_TOOLS: tuple[str, ...] = ("shell(git commit)", "shell(git push)")

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    @classmethod
    async def spawn(
        cls,
        binary: str = "copilot",
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> StdioTransport:
        argv = [binary, "--acp", "--stdio", "--allow-all-tools"]
        for denied in cls.DENY_TOOLS:
            argv += ["--deny-tool", denied]
        # 16 MiB line limit: a turn's aggregated plan/tool payloads can
        # dwarf the asyncio default (64 KiB) and would raise
        # LimitOverrunError mid-turn. start_new_session puts the child in
        # its own process GROUP so close() can reap MCP children the
        # direct child spawned (the Sortie graceful-kill lesson).
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd or None,
            env=dict(env) if env is not None else None,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
            start_new_session=True,
        )
        return cls(process)

    async def send_frame(self, frame: str) -> None:
        if self._process.stdin is None:  # pragma: no cover - closed pipe
            raise CopilotConnectionClosedError(None, None, "copilot --acp stdin is gone")
        self._process.stdin.write((frame + "\n").encode("utf-8"))
        await self._process.stdin.drain()

    async def receive_frame(self) -> str | None:
        if self._process.stdout is None:  # pragma: no cover - closed pipe
            return None
        line = await self._process.stdout.readline()
        if not line:
            return None
        return line.decode("utf-8", errors="replace").rstrip("\r\n")

    async def close(self) -> None:
        process = self._process
        if process.stdin is not None:
            process.stdin.close()
        # Escalate over the PROCESS GROUP: stdin-EOF grace, SIGTERM, then
        # SIGKILL — never wait forever, never orphan MCP grandchildren.
        for sig in (None, signal.SIGTERM, signal.SIGKILL):
            if process.returncode is not None:
                return
            if sig is not None:
                self._signal_group(sig)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=5.0)

    def _signal_group(self, sig: int) -> None:
        # The group first (start_new_session made the child its leader) —
        # that reaches the direct child AND any MCP grandchildren it
        # spawned; the per-process fallback fires only when the group was
        # unreachable (already reaped, or not a leader after all).
        try:
            os.killpg(os.getpgid(self._process.pid), sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
        if self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                self._process.send_signal(sig)


@dataclass
class CancelLedger:
    """The lane-side record of ``session/cancel`` notifications (#4561).

    The ACP wire currently reports an interrupted turn as
    ``stopReason: "end_turn"`` — the protocol response CANNOT be trusted
    to say the turn was cancelled (GitHub issue #4561, reproduced on
    1.0.80). This ledger is the side-channel: every ``interrupt`` records
    the monotonic timestamp of the cancel it is about to send, and a
    terminal response is reclassified (:func:`compute_outcome`) when a
    cancel was recorded AFTER the prompt it answers was issued. Entries
    are CONSUMED when the turn resolves, so a cancel can never poison the
    session's NEXT turn — and the ``cancel_since`` timestamp guard means a
    groundless cancel sent between turns is harmless bookkeeping, never a
    verdict.
    """

    _entries: list[float] = field(default_factory=list, repr=False)

    def record(self, at: float) -> None:
        """Note a cancel being sent at monotonic time *at*."""
        self._entries.append(at)

    def cancel_since(self, at: float) -> bool:
        """Whether any recorded cancel happened at or after *at*."""
        return any(entry >= at for entry in self._entries)

    def consume(self) -> list[float]:
        """Take every entry (the turn they belonged to just resolved)."""
        entries, self._entries = self._entries, []
        return entries


@dataclass
class _LiveSession:
    """One ACP session owned by this driver."""

    session_id: str
    #: Buffered ``session/update`` notifications (``{"method", "params"}``).
    updates: list[dict[str, Any]] = field(default_factory=list)
    #: Monotonic time the in-flight prompt was issued (None when idle).
    prompt_issued_at: float | None = None
    ledger: CancelLedger = field(default_factory=CancelLedger)
    #: The terminal record of the LAST resolved turn
    #: (``stopReason`` / ``outcome`` / ``cancelled_by_ledger``), or None.
    last_result: dict[str, Any] | None = None
    turn_active: bool = False


@dataclass
class _PendingCall:
    """One outbound request the pump may still owe an answer for.

    ``future`` is None for ``session/prompt`` — the turn's response is
    consumed by the pump itself (it settles the session's terminal
    record); nobody ever awaits it, so no orphaned future warnings and no
    double-ownership of the turn verdict.
    """

    method: str
    future: asyncio.Future[Any] | None
    session_id: str | None = None


@dataclass
class CopilotACPDriverClient:
    """The Copilot ACP client behind the interactive lane.

    Satisfies the claude-lane Protocol surface (``start_session`` /
    ``send`` / ``interrupt`` / ``query``) against the ACP wire:

    - ``start_session(task)`` — the §3.1 handshake (lazily, once per
      connection), ``session/new`` (§3.2), then the FIRST
      ``session/prompt`` fired and FORGOTTEN; returns the ``sessionId``
      IMMEDIATELY — the id exists before any turn resolves, which is this
      surface's whole advantage. The turn's verdict surfaces via
      :meth:`turn_result` (the response's ``stopReason`, ledger-corrected).
    - ``send(session_id, text)`` — a NEXT-TURN prompt on the same session
      (serial prompts keep full context; there is no steer method — §5).
      A prompt while a turn is still active raises
      :class:`TurnInProgressError` instead of relying on undefined
      server-side queueing.
    - ``interrupt(session_id)`` — the ``session/cancel`` NOTIFICATION,
      recorded in the ledger BEFORE the bytes go out (no await between
      the record and the send, so a racing response can never resolve
      ahead of its own cancel's bookkeeping). Nothing is awaited beyond
      the write: the notification has no response to hang on.
    - ``query(session_id)`` — drains the buffered ``session/update``
      notifications as raw ``{"method", "params"}`` dicts (draining
      CONSUMES; a second call returns only what arrived since).

    ``session/load``/``resume`` are deliberately NOT driven: the vendor
    session store is machine-local under ``COPILOT_HOME`` and nothing
    documented exports it to another host (§3.2, §12) — cross-runner
    restore stays the forge WIP checkpoint's business.
    """

    connect: Callable[[], Awaitable[Transport]]
    cwd: str = ""
    client_name: str = "forge"
    client_version: str = "0.1.0"
    #: Budget for the HANDSHAKE/BOOKKEEPING calls only (initialize,
    #: session/new) — the prompt turn is bounded by the lane's own budget,
    #: not by this client (the response legitimately arrives at turn end).
    request_timeout: float = 60.0
    max_buffered_updates: int = 4096
    #: The ``request_permission`` answer — an ALLOW spelling only (§6:
    #: a mid-turn denial ends the turn with zero output).
    permission_outcome: str = "allow_once"

    _ids: itertools.count = field(init=False, default_factory=itertools.count, repr=False)
    _pending: dict[int, _PendingCall] = field(init=False, default_factory=dict, repr=False)
    _sessions: dict[str, _LiveSession] = field(init=False, default_factory=dict, repr=False)
    _transport: Transport | None = field(init=False, default=None, repr=False)
    _pump_task: asyncio.Task[None] | None = field(init=False, default=None, repr=False)
    _initialized: bool = field(init=False, default=False, repr=False)
    _init_lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock, repr=False)
    _agent_info: dict[str, Any] = field(init=False, default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.permission_outcome not in _ALLOWING_OUTCOMES:
            raise ValueError(
                f"permission_outcome must be one of {sorted(_ALLOWING_OUTCOMES)} — a "
                "mid-turn denial ends the turn with zero agent output (Hermes #17284); "
                "mechanical tool posture belongs in the server-start flags"
            )

    # -- the Protocol surface ----------------------------------------------

    async def start_session(self, task: str) -> str:
        """Handshake + ``session/new`` + the first prompt, fired and forgotten.

        Returns the ``sessionId`` the moment the server assigns it — BEFORE
        any turn resolves (§3.2). The turn's terminal ``stopReason``
        arrives on the pump and surfaces via :meth:`turn_result`.
        """
        await self._ensure_initialized()
        result = await self._call("session/new", {"cwd": self.cwd, "mcpServers": []})
        session_id = result.get("sessionId") if isinstance(result, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise CopilotACPError("session/new", None, "session/new result carried no sessionId")
        session = _LiveSession(session_id=session_id)
        self._sessions[session_id] = session
        await self._start_prompt(session, task)
        return session_id

    async def send(self, session_id: str, text: str) -> None:
        """NEXT-TURN input: a new ``session/prompt`` on the same session.

        ACP v1 has no steer method and reference clients refuse a second
        in-flight prompt (§5) — on a busy session this raises
        :class:`TurnInProgressError` rather than queueing a prompt whose
        execution the spec leaves undefined.
        """
        session = self._require(session_id)
        if session.turn_active:
            raise TurnInProgressError(
                "session/prompt",
                None,
                f"session {session_id!r} still has a turn in flight — ACP has no steer "
                "method; land the input as the NEXT prompt once the turn resolves",
            )
        await self._start_prompt(session, text)

    async def interrupt(self, session_id: str) -> None:
        """The ``session/cancel`` notification, ledger-recorded first.

        The ledger entry is taken BEFORE the frame is written (no await
        between them), so even a response that races the cancel cannot
        escape its own bookkeeping. Unlike a codex groundless interrupt
        there is nothing to hang on — cancel is a notification — so a
        slightly-late cancel is sent anyway and the timestamp guard keeps
        it from ever poisoning the next turn's verdict.
        """
        session = self._require(session_id)
        session.ledger.record(asyncio.get_running_loop().time())
        await self._emit(
            {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session_id}}
        )

    async def query(self, session_id: str) -> list[dict[str, Any]]:
        """Drain the buffered ``session/update`` frames (consuming)."""
        session = self._require(session_id)
        drained, session.updates = session.updates, []
        return [{"method": event["method"], "params": dict(event["params"])} for event in drained]

    # -- inspection surfaces for the lane (not part of the Protocol) -------

    def turn_result(self, session_id: str) -> dict[str, Any] | None:
        """The session's LAST resolved terminal record, or None.

        ``{"stopReason": str, "outcome": str, "cancelled_by_ledger": bool}``
        — ``outcome`` is the ledger-corrected verdict
        (:func:`compute_outcome`); ``stopReason`` keeps the raw wire value
        beside it (the lie stays visible in the evidence). None while a
        turn is in flight, and before the first turn.
        """
        session = self._require(session_id)
        result = session.last_result
        return dict(result) if result is not None else None

    @property
    def agent_info(self) -> dict[str, Any]:
        """The ``agentInfo`` the handshake reported (provenance, §3.1)."""
        return dict(self._agent_info)

    def active(self, session_id: str) -> bool:
        """Whether the session currently has a turn in flight."""
        return self._require(session_id).turn_active

    async def close(self) -> None:
        """Stop the pump and the transport; in-flight turns fail honestly."""
        pump, self._pump_task = self._pump_task, None
        if pump is not None:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.close()
        self._initialized = False
        self._fail_all_in_flight("the copilot --acp connection was closed")

    # -- wire plumbing ------------------------------------------------------

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            if self._transport is None:
                self._transport = await self.connect()
            if self._pump_task is None:
                self._pump_task = asyncio.create_task(self._pump(), name="copilot-acp-pump")
            result = await self._call(
                "initialize",
                {
                    "protocolVersion": ACP_PROTOCOL_VERSION,
                    "clientCapabilities": {},
                    "clientInfo": {"name": self.client_name, "version": self.client_version},
                },
            )
            info = result.get("agentInfo") if isinstance(result, dict) else None
            self._agent_info = dict(info) if isinstance(info, dict) else {}
            # ACP v1 has NO `initialized` notification (unlike the codex
            # app-server) — the initialize response ends the handshake.
            self._initialized = True

    async def _start_prompt(self, session: _LiveSession, text: str) -> None:
        """Issue one ``session/prompt`` and forget it (fire-and-track).

        The request id is remembered so the pump can settle the session's
        terminal record when the response — the turn's END, whenever that
        is — finally arrives. No future is created: nobody awaits the turn
        here; the lane's budget and :meth:`turn_result` own the verdict.
        The session is marked active and its issue timestamp taken BEFORE
        the write is awaited, so a cancel racing the send can never land
        ahead of the ledger window it must be judged in.
        """
        request_id = next(self._ids)
        self._pending[request_id] = _PendingCall(
            method="session/prompt", future=None, session_id=session.session_id
        )
        session.prompt_issued_at = asyncio.get_running_loop().time()
        session.turn_active = True
        await self._emit(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "session/prompt",
                "params": {
                    "sessionId": session.session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
            }
        )

    async def _call(self, method: str, params: dict[str, Any]) -> Any:
        """Request/response by id, bounded by the bookkeeping timeout."""
        request_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = _PendingCall(method=method, future=future)
        await self._emit({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout=self.request_timeout)
        except TimeoutError:
            self._pending.pop(request_id, None)
            raise CopilotACPTimeoutError(
                method, None, f"no response to {method} within {self.request_timeout}s"
            ) from None

    async def _emit(self, frame: dict[str, Any]) -> None:
        if self._transport is None:
            raise CopilotConnectionClosedError(None, None, "the connection is not open")
        await self._transport.send_frame(json.dumps(frame))

    async def _pump(self) -> None:
        """The background reader: demultiplexes every incoming line."""
        transport = self._transport
        while transport is not None:
            line = await transport.receive_frame()
            if line is None:
                break
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if isinstance(frame, dict):
                await self._dispatch(frame)
        self._fail_all_in_flight("the copilot --acp connection ended (EOF)")

    async def _dispatch(self, frame: dict[str, Any]) -> None:
        if "id" in frame and ("result" in frame or "error" in frame):
            entry = self._pending.pop(frame["id"], None)
            if entry is None:
                return
            if entry.session_id is not None:
                self._settle_prompt(entry, frame)
            elif entry.future is not None and not entry.future.done():
                if "error" in frame:
                    error = frame["error"]
                    entry.future.set_exception(
                        CopilotACPError(
                            entry.method, error.get("code"), error.get("message") or "ACP error"
                        )
                    )
                else:
                    entry.future.set_result(frame.get("result"))
        elif "method" in frame:
            if "id" in frame:
                await self._answer_server_request(frame)
            else:
                self._on_notification(frame)

    async def _answer_server_request(self, frame: dict[str, Any]) -> None:
        method = frame["method"]
        if method == "session/request_permission":
            # §3.3/§6: ALWAYS allow — a denial ends the turn with zero
            # agent output (Hermes #17284); the mechanical denies live in
            # the server-start flags where they actually bind.
            await self._emit(
                {
                    "jsonrpc": "2.0",
                    "id": frame["id"],
                    "result": {"outcome": {"outcome": self.permission_outcome}},
                }
            )
            return
        await self._emit(
            {
                "jsonrpc": "2.0",
                "id": frame["id"],
                "error": {
                    "code": _METHOD_NOT_FOUND,
                    "message": f"forge does not implement {method}",
                },
            }
        )

    def _on_notification(self, frame: dict[str, Any]) -> None:
        if frame["method"] != "session/update":
            return  # available_commands_update and friends: not lane input
        params = frame.get("params") or {}
        session_id = params.get("sessionId")
        if not isinstance(session_id, str):
            return
        session = self._sessions.get(session_id)
        if session is None:
            return  # a session this client never opened — never ours to buffer
        session.updates.append({"method": frame["method"], "params": dict(params)})
        if len(session.updates) > self.max_buffered_updates:
            del session.updates[: len(session.updates) - self.max_buffered_updates]

    def _settle_prompt(self, entry: _PendingCall, frame: dict[str, Any]) -> None:
        """Resolve one turn: terminal record + ledger consumption (#4561)."""
        assert entry.session_id is not None
        session = self._sessions.get(entry.session_id)
        if session is None:  # pragma: no cover - closed between fire and settle
            return
        issued_at = session.prompt_issued_at
        cancelled_by_ledger = session.ledger.cancel_since(issued_at) if issued_at else False
        record: dict[str, Any] = {"cancelled_by_ledger": cancelled_by_ledger}
        if "error" in frame:
            error = frame["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            record.update(stopReason="", outcome="request_error", error=str(message or "ACP error"))
        else:
            result = frame.get("result")
            stop_reason = result.get("stopReason") if isinstance(result, dict) else None
            record["stopReason"] = str(stop_reason or "")
            record["outcome"] = compute_outcome(
                record["stopReason"], cancelled_by_ledger=cancelled_by_ledger
            )
        session.last_result = record
        session.turn_active = False
        session.prompt_issued_at = None
        session.ledger.consume()

    def _fail_all_in_flight(self, reason: str) -> None:
        for entry in self._pending.values():
            if entry.future is not None and not entry.future.done():
                entry.future.set_exception(CopilotConnectionClosedError(entry.method, None, reason))
            elif entry.session_id is not None:
                session = self._sessions.get(entry.session_id)
                if session is not None and session.turn_active:
                    issued_at = session.prompt_issued_at
                    session.last_result = {
                        "stopReason": "",
                        "outcome": "connection_lost",
                        "cancelled_by_ledger": (
                            session.ledger.cancel_since(issued_at) if issued_at else False
                        ),
                        "error": reason,
                    }
                    session.turn_active = False
                    session.prompt_issued_at = None
                    session.ledger.consume()
        self._pending.clear()

    def _require(self, session_id: str) -> _LiveSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(
                f"unknown copilot session {session_id!r}: start_session must return "
                f"before the session can be sent to, interrupted or drained"
            )
        return session


def copilot_acp_client_from_env(env: Mapping[str, str] | None = None) -> CopilotACPDriverClient:
    """Build the stdio client from the environment, headless by default.

    - ``COPILOT_BINARY`` — the copilot executable (default ``copilot``);
      the subprocess runs ``<binary> --acp --stdio --allow-all-tools`` with
      the mechanical commit/push ``--deny-tool``s (§6 lane posture).
    - ``COPILOT_CWD`` — the lane working directory (default: the current
      one); it rides ``session/new``'s ``cwd`` (the session's filesystem
      root, §3.2).

    Model/account auth needs no knob (§7): the child inherits the
    environment and reads ``COPILOT_GITHUB_TOKEN`` > ``GH_TOKEN`` >
    ``GITHUB_TOKEN`` > the stored login. Malformed numbers fail CLOSED —
    a typo must never silently downgrade a bound.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    binary = source.get("COPILOT_BINARY", "copilot")
    cwd = source.get("COPILOT_CWD") or os.getcwd()

    def _seconds(name: str, default: float) -> float:
        raw = source.get(name)
        if raw is None or raw == "":
            return default
        try:
            value = float(raw)
        except ValueError:
            raise ValueError(f"{name} must be a number, got {raw!r}") from None
        if value <= 0:
            raise ValueError(f"{name} must be positive, got {value}")
        return value

    return CopilotACPDriverClient(
        connect=functools.partial(StdioTransport.spawn, binary=binary, cwd=cwd),
        cwd=cwd,
        request_timeout=_seconds("COPILOT_REQUEST_TIMEOUT", 60.0),
    )
