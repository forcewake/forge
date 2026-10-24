"""Owned database triplets and frozen historical definitions for cutover proofs."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from forge.store.schema.__main__ import engine

PG_URL = os.environ.get("FORGE_SCHEMA_TEST_DATABASE_URL")
URLS = ["sqlite"] + ([PG_URL] if PG_URL else [])


@dataclass
class Databases:
    source: AsyncEngine
    destination: AsyncEngine
    verification: AsyncEngine
    directory: Path


@pytest.fixture(params=URLS, ids=lambda value: "sqlite" if value == "sqlite" else "postgres")
async def databases(tmp_path, request):
    names, admin = [], None
    if request.param == "sqlite":
        urls = [
            "sqlite+aiosqlite:///" + str(tmp_path / (name + ".db"))
            for name in ("source", "target", "verify")
        ]
    else:
        base = make_url(request.param)
        admin = engine(base.set(database="postgres").render_as_string(hide_password=False))
        names = ["forge_schema_case_" + uuid4().hex for _ in range(3)]
        async with admin.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            for name in names:
                await connection.exec_driver_sql("CREATE DATABASE " + name)
        urls = [base.set(database=name).render_as_string(hide_password=False) for name in names]
    engines = [engine(url) for url in urls]
    try:
        yield Databases(*engines, tmp_path)
    finally:
        for item in engines:
            await item.dispose()
        if admin is not None:
            async with admin.connect() as connection:
                connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
                for name in names:
                    await connection.exec_driver_sql("DROP DATABASE " + name)
            await admin.dispose()


async def legacy(database: AsyncEngine) -> None:
    async with database.begin() as connection:
        document = json.loads(
            (Path(__file__).parent / "fixtures/schema_legacy_042.json").read_text()
        )
        for statement in document["dialects"][database.dialect.name]["ddl"]:
            await connection.exec_driver_sql(statement)
        await connection.exec_driver_sql("INSERT INTO alembic_version (version_num) VALUES ('032')")
        await connection.exec_driver_sql(
            "INSERT INTO flow_runs (id,project_id,issue_iid,status,cancel_requested,commit_cycle,created_at,updated_at) VALUES ('history',1,7,'ready_for_human',false,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        blob_type = "BYTEA" if database.dialect.name == "postgresql" else "BLOB"
        await connection.exec_driver_sql(
            f"CREATE TABLE historical_payload (id INTEGER PRIMARY KEY, payload {blob_type}, exact_money TEXT, body TEXT)"
        )
        await connection.execute(
            text(
                "INSERT INTO historical_payload (id,payload,exact_money,body) VALUES (1,:payload,:money,:body)"
            ),
            {
                "payload": b"\x00\xff\r\n",
                "money": "99999999999999999999.000000000000000001",
                "body": "exact\r\nΩ",
            },
        )
