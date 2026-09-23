"""Q35-04: execution slots bound to OBSERVED native job occupancy.

The review's finding (c7ae8db): R32-07 built the draining machinery but
no production dispatch ever recorded a native correlation, and every
provider release wrapper released with ``native_completed=True`` — the
capacity limit was a reservation guarantee, not an occupancy guarantee.
A provider that accepted the start request and then lost the response
freed the slot on local status while the native job ran.

These tests pin the completed lifecycle: the native-start INTENT
persisted before the provider call, the handle attached when the
provider answers, occupancy DERIVED (never_dispatched /
dispatched_unknown / native_running / draining / observed_terminal), the
evidence-only release (observed terminal, proven never-dispatched, or
audited override — never local status alone), the terminal-run reclaim
parking intent-carrying leases instead of releasing them, and the
reconciler's probe-by-intent_ref resolution across a process restart.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool, StaticPool

from forge.adaptive.admission import (
    LeaseOccupancy,
    NativeStatus,
    clear_native_start_intent,
    lease_occupancy,
    lease_snapshot,
    occupancy_report,
    open_lease_for_run,
    reconcile_draining,
    record_native_handle,
    record_native_start_intent,
    register_native_probe,
    release_lease,
    release_lease_with_evidence,
    release_run_leases,
    try_acquire_lease,
)
from forge.adaptive.admission import AdmissionPolicy
from forge.durable import FlowRun
from forge.durable.controller import FlowStatus
from forge.models.base import Base
from tests.fixtures.fake_gitlab import FakeGitLab

PROJECT_ID = 42
ISSUE_IID = 7


@pytest.fixture()
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_run(
    db,
    *,
    status: FlowStatus,
    issue_iid: int = ISSUE_IID,
    run_id: str | None = None,
    provider: str = "gitlab",
) -> str:
    run_id = run_id or uuid4().hex
    async with db() as session:
        session.add(
            FlowRun(
                id=run_id,
                project_id=PROJECT_ID,
                issue_iid=issue_iid,
                provider=provider,
                status=status.value,
                evidence={},
                created_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()
    return run_id


async def _lease_row(db, lease_id: str):
    from forge.adaptive.admission import ExecutionLease

    async with db() as session:
        return await session.get(ExecutionLease, lease_id)


async def _run_lease(db, run_id: str):
    lease = await open_lease_for_run(db, run_id)
    assert lease is not None
    return await _lease_row(db, lease.lease_id)


async def _any_lease(db, run_id: str):
    """The run's lease row, released ones included (the refusal paths)."""
    from forge.adaptive.admission import ExecutionLease

    async with db() as session:
        row = (
            (await session.execute(select(ExecutionLease).where(ExecutionLease.run_id == run_id)))
            .scalars()
            .first()
        )
    assert row is not None
    return row


@pytest.fixture(autouse=True)
def _clean_probe_registry():
    """Every test starts with an empty probe registry (module state)."""
    from forge.adaptive import admission

    saved = dict(admission._NATIVE_PROBES)
    admission._NATIVE_PROBES.clear()
    yield
    admission._NATIVE_PROBES.clear()
    admission._NATIVE_PROBES.update(saved)


async def _pooled_file_db(path: Path):
    """A file-backed database with a REAL connection pool — concurrent
    legs get genuinely independent connections (the in-memory StaticPool
    default hands every session the same connection, which is a shared
    transaction, not a race)."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 10},
        poolclass=AsyncAdaptedQueuePool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False), engine


# ----------------------------------------------------------------------
# 1-2: the intent lifecycle and the lost start response
# ----------------------------------------------------------------------


class TestStartIntent:
    """The intent is durable BEFORE the provider call; a pre-call abort
    proves never-dispatched and returns capacity immediately."""

    async def test_intent_persists_before_the_provider_call(self, db):
        policy = AdmissionPolicy(max_active_per_project=1)
        run_id = await _seed_run(db, status=FlowStatus.WAITING_HARNESS)
        lease = await try_acquire_lease(policy, PROJECT_ID, db, run_id=run_id)
        assert lease is not None

        stamped = await record_native_start_intent(
            db, run_id, "gitlab:pipeline:42@factory/7/abcd1234"
        )

        assert stamped == 1
        row = await _lease_row(db, lease.lease_id)
        assert row.native_intent_at is not None
        assert row.native_intent_ref == "gitlab:pipeline:42@factory/7/abcd1234"
        assert lease_occupancy(row) is LeaseOccupancy.DISPATCHED_UNKNOWN

    async def test_no_open_lease_stamps_nothing(self, db):
        assert await record_native_start_intent(db, uuid4().hex, "github:workflow:x@y") == 0

    async def test_empty_ref_fails_closed(self, db):
        with pytest.raises(ValueError):
            await record_native_start_intent(db, uuid4().hex, "")

    async def test_precall_abort_clears_the_intent_and_capacity_returns(self, db):
        """The provider PROVED it refused the start (AT-06's sibling):
        the cleared intent is the never-dispatched proof, so the terminal
        release frees the slot without waiting for a nonexistent job."""
        policy = AdmissionPolicy(max_active_per_project=1)
        run_id = await _seed_run(db, status=FlowStatus.WAITING_HARNESS)
        lease = await try_acquire_lease(policy, PROJECT_ID, db, run_id=run_id)
        await record_native_start_intent(db, run_id, "azure:pipeline:P:207@forge/142/ab")

        assert await clear_native_start_intent(db, run_id) == 1
        row = await _lease_row(db, lease.lease_id)
        assert row.native_intent_at is None
        assert lease_occupancy(row) is LeaseOccupancy.NEVER_DISPATCHED

        outcome = await release_lease_with_evidence(db, run_id, reason="terminal:failed")
        assert outcome.released == 1
        assert await try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex) is not None

    async def test_clear_never_wipes_a_recorded_handle(self, db):
        """A handle proves the provider accepted a start — the clear
        (a pre-call abort) must not touch it."""
        run_id = await _seed_run(db, status=FlowStatus.WAITING_HARNESS)
        lease = await try_acquire_lease(AdmissionPolicy(), PROJECT_ID, db, run_id=run_id)
        await record_native_start_intent(db, run_id, "gitlab:pipeline:42@b")
        await record_native_handle(lease.lease_id, "gitlab:pipeline:42:77", db)

        assert await clear_native_start_intent(db, run_id) == 0
        row = await _lease_row(db, lease.lease_id)
        assert row.native_handle == "gitlab:pipeline:42:77"
        assert lease_occupancy(row) is LeaseOccupancy.NATIVE_RUNNING

    async def test_clear_is_selective_by_marker(self, db):
        """A replaced attempt's clear must not wipe its successor's marker."""
        run_id = await _seed_run(db, status=FlowStatus.WAITING_HARNESS)
        await try_acquire_lease(AdmissionPolicy(), PROJECT_ID, db, run_id=run_id)
        await record_native_start_intent(db, run_id, "github:workflow:o/r/w@b1")

        assert await clear_native_start_intent(db, run_id, "gitlab:pipeline:42@b") == 0
        row = await _run_lease(db, run_id)
        assert row.native_intent_ref == "github:workflow:o/r/w@b1"

    async def test_a_fresh_intent_supersedes_a_parked_drain(self, db):
        """Attempt replacement REUSES the held reservation: the new
        attempt's live occupancy question replaces the old drain wait."""
        run_id = await _seed_run(db, status=FlowStatus.WAITING_HARNESS)
        lease = await try_acquire_lease(AdmissionPolicy(), PROJECT_ID, db, run_id=run_id)
        await record_native_start_intent(db, run_id, "gitlab:pipeline:42@b/1")
        await release_lease(lease.lease_id, db, reason="terminal:ready", native_completed=False)
        assert (await _lease_row(db, lease.lease_id)).draining_at is not None

        await record_native_start_intent(db, run_id, "gitlab:pipeline:42@b/2")

        row = await _lease_row(db, lease.lease_id)
        assert row.draining_at is None
        assert row.native_intent_ref == "gitlab:pipeline:42@b/2"


class TestLostStartResponse:
    """AT-06: the provider accepted the start, the response was lost, the
    worker died. The slot stays held — the local verdict is not evidence."""

    async def test_dispatched_unknown_blocks_oversubscription_at_the_cap(self, db):
        policy = AdmissionPolicy(max_active_per_project=1)
        run_id = await _seed_run(db, status=FlowStatus.WAITING_HARNESS)
        lease = await try_acquire_lease(policy, PROJECT_ID, db, run_id=run_id)
        # The start was intended; the response (and the worker) were lost.
        await record_native_start_intent(db, run_id, "github:workflow:o/r/w@forge/42/ab")
        # The revival scan later parks the run terminal locally.
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.FAILED.value
            await session.commit()

        # The OLD behavior: the terminal-run reclaim released the lease
        # here (local status was the whole truth). The NEW observable:
        # the reclaim parks it draining, the next admission REFUSES.
        assert await try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex) is None
        row = await _lease_row(db, lease.lease_id)
        assert row.released_at is None  # NOT released...
        assert row.draining_at is not None  # ...parked for the reconciler
        assert "native occupancy unobserved" in (row.release_reason or "")
        snapshot = await lease_snapshot(policy, PROJECT_ID, db)
        assert snapshot["held"] == 1  # the capacity is still occupied

    async def test_a_handle_moves_occupancy_to_native_running(self, db):
        run_id = await _seed_run(db, status=FlowStatus.WAITING_HARNESS)
        lease = await try_acquire_lease(AdmissionPolicy(), PROJECT_ID, db, run_id=run_id)
        await record_native_start_intent(db, run_id, "azure:pipeline:P:207@forge/1/ab")
        await record_native_handle(lease.lease_id, "azure:build:P:207:99001", db)

        assert (
            lease_occupancy(await _lease_row(db, lease.lease_id)) is LeaseOccupancy.NATIVE_RUNNING
        )

    async def test_the_evidence_release_parks_an_intent_without_observation(self, db):
        run_id = await _seed_run(db, status=FlowStatus.WAITING_HARNESS)
        lease = await try_acquire_lease(AdmissionPolicy(), PROJECT_ID, db, run_id=run_id)
        await record_native_start_intent(db, run_id, "gitlab:pipeline:42@factory/7/ab")

        outcome = await release_lease_with_evidence(db, run_id, reason="terminal:cancelled")

        assert outcome.released == 0 and outcome.drained == 1
        row = await _lease_row(db, lease.lease_id)
        assert row.released_at is None and row.draining_at is not None

    async def test_failed_cancellation_holds_the_slot_until_native_terminal(self, db):
        """AT-05: the run was cancelled locally but the provider-side
        cancellation failed — the native job still runs, the slot holds."""
        policy = AdmissionPolicy(max_active_per_project=1)
        run_id = await _seed_run(db, status=FlowStatus.CANCELLED)
        lease = await try_acquire_lease(policy, PROJECT_ID, db, run_id=run_id)
        await record_native_start_intent(db, run_id, "github:workflow:o/r/w@b")
        await record_native_handle(lease.lease_id, "github:actions:o/r:501", db)

        outcome = await release_lease_with_evidence(db, run_id, reason="terminal:cancelled")
        assert outcome.drained == 1
        # The reconciler probes: the native job is STILL RUNNING.
        released = await reconcile_draining(db, lambda key: NativeStatus.RUNNING)
        assert released == 0
        assert await try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex) is None
        # ...until the probe observes it terminal.
        assert await reconcile_draining(db, lambda key: NativeStatus.TERMINAL) == 1
        assert await try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex) is not None


# ----------------------------------------------------------------------
# 3: reconcile — terminal observation releases exactly once
# ----------------------------------------------------------------------


class TestReconcileReleasesOnce:
    async def _drained_with_handle(self, db) -> str:
        run_id = await _seed_run(db, status=FlowStatus.READY_FOR_HUMAN)
        lease = await try_acquire_lease(AdmissionPolicy(), PROJECT_ID, db, run_id=run_id)
        await record_native_start_intent(db, run_id, "gitlab:pipeline:42@b")
        await record_native_handle(lease.lease_id, "gitlab:pipeline:42:77", db)
        await release_lease_with_evidence(db, run_id, reason="terminal:ready_for_human")
        row = await _lease_row(db, lease.lease_id)
        assert row.draining_at is not None and row.released_at is None
        return lease.lease_id

    async def test_native_terminal_releases_and_re_reconcile_is_idempotent(self, db):
        lease_id = await self._drained_with_handle(db)

        assert await reconcile_draining(db, lambda key: key == "gitlab:pipeline:42:77") == 1
        first = await _lease_row(db, lease_id)
        assert first.released_at is not None
        assert first.release_reason == "reconciled: native job terminal"

        # Re-reconciling the same (now released) row changes nothing.
        assert await reconcile_draining(db, lambda key: True) == 0
        again = await _lease_row(db, lease_id)
        assert again.released_at == first.released_at  # exactly once

    async def test_a_concurrent_callback_race_still_releases_exactly_once(self, tmp_path):
        """Two GENUINELY independent connections (the StaticPool in-memory
        default hands both sessions ONE connection — a session close would
        roll back the peer's transaction, which is not the race being
        pinned). The production shape: a file-backed database, a real
        connection pool, the terminal callback and the reconciler each on
        their own connection, and the CAS deciding the single winner."""
        db, engine = await _pooled_file_db(tmp_path / "callback-race.db")
        try:
            lease_id = await self._drained_with_handle(db)
            run_id = (await _lease_row(db, lease_id)).run_id or ""

            async def terminal_callback() -> int:
                # The service's terminal callback racing the reconciler —
                # both may try to free the same slot.
                return (
                    await release_lease_with_evidence(db, run_id, reason="terminal:late")
                ).released

            async def reconciler() -> int:
                return await reconcile_draining(db, lambda key: True)

            callback, reconciled = await asyncio.gather(terminal_callback(), reconciler())
            row = await _lease_row(db, lease_id)
            assert row.released_at is not None
            # However the race interleaved, the CAS frees exactly once —
            # and no double release ever resurrects capacity.
            assert (callback + reconciled) >= 1
            assert row.release_reason in {
                "reconciled: native job terminal",
                "terminal:late",
            }
        finally:
            await engine.dispose()

    async def test_concurrent_reconcilers_release_exactly_one_slot(self, tmp_path):
        """Two reconciler ticks on independent connections: the CAS
        UPDATE ... WHERE released_at IS NULL refuses the loser — exactly
        one slot is ever freed."""
        db, engine = await _pooled_file_db(tmp_path / "reconciler-race.db")
        try:
            lease_id = await self._drained_with_handle(db)

            async def slow_probe(key: str) -> NativeStatus:
                await asyncio.sleep(0.02)  # let both ticks read the row
                return NativeStatus.TERMINAL

            first, second = await asyncio.gather(
                reconcile_draining(db, slow_probe), reconcile_draining(db, slow_probe)
            )
            row = await _lease_row(db, lease_id)
            assert row.released_at is not None
            assert first + second == 1  # the CAS refuses the loser
        finally:
            await engine.dispose()

    async def test_an_undecidable_probe_keeps_the_lease_draining_with_age_visible(self, db):
        run_id = await _seed_run(db, status=FlowStatus.READY_FOR_HUMAN)
        await try_acquire_lease(AdmissionPolicy(), PROJECT_ID, db, run_id=run_id)
        await record_native_start_intent(db, run_id, "gitlab:pipeline:42@factory/7/ab")
        await release_lease_with_evidence(db, run_id, reason="terminal:ready_for_human")

        def outage(key: str) -> NativeStatus:
            raise RuntimeError("provider unreachable")

        assert await reconcile_draining(db, outage) == 0
        assert await reconcile_draining(db, lambda key: NativeStatus.UNKNOWN) == 0

        report = await occupancy_report(AdmissionPolicy(), PROJECT_ID, db)
        assert report["occupancy_unknown"] == 1
        assert report["draining_age_seconds"] is not None  # the age is visible
        assert report["occupancy"]["draining"] == 1


# ----------------------------------------------------------------------
# 4: the terminal-run reclaim cannot bypass occupancy
# ----------------------------------------------------------------------


class TestReclaimParksDraining:
    """The OLD failing observable: a locally-terminal run with a live
    intent was RELEASED by the next acquirer's reclaim. It must park."""

    async def test_intent_carrying_terminal_run_parks_not_releases(self, db):
        policy = AdmissionPolicy(max_active_per_project=1)
        run_id = await _seed_run(db, status=FlowStatus.WAITING_HARNESS)
        lease = await try_acquire_lease(policy, PROJECT_ID, db, run_id=run_id)
        await record_native_start_intent(db, run_id, "github:workflow:o/r/w@b")
        await record_native_handle(lease.lease_id, "github:actions:o/r:501", db)
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.CANCELLED.value
            await session.commit()

        assert await try_acquire_lease(policy, PROJECT_ID, db) is None  # cap holds
        row = await _lease_row(db, lease.lease_id)
        assert row.draining_at is not None and row.released_at is None

        # The reconciler is the only release path from here.
        assert await reconcile_draining(db, lambda key: True) == 1
        assert await try_acquire_lease(policy, PROJECT_ID, db) is not None

    async def test_never_dispatched_terminal_run_still_releases_on_reclaim(self, db):
        """The builtin-lane shape (no intent): the local verdict IS the
        whole truth — the crash backstop keeps working (NEXT-11)."""
        policy = AdmissionPolicy(max_active_per_project=1)
        run_id = await _seed_run(db, status=FlowStatus.WAITING_CI)
        await try_acquire_lease(policy, PROJECT_ID, db, run_id=run_id)
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.CANCELLED.value
            await session.commit()

        assert await try_acquire_lease(policy, PROJECT_ID, db) is not None


# ----------------------------------------------------------------------
# 5: the explicit paths — proven never-started and deliberate override
# ----------------------------------------------------------------------


class TestExplicitReleasePaths:
    async def test_proven_never_started_releases_immediately(self, db):
        policy = AdmissionPolicy(max_active_per_project=1)
        run_id = await _seed_run(db, status=FlowStatus.FAILED)
        await try_acquire_lease(policy, PROJECT_ID, db, run_id=run_id)

        # The old explicit spelling on a proven never-started lease...
        assert await release_run_leases(db, run_id, reason="terminal:failed") == 1
        # ...and the evidence spelling agree.
        run2 = await _seed_run(db, status=FlowStatus.FAILED, issue_iid=8)
        await try_acquire_lease(policy, PROJECT_ID, db, run_id=run2)
        outcome = await release_lease_with_evidence(db, run2, reason="terminal:failed")
        assert outcome.released == 1

    async def test_the_deliberate_override_is_audited(self, db):
        run_id = await _seed_run(db, status=FlowStatus.READY_FOR_HUMAN)
        lease = await try_acquire_lease(AdmissionPolicy(), PROJECT_ID, db, run_id=run_id)
        await record_native_start_intent(db, run_id, "gitlab:pipeline:42@b")

        outcome = await release_lease_with_evidence(
            db, run_id, reason="terminal:ready_for_human", override="operator: forced drain"
        )
        assert outcome.released == 1
        row = await _lease_row(db, lease.lease_id)
        assert row.released_at is not None
        assert "override: operator: forced drain" in (row.release_reason or "")

    async def test_native_terminal_observation_releases_past_the_intent(self, db):
        run_id = await _seed_run(db, status=FlowStatus.READY_FOR_HUMAN)
        lease = await try_acquire_lease(AdmissionPolicy(), PROJECT_ID, db, run_id=run_id)
        await record_native_start_intent(db, run_id, "gitlab:pipeline:42@b")
        await record_native_handle(lease.lease_id, "gitlab:pipeline:42:9", db)

        # The harness reconciler OBSERVED the native job finish — it may
        # free the slot in the same breath, no extra tick needed.
        outcome = await release_lease_with_evidence(
            db, run_id, reason="terminal:ready_for_human", native_terminal=True
        )
        assert outcome.released == 1
        assert (await _lease_row(db, lease.lease_id)).released_at is not None

    async def test_release_lease_guards_an_intent_row(self, db):
        run_id = await _seed_run(db, status=FlowStatus.READY_FOR_HUMAN)
        lease = await try_acquire_lease(AdmissionPolicy(), PROJECT_ID, db, run_id=run_id)
        await record_native_start_intent(db, run_id, "gitlab:pipeline:42@b")

        # native_completed=True alone no longer frees an intent row.
        assert await release_lease(lease.lease_id, db, reason="terminal:ready")
        row = await _lease_row(db, lease.lease_id)
        assert row.released_at is None and row.draining_at is not None

        # The deliberate test/operator spelling still can (audited).
        assert await release_lease(lease.lease_id, db, reason="operator: forced", force=True)
        assert (await _lease_row(db, lease.lease_id)).released_at is not None

    async def test_release_run_leases_force_is_the_deliberate_spelling(self, db):
        run_id = await _seed_run(db, status=FlowStatus.READY_FOR_HUMAN)
        lease = await try_acquire_lease(AdmissionPolicy(), PROJECT_ID, db, run_id=run_id)
        await record_native_start_intent(db, run_id, "gitlab:pipeline:42@b")

        assert await release_run_leases(db, run_id, reason="terminal:ready") == 0
        assert (await _lease_row(db, lease.lease_id)).draining_at is not None
        assert await release_run_leases(db, run_id, reason="operator: forced", force=True) == 1


# ----------------------------------------------------------------------
# 7: restart — a fresh process resolves occupancy by intent_ref
# ----------------------------------------------------------------------


class TestRestartResolution:
    async def test_a_fresh_session_factory_resolves_occupancy_by_intent_ref(self, tmp_path):
        url = f"sqlite+aiosqlite:///{tmp_path}/leases.db"
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        first = async_sessionmaker(engine, expire_on_commit=False)

        policy = AdmissionPolicy(max_active_per_project=1)
        run_id = await _seed_run(first, status=FlowStatus.READY_FOR_HUMAN)
        lease = await try_acquire_lease(policy, PROJECT_ID, first, run_id=run_id)
        await record_native_start_intent(first, run_id, "gitlab:pipeline:42@factory/7/ab")
        await release_lease_with_evidence(first, run_id, reason="terminal:ready_for_human")
        await engine.dispose()  # the worker dies / the process restarts

        engine2 = create_async_engine(url)
        second = async_sessionmaker(engine2, expire_on_commit=False)
        try:
            # The reconciler process knows NOTHING but the marker: it
            # registers a probe under the intent_ref's provider prefix.
            async def gitlab_probe(key: str) -> NativeStatus:
                assert key.startswith("gitlab:")
                return NativeStatus.TERMINAL

            register_native_probe("gitlab", gitlab_probe)

            assert await reconcile_draining(second) == 1
            row = await _lease_row(second, lease.lease_id)
            assert row.released_at is not None
            assert row.release_reason == "reconciled: native job terminal"
            # The restarted fleet sees the capacity again.
            assert await try_acquire_lease(policy, PROJECT_ID, second) is not None
        finally:
            await engine2.dispose()

    async def test_an_unregistered_provider_stays_draining(self, db):
        run_id = await _seed_run(db, status=FlowStatus.READY_FOR_HUMAN)
        lease = await try_acquire_lease(AdmissionPolicy(), PROJECT_ID, db, run_id=run_id)
        await record_native_start_intent(db, run_id, "unsupported:pipeline:1@b")
        await release_lease_with_evidence(db, run_id, reason="terminal:ready_for_human")

        assert await reconcile_draining(db) == 0  # no probe → undecidable → hold
        assert (await _lease_row(db, lease.lease_id)).released_at is None

    async def test_the_registry_routes_by_provider_prefix(self, db):
        run_id = await _seed_run(db, status=FlowStatus.READY_FOR_HUMAN)
        lease = await try_acquire_lease(AdmissionPolicy(), PROJECT_ID, db, run_id=run_id)
        await record_native_start_intent(db, run_id, "gitlab:pipeline:42@b")
        await record_native_handle(lease.lease_id, "github:actions:o/r:501", db)
        await release_lease_with_evidence(db, run_id, reason="terminal:ready_for_human")

        # The GitHub probe owns github:-prefixed keys; it must NOT answer
        # for a gitlab marker and vice versa.
        register_native_probe("github", lambda key: NativeStatus.TERMINAL)
        register_native_probe("gitlab", lambda key: NativeStatus.RUNNING)

        # The HANDLE (github:) wins as the probe key → released.
        assert await reconcile_draining(db) == 1


# ----------------------------------------------------------------------
# The occupancy view
# ----------------------------------------------------------------------


class TestOccupancyReport:
    async def test_the_five_states_are_visible_separately(self, db):
        policy = AdmissionPolicy(max_active_per_project=0)  # unbounded
        now = datetime.now(timezone.utc)

        # never_dispatched: acquired, no intent.
        run_a = await _seed_run(db, status=FlowStatus.PROPOSING, issue_iid=1)
        await try_acquire_lease(policy, PROJECT_ID, db, run_id=run_a)
        # dispatched_unknown: intent, no handle.
        run_b = await _seed_run(db, status=FlowStatus.WAITING_HARNESS, issue_iid=2)
        await try_acquire_lease(policy, PROJECT_ID, db, run_id=run_b)
        await record_native_start_intent(db, run_b, "gitlab:pipeline:42@b")
        # native_running: intent + handle.
        run_c = await _seed_run(db, status=FlowStatus.WAITING_HARNESS, issue_iid=3)
        lease_c = await try_acquire_lease(policy, PROJECT_ID, db, run_id=run_c)
        await record_native_start_intent(db, run_c, "gitlab:pipeline:42@c")
        await record_native_handle(lease_c.lease_id, "gitlab:pipeline:42:3", db)
        # draining: parked, unresolved.
        run_d = await _seed_run(db, status=FlowStatus.READY_FOR_HUMAN, issue_iid=4)
        await try_acquire_lease(policy, PROJECT_ID, db, run_id=run_d)
        await record_native_start_intent(db, run_d, "gitlab:pipeline:42@d")
        await release_lease_with_evidence(db, run_d, reason="terminal:ready_for_human")
        # observed_terminal: released.
        run_e = await _seed_run(db, status=FlowStatus.CANCELLED, issue_iid=5)
        lease_e = await try_acquire_lease(policy, PROJECT_ID, db, run_id=run_e)
        await record_native_start_intent(db, run_e, "gitlab:pipeline:42@e")
        await release_lease_with_evidence(db, run_e, reason="terminal:cancelled", override="test")
        assert (await _lease_row(db, lease_e.lease_id)).released_at is not None

        report = await occupancy_report(policy, PROJECT_ID, db, now=now + timedelta(minutes=3))
        assert report["occupancy"] == {
            "never_dispatched": 1,
            "dispatched_unknown": 1,
            "native_running": 1,
            "draining": 1,
            "observed_terminal": 0,  # released rows are not occupancy
        }
        assert report["occupancy_unknown"] == 2  # dispatched_unknown + draining
        assert report["held"] == 4
        assert report["native_vs_reserved"] == {"native_attributed": 1, "reserved": 4}
        assert 179 <= report["draining_age_seconds"] <= 181  # ~3 minutes parked


# ----------------------------------------------------------------------
# 6: the three provider dispatch legs (fake-client fixtures)
# ----------------------------------------------------------------------


class TestGitLabLeg:
    async def test_dispatch_records_intent_and_handle(self, db):
        from tests.test_runs_harness_service import make_service

        fake = FakeGitLab()
        fake.seed_issue(ISSUE_IID, "title", "description")
        fake.seed_commit("main", "base-sha-1", "initial")
        service = make_service(db, fake)

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, "title", "description", "alice")
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        row = await _run_lease(db, run_id)
        pipeline_id = int(await _run_evidence_pipeline(db, run_id))
        assert row.native_intent_at is not None
        assert (
            row.native_intent_ref
            == f"gitlab:pipeline:{PROJECT_ID}@factory/{ISSUE_IID}/{run_id[:8]}"
        )
        assert row.native_handle == f"gitlab:pipeline:{PROJECT_ID}:{pipeline_id}"
        assert lease_occupancy(row) is LeaseOccupancy.NATIVE_RUNNING

    async def test_definite_refusal_clears_intent_and_returns_capacity(self, db, monkeypatch):
        from forge.gitlab.client import GitLabAPIError
        from tests.test_runs_harness_service import make_service

        fake = FakeGitLab()
        fake.seed_issue(ISSUE_IID, "title", "description")
        fake.seed_commit("main", "base-sha-1", "initial")

        async def refused(project_id: int, ref: str, variables=None) -> dict:
            raise GitLabAPIError(422, "pipeline config invalid")

        monkeypatch.setattr(fake, "create_pipeline", refused)
        service = make_service(db, fake)
        monkeypatch.setenv("FORGE_ADMISSION_MAX_ACTIVE_PER_PROJECT", "1")

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, "title", "description", "alice")
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        # The provider PROVED the refusal: no intent survives, no slot leaks.
        row = await _any_lease(db, run_id)
        assert row.native_intent_at is None
        assert row.released_at is not None
        snapshot = await lease_snapshot(AdmissionPolicy(1), PROJECT_ID, db, provider="gitlab")
        assert snapshot["held"] == 0

    async def test_ambiguous_transport_failure_keeps_dispatched_unknown(self, db, monkeypatch):
        import httpx

        from tests.test_runs_harness_service import make_service

        fake = FakeGitLab()
        fake.seed_issue(ISSUE_IID, "title", "description")
        fake.seed_commit("main", "base-sha-1", "initial")

        async def lost(project_id: int, ref: str, variables=None) -> dict:
            raise httpx.ReadTimeout("response lost after acceptance")

        monkeypatch.setattr(fake, "create_pipeline", lost)
        service = make_service(db, fake)
        monkeypatch.setenv("FORGE_ADMISSION_MAX_ACTIVE_PER_PROJECT", "1")

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, "title", "description", "alice")
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        row = await _run_lease(db, run_id)
        assert row.native_intent_at is not None  # the start may have landed
        assert row.released_at is None  # the slot holds...
        assert row.draining_at is not None  # ...parked for the reconciler
        snapshot = await lease_snapshot(AdmissionPolicy(1), PROJECT_ID, db, provider="gitlab")
        assert snapshot["held"] == 1


class TestGitHubLeg:
    async def test_dispatch_records_intent_and_handle(self, db):
        from tests.fixtures.fake_github import FakeGitHub
        from tests.test_github_harness import go, make_service, start

        fake = FakeGitHub()
        fake.seed_repo("acme/acme-widget", {"src/app.py": "print('hi')\n"})
        fake.heads["acme/acme-widget"]["main"] = "1" * 40
        fake.seed_issue("acme/acme-widget", 42, "t", "d")
        service = make_service(db, fake)

        run_id = await start(service)
        await go(service, run_id)

        row = await _run_lease(db, run_id)
        branch = f"forge/42/{run_id[:8]}"
        assert row.native_intent_ref == (
            f"github:workflow:acme/acme-widget/forge-harness.github.yml@{branch}"
        )
        assert row.native_handle == "github:actions:acme/acme-widget:501"
        assert lease_occupancy(row) is LeaseOccupancy.NATIVE_RUNNING

    async def test_the_actions_probe_reads_the_workflow_run(self, db):
        from tests.fixtures.fake_github import FakeGitHub
        from tests.test_github_harness import go, make_service, start

        fake = FakeGitHub()
        fake.seed_repo("acme/acme-widget", {"src/app.py": "print('hi')\n"})
        fake.heads["acme/acme-widget"]["main"] = "1" * 40
        fake.seed_issue("acme/acme-widget", 42, "t", "d")
        service = make_service(db, fake)
        run_id = await start(service)
        await go(service, run_id)
        row = await _run_lease(db, run_id)
        await release_lease_with_evidence(db, run_id, reason="terminal:ready_for_human")

        probe = service._native_occupancy_probe()
        assert await probe(row.native_handle) is NativeStatus.RUNNING  # queued

        for run in fake.actions_runs:
            run.update(status="completed", conclusion="success")
        assert await probe(row.native_handle) is NativeStatus.TERMINAL
        assert await probe("gitlab:pipeline:42:1") is NativeStatus.UNKNOWN  # foreign key


class TestAzureLeg:
    async def test_dispatch_records_intent_and_handle(self, db):
        from tests.test_azure_runs import (
            LANE_PIPELINE_ID,
            PROJECT,
            FakeAzureDevOps,
            go,
            make_service,
            make_settings,
            start,
        )

        fake = FakeAzureDevOps()
        fake.seed_work_item(142, "t", "d")
        service = make_service(
            db, fake, settings=make_settings(FORGE_AZDO_LANE_PIPELINE_ID=LANE_PIPELINE_ID)
        )
        run_id = await start(service)
        await go(service, run_id)

        row = await _run_lease(db, run_id)
        branch = f"forge/142/{run_id[:8]}"
        assert row.native_intent_ref == f"azure:pipeline:{PROJECT}:{LANE_PIPELINE_ID}@{branch}"
        assert row.native_handle == f"azure:build:{PROJECT}:{LANE_PIPELINE_ID}:99001"
        assert lease_occupancy(row) is LeaseOccupancy.NATIVE_RUNNING


async def _run_evidence_pipeline(db, run_id: str) -> str:
    async with db() as session:
        run = await session.get(FlowRun, run_id)
        return str((run.evidence or {}).get("harness", {}).get("pipeline_id"))
