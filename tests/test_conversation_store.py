"""Chat is retired; historical thread identity and stored data remain intact."""

import json
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from forge.database import init_db
from tests.fixtures.historical_db import historical_engine as historical_engine

STATEMENT = text(
    "INSERT INTO conversations(project_id,noteable_type,noteable_iid,discussion_id,messages,created_at,updated_at) VALUES(:project,:kind,:iid,:discussion,:messages,'2026-09-01','2026-09-01')"
)


def thread(**overrides):
    values = dict(
        project=1,
        kind="MergeRequest",
        iid=7,
        discussion="discussion-1",
        messages=json.dumps([{"role": "user", "content": "historical text"}]),
    )
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    "other", [{"project": 2}, {"kind": "Issue"}, {"iid": 8}, {"discussion": "discussion-2"}]
)
async def test_historical_thread_keys_are_isolated(historical_engine, other):
    async with historical_engine.begin() as connection:
        await connection.execute(STATEMENT, thread())
        await connection.execute(STATEMENT, thread(**other))
        assert await connection.scalar(text("SELECT count(*) FROM conversations")) == 2


async def test_duplicate_historical_thread_identity_is_rejected(historical_engine):
    async with historical_engine.begin() as connection:
        await connection.execute(STATEMENT, thread())
        with pytest.raises(IntegrityError):
            await connection.execute(STATEMENT, thread())


async def test_booting_again_keeps_historical_messages_in_order(historical_engine):
    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "old reply"},
    ]
    async with historical_engine.begin() as connection:
        await connection.execute(STATEMENT, thread(messages=json.dumps(messages)))
    await init_db(str(historical_engine.url))
    async with historical_engine.connect() as connection:
        stored = await connection.scalar(text("SELECT messages FROM conversations"))
    assert json.loads(stored) == messages
