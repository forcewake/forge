"""R32-22 — qualify ONE two-writer WorkPackage against a complete CandidateSet.

The pins, in the order the issue states them:

- the scenario freezes the COMPLETE tested world (repo C at its PINNED
  digest, never its moved branch head; contract bundle, test bundle and
  environment profile all digested and persisted at freeze time);
- dependency-gated child start: the consumer lane CANNOT start while
  the producer is unproven or failed, and only a PROVEN outcome
  recorded against the package's ACTIVE world admits phase 2;
- the kill-at-step-k matrix, parametrized over every publication step
  of BOTH writers: restart reconciles with no duplicate publication, no
  destructive rollback (histories are prefix-preserved), idempotent
  re-recovery, and convergence to complete or parked;
- post-pivot (a human MERGED the first PR) recovery is FORWARD-ONLY:
  the merged publication stands, collisions park for a human;
- the lost-response window reconciles by ADOPTING the marker-carrying
  remote effect, never by re-publishing;
- readiness is the can-i-deploy QUERY over the recorded EvidenceLedger:
  stale evidence (a mutated consumer contract) blocks the invalidated
  edges BY NAME and is never silently reused;
- credential fan-out: read-only repositories never receive writer
  credential names, and each writer's credentials scope to its ONE repo;
- the report is versioned and deterministic end to end.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from forge.adaptive.two_writer_qualification import (
    PUBLICATION_STEPS,
    REPORT_SCHEMA,
    CredentialScopeViolation,
    KillCellResult,
    PhaseGatingArm,
    default_scenario,
    drive_dependency_gated_phases,
    kill_at_step_matrix,
    lost_response_adoption,
    pivot_kill_matrix,
    readiness,
    run_two_writer_qualification,
    verify_credential_scope,
)
from forge.adaptive.verification_sets import (
    EvidenceLedger,
    MemberChange,
    member_identity,
    record_evidence,
)
from forge.adaptive.workpackage import compile_dependencies, identity_changed, lane_assignment
from forge.models.base import Base

SCENARIO = default_scenario()


# ---------------------------------------------------------------------------
# Fixtures: a fresh aiosqlite engine per test (the workpackage-suite pattern).
# ---------------------------------------------------------------------------


async def _process(db_path: Path) -> tuple[async_sessionmaker[AsyncSession], AsyncEngine]:
    """A fresh engine + session factory over the file — one 'process'."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False), engine


# ---------------------------------------------------------------------------
# The scenario and its freeze.
# ---------------------------------------------------------------------------


class TestScenarioFreeze:
    def test_the_frozen_set_is_complete_and_carries_the_persisted_world(self):
        frozen = SCENARIO.freeze()
        roles = {member.repository_id: member.role for member in frozen.members}
        assert roles == {
            "repo-producer": "changed",
            "repo-consumer": "changed",
            "repo-pinned": "baseline",
            "repo-neighbor": "baseline",
        }
        # the world binding is PERSISTED at freeze time, not recomputable only
        assert frozen.tested_world_digest and len(frozen.tested_world_digest) == 64
        assert frozen.applicability_digest and frozen.applicability_digest != (
            frozen.tested_world_digest
        )
        assert frozen.contract_bundle_digest == SCENARIO.contract_bundle.digest()
        assert frozen.test_bundle_digest == SCENARIO.test_bundle_digest()
        assert frozen.environment_profile_digest == SCENARIO.environment_profile_digest()
        assert dict(frozen.environment_pins) == dict(SCENARIO.environment_pins)
        assert tuple(frozen.policy_refs) == SCENARIO.policy_refs

    def test_repo_c_rides_the_pinned_digest_not_the_branch_head(self):
        frozen = SCENARIO.freeze()
        pinned = next(
            member
            for member in frozen.members
            if member.repository_id == SCENARIO.pinned().repository_id
        )
        assert pinned.candidate_oid == SCENARIO.pinned().candidate_oid  # the PIN
        # the scenario's branch head HAS moved past the pin — the frozen
        # set must not silently ride the head
        assert SCENARIO.pinned().branch_head_oid
        assert pinned.candidate_oid != SCENARIO.pinned().branch_head_oid

    def test_the_package_is_the_two_write_n_read_shape(self):
        package = SCENARIO.package()
        assert package.validate() == []
        assert {item.item_id for item in package.writer_items()} == {
            "producer",
            "consumer",
        }
        readers = package.reader_repos()
        assert SCENARIO.pinned().repository_id in readers  # repo C: read-only item
        assert SCENARIO.neighbor().repository_id in readers  # repo D: context
        consumer = next(item for item in package.items if item.item_id == "consumer")
        assert consumer.depends_on == ("producer",)

    def test_the_consumer_compiles_into_phase_two_behind_the_producer(self):
        package = SCENARIO.package()
        phases = compile_dependencies(list(package.items))
        assert phases == [["pinned-baseline", "producer"], ["consumer"]]

    def test_the_three_edges_are_the_can_i_deploy_questions(self):
        edges = {edge.edge_id: edge for edge in SCENARIO.edges()}
        assert set(edges) == {
            "contract:repo-producer->repo-consumer",
            "baseline:repo-consumer->repo-pinned",
            "observation:repo-neighbor",
        }
        assert edges["contract:repo-producer->repo-consumer"].repositories == (
            "repo-producer",
            "repo-consumer",
        )
        # the observation edge covers ONLY the neighbor — a consumer-side
        # change must not invalidate it (the precision pinned below)
        assert edges["observation:repo-neighbor"].repositories == ("repo-neighbor",)

    def test_freeze_is_deterministic_for_the_same_scenario(self):
        assert SCENARIO.freeze().tested_world_digest == SCENARIO.freeze().tested_world_digest


# ---------------------------------------------------------------------------
# Dependency-gated child start.
# ---------------------------------------------------------------------------


class TestDependencyGatedPhases:
    async def test_an_unproven_producer_blocks_the_consumer(self, tmp_path):
        factory, engine = await _process(tmp_path / "tw.db")
        try:
            arm = await drive_dependency_gated_phases(
                factory, SCENARIO, parent_run_id="parent-happy"
            )
            assert isinstance(arm, PhaseGatingArm)
            assert arm.refusal_awaiting == ("producer",)  # launched-but-silent is NOT proof
            assert "refuses to advance" in arm.refusal_before_proof
            assert arm.consumer_launches_before_proof == 0  # never dispatched
            assert arm.producer_launched is True  # phase 1 did dispatch
        finally:
            await engine.dispose()

    async def test_a_failed_producer_blocks_the_consumer(self, tmp_path):
        factory, engine = await _process(tmp_path / "tw.db")
        try:
            arm = await drive_dependency_gated_phases(
                factory,
                SCENARIO,
                parent_run_id="parent-failed",
                producer_outcome="failed",
            )
            assert arm.refusal_failed == ("producer",)
            assert arm.consumer_launches_total == 0  # the consumer lane NEVER started
            assert arm.final_state == "failed"
            assert "failed items block their dependents" in arm.refusal_before_proof
        finally:
            await engine.dispose()

    async def test_only_a_proven_producer_admits_phase_two(self, tmp_path):
        factory, engine = await _process(tmp_path / "tw.db")
        try:
            arm = await drive_dependency_gated_phases(
                factory, SCENARIO, parent_run_id="parent-happy"
            )
            assert arm.producer_outcome == "succeeded"
            assert arm.consumer_launches_total == 1  # exactly once, after the proof
            assert arm.final_state == "complete"
            assert arm.phases == (("pinned-baseline", "producer"), ("consumer",))
        finally:
            await engine.dispose()

    async def test_an_outcome_from_a_different_tested_world_proves_nothing(self, tmp_path):
        factory, engine = await _process(tmp_path / "tw.db")
        try:
            arm = await drive_dependency_gated_phases(
                factory, SCENARIO, parent_run_id="parent-happy"
            )
            assert arm.wrong_world_outcome_status == "rejected"
            # and the rejection did not unblock anything by accident: the
            # REAL outcome (with the active world digest) still drove the
            # package to complete
            assert arm.final_state == "complete"
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# The kill-at-step-k matrix.
# ---------------------------------------------------------------------------


def _cells_by_key(cells: tuple[KillCellResult, ...]) -> dict[tuple[str, str], KillCellResult]:
    return {(cell.repository_id, cell.step): cell for cell in cells}


class TestKillAtStepMatrix:
    async def test_the_matrix_covers_every_step_of_both_writers(self):
        cells = await kill_at_step_matrix(SCENARIO)
        keys = set(_cells_by_key(cells))
        expected = {
            (repo.repository_id, step)
            for repo in SCENARIO.writer_repos()
            for step in PUBLICATION_STEPS
        }
        assert keys == expected
        assert len(cells) == 12

    @pytest.mark.parametrize(
        ("repository_id", "step"),
        [
            (repo.repository_id, step)
            for repo in SCENARIO.writer_repos()
            for step in PUBLICATION_STEPS
        ],
    )
    async def test_every_cell_reconciles_without_duplicates_or_destruction(
        self, repository_id, step
    ):
        cells = _cells_by_key(await kill_at_step_matrix(SCENARIO))
        cell = cells[(repository_id, step)]
        assert cell.saga_status == "complete", f"{repository_id}/{step}: {cell.to_document()}"
        # one DISTINCT publication per repo — the commit intent is idempotent
        assert dict(cell.commits_landed) == {
            "repo-producer": 1,
            "repo-consumer": 1,
        }
        assert all(count == 1 for count in cell.reviews_opened.values())
        # no destructive rollback: no force-push, no delete, and every
        # pre-restart branch history survives as a PREFIX of the recovered one
        assert cell.destructive_operations == ()
        assert cell.histories_preserved
        # a THIRD process over the reconciled state spends nothing
        assert cell.second_recovery_idempotent
        assert cell.invariants_hold
        assert not cell.parked_reasons  # nothing parks on a healthy provider
        assert not any(cell.adopted.values())  # and nothing needed adoption

    async def test_death_after_the_producers_record_is_between_the_publications(self):
        cells = _cells_by_key(await kill_at_step_matrix(SCENARIO))
        cell = cells[(SCENARIO.producer().repository_id, "record")]
        # the producer died exactly after its own publication completed:
        # recovery drives the consumer from scratch, both decided rungs stay
        assert cell.saga_status == "complete"
        assert cell.repo_statuses[SCENARIO.producer().repository_id] == "ready_for_review"
        assert cell.repo_statuses[SCENARIO.consumer().repository_id] == "ready_for_review"

    async def test_death_after_the_consumers_record_never_respends(self):
        cells = _cells_by_key(await kill_at_step_matrix(SCENARIO))
        cell = cells[(SCENARIO.consumer().repository_id, "record")]
        assert cell.commits_landed[SCENARIO.consumer().repository_id] == 1
        assert cell.reviews_opened[SCENARIO.consumer().repository_id] == 1
        assert cell.second_recovery_idempotent


class TestPivotForwardOnly:
    """Post-pivot (the producer's PR MERGED by a human), recovery never
    rolls back past the merge; a human edit on the consumer branch
    either parks or completes FORWARD."""

    async def test_every_pivot_cell_is_forward_only(self):
        cells = await pivot_kill_matrix(SCENARIO)
        producer_repo = SCENARIO.producer().repository_id
        for cell in cells:
            assert cell.invariants_hold, f"{cell.step}: {cell.to_document()}"
            # FORWARD-ONLY: the merged publication stands, untouched
            assert cell.repo_statuses[producer_repo] == "human_merged"
            assert cell.commits_landed[producer_repo] == 1  # never re-published
            assert cell.histories_preserved  # never rewritten or deleted
            assert cell.destructive_operations == ()
            # convergence: completed or parked-with-reason, nothing else
            assert cell.saga_status in {"complete", "parked"}
            if cell.saga_status == "parked":
                assert cell.parked_reasons, "a park without a reason is not a verdict"

    async def test_a_collision_in_the_open_effect_window_parks_the_consumer(self):
        cells = {cell.step: cell for cell in await pivot_kill_matrix(SCENARIO)}
        consumer_repo = SCENARIO.consumer().repository_id
        for step in ("commit_intent", "provider_commit"):
            cell = cells[step]
            assert cell.saga_status == "parked", step
            assert cell.repo_statuses[consumer_repo] == "parked_human"
            assert "human" in cell.parked_reasons[consumer_repo].lower()

    async def test_already_decided_cells_complete_forward(self):
        cells = {cell.step: cell for cell in await pivot_kill_matrix(SCENARIO)}
        consumer_repo = SCENARIO.consumer().repository_id
        for step in ("verify", "record"):
            cell = cells[step]
            assert cell.saga_status == "complete", step
            assert cell.repo_statuses[consumer_repo] == "ready_for_review"
        # from `preparing`/`fence_check` the current coordinator stacks its
        # commit ON TOP of the human edit (append-only, never a rewrite) —
        # forward, and honest about it: the PR will contain both
        for step in ("prepare", "fence_check"):
            cell = cells[step]
            assert cell.saga_status == "complete", step
            assert cell.histories_preserved and cell.destructive_operations == ()


class TestLostResponseAdoption:
    async def test_a_lost_commit_response_adopts_without_re_publishing(self):
        cell = await lost_response_adoption(SCENARIO)
        producer_repo = SCENARIO.producer().repository_id
        # the honest PARTIAL publication: producer unknown, consumer standing
        assert cell.first_pass_statuses[producer_repo] == "outcome_unknown"
        assert cell.first_pass_statuses[SCENARIO.consumer().repository_id] == "ready_for_review"
        # recovery closed the window by EVIDENCE, not a duplicate create
        assert cell.adopted[producer_repo] is True
        assert cell.commits_landed[producer_repo] == 1
        assert cell.saga_status == "complete"
        assert cell.invariants_hold


# ---------------------------------------------------------------------------
# readiness: the can-i-deploy query.
# ---------------------------------------------------------------------------


def _full_ledger() -> EvidenceLedger:
    frozen = SCENARIO.freeze()
    return EvidenceLedger().record(
        *[
            record_evidence(f"ev-{edge.edge_id}", frozen, edge.repositories)
            for edge in SCENARIO.edges()
        ]
    )


class TestReadiness:
    def test_the_happy_path_is_a_pure_lookup_over_recorded_evidence(self):
        frozen = SCENARIO.freeze()
        verdict = readiness(frozen, _full_ledger(), SCENARIO.edges())
        assert verdict.ready is True
        assert verdict.blocked_edges == ()
        assert verdict.tested_world_digest == frozen.tested_world_digest
        assert set(verdict.satisfied_evidence_ids) == {
            "ev-contract:repo-producer->repo-consumer",
            "ev-baseline:repo-consumer->repo-pinned",
            "ev-observation:repo-neighbor",
        }
        # the query is stable: asking twice re-reads the ledger, runs nothing
        assert readiness(frozen, _full_ledger(), SCENARIO.edges()) == verdict

    def test_a_missing_edge_is_blocked_by_name(self):
        frozen = SCENARIO.freeze()
        verdict = readiness(frozen, EvidenceLedger(), SCENARIO.edges())
        assert verdict.ready is False
        assert {blocked.edge_id for blocked in verdict.blocked_edges} == {
            edge.edge_id for edge in SCENARIO.edges()
        }
        assert all(blocked.reason_kind == "missing" for blocked in verdict.blocked_edges)

    def test_a_superseded_edge_is_stale_and_named(self):
        frozen = SCENARIO.freeze()
        members = {member.repository_id: member for member in frozen.members}
        consumer_repo = SCENARIO.consumer().repository_id
        ledger = _full_ledger()
        # the event-based invalidation: the consumer identity MOVED
        ledger = ledger.apply(
            MemberChange(
                repository_id=consumer_repo,
                previous=member_identity(members[consumer_repo]),
                current=None,
            )
        )
        verdict = readiness(frozen, ledger, SCENARIO.edges())
        assert verdict.ready is False
        named = {blocked.edge_id for blocked in verdict.blocked_edges}
        assert "contract:repo-producer->repo-consumer" in named
        stale = [blocked for blocked in verdict.blocked_edges if blocked.reason_kind == "stale"]
        assert stale and all("invalidated" in blocked.detail for blocked in stale)

    def test_a_mutated_consumer_contract_invalidates_the_frozen_evidence(self):
        frozen = SCENARIO.freeze()
        mutated = SCENARIO.mutate_consumer_contract().freeze()
        # BOTH identity fingerprints flip: a new consumer candidate AND a
        # changed contract bundle are a different tested world
        assert identity_changed(frozen, mutated) is True
        assert mutated.applicability_digest != frozen.applicability_digest

        verdict = readiness(mutated, _full_ledger(), SCENARIO.edges())
        assert verdict.ready is False
        named = {blocked.edge_id for blocked in verdict.blocked_edges}
        # the invalidated edges are named — never silently reused
        assert "contract:repo-producer->repo-consumer" in named
        assert "baseline:repo-consumer->repo-pinned" in named
        invalid = [b for b in verdict.blocked_edges if b.reason_kind == "invalid"]
        assert invalid and all("frozen identities" in b.detail for b in invalid)

    def test_the_neighbor_observation_survives_a_consumer_change(self):
        # per-edge precision: the D observation evidence covers ONLY the
        # neighbor, so the consumer's move does not invalidate it
        mutated = SCENARIO.mutate_consumer_contract().freeze()
        verdict = readiness(mutated, _full_ledger(), SCENARIO.edges())
        blocked_ids = {blocked.edge_id for blocked in verdict.blocked_edges}
        assert "observation:repo-neighbor" not in blocked_ids

    def test_a_moved_pin_blocks_the_baseline_edge(self):
        drifted = SCENARIO.mutate_pinned_baseline().freeze()
        assert identity_changed(SCENARIO.freeze(), drifted) is True
        verdict = readiness(drifted, _full_ledger(), SCENARIO.edges())
        named = {blocked.edge_id for blocked in verdict.blocked_edges}
        assert "baseline:repo-consumer->repo-pinned" in named

    def test_an_unfrozen_set_refuses_the_query(self):
        from forge.adaptive.models import CandidateSet

        bare = CandidateSet(
            work_id="tw-bare",
            plan_revision=1,
            work_contract_digest="a" * 64,
            members=[member.model_copy() for member in SCENARIO.freeze().members],
        )
        with pytest.raises(ValueError, match="freeze_verified_world first"):
            readiness(bare, _full_ledger(), SCENARIO.edges())


# ---------------------------------------------------------------------------
# Credential fan-out.
# ---------------------------------------------------------------------------


def _clean_staging() -> dict[str, list[str]]:
    return {
        "repo-producer": ["cred-write-repo-producer"],
        "repo-consumer": ["cred-write-repo-consumer"],
        "repo-pinned": ["cred-read-repo-pinned"],
        "repo-neighbor": ["cred-read-repo-neighbor"],
    }


class TestCredentialScope:
    def test_the_scenario_staging_is_scoped(self):
        violations = verify_credential_scope(lane_assignment(SCENARIO.package()), _clean_staging())
        assert violations == []

    def test_the_neighbor_never_receives_a_writer_credential(self):
        staging = {**_clean_staging(), "repo-neighbor": ["cred-write-repo-producer"]}
        violations = verify_credential_scope(lane_assignment(SCENARIO.package()), staging)
        assert len(violations) == 1
        violation = violations[0]
        assert isinstance(violation, CredentialScopeViolation)
        assert violation.repository_id == "repo-neighbor"
        assert violation.credential == "cred-write-repo-producer"
        assert "read-only" in str(violation)

    def test_a_writer_credential_cannot_cross_lanes(self):
        staging = {
            **_clean_staging(),
            "repo-consumer": ["cred-write-repo-producer", "cred-write-repo-consumer"],
        }
        owners = {
            "cred-write-repo-producer": "repo-producer",
            "cred-write-repo-consumer": "repo-consumer",
        }
        violations = verify_credential_scope(
            lane_assignment(SCENARIO.package()), staging, credential_owner=owners
        )
        assert len(violations) == 1
        assert violations[0].repository_id == "repo-consumer"
        assert violations[0].owner_repository_id == "repo-producer"
        assert "one writer, one repository" in str(violations[0])

    def test_a_name_staged_under_two_writers_is_a_leak_under_derived_ownership(self):
        # without the dispatch table the OWNER is ambiguous, but the LEAK
        # is not: one name under two writer repos is caught either way
        staging = {
            **_clean_staging(),
            "repo-consumer": ["cred-write-repo-producer", "cred-write-repo-consumer"],
        }
        violations = verify_credential_scope(lane_assignment(SCENARIO.package()), staging)
        assert len(violations) == 1
        assert "crossed lanes" in str(violations[0])

    def test_the_pinned_baseline_lane_never_receives_a_writer_credential(self):
        staging = {
            **_clean_staging(),
            "repo-pinned": ["cred-read-repo-pinned", "cred-write-repo-consumer"],
        }
        violations = verify_credential_scope(lane_assignment(SCENARIO.package()), staging)
        assert [violation.credential for violation in violations] == ["cred-write-repo-consumer"]

    def test_a_repository_outside_the_package_is_refused_writer_credentials(self):
        staging = {**_clean_staging(), "repo-unknown": ["cred-write-repo-producer"]}
        violations = verify_credential_scope(lane_assignment(SCENARIO.package()), staging)
        assert len(violations) == 1
        assert "no lane of this package" in str(violations[0])

    def test_read_credentials_in_read_only_lanes_are_fine(self):
        staging = {**_clean_staging(), "repo-neighbor": ["cred-read-repo-neighbor", "extra-read"]}
        assert verify_credential_scope(lane_assignment(SCENARIO.package()), staging) == []


# ---------------------------------------------------------------------------
# The versioned report.
# ---------------------------------------------------------------------------


class TestTwoWriterReport:
    async def test_the_report_is_versioned_and_complete(self, tmp_path):
        factory, engine = await _process(tmp_path / "tw.db")
        try:
            report = await run_two_writer_qualification(factory)
            doc = report.to_document()
            assert doc["schema"] == REPORT_SCHEMA == "forge.two-writer.qualification/1"
            assert len(doc["kill_matrix"]) == 12
            assert len(doc["pivot_matrix"]) == 6
            assert all(cell["invariants_hold"] for cell in doc["kill_matrix"])
            assert all(cell["invariants_hold"] for cell in doc["pivot_matrix"])
            assert doc["lost_response"]["invariants_hold"]
            # phase gating: both arms recorded
            assert doc["phase_gating"]["happy"]["final_state"] == "complete"
            assert doc["phase_gating"]["failed_producer"]["final_state"] == "failed"
            # readiness: the happy query ready, the stale query blocked by name
            ready = next(q for q in doc["readiness"] if q["query"] == "full-ledger@frozen")
            assert ready["ready"] is True
            stale = next(q for q in doc["readiness"] if "mutated-consumer-contract" in q["query"])
            assert stale["ready"] is False
            assert {blocked["edge_id"] for blocked in stale["blocked_edges"]} >= {
                "contract:repo-producer->repo-consumer"
            }
            # credential verdicts recorded, every adversarial probe caught
            assert doc["credential_scope"]["clean_staging_ok"] is True
            assert all(probe["caught"] for probe in doc["credential_scope"]["probes"])
            # human decision points: merges, the deploy promotion, the parks —
            # merge and deploy stay SEPARATE approvals
            kinds = [point["kind"] for point in doc["human_decision_points"]]
            assert kinds.count("merge_pr") == 2  # one standing PR per writer
            assert "promote_deploy" in kinds
            assert "resolve_parked" in kinds
            assert report.publication_reviews == {
                "repo-producer": "https://example.test/repo-producer/pull/1",
                "repo-consumer": "https://example.test/repo-consumer/pull/2",
            }
        finally:
            await engine.dispose()

    async def test_the_report_is_deterministic_across_fresh_runs(self, tmp_path):
        factory_a, engine_a = await _process(tmp_path / "a.db")
        factory_b, engine_b = await _process(tmp_path / "b.db")
        try:
            first = await run_two_writer_qualification(factory_a)
            second = await run_two_writer_qualification(factory_b)
            assert first.to_document() == second.to_document()
            assert first.report_digest == second.report_digest
            assert len(first.report_digest) == 64
        finally:
            await engine_a.dispose()
            await engine_b.dispose()
