"""R28-23: bounded admission and fair use — BEFORE preemptive scheduling.

The pure half (:mod:`forge.adaptive.admission`) is a total function of
its inputs: the four live counts and the policy. Every boundary is pinned
by its own test (limit-1 admits, limit refuses), every refusal is typed,
and the reason sentence carries the observed count against the limit so
an operator can explain why a task was refused. The wiring half (the
/implement path) parks a refused run as ``blocked(fair_use_denied: …)``
with a journaled note, and never constructs a planner prompt.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive.admission import (
    ENV_MAX_ACTIVE_PER_PROJECT,
    ENV_MAX_QUEUED_RUNS,
    ENV_MAX_RUNS_PER_ISSUE,
    ENV_USER_RUNS_PER_HOUR,
    QUEUED_STATUSES,
    AdmissionDecision,
    AdmissionPolicy,
    ExecutionLease,
    LeaseOccupancy,
    NativeStatus,
    RefusalReason,
    admission_report,
    check_admission,
    clear_native_start_intent,
    definite_start_refusal,
    lease_occupancy,
    lease_snapshot,
    native_probe_for,
    reconcile_draining,
    record_native_handle,
    record_native_start_intent,
    register_native_probe,
    release_lease,
    release_run_leases,
    saturation_report,
    try_acquire_lease,
)
from forge.durable import FlowRun
from forge.durable.controller import FlowStatus
from forge.models.base import Base
from tests.fixtures.fake_gitlab import FakeGitLab
from tests.test_runs_service import ISSUE_IID, make_service

PROJECT_ID = 42


class TestPolicyFromEnv:
    def test_defaults_when_nothing_is_set(self):
        assert AdmissionPolicy.from_env({}) == AdmissionPolicy()

    def test_reads_the_process_environment_by_default(self, monkeypatch):
        monkeypatch.setenv(ENV_MAX_ACTIVE_PER_PROJECT, "7")
        policy = AdmissionPolicy.from_env()
        assert policy.max_active_per_project == 7
        assert policy.max_queued_runs == 10  # untouched default

    def test_every_dimension_is_overridable(self):
        policy = AdmissionPolicy.from_env(
            {
                ENV_MAX_ACTIVE_PER_PROJECT: "1",
                ENV_MAX_QUEUED_RUNS: "2",
                ENV_MAX_RUNS_PER_ISSUE: "3",
                ENV_USER_RUNS_PER_HOUR: "4",
            }
        )
        assert policy == AdmissionPolicy(1, 2, 3, 4)

    def test_junk_fails_closed_naming_the_variable(self):
        with pytest.raises(ValueError, match=ENV_MAX_RUNS_PER_ISSUE):
            AdmissionPolicy.from_env({ENV_MAX_RUNS_PER_ISSUE: "many"})

    def test_zero_disables_the_dimension(self):
        policy = AdmissionPolicy.from_env({ENV_USER_RUNS_PER_HOUR: "0"})
        assert policy.max_user_runs_per_hour == 0


class TestBoundaries:
    """Each limit admits at limit-1 and refuses at the limit; the refusal
    is the typed reason for THAT dimension."""

    def test_all_below_the_bounds_admits(self):
        decision = check_admission(AdmissionPolicy(), 2, 9, 4, 5)
        assert decision.allowed is True
        assert decision.refusal is None
        assert decision.reason == "admitted within fair-use bounds"

    def test_issue_run_limit_boundary(self):
        policy = AdmissionPolicy(max_runs_per_issue=5)
        assert check_admission(policy, 0, 0, 4, 0).allowed is True
        refused = check_admission(policy, 0, 0, 5, 0)
        assert refused.allowed is False
        assert refused.refusal is RefusalReason.ISSUE_RUN_LIMIT

    def test_user_rate_limit_boundary(self):
        policy = AdmissionPolicy(max_user_runs_per_hour=6)
        assert check_admission(policy, 0, 0, 0, 5).allowed is True
        refused = check_admission(policy, 0, 0, 0, 6)
        assert refused.allowed is False
        assert refused.refusal is RefusalReason.USER_RATE_LIMIT

    def test_project_active_limit_boundary(self):
        policy = AdmissionPolicy(max_active_per_project=3)
        assert check_admission(policy, 2, 0, 0, 0).allowed is True
        refused = check_admission(policy, 3, 0, 0, 0)
        assert refused.allowed is False
        assert refused.refusal is RefusalReason.PROJECT_ACTIVE_LIMIT

    def test_queue_full_boundary(self):
        policy = AdmissionPolicy(max_queued_runs=10)
        assert check_admission(policy, 0, 9, 0, 0).allowed is True
        refused = check_admission(policy, 0, 10, 0, 0)
        assert refused.allowed is False
        assert refused.refusal is RefusalReason.QUEUE_FULL

    def test_a_disabled_dimension_never_refuses(self):
        policy = AdmissionPolicy(
            max_active_per_project=0,
            max_queued_runs=0,
            max_runs_per_issue=0,
            max_user_runs_per_hour=0,
        )
        assert check_admission(policy, 99, 99, 99, 99).allowed is True

    def test_burst_cannot_monopolize_via_wip_bound(self):
        """The acceptance shape: one project at WIP capacity parks new
        work instead of queueing it forever, and a waiting gate decision
        holds QUEUE capacity, never ACTIVE capacity."""
        policy = AdmissionPolicy(max_active_per_project=2, max_queued_runs=10)
        assert check_admission(policy, 2, 0, 0, 0).refusal is RefusalReason.PROJECT_ACTIVE_LIMIT
        assert check_admission(policy, 0, 10, 0, 0).refusal is RefusalReason.QUEUE_FULL

    def test_check_order_is_most_specific_first(self):
        policy = AdmissionPolicy()  # 5 issue / 6 user / 3 active / 10 queued
        assert check_admission(policy, 3, 10, 5, 6).refusal is RefusalReason.ISSUE_RUN_LIMIT
        assert check_admission(policy, 3, 10, 4, 6).refusal is RefusalReason.USER_RATE_LIMIT
        assert check_admission(policy, 3, 10, 4, 5).refusal is RefusalReason.PROJECT_ACTIVE_LIMIT
        assert check_admission(policy, 2, 10, 4, 5).refusal is RefusalReason.QUEUE_FULL

    def test_queued_statuses_are_pre_execution_only(self):
        assert QUEUED_STATUSES == frozenset(
            {"accepted", "preflight", "planning", "waiting_approval"}
        )


class TestOperatorExplanation:
    def test_the_reason_names_the_limit_and_the_observed_count(self):
        refused = check_admission(AdmissionPolicy(max_active_per_project=3), 3, 0, 0, 0)
        assert "project_active_limit" in refused.reason
        assert "limit of 3" in refused.reason

    def test_decision_snapshot_carries_counts_and_policy(self):
        decision = check_admission(AdmissionPolicy(max_queued_runs=4), 1, 2, 3, 0)
        assert decision.counts == {
            "active": 1,
            "queued": 2,
            "issue_runs": 3,
            "user_recent": 0,
        }
        assert decision.policy.max_queued_runs == 4

    def test_as_document_is_valid_json_with_the_required_fields(self):
        refused = check_admission(AdmissionPolicy(), 0, 10, 0, 0)
        round_tripped = json.loads(json.dumps(refused.as_document()))
        assert round_tripped["allowed"] is False
        assert round_tripped["refusal"] == "queue_full"
        assert round_tripped["policy"]["max_queued_runs"] == 10
        assert round_tripped["counts"]["queued"] == 10
        assert round_tripped["reason"]

    def test_admitted_decision_document_has_no_refusal(self):
        assert check_admission(AdmissionPolicy(), 0, 0, 0, 0).as_document()["refusal"] is None

    def test_decision_is_frozen_and_refusals_are_typed(self):
        assert isinstance(check_admission(AdmissionPolicy(), 0, 0, 0, 0), AdmissionDecision)
        assert isinstance(RefusalReason.QUEUE_FULL, RefusalReason)
        assert RefusalReason.QUEUE_FULL.value == "queue_full"


# ----------------------------------------------------------------------
# The /implement wiring (additive to the F16 identity admission)
# ----------------------------------------------------------------------


async def _seed_run(
    db,
    *,
    status: FlowStatus,
    issue_iid: int = ISSUE_IID,
    evidence: dict | None = None,
    minutes_ago: int = 0,
    run_id: str | None = None,
    status_reason: str | None = None,
) -> str:
    run_id = run_id or uuid4().hex
    async with db() as session:
        session.add(
            FlowRun(
                id=run_id,
                project_id=PROJECT_ID,
                issue_iid=issue_iid,
                provider="gitlab",
                status=status.value,
                status_reason=status_reason,
                evidence=evidence or {},
                created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
            )
        )
        await session.commit()
    return run_id


async def _run_row(db, run_id: str) -> FlowRun:
    async with db() as session:
        return (await session.execute(select(FlowRun).where(FlowRun.id == run_id))).scalar_one()


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


@pytest.fixture()
def fake_gitlab() -> FakeGitLab:
    fake = FakeGitLab()
    fake.seed_issue(ISSUE_IID, "title", "description")
    fake.seed_commit("main", "base-sha-1", "initial")
    return fake


class TestImplementWiring:
    async def test_per_issue_cap_refuses_the_next_run(self, db, fake_gitlab):
        # Five TERMINAL runs for the issue already — the default cap.
        for _ in range(5):
            await _seed_run(db, status=FlowStatus.FAILED)
        service = make_service(db, fake_gitlab)

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, "title", "description", "alice")

        row = await _run_row(db, run_id)
        assert row.status == FlowStatus.BLOCKED.value
        assert "fair_use_denied" in (row.status_reason or "")
        assert "issue_run_limit" in (row.status_reason or "")
        # The operator surface: the journaled note quotes the reason.
        bodies = [note["body"] for note in fake_gitlab.notes]
        assert any("fair-use admission refused for @alice" in body for body in bodies)

    async def test_active_wip_bound_parks_a_burst(self, db, fake_gitlab):
        # Three runs executing on OTHER issues (below, the per-issue cap
        # would not fire and the one-active-run-per-issue invariant would
        # divert a same-issue seed into the duplicate path instead).
        for number in range(3):
            await _seed_run(db, status=FlowStatus.WAITING_HARNESS, issue_iid=2000 + number)
        service = make_service(db, fake_gitlab)

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, "title", "description", "alice")

        row = await _run_row(db, run_id)
        assert row.status == FlowStatus.BLOCKED.value
        assert "project_active_limit" in (row.status_reason or "")

    async def test_user_hourly_rate_counts_requested_by(self, db, fake_gitlab):
        # Six runs requested by alice inside the trailing hour on other
        # issues (so the per-issue cap does not fire first).
        for number in range(6):
            await _seed_run(
                db,
                status=FlowStatus.FAILED,
                issue_iid=1000 + number,
                evidence={"requested_by": "alice"},
                minutes_ago=10,
            )
        service = make_service(db, fake_gitlab)

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, "title", "description", "alice")

        row = await _run_row(db, run_id)
        assert row.status == FlowStatus.BLOCKED.value
        assert "user_rate_limit" in (row.status_reason or "")

    async def test_hourly_window_excludes_stale_runs(self, db, fake_gitlab):
        # Six alice runs, but two hours old — outside the trailing hour.
        for number in range(6):
            await _seed_run(
                db,
                status=FlowStatus.FAILED,
                issue_iid=1000 + number,
                evidence={"requested_by": "alice"},
                minutes_ago=120,
            )
        service = make_service(db, fake_gitlab)

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, "title", "description", "alice")

        row = await _run_row(db, run_id)
        assert row.status == FlowStatus.WAITING_APPROVAL.value

    async def test_below_the_bounds_the_run_plans_as_before(self, db, fake_gitlab):
        # Two active runs (below the default WIP of 3), on other issues.
        for number in range(2):
            await _seed_run(db, status=FlowStatus.WAITING_HARNESS, issue_iid=3000 + number)
        service = make_service(db, fake_gitlab)

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, "title", "description", "alice")

        row = await _run_row(db, run_id)
        assert row.status == FlowStatus.WAITING_APPROVAL.value

    async def test_requested_by_is_journaled_at_creation(self, db, fake_gitlab):
        service = make_service(db, fake_gitlab)
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, "title", "description", "alice")
        row = await _run_row(db, run_id)
        assert (row.evidence or {}).get("requested_by") == "alice"


class TestExecutionLeaseAtGitLabDispatch:
    """R32-05 parity: the GitLab dispatch boundary takes the SAME lease
    the Azure and GitHub paths take — the second approval under a limit of
    one parks blocked(execution_capacity) with the snapshot in evidence
    and a journaled issue note, and the parked run never executed."""

    async def test_second_go_parks_execution_capacity(self, db, fake_gitlab, monkeypatch):
        from forge.adaptive.admission import AdmissionPolicy, lease_snapshot

        monkeypatch.setenv("FORGE_ADMISSION_MAX_ACTIVE_PER_PROJECT", "1")
        fake_gitlab.seed_issue(ISSUE_IID + 1, "second task", "second body")
        service = make_service(db, fake_gitlab)
        first = await service.start_run(PROJECT_ID, ISSUE_IID, "title", "description", "alice")
        second = await service.start_run(
            PROJECT_ID, ISSUE_IID + 1, "second task", "second body", "alice"
        )

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {first}", "alice", ISSUE_IID, author_user_id=11
        )
        assert (await _run_row(db, first)).status == FlowStatus.WAITING_CI.value

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {second}", "alice", ISSUE_IID + 1, author_user_id=11
        )
        parked = await _run_row(db, second)
        assert parked.status == FlowStatus.BLOCKED.value
        assert "execution_capacity" in (parked.status_reason or "")
        lease_evidence = dict(parked.evidence or {})["execution_lease"]
        assert lease_evidence["acquired"] is False
        assert lease_evidence["capacity"]["held"] == 1
        bodies = [note["body"] for note in fake_gitlab.notes]
        assert any("execution slot" in body for body in bodies)

        snapshot = await lease_snapshot(
            AdmissionPolicy(max_active_per_project=1), PROJECT_ID, db, provider="gitlab"
        )
        assert snapshot["held"] == 1  # the parked run holds nothing
        assert snapshot["completed"] == 0  # and consumed no execution attempt


# ----------------------------------------------------------------------
# NEXT-11: durable execution leases — the reservation at dispatch
# ----------------------------------------------------------------------


class TestExecutionLeases:
    """The slot reservation the /go dispatch takes: a CAS insert against
    the unique OPEN-slot index, held until terminal release, reclaimed
    lazily after worker death, idempotent per run."""

    async def test_acquires_up_to_the_limit_and_refuses_the_next(self, db):
        policy = AdmissionPolicy(max_active_per_project=3)

        leases = [
            await try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex) for _ in range(3)
        ]
        fourth = await try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex)

        assert sorted(lease.slot for lease in leases) == [1, 2, 3]  # distinct slots
        assert fourth is None  # the 4th dispatch refuses — no silent overbooking

    async def test_the_racing_pair_over_the_final_slot_produces_one_winner(self, db):
        policy = AdmissionPolicy(max_active_per_project=3)
        for _ in range(2):
            await try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex)

        first, second = await asyncio.gather(
            try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex),
            try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex),
        )

        winners = [lease for lease in (first, second) if lease is not None]
        assert len(winners) == 1  # the database index decided, not a read

    async def test_release_frees_the_slot_and_is_idempotent(self, db):
        policy = AdmissionPolicy(max_active_per_project=1)
        lease = await try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex)
        assert lease is not None
        assert await try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex) is None

        assert await release_lease(lease.lease_id, db, reason="terminal:ready_for_human")
        assert await release_lease(lease.lease_id, db) is False  # already released

        freed = await try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex)
        assert freed is not None and freed.slot == lease.slot  # the SAME slot reused

    async def test_a_terminal_release_via_the_run_frees_every_open_lease(self, db):
        policy = AdmissionPolicy(max_active_per_project=2)
        run_id = uuid4().hex
        assert await try_acquire_lease(policy, PROJECT_ID, db, run_id=run_id) is not None
        assert await try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex) is not None
        assert await try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex) is None

        released = await release_run_leases(db, run_id, reason="terminal:cancelled")

        assert released == 1
        assert await try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex) is not None

    async def test_a_dead_workers_lease_is_reclaimed_by_the_next_acquire(self, db):
        """The crash backstop: the run reached terminal status but the
        release never ran (worker death between dispatch and terminal
        transition handling) — the NEXT acquirer reclaims the slot."""
        policy = AdmissionPolicy(max_active_per_project=1)
        run_id = uuid4().hex
        await _seed_run(db, status=FlowStatus.WAITING_CI, run_id=run_id)
        async with db() as session:
            session.add(
                ExecutionLease(
                    id=uuid4().hex,
                    project_id=PROJECT_ID,
                    provider="gitlab",
                    run_id=run_id,
                    slot=1,
                )
            )
            await session.commit()
        assert (
            await try_acquire_lease(policy, PROJECT_ID, db, provider="gitlab") is None
        )  # still held

        # The run lands terminal WITHOUT any release running — the next
        # acquire observes it and reclaims the slot.
        async with db() as session:
            row = await session.get(FlowRun, run_id)
            row.status = FlowStatus.CANCELLED.value
            await session.commit()
        reclaimed = await try_acquire_lease(
            policy, PROJECT_ID, db, run_id=uuid4().hex, provider="gitlab"
        )
        assert reclaimed is not None

    async def test_acquisition_is_idempotent_per_run(self, db):
        """A re-driven dispatch (revival, reconciler recovery) must not
        double-reserve what the same run already holds."""
        policy = AdmissionPolicy(max_active_per_project=1)
        run_id = uuid4().hex
        first = await try_acquire_lease(policy, PROJECT_ID, db, run_id=run_id)
        again = await try_acquire_lease(policy, PROJECT_ID, db, run_id=run_id)

        assert first is not None and again is not None
        assert again.lease_id == first.lease_id

    async def test_a_disabled_limit_records_but_never_refuses(self, db):
        policy = AdmissionPolicy(max_active_per_project=0)

        leases = [await try_acquire_lease(policy, PROJECT_ID, db) for _ in range(5)]

        assert all(lease is not None for lease in leases)
        assert [lease.slot for lease in leases] == [1, 2, 3, 4, 5]

    async def test_connections_stay_independent_aggregates(self, db):
        """NEXT-12: identical project numbers on two connections are two
        capacities — canonical identity, never a shared pot."""
        policy = AdmissionPolicy(max_active_per_project=1)
        assert await try_acquire_lease(
            policy, PROJECT_ID, db, run_id=uuid4().hex, provider="gitlab"
        )
        assert (
            await try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex, provider="gitlab")
            is None
        )  # the connection's own limit is enforced
        azure = await try_acquire_lease(
            policy, PROJECT_ID, db, run_id=uuid4().hex, provider="azure_devops"
        )
        assert azure is not None  # the same project NUMBER, another capacity

        gitlab_view = await lease_snapshot(policy, PROJECT_ID, db, provider="gitlab")
        azure_view = await lease_snapshot(policy, PROJECT_ID, db, provider="azure_devops")
        assert gitlab_view == {"held": 1, "draining": 0, "completed": 0, "limit": 1, "available": 0}
        assert azure_view == {"held": 1, "draining": 0, "completed": 0, "limit": 1, "available": 0}


# ----------------------------------------------------------------------
# R32-06 (review 0fca1b7): one OPEN lease per RUN — the slot index alone
# let one run hold two slots. The idempotency unit is the run; the
# open-run index enforces it and the run-conflict loser reads the winner.
# ----------------------------------------------------------------------


class TestOneOpenLeasePerRun:
    async def test_the_database_refuses_a_second_open_lease_for_one_run(self, db):
        """The invariant itself: the partial unique index (not a helper's
        discipline) makes a second OPEN lease for the same run an
        IntegrityError; a RELEASED row never collides (the audit trail
        stays)."""
        from sqlalchemy.exc import IntegrityError

        policy = AdmissionPolicy(max_active_per_project=3)
        run_id = uuid4().hex
        first = await try_acquire_lease(policy, PROJECT_ID, db, run_id=run_id)
        assert first is not None

        async with db() as session:
            session.add(
                ExecutionLease(
                    id=uuid4().hex,
                    project_id=PROJECT_ID,
                    provider="gitlab",
                    run_id=run_id,
                    slot=2,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()

        # After the terminal release, history does not block a NEW lease.
        assert await release_run_leases(db, run_id, reason="terminal:cancelled") == 1
        again = await try_acquire_lease(policy, PROJECT_ID, db, run_id=run_id)
        assert again is not None and again.lease_id != first.lease_id

    async def test_two_racing_acquires_for_one_run_get_the_same_lease(self, tmp_path):
        """The R32-06 acceptance: two INDEPENDENT connections (a file-backed
        database — the in-memory StaticPool shares one connection and one
        transaction, which is not a race) both miss the pre-read; the
        loser's INSERT trips the open-run index and must read the winner
        back, not consume a second slot."""
        policy = AdmissionPolicy(max_active_per_project=3)
        run_id = uuid4().hex
        url = f"sqlite+aiosqlite:///{tmp_path}/leases.db"
        factories = []
        for _connection in range(2):
            engine = create_async_engine(
                url,
                connect_args={"check_same_thread": False, "timeout": 10},
                poolclass=StaticPool,
            )
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factories.append((async_sessionmaker(engine, expire_on_commit=False), engine))

        try:
            first, second = await asyncio.gather(
                try_acquire_lease(policy, PROJECT_ID, factories[0][0], run_id=run_id),
                try_acquire_lease(policy, PROJECT_ID, factories[1][0], run_id=run_id),
            )
        finally:
            for _factory, engine in factories:
                await engine.dispose()

        assert first is not None and second is not None
        assert second.lease_id == first.lease_id  # ONE reservation identity
        check = create_async_engine(url, connect_args={"check_same_thread": False})
        try:
            async with async_sessionmaker(check, expire_on_commit=False)() as session:
                open_rows = (
                    (
                        await session.execute(
                            select(ExecutionLease).where(
                                ExecutionLease.run_id == run_id,
                                ExecutionLease.released_at.is_(None),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
        finally:
            await check.dispose()
        assert len(open_rows) == 1  # exactly one slot held for the run

    async def test_a_run_conflict_after_a_missed_pre_read_adopts_the_winner(self, db):
        """The P02 schedule, characterized head-on: the winner landed where
        the acquirer's project-scoped pre-read cannot see it (the open-run
        index is global on run_id, the pre-read is not) — the CAS INSERT
        must trip the RUN constraint and return the existing winner, never
        a fresh slot."""
        policy = AdmissionPolicy(max_active_per_project=3)
        run_id = uuid4().hex
        winner_id = uuid4().hex
        async with db() as session:
            session.add(
                ExecutionLease(
                    id=winner_id,
                    project_id=PROJECT_ID + 999,  # a foreign row: the local
                    provider="",  # pre-read misses; the run index does not
                    run_id=run_id,
                    slot=2,
                )
            )
            await session.commit()

        adopted = await try_acquire_lease(policy, PROJECT_ID, db, run_id=run_id)

        assert adopted is not None
        assert adopted.lease_id == winner_id
        assert adopted.slot == 2

    async def test_the_disabled_limit_cannot_loop_on_an_unrelated_constraint(self, db):
        """R32-06: an IntegrityError that is neither the slot race nor the
        run race is RAISED — the unbounded disabled-limit loop must not
        mine it for retry signal forever."""
        from sqlalchemy.exc import IntegrityError

        class _PoisonedSession:
            """A real session whose INSERT commit fails with an unrelated
            constraint violation (a check constraint, say)."""

            def __init__(self, inner: AsyncSession) -> None:
                self._inner = inner

            async def __aenter__(self):
                await self._inner.__aenter__()
                return self

            async def __aexit__(self, *exc):
                return await self._inner.__aexit__(*exc)

            def add(self, obj):
                self._inner.add(obj)

            async def execute(self, *args, **kwargs):
                return await self._inner.execute(*args, **kwargs)

            async def rollback(self):
                await self._inner.rollback()

            async def commit(self):
                raise IntegrityError(
                    "INSERT INTO execution_leases ...",
                    None,
                    Exception("CHECK constraint failed: ck_execution_leases_slot"),
                )

        def poisoned_factory():
            return _PoisonedSession(db())

        policy = AdmissionPolicy(max_active_per_project=0)  # unbounded mode
        with pytest.raises(IntegrityError, match="ck_execution_leases_slot"):
            await try_acquire_lease(policy, PROJECT_ID, poisoned_factory, run_id=uuid4().hex)


class TestLeaseConflictKind:
    """The three distinct outcomes R32-06 demands: slot, run, unrelated."""

    @staticmethod
    def _error(message: str):
        from sqlalchemy.exc import IntegrityError

        return IntegrityError("INSERT ...", None, Exception(message))

    def test_postgresql_names_the_index_in_the_message(self):
        from forge.adaptive.admission import lease_conflict_kind

        assert (
            lease_conflict_kind(
                self._error(
                    'duplicate key value violates unique constraint "uq_execution_lease_slot"'
                )
            )
            == "uq_execution_lease_slot"
        )
        assert (
            lease_conflict_kind(
                self._error(
                    'duplicate key value violates unique constraint "uq_execution_lease_open_run"'
                )
            )
            == "uq_execution_lease_open_run"
        )

    def test_sqlite_reports_the_column_list(self):
        from forge.adaptive.admission import lease_conflict_kind

        assert (
            lease_conflict_kind(
                self._error(
                    "UNIQUE constraint failed: execution_leases.project_id, "
                    "execution_leases.provider, execution_leases.slot"
                )
            )
            == "uq_execution_lease_slot"
        )
        assert (
            lease_conflict_kind(self._error("UNIQUE constraint failed: execution_leases.run_id"))
            == "uq_execution_lease_open_run"
        )

    def test_unrelated_constraints_are_not_mineable(self):
        from forge.adaptive.admission import lease_conflict_kind

        assert lease_conflict_kind(self._error("FOREIGN KEY constraint failed: runs")) == ""
        assert lease_conflict_kind(self._error("NOT NULL constraint failed: x.y")) == ""
        assert lease_conflict_kind(self._error("checkpoint is bad")) == ""


def test_the_capacity_park_comment_names_the_next_action():
    """R32-05's shared renderer: capacity, not refusal — the /retry command,
    the operator knob, and the observed-against-limit counts."""
    from forge.adaptive.admission import execution_capacity_comment

    body = execution_capacity_comment("a" * 32, {"held": 3, "limit": 3, "available": 0})
    assert "parked" in body
    assert "3 of 3 slots held" in body
    assert "/retry " + "a" * 32 in body
    assert "FORGE_ADMISSION_MAX_ACTIVE_PER_PROJECT" in body

    unlimited = execution_capacity_comment("b" * 32, {"held": 7, "limit": None, "available": None})
    assert "7 slots held" in unlimited and " of " not in unlimited


# ----------------------------------------------------------------------
# NEXT-12: rejected requests, admitted work, execution attempts
# ----------------------------------------------------------------------


class TestAdmissionReport:
    """The three counters are SEPARABLE: a refused request never entered
    and consumed no execution attempt; admitted work waits in the queue;
    execution attempts are the leases actually held or completed."""

    async def test_the_three_counters_are_distinguishable(self, db):
        policy = AdmissionPolicy(max_active_per_project=2)
        # A request that never entered: parked at admission with the
        # fair-use refusal in its status_reason.
        await _seed_run(
            db,
            status=FlowStatus.BLOCKED,
            status_reason="fair_use_denied: queue_full — 10 against the limit of 10",
        )
        # Admitted work waiting to execute (distinct issues — the
        # one-active-run-per-issue partial index must not fire on seeds).
        await _seed_run(db, status=FlowStatus.WAITING_APPROVAL, issue_iid=ISSUE_IID + 1)
        await _seed_run(db, status=FlowStatus.PLANNING, issue_iid=ISSUE_IID + 2)
        # Execution attempts: one held, one completed.
        held = await try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex)
        done = await try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex)
        await release_lease(done.lease_id, db, reason="terminal:cancelled")

        report = await admission_report(policy, PROJECT_ID, db)

        assert report["rejected_requests"] == 1
        assert report["admitted_work"] == 2
        assert report["execution_attempts"]["held"] == 1
        assert report["execution_attempts"]["completed"] == 1
        assert report["policy"]["max_active_per_project"] == 2
        assert held is not None

    async def test_a_queue_full_refusal_consumes_no_execution_attempt(self, db):
        policy = AdmissionPolicy(max_active_per_project=2)
        before = await admission_report(policy, PROJECT_ID, db)

        await _seed_run(
            db,
            status=FlowStatus.BLOCKED,
            status_reason="fair_use_denied: queue_full — 10 against the limit of 10",
        )

        after = await admission_report(policy, PROJECT_ID, db)
        assert after["rejected_requests"] == before["rejected_requests"] + 1
        assert after["execution_attempts"] == before["execution_attempts"]
        assert after["admitted_work"] == before["admitted_work"]

    async def test_identity_denials_count_as_rejected_requests(self, db):
        policy = AdmissionPolicy()
        await _seed_run(db, status=FlowStatus.BLOCKED, status_reason="admission_denied: nope")

        report = await admission_report(policy, PROJECT_ID, db)

        assert report["rejected_requests"] == 1
        assert report["admitted_work"] == 0

    async def test_executing_work_is_not_admitted_work(self, db):
        policy = AdmissionPolicy()
        await _seed_run(db, status=FlowStatus.WAITING_CI, issue_iid=ISSUE_IID + 1)
        await _seed_run(db, status=FlowStatus.PROPOSING, issue_iid=ISSUE_IID + 2)

        report = await admission_report(policy, PROJECT_ID, db)

        assert report["admitted_work"] == 0
        assert report["terminal_runs"] == 0


class TestLifetimeCapWording:
    def test_the_issue_cap_refusal_does_not_suggest_draining(self):
        refused = check_admission(AdmissionPolicy(max_runs_per_issue=5), 0, 0, 5, 0)

        assert refused.refusal is RefusalReason.ISSUE_RUN_LIMIT
        assert "lifetime per-issue limit" in refused.reason
        assert "will not" in refused.reason  # draining will NOT reset it

    def test_the_capacity_refusals_still_suggest_draining(self):
        refused = check_admission(AdmissionPolicy(max_queued_runs=10), 0, 10, 0, 0)

        assert refused.refusal is RefusalReason.QUEUE_FULL
        assert "work drains" in refused.reason


class TestMigration024:
    """The R32-06 migration: reconcile pre-existing duplicate OPEN leases
    per run (release the later ones — history stays), then add the
    partial unique index; downgrade drops the invariant, keeps the data.
    Follows the TestMigration009 pattern (module-level alembic ops)."""

    @staticmethod
    def _load_migration():
        import importlib.util

        path = (
            Path(__file__).resolve().parent.parent
            / "alembic"
            / "versions"
            / "024_execution_lease_one_open_per_run.py"
        )
        spec = importlib.util.spec_from_file_location("migration_024", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _create_023_surface(conn):
        conn.execute(
            text(
                """
                CREATE TABLE execution_leases (
                    id VARCHAR(32) PRIMARY KEY,
                    project_id INTEGER NOT NULL,
                    provider VARCHAR(32) NOT NULL DEFAULT '',
                    run_id VARCHAR(32),
                    slot INTEGER NOT NULL,
                    acquired_at DATETIME NOT NULL,
                    released_at DATETIME,
                    release_reason VARCHAR(100),
                    CONSTRAINT ck_execution_leases_slot CHECK (slot >= 1)
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_execution_lease_slot ON execution_leases "
                "(project_id, provider, slot) WHERE released_at IS NULL"
            )
        )

    def test_upgrade_reconciles_duplicates_then_enforces_one_open_per_run(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import create_engine, inspect

        module = self._load_migration()
        engine = create_engine("sqlite:///:memory:")
        duplicated_run, healthy_run = uuid4().hex, uuid4().hex
        try:
            with engine.connect() as conn:
                self._create_023_surface(conn)
                # The R32-06 defect, live: ONE run holding TWO open slots
                # (both pre-reads missed; the loser of slot 1 won slot 2).
                conn.execute(
                    text(
                        "INSERT INTO execution_leases (id, project_id, run_id, slot, acquired_at)"
                        " VALUES (:a, 7, :r, 1, :early)"
                    ),
                    {"a": uuid4().hex, "r": duplicated_run, "early": "2026-01-01T00:00:00+00:00"},
                )
                conn.execute(
                    text(
                        "INSERT INTO execution_leases (id, project_id, run_id, slot, acquired_at)"
                        " VALUES (:a, 7, :r, 2, :late)"
                    ),
                    {"a": uuid4().hex, "r": duplicated_run, "late": "2026-06-01T00:00:00+00:00"},
                )
                # A healthy single open lease + released history: untouched.
                conn.execute(
                    text(
                        "INSERT INTO execution_leases (id, project_id, run_id, slot, acquired_at)"
                        " VALUES (:a, 7, :r, 3, :now)"
                    ),
                    {"a": uuid4().hex, "r": healthy_run, "now": "2026-07-01T00:00:00+00:00"},
                )
                conn.execute(
                    text(
                        "INSERT INTO execution_leases (id, project_id, run_id, slot, acquired_at,"
                        " released_at, release_reason) VALUES (:a, 7, :r, 1, :t, :t2, 'terminal:cancelled')"
                    ),
                    {
                        "a": uuid4().hex,
                        "r": uuid4().hex,
                        "t": "2026-01-01T00:00:00+00:00",
                        "t2": "2026-02-01T00:00:00+00:00",
                    },
                )
                conn.commit()

                ctx = MigrationContext.configure(conn)
                with Operations.context(ctx):
                    module.upgrade()

                indexes = {ix["name"] for ix in inspect(conn).get_indexes("execution_leases")}
                assert "uq_execution_lease_open_run" in indexes
                # The EARLIEST open lease survives; the later one is RELEASED
                # with the explicit reconcile reason — no row deleted.
                rows = conn.execute(
                    text(
                        "SELECT slot, released_at IS NOT NULL, release_reason "
                        "FROM execution_leases WHERE run_id = :r ORDER BY slot"
                    ),
                    {"r": duplicated_run},
                ).all()
                assert rows == [
                    (1, 0, None),
                    (2, 1, "reconciled: duplicate open lease per run (024, R32-06)"),
                ]
                # The invariant now holds and the healthy rows are untouched.
                duplicates = conn.execute(
                    text(
                        "SELECT count(*) FROM (SELECT run_id FROM execution_leases "
                        "WHERE released_at IS NULL GROUP BY run_id HAVING count(*) > 1)"
                    )
                ).scalar_one()
                assert duplicates == 0
                kept = conn.execute(
                    text("SELECT released_at IS NULL FROM execution_leases WHERE run_id = :r"),
                    {"r": healthy_run},
                ).scalar_one()
                assert kept == 1

                with Operations.context(ctx):
                    module.downgrade()
                assert "uq_execution_lease_open_run" not in {
                    ix["name"] for ix in inspect(conn).get_indexes("execution_leases")
                }
                assert conn.execute(text("SELECT count(*) FROM execution_leases")).scalar_one() == 4
        finally:
            engine.dispose()


class TestLeaseDraining:
    """R32-07: the reservation's lifetime split from the run's LOCAL
    status — a terminal FlowRun does not mean the dispatched native job
    stopped occupying capacity, so the lease DRAINS (slot still held)
    until the reconciler observes the native job terminal."""

    async def _drain_one(self, db, *, native_handle: str = "pipe-7") -> ExecutionLease:
        policy = AdmissionPolicy(max_active_per_project=1)
        lease = await try_acquire_lease(
            policy, PROJECT_ID, db, run_id=uuid4().hex, native_handle=native_handle
        )
        assert lease is not None
        assert lease.native_handle == native_handle
        assert await release_lease(
            lease.lease_id, db, reason="terminal:ready_for_human", native_completed=False
        )
        return await self._row(db, lease.lease_id)

    @staticmethod
    async def _row(db, lease_id: str) -> ExecutionLease:
        async with db() as session:
            return await session.get(ExecutionLease, lease_id)

    async def test_terminal_local_with_running_native_stays_draining(self, db):
        """The acceptance: the lease may NOT be released while the CI job
        still runs — the slot stays held and the next dispatch refuses."""
        policy = AdmissionPolicy(max_active_per_project=1)
        row = await self._drain_one(db)

        assert row.released_at is None  # NOT released...
        assert row.draining_at is not None  # ...but parked draining
        assert row.release_reason == "terminal:ready_for_human"
        snapshot = await lease_snapshot(policy, PROJECT_ID, db)
        assert snapshot["held"] == 1  # the slot is genuinely occupied
        assert snapshot["draining"] == 1  # and the operator sees WHY
        assert snapshot["completed"] == 0
        assert await try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex) is None

    async def test_native_observed_complete_releases_the_lease(self, db):
        policy = AdmissionPolicy(max_active_per_project=1)
        row = await self._drain_one(db, native_handle="pipe-done")

        released = await reconcile_draining(db, lambda handle: handle == "pipe-done")

        assert released == 1
        fresh = await self._row(db, row.id)
        assert fresh.released_at is not None
        assert fresh.release_reason == "reconciled: native job terminal"
        snapshot = await lease_snapshot(policy, PROJECT_ID, db)
        assert snapshot["held"] == 0 and snapshot["draining"] == 0
        freed = await try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex)
        assert freed is not None

    async def test_a_still_running_native_job_keeps_the_slot_held(self, db):
        policy = AdmissionPolicy(max_active_per_project=1)
        row = await self._drain_one(db)

        assert await reconcile_draining(db, lambda handle: False) == 0

        fresh = await self._row(db, row.id)
        assert fresh.released_at is None and fresh.draining_at is not None
        assert await try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex) is None

    async def test_a_provider_outage_keeps_the_lease_draining(self, db):
        """Absence and unknown stay distinct: a probe that cannot answer
        holds the capacity (uncertain occupancy), never frees it."""
        row = await self._drain_one(db)

        def outage(handle: str) -> bool:
            raise RuntimeError("provider unreachable")

        assert await reconcile_draining(db, outage) == 0
        fresh = await self._row(db, row.id)
        assert fresh.released_at is None and fresh.draining_at is not None

    async def test_an_async_provider_probe_is_awaited(self, db):
        row = await self._drain_one(db, native_handle="pipe-async")

        async def probe(handle: str) -> bool:
            return handle == "pipe-async"

        assert await reconcile_draining(db, probe) == 1
        assert (await self._row(db, row.id)).released_at is not None

    async def test_no_native_handle_released_on_terminal_backcompat(self, db):
        """The pre-R32-07 shape: a lease with no native correlation
        releases at the local terminal verdict — the columns stay NULL."""
        policy = AdmissionPolicy(max_active_per_project=1)
        lease = await try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex)
        assert lease is not None and lease.native_handle == ""

        assert await release_lease(lease.lease_id, db, reason="terminal:ready_for_human")

        row = await self._row(db, lease.lease_id)
        assert row.released_at is not None and row.draining_at is None
        freed = await try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex)
        assert freed is not None and freed.slot == lease.slot

    async def test_the_terminal_run_reclaim_refuses_to_free_a_draining_lease(self, db):
        """The R32-07 trap, head-on: the crash backstop that reclaims
        OPEN leases of TERMINAL runs must not swallow a draining lease —
        draining is exactly 'run terminal, native running'."""
        policy = AdmissionPolicy(max_active_per_project=1)
        run_id = uuid4().hex
        await _seed_run(db, status=FlowStatus.WAITING_CI, run_id=run_id)
        lease = await try_acquire_lease(
            policy, PROJECT_ID, db, run_id=run_id, provider="gitlab", native_handle="pipe-1"
        )
        assert lease is not None
        await release_lease(lease.lease_id, db, reason="terminal:ready", native_completed=False)

        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.CANCELLED.value
            await session.commit()

        # The next acquirer sees a terminal run BUT a draining lease — no slot.
        assert await try_acquire_lease(policy, PROJECT_ID, db, provider="gitlab") is None
        # The reconciler is the only path that frees it.
        assert await reconcile_draining(db, lambda handle: True) == 1
        reclaimed = await try_acquire_lease(policy, PROJECT_ID, db, provider="gitlab")
        assert reclaimed is not None

    async def test_the_native_handle_lands_after_dispatch_answers(self, db):
        """Dispatch takes the slot BEFORE the pipeline id exists; the
        correlation arrives with :func:`record_native_handle`."""
        lease = await try_acquire_lease(policy := AdmissionPolicy(), PROJECT_ID, db)
        assert lease is not None and lease.native_handle == ""

        assert await record_native_handle(lease.lease_id, "azure://run/1234", db)
        again = await try_acquire_lease(policy, PROJECT_ID, db, run_id="")
        assert again is not None
        # an unknown lease answers False, changes nothing
        assert await record_native_handle(uuid4().hex, "x", db) is False

    async def test_release_run_leases_can_drain_the_whole_run(self, db):
        policy = AdmissionPolicy(max_active_per_project=2)
        run_id = uuid4().hex
        lease = await try_acquire_lease(policy, PROJECT_ID, db, run_id=run_id)
        assert lease is not None

        assert await release_run_leases(
            db, run_id, reason="terminal:paused", native_completed=False
        )

        row = await self._row(db, lease.lease_id)
        assert row.released_at is None and row.draining_at is not None
        snapshot = await lease_snapshot(policy, PROJECT_ID, db)
        assert snapshot == {"held": 1, "draining": 1, "completed": 0, "limit": 2, "available": 1}
        # and the reconciler still finishes the story
        assert await reconcile_draining(db, lambda handle: True) == 1
        assert (await self._row(db, lease.lease_id)).released_at is not None

    async def test_a_draining_release_is_idempotent_and_still_releasable(self, db):
        row = await self._drain_one(db)

        assert await release_lease(
            row.id, db, reason="terminal:ready_for_human", native_completed=False
        )  # a second drain request holds the same state

        assert await release_lease(row.id, db, reason="operator: forced release")
        fresh = await self._row(db, row.id)
        assert fresh.released_at is not None
        assert fresh.release_reason == "operator: forced release"


class TestMigration025:
    """The R32-07 migration: native correlation + draining columns on the
    lease, with a partial index for the reconciler's worklist; downgrade
    refuses while draining leases still hold their slots (the 023
    precedent — a draining lease is a live reservation whose probe key
    lives in these columns)."""

    @staticmethod
    def _load_migration():
        import importlib.util

        path = (
            Path(__file__).resolve().parent.parent
            / "alembic"
            / "versions"
            / "025_lease_native_handle_and_draining.py"
        )
        spec = importlib.util.spec_from_file_location("migration_025", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _create_024_surface(conn):
        conn.execute(
            text(
                """
                CREATE TABLE execution_leases (
                    id VARCHAR(32) PRIMARY KEY,
                    project_id INTEGER NOT NULL,
                    provider VARCHAR(32) NOT NULL DEFAULT '',
                    run_id VARCHAR(32),
                    slot INTEGER NOT NULL,
                    acquired_at DATETIME NOT NULL,
                    released_at DATETIME,
                    release_reason VARCHAR(100),
                    CONSTRAINT ck_execution_leases_slot CHECK (slot >= 1)
                )
                """
            )
        )

    def test_upgrade_adds_the_columns_and_the_reconciler_index(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import create_engine, inspect

        module = self._load_migration()
        engine = create_engine("sqlite:///:memory:")
        try:
            with engine.connect() as conn:
                self._create_024_surface(conn)
                conn.execute(
                    text(
                        "INSERT INTO execution_leases (id, project_id, run_id, slot, acquired_at)"
                        " VALUES (:a, 7, :r, 1, :t)"
                    ),
                    {"a": uuid4().hex, "r": uuid4().hex, "t": "2026-01-01T00:00:00+00:00"},
                )
                conn.commit()

                ctx = MigrationContext.configure(conn)
                with Operations.context(ctx):
                    module.upgrade()

                columns = {col["name"] for col in inspect(conn).get_columns("execution_leases")}
                assert {"native_handle", "draining_at"} <= columns
                indexes = {ix["name"] for ix in inspect(conn).get_indexes("execution_leases")}
                assert "ix_execution_leases_draining" in indexes
                # Pre-existing rows keep the back-compat shape: both columns NULL.
                row = conn.execute(
                    text("SELECT native_handle, draining_at FROM execution_leases")
                ).one()
                assert row == (None, None)

                # The new columns carry the draining state end to end.
                conn.execute(
                    text(
                        "UPDATE execution_leases SET native_handle = 'pipe-1',"
                        " draining_at = '2026-09-23T00:00:00+00:00'"
                    )
                )
                conn.commit()
                with Operations.context(ctx):
                    try:
                        module.downgrade()
                    except RuntimeError as exc:
                        assert "draining" in str(exc)
                    else:
                        raise AssertionError("downgrade must refuse while leases drain")
                # Released history downgrades cleanly.
                conn.execute(
                    text("UPDATE execution_leases SET released_at = '2026-09-23T01:00:00+00:00'")
                )
                conn.commit()
                with Operations.context(ctx):
                    module.downgrade()
                columns = {col["name"] for col in inspect(conn).get_columns("execution_leases")}
                assert "native_handle" not in columns and "draining_at" not in columns
                assert conn.execute(text("SELECT count(*) FROM execution_leases")).scalar_one() == 1
        finally:
            engine.dispose()


# ----------------------------------------------------------------------
# Q35-04: occupancy — the derived state machine over the intent columns
# ----------------------------------------------------------------------


class TestLeaseOccupancyStates:
    """Occupancy is DERIVED, never stored: the five states are pure
    functions of (intent, handle, draining, released)."""

    @staticmethod
    async def _lease(db, **columns) -> ExecutionLease:
        run_id = uuid4().hex
        lease = await try_acquire_lease(AdmissionPolicy(), PROJECT_ID, db, run_id=run_id)
        assert lease is not None
        async with db() as session:
            row = await session.get(ExecutionLease, lease.lease_id)
            for name, value in columns.items():
                setattr(row, name, value)
            await session.commit()
            await session.refresh(row)
        return row

    async def test_never_dispatched_without_an_intent(self, db):
        row = await self._lease(db)
        assert lease_occupancy(row) is LeaseOccupancy.NEVER_DISPATCHED

    async def test_dispatched_unknown_with_intent_and_no_handle(self, db):
        row = await self._lease(db, native_intent_at=datetime.now(timezone.utc))
        assert lease_occupancy(row) is LeaseOccupancy.DISPATCHED_UNKNOWN

    async def test_native_running_with_a_handle(self, db):
        row = await self._lease(
            db,
            native_intent_at=datetime.now(timezone.utc),
            native_handle="gitlab:pipeline:42:7",
        )
        assert lease_occupancy(row) is LeaseOccupancy.NATIVE_RUNNING

    async def test_draining_parks_before_terminal(self, db):
        row = await self._lease(
            db,
            native_intent_at=datetime.now(timezone.utc),
            draining_at=datetime.now(timezone.utc),
        )
        assert lease_occupancy(row) is LeaseOccupancy.DRAINING

    async def test_observed_terminal_once_released(self, db):
        row = await self._lease(
            db,
            native_intent_at=datetime.now(timezone.utc),
            native_handle="github:actions:o/r:9",
            released_at=datetime.now(timezone.utc),
        )
        assert lease_occupancy(row) is LeaseOccupancy.OBSERVED_TERMINAL


class TestNativeStartIntentApi:
    """The Q35-04 writes: intent before the provider call, cleared on a
    proven pre-call abort, handle attached when the provider answers."""

    async def test_intent_and_handle_compose_the_running_state(self, db):
        run_id = uuid4().hex
        lease = await try_acquire_lease(AdmissionPolicy(), PROJECT_ID, db, run_id=run_id)
        assert await record_native_start_intent(db, run_id, "github:workflow:o/r/w@b") == 1
        assert await record_native_handle(lease.lease_id, "github:actions:o/r:501", db)
        row = await TestNativeStartIntentApi._row(db, lease.lease_id)
        assert lease_occupancy(row) is LeaseOccupancy.NATIVE_RUNNING

    async def test_intent_is_idempotent_and_refreshes_the_marker(self, db):
        run_id = uuid4().hex
        await try_acquire_lease(AdmissionPolicy(), PROJECT_ID, db, run_id=run_id)
        assert await record_native_start_intent(db, run_id, "gitlab:pipeline:42@a") == 1
        assert await record_native_start_intent(db, run_id, "gitlab:pipeline:42@b") == 1
        row = await open_run_lease(db, run_id)
        assert row.native_intent_ref == "gitlab:pipeline:42@b"

    async def test_clear_restores_the_never_dispatched_proof(self, db):
        run_id = uuid4().hex
        await try_acquire_lease(AdmissionPolicy(), PROJECT_ID, db, run_id=run_id)
        await record_native_start_intent(db, run_id, "azure:pipeline:P:9@b")
        assert await clear_native_start_intent(db, run_id) == 1
        row = await open_run_lease(db, run_id)
        assert row.native_intent_at is None and row.native_intent_ref is None

    async def test_a_released_lease_accepts_no_new_intent(self, db):
        run_id = uuid4().hex
        lease = await try_acquire_lease(AdmissionPolicy(), PROJECT_ID, db, run_id=run_id)
        await release_lease(lease.lease_id, db)
        assert await record_native_start_intent(db, run_id, "gitlab:pipeline:42@b") == 0

    @staticmethod
    async def _row(db, lease_id: str) -> ExecutionLease:
        async with db() as session:
            return await session.get(ExecutionLease, lease_id)


async def open_run_lease(db, run_id: str) -> ExecutionLease:
    async with db() as session:
        return (
            (
                await session.execute(
                    select(ExecutionLease).where(
                        ExecutionLease.run_id == run_id,
                        ExecutionLease.released_at.is_(None),
                    )
                )
            )
            .scalars()
            .one()
        )


class TestNativeProbeRegistry:
    """The reconciler's routing table: probe keys carry their provider
    prefix; an unregistered provider is UNDECIDABLE, never a guess."""

    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        from forge.adaptive import admission

        saved = dict(admission._NATIVE_PROBES)
        admission._NATIVE_PROBES.clear()
        yield
        admission._NATIVE_PROBES.clear()
        admission._NATIVE_PROBES.update(saved)

    def test_register_and_lookup_by_prefix(self):
        probe = lambda key: True  # noqa: E731 — trivial test probe
        register_native_probe("github", probe)
        assert native_probe_for("github:actions:o/r:501") is probe
        assert native_probe_for("gitlab:pipeline:42:7") is None

    def test_a_bare_legacy_key_has_no_provider(self):
        assert native_probe_for("pipe-7") is None

    def test_reregistration_replaces_the_previous_probe(self):
        first = lambda key: True  # noqa: E731
        second = lambda key: False  # noqa: E731
        register_native_probe("gitlab", first)
        register_native_probe("gitlab", second)
        assert native_probe_for("gitlab:pipeline:1") is second

    async def test_reconcile_uses_the_registry_when_no_probe_is_given(self, db):
        run_id = uuid4().hex
        lease = await try_acquire_lease(
            AdmissionPolicy(), PROJECT_ID, db, run_id=run_id, native_handle="github:actions:o/r:9"
        )
        await release_lease(lease.lease_id, db, native_completed=False)
        register_native_probe("github", lambda key: NativeStatus.TERMINAL)

        assert await reconcile_draining(db) == 1  # routed by the prefix


class TestDefiniteStartRefusal:
    """The evidence classifier for pre-call aborts: only a definitive
    non-retryable client error PROVES the provider never accepted."""

    def test_definitive_client_errors_prove_refusal(self):
        for status in (400, 401, 403, 404, 409, 422):
            assert definite_start_refusal(status) is True

    def test_retryable_and_server_errors_stay_ambiguous(self):
        for status in (408, 425, 429, 500, 502, 503):
            assert definite_start_refusal(status) is False

    def test_success_and_redirect_statuses_are_not_refusals(self):
        for status in (200, 201, 202, 301):
            assert definite_start_refusal(status) is False


class TestMigration027:
    """The Q35-04 migration: the native-start intent columns plus the
    occupancy watchlist index; downgrade refuses while an OPEN lease
    still carries an intent (dropping the columns would recast
    dispatched-unknown capacity as never-dispatched — premature release)."""

    @staticmethod
    def _load_migration():
        import importlib.util

        path = (
            Path(__file__).resolve().parent.parent
            / "alembic"
            / "versions"
            / "027_lease_native_start_intent.py"
        )
        spec = importlib.util.spec_from_file_location("migration_027", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _create_026_surface(conn):
        conn.execute(
            text(
                """
                CREATE TABLE execution_leases (
                    id VARCHAR(32) PRIMARY KEY,
                    project_id INTEGER NOT NULL,
                    provider VARCHAR(32) NOT NULL DEFAULT '',
                    run_id VARCHAR(32),
                    slot INTEGER NOT NULL,
                    acquired_at DATETIME NOT NULL,
                    native_handle VARCHAR(256),
                    draining_at DATETIME,
                    released_at DATETIME,
                    release_reason VARCHAR(100),
                    CONSTRAINT ck_execution_leases_slot CHECK (slot >= 1)
                )
                """
            )
        )

    def test_upgrade_adds_the_intent_columns_and_the_watchlist_index(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import create_engine, inspect

        module = self._load_migration()
        engine = create_engine("sqlite:///:memory:")
        try:
            with engine.connect() as conn:
                self._create_026_surface(conn)
                conn.execute(
                    text(
                        "INSERT INTO execution_leases (id, project_id, run_id, slot, acquired_at)"
                        " VALUES (:a, 7, :r, 1, :t)"
                    ),
                    {"a": uuid4().hex, "r": uuid4().hex, "t": "2026-01-01T00:00:00+00:00"},
                )
                conn.commit()

                ctx = MigrationContext.configure(conn)
                with Operations.context(ctx):
                    module.upgrade()

                columns = {col["name"] for col in inspect(conn).get_columns("execution_leases")}
                assert {"native_intent_at", "native_intent_ref"} <= columns
                indexes = {ix["name"] for ix in inspect(conn).get_indexes("execution_leases")}
                assert "ix_execution_leases_native_intent" in indexes
                # Pre-existing leases dispatched no intent we can invent:
                # NULL columns ARE the never-dispatched behavior (025 posture).
                row = conn.execute(
                    text("SELECT native_intent_at, native_intent_ref FROM execution_leases")
                ).one()
                assert row == (None, None)

                # An OPEN lease with a live intent blocks the downgrade.
                conn.execute(
                    text(
                        "UPDATE execution_leases SET native_intent_at ="
                        " '2026-09-23T00:00:00+00:00',"
                        " native_intent_ref = 'gitlab:pipeline:42@factory/7/ab'"
                    )
                )
                conn.commit()
                with Operations.context(ctx):
                    try:
                        module.downgrade()
                    except RuntimeError as exc:
                        assert "native-start intent" in str(exc)
                    else:
                        raise AssertionError("downgrade must refuse while intents are live")
                # Released history downgrades cleanly.
                conn.execute(
                    text("UPDATE execution_leases SET released_at = '2026-09-23T01:00:00+00:00'")
                )
                conn.commit()
                with Operations.context(ctx):
                    module.downgrade()
                columns = {col["name"] for col in inspect(conn).get_columns("execution_leases")}
                assert "native_intent_at" not in columns
                assert "native_intent_ref" not in columns
                assert conn.execute(text("SELECT count(*) FROM execution_leases")).scalar_one() == 1
        finally:
            engine.dispose()


class TestSaturationReport:
    """R36-21 (issue #280): the saturation signals — the operator's
    "when does it queue / pause / refuse" view, derived from the same
    durable occupancy columns every other report reads."""

    async def test_occupied_vs_limit_counts_every_held_slot(self, db):
        policy = AdmissionPolicy(max_active_per_project=2)
        for index in range(2):
            assert (await try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex)) is not None
        report = await saturation_report(policy, PROJECT_ID, db)
        assert report["execution.occupied_vs_limit"] == {
            "occupied": 2,
            "limit": 2,
            "available": 0,
            "at_limit": True,
        }

    async def test_unknown_age_reads_the_oldest_dispatched_unknown(self, db):
        from datetime import UTC, datetime, timedelta

        policy = AdmissionPolicy(max_active_per_project=3)
        run_id = uuid4().hex
        assert await try_acquire_lease(policy, PROJECT_ID, db, run_id=run_id) is not None
        past = datetime.now(UTC) - timedelta(minutes=10)
        await record_native_start_intent(db, run_id, "github:workflow:o/r/w@b", now=past)
        report = await saturation_report(
            policy, PROJECT_ID, db, now=datetime.now(UTC) - timedelta(seconds=30)
        )
        assert report["native_start.unknown_count"] == 1
        # ~9.5 minutes of unknown age at the report's clock.
        assert 500 <= report["native_start.unknown_age"] <= 700

    async def test_draining_counts_as_unknown_too(self, db):
        policy = AdmissionPolicy(max_active_per_project=2)
        run_id = uuid4().hex
        lease = await try_acquire_lease(policy, PROJECT_ID, db, run_id=run_id)
        assert lease is not None
        await record_native_start_intent(db, run_id, "gitlab:pipeline:42@b")
        assert await release_lease(lease.lease_id, db, native_completed=False) is True
        report = await saturation_report(policy, PROJECT_ID, db)
        assert report["native_start.unknown_count"] == 1
        assert report["native_start.unknown_age"] is not None

    async def test_a_native_running_slot_is_occupied_but_not_unknown(self, db):
        policy = AdmissionPolicy(max_active_per_project=2)
        run_id = uuid4().hex
        lease = await try_acquire_lease(policy, PROJECT_ID, db, run_id=run_id)
        assert lease is not None
        await record_native_start_intent(db, run_id, "azure:pipeline:P:1@b")
        assert await record_native_handle(lease.lease_id, "azure:run:99", db) is True
        report = await saturation_report(policy, PROJECT_ID, db)
        assert report["execution.occupied_vs_limit"]["occupied"] == 1
        assert report["native_start.unknown_count"] == 0
        assert report["native_start.unknown_age"] is None

    async def test_a_disabled_limit_reports_nulls_never_a_fake_number(self, db):
        policy = AdmissionPolicy(max_active_per_project=0)
        report = await saturation_report(policy, PROJECT_ID, db)
        occupied = report["execution.occupied_vs_limit"]
        assert occupied["limit"] is None and occupied["available"] is None
        assert occupied["at_limit"] is False

    async def test_the_escalation_path_is_named_not_left_to_guesswork(self, db):
        report = await saturation_report(AdmissionPolicy(), PROJECT_ID, db)
        assert "reconcile_draining" in report["escalation"]
        assert "override" in report["escalation"]

    async def test_provider_scoping_keeps_connections_independent(self, db):
        policy = AdmissionPolicy(max_active_per_project=2)
        assert (
            await try_acquire_lease(policy, PROJECT_ID, db, run_id=uuid4().hex, provider="a")
            is not None
        )
        scoped = await saturation_report(policy, PROJECT_ID, db, provider="a")
        other = await saturation_report(policy, PROJECT_ID, db, provider="b")
        assert scoped["execution.occupied_vs_limit"]["occupied"] == 1
        assert other["execution.occupied_vs_limit"]["occupied"] == 0
