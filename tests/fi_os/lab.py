"""The disposable Postgres lab behind the OS-process FI suite.

Schema comes from the REAL migration chain (R22: ``python -m forge.migrate``
is run once per session — ``create_all`` is never a second schema factory);
per test the lab truncates every table and hands the control plane its own
asyncpg pool, separate from every worker subprocess's pool.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[2]

MIGRATE_TIMEOUT_SECONDS = 120.0


def run_migrations(database_url: str) -> None:
    """Apply the shipped alembic chain via ``python -m forge.migrate`` (R22)."""
    result = subprocess.run(
        [sys.executable, "-m", "forge.migrate", "--database-url", database_url],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=MIGRATE_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "python -m forge.migrate failed "
            f"(rc={result.returncode}):\n{result.stdout}\n{result.stderr}"
        )


async def truncate_all(engine: AsyncEngine) -> None:
    """Fresh durable state per test — tables kept, rows and sequences reset.

    ``alembic_version`` is deliberately kept: the R22 gate must keep seeing a
    database at the migration head, exactly like production.
    """
    async with engine.begin() as conn:
        names = (
            (
                await conn.execute(
                    text(
                        "select tablename from pg_tables "
                        "where schemaname = 'public' and tablename <> 'alembic_version'"
                    )
                )
            )
            .scalars()
            .all()
        )
        if names:
            listing = ", ".join(f'"{name}"' for name in names)
            await conn.execute(text(f"truncate table {listing} restart identity cascade"))


def new_session_factory(database_url: str) -> async_sessionmaker:
    """A control-plane pool, separate from every worker's pool."""
    engine = create_async_engine(database_url)
    return async_sessionmaker(engine, expire_on_commit=False)
