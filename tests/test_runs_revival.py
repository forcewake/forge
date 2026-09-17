"""Contract tests for Tier 1 auto-revive and the Tier 2 ``/retry`` override.

- A terminal failure classified *transient* schedules a bounded auto-revive
  of the SAME branch (``revive_at``/``revive_count`` evidence, journaled);
  the reconciler skips until due, then walks ``failed → proposing`` over the
  explicit revival edge and re-dispatches the frozen leg.
- A *fatal* failure parks ``blocked`` with the precise reason — a human
  decides, never a blind retry.
- ``/retry`` revives a dead run in place: guards first, an operator-granted
  commit cycle, the ``retry_requested`` journal and the taken-in-work ack.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import Settings
from forge.durable import Controller, FlowRun, FlowStatus, InvalidTransition
from forge.models.base import Base
from forge.runs import RunService
from forge.runs.candidate import attempt_base_for
from forge.runs.failures import revival_count, revival_state
from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer
from tests.fixtures.fake_gitlab import FakeGitLab
from tests.test_runs_service import FakeWriter

PROJECT_ID = 42
ISSUE_IID = 7
ISSUE_TITLE = "Add a widget"
ISSUE_DESC = "Widgets make the app better."


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        FORGE_IMPLEMENTER_BACKEND="builtin",
        FORGE_APPROVERS="alice",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
    )
    values.update(overrides)
    return Settings(**values)


def make_service(db, fake_gitlab, **overrides) -> RunService:
    values = dict(
        session_factory=db,
        gitlab=fake_gitlab,
        settings=make_settings(),
        writer_class=FakeWriter,
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
    fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
    fake.seed_commit("main", "base-sha-1", "initial")
    return fake


@pytest.fixture()
def service(db, fake_gitlab):
    FakeWriter.reset()
    return make_service(db, fake_gitlab)


def make_run(**overrides) -> FlowRun:
    values = dict(
        id=uuid4().hex,
        project_id=PROJECT_ID,
        issue_iid=ISSUE_IID,
        provider="gitlab",
        status=FlowStatus.FAILED.value,
        status_reason="commit_failed: GitLab API error 503: Service Unavailable",
        base_sha="base-sha-1",
        candidate_shas=["cand-sha-1"],
        commit_cycle=1,
        evidence={"backend": "builtin"},
    )
    values.update(overrides)
    return FlowRun(**values)


async def seed_run(db, run: FlowRun) -> None:
    async with db() as session:
        session.add(run)
        await session.commit()


async def get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


def track_advance(service: RunService, calls: list[dict]) -> None:
    """Replace the advance legs with recorders — the revival walk stops there."""

    async def record_proposal(project_id, run_id, *, repair_context="", repair_reason=None):
        calls.append(
            {
                "leg": "proposal",
                "run_id": run_id,
                "repair_context": repair_context,
                "repair_reason": repair_reason,
            }
        )

    async def record_harness(
        project_id, run_id, *, repair_context="", repair_reason=None, driver=None
    ):
        calls.append(
            {
                "leg": "harness",
                "run_id": run_id,
                "repair_context": repair_context,
                "repair_reason": repair_reason,
            }
        )

    service._advance_proposal = record_proposal  # type: ignore[method-assign]
    service._advance_harness = record_harness  # type: ignore[method-assign]


# ----------------------------------------------------------------------
# Tier 1 — classification at terminalization time
# ----------------------------------------------------------------------


async def test_transient_death_schedules_a_revive(db, fake_gitlab, service):
    run = make_run()
    await seed_run(db, run)

    await service._to_terminal(
        run.id, FlowStatus.FAILED, "commit_failed: GitLab API error 503: Service Unavailable"
    )

    stored = await get_run(db, run.id)
    assert stored.status == FlowStatus.FAILED.value
    at, count = revival_state(stored.evidence)
    assert at is not None and at > datetime.now(timezone.utc)
    assert count == 1
    assert (
        stored.evidence["revive_reason"]
        == "commit_failed: GitLab API error 503: Service Unavailable"
    )


async def test_fatal_death_parks_blocked_with_the_reason(db, fake_gitlab, service):
    run = make_run(status_reason="harness_start_failed: 422 Unexpected inputs")
    await seed_run(db, run)

    await service._to_terminal(
        run.id, FlowStatus.FAILED, "harness_start_failed: 422 Unexpected inputs"
    )

    stored = await get_run(db, run.id)
    assert stored.status == FlowStatus.BLOCKED.value
    assert "Unexpected inputs" in stored.status_reason
    assert "revive_at" not in (stored.evidence or {})


async def test_revive_bound_is_exhausted_after_the_limit(db, fake_gitlab):
    service = make_service(db, fake_gitlab, settings=make_settings(FORGE_RUN_AUTO_REVIVE_LIMIT=1))
    track_advance(service, [])
    run = make_run()
    await seed_run(db, run)

    # First transient death schedules the one allowed revive…
    await service._to_terminal(
        run.id, FlowStatus.FAILED, "commit_failed: GitLab API error 503: Service Unavailable"
    )
    await service.evaluate_auto_revive(now=datetime.now(timezone.utc) + timedelta(hours=1))
    stored = await get_run(db, run.id)
    assert stored.status == FlowStatus.PROPOSING.value

    # …and the next one parks the run for a human instead of flapping.
    async with db() as session:
        fresh = await session.get(FlowRun, run.id)
        fresh.status = FlowStatus.FAILED.value
        await session.commit()
    await service._to_terminal(run.id, FlowStatus.FAILED, "commit_failed: connection reset by peer")

    stored = await get_run(db, run.id)
    assert stored.status == FlowStatus.BLOCKED.value
    assert stored.status_reason.startswith("auto_revive_exhausted")
    assert "reset by peer" in stored.status_reason


# ----------------------------------------------------------------------
# Tier 1 — the reconciler pass
# ----------------------------------------------------------------------


async def test_reconciler_skips_until_due_then_revives(db, fake_gitlab):
    service = make_service(db, fake_gitlab)
    calls: list[dict] = []
    track_advance(service, calls)
    run = make_run()
    await seed_run(db, run)
    await service._to_terminal(
        run.id, FlowStatus.FAILED, "commit_failed: GitLab API error 503: Service Unavailable"
    )

    # Not due yet — the run stays parked and no leg re-fires.
    due, _ = revival_state((await get_run(db, run.id)).evidence)
    await service.evaluate_auto_revive(now=due - timedelta(seconds=1))
    assert (await get_run(db, run.id)).status == FlowStatus.FAILED.value
    assert calls == []

    await service.evaluate_auto_revive(now=due)
    stored = await get_run(db, run.id)
    assert stored.status == FlowStatus.PROPOSING.value
    assert [call["leg"] for call in calls] == ["proposal"]
    assert calls[0]["run_id"] == run.id
    # The schedule is spent; the per-run count stays as the bound.
    assert revival_state(stored.evidence)[0] is None
    assert revival_count(stored.evidence) == 1


async def test_revive_rides_the_frozen_harness_leg(db, fake_gitlab):
    service = make_service(db, fake_gitlab)
    calls: list[dict] = []
    track_advance(service, calls)
    run = make_run(evidence={"backend": "ci_harness"}, candidate_shas=["cand-sha-9"])
    await seed_run(db, run)

    revived = await service._revive_run(run.id, PROJECT_ID, reason="auto-revive")

    assert revived is True
    assert [call["leg"] for call in calls] == ["harness"]
    assert calls[0]["repair_reason"] == "auto-revive"


async def test_revive_carries_the_terminal_reason_as_repair_context(db, fake_gitlab):
    service = make_service(db, fake_gitlab)
    calls: list[dict] = []
    track_advance(service, calls)
    run = make_run(status_reason="commit_failed: 503", evidence={"pipeline": {"id": 5}})
    await seed_run(db, run)

    await service._revive_run(run.id, PROJECT_ID, reason="auto-revive")

    context = calls[0]["repair_context"]
    assert "commit_failed: 503" in context
    assert "pipeline 5" in context


async def test_revival_never_touches_a_cancelled_run(db, fake_gitlab):
    service = make_service(db, fake_gitlab)
    calls: list[dict] = []
    track_advance(service, calls)
    run = make_run(cancel_requested=True)
    await seed_run(db, run)

    revived = await service._revive_run(run.id, PROJECT_ID, reason="auto-revive")

    assert revived is False
    assert calls == []
    assert (await get_run(db, run.id)).status == FlowStatus.FAILED.value


# ----------------------------------------------------------------------
# The revival graph edges are explicit
# ----------------------------------------------------------------------


async def test_terminal_states_have_no_ordinary_exit(db, fake_gitlab):
    run = make_run()
    await seed_run(db, run)

    async with db() as session:
        controller = Controller(session)
        with pytest.raises(InvalidTransition):
            await controller.transition(run.id, FlowStatus.PROPOSING, reason="sneaky")
        with pytest.raises(InvalidTransition):
            await controller.transition(
                run.id, FlowStatus.PROPOSING, reason="sneaky", revival=False
            )
        revived = await controller.transition(
            run.id, FlowStatus.PROPOSING, reason="revived", revival=True
        )
        assert revived.status == FlowStatus.PROPOSING.value


# ----------------------------------------------------------------------
# Attempt-base resolution on revive/retry
# ----------------------------------------------------------------------


async def test_attempt_base_continues_from_the_last_candidate(db, fake_gitlab):
    """A revived run is a continuation: its own candidate, even at cycle 1."""
    service = make_service(db, fake_gitlab)
    calls: list[dict] = []
    track_advance(service, calls)
    run = make_run(candidate_shas=["cand-sha-1", "cand-sha-2"], commit_cycle=1)
    await seed_run(db, run)

    await service._revive_run(run.id, PROJECT_ID, reason="auto-revive")

    assert calls[0]["leg"] == "proposal"
    # The advance leg the revival drives reads the same frozen base the
    # harness lane is told about.
    stored = await get_run(db, run.id)
    assert attempt_base_for(stored) == "cand-sha-2"


def test_attempt_base_table() -> None:
    """cycle 1 → approved base; repair/revival → the last candidate."""
    plain = make_run(candidate_shas=[], commit_cycle=1, evidence={})
    assert attempt_base_for(plain) == "base-sha-1"
    repair = make_run(candidate_shas=["c1", "c2"], commit_cycle=2, evidence={})
    assert attempt_base_for(repair) == "c2"
    revived = make_run(candidate_shas=["c1"], commit_cycle=1, evidence={"revive_count": 1})
    assert attempt_base_for(revived) == "c1"


# ----------------------------------------------------------------------
# Tier 2 — the operator /retry override
# ----------------------------------------------------------------------


async def test_retry_requires_an_approver(db, fake_gitlab):
    service = make_service(db, fake_gitlab)
    calls: list[dict] = []
    track_advance(service, calls)
    run = make_run()
    await seed_run(db, run)

    await service.handle_retry_note(PROJECT_ID, f"/retry {run.id}", "mallory", ISSUE_IID)

    assert calls == []
    assert (await get_run(db, run.id)).status == FlowStatus.FAILED.value
    assert fake_gitlab.notes == []


async def test_retry_guards_refuse_with_an_actionable_note(db, fake_gitlab):
    service = make_service(db, fake_gitlab)
    calls: list[dict] = []
    track_advance(service, calls)

    stranded = make_run(candidate_shas=[])
    await seed_run(db, stranded)
    cancelled = make_run(status=FlowStatus.CANCELLED.value, cancel_requested=True)
    await seed_run(db, cancelled)

    await service.handle_retry_note(PROJECT_ID, f"/retry {stranded.id}", "alice", ISSUE_IID)
    assert calls == []
    assert fake_gitlab.notes_containing("never published a candidate")

    await service.handle_retry_note(PROJECT_ID, f"/retry {cancelled.id}", "alice", ISSUE_IID)
    assert fake_gitlab.notes_containing("was **cancelled**")
    assert calls == []


async def test_bare_retry_without_a_dead_run_suggests_implement(db, fake_gitlab):
    service = make_service(db, fake_gitlab)
    await service.handle_retry_note(PROJECT_ID, "/retry", "alice", ISSUE_IID)
    assert fake_gitlab.notes_containing("`/implement` starts a fresh run")


async def test_retry_revives_the_dead_run_in_place(db, fake_gitlab):
    service = make_service(db, fake_gitlab)
    calls: list[dict] = []
    track_advance(service, calls)
    run = make_run(candidate_shas=["cand-sha-1", "cand-sha-2"], commit_cycle=2)
    await seed_run(db, run)

    await service.handle_retry_note(PROJECT_ID, f"/retry {run.id}", "alice", ISSUE_IID)

    stored = await get_run(db, run.id)
    assert stored.status == FlowStatus.PROPOSING.value
    # Operator-granted cycle: one past the budget, not a fresh budget.
    assert stored.commit_cycle == 3
    assert [call["leg"] for call in calls] == ["proposal"]
    assert calls[0]["repair_reason"] == "retried by @alice"
    # Same branch, same candidates — no new run id, no re-planning.
    assert stored.candidate_shas == ["cand-sha-1", "cand-sha-2"]
    assert attempt_base_for(stored) == "cand-sha-2"
    # The decision is journaled and acknowledged on the issue.
    kinds = [
        action.action_kind
        for action in await _actions(db, run.id)
        if action.action_kind == "retry_requested"
    ]
    assert kinds == ["retry_requested"]
    assert fake_gitlab.notes_containing("**retry** accepted by @alice")


async def _actions(db, run_id: str):
    from sqlalchemy import select

    from forge.durable import ActionLog

    async with db() as session:
        rows = await session.execute(
            select(ActionLog).where(ActionLog.flow_run_id == run_id).order_by(ActionLog.id)
        )
        return list(rows.scalars().all())
