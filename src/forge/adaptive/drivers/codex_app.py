"""The REAL Codex App Server client behind the ``CodexAppClient`` Protocol.

Speaks JSON-RPC 2.0 JSONL over the stdio of a spawned ``codex app-server``
process — the production transport (the WebSocket listener is experimental)
— with no vendor package: pure asyncio stdlib. Wire facts and section
references throughout are ``docs/research/codex-app-server.md``:

- §1: one JSON-RPC message per line; the ``"jsonrpc": "2.0"`` header is
  OMITTED on the wire in both directions; responses correlate by ``id``.
- §3: one ``initialize`` request per connection, answered, then the
  ``initialized`` notification.
- §4.1/§5.1: ``thread/start`` (result carries ``thread.id``), then
  ``turn/start`` with ``input: [{"type": "text", "text": ...}]``.
- §5.2: steering is ``turn/steer`` with ``expectedTurnId`` bound to the
  ACTIVE turn — pure input injection, no per-turn overrides, which is the
  wire-level shape of EXE-06: steering is guidance and can never
  re-permission the turn. New work on a busy thread goes through
  ``turn/interrupt`` first (the decision table), never a queued
  ``turn/start``.
- §5.3/§9.4: interrupt completion keys off the ``turn/completed``
  notification (status ``interrupted``), never the method response — a
  repeated/pending interrupt response is the documented hang.
- §6: server-initiated approval requests are answered with a denial
  immediately; the execution lane has no human to wait for (with
  ``approvalPolicy: "never"`` they should not arrive at all).
- §8: ``-32001`` "Server overloaded" is retried with exponential backoff
  plus jitter.

Doctrine (``docs/research/forge-harness-hacks.md``, EXE-02/EXE-06): this
driver runs in the EXECUTION LANE next to its runner, never inside the
privileged API process; the headless posture is ``approvalPolicy: "never"``
plus an explicit ``workspaceWrite`` sandbox, and no code path may block on
a prompt. The transport is a seam (:class:`Transport`) so tests drive an
in-memory fake while production spawns a subprocess — the protocol logic
never touches subprocess APIs directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import itertools
import json
import os
import random
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "CodexAppDriverClient",
    "CodexAppError",
    "CodexAppTimeoutError",
    "CodexConnectionClosedError",
    "NoActiveTurnError",
    "StdioTransport",
    "Transport",
    "TurnInProgressError",
    "codex_app_client_from_env",
]

#: §8: the server's overload rejection — the one JSON-RPC code retried.
OVERLOAD_ERROR_CODE = -32001

#: §6: the decision every server-initiated request is answered with.
_APPROVAL_DENIAL = "decline"


class CodexAppError(Exception):
    """A failure of the App Server conversation.

    ``code`` is the JSON-RPC error code when one crossed the wire (§8);
    ``method`` names the call the failure belongs to, for lane diagnostics.
    """

    def __init__(self, method: str | None, code: int | None, message: str) -> None:
        super().__init__(message)
        self.method = method
        self.code = code
        self.message = message


class CodexAppTimeoutError(CodexAppError):
    """A call went unanswered within the request budget."""


class CodexConnectionClosedError(CodexAppError):
    """The server side of the pipe ended with calls still in flight."""


class TurnInProgressError(CodexAppError):
    """``turn/start`` attempted while a turn is still active (§5.2 table).

    New work on a busy thread requires ``turn/interrupt`` first; this
    typed error replaces the deadlock of queueing blindly.
    """


class NoActiveTurnError(CodexAppError):
    """Steer/interrupt with no turn the client can bind to (§5.2/§5.3)."""


class Transport(Protocol):
    """The byte seam: one JSON-RPC message per line, both directions (§1).

    Kept separate from the protocol logic so tests inject in-memory
    queues while production spawns a subprocess. ``receive_frame``
    returns ``None`` at EOF.
    """

    async def send_frame(self, frame: str) -> None: ...
    async def receive_frame(self) -> str | None: ...
    async def close(self) -> None: ...


class StdioTransport:
    """The production transport: a spawned ``codex app-server`` over stdio.

    §0/§1: stdio is the supported production path; exactly one JSON-RPC
    message per line. stderr is inherited so server diagnostics stay
    visible in the lane log. Model/account auth needs nothing from this
    class: the child inherits the environment and reads ``~/.codex/
    auth.json`` or ``OPENAI_API_KEY``/``CODEX_API_KEY`` (§2.2).
    """

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    @classmethod
    async def spawn(
        cls,
        binary: str = "codex",
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> StdioTransport:
        # 16 MiB line limit: turn/diff/updated carries the turn's whole
        # aggregated diff (§7.2) and the asyncio default (64 KiB) would
        # raise LimitOverrunError mid-turn.
        process = await asyncio.create_subprocess_exec(
            binary,
            "app-server",
            cwd=cwd or None,
            env=dict(env) if env is not None else None,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
        )
        return cls(process)

    async def send_frame(self, frame: str) -> None:
        if self._process.stdin is None:  # pragma: no cover - closed pipe
            raise CodexConnectionClosedError(None, None, "app-server stdin is gone")
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
        # Escalate: stdin-EOF grace, SIGTERM, SIGKILL — never wait forever.
        for signal in (None, process.terminate, process.kill):
            if process.returncode is not None:
                return
            if signal is not None:
                signal()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=5.0)


@dataclass
class CodexAppDriverClient:
    """The :class:`~forge.adaptive.adapters.CodexAppClient` duck type, for real.

    Maps each Protocol method onto the App Server wire (section refs are
    ``docs/research/codex-app-server.md``):

    - ``start_thread`` — the §3 handshake (``initialize`` request, await
      the response, ``initialized`` notification), then ``thread/start``
      (§4.1), then the first ``turn/start`` with the task as its only
      text input (§5.1); returns the ``thread.id``.
    - ``send_turn`` — a NEW ``turn/start`` on the thread: conversation,
      not steering. A ``turn/start`` while a turn is active raises
      :class:`TurnInProgressError` instead of deadlocking — the §5.2
      decision table routes new work through ``turn/interrupt`` first.
    - ``steer_active_turn`` — ``turn/steer`` with ``expectedTurnId`` bound
      to the turn id tracked from ``turn/started`` (§5.2). A stale id
      surfaces the server's error; an unknown one is refused before the
      wire. Steer params carry input ONLY — no model/sandbox/approval
      overrides exist on steer, so guidance can never re-permission the
      turn (EXE-06).
    - ``interrupt`` — ``turn/interrupt`` for the tracked active turn
      (§5.3); completion is keyed off the ``turn/completed``
      notification, not the method response.

    The connection is lazy: the first call runs ``connect()``, starts the
    event pump, and performs the handshake once. ``connect`` is the
    transport seam — anything implementing :class:`Transport` works.
    """

    connect: Callable[[], Awaitable[Transport]]
    cwd: str = ""
    model: str | None = None
    approval_policy: str = "never"
    sandbox: str = "workspaceWrite"
    turn_overrides: Mapping[str, Any] = field(default_factory=dict)
    experimental_api: bool = False
    client_name: str = "forge"
    client_title: str = "Forge Adapter"
    client_version: str = "0.1.0"
    request_timeout: float = 600.0
    overload_attempts: int = 4
    backoff_base: float = 0.5
    backoff_cap: float = 8.0
    max_buffered_events: int = 4096
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    jitter: Callable[[], float] | None = None

    _ids: Iterator[int] = field(init=False, default_factory=itertools.count, repr=False)
    _pending: dict[int, tuple[str, asyncio.Future[Any]]] = field(
        init=False, default_factory=dict, repr=False
    )
    _active_turns: dict[str, str] = field(init=False, default_factory=dict, repr=False)
    _turn_waiters: dict[str, asyncio.Future[str]] = field(
        init=False, default_factory=dict, repr=False
    )
    _events: list[dict[str, Any]] = field(init=False, default_factory=list, repr=False)
    _transport: Transport | None = field(init=False, default=None, repr=False)
    _pump_task: asyncio.Task[None] | None = field(init=False, default=None, repr=False)
    _initialized: bool = field(init=False, default=False, repr=False)
    _init_lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock, repr=False)
    _last_thread_id: str | None = field(init=False, default=None, repr=False)

    # -- the CodexAppClient Protocol surface ---------------------------------

    async def start_thread(self, task: str) -> str:
        """Handshake (§3), ``thread/start`` (§4.1), first ``turn/start`` (§5.1)."""
        await self._ensure_initialized()
        result = await self._call("thread/start", self._thread_params())
        thread_id = ((result or {}).get("thread") or {}).get("id")
        if not thread_id:
            raise CodexAppError("thread/start", None, "thread/start result carried no thread.id")
        self._last_thread_id = thread_id
        await self._start_turn(thread_id, task)
        return thread_id

    async def send_turn(self, thread_id: str, text: str) -> None:
        """A NEW ``turn/start`` — conversation, never steering (§5.1/§5.2)."""
        await self._ensure_initialized()
        if thread_id in self._active_turns:
            raise TurnInProgressError(
                "turn/start",
                None,
                f"thread {thread_id!r} still has active turn "
                f"{self._active_turns[thread_id]!r} — the decision table routes new "
                "work through turn/interrupt first, never a queued turn/start",
            )
        await self._start_turn(thread_id, text)

    async def steer_active_turn(self, thread_id: str, text: str) -> None:
        """``turn/steer`` bound to the ACTIVE turn via ``expectedTurnId`` (§5.2).

        A stale id surfaces the server's error (steering a finished turn
        is an error, not a queue-for-next-turn); an unknown one is
        refused before the wire. The params carry input only — EXE-06:
        steering is guidance and cannot change execution permissions.
        """
        await self._ensure_initialized()
        turn_id = self._active_turns.get(thread_id)
        if turn_id is None:
            raise NoActiveTurnError(
                "turn/steer",
                None,
                f"no active turn on thread {thread_id!r} to steer — the decision "
                "table routes new work through turn/interrupt + turn/start",
            )
        await self._call(
            "turn/steer",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
                "expectedTurnId": turn_id,
            },
        )

    async def interrupt(self, thread_id: str) -> None:
        """``turn/interrupt`` for the active turn; done when the turn is (§5.3).

        Completion keys off the ``turn/completed`` notification — the
        method response is raced only to surface hard errors, because a
        pending/absent interrupt response is the documented hang (§5.3,
        §9.4). Without a tracked active turn this refuses rather than
        send the groundless interrupt that hangs.
        """
        await self._ensure_initialized()
        turn_id = self._active_turns.get(thread_id)
        if turn_id is None:
            raise NoActiveTurnError(
                "turn/interrupt",
                None,
                f"no active turn tracked on thread {thread_id!r} — a groundless or "
                "repeated turn/interrupt is the documented pending hang",
            )
        waiter: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._turn_waiters[thread_id] = waiter
        request_id, response = await self._send_request(
            "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
        )
        try:
            done, _ = await asyncio.wait(
                {waiter, response},
                timeout=self.request_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if waiter in done:
                exception = waiter.exception()
                if exception is not None:
                    raise exception
                return
            if response not in done:
                raise CodexAppTimeoutError(
                    "turn/interrupt",
                    None,
                    f"turn/interrupt on {thread_id!r} went unanswered within "
                    f"{self.request_timeout}s",
                )
            exception = response.exception()
            if exception is not None:
                raise exception
            # Clean {} response — the notification is still the source of truth.
            try:
                await asyncio.wait_for(waiter, timeout=self.request_timeout)
            except TimeoutError:
                raise CodexAppTimeoutError(
                    "turn/interrupt",
                    None,
                    "turn/completed(interrupted) never arrived after turn/interrupt",
                ) from None
        finally:
            self._pending.pop(request_id, None)
            if not response.done():
                response.cancel()
            if self._turn_waiters.get(thread_id) is waiter:
                del self._turn_waiters[thread_id]
            if not waiter.done():
                waiter.cancel()

    # -- inspection surfaces for the lane (not part of the Protocol) ---------

    def active_turn_id(self, thread_id: str) -> str | None:
        """The turn id currently tracked as active on *thread_id*, if any."""
        return self._active_turns.get(thread_id)

    def events(self) -> list[dict[str, Any]]:
        """The notifications buffered so far, as ``{"method", "params"}`` copies."""
        return [{"method": e["method"], "params": dict(e["params"])} for e in self._events]

    async def close(self) -> None:
        """Stop the pump and the transport; pending calls fail, they never hang."""
        pump, self._pump_task = self._pump_task, None
        if pump is not None:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.close()
        self._initialized = False
        self._fail_all_pending("the app-server connection was closed")

    # -- wire plumbing ---------------------------------------------------------

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            if self._transport is None:
                self._transport = await self.connect()
            if self._pump_task is None:
                self._pump_task = asyncio.create_task(self._pump(), name="codex-app-pump")
            await self._call(
                "initialize",
                {
                    "clientInfo": {
                        "name": self.client_name,
                        "title": self.client_title,
                        "version": self.client_version,
                    },
                    "capabilities": {"experimentalApi": self.experimental_api},
                },
            )
            await self._notify("initialized", {})
            self._initialized = True

    async def _start_turn(self, thread_id: str, text: str) -> None:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
        }
        params.update(self.turn_overrides)
        result = await self._call("turn/start", params)
        turn_id = ((result or {}).get("turn") or {}).get("id")
        if turn_id:
            self._active_turns[thread_id] = turn_id

    def _thread_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cwd": self.cwd,
            "approvalPolicy": self.approval_policy,
            "sandbox": self.sandbox,
        }
        if self.model:
            params["model"] = self.model
        return params

    async def _call(self, method: str, params: dict[str, Any]) -> Any:
        """Request/response by id, with §8 overload retry (backoff + jitter)."""
        attempt = 0
        while True:
            request_id, response = await self._send_request(method, params)
            try:
                return await asyncio.wait_for(response, timeout=self.request_timeout)
            except TimeoutError:
                self._pending.pop(request_id, None)
                raise CodexAppTimeoutError(
                    method, None, f"no response to {method} within {self.request_timeout}s"
                ) from None
            except CodexAppError as error:
                if error.code != OVERLOAD_ERROR_CODE or attempt + 1 >= self.overload_attempts:
                    raise
                delay = min(self.backoff_cap, self.backoff_base * (2**attempt))
                await self.sleep(delay + self._jitter())
                attempt += 1

    def _jitter(self) -> float:
        if self.jitter is not None:
            return self.jitter()
        return random.uniform(0, self.backoff_base)

    async def _send_request(
        self, method: str, params: dict[str, Any]
    ) -> tuple[int, asyncio.Future[Any]]:
        request_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = (method, future)
        await self._emit({"method": method, "id": request_id, "params": params})
        return request_id, future

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._emit({"method": method, "params": params})

    async def _emit(self, frame: dict[str, Any]) -> None:
        if self._transport is None:
            raise CodexConnectionClosedError(None, None, "the connection is not open")
        # §1.2: the "jsonrpc":"2.0" header is omitted on the wire.
        await self._transport.send_frame(json.dumps(frame))

    async def _pump(self) -> None:
        """The background reader: demultiplexes every incoming line (§1)."""
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
            await self._dispatch(frame)
        self._fail_all_pending("the app-server connection ended (EOF)")

    async def _dispatch(self, frame: dict[str, Any]) -> None:
        if "id" in frame and ("result" in frame or "error" in frame):
            entry = self._pending.pop(frame["id"], None)
            if entry is None:
                return
            method, future = entry
            if future.done():
                return
            if "error" in frame:
                error = frame["error"]
                future.set_exception(
                    CodexAppError(
                        method, error.get("code"), error.get("message") or "app-server error"
                    )
                )
            else:
                future.set_result(frame.get("result"))
        elif "method" in frame:
            if "id" in frame:
                await self._answer_server_request(frame)
            else:
                self._on_notification(frame)

    async def _answer_server_request(self, frame: dict[str, Any]) -> None:
        # §6: an unanswered approval blocks the turn indefinitely and the
        # execution lane has no human — every server-initiated request is
        # answered with a denial, immediately. With approvalPolicy "never"
        # this path should stay cold.
        await self._emit({"id": frame["id"], "result": _APPROVAL_DENIAL})

    def _on_notification(self, frame: dict[str, Any]) -> None:
        method = frame["method"]
        params = frame.get("params") or {}
        self._events.append({"method": method, "params": dict(params)})
        if len(self._events) > self.max_buffered_events:
            del self._events[: len(self._events) - self.max_buffered_events]
        # §7.1: turn events carry threadId context; the fallback covers a
        # server that omits it — this client only ever tracks its own threads.
        thread_id = params.get("threadId") or self._last_thread_id
        if thread_id is None:
            return
        if method == "turn/started":
            turn_id = (params.get("turn") or {}).get("id")
            if turn_id:
                self._active_turns[thread_id] = turn_id
        elif method == "turn/completed":
            self._active_turns.pop(thread_id, None)
            waiter = self._turn_waiters.pop(thread_id, None)
            if waiter is not None and not waiter.done():
                waiter.set_result((params.get("turn") or {}).get("status", "unknown"))

    def _fail_all_pending(self, reason: str) -> None:
        for method, future in self._pending.values():
            if not future.done():
                future.set_exception(CodexConnectionClosedError(method, None, reason))
        self._pending.clear()
        for waiter in self._turn_waiters.values():
            if not waiter.done():
                waiter.set_exception(CodexConnectionClosedError("turn/completed", None, reason))
        self._turn_waiters.clear()


def codex_app_client_from_env(env: Mapping[str, str] | None = None) -> CodexAppDriverClient:
    """Build the stdio client from the environment, headless by default.

    - ``CODEX_BINARY`` — the codex executable (default ``codex``); the
      subprocess runs ``<binary> app-server``.
    - ``CODEX_CWD`` — the lane working directory (default: the current
      one); it rides ``thread/start``'s ``cwd`` and the sandbox's
      ``writableRoots``.
    - ``CODEX_MODEL`` / ``CODEX_EFFORT`` — model config passthrough.
    - ``CODEX_APPROVAL_POLICY`` (default ``never``), ``CODEX_SANDBOX``
      (default ``workspaceWrite`` with explicit ``writableRoots`` and
      ``networkAccess``, §4.2's deterministic headless recipe),
      ``CODEX_NETWORK_ACCESS`` (default on — the implementation lane's
      package-registry egress, EXE-08).

    Model/account auth needs no knob here (§2.2): the child inherits the
    environment and reads ``~/.codex/auth.json`` or
    ``OPENAI_API_KEY``/``CODEX_API_KEY``.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    binary = source.get("CODEX_BINARY", "codex")
    cwd = source.get("CODEX_CWD") or os.getcwd()
    approval_policy = source.get("CODEX_APPROVAL_POLICY", "never")
    sandbox = source.get("CODEX_SANDBOX", "workspaceWrite")
    network_access = source.get("CODEX_NETWORK_ACCESS", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    turn_overrides: dict[str, Any] = {
        "sandboxPolicy": (
            {"type": "workspaceWrite", "writableRoots": [cwd], "networkAccess": network_access}
            if sandbox == "workspaceWrite"
            else {"type": sandbox}
        )
    }
    effort = source.get("CODEX_EFFORT")
    if effort:
        turn_overrides["effort"] = effort
    return CodexAppDriverClient(
        connect=functools.partial(StdioTransport.spawn, binary=binary, cwd=cwd),
        cwd=cwd,
        model=source.get("CODEX_MODEL") or None,
        approval_policy=approval_policy,
        sandbox=sandbox,
        turn_overrides=turn_overrides,
    )
