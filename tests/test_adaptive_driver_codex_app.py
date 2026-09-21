"""The REAL Codex App Server client, over in-memory wire fakes.

Contract fidelity to the VENDOR is the point: every fake speaks the exact
JSON-RPC frame shapes of ``docs/research/codex-app-server.md`` — requests
``{"method", "id", "params"}`` with the ``jsonrpc`` header OMITTED (§1.2),
``thread/start`` results carrying ``thread.id`` (§4.1), ``turn/started``
carrying the turn id (§7.1), ``turn/steer`` requiring ``expectedTurnId``
bound to the active turn (§5.2), ``turn/completed`` with status (§7.1),
server-initiated approval requests answered by id (§6), and the ``-32001``
overload rejection retried with backoff + jitter (§8). No subprocess is
ever spawned: the transport seam is exercised through asyncio queues.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from forge.adaptive.adapters import CodexAppAdapter
from forge.adaptive.drivers.codex_app import (
    CodexAppDriverClient,
    CodexAppError,
    CodexConnectionClosedError,
    NoActiveTurnError,
    TurnInProgressError,
    codex_app_client_from_env,
)

OVERLOAD_CODE = -32001


class QueueTransport:
    """The Transport seam as bare asyncio queues — tests script exact frames."""

    def __init__(self):
        self.sent: list[str] = []
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()

    async def send_frame(self, frame: str) -> None:
        self.sent.append(frame)

    async def receive_frame(self) -> str | None:
        return await self.incoming.get()

    async def close(self) -> None:
        self.incoming.put_nowait(None)

    def push(self, frame: dict) -> None:
        self.incoming.put_nowait(json.dumps(frame))

    @property
    def sent_frames(self) -> list[dict]:
        return [json.loads(line) for line in self.sent]


class FakeAppServer(QueueTransport):
    """The app-server's side of the wire, reactive and shape-faithful.

    Responds to client requests synchronously (as the real server would,
    soon after) with the documented result shapes, emits the turn/thread
    notifications, validates ``expectedTurnId`` against ITS active turn,
    and can be told to overload, drop interrupt responses, or fire a
    server-initiated approval request.
    """

    def __init__(self):
        super().__init__()
        self.active: dict[str, str] = {}
        self.approval_responses: list[dict] = []
        self.overload_method: str | None = None
        self.overload_remaining = 0
        self.drop_interrupt_responses = False
        self._thread_count = 0
        self._turn_count = 0
        self._next_server_request_id = 9000

    async def send_frame(self, frame: str) -> None:
        await super().send_frame(frame)
        self._handle(json.loads(frame))

    # -- test-side levers -----------------------------------------------------

    def complete_turn(self, thread_id: str, status: str = "completed") -> None:
        turn_id = self.active.pop(thread_id, None)
        self.push(self._turn_completed(thread_id, turn_id, status))

    def finish_turn_without_telling_the_client(self, thread_id: str) -> None:
        # Server-side completion whose notification never (yet) arrived —
        # the stale-expectedTurnId race §5.2 guards against.
        self.active.pop(thread_id, None)

    def emit_approval_request(self, thread_id: str, turn_id: str) -> int:
        request_id = self._next_server_request_id
        self._next_server_request_id += 1
        self.push(
            {
                "method": "item/commandExecution/requestApproval",
                "id": request_id,
                "params": {
                    "itemId": "item_1",
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "command": "rm -rf /",
                    "cwd": "/repo",
                },
            }
        )
        return request_id

    # -- the wire behavior ----------------------------------------------------

    def _handle(self, frame: dict) -> None:
        if "id" in frame and ("result" in frame or "error" in frame):
            # A response to a server-initiated request — the approval denial.
            self.approval_responses.append(frame)
            return
        if "id" not in frame:
            return  # client notifications (initialized) need no answer
        request_id = frame["id"]
        method = frame["method"]
        params = frame.get("params") or {}
        if self.overload_remaining > 0 and self.overload_method in (None, method):
            self.overload_remaining -= 1
            self.push(
                {
                    "id": request_id,
                    "error": {"code": OVERLOAD_CODE, "message": "Server overloaded; retry later."},
                }
            )
            return
        handler = {
            "initialize": self._on_initialize,
            "thread/start": self._on_thread_start,
            "turn/start": self._on_turn_start,
            "turn/steer": self._on_turn_steer,
            "turn/interrupt": self._on_turn_interrupt,
        }[method]
        handler(request_id, params)

    def _on_initialize(self, request_id: int, params: dict) -> None:
        self.push(
            {
                "id": request_id,
                "result": {
                    "userAgent": "codex/0.99.0",
                    "platformFamily": "darwin",
                    "platformOs": "macOS",
                },
            }
        )

    def _on_thread_start(self, request_id: int, params: dict) -> None:
        self._thread_count += 1
        thread_id = f"thr_{self._thread_count}"
        self.push({"method": "thread/started", "params": {"thread": {"id": thread_id}}})
        self.push(
            {
                "id": request_id,
                "result": {
                    "thread": {
                        "id": thread_id,
                        "sessionId": thread_id,
                        "preview": "",
                        "ephemeral": False,
                        "modelProvider": "openai",
                        "createdAt": 1730910000,
                    },
                    "instructionSources": [],
                },
            }
        )

    def _on_turn_start(self, request_id: int, params: dict) -> None:
        thread_id = params["threadId"]
        if thread_id in self.active:
            self.push(
                {
                    "id": request_id,
                    "error": {
                        "code": -32000,
                        "message": f"turn already in progress on {thread_id}",
                    },
                }
            )
            return
        self._turn_count += 1
        turn_id = f"turn_{self._turn_count}"
        self.active[thread_id] = turn_id
        self.push(
            {
                "method": "turn/started",
                "params": {
                    "threadId": thread_id,
                    "turn": {"id": turn_id, "items": [], "status": "inProgress"},
                },
            }
        )
        self.push(
            {
                "id": request_id,
                "result": {
                    "turn": {"id": turn_id, "status": "inProgress", "items": [], "error": None}
                },
            }
        )

    def _on_turn_steer(self, request_id: int, params: dict) -> None:
        thread_id = params["threadId"]
        expected = params.get("expectedTurnId")
        if self.active.get(thread_id) != expected:
            self.push(
                {
                    "id": request_id,
                    "error": {
                        "code": -32000,
                        "message": (
                            f"expectedTurnId {expected!r} is not the active turn on {thread_id}"
                        ),
                    },
                }
            )
            return
        self.push({"id": request_id, "result": {}})

    def _on_turn_interrupt(self, request_id: int, params: dict) -> None:
        thread_id = params["threadId"]
        turn_id = params.get("turnId")
        if self.active.get(thread_id) != turn_id:
            self.push(
                {
                    "id": request_id,
                    "error": {
                        "code": -32000,
                        "message": f"turnId {turn_id!r} is not the active turn on {thread_id}",
                    },
                }
            )
            return
        del self.active[thread_id]
        self.push(self._turn_completed(thread_id, turn_id, "interrupted"))
        if not self.drop_interrupt_responses:
            self.push({"id": request_id, "result": {}})

    def _turn_completed(self, thread_id: str, turn_id: str | None, status: str) -> dict:
        return {
            "method": "turn/completed",
            "params": {
                "threadId": thread_id,
                "turn": {"id": turn_id, "status": status, "items": [], "error": None},
            },
        }


class RecordingSleep:
    """Stands in for asyncio.sleep so backoff delays are assertable."""

    def __init__(self):
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def make_client(transport: QueueTransport, **overrides: object) -> CodexAppDriverClient:
    async def connect() -> QueueTransport:
        return transport

    return CodexAppDriverClient(connect=connect, cwd="/repo", **overrides)  # type: ignore[arg-type]


async def let_the_pump_catch_up() -> None:
    # The pump is the only reader; a few loop turns let it drain the queue.
    for _ in range(5):
        await asyncio.sleep(0)


class TestWireHandshake:
    async def test_initialize_thread_start_first_turn_and_the_thread_id_comes_back(self):
        fake = FakeAppServer()
        client = make_client(fake, model="gpt-5.6-terra")

        thread_id = await client.start_thread("add expiry to orders")

        assert thread_id == "thr_1"
        initialize, initialized, thread_start, turn_start = fake.sent_frames
        assert initialize == {
            "method": "initialize",
            "id": 0,
            "params": {
                "clientInfo": {"name": "forge", "title": "Forge Adapter", "version": "0.1.0"},
                "capabilities": {"experimentalApi": False},
            },
        }
        assert initialized == {"method": "initialized", "params": {}}
        assert thread_start == {
            "method": "thread/start",
            "id": 1,
            "params": {
                "model": "gpt-5.6-terra",
                "cwd": "/repo",
                "approvalPolicy": "never",
                "sandbox": "workspace-write",
            },
        }
        assert turn_start == {
            "method": "turn/start",
            "id": 2,
            "params": {
                "threadId": "thr_1",
                "input": [{"type": "text", "text": "add expiry to orders"}],
            },
        }
        # §1.2: the "jsonrpc":"2.0" header is omitted on the wire.
        assert all("jsonrpc" not in frame for frame in fake.sent_frames)

    async def test_the_first_turn_is_tracked_as_active(self):
        fake = FakeAppServer()
        client = make_client(fake)
        thread_id = await client.start_thread("task")
        assert client.active_turn_id(thread_id) == "turn_1"


class TestActiveTurnTracking:
    async def test_tracking_follows_the_turn_events(self):
        fake = FakeAppServer()
        client = make_client(fake)
        thread_id = await client.start_thread("task")

        # turn/started supersedes whatever the response said (§7.1).
        fake.push(
            {
                "method": "turn/started",
                "params": {
                    "threadId": thread_id,
                    "turn": {"id": "turn_777", "items": [], "status": "inProgress"},
                },
            }
        )
        await let_the_pump_catch_up()
        assert client.active_turn_id(thread_id) == "turn_777"

        # only turn/completed clears it — any status ends the turn.
        fake.push(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": {"id": "turn_777", "status": "completed", "items": [], "error": None},
                },
            }
        )
        await let_the_pump_catch_up()
        assert client.active_turn_id(thread_id) is None

    async def test_the_event_buffer_is_bounded(self):
        fake = FakeAppServer()
        client = make_client(fake, max_buffered_events=4)
        await client.start_thread("task")
        for i in range(6):
            fake.push(
                {"method": "fs/changed", "params": {"watchId": "w", "changedPaths": [str(i)]}}
            )
        await let_the_pump_catch_up()
        buffered = client.events()
        assert len(buffered) == 4
        assert [e["params"]["changedPaths"][0] for e in buffered] == ["2", "3", "4", "5"]


class TestSteering:
    async def test_steer_carries_the_tracked_expected_turn_id_and_nothing_else(self):
        fake = FakeAppServer()
        client = make_client(fake)
        thread_id = await client.start_thread("task")

        await client.steer_active_turn(thread_id, "watch the shared schema")

        # EXE-06 on the wire: steer is input injection ONLY — no
        # model/sandbox/approval keys exist here to re-permission the turn.
        assert fake.sent_frames[-1] == {
            "method": "turn/steer",
            "id": 3,
            "params": {
                "threadId": "thr_1",
                "input": [{"type": "text", "text": "watch the shared schema"}],
                "expectedTurnId": "turn_1",
            },
        }

    async def test_steer_with_a_stale_expected_turn_id_surfaces_the_server_error(self):
        fake = FakeAppServer()
        client = make_client(fake)
        thread_id = await client.start_thread("task")
        fake.finish_turn_without_telling_the_client(thread_id)

        with pytest.raises(CodexAppError, match="not the active turn") as excinfo:
            await client.steer_active_turn(thread_id, "too late")

        assert excinfo.value.method == "turn/steer"
        assert excinfo.value.code == -32000
        # Never silently queued: nothing but the failed steer hit the wire.
        methods = [frame["method"] for frame in fake.sent_frames]
        assert methods == ["initialize", "initialized", "thread/start", "turn/start", "turn/steer"]

    async def test_steer_with_no_active_turn_is_refused_without_touching_the_wire(self):
        fake = FakeAppServer()
        client = make_client(fake)
        thread_id = await client.start_thread("task")
        fake.complete_turn(thread_id)
        await let_the_pump_catch_up()

        with pytest.raises(NoActiveTurnError, match="turn/interrupt"):
            await client.steer_active_turn(thread_id, "hello?")

        assert fake.sent_frames[-1]["method"] == "turn/start"


class TestSendingTurns:
    async def test_send_turn_while_a_turn_is_active_raises_the_typed_error(self):
        fake = FakeAppServer()
        client = make_client(fake)
        thread_id = await client.start_thread("task")

        with pytest.raises(TurnInProgressError, match="turn/interrupt"):
            await client.send_turn(thread_id, "unrelated new work")

        # The guard fires BEFORE the wire — nothing was sent to deadlock on.
        assert [frame["method"] for frame in fake.sent_frames] == [
            "initialize",
            "initialized",
            "thread/start",
            "turn/start",
        ]

    async def test_send_turn_on_an_idle_thread_starts_a_new_turn(self):
        fake = FakeAppServer()
        client = make_client(fake)
        thread_id = await client.start_thread("task")
        fake.complete_turn(thread_id)
        await let_the_pump_catch_up()

        await client.send_turn(thread_id, "next chunk of conversation")

        assert fake.sent_frames[-1] == {
            "method": "turn/start",
            "id": 3,
            "params": {
                "threadId": "thr_1",
                "input": [{"type": "text", "text": "next chunk of conversation"}],
            },
        }
        assert fake.active[thread_id] == "turn_2"
        assert client.active_turn_id(thread_id) == "turn_2"
        # The handshake happened exactly once across both calls.
        assert [frame["method"] for frame in fake.sent_frames].count("initialize") == 1


class TestInterrupt:
    async def test_interrupt_resolves_on_turn_completed_even_when_the_response_never_arrives(self):
        fake = FakeAppServer()
        fake.drop_interrupt_responses = True  # §5.3/§9.4: the pending-response hang
        client = make_client(fake)
        thread_id = await client.start_thread("task")

        await client.interrupt(thread_id)  # must not hang

        assert fake.sent_frames[-1] == {
            "method": "turn/interrupt",
            "id": 3,
            "params": {"threadId": "thr_1", "turnId": "turn_1"},
        }
        assert client.active_turn_id(thread_id) is None
        completed = [e for e in client.events() if e["method"] == "turn/completed"]
        assert completed[0]["params"]["turn"]["status"] == "interrupted"

    async def test_interrupt_on_a_well_behaved_server_resolves_too(self):
        fake = FakeAppServer()
        client = make_client(fake)
        thread_id = await client.start_thread("task")

        await client.interrupt(thread_id)

        assert client.active_turn_id(thread_id) is None
        assert client.events()[-1]["params"]["turn"]["status"] == "interrupted"

    async def test_interrupt_with_no_active_turn_is_refused(self):
        fake = FakeAppServer()
        client = make_client(fake)
        thread_id = await client.start_thread("task")
        fake.complete_turn(thread_id)
        await let_the_pump_catch_up()

        with pytest.raises(NoActiveTurnError, match="no active turn"):
            await client.interrupt(thread_id)

        assert all(frame["method"] != "turn/interrupt" for frame in fake.sent_frames)


class TestDemultiplexing:
    async def test_responses_and_notifications_route_by_id(self):
        fake = FakeAppServer()
        client = make_client(fake)
        thread_id = await client.start_thread("route me by id")
        fake.complete_turn(thread_id)
        await let_the_pump_catch_up()
        assert client.active_turn_id(thread_id) is None

        # Interleaved inbound AHEAD of the pending call's response: a
        # notification, then a response to an id nobody waits on. The
        # pump must buffer the first and drop the second without losing
        # the response that answers the call when it arrives behind them.
        fake.push(
            {
                "method": "thread/status/changed",
                "params": {"threadId": thread_id, "status": "active"},
            }
        )
        fake.push({"id": 4242, "result": {"stale": True}})

        second = asyncio.create_task(client.send_turn(thread_id, "second turn"))
        await asyncio.wait_for(second, 1.0)

        assert fake.sent_frames[-1]["method"] == "turn/start"
        assert fake.sent_frames[-1]["params"]["threadId"] == thread_id
        assert client.active_turn_id(thread_id) == "turn_2"
        assert any(e["method"] == "thread/status/changed" for e in client.events())


class TestServerRequests:
    async def test_an_approval_request_is_answered_with_a_denial(self):
        fake = FakeAppServer()
        client = make_client(fake)
        thread_id = await client.start_thread("task")

        request_id = fake.emit_approval_request(thread_id, "turn_1")
        await let_the_pump_catch_up()

        # §6 pattern: answer the server's id with the decision — a denial,
        # because the lane has no human and must never block the turn.
        assert fake.approval_responses == [{"id": request_id, "result": "decline"}]


class TestOverload:
    async def test_overload_retries_with_exponential_backoff_jitter_and_cap(self):
        fake = FakeAppServer()
        fake.overload_method = "thread/start"
        fake.overload_remaining = 3
        sleeper = RecordingSleep()
        client = make_client(
            fake, sleep=sleeper, backoff_base=0.05, backoff_cap=0.15, jitter=lambda: 0.007
        )

        thread_id = await client.start_thread("task")

        assert thread_id == "thr_1"
        starts = [f for f in fake.sent_frames if f["method"] == "thread/start"]
        assert len(starts) == 4  # three rejections, one success
        assert [f["id"] for f in starts] == [1, 2, 3, 4]  # each retry is a FRESH request id
        # exponential (0.05, 0.10) then capped (0.20 -> 0.15), jitter folded in
        assert sleeper.delays == pytest.approx([0.057, 0.107, 0.157])
        assert client.active_turn_id(thread_id) == "turn_1"


class TestConnectionLifecycle:
    async def test_eof_fails_pending_calls_instead_of_hanging(self):
        transport = QueueTransport()
        client = make_client(transport)

        task = asyncio.create_task(client.start_thread("task"))
        await let_the_pump_catch_up()  # initialize is on the wire, awaiting its response
        transport.incoming.put_nowait(None)  # server EOF

        with pytest.raises(CodexConnectionClosedError):
            await asyncio.wait_for(task, 1.0)


class TestAdapterWiring:
    async def test_the_real_client_satisfies_the_frozen_adapter_contract(self):
        fake = FakeAppServer()
        adapter = CodexAppAdapter(client=make_client(fake))

        thread_id = await adapter.start_thread("stabilize the flaky queue test")
        await adapter.steer_active_turn(thread_id, "prefer the narrowest failing test first")
        await adapter.interrupt(thread_id)

        assert thread_id == "thr_1"
        assert [frame["method"] for frame in fake.sent_frames] == [
            "initialize",
            "initialized",
            "thread/start",
            "turn/start",
            "turn/steer",
            "turn/interrupt",
        ]


class TestFactoryFromEnv:
    def test_env_passthrough_and_headless_defaults(self):
        client = codex_app_client_from_env(
            {
                "CODEX_BINARY": "/opt/codex/bin/codex",
                "CODEX_CWD": "/workspace/lane",
                "CODEX_MODEL": "gpt-5.6-terra",
                "CODEX_EFFORT": "low",
            }
        )

        assert client.cwd == "/workspace/lane"
        assert client.model == "gpt-5.6-terra"
        assert client.approval_policy == "never"
        assert client.sandbox == "workspace-write"
        assert client.turn_overrides["effort"] == "low"
        policy = client.turn_overrides["sandboxPolicy"]
        # LIVE-verified asymmetry: thread/start wants kebab-case, the
        # turn/start sandboxPolicy.type wants camelCase.
        assert policy["type"] == "workspaceWrite"
        assert policy["writableRoots"] == ["/workspace/lane"]
        assert policy["networkAccess"] is True
        # The binary rides the connect seam, verifiable without spawning.
        assert client.connect.keywords["binary"] == "/opt/codex/bin/codex"

    def test_defaults_with_an_empty_env(self):
        client = codex_app_client_from_env(env={})

        assert client.connect.keywords["binary"] == "codex"
        assert client.cwd == os.getcwd()
        assert client.model is None
        assert client.approval_policy == "never"
        assert client.sandbox == "workspace-write"

    def test_legacy_camelcase_sandbox_is_normalized_to_the_wire_spelling(self):
        """LIVE-found 2026-09-21 (codex-cli 0.153.4): the wire variant enums
        are ASYMMETRIC — thread/start's sandbox is kebab-case while
        turn/start's sandboxPolicy.type is camelCase. Operator config in
        either spelling keeps working: normalized to kebab internally,
        emitted per-surface."""
        client = codex_app_client_from_env({"CODEX_SANDBOX": "workspaceWrite"})

        assert client.sandbox == "workspace-write"
        assert client.turn_overrides["sandboxPolicy"]["type"] == "workspaceWrite"

        read_only = codex_app_client_from_env({"CODEX_SANDBOX": "readOnly"})
        assert read_only.sandbox == "read-only"
        assert read_only.turn_overrides["sandboxPolicy"] == {"type": "readOnly"}
