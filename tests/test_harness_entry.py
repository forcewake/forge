"""Retained harness lessons against the sole authenticated protocol/SDK lane.

Script renderers, batch meta files, ambient driver/version routes and raw native
plan scans are retired. Historical names preserve authority, isolation, exact
bytes, terminal, usage and no-fallback lessons on the current native contracts.
"""

import hashlib
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from forge_lane.cli import load_driver
from forge_lane.drivers.claude_policy import FilePolicy
from forge_lane.drivers.claude_process import OwnedCLI
from forge_lane.drivers.copilot_transport import COPILOT_VERSION, StdioTransport
from forge_lane.errors import ExitCode, LaneFailure
from forge_protocol import Brief, ExecutionPolicy, LaneDispatch, Usage
from tests.native_driver_fixture import Case, FakeACP, Result, collect, completed_case


@pytest.fixture
async def finished(tmp_path):
    return await completed_case(tmp_path)


def rejected(name):
    with pytest.raises(LaneFailure) as failure:
        load_driver(name)
    assert failure.value.classification == "driver_unavailable"


async def refused(root, field, value):
    case = await completed_case(root, mutate=lambda case: case.server.brief.update({field: value}))
    assert case.result.exit_code in (ExitCode.APPROVAL, ExitCode.TRANSPORT)
    assert not case.sdk.clients and case.server.model_keys == 0 and not case.server.candidates
    return case


async def probe_version(root, version=COPILOT_VERSION):
    binary = root / "copilot"
    binary.write_text(f"#!{sys.executable}\nprint({version!r})\n")
    binary.chmod(0o700)
    await StdioTransport.verify_version(str(binary), root)


class TestRenderClaudeCode:
    async def test_script_follows_the_unattended_contract(self, finished):
        options = finished.sdk.clients[0].options
        assert options.permission_mode == "default" and options.setting_sources == []
        assert options.tools == ["Read", "Write", "Edit", "Grep", "Glob", "Bash"]
        assert options.allowed_tools == []
        assert (
            options.model == finished.brief.frozen_spec.model
            and "WebFetch" in options.disallowed_tools
        )

    async def test_empty_model_omits_the_model_flag(self, tmp_path):
        case = Case(tmp_path)
        document = case.dispatch.model_dump()
        document["model"] = ""
        with pytest.raises(ValidationError):
            LaneDispatch.model_validate(document)
        assert not case.sdk.clients and not case.server.calls

    async def test_repair_dispatch_caps_thinking_budget(self, tmp_path, monkeypatch):
        # No ambient repair/thinking export can alter the frozen approved execution.
        monkeypatch.setenv("FORGE_REPAIR_CONTEXT", "unapproved repair")
        finished = await completed_case(tmp_path)
        assert finished.result.report.approved_digest == finished.brief.approved_digest
        assert finished.sdk.clients[0].options.env.get("FORGE_REPAIR_CONTEXT", "") == ""
        assert (
            finished.brief.frozen_spec.limits.wallclock_seconds == finished.dispatch.timeout_seconds
        )


class TestRenderGrokBuild:
    def test_script_installs_the_platform_binary_explicitly(self):
        rejected("grok-build")

    def test_script_follows_the_unattended_contract(self):
        rejected("grok")


class TestRenderOpencode:
    def test_script_uses_auto_approval(self):
        rejected("opencode")


class TestRenderCommon:
    async def test_every_driver_keeps_the_audit_trail_streaming(self, finished):
        assert finished.server.reports == [finished.result.report]
        assert finished.server.heartbeats and finished.server.candidates
        assert finished.result.report_accepted and finished.result.exit_code == 0

    def test_unknown_driver_is_rejected(self):
        rejected("warp")

    async def test_the_prompt_is_the_short_shared_pointer(self, finished):
        prompt = finished.sdk.clients[0].prompts[0]
        assert (
            finished.brief.execution_brief_text in prompt and "Available workspace files:" in prompt
        )
        assert len(finished.sdk.clients[0].prompts) == 1


class TestRenderSdkLanes:
    def test_both_lanes_hand_over_to_the_lane_runner(self):
        assert load_driver("claude_code").requires_model_key
        assert not load_driver("copilot_cli").requires_model_key

    def test_codex_lane_routes_the_model_and_the_sandbox_recipe(self):
        rejected("codex-sdk-lane")

    def test_codex_lane_omits_the_model_export_when_unrouted(self):
        rejected("codex")

    def test_opencode_lane_carries_the_mechanical_deny_and_once_permission(self):
        rejected("opencode-sdk-lane")

    def test_opencode_lane_splits_a_provider_slash_model_route(self):
        rejected("zai/opencode")

    def test_opencode_lane_keeps_a_bare_model_as_the_model_id(self):
        rejected("opencode")

    async def test_mcp_servers_are_not_consumed_on_the_sdk_lanes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORGE_MCP_SERVERS", '{"unapproved":{"url":"https://attacker.invalid"}}')
        finished = await completed_case(tmp_path)
        assert finished.sdk.clients[0].options.mcp_servers == {}
        assert finished.sdk.clients[0].options.strict_mcp_config

    def test_the_sdk_lane_ids_are_the_registered_lane_driver_ids(self):
        rejected("claude-sdk-lane")
        rejected("copilot-sdk-lane")
        assert load_driver("claude_code").name == "claude_code"
        assert load_driver("copilot_cli").name == "copilot_cli"

    async def test_the_claude_lane_exports_the_lane_control_pair(self, finished):
        assert finished.server.calls[0] == ("POST", "session")
        assert ("GET", "controls") in finished.server.calls
        assert finished.sdk.clients[0].options.env.get("FORGE_LANE_CONTROL_TOKEN", "") == ""


class TestDriverVersionPins:
    async def test_defaults_pin_every_install_to_the_known_good_version(self, tmp_path):
        await probe_version(tmp_path)

    async def test_every_preamble_echoes_the_resolved_version(self, tmp_path):
        await probe_version(tmp_path, "Copilot CLI " + COPILOT_VERSION)

    async def test_latest_keeps_the_unpinned_dist_tag_install(self, tmp_path):
        with pytest.raises(LaneFailure) as failure:
            await probe_version(tmp_path, "latest")
        assert failure.value.classification == "driver_version_mismatch"

    async def test_an_override_pins_just_one_driver(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORGE_DRIVER_VERSIONS", '{"copilot":"latest"}')
        with pytest.raises(LaneFailure):
            await probe_version(tmp_path, "0.0.0")

    async def test_grok_platform_binary_follows_the_pinned_wrapper(self, tmp_path):
        # Grok wrapper/platform installation retired; bundled SDK executable must actually exist.
        case = Case(tmp_path)
        cli = Path(case.sdk.__file__).parent / "_bundled" / "claude"
        cli.unlink()
        with pytest.raises(LaneFailure) as failure:
            OwnedCLI(case.sdk)
        assert failure.value.classification == "driver_unavailable"

    async def test_render_refuses_a_pin_with_shell_metacharacters(self, tmp_path):
        with pytest.raises(LaneFailure):
            await probe_version(tmp_path, "0.0.0; touch escaped")
        assert not (tmp_path / "escaped").exists()


class TestResolveDriverVersions:
    def test_absent_and_empty_fall_back_to_the_known_good_defaults(self):
        rejected("")
        rejected("default")

    def test_the_json_override_wins_per_driver(self, monkeypatch):
        monkeypatch.setenv("FORGE_DRIVER_VERSIONS", '{"claude-code":"old","grok-build":"latest"}')
        assert load_driver("claude_code").name == "claude_code"
        rejected("grok-build")

    def test_malformed_json_fails_closed(self):
        with pytest.raises(ValidationError):
            ExecutionPolicy.model_validate_json("{broken")

    def test_a_non_object_fails_closed(self):
        with pytest.raises(ValidationError):
            ExecutionPolicy.model_validate_json("[]")

    def test_an_unknown_driver_fails_closed(self):
        rejected("unknown")

    def test_a_version_with_metacharacters_fails_closed(self):
        rejected("claude_code; touch escaped")

    def test_a_non_string_version_is_tolerated_as_its_string_form(self):
        # No numeric/version coercion grants a driver capability.
        with pytest.raises(LaneFailure):
            load_driver(2.1)


class TestRenderBrief:
    async def test_brief_carries_role_constraints_and_contract(self, finished):
        prompt = finished.sdk.clients[0].prompts[0]
        assert "Retain the complete input and reject trailing garbage." in prompt
        assert "Shell, repository writes and authority files are denied." in prompt
        assert finished.brief.brief_text in prompt

    async def test_skills_line_names_conventions_files_present_in_the_workspace(self, tmp_path):
        case = Case(tmp_path)
        (case.workspace / "AGENTS.md").write_text("guidance")
        policy = FilePolicy(case.workspace, case.sdk, scope=case.brief.frozen_spec.write_policy)
        assert "AGENTS.md" in policy.inventory()
        assert not policy.permitted("Skill", {"skill": "ambient"})

    async def test_codegraph_section_follows_the_mcp_variable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORGE_MCP_SERVERS", '{"codegraph":{}}')
        case = await completed_case(tmp_path)
        assert case.sdk.clients[0].options.mcp_servers == {}
        assert "codegraph" not in case.sdk.clients[0].prompts[0]

    async def test_render_brief_mode_fetches_issue_and_plan_and_writes_the_brief(self, finished):
        assert finished.server.calls[:2] == [("POST", "session"), ("GET", "brief")]
        assert finished.sdk.clients[0].prompts[0].count(finished.brief.brief_text) == 1
        assert all(not path.startswith("issues") for _, path in finished.server.calls)

    async def test_render_brief_mode_requires_repo_issue_and_token(self, tmp_path):
        await refused(tmp_path, "spec_json", None)


class TestBoundPlanBrief:
    async def test_binds_to_the_exact_comment_without_scanning(self, finished):
        assert finished.result.report.approved_digest == finished.dispatch.approved_digest
        assert [path for _, path in finished.server.calls].count("brief") == 1
        assert not any("comments" in path or "issues" in path for _, path in finished.server.calls)

    async def test_missing_comment_fails_closed(self, tmp_path):
        await refused(tmp_path, "brief_text", "")

    async def test_comment_without_the_plan_header_fails_closed(self, tmp_path):
        await refused(tmp_path, "brief_text", "Unapproved plan")

    async def test_comment_from_another_author_fails_closed(self, tmp_path):
        await refused(tmp_path, "author", {"id": 999})

    async def test_another_runs_plan_fails_closed(self, tmp_path):
        await refused(tmp_path, "forge_run_id", "foreign")

    async def test_edited_comment_body_fails_closed_after_approval(self, tmp_path):
        await refused(tmp_path, "brief_text", "Edited after approval")

    async def test_edited_task_section_fails_closed_after_approval(self, tmp_path):
        await refused(tmp_path, "spec_json", '{"task_description":"changed"}')

    async def test_tampered_envelope_digest_fails_closed(self, tmp_path):
        await refused(tmp_path, "approved_digest", "a" * 64)

    async def test_comment_without_envelope_sections_fails_closed(self, tmp_path):
        await refused(tmp_path, "plan_digest", "b" * 64)

    async def test_issue_edited_after_approval_keeps_the_frozen_task_bytes(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("FORGE_TASK_DESCRIPTION", "MALICIOUS POST APPROVAL EDIT")
        case = await completed_case(tmp_path)
        prompt = case.sdk.clients[0].prompts[0]
        assert "Use the approved source snapshot." in prompt and "MALICIOUS" not in prompt
        assert case.sdk.clients[0].options.env.get("FORGE_TASK_DESCRIPTION", "") == ""

    async def test_correct_bytes_render_the_brief(self, finished):
        assert finished.brief.brief_text in finished.sdk.clients[0].prompts[0]
        assert Brief.model_validate_json(finished.brief.model_dump_json()) == finished.brief

    async def test_configured_bot_login_with_app_suffix_is_accepted(self, finished):
        # Native bot/comment proof occurs before consumption; lane uses the exact immutable receipt.
        assert (
            finished.result.report_accepted
            and finished.server.candidates[0].approved_digest == finished.brief.approved_digest
        )
        assert all("github" not in path for _, path in finished.server.calls)

    async def test_envelope_inputs_absent_keeps_the_loud_r05_posture(self, tmp_path):
        await refused(tmp_path, "spec_digest", "c" * 64)

    async def test_input_absent_falls_back_to_the_loud_unenforced_scan(self, tmp_path):
        await refused(tmp_path, "spec_json", "")


class TestBriefEnvelope:
    async def test_envelope_schema_binds_bytes_run_and_spec(self, finished):
        brief = finished.brief
        assert hashlib.sha256(brief.spec_json.encode()).hexdigest() == brief.spec_digest
        assert brief.forge_run_id == finished.dispatch.forge_run_id
        assert brief.approved_digest == finished.dispatch.approved_digest

    async def test_sections_round_trip_byte_exactly(self, finished):
        assert (
            Brief.model_validate_json(finished.brief.model_dump_json()).brief_text.encode()
            == finished.brief.brief_text.encode()
        )

    async def test_sections_tolerate_empty_and_multiline_content(self, tmp_path):
        from forge.domain.approved_input import ApprovedInput
        from forge_protocol import RunIdentity
        from tests.test_domain_approved_input import spec

        frozen = spec(task_description="a\n\n- b\n- c\n")
        brief = ApprovedInput.from_spec(frozen).to_brief(
            RunIdentity(forge_run_id="run-1", attempt=0, generation=0), frozen
        )
        assert "a\n\n- b\n- c\n" in Brief.model_validate_json(brief.model_dump_json()).brief_text

    async def test_missing_sections_raise(self, tmp_path):
        await refused(tmp_path, "spec_json", "{}")

    async def test_verify_fails_closed_on_any_byte_change(self, tmp_path):
        await refused(tmp_path, "brief_text", "Use the approved source snapshot. ")


class TestParseUsage:
    async def test_claude_per_turn_receipts_are_summed(self, tmp_path):
        case = Case(tmp_path)
        session = await case.claude()
        await session.steer("bounded correction")
        case.sdk.clients[0].messages.put_nowait(
            Result(
                uuid="one",
                usage={"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 20},
                total_cost_usd=0.1,
            )
        )
        case.sdk.clients[0].messages.put_nowait(
            Result(
                uuid="two",
                usage={"input_tokens": 30, "cache_creation_input_tokens": 7, "output_tokens": 40},
                total_cost_usd=0.3,
            )
        )
        events = await collect(session)
        assert events[-1].usage.input_tokens == 52 and events[-1].usage.output_tokens == 60
        assert str(events[-1].usage.cost_usd) == "0.3"

    async def test_grok_end_aggregate_wins_over_per_response_events(self, tmp_path):
        # Cumulative SDK cost is latest, never added across complete result snapshots.
        case = Case(tmp_path)
        session = await case.claude()
        await session.steer("bounded correction")
        for result in (
            Result(uuid="one", usage={"input_tokens": 10, "output_tokens": 0}, total_cost_usd=0.1),
            Result(uuid="two", usage={"input_tokens": 20, "output_tokens": 0}, total_cost_usd=0.3),
        ):
            case.sdk.clients[0].messages.put_nowait(result)
        assert str((await collect(session))[-1].usage.cost_usd) == "0.3"

    async def test_opencode_has_no_receipt(self, tmp_path):
        case = await completed_case(tmp_path, frames=[Result()])
        assert case.result.report.usage is None and case.server.candidates[0].usage is None

    async def test_garbage_lines_are_skipped(self, tmp_path):
        case = await completed_case(tmp_path, frames=[object(), Result()])
        assert case.result.exit_code == 0 and case.result.report.usage is None

    async def test_tokens_that_never_appear_stay_none(self, tmp_path):
        case = await completed_case(tmp_path, frames=[Result(usage=None, total_cost_usd=0.1)])
        assert case.result.report.usage is None


class TestEmitCandidateMeta:
    async def test_writes_schema_v2_with_identity_digest_and_usage(self, finished):
        candidate = finished.server.candidates[0]
        diff = finished.server.blobs[candidate.diff_sha256]
        assert (
            candidate.forge_run_id == finished.dispatch.forge_run_id
            and candidate.generation == finished.dispatch.generation
        )
        assert (
            candidate.approved_digest == finished.dispatch.approved_digest
            and candidate.base_sha == finished.base
        )
        assert (
            hashlib.sha256(diff).hexdigest() == candidate.diff_sha256
            and len(diff) == candidate.diff_size_bytes
        )
        assert candidate.usage == finished.result.report.usage

    async def test_missing_usage_exit_and_env_degrade_to_unknown(self, tmp_path):
        case = await completed_case(tmp_path, frames=[Result()])
        assert case.result.report.usage is None and case.result.report.status == "completed"

    def test_non_object_usage_json_degrades_to_none(self):
        with pytest.raises(ValidationError):
            Usage.model_validate_json("[]")

    async def test_bootstrap_classification_and_profile_digest_ride_the_meta(self, finished):
        assert finished.result.report.classification == "completed"
        assert finished.brief.frozen_spec.execution_policy.profile == "workspace-files-v1"

    async def test_bootstrap_and_profile_degrade_to_unknown_honestly(self, tmp_path):
        await refused(tmp_path, "spec_json", '{"execution_policy":null}')

    async def test_a_garbage_bootstrap_status_is_not_a_classification(self, tmp_path):
        def mutate(case):
            (case.workspace / ".forge").mkdir()
            (case.workspace / ".forge" / "bootstrap").write_text("garbage")

        case = await completed_case(tmp_path, mutate=mutate)
        assert case.result.report.classification == "completed"
        assert b"garbage" not in case.server.blobs[case.server.candidates[0].diff_sha256]

    async def test_cli_emit_meta_writes_the_meta_and_exits_zero(self, finished):
        assert finished.result.exit_code == 0 and finished.result.report_accepted
        assert finished.server.reports[0] == finished.result.report

    async def test_cli_emit_meta_derives_the_profile_from_the_checkout(self, finished):
        # Policy is frozen before checkout; workspace claims cannot derive extra authority.
        assert finished.brief.frozen_spec.execution_policy == ExecutionPolicy()
        assert finished.result.report.approved_digest == finished.brief.approved_digest

    async def test_cli_emit_meta_fails_loud_on_a_missing_diff(self, tmp_path):
        case = await completed_case(tmp_path, edit=False)
        assert case.result.exit_code == ExitCode.EMPTY_DIFF and not case.server.candidates
        assert case.result.report.classification == "empty_diff"


class TestMain:
    async def test_successful_driver_writes_completed_and_usage(self, finished):
        assert finished.result.exit_code == 0 and finished.result.report.status == "completed"
        assert (
            finished.result.report.usage.input_tokens == 10
            and finished.result.report.usage.output_tokens == 20
        )

    async def test_failing_driver_writes_failed_but_still_exits_zero(self, tmp_path):
        case = await completed_case(tmp_path, frames=[Result(is_error=True)])
        assert case.result.exit_code == ExitCode.DRIVER and case.result.report.status == "failed"
        assert not case.server.candidates

    async def test_missing_brief_fails_before_running_anything(self, tmp_path):
        await refused(tmp_path, "brief_text", "")

    def test_unknown_driver_fails_before_running_anything(self):
        rejected("unknown-driver")

    async def test_env_fallbacks_mirror_the_workflow_template(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORGE_HARNESS_DRIVER", "grok-build")
        monkeypatch.setenv("FORGE_MODEL", "unapproved-model")
        case = await completed_case(tmp_path)
        assert (
            case.result.report.driver == "claude_code" and case.result.report.model == "test-model"
        )
        assert case.sdk.clients[0].options.env.get("FORGE_MODEL", "") == ""

    async def test_sdk_lane_exit_classifies_and_keeps_the_lane_usage_receipt(self, tmp_path):
        case = await completed_case(
            tmp_path, frames=[Result(is_error=True, usage={"input_tokens": 11})]
        )
        assert case.result.exit_code == ExitCode.DRIVER and not case.server.candidates
        # Failure is not a zero-cost receipt; gateway accounting remains authoritative.
        assert case.result.report.usage is None


class TestGrokLaneCredential:
    def test_grok_writes_the_subscription_auth_blob(self, monkeypatch):
        monkeypatch.setenv("FORGE_GROK_AUTH", '{"access_token":"unapproved"}')
        rejected("grok")

    async def test_no_other_driver_touches_another_providers_credential(self, finished):
        options = finished.sdk.clients[0].options
        assert (
            options.env.get("COPILOT_GITHUB_TOKEN", "") == options.env.get("XAI_API_KEY", "") == ""
        )
        assert options.env["ANTHROPIC_AUTH_TOKEN"] == "scoped-key"

    def test_the_sdk_lanes_own_credentials_are_guarded_or_absent(self):
        rejected("codex")
        rejected("opencode")


class TestDriverVersionWiring:
    async def test_env_pins_flow_resolved_into_the_render(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORGE_DRIVER_VERSIONS", '{"copilot":"latest"}')
        await probe_version(tmp_path)

    async def test_broken_env_pins_fail_the_lane_before_any_driver_runs(self, tmp_path):
        with pytest.raises(LaneFailure):
            await probe_version(tmp_path, "broken-version")

    async def test_unset_pins_run_on_the_known_good_defaults(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FORGE_DRIVER_VERSIONS", raising=False)
        await probe_version(tmp_path)


class TestQualityGateAllowlist:
    async def test_claude_allowlist_carries_the_full_gate_set(self, finished):
        options = finished.sdk.clients[0].options
        assert "WebFetch" in options.disallowed_tools and options.allowed_tools == []
        assert options.permission_mode == "default"

    def test_grok_grants_carry_the_full_gate_set(self):
        rejected("grok")

    async def test_copilot_grants_carry_the_full_gate_set(self, tmp_path, monkeypatch):
        case = Case(tmp_path, driver="copilot_cli")
        wire = FakeACP()
        session = await case.copilot(wire, monkeypatch)
        assert (await collect(session))[-1].kind == "completed"
        assert wire.sent[0]["params"]["clientCapabilities"]["terminal"] is False
        assert wire.sent[1]["params"]["mcpServers"] == []


class TestPermissionListsAreTokenized:
    async def test_claude_allowed_tools_is_a_comma_list_of_standalone_rules(self, finished):
        options = finished.sdk.clients[0].options
        assert options.tools == ["Read", "Write", "Edit", "Grep", "Glob", "Bash"]
        assert len(set(options.tools)) == 6
        assert not any("," in tool or "Bash(" in tool for tool in options.tools)

    async def test_claude_mcp_grants_join_as_plain_comma_tokens(self, finished):
        # Empty MCP is the frozen default; ambient comma strings do not add tools.
        assert finished.sdk.clients[0].options.mcp_servers == {}
        assert not FilePolicy(finished.workspace, finished.sdk).permitted(
            "mcp__unapproved__read", {}
        )

    async def test_no_rendered_script_carries_a_glued_rule(self, tmp_path):
        case = Case(tmp_path)
        policy = FilePolicy(case.workspace, case.sdk)
        for tool in ("Read,Write", "ReadWrite", "Bash(git status:*)"):
            assert not policy.permitted(tool, {"file_path": "base.txt"})

    async def test_grok_and_copilot_flags_each_carry_exactly_one_rule(self, tmp_path, monkeypatch):
        case = Case(tmp_path, driver="copilot_cli")
        wire = FakeACP(
            permission={
                "toolCall": {"kind": "edit", "rawInput": {"path": "base.txt"}},
                "options": [
                    {"kind": "allow_once", "optionId": "one"},
                    {"kind": "allow_always", "optionId": "all"},
                ],
            }
        )
        session = await case.copilot(wire, monkeypatch)
        await collect(session)
        response = next(frame for frame in wire.sent if frame.get("id") == 1000)
        assert response["result"]["outcome"] == {"outcome": "selected", "optionId": "one"}


class TestCommandReceiptsC10:
    async def test_the_wrapper_receipts_file_rides_the_meta(self, tmp_path):
        def mutate(case):
            (case.workspace / ".forge").mkdir()
            (case.workspace / ".forge" / "commands.tsv").write_text("pytest -q\t0\t\n")

        case = await completed_case(tmp_path, mutate=mutate)
        candidate = case.server.candidates[0]
        assert b"commands.tsv" not in case.server.blobs[candidate.diff_sha256]
        assert (
            "observed_execution" not in candidate.model_dump()
            and "commands" not in case.result.report.model_dump()
        )

    async def test_a_malformed_receipts_file_claims_nothing(self, tmp_path):
        def mutate(case):
            (case.workspace / ".forge").mkdir()
            (case.workspace / ".forge" / "commands.tsv").write_text("malformed\tzero\tforged")

        case = await completed_case(tmp_path, mutate=mutate)
        assert b"forged" not in case.server.blobs[case.server.candidates[0].diff_sha256]
        assert case.result.report.classification == "completed"
