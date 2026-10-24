"""Explicit offline backup, restore verification, history export and fresh cutover."""

from dataclasses import dataclass
import hashlib
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from forge.store.schema.archive import (
    ArchiveReceipt,
    canonical,
    private_directory,
    seal,
    snapshot,
    file_digest,
)
from forge.store.schema.backup import assert_empty, create_and_restore, identity
from forge.store.schema.gate import initialize
from forge.store.schema.legacy import CutoverRefused, assert_quiescent


@dataclass(frozen=True)
class StoppedDeclaration:
    old_service_stopped: bool
    native_executions_stopped: bool

    def require(self) -> None:
        if self.old_service_stopped is not True or self.native_executions_stopped is not True:
            raise CutoverRefused(
                "the operator must declare the old service and native executions stopped"
            )


async def prepare(
    source: AsyncEngine, verification: AsyncEngine, directory: Path, declaration: StoppedDeclaration
) -> ArchiveReceipt:
    declaration.require()
    if await identity(source) == await identity(verification):
        raise CutoverRefused("backup verification cannot overwrite the source database")
    await assert_empty(verification)
    private_directory(directory)
    async with source.connect() as connection:
        if source.dialect.name == "postgresql":
            connection = await connection.execution_options(isolation_level="REPEATABLE READ")
        async with connection.begin():
            if source.dialect.name == "postgresql":
                await connection.execute(text("SET TRANSACTION READ ONLY"))
            else:
                await connection.exec_driver_sql("BEGIN")
            await assert_quiescent(connection)
            facts = await snapshot(connection, directory)
            snapshot_id = (
                await connection.scalar(text("SELECT pg_export_snapshot()"))
                if source.dialect.name == "postgresql"
                else None
            )
            backup = await create_and_restore(source, verification, directory, snapshot_id)
            async with verification.connect() as restored:
                async with restored.begin():
                    observed = await snapshot(restored)
            if facts != observed:
                raise CutoverRefused(
                    "restored native backup differs from historical source evidence"
                )
    async with source.connect() as current:
        async with current.begin():
            await assert_quiescent(current)
            if await snapshot(current) != facts:
                raise CutoverRefused("the stopped source changed during backup verification")
    if file_digest(backup.path)[0] != backup.sha256:
        raise CutoverRefused("verified backup bytes changed before sealing")
    return seal(
        directory,
        facts,
        backup.path,
        {"old_service_stopped": True, "native_executions_stopped": True, "restore_verified": True},
    )


async def migrate(
    source: AsyncEngine,
    destination: AsyncEngine,
    verification: AsyncEngine,
    directory: Path,
    declaration: StoppedDeclaration,
) -> ArchiveReceipt:
    identities = [await identity(engine) for engine in (source, destination, verification)]
    if len(set(identities)) != 3:
        raise CutoverRefused(
            "source, destination and disposable verification databases must differ"
        )
    await assert_empty(destination)
    receipt = await prepare(source, verification, directory, declaration)
    async with source.connect() as connection:
        async with connection.begin():
            await assert_quiescent(connection)
            current_digest = hashlib.sha256(canonical(await snapshot(connection))).hexdigest()
            if current_digest != receipt.source_snapshot_sha256:
                raise CutoverRefused(
                    "source history changed after backup; destination remains empty"
                )
    await initialize(
        destination, archive_sha256=receipt.manifest_sha256, backup_sha256=receipt.backup_sha256
    )
    return receipt
