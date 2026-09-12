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

    # Branch created from the pinned start ref; commit applied with actions.
    assert [args for _, args in fake.calls_of("create_branch")] == [(42, changeset.branch, "main")]
    (call,) = fake.calls_of("create_commit")
    _, (project_id, branch, actions, message, start_branch) = call
    assert (project_id, branch, message, start_branch) == (
        42,
        changeset.branch,
        changeset.commit_message,
        "main",
    )
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
    # A previous attempt already landed the same message on the branch.
    fake.seed_commit(changeset.branch, "sha-older", changeset.commit_message)
    fake.create_commit_timeout_applies = True
    writer = ChangesetWriter(fake, session_factory, project_id=42)

    result = await writer.apply(RUN_ID, changeset, start_ref="main")

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
