"""Tier 1 + Tier 2 revival: failure classification, bounded auto-revive, /retry.

A transient death arms ``revive`` evidence and the reconciler re-fires the
frozen advance leg on the SAME branch (attempt base = last candidate); a
fatal one parks ``blocked`` with the precise reason and never auto-retries.
The operator's ``/retry`` revives a dead run in place — no new run id, no
re-planning.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import Settings
from forge.durable import ActionLog, FlowRun, FlowStatus
from forge.durable.controller import ALLOWED_TRANSITIONS, Controller
from forge.models.base import Base
from forge.repository import ChangesetWriter
from forge.runs import RunService
from forge.runs.candidate import attempt_base_for
from forge.runs.failure import (
    REVIVE_BACKOFF_MAX_SECONDS,
    FailureClass,
    arm_revive,
    classify_terminal_failure,
    revive_at,
    revive_backoff_seconds,
    revive_count,
)
from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer
from tests.fixtures.fake_gitlab import FakeGitLab

PROJECT_ID = 42
ISSUE_IID = 7
CANDIDATE_SHA = "c" * 40


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        FORGE_APPROVERS="alice",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        FORGE_IMPLEMENTER_BACKEND="ci_harness",
    )
    values.update(overrides)
    return Settings(**values)


def make_service(db, fake_gitlab, **overrides) -> RunService:
    values = dict(
        session_factory=db,
        gitlab=fake_gitlab,
        settings=make_settings(),
        writer_class=ChangesetWriter,
        planner=StubPlanner(),
        implementer=StubImplementer(),
        reviewer=StubReviewer(),
    )
    values.update(overrides)
    return RunService(**values)


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
    fake.seed_issue(ISSUE_IID, "Add a widget", "Widgets make the app better.")
    fake.seed_commit("main", "base-sha-1", "initial")
    return fake


async def seed_run(
    db,
    *,
    status: str = "proposing",
    candidate_shas: list[str] | None = None,
    evidence: dict | None = None,
    commit_cycle: int = 1,
) -> str:
    """One run parked mid-advance (the statuses a failure can leave it in)."""
    run_id = uuid.uuid4().hex
    async with db() as session:
        session.add(
            FlowRun(
                id=run_id,
                project_id=PROJECT_ID,
                issue_iid=ISSUE_IID,
                provider="gitlab",
                status=status,
                base_sha="base-sha-1",
                candidate_shas=list(
                    candidate_shas if candidate_shas is not None else [CANDIDATE_SHA]
                ),
                commit_cycle=commit_cycle,
                evidence={"backend": "ci_harness", **(evidence or {})},
            )
        )
        await session.commit()
    return run_id


async def get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


async def get_actions(db, run_id: str, kind: str) -> list[ActionLog]:
    async with db() as session:
        rows = (
            (
                await session.execute(
                    select(ActionLog)
                    .where(ActionLog.flow_run_id == run_id, ActionLog.action_kind == kind)
                    .order_by(ActionLog.id)
                )
            )
            .scalars()
            .all()
        )
        session.expunge_all()
        return list(rows)


# ----------------------------------------------------------------------
# Failure classification (the contract table)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        # transport-shaped noise → transient
        ("harness_start_failed: 500 Internal Server Error", FailureClass.TRANSIENT),
        ("harness_start_failed: 502 Bad Gateway", FailureClass.TRANSIENT),
        ("harness_start_failed: 429 too many requests", FailureClass.TRANSIENT),
        ("mr_failed: connection reset by peer", FailureClass.TRANSIENT),
        ("commit_failed: upstream request timed out", FailureClass.TRANSIENT),
        ("proposal_failed: LLM provider overloaded, retry later", FailureClass.TRANSIENT),
        ("harness_start_failed: no runner available", FailureClass.TRANSIENT),
        ("harness_start_failed:", FailureClass.TRANSIENT),  # empty dispatch error
        # quality signals and configuration errors → fatal
        ("harness_start_failed: 422 Unexpected inputs", FailureClass.FATAL),
        ("harness_start_failed: 404 workflow not found", FailureClass.FATAL),
        ("harness_start_failed: 401 Unauthorized", FailureClass.FATAL),
        ("proposal_failed: driver exit code 1", FailureClass.FATAL),
        ("commit_unknown_outcome", FailureClass.FATAL),
        ("mr_unknown_outcome", FailureClass.FATAL),
        ("backend_config: missing workflow", FailureClass.FATAL),
        ("commit_cycles_exhausted: 3 of 3 commit cycles used", FailureClass.FATAL),
        ("planning_failed: proxy down", FailureClass.FATAL),
        ("cancelled while failing: boom", FailureClass.FATAL),
        # unclassifiable → fatal (a human decides)
        ("something inexplicable", FailureClass.FATAL),
    ],
)
def test_failure_classification_table(reason: str, expected: FailureClass):
    assert classify_terminal_failure(reason) is expected


def test_revive_backoff_is_monotone_and_bounded():
    delays = [revive_backoff_seconds(count) for count in range(1, 8)]
    assert delays == sorted(delays)
    assert all(delay <= REVIVE_BACKOFF_MAX_SECONDS for delay in delays)
    assert delays[0] > 0


def test_arm_revive_evidence_increments_and_sets_the_due_time():
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    armed = arm_revive(1, "harness_start_failed: 500", now=now)
    assert armed["revive"]["count"] == 2
    assert armed["revive"]["reason"] == "harness_start_failed: 500"
    due = datetime.fromisoformat(armed["revive"]["at"])
    assert revive_backoff_seconds(2) <= (due - now).total_seconds() <= revive_backoff_seconds(2) + 1


# ----------------------------------------------------------------------
# Graph edges: the revival walk failed/blocked → proposing
# ----------------------------------------------------------------------


def test_revival_edges_lead_back_to_proposing():
    assert ALLOWED_TRANSITIONS[FlowStatus.FAILED] == {FlowStatus.PROPOSING}
    assert ALLOWED_TRANSITIONS[FlowStatus.BLOCKED] == {FlowStatus.PROPOSING}
    assert ALLOWED_TRANSITIONS[FlowStatus.CANCELLED] == set()
    # ready_for_human is a done deal — no revival edge there.
    assert ALLOWED_TRANSITIONS[FlowStatus.READY_FOR_HUMAN] == set()


# ----------------------------------------------------------------------
# Tier 1: _to_terminal classification → arm / park
# ----------------------------------------------------------------------


async def test_transient_failure_arms_the_revive_evidence(db, fake_gitlab):
    service = make_service(db, fake_gitlab)
    run_id = await seed_run(db, status="proposing")

    await service._to_terminal(
        run_id, FlowStatus.FAILED, "harness_start_failed: 500 Internal Server Error"
    )

    run = await get_run(db, run_id)
    # The run stays alive (non-terminal, same resumable status) — it waits.
    assert run.status == "proposing"
    assert revive_count(run.evidence) == 1
    assert revive_at(run.evidence) is not None
    assert revive_at(run.evidence) > datetime.now(timezone.utc)


async def test_fatal_failure_parks_blocked_with_the_reason(db, fake_gitlab):
    service = make_service(db, fake_gitlab)
    run_id = await seed_run(db, status="proposing")
    reason = "harness_start_failed: 422 Unexpected inputs"

    await service._to_terminal(run_id, FlowStatus.FAILED, reason)

    run = await get_run(db, run_id)
    assert run.status == FlowStatus.BLOCKED.value
    assert reason in run.status_reason
    assert revive_evidence_is_absent(run)


def revive_evidence_is_absent(run: FlowRun) -> bool:
    return not (run.evidence or {}).get("revive")


async def test_failure_outside_the_advance_legs_never_arms(db, fake_gitlab):
    """Planning has no fix-forward continuation — even a 5xx parks blocked."""
    service = make_service(db, fake_gitlab)
    run_id = await seed_run(db, status="planning", candidate_shas=[])

    await service._to_terminal(
        run_id, FlowStatus.FAILED, "harness_start_failed: 500 Internal Server Error"
    )

    run = await get_run(db, run_id)
    assert run.status == FlowStatus.BLOCKED.value
    assert revive_evidence_is_absent(run)


async def test_revive_exhaustion_parks_blocked(db, fake_gitlab):
    service = make_service(db, fake_gitlab)
    run_id = await seed_run(
        db,
        status="proposing",
        evidence={"revive": {"at": datetime.now(timezone.utc).isoformat(), "count": 2}},
    )

    await service._to_terminal(
        run_id, FlowStatus.FAILED, "harness_start_failed: 500 Internal Server Error"
    )

    run = await get_run(db, run_id)
    assert run.status == FlowStatus.BLOCKED.value
    assert run.status_reason.startswith("auto_revive_exhausted")


async def test_a_cancel_run_never_revives(db, fake_gitlab):
    service = make_service(db, fake_gitlab)
    run_id = await seed_run(db, status="proposing")
    async with db() as session:
        run = await session.get(FlowRun, run_id)
        run.cancel_requested = True
        await session.commit()

    await service._to_terminal(
        run_id, FlowStatus.FAILED, "harness_start_failed: 500 Internal Server Error"
    )

    run = await get_run(db, run_id)
    assert run.status == FlowStatus.CANCELLED.value


# ----------------------------------------------------------------------
# Tier 1: the reconciler leg — skip until due, then fire once
# ----------------------------------------------------------------------


async def test_reconciler_skips_until_the_revive_is_due(db, fake_gitlab):
    service = make_service(db, fake_gitlab)
    run_id = await seed_run(
        db,
        status="proposing",
        evidence={"revive": {"at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                             "count": 1}},
    )
    fired: list[tuple] = []

    async def recorder(project_id, run_id, **_kwargs):
        fired.append((project_id, run_id))

    service._advance_harness = recorder  # type: ignore[method-assign]
    await service.evaluate_revival(now=datetime.now(timezone.utc))

    assert fired == []
    assert await get_actions(db, run_id, "auto_revive") == []


async def test_due_revive_redispatches_the_same_branch_and_journals(db, fake_gitlab):
    service = make_service(db, fake_gitlab)
    run_id = await seed_run(
        db,
        status="proposing",
        candidate_shas=["a" * 40, CANDIDATE_SHA],
        commit_cycle=2,
        evidence={"revive": {"at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                             "count": 1}},
    )
    fired: list[tuple] = []

    async def recorder(project_id, run_id, **_kwargs):
        fired.append((project_id, run_id))

    service._advance_harness = recorder  # type: ignore[method-assign]
    await service.evaluate_revival(now=datetime.now(timezone.utc))

    assert fired == [(PROJECT_ID, run_id)]
    actions = await get_actions(db, run_id, "auto_revive")
    assert [action.status for action in actions] == ["succeeded"]
    assert actions[0].remote_result == {"revive_count": 1}
    # The due stamp is consumed (one launch per intent), the count stays.
    run = await get_run(db, run_id)
    assert (run.evidence or {}).get("revive") == {"count": 1}
    # A revive-parked repair run re-fires on the last candidate (ADR-0016 §4).
    assert attempt_base_for(run) == CANDIDATE_SHA


async def test_due_revive_of_a_builtin_run_takes_the_proposal_leg(db, fake_gitlab):
    service = make_service(db, fake_gitlab)
    run_id = await seed_run(
        db,
        status="validating",
        evidence={
            "backend": "builtin",
            "revive": {"at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                       "count": 1},
        },
    )
    fired: list[tuple] = []

    async def recorder(project_id, run_id, **_kwargs):
        fired.append((project_id, run_id))

    service._advance_proposal = recorder  # type: ignore[method-assign]
    await service.evaluate_revival(now=datetime.now(timezone.utc))

    assert fired == [(PROJECT_ID, run_id)]


# ----------------------------------------------------------------------
# Tier 2: /retry
# ----------------------------------------------------------------------


async def test_retry_is_approver_gated(db, fake_gitlab):
    service = make_service(db, fake_gitlab)
    run_id = await seed_run(db, status="blocked")

    await service.handle_retry_note(
        PROJECT_ID, f"@forge /retry {run_id[:8]}", "mallory", ISSUE_IID
    )

    assert (await get_run(db, run_id)).status == "blocked"
    assert fake_gitlab.notes == []


async def test_retry_refuses_a_live_run(db, fake_gitlab):
    service = make_service(db, fake_gitlab)
    run_id = await seed_run(db, status="proposing")

    await service.handle_retry_note(
        PROJECT_ID, f"@forge /retry {run_id[:8]}", "alice", ISSUE_IID
    )

    assert (await get_run(db, run_id)).status == "proposing"
    assert fake_gitlab.notes_containing("cannot be retried")


async def test_retry_without_a_candidate_points_at_implement(db, fake_gitlab):
    service = make_service(db, fake_gitlab)
    run_id = await seed_run(db, status="blocked", candidate_shas=[])

    await service.handle_retry_note(PROJECT_ID, "/retry", "alice", ISSUE_IID)

    assert (await get_run(db, run_id)).status == "blocked"
    assert fake_gitlab.notes_containing("/implement")


async def test_retry_walks_back_to_proposing_and_redispatches(db, fake_gitlab):
    service = make_service(db, fake_gitlab)
    run_id = await seed_run(
        db,
        status="blocked",
        commit_cycle=3,
        evidence={
            "pipeline": {"id": 9, "status": "failed", "url": "https://ci.test/9"},
        },
    )
    async with db() as session:
        run = await session.get(FlowRun, run_id)
        run.status_reason = "harness_start_failed: 500 Internal Server Error"
        await session.commit()
    fired: list[dict] = []

    async def recorder(project_id, run_id, **kwargs):
        fired.append({"project_id": project_id, "run_id": run_id, **kwargs})

    service._advance_harness = recorder  # type: ignore[method-assign]
    await service.handle_retry_note(PROJECT_ID, "/retry", "alice", ISSUE_IID)

    run = await get_run(db, run_id)
    assert run.status == FlowStatus.PROPOSING.value
    assert run.commit_cycle == 4  # one operator-granted cycle
    assert fired and fired[0]["run_id"] == run_id
    assert "harness_start_failed: 500" in fired[0]["repair_context"]
    actions = await get_actions(db, run_id, "retry_requested")
    assert [action.status for action in actions] == ["succeeded"]
    assert actions[0].remote_result == {"revived_from": "blocked", "commit_cycle": 4}
    assert fake_gitlab.notes_containing("**retried** by @alice")


async def test_retry_exceeds_the_cycle_budget_by_one(db, fake_gitlab):
    service = make_service(db, fake_gitlab)
    run_id = await seed_run(db, status="failed", commit_cycle=3, evidence={"backend": "builtin"})
    fired: list[dict] = []

    async def recorder(project_id, run_id, **kwargs):
        fired.append({"run_id": run_id, **kwargs})

    service._advance_proposal = recorder  # type: ignore[method-assign]
    await service.handle_retry_note(PROJECT_ID, "/retry", "alice", ISSUE_IID)

    run = await get_run(db, run_id)
    max_cycles = int(service._settings.FORGE_MAX_COMMIT_CYCLES)
    assert run.commit_cycle == max_cycles + 1  # the operator granted it
    assert run.status == FlowStatus.PROPOSING.value


async def test_retry_picks_the_latest_dead_run_and_clears_a_pending_revive(db, fake_gitlab):
    service = make_service(db, fake_gitlab)
    older = await seed_run(db, status="blocked")
    newer = await seed_run(
        db,
        status="failed",
        evidence={
            "backend": "builtin",
            "revive": {"at": datetime.now(timezone.utc).isoformat(), "count": 1},
        },
    )
    fired: list[tuple] = []

    async def recorder(project_id, run_id, **_kwargs):
        fired.append((project_id, run_id))

    service._advance_proposal = recorder  # type: ignore[method-assign]
    await service.handle_retry_note(PROJECT_ID, "/retry", "alice", ISSUE_IID)
    assert fired == [(PROJECT_ID, newer)]

    # Bare /retry targets the LATEST dead run, and its pending auto-revive is
    # superseded — no due stamp survives for the revival pass to double-fire.
    assert (await get_run(db, newer)).status == FlowStatus.PROPOSING.value
    run = await get_run(db, newer)
    assert (run.evidence or {}).get("revive") == {"count": 1}
    assert (await get_run(db, older)).status == "blocked"


async def test_graph_walk_failed_to_proposing_is_legal_for_a_retry(db):
    run_id = uuid.uuid4().hex
    async with db() as session:
        session.add(
            FlowRun(id=run_id, project_id=PROJECT_ID, issue_iid=ISSUE_IID, status="failed")
        )
        await session.commit()
    async with db() as session:
        controller = Controller(session)
        updated = await controller.transition(run_id, FlowStatus.PROPOSING, reason="retried")
        assert updated.status == FlowStatus.PROPOSING.value
