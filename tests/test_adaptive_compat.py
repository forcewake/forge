"""FND-07/FND-08: the legacy/adaptive compatibility boundary.

Adaptive is an opt-in, never a default. These tests pin the boundary:
flags that allow adaptive runs only where explicitly scoped, the safe
drain that waits for mid-flight legacy runs, the documented downgrade
constraints, honestly-labelled release verification, a migration that
invents no capability a v3 run never had, the registered invariant
suites, and performed-vs-skipped buckets that never flatter a skip
into a pass.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from forge.adaptive.compat import (
    INVARIANT_SUITES,
    CompatibilityFlags,
    downgrade_constraints,
    drain_in_flight,
    migration_invents_nothing,
    performed_vs_skipped,
    validate_release,
)

#: The repository root, so the suite-registry check reads the real
#: files rather than depending on pytest's working directory.
_REPO_ROOT = Path(__file__).resolve().parents[1]


class TestCompatibilityFlags:
    def test_defaults_allow_nothing(self):
        # adaptive is never the default: an unconfigured flag set is
        # fully legacy (FND-07)
        flags = CompatibilityFlags()
        assert flags.adaptive_enabled is False
        assert flags.allows("github", "acme/api") is False

    def test_the_master_switch_alone_still_allows_nothing(self):
        flags = CompatibilityFlags(adaptive_enabled=True)
        assert flags.allows("github", "acme/api") is False

    def test_a_matching_connection_scope_allows(self):
        flags = CompatibilityFlags(adaptive_enabled=True, connection_scopes=("github",))
        assert flags.allows("github", "unflagged/project") is True

    def test_a_matching_project_scope_allows(self):
        flags = CompatibilityFlags(adaptive_enabled=True, project_scopes=("acme/api",))
        assert flags.allows("gitlab", "acme/api") is True

    def test_scope_matches_never_override_a_disabled_master_switch(self):
        flags = CompatibilityFlags(connection_scopes=("github",), project_scopes=("acme/api",))
        assert flags.allows("github", "acme/api") is False

    def test_unflagged_connection_and_project_stay_legacy(self):
        flags = CompatibilityFlags(
            adaptive_enabled=True,
            connection_scopes=("github",),
            project_scopes=("acme/api",),
        )
        assert flags.allows("gitlab", "other/project") is False

    def test_the_flag_set_is_frozen(self):
        flags = CompatibilityFlags()
        with pytest.raises(dataclasses.FrozenInstanceError):
            flags.adaptive_enabled = True


class TestDrainInFlight:
    def test_no_legacy_runs_means_safe_to_enable(self):
        assert drain_in_flight([]) == {"safe_to_enable": True, "must_wait": []}

    @pytest.mark.parametrize(
        "status",
        ["ready_for_human", "blocked", "failed", "cancelled"],
    )
    def test_terminal_statuses_do_not_block_the_cutover(self, status):
        result = drain_in_flight([{"id": "run-1", "status": status}])
        assert result == {"safe_to_enable": True, "must_wait": []}

    @pytest.mark.parametrize("status", ["running", "planning", "queued", "publishing"])
    def test_mid_flight_runs_are_waited_on_by_id(self, status):
        result = drain_in_flight(
            [
                {"id": "run-1", "status": status},
                {"id": "run-2", "status": "failed"},
            ]
        )
        assert result == {"safe_to_enable": False, "must_wait": ["run-1"]}

    def test_every_must_wait_id_is_reported(self):
        result = drain_in_flight(
            [
                {"id": "run-1", "status": "running"},
                {"id": "run-2", "status": "verifying"},
            ]
        )
        assert result["safe_to_enable"] is False
        assert result["must_wait"] == ["run-1", "run-2"]

    def test_blocked_is_at_rest_not_mid_flight(self):
        # blocked runs park on a human decision; they hold no in-flight
        # work a mode switch could corrupt
        result = drain_in_flight([{"id": "run-1", "status": "blocked"}])
        assert result["safe_to_enable"] is True


class TestDowngradeConstraints:
    def test_the_documented_list_is_returned_verbatim(self):
        assert downgrade_constraints() == [
            "adaptive runs must be terminal",
            "evidence rows stay readable",
            "no schema rewrite of v3 specs",
        ]

    def test_a_fresh_list_is_returned_each_call(self):
        # callers may embed the contract without mutating the constant
        first = downgrade_constraints()
        first.append("operator sign-off")
        assert downgrade_constraints() == [
            "adaptive runs must be terminal",
            "evidence rows stay readable",
            "no schema rewrite of v3 specs",
        ]


class TestValidateRelease:
    def test_performed_and_passed_verifications_are_labelled_pass(self):
        report = validate_release(legacy_replay=True, new_startup=True)
        assert report == {"legacy_run_replay": "pass", "new_run_startup": "pass"}

    def test_checks_that_did_not_run_stay_not_run(self):
        report = validate_release(legacy_replay=False, new_startup=False)
        assert report == {"legacy_run_replay": "not_run", "new_run_startup": "not_run"}

    def test_one_ran_and_one_did_not(self):
        report = validate_release(legacy_replay=False, new_startup=True)
        assert report["legacy_run_replay"] == "not_run"
        assert report["new_run_startup"] == "pass"

    def test_the_labels_are_only_pass_or_not_run(self):
        # no third flattering label exists: a boot-time success can
        # never be dressed up as live verification
        for legacy_replay in (True, False):
            for new_startup in (True, False):
                report = validate_release(legacy_replay, new_startup)
                for label in report.values():
                    assert label in ("pass", "not_run")


class TestMigrationInventsNothing:
    def test_a_plain_v3_run_migrates_without_violations(self):
        assert migration_invents_nothing({"id": "run-1"}) == []

    def test_invented_read_scope_is_a_violation(self):
        violations = migration_invents_nothing({"id": "run-1", "read_scope": ["system"]})
        assert len(violations) == 1
        assert "read_scope" in violations[0]

    def test_invented_revision_permission_is_a_violation(self):
        violations = migration_invents_nothing({"id": "run-1", "revision_permission": "auto"})
        assert len(violations) == 1
        assert "revision_permission" in violations[0]

    def test_both_inventions_are_reported_together(self):
        violations = migration_invents_nothing(
            {"read_scope": ["system"], "revision_permission": "auto"}
        )
        assert len(violations) == 2

    def test_empty_values_are_absences_not_inventions(self):
        # the trigger is a NON-EMPTY key: an explicit empty scope is the
        # run saying "nothing was granted", not the migration granting
        assert migration_invents_nothing({"read_scope": [], "revision_permission": ""}) == []


class TestInvariantSuites:
    def test_the_three_named_suites_are_registered(self):
        assert INVARIANT_SUITES == {
            "final_boundary_fence": "tests/test_github_runs.py::TestFinalBoundaryFenceFND02",
            "repository_identity": "tests/test_project_config.py::TestRepositoryIdentityContractFND01",
            "mutation_guards": "tests/test_github_runs.py::TestMutationGuardsC11",
        }

    @pytest.mark.parametrize("suite", sorted(INVARIANT_SUITES))
    def test_every_registered_suite_points_at_a_real_file_and_class(self, suite):
        # the registry half of FND-08: documentation that stays anchored
        # to real nodes (the release manifest is the other half)
        node_id = INVARIANT_SUITES[suite]
        path, class_name = node_id.split("::")
        assert (_REPO_ROOT / path).is_file(), f"{path} is missing"
        source = (_REPO_ROOT / path).read_text(encoding="utf-8")
        assert f"class {class_name}" in source


class TestPerformedVsSkipped:
    def test_outcomes_land_in_their_own_buckets(self):
        buckets = performed_vs_skipped(
            {
                "final_boundary_fence": "pass",
                "repository_identity": "skip",
                "mutation_guards": "unsupported",
            }
        )
        assert buckets == {
            "performed": ["final_boundary_fence"],
            "skipped": ["repository_identity"],
            "unsupported": ["mutation_guards"],
        }

    def test_no_results_means_no_buckets_filled(self):
        assert performed_vs_skipped({}) == {
            "performed": [],
            "skipped": [],
            "unsupported": [],
        }

    def test_a_skip_is_never_converted_to_pass(self):
        buckets = performed_vs_skipped({"legacy_replay": "skip", "new_startup": "pass"})
        assert buckets["skipped"] == ["legacy_replay"]
        assert buckets["performed"] == ["new_startup"]

    def test_an_unknown_outcome_label_fails_loudly(self):
        with pytest.raises(ValueError, match="unknown verification outcome"):
            performed_vs_skipped({"suite": "deferred"})
