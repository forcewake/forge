from __future__ import annotations

import logging

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import forge.durable.models  # noqa: F401  (registers the durable tables in metadata)
from forge.migrate import build_alembic_config
from forge.models.base import Base

logger = logging.getLogger(__name__)

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


async def init_db(database_url: str, *, alembic_cfg: Config | None = None) -> None:
    """Schema compatibility gate plus fresh-database bootstrap (F26, R22).

    The gate compares the database's live alembic revision(s) against the
    migration chain's head(s) and NEVER mutates an existing database
    (Django ``migrate --check`` posture: detect, don't upgrade):

    - database at head(s) → boot;
    - database behind head(s) → :class:`RuntimeError` with the fix command
      and the current vs required revisions;
    - multiple heads in the database (branched history) →
      :class:`RuntimeError`;
    - forge tables but no ``alembic_version`` (pre-alembic legacy database)
      → :class:`RuntimeError` with manual baseline instructions;
    - branched migration script in this build → :class:`RuntimeError`;
    - fresh (empty) database → the schema is created (see
      :func:`_bootstrap_fresh`).

    *alembic_cfg* overrides the migration script Config (tests, embedded
    use); by default it is built from the shipped ``alembic/`` directory.
    """
    script, head = _script_and_head(alembic_cfg)
    engine = get_engine(database_url)
    async with engine.connect() as conn:
        current_heads = await _current_heads(conn)
        forge_present = await _forge_tables_present(conn)

    if _gate_verdict(current_heads, forge_present, head):
        logger.debug("database at alembic head %s — gate OK", head)
        return
    await _bootstrap_fresh(engine, script, head)


async def check_schema_compatible(database_url: str, *, alembic_cfg: Config | None = None) -> None:
    """Zero-mutation form of the :func:`init_db` gate.

    Raises :class:`RuntimeError` unless the database is exactly at the
    release head(s); a fresh (un-bootstrapped) database is reported instead
    of being created. For deploy prechecks and `forge doctor`-style tools —
    bootstrapping stays :func:`init_db`'s job.
    """
    _, head = _script_and_head(alembic_cfg)
    engine = get_engine(database_url)
    async with engine.connect() as conn:
        current_heads = await _current_heads(conn)
        forge_present = await _forge_tables_present(conn)

    if _gate_verdict(current_heads, forge_present, head):
        return
    raise RuntimeError(
        "Database is not bootstrapped: no alembic_version and no forge "
        "tables. Run `python -m forge.migrate` (equivalent: "
        f"`alembic upgrade heads`) to reach {head} — see "
        "docs/operations/upgrade.md."
    )


def _gate_verdict(current_heads: tuple[str, ...], forge_present: bool, head: str) -> bool:
    """True when the database may boot; raise on every refusal case.

    Never mutates anything: the caller decides whether a False verdict
    (fresh, un-gated database) means bootstrap or "not bootstrapped".
    """
    if current_heads:
        if len(current_heads) > 1:
            raise RuntimeError(
                f"Database alembic_version has multiple heads "
                f"({', '.join(current_heads)}) — the migration history is "
                "branched. Merge the branches or stamp the intended head "
                f"(this release requires {head}), then start again — see "
                "docs/operations/upgrade.md."
            )
        if set(current_heads) == {head}:
            return True
        raise RuntimeError(
            "Database schema is out of date with this forge release: "
            f"database is at {', '.join(current_heads)}, release requires "
            f"{head}. Run the migrations before starting: "
            "`uv run alembic upgrade heads` (in the release image: "
            "`python -m forge.migrate`). Forge refuses to boot against a "
            "stale schema and never auto-upgrades an existing database — "
            "see docs/operations/upgrade.md."
        )
    if forge_present:
        raise RuntimeError(
            "Database has forge tables but no alembic_version table "
            "(pre-alembic forge database): no safe migration path is "
            "knowable. Verify the schema matches this release, then "
            f"baseline it with `alembic stamp {head}` — or restore a backup "
            "(docs/operations/backup-restore.md) and run "
            "`python -m forge.migrate`. See docs/operations/upgrade.md."
        )
    return False


async def _bootstrap_fresh(engine: AsyncEngine, script: ScriptDirectory, head: str) -> None:
    """Create the schema on an empty database and record the alembic head.

    The full 001→head migration chain runs programmatically on the caller's
    engine in one transaction — ``create_all`` is never a second schema
    factory (R22: two schema factories are how models and migrations drift).
    The one exception is SQLite (dev/test): migrations 004+ ALTER CHECK
    constraints, which only Postgres supports
    (docs/operations/upgrade.md), so SQLite keeps the alembic-cookbook
    fresh-database shortcut — create the models, then stamp the head —
    keeping the database revision-tracked and gated like any other.
    """
    async with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            logger.info(
                "fresh database: creating model schema and stamping head %s "
                "(SQLite cannot run the migration chain)",
                head,
            )

            def _create_and_stamp(sync_conn) -> None:
                Base.metadata.create_all(sync_conn)
                MigrationContext.configure(sync_conn).stamp(script, head)

            await conn.run_sync(_create_and_stamp)
            return

        logger.info("fresh database: applying alembic chain up to %s", head)
        await conn.run_sync(lambda sync_conn: _run_migrations_sync(sync_conn, script))


def _run_migrations_sync(sync_conn, script: ScriptDirectory) -> None:
    """Apply the whole revision chain base→head on *sync_conn*.

    The programmatic equivalent of ``alembic upgrade heads``, driven through
    the caller's connection (no env.py, no env-var handoff), with the head
    recorded in the same transaction.
    """
    ctx = MigrationContext.configure(sync_conn)
    revisions = list(script.walk_revisions())
    revisions.reverse()  # walk_revisions yields head-first
    for revision in revisions:
        logger.info("running upgrade %s -> %s", revision.down_revision, revision.revision)
        with Operations.context(ctx):
            revision.module.upgrade()
    ctx.stamp(script, "heads")


def _script_and_head(alembic_cfg: Config | None = None) -> tuple[ScriptDirectory, str]:
    """The migration script directory and its single head.

    A branched (or empty) script directory is a broken build and refuses
    here — never a silent ambiguity at boot time.
    """
    if alembic_cfg is None:
        alembic_cfg = build_alembic_config()
    script = ScriptDirectory.from_config(alembic_cfg)
    script_heads = set(script.get_heads())
    if len(script_heads) != 1:
        raise RuntimeError(
            "forge migration tree is broken: expected exactly one alembic "
            f"head, found {sorted(script_heads) or 'none'} — refusing to "
            "start. Merge the branch or add a merge revision before "
            "releasing."
        )
    return script, next(iter(script_heads))


async def _current_heads(conn: AsyncConnection) -> tuple[str, ...]:
    """The alembic revision(s) recorded in the database.

    Empty tuple when the ``alembic_version`` table is absent (fresh or
    pre-alembic legacy database) — the forge-table probe disambiguates.
    """

    def _heads(sync_conn) -> tuple[str, ...]:
        return MigrationContext.configure(sync_conn).get_current_heads()

    return await conn.run_sync(_heads)


async def _forge_tables_present(conn: AsyncConnection) -> bool:
    """True when any well-known forge table exists in the database."""

    def _names(sync_conn) -> set[str]:
        return set(inspect(sync_conn).get_table_names())

    tables = await conn.run_sync(_names)
    return bool(_FORGE_TABLES & tables)


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
