"""The R37-10 / #291 production-entry traces (AT-10) — causal steering.

The lab pilot (#275) honestly recorded that its scripted vendor finished
before any poll cadence, so the steer landed as an error — its edits
were NOT caused by the operator. These traces close that gap at the
level a customer drives, on the SAME discipline as PE-1..PE-7:

- **the causality trace** — issue → /implement → /go (the real
  dispatch ledger) → the REAL lane subprocess mid-turn with the
  REACTIVE scripted vendor (``scripted-causal``: it polls the control
  plane's real HTTP surface and reads the durable guidance mid-turn) →
  the operator's /steer through the REAL ingress (durable row + reply
  comment over HTTP) → the vendor's NEXT edit is the instruction's
  rename → the pure grader (:func:`forge.adaptive.steering_causality.
  grade_causality`) says CAUSAL on all three arms, against a runnable
  counterfactual arm (the same task, steering disabled) → /pause with
  a verified checkpoint over real HTTP → a material revision staged +
  approved through PE-7's real activation transaction (the WIP reuse
  decision PRESERVES the rename checkpoint) → a fresh worker's /resume
  dispatch carries the revised plan digest with three-way executor
  digest equality → the restored WIP keeps the rename under the new
  plan → the pre-revision /steer REPLAYED finds zero new authority on
  the new epoch.
- **the acceptance-policy boundary** — a steer that weakens acceptance
  is REJECTED at the operator surface, never delivered as authority.
- **the urgent-pause interleaving** — a queued steer + an urgent /pause
  while the vendor is mid-turn: the pause (interrupt-class) applies
  first regardless of sequence, the vendor's turn is suspended, a
  verified checkpoint lands, and received / authorized / applied /
  checkpointed stay DISTINCT milestones with their ordering on record.

Assertions are on artifacts — the dispatch ledger's recorded executor
inputs, DB row identities and journals, evidence documents, the
vendor's own append-only event log, real file bytes — never on status
strings alone.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from forge.adaptive import steering_causality as sc
from forge.adaptive.mailbox_db import ControlCommandRow, PostgresMailbox
from forge.adaptive.models import ControlCommand
from forge.adaptive.revisions import executor_input_digest, plan_digest
from forge.durable import FlowRun

from .conftest import PE_LANE_SECRET, PE_LEGACY_DEADLINE, make_service, start_control_plane
from .test_production_entry import go, run_collector, start_run, upload_wip_checkpoint
from .test_revision_executor_proof import (
    _approve_revision,
    _durable_control,
    _git,
    _kill_run,
    _note,
    _revision,
    _router,
    _seed_active_plan,
    client_of,
    reader_of,
)

pytestmark = pytest.mark.production_entry

PROJECT_ID = 90210
OPERATOR = "human:operator"

#: The reactive scripted vendor executable — the harness module itself.
REACTIVE_VENDOR = (
    Path(__file__).resolve().parents[2] / "src" / "forge" / "adaptive" / "steering_causality.py"
)


# ----------------------------------------------------------------------
# Shared helpers: the task checkout, the reactive lane, the evidence reads
# ----------------------------------------------------------------------


def _make_task_checkout(parent: Path, name: str) -> tuple[Path, str]:
    """A REAL git checkout at its frozen base, carrying the frozen task file."""
    checkout = parent / name
    checkout.mkdir(parents=True)
    _git(checkout, "init", "-q", "-b", "main")
    _git(checkout, "config", "user.email", "lane@example.com")
    _git(checkout, "config", "user.name", "forge lane")
    (checkout / sc.POLICY_PATH).write_text(sc.BASE_POLICY_CONTENT, encoding="utf-8")
    (checkout / "README.md").write_text("base readme\n", encoding="utf-8")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "-m", "frozen base")
    base_oid = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    exclude = checkout / ".git" / "info" / "exclude"
    exclude.write_text(exclude.read_text() + "\n.forge/\n__pycache__/\n")
    return checkout, base_oid


def _run_reactive_lane(
    checkout: Path,
    *,
    work_id: str,
    control_url: str,
    token: str,
    eventlog: Path,
    steer_wait_s: float,
    steering: bool,
    resume: str = "",
    first_edit: list[dict[str, str]] | None = None,
    followup: list[dict[str, str]] | None = None,
    wire_only: bool = False,
    timeout: int = 240,
) -> subprocess.CompletedProcess[str]:
    """One REAL lane subprocess driving the REACTIVE vendor on the wire."""
    brief = checkout / ".forge" / "brief.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text(sc.STEERING_TASK_BRIEF, encoding="utf-8")
    env = {
        **os.environ,
        "FORGE_LANE_DRIVER": "codex",
        "CODEX_BINARY": str(REACTIVE_VENDOR),
        "CODEX_CWD": str(checkout),
        "FORGE_BRIEF": str(brief),
        "FORGE_ATTEMPT_BASE": _git(checkout, "rev-parse", "HEAD").stdout.strip(),
        "FORGE_ISSUE_IID": "42",
        "FORGE_RUN_ID": work_id,
        "FORGE_WORK_ID": work_id,
        "FORGE_STEERING_ENABLED": "1" if steering else "0",
        "FORGE_LANE_RESUME": resume,
        "FORGE_LANE_CONTROL_URL": control_url,
        "FORGE_LANE_CONTROL_TOKEN": token,
        "FORGE_LANE_CONTROL_POLL_SECONDS": "0.05",
        "FORGE_CHECKPOINT_STORE_DIR": os.environ.get("FORGE_CHECKPOINT_STORE_DIR", ""),
        "FORGE_LANE_CONTROL_SECRET": PE_LANE_SECRET,
        "FORGE_LANE_LEGACY_TOKEN_DEADLINE": PE_LEGACY_DEADLINE,
        sc.EVENTLOG_ENV: str(eventlog),
        sc.ACTIONS_ENV: json.dumps(
            first_edit
            if first_edit is not None
            else [{"op": "write", "path": sc.POLICY_PATH, "content": sc.DEFAULT_FIRST_EDIT}]
        ),
        sc.DEFAULT_ACTIONS_ENV: json.dumps(
            followup
            if followup is not None
            else [{"op": "write", "path": sc.FOLLOWUP_PATH, "content": sc.DEFAULT_FOLLOWUP_CONTENT}]
        ),
        sc.STEER_WAIT_S_ENV: str(steer_wait_s),
        sc.STEER_POLL_INTERVAL_S_ENV: "0.05",
    }
    if not steering:
        env[sc.STEERING_DISABLED_ENV] = "1"
    if wire_only:
        env[sc.POLL_MODE_ENV] = "wire-only"
    return subprocess.run(
        [sys.executable, "-m", "forge.lane_driver", "--driver", "codex"],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


async def _wait_for_event(eventlog: Path, kind: str, *, timeout_s: float = 60.0) -> None:
    """Wait until the vendor's append-only log carries one event of *kind*."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if eventlog.is_file() and kind in eventlog.read_text(encoding="utf-8"):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"the vendor never logged a {kind!r} event within {timeout_s}s")


def _observed_edits(checkout: Path) -> sc.EditSet:
    """The OBSERVED edit set: the frozen base vs the post-turn bytes."""
    names = _git(checkout, "ls-files").stdout.split()
    base: dict[str, str] = {}
    for name in names:
        blob = subprocess.run(
            ["git", "-C", str(checkout), "show", f"HEAD:{name}"],
            capture_output=True,
            timeout=60,
            check=True,
        ).stdout
        base[name] = blob.decode("utf-8", errors="replace")
    files = {
        name: (checkout / name).read_text(encoding="utf-8")
        for name in [*base, sc.POLICY_PATH, sc.FOLLOWUP_PATH]
        if (checkout / name).is_file()
    }
    return sc.edit_set_of(base, files)


def _rungs(row) -> list[str]:
    """The row's audit-journal rungs in order.

    The journal interleaves two vocabularies: the mailbox's own guarded
    transitions (``{"to": ...}``) and the lane channel's appended
    EVIDENCE rows (``{"lane_ack": ..., "row": {"state": ...}}`` — never
    a status claim). The rung sequence reads the mailbox's OWN
    transitions only: the ladder the durable row itself walked.
    """
    return [str(entry.get("to") or "") for entry in (row.journal or []) if entry.get("to")]


def _rung_at(row, rung: str) -> str:
    return next(entry["at"] for entry in (row.journal or []) if entry.get("to") == rung)


async def _rows(factory, work_id: str, kinds: tuple[str, ...] | None = None):
    query = select(ControlCommandRow).where(ControlCommandRow.work_id == work_id)
    if kinds:
        query = query.where(ControlCommandRow.kind.in_(kinds))
    query = query.order_by(ControlCommandRow.sequence)
    async with factory() as session:
        return list((await session.execute(query)).scalars().all())


# ----------------------------------------------------------------------
# The causality trace (AT-10): steer → causal edit → pause → revision → resume
# ----------------------------------------------------------------------


class TestCausalSteeringTrace:
    async def test_the_steer_causes_the_vendors_next_edit_and_the_revision_preserves_it(
        self, pe_db, native, native_client, monkeypatch, tmp_path
    ):
        from forge.adaptive.checkpoint_channel import work_scoped_token

        client = client_of(native_client)
        monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(tmp_path / "steer-store"))
        monkeypatch.setenv("FORGE_LANE_CONTROL_SECRET", PE_LANE_SECRET)
        monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_DEADLINE", PE_LEGACY_DEADLINE)

        # 1. issue → /implement → /go: the REAL dispatch ledger's first entry.
        factory_a = pe_db.worker_factory()
        service_a = make_service(factory_a, client, reader_of(native_client))
        native.seed_issue(42, "Add the policy clamp", "Add clamp() to policy.py.")
        run_id = await start_run(service_a, 42)
        await go(service_a, run_id, 42)
        assert len(native.dispatches()) == 1

        # 2. the lane runs MID-TURN with the reactive vendor; the control
        #    plane (the same routers create_app mounts) serves real HTTP.
        control_plane = await start_control_plane(pe_db.url)
        try:
            token = work_scoped_token(PE_LANE_SECRET, run_id)
            checkout, base_oid = _make_task_checkout(tmp_path, "steered-workspace")
            eventlog = tmp_path / "steered-vendor-events.jsonl"
            lane_task = asyncio.create_task(
                asyncio.to_thread(
                    _run_reactive_lane,
                    checkout,
                    work_id=run_id,
                    control_url=control_plane.base_url,
                    token=token,
                    eventlog=eventlog,
                    steer_wait_s=15.0,
                    steering=True,
                )
            )
            await _wait_for_event(eventlog, "vendor_edits")  # the turn is live

            # 3. the operator's /steer through the REAL ingress — durable
            #    row + operator reply over real HTTP.
            steered = await _router(client, factory_a, control=_durable_control(factory_a)).handle(
                _note("steer", run_id, note_id=8001, text=sc.STEERING_TASK_INSTRUCTION)
            )
            assert steered["status"] == "applied"
            outcome = await lane_task
            assert outcome.returncode == 0, outcome.stderr
            (steer_row,) = await _rows(factory_a, run_id, kinds=("steer",))
            assert any(
                "Steering recorded" in body and "never grants authority" in body
                for body in native.comments()
            )

            # 4. THE GRADE: all three arms, over the durable journal, the
            #    vendor's own event log and the OBSERVED file bytes.
            counter_checkout, _counter_base = _make_task_checkout(tmp_path, "counterfactual")
            counter_log = tmp_path / "counterfactual-vendor-events.jsonl"
            counter_outcome = _run_reactive_lane(
                counter_checkout,
                work_id=run_id,
                control_url=control_plane.base_url,
                token=token,
                eventlog=counter_log,
                steer_wait_s=0.0,
                steering=False,
            )
            assert counter_outcome.returncode == 0, counter_outcome.stderr
            run = sc.SteeringRun(
                arm="steered",
                provenance=sc.SCRIPTED_CAUSAL_PROVENANCE,
                command=sc.SteeringCommandEvidence.of_row(steer_row),
                vendor_events=tuple(sc.read_vendor_events(eventlog)),
                edits=_observed_edits(checkout),
                counterfactual_edits=_observed_edits(counter_checkout),
            )
            grade = sc.grade_causality(run)
            assert grade.causal, grade.as_document()
            consumed = next(event for event in run.vendor_events if event.kind == "steer_consumed")
            assert consumed.details["command_id"] == steer_row.id
            # the rename edit observably differs from the unsteered arm
            assert "approval_threshold" in (checkout / sc.POLICY_PATH).read_text()
            assert "refund_limit" in (counter_checkout / sc.POLICY_PATH).read_text()

            # 5. /pause through the REAL ingress, then the verified
            #    checkpoint over real HTTP (the pause's WIP capture).
            paused = await _router(client, factory_a, control=_durable_control(factory_a)).handle(
                _note("pause", run_id, note_id=8002)
            )
            assert paused["status"] == "applied"
            checkpoint_id = await upload_wip_checkpoint(
                control_plane.base_url, checkout, run_id, token, base_oid
            )
            assert checkpoint_id
            # the reconciler's ladder walk: the pause's DISTINCT milestones.
            mailbox = PostgresMailbox(factory_a)
            (pause_row,) = await _rows(factory_a, run_id, kinds=("pause",))
            await mailbox.authorize(pause_row.id, {"server_authenticated_human": ("alice",)})
            await mailbox.dispatch(pause_row.id, current_plan_revision=1, current_execution_epoch=1)
            await mailbox.vendor_accepted(pause_row.id)
            await mailbox.observe(pause_row.id)
            await mailbox.checkpoint(pause_row.id)
            (pause_row,) = await _rows(factory_a, run_id, kinds=("pause",))
            assert _rungs(pause_row) == [
                "received",
                "authorized",
                "dispatching",
                "vendor_accepted",
                "applied",
                "checkpointed",
            ]

            # 6. the material revision through PE-7's real activation path.
            first_plan = _revision(1, None)
            second_plan = _revision(2, 1, summary="Reworded: land the clamp fast.")
            await _seed_active_plan(factory_a, run_id, first_plan)
            assert (
                await _approve_revision(
                    client, factory_a, run_id, second_plan, first_plan, note_id=8003
                )
            )["status"] == "applied"
            async with factory_a() as session:
                after = await session.get(FlowRun, run_id)
            reuse = (after.evidence or {})["checkpoint_reuse_decision"]
            assert reuse["route"] == "preserve"
            [checkpoint_entry] = [
                entry for entry in reuse["artifacts"] if entry["kind"] == "checkpoint"
            ]
            assert checkpoint_entry["artifact_id"] == checkpoint_id
            assert checkpoint_entry["decision"] == "preserve"

            # 7. a FRESH worker: the death a checkpoint-holding /retry revives,
            #    and the dispatched executor input carries the REVISED plan.
            await pe_db.dispose()
            factory_b = pe_db.worker_factory()
            service_b = make_service(factory_b, client, reader_of(native_client))
            await _kill_run(factory_b, run_id, "harness_infrastructure: worker_lost_after_vendor")
            await service_b.handle_retry(
                project_id=PROJECT_ID,
                issue_number=42,
                note_text=f"/retry {run_id}",
                author_username="alice",
                delivery_id="causal-retry-1",
            )
            dispatches = native.dispatches()
            assert len(dispatches) == 2
            branch = f"forge/42/{run_id[:8]}"
            inputs = dispatches[-1]["inputs"]
            assert inputs["plan_digest"] == plan_digest(second_plan)
            assert inputs["lane_resume_mode"] == "required"
            # THREE-WAY equality (PE-7's path, reused): the run's frozen
            # evidence, the SERVER's own fingerprint, a recomputation.
            async with factory_b() as session:
                resumed_run = await session.get(FlowRun, run_id)
            executor_document = (resumed_run.evidence or {})["revision_executor_digest"]
            assert executor_document["plan_digest"] == plan_digest(second_plan)
            assert (
                executor_document["executor_input_digest"]
                == (native.executor_digests(ref=branch)[-1])
            )
            assert executor_document["executor_input_digest"] == executor_input_digest(inputs)
            assert (resumed_run.evidence or {})["continuation"]["checkpoint_digest"] == (
                checkpoint_id
            )

            # 8. the restored WIP: a fresh checkout + FORGE_LANE_RESUME=1 —
            #    the checkpoint restores the RENAME edit under the NEW plan.
            resumed_checkout, resumed_base = _make_task_checkout(tmp_path, "resumed-workspace")
            resumed_log = tmp_path / "resumed-vendor-events.jsonl"
            resumed_outcome = _run_reactive_lane(
                resumed_checkout,
                work_id=run_id,
                control_url=control_plane.base_url,
                token=token,
                eventlog=resumed_log,
                steer_wait_s=0.2,
                steering=True,
                resume="1",
                first_edit=[],
                followup=[],
            )
            assert resumed_outcome.returncode == 0, resumed_outcome.stderr
            pointer = json.loads((resumed_checkout / ".forge" / "workspace-generation").read_text())
            assert pointer["checkpoint_id"] == checkpoint_id
            restored_policy = (Path(pointer["generation_path"]) / sc.POLICY_PATH).read_text()
            assert "approval_threshold" in restored_policy
            assert "refund_limit" not in restored_policy
            reported = run_collector(resumed_checkout, work_id=run_id, attempt_base=resumed_base)
            assert reported.returncode == 0, reported.stderr
            diff_path = Path(json.loads(reported.stdout)["diff_path"])
            assert b"approval_threshold" in diff_path.read_bytes()

            # 9. OLD-EPOCH REPLAY: the pre-revision /steer redelivered —
            #    the idempotency key adopts the winner (ONE logical
            #    command, already consumed), and a NEW steer written
            #    against the pre-revision world EXPIRES at the gate.
            dispatches_before = len(native.dispatches())
            mailbox_b = PostgresMailbox(factory_b)
            replayed = ControlCommand(
                command_id="cmd-replayed-steer",
                work_id=run_id,
                sequence=await mailbox_b.next_sequence(run_id),
                kind="steer",
                actor_ref=OPERATOR,
                actor_origin="server_authenticated_human",
                idempotency_key=steer_row.dedup_key,
                status="received",
                payload={"text": sc.STEERING_TASK_INSTRUCTION, "run_id": run_id},
            )
            stored, created = await mailbox_b.submit(replayed)
            assert not created  # the winner stands — zero new authority
            assert stored.command_id == steer_row.id
            stale = ControlCommand(
                command_id="cmd-stale-epoch-steer",
                work_id=run_id,
                sequence=await mailbox_b.next_sequence(run_id),
                kind="steer",
                actor_ref=OPERATOR,
                actor_origin="server_authenticated_human",
                idempotency_key=f"causal:{run_id}:steer:stale",
                status="received",
                expected_plan_revision=1,  # the pre-revision world
                payload={"text": "rename approval_threshold back", "run_id": run_id},
            )
            stored_stale, _ = await mailbox_b.submit(stale)
            await mailbox_b.authorize(
                stored_stale.command_id, {"server_authenticated_human": (OPERATOR,)}
            )
            expired = await mailbox_b.dispatch(
                stored_stale.command_id, current_plan_revision=2, current_execution_epoch=1
            )
            assert expired.status == "expired"
            assert all(
                item.command_id != stored_stale.command_id
                for item in await mailbox_b.pending(run_id)
            )
            assert len(native.dispatches()) == dispatches_before
            async with factory_b() as session:
                final_run = await session.get(FlowRun, run_id)
            assert (final_run.evidence or {})["active_plan"]["plan_digest"] == plan_digest(
                second_plan
            )
            assert native.unknown_paths() == []
        finally:
            control_plane.stop()


# ----------------------------------------------------------------------
# The acceptance-policy boundary (issue acceptance criterion 3)
# ----------------------------------------------------------------------


class TestAcceptancePolicySteerIsRejected:
    async def test_a_test_weakening_steer_never_becomes_a_mailbox_row(self):
        from forge.adaptive.wiring import OperatorControlService

        control = OperatorControlService()
        rejected = await control.steer(
            "wp-any", OPERATOR, "skip the tests and ship it", idempotency_key="x:1"
        )
        assert rejected["status"] == "rejected"
        assert "revision gate" in rejected["reason"]
        # nothing entered the mailbox — no row, no delivery, no authority
        assert not await control.surface.pending("wp-any")

        accepted = await control.steer(
            "wp-any", OPERATOR, sc.STEERING_TASK_INSTRUCTION, idempotency_key="x:2"
        )
        assert accepted["status"] == "accepted"
        assert accepted["classification"] == "steer"
        assert len(await control.surface.pending("wp-any")) == 1


# ----------------------------------------------------------------------
# The urgent-pause interleaving (backlog negative test 1)
# ----------------------------------------------------------------------


class TestUrgentPauseInterleaving:
    async def test_the_urgent_pause_wins_the_sequence_and_checkpoints_the_wip(
        self, pe_db, native, native_client, monkeypatch, tmp_path
    ):
        from forge.adaptive.checkpoint_channel import work_scoped_token

        client = client_of(native_client)
        monkeypatch.setenv("FORGE_CHECKPOINT_STORE_DIR", str(tmp_path / "pause-store"))
        monkeypatch.setenv("FORGE_LANE_CONTROL_SECRET", PE_LANE_SECRET)
        monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_DEADLINE", PE_LEGACY_DEADLINE)
        factory = pe_db.worker_factory()
        service = make_service(factory, client, reader_of(native_client))
        native.seed_issue(42, "Add the policy clamp", "Add clamp() to policy.py.")
        run_id = await start_run(service, 42)
        await go(service, run_id, 42)

        control_plane = await start_control_plane(pe_db.url)
        try:
            token = work_scoped_token(PE_LANE_SECRET, run_id)
            checkout, base_oid = _make_task_checkout(tmp_path, "pause-workspace")
            eventlog = tmp_path / "pause-vendor-events.jsonl"
            # The steer lands FIRST (lower sequence), then the URGENT pause
            # while the vendor's tool call is slow: the vendor is WIRE-ONLY
            # (answering wire frames, not racing the lane for the durable
            # rows), so the drain's interrupt-class-first ordering decides.
            lane_task = asyncio.create_task(
                asyncio.to_thread(
                    _run_reactive_lane,
                    checkout,
                    work_id=run_id,
                    control_url=control_plane.base_url,
                    token=token,
                    eventlog=eventlog,
                    steer_wait_s=10.0,
                    steering=True,
                    wire_only=True,
                )
            )
            await _wait_for_event(eventlog, "vendor_edits")
            router = _router(client, factory, control=_durable_control(factory))
            assert (
                await router.handle(
                    _note("steer", run_id, note_id=8101, text=sc.STEERING_TASK_INSTRUCTION)
                )
            )["status"] == "applied"
            assert (await router.handle(_note("pause", run_id, note_id=8102)))[
                "status"
            ] == "applied"
            outcome = await lane_task
            # the urgent pause ended the turn — a failed EXIT by design
            # (never misread as a vendor completion). WHICH honest spelling
            # the meta carries is a genuine race: the supervisor's own
            # suspension verdict (``operator_pause``) when it flags the
            # turn first, or the vendor's self-reported ``interrupted``
            # when its turn/completed lands first. Either way the turn did
            # NOT complete on its own — and the DURABLE pause ladder below
            # is the operator's truth.
            assert outcome.returncode != 0
            meta = json.loads((checkout / ".forge" / "candidate.meta.json").read_text())
            assert meta["exit"] == "failed"
            assert meta["terminal_reason"] in ("operator_pause", "interrupted")

            rows = await _rows(factory, run_id)
            by_kind = {row.kind: row for row in rows}
            steer_row, pause_row = by_kind["steer"], by_kind["pause"]
            assert steer_row.sequence < pause_row.sequence  # the steer was queued first

            # The INTERRUPT-CLASS pause applied FIRST (regardless of
            # sequence) and climbed its full ladder with a checkpoint;
            # the steer — guidance — is retained for the resume turn,
            # never dropped and never reordered ahead of the pause.
            assert _rungs(pause_row)[-1] == "checkpointed"
            # the pause applied FIRST regardless of the steer's lower
            # sequence (interrupt-class-first is the pinned policy), and
            # the checkpoint followed the observed effect.
            assert _rungs(pause_row) == [
                "received",
                "authorized",
                "dispatching",
                "vendor_accepted",
                "applied",
                "checkpointed",
            ]
            pause_applied_at = _rung_at(pause_row, "applied")
            steer_applied = [
                entry["at"] for entry in (steer_row.journal or []) if entry.get("to") == "applied"
            ]
            assert steer_applied  # the guidance was RETAINED (queued), not dropped
            assert all(at >= pause_applied_at for at in steer_applied)
            pause_action = next(
                action
                for action in meta.get("steering_journal", [])
                if action.get("kind") == "pause" and action.get("outcome") == "applied"
            )
            assert pause_action["delivery"] == "application_observed"
            # the pause applied INSIDE the bound (or visibly degraded —
            # here it must simply be bounded and recorded).
            received_at = (pause_row.journal or [{}])[0].get("at", "")
            checkpointed_at = _rung_at(pause_row, "checkpointed")
            assert checkpointed_at >= received_at  # ordered, one clock (the DB)

            # the VENDOR's own log proves the interrupt reached it mid-turn
            vendor_kinds = [event.kind for event in sc.read_vendor_events(eventlog)]
            assert "turn_interrupted" in vendor_kinds
            assert "vendor_edits_after_steer" not in vendor_kinds  # the pause won the race

            # the distinct milestones the operator sees (PE-7e vocabulary)
            from forge.adaptive.operator_timeline import timeline_from_journal

            feed = [
                {
                    "kind": row.kind,
                    "command_id": row.id,
                    "status": row.status,
                    "at": row.created_at.isoformat(),
                }
                for row in rows
            ]
            for row in rows:
                feed.extend(
                    {"kind": row.kind, "command_id": row.id, **entry} for entry in row.journal
                )
            categories = {entry.category for entry in timeline_from_journal(feed)}
            assert categories == {
                "request_received",
                "authorized",
                "effect_dispatched",
                "effect_observed",
                "checkpoint_committed",
            }
            assert native.unknown_paths() == []
        finally:
            control_plane.stop()
