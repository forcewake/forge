"""The final startup gate has one schema owner and never upgrades an existing DB."""

import asyncio
from decimal import Decimal
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

import forge.store.models  # noqa: F401
from forge.store.base import Base
from forge.store.models import EngineBudgetPool
from forge.store.records import RunRequest
from forge.store.runs import RunStore
from forge.store.schema import SchemaMismatch, check_schema, initialize
from forge.store.schema import baseline
from forge.store.schema.signature import inspect_schema
from tests.schema_fixture import legacy
from tests.schema_fixture import databases as databases
from scripts.render_schema import statements


async def shape(database):
    async with database.connect() as connection:
        return await connection.run_sync(inspect_schema)


async def test_startup_refuses_fresh_and_legacy_without_creating_or_repairing_tables(databases):
    database = databases.source
    with pytest.raises(SchemaMismatch):
        await check_schema(database)
    if database.dialect.name == "sqlite":
        assert not Path(database.url.database).exists()
    await legacy(database)
    before = await shape(database)
    with pytest.raises(SchemaMismatch):
        await check_schema(database)
    assert await shape(database) == before
    with pytest.raises(SchemaMismatch):
        await initialize(database)
    assert await shape(database) == before


async def test_one_fresh_baseline_is_complete_unprefixed_and_preserves_exact_resource_values(
    databases,
):
    database = databases.source
    await initialize(database)
    await check_schema(database)
    names = set((await shape(database))["tables"])
    assert names == set(Base.metadata.tables) | {"alembic_version"}
    assert "flow_runs" not in names and not any(name.startswith("eng_") for name in names)
    factory = async_sessionmaker(database, expire_on_commit=False)
    run = await RunStore(factory).admit(
        RunRequest("a" * 32, "github", "connection", "123", "issue", "7")
    )
    amount = Decimal("99999999999999999999.000000000000000001")
    async with factory.begin() as session:
        session.add(
            EngineBudgetPool(
                id="b" * 32,
                run_id=run.id,
                purpose="implementation",
                usd=amount,
                calls=2**63 - 1,
                tokens=2**63 - 1,
                state_json="{}",
            )
        )
    async with factory() as session:
        saved = await session.scalar(select(EngineBudgetPool))
        assert saved.usd == amount and saved.tokens == 2**63 - 1


@pytest.mark.parametrize("fault", ("revision", "index", "installation", "extra_table"))
async def test_stamp_or_missing_gate_constraint_is_refused_without_mutation(databases, fault):
    database = databases.source
    await initialize(database)
    async with database.begin() as connection:
        if fault == "revision":
            await connection.execute(text("UPDATE alembic_version SET version_num='032'"))
        elif fault == "index":
            await connection.exec_driver_sql("DROP INDEX uq_active_subject")
        elif fault == "installation":
            await connection.execute(
                text("UPDATE schema_installation SET baseline_sha256=:digest"), {"digest": "0" * 64}
            )
        else:
            await connection.exec_driver_sql("CREATE TABLE unknown_runtime_state (id INTEGER)")
    before = await shape(database)
    with pytest.raises(SchemaMismatch):
        await check_schema(database)
    assert await shape(database) == before


async def test_concurrent_installers_have_one_winner_and_no_partial_schema(databases):
    database = databases.source
    outcomes = await asyncio.gather(
        initialize(database), initialize(database), return_exceptions=True
    )
    assert sum(result is None for result in outcomes) == 1
    assert sum(isinstance(result, SchemaMismatch) for result in outcomes) == 1
    await check_schema(database)


async def test_failed_fresh_install_rolls_back_every_table(databases, monkeypatch):
    from forge.store.schema import gate

    database = databases.source

    def refuse(connection):
        raise SchemaMismatch("injected post-install validation refusal")

    monkeypatch.setattr(gate, "check_connection", refuse)
    with pytest.raises(SchemaMismatch):
        await initialize(database)
    async with database.connect() as connection:
        assert await connection.run_sync(lambda sync: inspect(sync).get_table_names()) == []


def test_static_baseline_matches_current_models_and_has_one_packaged_revision():
    from forge.store.schema.migration import script

    assert script().get_heads() == [baseline.REVISION]
    assert len(list(script().walk_revisions())) == 1
    for dialect in ("sqlite", "postgresql"):
        assert statements(Base.metadata, dialect) == baseline.document()["dialects"][dialect]["ddl"]


def test_store_model_import_loads_no_retired_owner():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, forge.store.models; assert not any(k == 'forge.models' or k.startswith(('forge.models.', 'forge.durable.', 'forge.runs.')) for k in sys.modules)",
        ],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()


async def test_multiple_version_heads_and_disabled_native_foreign_keys_are_refused(databases):
    database = databases.source
    await initialize(database)
    async with database.begin() as connection:
        await connection.execute(text("INSERT INTO alembic_version VALUES ('other-head')"))
    with pytest.raises(SchemaMismatch, match="version differs"):
        await check_schema(database)
    async with database.begin() as connection:
        await connection.execute(text("DELETE FROM alembic_version WHERE version_num='other-head'"))
    if database.dialect.name == "sqlite":
        from forge.store.schema.gate import check_connection

        async with database.connect() as connection:
            await connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            with pytest.raises(SchemaMismatch, match="foreign-key"):
                await connection.run_sync(check_connection)
    else:
        async with database.begin() as connection:
            await connection.exec_driver_sql("ALTER TABLE runs ENABLE ROW LEVEL SECURITY")
        with pytest.raises(SchemaMismatch, match="physical schema"):
            await check_schema(database)


async def test_non_table_native_objects_also_prevent_fresh_install(databases):
    database = databases.source
    async with database.begin() as connection:
        if database.dialect.name == "sqlite":
            await connection.exec_driver_sql("CREATE VIEW owner_view AS SELECT 7 AS id")
        else:
            await connection.exec_driver_sql("CREATE SEQUENCE owner_sequence")
    before = await shape(database)
    with pytest.raises(SchemaMismatch, match="empty"):
        await initialize(database)
    assert await shape(database) == before
