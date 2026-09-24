"""The authorized snapshot reader (R36-15) — scope, coverage, binding.

``OperatorSnapshotReader`` assembles the projection inputs from durable
rows under ONE subject scope: every run query filters by the authorized
repositories, an unqueried source is never an empty success (coverage
``unknown``), ``verified_ready`` binds to the CURRENT candidate through
the run's verification evidence, ``safely_paused`` stands on real
checkpoint/fence rows, occupancy is visible, and the whole read is
side-effect free.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive.admission import ExecutionLease
from forge.adaptive.checkpoint_repository import CheckpointRepositoryUnavailable
from forge.adaptive.mailbox_db import ControlCommandDeliveryRow, ControlCommandRow
from forge.adaptive.operator_snapshot import OperatorSnapshotReader
from forge.adaptive.pause_fence import PauseFenceRow
from forge.durable.models import ActionLog, FlowRun, GateApproval, PublicationIntent
from forge.models.base import Base

NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
REPO_A = "owner/alpha"
REPO_B = "owner/beta"
RUN_A = "a" * 32
RUN_B = "b" * 32


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
    **over,
) -> ControlCommandRow:
    values: dict = {
        "id": f"cmd-{run_id[:6]}-{seq}",
        "work_id": run_id,
        "run_id": run_id,
        "kind": kind,
        "payload": {"run_id": run_id},
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

    snapshots = await _reader(session_factory).list_snapshots([REPO_A])

    assert [snapshot.run_id for snapshot in snapshots] == [RUN_A]
    assert all(snapshot.subject == REPO_A for snapshot in snapshots)


async def test_detail_for_a_foreign_repo_run_is_none(session_factory):
    await _seed(session_factory, _run(RUN_A, REPO_A), _run(RUN_B, REPO_B))
    reader = _reader(session_factory)

    assert await reader.snapshot(RUN_B, [REPO_A]) is None
    assert await reader.snapshot(RUN_B, [REPO_A, "owner/gamma"]) is None
    # the same run IS readable under its own scope
    snapshot = await reader.snapshot(RUN_B, [REPO_B])
    assert snapshot is not None and snapshot.run_id == RUN_B


async def test_an_empty_scope_sees_nothing(session_factory):
    await _seed(session_factory, _run(RUN_A, REPO_A))

    assert await _reader(session_factory).list_snapshots([]) == []
    assert await _reader(session_factory).snapshot(RUN_A, []) is None


async def test_the_bundle_path_is_scoped_too(session_factory):
    """The support bundle builds from the SAME scoped snapshot — a B run
    under an A scope never reaches SupportBundle at all."""
    from forge.adaptive.support_bundle import SupportBundle

    await _seed(session_factory, _run(RUN_A, REPO_A), _run(RUN_B, REPO_B))
    reader = _reader(session_factory)

    snapshot = await reader.snapshot(RUN_A, [REPO_A])
    assert snapshot is not None
    bundle = SupportBundle.build(RUN_A, snapshot.rows, now=NOW)
    assert bundle.run_id == RUN_A

    assert await reader.snapshot(RUN_B, [REPO_A]) is None


# ---------------------------------------------------------------------------
# Coverage honesty — missing vs unknown, never an invented empty success
# ---------------------------------------------------------------------------


async def test_unqueried_sources_read_unknown_not_missing(session_factory):
    """No checkpoint repository injected → checkpoints are UNKNOWN (the
    authority was never consulted), and questions — which have no durable
    authority at all — are unknown by construction."""
    await _seed(session_factory, _run(RUN_A, REPO_A))

    snapshot = await _reader(session_factory, repository=None).snapshot(RUN_A, [REPO_A])

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

    snapshot = await _reader(session_factory, repository=repository).snapshot(RUN_A, [REPO_A])

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

    snapshot = await _reader(session_factory, repository=repository).snapshot(RUN_A, [REPO_A])

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

    snapshot = await _reader(session_factory).snapshot(RUN_A, [REPO_A])

    assert snapshot is not None
    coverage = snapshot.source_coverage
    for section in ("attempts", "commands", "deliveries", "publications", "approvals"):
        assert coverage[section] == "present", section
    assert [row["status"] for row in snapshot.rows["attempts"]] == ["failed", "succeeded"]
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

    snapshot = await _reader(session_factory).snapshot(RUN_A, [REPO_A])

    assert snapshot is not None
    assert snapshot.source_coverage["verifications"] == "present"
    projection = snapshot.projection()
    assert projection.state == "verified_ready"
    assert projection.identity["candidate_shas"] == ["c" * 40]


async def test_an_old_green_verification_of_another_candidate_decorates_nothing(session_factory):
    """The R36-15 pin: a passed verdict for a DIFFERENT commit cannot make
    the current candidate verified_ready — the run reads unverified."""
    await _seed(session_factory, _verified_run(RUN_A, "d" * 40))

    snapshot = await _reader(session_factory).snapshot(RUN_A, [REPO_A])

    assert snapshot is not None
    assert snapshot.rows["verifications"][0]["candidate_sha"] == "d" * 40
    assert snapshot.projection().state == "unverified"


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
    ).snapshot(RUN_A, [REPO_A])
    assert without_entry is not None
    assert without_entry.projection().state != "safely_paused"

    with_entry = await _reader(
        session_factory, repository=RecordingRepository(entry=_checkpoint_entry())
    ).snapshot(RUN_A, [REPO_A])
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
    ).snapshot(RUN_A, [REPO_A])

    assert snapshot is not None
    assert snapshot.rows["checkpoints"][0]["fence"] == "cleared"
    assert snapshot.projection().state != "safely_paused"


async def test_a_resume_command_at_applied_marks_the_activation_proof(session_factory):
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
    ).snapshot(RUN_A, [REPO_A])

    assert snapshot is not None
    checkpoint = snapshot.rows["checkpoints"][0]
    assert checkpoint["activated_at"] == (NOW - timedelta(minutes=5)).isoformat()
    assert snapshot.projection().state == "resumed"


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

    snapshot = await _reader(session_factory).snapshot(RUN_A, [REPO_A])

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

    snapshot = await _reader(session_factory).snapshot(RUN_A, [REPO_A])

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

    snapshot = await _reader(session_factory, repository=repository).snapshot(RUN_A, [REPO_A])
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

    await reader.list_snapshots([REPO_A])
    snapshot = await reader.snapshot(RUN_A, [REPO_A])
    snapshot.projection() if snapshot else None

    async with reader._session_factory() as session:  # noqa: SLF001 — the audit read
        runs = list((await session.execute(select(FlowRun))).scalars().all())
        actions = list((await session.execute(select(ActionLog))).scalars().all())
        commands = list((await session.execute(select(ControlCommandRow))).scalars().all())
    assert len(runs) == 1 and runs[0].status == "validating"
    assert len(actions) == 1 and actions[0].status == "succeeded"
    assert len(commands) == 1 and commands[0].status == "checkpointed"
