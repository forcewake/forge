"""`python -m forge.migrate` — apply Alembic migrations to the configured
database (F25/F26: the image ships migrations; no source checkout needed).

Run inside the release image before starting app/worker:

    DATABASE_URL=postgresql+asyncpg://... python -m forge.migrate
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from alembic.config import Config


def build_alembic_config(database_url: str | None = None) -> Config:
    """Resolve the shipped chain before the image or source-checkout layout."""
    packaged = Path(__file__).resolve().parent / "_migrations"
    if packaged.is_dir():
        ini, scripts = packaged / "alembic.ini", packaged
    else:
        ini = Path("/app/alembic.ini")
        if not ini.exists():
            ini = Path(__file__).resolve().parent.parent.parent / "alembic.ini"
        scripts = ini.parent / "alembic"
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(scripts))
    if database_url:
        cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="forge-migrate", description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="target database URL (defaults to $DATABASE_URL)",
    )
    parser.add_argument(
        "--check", action="store_true", help="verify schema without applying migrations"
    )
    args = parser.parse_args(argv)
    if not args.database_url:
        print("forge-migrate: DATABASE_URL is not set", file=sys.stderr)
        return 2

    if args.check:
        try:
            asyncio.run(_check(args.database_url))
        except RuntimeError as error:
            print(f"forge-migrate: {error}", file=sys.stderr)
            return 1
        print("forge-migrate: schema is compatible")
        return 0

    # env.py reads DATABASE_URL (env vars survive the process spawn it may
    # run under); the Config pins the same URL for everything else.
    os.environ["DATABASE_URL"] = args.database_url
    from alembic import command

    cfg = build_alembic_config(args.database_url)
    command.upgrade(cfg, "head")
    print("forge-migrate: schema is up to date")
    return 0


async def _check(database_url: str) -> None:
    from forge.database import check_schema_compatible, dispose_engine

    try:
        await check_schema_compatible(database_url)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    sys.exit(main())
