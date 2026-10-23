"""Independent run publication retains isolation without a multi-repo coordinator."""

from sqlalchemy import select

from forge.durable import Controller, FlowRun, PublicationIntent, op_marker
from forge.durable.identity import factory_branch
from forge.repository import ChangesetWriter, WriteOutcome
from forge.runs.candidate import bundle_from_changeset
from forge.runs.publisher import publish_candidate
from tests.fixtures.publication_ports import (
    BASE,
    BRANCH,
    PROJECT,
    RUN_ID,
    changeset,
    dispatched_intent,
    publication_db as publication_db,
    remote,
    seed_run,
)


async def test_independent_runs_get_distinct_journaled_operation_keys(publication_db):
    fake = remote()
    other_id = "2" * 32
    await seed_run(publication_db, other_id, issue_iid=8)
    branches = [BRANCH, factory_branch(8, other_id)]
    writers = []
    for run_id, branch in zip((RUN_ID, other_id), branches, strict=True):
        fake.seed_commit(branch, BASE)
        writer = ChangesetWriter(fake, publication_db, PROJECT)
        result = await writer.apply(run_id, changeset(branch), expected_head=BASE)
        assert result.outcome == WriteOutcome.COMMITTED
        writers.append(writer)
    assert writers[0].operation_key != writers[1].operation_key
    async with publication_db() as session:
        rows = (await session.scalars(select(PublicationIntent))).all()
    assert {row.run_id for row in rows} == {RUN_ID, other_id}
    assert {row.target_ref for row in rows} == set(branches)
    assert {row.status for row in rows} == {"committed"}
    for branch, writer in zip(branches, writers, strict=True):
        assert op_marker(writer.operation_key) in fake.branches[branch][0]["message"]


async def test_one_runs_unknown_outcome_does_not_start_or_publish_another(publication_db):
    fake = remote()
    await dispatched_intent(publication_db)
    other_id = "2" * 32
    await seed_run(publication_db, other_id, issue_iid=8)
    result = await ChangesetWriter(fake, publication_db, PROJECT).apply(
        RUN_ID,
        changeset(),
        expected_head=BASE,
    )
    assert result.outcome == WriteOutcome.SETTLING
    async with publication_db() as session:
        rows = (await session.scalars(select(PublicationIntent))).all()
        other = await session.get(FlowRun, other_id)
    assert len(rows) == 1 and rows[0].run_id == RUN_ID
    assert other.status == "accepted"
    assert not fake.calls_of("create_commit")


async def test_repository_identity_separates_open_intents_for_the_same_run(publication_db):
    first = await dispatched_intent(publication_db, project_id=PROJECT)
    second = await dispatched_intent(publication_db, project_id=PROJECT + 1, key="otherrepokey123")
    fake = remote()
    fake.seed_commit(BRANCH, "landed", op_marker(first.operation_key), parents=[BASE])
    writer = ChangesetWriter(fake, publication_db, PROJECT)
    result = await writer.apply(RUN_ID, changeset(), expected_head=BASE)
    assert result.outcome == WriteOutcome.COMMITTED and result.commit_sha == "landed"
    async with publication_db() as session:
        adopted = await session.get(PublicationIntent, first.id)
        untouched = await session.get(PublicationIntent, second.id)
    assert untouched.status == "dispatched"
    assert adopted.status == "adopted"
    assert not fake.calls_of("create_commit")


async def test_cancelled_publication_does_not_undo_a_landed_effect(publication_db):
    fake = remote()
    async with publication_db() as session:
        run = await session.get(FlowRun, RUN_ID)
    writer = ChangesetWriter(fake, publication_db, PROJECT)
    original = writer.apply

    async def cancel_during_write(*args, **kwargs):
        async with publication_db() as session:
            await Controller(session).request_cancel(RUN_ID)
            await session.commit()
        return await original(*args, **kwargs)

    writer.apply = cancel_during_write
    result = await publish_candidate(
        gitlab=fake,
        session_factory=publication_db,
        writer=writer,
        run=run,
        bundle=bundle_from_changeset(changeset(), attempt_base_oid=BASE),
    )
    assert result.ok and result.superseded
    assert len(fake.calls_of("create_commit")) == 1
    assert fake.branches[BRANCH][0]["sha"] == result.commit_sha
    async with publication_db() as session:
        final = await session.get(FlowRun, RUN_ID)
    assert final.status != "ready_for_human"
    assert final.evidence["superseded"]["reason"] == "cancelled_during_publication"
