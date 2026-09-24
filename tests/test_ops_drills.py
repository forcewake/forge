"""R36-21 (issue #280): the operational drills' pinned invariants.

Each drill from :mod:`forge.adaptive.ops_drills` runs here at modest N
(fast) against disposable fixtures — a real file-backed sqlite, a real
checkpoint store, a real faulted fake-native lane — and the test pins
the INVARIANT, not the drill's own verdict alone: capacity never
exceeded under concurrency (failed cancels and lost responses
included), upload-budget overload as a typed refusal, the #263
GCLockTimeout contract under a contended sweep, bounded control-command
latency, typed degraded-mode outcomes, the backup/restore round-trip
with mismatch detection, the override audit's visibility, and the
report's shape. The PostgreSQL variants are gated on ``FORGE_PG_TEST_URL``
plus the disposable-database pattern (ADR-0017 failure-injection
convention).
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from forge import api_checkpoint_channel as api_channel
from forge.adaptive.admission import (
    AdmissionPolicy,
    ExecutionLease,
    saturation_report,
)
from forge.adaptive.checkpoint_repository import (
    BackupMismatchError,
    FilesystemCheckpointRepository,
    backup_store,
    restore_store,
    verify_backup_consistency,
)
from forge.adaptive.ops_drills import (
    DRILLS,
    DRILL_SCOPE,
    DrillOutcome,
    FaultedNativeLane,
    LostStartResponse,
    UploadAdmissionBudget,
    UploadBudgetExceeded,
    build_fixture,
    checkpoint_payload,
    drill_backup_restore,
    drill_checkpoint_upload_load,
    drill_control_responsiveness,
    drill_degraded_faults,
    drill_native_start_load,
    drill_operator_override_audit,
    run_drill,
)
from forge.models.base import Base

PROJECT_ID = 1


# ---------------------------------------------------------------------------
# The disposable fixture every drill test shares
# ---------------------------------------------------------------------------


@pytest.fixture()
async def fixture(tmp_path: Path):
    built = await build_fixture(tmp_path)
    try:
        yield built
    finally:
        await built.dispose()


class TestNativeStartLoad:
    async def test_capacity_never_exceeded_and_reconciliation_bounded(self, fixture):
        outcome = await drill_native_start_load(
            fixture,
            workers=6,
            cycles_per_worker=3,
            limit=3,
            cancel_fail_ratio=0.3,
            lost_response_ratio=0.3,
        )
        assert outcome.violations == []
        assert outcome.outcome == "pass"
        # The signal the operator watches: peak occupancy vs the limit.
        assert outcome.signals["execution.occupied_vs_limit"]["peak_occupied"] <= 3
        assert outcome.signals["execution.occupied_vs_limit"]["limit"] == 3

    async def test_failed_cancels_and_lost_responses_hold_slots_midload(self, fixture):
        outcome = await drill_native_start_load(fixture, workers=5, cycles_per_worker=2, limit=2)
        # The drill records this objective only when it OBSERVED the
        # hold mid-load; with these ratios it always does.
        assert any("HELD their slots" in objective for objective in outcome.objectives)

    async def test_the_report_states_its_tested_limits(self, fixture):
        outcome = await drill_native_start_load(fixture, workers=3, cycles_per_worker=1, limit=2)
        assert outcome.tested_limits["max_active_per_project"] == 2
        assert outcome.tested_limits["workers"] == 3
        assert outcome.as_document()["scope"] == DRILL_SCOPE


class TestCheckpointUploadLoad:
    async def test_overload_is_a_typed_refusal_and_the_budget_holds(self, fixture):
        outcome = await drill_checkpoint_upload_load(
            fixture,
            works=2,
            puts_per_work=2,
            uploaders=8,
            max_concurrent=2,
            max_in_flight_bytes=2048,
            admit_wait_s=0.05,
        )
        assert outcome.violations == []
        budget_signal = outcome.signals["checkpoint.upload_memory_budget"]
        assert budget_signal["peak_concurrent"] <= 2
        assert budget_signal["peak_in_flight_bytes"] <= 2048
        assert budget_signal["typed_refusals"] > 0  # overload actually exercised

    async def test_an_oversized_upload_refuses_at_admission_before_any_byte(self):
        budget = UploadAdmissionBudget(max_concurrent=2, max_in_flight_bytes=512)
        with pytest.raises(UploadBudgetExceeded, match="exceeds the whole in-flight budget"):
            budget.admit(1024)
        assert budget.refusals == 1
        assert budget.in_flight_bytes == 0  # nothing was ever held

    async def test_a_held_admission_releases_with_its_upload(self):
        budget = UploadAdmissionBudget(max_concurrent=1, max_in_flight_bytes=1024)
        async with budget.admit(256):
            assert budget.in_flight_bytes == 256
            assert budget.peak_concurrent == 1
            # A second admission must refuse TYPED within the wait budget.
            with pytest.raises(UploadBudgetExceeded):
                async with budget.admit(512):
                    pass
        assert budget.in_flight_bytes == 0
        # The released slot is reusable — failures cannot wedge the budget.
        async with budget.admit(512):
            assert budget.peak_bytes == 512

    async def test_the_contended_sweep_refuses_typed_and_converges(self, fixture, monkeypatch):
        first_id = ""
        second_id = ""
        for sequence in range(2):
            manifest, blobs, checkpoint_id = checkpoint_payload("wp-gc", sequence)
            if sequence == 0:
                first_id = checkpoint_id
            else:
                second_id = checkpoint_id
            await fixture.repository.put("wp-gc", checkpoint_id, manifest, blobs)
        monkeypatch.setenv(api_channel.GC_LOCK_WAIT_SECONDS_ENV, "0.2")
        lock_path = fixture.root / "cas-refs.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            started = time.monotonic()
            with pytest.raises(api_channel.GCLockTimeout):
                await asyncio.wait_for(
                    fixture.repository.apply_retention("wp-gc", keep_last=1), timeout=30.0
                )
            elapsed = time.monotonic() - started
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        # The #263 contract: bounded wait (never an indefinite block) and
        # a TYPED refusal. The mark (index drop) stood; the bytes did not
        # go — nothing still referenced was unlinked by the refusing side.
        assert elapsed < 2.0
        assert await fixture.repository.entry("wp-gc", second_id) is not None
        old_blob = fixture.root / first_id[:2] / first_id
        assert old_blob.is_file(), "the aborted sweep unlinked NOTHING"
        # Once the holder releases, the SAME sweep converges: the retry
        # completes the pending-GC unlink (0 further entries to drop).
        assert await fixture.repository.apply_retention("wp-gc", keep_last=1) == 0
        assert not old_blob.exists()


class TestControlResponsiveness:
    async def test_command_latency_is_measured_and_bounded_under_contention(self, fixture):
        outcome = await drill_control_responsiveness(
            fixture, commands=10, contention_puts=3, control_objective_s=2.0
        )
        assert outcome.violations == []
        latency = outcome.signals["control.command_latency"]
        assert latency["commands"] == 10
        assert 0.0 <= latency["p95_s"] <= latency["max_s"]
        assert latency["p95_s"] <= 2.0


class TestDegradedFaults:
    async def test_every_fault_produces_its_typed_outcome(self, fixture):
        outcome = await drill_degraded_faults(fixture)
        assert outcome.violations == []
        modes = outcome.signals["degraded_modes"]
        assert len(modes) == 5  # latency, 429/503, 404, restart, full disk

    async def test_an_ambiguous_start_holds_its_slot_until_probed(self, fixture):
        policy = AdmissionPolicy(max_active_per_project=2)
        lane = FaultedNativeLane(start_status=429)
        from uuid import uuid4

        from forge.adaptive.admission import (
            record_native_start_intent,
            release_lease_with_evidence,
            try_acquire_lease,
        )
        from forge.adaptive.ops_drills import occupancy_snapshot

        run_id = uuid4().hex
        lease = await try_acquire_lease(policy, 9, fixture.session_factory, run_id=run_id)
        assert lease is not None
        await record_native_start_intent(fixture.session_factory, run_id, "fake:w:amb@b")
        answer = await lane.start(run_id, "fake:w:amb@b")
        assert answer.status == 429 and not answer.handle  # ambiguous: no handle
        await release_lease_with_evidence(
            fixture.session_factory, run_id, reason="terminal:cancelled"
        )
        snapshot = await occupancy_snapshot(fixture.session_factory, 9)
        assert snapshot.get("dispatched_unknown", 0) + snapshot.get("draining", 0) == 1
        report = await saturation_report(policy, 9, fixture.session_factory)
        assert report["native_start.unknown_count"] == 1
        assert report["native_start.unknown_age"] is not None

    async def test_a_lost_start_response_keeps_capacity_accounted(self, fixture):
        from forge.adaptive.admission import NativeStatus

        lane = FaultedNativeLane(lose_response=True)
        with pytest.raises(LostStartResponse):
            await lane.start("run-1", "fake:w:lost@b")
        # The job exists provider-side even though the response died.
        assert len(lane.jobs) == 1
        assert await lane.probe(next(iter(lane.jobs))) is NativeStatus.RUNNING

    async def test_a_full_disk_shaped_quota_refuses_typed_leaving_the_store_intact(
        self, tmp_path: Path
    ):
        from forge.api_checkpoint_channel import StoragePolicy, StorageQuotaExceededError

        store = FilesystemCheckpointRepository(
            tmp_path / "tiny", policy=StoragePolicy(max_total_bytes_per_work=512)
        )
        manifest, blobs, checkpoint_id = checkpoint_payload("wp", 0, blob_count=4, blob_bytes=256)
        with pytest.raises(StorageQuotaExceededError):
            await store.put("wp", checkpoint_id, manifest, blobs)
        assert await store.entry("wp") is None  # refusal preceded the first write


class TestBackupRestore:
    async def test_round_trip_recovers_active_and_pinned_and_detects_mismatch(self, fixture):
        outcome = await drill_backup_restore(fixture)
        assert outcome.violations == []
        coverage = outcome.signals["backup.restore_coverage"]
        assert coverage["active_recovered"] is True
        assert coverage["pinned_recovered"] is True
        assert coverage["mismatch_refused"] is True
        assert coverage["mismatch_affected_works"] == ["wp-backup-0"]

    async def test_a_mismatched_restore_writes_nothing(self, tmp_path: Path):
        source = tmp_path / "source"
        repository = FilesystemCheckpointRepository(source)
        for sequence in range(2):
            manifest, blobs, checkpoint_id = checkpoint_payload("wp-x", sequence)
            await repository.put("wp-x", checkpoint_id, manifest, blobs)
        backup = await backup_store(source, tmp_path / "backup")
        # t2: a NEW checkpoint lands whose bytes the t1 blob half lacks.
        t2_manifest, t2_blobs, t2_id = checkpoint_payload("wp-x", 9)
        await repository.put("wp-x", t2_id, t2_manifest, t2_blobs)

        import shutil

        mismatch_dir = tmp_path / "mismatched"
        mismatch_dir.mkdir()
        shutil.copytree(backup.path / "works", mismatch_dir / "works")
        for shard in sorted(p for p in backup.path.iterdir() if p.is_dir() and len(p.name) == 2):
            shutil.copytree(shard, mismatch_dir / shard.name, dirs_exist_ok=True)
        (mismatch_dir / "works" / "wp-x.json").write_text(
            json.dumps(
                {"work_id": "wp-x", "checkpoints": [{"checkpoint_id": t2_id, "sequence": 9}]}
            ),
            encoding="utf-8",
        )
        mismatches = verify_backup_consistency(mismatch_dir)
        assert [item["checkpoint_id"] for item in mismatches] == [t2_id]
        target = tmp_path / "restore-refused"
        with pytest.raises(BackupMismatchError) as raised:
            await restore_store(mismatch_dir, target)
        assert raised.value.affected[0]["work_id"] == "wp-x"
        assert not target.exists() or not any(target.iterdir())

    async def test_a_consistent_backup_restores_and_covers_pending_gc(self, tmp_path: Path):
        source = tmp_path / "source"
        repository = FilesystemCheckpointRepository(source)
        manifest, blobs, checkpoint_id = checkpoint_payload("wp-p", 0)
        await repository.put("wp-p", checkpoint_id, manifest, blobs)
        await repository.pin("wp-p", checkpoint_id, reason="test pin")
        # A pending-GC record (the postgres authority's tombstone shape).
        from forge.adaptive.checkpoint_repository import CheckpointGcJournal

        CheckpointGcJournal(source).record("wp-p", ["a" * 64])
        backup = await backup_store(source, tmp_path / "backup")
        assert backup.pins == 1 and backup.pending_gc == 1
        coverage = await restore_store(backup, tmp_path / "target")
        assert coverage["pins"] == 1
        assert coverage["pending_gc"] == 1
        restored = FilesystemCheckpointRepository(tmp_path / "target")
        entry = await restored.entry("wp-p")
        assert entry is not None and entry["checkpoint_id"] == checkpoint_id
        # The restored checkpoint reads VERIFIED (bytes hashed to address).
        manifest_back, blobs_back = await restored.read_entry(entry)
        assert manifest_back == manifest
        # Lock files are transient coordination, never backed-up state.
        assert not (tmp_path / "target" / "cas-refs.lock").exists()


class TestOperatorOverrideAudit:
    async def test_the_override_is_audited_and_visible(self, fixture):
        outcome = await drill_operator_override_audit(fixture)
        assert outcome.violations == []
        audit = outcome.signals["override.audit"]
        assert audit["approver"] in audit["audit_trail"]
        assert audit["ticket"] in audit["audit_trail"]
        assert audit["unaudited_release_kept_slot"] is True

    async def test_the_unaudited_release_never_frees_uncertain_occupancy(self, fixture):
        from uuid import uuid4

        from forge.adaptive.admission import (
            record_native_start_intent,
            release_lease_with_evidence,
            try_acquire_lease,
        )

        policy = AdmissionPolicy(max_active_per_project=2)
        run_id = uuid4().hex
        lease = await try_acquire_lease(policy, 7, fixture.session_factory, run_id=run_id)
        assert lease is not None
        await record_native_start_intent(fixture.session_factory, run_id, "fake:w:x@b")
        outcome = await release_lease_with_evidence(
            fixture.session_factory, run_id, reason="terminal:cancelled"
        )
        assert outcome.drained == 1 and outcome.released == 0
        async with fixture.session_factory() as session:
            row = await session.get(ExecutionLease, lease.lease_id)
        assert row.released_at is None  # parked draining — the slot HOLDS


class TestSaturationSignals:
    async def test_occupied_vs_limit_and_unknown_age_are_derived(self, fixture):
        from datetime import UTC, datetime, timedelta

        from forge.adaptive.admission import (
            record_native_start_intent,
            try_acquire_lease,
        )

        policy = AdmissionPolicy(max_active_per_project=2)
        run_id = "satrun0001"
        assert await try_acquire_lease(policy, PROJECT_ID, fixture.session_factory, run_id=run_id)
        past = datetime.now(UTC) - timedelta(seconds=120)
        await record_native_start_intent(fixture.session_factory, run_id, "fake:w:age@b", now=past)
        report = await saturation_report(policy, PROJECT_ID, fixture.session_factory)
        assert report["execution.occupied_vs_limit"] == {
            "occupied": 1,
            "limit": 2,
            "available": 1,
            "at_limit": False,
        }
        assert report["native_start.unknown_count"] == 1
        assert report["native_start.unknown_age"] >= 100
        assert "override" in report["escalation"]  # the bounded escalation path is named

    async def test_unknown_age_is_none_when_nothing_is_unknown(self, fixture):
        policy = AdmissionPolicy(max_active_per_project=2)
        report = await saturation_report(policy, PROJECT_ID, fixture.session_factory)
        assert report["native_start.unknown_age"] is None
        assert report["native_start.unknown_count"] == 0


class TestReportShape:
    async def test_every_drill_outcome_document_carries_the_honest_scope(self, tmp_path: Path):
        outcome = await run_drill("operator_override_audit", tmp_path / "d")
        document = outcome.as_document()
        assert document["scope"] == DRILL_SCOPE
        assert set(document) == {
            "drill",
            "outcome",
            "achieved_objectives",
            "tested_limits",
            "signals",
            "violations",
            "scope",
        }
        assert document["outcome"] == "pass"
        assert json.dumps(document)  # JSON-serializable, no secrets/prompts

    def test_the_registry_names_six_drills(self):
        assert set(DRILLS) == {
            "native_start_load",
            "checkpoint_upload_load",
            "control_responsiveness",
            "degraded_faults",
            "backup_restore",
            "operator_override_audit",
        }

    async def test_an_unknown_drill_name_refuses_loudly(self, tmp_path: Path):
        with pytest.raises(KeyError, match="unknown drill"):
            await run_drill("fleet_throughput", tmp_path / "d")

    async def test_a_failing_check_is_a_violation_never_a_skip(self):
        outcome = DrillOutcome(drill="selftest")
        outcome.check(False, "the pinned invariant")
        assert outcome.outcome == "fail"
        assert outcome.objectives == []
        assert outcome.violations == ["the pinned invariant"]


# ---------------------------------------------------------------------------
# The PostgreSQL variant — disposable database, skipif-gated
# ---------------------------------------------------------------------------

PG_DISPOSABLE_DB = "forge_ops_drills_test"


def _podman_psql(statement: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "podman",
            "exec",
            "forge-postgres",
            "psql",
            "-U",
            "forge",
            "-d",
            "forge",
            "-c",
            statement,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.skipif(
    not os.environ.get("FORGE_PG_TEST_URL"),
    reason=(
        "FORGE_PG_TEST_URL not set — the PG drill variants run only against a "
        "disposable real PostgreSQL (created/dropped via the forge-postgres "
        "podman container)"
    ),
)
class TestPostgresDrillVariants:
    """The same drills against real PostgreSQL: the lease CAS, the
    checkpoint index in ``checkpoint_metadata`` and the restore drill
    over the real repository the control plane composes."""

    @pytest.fixture()
    def lab(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from sqlalchemy.engine import make_url

        _podman_psql(f"DROP DATABASE IF EXISTS {PG_DISPOSABLE_DB} WITH (FORCE)")
        created = _podman_psql(f"CREATE DATABASE {PG_DISPOSABLE_DB}")
        if created.returncode != 0:
            pytest.skip(f"the forge-postgres podman container is unavailable: {created.stderr}")
        url = (
            make_url(os.environ["FORGE_PG_TEST_URL"])
            .set(database=PG_DISPOSABLE_DB)
            .render_as_string(hide_password=False)
        )
        try:
            yield str(url)
        finally:
            _podman_psql(f"DROP DATABASE IF EXISTS {PG_DISPOSABLE_DB} WITH (FORCE)")

    async def test_native_start_load_on_real_postgres(self, lab, tmp_path: Path):
        outcome = await run_drill(
            "native_start_load",
            tmp_path / "pg-load",
            db_url=lab,
            fast=True,
        )
        assert outcome.violations == []
        assert outcome.tested_limits["database"] == "postgresql+asyncpg"

    async def test_checkpoint_upload_load_on_real_postgres(self, lab, tmp_path: Path):
        outcome = await run_drill(
            "checkpoint_upload_load",
            tmp_path / "pg-upload",
            db_url=lab,
            authority="postgres",
            fast=True,
        )
        assert outcome.violations == []

    async def test_backup_restore_on_real_postgres_exports_the_table(self, lab, tmp_path: Path):
        fixture = await build_fixture(tmp_path / "pg-backup", db_url=lab, authority="postgres")
        try:
            for sequence in range(2):
                manifest, blobs, checkpoint_id = checkpoint_payload("wp-pg", sequence)
                await fixture.repository.put("wp-pg", checkpoint_id, manifest, blobs)
            backup = await backup_store(
                fixture.root, tmp_path / "pg-backup-dir", session_factory=fixture.session_factory
            )
            assert backup.metadata_rows == 2  # the postgres metadata half exported
            # Restore into a FRESH disposable database: rows re-imported.
            _podman_psql(f"DROP DATABASE IF EXISTS {PG_DISPOSABLE_DB}_r WITH (FORCE)")
            _podman_psql(f"CREATE DATABASE {PG_DISPOSABLE_DB}_r")
            from sqlalchemy.engine import make_url

            restore_url = (
                make_url(lab)
                .set(database=f"{PG_DISPOSABLE_DB}_r")
                .render_as_string(hide_password=False)
            )
            engine = create_async_engine(restore_url, poolclass=NullPool)
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            try:
                coverage = await restore_store(
                    backup, tmp_path / "pg-restore-target", session_factory=factory
                )
                assert coverage["metadata_rows"] == 2
                # The PG authority over the RESTORED root (rows re-imported
                # + blobs restored) answers the same active checkpoint.
                from forge.adaptive.checkpoint_repository import PostgresCheckpointRepository

                restored_pg = PostgresCheckpointRepository(tmp_path / "pg-restore-target", factory)
                entry = await restored_pg.entry("wp-pg")
                assert entry is not None and entry.get("checkpoint_id")
                manifest_back, blobs_back = await restored_pg.read_entry(entry)
                assert manifest_back  # the verified read of the restored bytes
            finally:
                await engine.dispose()
                _podman_psql(f"DROP DATABASE IF EXISTS {PG_DISPOSABLE_DB}_r WITH (FORCE)")
        finally:
            await fixture.dispose()
