"""Retained runner lessons on the sole Claude/Copilot authenticated lane.

Historical class/method names preserve registered evidence. Unsupported driver
chains, templates, ambient routes, local meta journals and restoration flags
are retired; frozen protocol identity and durable control receipts own execution.
"""

import argparse
from hashlib import sha256
import importlib.metadata
import json
import sys

import pytest
from pydantic import ValidationError

from forge_lane import cli
from forge_lane.errors import ExitCode, LaneFailure
from forge_lane.workspace import Workspace
from forge_protocol import LaneDispatch
from tests.native_driver_fixture import (
    Case,
    Result,
    completed_case,
    copilot_case,
    restored_case,
)


@pytest.fixture(params=["github", "gitlab"])
async def finished(tmp_path, request):
    return await completed_case(tmp_path, provider=request.param, nonce="proof-nonce")


def rejects(name):
    with pytest.raises(LaneFailure) as failure:
        cli.load_driver(name)
    assert failure.value.classification == "driver_unavailable"


async def unsupported(root, name):
    case = Case(root)
    path = root / "dispatch.json"
    path.write_text(case.dispatch.model_copy(update={"driver": name}).model_dump_json())
    result = await cli.run(argparse.Namespace(dispatch=path, workspace=case.workspace, check=False))
    assert result == ExitCode.CONFIGURATION and not case.sdk.clients and not case.server.calls
    return case


async def invalid_brief(root, field, value):
    case = await completed_case(root, mutate=lambda case: case.server.brief.update({field: value}))
    assert case.result.exit_code in (ExitCode.APPROVAL, ExitCode.TRANSPORT)
    assert not case.sdk.clients and case.server.model_keys == 0 and not case.server.candidates
    return case


class TestBuildTask:
    async def test_task_carries_the_brief_the_issue_and_the_no_commit_posture(self, finished):
        prompt = finished.sdk.clients[0].prompts[0]
        assert (
            finished.brief.brief_text in prompt
            and "repository writes and authority files are denied" in prompt
        )
        assert finished.result.report.forge_run_id == finished.dispatch.forge_run_id

    async def test_issue_reference_is_omitted_when_not_dispatched(self, finished):
        assert "issue #" not in finished.sdk.clients[0].prompts[0]
        assert finished.server.candidates[0].approved_digest == finished.brief.approved_digest


class TestClassifyResult:
    async def test_completed_on_the_live_verified_terminal_reason(self, finished):
        assert (
            finished.result.exit_code == 0 and finished.result.report.classification == "completed"
        )

    async def test_missing_terminal_reason_on_a_successful_turn_stays_completed(self, tmp_path):
        case = await completed_case(tmp_path, frames=[Result(terminal_reason=None)])
        assert case.result.exit_code == 0 and case.server.candidates

    async def test_aborted_terminal_reason_classifies_failed_with_the_reason(self, tmp_path):
        case = await completed_case(tmp_path, frames=[Result(terminal_reason="aborted_tools")])
        assert (
            case.result.report.classification == "driver_result_incomplete"
            and not case.server.candidates
        )

    async def test_error_markers_beat_a_completed_terminal_reason(self, tmp_path):
        case = await completed_case(
            tmp_path, frames=[Result(is_error=True, terminal_reason="completed")]
        )
        assert (
            case.result.report.classification == "driver_result_error"
            and not case.server.candidates
        )


class TestUsageReceipt:
    async def test_vendor_counters_map_onto_the_meta_usage_shape(self, tmp_path):
        case = await completed_case(
            tmp_path,
            frames=[
                Result(
                    usage={
                        "input_tokens": 120,
                        "cache_read_input_tokens": 40,
                        "cache_creation_input_tokens": 10,
                        "output_tokens": 80,
                    },
                    total_cost_usd=0.0947,
                )
            ],
        )
        usage = case.result.report.usage
        assert (
            usage.input_tokens == 170
            and usage.output_tokens == 80
            and str(usage.cost_usd) == "0.0947"
        )
        assert case.server.candidates[0].usage == usage

    async def test_unknown_stays_unknown_never_zero(self, tmp_path):
        case = await completed_case(tmp_path, frames=[Result()])
        assert case.result.report.usage is None and case.server.candidates[0].usage is None

    async def test_malformed_counters_are_dropped_not_clamped(self, tmp_path):
        case = await completed_case(
            tmp_path, frames=[Result(usage={"input_tokens": -1, "output_tokens": 1})]
        )
        assert case.result.report.classification == "driver_usage_invalid"
        assert case.result.report.usage is None and not case.server.candidates


class TestMainCompleted:
    async def test_completed_turn_writes_the_batch_contract_and_exits_zero(self, finished):
        candidate = finished.server.candidates[0]
        assert finished.result.exit_code == 0 and finished.result.report_accepted
        assert candidate.base_sha == finished.base and candidate.driver == "claude_code"
        assert candidate.approved_digest == finished.dispatch.approved_digest

    async def test_the_brief_becomes_the_one_task(self, finished):
        assert len(finished.sdk.clients[0].prompts) == 1
        assert finished.brief.brief_text in finished.sdk.clients[0].prompts[0]

    async def test_the_lane_adds_no_capability_beyond_the_driver(self, finished):
        options = finished.sdk.clients[0].options
        assert options.tools == ["Read", "Write", "Edit"] and "Bash" in options.disallowed_tools
        assert options.env.get("GITHUB_TOKEN", "") == "" and options.setting_sources == []

    async def test_turn_without_usage_leaves_the_receipt_unknown(self, tmp_path):
        case = await completed_case(tmp_path, frames=[Result()])
        assert case.result.exit_code == 0 and case.result.report.usage is None


class TestMainFailures:
    async def test_aborted_turn_is_nonzero_with_the_reason_in_meta(self, tmp_path):
        case = await completed_case(tmp_path, frames=[Result(terminal_reason="aborted_streaming")])
        assert case.result.exit_code == ExitCode.DRIVER and case.result.report.status == "failed"
        assert case.result.report.classification == "driver_result_incomplete"

    async def test_error_subtype_is_nonzero_even_with_a_completed_terminal_reason(self, tmp_path):
        case = await completed_case(tmp_path, frames=[Result(subtype="error_during_execution")])
        assert (
            case.result.report.classification == "driver_result_error"
            and not case.server.candidates
        )

    async def test_missing_brief_fails_closed_but_still_writes_the_meta(self, tmp_path):
        case = await invalid_brief(tmp_path, "spec_json", None)
        assert case.result.report_accepted and case.result.report.status == "failed"

    async def test_missing_sdk_fails_with_the_actionable_message(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
        case = await completed_case(tmp_path, mutate=lambda case: setattr(case, "sdk", None))
        assert case.result.report.classification == "driver_unavailable"
        assert (
            "forge-lane[claude]" in case.result.report.terminal_reason
            and not case.server.candidates
        )

    async def test_malformed_budget_env_fails_closed(self, tmp_path):
        case = Case(tmp_path)
        document = case.dispatch.model_dump()
        document["timeout_seconds"] = "not-a-budget"
        with pytest.raises(ValidationError):
            LaneDispatch.model_validate(document)
        assert not case.sdk.clients and case.server.model_keys == 0


class TestBudgetBounded:
    async def test_budget_expiry_interrupts_and_records_budget_exceeded(self, tmp_path):
        case = await completed_case(tmp_path, frames=[], timeout_seconds=1)
        assert (
            case.result.exit_code == ExitCode.DEADLINE and case.result.report.status == "timed_out"
        )
        assert case.sdk.clients[0].disconnected and not case.server.candidates

    async def test_the_aborted_result_within_grace_wins_over_budget_exceeded(self, tmp_path):
        # An explicit aborted result before deadline is a failed turn, never deadline success.
        case = await completed_case(
            tmp_path, frames=[Result(terminal_reason="aborted_streaming")], timeout_seconds=1
        )
        assert (
            case.result.exit_code == ExitCode.DRIVER
            and case.result.report.classification == "driver_result_incomplete"
        )


class TestCodexLane:
    async def test_completed_turn_writes_the_lane_contract_and_exits_zero(self, tmp_path):
        await unsupported(tmp_path, "codex")

    async def test_the_brief_becomes_the_one_task(self, tmp_path):
        await unsupported(tmp_path, "codex-sdk-lane")

    async def test_the_env_dispatch_selects_the_lane_without_a_flag(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORGE_LANE_ENABLE_CONTRIB", "1")
        await unsupported(tmp_path, "codex")

    async def test_failed_turn_is_nonzero_with_the_vendor_status_and_error(self, tmp_path):
        await unsupported(tmp_path, "codex-app-server")

    async def test_a_turn_without_usage_leaves_the_receipt_unknown(self, tmp_path):
        case = await unsupported(tmp_path, "codex")
        assert not case.server.candidates and not case.server.reports

    async def test_budget_expiry_interrupts_and_honors_the_interrupted_verdict(self, tmp_path):
        await unsupported(tmp_path, "codex-lane")

    async def test_a_silent_interrupt_ends_as_budget_exceeded(self, tmp_path):
        await unsupported(tmp_path, "codex-sdk")

    async def test_another_threads_completion_is_never_taken(self, tmp_path):
        case = await completed_case(
            tmp_path, frames=[Result(origin={"kind": "task-notification"}), Result(uuid="own")]
        )
        assert case.result.exit_code == 0 and case.result.report.usage is None
        assert len(case.server.candidates) == 1


class TestCodexClassification:
    async def test_completed_requires_the_completed_status(self, tmp_path):
        case = await completed_case(tmp_path, frames=[Result(subtype="")])
        assert (
            case.result.report.classification == "driver_result_error"
            and not case.server.candidates
        )

    async def test_interrupted_and_failed_carry_the_vendor_status(self, tmp_path):
        case = await completed_case(tmp_path, frames=[Result(is_error=True, subtype="error")])
        assert case.result.report.status == "failed" and case.result.exit_code == ExitCode.DRIVER

    async def test_a_statusless_frame_never_guesses_success(self, tmp_path):
        case = await completed_case(tmp_path, frames=[None])
        assert (
            case.result.report.classification == "driver_stream_ended"
            and not case.server.candidates
        )

    async def test_usage_receipt_tolerates_both_vendor_spellings(self, tmp_path):
        case = await completed_case(
            tmp_path, frames=[Result(usage={"inputTokens": 1, "outputTokens": 2})]
        )
        assert (
            case.result.report.classification == "driver_usage_invalid"
            and not case.server.candidates
        )

    async def test_usage_receipt_stays_none_without_the_event(self, tmp_path):
        case = await completed_case(tmp_path, frames=[Result()])
        assert case.result.report.usage is None


class TestCopilotLane:
    async def test_completed_turn_writes_the_lane_contract_and_exits_zero(
        self, tmp_path, monkeypatch
    ):
        case = await copilot_case(tmp_path, monkeypatch)
        assert case.result.exit_code == 0 and case.result.report.driver == "copilot_cli"
        assert case.result.report.usage is None and case.server.model_keys == 0 and case.wire.closed

    async def test_the_brief_becomes_the_one_task(self, tmp_path, monkeypatch):
        case = await copilot_case(tmp_path, monkeypatch)
        prompt = next(frame for frame in case.wire.sent if frame.get("method") == "session/prompt")
        assert case.brief.brief_text in prompt["params"]["prompt"][0]["text"]
        assert prompt["params"]["sessionId"] == "session-1"

    async def test_the_env_dispatch_selects_the_lane_without_a_flag(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORGE_HARNESS_DRIVER", "grok-build")
        case = await copilot_case(tmp_path, monkeypatch)
        assert case.result.report.driver == case.dispatch.driver == "copilot_cli"

    async def test_budget_expiry_reclassifies_the_4561_lie(self, tmp_path, monkeypatch):
        case = await copilot_case(tmp_path, monkeypatch, reason=None, timeout_seconds=1)
        assert (
            case.result.exit_code == ExitCode.DEADLINE
            and case.wire.closed
            and not case.server.candidates
        )

    async def test_a_truthful_cancelled_stop_reason_is_honored(self, tmp_path, monkeypatch):
        case = await copilot_case(tmp_path, monkeypatch, reason="cancelled")
        assert (
            case.result.report.classification == "driver_cancelled" and not case.server.candidates
        )

    async def test_a_silent_interrupt_ends_as_budget_exceeded(self, tmp_path, monkeypatch):
        case = await copilot_case(
            tmp_path, monkeypatch, reason=None, controls=(("interrupt", None),)
        )
        assert case.result.exit_code == ExitCode.CANCELLED and not case.server.candidates
        assert case.server.acks[0]["status"] == "applied"

    async def test_a_prompt_error_record_carries_its_detail(self, tmp_path, monkeypatch):
        case = await copilot_case(tmp_path, monkeypatch, reason="rpc_error")
        assert case.result.report.classification == "driver_rpc_error" and case.wire.closed
        assert "vendor detail" not in case.result.report.terminal_reason

    async def test_drive_copilot_lane_records_the_episode_phases(self, tmp_path, monkeypatch):
        case = await copilot_case(tmp_path, monkeypatch)
        assert case.server.heartbeats and case.result.report_accepted and case.wire.closed
        assert "episode" not in case.result.report.model_dump()


class TestCopilotClassification:
    async def test_only_the_completed_outcome_exits_clean(self, tmp_path, monkeypatch):
        case = await copilot_case(tmp_path, monkeypatch, reason="refusal")
        assert case.result.report.classification == "driver_refused" and not case.server.candidates

    @pytest.mark.parametrize(
        "reason,classification",
        [("max_tokens", "driver_token_limit"), ("max_turns", "driver_turn_limit")],
    )
    async def test_the_ledger_outcome_and_vendor_spellings_carry_through(
        self, tmp_path, monkeypatch, reason, classification
    ):
        case = await copilot_case(tmp_path, monkeypatch, reason=reason)
        assert (
            case.result.report.classification == classification
            and case.result.exit_code == ExitCode.DRIVER
        )

    async def test_a_recordless_turn_never_guesses_success(self, tmp_path, monkeypatch):
        case = await copilot_case(tmp_path, monkeypatch, reason="eof")
        assert (
            case.result.report.classification == "driver_process_error"
            and not case.server.candidates
        )

    async def test_usage_stays_none_without_a_usage_update(self, tmp_path, monkeypatch):
        case = await copilot_case(tmp_path, monkeypatch)
        assert case.result.report.usage is None

    async def test_a_future_usage_update_rides_verbatim_never_as_tokens(
        self, tmp_path, monkeypatch
    ):
        case = await copilot_case(tmp_path, monkeypatch)
        assert case.result.report.usage is None and case.server.candidates[0].usage is None
        assert case.server.model_keys == 0


class TestOpenCodeLane:
    async def test_succeeded_turn_writes_the_lane_contract_and_exits_zero(self, tmp_path):
        await unsupported(tmp_path, "opencode")

    async def test_the_brief_becomes_the_one_task(self, tmp_path):
        await unsupported(tmp_path, "opencode-sdk-lane")

    async def test_the_env_dispatch_selects_the_lane_without_a_flag(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORGE_LANE_ENABLE_CONTRIB", "1")
        await unsupported(tmp_path, "opencode")

    async def test_failed_turn_is_nonzero_with_the_vendor_verdict(self, tmp_path):
        await unsupported(tmp_path, "opencode-server")

    async def test_usage_stays_unknown_without_the_usage_event(self, tmp_path):
        case = await unsupported(tmp_path, "opencode")
        assert not case.server.model_keys

    async def test_budget_timeout_classifies_budget_exceeded_and_tears_down(self, tmp_path):
        await unsupported(tmp_path, "opencode-sdk")

    async def test_the_client_env_pins_the_server_and_the_lane_posture(self, tmp_path):
        await unsupported(tmp_path, "zai-opencode")

    async def test_an_explicit_permission_response_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCODE_PERMISSION_RESPONSE", "once")
        await unsupported(tmp_path, "opencode")

    async def test_the_transcript_fallback_classifies_when_the_stream_was_uncovered(self, tmp_path):
        await unsupported(tmp_path, "opencode-transcript")


class TestOpenCodeClassification:
    async def test_the_last_verdict_for_the_session_wins(self, tmp_path, monkeypatch):
        case = await copilot_case(tmp_path, monkeypatch, reason="unknown-future")
        assert (
            case.result.report.classification == "driver_protocol_error"
            and not case.server.candidates
        )

    async def test_an_error_finish_in_the_transcript_classifies_failed(self, tmp_path):
        case = await completed_case(tmp_path, frames=[Result(is_error=True)])
        assert case.result.report.status == "failed" and not case.server.candidates

    async def test_nothing_observable_stays_unobserved(self, tmp_path):
        case = await completed_case(tmp_path, frames=[None])
        assert case.result.report.classification == "driver_stream_ended"

    async def test_usage_receipt_reads_nested_token_shapes_and_stays_unknown(self, tmp_path):
        case = await completed_case(
            tmp_path, frames=[Result(usage={"tokens": {"input": 8, "output": 9}})]
        )
        assert (
            case.result.report.classification == "driver_usage_invalid"
            and case.result.report.usage is None
        )


class TestLaneRegistration:
    def test_the_lane_id_is_a_shipped_driver_with_the_claude_code_recipe(self):
        driver = cli.load_driver("claude_code")
        assert driver.name == "claude_code" and driver.requires_model_key

    def test_the_lane_driver_constant_matches_the_registered_id(self):
        assert cli.load_driver("copilot_cli").name == "copilot_cli"

    def test_the_codex_and_opencode_lane_constants_are_their_registered_ids(self):
        for name in ("codex", "opencode", "grok"):
            rejects(name)

    async def test_an_unknown_driver_fails_closed_but_still_writes_the_meta(self, tmp_path, capsys):
        await unsupported(tmp_path, "warp")
        assert json.loads(capsys.readouterr().out)["classification"] == "driver_unavailable"


class TestLaneTemplate:
    async def test_rules_filter_on_the_run_id_and_the_lane_driver(self, tmp_path):
        await invalid_brief(tmp_path, "forge_run_id", "foreign")

    async def test_the_brief_is_written_into_the_repo_never_tmp(self, finished):
        assert finished.sdk.clients[0].options.cwd == str(finished.workspace)
        assert finished.brief.brief_text in finished.sdk.clients[0].prompts[0]

    def test_the_lane_installs_forge_interactive_from_a_pinnable_ref(self):
        assert importlib.metadata.version("claude-agent-sdk") == "0.2.157"

    async def test_the_candidate_contract_is_byte_compatible_with_the_batch_lane(self, finished):
        candidate = finished.server.candidates[0]
        diff = finished.server.blobs[candidate.diff_sha256]
        assert (
            sha256(diff).hexdigest() == candidate.diff_sha256
            and len(diff) == candidate.diff_size_bytes
        )
        assert b"diff --git a/base.txt b/base.txt" in diff

    async def test_a_driver_failure_fails_the_job_but_uploads_artifacts(self, tmp_path):
        case = await completed_case(tmp_path, frames=[Result(is_error=True)])
        assert (
            case.result.exit_code != 0
            and case.result.report_accepted
            and not case.server.candidates
        )


class TestSdkLaneTemplates:
    async def test_rules_filter_on_the_run_id_and_the_lane_driver(self, tmp_path):
        await invalid_brief(tmp_path, "generation", 999)

    def test_the_template_registers_the_shipped_lane_id(self):
        assert (
            cli.load_driver("claude_code").name == "claude_code"
            and cli.load_driver("copilot_cli").name == "copilot_cli"
        )

    async def test_the_brief_is_written_into_the_repo_never_tmp(self, finished):
        assert finished.sdk.clients[0].options.cwd == str(finished.workspace)
        assert not (finished.workspace / ".forge" / "brief.md").exists()

    def test_the_lane_installs_forge_interactive_and_runs_the_lane_driver(self):
        distribution = importlib.metadata.distribution("forge-lane")
        assert distribution.metadata["Name"] == "forge-lane"
        assert any(
            entry.name == "forge-lane" and entry.value == "forge_lane.cli:main"
            for entry in distribution.entry_points
        )

    async def test_the_cli_installs_over_npm_with_retries(self, tmp_path):
        await unsupported(tmp_path, "claude-sdk-lane")

    async def test_the_candidate_contract_is_byte_compatible_with_the_batch_lane(self, finished):
        assert (
            finished.result.report.candidate_diff_sha256
            == finished.server.candidates[0].diff_sha256
        )
        assert finished.server.candidates[0].generation == finished.dispatch.generation

    async def test_a_driver_failure_fails_the_job_but_uploads_artifacts(self, tmp_path):
        case = await completed_case(tmp_path, frames=[None])
        assert (
            case.result.exit_code == ExitCode.DRIVER
            and case.server.reports
            and not case.server.candidates
        )

    async def test_the_agent_is_told_not_to_commit_or_push(self, finished):
        options = finished.sdk.clients[0].options
        assert "Bash" in options.disallowed_tools
        assert (
            "repository writes and authority files are denied" in finished.sdk.clients[0].prompts[0]
        )


class TestCodexLaneTemplateDetails:
    def test_the_cli_is_openai_codex_over_npm(self):
        rejects("codex")

    def test_the_optional_provider_native_auth_lands_owner_only(self, monkeypatch):
        monkeypatch.setenv("FORGE_CODEX_AUTH", "unapproved")
        rejects("codex")

    def test_the_model_route_and_cwd_exports(self):
        rejects("codex-sdk-lane")


class TestOpenCodeLaneTemplateDetails:
    def test_the_cli_installs_via_the_official_installer(self):
        rejects("opencode")

    def test_the_mechanical_deny_rides_the_serve_config(self):
        rejects("opencode-server")

    def test_permission_prompts_are_answered_once(self):
        rejects("opencode-sdk-lane")

    def test_the_zai_provider_route_is_configured(self):
        rejects("zai-opencode")

    def test_the_lane_spawns_its_own_server_no_manual_serve(self):
        rejects("opencode-serve")


class TestCopilotLaneTemplateDetails:
    async def test_the_cli_is_github_copilot_over_npm_at_the_forge_pin(self, tmp_path):
        from tests.test_harness_entry import probe_version

        await probe_version(tmp_path)

    async def test_the_documented_credential_rides_the_ambient_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "must-not-authorize")
        case = await copilot_case(tmp_path, monkeypatch)
        assert case.received[0].token == "synthetic-copilot-only" and case.server.model_keys == 0

    async def test_the_model_route_and_cwd_exports(self, tmp_path, monkeypatch):
        case = await copilot_case(tmp_path, monkeypatch)
        assert (
            case.received[0].model == case.dispatch.model and case.received[0].cwd == case.workspace
        )


class TestSteeringGate:
    async def test_the_flag_defaults_off_and_parses_only_truthy_spellings(self, tmp_path):
        await invalid_brief(tmp_path, "steering_enabled", True)

    async def test_main_without_the_flag_writes_no_steering_journal(self, finished):
        assert (
            not finished.server.acks
            and not (finished.workspace / ".forge" / "steering.json").exists()
        )

    async def test_main_with_the_flag_carries_the_honest_empty_journal(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORGE_LANE_STEERING", "1")
        case = await completed_case(tmp_path)
        assert not case.server.acks

    async def test_the_flag_without_any_lane_identity_stays_detached(self, tmp_path):
        await invalid_brief(tmp_path, "forge_run_id", "")


class TestClaudeLaneSteering:
    async def test_the_lane_binds_attaches_and_journals_a_mid_turn_steer(self, tmp_path):
        def mutate(case):
            case.server.command("steer", 1, "Use approved approach")
            case.server.command("pause", 2)

        case = await completed_case(tmp_path, frames=[], mutate=mutate)
        assert case.result.exit_code == ExitCode.PAUSED
        assert [ack["status"] for ack in case.server.acks] == ["applied", "applied"]
        assert case.sdk.clients[0].prompts[1].endswith("Use approved approach")
        assert case.server.checkpoints and not case.server.candidates

    async def test_without_control_the_lane_runs_the_old_path(self, finished):
        assert not finished.server.acks and len(finished.sdk.clients[0].prompts) == 1


class TestCodexLaneSteering:
    async def test_the_thread_binds_at_acceptance_and_steers_the_active_turn(self, tmp_path):
        await unsupported(tmp_path, "codex")

    async def test_without_control_the_codex_lane_runs_the_old_path(self, tmp_path):
        await unsupported(tmp_path, "codex-sdk-lane")


class TestOpenCodeLaneSteering:
    async def test_the_session_binds_at_completion_and_journals_the_refusal(
        self, tmp_path, monkeypatch
    ):
        case = await copilot_case(
            tmp_path, monkeypatch, reason=None, controls=(("steer", "bounded"), ("interrupt", None))
        )
        assert case.result.exit_code == ExitCode.CANCELLED
        assert [ack["status"] for ack in case.server.acks] == ["refused", "applied"]

    async def test_without_control_the_opencode_lane_runs_the_old_path(self, tmp_path):
        await unsupported(tmp_path, "opencode")


class TestEpisodeTiming:
    async def test_the_outcomes_episode_keys_are_canonically_ordered(self, finished):
        assert finished.server.heartbeats[0]["phase"] == "running"
        assert (
            finished.result.report.classification == "completed"
            and "episode" not in finished.result.report.model_dump()
        )

    async def test_completed_claude_turn_carries_all_four_phase_keys(self, finished):
        assert (
            finished.result.report_accepted
            and finished.server.heartbeats
            and finished.sdk.clients[0].disconnected
        )

    async def test_failed_claude_turn_still_carries_the_breakdown(self, tmp_path):
        case = await completed_case(tmp_path, frames=[Result(is_error=True)])
        assert (
            case.result.report_accepted
            and case.result.report.status == "failed"
            and case.sdk.clients[0].disconnected
        )

    async def test_budget_exceeded_claude_turn_measures_the_interrupt_grace(self, tmp_path):
        case = await completed_case(tmp_path, frames=[], timeout_seconds=1)
        assert case.result.exit_code == ExitCode.DEADLINE and case.sdk.clients[0].disconnected

    async def test_a_failure_before_the_episode_carries_no_episode_key(self, tmp_path):
        case = await invalid_brief(tmp_path, "spec_json", "{}")
        assert not case.sdk.clients and not case.server.heartbeats

    async def test_completed_codex_turn_carries_the_breakdown(self, tmp_path):
        await unsupported(tmp_path, "codex")

    async def test_budget_exceeded_codex_turn_measures_the_interrupt_grace(self, tmp_path):
        await unsupported(tmp_path, "codex-sdk-lane")

    async def test_completed_opencode_turn_carries_the_breakdown(self, tmp_path):
        await unsupported(tmp_path, "opencode")

    async def test_budget_exceeded_opencode_turn_still_measures_start_and_turn(self, tmp_path):
        await unsupported(tmp_path, "opencode-sdk-lane")


class TestSdkLaneVersionPins:
    def test_claude_pin_defaults_to_the_live_verified_version(self):
        assert importlib.metadata.version("claude-agent-sdk") == "0.2.157"

    def test_codex_pin_defaults_to_the_live_verified_version(self):
        rejects("codex")

    def test_opencode_pin_defaults_to_the_live_verified_version(self):
        rejects("opencode")

    async def test_no_sdk_lane_template_ships_a_floating_latest_install(self, tmp_path):
        from tests.test_harness_entry import probe_version

        with pytest.raises(LaneFailure):
            await probe_version(tmp_path, "latest")


class TestSdkLaneProfileBlock:
    async def test_the_profile_defaults_to_v1_with_the_egress_hook(self, finished):
        assert finished.brief.frozen_spec.execution_policy.profile == "workspace-files-v1"
        assert (
            finished.sdk.clients[0].options.strict_mcp_config
            and finished.sdk.clients[0].options.mcp_servers == {}
        )


class TestWipRestoreGate:
    async def test_the_resume_flag_parses_only_truthy_spellings(self, tmp_path):
        case = Case(tmp_path)
        document = case.dispatch.model_dump()
        document["checkpoint_id"] = "true"
        with pytest.raises(ValidationError):
            LaneDispatch.model_validate(document)
        assert case.server.model_keys == 0

    async def test_a_failed_required_restore_starts_zero_vendor_turns(self, tmp_path):
        pair = await restored_case(tmp_path, corrupt=True)
        assert pair.second.result.exit_code == ExitCode.CHECKPOINT
        assert not pair.second.sdk.clients and pair.second.server.model_keys == 0

    async def test_a_required_resume_without_any_channel_halts_before_the_driver(self, tmp_path):
        case = await completed_case(tmp_path, checkpoint_id="a" * 64)
        assert (
            case.result.exit_code == ExitCode.TRANSPORT
            and not case.sdk.clients
            and case.server.model_keys == 0
        )

    async def test_a_fresh_run_is_never_blocked_by_an_absent_checkpoint(self, finished):
        assert finished.result.exit_code == 0 and not finished.server.checkpoints

    async def test_a_successful_required_restore_precedes_the_turn(self, tmp_path):
        pair = await restored_case(tmp_path)
        assert pair.second.result.exit_code == 0 and pair.seen == ["approved change\n"]
        assert pair.second.server.candidates[0].approved_digest == pair.first.brief.approved_digest

    async def test_main_enters_the_restored_generation_before_the_vendor_client(self, tmp_path):
        pair = await restored_case(tmp_path)
        assert pair.second.server.candidates[0].generation == pair.second.dispatch.generation == 1
        assert pair.first.dispatch.generation == 0 and pair.second.sdk.clients[
            0
        ].options.cwd == str(pair.second.workspace)

    async def test_main_stays_in_the_checkout_when_no_generation_was_restored(self, tmp_path):
        pair = await restored_case(tmp_path, requested=False)
        assert pair.seen == ["base\n"] and pair.second.sdk.clients[0].options.cwd == str(
            pair.second.workspace
        )


class TestTrackedBaseline:
    async def test_the_baseline_stores_raw_content_sha256_not_git_oids(self, tmp_path):
        case = Case(tmp_path)
        raw = b"\x00\xffWIP"
        (case.workspace / "binary.bin").write_bytes(raw)
        entries, blobs = await Workspace(case.workspace, case.base).capture()
        entry = next(value for value in entries if value.path == "binary.bin")
        assert entry.sha256 == sha256(raw).hexdigest() and blobs[entry.sha256] == raw
        native_oid = (
            (await Workspace(case.workspace, case.base).git("hash-object", "binary.bin"))
            .decode()
            .strip()
        )
        assert entry.sha256 != native_oid

    async def test_an_unchanged_repository_captures_no_delta_with_the_store_inside(self, tmp_path):
        case = Case(tmp_path)
        (case.workspace / ".forge").mkdir()
        (case.workspace / ".forge" / "private").write_text("secret")
        entries, blobs = await Workspace(case.workspace, case.base).capture()
        assert entries == () and blobs == {}
