"""The MRP core: packages, phases, lanes, the publication saga, frozen candidates.

These tests pin the review's separations — the service graph is not the
execution DAG, one writable repository per child lane, publication as an
explicit saga with no pretend rollback, and verification bound to a
frozen candidate-set identity.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from forge.adaptive.models import CandidateSet, CandidateSetMember
from forge.adaptive.workpackage import (
    SagaState,
    WorkItemRef,
    WorkPackage,
    baseline_members,
    bound_phases,
    compile_dependencies,
    freeze_candidate_set,
    identity_changed,
    lane_assignment,
    recovery_targets,
    saga_outcomes,
)


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
