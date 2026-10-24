"""Fresh baseline execution is explicit and never part of application startup."""

import asyncio

from alembic import context
from alembic.migration import MigrationContext
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from forge.store.schema.signature import is_empty


def migrate(connection: Connection) -> None:
    if connection.dialect.name == "postgresql":
        connection.exec_driver_sql("SELECT pg_advisory_xact_lock(7093700219310781)")
    if not MigrationContext.configure(connection).get_current_heads() and not is_empty(connection):
        raise RuntimeError("baseline installation requires a completely empty database")
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run() -> None:
    url = context.config.get_main_option("sqlalchemy.url")
    if url is None:
        raise RuntimeError("an explicit database URL is required")
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            if engine.dialect.name == "sqlite":
                await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                await connection.exec_driver_sql("BEGIN IMMEDIATE")
            await connection.run_sync(migrate)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("baseline installation requires a live empty database")
asyncio.run(run())
