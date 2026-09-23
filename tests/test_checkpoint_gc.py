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

The PostgreSQL proofs that need real isolation are gated on
``FORGE_PG_TEST_URL`` (the ADR-0017 failure-injection convention); the
SQLite approximations run always.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

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

            async def crashing_sweep(digests: list[str]) -> None:
                raise RuntimeError("killed between commit and unlink")

            original_sweep = store._asweep
            monkeypatch.setattr(store, "_asweep", crashing_sweep)

            with pytest.raises(RuntimeError, match="between commit and unlink"):
                await store.aapply_retention(WORK_A, keep_last=1)

            monkeypatch.setattr(store, "_asweep", original_sweep)
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
        def killed_before_sweep(digests: list[str]) -> None:
            raise OSError("killed before sweep")

        monkeypatch.setattr(store, "_sweep", killed_before_sweep)
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
# 6. DEDUPLICATED BLOBS — retention of one work never deletes a blob another
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
