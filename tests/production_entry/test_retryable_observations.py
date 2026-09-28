"""The R42-01 / #374 production-entry trace — a transient feedback
observation stays retryable after durable acceptance.

The service-level rules live in ``tests/test_retryable_observations.py``;
these traces prove the fix at the level a customer drives — the REAL
ASGI webhook ingress (``forge.main.create_app`` mounting
``forge.gateway.router``), the ADR-0017 §1 transaction (inbox + StepRun,
202 only after the commit), the INSTALLED step-runtime worker
(``run_step_worker``, the same function the worker process gathers) and
the REAL GitLab client whose every read travels REAL HTTP — through a
fault-injecting proxy in front of the fake native server, so the 503s
arrive over the wire exactly as an outage would:

- **RO-1 (the P01 trace)** — the webhook is accepted (202, inbox+step
  committed) while GitLab is transiently down on BOTH head-reading
  paths (the branch-head read AND the MR-document fallback, both 503)
  → the worker's handler defers the observation (the typed retryable
  exception) → the step records a RECOVERABLE RETRY — it NEVER
  succeeds-with-no-outcome: ``status=scheduled, attempt=1, error`` on
  the row, no request, ``provider_observation.retry`` +
  ``feedback.outcome=pending`` journaled on the run. A SECOND identical
  native event during the in-flight observation deduplicates at the
  inbox — ONE logical correction pending.
- **RO-2 (the recovery)** — the fault is removed and a NEW worker (a
  fresh ``run_step_worker`` loop) picks up the SAME step: ONE outcome
  (the request staged, bound to the head), ONE operator reply, and NO
  new user comment — the retried step is the same row
  (``source_event_id`` unchanged), not a re-delivery.
- **RO-3 (the exact replay)** — after the outcome, the EXACT delivery
  UUID replays: the ingress answers ``deduplicated``, the step row is
  untouched (``deadline_at``/``finished_at`` byte-identical — no
  deadline reset), no round is ever minted, the request never moves.
- **RO-4 (the reply is independently recoverable)** — the reply POST
  503s AFTER the staging outcome is durable: the step retries, the
  re-entry re-attempts ONLY the reply (no second staging, no second
  admission), and the step then completes.
- **RO-5 (the mutation arm)** — with ONLY the defer reverted to the
  plain return (the shipped symbol patched at test time, the #344
  discipline — no source file edited), the SAME trace records the
  review's P01 verbatim: ``step=succeeded, requests=0, handler_calls=1``
  — the obligation gone. The RO-1 assertions are the detector that
  fails on that regression.

This module is env-clean once (the module-scoped scrub).
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urllib_request
from urllib.error import HTTPError
from urllib.parse import unquote, urlparse

import pytest
from httpx import ASGITransport, AsyncClient

from forge.config import ForgeConfig
from forge.database import reset_engine
from forge.durable import Outbox, StepRun
from forge.main import create_app
from forge.runs.service import (
    FEEDBACK_OUTCOME_EVENT,
    PROVIDER_OBSERVATION_RETRY_EVENT,
    RunService,
)
from forge.worker.steps import run_step_worker

from .conftest import GL_PROJECT_ID, gl_settings
from .test_feedback_ingress import (
    PE_WEBHOOK_SECRET,
    _LostWakeQueue,
    _inbox_and_steps,
    _ingress_settings,
    _landed,
    _request_of,
)
from .test_review_feedback import FIX_NOTE, get_run

pytestmark = pytest.mark.production_entry


@pytest.fixture(autouse=True, scope="module")
def _env_clean_once():
    """Scrub the provider/forge environment ONCE for the whole module."""
    prefixes = ("FORGE_", "GITLAB_", "GITHUB_")
    saved = {key: value for key, value in os.environ.items() if key.startswith(prefixes)}
    for key in saved:
        del os.environ[key]
    try:
        yield
    finally:
        os.environ.update(saved)


# ----------------------------------------------------------------------
# The fault-injecting proxy — real HTTP in, real HTTP out, 503s on the
# two head-reading GET surfaces (and, separately, the MR-note POST)
# while armed.
# ----------------------------------------------------------------------


class _FaultProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:  # silence
        pass

    def _segments(self) -> list[str]:
        return [unquote(part) for part in urlparse(self.path).path.split("/") if part]

    def _project_tail(self) -> list[str] | None:
        segments = self._segments()
        if (
            len(segments) >= 5
            and segments[:2] == ["api", "v4"]
            and segments[2] == "projects"
            and segments[3] == str(self.server.project_id)
        ):
            return segments[4:]
        return None

    def _fault_reads(self) -> bool:
        if not self.server.fault_reads:
            return False
        tail = self._project_tail()
        if tail is None:
            return False
        return tail[:2] == ["repository", "branches"] or (
            tail[:1] == ["merge_requests"] and len(tail) == 2
        )

    def _fault_notes(self) -> bool:
        if not self.server.fault_notes:
            return False
        tail = self._project_tail()
        if tail is None or self.command != "POST":
            return False
        return len(tail) == 3 and tail[2] == "notes"

    def _send(self, status: int, payload: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 — http.server spelling
        if self._fault_reads():
            self._send(503, json.dumps({"message": "fault injected: reads unavailable"}).encode())
            return
        self._forward()

    def do_POST(self) -> None:  # noqa: N802 — http.server spelling
        if self._fault_notes():
            self._send(503, json.dumps({"message": "fault injected: notes unavailable"}).encode())
            return
        self._forward()

    def _forward(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length) if length else None
        req = urllib_request.Request(
            self.server.upstream + self.path, data=data, method=self.command
        )
        for header in ("PRIVATE-TOKEN", "Content-Type"):
            if self.headers.get(header):
                req.add_header(header, self.headers[header])
        try:
            with urllib_request.urlopen(req, timeout=60) as response:
                self._send(response.status, response.read(), response.headers.get_content_type())
        except HTTPError as exc:
            self._send(exc.code, exc.read(), "application/json")


class FaultProxy:
    """The transparent front door with two injectable faults."""

    def __init__(self, upstream: str, project_id: int) -> None:
        self.fault_reads = False
        self.fault_notes = False
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _FaultProxyHandler)
        self._server.upstream = upstream.rstrip("/")
        self._server.project_id = project_id
        self._server.fault_reads = False
        self._server.fault_notes = False
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def arm_reads(self) -> None:
        self.fault_reads = True
        self._server.fault_reads = True

    def disarm_reads(self) -> None:
        self.fault_reads = False
        self._server.fault_reads = False

    def arm_notes(self) -> None:
        self.fault_notes = True
        self._server.fault_notes = True

    def disarm_notes(self) -> None:
        self.fault_notes = False
        self._server.fault_notes = False

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture()
def fault_proxy(gitlab_native):
    proxy = FaultProxy(gitlab_native.base_url, GL_PROJECT_ID)
    try:
        yield proxy
    finally:
        proxy.close()


def _worker_settings(proxy: FaultProxy):
    """The worker's provider view — THROUGH the fault proxy."""
    return gl_settings(
        GITLAB_URL=proxy.base_url,
        GITLAB_TOKEN="pe-gitlab-native-token",  # noqa: S106 — fixture value
        REDIS_URL=None,
        FORGE_CAPTURE_DIR=None,
    )


async def _await_truth(predicate, *, timeout: float = 90.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def _run_worker(pe_db, settings, until, *, owner: str, timeout: float = 90.0) -> None:
    """The INSTALLED step-runtime loop over a FRESH engine (the restarted
    worker — the exact function the worker process gathers)."""
    shutdown = asyncio.Event()
    loop = asyncio.create_task(
        run_step_worker(
            pe_db.worker_factory(),
            settings,
            ForgeConfig(),
            owner,
            shutdown,
            None,
            poll_interval=0.05,
        )
    )
    try:
        assert await _await_truth(until, timeout=timeout), "the worker outcome drifted"
    finally:
        shutdown.set()
        await asyncio.wait_for(loop, timeout=15)


async def _feedback_step(pe_db) -> StepRun | None:
    _, steps = await _inbox_and_steps(pe_db)
    return steps[0] if steps else None


async def _post_note(
    client: AsyncClient,
    *,
    note_id: int,
    body: str,
    mr_iid: int,
    delivery: str,
    author: str = "alice",
):
    headers = {"X-Gitlab-Token": PE_WEBHOOK_SECRET, "X-Gitlab-Event": "Note Hook"}
    headers["X-Gitlab-Event-UUID"] = delivery
    payload = {
        "object_kind": "note",
        "event_type": "note",
        "user": {"id": 11, "name": "Alice Approver", "username": author, "email": ""},
        "project": {
            "id": GL_PROJECT_ID,
            "name": "forge-pe",
            "path_with_namespace": "acme/forge-pe",
            "web_url": "https://gitlab.test/acme/forge-pe",
        },
        "object_attributes": {
            "id": note_id,
            "note": body,
            "noteable_type": "MergeRequest",
            "noteable_id": 100,
            "author_id": 11,
            "discussion_id": f"d-{note_id}",
        },
        "merge_request": {
            "id": 100,
            "iid": mr_iid,
            "title": "Draft: the candidate",
            "source_branch": "forge/factory-1",
            "target_branch": "main",
            "state": "opened",
        },
    }
    return await client.post("/webhook", json=payload, headers=headers)


async def _outbox_events(pe_db, run_id: str) -> list[tuple[str, dict]]:
    from sqlalchemy import select

    factory = pe_db.worker_factory()
    async with factory() as session:
        rows = (
            await session.execute(
                select(Outbox.event_type, Outbox.payload)
                .where(Outbox.flow_run_id == run_id)
                .order_by(Outbox.id.asc())
            )
        ).all()
    return [(str(kind), dict(payload)) for kind, payload in rows]


NOTE_ID = 8801
DELIVERY = "ro-1-a"


class TestRO1TheTransientObservationStaysRetryable:
    async def test_the_step_never_succeeds_without_an_outcome(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path, fault_proxy
    ):
        monkeypatch.setenv("FORGE_REVIEW_FEEDBACK_ENABLED", "1")
        run_id, branch, mr_iid = await _landed(
            pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path
        )
        mr_notes_before = len(gitlab_native.state()["mr_notes"])
        ingress = _ingress_settings(gitlab_native)
        worker = _worker_settings(fault_proxy)
        fault_proxy.arm_reads()  # BOTH head-reading paths 503

        application = create_app(settings=ingress)
        async with application.router.lifespan_context(application):
            application.state.session_factory = pe_db.worker_factory()
            application.state.task_queue = _LostWakeQueue()
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                first = await _post_note(
                    client, note_id=NOTE_ID, body=FIX_NOTE, mr_iid=mr_iid, delivery=DELIVERY
                )
                assert first.status_code == 202
                assert first.json()["run_command"] is True
                # a SECOND identical native event (new uuid, same logical
                # note) during the in-flight observation
                second = await _post_note(
                    client,
                    note_id=NOTE_ID,
                    body=FIX_NOTE,
                    mr_iid=mr_iid,
                    delivery="ro-1-dup",
                )
                assert second.status_code == 202
                assert second.json()["deduplicated"] is True
        reset_engine()

        # ONE logical command — the inbox dedup collapsed the duplicate
        inbox, steps = await _inbox_and_steps(pe_db)
        assert len(inbox) == 1 and len(steps) == 1

        # --- the faulted worker: the handler defers, the step RETRIES --
        async def _retry_recorded() -> bool:
            step = await _feedback_step(pe_db)
            return (
                step is not None
                and step.status == "scheduled"
                and step.attempt >= 1
                and bool((step.output or {}).get("error"))
            )

        await _run_worker(pe_db, worker, _retry_recorded, owner="ro-faulted")

        # THE P01 ASSERTION — the step NEVER succeeded-with-no-outcome:
        step = await _feedback_step(pe_db)
        assert step is not None
        assert step.status != "succeeded"
        assert "review observation retryable" in str((step.output or {}).get("error"))
        assert step.attempt == 1  # handler_calls: exactly one faulted pass
        assert await _request_of(pe_db, run_id, NOTE_ID) is None  # requests=0
        assert len(gitlab_native.state()["mr_notes"]) == mr_notes_before
        # the observability trail on the run
        events = await _outbox_events(pe_db, run_id)
        retries = [payload for kind, payload in events if kind == PROVIDER_OBSERVATION_RETRY_EVENT]
        assert retries and retries[0]["reason"] == "mr_head"
        assert retries[0]["observed"] is False  # the head-never-observed fact
        outcomes = [payload for kind, payload in events if kind == FEEDBACK_OUTCOME_EVENT]
        assert outcomes[-1]["outcome"] == "pending"

        # --- the fault heals; a NEW worker resolves the SAME step -------
        fault_proxy.disarm_reads()
        candidate_sha = (await get_run(pe_db.worker_factory(), run_id)).candidate_shas[-1]

        async def _staged() -> bool:
            request = await _request_of(pe_db, run_id, NOTE_ID)
            return request is not None and request.status == "staged"

        await _run_worker(pe_db, worker, _staged, owner="ro-healed")

        request = await _request_of(pe_db, run_id, NOTE_ID)
        assert request is not None and request.status == "staged"
        assert request.head_sha == candidate_sha
        replies = [
            entry["body"]
            for entry in gitlab_native.state()["mr_notes"]
            if "/approve-revision" in entry["body"]
        ]
        assert len(replies) == 1  # ONE reply — no new user comment involved
        inbox_after, steps_after = await _inbox_and_steps(pe_db)
        assert len(inbox_after) == 1 and len(steps_after) == 1  # the SAME step retried
        assert steps_after[0].source_event_id == inbox_after[0].source_event_id
        assert steps_after[0].status == "succeeded"  # success only WITH the outcome
        assert steps_after[0].attempt == 1  # the recovery was the FIRST retry

        # --- the exact replay AFTER the outcome: nothing moves ---------
        step_before = await _feedback_step(pe_db)
        assert step_before is not None
        deadline_before, finished_before = step_before.deadline_at, step_before.finished_at
        application = create_app(settings=ingress)
        async with application.router.lifespan_context(application):
            application.state.session_factory = pe_db.worker_factory()
            application.state.task_queue = _LostWakeQueue()
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                replay = await _post_note(
                    client, note_id=NOTE_ID, body=FIX_NOTE, mr_iid=mr_iid, delivery=DELIVERY
                )
                assert replay.status_code == 202
                assert replay.json()["deduplicated"] is True
        reset_engine()

        step_after = await _feedback_step(pe_db)
        assert step_after is not None
        assert step_after.deadline_at == deadline_before  # no deadline reset
        assert step_after.finished_at == finished_before
        assert await _request_of(pe_db, run_id, NOTE_ID) == request  # the outcome stands

        # no round was ever minted — a staged pre-ready correction is not
        # an admission (the round no-duplicate bar; the round arms live
        # in the service-level suite)
        from forge.durable.models import ReviewRound
        from sqlalchemy import select as sa_select

        rounds_factory = pe_db.worker_factory()
        async with rounds_factory() as session:
            rounds = (await session.execute(sa_select(ReviewRound))).scalars().all()
        assert rounds == []
        assert (
            len(
                [
                    entry
                    for entry in gitlab_native.state()["mr_notes"]
                    if "/approve-revision" in entry["body"]
                ]
            )
            == 1
        )


class TestRO4TheReplyIsIndependentlyRecoverable:
    async def test_a_503_on_the_reply_post_retries_only_the_reply(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path, fault_proxy
    ):
        monkeypatch.setenv("FORGE_REVIEW_FEEDBACK_ENABLED", "1")
        run_id, branch, mr_iid = await _landed(
            pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path
        )
        ingress = _ingress_settings(gitlab_native)
        worker = _worker_settings(fault_proxy)
        fault_proxy.arm_notes()  # the reply POST 503s; every read is fine

        application = create_app(settings=ingress)
        async with application.router.lifespan_context(application):
            application.state.session_factory = pe_db.worker_factory()
            application.state.task_queue = _LostWakeQueue()
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                accepted = await _post_note(
                    client, note_id=8802, body=FIX_NOTE, mr_iid=mr_iid, delivery="ro-4-a"
                )
                assert accepted.status_code == 202
        reset_engine()

        # the staging outcome is durable; only the reply is missing
        async def _staged() -> bool:
            request = await _request_of(pe_db, run_id, 8802)
            return request is not None and request.status == "staged"

        await _run_worker(pe_db, worker, _staged, owner="ro-4-faulted")
        request = await _request_of(pe_db, run_id, 8802)
        assert request is not None and request.status == "staged"
        decision_id = request.decision_id
        assert [
            entry["body"]
            for entry in gitlab_native.state()["mr_notes"]
            if "/approve-revision" in entry["body"]
        ] == []
        step = await _feedback_step(pe_db)
        assert step is not None and step.status != "succeeded"  # the reply owns completion

        # the reply heals — the step retry re-attempts ONLY the reply
        fault_proxy.disarm_notes()

        async def _delivered() -> bool:
            step_row = await _feedback_step(pe_db)
            return step_row is not None and step_row.status == "succeeded"

        await _run_worker(pe_db, worker, _delivered, owner="ro-4-healed")

        after = await _request_of(pe_db, run_id, 8802)
        assert after is not None and after.decision_id == decision_id  # no second staging
        replies = [
            entry["body"]
            for entry in gitlab_native.state()["mr_notes"]
            if "/approve-revision" in entry["body"]
        ]
        assert len(replies) == 1  # exactly one reply after recovery


class TestRO5ThePlainReturnMutationArm:
    async def test_reverting_only_the_defer_records_the_reviews_p01_verbatim(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path, fault_proxy
    ):
        """The #374 defect, reproduced end-to-end with ONE symbol reverted
        at test time (the #344 discipline — no source file edited): the
        handler plain-returns, the step records SUCCESS, no request
        exists — ``step=succeeded, requests=0, handler_calls=1``, the
        review's P01 verbatim. The RO-1 trace above is the detector that
        fails when this shape re-enters the source."""

        async def _plain_return(self, run_id, note_id, reason, exc) -> None:  # noqa: ARG002
            return None  # the reverted defer: log-and-return

        monkeypatch.setattr(RunService, "_defer_review_observation", _plain_return)
        monkeypatch.setenv("FORGE_REVIEW_FEEDBACK_ENABLED", "1")
        run_id, branch, mr_iid = await _landed(
            pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path
        )
        ingress = _ingress_settings(gitlab_native)
        worker = _worker_settings(fault_proxy)
        fault_proxy.arm_reads()

        application = create_app(settings=ingress)
        async with application.router.lifespan_context(application):
            application.state.session_factory = pe_db.worker_factory()
            application.state.task_queue = _LostWakeQueue()
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                accepted = await _post_note(
                    client, note_id=8803, body=FIX_NOTE, mr_iid=mr_iid, delivery="ro-5-a"
                )
                assert accepted.status_code == 202
        reset_engine()

        async def _succeeded() -> bool:
            step = await _feedback_step(pe_db)
            return step is not None and step.status == "succeeded"

        await _run_worker(pe_db, worker, _succeeded, owner="ro-5-mutated")

        # the recorded defect, verbatim
        step = await _feedback_step(pe_db)
        assert step is not None
        assert step.status == "succeeded"
        assert step.attempt == 0  # handler_calls=1, zero failures recorded
        assert await _request_of(pe_db, run_id, 8803) is None  # requests=0
        # no observability trail — the reverted defer journaled nothing
        events = await _outbox_events(pe_db, run_id)
        assert not [payload for kind, payload in events if kind == PROVIDER_OBSERVATION_RETRY_EVENT]
        assert not [payload for kind, payload in events if kind == FEEDBACK_OUTCOME_EVENT]
        # and the obligation is GONE: a healed re-delivery of the SAME
        # note deduplicates at the inbox and records nothing
        fault_proxy.disarm_reads()
        application = create_app(settings=ingress)
        async with application.router.lifespan_context(application):
            application.state.session_factory = pe_db.worker_factory()
            application.state.task_queue = _LostWakeQueue()
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                redelivery = await _post_note(
                    client, note_id=8803, body=FIX_NOTE, mr_iid=mr_iid, delivery="ro-5-b"
                )
                assert redelivery.status_code == 202
                assert redelivery.json()["deduplicated"] is True
        reset_engine()

        async def _nothing() -> bool:
            return True

        await _run_worker(pe_db, worker, _nothing, owner="ro-5-after", timeout=1.0)
        assert await _request_of(pe_db, run_id, 8803) is None
