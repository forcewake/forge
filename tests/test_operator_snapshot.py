"""The authorized snapshot reader (R36-15, R37-02/R37-03) — scope,
coverage, binding.

``OperatorSnapshotReader`` assembles the projection inputs from durable
rows under ONE canonical subject scope: every run query filters by the
authorized :class:`~forge.adaptive.operator_snapshot.CanonicalSubject`
set (provider family + connection + native id — display names never
widen it), an unqueried source is never an empty success (coverage
``unknown``), ``verified_ready`` binds to the CURRENT candidate through
the run's verification evidence, ``safely_paused`` stands on real
checkpoint/fence rows, a resume's activation attaches only to the
checkpoint its command NAMED, each attempt keeps its OWN generation,
the assembly is fenced by a source version, and the whole read is
side-effect free.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive.admission import ExecutionLease
from forge.adaptive.checkpoint_repository import CheckpointRepositoryUnavailable
from forge.adaptive.mailbox_db import ControlCommandDeliveryRow, ControlCommandRow
from forge.adaptive.operator_snapshot import (
    CanonicalSubject,
    OperatorSnapshotReader,
)
from forge.adaptive.pause_fence import PauseFenceRow
from forge.durable.models import ActionLog, FlowRun, GateApproval, PublicationIntent
from forge.models.base import Base

NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
REPO_A = "owner/alpha"
REPO_B = "owner/beta"
RUN_A = "a" * 32
RUN_B = "b" * 32

#: The canonical subjects the plain fixtures map to: GitHub rows that
#: record no connection share the unrecorded-connection marker ("-").
SUBJECT_A = CanonicalSubject(provider_family="github", connection="", native_id=REPO_A)
SUBJECT_B = CanonicalSubject(provider_family="github", connection="", native_id=REPO_B)


def _foreign_subject() -> CanonicalSubject:
    """A third subject in the same family — declarable in a scope but
    matching nothing seeded (the negative space of a grant)."""
    return CanonicalSubject(provider_family="github", connection="", native_id="owner/gamma")


class RecordingRepository:
    """The injected checkpoint authority — recording every call so tests
    can pin the read-only charter (reads happen, writes never do)."""

    def __init__(self, entry: dict | None = None, *, unavailable: bool = False) -> None:
        self.entry_document = entry
        self.unavailable = unavailable
        self.calls: list[tuple[str, str]] = []

    async def entry(self, work_id: str) -> dict | None:
        self.calls.append(("entry", work_id))
        if self.unavailable:
            raise CheckpointRepositoryUnavailable("authority down")
        return dict(self.entry_document) if self.entry_document else None

    async def put(self, work_id: str, checkpoint_id: str, manifest: bytes, blobs: dict) -> None:
        self.calls.append(("put", work_id))

    async def read(self, work_id: str):
        self.calls.append(("read", work_id))
        return None

    async def lookup_outcome(self, work_id: str):
        self.calls.append(("lookup_outcome", work_id))

    async def authority(self) -> str:
        return "recording"

    async def pin(self, work_id: str, checkpoint_id: str, reason: str = "") -> bool:
        self.calls.append(("pin", work_id))
        return False

    async def unpin(self, work_id: str, checkpoint_id: str, reason: str | None = None) -> int:
        self.calls.append(("unpin", work_id))
        return 0

    async def pins(self, work_id: str | None = None) -> list[dict]:
        self.calls.append(("pins", work_id or ""))
        return []


@pytest.fixture()
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _run(run_id: str, repo: str, **over) -> FlowRun:
    values: dict = {
        "id": run_id,
        "project_id": 1,
        "provider": "github",
        "github_repo_full_name": repo,
        "status": "validating",
        "base_sha": "b" * 40,
        "candidate_shas": ["c" * 40],
        "plan_digest": "p" * 64,
        "evidence": {},
        "created_at": NOW - timedelta(hours=3),
        "updated_at": NOW - timedelta(minutes=10),
    }
    values.update(over)
    return FlowRun(**values)


def _revival(run_id: str, status: str, *, attempt: int = 1, **over) -> ActionLog:
    values: dict = {
        "flow_run_id": run_id,
        "action_kind": "retry_requested",
        "status": status,
        "retryability": "transient_infrastructure",
        "dispatch_state": "pending",
        "created_at": NOW - timedelta(minutes=30 + attempt),
    }
    values.update(over)
    return ActionLog(**values)


def _command(
    run_id: str,
    seq: int,
    kind: str,
    status: str,
    *,
    applied_at: datetime | None = None,
    checkpoint_ref: str | None = None,
    **over,
) -> ControlCommandRow:
    payload: dict = {"run_id": run_id}
    if checkpoint_ref is not None:
        payload["checkpoint_ref"] = checkpoint_ref
    values: dict = {
        "id": f"cmd-{run_id[:6]}-{seq}",
        "work_id": run_id,
        "run_id": run_id,
        "kind": kind,
        "payload": payload,
        "status": status,
        "sequence": seq,
        "dedup_key": f"dedup-{run_id[:6]}-{seq}",
        "actor_ref": "human:op",
        "actor_origin": "server_authenticated_human",
        "created_at": NOW - timedelta(minutes=25),
        "applied_at": applied_at,
    }
    values.update(over)
    return ControlCommandRow(**values)


def _intent(run_id: str, status: str = "committed", **over) -> PublicationIntent:
    values: dict = {
        "run_id": run_id,
        "provider": "github",
        "repo": REPO_A,
        "operation": "commit",
        "target_ref": "refs/heads/forge/run",
        "idempotency_scope": "cycle-1",
        "operation_key": f"op-{run_id[:6]}-1",
        "status": status,
        "created_at": NOW - timedelta(minutes=40),
        "updated_at": NOW - timedelta(minutes=20),
    }
    values.update(over)
    return PublicationIntent(**values)


def _checkpoint_entry(**over) -> dict:
    entry: dict = {
        "checkpoint_id": "e" * 64,
        "sequence": 3,
        "files": 2,
        "uploaded_at": (NOW - timedelta(minutes=15)).isoformat(),
    }
    entry.update(over)
    return entry


async def _seed(session_factory, *rows) -> None:
    async with session_factory() as session:
        session.add_all(rows)
        await session.commit()


def _reader(session_factory, repository=None) -> OperatorSnapshotReader:
    return OperatorSnapshotReader(
        session_factory, checkpoint_repository=repository, clock=lambda: NOW
    )


# ---------------------------------------------------------------------------
# Subject-scope enforcement — a token scoped to repo A never sees repo B
# ---------------------------------------------------------------------------


async def test_list_returns_only_runs_inside_the_authorized_scope(session_factory):
    await _seed(session_factory, _run(RUN_A, REPO_A), _run(RUN_B, REPO_B))

    page = await _reader(session_factory).list_snapshots([SUBJECT_A])
    snapshots = page.snapshots

    assert [snapshot.run_id for snapshot in snapshots] == [RUN_A]
    assert all(snapshot.subject == REPO_A for snapshot in snapshots)


async def test_detail_for_a_foreign_repo_run_is_none(session_factory):
    await _seed(session_factory, _run(RUN_A, REPO_A), _run(RUN_B, REPO_B))
    reader = _reader(session_factory)

    assert await reader.snapshot(RUN_B, [SUBJECT_A]) is None
    assert await reader.snapshot(RUN_B, [SUBJECT_A, _foreign_subject()]) is None
    # the same run IS readable under its own scope
    snapshot = await reader.snapshot(RUN_B, [SUBJECT_B])
    assert snapshot is not None and snapshot.run_id == RUN_B


async def test_an_empty_scope_sees_nothing(session_factory):
    await _seed(session_factory, _run(RUN_A, REPO_A))

    assert (await _reader(session_factory).list_snapshots([])).snapshots == ()
    assert await _reader(session_factory).snapshot(RUN_A, []) is None


async def test_the_bundle_path_is_scoped_too(session_factory):
    """The support bundle builds from the SAME scoped snapshot — a B run
    under an A scope never reaches SupportBundle at all."""
    from forge.adaptive.support_bundle import SupportBundle

    await _seed(session_factory, _run(RUN_A, REPO_A), _run(RUN_B, REPO_B))
    reader = _reader(session_factory)

    snapshot = await reader.snapshot(RUN_A, [SUBJECT_A])
    assert snapshot is not None
    bundle = SupportBundle.build(RUN_A, snapshot.rows, now=NOW)
    assert bundle.run_id == RUN_A

    assert await reader.snapshot(RUN_B, [SUBJECT_A]) is None


# ---------------------------------------------------------------------------
# Coverage honesty — missing vs unknown, never an invented empty success
# ---------------------------------------------------------------------------


async def test_unqueried_sources_read_unknown_not_missing(session_factory):
    """No checkpoint repository injected → checkpoints are UNKNOWN (the
    authority was never consulted), and questions — which have no durable
    authority at all — are unknown by construction."""
    await _seed(session_factory, _run(RUN_A, REPO_A))

    snapshot = await _reader(session_factory, repository=None).snapshot(RUN_A, [SUBJECT_A])

    assert snapshot is not None
    assert snapshot.source_coverage["checkpoints"] == "unknown"
    assert snapshot.source_coverage["questions"] == "unknown"
    assert "checkpoints" not in snapshot.rows


async def test_an_unavailable_checkpoint_authority_is_unknown_not_missing(session_factory):
    """R36-03's typed lesson at the reader: an outage is not an absence —
    the unavailable authority leaves the section UNKNOWN, and no empty
    checkpoint list is reported as success."""
    await _seed(session_factory, _run(RUN_A, REPO_A))
    repository = RecordingRepository(unavailable=True)

    snapshot = await _reader(session_factory, repository=repository).snapshot(RUN_A, [SUBJECT_A])

    assert snapshot is not None
    assert snapshot.source_coverage["checkpoints"] == "unknown"
    assert "checkpoints" not in snapshot.rows


async def test_queried_and_empty_reads_missing_with_the_section_present(session_factory):
    """A section the authority DID observe empty is ``missing`` — explicit,
    and distinguishable from ``unknown`` by the coverage map alone."""
    await _seed(
        session_factory,
        _run(RUN_A, REPO_A),
        _command(RUN_A, 1, "pause", "checkpointed"),  # commands observed non-empty
    )
    repository = RecordingRepository(entry=None)

    snapshot = await _reader(session_factory, repository=repository).snapshot(RUN_A, [SUBJECT_A])

    assert snapshot is not None
    assert snapshot.source_coverage["commands"] == "present"
    assert snapshot.source_coverage["deliveries"] == "missing"
    assert snapshot.source_coverage["attempts"] == "missing"
    assert snapshot.source_coverage["publications"] == "missing"
    assert snapshot.source_coverage["approvals"] == "missing"
    assert snapshot.source_coverage["verifications"] == "missing"
    assert snapshot.source_coverage["occupancy"] == "missing"
    assert snapshot.source_coverage["checkpoints"] == "missing"
    assert snapshot.rows["deliveries"] == []


async def test_present_sections_carry_their_rows(session_factory):
    await _seed(
        session_factory,
        _run(RUN_A, REPO_A),
        _revival(RUN_A, "succeeded"),
        _revival(RUN_A, "failed", attempt=2),
        _command(RUN_A, 1, "pause", "checkpointed"),
        _intent(RUN_A, status="dispatched"),
        GateApproval(
            flow_run_id=RUN_A,
            generation=1,
            plan_digest="p" * 64,
            base_sha="b" * 40,
            policy_digest="q" * 64,
            approver_user_id=7,
            source_event_id="evt-1",
            expires_at=NOW + timedelta(days=1),
        ),
        ControlCommandDeliveryRow(
            command_id=f"cmd-{RUN_A[:6]}-1",
            work_id=RUN_A,
            recipient="lane:ci",
            status="acknowledged",
        ),
    )

    snapshot = await _reader(session_factory).snapshot(RUN_A, [SUBJECT_A])

    assert snapshot is not None
    coverage = snapshot.source_coverage
    for section in ("attempts", "commands", "deliveries", "publications", "approvals"):
        assert coverage[section] == "present", section
    assert [row.get("status", "unknown") for row in snapshot.rows["attempts"]] == [
        "unknown",  # the INITIAL execution (no provable outcome from the run row)
        "failed",
        "succeeded",
    ]
    assert snapshot.rows["commands"][0]["kind"] == "pause"
    assert snapshot.rows["deliveries"][0]["recipient"] == "lane:ci"
    assert snapshot.rows["publications"][0]["status"] == "dispatched"
    assert snapshot.rows["approvals"][0]["approved_by"] == "user:7"


# ---------------------------------------------------------------------------
# verified_ready binds to the CURRENT candidate
# ---------------------------------------------------------------------------


def _verified_run(run_id: str, tested_oid: str) -> FlowRun:
    return _run(
        run_id,
        REPO_A,
        status="waiting_ci",
        evidence={
            "verification": {
                "status": "passed",
                "tested_oid": tested_oid,
                "observed_at": (NOW - timedelta(minutes=5)).isoformat(),
                "producer": "github-checks",
            }
        },
    )


async def test_a_passed_verification_of_the_current_candidate_is_verified_ready(session_factory):
    await _seed(session_factory, _verified_run(RUN_A, "c" * 40))

    snapshot = await _reader(session_factory).snapshot(RUN_A, [SUBJECT_A])

    assert snapshot is not None
    assert snapshot.source_coverage["verifications"] == "present"
    projection = snapshot.projection()
    assert projection.state == "verified_ready"
    assert projection.identity["candidate_shas"] == ["c" * 40]


async def test_an_old_green_verification_of_another_candidate_decorates_nothing(session_factory):
    """The R36-15 pin: a passed verdict for a DIFFERENT commit cannot make
    the current candidate verified_ready — the run reads unverified."""
    await _seed(session_factory, _verified_run(RUN_A, "d" * 40))

    snapshot = await _reader(session_factory).snapshot(RUN_A, [SUBJECT_A])

    assert snapshot is not None
    assert snapshot.rows["verifications"][0]["candidate_sha"] == "d" * 40
    assert snapshot.projection().state == "unverified"


# ---------------------------------------------------------------------------
# R37-03: per-attempt generations and the initial execution
# ---------------------------------------------------------------------------


async def test_attempt_rows_read_their_own_generation_not_the_runs_current(session_factory):
    """Each revival reads ITS OWN generation from its record; the initial
    execution reads ``unknown`` (no creation-time generation is recorded).
    The run's CURRENT cancellation_generation (5 here) is copied onto
    NOTHING."""
    await _seed(
        session_factory,
        _run(RUN_A, REPO_A, status="validating", cancellation_generation=5),
        _revival(
            RUN_A,
            "succeeded",
            attempt=1,
            remote_result={"generation": 1, "note_id": 11},
            created_at=NOW - timedelta(minutes=30),
        ),
        _revival(
            RUN_A,
            "requested",  # in flight — maps to "executing" in the view
            attempt=2,
            remote_result={"generation": 2},
            created_at=NOW - timedelta(minutes=10),
        ),
    )

    snapshot = await _reader(session_factory).snapshot(RUN_A, [SUBJECT_A])

    assert snapshot is not None
    generations = [row["generation"] for row in snapshot.rows["attempts"]]
    assert generations == ["unknown", 1, 2]
    assert snapshot.projection().identity["generation"] == 2  # the LATEST attempt


async def test_the_initial_execution_renders_without_a_revival_record(session_factory):
    """The first execution appears in the attempt history with the run's
    own timestamps — no revival ActionLog required; an unprovable outcome
    stays absent (the view reads absent as unknown, never a guess)."""
    await _seed(session_factory, _run(RUN_A, REPO_A, status="validating"))

    snapshot = await _reader(session_factory).snapshot(RUN_A, [SUBJECT_A])

    assert snapshot is not None
    attempts = snapshot.rows["attempts"]
    assert len(attempts) == 1
    initial = attempts[0]
    assert initial["attempt_id"] == f"run:{RUN_A}:initial"
    assert initial["kind"] == "initial_execution"
    assert "status" not in initial
    assert initial["generation"] == "unknown"
    assert initial["started_at"] == (NOW - timedelta(hours=3)).isoformat()


async def test_a_failed_run_synthesizes_the_failed_initial_outcome(session_factory):
    await _seed(session_factory, _run(RUN_A, REPO_A, status="failed"))

    snapshot = await _reader(session_factory).snapshot(RUN_A, [SUBJECT_A])

    assert snapshot is not None
    assert snapshot.rows["attempts"][0]["status"] == "failed"
    assert snapshot.projection().state == "rejected"


# ---------------------------------------------------------------------------
# R37-03: the current projection after a historical resume (AT-04, reader)
# ---------------------------------------------------------------------------


async def test_at04_current_projection_after_historical_resume_and_verification(
    session_factory,
):
    """AT-04 (reader arm): CP-A activation for attempt 1, then CP-B upload
    and attempt 2; candidate history [A, B] with only A independently
    passed. CP-B shows NO borrowed activation; attempt 1 keeps its
    generation; B is unverified (A's pass is historical); the support
    bundle built from the SAME rows agrees with the status."""
    from forge.adaptive.support_bundle import SupportBundle

    cp_a, cp_b = "a" * 64, "b" * 64
    cand_a, cand_b = "a" * 40, "b" * 40
    await _seed(
        session_factory,
        _run(
            RUN_A,
            REPO_A,
            status="waiting_ci",
            cancellation_generation=2,
            candidate_shas=[cand_a, cand_b],
            evidence={
                "verification": {
                    "status": "passed",
                    "tested_oid": cand_a,  # only A independently passed
                    "observed_at": (NOW - timedelta(minutes=6)).isoformat(),
                    "producer": "github-checks",
                }
            },
        ),
        _revival(
            RUN_A,
            "succeeded",
            attempt=1,
            remote_result={"generation": 0},
            created_at=NOW - timedelta(hours=2),
        ),
        _revival(
            RUN_A,
            "succeeded",  # attempt 2 finished; the run now waits on CI
            attempt=2,
            remote_result={"generation": 1},
            created_at=NOW - timedelta(minutes=12),
        ),
        PauseFenceRow(
            work_id=RUN_A,
            publication_epoch_bumped=2,
            fenced_at=NOW - timedelta(hours=2),
            cleared_at=NOW - timedelta(minutes=90),  # the CP-A resume cleared it
        ),
        _command(RUN_A, 1, "pause", "checkpointed"),
        _command(
            RUN_A,
            2,
            "resume",
            "applied",  # resume of CP-A — applied for attempt 1
            applied_at=NOW - timedelta(minutes=90),
            checkpoint_ref=f"{RUN_A}@{cp_a}",
        ),
    )
    repository = RecordingRepository(
        entry=_checkpoint_entry(
            checkpoint_id=cp_b,  # CP-B uploaded AFTER the CP-A resume applied
            sequence=5,
            uploaded_at=(NOW - timedelta(minutes=10)).isoformat(),
        )
    )

    snapshot = await _reader(session_factory, repository=repository).snapshot(RUN_A, [SUBJECT_A])

    assert snapshot is not None
    # CP-B has no borrowed activation
    checkpoint = snapshot.rows["checkpoints"][0]
    assert checkpoint["checkpoint_id"] == cp_b
    assert checkpoint["activated_at"] is None
    assert checkpoint["activation"] == "unmatched-command"
    # attempt 1 keeps its OWN generation (0), attempt 2 its own (1)
    attempts = snapshot.rows["attempts"]
    assert [row["generation"] for row in attempts] == ["unknown", 0, 1]
    # B is the current candidate and is NOT verified-ready; A's pass is history
    projection = snapshot.projection()
    assert projection.state == "unverified"
    assert projection.identity["active_candidate"] == cand_b
    assert [entry["candidate_sha"] for entry in projection.verification_history] == [cand_a]
    assert [entry["verdict"] for entry in projection.verification_history] == ["historical_pass"]
    # the bundle agrees with the status view on the current identities
    bundle = SupportBundle.build(RUN_A, snapshot.rows, now=NOW)
    assert bundle.projection["state"] == projection.state
    assert bundle.projection["identity"]["active_candidate"] == cand_b
    assert bundle.verifications[0]["candidate_sha"] == cand_a
    assert bundle.checkpoints[0]["activated_at"] == ""


async def test_a_stable_source_version_renders_consistent(session_factory):
    await _seed(session_factory, _run(RUN_A, REPO_A), _revival(RUN_A, "succeeded"))

    snapshot = await _reader(session_factory).snapshot(RUN_A, [SUBJECT_A])

    assert snapshot is not None
    assert snapshot.projection_inconsistent is False
    assert snapshot.source_version  # the fence is recorded, not blank


async def test_a_moving_source_version_marks_the_projection_inconsistent(
    session_factory,
):
    """The delayed-subquery arm (fault injection, named): a repair commits
    between the reader's fence reads — simulated by a reader whose fence
    advances between the two observations — and the snapshot says
    ``projection_inconsistent`` instead of a confident state."""

    class DriftingFenceReader(OperatorSnapshotReader):
        """Bounded fault injection: the source version drifts between the
        start and end fences (a concurrent commit under READ COMMITTED)."""

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._fence_reads = 0

        async def _source_version(self, session: Any, run_id: str) -> str:
            self._fence_reads += 1
            base = await super()._source_version(session, run_id)
            return f"{base}#r{self._fence_reads}"

    await _seed(session_factory, _run(RUN_A, REPO_A), _revival(RUN_A, "succeeded"))

    snapshot = await DriftingFenceReader(session_factory, clock=lambda: NOW).snapshot(
        RUN_A, [SUBJECT_A]
    )

    assert snapshot is not None
    assert snapshot.projection_inconsistent is True
    assert snapshot.source_version  # the moved fence is the recorded version


# ---------------------------------------------------------------------------
# safely_paused stands on real checkpoint + fence evidence
# ---------------------------------------------------------------------------


async def test_safely_paused_requires_the_real_checkpoint_entry(session_factory):
    """A checkpointed pause alone is pause-booking evidence; the state the
    operator trusts needs the repository's ACTIVE entry beside it."""
    await _seed(
        session_factory,
        _run(RUN_A, REPO_A, status="proposing"),
        _command(RUN_A, 1, "pause", "checkpointed"),
        PauseFenceRow(
            work_id=RUN_A,
            publication_epoch_bumped=2,
            fenced_at=NOW - timedelta(minutes=20),
        ),
    )

    without_entry = await _reader(
        session_factory, repository=RecordingRepository(entry=None)
    ).snapshot(RUN_A, [SUBJECT_A])
    assert without_entry is not None
    assert without_entry.projection().state != "safely_paused"

    with_entry = await _reader(
        session_factory, repository=RecordingRepository(entry=_checkpoint_entry())
    ).snapshot(RUN_A, [SUBJECT_A])
    assert with_entry is not None
    assert with_entry.source_coverage["checkpoints"] == "present"
    checkpoint = with_entry.rows["checkpoints"][0]
    assert checkpoint["fence"] == "held"
    assert checkpoint["checkpoint_id"] == "e" * 64
    assert with_entry.projection().state == "safely_paused"


async def test_a_cleared_fence_does_not_read_as_held(session_factory):
    await _seed(
        session_factory,
        _run(RUN_A, REPO_A, status="proposing"),
        _command(RUN_A, 1, "pause", "checkpointed"),
        PauseFenceRow(
            work_id=RUN_A,
            publication_epoch_bumped=2,
            fenced_at=NOW - timedelta(minutes=30),
            cleared_at=NOW - timedelta(minutes=10),
        ),
    )

    snapshot = await _reader(
        session_factory, repository=RecordingRepository(entry=_checkpoint_entry())
    ).snapshot(RUN_A, [SUBJECT_A])

    assert snapshot is not None
    assert snapshot.rows["checkpoints"][0]["fence"] == "cleared"
    assert snapshot.projection().state != "safely_paused"


async def test_a_resume_command_at_applied_marks_the_activation_proof(session_factory):
    """The activation receipt: a resume that reached ``applied`` AND whose
    ResumeSpec pinned THIS checkpoint activates it (R37-03 — the command
    application is tied to the exact checkpoint it named)."""
    await _seed(
        session_factory,
        _run(RUN_A, REPO_A, status="proposing"),
        _command(RUN_A, 1, "pause", "checkpointed"),
        _command(
            RUN_A,
            2,
            "resume",
            "applied",
            applied_at=NOW - timedelta(minutes=5),
            checkpoint_ref=f"{RUN_A}@{'e' * 64}",
        ),
        PauseFenceRow(
            work_id=RUN_A,
            publication_epoch_bumped=2,
            fenced_at=NOW - timedelta(minutes=30),
            cleared_at=NOW - timedelta(minutes=5),
        ),
    )

    snapshot = await _reader(
        session_factory, repository=RecordingRepository(entry=_checkpoint_entry())
    ).snapshot(RUN_A, [SUBJECT_A])

    assert snapshot is not None
    checkpoint = snapshot.rows["checkpoints"][0]
    assert checkpoint["activated_at"] == (NOW - timedelta(minutes=5)).isoformat()
    assert checkpoint["activation"] == "matched"
    assert snapshot.projection().state == "resumed"


async def test_an_applied_resume_for_another_checkpoint_never_activates_this_one(
    session_factory,
):
    """R37-03's headline negative: resume A applied at 10:00, checkpoint B
    uploaded at 10:05 → B carries NO borrowed activation (``activated_at``
    None, ``unmatched-command``), and the state does not read resumed."""
    await _seed(
        session_factory,
        _run(RUN_A, REPO_A, status="proposing"),
        _command(RUN_A, 1, "pause", "checkpointed"),
        _command(
            RUN_A,
            2,
            "resume",
            "applied",
            applied_at=NOW - timedelta(minutes=10),
            checkpoint_ref=f"{RUN_A}@{'a' * 64}",  # CP-A — not the active entry
        ),
        PauseFenceRow(
            work_id=RUN_A,
            publication_epoch_bumped=2,
            fenced_at=NOW - timedelta(minutes=30),
        ),
    )
    # the authority now holds CP-B (uploaded AFTER the CP-A resume applied)
    repository = RecordingRepository(
        entry=_checkpoint_entry(
            checkpoint_id="b" * 64, uploaded_at=(NOW - timedelta(minutes=5)).isoformat()
        )
    )

    snapshot = await _reader(session_factory, repository=repository).snapshot(RUN_A, [SUBJECT_A])

    assert snapshot is not None
    checkpoint = snapshot.rows["checkpoints"][0]
    assert checkpoint["checkpoint_id"] == "b" * 64
    assert checkpoint["activated_at"] is None
    assert checkpoint["activation"] == "unmatched-command"
    assert snapshot.projection().state != "resumed"


async def test_a_ref_less_applied_resume_is_not_a_restoration_proof(session_factory):
    """An applied ACK whose command recorded NO checkpoint reference is an
    absent receipt — never a resumed state (command application alone is
    not filesystem-restoration proof)."""
    await _seed(
        session_factory,
        _run(RUN_A, REPO_A, status="proposing"),
        _command(RUN_A, 1, "pause", "checkpointed"),
        _command(RUN_A, 2, "resume", "applied", applied_at=NOW - timedelta(minutes=5)),
        PauseFenceRow(
            work_id=RUN_A,
            publication_epoch_bumped=2,
            fenced_at=NOW - timedelta(minutes=30),
        ),
    )

    snapshot = await _reader(
        session_factory, repository=RecordingRepository(entry=_checkpoint_entry())
    ).snapshot(RUN_A, [SUBJECT_A])

    assert snapshot is not None
    checkpoint = snapshot.rows["checkpoints"][0]
    assert checkpoint["activated_at"] is None
    assert checkpoint["activation"] == "unmatched-command"
    assert snapshot.projection().state != "resumed"


# ---------------------------------------------------------------------------
# Occupancy — the admission/lease slice beside the state
# ---------------------------------------------------------------------------


async def test_open_and_draining_leases_are_visible_with_derived_occupancy(session_factory):
    await _seed(
        session_factory,
        _run(RUN_A, REPO_A),
        ExecutionLease(
            id="lease-open",
            project_id=1,
            provider="github",
            run_id=RUN_A,
            slot=1,
            acquired_at=NOW - timedelta(hours=1),
            native_intent_at=NOW - timedelta(minutes=55),
            native_handle="github:workflow:42",
        ),
        ExecutionLease(
            id="lease-done",
            project_id=1,
            provider="github",
            run_id=RUN_A,
            slot=2,
            acquired_at=NOW - timedelta(hours=2),
            draining_at=NOW - timedelta(minutes=30),
            released_at=NOW - timedelta(minutes=3),
            release_reason="native_terminal",
        ),
    )

    snapshot = await _reader(session_factory).snapshot(RUN_A, [SUBJECT_A])

    assert snapshot is not None
    assert snapshot.source_coverage["occupancy"] == "present"
    by_id = {row["lease_id"]: row for row in snapshot.occupancy}
    assert by_id["lease-open"]["occupancy"] == "native_running"
    assert by_id["lease-open"]["native_handle"] == "github:workflow:42"
    assert by_id["lease-done"]["occupancy"] == "observed_terminal"
    assert by_id["lease-done"]["released_at"] == (NOW - timedelta(minutes=3)).isoformat()


# ---------------------------------------------------------------------------
# Freshness — projection_age
# ---------------------------------------------------------------------------


async def test_projection_age_measures_the_newest_observed_row(session_factory):
    await _seed(
        session_factory,
        _run(RUN_A, REPO_A, updated_at=NOW - timedelta(minutes=10)),
        _revival(RUN_A, "succeeded", created_at=NOW - timedelta(minutes=30)),
    )

    snapshot = await _reader(session_factory).snapshot(RUN_A, [SUBJECT_A])

    assert snapshot is not None
    assert snapshot.projection_age_s is not None
    assert 599 <= snapshot.projection_age_s <= 601  # the run row is the newest


async def test_projection_age_is_none_when_no_row_carries_a_clock(session_factory):
    """A direct construction (durable rows always carry clocks — the pure
    edge a hand-built snapshot can hit): no readable timestamp → no
    synthesized age."""
    from forge.adaptive.operator_snapshot import OperatorSnapshot

    snapshot = OperatorSnapshot(
        run_id=RUN_A,
        subject=REPO_A,
        rows={"run": {"id": RUN_A, "status": "planning", "candidate_shas": []}},
        source_coverage={"run": "present"},
        computed_at=NOW.isoformat(),
    )
    assert snapshot.projection_age_s is None


# ---------------------------------------------------------------------------
# The read-only charter — reads only, never a write through the authority
# ---------------------------------------------------------------------------


async def test_the_read_performs_no_checkpoint_writes(session_factory):
    repository = RecordingRepository(entry=_checkpoint_entry())
    await _seed(session_factory, _run(RUN_A, REPO_A))

    snapshot = await _reader(session_factory, repository=repository).snapshot(RUN_A, [SUBJECT_A])
    projection = snapshot.projection() if snapshot else None

    assert projection is not None
    assert repository.calls == [("entry", RUN_A)]


async def test_the_reader_never_mutates_durable_rows(session_factory):
    await _seed(
        session_factory,
        _run(RUN_A, REPO_A),
        _revival(RUN_A, "succeeded"),
        _command(RUN_A, 1, "pause", "checkpointed"),
    )
    reader = _reader(session_factory)

    await reader.list_snapshots([SUBJECT_A])
    snapshot = await reader.snapshot(RUN_A, [SUBJECT_A])
    snapshot.projection() if snapshot else None

    async with reader._session_factory() as session:  # noqa: SLF001 — the audit read
        runs = list((await session.execute(select(FlowRun))).scalars().all())
        actions = list((await session.execute(select(ActionLog))).scalars().all())
        commands = list((await session.execute(select(ControlCommandRow))).scalars().all())
    assert len(runs) == 1 and runs[0].status == "validating"
    assert len(actions) == 1 and actions[0].status == "succeeded"
    assert len(commands) == 1 and commands[0].status == "checkpointed"


# ---------------------------------------------------------------------------
# R37-16: bounded drill-down — windows, totals, section selection
# ---------------------------------------------------------------------------


async def test_bounded_attempts_keep_the_last_window_with_the_total(session_factory):
    """The default read is the bounded one: the LAST N revival records
    beside the synthesized initial, with the AUTHORITY total recorded so
    the window is never mistaken for the whole journal."""
    await _seed(
        session_factory,
        _run(RUN_A, REPO_A),
        *[
            _revival(
                RUN_A,
                "succeeded",
                attempt=number,
                created_at=NOW - timedelta(minutes=60 - number),  # 1 oldest, 30 newest
                remote_result={"generation": number},
            )
            for number in range(1, 31)
        ],
    )

    snapshot = await _reader(session_factory).snapshot(RUN_A, [SUBJECT_A], section_limit=5)

    assert snapshot is not None
    generations = [row["generation"] for row in snapshot.rows["attempts"]]
    assert generations == ["unknown", 26, 27, 28, 29, 30]  # the LAST five, journal order
    assert snapshot.section_totals["attempts"] == 30
    assert snapshot.section_truncated["attempts"] is True
    assert snapshot.section_limit == 5
    # the projection derives from the NEWEST attempt — never a truncated edge
    assert (
        snapshot.projection().identity["attempt_id"] == snapshot.rows["attempts"][-1]["attempt_id"]
    )


async def test_the_full_history_opt_in_reads_every_attempt(session_factory):
    """``section_limit=None`` is the bundle export's evidence-completeness
    opt-in: every revival row, no truncation mark."""
    await _seed(
        session_factory,
        _run(RUN_A, REPO_A),
        *[
            _revival(RUN_A, "failed", attempt=number, created_at=NOW - timedelta(minutes=number))
            for number in range(1, 26)
        ],
    )

    snapshot = await _reader(session_factory).snapshot(RUN_A, [SUBJECT_A], section_limit=None)

    assert snapshot is not None
    assert len(snapshot.rows["attempts"]) == 26  # initial + all 25 revivals
    assert snapshot.section_totals["attempts"] == 25
    assert snapshot.section_truncated["attempts"] is False
    assert snapshot.section_limit is None


async def test_commands_read_pending_whole_plus_the_settled_tail(session_factory):
    rows = [
        _command(RUN_A, seq, "steer", "applied", applied_at=NOW - timedelta(minutes=60 - seq))
        for seq in range(1, 9)  # eight settled, sequence 8 the newest
    ]
    rows += [
        _command(RUN_A, 9, "pause", "received"),
        _command(RUN_A, 10, "resume", "dispatching"),
    ]
    await _seed(session_factory, _run(RUN_A, REPO_A), *rows)

    snapshot = await _reader(session_factory).snapshot(RUN_A, [SUBJECT_A], section_limit=3)

    assert snapshot is not None
    sequences = [row["sequence"] for row in snapshot.rows["commands"]]
    assert sequences == [6, 7, 8, 9, 10]  # the settled tail 6-8 + pending 9-10 whole
    assert snapshot.section_totals["commands"] == 10
    assert snapshot.section_truncated["commands"] is True


async def test_publications_read_unresolved_whole_plus_the_resolved_tail(session_factory):
    def intent(key: str, status: str, minutes: int) -> PublicationIntent:
        return _intent(
            RUN_A,
            status=status,
            operation_key=key,
            created_at=NOW - timedelta(minutes=minutes),
            updated_at=NOW - timedelta(minutes=min(minutes, 5)),
        )

    rows = [intent(f"op-{number}", "committed", 90 - number) for number in range(1, 11)]
    rows += [intent("op-unclear-1", "dispatched", 60), intent("op-unclear-2", "probing", 59)]
    await _seed(session_factory, _run(RUN_A, REPO_A), *rows)

    snapshot = await _reader(session_factory).snapshot(RUN_A, [SUBJECT_A], section_limit=4)

    assert snapshot is not None
    keys = [row["operation_key"] for row in snapshot.rows["publications"]]
    assert keys == ["op-7", "op-8", "op-9", "op-10", "op-unclear-1", "op-unclear-2"]
    assert snapshot.section_totals["publications"] == 12
    assert snapshot.section_truncated["publications"] is True


async def test_section_selection_skips_the_unqueried_sections(session_factory):
    """``sections=`` bounds the QUERY: the unselected sections read
    ``unknown`` (never queried, never invented) and the injected
    checkpoint authority is never consulted for them."""
    repository = RecordingRepository(entry=_checkpoint_entry())
    await _seed(
        session_factory,
        _run(RUN_A, REPO_A),
        _revival(RUN_A, "succeeded"),
        _command(RUN_A, 1, "pause", "checkpointed"),
    )
    reader = _reader(session_factory, repository=repository)

    snapshot = await reader.snapshot(RUN_A, [SUBJECT_A], sections=("attempts",))

    assert snapshot is not None
    assert snapshot.source_coverage["attempts"] == "present"
    assert snapshot.source_coverage["commands"] == "unknown"
    assert snapshot.source_coverage["checkpoints"] == "unknown"
    assert "commands" not in snapshot.rows
    assert "checkpoints" not in snapshot.rows
    assert "sections" not in snapshot.section_totals or "commands" not in snapshot.section_totals
    assert repository.calls == []  # the expensive authority was never consulted


async def test_section_selection_refuses_unknown_and_empty_names(session_factory):
    await _seed(session_factory, _run(RUN_A, REPO_A))
    reader = _reader(session_factory)

    with pytest.raises(ValueError, match="unknown operator section"):
        await reader.snapshot(RUN_A, [SUBJECT_A], sections=("attempts", "blobs"))
    with pytest.raises(ValueError, match="no operator sections selected"):
        await reader.snapshot(RUN_A, [SUBJECT_A], sections=("",))


async def test_occupancy_reads_open_leases_whole_plus_the_released_tail(session_factory):
    rows = [
        ExecutionLease(
            id=f"lease-{number}",
            project_id=1,
            provider="github",
            run_id=RUN_A,
            slot=number,
            acquired_at=NOW - timedelta(hours=10 - number),
            released_at=NOW - timedelta(minutes=number),
            release_reason="native_terminal",
        )
        for number in range(1, 7)  # six released leases
    ]
    rows.append(
        ExecutionLease(
            id="lease-open",
            project_id=1,
            provider="github",
            run_id=RUN_A,
            slot=7,
            acquired_at=NOW - timedelta(hours=1),
            native_intent_at=NOW - timedelta(minutes=55),
            native_intent_ref="github:workflow:42",
        )
    )
    await _seed(session_factory, _run(RUN_A, REPO_A), *rows)

    snapshot = await _reader(session_factory).snapshot(RUN_A, [SUBJECT_A], section_limit=2)

    assert snapshot is not None
    lease_ids = [row["lease_id"] for row in snapshot.occupancy]
    assert lease_ids == ["lease-5", "lease-6", "lease-open"]  # released tail + the open lease
    assert snapshot.section_totals["occupancy"] == 7
    assert snapshot.section_truncated["occupancy"] is True
    assert snapshot.source_coverage["occupancy"] == "present"


async def test_list_snapshots_window_member_sections_too(session_factory):
    """The LIST is bounded per member: each page snapshot's sections read
    under the same window, so page assembly never walks a member's whole
    journal."""
    await _seed(
        session_factory,
        _run(RUN_A, REPO_A),
        *[
            _revival(RUN_A, "succeeded", attempt=number, created_at=NOW - timedelta(minutes=number))
            for number in range(1, 31)
        ],
        _run(RUN_B, REPO_B),
    )

    page = await _reader(session_factory).list_snapshots([SUBJECT_A, SUBJECT_B], section_limit=5)

    alpha = next(snapshot for snapshot in page.snapshots if snapshot.run_id == RUN_A)
    assert alpha.section_totals["attempts"] == 30
    assert alpha.section_truncated["attempts"] is True
    assert len(alpha.rows["attempts"]) == 6
    beta = next(snapshot for snapshot in page.snapshots if snapshot.run_id == RUN_B)
    assert beta.section_totals["attempts"] == 0
    assert beta.section_truncated["attempts"] is False
