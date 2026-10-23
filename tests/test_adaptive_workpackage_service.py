"""Single-run durability lessons retained from the retired coordinator.

The multiwriter phase graph is deferred by ADR-0035. Intent ordering,
replay adoption, unknown outcomes and frozen evidence still apply to the
real publisher and durable control mailbox.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
import pytest

from forge.adaptive.mailbox_db import PostgresMailbox
from forge.adaptive.models import ControlCommand
from forge.durable import (
    ActionLog,
    FlowRun,
    PublicationIntent,
    mark_dispatched,
    op_marker,
    record_intent,
)
from forge.models.base import Base
from forge.repository import Change, ChangeSet, ChangesetWriter, Operation, WriteOutcome
from forge.runs.consistency import ReadyInvariantError, assert_ready_invariants, verified_verdict
from tests.fixtures.fake_gitlab import FakeGitLab

RUN_ID = "ported-single-run"
BRANCH = "factory/7/ported"
CANDIDATE = "a" * 40
OTHER = "b" * 40


@pytest.fixture()
async def database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(FlowRun(id=RUN_ID, project_id=42, issue_iid=7, provider="gitlab"))
        await session.commit()
    yield factory
    await engine.dispose()


def changeset():
    return ChangeSet(
        branch=BRANCH,
        commit_message="forge: implement 7",
        changes=[Change(path="src/ported.py", operation=Operation.CREATE, content="VALUE = 1\n")],
    )


async def test_intent_and_action_are_durable_before_publication(database):
    fake = FakeGitLab()
    original = fake.create_commit
    observed = []

    async def before_effect(*args, **kwargs):
        async with database() as session:
            intent = await session.scalar(
                select(PublicationIntent).where(PublicationIntent.run_id == RUN_ID)
            )
            action = await session.scalar(select(ActionLog).where(ActionLog.flow_run_id == RUN_ID))
        assert intent is not None and intent.status == "dispatched"
        assert action is not None and action.status == "requested"
        observed.append(intent.operation_key)
        return await original(*args, **kwargs)

    fake.create_commit = before_effect
    result = await ChangesetWriter(fake, database, project_id=42).apply(
        RUN_ID, changeset(), start_ref="main"
    )
    assert result.outcome is WriteOutcome.COMMITTED
    assert len(observed) == 1


async def test_a_restarted_publisher_adopts_the_frozen_effect(database):
    key = "ported-effect-key"
    async with database() as session:
        intent = await record_intent(
            session,
            run_id=RUN_ID,
            provider="gitlab",
            repo="42",
            target_ref=BRANCH,
            idempotency_scope="cycle-1",
            operation_key=key,
        )
        await mark_dispatched(session, intent.id)
        await session.commit()
    fake = FakeGitLab()
    fake.seed_commit(BRANCH, CANDIDATE, f"forge: implement 7 {op_marker(key)}", parents=[])
    result = await ChangesetWriter(fake, database, project_id=42).apply(
        RUN_ID, changeset(), start_ref="main"
    )
    assert result.outcome is WriteOutcome.COMMITTED
    assert result.commit_sha == CANDIDATE
    assert fake.calls_of("create_commit") == []
    async with database() as session:
        restored = await session.get(PublicationIntent, intent.id)
    assert restored.status == "adopted"
    assert restored.operation_key == key


def command(**overrides):
    values = dict(
        schema="forge.proposal.control-command/1",
        command_id="command-1",
        work_id=RUN_ID,
        sequence=1,
        kind="pause",
        actor_ref="human:owner",
        actor_origin="server_authenticated_human",
        idempotency_key="event-1",
        status="received",
    )
    values.update(overrides)
    return ControlCommand.model_validate(values)


async def test_a_restarted_mailbox_reads_the_first_committed_delivery(database):
    first, created = await PostgresMailbox(database).submit(command())
    assert created
    replay, created = await PostgresMailbox(database).submit(
        command(command_id="replay", sequence=2, status="checkpointed")
    )
    assert not created
    assert replay.command_id == first.command_id
    assert replay.status == "received"
    recovered = await PostgresMailbox(database).get(first.command_id)
    assert recovered == first


async def test_unknown_delivery_is_neither_pending_nor_checkpointed(database):
    mailbox = PostgresMailbox(database)
    await mailbox.submit(command())
    await mailbox.authorize("command-1", {"server_authenticated_human": ("human:owner",)})
    await mailbox.dispatch("command-1", current_plan_revision=1, current_execution_epoch=1)
    await mailbox.outcome_unknown("command-1")
    reopened = PostgresMailbox(database)
    assert await reopened.pending(RUN_ID) == []
    assert (await reopened.get("command-1")).status == "outcome_unknown"
    with pytest.raises(ValueError):
        await reopened.checkpoint("command-1")


@pytest.mark.parametrize(
    "verification",
    [
        None,
        {},
        {"status": "unknown", "tested_oid": CANDIDATE},
        {"status": "passed", "tested_oid": OTHER},
    ],
)
def test_unknown_or_foreign_evidence_is_not_a_verified_result(verification):
    assert verified_verdict(verification, CANDIDATE) is False


@pytest.mark.parametrize("status", ["cancelled", "failed", "blocked"])
def test_terminal_failure_never_advances_to_ready(status):
    with pytest.raises(ReadyInvariantError):
        assert_ready_invariants(status, {}, CANDIDATE, CANDIDATE)


def test_a_ready_transition_is_bound_to_the_current_reviewed_and_tested_candidate():
    evidence = {"verification": {"status": "passed", "tested_oid": CANDIDATE}}
    assert_ready_invariants("ready_for_human", evidence, CANDIDATE, CANDIDATE)
    with pytest.raises(ReadyInvariantError, match="different sha"):
        assert_ready_invariants("ready_for_human", evidence, CANDIDATE, OTHER)
    with pytest.raises(ReadyInvariantError, match="different commit"):
        assert_ready_invariants("ready_for_human", evidence, OTHER, OTHER)
