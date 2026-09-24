"""#278 / R36-19: verify a complete two-service CandidateSet AS A SYSTEM.

These are the DETERMINISTIC REFERENCE twin's pins
(``TWIN_REFERENCE_LABEL``, retained per R37-15/#296): the in-process
twin stays the cheap, reproducible semantics pin beside the verified
executor (``tests/test_verification_executor.py`` — the separate
subprocess, the BUILT wheel artifacts, the real-socket broker arm).

The pins, in the issue's own order:

- the tested-world identity is COMPLETE — changed AND unchanged
  revisions, image digests, contract bundle, test bundle, environment
  profile and policy refs — so an applicability input change in ANY
  member flips the digest, and a rebuilt image under an UNCHANGED
  source SHA (the review's exact arm) flips it too;
- the verifier itself holds NO model credentials and NO publication
  authority, and PROVES both absences in its environment document;
- the PYTHON-shaped twin runs end-to-end: the focused contract replay
  with synthetic messages, the consumer→pinned-baseline projection, a
  REAL sqlite schema upgrade from a pinned baseline with seeded data
  preserved under the canary's fingerprint discipline, and exactly-once
  semantics under a deterministic double delivery with the failure
  injected between state persistence and message acknowledgement;
- a failed edge NAMES its failing member and blocks system-ready, and
  missing evidence blocks the readiness query;
- invalidation is SELECTIVE by dependency inputs: a changed baseline
  image or test bundle invalidates only the affected edges, with the
  drift named and the history retained for audit;
- readiness is a QUERY — pure lookup, never a re-run — reporting
  verification readiness, merge permission and deployment permission
  as three DISTINCT booleans.
"""

from __future__ import annotations

import dataclasses
from dataclasses import replace

import pytest

from forge.adaptive.models import CandidateSet, CandidateSetMember
from forge.adaptive.system_verification import (
    BASELINE_API_V2,
    BASELINE_API_V3,
    CrashWindowOpened,
    DoubleDeliveryHarness,
    TwinContract,
    TwinScenario,
    TwinService,
    VerifierAuthorityError,
    VerifierEnvironmentDocument,
    baseline_drift,
    default_twin_scenario,
    default_verifier_environment,
    execute_schema_upgrade,
    forbid_verifier_authority,
    replay_against_changed_inputs,
    run_system_verification,
    system_readiness,
)
from forge.adaptive.verification_sets import (
    EvidenceLedger,
    bound_applicability_digest,
    bound_tested_world_digest,
    freeze_tested_world,
)

PRODUCER = "orders-api"
CONSUMER = "orders-projection"
PINNED = "ledger-baseline"
ENV_EDGE = "environment:integration"
CONTRACT_EDGE = f"contract:{PRODUCER}->{CONSUMER}"
BASELINE_EDGE = f"baseline:{CONSUMER}->{PINNED}"


def _hex(seed: str) -> str:
    """A hex-only 64-char seed product (the harness convention)."""
    return (seed * 64)[:64]


def _frozen() -> tuple[TwinScenario, CandidateSet]:
    twin = default_twin_scenario()
    return twin, twin.freeze()


def _with_service(twin: TwinScenario, service: TwinService) -> TwinScenario:
    return replace(
        twin,
        services=tuple(
            service if s.repository_id == service.repository_id else s for s in twin.services
        ),
    )


class TestCompleteTestedWorldIdentity:
    """The full-digest flip on EVERY applicability input change."""

    def test_changed_and_baseline_members_are_all_in_the_frozen_identity(self):
        _twin, frozen = _frozen()
        members = {member.repository_id: member for member in frozen.members}
        assert set(members) == {PRODUCER, CONSUMER, PINNED}
        assert members[PRODUCER].role == "changed"
        assert members[CONSUMER].role == "changed"
        assert members[PINNED].role == "baseline"
        # the PIN, not the branch head, is the frozen baseline identity
        assert members[PINNED].candidate_oid == members[PINNED].base_oid
        assert dict(frozen.environment_pins)  # the external deps ride exact pins
        assert frozen.contract_bundle_digest and frozen.test_bundle_digest
        assert frozen.environment_profile_digest and frozen.policy_refs

    def test_a_changed_revision_flips_both_digests(self):
        _twin, frozen = _frozen()
        twin = default_twin_scenario()
        producer = replace(twin.producer(), candidate_oid=_hex("a7e8")[:40])
        moved = _with_service(twin, producer).freeze()
        assert moved.tested_world_digest != frozen.tested_world_digest
        assert moved.applicability_digest != frozen.applicability_digest

    def test_a_rebuilt_image_flips_the_digest_even_with_identical_source_shas(self):
        # the review's exact arm: source SHAs match, the artifact does not.
        _twin, frozen = _frozen()
        twin = default_twin_scenario()
        rebuilt = twin.with_rebuilt_image(PRODUCER, "f1e2").freeze()
        before = {m.repository_id: m for m in frozen.members}
        after = {m.repository_id: m for m in rebuilt.members}
        assert after[PRODUCER].candidate_oid == before[PRODUCER].candidate_oid
        assert after[PRODUCER].image_digest != before[PRODUCER].image_digest
        assert rebuilt.tested_world_digest != frozen.tested_world_digest
        assert rebuilt.applicability_digest != frozen.applicability_digest

    def test_a_rebuilt_baseline_image_flips_the_digest_too(self):
        _twin, frozen = _frozen()
        twin = default_twin_scenario()
        rebuilt = twin.with_rebuilt_image(PINNED, "e4f5").freeze()
        assert rebuilt.tested_world_digest != frozen.tested_world_digest
        assert rebuilt.applicability_digest != frozen.applicability_digest

    def test_a_changed_contract_bundle_flips_the_tested_world(self):
        _twin, frozen = _frozen()
        twin = default_twin_scenario()
        rewided = replace(
            twin,
            shared_contract=TwinContract(
                schema_version="v2",
                fields=("id", "total", "region", "note"),
                required=("id", "total", "region"),
            ),
        ).freeze()
        assert rewided.tested_world_digest != frozen.tested_world_digest
        # the contract bundle is this-run obligation, not a dependency:
        # applicability alone does not move (the tested-world digest does).
        assert rewided.applicability_digest == frozen.applicability_digest

    def test_a_changed_test_bundle_flips_both_digests(self):
        _twin, frozen = _frozen()
        twin = default_twin_scenario()
        rebundled = twin.with_changed_test_bundle("region-backfill-replay").freeze()
        assert rebundled.tested_world_digest != frozen.tested_world_digest
        assert rebundled.applicability_digest != frozen.applicability_digest

    def test_a_changed_environment_profile_flips_both_digests(self):
        _twin, frozen = _frozen()
        twin = default_twin_scenario()
        reprofiled = replace(twin, dependencies=(*twin.dependencies, "orders-cache")).freeze()
        assert reprofiled.tested_world_digest != frozen.tested_world_digest
        assert reprofiled.applicability_digest != frozen.applicability_digest

    def test_a_changed_environment_pin_flips_both_digests(self):
        _twin, frozen = _frozen()
        twin = default_twin_scenario()
        repinned = replace(
            twin,
            environment_pins=(
                ("orders-db", f"sha256:{_hex('9a8b')}"),
                ("orders-bus", dict(twin.environment_pins)["orders-bus"]),
            ),
        ).freeze()
        assert repinned.tested_world_digest != frozen.tested_world_digest
        assert repinned.applicability_digest != frozen.applicability_digest

    def test_a_changed_policy_ref_flips_both_digests(self):
        _twin, frozen = _frozen()
        twin = default_twin_scenario()
        repoliced = replace(twin, policy_refs=("compat/orders-matrix@2",)).freeze()
        assert repoliced.tested_world_digest != frozen.tested_world_digest
        assert repoliced.applicability_digest != frozen.applicability_digest

    def test_the_same_world_refreezes_to_the_same_digests(self):
        twin = default_twin_scenario()
        assert twin.freeze() == twin.freeze()
        assert twin.freeze().tested_world_digest == twin.freeze().tested_world_digest

    def test_freeze_tested_world_refuses_replacing_a_recorded_bundle_digest(self):
        _twin, frozen = _frozen()
        with pytest.raises(ValueError, match="one frozen set records one world"):
            freeze_tested_world(frozen, contract_bundle_digest=_hex("feed"))
        with pytest.raises(ValueError, match="must be a lowercase 64-hex sha256"):
            freeze_tested_world(
                CandidateSet(
                    work_id="wp-x",
                    plan_revision=1,
                    work_contract_digest=_hex("c0ffee"),
                    members=(
                        CandidateSetMember(
                            repository_id=PRODUCER,
                            base_oid=_hex("a1")[:40],
                            candidate_oid=_hex("a2")[:40],
                            image_digest=f"sha256:{_hex('a3')}",
                            role="changed",
                        ),
                    ),
                ),
                test_bundle_digest="not-a-digest",
            )


class TestVerifierAuthority:
    """No model credentials, no publication authority — proven, not claimed."""

    def _env(self, **kwargs: object) -> VerifierEnvironmentDocument:
        twin = default_twin_scenario()
        clean = default_verifier_environment(twin)
        return replace(clean, **kwargs)  # type: ignore[arg-type]

    def test_a_clean_environment_is_accepted(self):
        forbid_verifier_authority(default_verifier_environment(default_twin_scenario()))

    def test_model_credentials_are_refused_before_anything_executes(self):
        environment = self._env(model_credentials=("OPENAI_API_KEY", "ANTHROPIC_API_KEY"))
        with pytest.raises(VerifierAuthorityError, match="model credentials present"):
            run_system_verification(default_twin_scenario().freeze(), environment)

    def test_publication_authority_is_refused(self):
        environment = self._env(publication_authority=True)
        with pytest.raises(VerifierAuthorityError, match="publication authority granted"):
            run_system_verification(default_twin_scenario().freeze(), environment)

    def test_the_report_environment_document_proves_both_absences(self):
        twin, frozen = _frozen()
        report = run_system_verification(frozen, default_verifier_environment(twin))
        document = report.environment_document
        assert document["model_credentials"] == []
        assert document["publication_authority"] is False
        assert document["lane"]["profile"] == "trusted-integration-v1"
        assert document["lane"]["runs_code"] is True
        # the composed services bind to the frozen world's exact artifacts
        members = {member.repository_id: member for member in frozen.members}
        composed = {svc["service"]: svc for svc in document["composed_services"]}
        assert composed[PRODUCER]["artifact_digest"] == members[PRODUCER].image_digest
        assert composed[PINNED]["artifact_digest"] == members[PINNED].image_digest
        assert (
            composed["orders-db"]["artifact_digest"] == dict(frozen.environment_pins)["orders-db"]
        )
        assert all(svc.get("unresolved") is not True for svc in document["composed_services"])


class TestPythonTwinEndToEnd:
    """The twin verifier run: contract replay, baseline projection, the
    real schema upgrade and the double-delivery idempotency."""

    def test_the_happy_world_is_system_ready_with_every_edge_passed(self):
        twin, frozen = _frozen()
        report = run_system_verification(frozen, default_verifier_environment(twin))
        assert report.system_ready is True
        assert report.failed_members == ()
        assert {edge.edge_id: edge.status for edge in report.edge_results} == {
            CONTRACT_EDGE: "passed",
            BASELINE_EDGE: "passed",
            ENV_EDGE: "passed",
        }
        assert report.tested_world_digest == bound_tested_world_digest(frozen)
        assert report.applicability_digest == bound_applicability_digest(frozen)

    def test_the_contract_edge_replays_synthetic_messages(self):
        twin, frozen = _frozen()
        report = run_system_verification(frozen, default_verifier_environment(twin))
        (contract,) = [e for e in report.edge_results if e.kind == "contract"]
        replay = [c for c in contract.checks if c["check"] == "message-replay"]
        assert replay[0]["status"] == "passed"
        assert replay[0]["messages"] == 12
        assert replay[0]["required_fields"] == ["id", "total", "region"]
        # the must_verify contract descriptors were satisfied BY EXECUTION
        descriptors = [c for c in contract.checks if c["check"] == "contract-descriptor"]
        assert descriptors and all(c["status"] == "verified-by-execution" for c in descriptors)

    def test_the_baseline_edge_projects_the_pinned_rows(self):
        twin, frozen = _frozen()
        report = run_system_verification(frozen, default_verifier_environment(twin))
        (baseline,) = [e for e in report.edge_results if e.kind == "baseline"]
        projection = [c for c in baseline.checks if c["check"] == "projection-replay"]
        assert projection[0]["projected"] == projection[0]["rows"] == 15
        assert projection[0]["served_api"] == BASELINE_API_V3
        assert PINNED in baseline.detail

    def test_the_environment_edge_upgrades_seed_bearing_schema_and_preserves_data(self):
        twin, frozen = _frozen()
        report = run_system_verification(frozen, default_verifier_environment(twin))
        (environment,) = [e for e in report.edge_results if e.kind == "environment"]
        upgrade = [c for c in environment.checks if c["check"] == "db-upgrade"][0]
        assert upgrade["status"] == "passed"
        assert upgrade["plan_steps"] == [
            "snapshot_baseline",
            "apply_migrations",
            "verify_synthetic_data",
        ]
        assert upgrade["seed_rows"] == 25  # data-bearing, not an empty schema
        assert upgrade["preserved"] is True
        assert upgrade["baseline_fingerprint"] == upgrade["target_fingerprint"]
        assert upgrade["schema_advanced"] is True  # the ladder really moved
        assert upgrade["post_upgrade_write"] is True  # new-shaped writes accepted

    def test_the_environment_edge_asserts_exactly_once_under_double_delivery(self):
        twin, frozen = _frozen()
        report = run_system_verification(frozen, default_verifier_environment(twin))
        (environment,) = [e for e in report.edge_results if e.kind == "environment"]
        delivery = [c for c in environment.checks if c["check"] == "double-delivery"][0]
        assert delivery["status"] == "passed"
        assert delivery["exactly_once_all"] is True
        # the full fixed catalog replayed: crash-redelivery, duplicate, order
        assert delivery["scenarios"] == {
            "crash_between_commit_and_ack": 5,
            "out_of_order_events": 7,
            "redelivery_duplicate": 3,
        }

    def test_every_passed_edge_records_evidence_bound_to_the_frozen_world(self):
        twin, frozen = _frozen()
        report = run_system_verification(frozen, default_verifier_environment(twin))
        assert [record.evidence_id for record in report.evidence_records] == [
            f"ev-{CONTRACT_EDGE}",
            f"ev-{BASELINE_EDGE}",
            f"ev-{ENV_EDGE}",
        ]
        # the environment edge covers EVERY member (the compose binds them all)
        (environment_record,) = [
            r for r in report.evidence_records if r.evidence_id == f"ev-{ENV_EDGE}"
        ]
        assert {dep.repository_id for dep in environment_record.dependencies} == {
            PRODUCER,
            CONSUMER,
            PINNED,
        }
        ledger = EvidenceLedger().record(*report.evidence_records)
        assert ledger.applicable_to(frozen) == {
            f"ev-{BASELINE_EDGE}",
            f"ev-{CONTRACT_EDGE}",
            f"ev-{ENV_EDGE}",
        }

    def test_report_coverage_is_recorded(self):
        twin, frozen = _frozen()
        report = run_system_verification(frozen, default_verifier_environment(twin))
        coverage = report.report_coverage
        assert coverage["edges_expected"] == sorted([CONTRACT_EDGE, BASELINE_EDGE, ENV_EDGE])
        assert coverage["edges_verified"] == coverage["edges_expected"]
        assert coverage["edges_failed"] == [] and coverage["edges_missing"] == []
        assert coverage["complete"] is True
        assert coverage["checks_executed"] == sum(len(edge.checks) for edge in report.edge_results)

    def test_the_run_is_deterministic(self):
        twin, frozen = _frozen()
        first = run_system_verification(frozen, default_verifier_environment(twin))
        second = run_system_verification(frozen, default_verifier_environment(twin))
        assert first.to_document() == second.to_document()

    def test_an_unfrozen_set_refuses_to_verify(self):
        twin = default_twin_scenario()
        unfrozen = CandidateSet(
            work_id=twin.scenario_id,
            plan_revision=1,
            work_contract_digest=twin.work_contract_digest,
            members=tuple(
                CandidateSetMember(
                    repository_id=service.repository_id,
                    base_oid=service.base_oid,
                    candidate_oid=service.candidate_oid,
                    image_digest=service.image_digest,
                    role="changed" if service.kind != "pinned" else "baseline",
                )
                for service in twin.services
            ),
        )
        with pytest.raises(ValueError, match="freeze_verified_world first"):
            run_system_verification(unfrozen, default_verifier_environment(twin))

    def test_a_twin_that_is_not_the_frozen_membership_refuses(self):
        twin, frozen = _frozen()
        stranger = replace(
            twin,
            services=(
                *twin.services,
                TwinService(
                    repository_id="orders-audit",
                    kind="pinned",
                    base_oid=_hex("d1")[:40],
                    candidate_oid=_hex("d1")[:40],
                    image_digest=f"sha256:{_hex('d2')}",
                ),
            ),
        )
        with pytest.raises(ValueError, match="must be exactly the frozen set's members"):
            run_system_verification(frozen, default_verifier_environment(stranger))


class TestEdgeFailureNaming:
    """A consumer/producer incompatibility produces a NAMED failed edge
    that blocks system-ready — the old/new version combinations."""

    def test_the_old_producer_build_fails_the_contract_edge_naming_the_producer(self):
        twin = default_twin_scenario().with_old_producer()
        report = run_system_verification(twin.freeze(), default_verifier_environment(twin))
        assert report.system_ready is False
        assert report.failed_members == (PRODUCER,)
        (failed,) = [e for e in report.edge_results if e.status == "failed"]
        assert failed.edge_id == CONTRACT_EDGE
        assert failed.failed_member == PRODUCER
        assert "dialect v1" in failed.detail and PRODUCER in failed.detail

    def test_the_old_consumer_build_fails_the_contract_edge_naming_the_consumer(self):
        twin = default_twin_scenario().with_old_consumer()
        report = run_system_verification(twin.freeze(), default_verifier_environment(twin))
        assert report.system_ready is False
        assert report.failed_members == (CONSUMER,)
        (failed,) = [e for e in report.edge_results if e.status == "failed"]
        assert failed.failed_member == CONSUMER

    def test_a_failed_edge_records_no_evidence_and_covers_it_as_failed(self):
        twin = default_twin_scenario().with_old_producer()
        report = run_system_verification(twin.freeze(), default_verifier_environment(twin))
        assert report.report_coverage["edges_failed"] == [CONTRACT_EDGE]
        assert report.report_coverage["complete"] is False
        # only the still-passing edges recorded evidence
        assert {r.evidence_id for r in report.evidence_records} == {
            f"ev-{BASELINE_EDGE}",
            f"ev-{ENV_EDGE}",
        }

    def test_a_stale_pin_fails_the_baseline_edge_naming_the_pinned_member(self):
        twin = default_twin_scenario()
        stale_pin = _with_service(twin, replace(twin.pinned(), baseline_api=BASELINE_API_V2))
        report = run_system_verification(
            stale_pin.freeze(), default_verifier_environment(stale_pin)
        )
        assert report.system_ready is False
        (failed,) = [e for e in report.edge_results if e.status == "failed"]
        assert failed.edge_id == BASELINE_EDGE
        assert failed.failed_member == PINNED
        assert "api/v2" in failed.detail


class TestSchemaUpgradeMechanism:
    """The upgrade leg is a real mechanism — the preservation check has
    teeth (a destructive migration is CAUGHT, not assumed away)."""

    def test_a_destructive_migration_is_caught_by_the_fingerprint(self, tmp_path):
        from forge.adaptive.system_verification import TwinMigration, _synthetic_seed_rows

        destructive = (
            TwinMigration(
                version="1",
                statements=(
                    "CREATE TABLE _schema_version (version TEXT PRIMARY KEY)",
                    "CREATE TABLE orders (id TEXT PRIMARY KEY, total INTEGER NOT NULL)",
                ),
            ),
            # the sharpest arm: the schema ADVANCES (region exists) but the
            # seeded rows DIE — only the data fingerprint can catch this.
            TwinMigration(
                version="2",
                statements=(
                    "DROP TABLE orders",
                    "CREATE TABLE orders"
                    " (id TEXT PRIMARY KEY, total INTEGER NOT NULL,"
                    " region TEXT NOT NULL DEFAULT 'eu')",
                ),
            ),
        )
        outcome = execute_schema_upgrade(
            tmp_path / "destructive.db", destructive, seed_rows=_synthetic_seed_rows(5)
        )
        assert outcome.preserved is False
        assert outcome.schema_advanced is True  # the ladder moved; the DATA died
        assert "did NOT preserve" in outcome.detail

    def test_the_upgrade_never_runs_from_an_empty_schema(self, tmp_path):
        outcome = execute_schema_upgrade(tmp_path / "upgrade.db")
        assert outcome.seed_rows == 25
        assert outcome.baseline_fingerprint[0].startswith("orders 25 ")
        assert outcome.preserved is True


class TestDoubleDeliveryHarness:
    """The idempotency twin: exactly-once under crash-redelivery."""

    def _message(self, index: int) -> dict[str, object]:
        return {
            "id": f"ord-{index:04d}",
            "total": 100 + index,
            "region": "eu",
        }

    def test_the_crash_window_lands_between_persistence_and_ack(self, tmp_path):
        harness = DoubleDeliveryHarness(tmp_path / "delivery.db")
        try:
            outcome = harness.deliver(self._message(1), crash_between_commit_and_ack=True)
        finally:
            harness.close()
        assert outcome.deliveries == 2  # the redelivery happened
        assert outcome.effects == 1  # exactly ONE projected effect
        assert outcome.acks == 1
        assert outcome.exactly_once is True

    def test_a_pure_duplicate_redelivery_adds_no_second_effect(self, tmp_path):
        harness = DoubleDeliveryHarness(tmp_path / "delivery.db")
        try:
            first = harness.deliver(self._message(7))
            duplicate = harness.deliver(self._message(7), scenario="redelivery_duplicate")
        finally:
            harness.close()
        assert first.effects == duplicate.effects == 1
        assert duplicate.deliveries == 2  # this call: the delivery + the duplicate

    def test_out_of_order_arrival_is_still_exactly_once(self, tmp_path):
        harness = DoubleDeliveryHarness(tmp_path / "delivery.db")
        try:
            outcomes = [
                harness.deliver(self._message(index), crash_between_commit_and_ack=True)
                for index in (3, 1, 2)
            ]
        finally:
            harness.close()
        assert all(outcome.exactly_once for outcome in outcomes)

    def test_the_crash_signal_is_raised_only_inside_the_window(self, tmp_path):
        harness = DoubleDeliveryHarness(tmp_path / "delivery.db")
        try:
            with pytest.raises(CrashWindowOpened):
                harness._handle(self._message(9), die_before_ack=True)
        finally:
            harness.close()


class TestSelectiveInvalidation:
    """Replay a previously-passed record against changed inputs: ONLY the
    affected edges invalidate; history retained for audit."""

    def _passed_world(self) -> tuple[TwinScenario, CandidateSet, EvidenceLedger]:
        twin, frozen = _frozen()
        report = run_system_verification(frozen, default_verifier_environment(twin))
        return twin, frozen, EvidenceLedger().record(*report.evidence_records)

    def test_a_rebuilt_baseline_image_invalidates_only_the_affected_edges(self):
        twin, frozen, ledger = self._passed_world()
        mutated = twin.with_rebuilt_image(PINNED, "e4f5").freeze()
        replay = replay_against_changed_inputs(ledger, frozen, mutated)
        # the environment edge covers EVERY member; the baseline edge covers
        # the pinned member; the CONTRACT edge covers only the two writers.
        assert set(replay.invalidated_evidence_ids) == {
            f"ev-{BASELINE_EDGE}",
            f"ev-{ENV_EDGE}",
        }
        assert replay.retained_evidence_ids == (f"ev-{CONTRACT_EDGE}",)

    def test_the_drift_is_named_and_flags_the_unchanged_source_sha(self):
        twin, frozen, ledger = self._passed_world()
        mutated = twin.with_rebuilt_image(PINNED, "e4f5").freeze()
        replay = replay_against_changed_inputs(ledger, frozen, mutated)
        (drift,) = replay.drift
        assert drift.input_path == f"member/{PINNED}/image_digest"
        assert drift.source_sha_unchanged is True  # the review's exact arm
        assert drift.previous != drift.current

    def test_a_changed_test_bundle_invalidates_every_record_judged_under_the_old(self):
        twin, frozen, ledger = self._passed_world()
        rebundled = twin.with_changed_test_bundle("region-backfill-replay").freeze()
        replay = replay_against_changed_inputs(ledger, frozen, rebundled)
        assert set(replay.invalidated_evidence_ids) == {
            f"ev-{BASELINE_EDGE}",
            f"ev-{CONTRACT_EDGE}",
            f"ev-{ENV_EDGE}",
        }

    def test_the_history_is_retained_for_audit(self):
        twin, frozen, ledger = self._passed_world()
        mutated = twin.with_rebuilt_image(PINNED, "e4f5").freeze()
        replay = replay_against_changed_inputs(ledger, frozen, mutated)
        by_id = {record.evidence_id: record for record in replay.applied_ledger.records}
        assert set(by_id) == {  # nobody was deleted
            f"ev-{BASELINE_EDGE}",
            f"ev-{CONTRACT_EDGE}",
            f"ev-{ENV_EDGE}",
        }
        superseded = by_id[f"ev-{BASELINE_EDGE}"]
        assert superseded.superseded is True
        assert PINNED in superseded.superseded_reason  # the drift is the reason
        assert by_id[f"ev-{CONTRACT_EDGE}"].superseded is False

    def test_an_unchanged_world_invalidates_nothing(self):
        _twin, frozen, ledger = self._passed_world()
        replay = replay_against_changed_inputs(ledger, frozen, frozen)
        assert replay.drift == ()
        assert replay.invalidated_evidence_ids == ()
        assert set(replay.retained_evidence_ids) == {
            f"ev-{BASELINE_EDGE}",
            f"ev-{CONTRACT_EDGE}",
            f"ev-{ENV_EDGE}",
        }

    def test_a_changed_environment_pin_is_replayed_per_service(self):
        twin, frozen, ledger = self._passed_world()
        pins = dict(twin.environment_pins)
        repinned = replace(
            twin,
            environment_pins=(
                ("orders-db", f"sha256:{_hex('9a8b')}"),
                ("orders-bus", pins["orders-bus"]),
            ),
        ).freeze()
        replay = replay_against_changed_inputs(ledger, frozen, repinned)
        paths = {drift.input_path for drift in replay.drift}
        assert paths == {"environment_pin/orders-db"}
        # every record from the frozen set pinned orders-db at persistence
        # time, so the changed dependency judge invalidates them ALL —
        # the same world is never judged under two different databases.
        assert set(replay.invalidated_evidence_ids) == {
            f"ev-{BASELINE_EDGE}",
            f"ev-{CONTRACT_EDGE}",
            f"ev-{ENV_EDGE}",
        }


class TestReadinessQuery:
    """Readiness is a QUERY over executed evidence — never a re-run —
    with the three permissions distinct."""

    def _ready_world(self) -> tuple[TwinScenario, CandidateSet, EvidenceLedger]:
        twin, frozen = _frozen()
        report = run_system_verification(frozen, default_verifier_environment(twin))
        return twin, frozen, EvidenceLedger().record(*report.evidence_records)

    def test_happy_path_ready_with_every_edge_satisfied(self):
        _twin, frozen, ledger = self._ready_world()
        twin = default_twin_scenario()
        verdict = system_readiness(ledger, frozen, twin.edges())
        assert verdict.verification_ready is True
        assert verdict.blocked_edges == ()
        assert set(verdict.satisfied_evidence_ids) == {
            f"ev-{BASELINE_EDGE}",
            f"ev-{CONTRACT_EDGE}",
            f"ev-{ENV_EDGE}",
        }

    def test_missing_evidence_blocks_every_edge_by_name(self):
        _twin, frozen, _ledger = self._ready_world()
        twin = default_twin_scenario()
        verdict = system_readiness(EvidenceLedger(), frozen, twin.edges())
        assert verdict.verification_ready is False
        assert [(b.edge_id, b.reason_kind) for b in verdict.blocked_edges] == [
            (CONTRACT_EDGE, "missing"),
            (BASELINE_EDGE, "missing"),
            (ENV_EDGE, "missing"),
        ]

    def test_stale_evidence_blocks_by_name_after_invalidation(self):
        twin, frozen, ledger = self._ready_world()
        mutated = twin.with_rebuilt_image(PINNED, "e4f5").freeze()
        replay = replay_against_changed_inputs(ledger, frozen, mutated)
        verdict = system_readiness(replay.applied_ledger, mutated, twin.edges())
        assert verdict.verification_ready is False
        blocked = {b.edge_id: b.reason_kind for b in verdict.blocked_edges}
        assert blocked == {BASELINE_EDGE: "stale", ENV_EDGE: "stale"}

    def test_evidence_from_a_different_world_is_invalid_never_reused(self):
        twin, frozen, _ledger = self._ready_world()
        other = twin.with_rebuilt_image(PINNED, "e4f5").freeze()
        report = run_system_verification(frozen, default_verifier_environment(twin))
        foreign = EvidenceLedger().record(*report.evidence_records)
        verdict = system_readiness(foreign, other, twin.edges())
        assert verdict.verification_ready is False
        assert {b.reason_kind for b in verdict.blocked_edges} == {"invalid"}
        assert all("never silently reused" in b.detail for b in verdict.blocked_edges)

    def test_a_failed_edge_blocks_even_with_standing_evidence(self):
        twin, frozen, ledger = self._ready_world()
        verdict = system_readiness(ledger, frozen, twin.edges(), failed_edge_ids=[CONTRACT_EDGE])
        assert verdict.verification_ready is False
        (blocked,) = verdict.blocked_edges
        assert (blocked.edge_id, blocked.reason_kind) == (CONTRACT_EDGE, "failed")

    def test_an_unfrozen_set_refuses_the_query(self):
        twin = default_twin_scenario()
        with pytest.raises(ValueError, match="freeze_verified_world first"):
            system_readiness(
                EvidenceLedger(),
                CandidateSet(
                    work_id="wp-x",
                    plan_revision=1,
                    work_contract_digest=_hex("c0ffee"),
                    members=(
                        CandidateSetMember(
                            repository_id=PRODUCER,
                            base_oid=_hex("a1")[:40],
                            candidate_oid=_hex("a2")[:40],
                            image_digest=f"sha256:{_hex('a3')}",
                            role="changed",
                        ),
                    ),
                ),
                twin.edges(),
            )

    def test_the_query_is_pure_calling_twice_changes_nothing(self):
        _twin, frozen, ledger = self._ready_world()
        twin = default_twin_scenario()
        records_before = ledger.records
        first = system_readiness(ledger, frozen, twin.edges())
        second = system_readiness(ledger, frozen, twin.edges())
        assert first == second  # same answer, no side effects
        assert ledger.records is records_before  # the ledger was not touched

    def test_the_three_permissions_are_distinct_booleans(self):
        _twin, frozen, ledger = self._ready_world()
        twin = default_twin_scenario()
        # a fully verified world authorizes NEITHER merge NOR deployment
        ungranted = system_readiness(ledger, frozen, twin.edges())
        assert (
            ungranted.verification_ready,
            ungranted.merge_permitted,
            ungranted.deploy_permitted,
        ) == (True, False, False)
        document = ungranted.to_document()
        assert document["verification_ready"] is True
        assert document["merge_permitted"] is False
        assert document["deploy_permitted"] is False
        # explicit human grants are recorded as their OWN booleans
        granted = system_readiness(
            ledger, frozen, twin.edges(), merge_approval=True, deploy_approval=True
        )
        assert (granted.verification_ready, granted.merge_permitted, granted.deploy_permitted) == (
            True,
            True,
            True,
        )
        # and a blocked world with grants still reports readiness False
        blocked = system_readiness(EvidenceLedger(), frozen, twin.edges(), deploy_approval=True)
        assert (blocked.verification_ready, blocked.deploy_permitted) == (False, True)

    def test_a_passed_environment_test_authorizes_no_deployment(self):
        # the environment edge PASSED (upgrade + idempotency green) while the
        # contract edge failed: the pass authorizes no production migration.
        twin = default_twin_scenario().with_old_producer()
        frozen = twin.freeze()
        report = run_system_verification(frozen, default_verifier_environment(twin))
        (environment,) = [e for e in report.edge_results if e.kind == "environment"]
        assert environment.status == "passed"
        ledger = EvidenceLedger().record(*report.evidence_records)
        verdict = system_readiness(ledger, frozen, twin.edges(), failed_edge_ids=[CONTRACT_EDGE])
        assert verdict.verification_ready is False
        assert verdict.deploy_permitted is False  # no grant, no derivation
        assert verdict.merge_permitted is False


class TestBaselineDriftNaming:
    """``verification.baseline_drift`` names the moved inputs."""

    def test_no_drift_between_identical_worlds(self):
        _twin, frozen = _frozen()
        assert baseline_drift(frozen, frozen) == ()

    def test_every_input_kind_is_named(self):
        twin, frozen = _frozen()
        mutated = replace(
            twin.with_rebuilt_image(PINNED, "e4f5").with_changed_test_bundle("x1"),
            policy_refs=("compat/orders-matrix@2",),
            environment_pins=(
                ("orders-db", f"sha256:{_hex('9a8b')}"),
                ("orders-bus", dict(twin.environment_pins)["orders-bus"]),
            ),
        ).freeze()
        paths = {drift.input_path for drift in baseline_drift(frozen, mutated)}
        assert paths == {
            f"member/{PINNED}/image_digest",
            "test_bundle_digest",
            "environment_pin/orders-db",
            "policy_refs",
        }

    def test_the_rebuilt_image_arm_is_flagged_source_sha_unchanged(self):
        twin, frozen = _frozen()
        moved = _with_service(
            twin, replace(twin.producer(), candidate_oid=_hex("a7e8")[:40])
        ).freeze()
        rebuilt = twin.with_rebuilt_image(PRODUCER, "f1e2").freeze()
        assert all(
            d.source_sha_unchanged is False
            for d in baseline_drift(frozen, moved)
            if d.input_path.endswith("candidate_oid")
        )
        (image_drift,) = [
            d for d in baseline_drift(frozen, rebuilt) if d.input_path.endswith("image_digest")
        ]
        assert image_drift.source_sha_unchanged is True

    def test_member_appearance_and_removal_are_named(self):
        twin, frozen = _frozen()
        without_pinned = replace(
            twin, services=tuple(s for s in twin.services if s.kind != "pinned")
        ).freeze()
        member_drifts = [
            d for d in baseline_drift(frozen, without_pinned) if d.input_path.startswith("member/")
        ]
        (removal,) = member_drifts
        assert removal.input_path == f"member/{PINNED}"
        assert removal.current == "<absent>"
        (appearance,) = [
            d for d in baseline_drift(without_pinned, frozen) if d.input_path.startswith("member/")
        ]
        assert appearance.input_path == f"member/{PINNED}"
        assert appearance.previous == "<absent>"

    def test_provenance_moves_nothing(self):
        twin, frozen = _frozen()
        renumbered = frozen.model_copy(update={"plan_revision": 42, "work_id": "wp-other"})
        assert baseline_drift(frozen, renumbered) == ()

    def test_the_drift_rows_render_the_observability_fragment(self):
        twin, frozen = _frozen()
        mutated = twin.with_rebuilt_image(PINNED, "e4f5").freeze()
        (drift,) = baseline_drift(frozen, mutated)
        assert drift.as_document() == {
            "input": f"member/{PINNED}/image_digest",
            "previous": drift.previous,
            "current": drift.current,
            "source_sha_unchanged": True,
        }

    def test_the_twin_report_carries_the_drift_against_a_prior_world(self):
        twin, frozen = _frozen()
        prior = twin.freeze()  # the same world: no drift
        report = run_system_verification(
            frozen, default_verifier_environment(twin), prior_world=prior
        )
        assert report.baseline_drift == ()
        drifted_world = twin.with_rebuilt_image(PINNED, "e4f5")
        report = run_system_verification(
            drifted_world.freeze(),
            default_verifier_environment(drifted_world),
            prior_world=frozen,
        )
        assert [d.input_path for d in report.baseline_drift] == [f"member/{PINNED}/image_digest"]


class TestFrozenShapes:
    """The report/verdict values are frozen; the twin composes with the
    existing two-writer machinery instead of duplicating it."""

    def test_the_report_and_verdict_are_frozen(self):
        twin, frozen = _frozen()
        report = run_system_verification(frozen, default_verifier_environment(twin))
        with pytest.raises(dataclasses.FrozenInstanceError):
            report.system_ready = False  # type: ignore[misc]

    def test_the_twin_world_digest_is_the_workpackage_digest(self):
        # composition, not duplication: the twin's freeze IS the existing
        # tested_world_digest over the same inputs.
        from forge.adaptive.workpackage import tested_world_digest as world_digest

        twin, frozen = _frozen()
        assert frozen.tested_world_digest == world_digest(
            frozen,
            environment=dict(frozen.environment_pins),
            policy_refs=frozen.policy_refs,
        )

    def test_the_ledger_interops_with_the_two_writer_readiness(self):
        # the evidence records the twin writes are the SAME EvidenceRecord
        # shape the two-writer can-i-deploy query consumes.
        from forge.adaptive.two_writer_qualification import DependencyEdge, readiness

        twin, frozen = _frozen()
        report = run_system_verification(frozen, default_verifier_environment(twin))
        ledger = EvidenceLedger().record(*report.evidence_records)
        contract_edge = DependencyEdge(
            edge_id=CONTRACT_EDGE,
            kind="contract",
            repositories=(PRODUCER, CONSUMER),
            description="twin contract edge",
        )
        verdict = readiness(frozen, ledger, [contract_edge])
        assert verdict.ready is True
