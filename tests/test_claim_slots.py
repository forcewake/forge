"""A05 (docs/reviews/2026-09-18-d16f523): a claimed step runs only while its
worker still owns it.

The shipped worker executes claims SEQUENTIALLY, so it must hold exactly one
lease at a time: the old default claimed five leases while running one
handler, leaving the unstarted four to rot on leases that expired mid-flight
— and the worker then started them with dead claims while another worker was
already executing them. The runtime now:

- claims ONE step per pass by default (``STEP_CLAIM_BATCH = 1``) and drains
  back-to-back passes while work remains — no rot, no throughput loss;
- re-validates every claim immediately before its handler, so a lease that
  died while queued is requeued (or parked dead per attempts) instead of
  executing unowned;
- hands never-started pre-claimed steps back to the queue on shutdown and on
  cancellation — SIGTERM never starts the next pre-claimed task, and nothing
  that never ran is ever marked failed.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forge.durable import StepRun
from forge.durable.claims import current_claim
from forge.models.base import Base
from forge.worker import steps as step_runtime
from forge.worker.steps import (
    STEP_CLAIM_BATCH,
    claim_due_steps,
    execute_claimed_step,
    reschedule_expired_leases,
    run_due_steps,
    run_step_worker,
    schedule_command_step,
)


@pytest.fixture()
async def db(tmp_path):
    """File-backed SQLite: the two-worker tests need several sessions."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/claim_slots.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def schedule_batch(db, count: int) -> list[int]:
    """``count`` due command steps, payloads carrying their index."""
    ids: list[int] = []
    async with db() as session:
        async with session.begin():
            for n in range(count):
                step = await schedule_command_step(
                    session,
                    {"command": "noop", "n": n},
                    source_event_id=uuid4().hex,
                )
                ids.append(step.id)
    return ids


async def step_rows(db) -> dict[int, StepRun]:
    async with db() as session:
        rows = (await session.execute(select(StepRun))).scalars().all()
        return {row.id: row for row in rows}


async def expire_leases(db, step_ids: list[int]) -> None:
    """Simulate a lease that died while its claim sat queued."""
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    async with db() as session:
        async with session.begin():
            await session.execute(
                update(StepRun).where(StepRun.id.in_(step_ids)).values(lease_expires_at=past)
            )


# ----------------------------------------------------------------------
# The shipped sequential configuration: one claim, no rot
# ----------------------------------------------------------------------


class TestShippedSequentialConfiguration:
    async def test_claim_batch_defaults_to_one(self, db):
        """STEP_CLAIM_BATCH = 1: a pass holds exactly one lease — the batch
        stays configurable for a future bounded-parallel executor, but the
        sequential worker must never claim work it cannot run concurrently."""
        assert STEP_CLAIM_BATCH == 1

        ids = await schedule_batch(db, 5)
        claimed = await claim_due_steps(db, "worker-a")

        assert [s.id for s in claimed] == [ids[0]]
        rows = await step_rows(db)
        for step_id in ids[1:]:
            assert rows[step_id].status == "scheduled"
            assert rows[step_id].lease_owner is None
            assert rows[step_id].lease_expires_at is None, "unclaimed steps get no lease to rot"

    async def test_default_pass_claims_one_and_the_rest_stay_claimable(self, db, monkeypatch):
        """A default run_due_steps pass executes exactly one step; the next
        worker immediately claims the next one — no reaper pass needed."""
        executed: list[int] = []

        async def fake_execute(settings, forge_config, session_factory, metadata):
            executed.append(metadata["n"])

        monkeypatch.setattr(step_runtime, "execute_run_command", fake_execute)

        ids = await schedule_batch(db, 5)

        assert await run_due_steps(db, object(), object(), owner="worker-a") == 1
        assert executed == [0]

        assert await run_due_steps(db, object(), object(), owner="worker-b") == 1
        assert executed == [0, 1]

        rows = await step_rows(db)
        for step_id in ids[2:]:
            assert rows[step_id].status == "scheduled"
            assert rows[step_id].lease_owner is None


# ----------------------------------------------------------------------
# Batch>1 with two workers: no double execution despite dead queued claims
# ----------------------------------------------------------------------


class TestTwoWorkersNoDoubleExecution:
    async def test_batch_claims_do_not_double_execute_after_the_lease_dies(self, db, monkeypatch):
        """Acceptance A05: batch=5, the first handler outlives the lease, two
        workers — every subsequent handler enters EXACTLY ONCE, under its
        CURRENT owner. The first worker's dead claims are re-validated away
        before the handler instead of executing unowned."""
        ids = await schedule_batch(db, 5)
        entries: list[tuple[int, str]] = []
        started = asyncio.Event()
        release_first = asyncio.Event()

        async def fake_execute(settings, forge_config, session_factory, metadata):
            claim = current_claim()
            assert claim is not None
            entries.append((metadata["n"], claim.owner))
            if metadata["n"] == 0:
                started.set()
                await release_first.wait()

        monkeypatch.setattr(step_runtime, "execute_run_command", fake_execute)

        worker_a = asyncio.create_task(
            run_due_steps(db, object(), object(), owner="worker-a", limit=5)
        )
        await asyncio.wait_for(started.wait(), timeout=5)

        # Steps 1..4's leases die while they sit unstarted behind the blocked
        # first handler, and the reaper hands them back to the queue.
        await expire_leases(db, ids[1:])
        assert await reschedule_expired_leases(db) == 4

        # Worker B (the shipped one-claim pass) takes each reaped step under a
        # FRESH claim and executes it.
        for _ in range(4):
            assert await run_due_steps(db, object(), object(), owner="worker-b") == 1

        release_first.set()
        await asyncio.wait_for(worker_a, timeout=5)

        # Each handler entered exactly once, with the current owner — worker
        # A never executed a step whose lease it had already lost.
        assert sorted(entries) == [(0, "worker-a")] + [(n, "worker-b") for n in range(1, 5)]

        rows = await step_rows(db)
        assert all(rows[step_id].status == "succeeded" for step_id in ids)


# ----------------------------------------------------------------------
# Shutdown: SIGTERM never starts the next pre-claimed task
# ----------------------------------------------------------------------


class TestShutdownHandback:
    async def test_sigterm_releases_unstarted_preclaimed_steps(self, db, monkeypatch):
        """SIGTERM lands while step 0 is in flight: the in-flight step
        finishes, the remaining pre-claimed steps go back to scheduled —
        never started, never failed, immediately claimable."""
        ids = await schedule_batch(db, 3)
        entered: list[int] = []
        shutdown = asyncio.Event()

        async def fake_execute(settings, forge_config, session_factory, metadata):
            entered.append(metadata["n"])
            shutdown.set()  # SIGTERM arrives mid-handler

        monkeypatch.setattr(step_runtime, "execute_run_command", fake_execute)

        assert (
            await run_due_steps(
                db, object(), object(), owner="worker-a", limit=3, shutdown_event=shutdown
            )
            == 3
        )

        assert entered == [0]
        rows = await step_rows(db)
        assert rows[ids[0]].status == "succeeded"
        for step_id in ids[1:]:
            assert rows[step_id].status == "scheduled"
            assert rows[step_id].lease_owner is None
            assert rows[step_id].attempt == 0, "nothing was attempted — no attempt bump"
            assert rows[step_id].output is None

        # The next worker picks the handed-back steps up immediately.
        next_claim = await claim_due_steps(db, "worker-b")
        assert [s.id for s in next_claim] == [ids[1]]

    async def test_cancellation_does_not_start_the_next_preclaimed_step(self, db, monkeypatch):
        """Hard cancellation mid-pass: the never-started claims are handed
        back un-failed, the in-flight step stays with the reaper (nothing
        recorded — the ADR-0017 §5 process-death contract), and the
        cancellation propagates instead of rolling into the next task."""
        ids = await schedule_batch(db, 3)
        entered: list[int] = []
        started = asyncio.Event()
        blocked = asyncio.Event()

        async def fake_execute(settings, forge_config, session_factory, metadata):
            entered.append(metadata["n"])
            started.set()
            await blocked.wait()

        monkeypatch.setattr(step_runtime, "execute_run_command", fake_execute)

        task = asyncio.create_task(run_due_steps(db, object(), object(), owner="worker-a", limit=3))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert entered == [0]
        rows = await step_rows(db)
        assert rows[ids[0]].status == "running", "in-flight step: nothing recorded"
        assert rows[ids[0]].attempt == 0
        assert rows[ids[0]].lease_owner == "worker-a"
        for step_id in ids[1:]:
            assert rows[step_id].status == "scheduled"
            assert rows[step_id].attempt == 0
            assert rows[step_id].lease_owner is None

        # The reaper recovers the in-flight step the usual way.
        await expire_leases(db, [ids[0]])
        assert await reschedule_expired_leases(db) == 1

    async def test_worker_loop_sigterm_does_not_start_the_next_claimed_task(self, db, monkeypatch):
        """The production loop: shutdown during a pass stops after the current
        step — with the one-claim default there is no next pre-claimed task,
        and the loop exits without touching the second step."""
        ids = await schedule_batch(db, 2)
        entered: list[int] = []
        shutdown = asyncio.Event()

        async def fake_execute(settings, forge_config, session_factory, metadata):
            entered.append(metadata["n"])
            shutdown.set()

        monkeypatch.setattr(step_runtime, "execute_run_command", fake_execute)

        worker = asyncio.create_task(
            run_step_worker(db, object(), object(), "worker-a", shutdown, poll_interval=0.01)
        )
        await asyncio.wait_for(worker, timeout=5)

        assert entered == [0]
        rows = await step_rows(db)
        assert rows[ids[0]].status == "succeeded"
        assert rows[ids[1]].status == "scheduled"
        assert rows[ids[1]].lease_owner is None
        assert rows[ids[1]].attempt == 0


# ----------------------------------------------------------------------
# Fresh entry: the claim is re-validated immediately before the handler
# ----------------------------------------------------------------------


class TestFreshClaimAtHandlerEntry:
    async def test_lease_died_while_queued_is_requeued_not_executed(self, db, monkeypatch):
        """A claim whose lease expired before the handler is re-validated
        away: the handler never runs, the step goes back to scheduled (the
        expired-lease attempt is accounted like a reap), nothing lingers for
        the reaper, and the next worker executes it under a fresh claim."""
        ids = await schedule_batch(db, 1)
        claimed = (await claim_due_steps(db, "worker-a"))[0]
        await expire_leases(db, [claimed.id])

        entered: list[int] = []

        async def fake_execute(settings, forge_config, session_factory, metadata):
            entered.append(metadata["n"])

        monkeypatch.setattr(step_runtime, "execute_run_command", fake_execute)

        await execute_claimed_step(db, object(), object(), claimed)

        assert entered == [], "an unowned claim never reaches the handler"
        rows = await step_rows(db)
        row = rows[claimed.id]
        assert row.status == "scheduled"
        assert row.attempt == 1, "the expired-lease attempt is accounted like a reap"
        assert row.lease_owner is None
        assert row.fence_token == claimed.fence_token, "the fence bumps only on the next grant"
        assert await reschedule_expired_leases(db) == 0, "nothing lingered for the reaper"

        fresh = await claim_due_steps(db, "worker-b")
        assert [s.id for s in fresh] == [ids[0]]
        await execute_claimed_step(db, object(), object(), fresh[0])
        assert entered == [0]

    async def test_expired_queued_claim_exhausting_attempts_parks_dead(self, db, monkeypatch):
        """A requeued stale claim that exhausts the poison-pill budget parks
        dead — the eager requeue never spins a step into a busy loop."""
        source_event_id = uuid4().hex
        async with db() as session:
            async with session.begin():
                step = await schedule_command_step(
                    session,
                    {"command": "noop", "n": 0},
                    source_event_id=source_event_id,
                    max_attempts=1,
                )
                step_id = step.id

        claimed = (await claim_due_steps(db, "worker-a", source_event_id=source_event_id))[0]
        await expire_leases(db, [step_id])

        entered: list[int] = []

        async def fake_execute(settings, forge_config, session_factory, metadata):
            entered.append(metadata["n"])

        monkeypatch.setattr(step_runtime, "execute_run_command", fake_execute)

        await execute_claimed_step(db, object(), object(), claimed)

        assert entered == []
        rows = await step_rows(db)
        row = rows[step_id]
        assert row.status == "dead"
        assert row.attempt == 1
        assert row.finished_at is not None
        assert "lease_expired" in (row.output or {}).get("error", "")
        assert await claim_due_steps(db, "worker-b", source_event_id=source_event_id) == []

    async def test_reassigned_claim_is_left_to_its_new_owner(self, db, monkeypatch):
        """The claim was reaped and another worker already re-claimed it: the
        stale claim skips without touching the row — the new owner's state is
        never clobbered."""
        source_event_id = uuid4().hex
        async with db() as session:
            async with session.begin():
                step = await schedule_command_step(
                    session,
                    {"command": "noop", "n": 0},
                    source_event_id=source_event_id,
                )
                step_id = step.id

        stale = (await claim_due_steps(db, "worker-a", source_event_id=source_event_id))[0]
        await expire_leases(db, [step_id])
        assert await reschedule_expired_leases(db) == 1
        fresh = (await claim_due_steps(db, "worker-b", source_event_id=source_event_id))[0]
        assert fresh.fence_token == stale.fence_token + 1

        entered: list[int] = []

        async def fake_execute(settings, forge_config, session_factory, metadata):
            entered.append(metadata["n"])

        monkeypatch.setattr(step_runtime, "execute_run_command", fake_execute)

        await execute_claimed_step(db, object(), object(), stale)

        assert entered == []
        rows = await step_rows(db)
        row = rows[step_id]
        assert row.status == "running"
        assert row.lease_owner == "worker-b"
        assert row.fence_token == fresh.fence_token
        assert row.attempt == 1, "the new owner's accounting is untouched"
