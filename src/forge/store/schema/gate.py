"""Startup only checks; installing an empty database is an explicit operation."""

from alembic.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine
import re
from pathlib import Path

from forge.store.schema import baseline
from forge.store.schema.signature import inspect_schema, is_empty
from forge.store.schema.migration import script


class SchemaMismatch(RuntimeError):
    pass


def _scope(connection: Connection) -> None:
    if connection.dialect.name not in ("sqlite", "postgresql"):
        raise SchemaMismatch("unsupported database dialect")
    if (
        connection.dialect.name == "postgresql"
        and connection.scalar(text("SELECT current_schema()")) != "public"
    ):
        raise SchemaMismatch("the engine requires the explicit public schema")


def check_connection(connection: Connection) -> None:
    _scope(connection)
    if script().get_heads() != [baseline.REVISION]:
        raise SchemaMismatch("the installed migration package must contain exactly one baseline")
    if connection.dialect.name == "sqlite" and connection.scalar(text("PRAGMA foreign_keys")) != 1:
        raise SchemaMismatch("SQLite foreign-key enforcement must be enabled before startup")
    current = MigrationContext.configure(connection).get_current_heads()
    if current != (baseline.REVISION,):
        raise SchemaMismatch(
            "schema version differs; run the explicit offline migration before startup"
        )
    actual = inspect_schema(connection)
    expected = baseline.document()["dialects"][connection.dialect.name]["signature"]
    if actual != expected:
        raise SchemaMismatch("physical schema differs from the installed baseline; startup refused")
    records = connection.execute(
        text(
            "SELECT baseline_sha256, installation_id, archive_sha256, backup_sha256 FROM schema_installation WHERE id=1"
        )
    ).all()
    if (
        len(records) != 1
        or records[0][0] != baseline.digest()
        or re.fullmatch(r"[0-9a-f]{32}", records[0][1]) is None
    ):
        raise SchemaMismatch("installation evidence differs from the baseline")
    if (records[0][2] is None) != (records[0][3] is None) or any(
        value is not None and re.fullmatch(r"[0-9a-f]{64}", value) is None
        for value in records[0][2:]
    ):
        raise SchemaMismatch("installation archive evidence is incomplete")


async def check_schema(engine: AsyncEngine) -> None:
    filename = engine.url.database
    if (
        engine.dialect.name == "sqlite"
        and filename is not None
        and filename != ":memory:"
        and not Path(filename).exists()
    ):
        raise SchemaMismatch("database is not initialized; run the explicit fresh installation")
    async with engine.connect() as connection:
        await connection.run_sync(check_connection)


def initialize_connection(
    connection: Connection, *, archive_sha256: str | None = None, backup_sha256: str | None = None
) -> None:
    _scope(connection)
    if connection.dialect.name == "postgresql":
        connection.execute(text("SELECT pg_advisory_xact_lock(7093700219310781)"))
    if not is_empty(connection):
        raise SchemaMismatch("initialization requires a completely empty database")
    baseline.apply(connection, archive_sha256=archive_sha256, backup_sha256=backup_sha256)
    MigrationContext.configure(connection).stamp(script(), baseline.REVISION)
    check_connection(connection)


async def initialize(
    engine: AsyncEngine, *, archive_sha256: str | None = None, backup_sha256: str | None = None
) -> None:
    async with engine.begin() as connection:
        if engine.dialect.name == "sqlite":
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
        await connection.run_sync(
            lambda sync: initialize_connection(
                sync, archive_sha256=archive_sha256, backup_sha256=backup_sha256
            )
        )
