"""R38-16 (#317): the native template + secret-consumer conformance gate.

``scripts/gate_conformance.py`` is the release gate this module pins.
Like ``tests/test_pg_gate.py`` for the PG gate, these tests load the
script from its path and verify BOTH halves:

- the DECISION LOGIC (block extraction, the mutation transforms, the
  schema findings, the report/manifest shape, the CLI plumbing) — pure,
  fast, no bash;
- the EXECUTED ARMS themselves (real bash against the shipped recipes,
  the sentinel proofs, the mutation self-tests) — the same execution the
  CI lint step performs on every push.

The module ALSO owns the production dispatch-payload CAPTURE harness:
the REAL GitHub/GitLab/Azure run services driven through the REAL
production-entry fakes, fronted by DECLARING WRAPPER CLIENTS that refuse
a dispatch whose payload falls outside the shipped templates' declared
surface (the early guard — the sibling fakes' ledgers accept anything;
the real providers answer a dispatch-wide 422). The captured KEY SETS
are drift-pinned against ``scripts/conformance_dispatch_captures.json``
(the recorded production truth the gate's dispatch-schema check reads),
and ``scripts/gate_conformance.py --regenerate-captures`` re-records
them through this same harness.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_SCRIPT = REPO_ROOT / "scripts" / "gate_conformance.py"
CAPTURES_PATH = REPO_ROOT / "scripts" / "conformance_dispatch_captures.json"
TEMPLATES_DIR = REPO_ROOT / "ci" / "templates"


def _load_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gate_conformance_under_test", GATE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    return _load_gate()


@pytest.fixture(scope="module")
def seams(gate: ModuleType) -> dict[str, Any]:
    return gate._load_seams()


# ---------------------------------------------------------------------------
# The credential-consumption block: extraction + the two mutations (pure)
# ---------------------------------------------------------------------------


class TestCredentialBlockExtraction:
    def test_extracts_the_shipped_gitlab_block_verbatim(self, gate: ModuleType):
        text = (TEMPLATES_DIR / "claude-sdk-lane.gitlab-ci.yml").read_text(encoding="utf-8")
        block = gate.credential_block(text)
        assert block is not None
        lines = block.splitlines()
        assert lines[0].lstrip().startswith("if [ -n")
        assert lines[-1].strip() == "fi"
        # the consumer mapping + the fail-closed posture ride the block
        assert 'export ANTHROPIC_AUTH_TOKEN="$_CRED_VALUE"' in block
        assert "FORGE_BOOTSTRAP_FAILED" in block

    @pytest.mark.parametrize("name", ["forge-harness.github.yml", "forge-lane.azure-pipelines.yml"])
    def test_extracts_the_native_templates_blocks(self, gate: ModuleType, name: str):
        block = gate.credential_block((TEMPLATES_DIR / name).read_text(encoding="utf-8"))
        assert block is not None
        assert "FORGE_CREDENTIAL_REF" in block

    def test_absent_block_returns_none(self, gate: ModuleType):
        assert gate.credential_block("# nothing to consume here\n") is None

    def test_a_guard_without_a_closing_fi_refuses(self, gate: ModuleType):
        text = 'if [ -n "${FORGE_CREDENTIAL_REF:-}" ] && [ "${FORGE_CREDENTIAL_REDEEM:-}" != "1" ]; then\n  echo hi\n'
        with pytest.raises(gate.PrerequisiteError, match="no closing 'fi'"):
            gate.credential_block(text)


class TestConsumerMappingMutation:
    def test_drop_removes_the_export_and_keeps_the_rest(self, gate: ModuleType):
        text = (TEMPLATES_DIR / "claude-code.gitlab-ci.yml").read_text(encoding="utf-8")
        block = gate.credential_block(text)
        assert block is not None
        mutated, dropped = gate.drop_consumer_mapping(block)
        assert [line.strip() for line in dropped] == ['export ANTHROPIC_AUTH_TOKEN="$_CRED_VALUE"']
        assert "export ANTHROPIC_AUTH_TOKEN" not in mutated
        # the guard and the fail-closed refusal survive — the receipt
        # still looks correct; only the DELIVERY is gone.
        assert gate.credential_block(mutated) is not None or "FORGE_CREDENTIAL_REF" in mutated
        assert "FORGE_BOOTSTRAP_FAILED" in mutated

    def test_no_mapping_to_drop_refuses(self, gate: ModuleType):
        with pytest.raises(gate.PrerequisiteError, match="no consumer mapping"):
            gate.drop_consumer_mapping("if true; then\n  echo x\nfi\n")


class TestUnconditionalExitRestoration:
    def test_restores_the_defect_after_the_driver_phase(self, gate: ModuleType):
        from tests.test_gitlab_sdk_lane_finalization import finalization_block

        block = finalization_block(TEMPLATES_DIR / "claude-sdk-lane.gitlab-ci.yml")
        mutated = gate.restore_unconditional_exit(block)
        lines = mutated.splitlines()
        echo_index = next(
            i
            for i, line in enumerate(lines)
            if line.lstrip().startswith('echo "forge lane: driver_exit=')
        )
        assert lines[echo_index + 1].strip() == 'exit "$_driver_rc"'
        # still a parseable shell
        parse = subprocess.run(
            ["bash", "-n"], input=mutated.encode(), capture_output=True, check=False
        )
        assert parse.returncode == 0, parse.stderr.decode(errors="replace")

    def test_a_block_without_the_driver_seam_refuses(self, gate: ModuleType):
        with pytest.raises(gate.PrerequisiteError, match="driver-phase end seam"):
            gate.restore_unconditional_exit("echo unrelated\n")


# ---------------------------------------------------------------------------
# Declared surfaces + the schema decision logic (pure)
# ---------------------------------------------------------------------------


class TestDeclaredSurfaces:
    def test_github_inputs_include_the_credential_inputs(self, gate: ModuleType):
        declared = gate.github_declared_inputs(
            gate.yaml_doc(TEMPLATES_DIR / "forge-harness.github.yml")
        )
        assert {"credential_ref", "credential_redeem", "run_id", "lane_resume_mode"} <= declared

    def test_azure_parameters_include_the_credential_parameters(self, gate: ModuleType):
        declared = gate.azure_declared_parameters(
            gate.yaml_doc(TEMPLATES_DIR / "forge-lane.azure-pipelines.yml")
        )
        assert {"credential_ref", "credential_redeem", "run_id", "driver"} <= declared

    def test_a_template_without_a_dispatch_surface_refuses(self, gate: ModuleType):
        with pytest.raises(gate.PrerequisiteError):
            gate.github_declared_inputs({"jobs": {}})


class TestSchemaFindings:
    def test_an_undeclared_sent_key_is_a_finding(self, gate: ModuleType):
        findings = gate.declared_vs_captured(
            "github", {"run_id", "driver"}, {"default": ["run_id", "driver", "stray_key"]}
        )
        assert [(f.type, f.key) for f in findings] == [("undeclared_sent", "stray_key")]

    def test_a_declared_key_never_sent_is_a_finding(self, gate: ModuleType):
        findings = gate.declared_vs_captured(
            "azure", {"run_id", "dead_input"}, {"default": ["run_id"]}
        )
        assert [(f.type, f.key) for f in findings] == [("declared_never_sent", "dead_input")]

    def test_a_source_verified_conditional_annotation_covers_the_key(self, gate: ModuleType):
        findings = gate.declared_vs_captured(
            "github",
            {"run_id", "repair_context"},
            {"default": ["run_id"]},
            {
                "repair_context": (
                    "src/forge/runs/github_service.py",
                    '"repair_context": repair_context',
                )
            },
        )
        assert findings == []

    def test_a_stale_conditional_annotation_is_a_finding(self, gate: ModuleType):
        findings = gate.declared_vs_captured(
            "github",
            {"run_id", "repair_context"},
            {"default": ["run_id"]},
            {"repair_context": ("src/forge/runs/github_service.py", "definitely-not-here-anymore")},
        )
        assert {f.type for f in findings} == {"stale_conditional_annotation"}

    def test_a_conditional_annotation_for_an_undeclared_key_is_a_finding(self, gate: ModuleType):
        findings = gate.declared_vs_captured(
            "azure", {"run_id"}, {"default": ["run_id"]}, {"ghost": ("README.md", "forge")}
        )
        assert {f.type for f in findings} == {"stale_conditional_annotation"}

    def test_a_sent_key_no_recipe_consumes_is_a_finding(self, gate: ModuleType):
        findings = gate.sent_vs_consumed(
            {"default": ["FORGE_RUN_ID", "FORGE_ORPHAN"]}, {"FORGE_RUN_ID"}
        )
        assert [(f.type, f.key) for f in findings] == [("sent_never_consumed", "FORGE_ORPHAN")]


# ---------------------------------------------------------------------------
# The executed shipped-recipes arms (real bash; one recipe keeps the suite
# fast — the full four-recipe matrix is the gate's own e2e test below)
# ---------------------------------------------------------------------------


class TestShippedRecipesExecutor:
    def test_the_four_contract_arms_and_the_mutation_self_test(
        self, gate: ModuleType, seams: dict[str, Any], tmp_path: Path
    ):
        report = gate.run_shipped_recipes(
            seams, tmp_path, templates=("claude-sdk-lane.gitlab-ci.yml",)
        )
        assert report["status"] == "pass"
        assert report["failures"] == [] and report["mutation_escapes"] == []
        (entry,) = report["templates"]
        assert entry["template"] == "claude-sdk-lane.gitlab-ci.yml"
        arms = {arm["arm"]: arm for arm in entry["arms"]}
        assert set(arms) == {
            "success",
            "failure",
            "noop",
            "restored_generation",
            "mutation:unconditional_exit",
        }
        # the four contract arms passed under real bash
        for name in ("success", "failure", "noop", "restored_generation"):
            assert arms[name]["status"] == "pass", arms[name]
            assert arms[name]["job_rc"] is not None
        # the restored unconditional exit was CAUGHT before collection
        mutation = arms["mutation:unconditional_exit"]
        assert mutation["status"] == "caught"
        assert mutation["expectations"]["outcome_marker"] is False
        assert mutation["expectations"]["diff_carries_the_edit"] is False

    def test_the_selection_is_the_full_shipped_sdk_set(self, seams: dict[str, Any]):
        assert set(seams["SDK_LANE_TEMPLATES"]) == {
            "claude-sdk-lane.gitlab-ci.yml",
            "codex-sdk-lane.gitlab-ci.yml",
            "copilot-sdk-lane.gitlab-ci.yml",
            "opencode-sdk-lane.gitlab-ci.yml",
        }


# ---------------------------------------------------------------------------
# The executed secret-consumer sentinels (real bash on the shipped blocks)
# ---------------------------------------------------------------------------


class TestSecretConsumerSentinels:
    def test_every_recipe_with_a_block_passes_and_the_mutation_is_caught(
        self, gate: ModuleType, seams: dict[str, Any]
    ):
        report = gate.run_secret_consumers(seams)
        assert report["status"] == "pass", report["failures"]
        assert report["failures"] == [] and report["mutation_escapes"] == []
        by_template = {recipe["template"]: recipe for recipe in report["recipes"]}
        # the four #303 recipes ship blocks today and all pass
        for name in (
            "claude-code.gitlab-ci.yml",
            "claude-sdk-lane.gitlab-ci.yml",
            "forge-harness.github.yml",
            "forge-lane.azure-pipelines.yml",
        ):
            arms = {arm["arm"]: arm for arm in by_template[name]["arms"]}
            assert by_template[name]["status"] == "pass", name
            assert arms["sentinel"]["status"] == "pass", name
            assert arms["fail_closed"]["status"] == "pass", name
            assert arms["mutation:consumer_mapping_dropped"]["status"] == "caught", name
        # recipes without a block are RECORDED, never silently skipped
        absent = [name for name, recipe in by_template.items() if recipe["status"] == "absent"]
        assert absent  # the remaining SDK lanes (the #305 rollout surface)
        assert "codex-sdk-lane.gitlab-ci.yml" in absent

    def test_a_wrong_value_consumer_fails_the_sentinel_expectations(self, gate: ModuleType):
        """A block that keeps the AMBIENT value (the dropped-mapping
        defect, inlined) must fail the sentinel expectations — the proof
        that a correct-looking receipt is not evidence of consumption."""
        broken_block = (
            'if [ -n "${FORGE_CREDENTIAL_REF:-}" ] && [ "${FORGE_CREDENTIAL_REDEEM:-}" != "1" ]; then\n'
            '  export ANTHROPIC_AUTH_TOKEN="${ANTHROPIC_AUTH_TOKEN:-}"\n'
            "fi\n"
        )
        expectations, _result = gate.run_sentinel_arm(
            broken_block, carrier_env={"FORGE_MODEL_X": gate.DELIVERED_SENTINEL}, ref="X"
        )
        assert expectations["consumed_is_the_delivered_sentinel"] is False
        assert expectations["ambient_never_consumed"] is False

    def test_the_sentinel_arm_passes_on_a_correct_consumer(self, gate: ModuleType):
        good_block = (
            'if [ -n "${FORGE_CREDENTIAL_REF:-}" ] && [ "${FORGE_CREDENTIAL_REDEEM:-}" != "1" ]; then\n'
            '  if [ -z "${FORGE_MODEL_X:-}" ]; then\n'
            '    echo "FORGE_BOOTSTRAP_FAILED: missing"\n'
            "    exit 1\n"
            "  fi\n"
            '  export ANTHROPIC_AUTH_TOKEN="$FORGE_MODEL_X"\n'
            "  unset ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN 2>/dev/null || true\n"
            "fi\n"
        )
        expectations, result = gate.run_sentinel_arm(
            good_block, carrier_env={"FORGE_MODEL_X": gate.DELIVERED_SENTINEL}, ref="X"
        )
        assert all(expectations.values()), (expectations, result.stdout, result.stderr)


# ---------------------------------------------------------------------------
# The dispatch-schema check against the committed capture fixture
# ---------------------------------------------------------------------------


class TestDispatchSchemaOnTheCommittedCaptures:
    def test_the_current_tree_produces_no_findings(self, gate: ModuleType, seams: dict[str, Any]):
        report = gate.run_dispatch_schema(seams, gate.load_captures())
        assert report["status"] == "pass"
        assert report["findings"] == []
        for provider in ("github", "azure", "gitlab"):
            assert report["providers"][provider]["findings"] == []

    def test_a_forged_undeclared_key_is_a_finding(self, gate: ModuleType, seams: dict[str, Any]):
        captures = gate.load_captures()
        captures["providers"]["github"]["shapes"]["unbound"] = [
            *captures["providers"]["github"]["shapes"]["unbound"],
            "mystery_key",
        ]
        report = gate.run_dispatch_schema(seams, captures)
        assert report["status"] == "fail"
        assert "github:undeclared_sent:mystery_key" in report["findings"]

    def test_a_missing_fixture_refuses(self, gate: ModuleType, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(gate, "CAPTURES_PATH", tmp_path / "missing.json")
        with pytest.raises(gate.PrerequisiteError, match="regenerate"):
            gate.load_captures()


# ---------------------------------------------------------------------------
# The report, the executed-ID manifest and the CLI plumbing
# ---------------------------------------------------------------------------


class TestReportAndCli:
    def _checks(self, gate: ModuleType) -> dict[str, Any]:
        recipes = {
            "status": "pass",
            "templates": [
                {"template": "t.gitlab-ci.yml", "arms": [{"arm": "success", "status": "pass"}]}
            ],
            "failures": [],
            "mutation_escapes": [],
        }
        schema = {"status": "pass", "providers": {}, "failures": []}
        sentinels = {
            "status": "pass",
            "recipes": [
                {
                    "template": "t.gitlab-ci.yml",
                    "status": "pass",
                    "arms": [{"arm": "sentinel", "status": "pass"}],
                }
            ],
            "failures": [],
            "mutation_escapes": [],
        }
        locators = {
            "status": "pass",
            "driver_templates": [{"driver": "claude-code", "status": "pass"}],
            "self_tests": {"legacy_encoder_flagged": {"status": "caught"}},
            "failures": [],
            "mutation_escapes": [],
        }
        consumers = {
            "status": "pass",
            "arms": [
                {"arm": "grant:redeem_granted_route", "status": "pass"},
                {"arm": "rebind:three_way_digest_equality", "status": "pass"},
            ],
            "failures": [],
            "mutation_escapes": [],
        }
        return recipes, schema, sentinels, locators, consumers

    def test_the_report_carries_the_executed_id_manifest(self, gate: ModuleType):
        recipes, schema, sentinels, locators, consumers = self._checks(gate)
        report = gate.build_report(
            recipes, schema, sentinels, locators, consumers, "2026-09-24T00:00:00+00:00", 1.5, []
        )
        assert report["qualification"]["result"] == "green"
        assert report["executed_ids"] == [
            "shipped-recipes/t.gitlab-ci.yml/success",
            "dispatch-schema/github",
            "dispatch-schema/azure",
            "dispatch-schema/gitlab",
            "secret-consumers/t.gitlab-ci.yml/sentinel",
            "native-locators/encoding",
            "native-locators/driver/claude-code",
            "native-locators/self-test/legacy_encoder_flagged",
            "consumer-contracts/grant:redeem_granted_route",
            "consumer-contracts/rebind:three_way_digest_equality",
        ]
        assert (
            report["gate"]["issue"]
            == "#317 (R38-16); native-locators #323 (Q39-04); consumer-contracts #327 (Q39-08)"
        )
        # Q39-08: the identity stamping distinguishes the execution classes
        identities = report["identities"]
        assert identities["checks"]["consumer_contracts"]["source"].endswith(
            "run_consumer_contracts"
        )
        assert set(identities["execution_classes"]) == {"offline", "native", "paid"}
        # Q39-08: the observability counters
        assert report["observability"]["conformance.executed_case_count"] == len(
            report["executed_ids"]
        )
        assert report["observability"]["ci.critical_path_seconds"] == 1.5
        # the required consumer cases that did NOT execute are named — a
        # report from a partial run can never pose as a complete green.
        missing = report["observability"]["conformance.required_case_missing"]
        assert "consumer-contracts/grant:redeem_granted_route" not in missing
        assert "consumer-contracts/grant:sibling_route_refused_zero_broker" in missing
        assert "consumer-contracts/rebind:mutation:source_identity_swapped" in missing

    def test_a_refusal_is_recorded_with_its_exit_code(self, gate: ModuleType):
        report = gate.build_report(
            None,
            None,
            None,
            None,
            None,
            "2026-09-24T00:00:00+00:00",
            0.1,
            [gate.MutationEscapeError("boom")],
        )
        assert report["qualification"]["result"] == "refused"
        assert report["qualification"]["refusals"][0]["exit_code"] == 6
        assert report["executed_ids"] == []
        assert report["observability"]["conformance.executed_case_count"] == 0

    def test_a_locator_failure_exits_seven(self, gate: ModuleType):
        recipes, schema, sentinels, locators, consumers = self._checks(gate)
        locators = {**locators, "status": "fail", "failures": ["encoding:collision:a~b"]}
        report = gate.build_report(
            recipes, schema, sentinels, locators, consumers, "2026-09-24T00:00:00+00:00", 0.2, []
        )
        assert report["checks"]["native_locators"] is locators

    def test_a_consumer_failure_exits_eight(self, gate: ModuleType):
        recipes, schema, sentinels, locators, consumers = self._checks(gate)
        consumers = {**consumers, "status": "fail", "failures": ["grant:redeem_granted_route"]}
        report = gate.build_report(
            recipes, schema, sentinels, locators, consumers, "2026-09-24T00:00:00+00:00", 0.2, []
        )
        assert report["checks"]["consumer_contracts"] is consumers
        assert gate.ConsumerContractError.exit_code == 8

    def test_a_broken_prerequisite_exits_two_with_the_report_written(
        self, gate: ModuleType, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setattr(gate, "CAPTURES_PATH", tmp_path / "missing.json")
        report_path = tmp_path / "report.json"
        rc = gate.main(["--report", str(report_path)])
        assert rc == 2
        document = json.loads(report_path.read_text(encoding="utf-8"))
        assert document["qualification"]["result"] == "refused"
        assert document["qualification"]["refusals"][0]["type"] == "PrerequisiteError"

    def test_the_full_gate_is_green_on_this_tree(self, gate: ModuleType, tmp_path: Path):
        """The e2e the CI lint step runs: every shipped recipe executed,
        every sentinel arm, both mutation self-tests caught, zero schema
        findings — and the report/manifest on disk."""
        report_path = tmp_path / "conformance-gate.json"
        rc = gate.main(["--report", str(report_path)])
        assert rc == 0
        document = json.loads(report_path.read_text(encoding="utf-8"))
        assert document["qualification"]["result"] == "green"
        executed = document["executed_ids"]
        for template in (
            "claude-sdk-lane.gitlab-ci.yml",
            "codex-sdk-lane.gitlab-ci.yml",
            "copilot-sdk-lane.gitlab-ci.yml",
            "opencode-sdk-lane.gitlab-ci.yml",
        ):
            assert f"shipped-recipes/{template}/mutation:unconditional_exit" in executed
        assert (
            "secret-consumers/forge-harness.github.yml/mutation:consumer_mapping_dropped"
            in executed
        )
        assert "dispatch-schema/gitlab" in executed
        # Q39-04 (#323): the locator arms executed — the per-driver
        # template conformance, the self-tests, and the sentinel arms on
        # the collision-safe spelling.
        assert "native-locators/encoding" in executed
        assert "native-locators/driver/claude-code" in executed
        assert "native-locators/self-test/legacy_encoder_flagged" in executed
        assert "secret-consumers/claude-code.gitlab-ci.yml/sentinel_locator" in executed
        assert "secret-consumers/forge-harness.github.yml/sentinel_locator" in executed
        assert "secret-consumers/forge-lane.azure-pipelines.yml/fail_closed_locator" in executed
        # Q39-08 (#327): the consumer-contract arms executed — the grant
        # through the real ASGI endpoint, the runner-side typed
        # verification, the rebind digest, every mutation caught, and the
        # report's identity/observability blocks are populated.
        assert "consumer-contracts/grant:redeem_granted_route" in executed
        assert "consumer-contracts/grant:sibling_route_refused_zero_broker" in executed
        assert "consumer-contracts/grant:mutation:dto_preserved_caller_disconnected" in executed
        assert "consumer-contracts/runner-verification:baseline_applies_delivered" in executed
        assert "consumer-contracts/runner-verification:env_var_answer_halts_zero_calls" in executed
        assert "consumer-contracts/rebind:three_way_digest_equality" in executed
        assert "consumer-contracts/rebind:mutation:plan_binding_removed" in executed
        assert "consumer-contracts/rebind:mutation:source_identity_swapped" in executed
        # the comment-only marker spoof ran on every blocked recipe
        assert (
            "secret-consumers/claude-code.gitlab-ci.yml/mutation:comment_only_marker_spoof"
            in executed
        )
        consumers = document["checks"]["consumer_contracts"]
        assert consumers["status"] == "pass"
        assert consumers["failures"] == [] and consumers["mutation_escapes"] == []
        statuses = {arm["arm"]: arm["status"] for arm in consumers["arms"]}
        assert statuses["grant:mutation:dto_preserved_caller_disconnected"] == "caught"
        assert statuses["rebind:mutation:plan_binding_removed"] == "caught"
        assert statuses["rebind:mutation:source_identity_swapped"] == "caught"
        identities = document["identities"]
        assert identities["checks"]["consumer_contracts"]["arm_classes"]
        assert identities["runtime"]["python"]
        observability = document["observability"]
        assert observability["conformance.executed_case_count"] == len(executed)
        assert observability["conformance.required_case_missing"] == []


# ---------------------------------------------------------------------------
# The native-locator dimension (Q39-04 / #323): the collision-safe
# encoding, the per-driver template route, the digest inventory.
# ---------------------------------------------------------------------------


class TestLocatorEncodingFindings:
    def test_the_shipped_locator_encoder_produces_zero_findings(self, gate: ModuleType):
        findings = gate._locator_encoding_findings(
            gate._load_seams()["native_locator"], gate.LOCATOR_PROBE_REFS
        )
        assert findings == []

    def test_the_legacy_lossy_encoder_is_flagged(self, gate: ModuleType):
        """The sensitivity proof (P05): the pre-#323 segment encoder
        collapses the whole probe family onto ONE legacy segment — the
        gate's encoding check MUST flag every aliasing pair."""
        findings = gate._locator_encoding_findings(
            gate._load_seams()["credential_secret_segment"], gate.LOCATOR_PROBE_REFS
        )
        # every slash/dash/underscore/case/dot variant aliases to the ONE
        # legacy segment VAULT_KV_TEAM_A (the first sorted ref holds it,
        # every other variant is flagged against it)
        assert len(findings) >= 4
        assert all("->VAULT_KV_TEAM_A" in finding for finding in findings)


class TestLocatorConformance:
    def test_green_on_this_tree_with_the_inventory_recorded(
        self, gate: ModuleType, seams: dict[str, Any]
    ):
        report = gate.run_locator_conformance(seams)
        assert report["status"] == "pass", report["failures"]
        assert report["failures"] == [] and report["mutation_escapes"] == []
        by_driver = {entry["driver"]: entry for entry in report["driver_templates"]}
        # the anthropic-route recipes implement the native route and pass
        # the conformance named with THEIR driver
        assert by_driver["claude-code"]["status"] == "pass"
        assert by_driver["claude-sdk-lane"]["status"] == "pass"
        # the not-yet-rolled-out lanes are RECORDED, never silently green
        assert by_driver["codex-sdk-lane"]["status"] == "route_absent"
        assert by_driver["grok-build"]["status"] == "route_absent"
        # the digest inventory: every validated template's identity digest
        assert "claude-code.gitlab-ci.yml" in report["digest_inventory"]
        assert "forge-harness.github.yml" in report["digest_inventory"]
        assert "forge-lane.azure-pipelines.yml" in report["digest_inventory"]
        for digest in report["digest_inventory"].values():
            assert len(digest) == 16 and digest == digest.lower()

    def test_every_self_test_is_caught(self, gate: ModuleType, seams: dict[str, Any]):
        report = gate.run_locator_conformance(seams)
        statuses = {name: arm["status"] for name, arm in report["self_tests"].items()}
        assert statuses["legacy_encoder_flagged"] == "caught"
        assert statuses["wrong_driver_template_refused"] == "caught"
        assert statuses["digest_mismatch_refused"] == "caught"
        assert statuses["unknown_driver_refused"] == "caught"

    def test_a_required_driver_without_the_route_fails_the_check(
        self, gate: ModuleType, seams: dict[str, Any], monkeypatch
    ):
        """A shipped anthropic-route recipe that LOST its consumption
        block must fail the check — the required set is not advisory."""
        monkeypatch.setitem(
            seams,
            "native_route_structural_gaps",
            lambda text, env_var: ["consumer_mapping:ANTHROPIC_AUTH_TOKEN"],
        )
        report = gate.run_locator_conformance(seams)
        assert report["status"] == "fail"
        assert any("driver_route_missing:claude-code" in f for f in report["failures"])


class TestSentinelLocatorArms:
    def test_every_recipe_with_a_block_passes_the_locator_arms(
        self, gate: ModuleType, seams: dict[str, Any]
    ):
        """The #317 sentinel arms on the collision-safe spelling (#323):
        the shipped blocks consume a LOCATOR-shaped ref + carrier — the
        delivered sentinel is the consumed one, the ambient never, and
        an empty locator carrier fails CLOSED."""
        report = gate.run_secret_consumers(seams)
        assert report["status"] == "pass", report["failures"]
        by_template = {recipe["template"]: recipe for recipe in report["recipes"]}
        for name in (
            "claude-code.gitlab-ci.yml",
            "claude-sdk-lane.gitlab-ci.yml",
            "forge-harness.github.yml",
            "forge-lane.azure-pipelines.yml",
        ):
            arms = {arm["arm"]: arm for arm in by_template[name]["arms"]}
            assert arms["sentinel_locator"]["status"] == "pass", name
            assert arms["fail_closed_locator"]["status"] == "pass", name


# ---------------------------------------------------------------------------
# The production dispatch-payload capture harness
# ---------------------------------------------------------------------------


class DeclaringGitHubClient:
    """A wrapper around the REAL GitHubClient (pointed at the
    production-entry fake native server) that REFUSES a workflow dispatch
    whose inputs fall outside the shipped template's declared surface.

    The sibling fake's ledger accepts anything — that permissiveness is
    the recorded failure mode; this wrapper is the early guard, and it
    records the exact payload key sets the capture fixture freezes.
    """

    def __init__(self, inner: Any, declared: set[str]) -> None:
        self._inner = inner
        self._declared = declared
        self.captured_inputs: list[dict[str, str]] = []

    async def dispatch_workflow(self, owner, repo, workflow_filename, ref, inputs=None):
        payload = dict(inputs or {})
        self.captured_inputs.append(dict(payload))
        undeclared = sorted(set(payload) - self._declared)
        if undeclared:
            from forge.integrations.github import GitHubAPIError

            raise GitHubAPIError(
                422,
                f"undeclared workflow_dispatch inputs {undeclared} — the shipped "
                "template does not declare them (conformance capture wrapper)",
            )
        return await self._inner.dispatch_workflow(
            owner, repo, workflow_filename, ref, inputs=payload
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class DeclaringGitLabClient:
    """The same guard on the GitLab pipeline-trigger boundary: every
    dispatched pipeline variable must be consumed by at least one shipped
    recipe (GitLab declares nothing in YAML — the recipes' consumed
    surface IS the contract)."""

    def __init__(self, inner: Any, consumed: set[str]) -> None:
        self._inner = inner
        self._consumed = consumed
        self.captured_keys: list[list[str]] = []

    async def create_pipeline(self, project_id, ref, variables=None):
        keys = [str(entry["key"]) for entry in (variables or [])]
        self.captured_keys.append(keys)
        orphaned = sorted(set(keys) - self._consumed)
        if orphaned:
            from forge.gitlab.client import GitLabAPIError

            raise GitLabAPIError(
                422,
                f"dispatched pipeline variables no shipped recipe consumes: "
                f"{orphaned} (conformance capture wrapper)",
            )
        return await self._inner.create_pipeline(project_id, ref, variables=variables)

    # The config reader probes the CLASS for the typed ``read_blob`` seam
    # (``hasattr(type(client), "read_blob")``) — a pure ``__getattr__``
    # delegator hides it and turns a provider-confirmed 404 into an
    # "unreadable" park. Delegate it explicitly.
    async def read_blob(self, project_id, file_path, ref="HEAD"):
        return await self._inner.read_blob(project_id, file_path, ref=ref)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class DeclaringAzureClient:
    """The same guard on the Azure Runs-API boundary: templateParameters
    must stay inside the shipped template's declared parameters."""

    def __init__(self, inner: Any, declared: set[str]) -> None:
        self._inner = inner
        self._declared = declared
        self.captured_parameters: list[dict[str, str]] = []

    async def run_pipeline(
        self, project, pipeline_id, *, ref_name=None, template_parameters=None, variables=None
    ):
        payload = dict(template_parameters or {})
        self.captured_parameters.append(dict(payload))
        undeclared = sorted(set(payload) - self._declared)
        if undeclared:
            from forge.integrations.azure import AzureDevOpsError

            raise AzureDevOpsError(
                400,
                f"undeclared templateParameters {undeclared} — the shipped lane "
                "template does not declare them (conformance capture wrapper)",
            )
        return await self._inner.run_pipeline(
            project,
            pipeline_id,
            ref_name=ref_name,
            template_parameters=payload,
            variables=variables,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _declared_github_inputs() -> set[str]:
    doc = yaml.safe_load((TEMPLATES_DIR / "forge-harness.github.yml").read_text(encoding="utf-8"))
    trigger = doc.get("on", doc.get(True))
    return set(trigger["workflow_dispatch"]["inputs"])


def _declared_azure_parameters() -> set[str]:
    doc = yaml.safe_load(
        (TEMPLATES_DIR / "forge-lane.azure-pipelines.yml").read_text(encoding="utf-8")
    )
    return {entry["name"] for entry in doc["parameters"]}


def _consumed_gitlab_variables() -> set[str]:
    import re

    consumed: set[str] = set()
    pattern = re.compile(r"\bFORGE_[A-Z0-9_]+")
    for path in sorted(TEMPLATES_DIR.glob("*.gitlab-ci.yml")):
        consumed |= set(pattern.findall(path.read_text(encoding="utf-8")))
    return consumed


@contextlib.contextmanager
def _env(**values: str):
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, previous in saved.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


@contextlib.contextmanager
def _fake_native(workroot: Path, *, gitlab: bool):
    """The production-entry fake native server as a real subprocess — the
    same launch shape the sibling ``native``/``gitlab_native`` fixtures
    use, reused by import from the conftest module."""
    from tests.production_entry import conftest as pe

    ready = workroot / ("gitlab-native-ready.json" if gitlab else "gh-native-ready.json")
    ready.parent.mkdir(parents=True, exist_ok=True)
    arguments = [
        sys.executable,
        str(pe.FAKE_NATIVE_SERVER),
        "--ready-file",
        str(ready),
        "--repo",
        str(pe.GL_PROJECT_ID) if gitlab else pe.PE_REPO,
        "--base-branch",
        pe.GL_BASE_BRANCH if gitlab else pe.PE_BASE_BRANCH,
        "--base-sha",
        pe.GL_BASE_SHA if gitlab else pe.PE_BASE_SHA,
    ]
    if gitlab:
        arguments += ["--api", "gitlab"]
    process = subprocess.Popen(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 20.0
    try:
        while not ready.is_file():
            if process.poll() is not None:
                raise AssertionError("the fake native server died at startup")
            if time.monotonic() > deadline:
                raise AssertionError("the fake native server never became ready")
            time.sleep(0.02)
        port = int(json.loads(ready.read_text(encoding="utf-8"))["port"])
        server = pe.FakeGitLabNative(process, port) if gitlab else pe.FakeNative(process, port)
        yield server
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover — a wedged child
            process.kill()
            process.wait(timeout=10)


class _StaticTokens:
    def __init__(self, token: str) -> None:
        self._token = token

    async def token(self) -> str:
        return self._token

    async def invalidate(self) -> None:
        return None


async def _capture_github(workroot: Path) -> dict[str, list[str]]:
    """The REAL GitHubRunService through the REAL fake native server,
    under the three delivery profiles — inputs recorded at the HTTP
    boundary by the declaring wrapper."""
    from forge.adaptive.credential_broker import (
        DELIVERY_MODE_GITHUB_NATIVE,
        DELIVERY_MODE_RUNNER_REDEMPTION,
    )
    from tests.production_entry import conftest as pe

    shapes: dict[str, list[str]] = {}
    with _fake_native(workroot, gitlab=False) as native:
        native.seed_issue(42, "Add the widget", "Body")
        for shape, delivery in (
            ("unbound", None),
            ("bound_native", DELIVERY_MODE_GITHUB_NATIVE),
            ("bound_redemption", DELIVERY_MODE_RUNNER_REDEMPTION),
        ):
            # a FRESH database per shape: the previous shape's run is
            # still parked waiting_harness and would occupy the project's
            # execution slot (one dispatch per drive, honestly isolated)
            database = pe.PEDatabase(f"sqlite+aiosqlite:///{workroot / f'gh-{shape}.db'}")
            await database.create_schema()
            try:
                shapes[shape] = await _drive_github_shape(native, database, delivery=delivery)
            finally:
                await database.dispose()
    return shapes


async def _drive_github_shape(native: Any, database: Any, *, delivery: str | None) -> list[str]:
    from forge.adaptive.credential_broker import StagedBroker
    from forge.adaptive.operator_snapshot import subject_of_run
    from forge.adaptive.project_credentials import ProjectCredentialRegistry
    from forge.config import ForgeConfig
    from forge.durable import FlowRun
    from forge.integrations.github import GitHubClient, GitHubRepositoryReader
    from forge.integrations.github_flow import GitHubAgents, GitHubPublishFlow
    from forge.runs.github_service import GitHubRunService
    from forge.runs.stubs import StubImplementer, StubPlanner
    from tests.production_entry import conftest as pe
    from forge.adaptive.credential_broker import (
        DELIVERY_ROUTE_ENV,
        DELIVERY_TEMPLATE_DIR_ENV,
    )

    client = DeclaringGitHubClient(
        GitHubClient(
            base_url=native.base_url,
            token_provider=_StaticTokens("pe-capture-token"),  # noqa: S106
        ),
        _declared_github_inputs(),
    )
    registry = None
    broker = None
    if delivery is not None:
        registry = ProjectCredentialRegistry()
        broker = StagedBroker()
    reader = GitHubRepositoryReader(client, pe.PE_OWNER, pe.PE_REPO_NAME)
    service = GitHubRunService(
        database.worker_factory(),
        pe.pe_settings(),
        ForgeConfig(),
        stack=GitHubAgents(
            client=client,
            reader=reader,
            planner=StubPlanner(),
            implementer=StubImplementer(),
            reviewer=pe.StubPRReviewer(),
            flow=GitHubPublishFlow(
                client, proposer=StubImplementer(), base_branch=pe.PE_BASE_BRANCH
            ),
        ),
        repo_full_name=pe.PE_REPO,
        credential_registry=registry,
        credential_broker=broker,
    )
    run_id = await service.start_run(
        project_id=42,
        issue_number=42,
        issue_title="Add the widget",
        issue_description="Body",
        author_username="alice",
    )
    if registry is not None and broker is not None:
        factory = database.worker_factory()
        async with factory() as session:
            row = await session.get(FlowRun, run_id)
            subject = subject_of_run(row)
        registry.bind(subject, "anthropic-gateway", CAPTURE_REF, bound_by="capture")
        broker.stage(CAPTURE_REF, CAPTURE_SECRET, env_var="ANTHROPIC_AUTH_TOKEN", version="v1")
    with _env(
        ANTHROPIC_AUTH_TOKEN=AMBIENT_CAPTURE_VALUE,
        **{DELIVERY_ROUTE_ENV: delivery or "", DELIVERY_TEMPLATE_DIR_ENV: str(TEMPLATES_DIR)},
    ):
        await service.handle_go(
            project_id=42,
            issue_number=42,
            note_text=f"@forge /go {run_id}",
            author_username="alice",
        )
    if not client.captured_inputs:
        factory = database.worker_factory()
        async with factory() as session:
            row = await session.get(FlowRun, run_id)
        raise AssertionError(
            f"the github capture drive never dispatched (status={row.status}, "
            f"reason={row.status_reason})"
        )
    keys = sorted(client.captured_inputs[-1])
    await client.aclose()
    return keys


CAPTURE_REF = "env:ANTHROPIC_AUTH_TOKEN"  # noqa: S105 — a fixture ref, never a value
CAPTURE_SECRET = "conformance-capture-secret"  # noqa: S105 — a fixture value
AMBIENT_CAPTURE_VALUE = "conformance-capture-ambient"  # noqa: S105 — a fixture value


async def _capture_gitlab(workroot: Path) -> dict[str, list[str]]:
    """The REAL (GitLab) RunService through the fake native server's
    GitLab mode — the dispatched pipeline variables per delivery profile."""
    from forge.adaptive.credential_broker import (
        DELIVERY_MODE_GITLAB_PROTECTED,
        DELIVERY_MODE_RUNNER_REDEMPTION,
    )
    from tests.production_entry import conftest as pe

    shapes: dict[str, list[str]] = {}
    with _fake_native(workroot, gitlab=True) as native:
        native.seed_issue(pe.GL_ISSUE_IID, pe.GL_ISSUE_TITLE, pe.GL_ISSUE_DESC)
        for shape, delivery in (
            ("unbound", None),
            ("bound_native", DELIVERY_MODE_GITLAB_PROTECTED),
            ("bound_redemption", DELIVERY_MODE_RUNNER_REDEMPTION),
        ):
            # a FRESH database per shape (one dispatch per drive — the
            # parked previous run would occupy the execution slot)
            database = pe.PEDatabase(f"sqlite+aiosqlite:///{workroot / f'gl-{shape}.db'}")
            await database.create_schema()
            try:
                shapes[shape] = await _drive_gitlab_shape(native, database, delivery=delivery)
            finally:
                await database.dispose()
    return shapes


async def _drive_gitlab_shape(native: Any, database: Any, *, delivery: str | None) -> list[str]:
    from forge.adaptive.credential_broker import (
        DELIVERY_ROUTE_ENV,
        DELIVERY_TEMPLATE_DIR_ENV,
        StagedBroker,
    )
    from forge.adaptive.operator_snapshot import subject_of_run
    from forge.adaptive.project_credentials import ProjectCredentialRegistry
    from forge.config import ForgeConfig
    from forge.durable import FlowRun
    from forge.gitlab.client import GitLabClient
    from forge.runs.service import RunService
    from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer
    from tests.production_entry import conftest as pe

    client = DeclaringGitLabClient(
        GitLabClient(base_url=native.base_url, token="pe-capture-token"),  # noqa: S106
        _consumed_gitlab_variables(),
    )
    registry = None
    broker = None
    if delivery is not None:
        registry = ProjectCredentialRegistry()
        broker = StagedBroker()
    service = RunService(
        database.worker_factory(),
        gitlab=client,
        settings=pe.gl_settings(),
        config=ForgeConfig(),
        planner=StubPlanner(),
        implementer=StubImplementer(),
        reviewer=StubReviewer(),
        credential_registry=registry,
        credential_broker=broker,
    )
    run_id = await service.start_run(
        pe.GL_PROJECT_ID,
        pe.GL_ISSUE_IID,
        pe.GL_ISSUE_TITLE,
        pe.GL_ISSUE_DESC,
        "alice",
    )
    if registry is not None and broker is not None:
        factory = database.worker_factory()
        async with factory() as session:
            row = await session.get(FlowRun, run_id)
            subject = subject_of_run(row)
        registry.bind(subject, "anthropic-gateway", CAPTURE_REF, bound_by="capture")
        broker.stage(CAPTURE_REF, CAPTURE_SECRET, env_var="ANTHROPIC_AUTH_TOKEN", version="v1")
    with _env(
        ANTHROPIC_AUTH_TOKEN=AMBIENT_CAPTURE_VALUE,
        **{DELIVERY_ROUTE_ENV: delivery or "", DELIVERY_TEMPLATE_DIR_ENV: str(TEMPLATES_DIR)},
    ):
        await service.handle_command_note(
            pe.GL_PROJECT_ID,
            f"@forge /go {run_id}",
            "alice",
            pe.GL_ISSUE_IID,
            author_user_id=11,
        )
    if not client.captured_keys:
        factory = database.worker_factory()
        async with factory() as session:
            row = await session.get(FlowRun, run_id)
        raise AssertionError(
            f"the gitlab capture drive never dispatched (status={row.status}, "
            f"reason={row.status_reason})"
        )
    keys = sorted(client.captured_keys[-1])
    await client.close()
    return keys


async def _capture_azure(workroot: Path) -> dict[str, list[str]]:
    """The REAL AzureRunService over the in-memory fake Azure DevOps —
    the Runs-API templateParameters per delivery profile."""
    from forge.adaptive.credential_broker import (
        DELIVERY_MODE_AZURE_GROUP,
        DELIVERY_MODE_RUNNER_REDEMPTION,
    )
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    shapes: dict[str, list[str]] = {}
    for shape, delivery in (
        ("unbound", None),
        ("bound_native", DELIVERY_MODE_AZURE_GROUP),
        ("bound_redemption", DELIVERY_MODE_RUNNER_REDEMPTION),
    ):
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        from forge.models.base import Base

        async with engine.begin() as connection:
            import forge.adaptive.mailbox_db  # noqa: F401 — register every table
            import forge.durable.models  # noqa: F401

            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            shapes[shape] = await _drive_azure_shape(factory, delivery=delivery)
        finally:
            await engine.dispose()
    return shapes


async def _drive_azure_shape(factory: Any, *, delivery: str | None) -> list[str]:
    from forge.adaptive.credential_broker import (
        DELIVERY_ROUTE_ENV,
        DELIVERY_TEMPLATE_DIR_ENV,
        StagedBroker,
    )
    from forge.adaptive.operator_snapshot import subject_of_run
    from forge.adaptive.project_credentials import ProjectCredentialRegistry
    from forge.config import ForgeConfig
    from forge.durable import FlowRun
    from forge.integrations.azure import AzureRepositoryReader
    from forge.runs.azure_service import AzureAgents, AzureRunService
    from forge.runs.stubs import StubImplementer, StubPlanner
    from tests.test_azure_runs import (
        FakeAzureDevOps,
        LANE_PIPELINE_ID,
        PROJECT,
        PROJECT_ID,
        REPO,
        REPO_FULL,
        WORK_ITEM,
        WORK_ITEM_DESC_HTML,
        WORK_ITEM_TITLE,
        StubAzureReviewer,
        make_settings,
    )

    fake = FakeAzureDevOps()
    fake.seed_work_item(WORK_ITEM, WORK_ITEM_TITLE, WORK_ITEM_DESC_HTML)
    client = DeclaringAzureClient(fake, _declared_azure_parameters())
    registry = None
    broker = None
    if delivery is not None:
        registry = ProjectCredentialRegistry()
        broker = StagedBroker()
    service = AzureRunService(
        factory,
        make_settings(FORGE_AZDO_LANE_PIPELINE_ID=LANE_PIPELINE_ID),
        ForgeConfig(),
        stack=AzureAgents(
            client=client,
            reader=AzureRepositoryReader(client, PROJECT, REPO),
            planner=StubPlanner(),
            implementer=StubImplementer(),
            reviewer=StubAzureReviewer(),
        ),
        repo_full_name=REPO_FULL,
        credential_registry=registry,
        credential_broker=broker,
    )
    run_id = await service.start_run(
        project_id=PROJECT_ID,
        issue_number=WORK_ITEM,
        issue_title=WORK_ITEM_TITLE,
        issue_description=WORK_ITEM_DESC_HTML,
        author_username="dev@fabrikam.example",
    )
    if registry is not None and broker is not None:
        async with factory() as session:
            row = await session.get(FlowRun, run_id)
            subject = subject_of_run(row)
        registry.bind(subject, "anthropic-gateway", CAPTURE_REF, bound_by="capture")
        broker.stage(CAPTURE_REF, CAPTURE_SECRET, env_var="ANTHROPIC_AUTH_TOKEN", version="v1")
    with _env(
        ANTHROPIC_AUTH_TOKEN=AMBIENT_CAPTURE_VALUE,
        **{DELIVERY_ROUTE_ENV: delivery or "", DELIVERY_TEMPLATE_DIR_ENV: str(TEMPLATES_DIR)},
    ):
        await service.handle_go(
            project_id=PROJECT_ID,
            issue_number=WORK_ITEM,
            note_text=f"/go {run_id}",
            author_username="dev@fabrikam.example",
        )
    if not client.captured_parameters:
        async with factory() as session:
            row = await session.get(FlowRun, run_id)
        raise AssertionError(
            f"the azure capture drive never dispatched (status={row.status}, "
            f"reason={row.status_reason})"
        )
    return sorted(client.captured_parameters[-1])


async def capture_dispatch_payloads_async(workroot: Path | None = None) -> dict[str, Any]:
    """The recorded production dispatch payload KEY SETS, per provider and
    per delivery profile, captured through the real services and fakes.

    Only KEY SETS are recorded — never values (dispatch payloads can
    carry refs; values never ride them, and the capture keeps it that
    way).
    """
    from datetime import UTC, datetime

    root = workroot or Path(tempfile.mkdtemp(prefix="forge-conformance-capture-"))
    return {
        "schema": "forge.conformance-dispatch-captures/1",
        "captured_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "provenance": (
            "fresh captures: the REAL GitHubRunService/RunService/AzureRunService driven "
            "through the production-entry fakes (fake native server subprocess; in-memory "
            "fake Azure DevOps), fronted by the declaring wrapper clients in "
            "tests/test_gate_conformance.py — keys only, never values"
        ),
        "providers": {
            "github": {"shapes": await _capture_github(root / "github")},
            "gitlab": {"shapes": await _capture_gitlab(root / "gitlab")},
            "azure": {"shapes": await _capture_azure(root / "azure")},
        },
    }


def capture_dispatch_payloads() -> dict[str, Any]:
    """The sync entry the gate's ``--regenerate-captures`` mode drives."""
    return asyncio.run(capture_dispatch_payloads_async())


class TestCapturesMatchTheCommittedFixture:
    async def test_the_live_capture_matches_the_committed_fixture(self, tmp_path: Path):
        """The drift pin: what the services send TODAY through the fakes
        is exactly what the committed fixture recorded (and what the
        gate's dispatch-schema check validates the templates against).
        A new key, a removed key or an undeclared key all fail here —
        the declaring wrappers refuse undeclared keys at the boundary."""
        live = await capture_dispatch_payloads_async(tmp_path)
        committed = json.loads(CAPTURES_PATH.read_text(encoding="utf-8"))
        assert committed["schema"] == live["schema"]
        for provider in ("github", "gitlab", "azure"):
            assert (
                committed["providers"][provider]["shapes"] == live["providers"][provider]["shapes"]
            ), (
                f"the {provider} dispatch payload drifted from the committed capture — "
                "regenerate with `uv run python scripts/gate_conformance.py "
                "--regenerate-captures` after reviewing the drift"
            )
        # the bound shapes carry the credential ENVELOPE keys (refs only)
        spellings = {
            "github": ("credential_ref", "credential_redeem"),
            "azure": ("credential_ref", "credential_redeem"),
            "gitlab": ("FORGE_CREDENTIAL_REF", "FORGE_CREDENTIAL_REDEEM"),
        }
        for provider, (ref_key, redeem_key) in spellings.items():
            shapes = committed["providers"][provider]["shapes"]
            assert ref_key in shapes["bound_native"], provider
            assert redeem_key in shapes["bound_native"], provider
            assert ref_key not in shapes["unbound"], provider
            assert redeem_key not in shapes["unbound"], provider
