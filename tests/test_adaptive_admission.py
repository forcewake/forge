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

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive.admission import (
    ENV_MAX_ACTIVE_PER_PROJECT,
    ENV_MAX_QUEUED_RUNS,
    ENV_MAX_RUNS_PER_ISSUE,
    ENV_USER_RUNS_PER_HOUR,
    QUEUED_STATUSES,
    AdmissionDecision,
    AdmissionPolicy,
    RefusalReason,
    check_admission,
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
) -> str:
    run_id = uuid4().hex
    async with db() as session:
        session.add(
            FlowRun(
                id=run_id,
                project_id=PROJECT_ID,
                issue_iid=issue_iid,
                provider="gitlab",
                status=status.value,
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
