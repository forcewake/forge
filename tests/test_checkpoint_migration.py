"""Q35-21: the operational checkpoint-metadata migration.

The procedure this suite pins: an installation whose checkpoint index
lives in per-work filesystem JSON adopts the postgres authority
(``checkpoint_metadata``) through EXPLICIT commands — inventory,
idempotent import, verification, a fenced cutover that leaves exactly
ONE backend accepting mutations, and a rollback gated by a clean verify
report for the CURRENT data state — without losing a paused work and
without ever selecting a checkpoint by timestamp.

Contract highlights under test:

- a paused work resumes after the migration from a NEWLY constructed
  repository instance (the fresh-process simulation), post-cutover;
- missing/corrupt blobs are REPORTED (entry unimportable, partial exit
  code), never invented, never silently skipped;
- re-importing adds no duplicate authority and alters no selection —
  the natural keys ``(work_id, checkpoint_id)`` make it a no-op;
- interrupting at every persistent boundary (inventory, mid-import,
  post-import pre-verify, post-verify pre-cutover) converges on resume;
- disagreeing JSON/DB active pointers DEMAND operator resolution — both
  actives reported, never a winner, never by time;
- post-cutover the fenced authority refuses mutations with the typed
  ``MutationsFencedError`` while immutable reads through the old root
  stay available;
- rollback refuses without a clean CURRENT verify report and restores
  the authority marker with one; the old inventory is never deleted;
- the doctor preflight reports migration coverage, active-pointer
  conflicts and the blob-volume topology heuristic.

Real PostgreSQL runs the same happy path when ``FORGE_PG_TEST_URL`` is
set (the FI convention; the URL's ``checkpoint_metadata`` rows are
deleted per test).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from forge import api_checkpoint_channel as api_channel
from forge.adaptive import checkpoint_migration as migration
from forge.adaptive.checkpoint_migration import (
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_REFUSED,
    MigrationCommandError,
    MutationsFencedError,
    cutover_fence,
    cutover_in_progress,
    enforce_authority_marker,
    migration_dir,
    read_authority_marker,
    run_cutover,
    run_import,
    run_rollback,
    run_verify,
    scan_inventory,
)
from forge.adaptive.checkpoint_repository import (
    CheckpointPins,
    FilesystemCheckpointRepository,
    PostgresCheckpointRepository,
)
from forge.adaptive.wiring import OperatorControlService
from forge.models.base import Base

WORKS_DEFAULT: dict[str, list[tuple[int, dict[str, bytes]]]] = {
    "wp-a": [(1, {"a.txt": b"alpha\n"}), (5, {"a.txt": b"v5\n"})]
}


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


def _db_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'metadata.db'}"


async def _factory_over(url: str) -> tuple[Any, async_sessionmaker[AsyncSession]]:
    """A fresh engine + factory over a file DB, schema in place."""
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed(root: Path, works: dict[str, list[tuple[int, dict[str, bytes]]]]) -> dict[str, str]:
    """Land checkpoints through the FILESYSTEM authority; return work -> active id."""
    repository = FilesystemCheckpointRepository(root)
    actives: dict[str, str] = {}
    for work_id, specs in works.items():
        best: tuple[int, str] | None = None
        for sequence, files in specs:
            manifest, blobs, checkpoint_id = _checkpoint(work_id, files, sequence)
            await repository.put(work_id, checkpoint_id, manifest, blobs)
            key = (sequence, checkpoint_id)
            if best is None or key > best:
                best = key
        assert best is not None
        actives[work_id] = best[1]
    return actives


async def _lab_async(
    tmp_path: Path, *, works: dict[str, list[tuple[int, dict[str, bytes]]]] | None = None
) -> tuple[Path, str, dict[str, str], Any, async_sessionmaker[AsyncSession]]:
    """Seeded → imported → verified clean, for async tests (functions, no CLI)."""
    root = tmp_path / "store"
    actives = await _seed(root, works or WORKS_DEFAULT)
    url = _db_url(tmp_path)
    engine, factory = await _factory_over(url)
    _report, exit_code = await run_import(root, factory)
    assert exit_code == EXIT_OK
    _verify, verify_exit = await run_verify(root, factory, out=migration_dir(root) / "verify.json")
    assert verify_exit == EXIT_OK
    return root, url, actives, engine, factory


def _lab_sync(
    tmp_path: Path,
    *,
    verify: bool = True,
    works: dict[str, list[tuple[int, dict[str, bytes]]]] | None = None,
) -> tuple[Path, str, dict[str, str]]:
    """The CLI-driven lab for sync tests: seed → inventory → import [→ verify]."""
    root = tmp_path / "store"
    actives = asyncio.run(_seed(root, works or WORKS_DEFAULT))
    url = _db_url(tmp_path)
    assert migration.main(["--root", str(root), "inventory"]) == EXIT_OK
    assert migration.main(["--root", str(root), "import", "--database-url", url]) == EXIT_OK
    if verify:
        assert migration.main(["--root", str(root), "verify", "--database-url", url]) == EXIT_OK
    return root, url, actives


async def _row_count(factory: async_sessionmaker[AsyncSession]) -> int:
    from sqlalchemy import func, select

    from forge.api_checkpoint_channel import CheckpointMetadataRow

    async with factory() as session:
        return int(await session.scalar(select(func.count()).select_from(CheckpointMetadataRow)))


async def _db_active(
    root: Path, factory: async_sessionmaker[AsyncSession], work_id: str
) -> str | None:
    entry = await PostgresCheckpointRepository(root, factory).entry(work_id)
    return str(entry["checkpoint_id"]) if entry else None


# ---------------------------------------------------------------------------
# inventory — the explicit source manifest
# ---------------------------------------------------------------------------


class TestInventory:
    async def test_the_structured_report_names_works_entries_and_actives(self, tmp_path):
        root = tmp_path / "store"
        actives = await _seed(root, {"wp-a": [(2, {"a": b"one\n"}), (7, {"a": b"seven\n"})]})

        report = scan_inventory(root)

        assert report["schema"] == migration.INVENTORY_SCHEMA
        assert report["summary"]["works"] == 1
        assert report["summary"]["checkpoints"] == 2
        assert report["summary"]["importable_entries"] == 2
        assert report["summary"]["unimportable_entries"] == 0
        (work,) = report["works"]
        assert work["work_id"] == "wp-a"
        assert work["active"]["checkpoint_id"] == actives["wp-a"]
        assert work["active"]["sequence"] == 7  # highest sequence, never last arrival

    async def test_a_missing_blob_is_listed_and_marks_the_entry_unimportable(self, tmp_path):
        root = tmp_path / "store"
        await _seed(root, {"wp-a": [(1, {"gone.txt": b"vanishing\n"})]})
        blob = _digest(b"vanishing\n")
        (root / blob[:2] / blob).unlink()

        report = scan_inventory(root)

        assert report["summary"]["missing_blobs"] == 1
        (entry,) = report["works"][0]["entries"]
        assert entry["blobs"][blob] == "missing"
        assert entry["importable"] is False
        assert f"blob missing: {blob}" in entry["problems"]

    async def test_a_rotted_blob_is_corrupt_not_missing(self, tmp_path):
        root = tmp_path / "store"
        await _seed(root, {"wp-a": [(1, {"x": b"intact\n"})]})
        blob = _digest(b"intact\n")
        (root / blob[:2] / blob).write_bytes(b"rotted\n")

        report = scan_inventory(root)

        (entry,) = report["works"][0]["entries"]
        assert entry["blobs"][blob] == "corrupt"
        assert entry["importable"] is False
        assert report["summary"]["corrupt_blobs"] == 1

    async def test_an_unparsable_index_file_is_named_never_skipped(self, tmp_path):
        root = tmp_path / "store"
        await _seed(root, {"wp-a": [(1, {"a": b"one\n"})]})
        (root / "works" / "wp-bad.json").write_text("{not json")

        report = scan_inventory(root)

        assert len(report["unparsable_index_files"]) == 1
        assert "wp-bad.json" in report["unparsable_index_files"][0]
        assert report["summary"]["works"] == 1  # the good work is still inventoried

    async def test_pins_and_orphaned_pins_are_recorded(self, tmp_path):
        root = tmp_path / "store"
        actives = await _seed(root, {"wp-a": [(3, {"a": b"pinned\n"})]})
        pins = CheckpointPins(root)
        pins.add("wp-a", actives["wp-a"], "resume:note:1")
        pins.add("wp-orphan", "a" * 64, "hold")

        report = scan_inventory(root)

        (work,) = (w for w in report["works"] if w["work_id"] == "wp-a")
        assert work["pinned"] == [actives["wp-a"]]
        (orphan,) = (w for w in report["works"] if w["work_id"] == "wp-orphan")
        assert orphan["pinned"] == ["a" * 64]
        assert report["orphaned_pins"] == [{"work_id": "wp-orphan", "checkpoint_ids": ["a" * 64]}]


# ---------------------------------------------------------------------------
# the happy path — inventory → import → verify → cutover → resume
# ---------------------------------------------------------------------------


class TestHappyPathCli:
    def test_full_command_sequence_then_cutover(self, tmp_path, capsys):
        root, url, _actives = _lab_sync(tmp_path)

        assert read_authority_marker(root) is None
        assert migration.main(["--root", str(root), "cutover"]) == EXIT_OK

        marker = read_authority_marker(root)
        assert marker is not None and marker["authority"] == "postgres"
        assert marker["previous_authority"] == "filesystem"
        assert marker["verify_report"] == str(migration_dir(root) / "verify.json")
        assert "FORGE_CHECKPOINT_DURABILITY=postgres" in capsys.readouterr().out
        # The old inventory is never deleted.
        assert (root / "works" / "wp-a.json").is_file()

    def test_each_command_writes_its_report(self, tmp_path):
        root, url, _actives = _lab_sync(tmp_path)

        assert (migration_dir(root) / "inventory.json").is_file()
        assert (migration_dir(root) / "import.json").is_file()
        assert (migration_dir(root) / "verify.json").is_file()
        summary = json.loads((migration_dir(root) / "import.json").read_text())["summary"]
        assert summary["imported"] == 2  # both checkpoints of wp-a


class TestPausedWorkResumesPostMigration:
    async def test_a_new_process_resumes_the_paused_work_after_cutover(self, tmp_path):
        root, url, actives, engine, factory = await _lab_async(tmp_path)
        try:
            assert run_cutover(root)["authority"] == "postgres"
        finally:
            await engine.dispose()

        # The NEW process: a fresh engine/factory and repository instance —
        # nothing of the migration's wiring survives except the data.
        engine_b, factory_b = await _factory_over(url)
        try:
            repository = PostgresCheckpointRepository(root, factory_b)
            service = OperatorControlService(checkpoint_repository=repository)

            assert await service.resume("wp-a", "human:op", "note:1") is True

            (resume,) = [c for c in service.mailbox.commands.values() if c.kind == "resume"]
            assert resume.payload["checkpoint_ref"] == f"wp-a@{actives['wp-a']}"
            assert resume.payload["checkpoint_sequence"] == 5
            # The authorized resume pinned its exact checkpoint (Q35-05).
            pinned = await repository.pins("wp-a")
            assert any(p["checkpoint_id"] == actives["wp-a"] for p in pinned)
        finally:
            await engine_b.dispose()


# ---------------------------------------------------------------------------
# idempotent re-import — no duplicate authority, no altered selection
# ---------------------------------------------------------------------------


class TestIdempotentImport:
    async def test_re_import_is_a_no_op(self, tmp_path):
        root = tmp_path / "store"
        actives = await _seed(root, {"wp-a": [(1, {"a": b"one\n"}), (4, {"a": b"four\n"})]})
        engine, factory = await _factory_over(_db_url(tmp_path))

        first, first_exit = await run_import(root, factory)
        rows_before = await _row_count(factory)
        active_before = await _db_active(root, factory, "wp-a")
        second, second_exit = await run_import(root, factory)

        assert first_exit == EXIT_OK and second_exit == EXIT_OK
        assert first["summary"]["imported"] == 2
        assert second["summary"]["imported"] == 0
        assert second["summary"]["skipped_already_present"] == 2
        assert await _row_count(factory) == rows_before  # no duplicate authority
        assert await _db_active(root, factory, "wp-a") == active_before == actives["wp-a"]
        await engine.dispose()

    async def test_import_keeps_the_history_an_aggressive_policy_would_drop(self, tmp_path):
        # A deployment with retention configured: the migration must still
        # import the FULL history — retention is the operator's posture for
        # the authority that will own the index, applied post-cutover.
        root = tmp_path / "store"
        await _seed(
            root, {"wp-a": [(1, {"a": b"one\n"}), (2, {"a": b"two\n"}), (9, {"a": b"nine\n"})]}
        )
        engine, factory = await _factory_over(_db_url(tmp_path))

        report, exit_code = await run_import(root, factory)

        assert exit_code == EXIT_OK
        assert report["summary"]["imported"] == 3  # nothing dropped by policy
        assert await _row_count(factory) == 3
        await engine.dispose()


# ---------------------------------------------------------------------------
# missing blob → reported, partial, the rest imported
# ---------------------------------------------------------------------------


class TestMissingBlobPartialImport:
    async def test_partial_import_reports_and_continues(self, tmp_path):
        root = tmp_path / "store"
        await _seed(
            root,
            {
                "wp-a": [(1, {"a": b"alpha-one\n"})],
                "wp-b": [(1, {"b": b"beta-one\n"})],
                "wp-c": [(1, {"c": b"gamma-one\n"})],
            },
        )
        blob = _digest(b"beta-one\n")
        (root / blob[:2] / blob).unlink()
        engine, factory = await _factory_over(_db_url(tmp_path))

        report, exit_code = await run_import(root, factory)

        assert exit_code == EXIT_PARTIAL
        (unimportable,) = report["unimportable"]
        assert unimportable["work_id"] == "wp-b"
        assert f"blob missing: {blob}" in unimportable["problems"]
        assert {entry["work_id"] for entry in report["imported"]} == {"wp-a", "wp-c"}
        # And verify refuses to call this state clean.
        verify, verify_exit = await run_verify(root, factory)
        assert verify_exit == EXIT_REFUSED
        assert verify["clean"] is False
        assert verify["metrics"][migration.METRIC_COVERAGE]["missing_in_database"] >= 1
        await engine.dispose()

    def test_the_cli_exit_code_reflects_partial(self, tmp_path, capsys):
        root = tmp_path / "store"
        asyncio.run(_seed(root, {"wp-a": [(1, {"a": b"aa\n"})], "wp-b": [(1, {"b": b"bb\n"})]}))
        blob = _digest(b"bb\n")
        (root / blob[:2] / blob).unlink()

        code = migration.main(["--root", str(root), "import", "--database-url", _db_url(tmp_path)])

        assert code == EXIT_PARTIAL
        assert "unimportable wp-b@" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# interrupt at each persistent boundary → resume converges
# ---------------------------------------------------------------------------


class TestInterruptConvergence:
    async def test_interrupted_mid_import_converges_on_rerun(self, tmp_path):
        root = tmp_path / "store"
        await _seed(root, {"wp-a": [(1, {"a": b"one\n"})], "wp-b": [(1, {"b": b"two\n"})]})
        engine, factory = await _factory_over(_db_url(tmp_path))

        with pytest.raises(migration._InterruptedMigration):
            await run_import(root, factory, fail_after=1)
        assert await _row_count(factory) == 1  # exactly the first landing survived

        report, exit_code = await run_import(root, factory)
        verify, verify_exit = await run_verify(root, factory)

        assert exit_code == EXIT_OK
        assert report["summary"]["imported"] == 1
        assert report["summary"]["skipped_already_present"] == 1  # the crash's survivor
        assert await _row_count(factory) == 2  # no duplicates from the resume
        assert verify_exit == EXIT_OK and verify["clean"] is True
        await engine.dispose()

    async def test_a_stale_inventory_leaves_a_gap_verify_names_and_rerun_heals(self, tmp_path):
        # Crash at the INVENTORY boundary: the report was taken, then the
        # store moved on. Importing the stale manifest misses the new
        # checkpoint — verify REFUSES (the gap is visible, not guessed
        # over) — and a fresh import converges.
        root = tmp_path / "store"
        await _seed(root, {"wp-a": [(1, {"a": b"one\n"})]})
        engine, factory = await _factory_over(_db_url(tmp_path))
        stale_path = tmp_path / "stale-inventory.json"
        stale_path.write_text(json.dumps(scan_inventory(root)))

        late_manifest, late_blobs, late_id = _checkpoint("wp-a", {"a": b"late\n"}, 8)
        await FilesystemCheckpointRepository(root).put("wp-a", late_id, late_manifest, late_blobs)

        stale, stale_exit = await run_import(root, factory, inventory_path=stale_path)
        verify, verify_exit = await run_verify(root, factory)

        assert stale_exit == EXIT_OK
        assert all(entry["checkpoint_id"] != late_id for entry in stale["imported"])
        assert verify_exit == EXIT_REFUSED

        fresh, fresh_exit = await run_import(root, factory)
        verify2, verify2_exit = await run_verify(root, factory)

        assert fresh_exit == EXIT_OK
        assert any(entry["checkpoint_id"] == late_id for entry in fresh["imported"])
        assert verify2_exit == EXIT_OK and verify2["clean"] is True
        await engine.dispose()

    def test_post_import_pre_verify_the_cutover_refuses_until_verified(self, tmp_path):
        root, url, _actives = _lab_sync(tmp_path, verify=False)

        assert migration.main(["--root", str(root), "cutover"]) == EXIT_REFUSED
        assert read_authority_marker(root) is None  # nothing flipped

        assert migration.main(["--root", str(root), "verify", "--database-url", url]) == EXIT_OK
        assert migration.main(["--root", str(root), "cutover"]) == EXIT_OK
        assert read_authority_marker(root)["authority"] == "postgres"

    def test_post_verify_pre_cutover_the_old_authority_still_stands(self, tmp_path):
        root, url, _actives = _lab_sync(tmp_path)

        # Not yet cut over: no marker exists, the old authority still accepts.
        assert read_authority_marker(root) is None
        manifest, blobs, checkpoint_id = _checkpoint("wp-new", {"n": b"new\n"}, 1)

        async def _put() -> None:
            await FilesystemCheckpointRepository(root).put("wp-new", checkpoint_id, manifest, blobs)

        asyncio.run(_put())
        assert (root / "works" / "wp-new.json").is_file()

        # ... and the un-imported work is exactly what the cutover gate catches.
        assert migration.main(["--root", str(root), "cutover"]) == EXIT_REFUSED
        assert migration.main(["--root", str(root), "import", "--database-url", url]) == EXIT_OK
        assert migration.main(["--root", str(root), "verify", "--database-url", url]) == EXIT_OK
        assert migration.main(["--root", str(root), "cutover"]) == EXIT_OK


# ---------------------------------------------------------------------------
# disagreeing active pointers — operator resolution, never timestamps
# ---------------------------------------------------------------------------


class TestDisagreeingActivePointers:
    async def test_both_actives_reported_and_the_cutover_refused(self, tmp_path):
        root = tmp_path / "store"
        actives = await _seed(root, {"wp-a": [(5, {"a": b"five\n"})]})
        engine, factory = await _factory_over(_db_url(tmp_path))
        await run_import(root, factory)

        # A DIFFERENT checkpoint lands in the database only (a drifted or
        # racing writer): sequence 9 outranks the index's 5.
        drift_manifest, drift_blobs, drift_id = _checkpoint("wp-a", {"a": b"drift\n"}, 9)
        await PostgresCheckpointRepository(root, factory).put(
            "wp-a", drift_id, drift_manifest, drift_blobs
        )

        report, exit_code = await run_verify(root, factory, out=migration_dir(root) / "verify.json")
        conflicts = report["metrics"][migration.METRIC_CONFLICTS]

        assert exit_code == EXIT_REFUSED
        assert conflicts["count"] == 1
        (conflict,) = conflicts["active_disagreements"]
        assert conflict["filesystem_active"] == actives["wp-a"]
        assert conflict["database_active"] == drift_id
        assert "operator decision required" in conflict["resolution"]
        assert "never selected by time" in conflict["resolution"]  # stated, not implied
        # No winner was recorded anywhere in the work's report.
        (work,) = (w for w in report["works"] if w["work_id"] == "wp-a")
        assert work["filesystem"]["active"] == actives["wp-a"]
        assert work["database"]["active"] == drift_id

        with pytest.raises(MigrationCommandError, match="not clean"):
            run_cutover(root)  # the gate holds on a conflict
        await engine.dispose()

    def test_the_cutover_refuses_a_verify_report_taken_for_another_root(self, tmp_path):
        root_a, _url_a, _actives = _lab_sync(tmp_path / "a")
        root_b, _url_b, _actives = _lab_sync(tmp_path / "b")

        with pytest.raises(MigrationCommandError, match="store root"):
            run_cutover(root_a, verify_report=migration_dir(root_b) / "verify.json")

        assert read_authority_marker(root_a) is None


# ---------------------------------------------------------------------------
# post-cutover: exactly one mutator; reads through the old root
# ---------------------------------------------------------------------------


class TestPostCutoverSingleMutator:
    async def test_the_fenced_authority_refuses_mutations_with_the_specific_error(self, tmp_path):
        root, url, actives, engine, _factory = await _lab_async(tmp_path)
        try:
            run_cutover(root)
            fs = enforce_authority_marker(FilesystemCheckpointRepository(root), root)
            manifest, blobs, checkpoint_id = _checkpoint("wp-a", {"a": b"post\n"}, 6)

            with pytest.raises(MutationsFencedError, match="authority marker"):
                await fs.put("wp-a", checkpoint_id, manifest, blobs)
            with pytest.raises(MutationsFencedError, match="authority marker"):
                await fs.apply_retention("wp-a", 1)

            # ... while the immutable reads through the old root stay available.
            entry = await fs.entry("wp-a")
            assert entry is not None and entry["checkpoint_id"] == actives["wp-a"]
            served = await fs.read("wp-a")
            assert served is not None
            served_manifest, _blobs = served
            expected_manifest, _, _ = _checkpoint("wp-a", {"a.txt": b"v5\n"}, 5)
            assert served_manifest == expected_manifest
        finally:
            await engine.dispose()

    async def test_the_owning_authority_accepts_mutations_post_cutover(self, tmp_path):
        root, url, _actives, engine, factory = await _lab_async(tmp_path)
        try:
            run_cutover(root)
            pg = enforce_authority_marker(PostgresCheckpointRepository(root, factory), root)
            manifest, blobs, checkpoint_id = _checkpoint("wp-a", {"a": b"post-cutover\n"}, 6)

            await pg.put("wp-a", checkpoint_id, manifest, blobs)

            entry = await pg.entry("wp-a")
            assert entry is not None and entry["checkpoint_id"] == checkpoint_id
        finally:
            await engine.dispose()

    async def test_a_cutover_in_progress_fences_both_sides(self, tmp_path):
        root, url, _actives, engine, factory = await _lab_async(tmp_path)
        try:
            fs = enforce_authority_marker(FilesystemCheckpointRepository(root), root)
            pg = enforce_authority_marker(PostgresCheckpointRepository(root, factory), root)
            manifest, blobs, checkpoint_id = _checkpoint("wp-a", {"a": b"fenced\n"}, 6)

            with cutover_fence(root):
                assert cutover_in_progress(root)
                with pytest.raises(MutationsFencedError, match="cutover is in progress"):
                    await fs.put("wp-a", checkpoint_id, manifest, blobs)
                with pytest.raises(MutationsFencedError, match="cutover is in progress"):
                    await pg.put("wp-a", checkpoint_id, manifest, blobs)
                with pytest.raises(MigrationCommandError, match="single-flight"):
                    run_cutover(root)  # the flip itself waits for the fence

            assert not cutover_in_progress(root)
            run_cutover(root)  # converged after the fence released
        finally:
            await engine.dispose()

    async def test_a_second_cutover_to_the_same_authority_refuses(self, tmp_path):
        root, url, _actives, engine, _factory = await _lab_async(tmp_path)
        try:
            run_cutover(root)

            with pytest.raises(MigrationCommandError, match="already names"):
                run_cutover(root)
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# rollback — gated by a clean verify report for the CURRENT data state
# ---------------------------------------------------------------------------


class TestRollback:
    async def test_a_stale_clean_report_refuses_and_the_marker_stands(self, tmp_path):
        root, url, actives, engine, factory = await _lab_async(tmp_path)
        try:
            run_cutover(root)
            # Post-cutover upload: exists ONLY in the database.
            manifest, blobs, checkpoint_id = _checkpoint("wp-a", {"a": b"post\n"}, 6)
            await PostgresCheckpointRepository(root, factory).put(
                "wp-a", checkpoint_id, manifest, blobs
            )
        finally:
            await engine.dispose()

        stale = migration_dir(root) / "verify.json"  # taken BEFORE the flip
        with pytest.raises(MigrationCommandError, match="predates the current authority state"):
            run_rollback(root, verify_report=stale)

        # The fresh report is current but NOT clean: the DB-only entry blocks.
        engine, factory = await _factory_over(url)
        try:
            _report, verify_exit = await run_verify(
                root, factory, out=migration_dir(root) / "verify.json"
            )
        finally:
            await engine.dispose()
        assert verify_exit == EXIT_REFUSED
        with pytest.raises(MigrationCommandError, match="not clean"):
            run_rollback(root, verify_report=migration_dir(root) / "verify.json")
        assert read_authority_marker(root)["authority"] == "postgres"  # unchanged

    async def test_the_documented_recovery_path_then_rollback_restores_the_marker(self, tmp_path):
        root, url, _actives, engine, factory = await _lab_async(tmp_path)
        try:
            run_cutover(root)
            manifest, blobs, _id = _checkpoint("wp-a", {"a": b"post\n"}, 6)
            await PostgresCheckpointRepository(root, factory).put("wp-a", _id, manifest, blobs)

            # The recovery: reverse import → verify → rollback.
            reverse, reverse_exit = await run_import(root, factory, reverse=True)
            assert reverse_exit == EXIT_OK
            assert reverse["summary"]["imported"] == 1
            index = json.loads((root / "works" / "wp-a.json").read_text())
            assert any(e["checkpoint_id"] == _id for e in index["checkpoints"])

            _verify, verify_exit = await run_verify(
                root, factory, out=migration_dir(root) / "verify.json"
            )
            assert verify_exit == EXIT_OK

            marker = run_rollback(root, verify_report=migration_dir(root) / "verify.json")
            assert marker["authority"] == "filesystem"
            assert marker["previous_authority"] == "postgres"
        finally:
            await engine.dispose()

    async def test_post_rollback_the_filesystem_authority_accepts_mutations_again(self, tmp_path):
        root, url, _actives, engine, factory = await _lab_async(tmp_path)
        try:
            run_cutover(root)
            # A post-cutover verify (the rollback gate's CURRENT-data-state half).
            await run_verify(root, factory, out=migration_dir(root) / "verify.json")
            run_rollback(root, verify_report=migration_dir(root) / "verify.json")

            fs = enforce_authority_marker(FilesystemCheckpointRepository(root), root)
            pg = enforce_authority_marker(PostgresCheckpointRepository(root, factory), root)
            manifest, blobs, checkpoint_id = _checkpoint("wp-a", {"a": b"back\n"}, 7)

            await fs.put("wp-a", checkpoint_id, manifest, blobs)  # accepted again
            with pytest.raises(MutationsFencedError, match="authority marker"):
                await pg.put("wp-a", checkpoint_id, manifest, blobs)  # the DB side fences now
        finally:
            await engine.dispose()

    async def test_rollback_without_a_marker_refuses(self, tmp_path):
        root, url, _actives, engine, _factory = await _lab_async(tmp_path)  # verified, NOT flipped
        try:
            with pytest.raises(MigrationCommandError, match="no authority marker"):
                run_rollback(root, verify_report=migration_dir(root) / "verify.json")
        finally:
            await engine.dispose()

    async def test_the_old_inventory_is_never_deleted_across_the_full_cycle(self, tmp_path):
        root, url, _actives, engine, factory = await _lab_async(tmp_path)
        index = root / "works" / "wp-a.json"
        before = index.read_bytes()
        try:
            run_cutover(root)
            await run_verify(root, factory, out=migration_dir(root) / "verify.json")
            run_rollback(root, verify_report=migration_dir(root) / "verify.json")
        finally:
            await engine.dispose()

        assert index.read_bytes() == before  # untouched across cutover + rollback

    def test_the_cli_rollback_gates_the_same_way(self, tmp_path, capsys):
        root, url, _actives = _lab_sync(tmp_path)
        assert migration.main(["--root", str(root), "cutover"]) == EXIT_OK

        # The pre-cutover report predates the flip: refused even though clean.
        code = migration.main(
            [
                "--root",
                str(root),
                "rollback",
                "--verify-report",
                str(migration_dir(root) / "verify.json"),
            ]
        )
        assert code == EXIT_REFUSED
        assert read_authority_marker(root)["authority"] == "postgres"

        assert migration.main(["--root", str(root), "verify", "--database-url", url]) == EXIT_OK
        assert (
            migration.main(
                [
                    "--root",
                    str(root),
                    "rollback",
                    "--verify-report",
                    str(migration_dir(root) / "verify.json"),
                ]
            )
            == EXIT_OK
        )
        assert read_authority_marker(root)["authority"] == "filesystem"
        assert "FORGE_CHECKPOINT_DURABILITY=best_effort" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# the doctor preflight — coverage, conflicts, topology
# ---------------------------------------------------------------------------


class TestPreflight:
    async def test_coverage_counts_not_yet_migrated_works_under_postgres(self, tmp_path):
        root = tmp_path / "store"
        await _seed(root, {"wp-a": [(1, {"a": b"one\n"})]})
        engine, factory = await _factory_over(_db_url(tmp_path))
        await run_import(root, factory)
        await _seed(root, {"wp-late": [(1, {"late": b"late\n"})]})

        views = await migration.preflight(
            root, env={api_channel.DURABILITY_ENV: "postgres"}, database_url=_db_url(tmp_path)
        )

        assert views["coverage"]["unmigrated_works"] == ["wp-late"]
        assert views["coverage"]["works_database"] == 1
        assert views["metrics"][migration.METRIC_COVERAGE] is views["coverage"]
        await engine.dispose()

    async def test_coverage_passes_when_nothing_is_left_behind(self, tmp_path):
        root, url, _actives, engine, _factory = await _lab_async(tmp_path)

        views = await migration.preflight(
            root, env={api_channel.DURABILITY_ENV: "postgres"}, database_url=url
        )
        assert views["coverage"]["unmigrated_works"] == []

        views = await migration.preflight(root, env={}, database_url="")  # best_effort, no DB
        assert views["coverage"]["mode"] == "best_effort"
        assert views["coverage"]["database_note"] == "no DATABASE_URL"
        await engine.dispose()

    async def test_conflicts_demand_operator_resolution_never_a_winner(self, tmp_path):
        root = tmp_path / "store"
        actives = await _seed(root, {"wp-a": [(5, {"a": b"five\n"})]})
        engine, factory = await _factory_over(_db_url(tmp_path))
        await run_import(root, factory)
        drift_manifest, drift_blobs, drift_id = _checkpoint("wp-a", {"a": b"drift\n"}, 9)
        await PostgresCheckpointRepository(root, factory).put(
            "wp-a", drift_id, drift_manifest, drift_blobs
        )

        views = await migration.preflight(root, env={}, database_url=_db_url(tmp_path))

        assert views["conflicts"]["count"] == 1
        (conflict,) = views["conflicts"]["works"]
        assert conflict["filesystem_active"] == actives["wp-a"]
        assert conflict["database_active"] == drift_id
        assert "never timestamp selection" in conflict["resolution"]
        await engine.dispose()

    async def test_post_cutover_drift_is_explained_not_a_conflict(self, tmp_path):
        root, url, _actives, engine, factory = await _lab_async(tmp_path)
        try:
            # Post-cutover upload: DB outranks the old index, old active still present.
            manifest, blobs, checkpoint_id = _checkpoint("wp-a", {"a": b"newer\n"}, 6)
            await PostgresCheckpointRepository(root, factory).put(
                "wp-a", checkpoint_id, manifest, blobs
            )
        finally:
            await engine.dispose()

        views = await migration.preflight(
            root, env={api_channel.DURABILITY_ENV: "postgres"}, database_url=url
        )

        assert views["conflicts"]["count"] == 0
        assert views["conflicts"]["post_cutover_drift"] == 1
        assert views["coverage"]["unmigrated_works"] == []

    async def test_the_topology_heuristic_flags_node_local_roots(self, tmp_path):
        under_tmp = await migration.preflight(
            root=tmp_path, env={api_channel.DURABILITY_ENV: "postgres"}
        )
        shared = await migration.preflight(
            root=Path("/srv/forge/checkpoints"), env={api_channel.DURABILITY_ENV: "postgres"}
        )

        assert under_tmp["topology"]["looks_node_local"] is True  # pytest tmp is per-machine
        assert shared["topology"]["looks_node_local"] is False  # assumed shared, named as such
        assert "ASSUMED shared" in shared["topology"]["heuristic"]
        assert migration._looks_node_local(Path("data/checkpoints")) is True  # relative


class TestDoctorPreflight:
    async def test_the_three_checks_with_their_verdicts(self, tmp_path, monkeypatch):
        root, url, _actives, engine, _factory = await _lab_async(tmp_path)
        await _seed(root, {"wp-late": [(1, {"late": b"late\n"})]})
        monkeypatch.setenv(api_channel.CHECKPOINT_STORE_DIR_ENV, str(root))
        monkeypatch.setenv(api_channel.DURABILITY_ENV, "postgres")

        from forge.doctor import check_checkpoint_authority

        results = await check_checkpoint_authority(SimpleNamespace(DATABASE_URL=url))
        by_name = {result.name: result for result in results}
        await engine.dispose()

        assert set(by_name) == {
            "checkpoint.migration_coverage",
            "checkpoint.pointer_conflicts",
            "checkpoint.blob_topology",
        }
        assert by_name["checkpoint.migration_coverage"].status == "warn"  # wp-late remains
        assert "wp-late" in by_name["checkpoint.migration_coverage"].detail
        assert "import" in by_name["checkpoint.migration_coverage"].detail
        assert by_name["checkpoint.pointer_conflicts"].status == "pass"
        assert by_name["checkpoint.blob_topology"].status == "warn"  # tmp_path looks node-local

    async def test_conflicting_pointers_fail_the_doctor_under_best_effort(
        self, tmp_path, monkeypatch
    ):
        url = _db_url(tmp_path)
        root = tmp_path / "store"
        await _seed(root, {"wp-a": [(5, {"a": b"five\n"})]})
        engine, factory = await _factory_over(url)
        await run_import(root, factory)
        drift_manifest, drift_blobs, drift_id = _checkpoint("wp-a", {"a": b"drift\n"}, 9)
        await PostgresCheckpointRepository(root, factory).put(
            "wp-a", drift_id, drift_manifest, drift_blobs
        )
        await engine.dispose()
        monkeypatch.setenv(api_channel.CHECKPOINT_STORE_DIR_ENV, str(root))

        from forge.doctor import check_checkpoint_authority

        results = await check_checkpoint_authority(SimpleNamespace(DATABASE_URL=url))
        by_name = {result.name for result in results}

        assert "checkpoint.pointer_conflicts" in by_name
        conflicts = next(r for r in results if r.name == "checkpoint.pointer_conflicts")
        assert conflicts.status == "fail"  # best_effort: the migration window is open
        assert "operator decision required" in conflicts.detail

    async def test_run_checks_carries_the_preflight(self, tmp_path, monkeypatch):
        monkeypatch.setenv(api_channel.CHECKPOINT_STORE_DIR_ENV, str(tmp_path / "empty-store"))

        from forge.doctor import CheckResult, run_checks

        async def stub(settings, project_id=None):
            return CheckResult("stub", "pass", "")

        async def stub_list(settings, project_id=None):
            return [CheckResult("stub", "pass", "")]

        for name in (
            "check_gitlab",
            "check_bot_token",
            "check_redis",
            "check_database",
            "check_litellm",
        ):
            monkeypatch.setattr(f"forge.doctor.{name}", stub)
        monkeypatch.setattr("forge.doctor.check_legacy_credential_window", stub_list)

        names = [result.name for result in await run_checks(SimpleNamespace(DATABASE_URL=""))]

        for expected in (
            "checkpoint.migration_coverage",
            "checkpoint.pointer_conflicts",
            "checkpoint.blob_topology",
        ):
            assert expected in names


# ---------------------------------------------------------------------------
# CLI refusals
# ---------------------------------------------------------------------------


class TestCliRefusals:
    def test_import_without_a_database_url_refuses(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)

        assert migration.main(["--root", str(tmp_path), "import"]) == EXIT_REFUSED

    def test_cutover_without_any_verify_report_refuses(self, tmp_path):
        assert migration.main(["--root", str(tmp_path), "cutover"]) == EXIT_REFUSED

    def test_rollback_requires_the_verify_report_argument(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as raised:
            migration.main(["--root", str(tmp_path), "rollback"])

        assert raised.value.code == 2  # argparse: --verify-report is the gate's spelling


# ---------------------------------------------------------------------------
# PG-gated: real PostgreSQL, the FI convention
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("FORGE_PG_TEST_URL"),
    reason=(
        "FORGE_PG_TEST_URL not set — the Q35-21 real-Postgres migration proof "
        "runs only against a disposable database (rows are deleted per test)"
    ),
)
class TestMigrationOverRealPostgres:
    async def _lab(self) -> tuple[Any, async_sessionmaker[AsyncSession]]:
        url = os.environ["FORGE_PG_TEST_URL"]
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(delete(api_channel.CheckpointMetadataRow))
        return engine, async_sessionmaker(engine, expire_on_commit=False)

    async def _fresh_factory(self) -> tuple[Any, async_sessionmaker[AsyncSession]]:
        """A SECOND instance's wiring over the same database — no reset."""
        engine = create_async_engine(os.environ["FORGE_PG_TEST_URL"])
        return engine, async_sessionmaker(engine, expire_on_commit=False)

    async def test_full_happy_path_and_a_fresh_instance_resume(self, tmp_path):
        engine, factory = await self._lab()
        root = tmp_path / "store"
        actives = await _seed(root, {"wp-pg": [(2, {"a": b"pg-two\n"}), (6, {"a": b"pg-six\n"})]})
        try:
            imported, import_exit = await run_import(root, factory)
            assert import_exit == EXIT_OK
            assert imported["summary"]["imported"] == 2

            verified, verify_exit = await run_verify(
                root, factory, out=migration_dir(root) / "verify.json"
            )
            assert verify_exit == EXIT_OK and verified["clean"] is True

            assert run_cutover(root)["authority"] == "postgres"

            # The fresh instance: its own factory over the same database.
            engine_b, factory_b = await self._fresh_factory()
            try:
                reader = PostgresCheckpointRepository(root, factory_b)
                entry = await reader.entry("wp-pg")
                assert entry is not None and entry["checkpoint_id"] == actives["wp-pg"]
                service = OperatorControlService(checkpoint_repository=reader)
                assert await service.resume("wp-pg", "human:op", "note:pg") is True
                (resume,) = [c for c in service.mailbox.commands.values() if c.kind == "resume"]
                assert resume.payload["checkpoint_ref"] == f"wp-pg@{actives['wp-pg']}"
            finally:
                await engine_b.dispose()
        finally:
            await engine.dispose()

    async def test_partial_import_over_real_postgres(self, tmp_path):
        engine, factory = await self._lab()
        root = tmp_path / "store"
        await _seed(root, {"wp-keep": [(1, {"k": b"keep\n"})], "wp-lose": [(1, {"l": b"lose\n"})]})
        blob = _digest(b"lose\n")
        (root / blob[:2] / blob).unlink()
        try:
            report, exit_code = await run_import(root, factory)

            assert exit_code == EXIT_PARTIAL
            assert {entry["work_id"] for entry in report["imported"]} == {"wp-keep"}
            assert {entry["work_id"] for entry in report["unimportable"]} == {"wp-lose"}
        finally:
            await engine.dispose()
