"""Historical migration grading retires; repository identity and outage rules stay.

The operator migration CLI is no longer shipped. Tests create known native
repository states directly, then exercise actual composition/read/pin paths.
They do not claim that a migration or rollback procedure ran.
"""

import pytest

from forge.adaptive.checkpoint_repository import (
    CheckpointRepositoryMisconfigured,
    CheckpointRepositoryUnavailable,
    FilesystemCheckpointRepository,
    resolve_repository,
)
from forge.api_checkpoint_channel import CheckpointCorruptError
from tests.test_checkpoint_repository import _checkpoint, _sqlite_factory


async def test_exact_checkpoint_identity_survives_independent_repository_instances(tmp_path):
    first = FilesystemCheckpointRepository(tmp_path)
    manifest, blobs, identity = _checkpoint("work", {"file.txt": b"exact bytes"}, 1)
    await first.put("work", identity, manifest, blobs)
    second = FilesystemCheckpointRepository(tmp_path)
    entry = await second.entry("work", identity)
    assert entry["checkpoint_id"] == identity
    assert await second.read_entry(entry) == (manifest, blobs)
    assert await second.entry("different-work", identity) is None


async def test_pinned_older_checkpoint_survives_retention_without_becoming_active(tmp_path):
    repository = FilesystemCheckpointRepository(tmp_path)
    for sequence in (1, 2, 3):
        manifest, blobs, identity = _checkpoint("work", {"file": str(sequence).encode()}, sequence)
        await repository.put("work", identity, manifest, blobs)
        if sequence == 1:
            pinned = identity
            assert await repository.pin("work", pinned, "approved-resume")
    await repository.apply_retention("work", keep_last=1)
    assert await repository.entry("work", pinned) is not None
    assert (await repository.entry("work"))["checkpoint_id"] == identity
    assert (await repository.pins("work"))[0]["reason"] == "approved-resume"


async def test_missing_and_corrupt_checkpoint_are_distinct(tmp_path):
    repository = FilesystemCheckpointRepository(tmp_path)
    assert await repository.entry("unknown") is None
    manifest, blobs, identity = _checkpoint("work", {"file": b"hello"}, 1)
    await repository.put("work", identity, manifest, blobs)
    entry = await repository.entry("work")
    # Corrupt the exact content-addressed blob through the store's own paths.
    digest = next(iter(blobs))
    matches = [p for p in tmp_path.rglob(digest) if p.is_file()]
    assert matches
    matches[0].write_bytes(b"tampered")
    with pytest.raises(CheckpointCorruptError):
        await repository.read_entry(entry)


def test_postgres_authority_never_silently_degrades_to_filesystem(tmp_path):
    with pytest.raises(CheckpointRepositoryMisconfigured):
        resolve_repository(env={"FORGE_CHECKPOINT_DURABILITY": "postgres"}, root=tmp_path)
    with pytest.raises(CheckpointRepositoryMisconfigured):
        resolve_repository(env={"FORGE_CHECKPOINT_DURABILITY": "typo"}, root=tmp_path)


async def test_postgres_metadata_outage_is_unavailable_even_with_valid_filesystem_copy(tmp_path):
    manifest, blobs, identity = _checkpoint("work", {"file": b"safe"}, 1)
    await FilesystemCheckpointRepository(tmp_path).put("work", identity, manifest, blobs)
    engine, factory = await _sqlite_factory()
    try:
        repository = resolve_repository(
            env={"FORGE_CHECKPOINT_DURABILITY": "postgres"}, root=tmp_path, session_factory=factory
        )
        from sqlalchemy import text

        async with engine.begin() as connection:
            await connection.execute(text("DROP TABLE checkpoint_metadata"))
        with pytest.raises(CheckpointRepositoryUnavailable):
            await repository.entry("work")
    finally:
        await engine.dispose()
