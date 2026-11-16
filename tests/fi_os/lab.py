"""Disposable database lab for the OS-process worker suite.

Schema comes from ``python -m forge.store.schema init``. ``create_all`` is
not a second schema factory.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_TIMEOUT_SECONDS = 120.0


def install_schema(database_url: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    result = subprocess.run(
        [sys.executable, "-m", "forge.store.schema", "init"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=INIT_TIMEOUT_SECONDS,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "python -m forge.store.schema init failed "
            f"(rc={result.returncode}):\n{result.stdout}\n{result.stderr}"
        )


async def truncate_all(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        names = [
            row[0]
            for row in (
                await connection.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
                    )
                )
            ).all()
        ]
        if engine.dialect.name == "postgresql":
            names = [
                row[0]
                for row in (
                    await connection.execute(
                        text(
                            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                            "AND tablename != 'alembic_version'"
                        )
                    )
                ).all()
            ]
        for name in names:
            await connection.execute(text(f'DELETE FROM "{name}"'))
