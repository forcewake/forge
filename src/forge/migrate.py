"""`python -m forge.migrate` — apply Alembic migrations to the configured
database (F25/F26: the image ships migrations; no source checkout needed).

Run inside the release image before starting app/worker:

    DATABASE_URL=postgresql+asyncpg://... python -m forge.migrate
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="forge-migrate", description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="target database URL (defaults to $DATABASE_URL)",
    )
    args = parser.parse_args(argv)
    if not args.database_url:
        print("forge-migrate: DATABASE_URL is not set", file=sys.stderr)
        return 2

    # alembic.ini lives at the image WORKDIR; env.py reads DATABASE_URL.
    os.environ["DATABASE_URL"] = args.database_url
    from alembic import command
    from alembic.config import Config

    ini = Path("/app/alembic.ini")
    if not ini.exists():
        ini = Path(__file__).resolve().parent.parent.parent / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(ini.parent / "alembic"))
    cfg.set_main_option("sqlalchemy.url", args.database_url)
    command.upgrade(cfg, "head")
    print("forge-migrate: schema is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
