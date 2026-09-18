"""A11: retry/auto-revive as a durable, single, idempotent transition.

The revival FLOW used to separate the persisted transition from the backend
dispatch (a crash between them lost the redispatch), two reconcilers could
drive the same recovery window, and a repeated /retry bumped cycles twice.
Contract ground regressed here:

- ATTEMPT DURABILITY — every revival writes the attempt record (the
  ``retry_requested``/``auto_revive`` action row: idempotency key +
  retryability class + ``pending`` dispatch state) in the SAME transaction
  as the CAS revival walk;
- /RETRY IDEMPOTENCY — the same webhook delivery id is a no-op (one cycle
  bump, one dispatch); a different delivery id while an attempt is in
  flight is refused with the existing rejection-note machinery;
- RECOVERY SCAN — a stranded ``pending`` attempt (SIGKILL between the
  revive commit and the dispatch) is re-driven exactly once; a claimed
  attempt is resolved by inspection with the dispatch legs' own journaled
  actions as the double-dispatch guard (an interrupted leg parks the run
  for an operator — an unknown remote effect is never re-dispatched);
- GUARDS — cancel always blocks auto-revive; an unknown/open publication
  outcome must be reconciled (A12 scanner) before any auto-revive;
- BUDGET DISCIPLINE — a retry/auto-revive never resets spent/unresolved
  budget; the operator grant consumes exactly its one extra cycle.

All offline: fakes and recorders only.
"""

import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import Settings
from forge.durable import ActionLog, Controller, FlowRun, FlowStatus, PublicationIntent, RunBudget
from forge.durable.budgets import budget_for_run, open_budget
from forge.durable.intents import complete_intent, mark_dispatched, record_intent
from forge.models.base import Base
from forge.runs.revival import (
    DISPATCH_RECOVERY_PENDING_SECONDS,
    FATAL,
    Retryability,
    _begin_auto_revive,
    begin_revival_attempt,
    claim_attempt_dispatch,
    classify_retryability,
    classify_terminal_failure,
    evaluate_attempt_recovery,
    open_revival_attempt,
    retry_delivery_key,
)
from forge.runs.service import RunService
from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer

PROJECT_ID = 42
ISSUE_IID = 7

DUE_STAMP = {
    "revival": {
        "count": 1,
        "due_at": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
        "reason": "harness_start_failed: GitLab API error 502: Bad Gateway",
    }
}


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        FORGE_APPROVERS="alice",
        FORGE_IMPLEMENTER_BACKEND="builtin",
        FORGE_MAX_COMMIT_CYCLES=3,
        FORGE_RUN_AUTO_REVIVE_LIMIT=2,
        FORGE_RUN_REVIVE_BACKOFF_SECONDS=60,
    )
    values.update(overrides)
    return Settings(**values)


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
def fake_gitlab():
    from tests.fixtures.fake_gitlab import FakeGitLab

    return FakeGitLab()


@pytest.fixture()
def service(db, fake_gitlab):
    return RunService(
        session_factory=db,
        gitlab=fake_gitlab,
        settings=make_settings(),
        planner=StubPlanner(),
        implementer=StubImplementer(),
        reviewer=StubReviewer(),
    )


async def make_run(
    db,
    *,
    run_id: str | None = None,
    issue_iid: int | None = ISSUE_IID,
    status: str = FlowStatus.BLOCKED.value,
    evidence: dict | None = None,
    candidate_shas: list[str] | None = None,
    cancel_requested: bool = False,
    commit_cycle: int = 1,
) -> str:
    async with db() as session:
        run = FlowRun(
            id=run_id or uuid4().hex,
            provider="gitlab",
            project_id=PROJECT_ID,
            issue_iid=issue_iid,
            status=status,
            evidence=evidence,
            candidate_shas=candidate_shas,
            cancel_requested=cancel_requested,
            commit_cycle=commit_cycle,
            status_reason="backend_config: boom",
        )
        session.add(run)
        await session.commit()
    return run.id


async def read_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        run = await session.get(FlowRun, run_id)
        assert run is not None
        session.expunge(run)
        return run


async def read_attempt(db, action_id: int) -> ActionLog:
    async with db() as session:
        row = await session.get(ActionLog, action_id)
        assert row is not None
        session.expunge(row)
        return row


async def attempts_of(db, run_id: str) -> list[ActionLog]:
    async with db() as session:
        rows = (
            (
                await session.execute(
                    select(ActionLog).where(ActionLog.flow_run_id == run_id).order_by(ActionLog.id)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            session.expunge(row)
        return list(rows)


async def backdate_attempt(
    db, action_id: int, *, created_at: datetime, claim_stamp: str | None = None
) -> None:
    async with db() as session:
        row = await session.get(ActionLog, action_id)
        assert row is not None
        row.created_at = created_at
        if claim_stamp is not None:
            row.correlation_id = claim_stamp
        await session.commit()


async def claim_attempt(db, action_id: int, *, at: datetime) -> None:
    """Claim the attempt exactly as the live flow would, at *at* time."""
    async with db() as session:
        assert await claim_attempt_dispatch(session, action_id, now=at)
        await session.commit()


def revive_recorder(into: list[str]):
    async def _record(run_id: str) -> None:
        into.append(run_id)

    return _record


def advance_recorder(into: list[str]):
    async def _record(project_id: int, run_id: str, **kwargs) -> None:
        into.append(run_id)

    return _record


def retry_note(run_id: str | None = None) -> str:
    return "@forge /retry" + (f" {run_id}" if run_id else "")


async def open_a_retry_attempt(db, run_id: str, *, key: str) -> int:
    """Leave an attempt open exactly as a crashed /retry would: the attempt
    row and the CAS walk committed, the dispatch never claimed."""
    async with db() as session:
        attempt = await begin_revival_attempt(
            session,
            run_id=run_id,
            kind="retry_requested",
            idempotency_key=key,
            retryability=Retryability.OPERATOR_OVERRIDE,
        )
        await Controller(session).revive_transition(
            run_id, reason="retry requested by @alice", authorized_by="operator:@alice"
        )
        run = await session.get(FlowRun, run_id)
        assert run is not None
        run.commit_cycle += 1
        await session.commit()
        return attempt.action_id


# ----------------------------------------------------------------------
# Retryability typing (orthogonal to A12 effect certainty)
# ----------------------------------------------------------------------


def test_retryability_classification_table():
    assert classify_retryability("retry_requested") is Retryability.OPERATOR_OVERRIDE
    assert (
        classify_retryability("auto_revive", "harness_start_failed: 502 Bad Gateway")
        is Retryability.TRANSIENT_INFRASTRUCTURE
    )
    assert (
        classify_retryability("auto_revive", "harness_code: verification timed out after 30m")
        is Retryability.VERIFICATION_TIMEOUT
    )
    assert (
        classify_retryability("auto_revive", "harness_code: verification timeout exceeded")
        is Retryability.VERIFICATION_TIMEOUT
    )


async def test_attempts_record_their_retryability_class(db):
    run_id = await make_run(db, evidence=dict(DUE_STAMP), candidate_shas=["c1"])
    auto = await _begin_auto_revive(db, make_settings(), run_id, datetime.now(timezone.utc))
    row = await read_attempt(db, auto.action_id)
    assert row.retryability == "transient_infrastructure"

    other = await make_run(db, issue_iid=9, candidate_shas=["c2"])
    async with db() as session:
        manual = await begin_revival_attempt(
            session,
            run_id=other,
            kind="retry_requested",
            idempotency_key=retry_delivery_key("1"),
            retryability=Retryability.OPERATOR_OVERRIDE,
        )
        await session.commit()
    assert (await read_attempt(db, manual.action_id)).retryability == "operator_override"


# ----------------------------------------------------------------------
# /retry delivery-id idempotency (Tier 2)
# ----------------------------------------------------------------------


async def test_same_retry_delivery_twice_is_one_no_op_bump(db, service, fake_gitlab, monkeypatch):
    run_id = await make_run(db, candidate_shas=["c1"], commit_cycle=2)
    dispatched: list[str] = []
    monkeypatch.setattr(service, "_advance_proposal", advance_recorder(dispatched))

    await service.handle_retry_note(
        PROJECT_ID, retry_note(run_id), "alice", ISSUE_IID, delivery_id="777"
    )
    await service.handle_retry_note(
        PROJECT_ID, retry_note(run_id), "alice", ISSUE_IID, delivery_id="777"
    )

    run = await read_run(db, run_id)
    assert run.commit_cycle == 3  # ONE operator-granted cycle, not two
    assert dispatched == [run_id]  # one dispatch
    attempts = [a for a in await attempts_of(db, run_id) if a.action_kind == "retry_requested"]
    assert len(attempts) == 1
    assert attempts[0].idempotency_key == "delivery:777"
    assert attempts[0].status == "succeeded"
    acks = fake_gitlab.notes_containing("retried by @alice")
    assert len(acks) == 1  # one ack; the redelivery posted nothing


async def test_a_different_delivery_while_in_flight_is_refused(db, service, fake_gitlab):
    """A second /retry (different delivery) while an attempt is open gets the
    rejection note — never a second revival, never a second cycle bump."""
    run_id = await make_run(db, candidate_shas=["c1"], commit_cycle=2)
    await open_a_retry_attempt(db, run_id, key="delivery:first")

    await service.handle_retry_note(
        PROJECT_ID, retry_note(run_id), "alice", ISSUE_IID, delivery_id="second"
    )

    assert fake_gitlab.notes_containing("already has a revival in flight")
    run = await read_run(db, run_id)
    # The crashed FIRST attempt already consumed its bump (2 -> 3); the
    # refused delivery must not bump again.
    assert run.commit_cycle == 3
    revival_rows = [a for a in await attempts_of(db, run_id) if a.action_kind == "retry_requested"]
    assert len(revival_rows) == 1  # no second attempt row


async def test_redelivered_retry_with_the_same_delivery_is_silent(db, service, fake_gitlab):
    """The SAME delivery id, even after the attempt completed, is a no-op:
    no rejection note, no re-dispatch, no cycle bump."""
    run_id = await make_run(db, candidate_shas=["c1"], commit_cycle=1)
    action_id = await open_a_retry_attempt(db, run_id, key="delivery:9")
    async with db() as session:
        await Controller(session).complete_action(action_id, "succeeded", None)
        await session.commit()

    await service.handle_retry_note(
        PROJECT_ID, retry_note(run_id), "alice", ISSUE_IID, delivery_id="9"
    )

    assert fake_gitlab.notes == []  # silent: no second ack, no rejection
    run = await read_run(db, run_id)
    assert run.commit_cycle == 2
    assert len(await attempts_of(db, run_id)) == 1


def test_the_delivery_key_format_is_stable():
    assert retry_delivery_key("abcd") == "delivery:abcd"
    assert retry_delivery_key(None) is None
    assert retry_delivery_key("") is None
    assert retry_delivery_key("  ") is None


# ----------------------------------------------------------------------
# Auto-revive window idempotency (Tier 1): two reconcilers, one window
# ----------------------------------------------------------------------


async def test_two_reconcilers_one_recovery_window_one_attempt_one_dispatch(
    db, service, monkeypatch
):
    run_id = await make_run(db, evidence=dict(DUE_STAMP), candidate_shas=["c1"])
    dispatched: list[str] = []
    monkeypatch.setattr(service, "_redispatch_revival", revive_recorder(dispatched))
    now = datetime.now(timezone.utc)

    # Reconciler A drives the window: the begin commits (attempt persisted),
    # then it dies before the dispatch.
    first = await _begin_auto_revive(db, make_settings(), run_id, now)
    assert first.created
    # Reconciler B enters the SAME window: the run is already walked — the
    # guard refuses it before any second attempt or second dispatch exists.
    with pytest.raises(LookupError):
        await _begin_auto_revive(db, make_settings(), run_id, now)
    assert len([a for a in await attempts_of(db, run_id) if a.action_kind == "auto_revive"]) == 1

    # The stranded attempt from A is re-driven exactly once by the recovery
    # scan; B's own pass over the (no longer parked) run finds nothing more.
    later = now + timedelta(seconds=DISPATCH_RECOVERY_PENDING_SECONDS + 1)
    assert await service.evaluate_revival_recovery(now=later) == 1
    await service.evaluate_revival(now=later)
    assert dispatched == [run_id]
    revival_attempts = [a for a in await attempts_of(db, run_id) if a.action_kind == "auto_revive"]
    assert len(revival_attempts) == 1
    assert revival_attempts[0].idempotency_key == f"revive:{run_id}:1"
    assert revival_attempts[0].status == "succeeded"
    assert (await read_run(db, run_id)).status == FlowStatus.PROPOSING.value


async def test_two_full_reconciler_passes_over_one_due_window_dispatch_once(
    db, service, monkeypatch
):
    run_id = await make_run(db, evidence=dict(DUE_STAMP), candidate_shas=["c1"])
    dispatched: list[str] = []
    monkeypatch.setattr(service, "_redispatch_revival", revive_recorder(dispatched))
    now = datetime.now(timezone.utc)

    await service.evaluate_revival(now=now)
    await service.evaluate_revival(now=now)

    assert dispatched == [run_id]
    revival_attempts = [a for a in await attempts_of(db, run_id) if a.action_kind == "auto_revive"]
    assert len(revival_attempts) == 1
    assert revival_attempts[0].status == "succeeded"


async def test_auto_revive_window_key_counts_the_stamp(db):
    run_id = await make_run(
        db,
        evidence={
            "revival": {
                "count": 2,
                "due_at": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
            }
        },
        candidate_shas=["c1"],
    )
    attempt = await _begin_auto_revive(db, make_settings(), run_id, datetime.now(timezone.utc))
    row = await read_attempt(db, attempt.action_id)
    assert row.idempotency_key == f"revive:{run_id}:2"


# ----------------------------------------------------------------------
# The recovery scan: SIGKILL between the revive commit and the dispatch
# ----------------------------------------------------------------------


async def test_sigkill_after_revive_commit_before_dispatch_is_recovered_exactly_once(
    db, service, monkeypatch
):
    run_id = await make_run(db, evidence=dict(DUE_STAMP), candidate_shas=["c1"])
    dispatched: list[str] = []
    monkeypatch.setattr(service, "_redispatch_revival", revive_recorder(dispatched))
    now = datetime.now(timezone.utc)

    # The worker dies right after the revive commit: the attempt is
    # persisted (pending, unclaimed), the dispatch never happened.
    attempt = await _begin_auto_revive(db, make_settings(), run_id, now)
    assert (await read_run(db, run_id)).status == FlowStatus.PROPOSING.value
    row = await read_attempt(db, attempt.action_id)
    assert row.status == "requested"
    assert row.dispatch_state == "pending"

    recovered_at = now + timedelta(seconds=DISPATCH_RECOVERY_PENDING_SECONDS + 1)
    assert await service.evaluate_revival_recovery(now=recovered_at) == 1
    assert dispatched == [run_id]  # re-driven exactly once
    row = await read_attempt(db, attempt.action_id)
    assert row.status == "succeeded"
    assert row.dispatch_state == "dispatched"

    # A second pass (and the reconciler's next ticks) never re-drive it.
    later = recovered_at + timedelta(seconds=DISPATCH_RECOVERY_PENDING_SECONDS)
    assert await service.evaluate_revival_recovery(now=later) == 0
    assert dispatched == [run_id]


async def test_recovery_scan_never_steals_a_fresh_claim_mid_leg(db, service, monkeypatch):
    """Scan B entering while scan A's dispatch leg is running (fresh claim
    stamp) must not re-arm the attempt — one driver, one dispatch."""
    run_id = await make_run(db, evidence=dict(DUE_STAMP), candidate_shas=["c1"])
    dispatched: list[str] = []
    now = datetime.now(timezone.utc)

    attempt = await _begin_auto_revive(db, make_settings(), run_id, now)
    # The attempt has been sitting stranded past every bound...
    await backdate_attempt(db, attempt.action_id, created_at=now - timedelta(hours=2))

    inner_counts: list[int] = []

    async def leg_running_while_scan_b_enters(run_id_in: str) -> None:
        dispatched.append(run_id_in)
        # Scan B runs while A's leg is mid-flight.
        inner_counts.append(await service.evaluate_revival_recovery(now=now + timedelta(seconds=1)))

    monkeypatch.setattr(service, "_redispatch_revival", leg_running_while_scan_b_enters)
    assert await service.evaluate_revival_recovery(now=now) == 1

    assert inner_counts == [0]  # B re-drove nothing
    assert dispatched == [run_id]
    assert (await read_attempt(db, attempt.action_id)).status == "succeeded"


async def test_a_claimed_but_never_started_attempt_is_re_driven(db, service, monkeypatch):
    """Crash after the dispatch claim, before the leg journaled anything:
    nothing was started — the scan re-arms and re-drives exactly once."""
    run_id = await make_run(db, evidence=dict(DUE_STAMP), candidate_shas=["c1"])
    dispatched: list[str] = []
    monkeypatch.setattr(service, "_redispatch_revival", revive_recorder(dispatched))
    now = datetime.now(timezone.utc)

    attempt = await _begin_auto_revive(db, make_settings(), run_id, now - timedelta(hours=2))
    # The row has been sitting claimed-and-stranded for two hours.
    await backdate_attempt(db, attempt.action_id, created_at=now - timedelta(hours=2))
    await claim_attempt(db, attempt.action_id, at=now - timedelta(hours=2))
    row = await read_attempt(db, attempt.action_id)
    assert row.dispatch_state == "dispatched"  # claimed...
    assert row.status == "requested"  # ...never completed, no leg journal

    assert await service.evaluate_revival_recovery(now=now) == 1
    assert dispatched == [run_id]
    assert (await read_attempt(db, attempt.action_id)).status == "succeeded"


async def test_an_interrupted_dispatch_leg_is_parked_not_re_driven(db, service, fake_gitlab):
    """The leg's own journaled action is the guard: an OPEN ``harness_start``
    after the attempt means the remote effect is UNKNOWN — the scan resolves
    the journal and parks the run for an operator, never re-dispatches."""
    run_id = await make_run(db, evidence=dict(DUE_STAMP), candidate_shas=["c1"])
    dispatched: list[str] = []
    now = datetime.now(timezone.utc)

    attempt = await _begin_auto_revive(db, make_settings(), run_id, now - timedelta(hours=2))
    await backdate_attempt(db, attempt.action_id, created_at=now - timedelta(hours=2))
    await claim_attempt(db, attempt.action_id, at=now - timedelta(hours=2))
    async with db() as session:
        leg = ActionLog(
            flow_run_id=run_id,
            action_kind="harness_start",
            status="requested",
            created_at=now - timedelta(hours=1),
        )
        session.add(leg)
        await session.commit()

    driven = await evaluate_attempt_recovery(
        db,
        provider="gitlab",
        redispatch=revive_recorder(dispatched),
        now=now,
        log=logging.getLogger("test"),
    )

    assert driven == 0
    assert dispatched == []  # never a blind re-dispatch
    row = await read_attempt(db, attempt.action_id)
    assert row.status == "unknown_outcome"  # ADR-0005: unknown ≠ failed ≠ retried
    run = await read_run(db, run_id)
    assert run.status == FlowStatus.BLOCKED.value
    assert (run.status_reason or "").startswith("revival_dispatch_interrupted")
    # The interrupted reason classifies FATAL: no fresh revival stamp is
    # scheduled (the consumed stamp keeps only its audit fields), so Tier 1
    # can never auto re-dispatch an unknown remote effect — /retry's job.
    assert "due_at" not in (run.evidence or {}).get("revival", {})
    assert classify_terminal_failure(run.status_reason or "") is FATAL
    assert fake_gitlab.notes == []  # no operator note spam from the scan itself


async def test_an_attempt_whose_run_moved_on_is_resolved_without_a_re_drive(
    db, service, monkeypatch
):
    run_id = await make_run(db, evidence=dict(DUE_STAMP), candidate_shas=["c1"])
    dispatched: list[str] = []
    monkeypatch.setattr(service, "_redispatch_revival", revive_recorder(dispatched))
    now = datetime.now(timezone.utc)

    attempt = await _begin_auto_revive(db, make_settings(), run_id, now - timedelta(hours=2))
    await backdate_attempt(db, attempt.action_id, created_at=now - timedelta(hours=2))
    await claim_attempt(db, attempt.action_id, at=now - timedelta(hours=2))
    # The dispatch demonstrably happened: the run moved on to waiting_ci.
    async with db() as session:
        run = await session.get(FlowRun, run_id)
        assert run is not None
        run.status = FlowStatus.WAITING_CI.value
        await session.commit()

    assert await service.evaluate_revival_recovery(now=now) == 0
    assert dispatched == []
    assert (await read_attempt(db, attempt.action_id)).status == "succeeded"


async def test_a_young_in_flight_attempt_is_never_touched_by_the_scan(db, service, monkeypatch):
    """A /retry whose attempt is seconds old (the live leg is legitimately
    running) is invisible to the recovery scan's pending bound."""
    run_id = await make_run(db, candidate_shas=["c1"], commit_cycle=1)
    dispatched: list[str] = []
    monkeypatch.setattr(service, "_redispatch_revival", revive_recorder(dispatched))
    now = datetime.now(timezone.utc)

    await open_a_retry_attempt(db, run_id, key="delivery:young")
    async with db() as session:
        assert await open_revival_attempt(session, run_id=run_id) is not None

    assert await service.evaluate_revival_recovery(now=now) == 0
    assert dispatched == []
    row = (await attempts_of(db, run_id))[0]
    assert row.status == "requested"  # still open — the live driver owns it


# ----------------------------------------------------------------------
# Guards: cancel and unknown publications
# ----------------------------------------------------------------------


async def test_cancel_always_blocks_auto_revive(db, service, monkeypatch):
    run_id = await make_run(
        db, evidence=dict(DUE_STAMP), candidate_shas=["c1"], cancel_requested=True
    )
    dispatched: list[str] = []
    monkeypatch.setattr(service, "_redispatch_revival", revive_recorder(dispatched))

    await service.evaluate_revival(now=datetime.now(timezone.utc))

    assert dispatched == []
    assert (await read_run(db, run_id)).status == FlowStatus.BLOCKED.value
    assert await attempts_of(db, run_id) == []  # no attempt was even opened


async def test_unknown_publication_blocks_auto_revive_until_reconciled(db, service, monkeypatch):
    run_id = await make_run(db, evidence=dict(DUE_STAMP), candidate_shas=["c1"])
    async with db() as session:
        intent = await record_intent(
            session,
            run_id=run_id,
            provider="gitlab",
            repo="demo",
            target_ref="factory/issue-7",
            idempotency_scope="cycle-1",
        )
        await session.commit()
    dispatched: list[str] = []
    monkeypatch.setattr(service, "_redispatch_revival", revive_recorder(dispatched))
    now = datetime.now(timezone.utc)

    # An OPEN publication intent holds the revival: the effect may exist.
    await service.evaluate_revival(now=now)
    assert dispatched == []
    assert (await read_run(db, run_id)).status == FlowStatus.BLOCKED.value
    assert await attempts_of(db, run_id) == []

    # Resolved to UNKNOWN: still held — reconcile first, never re-execute.
    async with db() as session:
        await mark_dispatched(session, intent.id)
        await complete_intent(session, intent.id, "unknown")
        await session.commit()
    await service.evaluate_revival(now=now)
    assert dispatched == []

    # Reconciled (operator resolved the unknown): the next pass revives once.
    async with db() as session:
        row = await session.get(PublicationIntent, intent.id)
        assert row is not None
        row.status = "duplicated"  # the operator's manual reconciliation
        await session.commit()
    await service.evaluate_revival(now=now)
    assert dispatched == [run_id]
    assert len([a for a in await attempts_of(db, run_id) if a.action_kind == "auto_revive"]) == 1


# ----------------------------------------------------------------------
# Budget discipline: a revival never resets spent/unresolved budget
# ----------------------------------------------------------------------


async def _seed_budget(db, run_id: str) -> None:
    async with db() as session:
        budget = await open_budget(session, run_id=run_id, max_calls=10, max_tokens=1000)
        budget.consumed_calls = 4
        budget.reserved_calls = 1
        budget.unresolved_calls = 1
        budget.consumed_tokens = 700
        budget.reserved_tokens = 100
        budget.unresolved_tokens = 50
        await session.commit()


async def _read_budget(db, run_id: str) -> RunBudget:
    async with db() as session:
        budget = await budget_for_run(session, run_id)
        assert budget is not None
        session.expunge(budget)
        return budget


async def test_retry_does_not_reset_spent_or_unresolved_budget(db, service, monkeypatch):
    run_id = await make_run(db, candidate_shas=["c1"], commit_cycle=1)
    await _seed_budget(db, run_id)
    dispatched: list[str] = []
    monkeypatch.setattr(service, "_advance_proposal", advance_recorder(dispatched))

    await service.handle_retry_note(
        PROJECT_ID, retry_note(run_id), "alice", ISSUE_IID, delivery_id="1"
    )

    assert dispatched == [run_id]
    budget = await _read_budget(db, run_id)
    assert (budget.consumed_calls, budget.reserved_calls, budget.unresolved_calls) == (4, 1, 1)
    assert (budget.consumed_tokens, budget.reserved_tokens, budget.unresolved_tokens) == (
        700,
        100,
        50,
    )
    assert budget.status == "open"  # spent + unresolved keep fencing capacity
    assert (budget.max_calls, budget.max_tokens) == (10, 1000)


async def test_auto_revive_does_not_reset_spent_or_unresolved_budget(db, service, monkeypatch):
    run_id = await make_run(db, evidence=dict(DUE_STAMP), candidate_shas=["c1"])
    await _seed_budget(db, run_id)
    dispatched: list[str] = []
    monkeypatch.setattr(service, "_redispatch_revival", revive_recorder(dispatched))

    await service.evaluate_revival(now=datetime.now(timezone.utc))

    assert dispatched == [run_id]
    budget = await _read_budget(db, run_id)
    assert (budget.consumed_calls, budget.reserved_calls, budget.unresolved_calls) == (4, 1, 1)
    assert (budget.consumed_tokens, budget.reserved_tokens, budget.unresolved_tokens) == (
        700,
        100,
        50,
    )


async def test_operator_grant_consumes_exactly_one_extra_cycle_per_retry(db, service, monkeypatch):
    """Pin: the operator grant adds ONE cycle per revival, even past the
    configured maximum — never a reset to 1, never a double bump per event."""
    run_id = await make_run(
        db, status=FlowStatus.FAILED.value, candidate_shas=["c1"], commit_cycle=3
    )

    monkeypatch.setattr(service, "_advance_proposal", advance_recorder([]))
    await service.handle_retry_note(
        PROJECT_ID, retry_note(run_id), "alice", ISSUE_IID, delivery_id="a"
    )

    assert (await read_run(db, run_id)).commit_cycle == 4  # 3 + 1 with FORGE_MAX=3
