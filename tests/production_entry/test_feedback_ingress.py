"""The R40-01 / #337 production-entry trace — the wired feedback ingress.

The service-level suite (``tests/test_review_feedback.py``) proves the
rules and the parser; the #332 traces
(``tests/production_entry/test_review_feedback.py``) drive the
``execute_run_command`` dispatch DIRECTLY — the honest seam #337 exists
to close: nothing in PRODUCTION reached the handler. These traces prove
the wired path a customer drives, with NO direct handle/evaluate call
anywhere in the positive arms:

- **FI-1 (the positive trace)** — a token-authenticated GitLab MR-note
  payload with ``/fix`` posted to the REAL ASGI ingress
  (``forge.main.create_app`` mounting ``forge.gateway.router``) → the
  ADR-0017 §1 transaction (inbox row + scheduled step, acknowledged 202
  only after the commit) → the wake-up is LOST (worker died after the
  inbox commit — the queue is the accelerator, Postgres owns the work) →
  the INSTALLED step-runtime loop (``run_step_worker``, the same function
  the worker process gathers) resumes over a FRESH engine and claims the
  due step through the claim → lease → fence protocol → the REAL
  ``execute_run_command`` classifies and stages the correction → the
  human approves through the REAL ``/approve-revision`` router → the
  INSTALLED periodic reconciler (``run_reconciler``, the exact loop
  ``worker/app.main`` gathers — the ``evaluate_review_corrections`` pass
  #337 registers) re-drives the correction. The dispatched
  ``FORGE_PLAN`` carries the reviewer's text; the request, the reply
  journal and the revision decision all key on the ONE logical note
  identity; the MR stays a Draft.
- **FI-2 (dedup)** — folded into FI-1: a manual redelivery (NEW delivery
  uuid, SAME logical triple) and an exact replay (SAME uuid) both answer
  ``deduplicated`` — ONE inbox row, ONE request, ONE decision identity,
  at most ONE authorized correction start.
- **FI-3 (typed refusals before any activation)** — an unauthorized
  reviewer and a foreign-repository MR (the same numeric MR/note ids
  under another project — no cross-subject resolution) are refused/ignored
  typed with ZERO new pipelines, and the ingress answers 2xx throughout
  (the GitLab hook auto-disable protection: a 4xx spike would back the
  whole project's hook off for 24h). A malformed verb is the typed
  ingress refusal — no row at all.
- **FI-4 (deletion vs transient)** — the fake native has no
  ``/discussions`` route: the auxiliary surface degrades (typed skip,
  logged) and the request STILL records — a transient provider failure is
  never misread as a confirmed deletion (the confirmed/refused and the
  5xx-retried arms are pinned at the service level, where the surface is
  faked).
- **FI-5 (the mutation arms)** — with ONLY the parser registration
  reverted (the capability flag off) the trace's first step fails: no
  inbox row is ever written; with ONLY the reconciler registration
  neutralized (the correction pass patched out of the installed loop) the
  trace's dispatch step fails: the approved correction never re-drives.

This module is env-clean once (the module-scoped scrub).
"""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from forge.adaptive.revisions import REQUEST_REFUSED_UNAUTHORIZED, review_feedback_requests_of
from forge.config import ForgeConfig, Settings
from forge.database import reset_engine
from forge.durable import EventInbox, FlowStatus, StepRun
from forge.main import create_app
from forge.runs.reconciler import run_reconciler
from forge.worker.steps import run_step_worker

from .conftest import (
    GL_PROJECT_ID,
    gl_settings,
    make_gitlab_service,
)
from .test_review_feedback import (
    FIX_NOTE,
    _approve_via_router,
    _publish_first_candidate,
    _seed_active_revision_one,
    get_run,
)

pytestmark = pytest.mark.production_entry

#: The webhook shared secret these traces authenticate with.
PE_WEBHOOK_SECRET = "pe-feedback-whsec"  # noqa: S105 — fixture value

FOREIGN_PROJECT_ID = 90999  # same numeric MR/note ids, ANOTHER project


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


class _LostWakeQueue:
    """The Redis TaskQueue's dedup semantics (SET-NX) with the submit
    swallowed — production's posture where the queue is only a wake-up
    accelerator and the wake-up is LOST: the worker that should have been
    woken died between the inbox commit and the claim. Postgres owns the
    work; a restarted worker's poll finds the due step."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    async def is_duplicate(self, fingerprint: str, ttl: int = 300) -> bool:
        if fingerprint in self._seen:
            return True
        self._seen.add(fingerprint)
        return False

    async def submit(self, task) -> None:  # the lost wake-up
        return None


def _ingress_settings(gitlab_native) -> Settings:
    return gl_settings(
        GITLAB_URL=gitlab_native.base_url,
        GITLAB_TOKEN="pe-gitlab-native-token",  # noqa: S106 — fixture value
        GITLAB_WEBHOOK_SECRET=SecretStr(PE_WEBHOOK_SECRET),
        DATABASE_URL="sqlite+aiosqlite:///:memory:",  # the app's own throwaway
        REDIS_URL=None,  # never touch a developer Redis from a trace
        FORGE_CAPTURE_DIR=None,  # never write capture files from a trace
    )


def _mr_note_payload(
    *,
    note_id: int,
    body: str,
    mr_iid: int,
    project_id: int = GL_PROJECT_ID,
    author: str = "alice",
) -> dict:
    """A GitLab Note Hook on an MR, the shape GitLab POSTs."""
    return {
        "object_kind": "note",
        "event_type": "note",
        "user": {"id": 11, "name": "Alice Approver", "username": author, "email": ""},
        "project": {
            "id": project_id,
            "name": "forge-pe",
            "path_with_namespace": "acme/forge-pe",
            "web_url": f"https://gitlab.test/acme/forge-pe-{project_id}",
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


async def _post_note(
    client: AsyncClient,
    *,
    note_id: int,
    body: str,
    mr_iid: int,
    project_id: int = GL_PROJECT_ID,
    author: str = "alice",
    delivery: str | None = None,
    token: str = PE_WEBHOOK_SECRET,
):
    headers = {"X-Gitlab-Token": token, "X-Gitlab-Event": "Note Hook"}
    if delivery is not None:
        headers["X-Gitlab-Event-UUID"] = delivery
    return await client.post(
        "/webhook",
        json=_mr_note_payload(
            note_id=note_id, body=body, mr_iid=mr_iid, project_id=project_id, author=author
        ),
        headers=headers,
    )


async def _await_truth(predicate, *, timeout: float = 30.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def _resume_step_worker(
    pe_db, settings, until, *, timeout: float = 30.0, expect: bool = True
) -> None:
    """The INSTALLED step-runtime loop over a FRESH engine — the restarted
    worker (``run_step_worker`` is the exact function ``worker/app.main``
    gathers; the claim → lease → fence protocol executes the persisted
    step through the REAL ``execute_run_command``)."""
    shutdown = asyncio.Event()
    loop = asyncio.create_task(
        run_step_worker(
            pe_db.worker_factory(),
            settings,
            ForgeConfig(),
            "pe-feedback-resumed",
            shutdown,
            None,
            poll_interval=0.05,
        )
    )
    try:
        reached = await _await_truth(until, timeout=timeout)
        assert reached is expect, "the restarted worker's outcome drifted from the expectation"
    finally:
        shutdown.set()
        await asyncio.wait_for(loop, timeout=10)


async def _run_installed_reconciler(
    service,
    until,
    *,
    timeout: float = 30.0,
    expect: bool = True,
    spy: list | None = None,
) -> None:
    """The INSTALLED periodic reconciler — the exact ``run_reconciler`` loop
    the worker gathers, never a manual pass call. *spy* records whether the
    correction pass actually ran inside the loop."""
    shutdown = asyncio.Event()
    if spy is not None:
        original = service.evaluate_review_corrections

        async def _spied() -> None:
            spy.append(True)
            await original()

        service.evaluate_review_corrections = _spied  # type: ignore[method-assign]
    loop = asyncio.create_task(
        run_reconciler(service, interval_seconds=0.2, shutdown_event=shutdown)
    )
    try:
        reached = await _await_truth(until, timeout=timeout)
        assert reached is expect, f"installed reconciler outcome {reached} (expected {expect})"
    finally:
        shutdown.set()
        await asyncio.wait_for(loop, timeout=10)


async def _inbox_and_steps(pe_db) -> tuple[list[EventInbox], list[StepRun]]:
    """The FEEDBACK inbox rows and steps only — the landed lane run's own
    StepRun legs (its /go dispatch) are not feedback deliveries."""
    from sqlalchemy import select

    factory = pe_db.worker_factory()
    async with factory() as session:
        inbox = list((await session.execute(select(EventInbox))).scalars().all())
        steps = list((await session.execute(select(StepRun))).scalars().all())
    feedback_inbox = [
        row for row in inbox if (row.payload or {}).get("command") == "review_feedback"
    ]
    feedback_steps = [row for row in steps if row.step_name == "review_feedback"]
    return feedback_inbox, feedback_steps


async def _landed(pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path):
    """Issue → /go → lane → native publish: a Draft-MR run in waiting_ci."""
    run_id, branch, mr_iid = await _publish_first_candidate(
        pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path
    )
    await _seed_active_revision_one(pe_db.worker_factory(), run_id)
    return run_id, branch, mr_iid


def _dispatch_variables(gitlab_native, index: int) -> dict:
    entry = gitlab_native.dispatches()[index]
    return {variable["key"]: variable["value"] for variable in entry["variables"]}


async def _request_of(pe_db, run_id: str, note_id: int):
    run = await get_run(pe_db.worker_factory(), run_id)
    return review_feedback_requests_of(run.evidence or {}).get(str(note_id))


class TestFI1TheWiredIngressTrace:
    async def test_note_to_ingress_to_worker_restart_to_reconciler(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("FORGE_REVIEW_FEEDBACK_ENABLED", "1")
        run_id, branch, mr_iid = await _landed(
            pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path
        )
        factory = pe_db.worker_factory()
        candidate_sha = (await get_run(factory, run_id)).candidate_shas[-1]
        dispatches_before = len(gitlab_native.dispatches())
        mr_notes_before = len(gitlab_native.state()["mr_notes"])
        settings = _ingress_settings(gitlab_native)

        application = create_app(settings=settings)
        async with application.router.lifespan_context(application):
            application.state.session_factory = pe_db.worker_factory()
            application.state.task_queue = _LostWakeQueue()
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                # --- the reviewer's note, through the REAL ASGI ingress --
                first = await _post_note(
                    client, note_id=8301, body=FIX_NOTE, mr_iid=mr_iid, delivery="fi1-a"
                )
                assert first.status_code == 202
                assert first.json() == {
                    "status": "accepted",
                    "event": "note",
                    "queued": True,
                    "run_command": True,
                }
                # --- FI-2: a manual redelivery (NEW uuid, SAME logical
                #     triple) and an exact replay (SAME uuid) --------------
                redelivery = await _post_note(
                    client, note_id=8301, body=FIX_NOTE, mr_iid=mr_iid, delivery="fi1-b"
                )
                assert redelivery.status_code == 202
                assert redelivery.json()["deduplicated"] is True
                replay = await _post_note(
                    client, note_id=8301, body=FIX_NOTE, mr_iid=mr_iid, delivery="fi1-a"
                )
                assert replay.status_code == 202
                assert replay.json()["deduplicated"] is True

            # --- kill-after-inbox-commit: the wake was lost, nothing has
            #     processed the note yet ----------------------------------
            inbox, steps = await _inbox_and_steps(pe_db)
            assert len(inbox) == 1 and len(steps) == 1  # ONE logical delivery
            assert inbox[0].event_type == "run_command"
            assert inbox[0].payload["command"] == "review_feedback"
            assert inbox[0].payload["project_id"] == GL_PROJECT_ID
            assert inbox[0].payload["note_id"] == "8301"
            assert inbox[0].payload["delivery_uuid"] == "fi1-a"
            assert steps[0].source_event_id == inbox[0].source_event_id
            assert steps[0].step_name == "review_feedback"
            assert steps[0].status == "scheduled"
            assert await _request_of(pe_db, run_id, 8301) is None
            assert len(gitlab_native.state()["mr_notes"]) == mr_notes_before
        reset_engine()

        # --- the RESTARTED worker: the installed step loop over a fresh
        #     engine claims the due step and classifies -------------------
        async def _staged() -> bool:
            request = await _request_of(pe_db, run_id, 8301)
            return request is not None and request.status == "staged"

        await _resume_step_worker(pe_db, settings, _staged)

        request = await _request_of(pe_db, run_id, 8301)
        assert request is not None
        assert request.classification == "in-scope_correction"
        assert request.status == "staged"
        assert request.head_sha == candidate_sha  # bound to the CURRENT head
        assert request.actor == "alice"
        assert request.discussion_id == "d-8301"
        decision_id = request.decision_id
        replies = [
            entry["body"]
            for entry in gitlab_native.state()["mr_notes"]
            if "/approve-revision" in entry["body"]
        ]
        assert len(replies) == 1  # ONE reply per note id, despite 3 deliveries
        # FI-4: the transient degradation — the fake has no /discussions
        # route (a 404 marks the surface down, logged), and the request
        # STILL records: a transient provider failure is never misread as
        # a confirmed deleted_discussion refusal.
        assert any("discussions" in path for path in gitlab_native.unknown_paths())
        assert request.status != "deleted_discussion"

        # --- the human approval through the REAL /approve-revision ingress
        approved = await _approve_via_router(
            gitlab_client, pe_db.worker_factory(), run_id, decision_id
        )
        assert approved["status"] == "applied", approved

        # --- the INSTALLED periodic reconciler re-drives the correction --
        service = make_gitlab_service(pe_db.worker_factory(), gitlab_client)
        spy: list[bool] = []

        async def _dispatched() -> bool:
            return len(gitlab_native.dispatches()) == dispatches_before + 1

        await _run_installed_reconciler(service, _dispatched, spy=spy)
        assert spy, "the correction pass never ran inside the installed reconciler"

        corrected = _dispatch_variables(gitlab_native, -1)
        assert "validate_email" in corrected["FORGE_PLAN"]
        assert "Reviewer correction" in corrected["FORGE_PLAN"]
        assert candidate_sha[:12] in corrected["FORGE_PLAN"]
        run = await get_run(pe_db.worker_factory(), run_id)
        assert review_feedback_requests_of(run.evidence or {})["8301"].status == "dispatched"
        assert run.status == FlowStatus.WAITING_HARNESS.value
        assert gitlab_native.dispatches()[-1]["ref"] == branch

        # --- FI-2's tail: one decision identity, AT MOST ONE authorized
        #     correction start — a second reconciler window adds nothing --
        async def _never() -> bool:
            return False

        await _run_installed_reconciler(service, _never, timeout=0.7, expect=False)
        assert len(gitlab_native.dispatches()) == dispatches_before + 1
        assert (
            review_feedback_requests_of(
                (await get_run(pe_db.worker_factory(), run_id)).evidence or {}
            )["8301"].decision_id
            == decision_id
        )

        # the bot NEVER merges and NEVER resolves: the MR stays a Draft
        mr = gitlab_native.merge_requests()[mr_iid]
        assert mr["state"] == "opened" and mr["title"].startswith("Draft:")


class TestFI3TypedRefusals:
    async def test_an_unauthorized_reviewer_is_refused_typed_before_any_activation(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("FORGE_REVIEW_FEEDBACK_ENABLED", "1")
        run_id, _, mr_iid = await _landed(
            pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path
        )
        dispatches_before = len(gitlab_native.dispatches())
        pipelines_before = len(gitlab_native.pipelines())
        settings = _ingress_settings(gitlab_native)

        application = create_app(settings=settings)
        async with application.router.lifespan_context(application):
            application.state.session_factory = pe_db.worker_factory()
            application.state.task_queue = _LostWakeQueue()
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await _post_note(
                    client,
                    note_id=8401,
                    body=FIX_NOTE,
                    mr_iid=mr_iid,
                    author="bob",  # NOT in FORGE_APPROVERS
                    delivery="fi3-bob",
                )
                # the ingress still answers 2xx — a 4xx spike would count
                # toward GitLab's hook auto-disable (4 → 24h backoff)
                assert response.status_code == 202
                assert response.json()["run_command"] is True
        reset_engine()

        async def _refused() -> bool:
            request = await _request_of(pe_db, run_id, 8401)
            return request is not None and request.status == REQUEST_REFUSED_UNAUTHORIZED

        await _resume_step_worker(pe_db, settings, _refused)

        # typed refusal BEFORE any revision activation or native job start
        request = await _request_of(pe_db, run_id, 8401)
        assert request is not None
        assert request.status == REQUEST_REFUSED_UNAUTHORIZED
        assert request.decision_id == ""  # nothing was ever staged
        assert len(gitlab_native.dispatches()) == dispatches_before
        assert len(gitlab_native.pipelines()) == pipelines_before
        refusal_notes = [
            entry["body"]
            for entry in gitlab_native.state()["mr_notes"]
            if "ignored" in entry["body"] or "not in" in entry["body"]
        ]
        assert refusal_notes  # the operator-visible refusal was posted

        # --- acceptance #2 at the WIRED level: an authorized /ask earns a
        #     clarification record + response and NOTHING else — no coding
        #     attempt, no coder reservation (no dispatch, no pipeline) ----
        application = create_app(settings=settings)
        async with application.router.lifespan_context(application):
            application.state.session_factory = pe_db.worker_factory()
            application.state.task_queue = _LostWakeQueue()
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await _post_note(
                    client,
                    note_id=8402,
                    body="/ask why is the retry idempotent?",
                    mr_iid=mr_iid,
                    delivery="fi3-ask",
                )
                assert response.status_code == 202
                assert response.json()["run_command"] is True
        reset_engine()

        async def _clarification_open() -> bool:
            ask = await _request_of(pe_db, run_id, 8402)
            return ask is not None and ask.status == "clarification_open"

        await _resume_step_worker(pe_db, settings, _clarification_open)
        ask = await _request_of(pe_db, run_id, 8402)
        assert ask is not None
        assert ask.classification == "clarification"
        assert ask.decision_id == ""  # never staged, never a correction
        assert len(gitlab_native.dispatches()) == dispatches_before  # no lane start
        assert len(gitlab_native.pipelines()) == pipelines_before  # no native job
        assert any(
            "No code change is dispatched" in entry["body"]
            for entry in gitlab_native.state()["mr_notes"]
        )

    async def test_a_foreign_repository_mr_never_resolves_cross_subject(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path
    ):
        """The same numeric MR/note ids under ANOTHER project: no run in
        that project exists, and the forge-lab run must NOT be resolved —
        the connection/repo axis is part of every lookup, never inferred
        from the ids alone."""
        monkeypatch.setenv("FORGE_REVIEW_FEEDBACK_ENABLED", "1")
        run_id, _, mr_iid = await _landed(
            pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path
        )
        dispatches_before = len(gitlab_native.dispatches())
        mr_notes_before = len(gitlab_native.state()["mr_notes"])
        settings = _ingress_settings(gitlab_native)

        application = create_app(settings=settings)
        async with application.router.lifespan_context(application):
            application.state.session_factory = pe_db.worker_factory()
            application.state.task_queue = _LostWakeQueue()
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await _post_note(
                    client,
                    note_id=8301,  # the SAME note id the FI-1 trace uses
                    body=FIX_NOTE,
                    mr_iid=mr_iid,  # the SAME MR iid
                    project_id=FOREIGN_PROJECT_ID,
                    delivery="fi3-foreign",
                )
                assert response.status_code == 202
                assert response.json()["run_command"] is True
        reset_engine()

        async def _step_settled() -> bool:
            _, steps = await _inbox_and_steps(pe_db)
            return bool(steps) and steps[0].status != "scheduled"

        await _resume_step_worker(pe_db, settings, _step_settled)
        inbox, steps = await _inbox_and_steps(pe_db)
        assert [row.project_id for row in inbox] == [FOREIGN_PROJECT_ID]

        # NO cross-subject resolution: the forge-lab run gained nothing
        assert await _request_of(pe_db, run_id, 8301) is None
        assert len(gitlab_native.dispatches()) == dispatches_before
        assert len(gitlab_native.state()["mr_notes"]) == mr_notes_before

    async def test_a_malformed_command_is_refused_typed_at_the_ingress(
        self, pe_db, gitlab_native, monkeypatch
    ):
        """``/fix`` with no description: the typed 2xx refusal, no inbox
        row, no step — nothing can activate (and the hook never sees a 4xx)."""
        monkeypatch.setenv("FORGE_REVIEW_FEEDBACK_ENABLED", "1")
        dispatches_before = len(gitlab_native.dispatches())
        settings = _ingress_settings(gitlab_native)

        application = create_app(settings=settings)
        async with application.router.lifespan_context(application):
            application.state.session_factory = pe_db.worker_factory()
            application.state.task_queue = _LostWakeQueue()
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await _post_note(
                    client, note_id=8501, body="/fix", mr_iid=7, delivery="fi3-malformed"
                )
                assert response.status_code == 202  # hook auto-disable protection
                assert response.json() == {
                    "status": "accepted",
                    "event": "note",
                    "feedback": "refused",
                    "refusal_reason": "malformed_feedback_command",
                }
        reset_engine()

        inbox, steps = await _inbox_and_steps(pe_db)
        assert inbox == [] and steps == []
        assert len(gitlab_native.dispatches()) == dispatches_before


class TestFI5TheRegistrationMutations:
    async def test_without_the_parser_registration_nothing_is_ever_ingested(
        self, pe_db, gitlab_native, monkeypatch
    ):
        """Revert ONLY the parser registration (the capability flag off —
        the adaptive rollout's zero-routing default) and the FI-1 trace's
        FIRST step fails: the webhook never produces a run command."""
        monkeypatch.delenv("FORGE_REVIEW_FEEDBACK_ENABLED", raising=False)
        settings = _ingress_settings(gitlab_native)

        application = create_app(settings=settings)
        async with application.router.lifespan_context(application):
            application.state.session_factory = pe_db.worker_factory()
            application.state.task_queue = _LostWakeQueue()
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await _post_note(
                    client, note_id=8601, body=FIX_NOTE, mr_iid=7, delivery="fi5-noparse"
                )
                assert response.status_code == 202
                assert "run_command" not in response.json()  # the legacy path
        reset_engine()

        inbox, steps = await _inbox_and_steps(pe_db)
        assert inbox == [] and steps == []

        # ...and the resumed worker still has nothing to process
        async def _never() -> bool:
            return False

        await _resume_step_worker(pe_db, settings, _never, timeout=0.6, expect=False)
        assert await _inbox_and_steps(pe_db) == ([], [])

    async def test_without_the_reconciler_registration_the_correction_never_re_drives(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path
    ):
        """Revert ONLY the reconciler registration (the correction pass
        neutralized inside the installed loop) and the FI-1 trace's
        dispatch step fails: the approved correction sits staged forever."""
        monkeypatch.setenv("FORGE_REVIEW_FEEDBACK_ENABLED", "1")
        run_id, _, mr_iid = await _landed(
            pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path
        )
        dispatches_before = len(gitlab_native.dispatches())
        settings = _ingress_settings(gitlab_native)

        application = create_app(settings=settings)
        async with application.router.lifespan_context(application):
            application.state.session_factory = pe_db.worker_factory()
            application.state.task_queue = _LostWakeQueue()
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await _post_note(
                    client, note_id=8701, body=FIX_NOTE, mr_iid=mr_iid, delivery="fi5-noreconcile"
                )
                assert response.status_code == 202
        reset_engine()

        async def _staged() -> bool:
            request = await _request_of(pe_db, run_id, 8701)
            return request is not None and request.status == "staged"

        await _resume_step_worker(pe_db, settings, _staged)
        request = await _request_of(pe_db, run_id, 8701)
        assert request is not None
        approved = await _approve_via_router(
            gitlab_client, pe_db.worker_factory(), run_id, request.decision_id
        )
        assert approved["status"] == "applied", approved

        service = make_gitlab_service(pe_db.worker_factory(), gitlab_client)
        service.evaluate_review_corrections = AsyncMock()  # the reverted pass

        async def _never() -> bool:
            return False

        await _run_installed_reconciler(service, _never, timeout=1.5, expect=False)
        assert len(gitlab_native.dispatches()) == dispatches_before  # nothing re-drove
        after = await _request_of(pe_db, run_id, 8701)
        assert after is not None and after.status == "staged"  # still parked
