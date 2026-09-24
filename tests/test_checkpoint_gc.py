"""Q35-05 (review ``c7ae8db``): GC never deletes bytes a live or approved
checkpoint reference still needs.

The defect (probe P03): retention computed a deletion digest set from the
CURRENT references, committed the metadata deletion, and only then unlinked
the CAS blobs. A work B that added a NEW reference to a shared blob between
the scan and the unlink kept an index entry whose bytes were gone — and the
newest-checkpoint selection never PINNED the older exact checkpoint a
pending approved ResumeSpec had bound to.

The contract this suite pins:

- **mark / recheck / sweep** — retention MARKS candidate digests (past the
  horizon, unpinned), RECHECKS live reachability in the very transaction
  (filesystem: the very lock section) performing the deletion, and SWEEPS
  only survivors; a reference that landed since the mark wins the race and
  its bytes stay readable;
- **pins** — an authorized resume pins its exact checkpoint
  (:meth:`CheckpointRepository.pin`); the pinned row AND its blobs survive
  newer checkpoints and retention until the pin is released EXPLICITLY
  (:meth:`CheckpointRepository.unpin`) — never by time alone;
- **first-upload serialization** — a work's first insert cannot rely on
  ``SELECT FOR UPDATE`` over an empty set: concurrent first uploads
  serialize (PostgreSQL advisory anchor + ``INSERT ... ON CONFLICT DO
  NOTHING``), so the per-work quota is judged over a consistent index;
- **quotas count REFERENCED bytes** — a deduplicated blob another work
  already stored still counts toward THIS work when the work newly
  references it;
- **crash windows** — an exception between recheck and sweep deletes
  nothing owned by committed references, and an interrupted GC converges
  on the next pass without double deletion.

R36-04 (probe P04) extends the contract past what a recheck can do: the
Q35-05 SELECT only sees references committed BEFORE it ran, so a
DIFFERENT work's landing could commit a reference to a shared digest
between the collector's FINAL scan and its unlink. The suite pins the
volume-wide reference/delete lock that closes it:

- **P04 schedule** — B's landing begins strictly after A's final scan
  (driven through the TEST-ONLY ``store.gc_after_final_scan`` barrier,
  which fires between the scan and the unlink); B's committed reference
  still reads verified bytes, for explicit retention, on-upload
  cleanup and pending-GC recovery, on both authorities and against
  real PostgreSQL with two engines; B-before-scan stays the control;
- **the lock** — held from the final scan through the last unlink,
  bounded (``GCLockTimeout``: a sweep aborts, deletes nothing and
  retries later), and deadlock-free under two concurrent sweeps plus a
  concurrent writer (one global lock order);
- **the rollout fence** — ``FORGE_CHECKPOINT_SWEEP=off`` marks but
  never unlinks; turning sweeping back on collects the marked set
  against current reachability.

The PostgreSQL proofs that need real isolation are gated on
``FORGE_PG_TEST_URL`` (the ADR-0017 failure-injection convention); the
SQLite approximations run always.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from forge import api_checkpoint_channel as api_channel
from forge.adaptive.checkpoint_repository import (
    FilesystemCheckpointRepository,
    PostgresCheckpointRepository,
)
from forge.models.base import Base

WORK_A = "wp-q35-05a"
WORK_B = "wp-q35-05b"
SHARED = b"the shared blob both works reference\n"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _checkpoint(
    work_id: str, files: dict[str, bytes], sequence: int
) -> tuple[bytes, dict[str, bytes], str]:
    """A minimal ``forge.wip.manifest/2`` payload the store verifies."""
    manifest = json.dumps(
        {
            "schema": "forge.wip.manifest/2",
            "work_id": work_id,
            "sequence": sequence,
            "source_oids": {"attempt_base": "e" * 40},
            "files": {
                name: {"digest": _digest(data), "mode": 0o644, "role": "new"}
                for name, data in sorted(files.items())
            },
            "deletions": [],
        }
    ).encode()
    document = json.loads(manifest)
    blobs = {entry["digest"]: files[name] for name, entry in document["files"].items()}
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


async def _sqlite_file_factory(
    tmp_path: Path,
) -> tuple[Any, async_sessionmaker[AsyncSession]]:
    """A file-backed SQLite factory with REAL separate connections.

    The in-memory ``StaticPool`` approximation hands every session the
    SAME connection, so a session that closes without committing (the
    retention MARK phase, deliberately outside the volume lock)
    implicitly rolls back whatever uncommitted work shares that
    connection — an artifact no deployment has. The concurrency proofs
    below need genuinely independent connections to interleave the way
    two processes would.
    """
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'gc-concurrency.db'}",
        poolclass=NullPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _pg_store(
    root: Path,
    factory: async_sessionmaker[AsyncSession],
    policy: api_channel.StoragePolicy | None = None,
) -> api_channel.CheckpointStore:
    return api_channel.CheckpointStore(
        root,
        policy=policy,
        durability=api_channel.DurabilityContract(mode="postgres", session_factory=factory),
    )


def _fs_repo(tmp_path: Path, policy: api_channel.StoragePolicy | None = None):
    return FilesystemCheckpointRepository(tmp_path / "cas", policy=policy)


def _pg_repo(
    tmp_path: Path,
    factory: async_sessionmaker[AsyncSession],
    policy: api_channel.StoragePolicy | None = None,
) -> PostgresCheckpointRepository:
    return PostgresCheckpointRepository(tmp_path / "cas", factory, policy=policy)


async def _put(repo: Any, work_id: str, files: dict[str, bytes], sequence: int) -> str:
    manifest, blobs, checkpoint_id = _checkpoint(work_id, files, sequence)
    await repo.put_checkpoint(
        work_id=work_id, manifest_bytes=manifest, blobs=blobs, sequence=sequence
    )
    return checkpoint_id


def _seed_two_checkpoints_with_shared_old_fs(store: Any) -> str:
    """Work A (filesystem store): seq 1 references the SHARED blob, seq 2
    supersedes it. Returns the shared blob's digest."""
    old_manifest, old_blobs, _old_id = _checkpoint(WORK_A, {"shared.txt": SHARED}, 1)
    new_manifest, new_blobs, _new_id = _checkpoint(WORK_A, {"v2.txt": b"work a v2\n"}, 2)
    store.put_checkpoint(work_id=WORK_A, manifest_bytes=old_manifest, blobs=old_blobs, sequence=1)
    store.put_checkpoint(work_id=WORK_A, manifest_bytes=new_manifest, blobs=new_blobs, sequence=2)
    return _digest(SHARED)


async def _seed_two_checkpoints_with_shared_old_pg(store: Any) -> str:
    """Work A (postgres store): the same two-checkpoint history."""
    old_manifest, old_blobs, _old_id = _checkpoint(WORK_A, {"shared.txt": SHARED}, 1)
    new_manifest, new_blobs, _new_id = _checkpoint(WORK_A, {"v2.txt": b"work a v2\n"}, 2)
    await store.aput_checkpoint(
        work_id=WORK_A, manifest_bytes=old_manifest, blobs=old_blobs, sequence=1
    )
    await store.aput_checkpoint(
        work_id=WORK_A, manifest_bytes=new_manifest, blobs=new_blobs, sequence=2
    )
    return _digest(SHARED)


# ---------------------------------------------------------------------------
# 1. THE P03 SCHEDULE — a B reference landing between A's retention MARK and
#    the physical SWEEP keeps the shared blob readable.
# ---------------------------------------------------------------------------


class TestP03ScheduleFilesystem:
    def test_b_reference_between_mark_and_sweep_keeps_the_shared_blob(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = api_channel.CheckpointStore(tmp_path / "cas")
        shared_digest = _seed_two_checkpoints_with_shared_old_fs(store)

        # The schedule driver: B's REAL upload (full landing — blobs and
        # index) runs inside work A's mark-time reachability computation,
        # i.e. strictly between the mark and the unlink phase.
        b_manifest, b_blobs, b_id = _checkpoint(WORK_B, {"shared.txt": SHARED}, 1)
        original = store._referenced_by_retained_works

        def mark_then_b_lands(exclude_ids: set[str]) -> set[str]:
            referenced = original(exclude_ids)
            if exclude_ids:  # the mark call carries the doomed checkpoint ids
                store.put_checkpoint(
                    work_id=WORK_B, manifest_bytes=b_manifest, blobs=b_blobs, sequence=1
                )
            return referenced

        monkeypatch.setattr(store, "_referenced_by_retained_works", mark_then_b_lands)

        removed = store.apply_retention(WORK_A, keep_last=1)

        assert removed == 1  # the old checkpoint's ROW did go
        entry = store.entry(WORK_B)
        assert entry is not None and entry["checkpoint_id"] == b_id
        # THE assertion: B's committed reference still resolves its bytes.
        served_manifest, served_blobs = store.read_checkpoint(entry)
        assert served_manifest == b_manifest
        assert served_blobs == {_digest(SHARED): SHARED}
        assert (tmp_path / "cas" / shared_digest[:2] / shared_digest).is_file()
        # And A's active checkpoint is untouched.
        new_manifest, _new_blobs, _new_id = _checkpoint(WORK_A, {"v2.txt": b"work a v2\n"}, 2)
        assert store.read_checkpoint(store.entry(WORK_A))[0] == new_manifest


class TestP03SchedulePostgres:
    async def test_b_reference_between_mark_and_sweep_keeps_the_shared_blob(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine, factory = await _sqlite_factory()
        try:
            store = _pg_store(tmp_path / "cas", factory)
            shared_digest = await _seed_two_checkpoints_with_shared_old_pg(store)

            b_manifest, b_blobs, b_id = _checkpoint(WORK_B, {"shared.txt": SHARED}, 1)
            original = store._amark_retention

            async def mark_then_b_lands(work_id: str, keep_last: int) -> Any:
                marked = await original(work_id, keep_last)
                if work_id == WORK_A and marked.removed_ids:
                    # B's full landing commits between A's mark and A's
                    # deletion transaction (A holds no rows open here).
                    await store.aput_checkpoint(
                        work_id=WORK_B, manifest_bytes=b_manifest, blobs=b_blobs, sequence=1
                    )
                return marked

            monkeypatch.setattr(store, "_amark_retention", mark_then_b_lands)

            removed = await store.aapply_retention(WORK_A, keep_last=1)

            assert removed == 1
            entry = await store.aentry(WORK_B)
            assert entry is not None and entry["checkpoint_id"] == b_id
            served_manifest, served_blobs = store.read_checkpoint(entry)
            assert served_manifest == b_manifest
            assert served_blobs == {_digest(SHARED): SHARED}
            assert (tmp_path / "cas" / shared_digest[:2] / shared_digest).is_file()
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# 1.5 THE P04 SCHEDULE (R36-04) — a B reference landing AFTER A's FINAL
#     reference scan, before A's unlink, keeps its bytes. The Q35-05
#     recheck protected references committed BEFORE its SELECT; the
#     remaining window (different work, per-work locks, unlink outside the
#     metadata transaction) is closed by the volume-wide reference/delete
#     lock: writers take it for their reference-recording transaction,
#     sweeps hold it from the final scan through the last unlink.
# ---------------------------------------------------------------------------


def _cas_path_of(root: Path, digest: str) -> Path:
    return root / digest[:2] / digest


class TestP04ScheduleFilesystem:
    def test_b_reference_after_the_final_scan_survives_the_sweep(self, tmp_path: Path) -> None:
        store = api_channel.CheckpointStore(tmp_path / "cas")
        shared_digest = _seed_two_checkpoints_with_shared_old_fs(store)
        b_manifest, b_blobs, b_id = _checkpoint(WORK_B, {"shared.txt": SHARED}, 1)
        b_manifest_path = _cas_path_of(tmp_path / "cas", b_id)
        landed = threading.Event()
        put_error: list[BaseException] = []

        def start_b_after_the_final_scan() -> None:
            def run_b() -> None:
                try:
                    store.put_checkpoint(
                        work_id=WORK_B, manifest_bytes=b_manifest, blobs=b_blobs, sequence=1
                    )
                except BaseException as exc:  # pragma: no cover — surfaced below
                    put_error.append(exc)
                finally:
                    landed.set()

            threading.Thread(target=run_b, daemon=True).start()
            # B's REAL landing wrote its blobs and is now queued on the
            # volume lock A holds: the manifest write is the landing's
            # LAST step before that queue, so its existence is the
            # deterministic rendezvous.
            deadline = time.monotonic() + 10.0
            while not b_manifest_path.is_file():
                if time.monotonic() > deadline:
                    raise AssertionError("work B never wrote its manifest bytes")
                time.sleep(0.005)

        store.gc_after_final_scan = start_b_after_the_final_scan

        removed = store.apply_retention(WORK_A, keep_last=1)

        assert removed == 1
        assert landed.wait(10.0), "work B's landing never finished"
        assert put_error == []
        # THE P04 assertion: B's committed reference resolves verified
        # bytes even though it began strictly after A's final scan.
        entry = store.entry(WORK_B)
        assert entry is not None and entry["checkpoint_id"] == b_id
        served_manifest, served_blobs = store.read_checkpoint(entry)
        assert served_manifest == b_manifest
        assert served_blobs == {shared_digest: SHARED}
        assert _cas_path_of(tmp_path / "cas", shared_digest).is_file()
        # And A's active checkpoint is untouched.
        new_manifest, _new_blobs, _new_id = _checkpoint(WORK_A, {"v2.txt": b"work a v2\n"}, 2)
        assert store.read_checkpoint(store.entry(WORK_A))[0] == new_manifest

    def test_b_reference_before_the_final_scan_is_spared_control(self, tmp_path: Path) -> None:
        store = api_channel.CheckpointStore(tmp_path / "cas")
        shared_digest = _seed_two_checkpoints_with_shared_old_fs(store)
        b_manifest, b_blobs, b_id = _checkpoint(WORK_B, {"shared.txt": SHARED}, 1)
        # B commits FIRST — the already-safe order A's recheck sees.
        store.put_checkpoint(work_id=WORK_B, manifest_bytes=b_manifest, blobs=b_blobs, sequence=1)

        removed = store.apply_retention(WORK_A, keep_last=1)

        assert removed == 1
        entry = store.entry(WORK_B)
        assert entry is not None and entry["checkpoint_id"] == b_id
        served_manifest, served_blobs = store.read_checkpoint(entry)
        assert served_manifest == b_manifest
        assert served_blobs == {shared_digest: SHARED}
        assert _cas_path_of(tmp_path / "cas", shared_digest).is_file()


class TestP04SchedulePostgres:
    async def test_b_reference_after_the_final_scan_survives_the_sweep(
        self, tmp_path: Path
    ) -> None:
        engine, factory = await _sqlite_factory()
        try:
            store = _pg_store(tmp_path / "cas", factory)
            shared_digest = await _seed_two_checkpoints_with_shared_old_pg(store)
            other = _pg_store(tmp_path / "cas", factory)  # a FRESH store over the same volume
            b_manifest, b_blobs, b_id = _checkpoint(WORK_B, {"shared.txt": SHARED}, 1)
            b_manifest_path = _cas_path_of(tmp_path / "cas", b_id)
            task: asyncio.Task[None] | None = None
            put_error: list[BaseException] = []

            async def start_b_after_the_final_scan() -> None:
                async def run_b() -> None:
                    try:
                        await other.aput_checkpoint(
                            work_id=WORK_B,
                            manifest_bytes=b_manifest,
                            blobs=b_blobs,
                            sequence=1,
                        )
                    except BaseException as exc:  # pragma: no cover — surfaced below
                        put_error.append(exc)

                nonlocal task
                task = asyncio.create_task(run_b())
                deadline = asyncio.get_running_loop().time() + 10.0
                while not b_manifest_path.is_file():
                    if asyncio.get_running_loop().time() > deadline:
                        raise AssertionError("work B never wrote its manifest bytes")
                    await asyncio.sleep(0.005)

            store.gc_after_final_scan = start_b_after_the_final_scan

            removed = await store.aapply_retention(WORK_A, keep_last=1)

            assert removed == 1
            assert task is not None
            await asyncio.wait_for(task, 10.0)
            assert put_error == []
            entry = await store.aentry(WORK_B)
            assert entry is not None and entry["checkpoint_id"] == b_id
            served_manifest, served_blobs = store.read_checkpoint(entry)
            assert served_manifest == b_manifest
            assert served_blobs == {shared_digest: SHARED}
            assert _cas_path_of(tmp_path / "cas", shared_digest).is_file()
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# 2. PINS — an approved ResumeSpec's exact checkpoint survives a newer
#    checkpoint and automatic retention, until the pin is released EXPLICITLY.
# ---------------------------------------------------------------------------


class TestApprovedResumePin:
    async def test_pinned_checkpoint_survives_newer_upload_and_retention_then_unpin_releases(
        self, tmp_path: Path
    ) -> None:
        repo = _fs_repo(tmp_path)
        old_id = await _put(repo, WORK_A, {"old.txt": b"old bytes\n"}, 1)
        await repo.pin(WORK_A, old_id, reason="resume:cmd-42")
        new_id = await _put(repo, WORK_A, {"new.txt": b"new bytes\n"}, 2)

        # Retention keeps only the newest — the pin holds the old row AND
        # its blobs back, pass after pass (never released by time).
        assert await repo.apply_retention(WORK_A, keep_last=1) == 0
        assert await repo.apply_retention(WORK_A, keep_last=1) == 0
        entry = await repo.entry(WORK_A, old_id)
        assert entry is not None, "the pinned exact checkpoint survived retention"
        manifest, blobs = await repo.read(WORK_A, old_id)  # type: ignore[misc]
        assert manifest == _checkpoint(WORK_A, {"old.txt": b"old bytes\n"}, 1)[0]
        assert blobs == [b"old bytes\n"]
        assert (await repo.entry(WORK_A))["checkpoint_id"] == new_id  # B is active

        # The EXPLICIT release is what eventually allows GC.
        assert await repo.unpin(WORK_A, old_id, reason="resume:cmd-42") == 1
        assert await repo.apply_retention(WORK_A, keep_last=1) == 1
        assert await repo.entry(WORK_A, old_id) is None
        assert await repo.read(WORK_A, new_id) is not None  # type: ignore[misc]

    async def test_pin_survives_newer_upload_and_retention_on_the_postgres_authority(
        self, tmp_path: Path
    ) -> None:
        engine, factory = await _sqlite_factory()
        try:
            repo = _pg_repo(tmp_path, factory)
            old_id = await _put(repo, WORK_A, {"old.txt": b"pg old\n"}, 1)
            await repo.pin(WORK_A, old_id, reason="resume:cmd-7")
            new_id = await _put(repo, WORK_A, {"new.txt": b"pg new\n"}, 2)

            assert await repo.apply_retention(WORK_A, keep_last=1) == 0
            assert await repo.entry(WORK_A, old_id) is not None
            served = await repo.read(WORK_A, old_id)
            assert served is not None and b"pg old" in served[1][0]

            assert await repo.unpin(WORK_A, old_id) == 1
            assert await repo.apply_retention(WORK_A, keep_last=1) == 1
            assert await repo.entry(WORK_A, old_id) is None
            assert (await repo.entry(WORK_A))["checkpoint_id"] == new_id
        finally:
            await engine.dispose()

    async def test_pinning_a_checkpoint_the_authority_does_not_hold_refuses(self, tmp_path):
        repo = _fs_repo(tmp_path)
        with pytest.raises(ValueError, match="no such checkpoint"):
            await repo.pin(WORK_A, "f" * 64, reason="resume:cmd-1")
        assert await repo.pins() == []  # refused pin left no record

    async def test_pins_are_listed_with_their_reasons(self, tmp_path: Path) -> None:
        repo = _fs_repo(tmp_path)
        first = await _put(repo, WORK_A, {"a.txt": b"a\n"}, 1)
        await _put(repo, WORK_B, {"b.txt": b"b\n"}, 1)
        second = await repo.entry(WORK_B)
        assert second is not None
        await repo.pin(WORK_A, first, reason="resume:cmd-1")
        await repo.pin(WORK_B, str(second["checkpoint_id"]), reason="legal-hold")

        listed = await repo.pins()

        assert [(pin["work_id"], pin["reason"]) for pin in listed] == [
            (WORK_A, "resume:cmd-1"),
            (WORK_B, "legal-hold"),
        ]

    async def test_an_authorized_resume_pins_its_exact_checkpoint(self, tmp_path: Path) -> None:
        """The wiring seam: OperatorControlService.resume records the pin."""
        from forge.adaptive.wiring import OperatorControlService

        repo = FilesystemCheckpointRepository(tmp_path / "cas")
        manifest, blobs, checkpoint_id = _checkpoint(WORK_A, {"wip.txt": b"wip\n"}, 4)
        await repo.put(WORK_A, checkpoint_id, manifest, blobs)
        service = OperatorControlService(checkpoint_repository=repo)

        resumed = await service.resume(WORK_A, actor="alice", idempotency_key="resume-1")

        assert resumed is True
        pins = await repo.pins()
        assert [pin["checkpoint_id"] for pin in pins] == [checkpoint_id]
        assert pins[0]["reason"] == "resume:resume-1"


# ---------------------------------------------------------------------------
# 3. FIRST-UPLOAD SERIALIZATION — the per-work quota holds when a work's
#    first two checkpoints land concurrently (SELECT FOR UPDATE over an
#    EMPTY set is not a mutex).
# ---------------------------------------------------------------------------


class TestConcurrentFirstUploads:
    async def test_concurrent_first_uploads_respect_the_quota_sqlite(self, tmp_path: Path) -> None:
        engine, factory = await _sqlite_factory()
        try:
            manifest_a, blobs_a, _id_a = _checkpoint(WORK_A, {"a.txt": b"aaa\n"}, 1)
            manifest_b, blobs_b, _id_b = _checkpoint(WORK_A, {"b.txt": b"bbb\n"}, 1)
            one = len(manifest_a) + sum(len(data) for data in blobs_a.values())
            other = len(manifest_b) + sum(len(data) for data in blobs_b.values())
            cap = max(one, other) + min(one, other) // 2  # either fits, both never
            repo = _pg_repo(
                tmp_path,
                factory,
                policy=api_channel.StoragePolicy(
                    max_total_bytes_per_work=cap, cleanup_trigger="manual"
                ),
            )

            results = await asyncio.gather(
                repo.put_checkpoint(
                    work_id=WORK_A, manifest_bytes=manifest_a, blobs=blobs_a, sequence=1
                ),
                repo.put_checkpoint(
                    work_id=WORK_A, manifest_bytes=manifest_b, blobs=blobs_b, sequence=1
                ),
                return_exceptions=True,
            )

            landed = [result for result in results if not isinstance(result, BaseException)]
            refused = [result for result in results if isinstance(result, BaseException)]
            assert len(landed) == 1
            assert len(refused) == 1
            assert isinstance(refused[0], api_channel.StorageQuotaExceededError)
            entry = await repo.entry(WORK_A)
            assert entry is not None
            assert entry["checkpoint_id"] == landed[0]["checkpoint_id"]
        finally:
            await engine.dispose()

    @pytest.mark.skipif(
        not os.environ.get("FORGE_PG_TEST_URL"),
        reason=(
            "FORGE_PG_TEST_URL not set — the real-isolation first-upload proof "
            "runs only against a disposable real Postgres"
        ),
    )
    async def test_concurrent_first_uploads_respect_the_quota_real_postgres(
        self, tmp_path: Path
    ) -> None:
        engine = create_async_engine(os.environ["FORGE_PG_TEST_URL"])
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(delete(api_channel.CheckpointMetadataRow))
            factory = async_sessionmaker(engine, expire_on_commit=False)
            manifest_a, blobs_a, _id_a = _checkpoint(WORK_A, {"a.txt": b"aaa\n"}, 1)
            manifest_b, blobs_b, _id_b = _checkpoint(WORK_A, {"b.txt": b"bbb\n"}, 1)
            one = len(manifest_a) + sum(len(data) for data in blobs_a.values())
            other = len(manifest_b) + sum(len(data) for data in blobs_b.values())
            cap = max(one, other) + min(one, other) // 2
            repo = _pg_repo(
                tmp_path,
                factory,
                policy=api_channel.StoragePolicy(
                    max_total_bytes_per_work=cap, cleanup_trigger="manual"
                ),
            )

            results = await asyncio.gather(
                repo.put_checkpoint(
                    work_id=WORK_A, manifest_bytes=manifest_a, blobs=blobs_a, sequence=1
                ),
                repo.put_checkpoint(
                    work_id=WORK_A, manifest_bytes=manifest_b, blobs=blobs_b, sequence=1
                ),
                return_exceptions=True,
            )

            landed = [result for result in results if not isinstance(result, BaseException)]
            refused = [result for result in results if isinstance(result, BaseException)]
            assert len(landed) == 1, results
            assert len(refused) == 1, results
            assert isinstance(refused[0], api_channel.StorageQuotaExceededError)
            entry = await repo.entry(WORK_A)
            assert entry is not None and entry["checkpoint_id"] == landed[0]["checkpoint_id"]
            # The winner's bytes are complete and re-readable.
            served = await repo.read(WORK_A)
            assert served is not None
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# 4. A FAILED TRANSACTION DELETES NOTHING — an exception between the recheck
#    and the sweep leaves every committed reference's bytes intact.
# ---------------------------------------------------------------------------


class TestFailedTransactionDeletesNothing:
    async def test_exception_before_commit_rolls_back_rows_and_keeps_blobs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine, factory = await _sqlite_factory()
        try:
            store = _pg_store(tmp_path / "cas", factory)
            await _seed_two_checkpoints_with_shared_old_pg(store)

            async def exploding_delete(session: Any, rows: list[Any]) -> None:
                raise RuntimeError("died between recheck and sweep")

            monkeypatch.setattr(store, "_adelete_rows", exploding_delete)

            with pytest.raises(RuntimeError, match="between recheck and sweep"):
                await store.aapply_retention(WORK_A, keep_last=1)

            # The transaction rolled back: BOTH rows still stand and every
            # committed reference still resolves its bytes.
            old_entry = await store.aentry(
                WORK_A, _checkpoint(WORK_A, {"shared.txt": SHARED}, 1)[2]
            )
            assert old_entry is not None
            store.read_checkpoint(old_entry)  # manifest + SHARED blob readable
            active = await store.aentry(WORK_A)
            assert active is not None and active["sequence"] == 2
            store.read_checkpoint(active)
        finally:
            await engine.dispose()

    async def test_exception_during_the_sweep_leaves_the_tombstone_recoverable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _fs_repo(tmp_path)
        old_id = await _put(repo, WORK_A, {"old.txt": b"old\n"}, 1)
        await _put(repo, WORK_A, {"new.txt": b"new\n"}, 2)
        b_manifest, b_blobs, b_id = _checkpoint(WORK_B, {"old.txt": b"old\n"}, 1)
        # B references the OLD blob — its landing between the mark and a
        # crashing sweep is exactly the P03 window, with the crash added.
        original_reach = repo._store._referenced_by_retained_works

        def b_lands_at_mark(exclude_ids: set[str]) -> set[str]:
            referenced = original_reach(exclude_ids)
            if exclude_ids:
                repo._store.put_checkpoint(
                    work_id=WORK_B, manifest_bytes=b_manifest, blobs=b_blobs, sequence=1
                )
            return referenced

        monkeypatch.setattr(repo._store, "_referenced_by_retained_works", b_lands_at_mark)

        from pathlib import Path as _Path

        original_unlink = _Path.unlink

        def crashing_unlink(self: _Path, missing_ok: bool = False) -> None:
            original_unlink(self, missing_ok=missing_ok)
            raise OSError("sweep crashed after the first unlink")

        monkeypatch.setattr(_Path, "unlink", crashing_unlink)
        with pytest.raises(OSError, match="sweep crashed"):
            await repo.apply_retention(WORK_A, keep_last=1)
        monkeypatch.setattr(_Path, "unlink", original_unlink)

        # The crash left rows already tombstoned — but B's committed
        # reference keeps its bytes, and the recorded pending GC converges
        # on the next pass WITHOUT touching anything still referenced.
        entry_b = await repo.entry(WORK_B)
        assert entry_b is not None and entry_b["checkpoint_id"] == b_id
        served = await repo.read(WORK_B)
        assert served is not None and served[0] == b_manifest

        removed = await repo.apply_retention(WORK_A, keep_last=1)

        assert removed == 0  # convergence, not a second deletion
        assert await repo.entry(WORK_A, old_id) is None
        assert await repo.read(WORK_B) is not None  # B still readable after convergence


# ---------------------------------------------------------------------------
# 5. INTERRUPTED GC — a collector killed between the mark and the sweep
#    converges on the next pass; no double deletion, no lost references.
# ---------------------------------------------------------------------------


class TestInterruptedGcConverges:
    async def test_postgres_gc_killed_before_the_sweep_resumes_and_converges(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine, factory = await _sqlite_factory()
        try:
            store = _pg_store(tmp_path / "cas", factory)
            shared_digest = await _seed_two_checkpoints_with_shared_old_pg(store)
            # The OLD checkpoint's manifest and the SHARED blob are the
            # doomed pair — no other work references them in this test.
            doomed_blob = shared_digest

            async def crashing_sweep(digests: list[str]) -> set[str]:
                raise RuntimeError("killed between commit and unlink")

            original_sweep = store._asweep_locked
            monkeypatch.setattr(store, "_asweep_locked", crashing_sweep)

            with pytest.raises(RuntimeError, match="between commit and unlink"):
                await store.aapply_retention(WORK_A, keep_last=1)

            monkeypatch.setattr(store, "_asweep_locked", original_sweep)
            # The crash window left orphans (rows committed, blobs present).
            assert (tmp_path / "cas" / doomed_blob[:2] / doomed_blob).is_file()

            removed = await store.aapply_retention(WORK_A, keep_last=1)

            assert removed == 0  # the metadata already converged — GC only
            assert not (tmp_path / "cas" / doomed_blob[:2] / doomed_blob).exists()
            active = await store.aentry(WORK_A)
            assert active is not None
            store.read_checkpoint(active)  # the surviving reference is intact
            journal = store.gc_journal.pending(WORK_A)
            assert journal == []
        finally:
            await engine.dispose()

    async def test_filesystem_gc_killed_between_mark_and_sweep_converges(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _fs_repo(tmp_path)
        old_manifest, _old_blobs, old_id = _checkpoint(WORK_A, {"old.txt": b"old\n"}, 1)
        await _put(repo, WORK_A, {"old.txt": b"old\n"}, 1)
        await _put(repo, WORK_A, {"new.txt": b"new\n"}, 2)
        old_blob = _digest(b"old\n")
        store = repo._store

        # Kill the collector right after the tombstone (index save),
        # before one unlink ran — by crashing the sweep entrypoint.
        def killed_before_sweep(digests: list[str]) -> set[str]:
            raise OSError("killed before sweep")

        monkeypatch.setattr(store, "_sweep_locked", killed_before_sweep)
        with pytest.raises(OSError, match="killed before sweep"):
            store.apply_retention(WORK_A, keep_last=1)
        monkeypatch.undo()  # the collector restarts for the recovery pass

        index = json.loads((tmp_path / "cas" / "works" / f"{WORK_A}.json").read_text())
        assert index["retention"]["pending_gc"], "the interrupted pass recorded its pending GC"
        assert await repo.entry(WORK_A, old_id) is None  # the tombstone stands

        removed = store.apply_retention(WORK_A, keep_last=1)

        assert removed == 0  # converged: no second metadata deletion
        assert not (tmp_path / "cas" / old_blob[:2] / old_blob).exists()
        served = await repo.read(WORK_A)
        assert served is not None


# ---------------------------------------------------------------------------
# 6. THE SAME BARRIER ON EVERY SWEEP PATH (R36-04) — on-upload cleanup and
#    pending-GC recovery obey the identical scan-to-unlink serialization.
# ---------------------------------------------------------------------------


def _start_b_thread(store: Any, b_manifest: bytes, b_blobs: dict[str, bytes]) -> threading.Event:
    """B's REAL sync landing on its own thread; the event fires when done."""
    landed = threading.Event()

    def run_b() -> None:
        try:
            store.put_checkpoint(
                work_id=WORK_B, manifest_bytes=b_manifest, blobs=b_blobs, sequence=1
            )
        except BaseException:  # pragma: no cover — surfaced by the caller's assertions
            raise
        finally:
            landed.set()

    threading.Thread(target=run_b, daemon=True).start()
    return landed


def _wait_for_file(path: Path, *, timeout: float = 10.0) -> None:
    """Block until *path* exists — B wrote its blobs and queued on the lock."""
    deadline = time.monotonic() + timeout
    while not path.is_file():
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} never appeared — work B never wrote its bytes")
        time.sleep(0.005)


async def _await_file(path: Path, *, timeout: float = 10.0) -> None:
    """The async twin: polls on the EVENT LOOP, no executor thread.

    The P04 hooks run while the sweep HOLDS the volume lock; routing the
    wait through the default ThreadPoolExecutor starved on CI's 2-core
    runners (the hook stalled inside the locked sweep and the probe
    coordination collapsed — the repeated 3.14 flake). A loop-level
    poll cannot starve: the blob write is a plain filesystem call the
    background task makes without the loop.
    """
    deadline = time.monotonic() + timeout
    while not path.is_file():
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} never appeared — work B never wrote its bytes")
        await asyncio.sleep(0.005)


class TestP04OnUploadCleanup:
    async def test_b_reference_after_the_final_scan_survives_on_upload_cleanup(
        self, tmp_path: Path
    ) -> None:
        engine, factory = await _sqlite_factory()
        try:
            root = tmp_path / "cas"
            seed = _pg_store(root, factory)  # keep-everything policy for seeding
            shared_digest = await _seed_two_checkpoints_with_shared_old_pg(seed)
            sweeping = _pg_store(
                root,
                factory,
                policy=api_channel.StoragePolicy(
                    max_checkpoints_per_work=1, cleanup_trigger="on_upload"
                ),
            )
            other = _pg_store(root, factory)
            b_manifest, b_blobs, b_id = _checkpoint(WORK_B, {"shared.txt": SHARED}, 1)
            b_manifest_path = _cas_path_of(root, b_id)
            task: asyncio.Task[None] | None = None

            async def start_b_after_the_final_scan() -> None:
                async def run_b() -> None:
                    await other.aput_checkpoint(
                        work_id=WORK_B, manifest_bytes=b_manifest, blobs=b_blobs, sequence=1
                    )

                nonlocal task
                task = asyncio.create_task(run_b())
                await _await_file(b_manifest_path)

            sweeping.gc_after_final_scan = start_b_after_the_final_scan
            # A's THIRD landing triggers the on-upload retention inside the
            # landing's own volume-lock section: scan (in txn) → commit →
            # barrier → unlink.
            v3 = _checkpoint(WORK_A, {"v3.txt": b"work a v3\n"}, 3)
            await sweeping.aput_checkpoint(
                work_id=WORK_A, manifest_bytes=v3[0], blobs=v3[1], sequence=3
            )

            assert task is not None
            await asyncio.wait_for(task, 10.0)
            entry = await sweeping.aentry(WORK_B)
            assert entry is not None and entry["checkpoint_id"] == b_id
            served_manifest, served_blobs = sweeping.read_checkpoint(entry)
            assert served_manifest == b_manifest
            assert served_blobs == {shared_digest: SHARED}
        finally:
            await engine.dispose()

    def test_b_reference_after_the_final_scan_survives_on_upload_cleanup_filesystem(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "cas"
        store = api_channel.CheckpointStore(root)
        shared_digest = _seed_two_checkpoints_with_shared_old_fs(store)
        sweeping = api_channel.CheckpointStore(
            root,
            policy=api_channel.StoragePolicy(
                max_checkpoints_per_work=1, cleanup_trigger="on_upload"
            ),
        )
        b_manifest, b_blobs, b_id = _checkpoint(WORK_B, {"shared.txt": SHARED}, 1)
        landed: list[threading.Event] = []

        def start_b_after_the_final_scan() -> None:
            landed.append(_start_b_thread(sweeping, b_manifest, b_blobs))
            _wait_for_file(_cas_path_of(root, b_id))

        sweeping.gc_after_final_scan = start_b_after_the_final_scan
        v3 = _checkpoint(WORK_A, {"v3.txt": b"work a v3\n"}, 3)
        sweeping.put_checkpoint(work_id=WORK_A, manifest_bytes=v3[0], blobs=v3[1], sequence=3)

        assert landed and landed[0].wait(10.0)
        entry = sweeping.entry(WORK_B)
        assert entry is not None and entry["checkpoint_id"] == b_id
        served_manifest, served_blobs = sweeping.read_checkpoint(entry)
        assert served_manifest == b_manifest
        assert served_blobs == {shared_digest: SHARED}


class TestP04PendingGcRecovery:
    async def test_b_reference_after_the_final_scan_survives_recovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine, factory = await _sqlite_factory()
        try:
            root = tmp_path / "cas"
            store = _pg_store(root, factory)
            shared_digest = await _seed_two_checkpoints_with_shared_old_pg(store)
            other = _pg_store(root, factory)

            # Crash the first pass between the metadata commit and the
            # unlink — the journal keeps the deletion set pending.
            async def crashed(digests: list[str]) -> set[str]:
                raise RuntimeError("killed between commit and unlink")

            monkeypatch.setattr(store, "_asweep_locked", crashed)
            with pytest.raises(RuntimeError, match="between commit and unlink"):
                await store.aapply_retention(WORK_A, keep_last=1)
            monkeypatch.undo()  # the collector restarts

            assert store.gc_journal.pending(WORK_A), "the interrupted pass journaled its set"
            b_manifest, b_blobs, b_id = _checkpoint(WORK_B, {"shared.txt": SHARED}, 1)
            task: asyncio.Task[None] | None = None

            async def start_b_after_the_final_scan() -> None:
                async def run_b() -> None:
                    await other.aput_checkpoint(
                        work_id=WORK_B, manifest_bytes=b_manifest, blobs=b_blobs, sequence=1
                    )

                nonlocal task
                task = asyncio.create_task(run_b())
                await _await_file(_cas_path_of(root, b_id))

            store.gc_after_final_scan = start_b_after_the_final_scan
            removed = await store.aapply_retention(WORK_A, keep_last=1)

            assert removed == 0  # metadata already converged — recovery only
            assert task is not None
            await asyncio.wait_for(task, 10.0)
            # B's put COMPLETED (the await above proves it) — under CI's
            # slower executor scheduling the row can surface on the
            # reader connection a beat later (file-backed sqlite, two
            # connections). A bounded visibility poll for an ALREADY-
            # committed row; the strict assertions below are unchanged.
            entry = None
            for _ in range(40):
                entry = await store.aentry(WORK_B)
                if entry is not None:
                    break
                await asyncio.sleep(0.05)
            assert entry is not None and entry["checkpoint_id"] == b_id
            served_manifest, served_blobs = store.read_checkpoint(entry)
            assert served_manifest == b_manifest
            assert served_blobs == {shared_digest: SHARED}
        finally:
            await engine.dispose()

    def test_b_reference_after_the_final_scan_survives_recovery_filesystem(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "cas"
        store = api_channel.CheckpointStore(root)
        shared_digest = _seed_two_checkpoints_with_shared_old_fs(store)

        def crashed(digests: list[str]) -> set[str]:
            raise OSError("killed between tombstone and unlink")

        monkeypatch.setattr(store, "_sweep_locked", crashed)
        with pytest.raises(OSError, match="between tombstone and unlink"):
            store.apply_retention(WORK_A, keep_last=1)
        monkeypatch.undo()

        b_manifest, b_blobs, b_id = _checkpoint(WORK_B, {"shared.txt": SHARED}, 1)
        landed: list[threading.Event] = []

        def start_b_after_the_final_scan() -> None:
            landed.append(_start_b_thread(store, b_manifest, b_blobs))
            _wait_for_file(_cas_path_of(root, b_id))

        store.gc_after_final_scan = start_b_after_the_final_scan
        removed = store.apply_retention(WORK_A, keep_last=1)

        assert removed == 0  # the tombstone stands — this pass only recovers
        assert landed and landed[0].wait(10.0)
        entry = store.entry(WORK_B)
        assert entry is not None and entry["checkpoint_id"] == b_id
        served_manifest, served_blobs = store.read_checkpoint(entry)
        assert served_manifest == b_manifest
        assert served_blobs == {shared_digest: SHARED}


# ---------------------------------------------------------------------------
# 7. THE LOCK ITSELF — held scan-to-unlink, bounded, single order, abortable.
# ---------------------------------------------------------------------------

try:  # POSIX flock — the volume lock's spine (matches the channel's import).
    import fcntl
except ImportError:  # pragma: no cover — non-POSIX platform
    fcntl = None  # type: ignore[assignment]


def _flock_held(path: Path) -> bool:
    """Whether another descriptor currently holds the volume lock."""
    assert fcntl is not None, "POSIX flock required"
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except OSError:
            return True
    finally:
        os.close(fd)


class TestVolumeLockMechanics:
    def test_the_lock_spans_the_final_scan_through_the_unlink_filesystem(
        self, tmp_path: Path
    ) -> None:
        store = api_channel.CheckpointStore(tmp_path / "cas")
        _seed_two_checkpoints_with_shared_old_fs(store)
        observed: list[bool] = []
        store.gc_after_final_scan = lambda: observed.append(
            _flock_held(store._cas_refs_lock_path())
        )

        removed = store.apply_retention(WORK_A, keep_last=1)

        assert removed == 1
        assert observed == [True], "the sweep must hold the volume lock at unlink time"

    async def test_the_lock_spans_the_final_scan_through_the_unlink_postgres(
        self, tmp_path: Path
    ) -> None:
        engine, factory = await _sqlite_factory()
        try:
            store = _pg_store(tmp_path / "cas", factory)
            await _seed_two_checkpoints_with_shared_old_pg(store)
            observed: list[bool] = []
            store.gc_after_final_scan = lambda: observed.append(
                _flock_held(store._cas_refs_lock_path())
            )

            removed = await store.aapply_retention(WORK_A, keep_last=1)

            assert removed == 1
            assert observed == [True]
        finally:
            await engine.dispose()

    async def test_a_contended_sweep_aborts_typed_and_retries_later_postgres(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine, factory = await _sqlite_factory()
        try:
            store = _pg_store(tmp_path / "cas", factory)
            await _seed_two_checkpoints_with_shared_old_pg(store)
            monkeypatch.setenv(api_channel.GC_LOCK_WAIT_SECONDS_ENV, "0.2")
            # An external holder — another process's live sweep.
            fd = os.open(str(store._cas_refs_lock_path()), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                with pytest.raises(api_channel.GCLockTimeout):
                    await asyncio.wait_for(store.aapply_retention(WORK_A, keep_last=1), 30.0)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

            # The aborted sweep deleted NOTHING — both rows still stand.
            old_id = _checkpoint(WORK_A, {"shared.txt": SHARED}, 1)[2]
            assert await store.aentry(WORK_A, old_id) is not None
            # Retried once the holder is gone, it converges.
            assert await store.aapply_retention(WORK_A, keep_last=1) == 1
            assert await store.aentry(WORK_A, old_id) is None
        finally:
            await engine.dispose()

    def test_a_contended_sweep_aborts_typed_and_retries_later_filesystem(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "cas"
        store = api_channel.CheckpointStore(root)
        shared_digest = _seed_two_checkpoints_with_shared_old_fs(store)
        monkeypatch.setenv(api_channel.GC_LOCK_WAIT_SECONDS_ENV, "0.2")
        fd = os.open(str(store._cas_refs_lock_path()), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with pytest.raises(api_channel.GCLockTimeout):
                store.apply_retention(WORK_A, keep_last=1)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        # The aborted sweep unlinked NOTHING — the tombstone stands with
        # the full pending set and every byte is still on disk (exactly
        # the crash-after-tombstone window the completion pass recovers).
        assert _cas_path_of(root, shared_digest).is_file()
        index = json.loads((root / "works" / f"{WORK_A}.json").read_text())
        assert index["retention"]["pending_gc"], "the aborted pass kept its marks"
        # Retried once the holder is gone, it converges — and collects.
        assert store.apply_retention(WORK_A, keep_last=1) == 0
        assert not _cas_path_of(root, shared_digest).exists()
        assert (
            store.read_checkpoint(store.entry(WORK_A))[0]
            == _checkpoint(WORK_A, {"v2.txt": b"work a v2\n"}, 2)[0]
        )

    async def test_two_sweeps_and_a_concurrent_writer_do_not_deadlock_postgres(
        self, tmp_path: Path
    ) -> None:
        engine, factory = await _sqlite_file_factory(tmp_path)
        try:
            work_c = "wp-r36-04c"
            store = _pg_store(tmp_path / "cas", factory)
            await _seed_two_checkpoints_with_shared_old_pg(store)
            await _put(_pg_repo(tmp_path, factory), WORK_B, {"b1.txt": b"b one\n"}, 1)
            await _put(_pg_repo(tmp_path, factory), WORK_B, {"b2.txt": b"b two\n"}, 2)
            c_manifest, c_blobs, c_id = _checkpoint(work_c, {"c.txt": b"see\n"}, 1)

            outcomes = await asyncio.wait_for(
                asyncio.gather(
                    store.aapply_retention(WORK_A, keep_last=1),
                    store.aapply_retention(WORK_B, keep_last=1),
                    store.aput_checkpoint(
                        work_id=work_c,
                        manifest_bytes=c_manifest,
                        blobs=c_blobs,
                        sequence=1,
                    ),
                ),
                timeout=30.0,
            )

            assert outcomes[0] == 1 and outcomes[1] == 1  # both sweeps collected
            entry = await store.aentry(work_c)
            assert entry is not None and entry["checkpoint_id"] == c_id
            store.read_checkpoint(entry)  # the concurrent writer's bytes are whole
        finally:
            await engine.dispose()

    def test_two_sweeps_and_a_concurrent_writer_do_not_deadlock_filesystem(
        self, tmp_path: Path
    ) -> None:
        import concurrent.futures

        work_c = "wp-r36-04c"
        store = api_channel.CheckpointStore(tmp_path / "cas")
        _seed_two_checkpoints_with_shared_old_fs(store)
        b1 = _checkpoint(WORK_B, {"b1.txt": b"b one\n"}, 1)
        b2 = _checkpoint(WORK_B, {"b2.txt": b"b two\n"}, 2)
        store.put_checkpoint(work_id=WORK_B, manifest_bytes=b1[0], blobs=b1[1], sequence=1)
        store.put_checkpoint(work_id=WORK_B, manifest_bytes=b2[0], blobs=b2[1], sequence=2)
        c = _checkpoint(work_c, {"c.txt": b"see\n"}, 1)

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            sweep_a = pool.submit(store.apply_retention, WORK_A, 1)
            sweep_b = pool.submit(store.apply_retention, WORK_B, 1)
            writer = pool.submit(
                store.put_checkpoint,
                work_id=work_c,
                manifest_bytes=c[0],
                blobs=c[1],
                sequence=1,
            )
            assert sweep_a.result(timeout=30.0) == 1
            assert sweep_b.result(timeout=30.0) == 1
            assert writer.result(timeout=30.0)["checkpoint_id"] == c[2]

        entry = store.entry(work_c)
        assert entry is not None
        store.read_checkpoint(entry)  # the concurrent writer's bytes are whole


# ---------------------------------------------------------------------------
# 8. THE OPERATOR FENCE — FORGE_CHECKPOINT_SWEEP=off marks but never unlinks;
#    turning sweeping back on collects the marked set safely.
# ---------------------------------------------------------------------------


class TestSweepDisabledRollout:
    async def test_off_marks_but_never_unlinks_then_collects_filesystem(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _fs_repo(tmp_path)
        old_id = await _put(repo, WORK_A, {"old.txt": b"old\n"}, 1)
        await _put(repo, WORK_A, {"new.txt": b"new\n"}, 2)
        old_blob = _digest(b"old\n")

        monkeypatch.setenv(api_channel.SWEEP_ENV, "off")
        assert await repo.apply_retention(WORK_A, keep_last=1) == 1  # metadata went
        assert await repo.entry(WORK_A, old_id) is None
        assert _cas_path_of(tmp_path / "cas", old_blob).is_file()  # bytes stayed
        index = json.loads((tmp_path / "cas" / "works" / f"{WORK_A}.json").read_text())
        assert index["retention"]["pending_gc"], "the tombstone stands while fenced"

        monkeypatch.delenv(api_channel.SWEEP_ENV)
        assert await repo.apply_retention(WORK_A, keep_last=1) == 0  # converged
        assert not _cas_path_of(tmp_path / "cas", old_blob).exists()  # collected
        assert await repo.read(WORK_A) is not None  # type: ignore[misc]

    async def test_off_marks_but_never_unlinks_then_collects_postgres(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine, factory = await _sqlite_factory()
        try:
            repo = _pg_repo(tmp_path, factory)
            old_id = await _put(repo, WORK_A, {"old.txt": b"pg old\n"}, 1)
            await _put(repo, WORK_A, {"new.txt": b"pg new\n"}, 2)
            old_blob = _digest(b"pg old\n")

            monkeypatch.setenv(api_channel.SWEEP_ENV, "off")
            assert await repo.apply_retention(WORK_A, keep_last=1) == 1
            assert await repo.entry(WORK_A, old_id) is None
            assert _cas_path_of(tmp_path / "cas", old_blob).is_file()
            assert repo._store.gc_journal.pending(WORK_A), "the journal keeps the marked set"

            monkeypatch.delenv(api_channel.SWEEP_ENV)
            assert await repo.apply_retention(WORK_A, keep_last=1) == 0  # recovery only
            assert not _cas_path_of(tmp_path / "cas", old_blob).exists()
            assert repo._store.gc_journal.pending(WORK_A) == []
            assert await repo.read(WORK_A) is not None  # type: ignore[misc]
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# 9. DEDUPLICATED BLOBS — retention of one work never deletes a blob another
#    work's committed reference still needs.
# ---------------------------------------------------------------------------


class TestDeduplicatedBlobAcrossWorks:
    async def test_retention_of_one_work_spares_the_shared_blob_filesystem(
        self, tmp_path: Path
    ) -> None:
        repo = _fs_repo(tmp_path)
        await _put(repo, WORK_A, {"shared.txt": SHARED}, 1)
        await _put(repo, WORK_B, {"shared.txt": SHARED}, 1)
        await _put(repo, WORK_A, {"solo.txt": b"only a\n"}, 2)

        removed = await repo.apply_retention(WORK_A, keep_last=1)

        assert removed == 1
        served = await repo.read(WORK_B)
        assert served is not None and SHARED in served[1]

    async def test_retention_of_one_work_spares_the_shared_blob_postgres(
        self, tmp_path: Path
    ) -> None:
        engine, factory = await _sqlite_factory()
        try:
            repo = _pg_repo(tmp_path, factory)
            await _put(repo, WORK_A, {"shared.txt": SHARED}, 1)
            await _put(repo, WORK_B, {"shared.txt": SHARED}, 1)
            await _put(repo, WORK_A, {"solo.txt": b"only a\n"}, 2)

            removed = await repo.apply_retention(WORK_A, keep_last=1)

            assert removed == 1
            served = await repo.read(WORK_B)
            assert served is not None and SHARED in served[1]
        finally:
            await engine.dispose()

    async def test_a_shared_blob_newly_referenced_counts_toward_the_work_quota(
        self, tmp_path: Path
    ) -> None:
        """Quotas judge REFERENCED bytes: a deduplicated blob another work
        already stored still counts when THIS work newly references it."""
        engine, factory = await _sqlite_factory()
        try:
            other = _pg_repo(tmp_path, factory)
            await _put(other, WORK_B, {"shared.txt": SHARED}, 1)  # the blob exists on disk
            shared_size = len(SHARED)
            manifest, blobs, checkpoint_id = _checkpoint(WORK_A, {"shared.txt": SHARED}, 1)
            own_bytes = len(manifest)  # the manifest is the work's only fresh byte
            cap = own_bytes + shared_size - 1  # referencing the shared blob tips it over
            repo = _pg_repo(
                tmp_path,
                factory,
                policy=api_channel.StoragePolicy(
                    max_total_bytes_per_work=cap, cleanup_trigger="manual"
                ),
            )

            with pytest.raises(api_channel.StorageQuotaExceededError):
                await repo.put_checkpoint(
                    work_id=WORK_A, manifest_bytes=manifest, blobs=blobs, sequence=1
                )

            assert await repo.entry(WORK_A) is None  # refused before any write
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# 10. REAL POSTGRESQL, TWO ENGINES — the P04 barrier against the deployment
#     topology the issue names: shared database, shared CAS volume, two
#     independent processes (simulated with two engines/factories/stores).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("FORGE_PG_TEST_URL"),
    reason=(
        "FORGE_PG_TEST_URL not set — the real-PostgreSQL P04 barrier proof "
        "runs only against a disposable real Postgres (rows are deleted per test)"
    ),
)
class TestP04ScheduleRealPostgres:
    async def _lab(self, tmp_path: Path) -> tuple[Any, Any]:
        url = os.environ["FORGE_PG_TEST_URL"]
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(delete(api_channel.CheckpointMetadataRow))
        return engine, async_sessionmaker(engine, expire_on_commit=False)

    async def test_b_reference_after_the_final_scan_survives_two_engines(
        self, tmp_path: Path
    ) -> None:
        engine_a, factory_a = await self._lab(tmp_path)
        engine_b, factory_b = await self._lab(tmp_path)
        shared_root = tmp_path / "shared-blobs"
        try:
            store_a = _pg_store(shared_root, factory_a)
            store_b = _pg_store(shared_root, factory_b)  # the OTHER process
            shared_digest = await _seed_two_checkpoints_with_shared_old_pg(store_a)
            b_manifest, b_blobs, b_id = _checkpoint(WORK_B, {"shared.txt": SHARED}, 1)
            b_manifest_path = _cas_path_of(shared_root, b_id)
            task: asyncio.Task[None] | None = None

            async def start_b_after_the_final_scan() -> None:
                async def run_b() -> None:
                    await store_b.aput_checkpoint(
                        work_id=WORK_B, manifest_bytes=b_manifest, blobs=b_blobs, sequence=1
                    )

                nonlocal task
                task = asyncio.create_task(run_b())
                await asyncio.get_running_loop().run_in_executor(
                    None, _wait_for_file, b_manifest_path
                )

            store_a.gc_after_final_scan = start_b_after_the_final_scan
            removed = await store_a.aapply_retention(WORK_A, keep_last=1)

            assert removed == 1
            assert task is not None
            await asyncio.wait_for(task, 15.0)
            entry = await store_b.aentry(WORK_B)
            assert entry is not None and entry["checkpoint_id"] == b_id
            served_manifest, served_blobs = store_b.read_checkpoint(entry)
            assert served_manifest == b_manifest
            assert served_blobs == {shared_digest: SHARED}
        finally:
            await engine_a.dispose()
            await engine_b.dispose()

    async def test_b_reference_before_the_final_scan_is_spared_two_engines_control(
        self, tmp_path: Path
    ) -> None:
        engine_a, factory_a = await self._lab(tmp_path)
        engine_b, factory_b = await self._lab(tmp_path)
        shared_root = tmp_path / "shared-blobs"
        try:
            store_a = _pg_store(shared_root, factory_a)
            store_b = _pg_store(shared_root, factory_b)
            shared_digest = await _seed_two_checkpoints_with_shared_old_pg(store_a)
            b_manifest, b_blobs, b_id = _checkpoint(WORK_B, {"shared.txt": SHARED}, 1)
            # B commits FIRST on its own engine — the already-safe order.
            await store_b.aput_checkpoint(
                work_id=WORK_B, manifest_bytes=b_manifest, blobs=b_blobs, sequence=1
            )

            removed = await store_a.aapply_retention(WORK_A, keep_last=1)

            assert removed == 1
            entry = await store_b.aentry(WORK_B)
            assert entry is not None and entry["checkpoint_id"] == b_id
            served_manifest, served_blobs = store_b.read_checkpoint(entry)
            assert served_manifest == b_manifest
            assert served_blobs == {shared_digest: SHARED}
        finally:
            await engine_a.dispose()
            await engine_b.dispose()

    async def test_the_advisory_twin_alone_blocks_a_sweep_and_is_bounded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Where the blob root is NOT shared, the pg_advisory_lock twin is
        the serializer: an external holder of the constant key blocks a
        sweep (flock free) within the SAME bounded budget — never an
        unbounded wait."""
        from sqlalchemy import text

        engine, factory = await self._lab(tmp_path)
        holder_engine = create_async_engine(os.environ["FORGE_PG_TEST_URL"])
        holder_factory = async_sessionmaker(holder_engine, expire_on_commit=False)
        try:
            store = _pg_store(tmp_path / "cas", factory)
            await _seed_two_checkpoints_with_shared_old_pg(store)
            assert await store._acas_refs_is_postgres() is True
            monkeypatch.setenv(api_channel.GC_LOCK_WAIT_SECONDS_ENV, "0.5")

            async with holder_factory() as session:
                async with session.begin():
                    await session.execute(
                        text("SELECT pg_advisory_lock(:k)"),
                        {"k": api_channel._CAS_REFS_ADVISORY_KEY},
                    )
                    with pytest.raises(api_channel.GCLockTimeout):
                        await asyncio.wait_for(store.aapply_retention(WORK_A, keep_last=1), 30.0)
                    await session.execute(
                        text("SELECT pg_advisory_unlock(:k)"),
                        {"k": api_channel._CAS_REFS_ADVISORY_KEY},
                    )

            # Released: the sweep retries and converges.
            assert await store.aapply_retention(WORK_A, keep_last=1) == 1
        finally:
            await engine.dispose()
            await holder_engine.dispose()
