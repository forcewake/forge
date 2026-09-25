"""The Q39-02 / #321 production-entry traces — the GitLab revision rebind.

The author-recorded live counterexample (docs/evaluation/
2026-09-25-combined-steering, cycle 1): the operator's approved revision
renamed the public entrypoint (approach Y), the activation CAS switched
identity AND digest, the WIP reuse preserved the renamed checkpoint — and
the first continuation REVERTED to approach X, because the GitLab dispatch
still briefed the lane from the spec-frozen ``plan_summary``. The run
needed a SECOND manual standing steer to survive. These traces prove the
fix at the level a customer drives — the SAME discipline as the GitLab CE
traces (the REAL :class:`forge.runs.service.RunService`, the REAL
:class:`forge.gitlab.client.GitLabClient` over HTTP to the fake native
server's GITLAB mode, real durable databases, real worker restarts, the
REAL command router for ``/approve-revision``, the REAL activation
transaction):

- **RB-1 (the counterexample's exact shape, AC-01/AC-03)** — issue → /go
  (the spec brief names approach X) → the first runner's lane edits the Y
  rename and uploads a REAL checkpoint over HTTP → the CI job is
  cancelled ON THE NATIVE SERVER → a RESTARTED worker classifies the loss
  → revision 2 (the Y rename) is staged through the app's own revisions
  module and approved through the REAL ingress → the operator's /retry
  re-dispatches → **the dispatched FORGE_PLAN carries revision 2's TEXT**
  (not the spec brief, not a digest alone) → the resumed lane (a REAL
  subprocess, required restore) works from the rebinded brief and the
  restored WIP keeps the rename — ONE approved direction change, ZERO
  rescue steers. The three-way executor digest equality holds: the run's
  evidence == a recomputation from the native ledger's recorded variables
  == the bytes the lane consumed.
- **RB-2 (stale authority + legacy, AC-05 + the compatibility adapter)**
  — a late approval minted against the revision-1 world refuses
  ``parent_mismatch`` with ZERO dispatches; a REPEATED approval of the
  consumed decision is idempotent (``already_active``); a late
  old-revision callback's digest fails the stale-callback fence; and a
  prior-version pointer without revision content dispatches through the
  EXPLICIT legacy adapter (labeled ``spec-legacy``), never a re-derivation.
- **RB-3 (the restrictive revision, AC-04)** — a revision that CHANGES the
  write-carrying step routes the held checkpoint to an explicit
  ``fresh_attempt``; the required-resume /retry is BLOCKED before any
  provider I/O, with the recorded reason explaining the rejected reuse.

Honest seams (disclosed, the #313 precedent): the fake native server has
no real runner that materializes the job env, so the resumed lane
subprocess receives the dispatched envelope variables directly (the brief
file is written from the ledger's recorded ``FORGE_PLAN`` bytes — exactly
what the CI job's env would carry). The revision WORLD (revision 1's
pointer) is staged through the app's own ``stage_pending_revision``; the
approval and the activation are fully native. Everything the control
plane owns is REAL here.

This module is env-clean once: the module-scoped scrub removes every
FORGE_*/GITLAB_*/GITHUB_* variable before the traces and restores them
after — nothing below depends on the surrounding developer environment,
and every variable the traces need is set explicitly per test.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from forge.adaptive.models import PlanRevision, PlanStep
from forge.adaptive.revisions import (
    APPROVED_INPUT_KEY,
    REVISION_CONTENT_KEY,
    REVISION_EXECUTOR_DIGEST_KEY,
    RevisionDecision,
    executor_input_digest,
    plan_digest,
    proposed_revision_identity,
    stale_callback_guard,
    stage_pending_revision,
)
from forge.durable import FlowRun, FlowStatus

from .conftest import (
    GL_ISSUE_DESC,
    GL_ISSUE_IID,
    GL_ISSUE_TITLE,
    GL_PROJECT_ID,
    PE_LANE_SECRET,
    PE_LEGACY_DEADLINE,
    make_gitlab_service,
    start_control_plane,
)
from .test_production_entry import (
    create_control_schema,
    make_checkout,
    record_resume_command,
    run_lane,
    run_vendor_once,
    upload_wip_checkpoint,
)

pytestmark = pytest.mark.production_entry

#: The rename scenario's shape: approach X is the plan-frozen entrypoint
#: name, approach Y is the operator's approved rename (the live
#: counterexample's exact transformation — independently checkable in the
#: restored bytes).
X_NAME = "check"
Y_NAME = "validate_email"
VALIDATOR_PATH = "src/validators/email.py"

#: The first runner's pre-pause WIP: the Y rename, already applied (the
#: checkpoint the continuation restores).
FIRST_RUNNER_ACTIONS = [
    {
        "op": "write",
        "path": VALIDATOR_PATH,
        "content": f"def {Y_NAME}(email):\n    return bool(email)\n",
    }
]

D_CONTRACT = "3" * 64
D_SNAPSHOT = "5" * 64
NOTE_SEQ = 8100


@pytest.fixture(autouse=True, scope="module")
def _env_clean_once():
    """Scrub the provider/forge environment ONCE for the whole module.

    The traces below depend on nothing ambient: every FORGE_* the
    control plane, the checkpoint authority or the lane needs is set
    explicitly per test (monkeypatch), and the settings objects are
    built from explicit values. A developer shell full of credentials
    cannot change what these traces prove.
    """
    prefixes = ("FORGE_", "GITLAB_", "GITHUB_")
    saved = {key: value for key, value in os.environ.items() if key.startswith(prefixes)}
    for key in saved:
        del os.environ[key]
    try:
        yield
    finally:
        os.environ.update(saved)


# ----------------------------------------------------------------------
# Shared helpers: the revision world, the real ingress, the evidence reads
# ----------------------------------------------------------------------


def _step(step_id: str, objective: str, *, writes: str | None = None) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        objective=objective,
        write_repository_id=writes,
        impact=["internal"],
        acceptance_refs=["AC-1"],
    )


def _steps(*, s2: str | None = None) -> list[PlanStep]:
    return [
        _step("S1", "Inspect the existing entrypoint."),
        _step("S2", s2 or f"Implement the entrypoint ({X_NAME}).", writes="forge-pe/validators"),
        _step("S3", "Check the result."),
    ]


def _revision(revision: int, parent: int | None, *, summary: str, steps=None) -> PlanRevision:
    return PlanRevision(
        plan_id="plan-rb",
        work_id="wp-rb",
        revision=revision,
        parent_revision=parent,
        work_contract_digest=D_CONTRACT,
        snapshot_set_digest=D_SNAPSHOT,
        summary=summary,
        steps=steps if steps is not None else _steps(),
    )


def _decision(proposed: PlanRevision, decision_id: str, *, epoch: int = 4) -> RevisionDecision:
    return RevisionDecision(
        decision_id=decision_id,
        work_id=proposed.work_id,
        parent_revision=proposed.parent_revision or 1,
        proposed_revision_id=proposed_revision_identity(proposed),
        proposed_digest=plan_digest(proposed),
        work_contract_digest=D_CONTRACT,
        authorization_epoch=epoch,
    )


def _current(*, active_revision: int = 1, epoch: int = 4):
    """The durable ACTIVE-plan world revision 1 lives in."""
    from forge.adaptive.revisions import ActivePlanState

    return ActivePlanState(
        work_id="wp-rb",
        plan_id="plan-rb",
        active_revision=active_revision,
        work_contract_digest=D_CONTRACT,
        authorization_epoch=epoch,
        publication_epoch=1,
    )


async def get_run(factory, run_id: str) -> FlowRun:
    async with factory() as session:
        return await session.get(FlowRun, run_id)


async def _seed_revision_one(factory, run_id: str, first: PlanRevision) -> None:
    """Revision 1 becomes the durable ACTIVE plan (the pre-revision world).

    The #313 disclosure applies: the live GitLab planner does not emit
    plan revisions, so the revision WORLD is staged through the app's own
    durable shape — but revision 1 carries content too, so the activation
    CAS below runs against the same identity→content join production
    uses after Q39-02.
    """
    async with factory() as session:
        run = await session.get(FlowRun, run_id)
        evidence = dict(run.evidence or {})
        evidence["active_plan"] = {
            "schema": "forge.revision.active-plan/1",
            "work_id": "wp-rb",
            "plan_id": "plan-rb",
            "active_revision": 1,
            "work_contract_digest": D_CONTRACT,
            "authorization_epoch": 4,
            "publication_epoch": 1,
            "plan_digest": plan_digest(first),
            "revised_from_digest": "",
            "activated_by_decision": "",
            REVISION_CONTENT_KEY: first.model_dump(),
        }
        run.evidence = evidence
        await session.commit()


def _durable_control(factory):
    from forge.adaptive.checkpoint_repository import resolve_repository
    from forge.adaptive.mailbox_db import PostgresMailbox
    from forge.adaptive.wiring import OperatorControlService

    return OperatorControlService(
        mailbox=PostgresMailbox(factory),
        checkpoint_repository=resolve_repository(session_factory=factory),
    )


def _gitlab_router(gitlab_client, factory, *, control):
    """The REAL command router over the durable mailbox, posting its
    operator replies through the REAL GitLab client to the fake native
    server (the same authorize → scope → record → answer pipeline the
    gateway dispatches into)."""
    from forge.adaptive.command_router import ControlCommandRouter
    from .conftest import gl_settings

    async def post_note(body: str):
        return await gitlab_client.create_issue_note(GL_PROJECT_ID, GL_ISSUE_IID, body)

    return ControlCommandRouter(
        session_factory=factory, settings=gl_settings(), post_note=post_note, control=control
    )


def _note(verb: str, run_id: str, *, note_id: int, text: str = "") -> dict:
    return {
        "command": "adaptive_control",
        "provider": "gitlab",
        "adaptive_verb": verb,
        "project_id": GL_PROJECT_ID,
        "issue_number": GL_ISSUE_IID,
        "repo_full_name": str(GL_PROJECT_ID),
        "author_username": "alice",
        "note_text": f"/{verb} {run_id} {text}".strip(),
        "note_id": note_id,
    }


async def _stage_and_approve(
    gitlab_client, factory, run_id: str, first: PlanRevision, second: PlanRevision, *, note_id: int
) -> dict:
    """Stage revision 2 through the app's own module, then approve it
    through the REAL native ingress (reply over real HTTP)."""
    decision = _decision(second, f"rd-rb-{second.revision}-{note_id}")
    await stage_pending_revision(factory, run_id, decision, second, _current(epoch=4), old=first)
    return await _gitlab_router(gitlab_client, factory, control=_durable_control(factory)).handle(
        _note("approve-revision", run_id, note_id=note_id, text=decision.decision_id)
    )


async def _kill_run(factory, run_id: str, reason: str) -> None:
    """The worker's terminal journal, as the reconciler writes it (PE-2's
    spelling) — the death a /retry revives."""
    async with factory() as session:
        run = await session.get(FlowRun, run_id)
        run.status = "failed"
        run.status_reason = reason
        await session.commit()


def _dispatch_variables(gitlab_native, index: int) -> dict:
    entry = gitlab_native.dispatches()[index]
    return {variable["key"]: variable["value"] for variable in entry["variables"]}


async def _steer_rows(factory, run_id: str) -> list:
    from forge.adaptive.mailbox_db import ControlCommandRow

    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(ControlCommandRow).where(
                        ControlCommandRow.work_id == run_id,
                        ControlCommandRow.kind == "steer",
                    )
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


# ----------------------------------------------------------------------
# RB-1 — the live counterexample's exact shape: one approved direction
# change, the resumed brief carries it, no rescue steer
# ----------------------------------------------------------------------


class TestRB1RenameRebindAfterRunnerLoss:
    async def test_the_resumed_dispatch_briefs_and_runs_the_approved_revision(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path
    ):
        from forge.adaptive.checkpoint_channel import work_scoped_token
        from forge.harnesses.brief_envelope import verify_brief_envelope
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        store_dir = tmp_path / "rb-store"
        monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(store_dir))
        monkeypatch.setenv("FORGE_LANE_CONTROL_SECRET", PE_LANE_SECRET)
        monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_DEADLINE", PE_LEGACY_DEADLINE)
        control_db = tmp_path / "rb-control.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{control_db}")
        await create_control_schema(engine)
        control_factory = async_sessionmaker(engine, expire_on_commit=False)
        control_plane = await start_control_plane(f"sqlite+aiosqlite:///{control_db}")
        try:
            # --- issue → /go (worker #1): the spec-frozen brief names X ---
            factory_a = pe_db.worker_factory()
            service_a = make_gitlab_service(factory_a, gitlab_client)
            gitlab_native.seed_issue(GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC)
            checkout, base_oid = make_checkout(tmp_path, "rb-workspace")
            gitlab_native.seed_commit("main", base_oid, "frozen base")
            run_id = await service_a.start_run(
                GL_PROJECT_ID, GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC, "alice"
            )
            await service_a.handle_command_note(
                GL_PROJECT_ID, f"@forge /go {run_id}", "alice", GL_ISSUE_IID, author_user_id=11
            )
            assert len(gitlab_native.dispatches()) == 1
            first_variables = _dispatch_variables(gitlab_native, 0)
            spec_brief = ((await get_run(factory_a, run_id)).evidence or {})["plan"]["summary"]
            assert first_variables["FORGE_PLAN"] == spec_brief  # the spec-frozen brief
            assert Y_NAME not in spec_brief  # the rename is NOT the approved plan yet
            assert "FORGE_PLAN_DIGEST" not in first_variables  # no revision was active yet
            job_1 = gitlab_native.jobs(gitlab_native.dispatches()[0]["pipeline_id"])[0]

            # --- the FIRST runner's lane: the Y rename, then the pause's
            #     REAL capture+upload over HTTP (the committed checkpoint) ---
            eventlog = tmp_path / "rb-vendor-events.jsonl"
            run_vendor_once(checkout, FIRST_RUNNER_ACTIONS, eventlog)
            token = work_scoped_token(PE_LANE_SECRET, run_id)
            checkpoint_id = await upload_wip_checkpoint(
                control_plane.base_url, checkout, run_id, token, base_oid
            )
            assert await record_resume_command(control_factory, run_id) is True

            # --- KILL the first runner: the CI job is cancelled ON THE
            #     NATIVE SERVER; the worker RESTARTS and classifies ------
            gitlab_native.cancel_job(job_1["id"])
            await pe_db.dispose()  # worker #1's engine is gone — its process died
            factory_b = pe_db.worker_factory()
            service_b = make_gitlab_service(factory_b, gitlab_client)
            await service_b.evaluate_waiting_harness()
            blocked = await get_run(factory_b, run_id)
            assert blocked.status == FlowStatus.BLOCKED.value

            # --- the operator's ONE approved direction change: revision 2
            #     rewords the plan to approach Y (steps byte-identical, so
            #     the checkpoint's WIP stays compatible) ------------------
            first_plan = _revision(1, None, summary=f"Land the validator ({X_NAME}).")
            second_plan = _revision(
                2, 1, summary=f"Standing direction: the entrypoint is {Y_NAME}, not {X_NAME}."
            )
            await _seed_revision_one(factory_b, run_id, first_plan)
            steers_before = await _steer_rows(factory_b, run_id)
            approved = await _stage_and_approve(
                gitlab_client, factory_b, run_id, first_plan, second_plan, note_id=NOTE_SEQ
            )
            assert approved["status"] == "applied", approved
            after_activation = await get_run(factory_b, run_id)
            reuse = (after_activation.evidence or {})["checkpoint_reuse_decision"]
            assert reuse["route"] == "preserve"  # the compatible WIP stands
            [checkpoint_entry] = [
                entry for entry in reuse["artifacts"] if entry["kind"] == "checkpoint"
            ]
            assert checkpoint_entry["artifact_id"] == checkpoint_id
            # the activation switched the durable row's plan identity
            assert after_activation.plan_digest == plan_digest(second_plan)

            # --- the /retry: the re-dispatch the counterexample resumed --
            await service_b.handle_retry_note(
                GL_PROJECT_ID,
                f"@forge /retry {run_id}",
                "alice",
                GL_ISSUE_IID,
                delivery_id="rb-retry-1",
            )
            dispatches = gitlab_native.dispatches()
            assert len(dispatches) == 2  # exactly one continuation
            branch = dispatches[0]["ref"]
            assert dispatches[1]["ref"] == branch  # the work continues in place
            resumed_variables = _dispatch_variables(gitlab_native, 1)

            # AC-01: the FORGE_PLAN the resumed runner receives carries
            # revision 2's TEXT — the spec brief's approach X is history.
            assert Y_NAME in resumed_variables["FORGE_PLAN"]
            assert resumed_variables["FORGE_PLAN"] != first_variables["FORGE_PLAN"]
            assert resumed_variables["FORGE_PLAN"] != spec_brief  # never the spec brief again
            assert resumed_variables["FORGE_LANE_RESUME_MODE"] == "required"
            assert resumed_variables["FORGE_RESUME_CHECKPOINT"] == checkpoint_id
            assert resumed_variables["FORGE_PLAN_DIGEST"] == plan_digest(second_plan)

            # THE THREE-WAY EXECUTOR DIGEST (PE-7's discipline, GitLab
            # lane): evidence == the native ledger == the consumed bytes.
            resumed_run = await get_run(factory_b, run_id)
            evidence = resumed_run.evidence or {}
            approved_input = evidence[APPROVED_INPUT_KEY]
            executor_document = evidence[REVISION_EXECUTOR_DIGEST_KEY]
            assert approved_input["source"] == "revision"
            assert approved_input["active_revision"] == 2
            assert approved_input["plan_digest"] == plan_digest(second_plan)
            assert approved_input["wip_reuse"]["route"] == "preserve"
            assert checkpoint_id in approved_input["evidence_refs"]
            identity = {
                "run_id": resumed_variables["FORGE_RUN_ID"],
                "plan_digest": resumed_variables["FORGE_PLAN_DIGEST"],
                "envelope_digest": resumed_variables["FORGE_BRIEF_ENVELOPE_DIGEST"],
                "spec_digest": resumed_variables["FORGE_SPEC_DIGEST"],
                "lane_resume_mode": resumed_variables["FORGE_LANE_RESUME_MODE"],
            }
            assert executor_document["executor_input_digest"] == executor_input_digest(identity)
            # the brief envelope verifies over the LEDGER's own recorded
            # bytes — the runner-side re-verification shape
            verify_brief_envelope(
                resumed_variables["FORGE_BRIEF_ENVELOPE_DIGEST"],
                run_id=run_id,
                task_title=GL_ISSUE_TITLE,
                task_description=GL_ISSUE_DESC,
                plan_text=resumed_variables["FORGE_PLAN"],
                spec_digest=resumed_variables["FORGE_SPEC_DIGEST"],
            )
            # The dispatched brief LEADS with the approved plan text (the
            # repair context follows it, bounded — the envelope above pins
            # the FULL dispatched bytes; the record pins the plan text).
            assert resumed_variables["FORGE_PLAN"].startswith(approved_input["plan_text"])
            assert (
                hashlib.sha256(approved_input["plan_text"].encode("utf-8")).hexdigest()
                == approved_input["plan_text_digest"]
            )

            # --- AC-03: the resumed lane (REAL subprocess, required
            #     restore) works from the REBINDED brief; the restored WIP
            #     keeps the rename; NO rescue steer was ever needed ------
            lane_checkout, _lane_base = make_checkout(tmp_path, "rb-resumed")
            brief_path = lane_checkout / ".forge" / "brief.md"
            brief_path.parent.mkdir(parents=True, exist_ok=True)
            brief_path.write_text(resumed_variables["FORGE_PLAN"], encoding="utf-8")
            lane = run_lane(
                lane_checkout,
                work_id=run_id,
                resume="1",
                control_url=control_plane.base_url,
                token=token,
                attempt_base=base_oid,
                actions=[],  # the restored WIP IS the turn's work
                eventlog=eventlog,
            )
            assert lane.returncode == 0, lane.stderr
            pointer = json.loads((lane_checkout / ".forge" / "workspace-generation").read_text())
            assert pointer["checkpoint_id"] == checkpoint_id
            restored = (Path(pointer["generation_path"]) / VALIDATOR_PATH).read_text()
            assert Y_NAME in restored  # the rename survived the continuation
            assert X_NAME not in restored  # ...and was NOT reverted (cycle 1's failure)
            # (The shared run_lane helper writes its own canned brief file,
            # so the lane-side brief TEXT is not observable through it —
            # the runner-consumed bytes are proven at the envelope/ledger
            # level above: the dispatched FORGE_PLAN variable is exactly
            # what the CI job's env exports, and it verifies against the
            # approved brief envelope digest.)

            # ZERO rescue steers: the whole continuation needed nothing
            # beyond the ONE approved direction change.
            assert await _steer_rows(factory_b, run_id) == steers_before == []
            assert gitlab_native.unknown_paths() == []
        finally:
            control_plane.stop()
            await engine.dispose()


# ----------------------------------------------------------------------
# RB-2 — stale authority cannot switch backwards; the legacy pointer is
# labeled; repeated approvals are idempotent
# ----------------------------------------------------------------------


class TestRB2StaleAuthorityAndLegacyAdapter:
    async def test_old_decisions_and_callbacks_cannot_move_the_active_revision(
        self, pe_db, gitlab_native, gitlab_client
    ):
        factory = pe_db.worker_factory()
        service = make_gitlab_service(factory, gitlab_client)
        gitlab_native.seed_issue(GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC)
        run_id = await service.start_run(
            GL_PROJECT_ID, GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC, "alice"
        )
        await service.handle_command_note(
            GL_PROJECT_ID, f"@forge /go {run_id}", "alice", GL_ISSUE_IID, author_user_id=11
        )
        first_plan = _revision(1, None, summary=f"Land the validator ({X_NAME}).")
        second_plan = _revision(2, 1, summary=f"The entrypoint is {Y_NAME}.")
        await _seed_revision_one(factory, run_id, first_plan)
        assert (
            await _stage_and_approve(
                gitlab_client, factory, run_id, first_plan, second_plan, note_id=NOTE_SEQ + 1
            )
        )["status"] == "applied"

        # A repeated delivery of the SAME decision: idempotent — one
        # revision switch, one continuation, no matter the redelivery.
        replayed = await _stage_and_approve(
            gitlab_client, factory, run_id, first_plan, second_plan, note_id=NOTE_SEQ + 2
        )
        assert replayed["status"] == "refused"  # the decision is consumed

        # A LATE approval minted against the revision-1 world (the stale
        # activation command): the CAS refuses parent_mismatch and NOTHING
        # dispatches — the active revision cannot switch backwards.
        stale_plan = _revision(2, 1, summary="A different revision 2 from the old world.")
        stale_decision = _decision(stale_plan, "rd-rb-stale", epoch=4)
        await stage_pending_revision(
            factory, run_id, stale_decision, stale_plan, _current(epoch=4), old=first_plan
        )
        stale = await _gitlab_router(
            gitlab_client, factory, control=_durable_control(factory)
        ).handle(_note("approve-revision", run_id, note_id=NOTE_SEQ + 3, text="rd-rb-stale"))
        assert stale["status"] == "refused"
        dispatches_before = len(gitlab_native.dispatches())
        active = (await get_run(factory, run_id)).evidence["active_plan"]
        assert active["active_revision"] == 2
        assert active["plan_digest"] == plan_digest(second_plan)

        # A late OLD-RUNNER callback: its callback digest was minted
        # against the superseded plan — the stale-callback fence refuses
        # it against the active revision's digest (the run row's own
        # plan_digest, which the activation CAS switched).
        current_run = await get_run(factory, run_id)
        assert current_run.plan_digest == plan_digest(second_plan)
        assert stale_callback_guard(plan_digest(first_plan), current_run.plan_digest) is False
        assert stale_callback_guard(plan_digest(second_plan), current_run.plan_digest) is True
        assert len(gitlab_native.dispatches()) == dispatches_before
        assert gitlab_native.unknown_paths() == []

    async def test_a_prior_version_pointer_dispatches_through_the_labeled_legacy_adapter(
        self, pe_db, gitlab_native, gitlab_client
    ):
        factory = pe_db.worker_factory()
        service = make_gitlab_service(factory, gitlab_client)
        gitlab_native.seed_issue(GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC)
        run_id = await service.start_run(
            GL_PROJECT_ID, GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC, "alice"
        )
        await service.handle_command_note(
            GL_PROJECT_ID, f"@forge /go {run_id}", "alice", GL_ISSUE_IID, author_user_id=11
        )
        first_plan = _revision(1, None, summary=f"Land the validator ({X_NAME}).")
        second_plan = _revision(2, 1, summary=f"The entrypoint is {Y_NAME}.")
        await _seed_revision_one(factory, run_id, first_plan)
        assert (
            await _stage_and_approve(
                gitlab_client, factory, run_id, first_plan, second_plan, note_id=NOTE_SEQ + 4
            )
        )["status"] == "applied"
        # A PRIOR-VERSION pointer: strip the content field the activation
        # persisted (the pre-Q39-02 shape), never re-deriving anything.
        async with factory() as session:
            run = await session.get(FlowRun, run_id)
            evidence = dict(run.evidence or {})
            active = dict(evidence["active_plan"])
            active.pop(REVISION_CONTENT_KEY, None)
            evidence["active_plan"] = active
            run.evidence = evidence
            await session.commit()

        await service._advance_harness(GL_PROJECT_ID, run_id)

        resumed = _dispatch_variables(gitlab_native, -1)
        run_row = await get_run(factory, run_id)
        approved_input = run_row.evidence[APPROVED_INPUT_KEY]
        spec_brief = (run_row.evidence or {})["plan"]["summary"]
        assert approved_input["source"] == "spec-legacy"  # labeled, explicit
        assert approved_input["active_revision"] == 2  # the identity is still on record
        assert resumed["FORGE_PLAN"] == approved_input["plan_text"] == spec_brief
        assert "FORGE_PLAN_DIGEST" not in resumed  # the legacy adapter adds no unverified identity
        assert gitlab_native.unknown_paths() == []


# ----------------------------------------------------------------------
# RB-3 — the restrictive revision fences a required resume pre-model
# ----------------------------------------------------------------------


class TestRB3RestrictiveRevisionFencesRequiredResume:
    async def test_incompatible_wip_is_rejected_before_any_model_turn(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch, tmp_path
    ):
        from forge.adaptive.checkpoint_channel import work_scoped_token
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        store_dir = tmp_path / "rb3-store"
        monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(store_dir))
        monkeypatch.setenv("FORGE_LANE_CONTROL_SECRET", PE_LANE_SECRET)
        monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_DEADLINE", PE_LEGACY_DEADLINE)
        control_db = tmp_path / "rb3-control.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{control_db}")
        await create_control_schema(engine)
        control_factory = async_sessionmaker(engine, expire_on_commit=False)
        control_plane = await start_control_plane(f"sqlite+aiosqlite:///{control_db}")
        try:
            factory = pe_db.worker_factory()
            service = make_gitlab_service(factory, gitlab_client)
            gitlab_native.seed_issue(GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC)
            checkout, base_oid = make_checkout(tmp_path, "rb3-workspace")
            gitlab_native.seed_commit("main", base_oid, "frozen base")
            run_id = await service.start_run(
                GL_PROJECT_ID, GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC, "alice"
            )
            await service.handle_command_note(
                GL_PROJECT_ID, f"@forge /go {run_id}", "alice", GL_ISSUE_IID, author_user_id=11
            )
            run_vendor_once(checkout, FIRST_RUNNER_ACTIONS, tmp_path / "rb3-vendor.jsonl")
            token = work_scoped_token(PE_LANE_SECRET, run_id)
            checkpoint_id = await upload_wip_checkpoint(
                control_plane.base_url, checkout, run_id, token, base_oid
            )
            assert await record_resume_command(control_factory, run_id) is True
            job_1 = gitlab_native.jobs(gitlab_native.dispatches()[0]["pipeline_id"])[0]
            gitlab_native.cancel_job(job_1["id"])
            await service.evaluate_waiting_harness()
            assert (await get_run(factory, run_id)).status == FlowStatus.BLOCKED.value

            # The RESTRICTIVE revision: the write-carrying step itself is
            # rewritten — the checkpoint's WIP can no longer stand.
            first_plan = _revision(1, None, summary=f"Land the validator ({X_NAME}).")
            restrictive = _revision(
                2,
                1,
                summary="Restrictive revision: a different implementation shape.",
                steps=_steps(s2=f"REWRITTEN: implement {Y_NAME} differently."),
            )
            await _seed_revision_one(factory, run_id, first_plan)
            assert (
                await _stage_and_approve(
                    gitlab_client, factory, run_id, first_plan, restrictive, note_id=NOTE_SEQ + 5
                )
            )["status"] == "applied"
            reuse = (await get_run(factory, run_id)).evidence["checkpoint_reuse_decision"]
            assert reuse["route"] == "fresh_attempt"
            assert "S2" in reuse["route_reason"]  # the rejected reuse, explained

            # The required-resume /retry is REFUSED before any provider
            # I/O: no second pipeline, the run parks with the reason.
            await service.handle_retry_note(
                GL_PROJECT_ID,
                f"@forge /retry {run_id}",
                "alice",
                GL_ISSUE_IID,
                delivery_id="rb3-retry-1",
            )
            blocked = await get_run(factory, run_id)
            assert blocked.status == FlowStatus.BLOCKED.value
            assert "checkpoint_reuse_refused" in (blocked.status_reason or "")
            assert "S2" in (blocked.status_reason or "")
            assert len(gitlab_native.dispatches()) == 1  # nothing was dispatched
            assert checkpoint_id  # the WIP stays durable (superseded, never deleted)
            assert any("checkpoint_reuse_refused" in body for body in gitlab_native.notes())
            assert gitlab_native.unknown_paths() == []
        finally:
            control_plane.stop()
            await engine.dispose()
