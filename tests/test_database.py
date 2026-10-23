"""Legacy ORM retirement preserves the historical tables and constraints."""

import pytest
from sqlalchemy import text, inspect
from sqlalchemy.exc import IntegrityError
from tests.fixtures.historical_db import historical_engine as historical_engine


async def test_historical_agent_rows_remain_readable(historical_engine):
    async with historical_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO agent_runs(project_id,event_type,agent_name,status,created_at) VALUES(1,'note','historical','success','2026-09-01')"
            )
        )
        row = (
            await connection.execute(
                text("SELECT agent_name,status FROM agent_runs WHERE project_id=1")
            )
        ).one()
    assert tuple(row) == ("historical", "success")


async def test_historical_review_identity_remains_unique(historical_engine):
    statement = text(
        "INSERT INTO review_states(project_id,mr_iid,last_reviewed_sha,created_at,updated_at) VALUES(1,7,:sha,'2026-09-01','2026-09-01')"
    )
    async with historical_engine.begin() as connection:
        await connection.execute(statement, {"sha": "a" * 40})
        with pytest.raises(IntegrityError):
            await connection.execute(statement, {"sha": "b" * 40})


async def test_retired_tables_still_exist_without_orm_classes(historical_engine):
    async with historical_engine.connect() as connection:
        tables = await connection.run_sync(lambda conn: set(inspect(conn).get_table_names()))
    assert {"agent_runs", "review_states", "conversations"} <= tables
