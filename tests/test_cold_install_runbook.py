"""R40-12 (#348) — the cold-install KIT: the runbook contract, the four
negative arms, the oracle replay, and the honest human/lab boundary.

The kit's pieces and what these tests hold:

- **the runbook document** (``docs/onboarding/cold-install-runbook.md``):
  its marked steps parse, its counts are the honest 9 machine / 4 human /
  3 lab, every machine step carries commands AND a registered observable
  assertion, the identity card pins equal the committed manifest, the
  observation template carries the issue's observability names, and every
  typed refusal the failure-mode table documents appears VERBATIM in the
  tool's source (the table is a contract, not prose);
- **the negative arms** (pure gates, fixture-driven): missing permission,
  unavailable runner and incompatible schema (N-2) each refuse TYPED
  BEFORE the unsafe or paid action;
- **the oracle replay**: the smoke job's own heredoc extracts verbatim
  from the manifest-rendered CI, passes on the seeded GOLD state and
  FAILS on a tampered state (the oracle bites);
- **the boundary**: human and lab steps are counted, never executed;
  an unregistered machine step is a refusal, never a silent pass; the
  package stays "ready for the second engineer", never
  "second-engineer-verified";
- **the restore rehearsal helper**: both referenced drills pass on
  disposable fixtures with the zero-model-turn ordering held.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import cold_install_check  # noqa: E402
from cold_install_check import (  # noqa: E402
    CredentialsObservation,
    NEGATIVE_ARMS,
    RUNBOOK_PATH,
    _RUNBOOK_STEP_ASSERTIONS,
    _seed_files,
    check_wheel_identity,
    credentials_preflight_findings,
    extract_smoke_oracle_script,
    load_manifest,
    parse_runbook,
    preflight_refusals,
    render_target_template,
    revision_two_behind,
    runner_availability_findings,
    schema_compatibility_findings,
    template_preflight_findings,
)

MANIFEST_PATH = ROOT / "qualification" / "profiles" / "supported-gitlab-ce-v1.json"
RUNBOOK = RUNBOOK_PATH


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return load_manifest(MANIFEST_PATH)


@pytest.fixture(scope="module")
def runbook_text() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def steps(runbook_text: str) -> list[cold_install_check.RunbookStep]:
    return parse_runbook(runbook_text)


def _section(text: str, start: str, end: str) -> str:
    body = text.split(start, 1)[1]
    return body.split(end, 1)[0] if end else body


# ---------------------------------------------------------------------------
# The runbook document: the kit's contract
# ---------------------------------------------------------------------------


class TestRunbookDocument:
    def test_the_honest_counts(self, steps: list[cold_install_check.RunbookStep]) -> None:
        kinds = [step.kind for step in steps]
        assert kinds.count("machine") == 9
        assert kinds.count("human") == 4
        assert kinds.count("lab") == 3
        # and the document states the same counts up front
        text = RUNBOOK.read_text(encoding="utf-8")
        assert "9 machine steps, 4 human steps, 3 lab steps" in text

    def test_step_ids_are_unique(self, steps: list[cold_install_check.RunbookStep]) -> None:
        ids = [step.step_id for step in steps]
        assert len(ids) == len(set(ids))

    def test_every_machine_step_has_commands_and_a_registered_assertion(
        self, steps: list[cold_install_check.RunbookStep]
    ) -> None:
        for step in steps:
            if step.kind != "machine":
                continue
            assert step.commands, f"{step.step_id} documents no command"
            assert step.expects, f"{step.step_id} documents no observable"
            assert step.step_id in _RUNBOOK_STEP_ASSERTIONS, (
                f"{step.step_id} has no registered observable assertion — an unchecked "
                "step would be a silent pass"
            )

    def test_every_human_and_lab_step_names_its_observable(
        self, steps: list[cold_install_check.RunbookStep]
    ) -> None:
        for step in steps:
            if step.kind in ("human", "lab"):
                assert step.expects, f"{step.step_id} documents no observable"
        for step in steps:
            if step.kind == "lab":
                assert step.blocked_reason, f"{step.step_id} records no blocked-on-lab reason"

    def test_the_human_steps_are_exactly_the_issues_human_deliverables(
        self, steps: list[cold_install_check.RunbookStep]
    ) -> None:
        human_ids = {step.step_id for step in steps if step.kind == "human"}
        assert human_ids == {
            "second-engineer-install",
            "token-provisioning",
            "observation-report",
            "support-decision",
        }

    def test_the_lab_steps_are_named_with_their_reasons(
        self, steps: list[cold_install_check.RunbookStep], runbook_text: str
    ) -> None:
        lab = {step.step_id: step for step in steps if step.kind == "lab"}
        assert set(lab) == {"smoke-gitlab", "verify-installed", "wip-continuation"}
        # the WIP continuation names the EXACT lab dependencies that make it
        # non-disposable: the app ingress, the pinned runner, the paid lane
        wip = lab["wip-continuation"]
        assert "localhost:8420" in wip.blocked_reason
        assert "runner id 4" in wip.blocked_reason
        assert "PAID" in wip.blocked_reason
        assert "run_useful_wip_resume" in runbook_text  # the #326 playbook is the named shape

    def test_the_identity_card_pins_equal_the_committed_manifest(
        self, manifest: dict[str, Any], runbook_text: str
    ) -> None:
        revision = manifest["control_plane"]["schema_revision"]
        assert str(manifest["manifest_digest"]) in runbook_text
        assert str(manifest["lane"]["wheel"]["sha256"]) in runbook_text
        assert str(manifest["target_template"]["frozen"]["sha256"][:16]) in runbook_text
        assert f"head **{revision['head']}**" in runbook_text
        assert f"predecessor **{revision['predecessor']}**" in runbook_text
        promoted = manifest["control_plane"]["promoted"]
        assert f"**v{promoted['release_version']}**" in runbook_text
        assert str(promoted["wheel_sha256"]) in runbook_text
        assert f"{manifest['harness']['binary']} {manifest['harness']['version']}" in runbook_text
        runner = manifest["runner"]
        assert f"id {runner['id']} `{runner['description']}`" in runbook_text
        # and the manifest is named normative — the table never substitutes
        # (whitespace-normalized: the document wraps lines)
        assert "The manifest is normative" in " ".join(runbook_text.split())

    def test_the_observation_template_carries_the_issues_observability_names(
        self, runbook_text: str
    ) -> None:
        template = _section(runbook_text, "## 7.", "## 8.")
        for name in (
            "onboarding.time_to_first_reviewable",
            "onboarding.manual_fixes",
            "qualification.artifact_identity",
            "qualification.support_decision",
        ):
            assert name in template, name
        # observed, not estimated — stated in the section itself
        assert "OBSERVED" in _section(runbook_text, "## 7.", "## 8.")

    def test_the_failure_table_is_a_contract_not_prose(self, runbook_text: str) -> None:
        """Every typed refusal the table documents appears VERBATIM in the
        tool's source — the runbook may never document a refusal the code
        does not produce."""
        table = _section(runbook_text, "## 6.", "## 7.")
        refusal_cells: list[str] = []
        for line in table.splitlines():
            if not line.strip().startswith("|") or set(line.strip()) <= {"|", "-", " "}:
                continue
            if "Symptom" in line:
                continue
            cells = line.split("|")
            if len(cells) >= 3:
                refusal_cells.append(cells[2])
        phrases = [span for cell in refusal_cells for span in re.findall(r"`([^`]+)`", cell)]
        assert len(phrases) >= 12, "the failure table lost its rows"
        source = (ROOT / "scripts" / "cold_install_check.py").read_text(encoding="utf-8")
        for phrase in phrases:
            assert phrase in source, (
                f"the documented typed refusal {phrase!r} does not appear in "
                "scripts/cold_install_check.py — the table and the tool disagree"
            )

    def test_the_honest_boundary_is_stated(self, runbook_text: str) -> None:
        assert "ready for the second engineer" in runbook_text
        assert "not" in runbook_text and "second-engineer-verified" in runbook_text
        assert "human_support_approval" in runbook_text  # the support decision stays separate
        assert "pending" in _section(runbook_text, "## 10.", "")


# ---------------------------------------------------------------------------
# The negative arms: typed refusals BEFORE the unsafe or paid action
# ---------------------------------------------------------------------------


class TestNegativeArmMissingPermission:
    def test_a_403_probe_refuses_typed_before_anything(self) -> None:
        findings = credentials_preflight_findings(
            CredentialsObservation(token_present=True, projects_probe_http_status=403)
        )
        refusals = preflight_refusals(findings)
        assert refusals and "MISSING PERMISSION" in refusals[0]
        assert "BEFORE the first project is created or any model call" in refusals[0]

    def test_no_token_at_all_refuses_the_same_way(self) -> None:
        findings = credentials_preflight_findings(CredentialsObservation(token_present=False))
        assert preflight_refusals(findings)
        assert "MISSING PERMISSION" in findings[0].detail

    def test_a_working_token_matches(self) -> None:
        findings = credentials_preflight_findings(
            CredentialsObservation(token_present=True, projects_probe_http_status=200)
        )
        assert findings[0].severity == "match"
        assert not preflight_refusals(findings)

    def test_an_unobservable_probe_is_a_refusal_never_a_silent_skip(self) -> None:
        findings = credentials_preflight_findings(
            CredentialsObservation(token_present=True, projects_probe_http_status=0)
        )
        assert preflight_refusals(findings)
        assert "unverifiable permission" in findings[0].detail


class TestNegativeArmUnavailableRunner:
    def test_an_offline_runner_refuses_before_the_paid_dispatch(
        self, manifest: dict[str, Any]
    ) -> None:
        findings = runner_availability_findings(observed_status="offline", manifest=manifest)
        refusals = preflight_refusals(findings)
        assert refusals and "RUNNER UNAVAILABLE" in refusals[0]
        assert "BEFORE dispatching the paid lane job" in refusals[0]
        # the PINNED runner is named — never a generic one
        assert str(manifest["runner"]["id"]) in refusals[0]
        assert str(manifest["runner"]["description"]) in refusals[0]

    def test_the_online_pinned_runner_matches(self, manifest: dict[str, Any]) -> None:
        findings = runner_availability_findings(
            observed_status=str(manifest["runner"]["observed_status"]), manifest=manifest
        )
        assert findings[0].severity == "match"
        assert not preflight_refusals(findings)


class TestNegativeArmIncompatibleSchema:
    def test_an_n2_database_refuses_before_the_migration_runs(
        self, manifest: dict[str, Any]
    ) -> None:
        n2 = revision_two_behind(manifest)
        head = str(manifest["control_plane"]["schema_revision"]["head"])
        predecessor = str(manifest["control_plane"]["schema_revision"]["predecessor"])
        assert n2 < predecessor < head  # genuinely N-2 along the chain
        findings = schema_compatibility_findings(observed_head=n2, manifest=manifest)
        refusals = preflight_refusals(findings)
        assert refusals and "INCOMPATIBLE SCHEMA" in refusals[0]
        assert "BEFORE the migration runs" in refusals[0]
        assert n2 in refusals[0] and head in refusals[0]

    def test_the_declared_predecessor_is_the_one_step_supported_path(
        self, manifest: dict[str, Any]
    ) -> None:
        predecessor = str(manifest["control_plane"]["schema_revision"]["predecessor"])
        findings = schema_compatibility_findings(observed_head=predecessor, manifest=manifest)
        assert findings[0].severity == "match"
        assert "one-step upgrade" in findings[0].detail

    def test_the_pinned_head_needs_no_migration(self, manifest: dict[str, Any]) -> None:
        head = str(manifest["control_plane"]["schema_revision"]["head"])
        findings = schema_compatibility_findings(observed_head=head, manifest=manifest)
        assert findings[0].severity == "match"

    def test_a_foreign_head_refuses_too(self, manifest: dict[str, Any]) -> None:
        findings = schema_compatibility_findings(observed_head="099", manifest=manifest)
        assert preflight_refusals(findings)
        assert "INCOMPATIBLE SCHEMA" in findings[0].detail


# ---------------------------------------------------------------------------
# The oracle replay: the independent oracle FROM the installed artifacts
# ---------------------------------------------------------------------------


class TestOracleReplay:
    def _gold(self, tmp_path: Path) -> Path:
        gold = tmp_path / "gold-project"
        gold.mkdir()
        for rel, content in sorted(
            _seed_files(load_manifest(MANIFEST_PATH), "oracle-test").items()
        ):
            target = gold / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return gold

    def test_the_heredoc_extracts_verbatim_and_passes_on_the_gold_state(
        self, manifest: dict[str, Any], tmp_path: Path
    ) -> None:
        rendered = render_target_template(manifest)
        script = extract_smoke_oracle_script(rendered)
        # the extraction is the job's OWN text: the manifest's exact cases
        for text, _expected in manifest["verification_contract"]["slugify_cases"]:
            assert f'("{text}",' in script
        gold = self._gold(tmp_path)
        completed = subprocess.run(
            [sys.executable, "-"], cwd=gold, input=script, text=True, capture_output=True
        )
        cases = len(manifest["verification_contract"]["slugify_cases"])
        assert completed.returncode == 0, completed.stderr
        assert f"slugify oracle: {cases}/{cases} OK" in completed.stdout
        assert "shape oracle: app rewired, legacy deleted" in completed.stdout

    def test_the_oracle_bites_on_a_tampered_state(
        self, manifest: dict[str, Any], tmp_path: Path
    ) -> None:
        gold = self._gold(tmp_path)
        # un-delete the legacy module and rewire the app back onto it
        (gold / "src" / "utils" / "legacy.py").write_text(
            "def shout(t: str) -> str:\n    return t.upper()\n", encoding="utf-8"
        )
        (gold / "src" / "app.py").write_text(
            "from utils.legacy import shout\n\n\ndef greet(name):\n    return shout(name)\n",
            encoding="utf-8",
        )
        script = extract_smoke_oracle_script(render_target_template(manifest))
        completed = subprocess.run(
            [sys.executable, "-"], cwd=gold, input=script, text=True, capture_output=True
        )
        assert completed.returncode != 0
        assert "legacy" in completed.stderr

    def test_extraction_refuses_a_ci_without_the_heredoc(self) -> None:
        with pytest.raises(cold_install_check.CheckRefused, match="oracle heredoc"):
            extract_smoke_oracle_script("smoke:\n  script: echo hi\n")


# ---------------------------------------------------------------------------
# The boundary: counted, never executed
# ---------------------------------------------------------------------------


class _FakeShell(cold_install_check.ShellProbe):
    """Records every invocation; answers nothing real."""

    def __init__(self) -> None:
        self.invocations: list[tuple[str, ...]] = []

    def run(self, command, *, cwd=None, env=None, input_text=None):  # type: ignore[no-untyped-def]
        from types import SimpleNamespace

        self.invocations.append(tuple(str(word) for word in command))
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class TestTheHonestBoundary:
    def test_human_and_lab_steps_never_execute(self, manifest: dict[str, Any]) -> None:
        runbook = (
            "# kit\n\n```bash\n"
            "# forge-step: one-human | human\n"
            "# forge-expects: a human fills the report\n"
            "echo should-not-run\n"
            "```\n\n```bash\n"
            "# forge-step: one-lab | lab\n"
            "# forge-blocked: needs the shared lab\n"
            "# forge-expects: the live trace\n"
            "echo also-not-run\n"
            "```\n"
        )
        path = Path("/tmp/forge-runbook-boundary-test.md")
        path.write_text(runbook, encoding="utf-8")
        try:
            findings, receipt = cold_install_check.run_from_runbook(
                manifest, _FakeShell(), runbook_path=path
            )
        finally:
            path.unlink(missing_ok=True)
        assert [f.severity for f in findings if f.axis.startswith("runbook.human_step")] == [
            "human"
        ]
        assert [f.severity for f in findings if f.axis.startswith("runbook.lab_step")] == [
            "lab-blocked"
        ]
        assert receipt["counts"] == {"machine": 0, "human": 1, "lab": 1}
        # and the zero-machine-step runbook honestly refuses its own emptiness
        assert any("ZERO machine steps" in f.detail for f in findings)

    def test_an_unregistered_machine_step_is_a_refusal(self, manifest: dict[str, Any]) -> None:
        runbook = (
            "```bash\n"
            "# forge-step: never-registered | machine\n"
            "# forge-expects: something no assertion checks\n"
            "true\n"
            "```\n"
        )
        path = Path("/tmp/forge-runbook-unregistered-test.md")
        path.write_text(runbook, encoding="utf-8")
        try:
            findings, _receipt = cold_install_check.run_from_runbook(
                manifest, _FakeShell(), runbook_path=path
            )
        finally:
            path.unlink(missing_ok=True)
        refusals = preflight_refusals(findings)
        assert any("NO observable assertion registered" in r for r in refusals)

    def test_the_four_arms_are_the_documented_set(self) -> None:
        assert NEGATIVE_ARMS == (
            "missing-permission",
            "unavailable-runner",
            "stale-wheel",
            "incompatible-schema-n2",
        )


# ---------------------------------------------------------------------------
# The restore rehearsal helper (referenced machinery, disposable fixtures)
# ---------------------------------------------------------------------------


class TestRestoreRehearsalHelper:
    def test_both_drills_pass_with_the_zero_model_turn_ordering(
        self, manifest: dict[str, Any], tmp_path: Path
    ) -> None:
        import cold_install_restore_rehearsal as rehearsal

        report = asyncio.run(rehearsal._rehearse(manifest, tmp_path / "work"))
        outcomes = {str(d["drill"]): str(d["outcome"]) for d in report["drills"]}
        assert outcomes["backup_restore"] == "pass"
        assert outcomes["deployment_mismatched_restore_preflight"] == "pass"
        gate = report["restore_gate"]
        assert gate["model_turns_before_refusals"] == 0
        assert gate["model_turns_after_consistent_restore"] == 1
        assert gate["restored_verified"] is True
        assert gate["refusals"] == {"backup-halves": True, "schema-head": True}
        # the binding row names its source honestly — the manifest's
        # RECORDED observation, never presented as a live probe
        assert "RECORDED" in report["profile_binding_source"]
        assert report["mismatched_schema_head"] == revision_two_behind(manifest)


# ---------------------------------------------------------------------------
# The stale-wheel arm's pure gate stays wired to the executed arm
# ---------------------------------------------------------------------------


class TestStaleWheelGate:
    def test_the_mutable_tag_refusal_text_matches_the_runbook(
        self, manifest: dict[str, Any], runbook_text: str
    ) -> None:
        finding = check_wheel_identity(
            expected_sha256=str(manifest["lane"]["wheel"]["sha256"]),
            actual_sha256="e" * 64,
            filename=str(manifest["lane"]["wheel"]["name"]),
            version=str(manifest["lane"]["wheel"]["version"]),
        )
        assert finding.severity == "refusal"
        assert "a DIFFERENT wheel under the SAME version string" in finding.detail
        assert "refusing BEFORE anything installs or executes" in finding.detail
        # the same phrases the failure table documents (§6) — checked here
        # against the LIVE function output, not just the source text
        table = _section(runbook_text, "## 6.", "## 7.")
        assert "a DIFFERENT wheel under the SAME version string" in table
        assert "refusing BEFORE anything installs or executes" in table

    def test_the_mismatched_template_phrase_matches_the_runbook(
        self, manifest: dict[str, Any], runbook_text: str
    ) -> None:
        working_tree = (ROOT / "ci/templates/claude-sdk-lane.gitlab-ci.yml").read_text("utf-8")
        drifted = working_tree.replace("forge-agent-claude-sdk:", "forge-agent-claude-sdk-x:", 1)
        findings = template_preflight_findings(
            rendered_ci_yaml=drifted, manifest=manifest, template_source=drifted
        )
        assert any("a MISMATCHED template" in f.detail for f in findings if f.severity == "refusal")
        assert "a MISMATCHED template" in _section(runbook_text, "## 6.", "## 7.")


def test_the_pinned_wheel_bytes_still_exist_and_hash(manifest: dict[str, Any]) -> None:
    """The kit's committed qualification bytes: present and reproducing the
    pin (a rebuild of a MOVED tree must refuse — that arm is above)."""
    wheel = ROOT / str(manifest["lane"]["wheel"]["path"])
    assert wheel.is_file(), f"{wheel} is absent — the kit's M3/M4 steps cannot run"
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == str(
        manifest["lane"]["wheel"]["sha256"]
    )
