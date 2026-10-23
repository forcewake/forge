"""Fixtures for retained publication contracts over the product writer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forge.durable import FlowRun, PublicationIntent, mark_dispatched, record_intent
from forge.durable.identity import factory_branch
from forge.models.base import Base
from forge.repository import Change, ChangeSet, Operation
from tests.fixtures.fake_gitlab import FakeGitLab

BASE = "a" * 40
RUN_ID = "1" * 32
PROJECT = 42
ISSUE = 7
BRANCH = factory_branch(ISSUE, RUN_ID)


@pytest.fixture()
async def publication_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'publication.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await seed_run(factory)
        yield factory
    finally:
        await engine.dispose()


async def seed_run(factory, run_id=RUN_ID, project_id=PROJECT, issue_iid=ISSUE):
    run = FlowRun(id=run_id, project_id=project_id, issue_iid=issue_iid, base_sha=BASE)
    async with factory() as session:
        session.add(run)
        await session.commit()
    return run


def remote():
    fake = FakeGitLab()
    fake.seed_commit("main", BASE)
    fake.seed_commit(BRANCH, BASE)
    return fake


def changeset(branch=BRANCH, path="src/feature.py"):
    return ChangeSet(
        branch=branch,
        commit_message="forge: implement approved task",
        changes=[Change(path=path, operation=Operation.CREATE, content="VALUE = 1\n")],
    )


async def dispatched_intent(
    factory, *, key="publishedkey123", run_id=RUN_ID, project_id=PROJECT, branch=BRANCH, parent=BASE
):
    async with factory() as session:
        intent = await record_intent(
            session,
            run_id=run_id,
            provider="gitlab",
            repo=str(project_id),
            target_ref=branch,
            idempotency_scope="cycle-1",
            operation_key=key,
            expected_parent_oid=parent,
            expected_head=parent,
        )
        await mark_dispatched(session, intent.id)
        await session.commit()
    return intent


async def read_intent(factory, intent_id):
    async with factory() as session:
        return await session.get(PublicationIntent, intent_id)


async def expire_probe(factory, intent_id):
    async with factory() as session:
        row = await session.get(PublicationIntent, intent_id)
        row.next_probe_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()
