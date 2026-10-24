"""Native backups are accepted only after restoring a separate empty database."""

import asyncio
from contextlib import closing
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import sqlite3
import stat

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine

from forge.store.schema.archive import MAX_FILE, new_file, file_digest
from forge.store.schema.legacy import CutoverRefused
from forge.store.schema.signature import is_empty


async def identity(engine: AsyncEngine) -> tuple[str, ...]:
    if engine.dialect.name == "sqlite":
        if not engine.url.database or engine.url.database == ":memory:":
            raise CutoverRefused("offline migration requires a persistent database")
        return ("sqlite", str(Path(engine.url.database).resolve()))
    if engine.dialect.name != "postgresql" or not engine.url.host:
        raise CutoverRefused("native backups require an explicit SQLite file or PostgreSQL host")
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT current_database(), inet_server_addr()::text, inet_server_port()")
            )
        ).one()
    return ("postgresql", *(str(value) for value in row))


async def assert_empty(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        empty = await connection.run_sync(is_empty)
    if not empty:
        raise CutoverRefused("verification and destination databases must be empty")


def _postgres(url: URL) -> tuple[list[str], dict[str, str]]:
    if not url.host or not url.database or not url.username:
        raise CutoverRefused("PostgreSQL backup requires an explicit host, database and user")
    options = url.query
    if set(options) - {"ssl", "sslmode"}:
        raise CutoverRefused("unsupported native backup connection options")
    ssl = options.get("sslmode", options.get("ssl", "prefer"))
    if not isinstance(ssl, str) or ssl not in (
        "disable",
        "allow",
        "prefer",
        "require",
        "verify-ca",
        "verify-full",
    ):
        raise CutoverRefused("unsupported native backup TLS mode")
    args = [
        "--host=" + url.host,
        "--port=" + str(url.port or 5432),
        "--username=" + url.username,
        "--dbname=" + url.database,
    ]
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PGCONNECT_TIMEOUT": "15",
        "PGSSLMODE": ssl,
        "PGPASSWORD": url.password or "",
    }
    return args, environment


async def _run(program: str, args: list[str], environment: dict[str, str], output: Path) -> None:
    executable = shutil.which(program)
    if executable is None:
        raise CutoverRefused("native PostgreSQL backup tools are unavailable")
    process = await asyncio.create_subprocess_exec(
        executable,
        *args,
        env=environment,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(300):
            while process.returncode is None:
                if output.exists() and output.stat().st_size > MAX_FILE:
                    raise CutoverRefused("native backup exceeds the supported bound")
                try:
                    await asyncio.wait_for(process.wait(), 0.25)
                except TimeoutError:
                    continue
            if process.returncode:
                raise CutoverRefused("native PostgreSQL backup or restore failed")
    except TimeoutError:
        raise CutoverRefused("native PostgreSQL backup or restore timed out") from None
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


def _sqlite_copy(source: Path, target: Path) -> None:
    if source.is_symlink() or target.is_symlink():
        raise CutoverRefused("SQLite backup files cannot be symlinks")
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(target)) as copy:
            original.backup(copy)
            if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise CutoverRefused("native SQLite backup integrity check failed")
    target.chmod(0o600)
    with target.open("rb") as stream:
        os.fsync(stream.fileno())


@dataclass(frozen=True)
class VerifiedBackup:
    path: Path
    sha256: str


async def create_and_restore(
    source: AsyncEngine, verification: AsyncEngine, directory: Path, snapshot_id: str | None
) -> VerifiedBackup:
    if source.dialect.name != verification.dialect.name:
        raise CutoverRefused("backup verification must use the same database dialect")
    await assert_empty(verification)
    await verification.dispose()
    backup = directory / (
        "database.sqlite3" if source.dialect.name == "sqlite" else "database.pgdump"
    )
    with new_file(backup):
        pass
    if source.dialect.name == "sqlite":
        target = Path(verification.url.database or "")
        if stat.S_IMODE(target.parent.stat().st_mode) & 0o077:
            raise CutoverRefused("SQLite verification directory must be private")
        await asyncio.to_thread(_sqlite_copy, Path(source.url.database or ""), backup)
        original_digest = file_digest(backup)[0]
        await asyncio.to_thread(_sqlite_copy, backup, target)
    else:
        if snapshot_id is None:
            raise CutoverRefused("PostgreSQL backup requires the exported read-only snapshot")
        args, environment = _postgres(source.url)
        await _run(
            "pg_dump",
            [*args, "--format=custom", "--snapshot=" + snapshot_id, "--file=" + str(backup)],
            environment,
            backup,
        )
        original_digest = file_digest(backup)[0]
        args, environment = _postgres(verification.url)
        await _run(
            "pg_restore",
            [*args, "--exit-on-error", "--no-owner", "--no-privileges", str(backup)],
            environment,
            backup,
        )
    backup.chmod(0o600)
    if file_digest(backup)[0] != original_digest:
        raise CutoverRefused("native backup bytes changed during restore verification")
    return VerifiedBackup(backup, original_digest)
