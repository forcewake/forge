"""R37-20 (issue #301) — the deployment-drill machinery against fakes.

Every deployment drill from :mod:`forge.adaptive.ops_drills` is driven
here against FAKES (a deterministic remote lane, a disposable store, a
recorded dispatch envelope, stub probes) — no lab, no container runtime,
no network. What is held:

- the remote-occupancy invariants: capacity NEVER exceeded through the
  real admission seam, overload parked with a typed verdict, queue wait
  measured SEPARATELY from execution, a lost cancel response resolving
  by observation, a failed cancel never releasing capacity;
- the backup/restore boundary: pinned checkpoints resolve in a
  disposable restore, mismatched halves refuse typed writing nothing;
- credential isolation: no control-plane root credential name in the
  model-facing variable set, every deny probe observing actual denial;
- the degraded modes: typed quota refusal, the bounded 429 revival
  budget, the measured received→applied window, fences never disabled;
- token rotation: the rotated-secret and old-generation refusals through
  the REAL generation-scoped auth path (the mounted lane-control router
  over a disposable database);
- the EXECUTED report round-trips: whatever
  ``qualification/deployment-ops-2026-09-24.json`` records is a valid
  report document (skip when the executed report is not on disk).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from forge.adaptive.checkpoint_repository import FilesystemCheckpointRepository
from forge.adaptive.ops_drills import (
    CAS_SHARED_VOLUME_STATEMENT,
    CAS_UNSHARED_REPLICA_BOUNDARY,
    CONTROL_PLANE_ROOT_CREDENTIAL_NAMES,
    DEPLOYMENT_DRILL_SCOPE,
    RemoteCycleRecord,
    RemoteDispatchLane,
    build_topology_document,
    checkpoint_payload,
    credential_shaped_names,
    drill_credential_isolation,
    drill_degraded_modes,
    drill_remote_occupancy,
    drill_restore_deployment,
    drill_token_rotation,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXECUTED_REPORT = REPO_ROOT / "qualification" / "deployment-ops-2026-09-24.json"


# ---------------------------------------------------------------------------
# The fake remote lane — the real one drives GitLab + the app's dispatch
# ---------------------------------------------------------------------------


class FakeRemoteDispatchLane(RemoteDispatchLane):
    """A deterministic dispatch lane: ``limit`` slots, N cycles, one
    deliberately-parked overload verdict, a held lease across the dropped
    cancel response, a completed job whose cancel fails."""

    def __init__(self, *, cycles: int, limit: int = 2, leak_capacity: bool = False) -> None:
        self.limit = limit
        self.cycles = cycles
        self.leak_capacity = leak_capacity
        self.open_leases: dict[str, str] = {}
        self.cancelled: list[str] = []
        self.occupancy_log: list[dict[str, int]] = []
        self.completed_job_cancelled = False
        self.samples = 0

    async def dispatch(self, index: int) -> RemoteCycleRecord:
        record = RemoteCycleRecord(index=index, run_id=f"run-{index:02d}")
        if index >= self.limit:
            record.end_state = "parked_execution_capacity"
            record.queue_wait_s = 0.4
            record.detail = "execution_capacity: no execution slot free in this project"
            return record
        record.lease_acquired = True
        record.queue_wait_s = 0.1 + index * 0.05
        record.pipeline_id = 900 + index
        record.job_id = 7000 + index
        record.execution_s = 0.6
        record.end_state = "dispatched"
        record.detail = f"pipeline {record.pipeline_id} on the lab runner"
        self.open_leases[record.run_id] = "native_running"
        # Hold the slot so the drill's sampler observes the occupancy
        # mid-load (the real lane's job runs until the cancel legs).
        await asyncio.sleep(0.15)
        return record

    async def occupancy_snapshot(self) -> dict[str, int]:
        self.samples += 1
        counts: dict[str, int] = {}
        for word in self.open_leases.values():
            counts[word] = counts.get(word, 0) + 1
        if self.leak_capacity and counts:
            # The pathological lane: one phantom extra occupant.
            first = next(iter(counts))
            counts[first] += 1
        self.occupancy_log.append(dict(counts))
        return counts

    async def lease_state(self, run_id: str) -> str:
        return self.open_leases.get(run_id, "")

    async def cancel_immediately(self, record: RemoteCycleRecord) -> str:
        self.cancelled.append(record.run_id)
        self.open_leases[record.run_id] = "draining"  # holds until reconciled
        return "ok:200"

    async def cancel_with_lost_response(self, record: RemoteCycleRecord) -> None:
        self.cancelled.append(record.run_id)
        # The cancel was issued; the response is dropped. The lease KEEPS
        # its hold — only the reconciler's observation may release it.
        self.open_leases[record.run_id] = "draining"

    async def cancel_completed_job(self, record: RemoteCycleRecord) -> dict:
        self.completed_job_cancelled = True
        # The provider refuses (or no-ops): either way the completed job's
        # state is unchanged — that is the leg's assertion.
        return {
            "exercised": True,
            "verdict": "refused:409",
            "status_before": "success",
            "status_after": "success",
        }

    async def wait_project_drained(self, timeout_s: float) -> tuple[bool, float]:
        # The deployment's reconciler: one bounded pass observes every
        # cancelled job terminal and drains the held slots.
        self.open_leases.clear()
        return True, 0.25


@pytest.fixture()
async def seeded_store(tmp_path: Path):
    root = tmp_path / "deploy-store"
    repository = FilesystemCheckpointRepository(root)
    for sequence in range(2):
        manifest, blobs, checkpoint_id = checkpoint_payload("wp-deploy", sequence)
        await repository.put("wp-deploy", checkpoint_id, manifest, blobs)
    entry = await repository.entry("wp-deploy")
    assert entry is not None
    await repository.pin("wp-deploy", str(entry["checkpoint_id"]), reason="deployment drill pin")
    return root


# ---------------------------------------------------------------------------
# The topology declaration
# ---------------------------------------------------------------------------


class TestTopologyDocument:
    def _document(self, *, worker_has_volume: bool = True) -> dict:
        mounts = {"/app/data": "/host/forge/data", "/app/.secrets": "/host/forge/.secrets"}
        return build_topology_document(
            app_health={
                "status": "ok",
                "version": "0.36.0",
                "database": "ok",
                "queue_depth": 0,
                "dlq_depth": 3,
            },
            container_inspections={
                "forge-app": {
                    "image": "localhost/forge:dev",
                    "status": "running",
                    "mounts": mounts,
                    "ports": {},
                },
                "forge-worker": {
                    "image": "localhost/forge:dev",
                    "status": "running",
                    "mounts": mounts if worker_has_volume else {"/app/.secrets": "/s"},
                    "ports": {},
                },
                "forge-postgres": {
                    "image": "postgres:17-alpine",
                    "status": "running",
                    "mounts": {},
                    "ports": {},
                },
            },
            runners=[
                {
                    "id": 4,
                    "description": "unraid",
                    "active": True,
                    "paused": False,
                    "runner_type": "instance_type",
                }
            ],
            admission_env={},
            cas_host_root="/host/forge/data/checkpoints",
            budget_caps={"FORGE_BUDGET_PROFILES_configured": True},
        )

    def test_the_shared_volume_is_stated_not_assumed(self):
        document = self._document()
        cas = document["observed"]["cas_volume"]
        assert cas["mounted_into"] == ["forge-app", "forge-worker"]
        assert cas["statement"] == CAS_SHARED_VOLUME_STATEMENT
        assert cas["boundary"] == CAS_UNSHARED_REPLICA_BOUNDARY
        assert "does not replicate" in cas["boundary"]
        assert document["discrepancies"] == []

    def test_a_consumer_without_the_volume_is_a_stated_discrepancy(self):
        document = self._document(worker_has_volume=False)
        assert any(
            "forge-worker does NOT mount the shared /app/data volume" in entry
            for entry in document["discrepancies"]
        )
        assert document["observed"]["cas_volume"]["mounted_into"] == ["forge-app"]

    def test_the_admission_limit_and_runner_isolation_are_observed(self):
        document = self._document()
        assert document["observed"]["admission"]["max_active_per_project"] == 3
        assert document["observed"]["admission"]["source"].startswith("default")
        assert document["observed"]["runner_isolation"]["runners"][0]["id"] == 4
        assert document["declared"]["api_replicas"] == 1
        assert document["read_only"] is True


# ---------------------------------------------------------------------------
# Deployment drill 1 — remote occupancy
# ---------------------------------------------------------------------------


class TestRemoteOccupancy:
    async def test_capacity_never_exceeded_and_overload_parks_typed(self):
        lane = FakeRemoteDispatchLane(cycles=4, limit=2)
        outcome = await drill_remote_occupancy(
            lane, cycles=4, reconcile_timeout_s=5.0, sample_interval_s=0.02
        )
        assert outcome.violations == []
        signal = outcome.signals["execution.occupied_vs_limit"]
        assert signal["limit"] == 2
        assert signal["peak_occupied"] <= 2
        assert signal["dispatched"] == 2
        assert "parked_execution_capacity" in signal["parked"]

    async def test_queue_wait_is_measured_separately_from_execution(self):
        lane = FakeRemoteDispatchLane(cycles=4, limit=2)
        outcome = await drill_remote_occupancy(
            lane, cycles=4, reconcile_timeout_s=5.0, sample_interval_s=0.02
        )
        assert len(outcome.signals["queue_wait_s"]["per_cycle"]) == 4
        assert all(
            value is not None for value in outcome.signals["queue_wait_s"]["per_cycle"].values()
        )
        # Execution windows exist ONLY for dispatched cycles (the slot's
        # own window), never blended into the parked cycles' queue wait.
        assert set(outcome.signals["execution_s"]["per_cycle"]) == {"0", "1"}

    async def test_the_lost_cancel_response_releases_nothing(self):
        lane = FakeRemoteDispatchLane(cycles=2, limit=2)
        outcome = await drill_remote_occupancy(
            lane, cycles=2, reconcile_timeout_s=5.0, sample_interval_s=0.02
        )
        assert outcome.violations == []
        lost = outcome.signals["native.occupancy_unknown"]["lost_response_leg"]
        assert lost["run_id"] == "run-00"
        assert lost["lease_after_drop"] == "draining"  # still accounted

    async def test_the_failed_cancel_moves_nothing(self):
        lane = FakeRemoteDispatchLane(cycles=2, limit=2)
        outcome = await drill_remote_occupancy(
            lane, cycles=2, reconcile_timeout_s=5.0, sample_interval_s=0.02
        )
        assert lane.completed_job_cancelled is True
        leg = outcome.signals["native.occupancy_unknown"]["failed_cancel_leg"]
        assert leg["exercised"] is True
        assert leg["status_before"] == leg["status_after"] == "success"
        assert outcome.violations == []

    async def test_a_failed_cancel_that_mutates_the_completed_job_is_a_violation(self):
        class MutatingLane(FakeRemoteDispatchLane):
            async def cancel_completed_job(self, record: RemoteCycleRecord) -> dict:
                return {
                    "exercised": True,
                    "verdict": "answered:200",
                    "status_before": "success",
                    "status_after": "canceled",  # the provider MOVED a completed job
                }

        outcome = await drill_remote_occupancy(
            MutatingLane(cycles=2, limit=2),
            cycles=2,
            reconcile_timeout_s=5.0,
            sample_interval_s=0.02,
        )
        assert outcome.outcome == "fail"
        assert any("releases no capacity" in violation for violation in outcome.violations)

    async def test_a_capacity_leak_is_a_violation_never_a_pass(self):
        lane = FakeRemoteDispatchLane(cycles=4, limit=2, leak_capacity=True)
        outcome = await drill_remote_occupancy(
            lane, cycles=4, reconcile_timeout_s=5.0, sample_interval_s=0.02
        )
        assert outcome.outcome == "fail"
        assert any("NEVER exceeded" in violation for violation in outcome.violations)

    async def test_a_lane_that_fails_to_drain_is_recorded_honestly(self):
        class NeverDrains(FakeRemoteDispatchLane):
            async def wait_project_drained(self, timeout_s: float) -> tuple[bool, float]:
                return False, timeout_s

        lane = NeverDrains(cycles=2, limit=2)
        outcome = await drill_remote_occupancy(
            lane, cycles=2, reconcile_timeout_s=0.2, sample_interval_s=0.02
        )
        assert outcome.outcome == "fail"
        assert any("drained the project to ZERO" in violation for violation in outcome.violations)


# ---------------------------------------------------------------------------
# Deployment drill 2 — backup/restore over the deployment's bytes
# ---------------------------------------------------------------------------


class TestDeploymentRestore:
    async def test_pinned_checkpoints_resolve_and_mismatch_refuses(self, seeded_store, tmp_path):
        outcome = await drill_restore_deployment(seeded_store, work_dir=tmp_path / "restore-work")
        assert outcome.violations == []
        reachability = outcome.signals["checkpoint.reachability"]
        assert reachability["works"] == 1
        assert reachability["works_resolved"] == 1
        assert reachability["pins"] == 1
        assert reachability["pins_resolved"] == 1
        assert reachability["mismatch_detected"] is True
        assert reachability["mismatch_refused"] is True
        assert reachability["mismatch_affected_works"] == ["wp-deploy-mismatch"]
        assert outcome.signals["restore_seconds"] >= 0.0

    async def test_the_mismatched_restore_writes_nothing(self, seeded_store, tmp_path):
        work = tmp_path / "restore-work-2"
        outcome = await drill_restore_deployment(seeded_store, work_dir=work)
        assert outcome.violations == []
        refused = work / "restore-refused"
        assert not refused.exists() or not any(refused.iterdir())

    async def test_every_restored_read_is_digest_verified(self, seeded_store, tmp_path):
        outcome = await drill_restore_deployment(seeded_store, work_dir=tmp_path / "w3")
        assert outcome.violations == []
        restored = FilesystemCheckpointRepository(tmp_path / "w3" / "restore-target")
        entry = await restored.entry("wp-deploy")
        assert entry is not None
        manifest, blobs = await restored.read_entry(entry)  # hashes to address
        assert manifest is not None and blobs


# ---------------------------------------------------------------------------
# Deployment drill 3 — credential isolation
# ---------------------------------------------------------------------------


RECORDED_ENVELOPE = {
    "resume_mode": "fresh",
    "checkpoint_digest": "",
    "decision_id": "",
    "attempt_generation": 0,
    "control_url": "https://forge.forcewake.me",
    "token_dispatched": True,
    "variable_keys": [
        "FORGE_LANE_RESUME_MODE",
        "FORGE_LANE_RESUME",
        "FORGE_RESUME_CHECKPOINT",
        "FORGE_ATTEMPT_GENERATION",
        "FORGE_CONTINUATION_DECISION_ID",
        "FORGE_LANE_CONTROL_URL",
        "FORGE_LANE_CONTROL_TOKEN",
    ],
    "digest": "7f1fed630bc0597effeab2814585d915a1bcfff5fbc7ed36d6cdcac41e3a655c",
}

CONTROL_PLANE_ENV = {name: "value-never-recorded" for name in CONTROL_PLANE_ROOT_CREDENTIAL_NAMES}


def _denied_probe(name: str) -> dict:
    return {
        "name": name,
        "url": "http://deployment.test/surface",
        "outcome": "denied-unauthorized",
        "status_code": 403,
        "detail": "refused (403)",
    }


class TestCredentialIsolation:
    def test_no_root_credential_in_the_model_facing_set(self):
        outcome = drill_credential_isolation(
            envelope=RECORDED_ENVELOPE,
            control_plane_env=CONTROL_PLANE_ENV,
            deny_probes=[_denied_probe("lane-control with the MCP master key")],
        )
        assert outcome.violations == []
        assert outcome.signals["attempt_generation"] == 0
        assert outcome.signals["token_dispatched"] is True
        assert "FORGE_LANE_CONTROL_TOKEN" in outcome.signals["model_facing_variable_keys"]
        assert "FORGE_LANE_CONTROL_SECRET" in outcome.signals["control_plane_root_names_checked"]

    def test_a_leaked_root_name_is_a_violation(self):
        leaked = dict(RECORDED_ENVELOPE)
        leaked["variable_keys"] = [*RECORDED_ENVELOPE["variable_keys"], "ZAI_API_KEY"]
        outcome = drill_credential_isolation(
            envelope=leaked,
            control_plane_env=CONTROL_PLANE_ENV,
            deny_probes=[],
        )
        assert outcome.outcome == "fail"
        assert any("ZAI_API_KEY" in violation for violation in outcome.violations)
        assert outcome.signals["credential_shaped_beyond_lane_token"] == ["ZAI_API_KEY"]

    def test_a_violated_deny_probe_is_a_violation(self):
        violated = _denied_probe("sentinel")
        violated["outcome"] = "violated"
        outcome = drill_credential_isolation(
            envelope=RECORDED_ENVELOPE,
            control_plane_env=CONTROL_PLANE_ENV,
            deny_probes=[violated],
        )
        assert outcome.outcome == "fail"
        assert any("ACTUAL denial" in violation for violation in outcome.violations)

    def test_credential_shaped_names_never_carry_values(self):
        names = credential_shaped_names({"ZAI_API_KEY": "secret", "HOME": "/x", "MCP_TOKEN": "t"})
        assert names == ["MCP_TOKEN", "ZAI_API_KEY"]


# ---------------------------------------------------------------------------
# Deployment drill 4 — degraded modes
# ---------------------------------------------------------------------------


class TestDegradedModes:
    async def test_every_mode_keeps_its_typed_bounded_behavior(self, tmp_path):
        async def health_ok() -> bool:
            return True

        async def control_ack_cycle() -> dict:
            return {
                "exercised": True,
                "received_to_applied_s": 11.4,
                "slow_ack_delay_s": 8.0,
            }

        outcome = await drill_degraded_modes(
            work_dir=tmp_path,
            health_ok=health_ok,
            control_ack_cycle=control_ack_cycle,
            revive_limit=2,
            control_objective_s=30.0,
        )
        assert outcome.violations == []
        assert outcome.signals["storage.quota_refusal"]["typed_refusal"] is True
        assert outcome.signals["storage.quota_refusal"]["store_intact"] is True
        assert outcome.signals["provider_throttling"]["revive_limit"] == 2
        assert max(outcome.signals["provider_throttling"]["backoff_ladder_s"]) <= 900
        assert outcome.signals["control.received_to_applied"]["received_to_applied_s"] == 11.4

    async def test_a_slow_ack_beyond_the_objective_is_a_violation(self, tmp_path):
        async def health_ok() -> bool:
            return True

        async def control_ack_cycle() -> dict:
            return {"exercised": True, "received_to_applied_s": 999.0}

        outcome = await drill_degraded_modes(
            work_dir=tmp_path,
            health_ok=health_ok,
            control_ack_cycle=control_ack_cycle,
            control_objective_s=30.0,
        )
        assert outcome.outcome == "fail"
        assert any("received→applied" in violation for violation in outcome.violations)

    async def test_the_health_gate_around_the_legs_is_asserted(self, tmp_path):
        states = iter([True, False])

        async def flapping_health() -> bool:
            return next(states, False)

        outcome = await drill_degraded_modes(
            work_dir=tmp_path,
            health_ok=flapping_health,
            control_ack_cycle=None,
        )
        assert outcome.outcome == "fail"
        assert any("fences were never disabled" in violation for violation in outcome.violations)

    async def test_the_throttling_budget_exhausts_never_loops(self, tmp_path):
        async def health_ok() -> bool:
            return True

        outcome = await drill_degraded_modes(
            work_dir=tmp_path, health_ok=health_ok, control_ack_cycle=None, revive_limit=1
        )
        assert outcome.violations == []
        throttling = outcome.signals["provider_throttling"]
        assert throttling["revive_limit"] == 1
        assert throttling["exhausted_budget"] == "blocked, no further revival"


# ---------------------------------------------------------------------------
# Deployment drill 5 — token rotation (the REAL generation-scoped path)
# ---------------------------------------------------------------------------


class TestTokenRotation:
    async def test_rotation_retires_the_old_secret_and_the_old_generation(self, tmp_path):
        outcome = await drill_token_rotation(
            secret_current="ops-secret-v1",
            secret_next="ops-secret-v2",
            work_id="rotationwork0001",
            work_dir=tmp_path / "rotation",
        )
        assert outcome.violations == []
        signal = outcome.signals["credential.rotation_generation"]
        assert signal["old_secret_token_status"] in (401, 403)
        assert signal["refusal_status"] == 403
        assert "superseded" in signal["refusal_detail"].lower()
        assert signal["new_secret_token_status"] == 200
        assert signal["refused_generation"] == 0 and signal["current_generation"] == 1

    async def test_the_refusal_names_both_generations(self, tmp_path):
        outcome = await drill_token_rotation(
            secret_current="a",
            secret_next="b",
            work_id="rotationwork0002",
            work_dir=tmp_path / "rotation",
        )
        detail = outcome.signals["credential.rotation_generation"]["refusal_detail"]
        assert "superseded runner generation (0" in detail and "generation 1" in detail


# ---------------------------------------------------------------------------
# The executed report round-trips
# ---------------------------------------------------------------------------


class TestExecutedReport:
    @pytest.mark.skipif(
        not EXECUTED_REPORT.is_file(),
        reason="the executed deployment-ops report is written by scripts/run_deployment_ops.py",
    )
    def test_the_executed_report_round_trips(self):
        document = json.loads(EXECUTED_REPORT.read_text(encoding="utf-8"))
        assert document["schema"] == "forge.deployment.ops/1"
        assert document["scope"] == DEPLOYMENT_DRILL_SCOPE
        assert document["read_only"] is True
        assert document["topology"]["observed"]["cas_volume"]["boundary"] == (
            CAS_UNSHARED_REPLICA_BOUNDARY
        )
        summary = document["summary"]
        assert summary["drills_run"] == len(document["drills"])
        assert summary["passed"] + summary["failed"] == summary["drills_run"]
        for drill in document["drills"]:
            assert drill["scope"] == DEPLOYMENT_DRILL_SCOPE
            assert drill["outcome"] in {"pass", "fail"}
            assert isinstance(drill["achieved_objectives"], list)
            assert isinstance(drill["tested_limits"], dict)
        # The objectives table carries THIS run's measured values.
        objectives = document["service_objectives"]["objectives"]
        labels = {entry["objective"] for entry in objectives}
        assert any(label.startswith("queue wait") for label in labels)
        assert any(label.startswith("control.received_to_applied") for label in labels)
        assert any(label.startswith("restore of the deployment") for label in labels)
        assert any(label.startswith("capacity") for label in labels)
        # No credential VALUE ever entered the report.
        text = EXECUTED_REPORT.read_text(encoding="utf-8")
        assert "PRIVATE-TOKEN" not in text
        for secret_field in ("get_secret_value()", "glpat-"):
            assert secret_field not in text

    @pytest.mark.skipif(
        not EXECUTED_REPORT.is_file(),
        reason="the executed deployment-ops report is written by scripts/run_deployment_ops.py",
    )
    def test_the_executed_report_is_strictly_json_serializable(self):
        document = json.loads(EXECUTED_REPORT.read_text(encoding="utf-8"))
        assert json.dumps(document, sort_keys=True)
