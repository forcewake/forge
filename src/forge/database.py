from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import insert, inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from forge.models.base import Base
from forge.models.schema_version import SchemaVersion

logger = logging.getLogger(__name__)

#: Version of the relational schema this code expects (F26). The FINAL
#: alembic migration writes/updates the single ``schema_version`` row; a
#: database whose marker is missing or different is NOT upgraded in place —
#: startup fails with upgrade instructions instead (see
#: docs/operations/upgrade.md).
EXPECTED_SCHEMA_VERSION = 1

#: Tables whose presence marks an existing forge database. ``agent_runs`` is
#: the oldest core table (migration 001); ``flow_runs`` the durable one.
_FORGE_TABLES = frozenset({"agent_runs", "flow_runs"})

_engine_cache: dict[str, AsyncEngine] = {}
_session_factory_cache: dict[str, async_sessionmaker[AsyncSession]] = {}


def get_engine(database_url: str) -> AsyncEngine:
    """Create (or return the cached) async engine for *database_url*.

    The cache is keyed by URL: two Settings instances with different
    DATABASE_URLs never share an engine (F26).
    """
    engine = _engine_cache.get(database_url)
    if engine is None:
        connect_args = {}
        if database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        engine = create_async_engine(database_url, connect_args=connect_args)
        _engine_cache[database_url] = engine
    return engine


def get_session_factory(database_url: str) -> async_sessionmaker[AsyncSession]:
    """Return a session factory bound to the given database URL."""
    factory = _session_factory_cache.get(database_url)
    if factory is None:
        factory = async_sessionmaker(get_engine(database_url), expire_on_commit=False)
        _session_factory_cache[database_url] = factory
    return factory


async def init_db(database_url: str) -> None:
    """Dev/first-run bootstrap plus the schema compatibility gate (F26).

    - Fresh (empty) database: ``Base.metadata.create_all`` and write the
      ``schema_version`` marker — the unchanged dev path.
    - Existing forge database: the ``schema_version`` marker must be present
      and equal :data:`EXPECTED_SCHEMA_VERSION`. Otherwise raise
      :class:`RuntimeError` with upgrade instructions — ``create_all`` is a
      bootstrap, never a schema upgrade; run ``alembic upgrade head`` first
      (docs/operations/upgrade.md).
    """
    engine = get_engine(database_url)
    async with engine.begin() as conn:
        if await _forge_tables_present(conn):
            stored = await _read_schema_version(conn)
            if stored != EXPECTED_SCHEMA_VERSION:
                raise RuntimeError(
                    "Database schema is not compatible with this forge version: "
                    f"expected schema_version={EXPECTED_SCHEMA_VERSION}, found {stored!r}. "
                    "Run the migrations before starting (alembic upgrade head, or "
                    "`python -m forge.migrate`) — see docs/operations/upgrade.md. "
                    "Forge refuses to create_all over an existing database."
                )
            logger.debug("schema_version=%d OK — skipping create_all", stored)
            return
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            insert(SchemaVersion.__table__).values(
                id=1, version=EXPECTED_SCHEMA_VERSION, updated_at=datetime.now(timezone.utc)
            )
        )


async def _forge_tables_present(conn: AsyncConnection) -> bool:
    """True when any well-known forge table exists in the database."""

    def _names(sync_conn) -> set[str]:
        return set(inspect(sync_conn).get_table_names())

    tables = await conn.run_sync(_names)
    return bool(_FORGE_TABLES & tables)


async def _read_schema_version(conn: AsyncConnection) -> int | None:
    """Return the stored schema version, or None when missing/unreadable."""
    try:
        row = (await conn.execute(text("SELECT version FROM schema_version WHERE id = 1"))).first()
    except Exception:
        # Table absent (pre-007b database) — treat as no marker.
        return None
    if row is None or row[0] is None:
        return None
    return int(row[0])


async def dispose_engine() -> None:
    """Dispose every cached engine, then clear the caches (F26 shutdown path).

    Await this in process shutdown (app lifespan, worker finally) so pooled
    connections are closed cleanly.
    """
    engines = list(_engine_cache.values())
    _engine_cache.clear()
    _session_factory_cache.clear()
    for engine in engines:
        await engine.dispose()


def reset_engine() -> None:
    """Reset the cached engines/session factories WITHOUT disposing.

    Deprecated: sync escape hatch kept for tests that tear down event loops.
    Production shutdown must use :func:`dispose_engine`, which awaits
    ``engine.dispose()`` before clearing the cache.
    """
    _engine_cache.clear()
    _session_factory_cache.clear()
