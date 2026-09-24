"""The R36-13 / #272 production-entry traces (AT-11 → PE-7).

The unit file (`tests/test_revision_executor_proof.py`) pins the RULES;
this file proves them at the level a customer drives — the same
discipline as PE-1..PE-6 (real HTTP to the fake native server, real
durable databases, real worker restarts, real ingress):

- **PE-7a (AT-11 core)** — a native ``/approve-revision`` of revision 2
  through the REAL command router (reply over real HTTP), then a worker
  restart, then the next ACTUAL dispatch: the native server's recorded
  executor input carries revision 2's exact plan digest (not revision
  1's, not the old plan comment's) and the server-side fingerprint
  equals the ``revision.executor_digest`` evidence — recomputable from
  the ledger's own inputs.
- **PE-7b (stale authority)** — an approval minted against the revision
  1 world arriving after revision 2 activated: the CAS refuses
  (``parent_mismatch``) with ZERO dispatches and an unchanged active
  plan; a delayed steer carrying ``expected_plan_revision=1`` EXPIRES at
  the durable mailbox's dispatch gate without ever reaching a vendor.
- **PE-7c (questions at-most-once)** — a question's authorized answer
  through the real ingress survives a worker restart as exactly ONE
  durable row; a redelivered note is deduplicated by the reply journal
  and a SECOND note adopts the same logical answer — and once the lane
  ladder applied it, the pending view holds nothing to re-apply.
- **PE-7d (WIP compatibility pair)** — pause → checkpoint over real
  HTTP → approve a COMPATIBLE revision → the resume dispatch carries
  BOTH the new plan digest AND the exact pinned checkpoint; the
  INCOMPATIBLE revision records ``checkpoint.reuse_decision`` =
  ``fresh_attempt`` and the required-resume dispatch is BLOCKED before
  any provider I/O — never a silent reuse.
- **PE-7e (separate visibility)** — pause received / effect applied /
  checkpoint committed / resumed are DISTINCT projection rows over the
  durable ``control_commands`` rows and their audit journals; a resume
  no runner drained stays a REQUEST — never a premature
  "resumed-success".

Assertions are on artifacts — the server's dispatch ledger and its
server-side executor-input fingerprints, DB row identities, evidence
documents — never on status strings alone.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from forge.adaptive.models import ControlCommand, PlanRevision, PlanStep
from forge.adaptive.revisions import (
    ACTIVE_PLAN_KEY,
    REVISION_ACTIVATIONS_KEY,
    ActivePlanState,
    RevisionDecision,
    executor_input_digest,
    plan_digest,
    proposed_revision_identity,
    stage_pending_revision,
)
from forge.durable import FlowRun

from .conftest import (
    FAKE_VENDOR,
    PE_LANE_SECRET,
    PE_LEGACY_DEADLINE,
    PE_OWNER,
    PE_REPO,
    PE_REPO_NAME,
    make_service,
    pe_settings,
    start_control_plane,
)
from .test_production_entry import go, start_run

pytestmark = pytest.mark.production_entry

PROJECT_ID = 90210
D_CONTRACT = "3" * 64
D_SNAPSHOT = "5" * 64


# ----------------------------------------------------------------------
# Shared helpers: the revision world, the real ingress, real checkpoints
# ----------------------------------------------------------------------


def _step(step_id: str, objective: str, *, writes: str | None = None) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        objective=objective,
        write_repository_id=writes,
        impact=["internal"],
        acceptance_refs=["AC-1"],
    )


def _steps(*, changed_s2: str | None = None) -> list[PlanStep]:
    return [
        _step("S1", "Inspect existing behavior."),
        _step("S2", changed_s2 or "Implement the authorized change.", writes="acme/forge-pe"),
        _step("S3", "Check the result."),
    ]


def _revision(
    revision: int,
    parent: int | None,
    *,
    summary: str = "Land the widget.",
    steps: list[PlanStep] | None = None,
) -> PlanRevision:
    return PlanRevision(
        plan_id="plan-pe7",
        work_id="wp-pe7",
        revision=revision,
        parent_revision=parent,
        work_contract_digest=D_CONTRACT,
        snapshot_set_digest=D_SNAPSHOT,
        summary=summary,
        steps=steps if steps is not None else _steps(),
    )


def _current() -> ActivePlanState:
    return ActivePlanState(
        work_id="wp-pe7",
        plan_id="plan-pe7",
        active_revision=1,
        work_contract_digest=D_CONTRACT,
        authorization_epoch=3,
        publication_epoch=1,
    )


def _decision(proposed: PlanRevision, decision_id: str) -> RevisionDecision:
    return RevisionDecision(
        decision_id=decision_id,
        work_id="wp-pe7",
        parent_revision=proposed.parent_revision or 1,
        proposed_revision_id=proposed_revision_identity(proposed),
        proposed_digest=plan_digest(proposed),
        work_contract_digest=D_CONTRACT,
        authorization_epoch=3,
    )


async def get_run(factory, run_id: str) -> FlowRun:
    async with factory() as session:
        return await session.get(FlowRun, run_id)


def client_of(native_client):
    """The real transport client out of the (client, reader) fixture pair."""
    client, _reader = native_client
    return client


def reader_of(native_client):
    """The repository reader out of the (client, reader) fixture pair."""
    _client, reader = native_client
    return reader


async def _seed_active_plan(factory, run_id: str, first: PlanRevision) -> None:
    """Revision 1 becomes the durable ACTIVE plan (the pre-revision world)."""
    async with factory() as session:
        run = await session.get(FlowRun, run_id)
        merged = dict(run.evidence or {})
        merged[ACTIVE_PLAN_KEY] = {
            "schema": "forge.revision.active-plan/1",
            "work_id": "wp-pe7",
            "plan_id": "plan-pe7",
            "active_revision": 1,
            "plan_digest": plan_digest(first),
            "revised_from_digest": "",
            "work_contract_digest": D_CONTRACT,
            "authorization_epoch": 3,
            "publication_epoch": 1,
            "activated_by_decision": "",
        }
        run.evidence = merged
        await session.commit()


def _durable_control(factory):
    """The operator control service over the DURABLE mailbox — the rows the
    operator surface reads, the rows a restarted worker still sees."""
    from forge.adaptive.checkpoint_repository import resolve_repository
    from forge.adaptive.mailbox_db import PostgresMailbox
    from forge.adaptive.wiring import OperatorControlService

    return OperatorControlService(
        mailbox=PostgresMailbox(factory),
        checkpoint_repository=resolve_repository(session_factory=factory),
    )


def _router(client, factory, *, control):
    """The REAL command router over the durable mailbox, posting its
    operator replies through the REAL GitHub client to the fake native
    server (the same authorize → scope → record → answer pipeline the
    gateway dispatches into)."""
    from forge.adaptive.command_router import ControlCommandRouter

    async def post(body: str):
        return await client.create_issue_comment(PE_OWNER, PE_REPO_NAME, 42, body)

    return ControlCommandRouter(
        session_factory=factory, settings=pe_settings(), post_note=post, control=control
    )


def _note(verb: str, run_id: str, *, note_id: int, text: str = "") -> dict:
    return {
        "command": "adaptive_control",
        "provider": "github",
        "adaptive_verb": verb,
        "project_id": PROJECT_ID,
        "issue_number": 42,
        "repo_full_name": PE_REPO,
        "author_username": "alice",
        "note_text": f"/{verb} {run_id} {text}".strip(),
        "note_id": note_id,
    }


def _answer_note(run_id: str, *, note_id: int, question_id: str, text: str) -> dict:
    """``/answer <question-id> <text>`` — the answer names its QUESTION (the
    run resolves through the note's issue, never a run prefix)."""
    note = _note("answer", run_id, note_id=note_id)
    note["note_text"] = f"/answer {question_id} {text}"
    return note


async def _kill_run(factory, run_id: str, reason: str) -> None:
    """The worker's terminal journal, as the reconciler writes it (PE-2's
    spelling) — the death a /retry revives."""
    async with factory() as session:
        run = await session.get(FlowRun, run_id)
        run.status = "failed"
        run.status_reason = reason
        await session.commit()


async def _retry(service, run_id: str, delivery: str) -> None:
    await service.handle_retry(
        project_id=PROJECT_ID,
        issue_number=42,
        note_text=f"/retry {run_id}",
        author_username="alice",
        delivery_id=delivery,
    )


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    return result


def _make_checkout(parent: Path, name: str) -> tuple[Path, str]:
    """A REAL git checkout at its frozen base (PE-1's starting shape)."""
    checkout = parent / name
    checkout.mkdir(parents=True)
    _git(checkout, "init", "-q", "-b", "main")
    _git(checkout, "config", "user.email", "lane@example.com")
    _git(checkout, "config", "user.name", "forge lane")
    (checkout / "src").mkdir()
    (checkout / "src" / "app.py").write_text("print('base')\n")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "-m", "frozen base")
    base_oid = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    exclude = checkout / ".git" / "info" / "exclude"
    exclude.write_text(exclude.read_text() + "\n.forge/\n__pycache__/\n")
    return checkout, base_oid


def _run_vendor_once(cwd: Path, content: str, eventlog: Path) -> None:
    """The controlled vendor's pre-pause WIP leg (real file edits)."""
    env = {
        **os.environ,
        "FAKE_VENDOR_ACTIONS": json.dumps(
            [{"op": "write", "path": "src/app.py", "content": content}]
        ),
        "FAKE_VENDOR_EVENTLOG": str(eventlog),
    }
    outcome = subprocess.run(
        [sys.executable, str(FAKE_VENDOR), "--once"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert outcome.returncode == 0, outcome.stderr


def _tracked_baseline(checkout: Path) -> dict[str, str]:
    """path -> sha256(raw bytes) of the tracked files at HEAD (R28-04)."""
    names = _git(checkout, "ls-files").stdout.split()
    baseline: dict[str, str] = {}
    for name in names:
        blob = subprocess.run(
            ["git", "-C", str(checkout), "show", f"HEAD:{name}"],
            capture_output=True,
            timeout=60,
            check=True,
        ).stdout
        baseline[name] = hashlib.sha256(blob).hexdigest()
    return baseline


async def _upload_checkpoint(
    control_url: str, checkout: Path, work_id: str, token: str, attempt_base: str
) -> str:
    """Capture the checkout's WIP and upload it over REAL HTTP (PE-1's leg)."""
    from forge.adaptive.artifact_store import ContentAddressedStore
    from forge.adaptive.checkpoint_channel import CheckpointChannel, LaneControlAPI
    from forge.adaptive.checkpointing import capture_wip

    store = ContentAddressedStore(root=checkout / ".forge" / "checkpoints", tenant=work_id)
    channel = CheckpointChannel(LaneControlAPI(base_url=control_url, work_token=token))
    receipt = capture_wip(
        work_id=work_id,
        root=checkout,
        store=store,
        tracked_baseline=_tracked_baseline(checkout),
        source_oids={"attempt_base": attempt_base},
        sequence=1,
        upload=channel,
    )
    assert receipt.verified
    return str(receipt.artifact_id)


async def _pause_and_checkpoint(
    client, factory, run_id: str, tmp_path: Path, *, note_id: int
) -> str:
    """The pause (real ingress → durable row + fence) and the checkpoint
    (real capture + upload over real HTTP through the control plane)."""
    from forge.adaptive.checkpoint_channel import work_scoped_token
    from sqlalchemy.ext.asyncio import create_async_engine

    from forge import api_checkpoint_channel  # noqa: F401 — checkpoint metadata table
    from forge.adaptive import mailbox_db  # noqa: F401 — control_commands table
    from forge.models.base import Base

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'pe7-control.db'}"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    control_plane = await start_control_plane(db_url)
    try:
        paused = await _router(client, factory, control=_durable_control(factory)).handle(
            _note("pause", run_id, note_id=note_id)
        )
        assert paused["status"] == "applied"

        checkout, base_oid = _make_checkout(tmp_path, "pe7-workspace")
        _run_vendor_once(checkout, "print('paused wip')\n", tmp_path / "pe7-vendor-events.jsonl")
        token = work_scoped_token(PE_LANE_SECRET, run_id)
        return await _upload_checkpoint(control_plane.base_url, checkout, run_id, token, base_oid)
    finally:
        control_plane.stop()
        await engine.dispose()


async def _approve_revision(
    client, factory, run_id: str, second: PlanRevision, first: PlanRevision, *, note_id: int
):
    decision = _decision(second, f"rd-pe7-{second.revision}-{note_id}")
    await stage_pending_revision(factory, run_id, decision, second, _current(), old=first)
    result = await _router(client, factory, control=_durable_control(factory)).handle(
        _note("approve-revision", run_id, note_id=note_id, text=decision.decision_id)
    )
    return result


# ----------------------------------------------------------------------
# PE-7a (AT-11) — the approved revision reaches the next REAL executor input
# ----------------------------------------------------------------------


class TestPE7ApproveRevisionReachesTheNextExecutorInput:
    async def test_the_next_real_dispatch_carries_the_approved_plan_and_exact_digest(
        self, pe_db, native, native_client, monkeypatch, tmp_path
    ):
        client, reader = native_client
        monkeypatch.setenv("FORGE_LANE_CONTROL_URL", "")  # no checkpoint authority
        monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(tmp_path / "no-checkpoints"))
        factory_a = pe_db.worker_factory()
        service_a = make_service(factory_a, client, reader)
        native.seed_issue(42, "Add a widget", "Make widgets real.")
        run_id = await start_run(service_a, 42)
        await go(service_a, run_id, 42)
        assert len(native.dispatches()) == 1  # the first dispatch really landed
        (first,) = native.dispatches()
        assert "plan_digest" not in first["inputs"]  # no revision was active yet
        original_comment_digest = (await get_run(factory_a, run_id)).plan_digest

        # The revision world: revision 1 active, revision 2 staged with the
        # SUPERSEDED content beside it (the compatibility proof's input).
        first_plan = _revision(1, None)
        second_plan = _revision(2, 1, summary="Reworded: land the widget fast.")
        await _seed_active_plan(factory_a, run_id, first_plan)
        decision = _decision(second_plan, "rd-pe7-a")
        await stage_pending_revision(
            factory_a, run_id, decision, second_plan, _current(), old=first_plan
        )

        # The NATIVE approve-revision: the REAL router pipeline over the
        # durable mailbox, its operator reply posted over real HTTP.
        result = await _router(client, factory_a, control=_durable_control(factory_a)).handle(
            _note("approve-revision", run_id, note_id=7001, text=decision.decision_id)
        )
        assert result["status"] == "applied"
        assert any("is now" in body and "ACTIVE" in body for body in native.comments())

        # THE WORKER DIES; a restarted one (fresh engine over the same rows)
        # drives the next dispatch through the real /retry surface.
        await pe_db.dispose()
        factory_b = pe_db.worker_factory()
        service_b = make_service(factory_b, client, reader)
        await _kill_run(
            factory_b,
            run_id,
            "harness_infrastructure: harness_bootstrap_failed (node setup)",
        )
        await _retry(service_b, run_id, "pe7a-retry-1")

        # ARTIFACTS on the native server: exactly one NEW dispatch, whose
        # recorded executor input names revision 2's EXACT digest — not
        # revision 1's, not the old plan comment's.
        dispatches = native.dispatches()
        assert len(dispatches) == 2
        second = dispatches[-1]
        branch = f"forge/42/{run_id[:8]}"
        assert second["ref"] == branch
        inputs = second["inputs"]
        assert inputs["plan_digest"] == plan_digest(second_plan)
        assert inputs["plan_digest"] != plan_digest(first_plan)
        assert inputs["plan_digest"] != original_comment_digest
        assert inputs["run_id"] == run_id

        # The EXACT digest, three independent ways: the run's
        # ``revision.executor_digest`` evidence, the SERVER's own
        # fingerprint of what the production client sent, and a
        # recomputation from the ledger's recorded inputs.
        run = await get_run(factory_b, run_id)
        executor_document = (run.evidence or {})["revision_executor_digest"]
        assert executor_document["plan_digest"] == plan_digest(second_plan)
        assert executor_document["active_revision"] == 2
        assert executor_document["executor_input_digest"] == native.executor_digests(ref=branch)[-1]
        assert executor_document["executor_input_digest"] == executor_input_digest(inputs)

        # The activation's WIP reuse decision is on the record too (no
        # checkpoint existed → nothing to refuse).
        reuse = (run.evidence or {})["checkpoint_reuse_decision"]
        assert reuse["route"] == "preserve"
        assert reuse["activated_revision"] == 2
        # The REAL client never fell off the modeled API surface.
        assert native.unknown_paths() == []


# ----------------------------------------------------------------------
# PE-7b — stale authority cannot reactivate
# ----------------------------------------------------------------------


class TestPE7StaleAuthorityCannotReactivate:
    async def _activated_world(self, pe_db, native, native_client, monkeypatch, tmp_path):
        client, reader = native_client
        monkeypatch.setenv("FORGE_LANE_CONTROL_URL", "")
        monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(tmp_path / "no-checkpoints"))
        factory = pe_db.worker_factory()
        service = make_service(factory, client, reader)
        native.seed_issue(42, "Add a widget", "Make widgets real.")
        run_id = await start_run(service, 42)
        await go(service, run_id, 42)
        first_plan = _revision(1, None)
        second_plan = _revision(2, 1, summary="Reworded: land the widget fast.")
        await _seed_active_plan(factory, run_id, first_plan)
        assert (
            await _approve_revision(client, factory, run_id, second_plan, first_plan, note_id=7101)
        )["status"] == "applied"
        return factory, run_id

    async def test_a_late_approval_for_the_superseded_world_refuses_with_zero_effects(
        self, pe_db, native, native_client, monkeypatch, tmp_path
    ):
        factory, run_id = await self._activated_world(
            pe_db, native, native_client, monkeypatch, tmp_path
        )
        before = (await get_run(factory, run_id)).evidence["active_plan"]
        dispatches_before = len(native.dispatches())

        # The LATE proposal: minted against the revision 1 world (its parent
        # expectation is revision 1), arriving after revision 2 activated.
        late_plan = _revision(3, 1, summary="The steer that came too late.")
        late_decision = _decision(late_plan, "rd-pe7-late")
        await stage_pending_revision(factory, run_id, late_decision, late_plan, _current())
        client = client_of(native_client)
        result = await _router(client, factory, control=_durable_control(factory)).handle(
            _note("approve-revision", run_id, note_id=7102, text=late_decision.decision_id)
        )
        assert result["status"] == "refused"

        run = await get_run(factory, run_id)
        evidence = run.evidence or {}
        # ZERO new effects: the active plan is untouched, exactly ONE
        # activation ever, no dispatch left the boundary.
        assert evidence["active_plan"] == before
        assert list(evidence[REVISION_ACTIVATIONS_KEY]) == ["rd-pe7-2-7101"]
        assert len(native.dispatches()) == dispatches_before
        [refused] = [body for body in native.comments() if "was refused" in body]
        assert "parent_mismatch" in refused  # the typed code, operator-visible

    async def test_a_delayed_steer_from_the_old_revision_expires_undelivered(
        self, pe_db, native, native_client, monkeypatch, tmp_path
    ):
        from forge.adaptive.mailbox_db import PostgresMailbox

        factory, run_id = await self._activated_world(
            pe_db, native, native_client, monkeypatch, tmp_path
        )
        active_digest = (await get_run(factory, run_id)).evidence["active_plan"]["plan_digest"]
        dispatches_before = len(native.dispatches())

        # The DELAYED STEER: written against revision 1 while revision 2 is
        # active — the durable mailbox's expected-revision CAS must EXPIRE it
        # at the dispatch gate, before any vendor correlation.
        mailbox = PostgresMailbox(factory)
        command = ControlCommand(
            command_id="cmd-pe7-stale-steer",
            work_id=run_id,
            sequence=await mailbox.next_sequence(run_id),
            kind="steer",
            actor_ref="alice",
            actor_origin="server_authenticated_human",
            idempotency_key="pe7:steer:stale",
            status="received",
            expected_plan_revision=1,  # the superseded world
            payload={"text": "Write to billing as well", "run_id": run_id},
        )
        stored, created = await mailbox.submit(command)
        assert created
        await mailbox.authorize(stored.command_id, {"server_authenticated_human": ("alice",)})
        expired = await mailbox.dispatch(
            stored.command_id, current_plan_revision=2, current_execution_epoch=1
        )
        assert expired.status == "expired"  # never dispatching, never vendor-bound
        # The queue no longer holds it — no lane can drain the stale authority.
        assert all(item.command_id != stored.command_id for item in await mailbox.pending(run_id))
        # Zero new write scope: nothing dispatched, the ACTIVE plan unchanged.
        assert len(native.dispatches()) == dispatches_before
        assert (await get_run(factory, run_id)).evidence["active_plan"]["plan_digest"] == (
            active_digest
        )


# ----------------------------------------------------------------------
# PE-7c — a question + authorized answer survive restart, applied at most once
# ----------------------------------------------------------------------


class TestPE7QuestionAnswerAtMostOnce:
    async def test_one_durable_answer_survives_the_restart_and_redeliveries(
        self, pe_db, native, native_client, monkeypatch, tmp_path
    ):
        from forge.adaptive.mailbox_db import ControlCommandRow, PostgresMailbox

        client, reader = native_client
        monkeypatch.setenv("FORGE_LANE_CONTROL_URL", "")
        monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(tmp_path / "no-checkpoints"))
        factory_a = pe_db.worker_factory()
        service_a = make_service(factory_a, client, reader)
        native.seed_issue(42, "Add a widget", "Make widgets real.")
        run_id = await start_run(service_a, 42)

        router_a = _router(client, factory_a, control=_durable_control(factory_a))
        question = "q-pe7-1"
        answer = "Ship the exponential backoff"

        # An UNAUTHORIZED answer is refused with a note (authority first).
        stranger = await router_a.handle(
            _answer_note(run_id, note_id=7201, question_id=question, text=answer)
            | {"author_username": "stranger"}
        )
        assert stranger["status"] == "refused"

        # The authorized answer lands through the real ingress.
        first = await router_a.handle(
            _answer_note(run_id, note_id=7202, question_id=question, text=answer)
        )
        assert first["status"] == "applied"

        # A REDELIVERED note (same delivery id): the reply journal collapses it.
        redelivery = await router_a.handle(
            _answer_note(run_id, note_id=7202, question_id=question, text=answer)
        )
        assert redelivery["status"] == "deduplicated"

        # A SECOND, DIFFERENT note answering the SAME question: the mailbox
        # dedup adopts the winner — one logical answer, "already on record".
        second_note = await router_a.handle(
            _answer_note(run_id, note_id=7203, question_id=question, text="Changed my mind")
        )
        assert second_note["status"] == "applied"
        assert any("already on record" in body for body in native.comments())

        async def answer_rows(factory) -> list[ControlCommandRow]:
            async with factory() as session:
                return list(
                    (
                        await session.execute(
                            select(ControlCommandRow).where(
                                ControlCommandRow.work_id == run_id,
                                ControlCommandRow.kind == "answer",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )

        rows_a = await answer_rows(factory_a)
        assert len(rows_a) == 1  # exactly ONE durable answer, first-writer-wins
        assert rows_a[0].payload["question_id"] == question
        assert rows_a[0].payload["text"] == answer  # the replay's bytes discarded

        # THE RESTART: a fresh service instance over the same durable rows.
        await pe_db.dispose()
        factory_b = pe_db.worker_factory()
        rows_b = await answer_rows(factory_b)
        assert [row.id for row in rows_b] == [row.id for row in rows_a]

        # Applied AT MOST ONCE as a logical decision: the lane ladder climbs
        # the answer to ``applied`` exactly once, and the pending view a
        # second drain would read holds NOTHING to re-apply.
        mailbox = PostgresMailbox(factory_b)
        await mailbox.authorize(rows_b[0].id, {"server_authenticated_human": ("alice",)})
        await mailbox.dispatch(rows_b[0].id, current_plan_revision=1, current_execution_epoch=1)
        await mailbox.vendor_accepted(rows_b[0].id)
        applied = await mailbox.observe(rows_b[0].id)
        assert applied.status == "applied"
        assert all(item.command_id != rows_b[0].id for item in await mailbox.pending(run_id))
        assert native.unknown_paths() == []


# ----------------------------------------------------------------------
# PE-7d — the WIP compatibility pair (compatible survives, incompatible is explicit)
# ----------------------------------------------------------------------


class TestPE7WipCompatibility:
    async def _driven_run(self, pe_db, native, native_client, monkeypatch, tmp_path):
        client, reader = native_client
        monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(tmp_path / "pe7-store"))
        monkeypatch.setenv("FORGE_LANE_CONTROL_SECRET", PE_LANE_SECRET)
        monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_DEADLINE", PE_LEGACY_DEADLINE)
        factory = pe_db.worker_factory()
        service = make_service(factory, client, reader)
        native.seed_issue(42, "Add a widget", "Make widgets real.")
        run_id = await start_run(service, 42)
        await go(service, run_id, 42)
        return client, factory, run_id

    async def test_a_compatible_checkpoint_survives_the_revision_into_the_resume_dispatch(
        self, pe_db, native, native_client, monkeypatch, tmp_path
    ):
        client, factory_a, run_id = await self._driven_run(
            pe_db, native, native_client, monkeypatch, tmp_path
        )
        checkpoint_id = await _pause_and_checkpoint(
            client, factory_a, run_id, tmp_path, note_id=7301
        )
        assert checkpoint_id

        first_plan = _revision(1, None)
        second_plan = _revision(2, 1, summary="Reworded: land the widget fast.")
        await _seed_active_plan(factory_a, run_id, first_plan)
        assert (
            await _approve_revision(
                client, factory_a, run_id, second_plan, first_plan, note_id=7302
            )
        )["status"] == "applied"

        # The activation recorded the COMPATIBLE verdict: the exact checkpoint
        # preserved under the new plan.
        run = await get_run(factory_a, run_id)
        reuse = (run.evidence or {})["checkpoint_reuse_decision"]
        assert reuse["route"] == "preserve"
        checkpoint_entries = [
            entry for entry in reuse["artifacts"] if entry["kind"] == "checkpoint"
        ]
        assert [entry["artifact_id"] for entry in checkpoint_entries] == [checkpoint_id]
        assert checkpoint_entries[0]["decision"] == "preserve"

        # Worker restart; the death a checkpoint-holding /retry revives.
        await pe_db.dispose()
        factory_b = pe_db.worker_factory()
        service_b = make_service(factory_b, client, reader_of(native_client))
        await _kill_run(factory_b, run_id, "harness_infrastructure: worker_lost_after_vendor")
        await _retry(service_b, run_id, "pe7d-retry-1")

        dispatches = native.dispatches()
        assert len(dispatches) == 2  # the resume dispatch really happened
        branch = f"forge/42/{run_id[:8]}"
        inputs = dispatches[-1]["inputs"]
        # BOTH identities on the dispatched executor input: the NEW plan
        # digest AND the required resume the exact checkpoint backs.
        assert inputs["plan_digest"] == plan_digest(second_plan)
        assert inputs["lane_resume_mode"] == "required"
        run = await get_run(factory_b, run_id)
        envelope = (run.evidence or {}).get("attempt_start") or {}
        assert envelope["continuation_ref_digest"] == checkpoint_id
        # The continuation decision pinned the SAME exact checkpoint.
        assert (run.evidence or {})["continuation"]["checkpoint_digest"] == checkpoint_id
        assert (run.evidence or {})["revision_executor_digest"]["executor_input_digest"] == (
            native.executor_digests(ref=branch)[-1]
        )
        assert native.unknown_paths() == []

    async def test_an_incompatible_revision_blocks_the_required_resume_visibly(
        self, pe_db, native, native_client, monkeypatch, tmp_path
    ):
        client, factory_a, run_id = await self._driven_run(
            pe_db, native, native_client, monkeypatch, tmp_path
        )
        checkpoint_id = await _pause_and_checkpoint(
            client, factory_a, run_id, tmp_path, note_id=7311
        )

        first_plan = _revision(1, None)
        incompatible = _revision(
            2, 1, summary="Rework the write step.", steps=_steps(changed_s2="Rip out and redo.")
        )
        await _seed_active_plan(factory_a, run_id, first_plan)
        assert (
            await _approve_revision(
                client, factory_a, run_id, incompatible, first_plan, note_id=7312
            )
        )["status"] == "applied"

        run = await get_run(factory_a, run_id)
        reuse = (run.evidence or {})["checkpoint_reuse_decision"]
        assert reuse["route"] == "fresh_attempt"  # explicit, never silent
        assert "S2" in reuse["route_reason"]
        checkpoint_entry = next(
            entry for entry in reuse["artifacts"] if entry["kind"] == "checkpoint"
        )
        assert checkpoint_entry["artifact_id"] == checkpoint_id
        assert checkpoint_entry["decision"] == "invalidate"

        # Worker restart; the SAME checkpoint-holding death — but the
        # required resume now routes through the recorded fresh attempt.
        await pe_db.dispose()
        factory_b = pe_db.worker_factory()
        service_b = make_service(factory_b, client, reader_of(native_client))
        await _kill_run(factory_b, run_id, "harness_infrastructure: worker_lost_after_vendor")
        await _retry(service_b, run_id, "pe7d-retry-2")

        run = await get_run(factory_b, run_id)
        assert run.status == "blocked"
        assert (run.status_reason or "").startswith("checkpoint_reuse_refused")
        # ZERO new dispatches: the silent reuse never left the boundary.
        assert len(native.dispatches()) == 1
        [refusal] = [body for body in native.comments() if "checkpoint_reuse_refused" in body]
        assert "fresh attempt" in refusal
        assert "/retry restart" in refusal  # the explicit way out
        assert native.unknown_paths() == []


# ----------------------------------------------------------------------
# PE-7e — pause/effect/checkpoint/resume are DISTINCT projection rows
# ----------------------------------------------------------------------


class TestPE7SeparateVisibility:
    @staticmethod
    def _projection_rows(rows) -> list[dict]:
        """The projection input rows the operator timeline reads: each
        command's CURRENT rung plus its append-only audit journal."""
        feed: list[dict] = []
        for row in rows:
            feed.append(
                {
                    "kind": row.kind,
                    "command_id": row.id,
                    "status": row.status,
                    "at": row.created_at.isoformat(),
                }
            )
            for entry in row.journal or []:
                feed.append({"kind": row.kind, "command_id": row.id, **entry})
        return feed

    async def _rows(self, factory, run_id: str, kinds: tuple[str, ...] | None = None):
        from forge.adaptive.mailbox_db import ControlCommandRow

        async with factory() as session:
            query = select(ControlCommandRow).where(ControlCommandRow.work_id == run_id)
            if kinds:
                query = query.where(ControlCommandRow.kind.in_(kinds))
            query = query.order_by(ControlCommandRow.sequence)
            return list((await session.execute(query)).scalars().all())

    async def test_pause_effect_checkpoint_and_resume_are_distinct_rows(
        self, pe_db, native, native_client, monkeypatch, tmp_path
    ):
        from forge.adaptive.mailbox_db import PostgresMailbox
        from forge.adaptive.operator_timeline import timeline_from_journal

        client, reader = native_client
        monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(tmp_path / "pe7-store"))
        monkeypatch.setenv("FORGE_LANE_CONTROL_SECRET", PE_LANE_SECRET)
        monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_DEADLINE", PE_LEGACY_DEADLINE)
        factory = pe_db.worker_factory()
        service = make_service(factory, client, reader)
        native.seed_issue(42, "Add a widget", "Make widgets real.")
        run_id = await start_run(service, 42)

        # 1. PAUSE RECEIVED — the operator's command lands as a durable row…
        checkpoint_id = await _pause_and_checkpoint(client, factory, run_id, tmp_path, note_id=7401)
        assert checkpoint_id

        # 2/3/4. …then the LANE's own durable ladder performs the transitions
        # the steering bridge performs: effect INTENDED, vendor took it,
        # application observed, checkpoint carries it. Each is its own row
        # + journal hop.
        mailbox = PostgresMailbox(factory)
        (pause_row,) = await self._rows(factory, run_id, kinds=("pause",))
        await mailbox.authorize(pause_row.id, {"server_authenticated_human": ("alice",)})
        await mailbox.dispatch(pause_row.id, current_plan_revision=1, current_execution_epoch=1)
        await mailbox.vendor_accepted(pause_row.id)
        await mailbox.observe(pause_row.id)
        await mailbox.checkpoint(pause_row.id)

        # 5. RESUMED — the operator's resume, recorded durably; no runner has
        # drained it yet.
        resumed = await _durable_control(factory).resume(run_id, "human:op", "pe7e-resume-1")
        assert resumed is True

        rows = await self._rows(factory, run_id)
        assert [row.kind for row in rows] == ["pause", "resume"]

        # The projection: request, authorization, effect intent, effect
        # observed and checkpoint committed are DISTINCT categories over the
        # pause's own rows — nothing collapsed into one "paused" word — and
        # the resume is its own REQUEST, not a success claim.
        entries = timeline_from_journal(self._projection_rows(rows))
        categories = [entry.category for entry in entries]
        assert set(categories) == {
            "request_received",
            "authorized",
            "effect_dispatched",
            "effect_observed",
            "checkpoint_committed",
        }
        resume_categories = {entry.category for entry in entries if entry.command_id == rows[1].id}
        assert resume_categories == {"request_received"}  # no premature resumed-success
        # The audit journal climbs in order — restoration and its evidence
        # are separate events.
        rungs = [entry["to"] for entry in rows[0].journal]
        assert rungs == [
            "received",
            "authorized",
            "dispatching",
            "vendor_accepted",
            "applied",
            "checkpointed",
        ]
        assert native.unknown_paths() == []
