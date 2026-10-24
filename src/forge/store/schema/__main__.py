"""Explicit fresh installation, read-only checks and offline historical cutover."""

import argparse
import asyncio
import os
from pathlib import Path
import sys
from typing import Any

from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from forge.store.schema import check_schema, initialize, SchemaMismatch
from forge.store.schema.cutover import StoppedDeclaration, migrate, prepare
from forge.store.schema.legacy import CutoverRefused


def engine(url: str) -> AsyncEngine:
    result = create_async_engine(url, poolclass=NullPool)
    if result.dialect.name == "sqlite":

        @event.listens_for(result.sync_engine, "connect")
        def enable_foreign_keys(connection: Any, _: Any) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return result


async def execute(args: argparse.Namespace) -> None:
    urls = (
        [args.database_url]
        if args.command in ("init", "check")
        else [args.source_url, args.verification_url]
    )
    if args.command == "migrate":
        urls.append(args.database_url)
    if not all(urls):
        raise CutoverRefused("all explicitly selected database URLs are required")
    engines = [engine(url) for url in urls]
    try:
        if args.command == "init":
            await initialize(engines[0])
        elif args.command == "check":
            await check_schema(engines[0])
        else:
            declaration = StoppedDeclaration(args.old_service_stopped, args.executions_stopped)
            if args.command == "export-history":
                receipt = await prepare(engines[0], engines[1], args.output, declaration)
            else:
                receipt = await migrate(
                    engines[0], engines[2], engines[1], args.output, declaration
                )
            print("archive_sha256=" + receipt.manifest_sha256)
            print("backup_sha256=" + receipt.backup_sha256)
    finally:
        for item in engines:
            await item.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="forge-schema", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for kind in ("init", "check", "export-history", "migrate"):
        command = commands.add_parser(kind)
        if kind in ("init", "check", "migrate"):
            command.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
        if kind in ("export-history", "migrate"):
            command.add_argument(
                "--source-url", default=os.environ.get("FORGE_LEGACY_DATABASE_URL", "")
            )
            command.add_argument(
                "--verification-url", default=os.environ.get("FORGE_MIGRATION_VERIFY_URL", "")
            )
            command.add_argument("--output", required=True, type=Path)
            command.add_argument("--old-service-stopped", action="store_true")
            command.add_argument("--executions-stopped", action="store_true")
    args = parser.parse_args(argv)
    try:
        asyncio.run(execute(args))
    except (SchemaMismatch, CutoverRefused) as error:
        print("forge-schema: " + str(error), file=sys.stderr)
        return 1
    except (RuntimeError, SQLAlchemyError, OSError, ValueError):
        print(
            "forge-schema: operation refused; database state and private diagnostics require operator review",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
