"""The REAL OpenCode server client for the execution lane (EXE-02/EXE-07).

Implements the :class:`~forge.adaptive.adapters.OpenCodeClient` Protocol
against the live ``opencode serve`` HTTP API. The wire layer targets
**opencode v2.0.10** and is grounded in the "LIVE CORRECTION — opencode
v2.0.10" section of ``docs/research/opencode-server.md`` (verified
against a live server on 2026-09-21); that section SUPERSEDES the older
research above it in the doc, which described a server whose routes,
prompt model, event vocabulary, and auth defaults all changed before
v2.0.10 shipped. The doctrine is
``docs/research/forge-harness-hacks.md`` — this client runs NEXT TO the
runner in the execution lane, never inside the privileged API process,
it exists for BYOK customer profiles, and which (provider route,
credential mode) combinations were actually verified stays the
:class:`~forge.adaptive.adapters.DriverMatrix`'s question. This client
never guesses support and never blocks unattended: a permission request
is always answered with the configured default.

Endpoint map (LIVE CORRECTION section — every route moved under ``/api``):

===================================  =======================================
Protocol method                      Vendor route (v2.0.10, live-verified)
===================================  =======================================
``start_session(task)``              ``POST /api/session`` then
                                     ``POST /api/session/{id}/model`` then
                                     the first prompt, awaited to turn end
``prompt(session_id, text)``         ``POST /api/session/{id}/prompt``
                                     ``{"text"}`` — answers IMMEDIATELY
                                     with the user-message echo; the turn
                                     ends on
                                     ``session.execution.succeeded|failed``
                                     over ``GET /api/event``, reconciled
                                     by transcript polling when uncovered
``events(session_id)``               buffered ``GET /api/event`` frames +
                                     ``GET /api/session/{id}/message``
                                     (``{"data": [...], "cursor": ...}``)
``abort(session_id)``                ``POST /api/session/{id}/interrupt``
                                     → ``{"interrupted": bool}``
``probe_spec()``                     ``GET /openapi.json`` (``/doc`` is
                                     only an HTML viewer now)
===================================  =======================================
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

#: ``opencode serve`` default bind (research §2 — unchanged in v2.0.10).
DEFAULT_BASE_URL = "http://127.0.0.1:4096"

#: Basic-auth username when ``OPENCODE_SERVER_PASSWORD`` is set (LIVE
#: CORRECTION §Auth: UNSET, v2.0.10 generates a random password and prints
#: it to stdout — the spawner always pins one).
DEFAULT_SERVER_USERNAME = "opencode"

#: Model selection defaults. Setting the model explicitly after create is
#: MANDATORY on v2.0.10 (LIVE CORRECTION: a fresh session's default model
#: may point at a dead provider, surfacing as session.execution.failed);
#: the object shape is ``{"providerID", "id"}`` — NOT ``modelID``.
DEFAULT_PROVIDER_ID = "anthropic"
DEFAULT_MODEL_ID = "claude-sonnet-4-5"

#: Kept for constructor/factory signature compatibility only: v2.0.10's
#: prompt body carries just ``{"text": ...}`` and no verified agent wire
#: surface exists, so this value is recorded but never sent.
DEFAULT_AGENT = "build"

#: The unattended lane refuses tools rather than approving them blind.
DEFAULT_PERMISSION_RESPONSE = "reject"

#: Generous turn budget — a v2 turn is observed over SSE (or reconciled
#: from the transcript), never through a blocking HTTP call; the lane
#: deadline still governs.
DEFAULT_PROMPT_TIMEOUT = 1800.0

#: The event vocabulary verified on v2.0.10 (LIVE CORRECTION §Event
#: vocabulary) plus names kept as plausible-but-unverified tolerance.
#: Anything outside it is logged once and parsed defensively — EventV2
#: drift is expected.
KNOWN_EVENT_TYPES: frozenset[str] = frozenset(
    {
        # -- live-verified on v2.0.10 (smoke + e2e campaigns) --
        "server.connected",
        "session.created",
        "session.execution.started",
        "session.execution.succeeded",
        "session.execution.failed",
        "session.step.started",
        "session.step.streamed",
        "session.step.ended",
        "session.reasoning.started",
        "session.reasoning.delta",
        "session.reasoning.ended",
        "session.text.started",
        "session.text.delta",
        "session.text.ended",
        "session.tool.input.started",
        "session.tool.input.ended",
        "session.tool.called",
        "session.tool.success",
        "session.tool.progress",
        "session.usage.updated",
        "session.instructions.updated",
        "session.inbox.enqueued",
        "session.inbox.delivered",
        "session.model.selected",
        "shell.created",
        "shell.exited",
        # -- plausible on v2.0.10, not live-verified --
        "server.heartbeat",
        "session.disposed",
        "session.permission.requested",
        "session.inbox.started",
        "session.inbox.message",
        "installation.updated",
        "mcp.resources.changed",
        # -- legacy v1 spelling, tolerated defensively --
        "permission.asked",
    }
)

#: What ends a ``prompt`` wait. The two execution verdicts are the
#: verified v2 turn-done signals; the v1 names stay so an older server
#: still unblocks the wait instead of hanging it.
#: The transcript ``finish`` values that mark a TERMINAL assistant
#: message (LIVE-found: intermediate tool rounds finish "tool-calls").
_TERMINAL_FINISHES: frozenset[str] = frozenset({"stop", "error"})

_TURN_DONE_EVENT_TYPES: tuple[str, ...] = (
    "session.execution.succeeded",
    "session.execution.failed",
    "session.idle",
    "session.error",
)

#: Permission-request event names. The v2 reply route is
#: ``/api/session/{id}/permission/{requestID}/reply`` (LIVE CORRECTION
#: §Routes); the event NAME was not live-verified, so the v1 spelling is
#: answered too — an unanswered permission hangs the server-side turn.
_PERMISSION_ASK_EVENT_TYPES: tuple[str, ...] = (
    "session.permission.requested",
    "permission.asked",
)

_PERMISSION_RESPONSES: tuple[str, ...] = ("once", "always", "reject")

_CONNECT_TIMEOUT = 10.0

#: Heartbeats keep the stream alive; a silent minute means the stream is
#: dead, not quiet.
_SSE_TIMEOUT = httpx.Timeout(60.0, connect=_CONNECT_TIMEOUT)

#: How long ``prompt`` waits for the subscription to become ready before
#: continuing with transcript-only reconciliation; ``server.connected``
#: is immediate.
_SUBSCRIBE_TIMEOUT = 15.0

#: Transcript polling while the event stream is uncovered (LIVE
#: CORRECTION: completion is execution.succeeded/failed over SSE, or the
#: transcript — no verified v2 status route exists to poll instead).
_RECONCILE_POLL_INTERVAL = 0.5

#: Ordinary requests (session create, model set, interrupt, permission
#: answers) stay short; the SSE stream carries its own timeout.
_FACTORY_REQUEST_TIMEOUT = 30.0

#: Routes this client needs on v2.0.10 (LIVE CORRECTION §Routes), with
#: path parameters collapsed for comparison across the spec's ``{id}``
#: and older ``:id`` spellings.
_NEEDED_ROUTE_KEYS: tuple[str, ...] = (
    "/api/event",
    "/api/session",
    "/api/session/{}/model",
    "/api/session/{}/prompt",
    "/api/session/{}/message",
    "/api/session/{}/interrupt",
    "/api/session/{}/permission/{}/reply",
)

#: Event types the SSE completion path cannot work without (verified v2).
_CRITICAL_EVENT_TYPES: tuple[str, ...] = (
    "session.execution.succeeded",
    "session.execution.failed",
)

#: Vocabulary that marks a spec as OLDER than the v2.0.10 wire layer this
#: client targets (LIVE CORRECTION — these names are gone).
_LEGACY_EVENT_TYPES: tuple[str, ...] = (
    "session.idle",
    "session.next.text.delta",
    "prompt_async",
)

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


@dataclass(frozen=True)
class SpecProbe:
    """What the served OpenAPI spec says about this client's needs.

    ``version`` is the capability token to pin against (research §10.1/
    §10.6); ``missing_routes`` and ``warnings`` are diagnostics — the
    probe never fails the lane, it makes the EventV2 defensive posture
    mechanical.
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

    Prompt strategy (v2.0.10, LIVE CORRECTION): ``prompt_async`` is GONE.
    ``POST /api/session/{id}/prompt`` ``{"text": ...}`` answers
    IMMEDIATELY with the user-message echo (``delivery: "steer"``) and
    the turn runs async. :meth:`prompt` subscribes to ``GET /api/event``
    FIRST (the global stream has no replay), posts, and waits for this
    session's ``session.execution.succeeded``/``failed``.
    ``session.execution.failed`` does NOT raise — the kept semantics: the
    failure stays observable via :meth:`events`, and the client never
    guesses success. While the stream is uncovered (it would not
    subscribe, or it dropped mid-turn) completion is reconciled by
    polling the transcript for an assistant message whose ``finish``
    appeared after a pre-prompt snapshot — the ids are captured BEFORE
    the post, so the race is closed. One caveat the caller inherits: a
    turn-done left over from a timed-out turn can satisfy the next wait;
    the timeout itself is the operator's signal to interrupt.
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
        """Land a BYOK provider key via ``PUT /api/auth/:id`` (research §6.2b).

        The ``/api`` prefix follows the v2.0.10 route move (LIVE
        CORRECTION); the auth route itself was not re-verified live. The
        value lives only in this frame and never reaches a log line; a
        key that failed to land fails the session rather than silently
        running on whatever auth the server already had.
        """
        key = self._provider_key
        if key is None:
            return
        self._provider_key = None
        response = await self._http.put(
            self._url(f"/api/auth/{self._provider_id}"),
            json={"type": "api", "key": key},
            auth=self._auth,
        )
        response.raise_for_status()

    async def start_session(self, task: str) -> str:
        """``POST /api/session`` + model + first prompt; returns the session id.

        v2.0.10 (LIVE CORRECTION): create answers ``{"data": {id, ...}}``
        and takes only ``{title?}`` — the lane checkout is pinned by
        serving the server FROM it (one directory per server context,
        research §8), so *directory* is no longer sent, only observed
        from the answer: a mismatch is logged loudly, never swallowed —
        the client does not pretend the session runs where the lane
        asked. The model is then set EXPLICITLY (mandatory), and the
        first prompt runs to turn completion before returning — the
        documented choice kept from v1: the smoke's 240 s start budget
        covers the first task turn, and ``prompt()`` stays the
        turn-waiting surface for everything after.
        """
        await self._apply_pending_provider_key()
        response = await self._http.post(
            self._url("/api/session"), json={"title": task[:80]}, auth=self._auth
        )
        response.raise_for_status()
        session = response.json()
        if isinstance(session, dict) and isinstance(session.get("data"), dict):
            session = session["data"]  # v2 wraps payloads: {"data": {...}}
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
        await self._set_session_model(session_id)
        await self.prompt(session_id, task)
        return session_id

    async def _set_session_model(self, session_id: str) -> None:
        """``POST /api/session/{id}/model`` — MANDATORY on v2.0.10.

        The shape is ``{"model": {"providerID", "id"}}`` (NOT
        ``{providerID, modelID}`` — that 400s with ``Missing key at
        ["model"]["id"]``; LIVE CORRECTION §Model selection). A failure
        raises: running on an unconfirmed default model is exactly the
        dead-provider failure mode this call exists to prevent.
        """
        response = await self._http.post(
            self._url(f"/api/session/{session_id}/model"),
            json={"model": {"providerID": self._provider_id, "id": self._model_id}},
            auth=self._auth,
        )
        response.raise_for_status()

    async def prompt(self, session_id: str, text: str) -> None:
        """Send *text* and return when the assistant turn ends (LIVE CORRECTION).

        ``POST /api/session/{id}/prompt`` ``{"text": text}`` answers
        immediately with the user-message echo; the turn runs async. This
        method subscribes to ``GET /api/event`` FIRST (no replay), posts,
        and waits for this session's
        ``session.execution.succeeded``/``failed`` — failure does NOT
        raise (kept semantics: inspect :meth:`events`). While the stream
        is uncovered — it would not subscribe, or it dropped mid-turn —
        completion is reconciled by polling the transcript for an
        assistant message that completed (``finish`` set) after the
        pre-prompt snapshot. Raises ``TimeoutError`` when no end-of-turn
        is observed within *prompt_timeout* — interrupt and inspect
        :meth:`events` then.
        """
        self._known_sessions.add(session_id)
        done = self._turn_event(session_id)
        done.clear()
        # Race-free reconciliation baseline: which assistant messages were
        # ALREADY complete before this turn existed (None = unreadable, and
        # then reconciliation cannot PROVE completion — the timeout stays
        # the operator's signal rather than a false turn-done).
        baseline = await self._completed_assistant_ids(session_id)
        await self._ensure_subscription()
        response = await self._http.post(
            self._url(f"/api/session/{session_id}/prompt"),
            json={"text": text},
            auth=self._auth,
        )
        response.raise_for_status()
        await self._wait_for_turn(session_id, done, baseline)

    def _turn_event(self, session_id: str) -> asyncio.Event:
        event = self._turn_done.get(session_id)
        if event is None:
            event = asyncio.Event()
            self._turn_done[session_id] = event
        return event

    async def _wait_for_turn(
        self, session_id: str, done: asyncio.Event, baseline: set[str] | None
    ) -> None:
        deadline = time.monotonic() + self._prompt_timeout
        reconciling = False
        while not done.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"opencode session {session_id} did not reach "
                    f"session.execution.succeeded/failed within "
                    f"{self._prompt_timeout}s"
                )
            reader = self._sse_task
            if not reconciling and reader is not None and not reader.done():
                # The reader task goes into the wait set DIRECTLY —
                # asyncio.wait never cancels its arguments, so there is
                # no cancellable intermediary between this waiter and
                # the stream (the LIVE-found reader-kill mechanism; see
                # _ensure_subscription's docstring).
                try:
                    await asyncio.wait(
                        {done_wait := asyncio.ensure_future(done.wait()), reader},
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    done_wait.cancel()
                continue
            # The stream is uncovered and /api/event has no replay: reconcile
            # completion from the transcript instead — the transcript, never
            # the delta stream, is the authority (research §10.3).
            reconciling = True
            if await self._transcript_reports_completion(session_id, baseline):
                return
            await asyncio.sleep(min(_RECONCILE_POLL_INTERVAL, remaining))

    async def _transcript_reports_completion(
        self, session_id: str, baseline: set[str] | None
    ) -> bool:
        if baseline is None:
            return False  # cannot prove THIS turn completed; never guess
        completed = await self._completed_assistant_ids(session_id)
        if completed is None:
            return False
        return bool(completed - baseline)

    async def _completed_assistant_ids(self, session_id: str) -> set[str] | None:
        """Ids of TERMINALLY finished assistant messages.

        LIVE-found (the e2e campaign): intermediate tool-call rounds
        carry ``finish: "tool-calls"`` — treating ANY finish as
        completion reconciled the turn as done after the model's first
        tool round while the agent loop was still running. Only
        ``stop`` (LIVE CORRECTION: the success value) and ``error``
        (failures AND interruptions — there is no dedicated interrupted
        value) are terminal here.
        """
        messages = await self._fetch_transcript(session_id)
        if messages is None:
            return None
        completed: set[str] = set()
        for message in messages:
            if not isinstance(message, dict):
                continue
            if message.get("type") == "assistant" and message.get("finish") in _TERMINAL_FINISHES:
                message_id = message.get("id")
                if isinstance(message_id, str):
                    completed.add(message_id)
        return completed

    async def events(self, session_id: str) -> list[dict[str, Any]]:
        """The event stream for *session_id* so far, as a list of dicts.

        The maintained ``GET /api/event`` subscription supplies buffered
        frames filtered to this session (``data.sessionID``; server-wide
        frames such as ``server.connected`` and heartbeats are not
        session events). Because the stream has no replay, every read of
        a known session is reconciled with the transcript poll
        ``GET /api/session/{id}/message`` and the authoritative messages
        are appended as one client-synthesized
        ``{"type": "transcript.reconciled", ...}`` event — streamed steps
        may be missing after a gap; the transcript never is. Returned
        events are copies, never aliases into the buffer.
        """
        buffered = [
            {**event, "data": dict(event["data"])}
            for event in self._event_buffer.get(session_id, [])
        ]
        if session_id not in self._known_sessions:
            return buffered
        if self._events_may_be_missing:
            logger.debug("reconciling session %s after an /api/event gap", session_id)
        messages = await self._fetch_transcript(session_id)
        if messages is None:
            return buffered
        return [
            *buffered,
            {
                "id": None,
                "type": "transcript.reconciled",
                "data": {"sessionID": session_id, "messages": messages},
            },
        ]

    async def _fetch_transcript(self, session_id: str) -> list[Any] | None:
        try:
            response = await self._http.get(
                self._url(f"/api/session/{session_id}/message"),
                auth=self._auth,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            logger.warning("transcript reconciliation for session %s failed", session_id)
            return None
        if isinstance(payload, dict):
            # v2 paginates via cursor; the first page is what reconciliation
            # needs (LIVE CORRECTION: {"data": [...], "cursor": {...}}).
            data = payload.get("data")
            return data if isinstance(data, list) else None
        return payload if isinstance(payload, list) else None

    async def abort(self, session_id: str) -> None:
        """``POST /api/session/{id}/interrupt`` (LIVE CORRECTION §Abort).

        The answer is ``{"interrupted": bool}`` — true only when a turn
        was in flight; a turn already settled is a no-op, not an error.
        The Protocol's ``-> None`` leaves no return surface, so the
        result is recorded honestly as a client-synthesized
        ``interrupt.result`` event (visible via :meth:`events`). A
        concurrent :meth:`prompt` wait still ends on the server's own
        execution verdict or transcript reconciliation — the interrupted
        turn's assistant message completes with ``finish: "error"`` and
        empty content.
        """
        response = await self._http.post(
            self._url(f"/api/session/{session_id}/interrupt"), auth=self._auth
        )
        response.raise_for_status()
        interrupted: bool | None = None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            observed = payload.get("interrupted")
            if isinstance(observed, bool):
                interrupted = observed
        if interrupted is None:
            logger.warning(
                "interrupt for session %s answered without a parseable "
                '"interrupted" bool — recorded as unknown',
                session_id,
            )
        self._event_buffer.setdefault(session_id, []).append(
            {
                "id": None,
                "type": "interrupt.result",
                "data": {"sessionID": session_id, "interrupted": interrupted},
            }
        )

    # -- the /api/event subscription (LIVE CORRECTION §Routes) ---------------

    async def _ensure_subscription(self) -> bool:
        """Have a live ``GET /api/event`` subscription before prompting.

        LIVE-found (2026-09-21, the e2e campaign): awaiting the reader
        through an observer wrapper and CANCELLING that wrapper raced a
        CancellationError into the reader itself — the stream silently
        died after the first frame and every turn limped home on
        transcript reconciliation (which then fired early on
        ``finish: "tool-calls"`` rounds; see
        :meth:`_completed_assistant_ids`). The reader task is therefore
        never awaited through a cancellable intermediary: readiness is a
        bounded wait on the event, and reader death is observed by
        polling ``task.done()`` where it matters.
        """
        if self._sse_task is None or self._sse_task.done():
            self._sse_ready.clear()
            self._sse_task = asyncio.create_task(self._read_event_stream())
        try:
            await asyncio.wait_for(self._sse_ready.wait(), timeout=_SUBSCRIBE_TIMEOUT)
        except TimeoutError:
            pass
        return self._sse_ready.is_set()

    async def _read_event_stream(self) -> None:
        # One subscription serves every session: /api/event is the instance bus.
        try:
            async with self._http.stream(
                "GET",
                self._url("/api/event"),
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
            logger.warning("opencode /api/event subscription dropped", exc_info=True)
        finally:
            # /api/event has no replay: any exit, clean or not, means later
            # reads must reconcile against the transcript.
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
        """Normalize one frame to ``{id, type, data}``.

        Defensive by necessity: v2.0.10 sends flat frames with the
        payload in ``data`` (LIVE CORRECTION — no ``properties`` wrapper);
        older builds wrapped the payload in ``properties``, put the
        fields next to ``type`` directly, or nested the whole event under
        ``payload`` (handled by the caller). Unknown shapes pass through
        as-is rather than crashing.
        """
        event_type = event.get("type")
        if not isinstance(event_type, str):
            return None
        data = event.get("data")
        if not isinstance(data, dict):
            data = event.get("properties")
        if not isinstance(data, dict):
            data = {k: v for k, v in event.items() if k not in ("id", "type")}
        return {"id": event.get("id"), "type": event_type, "data": data}

    async def _ingest(self, event: dict[str, Any]) -> None:
        event_type = event["type"]
        data = event["data"]
        if event_type not in KNOWN_EVENT_TYPES and event_type not in self._warned_event_types:
            self._warned_event_types.add(event_type)
            logger.warning(
                "opencode event type %r is outside this client's known vocabulary "
                "(EventV2 drift?) — parsed defensively and passed through",
                event_type,
            )
        session_id = data.get("sessionID")
        if not isinstance(session_id, str):
            return
        self._event_buffer.setdefault(session_id, []).append(event)
        if event_type in _TURN_DONE_EVENT_TYPES:
            done = self._turn_done.get(session_id)
            if done is not None:
                done.set()
        if event_type in _PERMISSION_ASK_EVENT_TYPES and session_id in self._known_sessions:
            request_id = data.get("requestID")
            if not isinstance(request_id, str) or not request_id:
                request_id = data.get("id")  # the v1 spelling of the same field
            if isinstance(request_id, str) and request_id:
                await self._answer_permission(session_id, request_id)

    async def _answer_permission(self, session_id: str, request_id: str) -> None:
        """Answer a permission request — never leave it hanging.

        v2 route ``POST /api/session/{id}/permission/{requestID}/reply``
        (LIVE CORRECTION §Routes); the reply BODY was not live-verified,
        so the documented answer ``{"response": ...}`` is sent and a
        failure is logged loudly (fail-soft): the server-side prompt may
        hang, and the operator must see why. Only sessions this client
        started are answered; another client's permission is not ours to
        grant.
        """
        try:
            response = await self._http.post(
                self._url(f"/api/session/{session_id}/permission/{request_id}/reply"),
                json={"response": self._permission_response},
                auth=self._auth,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            logger.warning(
                "could not answer permission request %s for session %s with %r — "
                "the server-side prompt may hang",
                request_id,
                session_id,
                self._permission_response,
            )

    # -- the optional /openapi.json version guard (LIVE CORRECTION §Spec) ----

    async def probe_spec(self) -> SpecProbe:
        """Read the served OpenAPI spec and report drift, cheaply.

        v2.0.10 serves the JSON spec at ``GET /openapi.json`` (LIVE
        CORRECTION); ``/doc`` is only an HTML viewer now and stays as the
        fallback surface. At most three GETs; never raises — an
        unreadable spec degrades to a warning and the defensive parsing
        posture stays in force.
        """
        spec = await self._fetch_openapi_spec()
        if spec is None:
            return SpecProbe(
                available=False,
                version="",
                missing_routes=(),
                warnings=(
                    "no OpenAPI spec was readable at /openapi.json (or /doc) — "
                    "route and event vocabulary unconfirmed; parsing stays defensive",
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
        warnings: list[str] = []
        unconfirmed = [name for name in _CRITICAL_EVENT_TYPES if name not in text]
        if unconfirmed:
            warnings.append(
                "the spec never mentions "
                + ", ".join(unconfirmed)
                + " — EventV2 drift is possible; event parsing stays defensive"
            )
        legacy = [name for name in _LEGACY_EVENT_TYPES if name in text]
        if legacy:
            warnings.append(
                "the spec still speaks "
                + ", ".join(legacy)
                + " — that vocabulary is OLDER than the v2.0.10 wire layer "
                "this client targets"
            )
        info = spec.get("info")
        version = info.get("version") if isinstance(info, dict) else ""
        return SpecProbe(
            available=True,
            version=version if isinstance(version, str) else "",
            missing_routes=missing,
            warnings=tuple(warnings),
        )

    async def _fetch_openapi_spec(self) -> dict[str, Any] | None:
        # /openapi.json is the verified v2 spec surface; /doc (inline JSON,
        # or an HTML viewer one link away) stays as the fallback.
        for path in ("/openapi.json", "/doc"):
            try:
                response = await self._http.get(self._url(path), auth=self._auth)
                response.raise_for_status()
            except httpx.HTTPError:
                continue
            spec = _spec_from_text(response.text)
            if spec is not None:
                return spec
            link = _SPEC_LINK.search(response.text)
            if link is None:
                continue
            # The viewer page names the JSON spec one hop away (research §2).
            target = urljoin(f"{self._base_url}/", link.group(1))
            try:
                follow = await self._http.get(target, auth=self._auth)
                follow.raise_for_status()
            except httpx.HTTPError:
                return None
            return _spec_from_text(follow.text)
        return None


def opencode_client_from_env(
    provider_key: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> OpenCodeDriverClient:
    """Build the lane client from the documented environment.

    *provider_key* is the BYOK provider credential supplied TO the factory
    (in-memory only; landed via ``PUT /api/auth/:id`` on first use —
    research §6.2b). Environment variables:

    ================================  =====================================
    Variable                           Meaning (default)
    ================================  =====================================
    ``OPENCODE_SERVER_URL``            server origin (``http://127.0.0.1:4096``)
    ``OPENCODE_SERVER_USERNAME``       Basic-auth user (``opencode``, §6.1)
    ``OPENCODE_SERVER_PASSWORD``       Basic-auth password — ALWAYS pin it:
                                       unset, v2.0.10 generates a RANDOM one
    ``OPENCODE_PROVIDER_ID``           model providerID (``anthropic``)
    ``OPENCODE_MODEL_ID``              model id (``claude-sonnet-4-5``)
    ``OPENCODE_AGENT``                 recorded, no verified v2 surface (``build``)
    ``OPENCODE_SESSION_DIRECTORY``     directory the session must OBSERVE
    ``OPENCODE_PERMISSION_RESPONSE``   unattended default (``reject``)
    ``OPENCODE_PROMPT_TIMEOUT``        turn budget seconds (``1800``)
    ================================  =====================================

    Malformed values raise rather than degrade — the lane fails closed.
    """
    if isinstance(provider_key, dict):
        # LIVE-found 2026-09-21: an env dict passed positionally lands in
        # provider_key, the factory silently reads os.environ, and every
        # request dials the DEFAULT port 4096. Fail closed with the fix.
        raise TypeError(
            "opencode_client_from_env: an env mapping goes in the `env=` "
            "keyword — a dict in provider_key would be silently ignored "
            "while the factory reads the ambient environment"
        )
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
