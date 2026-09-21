"""The REAL OpenCode server client, pinned against the vendor's HTTP shapes.

Every fake here mirrors the documented surface from
``docs/research/opencode-server.md`` — exact routes, prompt bodies, the SSE
wire format (``event: message`` + ``data: {json}``, ``server.connected``
first, heartbeats, ``session.next.text.delta``, ``session.idle``) — because
contract fidelity to the VENDOR is the point. No real server is contacted;
``pytest-httpx`` stands in for one. The driver under test is the production
:class:`forge.adaptive.drivers.opencode.OpenCodeDriverClient` injected into
the frozen :class:`forge.adaptive.adapters.OpenCodeAdapter` contract.
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

# -- SSE frames exactly as the server writes them (research §5) ------------

SSE_HEADERS = {
    "content-type": "text/event-stream",
    "cache-control": "no-cache, no-transform",
    "x-accel-buffering": "no",
}

CONNECTED = {"type": "server.connected", "properties": {}}
HEARTBEAT = {"type": "server.heartbeat", "properties": {}}


def idle(session_id: str) -> dict:
    return {"type": "session.idle", "properties": {"sessionID": session_id}}


def delta(session_id: str, text: str) -> dict:
    return {
        "type": "session.next.text.delta",
        "properties": {
            "sessionID": session_id,
            "assistantMessageID": "msg_1",
            "textID": "txt_1",
            "delta": text,
        },
    }


def permission_asked(session_id: str, permission_id: str) -> dict:
    # The shape observed in the repo tests (research §7).
    return {
        "type": "permission.asked",
        "properties": {
            "id": permission_id,
            "sessionID": session_id,
            "permission": "bash",
            "patterns": ["rm -rf /tmp/probe"],
            "metadata": {},
            "always": [],
            "tool": {"messageID": "msg_1", "callID": "call_1"},
        },
    }


def sse(*frames: dict) -> bytes:
    """Serialize frames in the vendor's wire format: event is always message."""
    return b"".join(f"event: message\ndata: {json.dumps(frame)}\n\n".encode() for frame in frames)


TRANSCRIPT = [
    {
        "info": {"id": "msg_2", "role": "assistant", "metadata": {"sessionID": "ses_1"}},
        "parts": [{"id": "prt_1", "type": "text", "text": "done"}],
    }
]


def register_event_stream(httpx_mock: HTTPXMock, *frames: dict) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/event", method="GET", headers=SSE_HEADERS, content=sse(*frames)
    )


def register_prompt_async(httpx_mock: HTTPXMock, session_id: str) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/session/{session_id}/prompt_async", method="POST", status_code=204
    )


def register_transcript(httpx_mock: HTTPXMock, session_id: str) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/session/{session_id}/message?limit=100", method="GET", json=TRANSCRIPT
    )


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
    async def test_creates_the_session_then_sends_the_first_prompt_with_the_documented_body(
        self, httpx_mock: HTTPXMock
    ):
        register_event_stream(httpx_mock, CONNECTED, idle("ses_1"))
        httpx_mock.add_response(
            url=f"{BASE}/session",
            method="POST",
            json={"id": "ses_1", "directory": "/lane/checkout"},
        )
        register_prompt_async(httpx_mock, "ses_1")

        async with opencode_lane(
            httpx_mock,
            directory="/lane/checkout",
            provider_id="anthropic",
            model_id="claude-sonnet-4-5",
            agent="build",
        ) as driver:
            session_id = await driver.start_session("modernize the orders service")

        assert session_id == "ses_1"
        create = httpx_mock.get_request(url=f"{BASE}/session", method="POST")
        assert json.loads(create.content) == {
            "title": "modernize the orders service",
            "directory": "/lane/checkout",
        }
        prompt = httpx_mock.get_request(url=f"{BASE}/session/ses_1/prompt_async", method="POST")
        assert json.loads(prompt.content) == {
            "model": {"providerID": "anthropic", "modelID": "claude-sonnet-4-5"},
            "agent": "build",
            "parts": [{"type": "text", "text": "modernize the orders service"}],
        }

    async def test_a_directory_mismatch_is_surfaced_not_swallowed(
        self, httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
    ):
        # One directory per server context (research §8): the client never
        # pretends the session runs where the lane asked.
        register_event_stream(httpx_mock, CONNECTED, idle("ses_1"))
        httpx_mock.add_response(
            url=f"{BASE}/session", method="POST", json={"id": "ses_1", "directory": "/elsewhere"}
        )
        register_prompt_async(httpx_mock, "ses_1")

        async with opencode_lane(httpx_mock, directory="/lane/checkout") as driver:
            with caplog.at_level(logging.WARNING):
                await driver.start_session("task")

        assert any("/elsewhere" in record.message for record in caplog.records)


class TestPrompt:
    async def test_prompt_completes_on_session_idle(self, httpx_mock: HTTPXMock):
        register_event_stream(
            httpx_mock, CONNECTED, HEARTBEAT, delta("ses_1", "hello"), idle("ses_1")
        )
        register_prompt_async(httpx_mock, "ses_1")

        async with opencode_lane(httpx_mock) as driver:
            await driver.prompt("ses_1", "say hi")

        # The async path was used to completion — the blocking route was never hit.
        assert httpx_mock.get_requests(url=f"{BASE}/session/ses_1/message", method="POST") == []

    async def test_prompt_falls_back_to_the_blocking_route_when_prompt_async_is_absent(
        self, httpx_mock: HTTPXMock
    ):
        register_event_stream(httpx_mock, CONNECTED)
        httpx_mock.add_response(
            url=f"{BASE}/session/ses_1/prompt_async", method="POST", status_code=404
        )
        httpx_mock.add_response(
            url=f"{BASE}/session/ses_1/message",
            method="POST",
            json={"info": {"id": "msg_1", "role": "assistant"}, "parts": []},
        )

        async with opencode_lane(httpx_mock, model_id="some-model") as driver:
            await driver.prompt("ses_1", "say hi")

        blocking = httpx_mock.get_request(url=f"{BASE}/session/ses_1/message", method="POST")
        # Same documented body on the fallback route.
        assert json.loads(blocking.content) == {
            "model": {"providerID": "anthropic", "modelID": "some-model"},
            "agent": "build",
            "parts": [{"type": "text", "text": "say hi"}],
        }

    async def test_prompt_falls_back_to_blocking_when_the_event_stream_is_unavailable(
        self, httpx_mock: HTTPXMock
    ):
        httpx_mock.add_response(url=f"{BASE}/event", method="GET", status_code=500)
        httpx_mock.add_response(
            url=f"{BASE}/session/ses_1/message",
            method="POST",
            json={"info": {"id": "msg_1", "role": "assistant"}, "parts": []},
        )

        async with opencode_lane(httpx_mock) as driver:
            await driver.prompt("ses_1", "say hi")

        assert httpx_mock.get_requests(url=f"{BASE}/session/ses_1/prompt_async") == []

    async def test_prompt_reconciles_completion_by_status_polling_when_the_stream_drops(
        self, httpx_mock: HTTPXMock
    ):
        # /event has NO replay (research §8): the idle frame was lost with the
        # stream, so completion is reconciled from GET /session/status.
        register_event_stream(httpx_mock, CONNECTED, delta("ses_1", "partial"))
        register_prompt_async(httpx_mock, "ses_1")

        def status(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ses_1": "idle"})

        httpx_mock.add_callback(
            status, url=f"{BASE}/session/status", method="GET", is_reusable=True
        )

        async with opencode_lane(httpx_mock) as driver:
            await driver.prompt("ses_1", "say hi")

        assert httpx_mock.get_requests(url=f"{BASE}/session/status", method="GET")

    async def test_prompt_times_out_when_the_turn_never_ends(self, httpx_mock: HTTPXMock):
        register_event_stream(httpx_mock, CONNECTED)
        register_prompt_async(httpx_mock, "ses_1")

        def status(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ses_1": "busy"})

        httpx_mock.add_callback(
            status, url=f"{BASE}/session/status", method="GET", is_reusable=True
        )

        async with opencode_lane(httpx_mock, prompt_timeout=0.15) as driver:
            with pytest.raises(TimeoutError, match="session.idle"):
                await driver.prompt("ses_1", "say hi")


class TestEvents:
    async def test_returns_the_session_filtered_buffer_with_both_event_generations_parsed(
        self, httpx_mock: HTTPXMock
    ):
        frames = (
            CONNECTED,
            HEARTBEAT,
            delta("ses_1", "hello "),
            delta("ses_2", "other session"),
            # EventV1: fields sit next to `type` directly, no properties wrapper.
            {"type": "session.next.text.delta", "sessionID": "ses_1", "delta": "world"},
            # Some versions nest the whole event under `payload` (research §5).
            {
                "payload": {"type": "session.idle", "properties": {"sessionID": "ses_1"}},
                "directory": "/lane/checkout",
                "project": "proj_1",
                "workspace": "main",
            },
        )
        register_event_stream(httpx_mock, *frames)
        register_prompt_async(httpx_mock, "ses_1")
        register_transcript(httpx_mock, "ses_1")

        async with opencode_lane(httpx_mock) as driver:
            await driver.prompt("ses_1", "say hi")
            events = await driver.events("ses_1")

        types = [event["type"] for event in events]
        # Server-wide frames and OTHER sessions never leak into this session's view.
        assert "server.connected" not in types
        assert "server.heartbeat" not in types
        assert all(
            event["properties"].get("sessionID") != "ses_2"
            for event in events
            if event["type"] != "transcript.reconciled"
        )
        deltas = [event for event in events if event["type"] == "session.next.text.delta"]
        assert deltas[0]["properties"] == {
            "sessionID": "ses_1",
            "assistantMessageID": "msg_1",
            "textID": "txt_1",
            "delta": "hello ",
        }
        assert deltas[1]["properties"] == {"sessionID": "ses_1", "delta": "world"}
        assert "session.idle" in types

    async def test_reconciles_with_the_transcript_when_events_were_missed(
        self, httpx_mock: HTTPXMock
    ):
        # Total event loss: the stream never subscribed, the prompt went out
        # over the blocking route — the transcript poll is the only survivor.
        httpx_mock.add_response(url=f"{BASE}/event", method="GET", status_code=500)
        httpx_mock.add_response(
            url=f"{BASE}/session/ses_1/message",
            method="POST",
            json={"info": {"id": "msg_1", "role": "assistant"}, "parts": []},
        )
        register_transcript(httpx_mock, "ses_1")

        async with opencode_lane(httpx_mock) as driver:
            await driver.prompt("ses_1", "say hi")
            events = await driver.events("ses_1")

        assert [event["type"] for event in events] == ["transcript.reconciled"]
        assert events[0]["properties"] == {"sessionID": "ses_1", "messages": TRANSCRIPT}
        poll = httpx_mock.get_request(url=f"{BASE}/session/ses_1/message?limit=100", method="GET")
        assert poll is not None
        assert poll.url.params["limit"] == "100"

    async def test_an_unknown_session_reads_no_network(self, httpx_mock: HTTPXMock):
        # pytest-httpx fails the test on any unmatched request: reading an
        # unknown session must be a pure buffer read.
        async with opencode_lane(httpx_mock) as driver:
            assert await driver.events("ses_unknown") == []


class TestAbort:
    async def test_abort_posts_to_the_documented_route(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=f"{BASE}/session/ses_1/abort", method="POST", json=True)

        async with opencode_lane(httpx_mock) as driver:
            await driver.abort("ses_1")

        aborted = httpx_mock.get_request(url=f"{BASE}/session/ses_1/abort", method="POST")
        assert aborted is not None
        # A session.idle follows the abort (research §4.4) — prompt unblocks.


class TestPermissions:
    async def test_permission_asked_is_answered_with_the_unattended_default(
        self, httpx_mock: HTTPXMock
    ):
        register_event_stream(
            httpx_mock, CONNECTED, permission_asked("ses_1", "perm_123"), idle("ses_1")
        )
        register_prompt_async(httpx_mock, "ses_1")
        httpx_mock.add_response(
            url=f"{BASE}/session/ses_1/permissions/perm_123", method="POST", json=True
        )

        async with opencode_lane(httpx_mock) as driver:  # default: reject
            await driver.prompt("ses_1", "run the tests")

        answer = httpx_mock.get_request(
            url=f"{BASE}/session/ses_1/permissions/perm_123", method="POST"
        )
        assert json.loads(answer.content) == {"response": "reject"}

    async def test_the_permission_default_is_configurable(self, httpx_mock: HTTPXMock):
        register_event_stream(
            httpx_mock, CONNECTED, permission_asked("ses_1", "perm_123"), idle("ses_1")
        )
        register_prompt_async(httpx_mock, "ses_1")
        httpx_mock.add_response(
            url=f"{BASE}/session/ses_1/permissions/perm_123", method="POST", json=True
        )

        async with opencode_lane(httpx_mock, permission_response="once") as driver:
            await driver.prompt("ses_1", "run the tests")

        answer = httpx_mock.get_request(
            url=f"{BASE}/session/ses_1/permissions/perm_123", method="POST"
        )
        assert json.loads(answer.content) == {"response": "once"}

    async def test_a_permission_for_a_session_this_client_does_not_own_is_left_alone(
        self, httpx_mock: HTTPXMock
    ):
        register_event_stream(
            httpx_mock, CONNECTED, permission_asked("ses_other", "perm_9"), idle("ses_1")
        )
        register_prompt_async(httpx_mock, "ses_1")

        async with opencode_lane(httpx_mock) as driver:
            await driver.prompt("ses_1", "task")

        assert httpx_mock.get_requests(url=f"{BASE}/session/ses_other/permissions/perm_9") == []

    async def test_an_unknown_permission_response_is_refused(self):
        http = httpx.AsyncClient()
        with pytest.raises(ValueError, match="permission_response"):
            OpenCodeDriverClient(http, permission_response="maybe")
        await http.aclose()


class TestAuth:
    async def test_basic_auth_credentials_travel_on_every_request_including_the_stream(
        self, httpx_mock: HTTPXMock
    ):
        register_event_stream(httpx_mock, CONNECTED, idle("ses_1"))
        httpx_mock.add_response(url=f"{BASE}/session", method="POST", json={"id": "ses_1"})
        register_prompt_async(httpx_mock, "ses_1")

        async with opencode_lane(
            httpx_mock, auth=httpx.BasicAuth("lane-user", "lane-pass")
        ) as driver:
            await driver.start_session("task")

        expected = "Basic " + base64.b64encode(b"lane-user:lane-pass").decode()
        requests = httpx_mock.get_requests()
        assert {request.method for request in requests} >= {"GET", "POST"}
        assert all(request.headers["authorization"] == expected for request in requests)

    async def test_a_byok_provider_key_lands_via_put_auth_before_the_first_prompt(
        self, httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
    ):
        httpx_mock.add_response(url=f"{BASE}/auth/anthropic", method="PUT", json=True)
        register_event_stream(httpx_mock, CONNECTED, idle("ses_1"))
        httpx_mock.add_response(url=f"{BASE}/session", method="POST", json={"id": "ses_1"})
        register_prompt_async(httpx_mock, "ses_1")

        async with opencode_lane(
            httpx_mock, provider_key="sk-ant-test-value", provider_id="anthropic"
        ) as driver:
            with caplog.at_level(logging.DEBUG):
                await driver.start_session("task")

        key_put = httpx_mock.get_request(url=f"{BASE}/auth/anthropic", method="PUT")
        assert json.loads(key_put.content) == {"type": "api", "key": "sk-ant-test-value"}
        # The key lands BEFORE the session is created, and never reaches a log.
        assert httpx_mock.get_requests()[0].url == f"{BASE}/auth/anthropic"
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
            url=f"{base}/event",
            method="GET",
            headers=SSE_HEADERS,
            content=sse(CONNECTED, idle("ses_env")),
        )
        httpx_mock.add_response(
            url=f"{base}/session",
            method="POST",
            json={"id": "ses_env", "directory": "/lane/checkout"},
        )
        httpx_mock.add_response(
            url=f"{base}/session/ses_env/prompt_async", method="POST", status_code=204
        )

        driver = opencode_client_from_env()
        try:
            session_id = await driver.start_session("task")
        finally:
            await driver.aclose()

        assert session_id == "ses_env"
        create = httpx_mock.get_request(url=f"{base}/session", method="POST")
        assert json.loads(create.content)["directory"] == "/lane/checkout"
        assert (
            create.headers["authorization"] == "Basic " + base64.b64encode(b"lane:s3cret").decode()
        )
        prompt = httpx_mock.get_request(url=f"{base}/session/ses_env/prompt_async", method="POST")
        assert json.loads(prompt.content)["model"] == {
            "providerID": "my-local",
            "modelID": "some-model",
        }
        assert json.loads(prompt.content)["agent"] == "plan"

    async def test_the_factory_defaults_to_the_documented_local_server(
        self, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
    ):
        for name in (*self.ENV, "OPENCODE_PERMISSION_RESPONSE", "OPENCODE_PROMPT_TIMEOUT"):
            monkeypatch.delenv(name, raising=False)
        register_event_stream(httpx_mock, CONNECTED, idle("ses_1"))
        httpx_mock.add_response(url=f"{BASE}/session", method="POST", json={"id": "ses_1"})
        register_prompt_async(httpx_mock, "ses_1")

        driver = opencode_client_from_env()
        try:
            await driver.start_session("task")
        finally:
            await driver.aclose()

        create = httpx_mock.get_request(url=f"{BASE}/session", method="POST")
        assert create is not None  # the default origin, no auth configured
        assert "authorization" not in create.headers

    async def test_the_factory_applies_a_supplied_provider_key_on_first_use(
        self, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
    ):
        for name in self.ENV:
            monkeypatch.delenv(name, raising=False)
        httpx_mock.add_response(url=f"{BASE}/auth/anthropic", method="PUT", json=True)
        register_event_stream(httpx_mock, CONNECTED, idle("ses_1"))
        httpx_mock.add_response(url=f"{BASE}/session", method="POST", json={"id": "ses_1"})
        register_prompt_async(httpx_mock, "ses_1")

        driver = opencode_client_from_env(provider_key="sk-factory-test")
        try:
            await driver.start_session("task")
        finally:
            await driver.aclose()

        key_put = httpx_mock.get_request(url=f"{BASE}/auth/anthropic", method="PUT")
        assert json.loads(key_put.content) == {"type": "api", "key": "sk-factory-test"}


def _spec(paths: dict[str, Any], version: str = "1.2.3") -> dict[str, Any]:
    return {"openapi": "3.1.0", "info": {"version": version}, "paths": paths}


class TestSpecProbe:
    async def test_confirms_routes_and_version(self, httpx_mock: HTTPXMock):
        paths = {
            path: {"post": {}}
            for path in (
                "/session",
                "/session/{id}/message",
                "/session/{id}/prompt_async",
                "/session/{id}/abort",
                "/session/{id}/permissions/{permissionID}",
            )
        }
        paths["/event"] = {
            "get": {
                "description": "frames: session.idle, permission.asked, session.next.text.delta"
            }
        }
        httpx_mock.add_response(url=f"{BASE}/doc", json=_spec(paths))

        async with opencode_lane(httpx_mock) as driver:
            probe = await driver.probe_spec()

        assert probe.available is True
        assert probe.version == "1.2.3"
        assert probe.missing_routes == ()
        assert probe.warnings == ()

    async def test_tolerates_colon_path_parameters_and_flags_missing_routes(
        self, httpx_mock: HTTPXMock
    ):
        httpx_mock.add_response(
            url=f"{BASE}/doc",
            json=_spec({"/session": {"post": {}}, "/session/:id/message": {"post": {}}}),
        )

        async with opencode_lane(httpx_mock) as driver:
            probe = await driver.probe_spec()

        assert set(probe.missing_routes) == {
            "/event",
            "/session/{}/abort",
            "/session/{}/permissions/{}",
            "/session/{}/prompt_async",
        }
        assert any("session.idle" in warning for warning in probe.warnings)

    async def test_follows_the_openapi_json_link_behind_an_html_viewer(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{BASE}/doc",
            headers={"content-type": "text/html"},
            html='<script src="/doc/openapi.json"></script>',
        )
        httpx_mock.add_response(
            url=f"{BASE}/doc/openapi.json", json=_spec({"/session": {"post": {}}})
        )

        async with opencode_lane(httpx_mock) as driver:
            probe = await driver.probe_spec()

        assert probe.available is True
        assert probe.version == "1.2.3"

    async def test_degrades_gracefully_when_no_spec_is_readable(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=f"{BASE}/doc", status_code=404)

        async with opencode_lane(httpx_mock) as driver:
            probe = await driver.probe_spec()

        assert probe.available is False
        assert any("/doc" in warning for warning in probe.warnings)


class TestProtocolFidelity:
    async def test_the_real_client_drives_the_frozen_adapter_end_to_end(
        self, httpx_mock: HTTPXMock
    ):
        register_event_stream(httpx_mock, CONNECTED, delta("ses_1", "on it"), idle("ses_1"))
        httpx_mock.add_response(url=f"{BASE}/session", method="POST", json={"id": "ses_1"})
        register_prompt_async(httpx_mock, "ses_1")
        register_transcript(httpx_mock, "ses_1")
        httpx_mock.add_response(url=f"{BASE}/session/ses_1/abort", method="POST", json=True)

        async with opencode_lane(httpx_mock) as client:
            adapter = OpenCodeAdapter(client)
            session_id = await adapter.start_session("audit the BYOK profiles")
            events = await adapter.events(session_id)
            await adapter.abort(session_id)

        assert session_id == "ses_1"
        types = [event["type"] for event in events]
        assert "session.next.text.delta" in types
        assert "transcript.reconciled" in types
        assert httpx_mock.get_request(url=f"{BASE}/session/ses_1/abort", method="POST")
