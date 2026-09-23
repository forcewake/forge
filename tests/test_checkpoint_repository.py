"""Q35-03: one configured checkpoint repository across upload, resume and operations.

The integration defect this suite pins out of existence: the HTTP
upload route honored ``FORGE_CHECKPOINT_DURABILITY`` (postgres mode
writes the index to ``checkpoint_metadata``) while the resume producer
built the SYNCHRONOUS filesystem JSON-index reader with no contract —
upload and resume read DIFFERENT authorities, so a checkpoint the API
confirmed could be invisible to a fresh ``/resume``.

The contract here:

- :func:`forge.adaptive.checkpoint_repository.resolve_repository` is
  the ONE composition point — mode selection from the env, storage
  root, session factory — and REFUSES half-configurations at
  construction (unknown mode, ``postgres`` without a session factory);
- the resume producer reads through the injected repository
  (``OperatorControlService.checkpoint_repository``) and never
  constructs a bare ``CheckpointStore`` that ignores the contract;
- the typed read states stay disjoint: absent (``None``), corrupt
  (:class:`forge.api_checkpoint_channel.CheckpointCorruptError`),
  unavailable (:class:`CheckpointRepositoryUnavailable` — a database
  outage is NEVER answered as absence or by consulting the filesystem);
- cross-instance agreement: two separately constructed repositories
  over the same storage see the same active entry — proven against real
  PostgreSQL when ``FORGE_PG_TEST_URL`` is set (the FI convention; the
  URL's database is shared, its ``checkpoint_metadata`` rows are
  deleted per test).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge import api_checkpoint_channel as api_channel
from forge.adaptive.checkpoint_channel import work_scoped_token
from forge.adaptive.checkpoint_repository import (
    AUTHORITY_FILESYSTEM,
    AUTHORITY_POSTGRES,
    CheckpointRepository,
    CheckpointRepositoryMisconfigured,
    CheckpointRepositoryUnavailable,
    FilesystemCheckpointRepository,
    PostgresCheckpointRepository,
    resolve_repository,
)
from forge.adaptive.wiring import OperatorControlService, control_service_from_env
from forge.models.base import Base

WORK_ID = "wp-q35-03"
SECRET = "test-lane-secret"  # noqa: S105 — fake value for tests


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _checkpoint(
    work_id: str, files: dict[str, bytes], sequence: int, *, source_oid: str = "e" * 40
) -> tuple[bytes, dict[str, bytes], str]:
    """A minimal ``forge.wip.manifest/2`` payload the store verifies."""
    manifest = json.dumps(
        {
            "schema": "forge.wip.manifest/2",
            "work_id": work_id,
            "sequence": sequence,
            "source_oids": {"attempt_base": source_oid},
            "files": {
                name: {"digest": _digest(data), "mode": 0o644, "role": "new"}
                for name, data in sorted(files.items())
            },
            "deletions": [],
        }
    ).encode()
    blobs = {entry["digest"]: files[name] for name, entry in json.loads(manifest)["files"].items()}
    return manifest, blobs, _digest(manifest)


async def _sqlite_factory() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    """An async session factory over a fresh in-memory DB with every table."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


class _FakeRepository:
    """A recording stand-in satisfying the protocol's resume-facing half."""

    def __init__(
        self,
        entry: dict[str, Any] | None = None,
        manifest: bytes = b"",
        blobs: dict[str, bytes] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.entry_dict = entry
        self.manifest = manifest
        self.blobs = blobs or {}
        self.error = error
        self.entry_calls: list[str] = []
        self.read_calls: list[str] = []
        self.pin_calls: list[tuple[str, str, str]] = []

    async def entry(self, work_id: str, checkpoint_id: str | None = None) -> dict | None:
        self.entry_calls.append(work_id)
        if self.error is not None:
            raise self.error
        if checkpoint_id is not None:
            return None  # the resume path asks for the ACTIVE entry only
        return self.entry_dict

    async def read_entry(self, entry: dict[str, Any]) -> tuple[bytes, dict[str, bytes]]:
        self.read_calls.append(str(entry.get("checkpoint_id")))
        return self.manifest, dict(self.blobs)

    async def read(self, work_id: str) -> tuple[bytes, list[bytes]] | None:
        entry = await self.entry(work_id)
        if entry is None:
            return None
        manifest, blobs = await self.read_entry(entry)
        return manifest, [blobs[digest] for digest in sorted(blobs)]

    async def put(
        self,
        work_id: str,
        checkpoint_id: str,
        manifest_bytes: bytes,
        blobs: dict[str, bytes],
    ) -> None:
        raise AssertionError("the resume path never writes through the fake")

    async def pin(self, work_id: str, checkpoint_id: str, reason: str = "") -> bool:
        # Q35-05: the protocol grew the pin API; the fake records it so
        # the resume-path proofs can assert the authorization pinned.
        self.pin_calls.append((work_id, checkpoint_id, reason))
        return True

    async def unpin(self, work_id: str, checkpoint_id: str, reason: str | None = None) -> int:
        return 0

    async def pins(self, work_id: str | None = None) -> list[dict[str, Any]]:
        return []

    async def authority(self) -> str:
        return AUTHORITY_POSTGRES


# ---------------------------------------------------------------------------
# resolve_repository — the ONE composition point
# ---------------------------------------------------------------------------


class TestResolveRepository:
    async def test_the_default_env_selects_the_filesystem_authority(self, tmp_path):
        repository = resolve_repository(env={}, root=tmp_path / "store")

        assert isinstance(repository, FilesystemCheckpointRepository)
        assert await repository.authority() == AUTHORITY_FILESYSTEM

    async def test_the_storage_root_comes_from_the_env_mapping(self, tmp_path):
        repository = resolve_repository(
            env={api_channel.CHECKPOINT_STORE_DIR_ENV: str(tmp_path / "from-env")},
        )

        manifest, blobs, checkpoint_id = _checkpoint(WORK_ID, {"a.txt": b"one\n"}, 4)
        await repository.put(WORK_ID, checkpoint_id, manifest, blobs)

        assert (tmp_path / "from-env" / "works" / f"{WORK_ID}.json").is_file()

    async def test_postgres_is_selected_with_a_session_factory(self, tmp_path):
        _engine, factory = await _sqlite_factory()

        repository = resolve_repository(
            env={api_channel.DURABILITY_ENV: "postgres"},
            session_factory=factory,
            root=tmp_path / "store",
        )

        assert isinstance(repository, PostgresCheckpointRepository)
        assert await repository.authority() == AUTHORITY_POSTGRES
        assert repository.session_factory is factory

    def test_an_unknown_mode_refuses_at_construction(self, tmp_path):
        with pytest.raises(CheckpointRepositoryMisconfigured) as raised:
            resolve_repository(env={api_channel.DURABILITY_ENV: "wal"}, root=tmp_path)

        assert api_channel.DURABILITY_ENV in str(raised.value)

    def test_postgres_without_a_session_factory_refuses_never_fs_fallback(self, tmp_path):
        with pytest.raises(CheckpointRepositoryMisconfigured, match="session factory"):
            resolve_repository(env={api_channel.DURABILITY_ENV: "postgres"}, root=tmp_path)

    async def test_both_implementations_satisfy_the_protocol(self, tmp_path):
        _engine, factory = await _sqlite_factory()
        postgres = resolve_repository(
            env={api_channel.DURABILITY_ENV: "postgres"},
            session_factory=factory,
            root=tmp_path / "pg",
        )

        assert isinstance(FilesystemCheckpointRepository(tmp_path / "fs"), CheckpointRepository)
        assert isinstance(postgres, CheckpointRepository)
        assert isinstance(_FakeRepository(), CheckpointRepository)

    def test_the_env_mapping_wins_over_the_process_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv(api_channel.DURABILITY_ENV, "postgres")

        repository = resolve_repository(env={}, root=tmp_path / "store")

        assert isinstance(repository, FilesystemCheckpointRepository)


# ---------------------------------------------------------------------------
# The filesystem authority — round trips over tmp storage
# ---------------------------------------------------------------------------


class TestFilesystemRepositoryRoundTrip:
    async def test_put_entry_read_round_trip(self, tmp_path):
        repository = FilesystemCheckpointRepository(tmp_path / "store")
        manifest, blobs, checkpoint_id = _checkpoint(WORK_ID, {"a.txt": b"one\n"}, 6)

        await repository.put(WORK_ID, checkpoint_id, manifest, blobs)

        entry = await repository.entry(WORK_ID)
        assert entry is not None
        assert entry["checkpoint_id"] == checkpoint_id
        assert entry["sequence"] == 6  # the MANIFEST's own sequence is authority
        served = await repository.read(WORK_ID)
        assert served is not None
        served_manifest, served_blobs = served
        assert served_manifest == manifest
        assert served_blobs == [blobs[_digest(b"one\n")]]  # digest-ordered

    async def test_an_unknown_work_is_absent_not_an_error(self, tmp_path):
        repository = FilesystemCheckpointRepository(tmp_path / "store")

        assert await repository.entry("wp-never") is None
        assert await repository.read("wp-never") is None

    async def test_a_mismatched_declared_address_is_refused_before_any_write(self, tmp_path):
        repository = FilesystemCheckpointRepository(tmp_path / "store")
        manifest, blobs, _checkpoint_id = _checkpoint(WORK_ID, {"a.txt": b"one\n"}, 1)

        with pytest.raises(ValueError, match="addresses to"):
            await repository.put(WORK_ID, "f" * 64, manifest, blobs)

        assert await repository.entry(WORK_ID) is None  # nothing landed

    async def test_rotted_bytes_are_typed_corrupt_never_served(self, tmp_path):
        repository = FilesystemCheckpointRepository(tmp_path / "store")
        manifest, blobs, checkpoint_id = _checkpoint(WORK_ID, {"a.txt": b"one\n"}, 2)
        await repository.put(WORK_ID, checkpoint_id, manifest, blobs)

        blob_path = tmp_path / "store" / _digest(b"one\n")[:2] / _digest(b"one\n")
        blob_path.write_bytes(b"rotted\n")  # the address no longer backs these bytes

        from forge.api_checkpoint_channel import CheckpointCorruptError

        with pytest.raises(CheckpointCorruptError):
            await repository.read(WORK_ID)

    async def test_the_active_entry_is_the_highest_sequence_not_the_last_arrival(self, tmp_path):
        repository = FilesystemCheckpointRepository(tmp_path / "store")
        late = _checkpoint(WORK_ID, {"a.txt": b"late\n"}, 9)
        early = _checkpoint(WORK_ID, {"a.txt": b"early\n"}, 2)

        await repository.put(WORK_ID, late[2], late[0], late[1])
        await repository.put(WORK_ID, early[2], early[0], early[1])  # arrives LAST

        entry = await repository.entry(WORK_ID)
        assert entry is not None
        assert entry["checkpoint_id"] == late[2]  # the higher sequence stays active


class TestCrossInstanceAgreement:
    async def test_two_separate_filesystem_repositories_see_the_same_active_entry(self, tmp_path):
        """The fresh-process simulation over the filesystem authority."""
        manifest, blobs, checkpoint_id = _checkpoint(WORK_ID, {"a.txt": b"one\n"}, 3)
        writer = FilesystemCheckpointRepository(tmp_path / "shared-store")
        await writer.put(WORK_ID, checkpoint_id, manifest, blobs)

        reader = FilesystemCheckpointRepository(tmp_path / "shared-store")  # a NEW instance

        entry = await reader.entry(WORK_ID)
        assert entry is not None
        assert entry["checkpoint_id"] == checkpoint_id
        served = await reader.read(WORK_ID)
        assert served == (manifest, [blobs[digest] for digest in sorted(blobs)])


class TestPostgresRepositoryOverSqlite:
    """The wrapper contract over the store's async ops (fast profile; the
    real-Postgres cross-instance proof is the PG-gated class below)."""

    async def test_the_index_lives_in_the_table_and_no_json_mirror_is_written(self, tmp_path):
        engine, factory = await _sqlite_factory()
        repository = PostgresCheckpointRepository(tmp_path / "store", factory)
        manifest, blobs, checkpoint_id = _checkpoint(WORK_ID, {"a.txt": b"one\n"}, 5)

        await repository.put(WORK_ID, checkpoint_id, manifest, blobs)

        from sqlalchemy import select

        from forge.api_checkpoint_channel import CheckpointMetadataRow

        async with factory() as session:
            rows = list((await session.execute(select(CheckpointMetadataRow))).scalars().all())
        assert [row.checkpoint_id for row in rows] == [checkpoint_id]
        # Acceptance: NO works/<id>.json mirror exists in the postgres case —
        # a mirror would be a second authority whose pointer can drift.
        assert not (tmp_path / "store" / "works").is_dir()
        await engine.dispose()

    async def test_a_database_error_is_typed_unavailable_not_absent(self, tmp_path):
        engine, factory = await _sqlite_factory()
        repository = PostgresCheckpointRepository(tmp_path / "store", factory)
        # Drop the metadata table out from under the authority: the very
        # shape a schema loss / migration accident produces.
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE checkpoint_metadata"))

        with pytest.raises(CheckpointRepositoryUnavailable):
            await repository.entry(WORK_ID)
        with pytest.raises(CheckpointRepositoryUnavailable):
            await repository.read(WORK_ID)
        await engine.dispose()


# ---------------------------------------------------------------------------
# The resume producer — repository-driven, never a bare store
# ---------------------------------------------------------------------------


class TestResumeUsesTheRepository:
    async def test_the_injected_repository_is_called_and_its_entry_returned(
        self, tmp_path, monkeypatch
    ):
        manifest, blobs, checkpoint_id = _checkpoint(WORK_ID, {"a.txt": b"v\n"}, 7)

        # Q35-03: the resume path must not build a bare CheckpointStore that
        # ignores the durability contract — the old defect.
        def _no_bare_store(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("resume must read through the repository, not a bare store")

        monkeypatch.setattr(api_channel, "CheckpointStore", _no_bare_store)
        fake = _FakeRepository(
            entry={"checkpoint_id": checkpoint_id, "sequence": 7, "files": 1},
            manifest=manifest,
            blobs=blobs,
        )
        service = OperatorControlService(checkpoint_repository=fake)

        assert await service.resume(WORK_ID, "human:op", "note:1") is True

        assert fake.entry_calls == [WORK_ID]  # the repository WAS the authority
        assert fake.read_calls == [checkpoint_id]
        (resume,) = [c for c in service.mailbox.commands.values() if c.kind == "resume"]
        assert resume.payload["checkpoint_ref"] == f"{WORK_ID}@{checkpoint_id}"
        assert resume.payload["checkpoint_sequence"] == 7
        assert resume.payload["source_oid"] == "e" * 40

    async def test_an_unavailable_authority_raises_never_answers_no_checkpoint(self):
        fake = _FakeRepository(error=CheckpointRepositoryUnavailable("db is down"))
        service = OperatorControlService(checkpoint_repository=fake)
        await service.pause(WORK_ID, "human:op", "note:1")

        with pytest.raises(CheckpointRepositoryUnavailable, match="db is down"):
            await service.resume(WORK_ID, "human:op", "note:2")
        resumes = [c for c in service.mailbox.commands.values() if c.kind == "resume"]
        assert resumes == []  # no resume command was fabricated

    async def test_without_a_repository_a_misconfigured_authority_refuses(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv(api_channel.DURABILITY_ENV, "postgres")
        monkeypatch.setenv(api_channel.CHECKPOINT_STORE_DIR_ENV, str(tmp_path / "store"))
        service = OperatorControlService()  # constructed bare: no injected authority

        with pytest.raises(CheckpointRepositoryMisconfigured, match="session factory"):
            await service.resume(WORK_ID, "human:op", "note:1")

    async def test_without_a_repository_the_configured_filesystem_authority_stands(
        self, tmp_path, monkeypatch
    ):
        """Back-compat: a bare service resolves the CONFIGURED authority
        through the same composition point — the durability contract is
        honored, never bypassed (and never a guessed postgres fallback)."""
        store_dir = tmp_path / "store"
        monkeypatch.setenv(api_channel.CHECKPOINT_STORE_DIR_ENV, str(store_dir))
        manifest, blobs, checkpoint_id = _checkpoint(WORK_ID, {"a.txt": b"v\n"}, 4)
        store = api_channel.CheckpointStore(store_dir)
        store.put_checkpoint(work_id=WORK_ID, manifest_bytes=manifest, blobs=blobs, sequence=4)

        service = OperatorControlService()

        assert await service.resume(WORK_ID, "human:op", "note:1") is True
        (resume,) = [c for c in service.mailbox.commands.values() if c.kind == "resume"]
        assert resume.payload["checkpoint_ref"] == f"{WORK_ID}@{checkpoint_id}"


class TestControlServiceComposition:
    async def test_the_composition_root_injects_the_resolved_repository(self, tmp_path):
        service = control_service_from_env(
            env={api_channel.CHECKPOINT_STORE_DIR_ENV: str(tmp_path / "store")}
        )

        assert isinstance(service.checkpoint_repository, FilesystemCheckpointRepository)
        assert await service.checkpoint_repository.authority() == AUTHORITY_FILESYSTEM

    async def test_a_misconfigured_durability_refuses_the_composition(self, tmp_path):
        with pytest.raises(CheckpointRepositoryMisconfigured, match="session factory"):
            control_service_from_env(
                env={
                    api_channel.DURABILITY_ENV: "postgres",
                    api_channel.CHECKPOINT_STORE_DIR_ENV: str(tmp_path / "store"),
                }
            )

    async def test_the_composition_derives_the_postgres_factory_from_the_env(self, tmp_path):
        db_path = tmp_path / "derived.db"
        service = control_service_from_env(
            env={
                api_channel.DURABILITY_ENV: "postgres",
                "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
                api_channel.CHECKPOINT_STORE_DIR_ENV: str(tmp_path / "store"),
            }
        )

        assert isinstance(service.checkpoint_repository, PostgresCheckpointRepository)
        assert await service.checkpoint_repository.authority() == AUTHORITY_POSTGRES


# ---------------------------------------------------------------------------
# The HTTP channel delegates to the repository
# ---------------------------------------------------------------------------


class TestHttpRoutesDelegateToTheRepository:
    def _client(self, monkeypatch, repository: Any) -> TestClient:
        monkeypatch.setenv(api_channel.LANE_CONTROL_SECRET_ENV, SECRET)

        def _fake_resolve(*args: Any, **kwargs: Any) -> Any:
            return repository

        monkeypatch.setattr(api_channel, "resolve_repository", _fake_resolve)
        application = FastAPI()
        application.include_router(api_channel.checkpoint_channel_router)
        return TestClient(application)

    def _bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {work_scoped_token(SECRET, WORK_ID)}"}

    def test_a_database_outage_answers_503_never_404_or_a_fs_fallback(self, monkeypatch):
        client = self._client(
            monkeypatch, _FakeRepository(error=CheckpointRepositoryUnavailable("db is down"))
        )

        response = client.get(f"/lane/checkpoints/{WORK_ID}", headers=self._bearer())

        assert response.status_code == 503
        assert "db is down" in response.json()["detail"]

    def test_the_served_checkpoint_comes_from_the_repository(self, monkeypatch):
        manifest, blobs, checkpoint_id = _checkpoint(WORK_ID, {"a.txt": b"served\n"}, 8)
        fake = _FakeRepository(
            entry={"checkpoint_id": checkpoint_id, "sequence": 8, "files": 1},
            manifest=manifest,
            blobs=blobs,
        )
        client = self._client(monkeypatch, fake)

        response = client.get(f"/lane/checkpoints/{WORK_ID}", headers=self._bearer())

        assert response.status_code == 200
        body = response.json()
        assert body["checkpoint_id"] == checkpoint_id
        assert base64.b64decode(body["manifest"]) == manifest
        assert base64.b64decode(body["blobs"][_digest(b"served\n")]) == b"served\n"
        assert fake.entry_calls  # the route read through the repository


# ---------------------------------------------------------------------------
# PG-gated: real PostgreSQL, separate instances, the FI convention
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("FORGE_PG_TEST_URL"),
    reason=(
        "FORGE_PG_TEST_URL not set — the Q35-03 cross-instance authority proof "
        "runs only against a disposable real Postgres (rows are deleted per test)"
    ),
)
class TestPostgresAuthorityOverRealPostgres:
    """AT-04's core: repository A (process 1) PUTs; repository B — a FRESH
    instance with its OWN engine and session factory over the same database
    and shared blob root — sees exactly that checkpoint on entry/read."""

    async def _lab(self, tmp_path: Path) -> tuple[Any, Any]:
        url = os.environ["FORGE_PG_TEST_URL"]
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(delete(api_channel.CheckpointMetadataRow))
        return engine, async_sessionmaker(engine, expire_on_commit=False)

    async def test_put_on_one_instance_read_on_a_fresh_one(self, tmp_path):
        engine_a, factory_a = await self._lab(tmp_path)
        engine_b, factory_b = await self._lab(tmp_path)
        shared_root = tmp_path / "shared-blobs"
        manifest, blobs, checkpoint_id = _checkpoint(WORK_ID, {"a.txt": b"pg-one\n"}, 11)
        try:
            writer = PostgresCheckpointRepository(shared_root, factory_a)
            await writer.put(WORK_ID, checkpoint_id, manifest, blobs)

            # The FRESH instance: own session factory, same DB, same root.
            reader = PostgresCheckpointRepository(shared_root, factory_b)
            entry = await reader.entry(WORK_ID)
            assert entry is not None
            assert entry["checkpoint_id"] == checkpoint_id
            assert entry["sequence"] == 11
            served = await reader.read(WORK_ID)
            assert served == (manifest, [blobs[digest] for digest in sorted(blobs)])
        finally:
            await engine_a.dispose()
            await engine_b.dispose()

    async def test_a_stale_filesystem_index_cannot_override_the_db_authority(self, tmp_path):
        engine_a, factory_a = await self._lab(tmp_path)
        engine_b, factory_b = await self._lab(tmp_path)
        shared_root = tmp_path / "shared-blobs"
        manifest, blobs, checkpoint_id = _checkpoint(WORK_ID, {"a.txt": b"pg-two\n"}, 3)
        try:
            writer = PostgresCheckpointRepository(shared_root, factory_a)
            await writer.put(WORK_ID, checkpoint_id, manifest, blobs)

            # Seed a DISAGREEING filesystem JSON index naming another active
            # checkpoint — the stale authority the old resume path would
            # have trusted (the Q35-03 defect, replayed on purpose).
            stale_index = shared_root / "works" / f"{WORK_ID}.json"
            stale_index.parent.mkdir(parents=True, exist_ok=True)
            stale_bytes = json.dumps(
                {
                    "work_id": WORK_ID,
                    "checkpoints": [{"checkpoint_id": "a" * 64, "sequence": 99, "files": 0}],
                }
            ).encode()
            stale_index.write_bytes(stale_bytes)

            reader = PostgresCheckpointRepository(shared_root, factory_b)
            entry = await reader.entry(WORK_ID)
            assert entry is not None
            assert entry["checkpoint_id"] == checkpoint_id  # the DB row won
            assert entry["sequence"] == 3
            # And the stale index was not copied to, fixed, or mirrored:
            assert stale_index.read_bytes() == stale_bytes
        finally:
            await engine_a.dispose()
            await engine_b.dispose()

    async def test_no_json_mirror_is_written_in_the_postgres_case(self, tmp_path):
        engine_a, factory_a = await self._lab(tmp_path)
        shared_root = tmp_path / "shared-blobs"
        manifest, blobs, checkpoint_id = _checkpoint(WORK_ID, {"a.txt": b"pg-three\n"}, 1)
        try:
            writer = PostgresCheckpointRepository(shared_root, factory_a)
            await writer.put(WORK_ID, checkpoint_id, manifest, blobs)

            works_dir = shared_root / "works"
            assert not (works_dir.is_dir() and list(works_dir.glob("*.json")))
            # while the blobs stay content-addressed filesystem bytes
            assert (shared_root / _digest(b"pg-three\n")[:2] / _digest(b"pg-three\n")).is_file()
        finally:
            await engine_a.dispose()

    async def test_an_unreachable_database_is_typed_unavailable_not_absent(self, tmp_path):
        from sqlalchemy.engine import make_url

        url = make_url(os.environ["FORGE_PG_TEST_URL"]).set(database="forge_no_such_db_q3503")
        engine = create_async_engine(str(url))
        try:
            repository = PostgresCheckpointRepository(
                tmp_path / "store", async_sessionmaker(engine, expire_on_commit=False)
            )

            with pytest.raises(CheckpointRepositoryUnavailable):
                await repository.entry(WORK_ID)
            with pytest.raises(CheckpointRepositoryUnavailable):
                await repository.read(WORK_ID)
        finally:
            await engine.dispose()
