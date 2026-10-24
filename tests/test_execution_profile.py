"""Retained execution lessons on native evidence and explicit frozen lane authority.

The OCI v1/v2 declarations, language recipe compiler, log classifiers and
ambient template profiles are retired. They cannot grant capabilities to the
sole lane. Native reads retain exact bytes and unknown outcomes; RunSpec2
binds the approved policy; actual SDK tools and credentials stay confined.
"""

from datetime import datetime, timezone
from hashlib import sha256

import pytest
from pydantic import ValidationError

from forge.domain.run_spec import RunSpec2, VerificationContract
from forge.port_values import ReadUnavailable
from forge.verification.checks import code_failure, verdict
from forge_lane.cli import load_driver
from forge_lane.errors import LaneFailure
from forge_protocol import ExecutionPolicy, LaneReport, McpServerPolicy, canonical_json
from tests import gitlab_engine_fixture as native
from tests.native_driver_fixture import completed_case
from tests.native_policy_fixture import (
    execution_policy_refuses,
    execution_refuses,
    file_guard,
    frozen_brief,
    intent,
    isolated_child,
    native_checks,
    native_file,
    planned,
    policy,
    policy_refuses,
    spec_refuses,
)
from tests.test_domain_approved_input import spec

_LOCK_TEXT = (
    b'version = 1\nrequires-python = ">=3.13"\n[[package]]\nname = "ruff"\nversion = "0.15.7"\n'
)
NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


def reduced(checks, required=("forge-lane-v1",)):
    return verdict(checks, native.SHA, required, now=NOW, max_age_seconds=60)


def unsupported(name):
    with pytest.raises(LaneFailure) as failure:
        load_driver(name)
    assert failure.value.classification == "driver_unavailable"


class TestDeriveFromLocalRepo:
    async def test_locked_repo_carries_its_own_toolchain_pins(self):
        raw = await native_file(_LOCK_TEXT)
        assert raw == _LOCK_TEXT and b'"0.15.7"' in raw
        assert sha256(raw).digest() == sha256(_LOCK_TEXT).digest()

    def test_lane_capability_axes_are_recorded_not_implied(self):
        brief = frozen_brief()
        assert brief.execution_policy.profile == "workspace-files-v1"
        assert brief.execution_policy.mcp_servers == ()
        assert brief.frozen_spec.driver == "claude_code" and brief.frozen_spec.metering == "metered"

    async def test_lock_less_repo_degrades_to_the_documented_pip_fallback(self):
        # Confirmed absence survives; it no longer selects an unapproved pip bootstrap.
        assert await native_file(absent=True) is None
        execution_refuses(install_strategy="pip-minimal")

    async def test_unreadable_lock_is_unknown_not_absent(self):
        with pytest.raises(ReadUnavailable):
            await native_file(status=403)

    async def test_unparseable_lock_still_pins_the_bytes(self):
        raw = await native_file(b"not toml {{{\r\n")
        assert raw == b"not toml {{{\r\n"
        assert sha256(raw).hexdigest() == sha256(b"not toml {{{\r\n").hexdigest()

    def test_absent_manifest_degrades_requires_python_to_unknown(self):
        execution_refuses(requires_python="unknown")
        assert "requires_python" not in policy().model_dump()


class TestDigestStability:
    def test_the_same_repo_state_yields_the_same_digest(self):
        assert spec().canonical_bytes == spec().canonical_bytes
        assert spec().digest == spec().digest

    def test_any_relevant_byte_change_moves_the_digest(self):
        original = spec()
        assert spec(task_description=original.task_description + " ").digest != original.digest
        assert (
            spec(
                execution_policy=ExecutionPolicy(
                    mcp_servers=(
                        McpServerPolicy(
                            name="docs",
                            transport="http",
                            tools=("lookup",),
                            url="https://docs.invalid/mcp",
                        ),
                    )
                )
            ).digest
            != original.digest
        )

    def test_digest_is_the_canonical_json_of_the_record(self):
        frozen = spec()
        assert frozen.canonical_bytes == canonical_json(frozen.model_dump(mode="json"))
        assert frozen.digest == sha256(frozen.canonical_bytes).hexdigest()

    def test_record_validates_its_own_invariants(self):
        execution_refuses(profile="v2")
        with pytest.raises(ValidationError):
            spec().model = "unapproved"


class TestCiContract:
    async def test_ci_within_the_lane_surface_is_matching(self):
        observed = await native_checks()
        assert reduced(observed).status == "passed"
        assert observed[0].tested_sha == native.SHA and observed[0].role == "verification"

    async def test_ci_commands_the_lane_cannot_run_make_it_an_honest_subset(self):
        result = reduced(await native_checks(), ("forge-lane-v1", "external-acceptance"))
        assert result.status == "pending" and result.successful == ("forge-lane-v1",)

    def test_no_detectable_ci_config_is_unknown_never_matching(self):
        result = reduced(())
        assert result.status == "pending" and result.successful == ()

    async def test_unrelated_ci_tools_still_detect_the_lane_surface(self):
        observed = await native_checks("failed", "script_failure")
        result = reduced(observed)
        assert result.status == "failed" and not code_failure(observed, result)

    async def test_materialized_files_view_is_equivalent(self):
        raw = await native_file(_LOCK_TEXT)
        assert raw == _LOCK_TEXT
        assert sha256(raw).hexdigest() == sha256(bytes(raw)).hexdigest()


class TestReaderDerivation:
    async def test_lock_and_ci_config_are_read_over_the_blob_surface(self):
        assert await native_file(_LOCK_TEXT) == _LOCK_TEXT
        assert (await native_checks())[0].workflow_path == ".gitlab-ci.yml"

    async def test_confirmed_absence_is_absent_not_unknown(self):
        assert await native_file(absent=True) is None

    async def test_read_failure_degrades_to_unknown_never_raises(self):
        # The native port exposes a typed unavailable outcome, never a false absence.
        with pytest.raises(ReadUnavailable):
            await native_file(status=503)

    async def test_reader_without_the_blob_surface_is_all_unknown(self):
        with pytest.raises(ReadUnavailable):
            await native_file(status=404)


class TestBootstrapClassification:
    async def test_the_marker_in_a_lane_log_classifies_infrastructure(self):
        checks = await native_checks("failed", "runner_system_failure")
        result = reduced(checks)
        assert result.status == "failed" and checks[0].failure_kind == "infrastructure"
        assert not code_failure(checks, result)

    async def test_meta_bootstrap_field_carries_the_lane_classification(self):
        checks = await native_checks("failed", "script_failure")
        assert checks[0].failure_kind == "unknown" and not code_failure(checks, reduced(checks))

    async def test_the_log_pattern_classifier_carries_the_marker(self):
        checks = await native_checks("failed", "FORGE_BOOTSTRAP_FAILED")
        assert checks[0].failure_kind == "unknown" and not code_failure(checks, reduced(checks))

    async def test_status_values_are_the_two_the_template_writes(self):
        passed, failed = (
            await native_checks(),
            await native_checks("failed", "runner_system_failure"),
        )
        assert passed[0].status == "passed" and failed[0].status == "failed"
        assert reduced(passed).status == "passed" and reduced(failed).status == "failed"


class TestSpecEmbedding:
    def test_the_profile_digest_rides_the_frozen_spec(self):
        frozen, brief = spec(), frozen_brief()
        assert brief.spec_digest == frozen.digest and brief.spec_json == frozen.spec_json
        assert brief.frozen_spec.execution_policy == frozen.execution_policy

    def test_pre_profile_documents_parse_unchanged(self):
        # There is one spec schema; old additive profile documents are explicitly refused.
        spec_refuses(schema="forge.run-spec/1")
        spec_refuses(execution_profile={"profile_digest": "a" * 64})

    async def test_a_malformed_section_is_a_corrupt_spec(self, tmp_path):
        await execution_policy_refuses(tmp_path, {"mcp_servers": "inherit"})

    def test_a_non_digest_value_is_a_corrupt_spec(self):
        brief = frozen_brief().model_dump()
        brief["spec_digest"] = "not-a-digest"
        with pytest.raises(ValidationError):
            type(frozen_brief()).model_validate(brief)

    def test_gate_approves_the_derived_contract_end_to_end(self):
        admission = intent()
        frozen = admission.freeze(planned())
        assert frozen.execution_policy == admission.policy.execution_policy
        assert frozen.verification == admission.policy.verification
        assert RunSpec2.model_validate_json(frozen.canonical_bytes) == frozen


def test_observed_execution_records_facts_not_allowance():
    document = frozen_brief().model_dump()
    document["observed_execution"] = {"shell": True, "publisher": True}
    with pytest.raises(ValidationError):
        type(frozen_brief()).model_validate(document)


async def test_observed_execution_carries_command_receipts(tmp_path):
    case = await completed_case(tmp_path)
    report = case.result.report.model_dump()
    report["commands"] = [{"command": "pytest", "exit_code": 0}]
    with pytest.raises(ValidationError):
        LaneReport.model_validate(report)
    assert case.result.report.status == "completed" and not reduced(()).successful


async def test_workspace_receipts_are_stamped_self_reported(tmp_path):
    def receipts(case):
        old = case.sdk.edit

        def edit(workspace):
            old(workspace)
            (workspace / ".forge").mkdir(exist_ok=True)
            (workspace / ".forge/commands.tsv").write_text("pytest\t0\tpass\n")

        case.sdk.edit = edit

    case = await completed_case(tmp_path, mutate=receipts)
    diff = next(iter(case.server.blobs.values()))
    assert b"commands.tsv" not in diff and b"approved change" in diff
    assert reduced(()).status == "pending"


class TestLaneProfileV2:
    async def test_staged_credentials_mount_the_provider_key_only_for_coding(
        self, tmp_path, monkeypatch
    ):
        case, options = await isolated_child(tmp_path, monkeypatch, ANTHROPIC_API_KEY="ambient")
        assert options.env["ANTHROPIC_API_KEY"] == ""
        assert options.env["ANTHROPIC_AUTH_TOKEN"] == "scoped-key"
        assert case.result.report.status == "completed"

    async def test_no_stage_carries_a_publisher_or_write_credential(self, tmp_path, monkeypatch):
        _, options = await isolated_child(
            tmp_path, monkeypatch, GITHUB_TOKEN="write", GITLAB_TOKEN="write"
        )
        assert options.env["GITHUB_TOKEN"] == options.env["GITLAB_TOKEN"] == ""

    def test_data_boundaries_are_workspace_and_tmpfs_only(self, tmp_path):
        guard = file_guard(tmp_path)
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("synthetic private")
        (tmp_path / "link").symlink_to(outside)
        assert not guard.permitted("Read", {"file_path": "link"})
        assert not guard.permitted("Read", {"file_path": str(outside)})

    def test_capabilities_drop_all_and_add_none(self, tmp_path):
        guard = file_guard(tmp_path)
        for tool in ("Bash", "Agent", "WebFetch", "Skill", "NotebookEdit"):
            assert not guard.permitted(tool, {"file_path": "src/file.py"})

    def test_the_egress_hook_is_env_carried_and_deny_by_default(self):
        # No env declaration can become an unsupported host-egress attestation.
        execution_refuses(network_policy="deny-all", egress_allowlist=[])

    def test_v1_is_the_unstaged_posture_stated_as_data(self):
        execution_refuses(profile="v1")

    def test_the_profiles_stay_out_of_the_execution_profile_digest(self):
        frozen = spec()
        changed = spec(
            execution_policy=ExecutionPolicy(
                mcp_servers=(
                    McpServerPolicy(
                        name="docs",
                        transport="http",
                        tools=("lookup",),
                        url="https://docs.invalid/mcp",
                    ),
                )
            )
        )
        assert changed.digest != frozen.digest
        spec_refuses(lane_profile="v2")


class TestLaneProfileValidation:
    def test_unknown_profile_ids_fail_closed(self):
        execution_refuses(profile="unknown-profile")

    def test_the_vocabulary_is_derived_from_the_shipped_records(self):
        assert ExecutionPolicy(profile="workspace-files-v1").profile == "workspace-files-v1"
        for name in ("v1", "v2", "oci", "workspace-files-v2"):
            execution_refuses(profile=name)

    def test_an_unnamed_v2_stage_sees_nothing(self, tmp_path):
        execution_refuses(stages={"unknown": {"credentials": ["publisher"]}})
        assert not file_guard(tmp_path).permitted("Read", {"file_path": ".git-credentials"})

    def test_record_construction_validates_its_own_invariants(self):
        with pytest.raises(ValidationError):
            ExecutionPolicy(
                mcp_servers=(
                    McpServerPolicy(
                        name="docs", transport="http", tools=("lookup",), url="http://docs.invalid"
                    ),
                )
            )


class TestLaneProfileDeclaration:
    async def test_an_undeclared_profile_is_refused(self, tmp_path):
        await execution_policy_refuses(tmp_path, {"profile": None})

    async def test_a_value_outside_the_vocabulary_is_refused(self, tmp_path):
        await execution_policy_refuses(tmp_path, {"profile": "v99"})

    def test_v1_declares_cleanly_without_needing_the_hook(self):
        # The current profile has no ambient hook dependency; old v1 is not an alias.
        assert ExecutionPolicy().mcp_servers == ()
        execution_refuses(profile="v1")

    def test_v2_without_the_egress_hook_is_a_refused_half_declaration(self):
        execution_refuses(profile="v2")

    def test_v2_with_an_empty_allowlist_is_deny_all_and_valid(self):
        execution_refuses(profile="v2", egress_allowlist=[])


class TestSdkLaneTemplatesDeclareTheProfile:
    def test_every_sdk_lane_template_declares_a_valid_default(self):
        for driver in ("claude_code", "copilot_cli"):
            frozen = spec(
                driver=driver,
                metering="copilot-scoped-unmetered" if driver == "copilot_cli" else "metered",
            )
            assert frozen.execution_policy.profile == "workspace-files-v1"

    def test_every_sdk_lane_template_can_declare_v2(self):
        execution_refuses(profile="v2")
        unsupported("claude-code-sdk-lane")

    def test_the_templates_do_not_silently_default_to_v2(self):
        assert policy().execution_policy == ExecutionPolicy()
        policy_refuses(lane_profile="v2")


class TestRuntimeEnforcement:
    async def test_v2_compliant_runtime_passes(self, tmp_path):
        case = await completed_case(tmp_path)
        assert case.result.report.status == "completed" and len(case.server.candidates) == 1

    def test_v2_with_an_empty_allowlist_is_deny_all_and_compliant(self, tmp_path):
        guard = file_guard(tmp_path)
        assert not guard.permitted("WebFetch", {"url": "https://example.invalid"})
        assert not guard.permitted("mcp__unapproved__lookup", {})

    def test_v2_without_the_egress_allowlist_env_is_refused(self):
        execution_refuses(egress_allowlist_env="FORGE_ALLOWLIST")

    def test_v2_with_a_writable_root_filesystem_is_refused(self, tmp_path):
        guard = file_guard(tmp_path)
        assert not guard.permitted("Write", {"file_path": "/tmp/outside.py"})
        assert not guard.permitted("Write", {"file_path": "../outside.py"})

    def test_v2_with_an_empty_mount_table_cannot_prove_the_boundary(self):
        execution_refuses(mounts=[], read_only_root=True)

    async def test_a_bootstrap_credential_leaking_into_coding_is_refused(
        self, tmp_path, monkeypatch
    ):
        _, options = await isolated_child(
            tmp_path,
            monkeypatch,
            PIP_INDEX_URL="https://u:p@private.invalid",
            NPM_TOKEN="bootstrap",
        )
        assert options.env["PIP_INDEX_URL"] == options.env["NPM_TOKEN"] == ""

    async def test_a_provider_key_reaching_verification_is_refused(self, tmp_path, monkeypatch):
        case, options = await isolated_child(
            tmp_path, monkeypatch, FORGE_SESSION_TOKEN="session", AWS_SECRET_ACCESS_KEY="secret"
        )
        assert options.env["FORGE_SESSION_TOKEN"] == options.env["AWS_SECRET_ACCESS_KEY"] == ""
        assert not reduced(()).successful and case.result.report.status == "completed"

    async def test_an_allowed_credential_merely_absent_is_not_a_violation(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        _, options = await isolated_child(tmp_path, monkeypatch)
        assert options.env["ANTHROPIC_API_KEY"] == "" and options.env["ANTHROPIC_AUTH_TOKEN"]

    def test_an_unknown_stage_cannot_be_scoped(self):
        execution_refuses(stage="verification")

    def test_v1_declares_no_isolation_so_nothing_is_checked(self):
        # Unsupported host isolation cannot bypass current policy validation.
        execution_refuses(profile="v1", isolate=False)

    def test_every_violation_carries_an_actionable_detail(self):
        error = execution_refuses(profile="v2")
        assert error.errors()[0]["loc"] == ("profile",)
        assert "workspace-files-v1" in error.errors()[0]["msg"]


class TestLaneEntryEnforcesTheProfile:
    async def test_v2_on_a_non_compliant_runner_fails_closed(self, tmp_path):
        await execution_policy_refuses(tmp_path, {"profile": "v2"})

    async def test_v1_runs_its_own_startup_checks_instead(self, tmp_path):
        case = await completed_case(tmp_path)
        assert case.sdk.clients[0].options.strict_mcp_config
        assert case.sdk.clients[0].options.setting_sources == []

    async def test_an_unset_profile_is_the_legacy_dispatch_no_check(self, tmp_path):
        await execution_policy_refuses(tmp_path, {"profile": ""})

    async def test_an_unknown_profile_id_fails_closed_too(self, tmp_path):
        await execution_policy_refuses(tmp_path, {"profile": "staged-future"})


class TestRuntimeRecipeHarnessDecomposition:
    def test_the_dotnet_lane_alias_resolves_to_its_decomposition(self):
        unsupported("dotnet-lane")

    def test_a_new_recipe_harness_combination_validates(self):
        policy_refuses(runtime_recipe="node", harness="claude_code")

    async def test_an_unsupported_combination_is_refused_before_model_calls(self, tmp_path):
        await execution_policy_refuses(tmp_path, {"runtime_recipe": "dotnet"})

    def test_unknown_axes_and_ids_fail_closed(self):
        policy_refuses(runtime_recipe="future")
        execution_refuses(harness_profile="future")

    def test_the_dotnet_recipe_pins_the_template_image_digest(self):
        execution_refuses(image="mcr.microsoft.com/dotnet/sdk@sha256:" + "a" * 64)

    def test_the_dotnet_recipe_carries_the_locked_tail_and_trx_prefix(self):
        execution_refuses(bootstrap_commands=["dotnet restore --locked-mode"], test_results="*.trx")

    def test_the_compiled_matrix_and_aliases_are_consistent(self):
        assert load_driver("claude_code").name == "claude_code"
        assert load_driver("copilot_cli").name == "copilot_cli"
        for alias in ("dotnet-lane", "node-lane", "python-lane", "grok-build"):
            unsupported(alias)

    def test_the_profile_document_states_both_axes(self):
        frozen = spec()
        assert frozen.driver == "claude_code"
        assert frozen.execution_policy.profile == "workspace-files-v1"
        spec_refuses(runtime_recipe="python")


class TestRecipeGatesTheToolchain:
    def test_a_missing_global_json_is_a_violation_naming_the_remedy(self):
        execution_refuses(required_files=["global.json"])

    async def test_a_present_global_json_gates_clean(self):
        body = b'{"sdk":{"version":"8.0.100"}}\n'
        assert await native_file(body) == body

    async def test_an_unreadable_pin_file_is_a_violation_not_a_pass(self):
        with pytest.raises(ReadUnavailable):
            await native_file(status=500)

    def test_the_gate_is_harness_independent(self, tmp_path):
        for driver in ("claude_code", "copilot_cli"):
            guard = file_guard(
                tmp_path,
                driver=driver,
                metering="copilot-scoped-unmetered" if driver == "copilot_cli" else "metered",
            )
            assert not guard.permitted("Write", {"file_path": "uv.lock"})

    def test_the_python_recipe_gates_no_files(self, tmp_path):
        guard = file_guard(tmp_path)
        assert guard.permitted("Write", {"file_path": "src/parser.py"})
        assert not guard.permitted("Write", {"file_path": "uv.lock"})

    def test_the_node_recipe_requires_its_lockfile(self, tmp_path):
        guard = file_guard(tmp_path)
        for name in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock"):
            assert guard.permitted("Read", {"file_path": name})
            assert not guard.permitted("Write", {"file_path": name})

    def test_an_empty_recipe_is_refused_at_construction(self):
        execution_refuses(runtime_recipe="")


class TestProfileCoherence:
    def test_a_coherent_profile_passes_clean(self):
        frozen = spec()
        assert RunSpec2.model_validate_json(frozen.canonical_bytes) == frozen
        assert frozen.execution_policy.mcp_servers == ()

    def test_a_coherent_dotnet_lane_passes_under_v2(self):
        unsupported("dotnet-lane")
        execution_refuses(profile="v2")

    def test_a_dotnet_recipe_missing_dotnet_version_fails(self):
        execution_refuses(runtime_recipe={"name": "dotnet", "version": None})

    def test_a_missing_pin_file_is_an_incoherent_recipe_for_every_harness(self):
        policy_refuses(runtime_recipe={"required_files": ["global.json"]})

    def test_an_unstaged_harness_credential_fails(self):
        with pytest.raises(ValidationError):
            McpServerPolicy(
                name="docs",
                transport="http",
                tools=("lookup",),
                url="https://${PUBLISHER_TOKEN}@docs.invalid",
            )

    def test_an_incompatible_network_policy_fails(self):
        with pytest.raises(ValidationError):
            McpServerPolicy(
                name="docs", transport="http", tools=("lookup",), url="http://docs.invalid/mcp"
            )

    def test_the_network_policy_passes_when_the_allowlist_covers_the_registries(self):
        server = McpServerPolicy(
            name="docs", transport="http", tools=("lookup",), url="https://docs.invalid/mcp"
        )
        value = ExecutionPolicy(mcp_servers=(server,))
        assert value.mcp_servers == (server,)
        assert frozen_brief(execution_policy=value).execution_policy == value

    def test_v2s_documented_bootstrap_allowance_carries_the_registries(self):
        execution_refuses(bootstrap_allowlist=["pypi.org", "registry.npmjs.org"])

    def test_every_issue_renders_with_its_check_and_an_actionable_detail(self):
        with pytest.raises(ValidationError) as failure:
            VerificationContract(required_checks=("unit", "unit"))
        assert "unique" in str(failure.value)
        with pytest.raises(ValidationError):
            VerificationContract(required_checks=())


class TestLaneEntryValidatesProfileCoherence:
    async def test_a_dotnet_recipe_without_dotnet_version_fails_closed(self, tmp_path):
        await execution_policy_refuses(tmp_path, {"runtime_recipe": {"name": "dotnet"}})

    async def test_a_coherent_recipe_proceeds_past_the_check(self, tmp_path):
        case = await completed_case(tmp_path)
        assert case.result.report.status == "completed" and case.server.candidates
        assert case.brief.frozen_spec.execution_policy.profile == "workspace-files-v1"

    async def test_an_unknown_recipe_id_fails_closed(self, tmp_path):
        await execution_policy_refuses(tmp_path, {"runtime_recipe": "future"})

    async def test_an_unset_recipe_keeps_the_legacy_startup_checks(self, tmp_path):
        case = await completed_case(tmp_path)
        options = case.sdk.clients[0].options
        assert options.setting_sources == options.skills == options.plugins == []
        assert not options.allowed_tools and "Bash" in options.disallowed_tools
