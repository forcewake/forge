"""Tier-1 auto-revive and Tier-2 operator ``/retry`` (forge.runs.revival).

Contract coverage: the failure classification table, the bounded revive
backoff and the reconciler due-skip, the explicit revival graph edge, the
``/retry`` guards and same-branch dispatch, evidence journaling, and the
parity of classification and limits across the GitLab, GitHub and Azure
DevOps services. All offline: fakes and stub agents only.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import Settings
from forge.durable import ActionLog, Controller, FlowRun, FlowStatus, InvalidTransition
from forge.gateway.azure_webhook import _COMMAND_MAP as _AZDO_COMMAND_MAP
from forge.gateway.azure_webhook import _AZDO_RUN_COMMANDS
from forge.gateway.github_webhook import _COMMAND_MAP as _GITHUB_COMMAND_MAP
from forge.gateway.github_webhook import _GITHUB_RUN_COMMANDS
from forge.gateway.router import _RUN_COMMANDS, _match_run_command
from forge.gitlab.events import (
    NoteEvent,
    NoteIssueInfo,
    NoteObjectAttributes,
    ProjectInfo,
    UserInfo,
)
from forge.models.base import Base
from forge.runs.azure_service import AzureRunService
from forge.runs.github_service import GitHubRunService
from forge.runs.revival import (
    FATAL,
    MAX_BACKOFF_SECONDS,
    TRANSIENT,
    classify_terminal_failure,
    revival_backoff_seconds,
    revival_due,
    revival_limit,
)
from forge.runs.service import RunService
from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer, factory_branch

PROJECT_ID = 42
ISSUE_IID = 7

TRANSIENT_5XX = "harness_start_failed: GitLab API error 502: Bad Gateway"
FATAL_422 = "harness_start_failed: GitLab API error 422: Unexpected inputs"


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        FORGE_APPROVERS="alice",
        # Hermetic vs the dev .env (which sets ci_harness): the retry leg
        # dispatches on the backend frozen in the run evidence, and the
        # default backend decides between _advance_proposal/_advance_harness.
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
    provider: str = "gitlab",
    project_id: int = PROJECT_ID,
    issue_iid: int | None = ISSUE_IID,
    status: str = FlowStatus.PROPOSING.value,
    commit_cycle: int = 1,
    candidate_shas: list[str] | None = None,
    evidence: dict | None = None,
    cancel_requested: bool = False,
    status_reason: str | None = None,
    updated_at: datetime | None = None,
) -> str:
    """Insert a FlowRun directly, bypassing the state machine (test seam)."""
    run_id = run_id or uuid.uuid4().hex
    async with db() as session:
        run = FlowRun(
            id=run_id,
            provider=provider,
            project_id=project_id,
            issue_iid=issue_iid,
            status=status,
            commit_cycle=commit_cycle,
            candidate_shas=candidate_shas,
            evidence=evidence,
            cancel_requested=cancel_requested,
            status_reason=status_reason,
        )
        if updated_at is not None:
            run.updated_at = updated_at
        session.add(run)
        await session.commit()
    return run_id


def revive_recorder(into: list[str]):
    """An async ``_redispatch_revival`` stand-in that records the run ids."""

    async def _record(run_id: str) -> None:
        into.append(run_id)

    return _record


def advance_recorder(into: list[tuple]):
    """An async advance-leg stand-in recording (project_id, run_id, kwargs)."""

    async def _record(project_id: int, run_id: str, **kwargs):
        into.append((project_id, run_id, kwargs))

    return _record


def bare_service(db, settings, cls: type = RunService):
    """*cls* without agents — enough for the terminalization/revival paths."""
    service = object.__new__(cls)
    service._session_factory = db
    service._settings = settings
    return service


async def read_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        # expire_on_commit=False with a fresh session: the row is read as-is.
        return await session.get(FlowRun, run_id)


# ----------------------------------------------------------------------
# The failure classification table
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        # transient: dispatch/CI 5xx
        ("harness_start_failed: GitLab API error 502: Bad Gateway", TRANSIENT),
        ("commit_failed: GitLab API error 503: Service Unavailable", TRANSIENT),
        ("mr_failed: 504 gateway timeout", TRANSIENT),
        # transient: network / timeout
        ("proposal_failed: LLMError: connection reset by peer", TRANSIENT),
        ("planning_failed: ReadTimeout: timed out", TRANSIENT),
        ("harness_start_failed: All connection attempts failed", TRANSIENT),
        # transient: rate limits
        ("planning_failed: 429 Too Many Requests (rate limit)", TRANSIENT),
        # transient: runner startup, and an empty harness-start error
        ("harness_start_failed: runner startup failure: no runner available", TRANSIENT),
        ("harness_start_failed: ", TRANSIENT),
        # fatal: config errors — the db5408f4 undeclared-input 422
        (FATAL_422, FATAL),
        ("commit_failed: GitLab API error 404: branch not found", FATAL),
        ("backend_config: missing workflow forge-lane.yml", FATAL),
        # fatal: real quality signals and spent budgets
        ("harness_code: driver exit failed: 2 tests red", FATAL),
        ("commit_unknown_outcome", FATAL),
        ("mr_unknown_outcome", FATAL),
        ("commit_cycles_exhausted: 3 of 3 commit cycles used", FATAL),
        # fatal is the default — Tier 1 must be conservative
        ("", FATAL),
        ("weird unclassified breakdown", FATAL),
    ],
)
def test_failure_classification_table(reason, expected):
    assert classify_terminal_failure(reason) is expected


def test_backoff_ladder_is_bounded():
    assert revival_backoff_seconds(0) == 60
    assert revival_backoff_seconds(1) == 120
    assert revival_backoff_seconds(2) == 240
    assert revival_backoff_seconds(9) == MAX_BACKOFF_SECONDS  # capped, never unbounded


def test_backoff_follows_the_configured_base():
    settings = make_settings(FORGE_RUN_REVIVE_BACKOFF_SECONDS=30)
    assert revival_backoff_seconds(0, settings) == 30
    assert revival_backoff_seconds(1, settings) == 60


def test_revival_limit_reads_the_setting():
    assert revival_limit(make_settings()) == 2
    assert revival_limit(make_settings(FORGE_RUN_AUTO_REVIVE_LIMIT=0)) == 0


# ----------------------------------------------------------------------
# Tier 1: terminalization classifies instead of dying `failed`
# ----------------------------------------------------------------------


async def test_transient_failure_parks_blocked_with_revival_stamp(db, service):
    run_id = await make_run(db)
    await service._to_terminal(run_id, FlowStatus.FAILED, TRANSIENT_5XX)

    run = await read_run(db, run_id)
    assert run.status == FlowStatus.BLOCKED.value
    assert "auto-revive 1/2" in (run.status_reason or "")
    stamp = run.evidence["revival"]
    assert stamp["count"] == 1
    now = datetime.now(timezone.utc)
    assert not revival_due(run, now)  # the backoff is still running
    assert revival_due(run, now + timedelta(seconds=61))


async def test_fatal_failure_parks_blocked_without_revival(db, service):
    run_id = await make_run(db)
    await service._to_terminal(run_id, FlowStatus.FAILED, FATAL_422)

    run = await read_run(db, run_id)
    assert run.status == FlowStatus.BLOCKED.value
    # The precise, actionable reason survives on the run for /retry to quote.
    assert FATAL_422 in (run.status_reason or "")
    assert "revival" not in (run.evidence or {})


async def test_revival_budget_is_bounded(db, service):
    spent = {"revival": {"count": 2, "due_at": None}}
    run_id = await make_run(db, evidence=spent)
    await service._to_terminal(run_id, FlowStatus.FAILED, TRANSIENT_5XX)

    run = await read_run(db, run_id)
    assert run.status == FlowStatus.BLOCKED.value
    assert run.evidence["revival"]["count"] == 2  # no further revive scheduled
    assert TRANSIENT_5XX in (run.status_reason or "")


async def test_a_cancelled_grant_is_never_revived(db, service):
    run_id = await make_run(db, cancel_requested=True)
    await service._to_terminal(run_id, FlowStatus.FAILED, TRANSIENT_5XX)

    run = await read_run(db, run_id)
    assert run.status == FlowStatus.BLOCKED.value
    assert "revival" not in (run.evidence or {})


async def test_limit_zero_disables_auto_revive(db):
    service = bare_service(db, make_settings(FORGE_RUN_AUTO_REVIVE_LIMIT=0))
    run_id = await make_run(db)
    await service._to_terminal(run_id, FlowStatus.FAILED, TRANSIENT_5XX)

    run = await read_run(db, run_id)
    assert run.status == FlowStatus.BLOCKED.value
    assert "revival" not in (run.evidence or {})


# ----------------------------------------------------------------------
# The explicit revival graph edge
# ----------------------------------------------------------------------


async def test_plain_transition_never_leaves_a_terminal_state(db):
    run_id = await make_run(db, status=FlowStatus.BLOCKED.value)
    async with db() as session:
        controller = Controller(session)
        with pytest.raises(InvalidTransition):
            await controller.transition(run_id, FlowStatus.PROPOSING, reason="sneak")


@pytest.mark.parametrize("status", ["blocked", "failed"])
async def test_revival_edge_walks_dead_runs_to_proposing(db, status):
    run_id = await make_run(db, status=status)
    async with db() as session:
        controller = Controller(session)
        run = await controller.revive_transition(
            run_id, reason="retry requested by @alice", authorized_by="operator:@alice"
        )
        await session.commit()
        assert run.status == FlowStatus.PROPOSING.value
    assert (await read_run(db, run_id)).status_reason == "retry requested by @alice"


@pytest.mark.parametrize(
    "status",
    [
        FlowStatus.PROPOSING.value,
        FlowStatus.WAITING_CI.value,
        FlowStatus.CANCELLED.value,
        FlowStatus.READY_FOR_HUMAN.value,
    ],
)
async def test_revival_edge_rejects_live_and_irrevocable_runs(db, status):
    run_id = await make_run(db, status=status)
    async with db() as session:
        controller = Controller(session)
        with pytest.raises(InvalidTransition):
            await controller.revive_transition(
                run_id, reason="too late", authorized_by="operator:@alice"
            )


# ----------------------------------------------------------------------
# Tier 1: the reconciler revive pass (worker-free wait, due-skip)
# ----------------------------------------------------------------------


async def test_revival_pass_skips_runs_until_due(db, service, monkeypatch):
    due_later = {
        "revival": {
            "count": 1,
            "due_at": (datetime.now(timezone.utc) + timedelta(seconds=600)).isoformat(),
        }
    }
    run_id = await make_run(db, status=FlowStatus.BLOCKED.value, evidence=due_later)
    dispatched: list[str] = []
    monkeypatch.setattr(service, "_redispatch_revival", revive_recorder(dispatched))

    await service.evaluate_revival(now=datetime.now(timezone.utc))

    assert dispatched == []
    assert (await read_run(db, run_id)).status == FlowStatus.BLOCKED.value


async def test_revival_pass_dispatches_a_due_run_on_the_same_branch(db, service, monkeypatch):
    due_now = {
        "revival": {
            "count": 1,
            "due_at": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
            "reason": TRANSIENT_5XX,
        }
    }
    run_id = await make_run(
        db,
        status=FlowStatus.BLOCKED.value,
        evidence={"backend": "builtin", **due_now},
        candidate_shas=["candidate-sha-1"],
    )
    dispatched: list[str] = []
    monkeypatch.setattr(service, "_redispatch_revival", revive_recorder(dispatched))

    await service.evaluate_revival(now=datetime.now(timezone.utc))

    assert dispatched == [run_id]
    run = await read_run(db, run_id)
    assert run.status == FlowStatus.PROPOSING.value
    # Same branch, same candidate history: the revival is a continuation.
    assert run.candidate_shas == ["candidate-sha-1"]
    # The due stamp is consumed: a second pass cannot re-dispatch the run.
    assert "due_at" not in run.evidence["revival"]
    assert "dispatched_at" in run.evidence["revival"]
    # Journaled as an auto_revive action (ADR-0005) — the durable attempt
    # record (A11): window idempotency key, cause class, dispatch claimed.
    async with db() as session:
        actions = (
            (await session.execute(select(ActionLog).where(ActionLog.flow_run_id == run_id)))
            .scalars()
            .all()
        )
    assert [action.action_kind for action in actions] == ["auto_revive"]
    assert actions[0].status == "succeeded"
    assert actions[0].idempotency_key == f"revive:{run_id}:1"
    assert actions[0].retryability == "transient_infrastructure"
    assert actions[0].dispatch_state == "dispatched"


async def test_revival_redispatch_follows_the_frozen_backend(db, service, monkeypatch):
    harness_run = await make_run(
        db,
        issue_iid=11,
        status=FlowStatus.BLOCKED.value,
        evidence={"backend": "ci_harness:claude-code"},
        candidate_shas=["c1"],
    )
    builtin_run = await make_run(
        db,
        issue_iid=12,
        status=FlowStatus.BLOCKED.value,
        evidence={"backend": "builtin"},
        candidate_shas=["c2"],
    )
    harness: list[str] = []
    builtin: list[str] = []
    harness_rec = advance_recorder(harness)
    builtin_rec = advance_recorder(builtin)
    monkeypatch.setattr(service, "_advance_harness", harness_rec)
    monkeypatch.setattr(service, "_advance_proposal", builtin_rec)

    await service._redispatch_revival(harness_run)
    await service._redispatch_revival(builtin_run)

    assert [rid for _, rid, _ in harness] == [harness_run]
    assert [rid for _, rid, _ in builtin] == [builtin_run]


# ----------------------------------------------------------------------
# Tier 2: operator /retry
# ----------------------------------------------------------------------


def retry_note(run_id: str | None = None) -> str:
    return "@forge /retry" + (f" {run_id}" if run_id else "")


async def test_retry_requires_an_approver(db, service, fake_gitlab):
    run_id = await make_run(
        db,
        status=FlowStatus.BLOCKED.value,
        candidate_shas=["c1"],
        status_reason="backend_config: boom",
    )
    await service.handle_retry_note(PROJECT_ID, retry_note(run_id), "mallory", ISSUE_IID)

    assert (await read_run(db, run_id)).status == FlowStatus.BLOCKED.value
    assert fake_gitlab.notes == []


async def test_retry_rejects_a_run_without_a_candidate(db, service, fake_gitlab):
    run_id = await make_run(db, status=FlowStatus.BLOCKED.value)
    await service.handle_retry_note(PROJECT_ID, retry_note(run_id), "alice", ISSUE_IID)

    assert (await read_run(db, run_id)).status == FlowStatus.BLOCKED.value
    assert fake_gitlab.notes_containing("new implement request")  # no literal slash-command (#67)


@pytest.mark.parametrize(
    "status",
    [FlowStatus.CANCELLED.value, FlowStatus.READY_FOR_HUMAN.value, FlowStatus.WAITING_CI.value],
)
async def test_retry_rejects_runs_that_are_not_dead(db, service, fake_gitlab, status):
    run_id = await make_run(db, status=status, candidate_shas=["c1"])
    await service.handle_retry_note(PROJECT_ID, retry_note(run_id), "alice", ISSUE_IID)

    assert (await read_run(db, run_id)).status == status
    assert fake_gitlab.notes_containing("new implement request")  # no literal slash-command (#67)


async def test_retry_rejects_a_cancelled_grant(db, service, fake_gitlab):
    run_id = await make_run(
        db,
        status=FlowStatus.BLOCKED.value,
        candidate_shas=["c1"],
        cancel_requested=True,
    )
    await service.handle_retry_note(PROJECT_ID, retry_note(run_id), "alice", ISSUE_IID)

    assert (await read_run(db, run_id)).status == FlowStatus.BLOCKED.value
    assert fake_gitlab.notes_containing("cancelled")


async def test_bare_retry_targets_the_latest_dead_run(db, service, fake_gitlab, monkeypatch):
    older = await make_run(
        db,
        status=FlowStatus.BLOCKED.value,
        candidate_shas=["c1"],
        updated_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    newer = await make_run(
        db,
        status=FlowStatus.BLOCKED.value,
        candidate_shas=["c2"],
        updated_at=datetime.now(timezone.utc) - timedelta(minutes=2),
    )
    dispatched: list[str] = []
    monkeypatch.setattr(service, "_advance_proposal", advance_recorder(dispatched))

    await service.handle_retry_note(PROJECT_ID, retry_note(), "alice", ISSUE_IID)

    assert [rid for _, rid, _ in dispatched] == [newer]
    assert (await read_run(db, older)).status == FlowStatus.BLOCKED.value


async def test_retry_continues_the_same_branch_and_grants_one_cycle(
    db, service, fake_gitlab, monkeypatch
):
    run_id = await make_run(
        db,
        status=FlowStatus.BLOCKED.value,
        commit_cycle=3,  # == FORGE_MAX_COMMIT_CYCLES: the operator may exceed it by one
        candidate_shas=["candidate-sha-9"],
        status_reason="commit_cycles_exhausted: 3 of 3 commit cycles used",
    )
    dispatched: list[tuple[int, str, dict]] = []
    monkeypatch.setattr(service, "_advance_proposal", advance_recorder(dispatched))

    await service.handle_retry_note(PROJECT_ID, retry_note(run_id), "alice", ISSUE_IID)

    run = await read_run(db, run_id)
    assert run.status == FlowStatus.PROPOSING.value
    assert run.commit_cycle == 4  # operator-granted: beyond the budget
    assert run.candidate_shas == ["candidate-sha-9"]  # no new branch, no re-derivation
    assert (run.status_reason or "").startswith("retry requested by @alice")

    # The advance leg carries the terminal reason as the repair context.
    assert len(dispatched) == 1
    pid, rid, kwargs = dispatched[0]
    assert (pid, rid) == (PROJECT_ID, run_id)
    assert "commit_cycles_exhausted" in kwargs["repair_context"]

    # Issue ack (naming the branch) + journaled retry action (ADR-0005).
    assert fake_gitlab.notes_containing(factory_branch(ISSUE_IID, run_id))
    async with db() as session:
        actions = (
            (await session.execute(select(ActionLog).where(ActionLog.flow_run_id == run_id)))
            .scalars()
            .all()
        )
    assert "retry_requested" in {action.action_kind for action in actions}
    requested = next(a for a in actions if a.action_kind == "retry_requested")
    assert requested.status == "succeeded"
    # A11: the attempt record — operator override class; no delivery id was
    # passed in this direct call, so no idempotency key is pinned.
    assert requested.retryability == "operator_override"
    assert requested.idempotency_key is None
    assert requested.dispatch_state == "dispatched"


async def test_retry_of_a_harness_run_redispatches_the_lane(db, service, monkeypatch):
    run_id = await make_run(
        db,
        status=FlowStatus.BLOCKED.value,
        candidate_shas=["c1"],
        evidence={"backend": "ci_harness:claude-code", "harness": {"handle": "{}"}},
        status_reason="harness_code: driver exit failed",
    )
    dispatched: list[str] = []
    monkeypatch.setattr(service, "_advance_harness", advance_recorder(dispatched))
    monkeypatch.setattr(service, "_advance_proposal", advance_recorder(dispatched))

    await service.handle_retry_note(PROJECT_ID, retry_note(run_id), "alice", ISSUE_IID)

    assert [rid for _, rid, _ in dispatched] == [run_id]


async def test_retry_prefix_resolves_a_unique_run(db, service, fake_gitlab, monkeypatch):
    run_id = await make_run(
        db,
        status=FlowStatus.BLOCKED.value,
        issue_iid=21,
        candidate_shas=["c1"],
    )
    other = await make_run(
        db,
        status=FlowStatus.BLOCKED.value,
        issue_iid=21,
    )
    dispatched: list[str] = []
    monkeypatch.setattr(service, "_advance_proposal", advance_recorder(dispatched))

    await service.handle_retry_note(PROJECT_ID, retry_note(run_id[:8]), "alice", 21)

    assert [rid for _, rid, _ in dispatched] == [run_id]
    assert (await read_run(db, other)).status == FlowStatus.BLOCKED.value


# ----------------------------------------------------------------------
# R29 read-only surface + A07 subject scope: /status and /why-blocked
# ----------------------------------------------------------------------


async def test_status_bare_reports_the_latest_run_on_the_issue(db, service, fake_gitlab):
    run_id = await make_run(db, status=FlowStatus.WAITING_CI.value, candidate_shas=["c1"])

    await service.handle_status_note(PROJECT_ID, "@forge /status", "alice", ISSUE_IID)

    assert fake_gitlab.notes_containing(f"run `{run_id[:8]}` status")
    assert fake_gitlab.notes_containing("`waiting_ci`")


async def test_status_full_id_reports_the_run_on_this_issue(db, service, fake_gitlab):
    run_id = await make_run(
        db,
        status=FlowStatus.BLOCKED.value,
        candidate_shas=["c1"],
        status_reason="backend_config: boom",
    )

    await service.handle_status_note(PROJECT_ID, f"@forge /status {run_id}", "alice", ISSUE_IID)

    assert fake_gitlab.notes_containing(f"run `{run_id[:8]}` status")
    assert fake_gitlab.notes_containing("`blocked`")


async def test_why_blocked_full_id_reports_the_run_on_this_issue(db, service, fake_gitlab):
    run_id = await make_run(
        db,
        status=FlowStatus.BLOCKED.value,
        candidate_shas=["c1"],
        status_reason="backend_config: boom",
    )

    await service.handle_why_blocked_note(
        PROJECT_ID, f"@forge /why-blocked {run_id}", "alice", ISSUE_IID
    )

    assert fake_gitlab.notes_containing("why blocked")
    assert fake_gitlab.notes_containing("backend_config: boom")


async def test_status_of_another_projects_same_iid_run_reports_nothing(db, service, fake_gitlab):
    """A07: a full 32-char id of a run on ANOTHER project that shares the
    issue iid resolves to nothing here — the reply carries no foreign state
    and the foreign run itself is untouched (read-only command)."""
    foreign_id = await make_run(
        db,
        project_id=202,
        status=FlowStatus.BLOCKED.value,
        candidate_shas=["c9"],
        status_reason="backend_config: foreign secret",
    )

    await service.handle_status_note(PROJECT_ID, f"@forge /status {foreign_id}", "alice", ISSUE_IID)

    assert fake_gitlab.notes_containing("No forge run found")
    assert not fake_gitlab.notes_containing("foreign secret")
    assert not fake_gitlab.notes_containing(foreign_id[:8])
    assert (await read_run(db, foreign_id)).status == FlowStatus.BLOCKED.value


async def test_retry_of_another_projects_same_iid_run_is_inert(
    db, service, fake_gitlab, monkeypatch
):
    """A07: the same refusal for /retry — an explicit (prefix or full) id of
    another project's run grants no cycle, dispatches nothing, posts no note."""
    foreign_id = await make_run(
        db,
        project_id=202,
        status=FlowStatus.BLOCKED.value,
        commit_cycle=2,
        candidate_shas=["c9"],
        evidence={"backend": "builtin"},
    )
    dispatched: list[tuple[int, str, dict]] = []
    monkeypatch.setattr(service, "_advance_proposal", advance_recorder(dispatched))
    monkeypatch.setattr(service, "_advance_harness", advance_recorder(dispatched))

    for requested in (foreign_id, foreign_id[:8]):
        await service.handle_retry_note(PROJECT_ID, retry_note(requested), "alice", ISSUE_IID)

    assert dispatched == []
    assert fake_gitlab.notes == []
    foreign = await read_run(db, foreign_id)
    assert foreign.status == FlowStatus.BLOCKED.value
    assert foreign.commit_cycle == 2


# ----------------------------------------------------------------------
# Parity: one classification, one budget, three lanes
# ----------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["gitlab", "github", "azure_devops"])
async def test_all_lanes_classify_and_park_alike(db, provider):
    """GitLab, GitHub and AzDO runs get the same classes and the same park."""
    cls = {
        "gitlab": RunService,
        "github": GitHubRunService,
        "azure_devops": AzureRunService,
    }[provider]
    service = bare_service(db, make_settings(), cls)

    transient_run = await make_run(
        db, provider=provider, issue_iid=31, status=FlowStatus.PROPOSING.value
    )
    fatal_run = await make_run(
        db, provider=provider, issue_iid=32, status=FlowStatus.PROPOSING.value
    )
    await service._to_terminal(transient_run, FlowStatus.FAILED, TRANSIENT_5XX)
    await service._to_terminal(fatal_run, FlowStatus.FAILED, FATAL_422)

    assert (await read_run(db, transient_run)).evidence["revival"]["count"] == 1
    parked = await read_run(db, fatal_run)
    assert parked.status == FlowStatus.BLOCKED.value
    assert "revival" not in (parked.evidence or {})


async def test_ingress_routes_retry_on_every_lane():
    assert "/retry" in _RUN_COMMANDS
    assert _GITHUB_RUN_COMMANDS & {"/retry"} and _AZDO_RUN_COMMANDS & {"/retry"}
    assert _GITHUB_COMMAND_MAP["/retry"] == "retry"
    assert _AZDO_COMMAND_MAP["/retry"] == "retry"


def make_note_event(note: str) -> NoteEvent:
    return NoteEvent(
        object_kind="note",
        user=UserInfo(id=3, name="Alice", username="alice"),
        project=ProjectInfo(
            id=PROJECT_ID, name="demo", path_with_namespace="demo", web_url="https://gitlab.test"
        ),
        object_attributes=NoteObjectAttributes(id=9, note=note),
        issue=NoteIssueInfo(iid=ISSUE_IID),
    )


@pytest.mark.parametrize("note", ["@forge /retry", "/retry", "@forge /retry ab12cd34"])
def test_gitlab_ingress_normalizes_retry(note):
    command = _match_run_command(make_note_event(note), make_settings())
    assert command is not None
    assert command["command"] == "retry"
    assert command["issue_iid"] == ISSUE_IID
    assert command["author_username"] == "alice"


def test_gitlab_ingress_leaves_other_notes_alone():
    assert _match_run_command(make_note_event("please look at this"), make_settings()) is None


# ----------------------------------------------------------------------
# One active run per subject: a revival never fights a live sibling
# ----------------------------------------------------------------------


async def test_retry_refuses_while_another_run_is_in_flight(db, service, fake_gitlab):
    dead = await make_run(
        db,
        status=FlowStatus.BLOCKED.value,
        candidate_shas=["c1"],
        status_reason="backend_config: boom",
    )
    await make_run(db, status=FlowStatus.WAITING_APPROVAL.value)  # a live sibling

    await service.handle_retry_note(PROJECT_ID, retry_note(dead), "alice", ISSUE_IID)

    assert (await read_run(db, dead)).status == FlowStatus.BLOCKED.value
    assert fake_gitlab.notes_containing("one active run per subject")


async def test_auto_revive_is_superseded_by_a_live_sibling(db, service, monkeypatch):
    due = {
        "revival": {
            "count": 1,
            "due_at": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
        }
    }
    run_id = await make_run(db, status=FlowStatus.BLOCKED.value, evidence=due)
    await make_run(db, status=FlowStatus.PLANNING.value)  # a live sibling
    dispatched: list[str] = []
    monkeypatch.setattr(service, "_redispatch_revival", revive_recorder(dispatched))

    await service.evaluate_revival(now=datetime.now(timezone.utc))

    assert dispatched == []
    run = await read_run(db, run_id)
    assert run.status == FlowStatus.BLOCKED.value
    assert revival_due(run, datetime.now(timezone.utc))  # still parked and due


# ----------------------------------------------------------------------
# B15: recovery scans live in ONE place — the provider services never
# re-implement their own blocked-run scans (the e53ffd2 B08 lesson)
# ----------------------------------------------------------------------


def test_provider_services_never_reimplement_recovery_scans() -> None:
    """The recovery dispatcher helpers (evaluate_revivals /
    evaluate_attempt_recovery / evaluate_config_blocks) own the blocked-run
    scans; a provider service defining its own ``select(FlowRun)...BLOCKED``
    scan is exactly how B08's cross-repository recovery happened."""
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent / "src" / "forge"
    # The DISPATCH enumerations (which repos hold config-blocked runs — read
    # only, they never drive a run through an adapter) are the scheduler's
    # legitimate own queries; everything else must live in revival.py.
    allowed_markers = ("Distinct", "distinct GitHub repos", "config-blocked repos")
    offenders: list[str] = []
    for service in ("runs/service.py", "runs/github_service.py", "runs/azure_service.py"):
        text = (repo / service).read_text(encoding="utf-8")
        for match in re.finditer(r"select\(FlowRun\)[\s\S]{0,400}?FlowStatus\.BLOCKED", text):
            # the enclosing function IS the dispatcher's repo enumeration
            head = text[: match.start()]
            fn = head[head.rfind("def ") :][:60]
            if "_repos_with_config_blocked" in fn or any(
                marker in head[-500:] for marker in allowed_markers
            ):
                continue
            offenders.append(f"{service}: own blocked-run scan near offset {match.start()}")
    assert offenders == [], offenders
