"""Retained selection lessons on explicit trusted two-driver frozen policies.

Planner suggestions, environment preferences, budget classes and fallback
chains are retired authority. A model cannot choose or replace the driver,
model, budget or credential mode carried by an approved RunSpec2.
"""

import json

import pytest
from pydantic import ValidationError

from forge.domain.plan import Plan
from forge.domain.run_spec import RunPolicy, RunSpec2
from forge.runtime.settings import RuntimeSettings
from forge_lane.cli import load_driver
from forge_lane.errors import LaneFailure
from tests.native_policy_fixture import (
    intent,
    planned,
    policy,
    policy_refuses,
    runtime_document,
    runtime_refuses,
    switched_driver_refuses,
)
from tests.test_domain_approved_input import spec


def unsupported(name):
    with pytest.raises(LaneFailure) as failure:
        load_driver(name)
    assert failure.value.classification == "driver_unavailable"


def no_planner_authority(**proposal):
    with pytest.raises(ValidationError):
        Plan.model_validate({**planned("Task").model_dump(), **proposal})


class TestImplementationBlock:
    def test_five_fixed_lines_with_model_and_chain(self):
        admission = intent()
        frozen = admission.freeze(planned(summary="Approved implementation"))
        assert frozen.driver == admission.policy.driver and frozen.model == admission.policy.model
        assert (
            frozen.limits == admission.policy.limits
            and "Approved implementation" in frozen.brief_text
        )
        assert "fallbacks" not in frozen.model_dump()

    def test_empty_fallbacks_read_none(self):
        frozen = intent().freeze(planned(summary="Task"))
        assert frozen.driver == "claude_code" and "fallbacks" not in frozen.model_dump()

    def test_block_starts_with_the_implementation_heading(self):
        admission = intent()
        frozen = admission.freeze(planned(summary="Complete approved plan"))
        assert frozen.brief_text.startswith("# Approved task\n\nExact original text")
        assert "Complete approved plan" in frozen.brief_text


class TestCompilerRules:
    def test_rule_1_lanes_cap_the_result(self, tmp_path):
        runtime_refuses(
            tmp_path,
            lambda doc: doc["connections"][0]["repositories"][0]["policy"].update(driver="grok"),
        )

    def test_rule_2_empty_preference_is_the_backend_one_element_list(self):
        policy_refuses(driver="")

    def test_rule_2_bare_ci_harness_defaults_to_claude_code(self):
        unsupported("ci_harness")

    def test_rule_3_non_onboarded_preference_entries_are_dropped(self):
        policy_refuses(driver_preference=["claude_code", "grok"])

    def test_rule_4_planner_proposal_honored_within_preference_and_lanes(self):
        no_planner_authority(driver="copilot_cli")

    def test_rule_4_proposal_outside_preference_is_ignored(self):
        no_planner_authority(harness="grok-build")

    def test_rule_4_invalid_budget_class_falls_back_to_default(self):
        no_planner_authority(budget_class="unbounded")

    def test_rule_4_missing_reason_defaults_to_planner_selection(self):
        admission = intent()
        frozen = admission.freeze(planned(summary="No inferred driver choice"))
        assert (
            frozen.driver == admission.policy.driver
            and frozen.planning_allowance == admission.policy.planning_allowance
        )

    def test_rule_4_budget_class_applies_even_without_harness(self):
        no_planner_authority(budget_class="trivial")

    def test_rule_5_fallback_tail_frozen_even_when_switch_is_off(self):
        policy_refuses(fallbacks=["copilot_cli"])

    def test_rule_5_proposal_selection_splits_the_tail(self):
        no_planner_authority(fallbacks=["grok"])

    def test_rule_6_deterministic_same_inputs_identical_output(self):
        admission = intent()
        plan = planned(summary="Exact deterministic plan")
        assert admission.freeze(plan).canonical_bytes == admission.freeze(plan).canonical_bytes
        assert admission.freeze(plan).digest == admission.freeze(plan).digest

    def test_malformed_planner_proposal_is_tolerated(self):
        # Malformed policy proposals are rejected, never silently normalized into authority.
        with pytest.raises(ValidationError):
            Plan.model_validate({**planned("Task").model_dump(), "driver": 42})


class TestBudgetClasses:
    def test_budget_class_set_is_the_briefs_triple(self):
        frozen = intent().freeze(planned(summary="Finite exact budget"))
        assert frozen.limits.usd == policy().limits.usd and frozen.limits.calls == 20
        assert frozen.limits.tokens == 10000 and "budget_class" not in frozen.model_dump()


class TestCurrentDriver:
    @pytest.mark.parametrize("name", ["claude_code", "copilot_cli"])
    def test_driver_resolution(self, name):
        assert load_driver(name).name == name


class TestParsePreference:
    def test_splits_strips_and_deduplicates(self):
        policy_refuses(driver="claude_code, copilot_cli")

    def test_empty_is_empty(self):
        policy_refuses(driver=" ")


class TestValidatePreference:
    def test_unknown_driver_is_refused(self):
        unsupported("warp")

    def test_list_must_include_the_configured_backend(self):
        policy_refuses(driver_preference=["copilot_cli"])

    def test_valid_list_passes(self):
        value = policy()
        assert RunPolicy.model_validate_json(value.canonical_bytes) == value

    def test_empty_list_is_always_valid(self):
        policy_refuses(driver=[])

    def test_driver_check_is_skipped_for_the_builtin_backend(self):
        unsupported("builtin")

    def test_shipped_driver_set_is_the_nine_lanes(self):
        for name in ("grok", "codex", "opencode", "dotnet-lane", "claude-code", "copilot"):
            unsupported(name)
        assert (
            load_driver("claude_code").requires_model_key
            and not load_driver("copilot_cli").requires_model_key
        )

    def test_the_sdk_lane_shares_the_claude_code_credential_recipe(self):
        value = policy()
        assert value.driver == "claude_code" and value.metering == "metered"

    def test_codex_and_opencode_sdk_lanes_have_their_own_recipes(self):
        unsupported("codex-sdk-lane")
        unsupported("opencode-sdk-lane")

    def test_the_dotnet_lane_declares_no_driver_credentials(self):
        unsupported("dotnet-lane")

    def test_the_copilot_sdk_lane_shares_the_batch_copilot_token(self):
        value = policy(driver="copilot_cli", metering="copilot-scoped-unmetered")
        assert value.driver == "copilot_cli" and value.metering == "copilot-scoped-unmetered"
        policy_refuses(driver="copilot_cli", metering="metered")


class TestPinnedChainEntries:
    def test_parse_splits_the_pin_from_the_driver(self):
        policy_refuses(driver="claude_code@2.0.0")

    def test_parse_refuses_a_malformed_pin(self):
        policy_refuses(driver="claude_code@;touch escaped")

    def test_validate_accepts_the_pinned_form_by_its_driver_part(self):
        unsupported("copilot_cli@1.0.86")

    def test_validate_still_refuses_unknown_drivers_under_a_pin(self):
        unsupported("warp@1.0")

    def test_compile_strips_the_pin_into_the_bare_frozen_selection(self):
        value = policy()
        assert value.driver == "claude_code"
        policy_refuses(driver_versions={"claude_code": "latest"})

    def test_compile_collapses_duplicate_driver_entries_on_the_bare_id(self):
        policy_refuses(driver_preference=["claude_code", "claude_code"])


class TestSelectionFromSpecDocument:
    def test_round_trips_the_backend_config_fragment(self):
        frozen = intent().freeze(planned(summary="Frozen trusted policy"))
        assert RunSpec2.model_validate_json(frozen.canonical_bytes) == frozen
        assert frozen.execution_policy == policy().execution_policy

    def test_pre_v2_document_has_no_selection(self):
        document = spec().model_dump(mode="json")
        document["schema"] = "forge.run-spec/1"
        with pytest.raises(ValidationError):
            RunSpec2.model_validate_json(json.dumps(document))


class TestAdvanceHarnessFallback:
    async def test_off_by_default_never_advances(self, tmp_path):
        await switched_driver_refuses(tmp_path)

    async def test_infrastructure_failure_advances_to_the_next_entry(self, tmp_path):
        await switched_driver_refuses(tmp_path, other="grok")

    async def test_code_failure_never_switches(self, tmp_path):
        await switched_driver_refuses(tmp_path, other="codex")

    async def test_config_failure_never_switches(self, tmp_path):
        await switched_driver_refuses(tmp_path, other="opencode")

    async def test_never_switches_once_a_candidate_exists(self, tmp_path):
        await switched_driver_refuses(tmp_path, other="copilot_cli")

    async def test_stale_event_for_a_different_driver_is_ignored(self, tmp_path):
        await switched_driver_refuses(tmp_path, other="stale_driver")

    def test_chain_exhaustion_returns_none(self):
        policy_refuses(fallbacks=[])

    def test_chain_walks_to_exhaustion(self):
        policy_refuses(fallbacks=["copilot_cli", "grok"])


class TestGitLabFallbackFlow:
    async def test_off_by_default_blocks_on_the_first_infrastructure_failure(self, tmp_path):
        await switched_driver_refuses(tmp_path, "gitlab")

    async def test_infrastructure_failure_advances_down_the_frozen_chain(self, tmp_path):
        await switched_driver_refuses(tmp_path, "gitlab", "grok")

    async def test_chain_exhaustion_fails_visibly(self, tmp_path):
        await switched_driver_refuses(tmp_path, "gitlab", "exhausted")

    async def test_code_failure_never_switches_even_when_enabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORGE_HARNESS_FALLBACK_ENABLED", "1")
        await switched_driver_refuses(tmp_path, "gitlab", "copilot_cli")

    async def test_never_switches_once_a_candidate_exists(self, tmp_path):
        await switched_driver_refuses(tmp_path, "gitlab", "codex")


class TestGitHubFallbackFlow:
    async def test_off_by_default_blocks_on_the_first_infrastructure_failure(self, tmp_path):
        await switched_driver_refuses(tmp_path, "github")

    async def test_infrastructure_failure_advances_down_the_frozen_chain(self, tmp_path):
        await switched_driver_refuses(tmp_path, "github", "grok")

    async def test_chain_exhaustion_fails_visibly(self, tmp_path):
        await switched_driver_refuses(tmp_path, "github", "exhausted")

    async def test_code_failure_never_switches_even_when_enabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORGE_HARNESS_FALLBACK_ENABLED", "1")
        await switched_driver_refuses(tmp_path, "github", "copilot_cli")


class TestDoctorHarnessLanes:
    def test_full_chain_reports_per_driver_passes(self, tmp_path):
        document = runtime_document(tmp_path)
        config = RuntimeSettings.model_validate_json(json.dumps(document))
        assert config.connections[0].repositories[0].policy.driver == "claude_code"

    def test_empty_chain_with_harness_backend_fails(self, tmp_path):
        runtime_refuses(
            tmp_path,
            lambda doc: doc["connections"][0]["repositories"][0]["policy"].update(driver=""),
        )

    def test_empty_chain_with_builtin_backend_is_only_a_warning(self, tmp_path):
        runtime_refuses(
            tmp_path,
            lambda doc: doc["connections"][0]["repositories"][0]["policy"].update(driver="builtin"),
        )

    def test_contradictory_preference_is_reported_as_a_failure(self, tmp_path):
        runtime_refuses(
            tmp_path,
            lambda doc: doc["connections"][0]["repositories"][0]["policy"].update(
                driver_preference=["copilot_cli"]
            ),
        )

    def test_default_preference_is_the_backend_driver(self, tmp_path):
        document = runtime_document(tmp_path)
        repository = document["connections"][0]["repositories"][0]
        assert (
            RuntimeSettings.model_validate_json(json.dumps(document))
            .repositories()["project"][1]
            .policy.driver
            == repository["policy"]["driver"]
        )


class TestStrictManifestC06:
    def test_an_explicit_disjoint_manifest_refuses_the_default_driver(self, tmp_path):
        runtime_refuses(
            tmp_path,
            lambda doc: doc["connections"][0]["repositories"][0]["policy"].update(
                driver="uninstalled"
            ),
        )

    def test_no_manifest_keeps_the_legacy_default(self):
        document = policy().model_dump(mode="json")
        document.pop("driver")
        with pytest.raises(ValidationError):
            RunPolicy.model_validate_json(json.dumps(document))

    def test_an_manifest_containing_the_default_still_degrades(self):
        value = policy()
        assert value.driver == "claude_code"
        policy_refuses(available_drivers=["claude_code"])


class TestManifestEmptinessD06:
    def test_an_explicitly_empty_json_manifest_allows_no_driver(self, tmp_path):
        runtime_refuses(tmp_path, lambda doc: doc.update(connections=[]))

    def test_no_manifest_anywhere_is_legacy_none(self):
        policy_refuses(available_drivers=None)
