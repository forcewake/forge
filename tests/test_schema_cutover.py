"""Actual native backup and restore precede every fresh-schema cutover."""

import hashlib
import json
import stat

import pytest
from sqlalchemy import inspect, text

from forge.store.schema import check_schema
from forge.store.schema.archive import snapshot
from forge.store.schema.cutover import StoppedDeclaration, migrate, prepare
from forge.store.schema.legacy import CutoverRefused
from tests.schema_fixture import legacy
from tests.schema_fixture import databases as databases

STOPPED = StoppedDeclaration(True, True)


async def facts(database):
    async with database.begin() as connection:
        return await snapshot(connection)


async def empty(database):
    async with database.connect() as connection:
        return not await connection.run_sync(lambda sync: inspect(sync).get_table_names())


async def test_cutover_preserves_every_historical_value_in_a_verified_private_backup_and_fresh_target(
    databases,
):
    source, destination, verification = (
        databases.source,
        databases.destination,
        databases.verification,
    )
    await legacy(source)
    before = await facts(source)
    directory = databases.directory / "archive"
    receipt = await migrate(source, destination, verification, directory, STOPPED)
    await check_schema(destination)
    assert await facts(source) == before == await facts(verification)
    manifest = json.loads((directory / "manifest.json").read_bytes())
    assert manifest["snapshot"] == before
    assert (
        hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()
        == receipt.manifest_sha256
    )
    backup = directory / manifest["backup"]["file"]
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == receipt.backup_sha256
    for path in directory.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o400
    async with destination.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM runs")) == 0
        linked = (
            await connection.execute(
                text("SELECT archive_sha256,backup_sha256 FROM schema_installation")
            )
        ).one()
        assert tuple(linked) == (receipt.manifest_sha256, receipt.backup_sha256)


@pytest.mark.parametrize(
    "declaration", (StoppedDeclaration(False, True), StoppedDeclaration(True, False))
)
async def test_offline_declaration_is_required_and_never_inferred_from_empty_activity(
    databases, declaration
):
    await legacy(databases.source)
    with pytest.raises(CutoverRefused, match="declare"):
        await prepare(
            databases.source, databases.verification, databases.directory / "archive", declaration
        )
    assert await empty(databases.destination) and await empty(databases.verification)


async def test_active_legacy_run_or_unknown_source_revision_refuses_without_reinterpreting_it(
    databases,
):
    await legacy(databases.source)
    async with databases.source.begin() as connection:
        await connection.execute(text("UPDATE flow_runs SET status='waiting_ci'"))
    with pytest.raises(CutoverRefused, match="unresolved"):
        await migrate(
            databases.source,
            databases.destination,
            databases.verification,
            databases.directory / "active",
            STOPPED,
        )
    assert await empty(databases.destination)
    async with databases.source.begin() as connection:
        await connection.execute(text("UPDATE flow_runs SET status='ready_for_human'"))
        await connection.execute(text("UPDATE alembic_version SET version_num='999'"))
    with pytest.raises(CutoverRefused, match="revision"):
        await migrate(
            databases.source,
            databases.destination,
            databases.verification,
            databases.directory / "unknown",
            STOPPED,
        )
    assert await empty(databases.destination)


async def test_same_database_or_nonempty_target_is_never_overwritten(databases):
    await legacy(databases.source)
    with pytest.raises(CutoverRefused, match="differ"):
        await migrate(
            databases.source,
            databases.source,
            databases.verification,
            databases.directory / "same",
            STOPPED,
        )
    async with databases.destination.begin() as connection:
        await connection.exec_driver_sql("CREATE TABLE owner_data (id INTEGER PRIMARY KEY)")
        await connection.exec_driver_sql("INSERT INTO owner_data VALUES (7)")
    with pytest.raises(CutoverRefused, match="empty"):
        await migrate(
            databases.source,
            databases.destination,
            databases.verification,
            databases.directory / "occupied",
            STOPPED,
        )
    async with databases.destination.connect() as connection:
        assert await connection.scalar(text("SELECT id FROM owner_data")) == 7


async def test_backup_failure_and_corrupt_export_have_no_accepted_manifest_or_new_schema(
    databases, monkeypatch
):
    from forge.store.schema import cutover

    await legacy(databases.source)

    async def fail(*args):
        raise CutoverRefused("injected native backup failure")

    monkeypatch.setattr(cutover, "create_and_restore", fail)
    directory = databases.directory / "failed"
    with pytest.raises(CutoverRefused, match="native backup failure"):
        await migrate(
            databases.source, databases.destination, databases.verification, directory, STOPPED
        )
    assert await empty(databases.destination) and not (directory / "manifest.json").exists()


async def test_source_drift_after_verified_export_refuses_before_initializing_target(
    databases, monkeypatch
):
    from forge.store.schema import cutover

    await legacy(databases.source)
    original = cutover.prepare

    async def changed(*args):
        receipt = await original(*args)
        async with databases.source.begin() as connection:
            await connection.execute(
                text("UPDATE historical_payload SET body='owner changed history'")
            )
        return receipt

    monkeypatch.setattr(cutover, "prepare", changed)
    with pytest.raises(CutoverRefused, match="history changed"):
        await migrate(
            databases.source,
            databases.destination,
            databases.verification,
            databases.directory / "drift",
            STOPPED,
        )
    assert await empty(databases.destination)


async def test_export_readback_must_match_the_native_snapshot_before_sealing(
    databases, monkeypatch
):
    from forge.store.schema import cutover

    await legacy(databases.source)
    original = cutover.snapshot

    async def corrupt(connection, directory=None):
        result = await original(connection, directory)
        if directory is not None:
            name = result["tables"]["historical_payload"]["file"]
            with (directory / name).open("ab") as stream:
                stream.write(b"corrupt")
        return result

    monkeypatch.setattr(cutover, "snapshot", corrupt)
    directory = databases.directory / "corrupt"
    with pytest.raises(CutoverRefused, match="readback"):
        await migrate(
            databases.source, databases.destination, databases.verification, directory, STOPPED
        )
    assert await empty(databases.destination) and not (directory / "manifest.json").exists()
