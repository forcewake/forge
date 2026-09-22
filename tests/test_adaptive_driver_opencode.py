"""The REAL OpenCode server client, pinned against the vendor's v2.0.10 HTTP shapes.

Every fake here mirrors the LIVE-VERIFIED surface from the "LIVE
CORRECTION — opencode v2.0.10" section of
``docs/research/2026-09-21-opencode-server.md`` — the ``/api`` route prefix, the
mandatory ``{providerID, id}`` model call, the user-echo prompt,
``session.execution.succeeded|failed`` as the turn-done signal, flat SSE
frames (``{"id", "created", "type", "data"}``), interrupt's
``{"interrupted": bool}`` answer, and the ``{"data": [...],
"cursor": {...}}`` transcript — because contract fidelity to the VENDOR
is the point. The defensive-parser tests additionally feed the older
``{type, properties}`` and ``payload``-nested shapes this client still
tolerates. No real server is contacted; ``pytest-httpx`` stands in for
one. The driver under test is the production
:class:`forge.adaptive.drivers.opencode.OpenCodeDriverClient` injected
into the frozen :class:`forge.adaptive.adapters.OpenCodeAdapter`
contract.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from forge.adaptive.adapters import OpenCodeAdapter
from forge.adaptive.drivers.opencode import (
    DEFAULT_BASE_URL,
    OpenCodeDriverClient,
    opencode_client_from_env,
)

BASE = DEFAULT_BASE_URL

# -- SSE frames exactly as v2.0.10 writes them (LIVE CORRECTION) -----------
# Flat frames: {"id", "created", "type", "data": {...}} — no properties wrapper.

SSE_HEADERS = {
    "content-type": "text/event-stream",
    "cache-control": "no-cache, no-transform",
    "x-accel-buffering": "no",
}

CONNECTED = {"id": "evt_0", "created": 1758000000.0, "type": "server.connected", "data": {}}
HEARTBEAT = {"id": "evt_h", "created": 1758000010.0, "type": "server.heartbeat", "data": {}}


def frame(event_type: str, **data: Any) -> dict:
    return {"id": "evt_1", "created": 1758000001.0, "type": event_type, "data": data}


def execution_started(session_id: str) -> dict:
    return frame("session.execution.started", sessionID=session_id)


def step_streamed(session_id: str, delta_text: str) -> dict:
    return frame("session.step.streamed", sessionID=session_id, delta=delta_text)


def succeeded(session_id: str) -> dict:
    return frame("session.execution.succeeded", sessionID=session_id)


def execution_failed(session_id: str, **extra: Any) -> dict:
    return frame("session.execution.failed", sessionID=session_id, **extra)


def permission_requested(session_id: str, request_id: str) -> dict:
    # requestID matches the v2 reply route's {requestID} parameter; the
    # event name itself was not live-verified.
    return frame(
        "session.permission.requested",
        requestID=request_id,
        sessionID=session_id,
        permission="bash",
        patterns=["rm -rf /tmp/probe"],
    )


def user_echo(text: str) -> dict:
    # The prompt POST's immediate answer (LIVE CORRECTION §Routes).
    return {
        "data": {"id": "msg_u1", "type": "user", "payload": {"text": text}, "delivery": "steer"}
    }


# An assistant transcript message exactly as v2.0.10 shapes it.
ASSISTANT_DONE = {
    "id": "msg_2",
    "time": {"created": 1758000002.0, "completed": 1758000003.0},
    "type": "assistant",
    "agent": "build",
    "model": {"id": "claude-sonnet-4-5", "providerID": "anthropic"},
    "content": [{"type": "text", "text": "done"}],
    "finish": "stop",
}

TRANSCRIPT_PAGE = {"data": [ASSISTANT_DONE], "cursor": {"next": None}}


def sse(*frames: dict) -> bytes:
    """Serialize frames in the vendor's wire format: event is always message."""
    return b"".join(f"event: message\ndata: {json.dumps(item)}\n\n".encode() for item in frames)


def register_event_stream(httpx_mock: HTTPXMock, *frames: dict) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/event", method="GET", headers=SSE_HEADERS, content=sse(*frames)
    )


def register_create(
    httpx_mock: HTTPXMock, session_id: str = "ses_1", directory: str = None
) -> None:
    body: dict[str, Any] = {"id": session_id}
    if directory is not None:
        body["directory"] = directory
    httpx_mock.add_response(url=f"{BASE}/api/session", method="POST", json={"data": body})


def register_model(httpx_mock: HTTPXMock, session_id: str) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/session/{session_id}/model", method="POST", status_code=204
    )


def register_prompt(httpx_mock: HTTPXMock, session_id: str, text: str) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/session/{session_id}/prompt", method="POST", json=user_echo(text)
    )


def register_transcript(
    httpx_mock: HTTPXMock, session_id: str, messages: list | None = None
) -> None:
    page = TRANSCRIPT_PAGE["data"] if messages is None else messages
    httpx_mock.add_response(
        url=f"{BASE}/api/session/{session_id}/message",
        method="GET",
        json={"data": page, "cursor": {}},
        is_reusable=True,
    )


def counting_transcript(httpx_mock: HTTPXMock, session_id: str, pages: list[dict]) -> dict:
    """Serve transcript pages in order (first call = the pre-prompt baseline)."""
    state = {"count": 0}

    def callback(request: httpx.Request) -> httpx.Response:
        page = pages[min(state["count"], len(pages) - 1)]
        state["count"] += 1
        return httpx.Response(200, json=page)

    httpx_mock.add_callback(
        callback, url=f"{BASE}/api/session/{session_id}/message", method="GET", is_reusable=True
    )
    return state


@contextlib.asynccontextmanager
async def opencode_lane(
    httpx_mock: HTTPXMock, **kwargs: Any
) -> AsyncIterator[OpenCodeDriverClient]:
    http = httpx.AsyncClient()
    driver = OpenCodeDriverClient(http, **kwargs)
    try:
        yield driver
    finally:
        await driver.aclose()
        await http.aclose()


class TestStartSession:
    async def test_creates_sets_the_model_then_runs_the_first_prompt_to_completion(
        self, httpx_mock: HTTPXMock
    ):
        register_event_stream(httpx_mock, CONNECTED, execution_started("ses_1"), succeeded("ses_1"))
        register_create(httpx_mock, directory="/lane/checkout")
        register_model(httpx_mock, "ses_1")
        register_prompt(httpx_mock, "ses_1", "modernize the orders service")
        register_transcript(httpx_mock, "ses_1")

        async with opencode_lane(
            httpx_mock,
            directory="/lane/checkout",
            provider_id="anthropic",
            model_id="claude-sonnet-4-5",
        ) as driver:
            session_id = await driver.start_session("modernize the orders service")

        assert session_id == "ses_1"
        create = httpx_mock.get_request(url=f"{BASE}/api/session", method="POST")
        # v2 create takes only {title?} — the checkout is pinned by serving
        # from it, not by a request field.
        assert json.loads(create.content) == {"title": "modernize the orders service"}
        model = httpx_mock.get_request(url=f"{BASE}/api/session/ses_1/model", method="POST")
        # The MANDATORY model call, in the verified {providerID, id} shape.
        assert json.loads(model.content) == {
            "model": {"providerID": "anthropic", "id": "claude-sonnet-4-5"}
        }
        prompt = httpx_mock.get_request(url=f"{BASE}/api/session/ses_1/prompt", method="POST")
        assert json.loads(prompt.content) == {"text": "modernize the orders service"}
        # The mandatory order: create -> model -> prompt.
        paths = [request.url.path for request in httpx_mock.get_requests()]
        assert paths.index("/api/session") < paths.index("/api/session/ses_1/model")
        assert paths.index("/api/session/ses_1/model") < paths.index("/api/session/ses_1/prompt")

    async def test_a_directory_mismatch_is_surfaced_not_swallowed(
        self, httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
    ):
        # One directory per server context (research §8): the client never
        # pretends the session runs where the lane asked — v2 only OBSERVES
        # the directory from the create answer.
        register_event_stream(httpx_mock, CONNECTED, succeeded("ses_1"))
        register_create(httpx_mock, directory="/elsewhere")
        register_model(httpx_mock, "ses_1")
        register_prompt(httpx_mock, "ses_1", "task")
        register_transcript(httpx_mock, "ses_1")

        async with opencode_lane(httpx_mock, directory="/lane/checkout") as driver:
            with caplog.at_level(logging.WARNING):
                await driver.start_session("task")

        assert any("/elsewhere" in record.message for record in caplog.records)


class TestPrompt:
    async def test_prompt_completes_on_execution_succeeded(self, httpx_mock: HTTPXMock):
        register_event_stream(
            httpx_mock,
            CONNECTED,
            HEARTBEAT,
            execution_started("ses_1"),
            step_streamed("ses_1", "hello"),
            succeeded("ses_1"),
        )
        register_prompt(httpx_mock, "ses_1", "say hi")
        register_transcript(httpx_mock, "ses_1")

        async with opencode_lane(httpx_mock) as driver:
            await driver.prompt("ses_1", "say hi")

        prompt = httpx_mock.get_request(url=f"{BASE}/api/session/ses_1/prompt", method="POST")
        assert json.loads(prompt.content) == {"text": "say hi"}
        # The SSE path carried the whole turn: the transcript was touched
        # exactly once, for the race-free pre-prompt baseline — no polling.
        polls = httpx_mock.get_requests(url=f"{BASE}/api/session/ses_1/message", method="GET")
        assert len(polls) == 1

    async def test_execution_failed_ends_the_wait_without_raising(self, httpx_mock: HTTPXMock):
        # The observed live failure mode: provider.auth surfaced as
        # session.execution.failed. Kept semantics: prompt RETURNS, the
        # failure stays observable via events.
        register_event_stream(
            httpx_mock,
            CONNECTED,
            execution_started("ses_1"),
            execution_failed("ses_1", error="provider.auth: Unauthorized"),
        )
        register_prompt(httpx_mock, "ses_1", "say hi")
        register_transcript(httpx_mock, "ses_1")

        async with opencode_lane(httpx_mock) as driver:
            await driver.prompt("ses_1", "say hi")
            events = await driver.events("ses_1")

        assert "session.execution.failed" in [event["type"] for event in events]

    async def test_prompt_falls_back_to_transcript_polling_when_the_stream_is_unavailable(
        self, httpx_mock: HTTPXMock
    ):
        httpx_mock.add_response(url=f"{BASE}/api/event", method="GET", status_code=500)
        register_prompt(httpx_mock, "ses_1", "say hi")
        state = counting_transcript(
            httpx_mock, "ses_1", [{"data": [], "cursor": {}}, TRANSCRIPT_PAGE]
        )

        async with opencode_lane(httpx_mock) as driver:
            await driver.prompt("ses_1", "say hi")

        # Baseline snapshot, then reconciliation saw the assistant finish.
        assert state["count"] >= 2

    async def test_prompt_reconciles_by_transcript_when_the_stream_drops_mid_turn(
        self, httpx_mock: HTTPXMock
    ):
        # /api/event has no replay: the succeeded frame was lost with the
        # stream, so completion is reconciled from the transcript.
        register_event_stream(httpx_mock, CONNECTED, step_streamed("ses_1", "partial"))
        register_prompt(httpx_mock, "ses_1", "say hi")
        state = counting_transcript(
            httpx_mock, "ses_1", [{"data": [], "cursor": {}}, TRANSCRIPT_PAGE]
        )

        async with opencode_lane(httpx_mock) as driver:
            await driver.prompt("ses_1", "say hi")

        assert state["count"] >= 2

    async def test_prompt_times_out_when_the_turn_never_ends(self, httpx_mock: HTTPXMock):
        register_event_stream(httpx_mock, CONNECTED)
        register_prompt(httpx_mock, "ses_1", "say hi")
        register_transcript(httpx_mock, "ses_1", messages=[])

        async with opencode_lane(httpx_mock, prompt_timeout=0.15) as driver:
            with pytest.raises(TimeoutError, match="session.execution"):
                await driver.prompt("ses_1", "say hi")

    async def test_tool_call_round_is_not_turn_completion(self, httpx_mock: HTTPXMock):
        """LIVE-found (e2e campaign): intermediate assistant messages
        carry finish="tool-calls" — reconciliation that treats ANY
        finish as completion declares the turn done after the model's
        first tool round while the agent loop is still running. Only
        stop/error are terminal."""
        register_event_stream(httpx_mock, CONNECTED)
        register_prompt(httpx_mock, "ses_1", "implement it")
        # The transcript shows an intermediate tool-call round FIRST,
        # the terminal message only later.
        tool_round = {
            "id": "msg_tool",
            "type": "assistant",
            "finish": "tool-calls",
            "content": [{"type": "tool", "name": "read", "executed": False}],
        }
        terminal = {
            "id": "msg_done",
            "type": "assistant",
            "finish": "stop",
            "content": [{"type": "text", "text": "DONE"}],
        }
        pages = [
            {"data": [], "cursor": {}},  # baseline snapshot: nothing yet
            {"data": [tool_round], "cursor": {}},  # mid-turn poll: NOT done
            {"data": [tool_round, terminal], "cursor": {}},  # now terminal
        ]
        calls = counting_transcript(httpx_mock, "ses_1", pages)

        async with opencode_lane(httpx_mock, prompt_timeout=10.0) as driver:
            await driver.prompt("ses_1", "implement it")

        # The tool-call round alone did NOT satisfy the wait: the
        # reconciliation had to keep polling until the terminal page.
        assert calls["count"] >= 3


class TestEvents:
    async def test_returns_the_session_filtered_buffer_with_both_event_generations_parsed(
        self, httpx_mock: HTTPXMock
    ):
        frames = (
            CONNECTED,
            HEARTBEAT,
            step_streamed("ses_1", "hello "),
            step_streamed("ses_2", "other session"),
            # Legacy EventV1: fields sit next to `type` directly, no wrapper.
            {"type": "session.step.streamed", "sessionID": "ses_1", "delta": "world"},
            # Legacy wrapper: payload under `properties`, nested in `payload`.
            {
                "payload": {"type": "session.idle", "properties": {"sessionID": "ses_1"}},
                "directory": "/lane/checkout",
                "project": "proj_1",
                "workspace": "main",
            },
        )
        register_event_stream(httpx_mock, *frames)
        register_prompt(httpx_mock, "ses_1", "say hi")
        register_transcript(httpx_mock, "ses_1")

        async with opencode_lane(httpx_mock) as driver:
            await driver.prompt("ses_1", "say hi")
            events = await driver.events("ses_1")

        types = [event["type"] for event in events]
        # Server-wide frames and OTHER sessions never leak into this session's view.
        assert "server.connected" not in types
        assert "server.heartbeat" not in types
        assert all(
            event["data"].get("sessionID") != "ses_2"
            for event in events
            if event["type"] != "transcript.reconciled"
        )
        streamed = [event for event in events if event["type"] == "session.step.streamed"]
        assert streamed[0]["data"] == {"sessionID": "ses_1", "delta": "hello "}
        assert streamed[1]["data"] == {"sessionID": "ses_1", "delta": "world"}
        # The legacy turn-done still lands — tolerated, not dropped.
        assert "session.idle" in types

    async def test_reconciles_with_the_transcript_when_events_were_missed(
        self, httpx_mock: HTTPXMock
    ):
        # Total event loss: the stream never subscribed — the transcript
        # poll is the only survivor, delivered as one synthesized event.
        httpx_mock.add_response(url=f"{BASE}/api/event", method="GET", status_code=500)
        register_prompt(httpx_mock, "ses_1", "say hi")
        counting_transcript(httpx_mock, "ses_1", [{"data": [], "cursor": {}}, TRANSCRIPT_PAGE])

        async with opencode_lane(httpx_mock) as driver:
            await driver.prompt("ses_1", "say hi")
            events = await driver.events("ses_1")

        assert [event["type"] for event in events] == ["transcript.reconciled"]
        assert events[0]["data"] == {"sessionID": "ses_1", "messages": [ASSISTANT_DONE]}
        polls = httpx_mock.get_requests(url=f"{BASE}/api/session/ses_1/message", method="GET")
        assert polls != []

    async def test_an_unknown_session_reads_no_network(self, httpx_mock: HTTPXMock):
        # pytest-httpx fails the test on any unmatched request: reading an
        # unknown session must be a pure buffer read.
        async with opencode_lane(httpx_mock) as driver:
            assert await driver.events("ses_unknown") == []


class TestAbort:
    async def test_abort_posts_interrupt_and_records_the_answer(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{BASE}/api/session/ses_1/interrupt", method="POST", json={"interrupted": True}
        )

        async with opencode_lane(httpx_mock) as driver:
            await driver.abort("ses_1")
            events = await driver.events("ses_1")

        interrupted = httpx_mock.get_request(
            url=f"{BASE}/api/session/ses_1/interrupt", method="POST"
        )
        assert interrupted is not None
        # The Protocol returns None; the honest surface is the recorded event.
        assert events == [
            {
                "id": None,
                "type": "interrupt.result",
                "data": {"sessionID": "ses_1", "interrupted": True},
            }
        ]

    async def test_an_interrupt_when_no_turn_is_in_flight_is_a_no_op(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{BASE}/api/session/ses_1/interrupt", method="POST", json={"interrupted": False}
        )

        async with opencode_lane(httpx_mock) as driver:
            await driver.abort("ses_1")  # a settled turn is not an error

        assert (
            httpx_mock.get_request(url=f"{BASE}/api/session/ses_1/interrupt", method="POST")
            is not None
        )

    async def test_an_unparseable_interrupt_answer_is_recorded_as_unknown(
        self, httpx_mock: HTTPXMock
    ):
        httpx_mock.add_response(
            url=f"{BASE}/api/session/ses_1/interrupt", method="POST", json={"unexpected": 1}
        )

        async with opencode_lane(httpx_mock) as driver:
            await driver.abort("ses_1")
            events = await driver.events("ses_1")

        assert events[0]["data"]["interrupted"] is None


class TestPermissions:
    async def test_a_permission_request_is_answered_with_the_unattended_default(
        self, httpx_mock: HTTPXMock
    ):
        register_event_stream(
            httpx_mock, CONNECTED, permission_requested("ses_1", "req_123"), succeeded("ses_1")
        )
        register_prompt(httpx_mock, "ses_1", "run the tests")
        register_transcript(httpx_mock, "ses_1")
        httpx_mock.add_response(
            url=f"{BASE}/api/session/ses_1/permission/req_123/reply", method="POST", json=True
        )

        async with opencode_lane(httpx_mock) as driver:  # default: reject
            await driver.prompt("ses_1", "run the tests")

        answer = httpx_mock.get_request(
            url=f"{BASE}/api/session/ses_1/permission/req_123/reply", method="POST"
        )
        assert json.loads(answer.content) == {"response": "reject"}

    async def test_the_permission_default_is_configurable(self, httpx_mock: HTTPXMock):
        register_event_stream(
            httpx_mock, CONNECTED, permission_requested("ses_1", "req_123"), succeeded("ses_1")
        )
        register_prompt(httpx_mock, "ses_1", "run the tests")
        register_transcript(httpx_mock, "ses_1")
        httpx_mock.add_response(
            url=f"{BASE}/api/session/ses_1/permission/req_123/reply", method="POST", json=True
        )

        async with opencode_lane(httpx_mock, permission_response="once") as driver:
            await driver.prompt("ses_1", "run the tests")

        answer = httpx_mock.get_request(
            url=f"{BASE}/api/session/ses_1/permission/req_123/reply", method="POST"
        )
        assert json.loads(answer.content) == {"response": "once"}

    async def test_the_legacy_permission_event_is_still_answered_on_the_v2_route(
        self, httpx_mock: HTTPXMock
    ):
        # The v2 event name was not live-verified: the v1 `permission.asked`
        # spelling (with `properties`, id under `id`) must still be answered.
        legacy = {
            "type": "permission.asked",
            "properties": {
                "id": "perm_9",
                "sessionID": "ses_1",
                "permission": "bash",
                "patterns": ["rm -rf /tmp/probe"],
            },
        }
        register_event_stream(httpx_mock, CONNECTED, legacy, succeeded("ses_1"))
        register_prompt(httpx_mock, "ses_1", "run the tests")
        register_transcript(httpx_mock, "ses_1")
        httpx_mock.add_response(
            url=f"{BASE}/api/session/ses_1/permission/perm_9/reply", method="POST", json=True
        )

        async with opencode_lane(httpx_mock) as driver:
            await driver.prompt("ses_1", "run the tests")

        answer = httpx_mock.get_request(
            url=f"{BASE}/api/session/ses_1/permission/perm_9/reply", method="POST"
        )
        assert json.loads(answer.content) == {"response": "reject"}

    async def test_a_permission_for_a_session_this_client_does_not_own_is_left_alone(
        self, httpx_mock: HTTPXMock
    ):
        register_event_stream(
            httpx_mock, CONNECTED, permission_requested("ses_other", "req_9"), succeeded("ses_1")
        )
        register_prompt(httpx_mock, "ses_1", "task")
        register_transcript(httpx_mock, "ses_1")

        async with opencode_lane(httpx_mock) as driver:
            await driver.prompt("ses_1", "task")

        assert httpx_mock.get_requests(url=f"{BASE}/session/ses_other/permissions/req_9") == []

    async def test_an_unknown_permission_response_is_refused(self):
        http = httpx.AsyncClient()
        with pytest.raises(ValueError, match="permission_response"):
            OpenCodeDriverClient(http, permission_response="maybe")
        await http.aclose()


class TestAuth:
    async def test_basic_auth_credentials_travel_on_every_request_including_the_stream(
        self, httpx_mock: HTTPXMock
    ):
        register_event_stream(httpx_mock, CONNECTED, succeeded("ses_1"))
        register_create(httpx_mock)
        register_model(httpx_mock, "ses_1")
        register_prompt(httpx_mock, "ses_1", "task")
        register_transcript(httpx_mock, "ses_1")

        async with opencode_lane(
            httpx_mock, auth=httpx.BasicAuth("lane-user", "lane-pass")
        ) as driver:
            await driver.start_session("task")

        expected = "Basic " + base64.b64encode(b"lane-user:lane-pass").decode()
        requests = httpx_mock.get_requests()
        assert {request.method for request in requests} >= {"GET", "POST"}
        assert all(request.headers["authorization"] == expected for request in requests)

    async def test_a_byok_provider_key_lands_via_put_auth_before_the_session(
        self, httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
    ):
        httpx_mock.add_response(url=f"{BASE}/api/auth/anthropic", method="PUT", json=True)
        register_event_stream(httpx_mock, CONNECTED, succeeded("ses_1"))
        register_create(httpx_mock)
        register_model(httpx_mock, "ses_1")
        register_prompt(httpx_mock, "ses_1", "task")
        register_transcript(httpx_mock, "ses_1")

        async with opencode_lane(
            httpx_mock, provider_key="sk-ant-test-value", provider_id="anthropic"
        ) as driver:
            with caplog.at_level(logging.DEBUG):
                await driver.start_session("task")

        key_put = httpx_mock.get_request(url=f"{BASE}/api/auth/anthropic", method="PUT")
        assert json.loads(key_put.content) == {"type": "api", "key": "sk-ant-test-value"}
        # The key lands BEFORE the session is created, and never reaches a log.
        assert httpx_mock.get_requests()[0].url == f"{BASE}/api/auth/anthropic"
        assert "sk-ant-test-value" not in caplog.text


class TestFactory:
    ENV = {
        "OPENCODE_SERVER_URL": "http://127.0.0.1:9999",
        "OPENCODE_SERVER_USERNAME": "lane",
        "OPENCODE_SERVER_PASSWORD": "s3cret",
        "OPENCODE_PROVIDER_ID": "my-local",
        "OPENCODE_MODEL_ID": "some-model",
        "OPENCODE_AGENT": "plan",
        "OPENCODE_SESSION_DIRECTORY": "/lane/checkout",
    }

    async def test_the_factory_reads_the_documented_environment(
        self, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
    ):
        base = self.ENV["OPENCODE_SERVER_URL"]
        for name, value in self.ENV.items():
            monkeypatch.setenv(name, value)
        httpx_mock.add_response(
            url=f"{base}/api/event",
            method="GET",
            headers=SSE_HEADERS,
            content=sse(CONNECTED, succeeded("ses_env")),
        )
        httpx_mock.add_response(
            url=f"{base}/api/session",
            method="POST",
            json={"data": {"id": "ses_env", "directory": "/lane/checkout"}},
        )
        httpx_mock.add_response(
            url=f"{base}/api/session/ses_env/model", method="POST", status_code=204
        )
        httpx_mock.add_response(
            url=f"{base}/api/session/ses_env/prompt", method="POST", json=user_echo("task")
        )
        httpx_mock.add_response(
            url=f"{base}/api/session/ses_env/message",
            method="GET",
            json={"data": [], "cursor": {}},
            is_reusable=True,
        )

        driver = opencode_client_from_env()
        try:
            session_id = await driver.start_session("task")
        finally:
            await driver.aclose()

        assert session_id == "ses_env"
        create = httpx_mock.get_request(url=f"{base}/api/session", method="POST")
        assert json.loads(create.content) == {"title": "task"}
        assert (
            create.headers["authorization"] == "Basic " + base64.b64encode(b"lane:s3cret").decode()
        )
        model = httpx_mock.get_request(url=f"{base}/api/session/ses_env/model", method="POST")
        assert json.loads(model.content) == {
            "model": {"providerID": "my-local", "id": "some-model"}
        }
        prompt = httpx_mock.get_request(url=f"{base}/api/session/ses_env/prompt", method="POST")
        # v2: the prompt body carries only text — agent rides no verified surface.
        assert json.loads(prompt.content) == {"text": "task"}

    async def test_the_factory_defaults_to_the_documented_local_server(
        self, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
    ):
        for name in (*self.ENV, "OPENCODE_PERMISSION_RESPONSE", "OPENCODE_PROMPT_TIMEOUT"):
            monkeypatch.delenv(name, raising=False)
        register_event_stream(httpx_mock, CONNECTED, succeeded("ses_1"))
        register_create(httpx_mock)
        register_model(httpx_mock, "ses_1")
        register_prompt(httpx_mock, "ses_1", "task")
        register_transcript(httpx_mock, "ses_1")

        driver = opencode_client_from_env()
        try:
            await driver.start_session("task")
        finally:
            await driver.aclose()

        create = httpx_mock.get_request(url=f"{BASE}/api/session", method="POST")
        assert create is not None  # the default origin, no auth configured
        assert "authorization" not in create.headers

    async def test_the_factory_applies_a_supplied_provider_key_on_first_use(
        self, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
    ):
        for name in self.ENV:
            monkeypatch.delenv(name, raising=False)
        httpx_mock.add_response(url=f"{BASE}/api/auth/anthropic", method="PUT", json=True)
        register_event_stream(httpx_mock, CONNECTED, succeeded("ses_1"))
        register_create(httpx_mock)
        register_model(httpx_mock, "ses_1")
        register_prompt(httpx_mock, "ses_1", "task")
        register_transcript(httpx_mock, "ses_1")

        driver = opencode_client_from_env(provider_key="sk-factory-test")
        try:
            await driver.start_session("task")
        finally:
            await driver.aclose()

        key_put = httpx_mock.get_request(url=f"{BASE}/api/auth/anthropic", method="PUT")
        assert json.loads(key_put.content) == {"type": "api", "key": "sk-factory-test"}

    def test_the_factory_refuses_an_env_mapping_passed_as_provider_key(self):
        # LIVE-found 2026-09-21: a positional env dict would be silently
        # ignored while the factory reads the ambient environment.
        with pytest.raises(TypeError, match="env="):
            opencode_client_from_env({"OPENCODE_SERVER_URL": "http://127.0.0.1:1"})


def _spec(paths: dict[str, Any], version: str = "2.0.3") -> dict[str, Any]:
    return {"openapi": "3.1.0", "info": {"version": version}, "paths": paths}


#: The v2.0.10 route map this client needs (LIVE CORRECTION §Routes).
V2_PATHS: dict[str, Any] = {
    "/api/session": {"post": {}},
    "/api/session/{id}/model": {"post": {}},
    "/api/session/{id}/prompt": {"post": {}},
    "/api/session/{id}/message": {"get": {}},
    "/api/session/{id}/interrupt": {"post": {}},
    "/api/session/{id}/permission/{requestID}/reply": {"post": {}},
    "/api/event": {
        "get": {"description": "frames: session.execution.succeeded, session.execution.failed"}
    },
}


class TestSpecProbe:
    async def test_confirms_routes_and_version(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=f"{BASE}/openapi.json", json=_spec(V2_PATHS))

        async with opencode_lane(httpx_mock) as driver:
            probe = await driver.probe_spec()

        assert probe.available is True
        assert probe.version == "2.0.3"
        assert probe.missing_routes == ()
        assert probe.warnings == ()

    async def test_tolerates_colon_path_parameters_and_flags_missing_routes(
        self, httpx_mock: HTTPXMock
    ):
        paths = dict(V2_PATHS)
        del paths["/api/session/{id}/permission/{requestID}/reply"]
        # An older spelling of one live route must still count as served.
        paths["/api/session/:id/prompt"] = paths.pop("/api/session/{id}/prompt")
        httpx_mock.add_response(url=f"{BASE}/openapi.json", json=_spec(paths))

        async with opencode_lane(httpx_mock) as driver:
            probe = await driver.probe_spec()

        assert probe.missing_routes == ("/api/session/{}/permission/{}/reply",)
        assert probe.warnings == ()

    async def test_warns_when_the_spec_speaks_legacy_vocabulary(self, httpx_mock: HTTPXMock):
        paths = dict(V2_PATHS)
        paths["/api/event"] = {"get": {"description": "frames: session.idle, prompt_async"}}
        httpx_mock.add_response(url=f"{BASE}/openapi.json", json=_spec(paths))

        async with opencode_lane(httpx_mock) as driver:
            probe = await driver.probe_spec()

        assert probe.missing_routes == ()
        assert any("session.idle" in warning for warning in probe.warnings)

    async def test_follows_the_openapi_json_link_behind_an_html_viewer(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=f"{BASE}/openapi.json", status_code=404)
        httpx_mock.add_response(
            url=f"{BASE}/doc",
            headers={"content-type": "text/html"},
            html='<script src="/doc/openapi.json"></script>',
        )
        httpx_mock.add_response(url=f"{BASE}/doc/openapi.json", json=_spec(V2_PATHS))

        async with opencode_lane(httpx_mock) as driver:
            probe = await driver.probe_spec()

        assert probe.available is True
        assert probe.version == "2.0.3"

    async def test_degrades_gracefully_when_no_spec_is_readable(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=f"{BASE}/openapi.json", status_code=404)
        httpx_mock.add_response(url=f"{BASE}/doc", status_code=404)

        async with opencode_lane(httpx_mock) as driver:
            probe = await driver.probe_spec()

        assert probe.available is False
        assert any("/openapi.json" in warning for warning in probe.warnings)


class TestProtocolFidelity:
    async def test_the_real_client_drives_the_frozen_adapter_end_to_end(
        self, httpx_mock: HTTPXMock
    ):
        register_event_stream(
            httpx_mock, CONNECTED, step_streamed("ses_1", "on it"), succeeded("ses_1")
        )
        register_create(httpx_mock)
        register_model(httpx_mock, "ses_1")
        register_prompt(httpx_mock, "ses_1", "audit the BYOK profiles")
        register_transcript(httpx_mock, "ses_1")
        httpx_mock.add_response(
            url=f"{BASE}/api/session/ses_1/interrupt", method="POST", json={"interrupted": False}
        )

        async with opencode_lane(httpx_mock) as client:
            adapter = OpenCodeAdapter(client)
            session_id = await adapter.start_session("audit the BYOK profiles")
            events = await adapter.events(session_id)
            await adapter.abort(session_id)

        assert session_id == "ses_1"
        types = [event["type"] for event in events]
        assert "session.step.streamed" in types
        assert "transcript.reconciled" in types
        assert (
            httpx_mock.get_request(url=f"{BASE}/api/session/ses_1/interrupt", method="POST")
            is not None
        )
