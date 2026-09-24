"""R37-01 (issue #282) production-entry proofs — the continuation decision
is bound to the exact recovery EVENT and source ATTEMPT.

AT-01 (the defect's exact shape, through the REAL retry entry): attempt 1
creates checkpoint A, retry E1 approves continuing from it; attempt 2
creates checkpoint B and dies with EQUAL objective booleans; retry E2 (a
NEW delivery id, a LATER source attempt) must bind checkpoint B and
attempt 2 — never attempt 1's decision, however equal the digest is. E1
redelivered afterwards changes nothing: its frozen decision does not
become the current authority and no new dispatch happens.

AT-02: a worker killed between E2's decision commit and its dispatch; a
FRESH worker (its own engine and session factory over the same real
PostgreSQL) recovers the stranded attempt and RECONSTRUCTS the identical
decision BY ID — the same ``decision_id``, the same ``decided_at``, the
same pinned checkpoint — even after a NEWER checkpoint landed in between.
E1 redelivered on the new worker still changes nothing.

The mutation arm reverts the matcher to digest-only (the pre-R37-01
behavior) and asserts the AT-01 outcome BREAKS — the defective signature
(attempt 1's decision grafted onto E2) must appear.

Execution boundary: real PostgreSQL (``FORGE_PG_TEST_URL`` — a disposable
database), the REAL ``GitHubRunService`` retry entry whose provider writes
travel REAL HTTP from the REAL client to the RECORDING fake native server
(a separate process), and the real checkpoint repository in postgres mode.
``matching_decision`` is never called directly in the positive arms.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from forge.durable import FlowRun

from .conftest import make_service

pytestmark = [
    pytest.mark.production_entry,
    pytest.mark.skipif(
        not os.environ.get("FORGE_PG_TEST_URL"),
        reason=(
            "FORGE_PG_TEST_URL not set — the R37-01 identity proofs run only "
            "against a disposable real Postgres"
        ),
    ),
]

PROJECT_ID = 90210
ISSUE = 42
DEATH_PLAIN_TIMEOUT = "harness_infrastructure: harness_timeout"


# ----------------------------------------------------------------------
# Shared helpers (real artifacts only)
# ----------------------------------------------------------------------


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _checkpoint(work_id: str, files: dict[str, bytes], sequence: int) -> tuple[bytes, dict, str]:
    """A minimal ``forge.wip.manifest/2`` payload the store verifies."""
    manifest = json.dumps(
        {
            "schema": "forge.wip.manifest/2",
            "work_id": work_id,
            "sequence": sequence,
            "source_oids": {"attempt_base": "e" * 40},
            "files": {
                name: {"digest": _digest(data), "mode": 0o644, "role": "new"}
                for name, data in sorted(files.items())
            },
            "deletions": [],
        }
    ).encode()
    blobs = {entry["digest"]: files[name] for name, entry in json.loads(manifest)["files"].items()}
    return manifest, blobs, _digest(manifest)


def _repository(factory, root: Path):
    from forge.adaptive.checkpoint_repository import PostgresCheckpointRepository

    return PostgresCheckpointRepository(root, factory)


async def _upload(repository, run_id: str, files: dict[str, bytes], sequence: int) -> str:
    manifest, blobs, checkpoint_id = _checkpoint(run_id, files, sequence)
    await repository.put(run_id, checkpoint_id, manifest, blobs)
    return checkpoint_id


async def _get_run(factory, run_id: str) -> FlowRun:
    async with factory() as session:
        return await session.get(FlowRun, run_id)


async def _die(factory, run_id: str) -> None:
    """The attempt dies terminally (the reconciler's recorded shape)."""
    async with factory() as session:
        run = await session.get(FlowRun, run_id)
        run.status = "failed"
        run.status_reason = DEATH_PLAIN_TIMEOUT
        run.candidate_shas = []
        await session.commit()


async def _retry(service, run_id: str, delivery_id: str) -> None:
    await service.handle_retry(
        project_id=PROJECT_ID,
        issue_number=ISSUE,
        note_text=f"/retry {run_id}",
        author_username="alice",
        delivery_id=delivery_id,
    )


def _branch_dispatches(native, run_id: str) -> list[dict]:
    branch = f"forge/{ISSUE}/{run_id[:8]}"
    return [entry["inputs"] for entry in native.dispatches() if entry["ref"] == branch]


def _doc(run: FlowRun) -> dict:
    return dict((run.evidence or {}).get("continuation") or {})


async def _start_and_dispatch_first_attempt(native, client, reader, factory) -> str:
    service = make_service(factory, client, reader)
    run_id = await service.start_run(
        project_id=PROJECT_ID,
        issue_number=ISSUE,
        issue_title="Add the widget",
        issue_description="Make the widget real.",
        author_username="alice",
    )
    await service.handle_go(
        project_id=PROJECT_ID,
        issue_number=ISSUE,
        note_text=f"@forge /go {run_id}",
        author_username="alice",
    )
    assert len(native.dispatches()) == 1  # the initial dispatch really landed
    return run_id


class _PgLab:
    """The postgres-mode checkpoint authority wiring every trace shares."""

    def __init__(self, factory, root: Path, monkeypatch) -> None:
        import forge.api_checkpoint_channel as api_channel

        monkeypatch.setenv(api_channel.DURABILITY_ENV, "postgres")
        monkeypatch.setenv(api_channel.CHECKPOINT_STORE_DIR_ENV, str(root))
        self.repository = _repository(factory, root)


# ----------------------------------------------------------------------
# AT-01 — two distinct recovery events, two attempts, two checkpoints
# ----------------------------------------------------------------------


class TestAT01TwoEventsTwoAttemptsTwoCheckpoints:
    async def test_e2_binds_attempt_2_and_checkpoint_b_and_e1_replay_changes_nothing(
        self, pe_db, native, native_client, tmp_path, monkeypatch
    ):
        """E1 binds CP-A/attempt 1; E2 — with EQUAL objective booleans —
        binds CP-B/attempt 2; dispatch, pin, envelope and the decision
        record agree; E1 redelivered afterwards changes nothing."""
        client, reader = native_client
        native.seed_issue(ISSUE, "Add the widget", "Make the widget real.")
        factory = pe_db.worker_factory()
        lab = _PgLab(factory, tmp_path / "authority-store", monkeypatch)
        service = make_service(factory, client, reader)
        run_id = await _start_and_dispatch_first_attempt(native, client, reader, factory)

        # Attempt 1 creates checkpoint A and dies.
        checkpoint_a = await _upload(lab.repository, run_id, {"a.txt": b"attempt-1 wip\n"}, 1)
        await _die(factory, run_id)

        # E1 — the operator's first retry — executes its continuation.
        await _retry(service, run_id, "at01-e1")
        assert len(native.dispatches()) == 2
        doc1 = _doc(await _get_run(factory, run_id))
        assert doc1["mode_selected"] == "required"
        assert doc1["checkpoint_digest"] == checkpoint_a
        assert doc1["event"] == {"source_attempt": 0, "native_command_id": "at01-e1"}
        assert doc1["continuation_decision_id"] == doc1["decision_id"]
        decision1 = doc1["decision_id"]
        pins = {(pin["checkpoint_id"], pin["reason"]) for pin in await lab.repository.pins(run_id)}
        assert pins == {(checkpoint_a, f"decision:{decision1}")}

        # Attempt 2 creates checkpoint B and dies with EQUAL booleans: the
        # plain-timeout reason carries no vendor markers, a checkpoint is
        # held, no candidate, no discard — the digest is UNCHANGED.
        checkpoint_b = await _upload(lab.repository, run_id, {"a.txt": b"attempt-2 wip\n"}, 2)
        await _die(factory, run_id)

        # E2 — a NEW delivery for a LATER attempt.
        await _retry(service, run_id, "at01-e2")
        assert len(native.dispatches()) == 3  # exactly one NEW native start
        run = await _get_run(factory, run_id)
        doc2 = _doc(run)
        decision2 = doc2["decision_id"]
        assert decision2 != decision1
        assert doc2["event"] == {"source_attempt": 1, "native_command_id": "at01-e2"}
        assert doc2["checkpoint_digest"] == checkpoint_b  # B's, never A's
        assert doc2["continuation_decision_id"] == decision2
        # BOTH decisions survive on the record; the old one never became E2's.
        assert set(doc2["decisions"]) == {decision1, decision2}
        assert doc2["decisions"][decision1]["checkpoint_digest"] == checkpoint_a
        # The dispatch consumed E2's decision BY identity: the recorded
        # inputs carry the required contract and the composed envelope pins
        # checkpoint B — the bytes the decision approved.
        (resume_inputs,) = _branch_dispatches(native, run_id)[-1:]
        assert resume_inputs["lane_resume_mode"] == "required"
        envelope = dict((run.evidence or {}).get("attempt_start") or {})
        assert envelope["continuation_ref_digest"] == checkpoint_b
        # One pin per decision: B's pin landed; A's survives audit.
        pins = {(pin["checkpoint_id"], pin["reason"]) for pin in await lab.repository.pins(run_id)}
        assert pins == {
            (checkpoint_a, f"decision:{decision1}"),
            (checkpoint_b, f"decision:{decision2}"),
        }

        # E1 REDELIVERED after E2 activated: a no-op — the frozen decision
        # does not become the current authority, ZERO new dispatches, no
        # change to E2's WIP (the AT-02 half of the redelivery arm).
        await _retry(service, run_id, "at01-e1")
        assert len(native.dispatches()) == 3
        replay_run = await _get_run(factory, run_id)
        replay_doc = _doc(replay_run)
        assert replay_doc["decision_id"] == decision2  # E2 stays the authority
        assert replay_doc["checkpoint_digest"] == checkpoint_b
        assert replay_doc["continuation_decision_id"] == decision2
        assert replay_run.status == "waiting_harness"
        assert {
            (pin["checkpoint_id"], pin["reason"]) for pin in await lab.repository.pins(run_id)
        } == pins  # unchanged
        # The REAL client never fell off the modeled API surface.
        assert native.unknown_paths() == []


# ----------------------------------------------------------------------
# AT-02 — restart between decision commit and dispatch
# ----------------------------------------------------------------------


class TestAT02RestartBetweenCommitAndDispatch:
    async def test_the_new_worker_reconstructs_the_identical_decision_by_id(
        self, pe_db, native, native_client, tmp_path, monkeypatch
    ):
        """The worker dies between E2's decision commit and its dispatch; a
        FRESH worker recovers the stranded attempt exactly once and the
        dispatch pins the checkpoint E2's decision approved — even after a
        NEWER checkpoint landed in between. E1 redelivered still changes
        nothing."""
        import forge.runs.github_service as gh_module

        client, reader = native_client
        native.seed_issue(ISSUE, "Add the widget", "Make the widget real.")
        factory_a = pe_db.worker_factory()
        lab = _PgLab(factory_a, tmp_path / "authority-store", monkeypatch)
        service_a = make_service(factory_a, client, reader)
        run_id = await _start_and_dispatch_first_attempt(native, client, reader, factory_a)

        # Attempt 1 → checkpoint A → E1 executes.
        checkpoint_a = await _upload(lab.repository, run_id, {"a.txt": b"attempt-1 wip\n"}, 1)
        await _die(factory_a, run_id)
        await _retry(service_a, run_id, "at02-e1")
        assert len(native.dispatches()) == 2

        # Attempt 2 → checkpoint B → dies with equal booleans.
        checkpoint_b = await _upload(lab.repository, run_id, {"a.txt": b"attempt-2 wip\n"}, 2)
        await _die(factory_a, run_id)

        # The worker is killed BETWEEN E2's decision commit and its
        # dispatch claim: the attempt row strands ``pending`` with the
        # decision durably committed.
        async def _killed_before_dispatch(session, action_id, **_: Any) -> bool:
            raise RuntimeError("worker killed between decision commit and dispatch")

        original_claim = gh_module.claim_attempt_dispatch
        monkeypatch.setattr(gh_module, "claim_attempt_dispatch", _killed_before_dispatch)
        try:
            with pytest.raises(RuntimeError, match="killed between decision commit"):
                await _retry(service_a, run_id, "at02-e2")
        finally:
            # Restore ONLY this patch (the lab's env wiring must survive).
            monkeypatch.setattr(gh_module, "claim_attempt_dispatch", original_claim)
        stranded = _doc(await _get_run(factory_a, run_id))
        decision2 = stranded["decision_id"]
        assert stranded["event"] == {"source_attempt": 1, "native_command_id": "at02-e2"}
        assert stranded["checkpoint_digest"] == checkpoint_b
        assert len(native.dispatches()) == 2  # nothing was dispatched

        # A NEWER checkpoint lands while the dispatch is stranded.
        checkpoint_c = await _upload(lab.repository, run_id, {"a.txt": b"attempt-3 wip\n"}, 3)

        # The RESTARTED worker: a fresh engine/session factory/service over
        # the same PostgreSQL (and the same recording native server).
        factory_b = pe_db.worker_factory()
        service_b = make_service(factory_b, client, reader)

        recovered = await service_b.evaluate_revival_recovery(
            now=datetime.now(timezone.utc) + timedelta(seconds=600)
        )
        assert recovered == 1  # the stranded intent completed exactly once
        assert len(native.dispatches()) == 3
        run = await _get_run(factory_b, run_id)
        doc = _doc(run)
        assert doc["decision_id"] == decision2  # reconstructed BY ID
        assert doc["decided_at"] == stranded["decided_at"]  # the SAME decision
        assert doc["event"] == {"source_attempt": 1, "native_command_id": "at02-e2"}
        assert doc["checkpoint_digest"] == checkpoint_b  # frozen — NOT checkpoint C
        assert doc["continuation_decision_id"] == decision2
        (resume_inputs,) = _branch_dispatches(native, run_id)[-1:]
        assert resume_inputs["lane_resume_mode"] == "required"
        envelope = dict((run.evidence or {}).get("attempt_start") or {})
        assert envelope["continuation_ref_digest"] == checkpoint_b  # never C
        # The decision owners of both pinned checkpoints are decisions; the
        # NEWER checkpoint (C) was never pinned by any decision.
        pins = {(pin["checkpoint_id"], pin["reason"]) for pin in await lab.repository.pins(run_id)}
        assert checkpoint_c not in {entry[0] for entry in pins}
        assert (checkpoint_b, f"decision:{decision2}") in pins
        assert any(entry[0] == checkpoint_a and entry[1].startswith("decision:") for entry in pins)

        # A second recovery pass re-drives NOTHING (exactly once).
        assert (
            await service_b.evaluate_revival_recovery(
                now=datetime.now(timezone.utc) + timedelta(seconds=1200)
            )
        ) == 0
        assert len(native.dispatches()) == 3

        # E1 redelivered on the RESTARTED worker: still a no-op.
        await _retry(service_b, run_id, "at02-e1")
        assert len(native.dispatches()) == 3
        replay_doc = _doc(await _get_run(factory_b, run_id))
        assert replay_doc["decision_id"] == decision2
        assert replay_doc["checkpoint_digest"] == checkpoint_b
        assert native.unknown_paths() == []


# ----------------------------------------------------------------------
# The mutation arm — digest-only matching must restore the defect
# ----------------------------------------------------------------------


class TestMutationDigestOnlyMatchingFails:
    async def test_reverting_to_digest_only_matching_restores_the_defect(
        self, pe_db, native, native_client, tmp_path, monkeypatch
    ):
        """The mutant this issue kills: reuse keyed on the objective digest
        ALONE (the pre-R37-01 ``matching_decision``). Under it, E2 — equal
        booleans — RECEIVES attempt 1's decision and checkpoint A: exactly
        what the AT-01 assertions refuse. If this test ever FAILS, the
        mutation no longer reproduces the defect; if AT-01 fails while this
        passes, the fix regressed."""
        from forge.adaptive import continuation as continuation_module
        from forge.adaptive.continuation import (
            ContinuationDecision,
            ContinuationMode,
        )
        from dataclasses import replace

        def _digest_only_matching(document, evidence):
            """The pre-R37-01 matcher (reverted): digest equality alone."""
            if not isinstance(document, dict):
                return None
            try:
                if str(document.get("evidence_digest") or "") != evidence.digest():
                    return None
                mode = ContinuationMode(str(document.get("mode") or ""))
                reason = str(document.get("reason") or "")
                decided_at = str(document.get("decided_at") or "")
                if not reason or not decided_at:
                    return None
            except ValueError:
                return None

            def _graft(key, current):
                value = document.get(key)
                return current if value is None else value

            return ContinuationDecision(
                mode=mode,
                reason=reason,
                evidence=replace(
                    evidence,
                    native_command_id=_graft("native_command_id", evidence.native_command_id),
                    source_attempt=_graft("source_attempt", evidence.source_attempt),
                    native_start_verdict=_graft(
                        "native_start_verdict", evidence.native_start_verdict
                    ),
                    discard_authorized_by=_graft(
                        "discard_authorized_by", evidence.discard_authorized_by
                    ),
                    checkpoint_digest=_graft("checkpoint_digest", evidence.checkpoint_digest),
                ),
                decided_at=decided_at,
                reused=True,
            )

        client, reader = native_client
        native.seed_issue(ISSUE, "Add the widget", "Make the widget real.")
        factory = pe_db.worker_factory()
        lab = _PgLab(factory, tmp_path / "authority-store", monkeypatch)
        service = make_service(factory, client, reader)
        run_id = await _start_and_dispatch_first_attempt(native, client, reader, factory)

        checkpoint_a = await _upload(lab.repository, run_id, {"a.txt": b"attempt-1 wip\n"}, 1)
        await _die(factory, run_id)
        await _retry(service, run_id, "mut-e1")
        doc1 = _doc(await _get_run(factory, run_id))
        assert doc1["checkpoint_digest"] == checkpoint_a

        checkpoint_b = await _upload(lab.repository, run_id, {"a.txt": b"attempt-2 wip\n"}, 2)
        await _die(factory, run_id)

        # THE MUTATION: revert matching to digest-only.
        monkeypatch.setattr(continuation_module, "matching_decision", _digest_only_matching)
        await _retry(service, run_id, "mut-e2")

        doc2 = _doc(await _get_run(factory, run_id))
        mutant_reused_the_old_decision = (
            doc2["checkpoint_digest"] == checkpoint_a  # CP-A grafted onto E2
            and doc2["decided_at"] == doc1["decided_at"]
        )
        assert mutant_reused_the_old_decision, (
            "the digest-only mutation no longer reproduces the R37-01 defect — "
            "if AT-01 also passes, this mutation arm lost its teeth"
        )
        # And the checkpoint B the operator actually selected is NOT what
        # the current authority names — the defect, demonstrably.
        assert doc2["checkpoint_digest"] != checkpoint_b
