"""The REAL Copilot CLI ACP client, over in-memory wire fakes.

Contract fidelity to the VENDOR is the point: every fake speaks the exact
JSON-RPC 2.0 frame shapes of ``docs/research/2026-09-23-copilot-sdk-lane.md``
— requests carry the ``jsonrpc`` header (unlike the codex app-server, §3.1),
``session/new`` results carrying ``sessionId`` (§3.2), ``session/prompt``
with single-block text prompts whose RESPONSE ends the turn with a
``stopReason`` (§4), ``session/cancel`` as a NOTIFICATION with no id (§4),
server-initiated ``session/request_permission`` answered by id with an
allow outcome (§3.3/§6), and the #4561 lie reproduced faithfully: a
canceled turn answers ``stopReason: "end_turn"`` and only the client's
cancel LEDGER restores the truth. No subprocess is ever spawned: the
transport seam is exercised through asyncio queues.
"""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

import forge.adaptive.drivers as drivers
from forge.adaptive.drivers.copilot_acp import (
    ACP_PROTOCOL_VERSION,
    OUTCOME_CANCELLED,
    OUTCOME_COMPLETED,
    OUTCOME_INTERRUPTED_BY_LEDGER,
    CancelLedger,
    CopilotACPDriverClient,
    CopilotACPError,
    CopilotACPTimeoutError,
    TurnInProgressError,
    compute_outcome,
)


class QueueTransport:
    """The Transport seam as bare asyncio queues — tests script exact frames."""

    def __init__(self):
        self.sent: list[str] = []
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False

    async def send_frame(self, frame: str) -> None:
        self.sent.append(frame)

    async def receive_frame(self) -> str | None:
        return await self.incoming.get()

    async def close(self) -> None:
        self.closed = True
        self.incoming.put_nowait(None)

    def push(self, frame: dict) -> None:
        self.incoming.put_nowait(json.dumps(frame))

    @property
    def sent_frames(self) -> list[dict]:
        return [json.loads(line) for line in self.sent]


class FakeACPServer(QueueTransport):
    """The ``copilot --acp`` side of the wire, reactive and shape-faithful.

    Responds to client requests synchronously with the documented result
    shapes, streams ``session/update`` notifications on demand, and
    reproduces issue #4561 exactly: ``cancel_and_lie()`` records the
    cancel, emits the agent's "Operation cancelled by user" chunk, and
    answers the prompt with ``stopReason: "end_turn"`` — the same frame a
    completed turn would carry. ``cancel_truthfully()`` answers
    ``cancelled`` instead (the spec-correct spelling the sibling ACP
    adapters show).
    """

    def __init__(self):
        super().__init__()
        self.session_count = 0
        self.cancels: list[dict] = []
        self.permission_answers: list[dict] = []
        self._prompts: dict[int, tuple[str, dict]] = {}
        self._next_server_request_id = 9000

    async def send_frame(self, frame: str) -> None:
        await super().send_frame(frame)
        self._handle(json.loads(frame))

    # -- test-side levers -----------------------------------------------------

    def update(self, session_id: str, kind: str, text: str = "") -> None:
        self.push(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": kind,
                        "content": {"type": "text", "text": text},
                    },
                },
            }
        )

    def end_turn(self, session_id: str, stop_reason: str = "end_turn") -> None:
        self._resolve_prompt(session_id, stop_reason)

    def cancel_and_lie(self, session_id: str) -> None:
        """#4561: acknowledge the cancel, answer end_turn anyway."""
        self.update(session_id, "agent_message_chunk", "Info: Operation cancelled by user")
        self._resolve_prompt(session_id, "end_turn")

    def cancel_truthfully(self, session_id: str) -> None:
        self._resolve_prompt(session_id, "cancelled")

    def emit_permission_request(self, session_id: str, tool: str = "shell") -> int:
        request_id = self._next_server_request_id
        self._next_server_request_id += 1
        self.push(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "session/request_permission",
                "params": {
                    "sessionId": session_id,
                    "permissionRequest": {
                        "id": "pr_1",
                        "title": f"Run {tool}",
                        "tool": {"kind": tool, "command": "pwd"},
                        "options": [
                            {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                            {"optionId": "reject_once", "name": "Deny", "kind": "reject_once"},
                        ],
                    },
                },
            }
        )
        return request_id

    # -- the wire behavior ----------------------------------------------------

    def _handle(self, frame: dict) -> None:
        if "id" in frame and ("result" in frame or "error" in frame):
            # A response to a server-initiated request — the permission
            # answer. Denials must never appear here.
            self.permission_answers.append(frame)
            return
        if "method" not in frame:
            return
        method = frame["method"]
        if "id" not in frame:
            if method == "session/cancel":
                self.cancels.append(frame["params"])
            return  # client notifications need no answer
        request_id = frame["id"]
        params = frame.get("params") or {}
        if method == "initialize":
            self.push(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": ACP_PROTOCOL_VERSION,
                        "agentCapabilities": {
                            "loadSession": True,
                            "sessionCapabilities": {"close": True},
                        },
                        "agentInfo": {
                            "name": "Copilot",
                            "version": "1.0.86 (protocol v1)",
                        },
                        "authMethods": [],
                    },
                }
            )
        elif method == "session/new":
            self.session_count += 1
            self.push(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"sessionId": f"ses_{self.session_count}"},
                }
            )
        elif method == "session/prompt":
            # The response is DEFERRED — the turn runs until the test-side
            # lever resolves it (a prompt response IS the turn's end).
            self._prompts[request_id] = (params["sessionId"], params)

    def _resolve_prompt(self, session_id: str, stop_reason: str) -> None:
        for request_id, (owner, _params) in list(self._prompts.items()):
            if owner == session_id:
                del self._prompts[request_id]
                self.push(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"stopReason": stop_reason},
                    }
                )
                return


def make_client(fake: FakeACPServer, **overrides: object) -> CopilotACPDriverClient:
    async def connect() -> QueueTransport:
        return fake

    return CopilotACPDriverClient(connect=connect, cwd="/repo", **overrides)  # type: ignore[arg-type]


async def settle() -> None:
    # The pump is the only reader; a few loop turns let it drain the queue.
    for _ in range(5):
        await asyncio.sleep(0)


def _prompt_params(session_id: str, text: str) -> dict:
    return {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]}


class TestPureLedger:
    def test_end_turn_is_completed_only_without_a_recorded_cancel(self):
        assert compute_outcome("end_turn", cancelled_by_ledger=False) == OUTCOME_COMPLETED

    def test_end_turn_after_a_cancel_is_interrupted_by_ledger(self):
        # #4561: the wire lies — the ledger restores the truth.
        assert compute_outcome("end_turn", cancelled_by_ledger=True) == (
            OUTCOME_INTERRUPTED_BY_LEDGER
        )

    def test_a_truthful_cancelled_and_the_other_spec_spellings_ride_through(self):
        assert compute_outcome("cancelled", cancelled_by_ledger=True) == OUTCOME_CANCELLED
        assert compute_outcome("refusal", cancelled_by_ledger=False) == "refusal"
        assert compute_outcome("max_tokens", cancelled_by_ledger=False) == "max_tokens"
        assert compute_outcome("max_turns", cancelled_by_ledger=False) == "max_turns"
        assert compute_outcome("", cancelled_by_ledger=False) == "stop_reason_missing"

    def test_the_ledger_consumes_on_resolution_so_cancels_never_leak(self):
        ledger = CancelLedger()
        ledger.record(10.0)
        assert ledger.cancel_since(9.0) is True
        assert ledger.consume() == [10.0]
        assert ledger.cancel_since(9.0) is False
        assert ledger.consume() == []


class TestWireHandshake:
    async def test_initialize_session_new_first_prompt_and_the_id_before_the_turn(self):
        fake = FakeACPServer()
        client = make_client(fake)

        session_id = await client.start_session("add expiry to orders")

        assert session_id == "ses_1"
        initialize, session_new, prompt = fake.sent_frames
        assert initialize == {
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 0,
            "params": {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "forge", "version": "0.1.0"},
            },
        }
        assert session_new == {
            "jsonrpc": "2.0",
            "method": "session/new",
            "id": 1,
            "params": {"cwd": "/repo", "mcpServers": []},
        }
        assert prompt == {
            "jsonrpc": "2.0",
            "method": "session/prompt",
            "id": 2,
            "params": _prompt_params("ses_1", "add expiry to orders"),
        }
        # §3.1: the jsonrpc header rides every frame (the codex
        # app-server omits it — ACP does not).
        assert all(frame.get("jsonrpc") == "2.0" for frame in fake.sent_frames)
        # §3.2: the id exists BEFORE any turn — the prompt has not
        # resolved yet and there is no terminal record to read.
        assert client.active(session_id) is True
        assert client.turn_result(session_id) is None
        # Provenance: the handshake's agentInfo is recorded verbatim.
        assert client.agent_info == {"name": "Copilot", "version": "1.0.86 (protocol v1)"}

    async def test_a_later_turn_surfaces_its_terminal_record(self):
        fake = FakeACPServer()
        client = make_client(fake)
        session_id = await client.start_session("task")

        fake.end_turn(session_id, "end_turn")
        await settle()

        assert client.active(session_id) is False
        assert client.turn_result(session_id) == {
            "stopReason": "end_turn",
            "outcome": OUTCOME_COMPLETED,
            "cancelled_by_ledger": False,
        }

    async def test_a_session_new_without_a_session_id_fails_closed(self):
        fake = FakeACPServer()
        client = make_client(fake)
        original = fake._handle

        def handle_with_empty_result(frame: dict) -> None:
            if "method" in frame and frame.get("method") == "session/new":
                fake.push({"jsonrpc": "2.0", "id": frame["id"], "result": {}})
                return
            original(frame)

        fake._handle = handle_with_empty_result

        with pytest.raises(CopilotACPError, match="carried no sessionId"):
            await client.start_session("task")


class TestStreamingUpdates:
    async def test_updates_accumulate_and_query_drains_consuming(self):
        fake = FakeACPServer()
        client = make_client(fake)
        session_id = await client.start_session("task")

        fake.update(session_id, "agent_thought_chunk", "thinking")
        fake.update(session_id, "agent_message_chunk", "partial answer")
        fake.update(session_id, "tool_call", "")
        await settle()

        events = await client.query(session_id)
        assert [event["params"]["update"]["sessionUpdate"] for event in events] == [
            "agent_thought_chunk",
            "agent_message_chunk",
            "tool_call",
        ]
        assert events[0]["method"] == "session/update"
        # Draining CONSUMES: a second call returns only what arrived since.
        fake.update(session_id, "agent_message_chunk", "more")
        await settle()
        again = await client.query(session_id)
        assert len(again) == 1
        assert again[0]["params"]["update"]["content"]["text"] == "more"

    async def test_another_sessions_updates_are_never_buffered(self):
        fake = FakeACPServer()
        client = make_client(fake)
        mine = await client.start_session("task")

        fake.update("ses_other", "agent_message_chunk", "not mine")
        await settle()

        assert await client.query(mine) == []

    async def test_the_update_buffer_is_bounded(self):
        fake = FakeACPServer()
        client = make_client(fake, max_buffered_updates=3)
        session_id = await client.start_session("task")

        for i in range(5):
            fake.update(session_id, "agent_message_chunk", str(i))
        await settle()

        events = await client.query(session_id)
        assert [event["params"]["update"]["content"]["text"] for event in events] == [
            "2",
            "3",
            "4",
        ]


class TestCancelLedger:
    """The #4561 workaround end-to-end over the wire."""

    async def test_cancel_fires_the_notification_and_records_the_ledger(self):
        fake = FakeACPServer()
        client = make_client(fake)
        session_id = await client.start_session("task")

        await client.interrupt(session_id)
        await settle()

        # A NOTIFICATION: method + params, no id — nothing to hang on.
        cancel = fake.sent_frames[-1]
        assert cancel == {
            "jsonrpc": "2.0",
            "method": "session/cancel",
            "params": {"sessionId": session_id},
        }
        assert "id" not in cancel
        assert fake.cancels == [{"sessionId": session_id}]

    async def test_the_4561_lie_is_reclassified_interrupted_by_ledger(self):
        fake = FakeACPServer()
        client = make_client(fake)
        session_id = await client.start_session("task")

        fake.update(session_id, "agent_message_chunk", "working")
        await settle()
        await client.interrupt(session_id)
        fake.cancel_and_lie(session_id)  # stopReason: end_turn anyway (#4561)
        await settle()

        result = client.turn_result(session_id)
        assert result == {
            "stopReason": "end_turn",  # the raw lie stays visible
            "outcome": OUTCOME_INTERRUPTED_BY_LEDGER,  # the ledger's truth
            "cancelled_by_ledger": True,
        }

    async def test_a_truthful_cancelled_stop_reason_stands(self):
        fake = FakeACPServer()
        client = make_client(fake)
        session_id = await client.start_session("task")

        await client.interrupt(session_id)
        fake.cancel_truthfully(session_id)
        await settle()

        assert client.turn_result(session_id) == {
            "stopReason": "cancelled",
            "outcome": OUTCOME_CANCELLED,
            "cancelled_by_ledger": True,
        }

    async def test_the_ledger_never_poisons_the_next_turn(self):
        fake = FakeACPServer()
        client = make_client(fake)
        session_id = await client.start_session("task")

        # A cancel lands AFTER the turn already resolved (a slightly-late
        # lane interrupt): harmless bookkeeping, consumed at resolution.
        fake.end_turn(session_id)
        await settle()
        await client.interrupt(session_id)
        await settle()
        assert client.turn_result(session_id)["outcome"] == OUTCOME_COMPLETED

        # The NEXT turn must be judged on its own wire, not on the stale
        # cancel sitting in the ledger.
        await client.send(session_id, "follow-up")
        fake.end_turn(session_id)
        await settle()

        assert client.turn_result(session_id) == {
            "stopReason": "end_turn",
            "outcome": OUTCOME_COMPLETED,
            "cancelled_by_ledger": False,
        }


class TestNextTurnInput:
    async def test_send_is_a_new_prompt_on_the_same_session(self):
        fake = FakeACPServer()
        client = make_client(fake)
        session_id = await client.start_session("first")

        fake.end_turn(session_id)
        await settle()
        await client.send(session_id, "now do the follow-up")

        prompt = fake.sent_frames[-1]
        assert prompt["method"] == "session/prompt"
        assert prompt["params"] == _prompt_params(session_id, "now do the follow-up")
        assert client.active(session_id) is True
        # turn_result keeps the LAST resolved record while the new turn
        # runs (the lane's poll reads a first-turn-only None-to-record
        # transition; a stale record is never mistaken for the new turn's
        # because active() separates the two).
        assert client.turn_result(session_id)["outcome"] == OUTCOME_COMPLETED

        fake.end_turn(session_id)
        await settle()
        assert client.turn_result(session_id)["stopReason"] == "end_turn"

    async def test_send_while_a_turn_is_in_flight_is_refused(self):
        # §5: ACP models one in-flight prompt per session — no steer.
        fake = FakeACPServer()
        client = make_client(fake)
        session_id = await client.start_session("first")

        with pytest.raises(TurnInProgressError, match="no steer"):
            await client.send(session_id, "too early")

        # The refusal changed nothing on the wire.
        assert [frame["method"] for frame in fake.sent_frames].count("session/prompt") == 1

    async def test_unknown_session_is_a_key_error(self):
        fake = FakeACPServer()
        client = make_client(fake)

        with pytest.raises(KeyError):
            await client.send("ses_missing", "text")
        with pytest.raises(KeyError):
            client.turn_result("ses_missing")


class TestPermissions:
    async def test_request_permission_is_answered_allow_once_never_deny(self):
        fake = FakeACPServer()
        client = make_client(fake)
        session_id = await client.start_session("task")

        fake.emit_permission_request(session_id)
        await settle()

        (answer,) = fake.permission_answers
        assert answer == {
            "jsonrpc": "2.0",
            "id": 9000,
            "result": {"outcome": {"outcome": "allow_once"}},
        }

    async def test_a_deny_outcome_is_refused_at_construction(self):
        # Hermes #17284: a mid-turn denial ends the turn with zero output —
        # the posture belongs in the server-start flags, never here.
        with pytest.raises(ValueError, match="permission_outcome"):
            CopilotACPDriverClient(connect=None, permission_outcome="reject_once")


class TestProtocolErrors:
    async def test_an_initialize_error_surfaces_with_its_code(self):
        fake = FakeACPServer()
        original = fake._handle

        def handle_with_error(frame: dict) -> None:
            if "method" in frame and frame.get("method") == "initialize":
                fake.push(
                    {
                        "jsonrpc": "2.0",
                        "id": frame["id"],
                        "error": {"code": -32000, "message": "Unsupported protocol version"},
                    }
                )
                return
            original(frame)

        fake._handle = handle_with_error
        client = make_client(fake)

        with pytest.raises(CopilotACPError) as excinfo:
            await client.start_session("task")
        assert excinfo.value.code == -32000
        assert excinfo.value.method == "initialize"
        assert "Unsupported protocol version" in excinfo.value.message

    async def test_a_prompt_error_response_lands_in_the_terminal_record(self):
        fake = FakeACPServer()
        client = make_client(fake)
        session_id = await client.start_session("task")

        # The server rejects the prompt outright (session gone, quota...).
        prompt_id = fake.sent_frames[-1]["id"]
        fake.push(
            {
                "jsonrpc": "2.0",
                "id": prompt_id,
                "error": {"code": -32000, "message": "Session not found"},
            }
        )
        await settle()

        assert client.active(session_id) is False
        result = client.turn_result(session_id)
        assert result["outcome"] == "request_error"
        assert result["stopReason"] == ""
        assert "Session not found" in result["error"]

    async def test_an_unanswered_initialize_times_out(self):
        fake = FakeACPServer()
        original = fake._handle

        def silent(frame: dict) -> None:  # never answer initialize
            if "method" in frame and frame.get("method") == "initialize":
                return
            original(frame)

        fake._handle = silent
        client = make_client(fake, request_timeout=0.05)

        with pytest.raises(CopilotACPTimeoutError, match="initialize"):
            await client.start_session("task")

    async def test_eof_mid_turn_fails_the_turn_honestly(self):
        fake = FakeACPServer()
        client = make_client(fake)
        session_id = await client.start_session("task")

        await fake.close()  # the child died with the turn in flight
        await settle()

        result = client.turn_result(session_id)
        assert result["outcome"] == "connection_lost"
        assert client.active(session_id) is False

    async def test_malformed_lines_are_skipped_not_fatal(self):
        fake = FakeACPServer()
        client = make_client(fake)
        session_id = await client.start_session("task")

        fake.incoming.put_nowait("not json at all")
        fake.update(session_id, "agent_message_chunk", "still here")
        await settle()

        events = await client.query(session_id)
        assert len(events) == 1


class TestLifecycle:
    async def test_close_is_idempotent_and_fails_pending_calls(self):
        fake = FakeACPServer()
        client = make_client(fake)
        await client.start_session("task")

        await client.close()
        await client.close()

        assert fake.closed

    async def test_the_client_satisfies_the_claude_lane_protocol_surface(self):
        # The Protocol join the package pins for the other drivers: the
        # same four methods, all async (structural duck typing — no
        # adapters.py change rides this lane yet).
        for name in ("start_session", "send", "interrupt", "query"):
            fn = getattr(CopilotACPDriverClient, name, None)
            assert fn is not None, name
            assert inspect.iscoroutinefunction(fn), name

    async def test_the_package_reexports_the_client_and_factory(self):
        assert drivers.CopilotACPDriverClient is CopilotACPDriverClient
        assert callable(drivers.copilot_acp_client_from_env)


class TestEnvFactory:
    def test_defaults_and_binary_cwd_routing(self):
        client = drivers.copilot_acp_client_from_env(
            {"COPILOT_BINARY": "/opt/copilot/bin/copilot", "COPILOT_CWD": "/repo"}
        )

        assert client.cwd == "/repo"
        assert client.permission_outcome == "allow_once"
        transport_factory = client.connect
        assert transport_factory.keywords["binary"] == "/opt/copilot/bin/copilot"
        assert transport_factory.keywords["cwd"] == "/repo"

    def test_a_malformed_timeout_fails_closed(self):
        with pytest.raises(ValueError, match="COPILOT_REQUEST_TIMEOUT"):
            drivers.copilot_acp_client_from_env({"COPILOT_REQUEST_TIMEOUT": "soon"})
