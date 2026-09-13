"""F26 — DB engine lifecycle and the schema compatibility gate.

Covers:

- the engine/session-factory caches being keyed by database URL,
- ``dispose_engine`` actually disposing cached engines on shutdown,
- ``init_db``: fresh DB → create_all + schema_version marker; existing forge
  DB with a missing/wrong marker → RuntimeError with upgrade instructions,
- migration ``007b`` writing the ``schema_version`` row alembic-managed DBs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from forge.database import (
    EXPECTED_SCHEMA_VERSION,
    dispose_engine,
    get_engine,
    get_session_factory,
    init_db,
    reset_engine,
)


@pytest.fixture(autouse=True)
async def _clean_engine_cache():
    reset_engine()
    yield
    await dispose_engine()


async def test_engines_are_cached_per_database_url():
    """Two different URLs never share an engine (F26)."""
    e1 = get_engine("sqlite+aiosqlite://")
    e1_again = get_engine("sqlite+aiosqlite://")
    e2 = get_engine("sqlite+aiosqlite:///./data/f26-a.db")

    assert e1 is e1_again
    assert e1 is not e2


async def test_session_factories_are_cached_per_database_url():
    f1 = get_session_factory("sqlite+aiosqlite://")
    f1_again = get_session_factory("sqlite+aiosqlite://")
    f2 = get_session_factory("sqlite+aiosqlite:///./data/f26-b.db")

    assert f1 is f1_again
    assert f1 is not f2
    assert f1.kw["bind"] is get_engine("sqlite+aiosqlite://")
    assert f2.kw["bind"] is not get_engine("sqlite+aiosqlite://")


async def test_dispose_engine_disposes_and_clears_cache():
    url = "sqlite+aiosqlite://"
    engine = get_engine(url)
    assert engine is not None

    await dispose_engine()

    # The cache is cleared: a fresh engine object is handed out next.
    assert get_engine(url) is not engine


async def test_init_db_fresh_database_creates_tables_and_version(tmp_path: Path):
    url = f"sqlite+aiosqlite:///{tmp_path}/fresh.db"

    await init_db(url)

    factory = get_session_factory(url)
    async with factory() as session:
        version = (
            await session.execute(text("SELECT version FROM schema_version WHERE id = 1"))
        ).scalar_one()
        assert version == EXPECTED_SCHEMA_VERSION
        # Dev bootstrap created the durable tables too.
        assert (await session.execute(text("SELECT count(*) FROM flow_runs"))).scalar() == 0


async def test_init_db_matching_version_skips_create_all(tmp_path: Path):
    """A gated DB at the expected version is accepted — and NOT touched."""
    url = f"sqlite+aiosqlite:///{tmp_path}/current.db"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE agent_runs (id INTEGER PRIMARY KEY)"))
        await conn.execute(
            text(
                "CREATE TABLE schema_version (id INTEGER PRIMARY KEY, "
                "version INTEGER NOT NULL, updated_at TIMESTAMP)"
            )
        )
        await conn.execute(
            text(f"INSERT INTO schema_version (id, version) VALUES (1, {EXPECTED_SCHEMA_VERSION})")
        )
    await engine.dispose()

    # No error; and the partial legacy table set is left alone (no create_all).
    await init_db(url)
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        tables = {
            row[0]
            for row in (
                await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            ).all()
        }
    await engine.dispose()
    assert "agent_runs" in tables
    assert "flow_runs" not in tables  # create_all was skipped


async def test_init_db_missing_schema_version_raises(tmp_path: Path):
    """A pre-007b forge database (no marker) refuses to bootstrap."""
    url = f"sqlite+aiosqlite:///{tmp_path}/legacy.db"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE flow_runs (id TEXT PRIMARY KEY)"))
    await engine.dispose()

    with pytest.raises(RuntimeError, match="upgrade\\.md"):
        await init_db(url)


async def test_init_db_wrong_schema_version_raises(tmp_path: Path):
    """Newer/older schema_version → startup failure with instructions."""
    url = f"sqlite+aiosqlite:///{tmp_path}/newer.db"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE flow_runs (id TEXT PRIMARY KEY)"))
        await conn.execute(
            text(
                "CREATE TABLE schema_version (id INTEGER PRIMARY KEY, "
                "version INTEGER NOT NULL, updated_at TIMESTAMP)"
            )
        )
        await conn.execute(
            text(
                f"INSERT INTO schema_version (id, version) VALUES (1, {EXPECTED_SCHEMA_VERSION + 1})"
            )
        )
    await engine.dispose()

    with pytest.raises(RuntimeError, match=str(EXPECTED_SCHEMA_VERSION + 1)):
        await init_db(url)


async def test_migration_007b_writes_schema_version(tmp_path: Path):
    """Migration 007b creates schema_version and inserts the expected row.

    007b is run in isolation via alembic's programmatic Operations API: the
    earlier chain (004+) drops CHECK constraints, which Postgres supports
    but SQLite does not — production migrations run on Postgres (see
    docs/operations/upgrade.md), dev bootstraps via init_db/create_all.
    """
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            spec = importlib.util.spec_from_file_location(
                "migration_007b",
                Path(__file__).resolve().parent.parent
                / "alembic"
                / "versions"
                / "007b_schema_version.py",
            )
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
            module.upgrade()

            version = conn.execute(
                text("SELECT version FROM schema_version WHERE id = 1")
            ).scalar_one()
    assert version == EXPECTED_SCHEMA_VERSION
    engine.dispose()

    # And a database carrying that marker passes the init_db gate.
    url = f"sqlite+aiosqlite:///{tmp_path}/migrated.db"
    sync_engine = create_engine(f"sqlite:///{tmp_path}/migrated.db")
    with sync_engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()  # creates schema_version + row on the file DB
        # a forge table so the gate recognises an existing database
        conn.execute(text("CREATE TABLE flow_runs (id TEXT PRIMARY KEY)"))
        conn.commit()
    sync_engine.dispose()

    await init_db(url)  # must NOT raise

    factory = get_session_factory(url)
    async with factory() as session:
        stored = (
            await session.execute(text("SELECT version FROM schema_version WHERE id = 1"))
        ).scalar_one()
    assert stored == EXPECTED_SCHEMA_VERSION
