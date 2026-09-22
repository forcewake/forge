"""The MRP core: packages, phases, lanes, the publication saga, frozen candidates.

These tests pin the review's separations — the service graph is not the
execution DAG, one writable repository per child lane, publication as an
explicit saga with no pretend rollback, and verification bound to a
frozen candidate-set identity: the COMPLETE tested world (NXT-25), with
historical provenance deliberately kept out of it and applicability
kept distinct from it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from forge.adaptive.models import CandidateSet, CandidateSetMember
from forge.adaptive.workpackage import (
    OutcomeApplication,
    PhaseAdvanceRefused,
    SagaState,
    WorkItemRef,
    WorkPackage,
    WorkPackageCoordinator,
    WorkPackageStateError,
    applicability_digest,
    baseline_members,
    bound_phases,
    compile_dependencies,
    freeze_candidate_set,
    identity_changed,
    lane_assignment,
    read_workpackage_state,
    recovery_targets,
    saga_outcomes,
)
from forge.durable import FlowRun, Outbox
from forge.models.base import Base

# aliased: the real name starts with "test" and pytest would collect the
# imported FUNCTION as a test of its own.
from forge.adaptive.workpackage import tested_world_digest as world_digest
from forge.adaptive.verification_sets import freeze_verified_world


def _oid(seed: str) -> str:
    """A valid lowercase 40-hex git sha (repeated seed, still exactly 40 hex)."""
    return (seed * 40)[:40]


def _digest(seed: str) -> str:
    """A valid lowercase 64-hex sha256."""
    return (seed * 64)[:64]


def _image_digest(seed: str) -> str:
    """A sha256-prefixed image digest, as registries spell them."""
    return f"sha256:{_digest(seed)}"


def _valid_package() -> WorkPackage:
    return WorkPackage(
        package_id="pkg-1",
        objective="Widen the widget API across services",
        items=(
            WorkItemRef(item_id="api", repository_id="widgets", writable=True),
            WorkItemRef(
                item_id="consumer",
                repository_id="consumers",
                writable=True,
                depends_on=("api",),
            ),
            WorkItemRef(item_id="contract-check", repository_id="contracts", writable=False),
        ),
        read_only_repositories=("runbooks",),
    )


class TestWorkPackageValidation:
    def test_a_valid_package_has_no_violations(self):
        assert _valid_package().validate() == []

    def test_empty_items_is_a_violation(self):
        package = WorkPackage(package_id="pkg-empty", objective="nothing to authorize")
        violations = package.validate()
        assert any("no items" in v for v in violations)

    def test_duplicate_item_ids_are_a_violation(self):
        package = WorkPackage(
            package_id="pkg-2",
            objective="dup ids",
            items=(
                WorkItemRef(item_id="lane", repository_id="widgets"),
                WorkItemRef(item_id="lane", repository_id="consumers"),
            ),
        )
        assert any("duplicate item id" in v for v in package.validate())

    def test_one_writable_lane_per_repository(self):
        package = WorkPackage(
            package_id="pkg-3",
            objective="dup repository",
            items=(
                WorkItemRef(item_id="left", repository_id="widgets"),
                WorkItemRef(item_id="right", repository_id="widgets"),
            ),
        )
        violations = package.validate()
        assert any("widgets" in v and "one writable lane" in v for v in violations)

    def test_dependency_on_unknown_item_is_a_violation(self):
        package = WorkPackage(
            package_id="pkg-4",
            objective="unknown dep",
            items=(WorkItemRef(item_id="lane", repository_id="widgets", depends_on=("ghost",)),),
        )
        assert any("unknown item ghost" in v for v in package.validate())

    def test_all_violations_are_reported_at_once(self):
        package = WorkPackage(
            package_id="pkg-5",
            objective="multiply broken",
            items=(
                WorkItemRef(item_id="a", repository_id="widgets", depends_on=("ghost",)),
                WorkItemRef(item_id="a", repository_id="widgets"),
            ),
        )
        violations = package.validate()
        # empty? no. dup id, dup repository, unknown dep — all present.
        assert any("duplicate item id" in v for v in violations)
        assert any("one writable lane" in v for v in violations)
        assert any("unknown item ghost" in v for v in violations)


class TestWritersAndReaders:
    def test_one_writer_many_readers_falls_out_of_the_package_shape(self):
        package = _valid_package()
        writers = package.writer_items()
        assert [item.item_id for item in writers] == ["api", "consumer"]
        # readers: the non-writable item's repo plus the context repos.
        assert package.reader_repos() == {"contracts", "runbooks"}

    def test_lane_assignment_gives_each_writer_its_one_target(self):
        assignment = lane_assignment(_valid_package())
        assert assignment["api"] == {
            "repository_id": "widgets",
            "mode": "writable",
            "siblings_read": ["consumers", "contracts", "runbooks"],
        }
        assert assignment["consumer"]["repository_id"] == "consumers"
        assert assignment["consumer"]["mode"] == "writable"
        assert assignment["consumer"]["siblings_read"] == ["contracts", "runbooks", "widgets"]

    def test_a_non_writable_item_is_assigned_read_only(self):
        assignment = lane_assignment(_valid_package())
        assert assignment["contract-check"]["mode"] == "read_only"
        assert assignment["contract-check"]["repository_id"] == "contracts"

    def test_lane_assignment_refuses_an_invalid_package(self):
        broken = WorkPackage(
            package_id="pkg-broken",
            objective="no items",
        )
        with pytest.raises(ValueError, match="invalid package"):
            lane_assignment(broken)


class TestCompileDependencies:
    def test_a_chain_lands_in_one_phase_per_level(self):
        items = [
            WorkItemRef(item_id="C", repository_id="rc", depends_on=("B",)),
            WorkItemRef(item_id="A", repository_id="ra"),
            WorkItemRef(item_id="B", repository_id="rb", depends_on=("A",)),
        ]
        assert compile_dependencies(items) == [["A"], ["B"], ["C"]]

    def test_independent_items_share_a_phase(self):
        items = [
            WorkItemRef(item_id="A", repository_id="ra"),
            WorkItemRef(item_id="B", repository_id="rb"),
            WorkItemRef(item_id="C", repository_id="rc", depends_on=("A", "B")),
        ]
        assert compile_dependencies(items) == [["A", "B"], ["C"]]

    def test_a_diamond_compiles_to_three_phases(self):
        items = [
            WorkItemRef(item_id="top", repository_id="r1"),
            WorkItemRef(item_id="left", repository_id="r2", depends_on=("top",)),
            WorkItemRef(item_id="right", repository_id="r3", depends_on=("top",)),
            WorkItemRef(item_id="join", repository_id="r4", depends_on=("left", "right")),
        ]
        assert compile_dependencies(items) == [["top"], ["left", "right"], ["join"]]

    def test_a_cycle_demands_phasing_the_change_not_the_architecture(self):
        items = [
            WorkItemRef(item_id="A", repository_id="ra", depends_on=("B",)),
            WorkItemRef(item_id="B", repository_id="rb", depends_on=("A",)),
        ]
        with pytest.raises(ValueError) as excinfo:
            compile_dependencies(items)
        assert "cyclic dependencies require phasing the CHANGE, not the architecture" in str(
            excinfo.value
        )

    def test_a_self_dependency_is_a_cycle(self):
        items = [WorkItemRef(item_id="A", repository_id="ra", depends_on=("A",))]
        with pytest.raises(ValueError, match="phasing the CHANGE"):
            compile_dependencies(items)

    def test_a_dangling_dependency_is_malformed_not_a_cycle(self):
        items = [WorkItemRef(item_id="A", repository_id="ra", depends_on=("ghost",))]
        with pytest.raises(ValueError, match="outside the package"):
            compile_dependencies(items)


class TestBoundPhases:
    def test_a_wide_phase_splits_into_consecutive_subphases(self):
        phases = [["a", "b", "c", "d", "e"], ["f"]]
        assert bound_phases(phases, max_parallel=2) == [
            ["a", "b"],
            ["c", "d"],
            ["e"],
            ["f"],
        ]

    def test_narrow_phases_pass_through_unchanged(self):
        phases = [["a"], ["b", "c"]]
        assert bound_phases(phases, max_parallel=2) == phases

    def test_max_parallel_one_serializes_everything_in_order(self):
        phases = [["a", "b"], ["c", "d", "e"]]
        assert bound_phases(phases, max_parallel=1) == [["a"], ["b"], ["c"], ["d"], ["e"]]

    def test_dependency_order_survives_across_subphases(self):
        # a dependency's phase still strictly precedes its dependent's.
        bounded = bound_phases([["dep1", "dep2", "dep3"], ["dependent"]], max_parallel=2)
        flat = [item for phase in bounded for item in phase]
        assert flat.index("dependent") > max(
            flat.index("dep1"), flat.index("dep2"), flat.index("dep3")
        )

    def test_max_parallel_below_one_is_refused(self):
        with pytest.raises(ValueError, match="at least 1"):
            bound_phases([["a"]], max_parallel=0)


class TestSagaState:
    def test_partial_then_published(self):
        saga = SagaState(package_id="pkg-1", steps=("api", "consumer"))
        assert saga.state == "pending"
        mid = saga.mark_published("api")
        assert mid.state == "partially_published"
        assert mid.published == ("api",)
        done = mid.mark_published("consumer")
        assert done.state == "published"
        assert done.published == ("api", "consumer")

    def test_a_single_step_saga_publishes_in_one_mark(self):
        saga = SagaState(package_id="pkg-1", steps=("only",)).mark_published("only")
        assert saga.state == "published"

    def test_failure_is_recorded_not_rolled_back(self):
        saga = SagaState(package_id="pkg-1", steps=("api", "consumer"))
        failed = saga.mark_published("api").mark_failed("consumer")
        assert failed.state == "failed"
        # the saga RECORDS: the already-published step stays published —
        # no compensating delete, no fabricated rollback.
        assert failed.published == ("api",)

    def test_recovery_targets_are_the_unpublished_steps_in_order(self):
        failed = SagaState(package_id="pkg-1", steps=("a", "b", "c"), published=("a",)).mark_failed(
            "b"
        )
        assert recovery_targets(failed) == ["b", "c"]

    def test_recovery_targets_of_a_fresh_saga_are_all_steps(self):
        saga = SagaState(package_id="pkg-1", steps=("a", "b"))
        assert recovery_targets(saga) == ["a", "b"]

    def test_an_unknown_item_cannot_be_marked(self):
        saga = SagaState(package_id="pkg-1", steps=("a",))
        with pytest.raises(ValueError, match="not a step"):
            saga.mark_published("ghost")
        with pytest.raises(ValueError, match="not a step"):
            saga.mark_failed("ghost")

    def test_double_publishing_is_refused(self):
        saga = SagaState(package_id="pkg-1", steps=("a", "b")).mark_published("a")
        with pytest.raises(ValueError, match="already recorded"):
            saga.mark_published("a")


class TestSagaOutcomes:
    def test_all_true_publishes(self):
        assert saga_outcomes({"a": True, "b": True}) == "published"

    def test_any_false_fails(self):
        assert saga_outcomes({"a": True, "b": False}) == "failed"

    def test_no_steps_is_pending(self):
        assert saga_outcomes({}) == "pending"


def _per_repo() -> dict[str, dict]:
    return {
        "widgets": {
            "base_oid": _oid("a"),
            "candidate_oid": _oid("b"),
            "role": "changed",
            "image_digest": _image_digest("1"),
        },
        "consumers": {
            "base_oid": _oid("c"),
            "candidate_oid": _oid("d"),
            "role": "changed",
            "image_digest": _image_digest("2"),
        },
        "runbooks": {
            "base_oid": _oid("e"),
            "candidate_oid": _oid("e"),  # unchanged: baseline rides along
            "role": "baseline",
            "image_digest": _image_digest("3"),
        },
    }


class TestFreezeCandidateSet:
    def test_round_trips_a_valid_set_with_sorted_members(self):
        frozen = freeze_candidate_set(
            work_id="wp-demo-1",
            plan_revision=2,
            contract_digest=_digest("f"),
            per_repo=_per_repo(),
        )
        assert isinstance(frozen, CandidateSet)
        assert frozen.work_id == "wp-demo-1"
        assert frozen.plan_revision == 2
        assert [m.repository_id for m in frozen.members] == ["consumers", "runbooks", "widgets"]
        roles = {m.repository_id: m.role for m in frozen.members}
        assert roles == {
            "widgets": "changed",
            "consumers": "changed",
            "runbooks": "baseline",
        }

    def test_the_model_enforces_the_contract_digest_shape(self):
        with pytest.raises(ValidationError, match="sha256"):
            freeze_candidate_set("wp-demo-1", 1, "not-hex", _per_repo())

    def test_the_closed_role_vocabulary_is_enforced_through_the_model(self):
        per_repo = _per_repo()
        per_repo["widgets"]["role"] = "maybe"
        with pytest.raises(ValidationError):
            freeze_candidate_set("wp-demo-1", 1, _digest("f"), per_repo)

    def test_unique_repositories_are_guaranteed_twice_over(self):
        # freeze_candidate_set keys per_repo BY repository, so a duplicate
        # is not even expressible here — and the model beneath still
        # refuses one if a caller bypasses the dict.
        widgets = {
            "base_oid": _oid("a"),
            "candidate_oid": _oid("b"),
            "role": "changed",
            "image_digest": _image_digest("1"),
        }
        with pytest.raises(ValidationError, match="twice"):
            CandidateSet(
                work_id="wp-demo-1",
                plan_revision=1,
                work_contract_digest=_digest("f"),
                members=[
                    CandidateSetMember(repository_id="widgets", **widgets),
                    CandidateSetMember(repository_id="widgets", **widgets),
                ],
            )


class TestIdentityChanged:
    def test_the_same_set_is_the_same_identity(self):
        a = freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo())
        b = freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo())
        assert identity_changed(a, b) is False

    def test_one_moved_candidate_invalidates_the_binding(self):
        a = freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo())
        moved = _per_repo()
        moved["widgets"]["candidate_oid"] = _oid("9")
        b = freeze_candidate_set("wp-demo-1", 1, _digest("f"), moved)
        assert identity_changed(a, b) is True

    def test_a_member_added_or_removed_changes_identity(self):
        a = freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo())
        with_extra = _per_repo()
        with_extra["extra"] = {
            "base_oid": _oid("1"),
            "candidate_oid": _oid("2"),
            "role": "changed",
            "image_digest": _image_digest("4"),
        }
        b = freeze_candidate_set("wp-demo-1", 1, _digest("f"), with_extra)
        assert identity_changed(a, b) is True
        assert identity_changed(b, a) is True

    def test_a_moved_base_keeps_the_verified_identity(self):
        # the CANDIDATES are what was verified — rebased candidates move
        # identity, a re-declared base alone does not.
        a = freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo())
        rebased = _per_repo()
        rebased["widgets"]["base_oid"] = _oid("7")
        b = freeze_candidate_set("wp-demo-1", 1, _digest("f"), rebased)
        assert identity_changed(a, b) is False

    def test_the_same_candidates_with_a_rebuilt_image_is_a_new_identity(self):
        # NXT-25: same source commits, different rebuilt artifact — a
        # DIFFERENT tested world. The old green result confirms nothing.
        a = freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo())
        rebuilt = _per_repo()
        rebuilt["widgets"]["image_digest"] = _image_digest("9")
        b = freeze_candidate_set("wp-demo-1", 1, _digest("f"), rebuilt)
        assert identity_changed(a, b) is True

    def test_the_same_candidates_with_a_new_test_bundle_is_a_new_identity(self):
        a = freeze_candidate_set(
            "wp-demo-1", 1, _digest("f"), _per_repo(), test_bundle_digest=_digest("t")
        )
        b = freeze_candidate_set(
            "wp-demo-1", 1, _digest("f"), _per_repo(), test_bundle_digest=_digest("u")
        )
        assert identity_changed(a, b) is True

    def test_environment_pins_enter_the_identity_comparison(self):
        a = freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo())
        b = freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo())
        postgres_old = {"postgres": _image_digest("p")}
        postgres_new = {"postgres": _image_digest("q")}
        # same pins on both sides → still the same identity
        assert identity_changed(a, b, environment=postgres_old) is False
        # the SAME sets compared under different dependency baselines are
        # different worlds — identity is world + environment context.
        assert world_digest(a, environment=postgres_old) != world_digest(
            b, environment=postgres_new
        )


class TestTestedWorldDigest:
    def test_the_same_world_gives_the_same_digest(self):
        a = freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo())
        b = freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo())
        assert world_digest(a) == world_digest(b)

    def test_a_revision_bump_alone_keeps_the_tested_world_stable(self):
        # the review's counter-warning: historical provenance (who asked,
        # under which plan revision) is NOT part of the tested world. A
        # revision-number bump must not invalidate a world that did not
        # change — that is what applicability evidence is for.
        a = freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo())
        b = freeze_candidate_set("wp-demo-1", 7, _digest("f"), _per_repo())
        assert world_digest(a) == world_digest(b)

    def test_bundle_and_contract_digests_are_all_part_of_the_world(self):
        base = freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo())
        variants = {
            "work_contract": freeze_candidate_set("wp-demo-1", 1, _digest("6"), _per_repo()),
            "contract_bundle": freeze_candidate_set(
                "wp-demo-1", 1, _digest("f"), _per_repo(), contract_bundle_digest=_digest("c")
            ),
            "test_bundle": freeze_candidate_set(
                "wp-demo-1", 1, _digest("f"), _per_repo(), test_bundle_digest=_digest("t")
            ),
            "environment_profile": freeze_candidate_set(
                "wp-demo-1", 1, _digest("f"), _per_repo(), environment_profile_digest=_digest("e")
            ),
        }
        for label, variant in variants.items():
            assert world_digest(base) != world_digest(variant), label

    def test_a_drifted_baseline_is_a_different_tested_world(self):
        # the baseline rides along UNCHANGED — so when it moves (a new
        # candidate oid or a rebuilt image), the system under test is no
        # longer the one the candidates were cut against.
        a = freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo())
        moved = _per_repo()
        moved["runbooks"]["candidate_oid"] = _oid("8")
        b = freeze_candidate_set("wp-demo-1", 1, _digest("f"), moved)
        assert world_digest(a) != world_digest(b)
        rebuilt = _per_repo()
        rebuilt["runbooks"]["image_digest"] = _image_digest("8")
        c = freeze_candidate_set("wp-demo-1", 1, _digest("f"), rebuilt)
        assert world_digest(a) != world_digest(c)

    def test_environment_pins_and_policy_refs_are_part_of_the_world(self):
        a = freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo())
        bare = world_digest(a)
        assert bare != world_digest(a, environment={"postgres": _image_digest("p")})
        assert bare != world_digest(a, policy_refs=["compat/matrix@2"])
        # both together differ from either alone
        assert world_digest(a, environment={"postgres": _image_digest("p")}) != (
            world_digest(
                a, environment={"postgres": _image_digest("p")}, policy_refs=["compat/matrix@2"]
            )
        )

    def test_policy_ref_order_and_duplicates_do_not_matter(self):
        a = freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo())
        one = world_digest(a, policy_refs=["compat/a@1", "compat/b@1"])
        two = world_digest(a, policy_refs=["compat/b@1", "compat/a@1", "compat/a@1"])
        assert one == two

    def test_deterministic_across_dict_and_member_order(self):
        # the same world spelled with different insertion orders — per_repo
        # dicts and even a directly-constructed reversed member list —
        # must serialize to ONE digest.
        forward = _per_repo()
        reverse = {repo: forward[repo] for repo in reversed(list(forward))}
        a = freeze_candidate_set("wp-demo-1", 1, _digest("f"), forward)
        b = freeze_candidate_set("wp-demo-1", 1, _digest("f"), reverse)
        shuffled = CandidateSet(
            work_id="wp-demo-1",
            plan_revision=1,
            work_contract_digest=_digest("f"),
            members=list(reversed(a.members)),
        )
        assert world_digest(a) == world_digest(b)
        assert world_digest(a) == world_digest(shuffled)

    def test_a_moved_base_does_not_move_the_tested_world(self):
        # base_oid is the diff the candidate was cut FROM — provenance of
        # the change, not content of the tested world.
        a = freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo())
        rebased = _per_repo()
        rebased["widgets"]["base_oid"] = _oid("7")
        b = freeze_candidate_set("wp-demo-1", 1, _digest("f"), rebased)
        assert world_digest(a) == world_digest(b)


class TestApplicabilityDigest:
    """Provenance vs applicability: the reuse fingerprint is a DISTINCT concept."""

    def _frozen(self, **kwargs) -> CandidateSet:
        return freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo(), **kwargs)

    def test_the_two_digests_never_coincide_for_the_same_inputs(self):
        # different questions, domain-separated tags: a tested-world
        # digest must never be mistakable for an applicability digest.
        a = self._frozen()
        assert applicability_digest(a) != world_digest(a)
        # and each is a stable 64-hex sha256
        assert len(world_digest(a)) == 64
        assert len(applicability_digest(a)) == 64
        # each still moves when its own inputs move
        assert applicability_digest(a) != applicability_digest(a, policy_refs=["compat/1"])

    def test_a_revision_bump_keeps_applicability_stable(self):
        a = freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo())
        b = freeze_candidate_set("wp-demo-1", 7, _digest("f"), _per_repo())
        assert applicability_digest(a) == applicability_digest(b)

    def test_a_contract_change_moves_the_tested_world_not_applicability(self):
        # the work contract defined THIS run's obligations; it is not a
        # dependency the evidence claims to cover — evidence stays
        # applicable when only the asking contract changes.
        a = self._frozen()
        b = freeze_candidate_set("wp-demo-1", 1, _digest("6"), _per_repo())
        assert world_digest(a) != world_digest(b)
        assert applicability_digest(a) == applicability_digest(b)

    def test_a_contract_bundle_change_moves_the_tested_world_not_applicability(self):
        a = self._frozen()
        b = self._frozen(contract_bundle_digest=_digest("c"))
        assert world_digest(a) != world_digest(b)
        assert applicability_digest(a) == applicability_digest(b)

    def test_dependency_changes_move_applicability_too(self):
        # no indefinite reuse by SHA: a rebuilt image or a drifted
        # baseline changes what the evidence vouches for.
        a = self._frozen()
        rebuilt = _per_repo()
        rebuilt["widgets"]["image_digest"] = _image_digest("9")
        b = freeze_candidate_set("wp-demo-1", 1, _digest("f"), rebuilt)
        assert applicability_digest(a) != applicability_digest(b)
        drifted = _per_repo()
        drifted["runbooks"]["candidate_oid"] = _oid("8")
        c = freeze_candidate_set("wp-demo-1", 1, _digest("f"), drifted)
        assert applicability_digest(a) != applicability_digest(c)

    def test_test_and_environment_changes_move_applicability(self):
        a = self._frozen()
        assert applicability_digest(a) != applicability_digest(
            self._frozen(test_bundle_digest=_digest("t"))
        )
        assert applicability_digest(a) != applicability_digest(
            self._frozen(environment_profile_digest=_digest("e"))
        )
        assert applicability_digest(a) != applicability_digest(
            a, environment={"postgres": _image_digest("p")}
        )

    def test_the_same_world_under_a_different_work_shares_applicability(self):
        # reuse crosses works: which work ASKED is provenance, not world.
        a = freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo())
        b = freeze_candidate_set("wp-other-9", 1, _digest("f"), _per_repo())
        assert applicability_digest(a) == applicability_digest(b)
        assert world_digest(a) == world_digest(b)


class TestBaselineMembers:
    def test_only_unchanged_repositories_ride_along(self):
        frozen = freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo())
        assert baseline_members(frozen) == ["runbooks"]

    def test_a_set_without_baselines_has_none(self):
        per_repo = {
            "widgets": {
                "base_oid": _oid("a"),
                "candidate_oid": _oid("b"),
                "role": "changed",
                "image_digest": _image_digest("1"),
            }
        }
        frozen = freeze_candidate_set("wp-demo-1", 1, _digest("f"), per_repo)
        assert baseline_members(frozen) == []


class TestFreezeCandidateSetContractHardening:
    """NXT-22 enforced through the workpackage's own freeze entry point."""

    def test_a_garbage_candidate_oid_no_longer_flows_through(self):
        # the models gap the freeze docstring recorded: member oids had
        # NO format validators, so garbage spelled like an oid passed.
        # Now the model refuses it at construction.
        per_repo = _per_repo()
        per_repo["widgets"]["candidate_oid"] = "definitely-not-a-sha"
        with pytest.raises(ValidationError, match="oid must be"):
            freeze_candidate_set("wp-demo-1", 1, _digest("f"), per_repo)

    def test_a_mutable_tag_image_is_refused_at_freeze(self):
        per_repo = _per_repo()
        per_repo["widgets"]["image_digest"] = "widgets:latest"
        with pytest.raises(ValidationError, match="mutable tags and bare names"):
            freeze_candidate_set("wp-demo-1", 1, _digest("f"), per_repo)

    def test_the_frozen_members_are_a_tuple(self):
        frozen = freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo())
        assert isinstance(frozen.members, tuple)
        with pytest.raises(AttributeError):
            frozen.members.append(frozen.members[0])  # type: ignore[attr-defined]

    def test_the_persisted_world_fields_default_clean(self):
        # backward compatibility: existing constructors produce a set
        # with NO persisted world binding — freeze_verified_world (in
        # verification_sets) is what records one, deliberately.
        frozen = freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo())
        assert frozen.environment_pins == ()
        assert frozen.policy_refs == ()
        assert frozen.tested_world_digest is None
        assert frozen.applicability_digest is None


class TestPersistedWorldVsCallTimeDigests:
    """The NXT-22 boundary: persistence lives on the model, purity in the functions."""

    def test_persisted_fields_are_not_inputs_to_the_call_time_digests(self):
        bare = freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo())
        bound = freeze_verified_world(
            bare, environment_pins={"postgres": _image_digest("a")}, policy_refs=["compat/1"]
        )
        # the workpackage digest functions stay call-time pure: the
        # persisted fields change nothing until the same pins/refs are
        # passed at call time
        assert world_digest(bare) == world_digest(bound)
        assert identity_changed(bare, bound) is False

    def test_the_persisted_digest_reproduces_only_under_its_own_pins(self):
        pins = {"postgres": _image_digest("a")}
        bare = freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo())
        bound = freeze_verified_world(bare, environment_pins=pins, policy_refs=["compat/1"])
        assert world_digest(bound) != bound.tested_world_digest  # naive call "forgets"
        assert (
            world_digest(bound, environment=pins, policy_refs=["compat/1"])
            == bound.tested_world_digest
        )
        assert (
            applicability_digest(bound, environment=pins, policy_refs=["compat/1"])
            == bound.applicability_digest
        )

    def test_a_frozen_set_still_round_trips_through_the_model(self):
        bare = freeze_candidate_set("wp-demo-1", 1, _digest("f"), _per_repo())
        bound = freeze_verified_world(bare, environment_pins={"postgres": _image_digest("a")})
        again = CandidateSet.model_validate(bound.model_dump())
        assert again == bound
        assert again.tested_world_digest == bound.tested_world_digest


# ---------------------------------------------------------------------------
# NXT-23: the durable coordination layer
# ---------------------------------------------------------------------------

PARENT_RUN = "run-parent-1"


class RecordingFactory:
    """The child-run factory seam as NXT-23 contracts it.

    Idempotent on *intent_key* (a re-issued intent returns the EXISTING
    child), records every call, and can simulate the crash windows: die
    after the child exists but before the coordinator could persist the
    link (``crash_after_create``), or before creating it at all
    (``crash_before_create``).
    """

    def __init__(
        self,
        session_factory=None,
        *,
        crash_after_create: bool = False,
        crash_before_create: bool = False,
    ) -> None:
        self.session_factory = session_factory
        self.crash_after_create = crash_after_create
        self.crash_before_create = crash_before_create
        self.children: dict[str, str] = {}
        self.calls: list[dict] = []
        self.creations = 0
        self.intent_status_at_call: list[str] = []

    async def __call__(
        self, item_id: str, repository_id: str, *, writable: bool, intent_key: str, brief: str
    ) -> str:
        observed = ""
        if self.session_factory is not None:
            state = await read_workpackage_state(self.session_factory, PARENT_RUN)
            observed = str((state["children"].get(item_id) or {}).get("intent_status"))
        self.calls.append(
            {
                "item_id": item_id,
                "repository_id": repository_id,
                "writable": writable,
                "intent_key": intent_key,
                "brief": brief,
            }
        )
        self.intent_status_at_call.append(observed)
        if intent_key in self.children:
            return self.children[intent_key]
        if self.crash_before_create:
            raise RuntimeError("process died before the factory created the child")
        self.creations += 1
        self.children[intent_key] = f"child-{item_id}"
        if self.crash_after_create:
            raise RuntimeError("process died after the child existed, link unsaved")
        return self.children[intent_key]

    def launches_of(self, item_id: str) -> list[dict]:
        return [call for call in self.calls if call["item_id"] == item_id]


async def _wp_process(db_path: Path) -> tuple[async_sessionmaker[AsyncSession], AsyncEngine]:
    """A fresh engine + session factory over the SAME file = a restart."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False), engine


async def _seed_parent(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        session.add(FlowRun(id=PARENT_RUN, project_id=1, status="planning"))
        await session.commit()


async def _wp_state(factory: async_sessionmaker[AsyncSession]) -> dict:
    return await read_workpackage_state(factory, PARENT_RUN) or {}


async def _outbox_types(factory: async_sessionmaker[AsyncSession]) -> list[str]:
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(Outbox).where(Outbox.flow_run_id == PARENT_RUN).order_by(Outbox.id)
                )
            )
            .scalars()
            .all()
        )
        return [row.event_type for row in rows]


class TestDurableLaunch:
    async def test_start_persists_the_intent_before_the_factory_call(self, tmp_path):
        factory, engine = await _wp_process(tmp_path / "wp.db")
        await _seed_parent(factory)
        seam = RecordingFactory(session_factory=factory)
        coordinator = WorkPackageCoordinator(factory, seam)
        try:
            await coordinator.start(
                _valid_package(), parent_run_id=PARENT_RUN, task_brief="Widen the API"
            )
            # every factory call SAW a durable intended intent — the
            # crash window is closed from the front
            assert seam.intent_status_at_call
            assert set(seam.intent_status_at_call) == {"intended"}
            state = await _wp_state(factory)
            assert state["schema"] == "forge.workpackage.state/1"
            assert state["phases"] == [["api", "contract-check"], ["consumer"]]
            assert state["children"]["api"]["intent_status"] == "launched"
            assert state["children"]["api"]["child_run_id"] == "child-api"
            assert state["children"]["api"]["writable"] is True
            events = await _outbox_types(factory)
            assert events[0] == "workpackage.started"
            assert "workpackage.child_intent" in events
            assert "workpackage.child_launched" in events
            assert "workpackage.reference_snapshot" in events
        finally:
            await engine.dispose()

    async def test_crash_after_child_creation_replays_to_one_child(self, tmp_path):
        factory, engine = await _wp_process(tmp_path / "wp.db")
        await _seed_parent(factory)
        seam = RecordingFactory(crash_after_create=True)
        coordinator = WorkPackageCoordinator(factory, seam)
        try:
            with pytest.raises(RuntimeError, match="link unsaved"):
                await coordinator.start(_valid_package(), parent_run_id=PARENT_RUN, task_brief="b")
            state = await _wp_state(factory)
            assert state["children"]["api"]["intent_status"] == "intended"  # durable crash mark

            # replay: the SAME intent key returns the child that exists
            replay = WorkPackageCoordinator(factory, RecordingFactory())
            await replay.start(_valid_package(), parent_run_id=PARENT_RUN, task_brief="b")
            state = await _wp_state(factory)
            assert state["children"]["api"]["intent_status"] == "launched"
            assert state["children"]["api"]["child_run_id"] == "child-api"
            # the crash happened after the creation; the replay re-issued
            # the intent, so exactly ONE child ever existed
            assert seam.creations == 1
        finally:
            await engine.dispose()

    async def test_crash_before_the_factory_call_also_converges_to_one_child(self, tmp_path):
        factory, engine = await _wp_process(tmp_path / "wp.db")
        await _seed_parent(factory)
        seam = RecordingFactory(crash_before_create=True)
        coordinator = WorkPackageCoordinator(factory, seam)
        try:
            with pytest.raises(RuntimeError, match="before the factory created"):
                await coordinator.start(_valid_package(), parent_run_id=PARENT_RUN, task_brief="b")
            state = await _wp_state(factory)
            assert state["children"]["api"]["intent_status"] == "intended"

            replay = WorkPackageCoordinator(factory, RecordingFactory())
            await replay.start(_valid_package(), parent_run_id=PARENT_RUN, task_brief="b")
            state = await _wp_state(factory)
            assert state["children"]["api"]["child_run_id"] == "child-api"
            assert seam.creations == 0  # the first process never got to create
        finally:
            await engine.dispose()

    async def test_reconcile_twice_never_duplicates_children(self, tmp_path):
        factory, engine = await _wp_process(tmp_path / "wp.db")
        await _seed_parent(factory)
        seam = RecordingFactory()
        coordinator = WorkPackageCoordinator(factory, seam)
        try:
            await coordinator.start(_valid_package(), parent_run_id=PARENT_RUN, task_brief="b")
            first_calls = len(seam.calls)
            await coordinator.start(_valid_package(), parent_run_id=PARENT_RUN, task_brief="b")
            assert len(seam.calls) == first_calls  # nothing re-issued
            assert seam.creations == 1  # exactly one writer child for 'api'
        finally:
            await engine.dispose()

    async def test_a_second_process_adopts_the_launched_link(self, tmp_path):
        db = tmp_path / "wp.db"
        factory_a, engine_a = await _wp_process(db)
        await _seed_parent(factory_a)
        seam_a = RecordingFactory()
        coordinator_a = WorkPackageCoordinator(factory_a, seam_a)
        await coordinator_a.start(_valid_package(), parent_run_id=PARENT_RUN, task_brief="b")
        state_a = await _wp_state(factory_a)
        await engine_a.dispose()

        # a brand-new process over the same database: adoption, no calls
        factory_b, engine_b = await _wp_process(db)
        try:
            seam_b = RecordingFactory()
            coordinator_b = WorkPackageCoordinator(factory_b, seam_b)
            state_b = await coordinator_b.start(
                _valid_package(), parent_run_id=PARENT_RUN, task_brief="b"
            )
            assert seam_b.calls == []  # the durable link was adopted, not re-issued
            assert (
                state_b["children"]["api"]["child_run_id"]
                == state_a["children"]["api"]["child_run_id"]
            )
        finally:
            await engine_b.dispose()

    async def test_one_coordinated_package_per_parent_run(self, tmp_path):
        factory, engine = await _wp_process(tmp_path / "wp.db")
        await _seed_parent(factory)
        coordinator = WorkPackageCoordinator(factory, RecordingFactory())
        try:
            await coordinator.start(_valid_package(), parent_run_id=PARENT_RUN, task_brief="b")
            other = WorkPackage(
                package_id="pkg-other",
                objective="a different change",
                items=(WorkItemRef(item_id="solo", repository_id="widgets"),),
            )
            with pytest.raises(WorkPackageStateError, match="already coordinates"):
                await coordinator.start(other, parent_run_id=PARENT_RUN, task_brief="b")
        finally:
            await engine.dispose()

    async def test_an_invalid_package_refuses_to_start(self, tmp_path):
        factory, engine = await _wp_process(tmp_path / "wp.db")
        await _seed_parent(factory)
        coordinator = WorkPackageCoordinator(factory, RecordingFactory())
        try:
            with pytest.raises(ValueError, match="invalid"):
                await coordinator.start(
                    WorkPackage(package_id="pkg-broken", objective="nothing"),
                    parent_run_id=PARENT_RUN,
                    task_brief="b",
                )
        finally:
            await engine.dispose()

    async def test_a_missing_parent_run_is_a_loud_error(self, tmp_path):
        factory, engine = await _wp_process(tmp_path / "wp.db")
        try:
            with pytest.raises(WorkPackageStateError, match="not found"):
                await WorkPackageCoordinator(factory, RecordingFactory()).start(
                    _valid_package(), parent_run_id=PARENT_RUN, task_brief="b"
                )
        finally:
            await engine.dispose()


class TestProvenAdvance:
    async def _started(self, tmp_path, **start_kwargs):
        factory, engine = await _wp_process(tmp_path / "wp.db")
        await _seed_parent(factory)
        seam = RecordingFactory()
        coordinator = WorkPackageCoordinator(factory, seam)
        await coordinator.start(
            _valid_package(), parent_run_id=PARENT_RUN, task_brief="Widen the API", **start_kwargs
        )
        return factory, engine, seam, coordinator

    async def test_advance_without_proven_outcomes_refuses(self, tmp_path):
        factory, engine, seam, coordinator = await self._started(tmp_path)
        try:
            with pytest.raises(PhaseAdvanceRefused) as excinfo:
                await coordinator.advance(PARENT_RUN, _valid_package())
            assert excinfo.value.awaiting == ("api",)  # launched but silent is NOT proof
            state = await _wp_state(factory)
            assert state["current_phase"] == 0  # nothing moved
            assert [c["item_id"] for c in seam.calls] == ["api"]  # consumer never dispatched
            assert "workpackage.phase_advanced" not in await _outbox_types(factory)
        finally:
            await engine.dispose()

    async def test_advance_after_proven_outcomes_launches_the_next_phase_in_order(self, tmp_path):
        factory, engine, seam, coordinator = await self._started(tmp_path)
        try:
            applied = await coordinator.record_outcome(PARENT_RUN, "api", "succeeded")
            assert applied.status == "applied"
            state = await coordinator.advance(PARENT_RUN, _valid_package())
            assert state["current_phase"] == 1
            assert state["children"]["consumer"]["intent_status"] == "launched"
            # phase ordering pinned: api's call strictly precedes consumer's
            assert [c["item_id"] for c in seam.calls] == ["api", "consumer"]
            assert "workpackage.phase_advanced" in await _outbox_types(factory)

            # the last phase still needs its own proof before completing
            with pytest.raises(PhaseAdvanceRefused):
                await coordinator.advance(PARENT_RUN, _valid_package())
            await coordinator.record_outcome(PARENT_RUN, "consumer", "succeeded")
            done = await coordinator.advance(PARENT_RUN, _valid_package())
            assert done["state"] == "complete"
            assert "workpackage.complete" in await _outbox_types(factory)
            with pytest.raises(PhaseAdvanceRefused, match="already complete"):
                await coordinator.advance(PARENT_RUN, _valid_package())
        finally:
            await engine.dispose()

    async def test_a_failed_predecessor_blocks_dependent_dispatch(self, tmp_path):
        factory, engine, seam, coordinator = await self._started(tmp_path)
        try:
            applied = await coordinator.record_outcome(
                PARENT_RUN, "api", "failed", detail="tests are red"
            )
            assert isinstance(applied, OutcomeApplication) and applied.applied
            state = await _wp_state(factory)
            assert state["state"] == "failed"
            assert state["failed_item"] == "api"
            with pytest.raises(PhaseAdvanceRefused) as excinfo:
                await coordinator.advance(PARENT_RUN, _valid_package())
            assert excinfo.value.failed == ("api",)
            assert seam.launches_of("consumer") == []  # never dispatched
            assert "workpackage.child_failed" in await _outbox_types(factory)
        finally:
            await engine.dispose()

    async def test_outcomes_from_a_previous_tested_world_do_not_prove_anything(self, tmp_path):
        active = _digest("a")
        factory, engine, seam, coordinator = await self._started(
            tmp_path, tested_world_digest=active
        )
        try:
            stale = await coordinator.record_outcome(
                PARENT_RUN, "api", "succeeded", tested_world_digest=_digest("z")
            )
            assert stale.status == "rejected"  # the previous revision's completion
            with pytest.raises(PhaseAdvanceRefused):
                await coordinator.advance(PARENT_RUN, _valid_package())

            current = await coordinator.record_outcome(
                PARENT_RUN, "api", "succeeded", tested_world_digest=active
            )
            assert current.status == "applied"
            state = await coordinator.advance(PARENT_RUN, _valid_package())
            assert state["children"]["consumer"]["intent_status"] == "launched"
        finally:
            await engine.dispose()

    async def test_duplicate_outcomes_are_idempotent_and_conflicts_refused(self, tmp_path):
        factory, engine, seam, coordinator = await self._started(tmp_path)
        try:
            await coordinator.record_outcome(PARENT_RUN, "api", "succeeded", detail="green")
            again = await coordinator.record_outcome(PARENT_RUN, "api", "succeeded", detail="green")
            assert again.status == "duplicate"
            conflict = await coordinator.record_outcome(PARENT_RUN, "api", "failed", detail="red")
            assert conflict.status == "rejected"  # the first recorded outcome stands
        finally:
            await engine.dispose()

    async def test_outcome_status_vocabulary_and_unknown_items(self, tmp_path):
        factory, engine, seam, coordinator = await self._started(tmp_path)
        try:
            assert (
                await coordinator.record_outcome(PARENT_RUN, "api", "weird")
            ).status == "rejected"
            ghost = await coordinator.record_outcome(PARENT_RUN, "ghost", "succeeded")
            assert ghost.status == "rejected" and "unknown item" in ghost.reason
        finally:
            await engine.dispose()

    async def test_advance_without_a_state_refuses_loudly(self, tmp_path):
        factory, engine = await _wp_process(tmp_path / "wp.db")
        await _seed_parent(factory)
        try:
            with pytest.raises(WorkPackageStateError, match="coordinates no work package"):
                await WorkPackageCoordinator(factory, RecordingFactory()).advance(
                    PARENT_RUN, _valid_package()
                )
        finally:
            await engine.dispose()


class TestReferenceSnapshots:
    async def test_read_only_items_never_reach_the_writer_factory(self, tmp_path):
        factory, engine = await _wp_process(tmp_path / "wp.db")
        await _seed_parent(factory)
        seam = RecordingFactory()
        coordinator = WorkPackageCoordinator(factory, seam)
        try:
            await coordinator.start(
                _valid_package(), parent_run_id=PARENT_RUN, task_brief="Widen the API"
            )
            state = await _wp_state(factory)
            check = state["children"]["contract-check"]
            assert check["kind"] == "reference_snapshot"
            assert check["writable"] is False  # the mode is recorded, not dropped
            assert check["child_run_id"] == ""  # no writer was ever created
            assert seam.launches_of("contract-check") == []
            assert all(call["writable"] is True for call in seam.calls)

            # a reference snapshot executes nothing — nothing to report
            report = await coordinator.record_outcome(PARENT_RUN, "contract-check", "succeeded")
            assert report.status == "rejected"
            assert "reference snapshot" in report.reason
        finally:
            await engine.dispose()

    async def test_a_reference_snapshot_needs_no_outcome_to_advance(self, tmp_path):
        # phase 0 = [api (writer), contract-check (snapshot)]: proving the
        # WRITER alone advances; the snapshot is proven by construction.
        factory, engine = await _wp_process(tmp_path / "wp.db")
        await _seed_parent(factory)
        seam = RecordingFactory()
        coordinator = WorkPackageCoordinator(factory, seam)
        try:
            await coordinator.start(_valid_package(), parent_run_id=PARENT_RUN, task_brief="b")
            await coordinator.record_outcome(PARENT_RUN, "api", "succeeded")
            state = await coordinator.advance(PARENT_RUN, _valid_package())
            assert state["current_phase"] == 1
        finally:
            await engine.dispose()
