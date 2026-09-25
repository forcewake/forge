"""R38-18 (issue #319) — the profile-bound deployment arms, at the
machinery level.

The frozen supported profile (#307) changed what a deployment
measurement MEANS: it is evidence only for the deployment whose
executed-lab bind the manifest names. What is held here:

- the profile binding: the committed manifest self-vouches (digest
  7c292dd8…), a matching observation renders qualified-for-profile,
  every mismatch axis (and a drifted manifest, and an unobserved axis)
  renders unqualified-for-profile with the difference named;
- the three review-named arms' typed outcomes against fakes/disposable
  stores: the lost dispatch response at the cap, the volume fill during
  a paused run, the mismatched-restore preflight refusing BEFORE any
  new model turn;
- the percentile computation (nearest-rank, stated scope);
- the reviewer-WIP policy field (admission vs reviewable volume);
- the sanitized publication shape (raw identifiers never publish);
- the EXECUTED report round-trips (skip when not on disk).
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from forge.adaptive.ops_drills import (
    PROFILE_BINDING_AXES,
    PROFILE_QUALIFIED,
    PROFILE_UNQUALIFIED,
    PUBLISHED_REPORT_STAMP,
    CapBoundaryLane,
    RemoteCycleRecord,
    RestorePreflightRefused,
    checkpoint_payload,
    drill_lost_response_at_cap,
    drill_mismatched_restore_preflight,
    drill_pause_cancel_percentiles,
    drill_volume_fill_during_pause,
    percentile_summary,
    profile_binding_row,
    profile_manifest_digest,
    restore_with_preflight,
    reviewer_wip_bound_row,
    summarize_for_publication,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FROZEN_PROFILE = REPO_ROOT / "qualification" / "profiles" / "supported-gitlab-ce-v1.json"
EXECUTED_REPORT = REPO_ROOT / "qualification" / "deployment-ops-2026-09-25.json"

#: The frozen profile's manifest digest (#307) — the executed-lab bind.
#: The frozen digest DERIVES from the committed manifest (a literal broke
#: when the closure-receipt repoint re-froze the document).
FROZEN_DIGEST = profile_manifest_digest(
    __import__("json").loads(
        (
            Path(__file__).resolve().parents[1]
            / "qualification/profiles/supported-gitlab-ce-v1.json"
        ).read_text()
    )
)


def _frozen_manifest() -> dict:
    return json.loads(FROZEN_PROFILE.read_text(encoding="utf-8"))


def _executed_lab() -> dict:
    return _frozen_manifest()["control_plane"]["executed_lab"]


def _matched_observation() -> dict[str, str]:
    lab = _executed_lab()
    return {
        "image_name": str(lab["image_name"]),
        "image_id": str(lab["image_id"]),
        "image_digest": str(lab["image_digest"]),
        "schema_head": str(lab["deployed_schema_head"]),
        "reported_version": str(lab["reported_version"]),
    }


def _matched_binding() -> dict:
    return profile_binding_row(_frozen_manifest(), _matched_observation())


# ---------------------------------------------------------------------------
# The profile binding
# ---------------------------------------------------------------------------


class TestProfileBinding:
    def test_the_committed_manifest_self_vouches_at_the_frozen_digest(self):
        manifest = _frozen_manifest()
        assert manifest["manifest_digest"] == FROZEN_DIGEST
        assert profile_manifest_digest(manifest) == FROZEN_DIGEST

    def test_a_matching_observation_renders_qualified_for_profile(self):
        binding = _matched_binding()
        assert binding["bind"] == "matched"
        assert binding["differences"] == []
        assert binding["qualification"] == PROFILE_QUALIFIED
        assert binding["manifest_digest"] == FROZEN_DIGEST
        assert binding["profile"] == "supported-gitlab-ce-v1"

    @pytest.mark.parametrize(
        ("axis", "observed_key", "wrong_value"),
        [
            ("image_name", "image_name", "localhost/forge:other"),
            ("image_id", "image_id", "0" * 64),
            ("image_digest", "image_digest", "sha256:" + "1" * 64),
            ("deployed_schema_head", "schema_head", "026"),
            ("reported_version", "reported_version", "0.36.0"),
        ],
    )
    def test_every_bind_axis_mismatch_renders_unqualified(self, axis, observed_key, wrong_value):
        binding = profile_binding_row(
            _frozen_manifest(), _matched_observation() | {observed_key: wrong_value}
        )
        assert binding["bind"] == "mismatched"
        assert binding["qualification"] == PROFILE_UNQUALIFIED
        assert any(difference.startswith(f"{axis}:") for difference in binding["differences"]), (
            binding["differences"]
        )

    def test_an_unobserved_axis_is_a_named_difference_never_a_pass(self):
        observation = _matched_observation()
        observation.pop("image_digest")
        binding = profile_binding_row(_frozen_manifest(), observation)
        assert binding["qualification"] == PROFILE_UNQUALIFIED
        assert any(
            "image_digest: not observed" in difference for difference in binding["differences"]
        )

    def test_a_drifted_manifest_does_not_vouch_for_itself(self):
        drifted = _frozen_manifest() | {"frozen_at": "2000-01-01T00:00:00+00:00"}
        binding = profile_binding_row(drifted, _matched_observation())
        assert binding["qualification"] == PROFILE_UNQUALIFIED
        assert any(
            "does not vouch for itself" in difference for difference in binding["differences"]
        )

    def test_the_bind_axes_cover_the_executed_lab_composition(self):
        manifest_keys = {manifest_key for _axis, manifest_key, _observed in PROFILE_BINDING_AXES}
        executed_lab = _executed_lab()
        for key in manifest_keys:
            assert key in executed_lab, f"the executed-lab bind no longer names {key}"
        assert len(PROFILE_BINDING_AXES) == 5


# ---------------------------------------------------------------------------
# Arm 1 — the lost dispatch response at the concurrency cap
# ---------------------------------------------------------------------------


class FakeCapBoundaryLane(CapBoundaryLane):
    """A deterministic phased cap-boundary lane: every cycle plans first
    (no lease), the dropped ``/go`` holds its slot as unknown occupancy,
    the fill ``/go`` cycles hold running leases, the over-cap ``/go``
    parks typed, the reconciler draining at the end."""

    def __init__(self, *, limit: int = 2, park_over_cap: bool = True) -> None:
        self.limit = limit
        self.park_over_cap = park_over_cap
        self.open_leases: dict[str, str] = {}
        self.cancelled: list[str] = []
        self.occupancy_log: list[dict[str, int]] = []
        self.phase_order: list[str] = []

    async def plan_cycle(self, index: int) -> RemoteCycleRecord:
        # The plan holds NO lease (a plan is not capacity).
        self.phase_order.append(f"plan:{index}")
        assert not self.open_leases, "a plan must never run while a lease is held"
        return RemoteCycleRecord(index=index, run_id=f"cap-run-{index:02d}", end_state="planned")

    async def go_dropped(self, index: int) -> RemoteCycleRecord:
        self.phase_order.append(f"go-dropped:{index}")
        record = RemoteCycleRecord(index=index, run_id=f"cap-run-{index:02d}")
        record.lease_acquired = True
        record.queue_wait_s = 0.2  # measured from the durable row, not the answer
        record.end_state = "dispatched_response_dropped"
        record.detail = "the dispatch response was dropped at the client seam"
        # The provider accepted the dispatch; the client never saw the
        # answer — occupancy is UNKNOWN and HOLDS its slot.
        self.open_leases[record.run_id] = "dispatched_unknown"
        await asyncio.sleep(0.05)
        return record

    async def go_fill(self, index: int) -> RemoteCycleRecord:
        self.phase_order.append(f"go-fill:{index}")
        record = RemoteCycleRecord(index=index, run_id=f"cap-run-{index:02d}")
        record.lease_acquired = True
        record.queue_wait_s = 0.1 + index * 0.05
        record.job_id = 7100 + index
        record.execution_s = 0.5
        record.end_state = "dispatched"
        self.open_leases[record.run_id] = "native_running"
        await asyncio.sleep(0.05)  # hold the slot so the sampler sees the cap
        return record

    async def go_over_cap(self, index: int) -> RemoteCycleRecord:
        self.phase_order.append(f"go-over:{index}")
        record = RemoteCycleRecord(index=index, run_id=f"cap-run-{index:02d}")
        if self.park_over_cap:
            record.end_state = "parked_execution_capacity"
            record.queue_wait_s = 0.3
            record.detail = "execution_capacity: no execution slot free in this project"
            return record
        # The pathological lane: capacity silently oversubscribes.
        record.end_state = "dispatched"
        record.queue_wait_s = 0.1
        self.open_leases[record.run_id] = "native_running"
        return record

    async def occupancy_snapshot(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for word in self.open_leases.values():
            counts[word] = counts.get(word, 0) + 1
        self.occupancy_log.append(dict(counts))
        return counts

    async def lease_state(self, run_id: str) -> str:
        return self.open_leases.get(run_id, "")

    async def cancel_immediately(self, record: RemoteCycleRecord) -> str:
        self.cancelled.append(record.run_id)
        self.open_leases[record.run_id] = "draining"
        return "ok:200"

    async def cancel_with_lost_response(self, record: RemoteCycleRecord) -> None:
        self.open_leases[record.run_id] = "draining"

    async def cancel_completed_job(self, record: RemoteCycleRecord) -> dict:
        return {"exercised": False}

    async def wait_project_drained(self, timeout_s: float) -> tuple[bool, float]:
        self.open_leases.clear()  # the reconciler's bounded pass
        return True, 0.25


class TestLostResponseAtCap:
    async def test_the_dropped_dispatch_holds_capacity_until_the_reconciler_resolves(self):
        lane = FakeCapBoundaryLane(limit=2)
        outcome = await drill_lost_response_at_cap(
            lane,
            profile_binding=_matched_binding(),
            reconcile_timeout_s=5.0,
            sample_interval_s=0.02,
        )
        assert outcome.violations == []
        signal = outcome.signals["execution.occupied_vs_limit"]
        assert signal["limit"] == 2
        assert signal["peak_occupied"] == 2
        assert signal["occupied_at_cap"] == 2
        # The unknown occupancy sits INSIDE the cap next to the running one.
        assert signal["occupancy_mix_at_cap"] == {
            "dispatched_unknown": 1,
            "native_running": 1,
        }
        unknown = outcome.signals["native.occupancy_unknown"]
        assert unknown["dropped_response_lease_word"] == "dispatched_unknown"
        assert unknown["over_cap_verdict"] == "parked_execution_capacity"
        assert "reconciler observation" in unknown["resolved_by"]

    async def test_the_over_cap_cycle_must_park_typed(self):
        class SilentOversubscriber(FakeCapBoundaryLane):
            def __init__(self) -> None:
                super().__init__(limit=2, park_over_cap=False)

        outcome = await drill_lost_response_at_cap(
            SilentOversubscriber(),
            profile_binding=_matched_binding(),
            reconcile_timeout_s=5.0,
            sample_interval_s=0.02,
        )
        assert outcome.outcome == "fail"
        assert any("parked with the TYPED verdict" in v for v in outcome.violations)

    async def test_a_dropped_response_that_releases_its_lease_is_a_violation(self):
        class VanishingLease(FakeCapBoundaryLane):
            async def go_dropped(self, index: int) -> RemoteCycleRecord:
                record = await super().go_dropped(index)
                # The pathological client: it treats its own dropped answer
                # as evidence the slot is free.
                self.open_leases.pop(record.run_id, None)
                return record

        outcome = await drill_lost_response_at_cap(
            VanishingLease(),
            profile_binding=_matched_binding(),
            reconcile_timeout_s=5.0,
            sample_interval_s=0.02,
        )
        assert outcome.outcome == "fail"
        assert any(
            "DROPPED native dispatch response released NOTHING" in v for v in outcome.violations
        )

    async def test_a_profile_bind_mismatch_renders_the_arm_unqualified(self):
        mismatched = profile_binding_row(
            _frozen_manifest(), _matched_observation() | {"schema_head": "026"}
        )
        assert mismatched["qualification"] == PROFILE_UNQUALIFIED
        outcome = await drill_lost_response_at_cap(
            FakeCapBoundaryLane(limit=2),
            profile_binding=mismatched,
            reconcile_timeout_s=5.0,
            sample_interval_s=0.02,
        )
        assert outcome.outcome == "fail"
        assert any(
            PROFILE_UNQUALIFIED in violation and "deployed_schema_head" in violation
            for violation in outcome.violations
        )

    async def test_the_cap_never_exceeds_the_limit_even_under_the_race(self):
        lane = FakeCapBoundaryLane(limit=3)
        outcome = await drill_lost_response_at_cap(
            lane,
            profile_binding=_matched_binding(),
            reconcile_timeout_s=5.0,
            sample_interval_s=0.01,
        )
        assert outcome.violations == []
        assert max(sum(snapshot.values()) for snapshot in lane.occupancy_log) <= 3


# ---------------------------------------------------------------------------
# Arm 2 — the checkpoint volume filled during a PAUSED run
# ---------------------------------------------------------------------------


class TestVolumeFillDuringPause:
    async def test_the_pinned_wip_survives_and_new_writes_refuse_typed(self, tmp_path):
        outcome = await drill_volume_fill_during_pause(
            tmp_path, profile_binding=_matched_binding(), safety_threshold_bytes=4096
        )
        assert outcome.violations == []
        signal = outcome.signals["storage.volume_fill"]
        assert signal["typed_refusals"] >= 1
        assert signal["silent_writes"] == 0
        assert signal["pinned_wip_survives"] is True
        assert signal["store_bytes_at_refusal"] <= 4096
        assert "typed refusal" in signal["admission_stop"]

    async def test_a_profile_bind_mismatch_renders_the_arm_unqualified(self, tmp_path):
        mismatched = profile_binding_row(
            _frozen_manifest(), _matched_observation() | {"image_id": "f" * 64}
        )
        outcome = await drill_volume_fill_during_pause(
            tmp_path, profile_binding=mismatched, safety_threshold_bytes=4096
        )
        assert outcome.outcome == "fail"
        assert any(PROFILE_UNQUALIFIED in violation for violation in outcome.violations)
        # The storage arm itself still measured typed behavior — the
        # failure is the BIND, reported as such.
        assert outcome.signals["storage.volume_fill"]["typed_refusals"] >= 1


# ---------------------------------------------------------------------------
# Arm 3 — the mismatched-restore preflight
# ---------------------------------------------------------------------------


class TestMismatchedRestorePreflight:
    async def _seeded_backup(self, work_dir: Path) -> Path:
        from forge.adaptive.checkpoint_repository import (
            FilesystemCheckpointRepository,
            backup_store,
        )

        root = work_dir / "source"
        repository = FilesystemCheckpointRepository(root)
        for sequence in range(2):
            manifest, blobs, checkpoint_id = checkpoint_payload("wp-preflight", sequence)
            await repository.put("wp-preflight", checkpoint_id, manifest, blobs)
        backup = await backup_store(root, work_dir / "backup")
        return backup.path

    async def test_a_consistent_snapshot_at_the_profile_head_restores(self, tmp_path):
        backup_dir = await self._seeded_backup(tmp_path)
        coverage = await restore_with_preflight(
            backup_dir,
            tmp_path / "target",
            expected_schema_head="027",
            observed_schema_head="027",
        )
        assert coverage["checkpoints"] >= 2

    async def test_a_wrong_schema_head_is_refused_typed_before_any_restore(self, tmp_path):
        backup_dir = await self._seeded_backup(tmp_path)
        target = tmp_path / "refused"
        with pytest.raises(RestorePreflightRefused) as refused:
            await restore_with_preflight(
                backup_dir,
                target,
                expected_schema_head="027",
                observed_schema_head="026",
            )
        assert refused.value.reason == "schema-head"
        assert "027" in refused.value.detail and "026" in refused.value.detail
        assert not target.exists() or not any(target.iterdir())

    async def test_the_drill_refuses_both_mismatch_shapes_before_any_model_turn(self, tmp_path):
        outcome = await drill_mismatched_restore_preflight(
            tmp_path, profile_binding=_matched_binding(), expected_schema_head="027"
        )
        assert outcome.violations == []
        signal = outcome.signals["preflight.restore_gate"]
        assert signal["refusals"] == {"backup-halves": True, "schema-head": True}
        assert signal["nothing_written"] is True
        assert signal["model_turns_before_refusals"] == 0
        # The resume dispatch (the first NEW model turn) runs only after
        # the consistent restore verified.
        assert signal["model_turns_after_consistent_restore"] == 1
        assert signal["restored_verified"] is True

    async def test_the_refused_targets_write_nothing(self, tmp_path):
        outcome = await drill_mismatched_restore_preflight(
            tmp_path / "work", profile_binding=_matched_binding()
        )
        assert outcome.violations == []
        for refused in ("preflight-refused-halves", "preflight-refused-schema"):
            target = tmp_path / "work" / refused
            assert not target.exists() or not any(target.iterdir())

    async def test_a_profile_bind_mismatch_renders_the_arm_unqualified(self, tmp_path):
        mismatched = profile_binding_row(
            _frozen_manifest(), _matched_observation() | {"reported_version": "0.36.0"}
        )
        outcome = await drill_mismatched_restore_preflight(tmp_path, profile_binding=mismatched)
        assert outcome.outcome == "fail"
        assert any(PROFILE_UNQUALIFIED in violation for violation in outcome.violations)


# ---------------------------------------------------------------------------
# The percentile computation
# ---------------------------------------------------------------------------


class TestPercentileSummary:
    def test_nearest_rank_percentiles_over_a_known_sample(self):
        summary = percentile_summary([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        assert summary == {
            "n": 10,
            "min_s": 1.0,
            "p50_s": 5.0,
            "p95_s": 10.0,
            "max_s": 10.0,
            "objective_s": None,
        }

    def test_small_samples_and_the_objective_travel_with_the_numbers(self):
        summary = percentile_summary([0.3, 0.1, 0.2], objective_s=2.0)
        assert summary["n"] == 3
        assert summary["min_s"] == 0.1
        assert summary["p50_s"] == 0.2
        assert summary["p95_s"] == 0.3  # nearest-rank: the worst of three
        assert summary["max_s"] == 0.3
        assert summary["objective_s"] == 2.0

    def test_an_empty_sample_set_never_fabricates_a_zero(self):
        summary = percentile_summary([])
        assert summary["n"] == 0
        assert summary["p50_s"] is None and summary["p95_s"] is None

    def test_duplicate_samples_are_kept_not_deduplicated(self):
        summary = percentile_summary([5.0, 5.0, 5.0, 9.0])
        assert summary["p50_s"] == 5.0
        assert summary["p95_s"] == 9.0


# ---------------------------------------------------------------------------
# The pause/cancel percentiles drill (REAL lane-control machinery)
# ---------------------------------------------------------------------------


class TestPauseCancelPercentiles:
    async def test_percentiles_are_measured_with_scope_through_the_real_ladder(self, tmp_path):
        outcome = await drill_pause_cancel_percentiles(
            tmp_path,
            profile_binding=_matched_binding(),
            cycles=6,
            upload_workers=2,
            puts_per_worker=3,
            provider_latency_s=0.02,
            control_objective_s=60.0,
        )
        assert outcome.violations == []
        signal = outcome.signals["control.pause_cancel_percentiles_s"]
        assert signal["control"]["n"] == 6
        assert signal["cancel_under_slow_provider"]["n"] == 6
        # STATED PERCENTILES + SCOPE, never one best-case latency.
        for key in ("p50_s", "p95_s", "max_s"):
            assert signal["control"][key] is not None
        assert "n=6" in signal["scope"]
        assert "not a fleet claim" in signal["scope"]

    async def test_a_profile_bind_mismatch_renders_the_measurement_unqualified(self, tmp_path):
        mismatched = profile_binding_row(
            _frozen_manifest(), _matched_observation() | {"image_name": "localhost/forge:x"}
        )
        outcome = await drill_pause_cancel_percentiles(
            tmp_path,
            profile_binding=mismatched,
            cycles=2,
            upload_workers=1,
            puts_per_worker=2,
            control_objective_s=60.0,
        )
        assert outcome.outcome == "fail"
        assert any(PROFILE_UNQUALIFIED in violation for violation in outcome.violations)


# ---------------------------------------------------------------------------
# The reviewer-WIP policy field
# ---------------------------------------------------------------------------


class TestReviewerWipBound:
    def test_the_bound_is_a_stated_policy_field_not_a_claim(self):
        row = reviewer_wip_bound_row(admission_limit=3, reviewer_wip_bound=5)
        assert row["coherent"] is True
        assert row["reviewer_wip_bound"] == 5
        assert row["admission_bound"] == 3
        assert row["throughput_claim"] is None
        assert "STATED POLICY FIELD" in row["unit"]
        assert "stays within" in row["statement"]

    def test_an_admission_bound_beyond_the_reviewable_volume_names_itself(self):
        row = reviewer_wip_bound_row(admission_limit=8, reviewer_wip_bound=5)
        assert row["coherent"] is False
        assert "EXCEEDS" in row["statement"]
        assert "reviewable volume" in row["statement"]


# ---------------------------------------------------------------------------
# The sanitized publication
# ---------------------------------------------------------------------------

IDENTIFIER_RUN = "b05e7708a6714b899997dd93454fd3bb"


def _full_report_shape() -> dict:
    return {
        "schema": "forge.deployment.ops/1",
        "issue": "R38-18 (#319)",
        "generated_at": "2026-09-25T00:00:00+00:00",
        "scope": "measured on the actual lab deployment",
        "read_only": True,
        "runbook": "docs/operations/deployment-boundaries.md",
        "profile": _matched_binding(),
        "summary": {
            "drills_run": 2,
            "passed": 2,
            "failed": 0,
            "refused_sections": 1,
            "profile_qualification": PROFILE_QUALIFIED,
            "policy_findings": 0,
        },
        "measured_limits": {
            "profile": {"manifest_digest": FROZEN_DIGEST},
            "concurrency": {"limit": 3, "peak_occupied": 3},
            "pause_cancel_responsiveness": {"scope": "n=12 cycles, disposable fixture"},
            "restore_time_s": {"measured": 0.152},
        },
        "reviewer_wip": reviewer_wip_bound_row(admission_limit=3, reviewer_wip_bound=5),
        "private_diagnostics": {
            "retained": True,
            "location_class": "outside the repository, access-controlled (never a public artifact)",
        },
        "refusals": [
            {
                "section": "credentials",
                "reason": f"boom at /Users/secret/runner with run {IDENTIFIER_RUN}",
            }
        ],
        "drills": [
            {
                "drill": "deployment_remote_occupancy",
                "outcome": "pass",
                "profile": _matched_binding(),
                "achieved_objectives": ["objective one"],
                "tested_limits": {"cycles": 4, "observed_limit": 3},
                "signals": {
                    "execution.occupied_vs_limit": {
                        "limit": 3,
                        "peak_occupied": 3,
                        "cycles": 4,
                        "dispatched": 3,
                        "parked": ["parked_execution_capacity"],
                    },
                    "queue_wait_s": {
                        "max": 3.34,
                        "per_cycle": {"0": 0.591, "1": 1.212},
                    },
                    "cycle_end_states": {
                        "0": {"state": "dispatched", "detail": "pipeline 409, lane job 755"}
                    },
                    "drained_after_s": 20.4,
                },
                "violations": [],
                "lane_notes": [f"failed-cancel leg: job 754 (success) — run {IDENTIFIER_RUN}"],
            },
            {
                "drill": "deployment_lost_response_at_cap",
                "outcome": "pass",
                "profile": _matched_binding(),
                "achieved_objectives": ["objective two"],
                "tested_limits": {"observed_limit": 3},
                "signals": {
                    "execution.occupied_vs_limit": {
                        "limit": 3,
                        "peak_occupied": 3,
                        "occupied_at_cap": 3,
                        "occupancy_mix_at_cap": {"dispatched_unknown": 1, "native_running": 2},
                        "occupancy_words_seen": ["dispatched_unknown", "native_running"],
                    },
                    "queue_wait_s": {"measured_from": "the durable lease row"},
                    "drained_after_s": 18.2,
                },
                "violations": [],
            },
        ],
    }


class TestSanitizedPublication:
    def test_the_published_summary_carries_the_digest_limits_and_policy(self):
        published = summarize_for_publication(_full_report_shape())
        assert published["schema"] == PUBLISHED_REPORT_STAMP
        assert published["profile"]["manifest_digest"] == FROZEN_DIGEST
        assert published["profile"]["qualification"] == PROFILE_QUALIFIED
        assert published["summary"]["profile_qualification"] == PROFILE_QUALIFIED
        assert published["measured_limits"]["concurrency"]["limit"] == 3
        assert published["reviewer_wip"]["reviewer_wip_bound"] == 5
        assert published["private_diagnostics"]["retained"] is True
        assert published["refusals"] == [{"section": "credentials"}]  # section only

    def test_raw_identifiers_never_publish(self):
        published = summarize_for_publication(_full_report_shape())
        text = json.dumps(published)
        assert IDENTIFIER_RUN not in text
        assert "pipeline 409" not in text
        assert "job 754" not in text
        assert "/Users/secret" not in text
        assert "lane_notes" not in text
        assert "cycle_end_states" not in text
        assert "per_cycle" not in text
        assert "PRIVATE-TOKEN" not in text and "glpat-" not in text

    def test_every_drill_row_names_the_manifest_digest(self):
        published = summarize_for_publication(_full_report_shape())
        for drill in published["drills"]:
            assert drill["profile"]["manifest_digest"] == FROZEN_DIGEST
            assert drill["profile"]["qualification"] == PROFILE_QUALIFIED

    def test_the_public_signals_keep_counts_and_drop_identifiers(self):
        published = summarize_for_publication(_full_report_shape())
        by_name = {drill["drill"]: drill for drill in published["drills"]}
        occupancy = by_name["deployment_remote_occupancy"]["signals"]
        assert occupancy["execution.occupied_vs_limit"]["peak_occupied"] == 3
        assert occupancy["queue_wait_s"] == {"max": 3.34}
        cap = by_name["deployment_lost_response_at_cap"]["signals"]
        assert cap["execution.occupied_vs_limit"]["occupancy_mix_at_cap"] == {
            "dispatched_unknown": 1,
            "native_running": 2,
        }

    def test_an_unknown_drill_publishes_no_signals(self):
        report = _full_report_shape()
        report["drills"] = [
            {
                "drill": "deployment_future_arm",
                "outcome": "pass",
                "profile": _matched_binding(),
                "achieved_objectives": [],
                "tested_limits": {},
                "signals": {"secret.detail": {"run_id": IDENTIFIER_RUN}},
                "violations": [],
            }
        ]
        published = summarize_for_publication(report)
        assert published["drills"][0]["signals"] == {}
        assert IDENTIFIER_RUN not in json.dumps(published)


# ---------------------------------------------------------------------------
# The executed report round-trips
# ---------------------------------------------------------------------------


class TestExecutedReport:
    @pytest.mark.skipif(
        not EXECUTED_REPORT.is_file(),
        reason="the executed report is written by scripts/run_deployment_ops.py",
    )
    def test_the_executed_report_is_the_sanitized_summary_bound_to_the_profile(self):
        document = json.loads(EXECUTED_REPORT.read_text(encoding="utf-8"))
        assert document["schema"] == PUBLISHED_REPORT_STAMP
        assert document["profile"]["manifest_digest"] == FROZEN_DIGEST
        assert document["profile"]["qualification"] == PROFILE_QUALIFIED
        assert document["summary"]["profile_qualification"] == PROFILE_QUALIFIED
        # Every drill row names the digest.
        assert document["drills"], "the executed report carries no drills"
        for drill in document["drills"]:
            assert drill["profile"]["manifest_digest"] == FROZEN_DIGEST
            assert drill["outcome"] in {"pass", "fail"}
        # The measured-limits table is the runbook's numbers.
        limits = document["measured_limits"]
        assert limits["profile"]["manifest_digest"] == FROZEN_DIGEST
        for key in (
            "concurrency",
            "pause_cancel_responsiveness",
            "command_latency_under_contention",
            "restore_time_s",
        ):
            assert key in limits
        # The reviewer-WIP policy field.
        assert document["reviewer_wip"]["reviewer_wip_bound"] >= 1
        assert document["reviewer_wip"]["coherent"] is True

    @pytest.mark.skipif(
        not EXECUTED_REPORT.is_file(),
        reason="the executed report is written by scripts/run_deployment_ops.py",
    )
    def test_the_executed_report_publishes_no_raw_diagnostics(self):
        text = EXECUTED_REPORT.read_text(encoding="utf-8")
        document = json.loads(text)
        # No run/job/pipeline identifiers, no credentials, no host paths.
        assert re.findall(r"\b[0-9a-f]{32}\b", text) == []
        assert re.findall(r"pipeline \d+", text) == []
        assert re.findall(r"\bjob \d+\b", text) == []
        assert "PRIVATE-TOKEN" not in text
        assert "glpat-" not in text
        assert "/Users/" not in text
        assert document["private_diagnostics"]["retained"] is True
        assert "outside the repository" in document["private_diagnostics"]["location_class"]

    @pytest.mark.skipif(
        not EXECUTED_REPORT.is_file(),
        reason="the executed report is written by scripts/run_deployment_ops.py",
    )
    def test_the_executed_report_is_strictly_json_serializable(self):
        document = json.loads(EXECUTED_REPORT.read_text(encoding="utf-8"))
        assert json.dumps(document, sort_keys=True)
