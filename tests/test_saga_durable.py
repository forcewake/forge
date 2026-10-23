"""Retained durable-publication lessons, exercised on the product writer."""

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forge.durable import ActionLog, PublicationIntent, op_marker
from forge.gitlab.client import GitLabAPIError
from forge.repository import ChangesetWriter, WriteOutcome
from forge.repository.writer import BranchDriftError
from tests.fixtures.publication_ports import (
    BASE,
    BRANCH,
    PROJECT,
    RUN_ID,
    changeset,
    dispatched_intent,
    expire_probe,
    publication_db as publication_db,
    read_intent,
    remote,
)


def test_obsolete_publication_authority_is_absent():
    root = Path(__file__).resolve().parents[1] / "src/forge/adaptive"
    names = ("publication_saga", "saga_durable", "saga_native", "two_writer_qualification")
    assert not [name for name in names if (root / f"{name}.py").exists()]
    assert not (root / "reference/native_shaped_remote.py").exists()


async def test_intent_and_action_are_committed_before_provider_write(publication_db):
    fake = remote()
    original = fake.create_commit
    observed = []

    async def inspect_before_effect(*args, **kwargs):
        async with publication_db() as session:
            intent = (await session.scalars(select(PublicationIntent))).one()
            action = (await session.scalars(select(ActionLog))).one()
            observed.append((intent.status, action.status, intent.operation_key))
        return await original(*args, **kwargs)

    fake.create_commit = inspect_before_effect
    writer = ChangesetWriter(fake, publication_db, PROJECT)
    result = await writer.apply(RUN_ID, changeset(), expected_head=BASE)
    assert result.outcome == WriteOutcome.COMMITTED
    assert observed == [("dispatched", "requested", writer.operation_key)]
    assert op_marker(writer.operation_key) in fake.calls_of("create_commit")[0][1][3]


async def test_new_engine_adopts_landed_intent_without_second_write(publication_db):
    fake = remote()
    intent = await dispatched_intent(publication_db)
    fake.seed_commit(BRANCH, "landed", op_marker(intent.operation_key), parents=[BASE])
    engine = create_async_engine(str(publication_db.kw["bind"].url))
    restarted = async_sessionmaker(engine, expire_on_commit=False)
    try:
        result = await ChangesetWriter(fake, restarted, PROJECT).apply(
            RUN_ID,
            changeset(),
            expected_head=BASE,
        )
        resolved = await read_intent(restarted, intent.id)
    finally:
        await engine.dispose()
    assert result.outcome == WriteOutcome.COMMITTED
    assert result.commit_sha == "landed"
    assert resolved.status == "adopted"
    assert resolved.provider_object_id == "landed"
    assert not fake.calls_of("create_commit")


async def test_negative_probe_waits_then_parks_unknown_without_republish(publication_db):
    fake = remote()
    intent = await dispatched_intent(publication_db)
    outcomes = []
    for _ in range(4):
        result = await ChangesetWriter(fake, publication_db, PROJECT).apply(
            RUN_ID,
            changeset(),
            expected_head=BASE,
        )
        outcomes.append(result.outcome)
        await expire_probe(publication_db, intent.id)
    assert outcomes == [WriteOutcome.SETTLING] * 3 + [WriteOutcome.UNKNOWN]
    resolved = await read_intent(publication_db, intent.id)
    assert resolved.status == "unknown"
    assert resolved.remote_result["reason"] == "settle_window_exhausted"
    assert "operator_instruction" in resolved.remote_result
    assert not fake.calls_of("create_commit")


async def test_unavailable_probe_remains_unknown_without_writing(publication_db):
    fake = remote()
    intent = await dispatched_intent(publication_db)

    async def unavailable(*args, **kwargs):
        raise GitLabAPIError(503, "provider unavailable")

    fake.list_commits = unavailable
    result = await ChangesetWriter(fake, publication_db, PROJECT).apply(
        RUN_ID,
        changeset(),
        expected_head=BASE,
    )
    assert result.outcome == WriteOutcome.UNKNOWN
    pending = await read_intent(publication_db, intent.id)
    assert pending.status == "dispatched"
    assert pending.next_probe_at is not None
    assert not fake.calls_of("create_commit")


@pytest.mark.parametrize("wrong_parent", [False, True])
async def test_ambiguous_or_wrong_parent_marker_never_adopts(publication_db, wrong_parent):
    fake = remote()
    intent = await dispatched_intent(publication_db)
    parent = "foreign-parent" if wrong_parent else BASE
    fake.seed_commit(BRANCH, "candidate-1", op_marker(intent.operation_key), parents=[parent])
    if not wrong_parent:
        fake.seed_commit(BRANCH, "candidate-2", op_marker(intent.operation_key), parents=[BASE])
    writer = ChangesetWriter(fake, publication_db, PROJECT)
    if wrong_parent:
        with pytest.raises(BranchDriftError):
            await writer.apply(RUN_ID, changeset(), expected_head=BASE)
        assert (await read_intent(publication_db, intent.id)).status == "duplicated"
    else:
        assert (await writer.apply(RUN_ID, changeset())).outcome == WriteOutcome.UNKNOWN
        assert (await read_intent(publication_db, intent.id)).status == "unknown"
    assert not fake.calls_of("create_commit")
