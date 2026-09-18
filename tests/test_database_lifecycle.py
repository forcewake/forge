"""F26/R22 — DB engine lifecycle and the alembic revision gate.

Covers:

- the engine/session-factory caches being keyed by database URL,
- ``dispose_engine`` actually disposing cached engines on shutdown,
- ``init_db``'s gate comparing the database's live alembic revisions with
  the migration script's heads: at head → boot; behind → refuse with the
  fix command; branched history / pre-alembic legacy / branched script →
  refuse; fresh (empty) database → bootstrap and stamp the head,
- the gate comparing LIVE script heads: a newly added revision makes a
  database stamped at the previous head refuse (the R22 bug class),
- migration ``007b`` writing the ``schema_version`` row (cosmetic marker).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from forge.database import (
    check_schema_compatible,
    dispose_engine,
    get_engine,
    get_session_factory,
    init_db,
    reset_engine,
)
from forge.migrate import build_alembic_config


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


# --- the revision gate (R22) -------------------------------------------------


def _alembic_head() -> str:
    """The single head of the shipped migration chain."""
    heads = set(ScriptDirectory.from_config(build_alembic_config()).get_heads())
    assert len(heads) == 1, heads
    return next(iter(heads))


async def _seed_database(path: Path, *revisions: str, forge_tables: bool = True) -> str:
    """Create a database with forge tables and an alembic_version at *revisions*."""
    url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        if forge_tables:
            await conn.execute(text("CREATE TABLE agent_runs (id INTEGER PRIMARY KEY)"))
            await conn.execute(text("CREATE TABLE flow_runs (id TEXT PRIMARY KEY)"))
        if revisions:
            await conn.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            )
            for revision in revisions:
                await conn.execute(
                    text("INSERT INTO alembic_version (version_num) VALUES (:r)"),
                    {"r": revision},
                )
    await engine.dispose()
    return url


async def _stamped_revisions(url: str) -> set[str]:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        rows = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
    await engine.dispose()
    return set(rows)


async def test_init_db_fresh_database_bootstraps_and_stamps_head(tmp_path: Path):
    """An empty database is bootstrapped and lands revision-tracked at head."""
    url = f"sqlite+aiosqlite:///{tmp_path}/fresh.db"

    await init_db(url)

    assert await _stamped_revisions(url) == {_alembic_head()}
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        tables = {
            row[0]
            for row in (
                await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            ).all()
        }
    await engine.dispose()
    assert {"flow_runs", "run_budgets"} <= tables  # model surface, incl. durable

    # Second boot: the gate passes and nothing is re-created or re-stamped.
    await init_db(url)
    assert await _stamped_revisions(url) == {_alembic_head()}


async def test_init_db_at_head_passes_and_touches_nothing(tmp_path: Path):
    """A database stamped at head boots — and is NOT bootstrapped over."""
    head = _alembic_head()
    url = await _seed_database(tmp_path / "current.db", head)

    await init_db(url)  # must NOT raise

    engine = create_async_engine(url)
    async with engine.begin() as conn:
        tables = {
            row[0]
            for row in (
                await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            ).all()
        }
    await engine.dispose()
    assert "run_budgets" not in tables  # the gate never mutates a gated database


async def test_init_db_stale_revision_refuses_with_remediation(tmp_path: Path):
    """A database behind head refuses, naming both revisions and the fix."""
    head = _alembic_head()
    url = await _seed_database(tmp_path / "stale.db", "007")

    with pytest.raises(RuntimeError) as excinfo:
        await init_db(url)

    message = str(excinfo.value)
    assert "007" in message and head in message
    assert "uv run alembic upgrade heads" in message
    assert "python -m forge.migrate" in message


async def test_init_db_pre_alembic_legacy_database_refuses(tmp_path: Path):
    """Forge tables without alembic_version → refuse with stamp instructions."""
    head = _alembic_head()
    url = await _seed_database(tmp_path / "legacy.db")  # forge tables, no version

    with pytest.raises(RuntimeError) as excinfo:
        await init_db(url)

    message = str(excinfo.value)
    assert "alembic_version" in message
    assert f"alembic stamp {head}" in message


async def test_init_db_multi_head_database_refuses(tmp_path: Path):
    """A branched alembic_version refuses, naming every head."""
    head = _alembic_head()
    url = await _seed_database(tmp_path / "branched.db", "010", head)

    with pytest.raises(RuntimeError, match="multiple heads"):
        await init_db(url)


async def test_init_db_branched_script_refuses(tmp_path: Path):
    """A build whose migration tree has two heads is broken — refuse."""
    script_dir = tmp_path / "branched_script"
    (script_dir / "versions").mkdir(parents=True)
    _write_revision(script_dir / "versions", "001_left", "000_base")
    _write_revision(script_dir / "versions", "001_right", "000_base")
    _write_revision(script_dir / "versions", "000_base", None)
    cfg = Config()
    cfg.set_main_option("script_location", str(script_dir))
    url = f"sqlite+aiosqlite:///{tmp_path}/whatever.db"

    with pytest.raises(RuntimeError, match="alembic head"):
        await init_db(url, alembic_cfg=cfg)


async def test_gate_tracks_live_script_heads(tmp_path: Path):
    """Adding a revision makes a database at the old head refuse (R22 class).

    The gate compares the database against the script heads it is shipped
    with — never a frozen constant — so shipping a new migration without
    migrating existing databases is caught at startup, not at runtime.
    """
    head = _alembic_head()
    url = await _seed_database(tmp_path / "at_old_head.db", head)

    # The shipped chain plus one freshly added revision on top.
    script_dir = tmp_path / "extended_script"
    (script_dir / "versions").mkdir(parents=True)
    shutil.copytree(
        Path(__file__).resolve().parent.parent / "alembic" / "versions",
        script_dir / "versions",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    _write_revision(script_dir / "versions", "999_next", head)
    cfg = Config()
    cfg.set_main_option("script_location", str(script_dir))

    with pytest.raises(RuntimeError) as excinfo:
        await init_db(url, alembic_cfg=cfg)

    assert "999_next" in str(excinfo.value)


async def test_check_schema_compatible_is_zero_mutation(tmp_path: Path):
    """The read-only gate refuses an un-bootstrapped DB instead of creating it."""
    fresh = f"sqlite+aiosqlite:///{tmp_path}/empty.db"

    with pytest.raises(RuntimeError, match="not bootstrapped"):
        await check_schema_compatible(fresh)

    engine = create_async_engine(fresh)
    async with engine.begin() as conn:
        tables = {
            row[0]
            for row in (
                await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            ).all()
        }
    await engine.dispose()
    assert not tables  # nothing was created

    url = await _seed_database(tmp_path / "current.db", _alembic_head())
    await check_schema_compatible(url)  # at head → OK


def _write_revision(directory: Path, revision: str, down_revision: str | None) -> None:
    """Write a minimal alembic revision module into a versions *directory*."""
    down = "None" if down_revision is None else f'"{down_revision}"'
    (directory / f"{revision}.py").write_text(
        '"""Test revision."""\n'
        f'revision = "{revision}"\n'
        f"down_revision = {down}\n"
        "\n"
        "\n"
        "def upgrade() -> None:\n"
        "    pass\n"
        "\n"
        "\n"
        "def downgrade() -> None:\n"
        "    pass\n"
    )


# --- full-chain bootstrap (real Postgres only) --------------------------------


@pytest.mark.skipif(
    not os.environ.get("FORGE_PG_TEST_URL"),
    reason=(
        "the full alembic chain needs real Postgres (migrations 004+ ALTER "
        "CHECK constraints — Postgres DDL); point FORGE_PG_TEST_URL at a "
        "disposable database (its tables are dropped)"
    ),
)
async def test_init_db_fresh_postgres_runs_full_chain():
    """Fresh Postgres: the whole 001→head chain applies, then the gate passes."""
    url = os.environ["FORGE_PG_TEST_URL"]
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        names = (
            (
                await conn.execute(
                    text("select tablename from pg_tables where schemaname = 'public'")
                )
            )
            .scalars()
            .all()
        )
        for name in names:
            await conn.execute(text(f'drop table if exists "{name}" cascade'))
    await engine.dispose()

    await init_db(url)

    assert await _pg_stamped_revisions(url) == {_alembic_head()}
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        tables = set(
            (
                await conn.execute(
                    text("select tablename from pg_tables where schemaname = 'public'")
                )
            )
            .scalars()
            .all()
        )
    await engine.dispose()
    assert {"flow_runs", "step_runs", "run_budgets"} <= tables  # durable surface
    await init_db(url)  # second boot: gate passes, no error


async def _pg_stamped_revisions(url: str) -> set[str]:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        rows = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
    await engine.dispose()
    return set(rows)


# --- migration 007b (cosmetic marker) -----------------------------------------


async def test_migration_007b_writes_schema_version(tmp_path: Path):
    """Migration 007b creates schema_version and inserts the expected row.

    007b is run in isolation via alembic's programmatic Operations API: the
    earlier chain (004+) drops CHECK constraints, which Postgres supports
    but SQLite does not — production migrations run on Postgres (see
    docs/operations/upgrade.md). The marker is cosmetic since R22 (the
    revision gate is authoritative).
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
    assert version == 1
    engine.dispose()
