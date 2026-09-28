"""R42-05 (#378) — the composed fault traces: the completion criterion.

External review ``9f2c850``, item R42-05: the suite was green and the
R42 counterexamples still lived in COMPOSITIONS the component suites do
not cross — a transient observation fault COMPOSED with a real worker
DEATH at the observation seam, and a lost native start COMPOSED with the
mixed-history occupancy verdict. These are the two REQUIRED traces that
close exactly those gaps, each paired with its seeded-regression arm:

- **CF-A — the killed observation (a real process kill at the #374
  seam)** — the reviewer's ``/fix`` note is ACCEPTED through the REAL
  ASGI ingress (the 202 follows the inbox+StepRun commit — the
  AFTER-INBOX-COMMIT barrier, asserted before any worker exists); the
  worker is a REAL OS PROCESS running the INSTALLED composition
  (``run_step_worker`` + ``run_reconciler`` + ``run_step_reaper`` — the
  exact loops ``worker/app.main`` gathers); GitLab is transiently down on
  both head-reading paths through the fault proxy; the child SIGKILLs
  itself INSIDE the #374 retryable-observation seam — AFTER the
  deferral's journals committed (``provider_observation.retry`` +
  ``feedback.outcome=pending``, their OWN transactions) and BEFORE the
  typed raise reached ``fail_step`` (the BEFORE-LOCAL-COMPLETION
  barrier). The durable state at the kill is the composed window the
  in-process #374 traces cannot produce: the observability trail is
  durable, the step row still sits claimed by the dead worker, no retry
  record, no outcome. A FRESH worker process (fault healed, the dead
  claim's lease expired by the trace's clock advance) re-executes the
  SAME durable command through the REAL reaper + step loop: ONE outcome,
  ONE operator reply, ONE inbox row — durable states and NATIVE EFFECT
  COUNTS, never exception text.

- **CF-B — the lost start under mixed occupancy (a real dispatch, a real
  dropped create response, the #375 verdict)** — the whole trace rides
  the installed dispatcher: a REAL gateway subprocess takes ``/implement``
  and ``/go``; the worker child's dispatch leg acquires the execution
  lease (capacity ONE), records the native-start intent BEFORE the
  provider call, and the fake native's DROP rule (R42-05) registers the
  pipeline then closes the connection WITHOUT a reply (the
  AFTER-REMOTE-EFFECT barrier — the ledger row exists while the worker
  saw a transport error). The run parks blocked with its lease DRAINING;
  the branch listing then carries a PRIOR attempt's journaled terminal
  (historical), a terminal execution the attempt can own (correlated),
  and the lost start's own RUNNING row at the input revision — degraded
  timestamp, base SHA: AMBIGUOUS. The #375 verdict answers RUNNING (the
  ambiguous-ACTIVE guard): the lease KEEPS its slot, and at capacity one
  a second run's real ``/go`` dispatch is refused — the ACTUAL native
  start count stays ONE. When the ambiguous row resolves terminal, the
  correlated terminal releases the slot EXACTLY ONCE.

- **CF-M1 — the #374 plain-return arm** — the SAME CF-A sequence with
  ONLY the defer reverted to log-and-return (seeded into the worker
  child the way a regression would): the step records SUCCESS with no
  request outcome — the review's P01 verbatim (``step=succeeded,
  requests=0, handler_calls=1``). CF-A's succeeded-with-no-outcome
  assertions are the detector that fails under this patch.

- **CF-M2 — the #375 ambiguous-guard-disabled arm** — the SAME CF-B
  mixed listing with ONLY rule 2 (ambiguous-ACTIVE precedes any
  all-terminal verdict) disabled in-process: the correlated terminal
  wins, the verdict answers TERMINAL, the drain pass RELEASES the slot
  with the ambiguous row still running, and the next acquire
  OVERSUBSCRIBES at capacity one. CF-B's lease-kept assertions are the
  detector that fails under this patch.

The barrier map (the issue's four real boundaries):

=========================  ===========================================
boundary                   where it lands
=========================  ===========================================
after inbox commit         CF-A: the 202's committed rows asserted
                           before any worker exists
before the required        CF-A: the fault proxy is armed before the
observation                worker starts; the observation cannot
                           succeed, so the kill at the defer seam is
                           deterministic
after the remote effect    CF-B: the drop rule registers the pipeline
                           first — the ledger row is asserted present
                           while the caller saw the error
before local completion    CF-A: the SIGKILL lands between the
                           deferral's journals and ``fail_step`` —
                           asserted by the post-kill durable state
=========================  ===========================================

Fake surfaces (documented, per the acceptance): the fake native server
and the fault proxy stand in for GitLab (real HTTP, recorded state); the
deterministic stub agents stand in for the model behind
``build_default_agents``; everything else — ingress, dispatch, lease,
occupancy, step runtime, reaper, reconciler — is the shipped code over a
real database (real PostgreSQL under ``FORGE_PG_TEST_URL``). Unproven
natively: real GitLab's scheduling latency and its true ``created_at``
fidelity (the fake's listing rows are deliberately degraded — the exact
surface the R42-02 ambiguity rules exist for).

Fixture hygiene (this issue): the module escalates
``PytestUnhandledThreadExceptionWarning`` to an error FOR THIS SUITE
ONLY and asserts at teardown that no aiosqlite worker thread leaked —
the repeated focused suite ends with zero leaked DB threads.

This module is env-clean once (the module-scoped scrub).
"""

from __future__ import annotations

import os
import threading
import time
import warnings
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from forge.config import ForgeConfig
from forge.database import reset_engine
from forge.durable import Controller, FlowRun, FlowStatus
from forge.main import create_app

from .conftest import GL_PROJECT_ID
from .test_feedback_ingress import (
    _LostWakeQueue,
    _inbox_and_steps,
    _ingress_settings,
    _landed,
    _post_note,
    _request_of,
)
from .test_mutation_gates import trace_record, write_trace_record
from .test_production_matrix import (
    MX_ISSUE_IID,
    MX_ISSUE_TITLE,
    MX_WEBHOOK_SECRET,
    _expire_dead_claims,
    _run_of,
    _seed_workspace,
    _target_of,
    run_mx_worker,
    start_mx_gateway,
)
from .test_retryable_observations import FaultProxy
from .test_review_feedback import FIX_NOTE, get_run

pytestmark = pytest.mark.production_entry

#: The reviewer note identity the CF-A trace rides.
NOTE_ID = 8901
DELIVERY = "cf-a-1"

#: The capacity the CF-B trace pins (the second start must be refused).
CF_CAP = 1

#: The second issue the CF-B capacity refusal competes from.
CF_SECOND_IID = MX_ISSUE_IID + 1
CF_SECOND_TITLE = "Second widget"
CF_SECOND_DESC = "Competes for the one slot."


@pytest.fixture(autouse=True, scope="module")
def _env_clean_once():
    """Scrub the provider/forge environment ONCE for the whole module."""
    prefixes = ("FORGE_", "GITLAB_", "GITHUB_", "AZURE_", "REDIS_URL", "LITELLM_")
    saved = {key: value for key, value in os.environ.items() if key.startswith(prefixes)}
    for key in saved:
        del os.environ[key]
    try:
        yield
    finally:
        os.environ.update(saved)


@pytest.fixture(autouse=True)
def _clean_shared_state():
    """The shared engine cache and the process-shared control mailbox."""
    from forge.adaptive.command_router import reset_shared_control_service

    reset_shared_control_service()
    yield
    reset_shared_control_service()
    reset_engine()


@pytest.fixture(autouse=True)
def _record_the_trace(request: pytest.FixtureRequest):
    """Record every CF trace/arm execution (the #344 record machinery)."""
    from .conftest import TRACE_OUTCOME_KEY

    started = time.monotonic()
    yield
    marker = request.node.get_closest_marker("trace_record")
    if marker is None:
        return
    write_trace_record(
        request.node.nodeid,
        label=str(marker.kwargs.get("label", "")),
        mutation=marker.kwargs.get("mutation"),
        outcome=request.node.stash.get(TRACE_OUTCOME_KEY, "executed"),
        duration_seconds=time.monotonic() - started,
    )


@pytest.fixture(autouse=True, scope="module")
def _zero_leaked_db_threads():
    """The focused suite's leak gate (R42-05 fixture hygiene).

    Two mechanical bars, this suite ONLY (the legacy debt elsewhere is
    being cleared at its owners — see the two fixture fixes this issue
    landed):

    - ``PytestUnhandledThreadExceptionWarning`` is an ERROR for every
      test in this module — an aiosqlite worker thread dying against a
      closed loop fails the test it surfaces under, never smears a
      warning across the report;
    - at module teardown, no aiosqlite ``_connection_worker_thread``
      started during this suite is still alive — the owning fixtures
      (``pe_db`` disposes every engine it minted; the children own their
      engines in their own processes) must have joined them all.
    """
    before = {thread.name for thread in threading.enumerate()}
    with warnings.catch_warnings():
        warnings.filterwarnings("error", category=pytest.PytestUnhandledThreadExceptionWarning)
        yield
    leaked = sorted(
        thread.name
        for thread in threading.enumerate()
        if thread.name not in before and "_connection_worker_thread" in thread.name
    )
    assert not leaked, (
        f"the focused suite leaked aiosqlite worker threads: {leaked} — "
        "an engine was abandoned before its owning fixture tore it down"
    )


# ----------------------------------------------------------------------
# The fault proxy fixture (the same transparent front door the #374
# traces use — 503s on the two head-reading GET surfaces while armed)
# ----------------------------------------------------------------------


@pytest.fixture()
def fault_proxy(gitlab_native):
    proxy = FaultProxy(gitlab_native.base_url, GL_PROJECT_ID)
    try:
        yield proxy
    finally:
        proxy.close()


# ----------------------------------------------------------------------
# Shared reads
# ----------------------------------------------------------------------


async def _all_flow_runs(pe_db) -> list[FlowRun]:
    factory = pe_db.worker_factory()
    async with factory() as session:
        return list((await session.execute(select(FlowRun))).scalars().all())


async def _run_status(pe_db, run_id: str) -> tuple[str, str]:
    run = await _run_of(pe_db, run_id)
    return str(run.status), str(run.status_reason or "")


async def _outbox_events(pe_db, run_id: str) -> list[tuple[str, dict]]:
    from forge.durable import Outbox

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


def _reply_notes(gitlab_native) -> list[str]:
    return [
        entry["body"]
        for entry in gitlab_native.state()["mr_notes"]
        if "/approve-revision" in entry["body"]
    ]


async def _lease_of(pe_db, run_id: str):
    from forge.adaptive.admission import ExecutionLease

    factory = pe_db.worker_factory()
    async with factory() as session:
        return (
            (await session.execute(select(ExecutionLease).where(ExecutionLease.run_id == run_id)))
            .scalars()
            .first()
        )


async def _held_slots(pe_db, policy) -> int:
    from forge.adaptive.admission import lease_snapshot

    snapshot = await lease_snapshot(
        policy, GL_PROJECT_ID, pe_db.worker_factory(), provider="gitlab"
    )
    return int(snapshot["held"])


async def _journal_prior_start(pe_db, run_id: str, branch: str, pipeline_id: int) -> None:
    """A PRIOR attempt's journaled terminal start, BACKDATED below the
    current attempt's intent window — the durable identity the branch
    search classifies rows against, seeded the way an earlier attempt's
    journal would read (``created_at`` is the window key, so the seed
    writes it explicitly; the real dispatch's own journal stays exactly
    as the shipped code wrote it)."""
    from datetime import timedelta

    from forge.durable.models import ActionLog
    from forge.worker.steps import _utcnow

    factory = pe_db.worker_factory()
    async with factory() as session:
        controller = Controller(session)
        action_id = await controller.record_action(run_id, "harness_start", correlation_id=branch)
        await controller.complete_action(
            action_id, "succeeded", {"pipeline_id": pipeline_id, "ref": branch}
        )
        action_row = await session.get(ActionLog, action_id)
        assert action_row is not None
        action_row.created_at = _utcnow() - timedelta(seconds=120)
        await session.commit()


async def _probe_with_strengths(
    pe_db, gitlab_native, intent_key: str, *, pin_detail: bool = True
) -> SimpleNamespace:
    """The REAL probe verdict over the real listing, beside the per-row
    correlation strengths — the classification the verdict ran on, pinned
    so the trace documents WHICH rule fired. *pin_detail* cross-checks
    the verdict against the full decision table (skipped only by the
    mutation arm, whose patched verdict deliberately diverges)."""
    from forge.config import Settings
    from forge.gitlab.client import GitLabClient
    from forge.runs.service import RunService, forge_token

    from .test_production_matrix import _mx_settings_kwargs

    kwargs = _mx_settings_kwargs(gitlab_native.base_url, pe_db.url)
    gitlab = GitLabClient(base_url=kwargs["GITLAB_URL"], token=forge_token(Settings(**kwargs)))
    try:
        service = RunService(
            session_factory=pe_db.worker_factory(),
            gitlab=gitlab,
            settings=Settings(**kwargs),
            config=ForgeConfig(),
        )
        probe = service._native_occupancy_probe()
        verdict = await probe(intent_key)
        branch = intent_key.split("@", 1)[1]
        correlation = await service._occupancy_correlation(intent_key, GL_PROJECT_ID, branch)
        assert correlation is not None
        pipelines = await gitlab.list_pipelines(GL_PROJECT_ID, ref=branch)
        if pin_detail:
            detail = RunService._branch_search_verdict(pipelines, correlation)
            assert detail.status is verdict, (detail.status, verdict)
        strengths = {
            int(row.id): RunService._classify_branch_pipeline(row, correlation) for row in pipelines
        }
        return SimpleNamespace(status=verdict, strengths=strengths)
    finally:
        await gitlab.close()


async def _seed_mixed_rows(pe_db, gitlab_native, run_id: str, branch: str) -> tuple[dict, dict]:
    """The mixed-listing rows, seeded BEFORE the dispatch so their ids sit
    BELOW the start the dispatch mints (project ids are monotone — the
    honest ordering the R42-02 classifier relies on):

    - the PRIOR attempt's terminal pipeline + its BACKDATED journal (the
      durable identity that makes every later row above it CORRELATED
      rather than ambiguous-by-default);
    - a CORRELATED terminal — a terminal execution on the run-owned
      branch whose id is above every journaled prior: a fresh execution
      the current attempt must own (the release evidence, present from
      the start — the guard exists precisely so it must NOT release
      while a possibly-current row is still active).
    """
    prior = gitlab_native.seed_pipeline(
        ref=branch,
        sha="1" * 40,
        status="success",
        source="push",
        jobs=[{"name": "verify", "status": "success"}],
    )
    await _journal_prior_start(pe_db, run_id, branch, int(prior["id"]))
    correlated = gitlab_native.seed_pipeline(
        ref=branch,
        sha="5" * 40,
        status="success",
        source="api",
        jobs=[{"name": "forge-agent-claude-code", "status": "success"}],
    )
    return prior, correlated


async def _reconcile_draining_inprocess(pe_db, gitlab_native) -> int:
    """One drain pass in-process (the exactly-once bar's second pass)."""
    from forge.adaptive.admission import reconcile_draining
    from forge.config import Settings
    from forge.gitlab.client import GitLabClient
    from forge.runs.service import RunService, forge_token

    from .test_production_matrix import _mx_settings_kwargs

    kwargs = _mx_settings_kwargs(gitlab_native.base_url, pe_db.url)
    gitlab = GitLabClient(base_url=kwargs["GITLAB_URL"], token=forge_token(Settings(**kwargs)))
    try:
        service = RunService(
            session_factory=pe_db.worker_factory(),
            gitlab=gitlab,
            settings=Settings(**kwargs),
            config=ForgeConfig(),
        )
        return await reconcile_draining(pe_db.worker_factory(), service._native_occupancy_probe())
    finally:
        await gitlab.close()


def _cf_worker(pe_db, gitlab_url, tmp_path, *, name: str, **kwargs):
    """One CF worker child over the shared durable database."""
    return run_mx_worker(
        tmp_path,
        db_url=pe_db.url,
        gitlab_url=gitlab_url,
        budget="unlimited",
        backend="harness",
        name=name,
        **kwargs,
    )


async def _post_issue_note(gateway, note_id: int, text: str, *, iid: int, delivery: str):
    """One issue-note webhook through the REAL gateway subprocess; a
    SIGKILLed child surfaces as ``None``."""
    payload = {
        "object_kind": "note",
        "event_type": "note",
        "user": {"id": 11, "name": "Alice Approver", "username": "alice", "email": ""},
        "project": {
            "id": GL_PROJECT_ID,
            "name": "forge-cf",
            "path_with_namespace": "acme/forge-cf",
            "web_url": "https://gitlab.test/acme/forge-cf",
        },
        "object_attributes": {
            "id": note_id,
            "note": text,
            "noteable_type": "Issue",
            "noteable_id": 77000 + iid,
            "author_id": 11,
            "discussion_id": f"d-{note_id}",
        },
        "issue": {"id": 77000 + iid, "iid": iid, "title": "t", "description": "d"},
    }
    headers = {"X-Gitlab-Token": MX_WEBHOOK_SECRET, "X-Gitlab-Event": "Note Hook"}
    headers["X-Gitlab-Event-UUID"] = delivery
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            return await client.post(gateway.base_url + "/webhook", json=payload, headers=headers)
        except httpx.TransportError:
            return None


# ----------------------------------------------------------------------
# CF-A — the killed observation trace
# ----------------------------------------------------------------------


class TestCFATheKilledObservationTrace:
    @trace_record("CF-A the killed observation (accepted → faulted → kill → recovery)")
    async def test_accepted_command_faulted_observation_worker_kill_recovery(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path, fault_proxy
    ):
        monkeypatch.setenv("FORGE_REVIEW_FEEDBACK_ENABLED", "1")
        run_id, branch, mr_iid = await _landed(
            pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path
        )
        candidate_sha = (await get_run(pe_db.worker_factory(), run_id)).candidate_shas[-1]
        mr_notes_before = len(gitlab_native.state()["mr_notes"])
        ingress = _ingress_settings(gitlab_native)

        # --- BARRIER (after inbox commit): the accepted command IS durable
        #     before any worker exists — the 202 followed the transaction --
        application = create_app(settings=ingress)
        async with application.router.lifespan_context(application):
            application.state.session_factory = pe_db.worker_factory()
            application.state.task_queue = _LostWakeQueue()
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                accepted = await _post_note(
                    client, note_id=NOTE_ID, body=FIX_NOTE, mr_iid=mr_iid, delivery=DELIVERY
                )
                assert accepted.status_code == 202
                assert accepted.json()["run_command"] is True
        reset_engine()
        inbox, steps = await _inbox_and_steps(pe_db)
        assert len(inbox) == 1 and len(steps) == 1
        assert steps[0].status == "scheduled"  # durable, never executed
        source_event_id = inbox[0].source_event_id
        assert steps[0].source_event_id == source_event_id
        assert await _request_of(pe_db, run_id, NOTE_ID) is None  # no outcome yet

        # --- the faulted worker: a REAL process killed INSIDE the #374
        #     seam (the fault is armed BEFORE the worker starts — the
        #     required observation cannot succeed, so the boundary is
        #     deterministic).
        fault_proxy.arm_reads()
        killed = _cf_worker(
            pe_db,
            fault_proxy.base_url,  # the worker's provider view — through the fault
            tmp_path,
            name="cf-a-killed",
            kill="observation-boundary",
            max_seconds=30,
            strict_exit=False,  # the semantic detector below fails FIRST
        )

        # --- THE P01 DETECTOR — the step NEVER succeeded-with-no-outcome,
        #     asserted BEFORE the process-level corroboration so a
        #     plain-return defer regression (the pre-#374 shape: the
        #     handler returns, the step records SUCCESS, no request
        #     exists) fails HERE, on the semantic bar, not on harness
        #     plumbing.
        inbox_during, steps_during = await _inbox_and_steps(pe_db)
        step = steps_during[0]
        assert step.status == "running", (
            f"the faulted observation completed the step ({step.status}) with "
            f"request={await _request_of(pe_db, run_id, NOTE_ID)} — succeeded "
            "with no outcome (the pre-#374 plain return)"
        )
        assert await _request_of(pe_db, run_id, NOTE_ID) is None  # no outcome
        assert killed.returncode == -9  # the process died at the seam

        # --- the composed durable window (BEFORE local completion): the
        #     deferral's journals SURVIVED the process death; the step row
        #     recorded nothing — still claimed by the DEAD worker.
        assert step.attempt == 0  # fail_step never ran — no retry record
        assert step.lease_owner == "mx-worker"  # claimed by the dead process
        events = await _outbox_events(pe_db, run_id)
        retries = [p for kind, p in events if kind == "provider_observation.retry"]
        assert retries and retries[0]["reason"] == "mr_head"
        assert retries[0]["observed"] is False  # the head-never-observed fact
        outcomes = [p for kind, p in events if kind == "feedback.outcome"]
        assert outcomes and outcomes[-1]["outcome"] == "pending"
        assert len(_reply_notes(gitlab_native)) == 0  # native effects: none
        assert len(gitlab_native.state()["mr_notes"]) == mr_notes_before

        # --- the restart: the fault heals, the dead claim's lease elapses
        #     (the trace's clock advance — the honest spelling), and a
        #     FRESH worker process runs the SAME durable command through
        #     its REAL reaper + step loop.
        fault_proxy.disarm_reads()
        assert await _expire_dead_claims(pe_db) == 1
        _cf_worker(
            pe_db,
            fault_proxy.base_url,
            tmp_path,
            name="cf-a-recovery",
            max_seconds=45,
        )

        # --- the recovery: durable states + NATIVE EFFECT COUNTS ---------
        inbox_after, steps_after = await _inbox_and_steps(pe_db)
        assert len(inbox_after) == 1  # no re-delivery ever minted a row
        step_after = steps_after[0]
        assert step_after.status == "succeeded"  # success only WITH an outcome
        assert step_after.attempt >= 1  # the reaped claim's re-execution
        assert step_after.source_event_id == source_event_id  # the SAME step
        request = await _request_of(pe_db, run_id, NOTE_ID)
        assert request is not None and request.status == "staged"
        assert request.head_sha == candidate_sha  # bound to the current head
        assert len(_reply_notes(gitlab_native)) == 1  # exactly ONE reply
        assert len(gitlab_native.state()["mr_notes"]) == mr_notes_before + 1  # no more
        # the killed worker's journal survived the restart — the durable
        # seam the composition is about
        events_after = await _outbox_events(pe_db, run_id)
        assert [p for kind, p in events_after if kind == "provider_observation.retry"]


# ----------------------------------------------------------------------
# CF-B — the lost start under mixed occupancy
# ----------------------------------------------------------------------


class TestCFBTheLostStartMixedOccupancyTrace:
    @trace_record("CF-B the lost start under mixed occupancy (keeps; correlated terminal)")
    async def test_lost_create_response_mixed_listing_keeps_then_releases_once(
        self, pe_db, gitlab_native, tmp_path, monkeypatch
    ):
        from forge.adaptive.admission import AdmissionPolicy, try_acquire_lease

        # The pinned axes (explicit, like the gateway's --no-nudge): the
        # admission cap is ONE; the auto-revive axis is OFF — this trace
        # is about OCCUPANCY, and whose second start gets refused is
        # asserted through a second run's real /go below, not through the
        # revival machinery's own re-drive.
        monkeypatch.setenv("FORGE_ADMISSION_MAX_ACTIVE_PER_PROJECT", str(CF_CAP))
        monkeypatch.setenv("FORGE_RUN_AUTO_REVIVE_LIMIT", "0")
        policy = AdmissionPolicy(max_active_per_project=CF_CAP)
        _seed_workspace(gitlab_native, tmp_path, "harness")

        gateway = start_mx_gateway(
            tmp_path,
            db_url=pe_db.url,
            gitlab_url=gitlab_native.base_url,
            budget="unlimited",
            backend="harness",
            name="cf-b-gw",
        )
        try:
            first = await _post_issue_note(
                gateway,
                3701,
                f"@forge /implement {MX_ISSUE_TITLE}",
                iid=MX_ISSUE_IID,
                delivery="cf-b-imp",
            )
            assert first is not None and first.status_code == 202
            _cf_worker(pe_db, gitlab_native.base_url, tmp_path, name="cf-b-wk1")
            [run1] = await _all_flow_runs(pe_db)
            run_id = str(run1.id)
            branch = str((await _target_of(pe_db, run_id)).source_branch)
            # --- the mixed listing is seeded BEFORE the dispatch: the
            #     collaboration branch exists (the recorded target), the
            #     PRIOR attempt's terminal + journal and the CORRELATED
            #     terminal land with ids BELOW the start the dispatch is
            #     about to mint (project ids are monotone — the honest
            #     ordering the classifier relies on).
            prior, correlated = await _seed_mixed_rows(pe_db, gitlab_native, run_id, branch)
            # --- the AFTER-REMOTE-EFFECT barrier: the drop rule fires
            #     once, on the FIRST create_pipeline — the effect
            #     registers, the reply never leaves (the
            #     lost-create-response window).
            gitlab_native.set_latency(
                [{"method": "POST", "tail": ["pipeline"], "phase": "drop", "ms": 1}]
            )
            go = await _post_issue_note(
                gateway, 3702, f"@forge /go {run_id}", iid=MX_ISSUE_IID, delivery="cf-b-go"
            )
            assert go is not None and go.status_code == 202
        finally:
            gateway.stop()
        _cf_worker(pe_db, gitlab_native.base_url, tmp_path, name="cf-b-wk2")

        # --- the lost start: the run parked blocked, the intent retained,
        #     the lease DRAINING — and the native ledger holds EXACTLY ONE
        #     dispatch whose pipeline is RUNNING on the collaboration
        #     branch (the effect landed; the caller saw an error).
        status, reason = await _run_status(pe_db, run_id)
        assert status == FlowStatus.BLOCKED.value, (status, reason)
        assert "harness_start_failed" in reason, reason
        dispatches = gitlab_native.dispatches()
        assert len(dispatches) == 1  # THE ACTUAL NATIVE START COUNT: one
        (lost,) = dispatches
        assert str(lost["ref"]) == branch
        lost_pipeline_id = int(lost["pipeline_id"])
        lost_job_id = int(lost["job_id"])
        lease = await _lease_of(pe_db, run_id)
        assert lease is not None and lease.released_at is None, (
            "the lost start's lease was RELEASED while its own pipeline row was "
            "still AMBIGUOUS-ACTIVE on the listing (the pre-#375 reducer order — "
            "the correlated terminal must not release past an active ambiguous row)"
        )
        assert lease.native_intent_ref == f"gitlab:pipeline:{GL_PROJECT_ID}@{branch}"
        assert await _held_slots(pe_db, policy) == CF_CAP  # the slot is held

        # --- THE LEASE-KEPT ASSERTION — the INSTALLED reconciler tick runs
        #     the occupancy pass over the mixed listing and releases
        #     NOTHING while the ambiguous row runs; asserted BEFORE the
        #     verdict pin so a disabled ambiguous-active guard (the
        #     pre-#375 reducer: the correlated terminal releases) fails
        #     HERE, on the kept lease, not on a downstream corollary.
        _cf_worker(
            pe_db,
            gitlab_native.base_url,
            tmp_path,
            name="cf-b-mixed",
            mode="reconciler",
            ticks=2,
        )
        assert await _held_slots(pe_db, policy) == CF_CAP, (
            "the mixed listing released the lease while the lost start's own "
            "row was still AMBIGUOUS-ACTIVE (the pre-#375 reducer order)"
        )
        assert (await _lease_of(pe_db, run_id)).released_at is None

        # --- the mixed verdict, pinned per row: the PRIOR attempt's
        #     terminal (journal-identified → historical), a CORRELATED
        #     terminal the current attempt can own (id above every
        #     journaled prior), and the lost start's own RUNNING row at
        #     the input revision (degraded timestamp, base SHA → AMBIGUOUS
        #     — the REAL row the dispatch minted, not a seed).
        intent_key = f"gitlab:pipeline:{GL_PROJECT_ID}@{branch}"
        mixed = await _probe_with_strengths(pe_db, gitlab_native, intent_key)
        assert mixed.status.name == "RUNNING"  # rule 2: ambiguous-ACTIVE
        assert mixed.strengths[lost_pipeline_id] == "ambiguous"
        assert mixed.strengths[int(prior["id"])] == "historical"
        assert mixed.strengths[int(correlated["id"])] == "corroborated_start"

        # --- no second start while unresolved: a SECOND run's real /go
        #     dispatch is refused at the cap — the native ledger does not
        #     move.
        second = await _drive_second_run_to_go(pe_db, gitlab_native, tmp_path)
        assert second.status == FlowStatus.BLOCKED.value, second.status_reason
        assert "execution_capacity" in (second.status_reason or "")
        assert len(gitlab_native.dispatches()) == 1  # STILL exactly one start
        assert await _held_slots(pe_db, policy) == CF_CAP

        # --- the resolution: the ambiguous row goes terminal; the
        #     correlated terminal then releases the slot EXACTLY ONCE.
        gitlab_native.mark_job(lost_job_id, "success")
        resolved = await _probe_with_strengths(pe_db, gitlab_native, intent_key)
        assert resolved.status.name == "TERMINAL"
        _cf_worker(
            pe_db,
            gitlab_native.base_url,
            tmp_path,
            name="cf-b-release",
            mode="reconciler",
            ticks=2,
        )
        released = await _lease_of(pe_db, run_id)
        assert released is not None and released.released_at is not None
        assert await _held_slots(pe_db, policy) == 0  # the slot returned
        # EXACTLY ONCE: a further drain pass releases nothing more.
        assert await _reconcile_draining_inprocess(pe_db, gitlab_native) == 0
        assert (
            await try_acquire_lease(
                policy,
                GL_PROJECT_ID,
                pe_db.worker_factory(),
                run_id="9" * 31 + "a",
                provider="gitlab",
            )
            is not None
        )  # the honestly-freed slot admits again


async def _drive_second_run_to_go(pe_db, gitlab_native, tmp_path) -> FlowRun:
    """A second issue through /implement + /go: its dispatch must be the
    one refused at the cap (the real choke point, never a hand-made
    lease)."""
    gitlab_native.seed_issue(CF_SECOND_IID, CF_SECOND_TITLE, CF_SECOND_DESC)
    gateway = start_mx_gateway(
        tmp_path,
        db_url=pe_db.url,
        gitlab_url=gitlab_native.base_url,
        budget="unlimited",
        backend="harness",
        name="cf-b-gw2",
    )
    try:
        first = await _post_issue_note(
            gateway,
            3711,
            f"@forge /implement {CF_SECOND_TITLE}",
            iid=CF_SECOND_IID,
            delivery="cf-b2-imp",
        )
        assert first is not None and first.status_code == 202
        _cf_worker(pe_db, gitlab_native.base_url, tmp_path, name="cf-b2-wk1")
        [run2] = [row for row in await _all_flow_runs(pe_db) if row.issue_iid == CF_SECOND_IID]
        go = await _post_issue_note(
            gateway, 3712, f"@forge /go {run2.id}", iid=CF_SECOND_IID, delivery="cf-b2-go"
        )
        assert go is not None and go.status_code == 202
    finally:
        gateway.stop()
    _cf_worker(pe_db, gitlab_native.base_url, tmp_path, name="cf-b2-wk2")
    return await _run_of(pe_db, str(run2.id))


# ----------------------------------------------------------------------
# CF-M1 — the #374 plain-return mutation arm (the worker-child pairing)
# ----------------------------------------------------------------------


class TestCFM1ThePlainReturnDeferArm:
    @trace_record("CF-M1 the #374 plain-return defer mutant", mutation="plain-return-defer")
    async def test_the_reverted_defer_records_success_with_no_outcome(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path, fault_proxy
    ):
        """The #374 defect seeded into the worker CHILD (the process that
        executes the policy): the handler plain-returns on the faulted
        observation, the step records SUCCESS, no request exists — the
        review's P01 verbatim. The CF-A trace's
        never-succeeded-without-an-outcome assertions fail under exactly
        this patch (the pairing); the arm pins the defect's observable so
        the regression can never re-enter silently."""
        monkeypatch.setenv("FORGE_REVIEW_FEEDBACK_ENABLED", "1")
        run_id, branch, mr_iid = await _landed(
            pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path
        )
        mr_notes_before = len(gitlab_native.state()["mr_notes"])
        ingress = _ingress_settings(gitlab_native)

        application = create_app(settings=ingress)
        async with application.router.lifespan_context(application):
            application.state.session_factory = pe_db.worker_factory()
            application.state.task_queue = _LostWakeQueue()
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                accepted = await _post_note(
                    client, note_id=8902, body=FIX_NOTE, mr_iid=mr_iid, delivery="cf-m1-a"
                )
                assert accepted.status_code == 202
        reset_engine()

        fault_proxy.arm_reads()
        _cf_worker(
            pe_db,
            fault_proxy.base_url,
            tmp_path,
            name="cf-m1-mutant",
            mutation="plain-return-defer",
            max_seconds=30,
        )

        # THE DEFECT'S OBSERVABLE — the review's P01, verbatim:
        # step=succeeded, requests=0, handler_calls=1.
        _, steps = await _inbox_and_steps(pe_db)
        step = steps[0]
        assert step.status == "succeeded"
        assert step.attempt == 0  # one handler call, zero failures recorded
        assert await _request_of(pe_db, run_id, 8902) is None  # requests=0
        events = await _outbox_events(pe_db, run_id)
        assert not [p for kind, p in events if kind == "provider_observation.retry"]
        assert not [p for kind, p in events if kind == "feedback.outcome"]
        assert len(_reply_notes(gitlab_native)) == 0
        assert len(gitlab_native.state()["mr_notes"]) == mr_notes_before
        # and the obligation is GONE: a healed re-delivery of the SAME
        # note deduplicates at the inbox and records nothing.
        fault_proxy.disarm_reads()
        application = create_app(settings=ingress)
        async with application.router.lifespan_context(application):
            application.state.session_factory = pe_db.worker_factory()
            application.state.task_queue = _LostWakeQueue()
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                redelivery = await _post_note(
                    client, note_id=8902, body=FIX_NOTE, mr_iid=mr_iid, delivery="cf-m1-b"
                )
                assert redelivery.status_code == 202
                assert redelivery.json()["deduplicated"] is True
        reset_engine()
        _cf_worker(pe_db, fault_proxy.base_url, tmp_path, name="cf-m1-after", max_seconds=20)
        assert await _request_of(pe_db, run_id, 8902) is None


# ----------------------------------------------------------------------
# CF-M2 — the #375 ambiguous-guard-disabled mutation arm
# ----------------------------------------------------------------------


async def _lost_start_world(pe_db, gitlab_native, tmp_path, monkeypatch, *, tag: str):
    """The CF-B baseline world up to the mixed listing: the REAL lost
    create response (the drop rule on the first dispatch) plus the
    seeded prior-terminal and correlated-terminal rows (seeded BEFORE the
    dispatch — monotone id ordering) — everything the lease-kept phase
    and its mutation arm run against."""
    monkeypatch.setenv("FORGE_ADMISSION_MAX_ACTIVE_PER_PROJECT", str(CF_CAP))
    monkeypatch.setenv("FORGE_RUN_AUTO_REVIVE_LIMIT", "0")
    _seed_workspace(gitlab_native, tmp_path, "harness")
    gateway = start_mx_gateway(
        tmp_path,
        db_url=pe_db.url,
        gitlab_url=gitlab_native.base_url,
        budget="unlimited",
        backend="harness",
        name=f"cf-{tag}-gw",
    )
    try:
        first = await _post_issue_note(
            gateway,
            3801,
            f"@forge /implement {MX_ISSUE_TITLE}",
            iid=MX_ISSUE_IID,
            delivery=f"cf-{tag}-imp",
        )
        assert first is not None and first.status_code == 202
        _cf_worker(pe_db, gitlab_native.base_url, tmp_path, name=f"cf-{tag}-wk1")
        [run1] = await _all_flow_runs(pe_db)
        run_id = str(run1.id)
        branch = str((await _target_of(pe_db, run_id)).source_branch)
        await _seed_mixed_rows(pe_db, gitlab_native, run_id, branch)
        gitlab_native.set_latency(
            [{"method": "POST", "tail": ["pipeline"], "phase": "drop", "ms": 1}]
        )
        go = await _post_issue_note(
            gateway, 3802, f"@forge /go {run_id}", iid=MX_ISSUE_IID, delivery=f"cf-{tag}-go"
        )
        assert go is not None and go.status_code == 202
    finally:
        gateway.stop()
    _cf_worker(pe_db, gitlab_native.base_url, tmp_path, name=f"cf-{tag}-wk2")

    (lost,) = gitlab_native.dispatches()
    assert len(gitlab_native.dispatches()) == 1  # exactly one native start
    branch = str(lost["ref"])
    return SimpleNamespace(
        run_id=run_id,
        branch=branch,
        intent_key=f"gitlab:pipeline:{GL_PROJECT_ID}@{branch}",
        lost_id=int(lost["pipeline_id"]),
        lost_job_id=int(lost["job_id"]),
    )


class TestCFM2TheAmbiguousGuardDisabledArm:
    @trace_record(
        "CF-M2 the #375 ambiguous-active guard mutant", mutation="ambiguous-guard-disabled"
    )
    async def test_the_guard_disabled_releases_on_the_correlated_terminal(
        self, pe_db, gitlab_native, tmp_path, monkeypatch
    ):
        """The #375 guard (rule 2: ambiguous-ACTIVE precedes any
        all-terminal verdict) disabled where the CF-B probe assertions
        run: the correlated terminal beside the still-running ambiguous
        row wins, the verdict answers TERMINAL, the drain pass RELEASES
        the slot and the next acquire OVERSUBSCRIBES at capacity one. The
        CF-B lease-kept assertions fail under exactly this patch (the
        pairing)."""
        from forge.adaptive.admission import (
            AdmissionPolicy,
            NativeStatus,
            reconcile_draining,
            try_acquire_lease,
        )
        from forge.config import Settings
        from forge.gitlab.client import GitLabClient
        from forge.gitlab.schemas import Pipeline
        from forge.runs.service import (
            _CI_ACTIVE_STATUSES,
            _OCCUPANCY_CURRENT_STRENGTHS,
            RunService,
            forge_token,
        )

        from .test_production_matrix import _mx_settings_kwargs

        world = await _lost_start_world(pe_db, gitlab_native, tmp_path, monkeypatch, tag="m2")
        policy = AdmissionPolicy(max_active_per_project=CF_CAP)

        # THE MUTANT — the #375 guard removed: the ambiguous-ACTIVE check
        # no longer precedes the all-terminal verdict (the reducer order
        # the review's P02 counterexample broke).
        def guard_disabled(pipelines: list[Pipeline], corr):  # noqa: ANN001
            current = [
                p
                for p in pipelines
                if RunService._classify_branch_pipeline(p, corr) in _OCCUPANCY_CURRENT_STRENGTHS
            ]
            if any((p.status or "").lower() in _CI_ACTIVE_STATUSES for p in current):
                return NativeStatus.RUNNING
            if current:
                return NativeStatus.TERMINAL  # ← releases beside ambiguity
            return NativeStatus.UNKNOWN

        monkeypatch.setattr(RunService, "_branch_search_occupancy", staticmethod(guard_disabled))

        # the REAL classifier still labels the rows (the premise is real):
        # the ambiguous row is ACTIVE — the lost start's own pipeline.
        mixed = await _probe_with_strengths(
            pe_db, gitlab_native, world.intent_key, pin_detail=False
        )
        assert mixed.strengths[world.lost_id] == "ambiguous"
        (lost_pipeline,) = [p for p in gitlab_native.pipelines() if p["id"] == world.lost_id]
        assert lost_pipeline["status"] == "running"

        # THE DEFECT'S OBSERVABLE — the correlated terminal wins while the
        # ambiguous row is still running:
        assert mixed.status.name == "TERMINAL"  # the guard would have kept it
        kwargs = _mx_settings_kwargs(gitlab_native.base_url, pe_db.url)
        gitlab = GitLabClient(base_url=kwargs["GITLAB_URL"], token=forge_token(Settings(**kwargs)))
        try:
            service = RunService(
                session_factory=pe_db.worker_factory(),
                gitlab=gitlab,
                settings=Settings(**kwargs),
                config=ForgeConfig(),
            )
            assert (
                await reconcile_draining(pe_db.worker_factory(), service._native_occupancy_probe())
                == 1
            )  # freed early — with the ambiguous row still running
        finally:
            await gitlab.close()
        assert await _held_slots(pe_db, policy) == 0
        # OVERSUBSCRIPTION at capacity one: the second lease is granted
        # while the first job runs natively.
        assert (
            await try_acquire_lease(
                policy,
                GL_PROJECT_ID,
                pe_db.worker_factory(),
                run_id="9" * 31 + "b",
                provider="gitlab",
            )
            is not None
        ), "the oversubscription did not reproduce"
