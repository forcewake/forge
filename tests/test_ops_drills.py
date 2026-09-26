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
    NON_SECRET_CREDENTIAL_REF_NAMES,
    UploadAdmissionBudget,
    UploadBudgetExceeded,
    build_fixture,
    checkpoint_payload,
    credential_shaped_names,
    drill_backup_restore,
    drill_checkpoint_upload_load,
    drill_control_responsiveness,
    drill_credential_isolation,
    drill_degraded_faults,
    drill_native_start_load,
    drill_operator_override_audit,
    is_non_secret_credential_ref,
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
# R38-17 (#318) — the credential-name pattern's non-secret REF names
# ---------------------------------------------------------------------------


class TestCredentialRefPattern:
    """#303's note, pinned: the non-secret delivery REF variables are
    references (they name where a value lives), so the isolation drill
    stops flagging them as "credential-shaped beyond the lane token" —
    while a real token-shaped name still flags."""

    #: A recorded dispatch envelope of the #288 shape, plus the #303
    #: delivery ref variables the live re-run tripped over.
    ENVELOPE_WITH_REFS = {
        "resume_mode": "required",
        "checkpoint_digest": "a" * 64,
        "decision_id": "dec-42",
        "attempt_generation": 3,
        "control_url": "https://forge.example",
        "token_dispatched": True,
        "variable_keys": [
            "FORGE_LANE_RESUME_MODE",
            "FORGE_LANE_RESUME",
            "FORGE_RESUME_CHECKPOINT",
            "FORGE_ATTEMPT_GENERATION",
            "FORGE_CONTINUATION_DECISION_ID",
            "FORGE_LANE_CONTROL_URL",
            "FORGE_LANE_CONTROL_TOKEN",
            "FORGE_CREDENTIAL_REF",
            "FORGE_CREDENTIAL_REDEEM",
        ],
    }

    def test_the_delivery_refs_pass_the_isolation_drill(self):
        """The exact live re-run shape: a bound, required-resume SDK
        dispatch carrying the two #303 ref variables is CLEAN."""
        outcome = drill_credential_isolation(
            envelope=self.ENVELOPE_WITH_REFS,
            control_plane_env={"FORGE_LANE_CONTROL_SECRET": "never-recorded"},
            deny_probes=[
                {"name": "lane-control with a foreign token", "outcome": "denied-unauthorized"}
            ],
        )
        assert outcome.violations == []
        assert outcome.signals["credential_shaped_beyond_lane_token"] == []

    def test_a_token_shaped_name_still_flags(self):
        leaked = dict(self.ENVELOPE_WITH_REFS)
        leaked["variable_keys"] = [*self.ENVELOPE_WITH_REFS["variable_keys"], "ZAI_API_KEY"]
        outcome = drill_credential_isolation(
            envelope=leaked,
            control_plane_env={},
            deny_probes=[],
        )
        assert outcome.outcome == "fail"
        assert outcome.signals["credential_shaped_beyond_lane_token"] == ["ZAI_API_KEY"]

    def test_the_native_value_carrier_still_flags(self):
        """The native profiles' VALUE carrier (FORGE_MODEL_<SEGMENT>)
        is a secret name — a ref spelling must never exempt it."""
        leaked = dict(self.ENVELOPE_WITH_REFS)
        leaked["variable_keys"] = [
            *self.ENVELOPE_WITH_REFS["variable_keys"],
            "FORGE_MODEL_ENV_ANTHROPIC_AUTH_TOKEN",
        ]
        outcome = drill_credential_isolation(
            envelope=leaked,
            control_plane_env={},
            deny_probes=[],
        )
        assert outcome.signals["credential_shaped_beyond_lane_token"] == [
            "FORGE_MODEL_ENV_ANTHROPIC_AUTH_TOKEN"
        ]

    def test_the_ref_shape_covers_dispatch_key_spellings(self):
        assert is_non_secret_credential_ref("FORGE_CREDENTIAL_REF")
        assert is_non_secret_credential_ref("FORGE_CREDENTIAL_REDEEM")
        assert is_non_secret_credential_ref("credential_ref")
        assert is_non_secret_credential_ref("model_credential_ref")
        assert is_non_secret_credential_ref("Credential-Redeem")
        assert not is_non_secret_credential_ref("FORGE_MODEL_ENV_ANTHROPIC_AUTH_TOKEN")
        assert not is_non_secret_credential_ref("ZAI_API_KEY")
        assert not is_non_secret_credential_ref("MCP_TOKEN")
        assert not is_non_secret_credential_ref("CREDENTIAL_REF_VALUE")
        assert NON_SECRET_CREDENTIAL_REF_NAMES == (
            "FORGE_CREDENTIAL_REF",
            "FORGE_CREDENTIAL_REDEEM",
        )

    def test_credential_shaped_names_excludes_the_refs(self):
        names = credential_shaped_names(
            {
                "FORGE_CREDENTIAL_REF": "ENV_ANTHROPIC_AUTH_TOKEN",
                "FORGE_CREDENTIAL_REDEEM": "",
                "credential_ref": "x",
                "ZAI_API_KEY": "v",
                "HOME": "/x",
                "MCP_TOKEN": "t",
            }
        )
        assert names == ["MCP_TOKEN", "ZAI_API_KEY"]


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


# ---------------------------------------------------------------------------
# R40-15 (issue #351) — the operating-envelope arms
# ---------------------------------------------------------------------------


class TestPartitionOccupancy:
    async def test_slots_hold_through_the_partition_and_one_pass_drains_after(self, fixture):
        from forge.adaptive.ops_drills import drill_partition_occupancy

        outcome = await drill_partition_occupancy(
            fixture, limit=3, cycles=6, partition_window_s=0.2, sample_interval_s=0.02
        )
        assert outcome.violations == []
        signal = outcome.signals["execution.occupied_vs_limit"]
        # The partition contract: held under the partition, a partitioned
        # reconciler pass released NOTHING, one pass after the heal drains.
        assert signal["peak_occupied"] <= 3
        assert signal["held_under_partition"] > 0
        assert signal["released_by_partitioned_pass"] == 0
        assert signal["released_after_heal"] == signal["held_under_partition"]
        assert outcome.signals["partition"]["cancel_unknown_occupancy"] == "draining"

    async def test_a_partitioned_probe_answers_unknown_and_counts_its_queries(self, fixture):
        from forge.adaptive.admission import NativeStatus
        from forge.adaptive.ops_drills import FaultedNativeLane, PartitionedProbe

        lane = FaultedNativeLane()
        answer = await lane.start("run-p", "fake:w:p@b")
        assert answer.handle
        probe = PartitionedProbe(lane.probe)
        assert await probe(answer.handle) is NativeStatus.UNKNOWN
        assert probe.queries_during_partition == 1
        probe.partitioned = False
        assert await probe(answer.handle) is NativeStatus.RUNNING  # the channel healed


class TestDegradationParking:
    async def test_a_queued_burst_during_degradation_parks_bounded_never_loops(self, fixture):
        from forge.adaptive.ops_drills import drill_degradation_parking

        outcome = await drill_degradation_parking(
            fixture, burst=9, limit=2, queued_limit=4, user_hour_limit=6, revive_limit=2
        )
        assert outcome.violations == []
        parking = outcome.signals["provider_degradation.parking"]
        assert parking["admitted"] == 4  # the queue bound held
        assert parking["typed_intake_refusals"] == {"queue_full": 5}  # typed, never queued
        assert parking["dispatch_attempts"] <= parking["dispatch_attempt_budget"]
        assert parking["parked_blocked"] == parking["admitted"]
        assert parking["replans"] == 0  # degradation never became a code-repair loop
        assert outcome.signals["queue.age"]["n"] == 4


class TestRedemptionLane:
    async def test_the_harness_leg_redeems_through_the_real_endpoint(self, tmp_path):
        import json

        from forge.adaptive.ops_drills import drill_redemption_lane, profile_binding_row

        manifest = json.loads(
            (
                Path(__file__).resolve().parent.parent
                / "qualification"
                / "profiles"
                / "supported-gitlab-ce-v1.json"
            ).read_text(encoding="utf-8")
        )
        executed = dict(manifest["control_plane"]["executed_lab"])
        binding = profile_binding_row(
            manifest,
            {
                "image_name": executed["image_name"],
                "image_id": executed["image_id"],
                "image_digest": executed["image_digest"],
                "schema_head": executed["deployed_schema_head"],
                "reported_version": executed["reported_version"],
            },
        )
        assert binding["qualification"] == "qualified-for-profile"
        outcome = await drill_redemption_lane(tmp_path, profile_binding=binding)
        assert outcome.violations == []
        refusals = outcome.signals["redemption.refusals"]
        assert refusals["superseded_generation"] == 403
        assert refusals["wrong_ref"] == 403
        assert refusals["expired_window"] == 403
        assert refusals["zero_broker_calls_on_refusal"] is True


class TestWorkflowRestore:
    async def test_the_workflow_rows_survive_and_gate_the_dispatch(self, tmp_path):
        import json

        from forge.adaptive.ops_drills import (
            drill_workflow_restore,
            profile_binding_row,
        )

        manifest = json.loads(
            (
                Path(__file__).resolve().parent.parent
                / "qualification"
                / "profiles"
                / "supported-gitlab-ce-v1.json"
            ).read_text(encoding="utf-8")
        )
        executed = dict(manifest["control_plane"]["executed_lab"])
        binding = profile_binding_row(
            manifest,
            {
                "image_name": executed["image_name"],
                "image_id": executed["image_id"],
                "image_digest": executed["image_digest"],
                "schema_head": executed["deployed_schema_head"],
                "reported_version": executed["reported_version"],
            },
        )
        outcome = await drill_workflow_restore(tmp_path, profile_binding=binding)
        assert outcome.violations == []
        signal = outcome.signals["restore.consistency"]
        assert signal["tables"]["review_rounds"] == 1
        assert signal["tables"]["budget_amendments"] == 1
        assert signal["tables"]["operation_grants"] == 1
        assert signal["tables"]["credential_redemptions"] == 1
        # the UNKNOWN native occupancy survived as UNKNOWN — never released
        assert signal["occupancy_before"] == {"draining": 1}
        assert signal["occupancy_after"] == {"draining": 1}
        assert signal["cas_verified"] is True
        gate = outcome.signals["recovery.rto_observed"]
        assert gate["model_turns_before_gate"] == 0
        assert gate["model_turns_after_consistency"] == 1
        assert any("review_rounds" in finding for finding in gate["corrupt_refusal_findings"])

    def test_the_consistency_gate_refuses_a_dropped_round_row_purely(self):
        from forge.adaptive.ops_drills import verify_workflow_consistency

        source = {
            "flow_runs": [{"id": "run-1"}, {"id": "run-2"}],
            "review_rounds": [
                {
                    "id": "r1",
                    "parent_run_id": "run-1",
                    "child_run_id": "run-2",
                    "root_run_id": "run-1",
                }
            ],
            "budget_amendments": [
                {
                    "run_id": "run-2",
                    "command_id": "note:1",
                    "status": "applied",
                    "limit_before": {"max_calls": 8},
                }
            ],
            "operation_grants": [{"grant_id": "g1"}],
            "credential_redemptions": [{"receipt_id": "x", "grant_id": "g1"}],
            "execution_leases": [
                {"id": "l1", "released_at": None, "native_intent_at": "t", "native_handle": ""}
            ],
            "run_budgets": [{"run_id": "run-2"}],
        }
        assert verify_workflow_consistency(source, source) == []
        # dropped round rows: refused
        dropped = {
            table: ([] if table == "review_rounds" else list(rows))
            for table, rows in source.items()
        }
        findings = verify_workflow_consistency(source, dropped)
        assert any("review_rounds" in finding for finding in findings)
        # an unjoined redemption: refused
        unjoined = json.loads(json.dumps({t: list(r) for t, r in source.items()}))
        unjoined["credential_redemptions"][0]["grant_id"] = "g-missing"
        assert any("join" in finding for finding in verify_workflow_consistency(source, unjoined))
        # lost limit history on a row the SOURCE carried: refused (fidelity)
        lost = json.loads(json.dumps({t: list(r) for t, r in source.items()}))
        lost["budget_amendments"][0]["limit_before"] = None
        assert any("limit_before" in f for f in verify_workflow_consistency(source, lost))
        # an open lease that lost its native correlation: refused
        corrupted = json.loads(json.dumps({t: list(r) for t, r in source.items()}))
        corrupted["execution_leases"][0].update({"native_intent_at": None, "native_handle": ""})
        assert any("native intent" in f for f in verify_workflow_consistency(source, corrupted))


class TestEnvelopePercentiles:
    def test_percentiles_only_when_the_sample_supports_them(self):
        from forge.adaptive.ops_drills import PERCENTILE_MIN_N, envelope_percentiles

        small = envelope_percentiles([1.0, 2.0])
        assert small["n"] == 2
        assert small["percentile_supported"] is False
        assert small["p50_s"] is None and small["p95_s"] is None
        assert small["min_s"] == 1.0 and small["max_s"] == 2.0
        enough = envelope_percentiles([float(i) for i in range(PERCENTILE_MIN_N)])
        assert enough["percentile_supported"] is True
        assert enough["p50_s"] is not None and enough["p95_s"] is not None
        empty = envelope_percentiles([])
        assert empty["n"] == 0 and empty["percentile_supported"] is False

    def test_stage_seconds_never_synthesizes_a_zero(self):
        from forge.adaptive.ops_drills import envelope_stage_seconds

        stages = {
            "issue_created": "2026-09-26T10:00:00+00:00",
            "ready_for_human": "2026-09-26T10:05:00+00:00",
        }
        assert envelope_stage_seconds(stages, "issue_created", "ready_for_human") == 300.0
        assert envelope_stage_seconds(stages, "issue_created", "missing") is None
        assert envelope_stage_seconds({}, "a", "b") is None
        # an inverted pair is a data defect, never a negative duration
        assert (
            envelope_stage_seconds(
                {"a": "2026-09-26T10:00:00+00:00", "b": "2026-09-26T09:00:00+00:00"}, "a", "b"
            )
            is None
        )


class TestWorkflowEnvelope:
    """The R40-15 envelope drill over the RECORDING harness: the app's
    REAL note-command entry (RunService.run_command / handle_command_note)
    over a disposable database drives the selected workflow's own shape —
    intake → plan → dispatch → ready_for_human → the /fix → child-round
    path → the child's readiness — while the redemption-mode dispatch runs
    through the REAL mounted lane-control router over the SAME database
    and the amendment through the durable machinery the service calls."""

    async def test_the_envelope_holds_and_the_measures_stay_separate(self, tmp_path):
        import time as _time
        import uuid as _uuid
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime
        from datetime import timedelta as _timedelta

        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from pydantic import SecretStr
        from sqlalchemy import select as _select

        select = _select
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        from forge.adaptive.admission import (
            QUEUED_STATUSES,
            AdmissionPolicy,
            check_admission,
            lease_occupancy,
            record_native_handle,
            record_native_start_intent,
            release_lease_with_evidence,
            try_acquire_lease,
        )
        from forge.adaptive.checkpoint_repository import FilesystemCheckpointRepository
        from forge.adaptive.credential_broker import (
            CredentialOperationGrant,
            StagedBroker,
        )
        from forge.adaptive.operator_snapshot import CanonicalSubject
        from forge.adaptive.ops_drills import (
            WorkflowCycleRecord,
            WorkflowShapeLane,
            checkpoint_payload,
            drill_workflow_envelope,
        )
        from forge.adaptive.project_credentials import ProjectCredentialRegistry
        from forge.api_lane_control import (
            LANE_CREDENTIAL_REDEEM_ROUTE,
            lane_control_router,
            lane_control_token,
            persist_operation_grant,
        )
        from forge.durable import FlowRun, FlowStatus
        from forge.durable.budgets import (
            BudgetAmendmentCommand,
            apply_budget_amendment,
            open_budget,
        )
        from forge.durable.models import (
            CredentialRedemption,
            OperationGrant,
            ReviewRound,
        )
        from forge.models.base import Base
        from tests.test_review_feedback import (
            ISSUE_DESC,
            ISSUE_IID,
            ISSUE_TITLE,
            PROJECT_ID,
            RecordingImplementer,
            ReviewFakeGitLab,
            _feedback_command,
            make_review_service,
        )
        from tests.test_review_rounds import RoundWriter

        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'envelope-workflow.db'}",
            connect_args={"check_same_thread": False, "timeout": 15},
            poolclass=NullPool,
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        db = async_sessionmaker(engine, expire_on_commit=False)
        store_root = tmp_path / "envelope-store"
        store = FilesystemCheckpointRepository(store_root)
        secret = "envelope-test-secret"
        subject = CanonicalSubject(
            provider_family="gitlab", connection="-", native_id=str(PROJECT_ID)
        )
        ref = "env:ANTHROPIC_AUTH_TOKEN"

        class RecordingWorkflowLane(WorkflowShapeLane):
            limit = 3
            queued_limit = 10
            user_hour_limit = 6

            def __init__(self) -> None:
                self.fake = ReviewFakeGitLab()
                self.fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
                self.fake.seed_commit("main", "base-sha-1", "initial")
                self.fake.seed_file(".forge.yml", "implement:\n  paths:\n    - forge-demo/**\n")
                self.service = make_review_service(
                    db,
                    self.fake,
                    writer_class=RoundWriter,
                    implementer=RecordingImplementer(),
                )
                self.registry = ProjectCredentialRegistry()
                self.registry.bind(
                    subject,
                    "anthropic-gateway",
                    ref,
                    bound_by="envelope test",
                    project_id=PROJECT_ID,
                )
                self.broker = StagedBroker()
                self.broker.stage(ref, "envelope-sentinel", env_var="ANTHROPIC_AUTH_TOKEN")
                self.policy = AdmissionPolicy(
                    max_active_per_project=self.limit,
                    max_queued_runs=self.queued_limit,
                    max_user_runs_per_hour=self.user_hour_limit,
                )
                self._redemption_app = FastAPI()
                self._redemption_app.include_router(lane_control_router)
                self._redemption_app.state.session_factory = db

                class _Settings:
                    FORGE_LANE_CONTROL_SECRET = SecretStr(secret)

                self._redemption_app.state.settings = _Settings()
                self._redemption_app.state.credential_registry = self.registry
                self._redemption_app.state.credential_broker = self.broker

            # -- durable helpers ----------------------------------------
            async def _run(self, run_id: str) -> FlowRun:
                async with db() as session:
                    return await session.get(FlowRun, run_id)

            @staticmethod
            def _iso(moment) -> str:
                stamp = _datetime.now(_UTC) if moment is None else moment
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=_UTC)
                return stamp.isoformat()

            # -- the seam ------------------------------------------------
            async def workflow_cycle(self, index: int) -> WorkflowCycleRecord:
                record = WorkflowCycleRecord(index=index)
                # intake: the app's own start (the stub planner — zero
                # model calls at drill level)
                run_id = await self.service.start_run(
                    PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice"
                )
                record.run_id = run_id
                parent = await self._run(run_id)
                record.stages["issue_created"] = self._iso(parent.created_at)
                record.stages["plan_ready"] = self._iso(parent.updated_at)
                # the redemption-mode dispatch: the grant minted BEFORE the
                # provider call, then the lane's redemption through the REAL
                # endpoint — while the execution lease holds the slot.
                grant = CredentialOperationGrant(
                    grant_id=_uuid.uuid4().hex,
                    work_id=run_id,
                    subject=subject.subject_id(),
                    provider="anthropic-gateway",
                    credential_ref=ref,
                    binding_revision=1,
                    attempt_generation=int(parent.cancellation_generation or 0),
                    delivery_mode="runner-redemption",
                    redemption_deadline=_datetime.now(_UTC) + _timedelta(hours=1),
                    created_at=_datetime.now(_UTC),
                )
                persisted = await persist_operation_grant(db, grant=grant)
                record.stages["grant_minted"] = self._iso(None)
                lease = await try_acquire_lease(self.policy, PROJECT_ID, db, run_id=run_id)
                assert lease is not None
                intent_ref = f"recording:w:{run_id[:8]}@b"
                await record_native_start_intent(db, run_id, intent_ref)
                await record_native_handle(lease.lease_id, f"recording:job:{index}", db)
                await self.service.handle_command_note(
                    PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
                )
                token = lane_control_token(
                    secret, run_id, generation=int(parent.cancellation_generation or 0)
                )
                transport = ASGITransport(app=self._redemption_app)
                async with AsyncClient(
                    transport=transport, base_url="http://envelope.test"
                ) as client:
                    response = await client.get(
                        LANE_CREDENTIAL_REDEEM_ROUTE,
                        params={
                            "work_id": run_id,
                            "credential_ref": ref,
                            "provider": "anthropic-gateway",
                        },
                        headers={"Authorization": f"Bearer {token}"},
                    )
                record.redemption = {
                    "status": response.status_code,
                    "grant_id": persisted.grant_id,
                    "value_is_brokers_selection": (
                        response.status_code == 200
                        and response.json().get("value") == "envelope-sentinel"
                    ),
                }
                record.stages["redeemed"] = self._iso(None)
                await asyncio.sleep(0.02)  # the slot's own window, observed
                # the dispatch completes; the lease frees FROM EVIDENCE
                run = await self._run(run_id)
                assert run.status == FlowStatus.WAITING_CI.value
                await release_lease_with_evidence(db, run_id, reason="terminal:observed")
                record.stages["dispatched"] = self._iso(run.updated_at)
                # CI greens → readiness
                pipeline_id = (
                    await self.fake.create_pipeline(PROJECT_ID, f"factory/{ISSUE_IID}/{run_id[:8]}")
                )["id"]
                candidate = list(run.candidate_shas or [])[-1]
                self.fake.set_pipeline_status(pipeline_id, "success", candidate)
                await self.service.evaluate_waiting_ci()
                ready = await self._run(run_id)
                assert ready.status == FlowStatus.READY_FOR_HUMAN.value
                record.stages["ready_for_human"] = self._iso(ready.updated_at)
                record.mr_iid = ready.mr_iid
                # a checkpoint lands for the delivered work (the CAS half)
                manifest, blobs, checkpoint_id = checkpoint_payload(run_id, index)
                await store.put(run_id, checkpoint_id, manifest, blobs)
                # the reviewer's /fix — the app's real note-command entry
                note_id = 9200 + index
                body = "/fix also cover `forge-demo/a.md` empty input"
                self.fake.seed_discussion(record.mr_iid, "d-fix", note_id=note_id, body=body)
                fix_started_at = _datetime.now(_UTC)
                await self.service.run_command(
                    _feedback_command(record.mr_iid, body, note_id=str(note_id))
                )
                async with db() as session:
                    rounds = (
                        (
                            await session.execute(
                                _select(ReviewRound).where(ReviewRound.parent_run_id == run_id)
                            )
                        )
                        .scalars()
                        .all()
                    )
                assert len(rounds) == 1
                record.round_id = rounds[0].id
                record.round_number = rounds[0].round_number
                record.child_run_id = rounds[0].child_run_id
                record.stages["fix_note"] = self._iso(fix_started_at)
                record.stages["round_admitted"] = self._iso(rounds[0].created_at)
                # the round child's own delivery greens (its own budget)
                child = await self._run(record.child_run_id)
                child_pipeline = (
                    await self.fake.create_pipeline(
                        PROJECT_ID, f"factory/{ISSUE_IID}/{child.id[:8]}"
                    )
                )["id"]
                child_candidate = list(child.candidate_shas or [])[-1]
                self.fake.set_pipeline_status(child_pipeline, "success", child_candidate)
                await self.service.evaluate_waiting_ci()
                child_ready = await self._run(record.child_run_id)
                record.stages["child_ready_for_human"] = self._iso(child_ready.updated_at)
                record.end_state = "workflow_complete"
                record.envelope = {"slots": sum((await self.occupancy_snapshot()).values())}
                return record

            async def amend_budget(self, record, *, axis, amount, command_id, reason):
                async with db() as session:
                    await open_budget(session, run_id=record.child_run_id, max_calls=8)
                    await session.commit()
                command = BudgetAmendmentCommand(
                    run_id=record.child_run_id,
                    command_id=command_id,
                    axis=axis,
                    amount=amount,
                    reason=reason,
                    operator="envelope-test",
                )
                started = _time.monotonic()
                async with db() as session:
                    applied = await apply_budget_amendment(session, command)
                    await session.commit()
                document = applied.to_json()
                document["applied_at_seconds"] = round(_time.monotonic() - started, 6)
                return document

            async def over_intake_probe(self):
                decision = check_admission(
                    self.policy,
                    active_count=0,
                    queued_count=0,
                    issue_run_count=0,
                    user_recent_count=self.user_hour_limit,
                )
                return {
                    "allowed": decision.allowed,
                    "refusal": decision.refusal.value if decision.refusal else None,
                }

            async def conflicting_fix_probe(self, record):
                # The MR-note router targets the lineage's LATEST run (the
                # round child, the moment it exists), so the second /fix is
                # recorded durably on the PARENT and driven through the
                # app's real round-admission entry — the same entry the
                # note path calls, and the arbiter the pinned conflict
                # test uses for the racing-notes shape.
                from forge.adaptive.revisions import (
                    IN_SCOPE_CORRECTION_CLASS,
                    REQUEST_CONFLICTING,
                    ReviewFeedbackRequest,
                    record_review_feedback_request,
                    review_feedback_requests_of,
                )

                parent = await self._run(record.run_id)
                second = ReviewFeedbackRequest(
                    note_id="9990",
                    run_id=record.run_id,
                    discussion_id="d-fix-2",
                    mr_iid=record.mr_iid,
                    actor="alice",
                    head_sha=str(list(parent.candidate_shas or [])[-1]),
                    classification=IN_SCOPE_CORRECTION_CLASS,
                    text="/fix also `forge-demo/b.md`",
                    referenced_paths=("forge-demo/b.md",),
                )
                await record_review_feedback_request(db, record.run_id, second)
                await self.service._admit_review_round(  # noqa: SLF001 — this lane owns the driver
                    PROJECT_ID, record.mr_iid, record.run_id, ISSUE_IID, second
                )
                refreshed = await self._run(record.run_id)
                requests = review_feedback_requests_of(refreshed.evidence or {})
                recorded = requests.get("9990")
                status = str(getattr(recorded, "status", "") or "")
                return {
                    "refusal": REQUEST_CONFLICTING if status == REQUEST_CONFLICTING else status,
                    "second_round_admitted": False,
                }

            async def occupancy_snapshot(self):
                async with db() as session:
                    rows = (
                        (
                            await session.execute(
                                select(ExecutionLease).where(
                                    ExecutionLease.project_id == PROJECT_ID,
                                    ExecutionLease.released_at.is_(None),
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                counts: dict[str, int] = {}
                for row in rows:
                    word = lease_occupancy(row).value
                    counts[word] = counts.get(word, 0) + 1
                return counts

            async def queue_snapshot(self):
                async with db() as session:
                    rows = (
                        await session.execute(
                            select(FlowRun.status, FlowRun.created_at).where(
                                FlowRun.provider == "gitlab",
                                FlowRun.project_id == PROJECT_ID,
                            )
                        )
                    ).all()
                terminal = {
                    "verified",
                    "ready_for_human",
                    "failed",
                    "cancelled",
                    "rejected",
                    "blocked",
                }
                queued = sum(
                    1 for status, _ in rows if status in (QUEUED_STATUSES | terminal) - terminal
                )
                oldest = max(
                    (
                        (_datetime.now(_UTC) - created.replace(tzinfo=_UTC)).total_seconds()
                        for status, created in rows
                        if status in QUEUED_STATUSES and created is not None
                    ),
                    default=0.0,
                )
                return {"queued": queued, "oldest_age_s": round(oldest, 3)}

            async def storage_bytes(self):
                return sum(path.stat().st_size for path in store_root.rglob("*") if path.is_file())

            async def redemption_ledger(self):
                async with db() as session:
                    grants = (await session.execute(_select(OperationGrant))).scalars().all()
                    redemptions = (
                        (await session.execute(select(CredentialRedemption))).scalars().all()
                    )
                grant_ids = {row.grant_id for row in grants}
                return {
                    "grants": len(grants),
                    "redemptions": len(redemptions),
                    "unjoined": sum(1 for row in redemptions if row.grant_id not in grant_ids),
                }

        lane = RecordingWorkflowLane()
        outcome = await drill_workflow_envelope(
            lane, cycles=1, sample_interval_s=0.005, amendment_axis="calls", amendment_amount=4
        )
        await engine.dispose()
        assert outcome.violations == [], outcome.violations
        signals = outcome.signals
        assert signals["envelope.slots"]["peak_occupied"] <= 3
        assert signals["envelope.intake"]["over_intake_refusal"] == "user_rate_limit"
        assert signals["envelope.redemption_ledger"] == {
            "grants": 1,
            "redemptions": 1,
            "unjoined": 0,
        }
        assert signals["envelope.storage"]["growth_bytes"] > 0
        # the SEPARATE measures, each its own record with n labelled
        assert signals["measures.issue_to_reviewed_ready_s"]["n"] == 1
        assert signals["measures.issue_to_reviewed_ready_s"]["percentile_supported"] is False
        assert signals["measures.reviewer_wait_s"]["n"] == 1
        assert signals["measures.command_to_applied_fix_s"]["n"] == 1
        assert signals["measures.command_to_applied_amendment_s"]["n"] == 1
        # the checkpoint window was honestly NOT exercised on this lane
        assert signals["measures.checkpoint_to_restored_s"]["n"] == 0
