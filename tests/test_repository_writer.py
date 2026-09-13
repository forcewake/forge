"""Tests for the journaled GitLab write path (ADR-0005) and its reconciliation."""

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.durable import ActionLog, FlowRun
from forge.gitlab.client import GitLabAPIError
from forge.models.base import Base
from forge.repository import (
    Change,
    ChangeSet,
    ChangesetWriter,
    Operation,
    WriteOutcome,
    WriteResult,
)
from forge.repository.writer import BranchDriftError
from tests.fixtures.fake_gitlab import FakeGitLab

RUN_ID = uuid4().hex


@pytest.fixture()
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(FlowRun(id=RUN_ID, project_id=42, issue_iid=7))
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.fixture()
def changeset() -> ChangeSet:
    return ChangeSet(
        branch="factory/7/" + RUN_ID[:8],
        commit_message="forge: implement 7 (run " + RUN_ID[:8] + ")",
        changes=[
            Change(
                path=f"forge-demo/run-{RUN_ID[:8]}.md",
                operation=Operation.CREATE,
                content=f"# run {RUN_ID}\n",
            )
        ],
    )


async def last_action(session_factory, run_id: str = RUN_ID) -> ActionLog:
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ActionLog)
                    .where(ActionLog.flow_run_id == run_id)
                    .order_by(ActionLog.id.desc())
                )
            )
            .scalars()
            .all()
        )
        return rows[0] if rows else None


async def test_success_returns_exact_sha_and_journals(session_factory, changeset):
    fake = FakeGitLab()
    writer = ChangesetWriter(fake, session_factory, project_id=42)

    result = await writer.apply(RUN_ID, changeset, start_ref="main")

    # Exact-SHA correlation: the sha GitLab returned is what the caller records.
    assert result.outcome is WriteOutcome.COMMITTED
    assert result.commit_sha == fake.branches[changeset.branch][0]["sha"]

    # Branch created from the pinned start ref; commit applied without
    # start_branch (GitLab 18.x 400s 'already exists' when it is present).
    assert [args for _, args in fake.calls_of("create_branch")] == [(42, changeset.branch, "main")]
    (call,) = fake.calls_of("create_commit")
    _, (project_id, branch, actions, message, start_branch) = call
    assert (project_id, branch) == (42, changeset.branch)
    # Human prefix intact, unique per-apply operation marker appended (F07).
    assert message.startswith(changeset.commit_message + " ")
    assert message.endswith(f"(forge-op:{writer.operation_key})")
    assert start_branch is None
    assert actions == [
        {
            "action": "create",
            "file_path": changeset.changes[0].path,
            "content": changeset.changes[0].content,
        }
    ]

    # Intent -> outcome journaling with the remote sha (ADR-0005).
    action = await last_action(session_factory)
    assert action.action_kind == "commit"
    assert action.correlation_id == changeset.branch
    assert action.status == "succeeded"
    assert action.remote_result == {"sha": result.commit_sha}


async def test_branch_already_exists_is_tolerated(session_factory, changeset):
    fake = FakeGitLab()
    fake.branches[changeset.branch] = []  # branch pre-exists (crash re-entry)
    writer = ChangesetWriter(fake, session_factory, project_id=42)

    result = await writer.apply(RUN_ID, changeset, start_ref="main")

    assert result.outcome is WriteOutcome.COMMITTED
    assert fake.calls_of("get_branch")  # existing branch verified, not assumed
    assert len(fake.calls_of("create_commit")) == 1


async def test_branch_creation_error_propagates(session_factory, changeset):
    fake = FakeGitLab()
    fake.raise_on_create_commit = None
    writer = ChangesetWriter(fake, session_factory, project_id=42)

    # Simulate a non-tolerable branch error by making create_branch 403.
    async def forbidden(*args, **kwargs):
        raise GitLabAPIError(403, "forbidden")

    fake.create_branch = forbidden
    with pytest.raises(GitLabAPIError):
        await writer.apply(RUN_ID, changeset)


async def test_timeout_then_reconcile_finds_single_commit(session_factory, changeset):
    fake = FakeGitLab()
    fake.create_commit_timeout_applies = True  # lost response, server executed
    writer = ChangesetWriter(fake, session_factory, project_id=42)

    result = await writer.apply(RUN_ID, changeset, start_ref="main")

    assert result.outcome is WriteOutcome.COMMITTED
    assert result.commit_sha == fake.branches[changeset.branch][0]["sha"]

    action = await last_action(session_factory)
    assert action.status == "succeeded"
    assert action.remote_result == {"sha": result.commit_sha, "reconciled": True}


async def test_timeout_with_ambiguous_commits_stays_unknown(session_factory, changeset):
    fake = FakeGitLab()
    # Two commits carry THIS attempt's marker with the same parent (the write
    # executed twice) — neither can be attributed to one SHA.
    fake.seed_commit(
        changeset.branch,
        "sha-twin",
        f"{changeset.commit_message} (forge-op:fixedkey12345)",
        parents=["sha-base"],
    )
    fake.seed_commit(changeset.branch, "sha-base", "branch head at intent time")
    fake.create_commit_timeout_applies = True
    writer = ChangesetWriter(fake, session_factory, project_id=42)

    result = await writer.apply(RUN_ID, changeset, start_ref="main", operation_key="fixedkey12345")

    assert result == WriteResult(WriteOutcome.UNKNOWN, None)
    action = await last_action(session_factory)
    assert action.status == "unknown_outcome"


async def test_timeout_with_no_landed_commit_stays_unknown(session_factory, changeset):
    fake = FakeGitLab()
    fake.create_commit_timeout_drops = True  # lost response, nothing executed
    writer = ChangesetWriter(fake, session_factory, project_id=42)

    result = await writer.apply(RUN_ID, changeset, start_ref="main")

    # Conservative: unresolved means the run blocks — never a blind retry.
    assert result.outcome is WriteOutcome.UNKNOWN
    action = await last_action(session_factory)
    assert action.status == "unknown_outcome"


async def test_gitlab_error_journals_failure_and_raises(session_factory, changeset):
    fake = FakeGitLab()
    fake.raise_on_create_commit = GitLabAPIError(400, "invalid actions")
    writer = ChangesetWriter(fake, session_factory, project_id=42)

    with pytest.raises(GitLabAPIError):
        await writer.apply(RUN_ID, changeset, start_ref="main")

    action = await last_action(session_factory)
    assert action.status == "failed"


async def test_action_log_intent_written_before_dispatch(session_factory, changeset):
    """The intent row must exist even if the HTTP call never returns."""
    fake = FakeGitLab()

    original = fake.create_commit

    async def inspecting(*args, **kwargs):
        action = await last_action(session_factory)
        assert action is not None  # intent journaled BEFORE dispatch
        assert action.status == "requested"
        return await original(*args, **kwargs)

    fake.create_commit = inspecting
    writer = ChangesetWriter(fake, session_factory, project_id=42)

    result = await writer.apply(RUN_ID, changeset, start_ref="main")
    assert result.outcome is WriteOutcome.COMMITTED


async def test_no_start_branch_when_branch_pre_exists(session_factory, changeset):
    """GitLab 18.x rejects create_commit with start_branch on an existing
    branch (400 'already exists') — the writer must omit it there."""
    fake = FakeGitLab()
    fake.branches[changeset.branch] = []  # branch pre-exists (crash re-entry)
    writer = ChangesetWriter(fake, session_factory, project_id=42)

    result = await writer.apply(RUN_ID, changeset, start_ref="main")

    assert result.outcome is WriteOutcome.COMMITTED
    call = fake.calls_of("create_commit")[-1]
    assert call[1][4] is None  # start_branch omitted


async def test_no_start_branch_on_fresh_branch(session_factory, changeset):
    """GitLab CE 18.x Commits API re-creates the branch when start_branch is
    passed and 400s 'already exists' — start_branch must never be sent,
    even on the fresh-branch path (ensure_branch owns creation)."""
    fake = FakeGitLab()
    writer = ChangesetWriter(fake, session_factory, project_id=42)

    result = await writer.apply(RUN_ID, changeset, start_ref="main")

    assert result.outcome is WriteOutcome.COMMITTED
    call = fake.calls_of("create_commit")[-1]
    assert call[1][4] is None


async def test_operation_marker_is_unique_per_apply_call(session_factory, changeset):
    """F07: each apply() stamps a fresh operation key into the commit message
    so a previous repair cycle's commit can never collide with this one."""
    fake = FakeGitLab()
    writer = ChangesetWriter(fake, session_factory, project_id=42)

    await writer.apply(RUN_ID, changeset, start_ref="main")
    first_message = fake.calls_of("create_commit")[-1][1][3]
    await writer.apply(RUN_ID, changeset, start_ref="main")
    second_message = fake.calls_of("create_commit")[-1][1][3]

    assert first_message != second_message
    assert first_message.startswith(changeset.commit_message + " (forge-op:")
    assert second_message == f"{changeset.commit_message} (forge-op:{writer.operation_key})"


async def test_expected_head_mismatch_aborts_before_commit(session_factory, changeset):
    """F03: the branch moved away from the pinned base — raise BranchDriftError
    with both OIDs and never dispatch the commit."""
    fake = FakeGitLab()
    fake.branches[changeset.branch] = []  # branch pre-exists, then a human pushed
    fake.seed_commit(changeset.branch, "moved-head-sha", "human push")
    writer = ChangesetWriter(fake, session_factory, project_id=42)

    with pytest.raises(BranchDriftError) as exc_info:
        await writer.apply(RUN_ID, changeset, start_ref="main", expected_head="pinned-base-sha")

    assert exc_info.value.expected == "pinned-base-sha"
    assert exc_info.value.actual == "moved-head-sha"
    assert fake.calls_of("create_commit") == []  # aborted BEFORE dispatch


async def test_expected_head_match_commits_and_journals_pinned_head(session_factory, changeset):
    """F03: a matching expected_head lets the commit through, and the pinned
    head is recorded in the action_log outcome metadata."""
    fake = FakeGitLab()
    fake.branches[changeset.branch] = []
    fake.seed_commit(changeset.branch, "pinned-base-sha", "base")
    writer = ChangesetWriter(fake, session_factory, project_id=42)

    result = await writer.apply(
        RUN_ID, changeset, start_ref="main", expected_head="pinned-base-sha"
    )

    assert result.outcome is WriteOutcome.COMMITTED
    action = await last_action(session_factory)
    assert action.remote_result == {
        "sha": result.commit_sha,
        "expected_head": "pinned-base-sha",
    }


async def test_reconcile_ignores_previous_cycle_same_message(session_factory, changeset):
    """F07: a prior repair cycle's commit repeats the human message but carries
    a different operation marker — only this attempt's commit is attributed."""
    fake = FakeGitLab()
    fake.seed_commit(
        changeset.branch,
        "sha-prev",
        f"{changeset.commit_message} (forge-op:prev1cycle12)",
    )
    fake.create_commit_timeout_applies = True
    writer = ChangesetWriter(fake, session_factory, project_id=42)

    result = await writer.apply(RUN_ID, changeset, start_ref="main", operation_key="currcycle3456")

    assert result.outcome is WriteOutcome.COMMITTED
    assert result.commit_sha != "sha-prev"
    action = await last_action(session_factory)
    assert action.status == "succeeded"
    assert action.remote_result == {"sha": result.commit_sha, "reconciled": True}


async def test_reconcile_ignores_right_marker_but_wrong_parent(session_factory, changeset):
    """F07: the exact message (marker included) on a commit whose parent is
    not the intent-time branch head proves nothing — stay unknown, block."""
    fake = FakeGitLab()
    fake.branches[changeset.branch] = []
    fake.seed_commit(
        changeset.branch,
        "orphan-sha",
        f"{changeset.commit_message} (forge-op:fixedkey12345)",
        parents=["some-other-base"],
    )
    fake.create_commit_timeout_drops = True  # nothing landed for this attempt
    writer = ChangesetWriter(fake, session_factory, project_id=42)

    result = await writer.apply(RUN_ID, changeset, start_ref="main", operation_key="fixedkey12345")

    assert result == WriteResult(WriteOutcome.UNKNOWN, None)
    action = await last_action(session_factory)
    assert action.status == "unknown_outcome"
