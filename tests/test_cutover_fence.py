"""R36-05 (issue #264): the cutover fence at the STANDARD composition root.

AT-06's core, driven the way the trace demands — ordinary repository
instances composed through the ONE standard
:func:`forge.adaptive.checkpoint_repository.resolve_repository` (no
hand-wrapped repository objects), the REAL migration CLI flipping the
marker, and the actual HTTP upload route:

- an OLD-authority process (composed before the flip, still running)
  has its NEXT metadata mutation refused with the typed
  :class:`MutationsFencedError` naming configured-vs-active, while its
  immutable reads stay available and distinguishable;
- a NEW-authority process (postgres configured, composed the same way)
  reads every imported ACTIVE and PINNED checkpoint with NO JSON
  mirror, and its mutations land in the database only;
- the fence check runs BEFORE the store's volume-wide GC lock — a
  fence refusal never waits on a contended volume (bounded both ways),
  and the one residual window (a mutation that already passed the
  check when the flip lands) is pinned honestly: drained-offline
  cutover is the documented first mode, never an online zero-downtime
  promise;
- startup observability reports
  ``migration.configured_vs_active_authority`` + the marker
  generation wherever the app composes the repository, and ``forge
  doctor`` renders the marker/configured/mismatch verdict;
- losing the target database after the cutover answers the TYPED
  unavailable — never a filesystem fallback.

Real PostgreSQL runs the two-instance AT-06 core when
``FORGE_PG_TEST_URL`` is set (the FI convention; rows are deleted per
test).
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from forge import api_checkpoint_channel as api_channel
from forge.adaptive import checkpoint_migration as migration
from forge.adaptive.checkpoint_migration import (
    EXIT_OK,
    MutationsFencedError,
    read_authority_marker,
)
from forge.adaptive.checkpoint_repository import (
    AUTHORITY_FILESYSTEM,
    AUTHORITY_POSTGRES,
    CheckpointRepositoryUnavailable,
    METRIC_CONFIGURED_VS_ACTIVE,
    PostgresCheckpointRepository,
    authority_state_report,
    enforce_authority_marker,
    resolve_repository,
)
from forge.models.base import Base

SECRET = "fence-test-secret"
WORK = "wp-at06"


# ---------------------------------------------------------------------------
# The lab: ordinary composition + the REAL CLI
# ---------------------------------------------------------------------------


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
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _old_env(root: Path) -> dict[str, str]:
    """The OLD deployment's environment: the filesystem authority."""
    return {
        api_channel.DURABILITY_ENV: "best_effort",
        api_channel.CHECKPOINT_STORE_DIR_ENV: str(root),
    }


def _new_env(root: Path) -> dict[str, str]:
    """The NEW deployment's environment: the postgres authority."""
    return {
        api_channel.DURABILITY_ENV: "postgres",
        api_channel.CHECKPOINT_STORE_DIR_ENV: str(root),
    }


async def _cli(*argv: str) -> int:
    """The REAL migration CLI, run off the test's event loop (its ``main``
    owns its loop through ``asyncio.run`` — a worker thread is exactly the
    separate process the trace simulates)."""
    return await asyncio.to_thread(migration.main, list(argv))


async def _run(*steps: Any) -> None:
    """Await CLI steps in order inside one ``asyncio.run`` (sync-test arm)."""
    for step in steps:
        assert await step == EXIT_OK


class _Lab:
    """The AT-06 schedule: seed → import → verify through the real CLI.

    The two ``processes'' are two ordinary repository instances composed
    through :func:`resolve_repository` BEFORE the flip — exactly what an
    old API process and a newly started postgres process hold.
    """

    def __init__(self, root: Path, url: str) -> None:
        self.root = root
        self.url = url

    async def build(self, *, pinned: bool = True) -> None:
        # 1. the old deployment's data: two checkpoints, the older PINNED
        #    (an approved resume's exact reference).
        self.old = resolve_repository(env=_old_env(self.root), root=self.root)
        manifest, blobs, checkpoint_id = _checkpoint(WORK, {"a.txt": b"one\n"}, 1)
        await self.old.put(WORK, checkpoint_id, manifest, blobs)
        self.pinned_id = checkpoint_id
        if pinned:
            await self.old.pin(WORK, checkpoint_id, "resume:at06:1")
        newer, newer_blobs, newer_id = _checkpoint(WORK, {"a.txt": b"five\n"}, 5)
        await self.old.put(WORK, newer_id, newer, newer_blobs)
        self.active_id = newer_id
        # 2. the NEW process, composed the same ordinary way BEFORE the flip.
        self.engine, self.factory = await _factory_over(self.url)
        self.new = resolve_repository(
            env=_new_env(self.root), session_factory=self.factory, root=self.root
        )
        # 3. the REAL CLI: inventory → import → verify (clean).
        assert await _cli("--root", str(self.root), "inventory") == EXIT_OK
        assert await _cli("--root", str(self.root), "import", "--database-url", self.url) == EXIT_OK
        assert await _cli("--root", str(self.root), "verify", "--database-url", self.url) == EXIT_OK

    async def cutover(self) -> None:
        """The REAL cutover CLI — the flip under its fence."""
        assert (
            await _cli("--root", str(self.root), "cutover", "--database-url", self.url) == EXIT_OK
        )

    async def dispose(self) -> None:
        await self.engine.dispose()


@pytest.fixture
async def lab(tmp_path: Path):
    laboratory = _Lab(tmp_path / "store", _db_url(tmp_path))
    await laboratory.build()
    try:
        yield laboratory
    finally:
        await laboratory.dispose()


# ---------------------------------------------------------------------------
# The standard composition carries the fence
# ---------------------------------------------------------------------------


class TestStandardCompositionCarriesTheFence:
    async def test_the_default_resolution_attaches_the_fence(self, lab):
        """A repository resolved through the composition point (the DEFAULT
        path) refuses its next mutation after the real cutover — no
        wrapper was composed by hand."""
        await lab.cutover()
        manifest, blobs, checkpoint_id = _checkpoint(WORK, {"a.txt": b"post\n"}, 6)

        with pytest.raises(MutationsFencedError, match="configured vs active"):
            await lab.old.put(WORK, checkpoint_id, manifest, blobs)

    async def test_the_fence_is_dormant_until_a_cutover_flips_the_marker(self, lab):
        manifest, blobs, checkpoint_id = _checkpoint(WORK, {"a.txt": b"pre\n"}, 4)

        await lab.old.put(WORK, checkpoint_id, manifest, blobs)  # allowed pre-flip

        assert read_authority_marker(lab.root) is None
        report = authority_state_report(env=_old_env(lab.root), root=lab.root)
        assert report["state"] == "unmarked"

    async def test_fenced_false_is_the_migration_tools_own_spelling(self, lab):
        await lab.cutover()
        raw = resolve_repository(env=_old_env(lab.root), root=lab.root, fenced=False)
        manifest, blobs, checkpoint_id = _checkpoint(WORK, {"a.txt": b"raw\n"}, 7)

        await raw.put(WORK, checkpoint_id, manifest, blobs)  # the operator's path, unfenced

        # ...and the wrapper over the SAME raw resolution shares the fence.
        wrapped = enforce_authority_marker(raw, lab.root)
        with pytest.raises(MutationsFencedError):
            await wrapped.put(WORK, checkpoint_id, manifest, blobs)

    async def test_a_cutover_in_progress_fences_both_composed_sides(self, lab):
        manifest, blobs, checkpoint_id = _checkpoint(WORK, {"a.txt": b"during\n"}, 6)
        with migration.cutover_fence(lab.root):
            with pytest.raises(MutationsFencedError, match="cutover is in progress"):
                await lab.old.put(WORK, checkpoint_id, manifest, blobs)
            with pytest.raises(MutationsFencedError, match="cutover is in progress"):
                await lab.new.put(WORK, checkpoint_id, manifest, blobs)

    async def test_the_delete_family_fences_too(self, lab):
        await lab.cutover()

        with pytest.raises(MutationsFencedError, match="configured vs active"):
            await lab.old.apply_retention(WORK, keep_last=1)

    async def test_pins_are_protection_not_the_retired_authority(self, lab):
        """The pin overlay is the shared blob-volume protection, never the
        retired metadata authority — pinning a checkpoint the RETIRED
        authority itself holds stays available (its retention is fenced,
        so nothing pinned can be deleted by the retired side)."""
        await lab.cutover()

        assert await lab.old.pin(WORK, lab.active_id, "resume:at06:2") is True
        assert any(
            p["checkpoint_id"] == lab.active_id and p["reason"] == "resume:at06:2"
            for p in await lab.old.pins(WORK)
        )


# ---------------------------------------------------------------------------
# AT-06 core — the two ordinary processes around the real cutover
# ---------------------------------------------------------------------------


class TestAt06Core:
    async def test_the_old_process_refuses_and_the_new_one_reads_everything(self, lab):
        await lab.cutover()

        # THE OLD PROCESS: its next metadata mutation is refused — the
        # running process switches behavior the moment the flip lands.
        manifest, blobs, checkpoint_id = _checkpoint(WORK, {"a.txt": b"post\n"}, 6)
        with pytest.raises(MutationsFencedError, match="authority marker") as raised:
            await lab.old.put(WORK, checkpoint_id, manifest, blobs)
        # The refusal names configured-vs-active, both spellings.
        message = str(raised.value)
        assert "postgres" in message and "filesystem" in message

        # THE NEW PROCESS: reads the imported ACTIVE checkpoint...
        entry = await lab.new.entry(WORK)
        assert entry is not None and entry["checkpoint_id"] == lab.active_id
        served = await lab.new.read(WORK)
        assert served is not None
        served_manifest, _blobs = served
        expected_manifest, _, _ = _checkpoint(WORK, {"a.txt": b"five\n"}, 5)
        assert served_manifest == expected_manifest
        # ...AND the PINNED exact checkpoint (the approved resume's ref).
        pinned_entry = await lab.new.entry(WORK, lab.pinned_id)
        assert pinned_entry is not None
        assert any(p["checkpoint_id"] == lab.pinned_id for p in await lab.new.pins(WORK))

        # The NEW process's own mutation lands in the database ONLY — no
        # JSON mirror is ever written by the postgres authority.
        await lab.new.put(WORK, checkpoint_id, manifest, blobs)
        rows = await PostgresCheckpointRepository(lab.root, lab.factory).entry(WORK)
        assert rows is not None and rows["checkpoint_id"] == checkpoint_id
        index = json.loads((lab.root / "works" / f"{WORK}.json").read_text())
        assert all(e["checkpoint_id"] != checkpoint_id for e in index["checkpoints"])

    async def test_reads_through_the_retired_process_stay_available_and_distinguishable(self, lab):
        await lab.cutover()

        entry = await lab.old.entry(WORK)
        assert entry is not None and entry["checkpoint_id"] == lab.active_id
        assert await lab.old.read(WORK) is not None
        # The read answers name the authority they came from — retired
        # history is explicitly read-only and DISTINGUISHABLE, never
        # masquerading as the active authority's answer.
        assert await lab.old.authority() == AUTHORITY_FILESYSTEM
        assert await lab.new.authority() == AUTHORITY_POSTGRES
        outcome = await lab.old.lookup_outcome(WORK)
        assert outcome.is_exact and outcome.authority == AUTHORITY_FILESYSTEM
        state = authority_state_report(env=_old_env(lab.root), root=lab.root)
        assert state["state"] == "mismatch"
        assert state["active_authority"] == AUTHORITY_POSTGRES
        assert state["configured_authority"] == AUTHORITY_FILESYSTEM

    async def test_rollback_restores_the_old_process_without_a_restart(self, lab):
        await lab.cutover()
        manifest, blobs, checkpoint_id = _checkpoint(WORK, {"a.txt": b"post\n"}, 6)

        # A post-cutover upload exists only in the database: the rollback
        # refuses until the reverse transfer + verification pass.
        await lab.new.put(WORK, checkpoint_id, manifest, blobs)
        assert (
            await _cli("--root", str(lab.root), "verify", "--database-url", lab.url)
            == migration.EXIT_REFUSED
        )
        assert (
            await _cli(
                "--root",
                str(lab.root),
                "rollback",
                "--database-url",
                lab.url,
                "--verify-report",
                str(migration.migration_dir(lab.root) / "verify.json"),
            )
            == migration.EXIT_REFUSED  # the not-clean report gates the flip
        )

        assert (
            await _cli("--root", str(lab.root), "import", "--database-url", lab.url, "--reverse")
            == EXIT_OK
        )
        assert await _cli("--root", str(lab.root), "verify", "--database-url", lab.url) == EXIT_OK
        assert (
            await _cli(
                "--root",
                str(lab.root),
                "rollback",
                "--database-url",
                lab.url,
                "--verify-report",
                str(migration.migration_dir(lab.root) / "verify.json"),
            )
            == EXIT_OK
        )

        # The still-running OLD process accepts mutations again — no
        # restart, exactly the fence's symmetric recovery.
        later, later_blobs, later_id = _checkpoint(WORK, {"a.txt": b"back\n"}, 7)
        await lab.old.put(WORK, later_id, later, later_blobs)
        with pytest.raises(MutationsFencedError):
            await lab.new.put(WORK, later_id, later, later_blobs)


# ---------------------------------------------------------------------------
# The actual HTTP upload route (no monkeypatched resolution)
# ---------------------------------------------------------------------------


class TestHttpUploadRouteHonorsTheFence:
    def _client(self, monkeypatch, root: Path) -> TestClient:
        monkeypatch.setenv(api_channel.LANE_CONTROL_SECRET_ENV, SECRET)
        monkeypatch.setenv(api_channel.CHECKPOINT_STORE_DIR_ENV, str(root))
        application = FastAPI()
        application.state.session_factory = None
        application.include_router(api_channel.checkpoint_channel_router)
        return TestClient(application)

    def _payload(self, work: str, sequence: int) -> dict[str, Any]:
        import base64

        manifest, blobs, _checkpoint_id = _checkpoint(work, {"route.txt": b"route\n"}, sequence)
        return {
            "manifest": base64.b64encode(manifest).decode("ascii"),
            "blobs": {
                digest: base64.b64encode(data).decode("ascii") for digest, data in blobs.items()
            },
            "sequence": sequence,
        }

    def _bearer(self, work: str) -> dict[str, str]:
        from forge.adaptive.checkpoint_channel import work_scoped_token

        return {"Authorization": f"Bearer {work_scoped_token(SECRET, work)}"}

    def test_the_upload_route_answers_503_with_the_fence_after_the_cutover(
        self, tmp_path, monkeypatch
    ):
        """The route resolves the repository through the ONE composition
        point per request — after the real cutover, an upload to the still
        running old-authority app is refused (503 naming the marker), and
        nothing lands on the retired index."""
        root = tmp_path / "store"
        url = _db_url(tmp_path)
        laboratory = _Lab(root, url)
        asyncio.run(laboratory.build(pinned=False))
        try:
            client = self._client(monkeypatch, root)

            # Before the flip: the ordinary 200 upload. It changes the OLD
            # authority's index, so the drained-offline discipline re-runs
            # import + verify before flipping (the cutover's generation
            # gate would refuse a stale report — and does, below, for the
            # post-flip one).
            response = client.put(
                f"/lane/checkpoints/{WORK}", json=self._payload(WORK, 6), headers=self._bearer(WORK)
            )
            assert response.status_code == 200, response.text
            asyncio.run(
                _run(
                    _cli("--root", str(root), "import", "--database-url", url),
                    _cli("--root", str(root), "verify", "--database-url", url),
                )
            )

            asyncio.run(laboratory.cutover())

            response = client.put(
                f"/lane/checkpoints/{WORK}", json=self._payload(WORK, 7), headers=self._bearer(WORK)
            )
            assert response.status_code == 503, response.text
            assert "authority marker" in response.json()["detail"]
            assert "postgres" in response.json()["detail"]

            # The refused upload landed NOWHERE on the retired authority.
            index = json.loads((root / "works" / f"{WORK}.json").read_text())
            sequences = {e["sequence"] for e in index["checkpoints"]}
            assert 7 not in sequences

            # ...while the reads through the same old app keep serving.
            response = client.get(f"/lane/checkpoints/{WORK}", headers=self._bearer(WORK))
            assert response.status_code == 200
        finally:
            asyncio.run(laboratory.dispose())


# ---------------------------------------------------------------------------
# Fence check × GC volume lock — the one documented order, bounded both ways
# ---------------------------------------------------------------------------


class TestFenceVersusVolumeLock:
    """LOCK ORDER: the fence check runs BEFORE the volume-wide GC lock —
    a fence refusal never WAITS on a contended volume; no path holds the
    volume lock while waiting for the cutover fence; the orders cannot
    cycle. Both waits are bounded."""

    async def test_a_fence_refusal_does_not_wait_on_a_held_volume_lock(self, lab, monkeypatch):
        await lab.cutover()
        monkeypatch.setenv(api_channel.GC_LOCK_WAIT_SECONDS_ENV, "30")
        lock_path = lab.root / "cas-refs.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        manifest, blobs, checkpoint_id = _checkpoint(WORK, {"a.txt": b"refused\n"}, 6)

        holder = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            started = time.monotonic()
            with pytest.raises(MutationsFencedError, match="configured vs active"):
                await lab.old.put(WORK, checkpoint_id, manifest, blobs)
            elapsed = time.monotonic() - started
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            os.close(holder)

        # The refusal was IMMEDIATE — it never reached (never mind waited
        # on) the contended volume lock, and it is the FENCE's typed
        # refusal, not the lock's timeout.
        assert elapsed < 5.0
        index = json.loads((lab.root / "works" / f"{WORK}.json").read_text())
        assert all(e["checkpoint_id"] != checkpoint_id for e in index["checkpoints"])

    async def test_the_owning_side_still_waits_bounded_on_the_volume_lock(self, lab, monkeypatch):
        """The control arm: with the SAME lock held, a mutation the fence
        ALLOWED (the marker's own authority) reaches the volume lock and
        times out with the TYPED :class:`GCLockTimeout` — proving the
        fence check really does precede the lock, and that both waits are
        bounded."""
        from forge.api_checkpoint_channel import GCLockTimeout

        await lab.cutover()
        monkeypatch.setenv(api_channel.GC_LOCK_WAIT_SECONDS_ENV, "0.25")
        lock_path = lab.root / "cas-refs.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        manifest, blobs, checkpoint_id = _checkpoint(WORK, {"a.txt": b"mine\n"}, 6)

        holder = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            started = time.monotonic()
            with pytest.raises(GCLockTimeout):
                await lab.new.put(WORK, checkpoint_id, manifest, blobs)
            elapsed = time.monotonic() - started
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            os.close(holder)

        assert elapsed < 5.0  # bounded by the configured budget, never a deadlock

    async def test_the_documented_residual_window_is_exactly_this_wide(self, lab, monkeypatch):
        """A mutation that ALREADY PASSED the fence check when the flip
        lands may still commit — the honest boundary of the
        drained-offline mode. Pinned exactly: the acknowledged mutation
        reaches the OLD authority, the NEW one never sees it, and the
        very NEXT mutation is refused."""
        monkeypatch.setenv(api_channel.LOCK_WAIT_SECONDS_ENV, "30")
        index_lock = lab.root / "works" / f"{WORK}.lock"
        index_lock.parent.mkdir(parents=True, exist_ok=True)
        manifest, blobs, checkpoint_id = _checkpoint(WORK, {"a.txt": b"racing\n"}, 6)

        holder = os.open(index_lock, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            # The old process's mutation passes the fence check (no marker
            # yet) and blocks INSIDE its critical section — the per-work
            # index lock, before its reference commit.
            task = asyncio.create_task(lab.old.put(WORK, checkpoint_id, manifest, blobs))
            await asyncio.sleep(0.2)  # the fence check ran; the landing is parked

            await lab.cutover()  # the real flip — no store lock needed

            fcntl.flock(holder, fcntl.LOCK_UN)
            await asyncio.wait_for(task, timeout=10.0)  # the landing completes
        finally:
            os.close(holder)

        # The acknowledged mutation reached the RETIRED authority...
        index = json.loads((lab.root / "works" / f"{WORK}.json").read_text())
        assert any(e["checkpoint_id"] == checkpoint_id for e in index["checkpoints"])
        # ...the new authority never saw it...
        assert (
            await PostgresCheckpointRepository(lab.root, lab.factory).entry(WORK, checkpoint_id)
            is None
        )
        # ...and the very NEXT mutation of the old process is refused.
        next_manifest, next_blobs, next_id = _checkpoint(WORK, {"a.txt": b"next\n"}, 7)
        with pytest.raises(MutationsFencedError):
            await lab.old.put(WORK, next_id, next_manifest, next_blobs)


# ---------------------------------------------------------------------------
# No filesystem fallback when the new authority is unavailable
# ---------------------------------------------------------------------------


class TestNoFilesystemFallbackAfterCutover:
    async def test_a_dead_target_database_answers_typed_unavailable_never_the_index(
        self, tmp_path, lab
    ):
        await lab.cutover()

        # The target database's access is GONE: a URL whose directory
        # cannot exist fails every connection — the typed outage, never a
        # filesystem consult and never a silent "no checkpoint".
        dead_url = "sqlite+aiosqlite:////nonexistent-fence-lab/no-such.db"
        engine = create_async_engine(dead_url, connect_args={"check_same_thread": False})
        try:
            factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine)
            stranded = resolve_repository(
                env=_new_env(lab.root), session_factory=factory, root=lab.root
            )

            with pytest.raises(CheckpointRepositoryUnavailable):
                await stranded.entry(WORK)

            # No filesystem fallback: the retired index wrote nothing new.
            before = (lab.root / "works" / f"{WORK}.json").read_bytes()
            _manifest, _blobs, dead_id = _checkpoint(WORK, {"a.txt": b"never\n"}, 9)
            with pytest.raises(CheckpointRepositoryUnavailable):
                await stranded.put(WORK, dead_id, _manifest, _blobs)
            assert (lab.root / "works" / f"{WORK}.json").read_bytes() == before
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# Startup observability + doctor
# ---------------------------------------------------------------------------


class TestStartupObservability:
    async def test_the_startup_line_reports_state_and_generation(self, lab):
        report = authority_state_report(env=_old_env(lab.root), root=lab.root)
        assert report["metric"] == METRIC_CONFIGURED_VS_ACTIVE
        assert report["state"] == "unmarked"
        assert report["marker_generation"] is None

        await lab.cutover()

        old_view = authority_state_report(env=_old_env(lab.root), root=lab.root)
        assert old_view["state"] == "mismatch"
        assert old_view["marker_generation"] == 1
        assert old_view["active_authority"] == AUTHORITY_POSTGRES
        assert old_view["configured_authority"] == AUTHORITY_FILESYSTEM

        new_view = authority_state_report(env=_new_env(lab.root), root=lab.root)
        assert new_view["state"] == "aligned"
        assert new_view["configured_repository"] == "postgres"

    async def test_the_control_service_composition_logs_the_line(self, lab, caplog, monkeypatch):
        from forge.adaptive.wiring import control_service_from_env

        await lab.cutover()
        monkeypatch.setenv(api_channel.CHECKPOINT_STORE_DIR_ENV, str(lab.root))
        monkeypatch.setenv(api_channel.DURABILITY_ENV, "best_effort")

        with caplog.at_level(logging.WARNING, logger="forge.adaptive.wiring"):
            control_service_from_env(env={**_old_env(lab.root)})

        messages = [record.getMessage() for record in caplog.records]
        assert any(METRIC_CONFIGURED_VS_ACTIVE in message for message in messages)
        assert any("fenced" in message for message in messages)

    async def test_the_app_startup_logs_the_line(self, lab, monkeypatch):
        """The lifespan's observability mount: one INFO line naming the
        configured-vs-active state — and a WARNING on mismatch (the same
        real lifespan the suite's app fixture enters)."""
        from pydantic import SecretStr

        from forge.config import Settings
        from forge.database import reset_engine
        from forge.main import create_app

        await lab.cutover()
        monkeypatch.setenv(api_channel.CHECKPOINT_STORE_DIR_ENV, str(lab.root))
        monkeypatch.setenv(api_channel.DURABILITY_ENV, "best_effort")
        settings = Settings(
            GITLAB_URL="https://gitlab.test",
            GITLAB_TOKEN=SecretStr("glpat-test-token"),
            GITLAB_WEBHOOK_SECRET=SecretStr("test-secret-token"),
            DATABASE_URL=_db_url(lab.root),
            LOG_LEVEL="DEBUG",
        )
        application = create_app(settings=settings)

        # The lifespan's setup_logging REPLACES the root handlers (JSON to
        # stderr), so caplog cannot observe inside it — a dedicated handler
        # on the forge.main logger is the honest window.
        captured: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        capture = _Capture()
        forge_logger = logging.getLogger("forge.main")
        forge_logger.addHandler(capture)
        reset_engine()
        try:
            async with application.router.lifespan_context(application):
                lines = [record.getMessage() for record in captured]
        finally:
            forge_logger.removeHandler(capture)
            reset_engine()

        assert any(METRIC_CONFIGURED_VS_ACTIVE in message for message in lines)
        assert any("state=mismatch" in message for message in lines)
        assert any(
            "MutationsFencedError" in message
            for message in lines
            if "marker names" in message or "mismatch" in message
        )


class TestDoctorReportsTheMarker:
    async def _checks(self, monkeypatch, root: Path, url: str):
        from forge.doctor import check_checkpoint_authority

        monkeypatch.setenv(api_channel.CHECKPOINT_STORE_DIR_ENV, str(root))
        return await check_checkpoint_authority(SimpleNamespace(DATABASE_URL=url))

    async def test_unmarked_passes_with_the_fence_dormant(self, tmp_path, monkeypatch):
        root = tmp_path / "store"
        (root / "works").mkdir(parents=True, exist_ok=True)
        results = await self._checks(monkeypatch, root, _db_url(tmp_path))
        marker = next(r for r in results if r.name == "checkpoint.authority_marker")
        assert marker.status == "pass"
        assert "no authority marker" in marker.detail
        assert "dormant" in marker.detail

    async def test_aligned_passes_naming_both_sides(self, lab, monkeypatch):
        await lab.cutover()
        monkeypatch.setenv(api_channel.DURABILITY_ENV, "postgres")
        results = await self._checks(monkeypatch, lab.root, lab.url)
        marker = next(r for r in results if r.name == "checkpoint.authority_marker")
        assert marker.status == "pass"
        assert "generation 1" in marker.detail
        assert "aligned" in marker.detail
        assert "postgres" in marker.detail

    async def test_a_mismatch_fails_naming_the_configured_and_active(self, lab, monkeypatch):
        await lab.cutover()
        monkeypatch.setenv(api_channel.DURABILITY_ENV, "best_effort")
        results = await self._checks(monkeypatch, lab.root, lab.url)
        marker = next(r for r in results if r.name == "checkpoint.authority_marker")
        assert marker.status == "fail"
        assert "filesystem" in marker.detail and "postgres" in marker.detail
        assert "MutationsFencedError" in marker.detail
        assert "rollback" in marker.detail


# ---------------------------------------------------------------------------
# PG-gated: the AT-06 two-process core over real PostgreSQL
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("FORGE_PG_TEST_URL"),
    reason=(
        "FORGE_PG_TEST_URL not set — the R36-05 real-PostgreSQL cutover-fence "
        "proof runs only against a disposable database (rows are deleted per test)"
    ),
)
class TestAt06OverRealPostgres:
    async def _lab(self, tmp_path: Path) -> _Lab:
        from sqlalchemy import delete

        url = os.environ["FORGE_PG_TEST_URL"]
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(delete(api_channel.CheckpointMetadataRow))
        await engine.dispose()
        return _Lab(tmp_path / "store", url)

    async def test_the_two_process_cutover_over_real_postgres(self, tmp_path):
        lab = await self._lab(tmp_path)
        await lab.build()
        try:
            await lab.cutover()

            manifest, blobs, checkpoint_id = _checkpoint(WORK, {"a.txt": b"pg-post\n"}, 6)
            with pytest.raises(MutationsFencedError, match="configured vs active"):
                await lab.old.put(WORK, checkpoint_id, manifest, blobs)

            entry = await lab.new.entry(WORK)
            assert entry is not None and entry["checkpoint_id"] == lab.active_id
            pinned = await lab.new.entry(WORK, lab.pinned_id)
            assert pinned is not None

            await lab.new.put(WORK, checkpoint_id, manifest, blobs)
            index = json.loads((lab.root / "works" / f"{WORK}.json").read_text())
            assert all(e["checkpoint_id"] != checkpoint_id for e in index["checkpoints"])

            # A SECOND new-authority process (own factory, same data) sees
            # the same state through its own composition.
            engine_b, factory_b = await _factory_over(lab.url)
            try:
                second = resolve_repository(
                    env=_new_env(lab.root), session_factory=factory_b, root=lab.root
                )
                entry_b = await second.entry(WORK)
                assert entry_b is not None and entry_b["checkpoint_id"] == checkpoint_id
            finally:
                await engine_b.dispose()
        finally:
            await lab.dispose()
