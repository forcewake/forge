"""The REAL OpenCode server client for the execution lane (EXE-02/EXE-07).

Implements the :class:`~forge.adaptive.adapters.OpenCodeClient` Protocol
against the live ``opencode serve`` HTTP API: sessions, prompts, the
``/event`` SSE stream, abort, and the permission question/answer flow.
Ground truth is ``docs/research/opencode-server.md`` (cited per method);
the doctrine is ``docs/research/forge-harness-hacks.md`` — this client
runs NEXT TO the runner in the execution lane, never inside the
privileged API process, it exists for BYOK customer profiles, and which
(provider route, credential mode) combinations were actually verified
stays the :class:`~forge.adaptive.adapters.DriverMatrix`'s question.
This client never guesses support and never blocks unattended: a
``permission.asked`` is always answered with the configured default.

Endpoint map (research doc sections in parentheses):

===================================  ========================================
Protocol method                      Vendor route
===================================  ========================================
``start_session(task)``              ``POST /session`` then the first prompt
``prompt(session_id, text)``         ``POST /session/:id/prompt_async`` +
                                     ``session.idle`` over ``GET /event``;
                                     blocking ``POST /session/:id/message``
                                     as the documented fallback
``events(session_id)``               buffered ``GET /event`` frames +
                                     ``GET /session/:id/message?limit=``
``abort(session_id)``                ``POST /session/:id/abort``
===================================  ========================================
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx

__all__ = [
    "KNOWN_EVENT_TYPES",
    "OpenCodeDriverClient",
    "SpecProbe",
    "opencode_client_from_env",
]

logger = logging.getLogger(__name__)

#: ``opencode serve`` default bind (research §2).
DEFAULT_BASE_URL = "http://127.0.0.1:4096"

#: Basic-auth username when ``OPENCODE_SERVER_PASSWORD`` is set (research §6.1).
DEFAULT_SERVER_USERNAME = "opencode"

#: Model selection defaults (research §4.2 examples); override via env/factory.
DEFAULT_PROVIDER_ID = "anthropic"
DEFAULT_MODEL_ID = "claude-sonnet-4-5"

#: ``build`` edits files; ``plan`` is the read-only agent (research §4.2).
DEFAULT_AGENT = "build"

#: The unattended lane refuses tools rather than approving them blind.
DEFAULT_PERMISSION_RESPONSE = "reject"

#: Generous turn budget — far above the reported ~5-min ``HeadersTimeoutError``
#: on long blocking prompts (research §8); the lane deadline still governs.
DEFAULT_PROMPT_TIMEOUT = 1800.0

#: The event vocabulary observed in the repo/tests (research §4.3, §5). Anything
#: outside it is logged once and parsed defensively — EventV2 drift is expected.
KNOWN_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "installation.updated",
        "message.updated",
        "permission.asked",
        "server.connected",
        "server.heartbeat",
        "session.created",
        "session.disposed",
        "session.error",
        "session.idle",
        "session.next.text.delta",
        "session.next.text.ended",
        "session.next.text.started",
        "sync",
    }
)

_PERMISSION_RESPONSES: tuple[str, ...] = ("once", "always", "reject")

_CONNECT_TIMEOUT = 10.0

#: Heartbeats arrive every 10 s (research §5); a silent minute means the
#: stream is dead, not quiet.
_SSE_TIMEOUT = httpx.Timeout(60.0, connect=_CONNECT_TIMEOUT)

#: How long ``prompt`` waits for the subscription to become ready before
#: falling back to the blocking route; ``server.connected`` is immediate.
_SUBSCRIBE_TIMEOUT = 15.0

#: Completion polling while the event stream is uncovered (research §4.3).
_STATUS_POLL_INTERVAL = 0.5

#: Mirrors the transcript read in the research curl walkthrough (§9.1).
_TRANSCRIPT_LIMIT = 100

#: Ordinary requests (session create, abort, permission answers) stay short;
#: the two long-lived calls carry their own per-request timeouts.
_FACTORY_REQUEST_TIMEOUT = 30.0

#: Routes this client needs, with path parameters collapsed for comparison
#: across the spec's ``{id}`` and the docs' ``:id`` spellings.
_NEEDED_ROUTE_KEYS: tuple[str, ...] = (
    "/event",
    "/session",
    "/session/{}/abort",
    "/session/{}/message",
    "/session/{}/permissions/{}",
    "/session/{}/prompt_async",
)

#: Event types the async-prompt path cannot work without.
_CRITICAL_EVENT_TYPES: tuple[str, ...] = ("session.idle", "permission.asked")

_PATH_PARAMETER = re.compile(r"\{[^}]+\}|:[A-Za-z0-9_]+")
_SPEC_LINK = re.compile(r"""["']([^"']*openapi[^"']*\.json)["']""", re.IGNORECASE)


def _route_key(path: str) -> str:
    """Collapse path-parameter spellings (``{id}``, ``:id``, ``{sessionID}``)."""
    return _PATH_PARAMETER.sub("{}", path)


def _spec_from_text(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if isinstance(parsed, dict) and ("paths" in parsed or "openapi" in parsed):
        return parsed
    return None


async def _observe_task(task: asyncio.Task[None]) -> None:
    """Let a waiter notice a finished reader without inheriting its fate."""
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


@dataclass(frozen=True)
class SpecProbe:
    """What the served OpenAPI spec (``/doc``) says about this client's needs.

    ``version`` is the capability token to pin against (research §10.1/§10.6);
    ``missing_routes`` and ``warnings`` are diagnostics — the probe never
    fails the lane, it makes the EventV2 defensive posture mechanical.
    """

    available: bool
    version: str
    missing_routes: tuple[str, ...]
    warnings: tuple[str, ...]


class OpenCodeDriverClient:
    """The OpenCodeClient Protocol against a real ``opencode serve`` (EXE-07).

    The HTTP client is INJECTED (tests fake it; the lane builds it via
    :func:`opencode_client_from_env`) and stays the injector's to close —
    :meth:`aclose` only stops the SSE subscription task.

    Prompt strategy: ``prompt_async`` + event-driven completion
    (research §4.2/§10.2), because the blocking ``POST /session/:id/message``
    holds the connection for the whole agent run and has a reported
    ~5-minute ``HeadersTimeoutError`` failure mode (research §8). The
    blocking route remains the documented fallback — used when
    ``prompt_async`` is absent (404) or the event stream will not
    subscribe — always with a read timeout sized for a whole turn.

    End-of-turn is ``session.idle`` for the session; ``session.error``
    also ends the wait (inspect :meth:`events` for it — prompt returns,
    it does not guess success). If the event stream drops mid-turn the
    global ``/event`` has NO replay (research §8), so completion is
    reconciled by polling ``GET /session/status`` and later reads are
    reconciled against the transcript poll ``GET /session/:id/message``
    (research §10.3 — the transcript, not the delta stream, is the
    authority). One caveat the caller inherits: a ``session.idle`` left
    over from a timed-out turn can satisfy the next wait; the timeout
    itself is the operator's signal to abort.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        base_url: str = DEFAULT_BASE_URL,
        provider_id: str = DEFAULT_PROVIDER_ID,
        model_id: str = DEFAULT_MODEL_ID,
        agent: str = DEFAULT_AGENT,
        directory: str | None = None,
        permission_response: str = DEFAULT_PERMISSION_RESPONSE,
        prompt_timeout: float = DEFAULT_PROMPT_TIMEOUT,
        auth: httpx.BasicAuth | None = None,
        provider_key: str | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url must be the opencode server's origin")
        if permission_response not in _PERMISSION_RESPONSES:
            raise ValueError(
                f"permission_response must be one of {_PERMISSION_RESPONSES}, "
                f"got {permission_response!r} — an unattended lane never guesses"
            )
        if prompt_timeout <= 0:
            raise ValueError("prompt_timeout must be positive")
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._provider_id = provider_id
        self._model_id = model_id
        self._agent = agent
        self._directory = directory
        self._permission_response = permission_response
        self._prompt_timeout = prompt_timeout
        self._auth = auth
        self._provider_key = provider_key
        self._known_sessions: set[str] = set()
        self._event_buffer: dict[str, list[dict[str, Any]]] = {}
        self._turn_done: dict[str, asyncio.Event] = {}
        self._warned_event_types: set[str] = set()
        self._sse_task: asyncio.Task[None] | None = None
        self._sse_ready = asyncio.Event()
        self._events_may_be_missing = False

    async def __aenter__(self) -> OpenCodeDriverClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Stop the SSE subscription; the injected HTTP client is not ours."""
        reader = self._sse_task
        if reader is not None and not reader.done():
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
        self._sse_task = None

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    async def _apply_pending_provider_key(self) -> None:
        """Land a BYOK provider key via ``PUT /auth/:id`` (research §6.2b).

        The value lives only in this frame and never reaches a log line;
        a key that failed to land fails the session rather than silently
        running on whatever auth the server already had.
        """
        key = self._provider_key
        if key is None:
            return
        self._provider_key = None
        response = await self._http.put(
            self._url(f"/auth/{self._provider_id}"),
            json={"type": "api", "key": key},
            auth=self._auth,
        )
        response.raise_for_status()

    async def start_session(self, task: str) -> str:
        """``POST /session`` then the first prompt; returns the session id.

        The session carries the working directory (``Session.directory``,
        research §4.1) — one directory per server context (§8), so the lane
        runs one server per checkout. When *directory* is configured it is
        requested on create and a mismatching answer is logged loudly,
        never swallowed: the client does not pretend the session runs
        where the lane asked.
        """
        await self._apply_pending_provider_key()
        body: dict[str, Any] = {"title": task[:80]}
        if self._directory is not None:
            body["directory"] = self._directory
        response = await self._http.post(self._url("/session"), json=body, auth=self._auth)
        response.raise_for_status()
        session = response.json()
        session_id = session["id"]
        observed = session.get("directory")
        if self._directory and observed and observed != self._directory:
            logger.warning(
                "opencode session %s runs in %s, not the requested %s — one "
                "directory per server context; serve from the lane checkout",
                session_id,
                observed,
                self._directory,
            )
        self._known_sessions.add(session_id)
        await self.prompt(session_id, task)
        return session_id

    async def prompt(self, session_id: str, text: str) -> None:
        """Send *text* and return when the assistant turn ends (research §4.2).

        Preferred path: subscribe to ``GET /event`` FIRST (§4.3 step 1),
        ``POST /session/:id/prompt_async`` (204) and wait for
        ``session.idle``/``session.error`` for this session. Fallbacks, in
        order: the blocking ``POST /session/:id/message`` (same body, whole-
        turn read timeout) when ``prompt_async`` is absent (404) or the
        event stream will not subscribe. Raises ``TimeoutError`` when no
        end-of-turn is observed within *prompt_timeout* — abort and inspect
        :meth:`events` then.
        """
        self._known_sessions.add(session_id)
        body: dict[str, Any] = {
            "model": {"providerID": self._provider_id, "modelID": self._model_id},
            "agent": self._agent,
            "parts": [{"type": "text", "text": text}],
        }
        done = self._turn_event(session_id)
        done.clear()
        if not await self._ensure_subscription():
            await self._prompt_blocking(session_id, body)
            return
        response = await self._http.post(
            self._url(f"/session/{session_id}/prompt_async"), json=body, auth=self._auth
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            await self._prompt_blocking(session_id, body)
            return
        response.raise_for_status()
        await self._wait_for_turn(session_id, done)

    async def _prompt_blocking(self, session_id: str, body: dict[str, Any]) -> None:
        response = await self._http.post(
            self._url(f"/session/{session_id}/message"),
            json=body,
            auth=self._auth,
            # The response IS the finished assistant message (research §4.2):
            # the read timeout must cover the whole agent run.
            timeout=httpx.Timeout(self._prompt_timeout, connect=_CONNECT_TIMEOUT),
        )
        response.raise_for_status()
        self._turn_event(session_id).set()

    def _turn_event(self, session_id: str) -> asyncio.Event:
        event = self._turn_done.get(session_id)
        if event is None:
            event = asyncio.Event()
            self._turn_done[session_id] = event
        return event

    async def _wait_for_turn(self, session_id: str, done: asyncio.Event) -> None:
        deadline = time.monotonic() + self._prompt_timeout
        reconciling = False
        while not done.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"opencode session {session_id} did not reach session.idle "
                    f"within {self._prompt_timeout}s"
                )
            reader = self._sse_task
            if not reconciling and reader is not None and not reader.done():
                turn_done = asyncio.ensure_future(done.wait())
                stream_lost = asyncio.ensure_future(_observe_task(reader))
                try:
                    await asyncio.wait(
                        {turn_done, stream_lost},
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    turn_done.cancel()
                    stream_lost.cancel()
                continue
            # The stream dropped mid-turn and /event has no replay (research
            # §8): reconcile completion from the session status instead.
            reconciling = True
            if await self._status_reports_idle(session_id):
                return
            await asyncio.sleep(min(_STATUS_POLL_INTERVAL, remaining))

    async def _status_reports_idle(self, session_id: str) -> bool:
        try:
            response = await self._http.get(self._url("/session/status"), auth=self._auth)
            response.raise_for_status()
            statuses = response.json()
        except (httpx.HTTPError, ValueError):
            return False
        if not isinstance(statuses, dict):
            return False
        status = statuses.get(session_id)
        if isinstance(status, str):
            return status.lower() == "idle"
        if isinstance(status, dict):
            if status.get("busy") is False:
                return True
            for key in ("status", "state"):
                value = status.get(key)
                if isinstance(value, str):
                    return value.lower() == "idle"
        return False

    async def events(self, session_id: str) -> list[dict[str, Any]]:
        """The event stream for *session_id* so far, as a list of dicts.

        The maintained ``GET /event`` subscription supplies buffered frames
        filtered to this session (server-wide frames such as
        ``server.connected`` and heartbeats are not session events). Because
        the global stream has no replay (research §8), every read of a known
        session is reconciled with the transcript poll
        ``GET /session/:id/message?limit=`` (§10.3) and the authoritative
        messages are appended as one client-synthesized
        ``{"type": "transcript.reconciled", ...}`` event — deltas may be
        missing after a gap; the transcript never is. Returned events are
        copies, never aliases into the buffer.
        """
        buffered = [
            {**event, "properties": dict(event["properties"])}
            for event in self._event_buffer.get(session_id, [])
        ]
        if session_id not in self._known_sessions:
            return buffered
        if self._events_may_be_missing:
            logger.debug("reconciling session %s after an /event gap", session_id)
        messages = await self._fetch_transcript(session_id)
        if messages is None:
            return buffered
        return [
            *buffered,
            {
                "id": None,
                "type": "transcript.reconciled",
                "properties": {"sessionID": session_id, "messages": messages},
            },
        ]

    async def _fetch_transcript(self, session_id: str) -> list[Any] | None:
        try:
            response = await self._http.get(
                self._url(f"/session/{session_id}/message"),
                params={"limit": _TRANSCRIPT_LIMIT},
                auth=self._auth,
            )
            response.raise_for_status()
            messages = response.json()
        except httpx.HTTPError:
            logger.warning("transcript reconciliation for session %s failed", session_id)
            return None
        return messages if isinstance(messages, list) else None

    async def abort(self, session_id: str) -> None:
        """``POST /session/:id/abort`` (research §4.4).

        The vendor follows an abort with ``session.idle``, so a concurrent
        :meth:`prompt` wait unblocks on its own; still wait for that idle
        before reusing the session.
        """
        response = await self._http.post(self._url(f"/session/{session_id}/abort"), auth=self._auth)
        response.raise_for_status()

    # -- the /event subscription (research §4.3, §5) ------------------------

    async def _ensure_subscription(self) -> bool:
        """Have a live ``GET /event`` subscription before prompting (§4.3)."""
        if self._sse_task is None or self._sse_task.done():
            self._sse_ready.clear()
            self._sse_task = asyncio.create_task(self._read_event_stream())
        reader = self._sse_task
        ready = asyncio.ensure_future(self._sse_ready.wait())
        failed = asyncio.ensure_future(_observe_task(reader))
        try:
            await asyncio.wait(
                {ready, failed}, timeout=_SUBSCRIBE_TIMEOUT, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            ready.cancel()
            failed.cancel()
        return self._sse_ready.is_set()

    async def _read_event_stream(self) -> None:
        # One subscription serves every session: /event is the project bus.
        try:
            async with self._http.stream(
                "GET",
                self._url("/event"),
                headers={"accept": "text/event-stream"},
                auth=self._auth,
                timeout=_SSE_TIMEOUT,
            ) as response:
                response.raise_for_status()
                self._sse_ready.set()
                data: list[str] = []
                async for line in response.aiter_lines():
                    if not line:
                        if data:
                            await self._handle_sse_message("\n".join(data))
                            data = []
                        continue
                    if line.startswith("data:"):
                        data.append(line[5:].strip())
                    # event:/id: lines carry nothing switchable — the vendor
                    # always emits event: message (research §5); only data: is
                    # meaningful, and comment lines are ignored.
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("opencode /event subscription dropped", exc_info=True)
        finally:
            # /event has no replay (research §8): any exit, clean or not,
            # means later reads must reconcile against the transcript.
            self._events_may_be_missing = True

    async def _handle_sse_message(self, data_text: str) -> None:
        try:
            frame = json.loads(data_text)
        except ValueError:
            return  # one malformed frame never kills the subscription
        if not isinstance(frame, dict):
            return
        nested = frame.get("payload")
        await self._ingest(self._normalize_event(nested if isinstance(nested, dict) else frame))

    @staticmethod
    def _normalize_event(event: dict[str, Any]) -> dict[str, Any] | None:
        """Normalize one frame to ``{id, type, properties}``.

        Defensive by necessity (research §5): current builds send
        ``{type, properties}``, older EventV1 frames put the fields next to
        ``type`` directly, and some versions nest the whole event under
        ``payload``. Unknown shapes pass through as-is rather than crashing.
        """
        event_type = event.get("type")
        if not isinstance(event_type, str):
            return None
        properties = event.get("properties")
        if not isinstance(properties, dict):
            properties = {k: v for k, v in event.items() if k not in ("id", "type")}
        return {"id": event.get("id"), "type": event_type, "properties": properties}

    async def _ingest(self, event: dict[str, Any]) -> None:
        event_type = event["type"]
        properties = event["properties"]
        if event_type not in KNOWN_EVENT_TYPES and event_type not in self._warned_event_types:
            self._warned_event_types.add(event_type)
            logger.warning(
                "opencode event type %r is outside this client's known vocabulary "
                "(EventV2 drift?) — parsed defensively and passed through",
                event_type,
            )
        session_id = properties.get("sessionID")
        if not isinstance(session_id, str):
            return
        self._event_buffer.setdefault(session_id, []).append(event)
        if event_type in ("session.idle", "session.error"):
            done = self._turn_done.get(session_id)
            if done is not None:
                done.set()
        if event_type == "permission.asked" and session_id in self._known_sessions:
            permission_id = properties.get("id")
            if isinstance(permission_id, str) and permission_id:
                await self._answer_permission(session_id, permission_id)

    async def _answer_permission(self, session_id: str, permission_id: str) -> None:
        """Answer a permission question (research §7) — never leave it hanging.

        Only sessions this client started are answered; another client's
        permission is not ours to grant. A failed answer is logged loudly:
        the server-side prompt may hang, and the operator must see why.
        """
        try:
            response = await self._http.post(
                self._url(f"/session/{session_id}/permissions/{permission_id}"),
                json={"response": self._permission_response},
                auth=self._auth,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            logger.warning(
                "could not answer permission %s for session %s with %r — the "
                "server-side prompt may hang",
                permission_id,
                session_id,
                self._permission_response,
            )

    # -- the optional /doc version guard (research §10.6) -------------------

    async def probe_spec(self) -> SpecProbe:
        """Read the served OpenAPI spec and report drift, cheaply.

        At most two GETs; never raises — an unreadable spec degrades to a
        warning and the defensive parsing posture stays in force.
        """
        spec = await self._fetch_openapi_spec()
        if spec is None:
            return SpecProbe(
                available=False,
                version="",
                missing_routes=(),
                warnings=(
                    "no OpenAPI spec was readable at /doc — route and event "
                    "vocabulary unconfirmed; parsing stays defensive",
                ),
            )
        paths = spec.get("paths")
        served = (
            {_route_key(path) for path in paths if isinstance(path, str)}
            if isinstance(paths, dict)
            else set()
        )
        missing = tuple(route for route in _NEEDED_ROUTE_KEYS if route not in served)
        text = json.dumps(spec)
        unconfirmed = [name for name in _CRITICAL_EVENT_TYPES if name not in text]
        warnings: tuple[str, ...] = ()
        if unconfirmed:
            warnings = (
                "the spec never mentions "
                + ", ".join(unconfirmed)
                + " — EventV2 drift is possible; event parsing stays defensive",
            )
        info = spec.get("info")
        version = info.get("version") if isinstance(info, dict) else ""
        return SpecProbe(
            available=True,
            version=version if isinstance(version, str) else "",
            missing_routes=missing,
            warnings=warnings,
        )

    async def _fetch_openapi_spec(self) -> dict[str, Any] | None:
        try:
            response = await self._http.get(self._url("/doc"), auth=self._auth)
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        spec = _spec_from_text(response.text)
        if spec is not None:
            return spec
        link = _SPEC_LINK.search(response.text)
        if link is None:
            return None
        # /doc may serve the HTML viewer; the JSON spec is one hop away (§2).
        target = urljoin(f"{self._base_url}/", link.group(1))
        try:
            follow = await self._http.get(target, auth=self._auth)
            follow.raise_for_status()
        except httpx.HTTPError:
            return None
        return _spec_from_text(follow.text)


def opencode_client_from_env(
    provider_key: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> OpenCodeDriverClient:
    """Build the lane client from the documented environment.

    *provider_key* is the BYOK provider credential supplied TO the factory
    (in-memory only; landed via ``PUT /auth/:id`` on first use — research
    §6.2b). Environment variables:

    ================================  =====================================
    Variable                           Meaning (default)
    ================================  =====================================
    ``OPENCODE_SERVER_URL``            server origin (``http://127.0.0.1:4096``)
    ``OPENCODE_SERVER_USERNAME``       Basic-auth user (``opencode``, §6.1)
    ``OPENCODE_SERVER_PASSWORD``       Basic-auth password (unset = no auth)
    ``OPENCODE_PROVIDER_ID``           prompt body providerID (``anthropic``)
    ``OPENCODE_MODEL_ID``              prompt body modelID (``claude-sonnet-4-5``)
    ``OPENCODE_AGENT``                 prompt agent (``build``; ``plan`` is read-only)
    ``OPENCODE_SESSION_DIRECTORY``     working directory the session must run in
    ``OPENCODE_PERMISSION_RESPONSE``   unattended default (``reject``)
    ``OPENCODE_PROMPT_TIMEOUT``        turn budget seconds (``1800``)
    ================================  =====================================

    Malformed values raise rather than degrade — the lane fails closed.
    """
    source = os.environ if env is None else env
    password = source.get("OPENCODE_SERVER_PASSWORD", "")
    return OpenCodeDriverClient(
        httpx.AsyncClient(
            timeout=httpx.Timeout(_FACTORY_REQUEST_TIMEOUT, connect=_CONNECT_TIMEOUT)
        ),
        base_url=source.get("OPENCODE_SERVER_URL", DEFAULT_BASE_URL),
        provider_id=source.get("OPENCODE_PROVIDER_ID", DEFAULT_PROVIDER_ID),
        model_id=source.get("OPENCODE_MODEL_ID", DEFAULT_MODEL_ID),
        agent=source.get("OPENCODE_AGENT", DEFAULT_AGENT),
        directory=source.get("OPENCODE_SESSION_DIRECTORY") or None,
        permission_response=source.get("OPENCODE_PERMISSION_RESPONSE", DEFAULT_PERMISSION_RESPONSE),
        prompt_timeout=float(source.get("OPENCODE_PROMPT_TIMEOUT", DEFAULT_PROMPT_TIMEOUT)),
        auth=(
            httpx.BasicAuth(
                source.get("OPENCODE_SERVER_USERNAME", DEFAULT_SERVER_USERNAME), password
            )
            if password
            else None
        ),
        provider_key=provider_key,
    )
