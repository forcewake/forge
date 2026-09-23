"""Stage D: the proposal-only template contract (ADR-0016 §1).

The lab templates are the trust boundary made visible: a harness lane must
never carry a write credential, never commit, never push — its only
deliverable is the candidate artifact. These tests grep the shipped
templates so a regression (a reintroduced `git push`, a write token in the
lane) fails CI instead of a live run.
"""

import json
import re
from pathlib import Path

import pytest
import yaml

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "ci" / "templates"

HARNESS_TEMPLATES = (
    "grok.gitlab-ci.yml",
    "claude-code.gitlab-ci.yml",
    "opencode.gitlab-ci.yml",
    "copilot.gitlab-ci.yml",
    "claude-sdk-lane.gitlab-ci.yml",
    "codex-sdk-lane.gitlab-ci.yml",
    "opencode-sdk-lane.gitlab-ci.yml",
    "dotnet-lane.gitlab-ci.yml",
    "copilot-sdk-lane.gitlab-ci.yml",
)

# A write token must never appear, but FORGE_BOT_READ_TOKEN (the read-only
# convention) must: the negative regex below excludes it via the READ_
# infix.
_WRITE_TOKEN_RE = re.compile(r"FORGE_BOT_(?!READ_TOKEN)TOKEN")

# R5: the mechanical deny rules legitimately NAME the forbidden commands —
# naming them is the enforcement. A violation is a line that carries the
# command WITHOUT a deny marker on the same line (an actual invocation, as
# opposed to a `--deny`/`--disallowedTools`/`"deny"` rule or a bare
# `set-url --push` flag).
_DENY_MARKER_RE = re.compile(r'--disallowedTools|--deny\b|"deny"')


def _invocations_of(text: str, command: str) -> list[str]:
    """Lines that reference *command* outside any deny-rule context."""
    return [
        line for line in text.splitlines() if command in line and not _DENY_MARKER_RE.search(line)
    ]


@pytest.fixture(params=HARNESS_TEMPLATES)
def template_text(request) -> str:
    return (TEMPLATES_DIR / request.param).read_text()


def _lane_job(parsed: dict) -> tuple[str, dict]:
    """The lane job (name, body) — exactly one ``forge-agent*`` job.

    LIVE-found: every SDK-lane template names its job
    ``forge-agent-<driver>`` — a SHARED name meant GitLab's
    last-include-wins silently replaced the other lanes' jobs in repos
    including several templates.
    """
    keys = [key for key in parsed if key.startswith("forge-agent")]
    assert len(keys) == 1, f"expected exactly one forge-agent* job, got {keys}"
    return keys[0], parsed[keys[0]]


@pytest.fixture(params=HARNESS_TEMPLATES)
def template_doc(request) -> dict:
    parsed = yaml.safe_load((TEMPLATES_DIR / request.param).read_text())
    assert isinstance(parsed, dict)
    _, body = _lane_job(parsed)
    return body


class TestNoWriteCapability:
    def test_no_git_push_anywhere(self, template_text):
        assert _invocations_of(template_text, "git push") == []

    def test_no_git_commit_anywhere(self, template_text):
        # The lane stages and diffs; committing is the publisher's job.
        assert _invocations_of(template_text, "git commit") == []

    def test_no_write_token_variable(self, template_text):
        assert not _WRITE_TOKEN_RE.search(template_text)

    def test_push_url_is_disabled(self, template_text):
        assert "git remote set-url --push origin FORBIDDEN" in template_text

    def test_read_only_token_convention_documented(self, template_text):
        assert "FORGE_BOT_READ_TOKEN" in template_text


class TestCandidateContract:
    def test_detached_checkout_of_frozen_attempt_base(self, template_text):
        assert 'git checkout --detach "$FORGE_ATTEMPT_BASE"' in template_text

    def test_candidate_diff_against_attempt_base(self, template_text):
        assert (
            'git diff --cached --binary --full-index "$FORGE_ATTEMPT_BASE" '
            "> .forge/candidate.diff" in template_text
        )

    def test_meta_json_is_written(self, template_text):
        assert ".forge/candidate.meta.json" in template_text
        assert "attempt_base" in template_text
        assert '"exit"' in template_text

    def test_artifacts_declared_and_always_uploaded(self, template_doc):
        artifacts = template_doc["artifacts"]
        assert set(artifacts["paths"]) == {
            ".forge/candidate.diff",
            ".forge/candidate.meta.json",
        }
        assert artifacts["when"] == "always"  # failures upload too

    def test_forge_candidate_marker_printed(self, template_text):
        assert "FORGE_CANDIDATE:" in template_text
        assert '"artifact": "candidate.diff"' in template_text

    def test_legacy_forge_result_line_kept_for_migration(self, template_text):
        # v0.2 → v0.3 migration window only; it carries the attempt base,
        # not a branch claim.
        line = next(
            (ln for ln in template_text.splitlines() if "FORGE_RESULT:" in ln and "printf" in ln),
            "",
        )
        assert 'FORGE_RESULT:{"head": "%s"' in line.replace('\\"', '"')
        assert "$FORGE_ATTEMPT_BASE" in line

    def test_control_dir_excluded_from_the_index(self, template_text):
        assert 'echo ".forge/" >> .git/info/exclude' in template_text


class TestDriverPrompts:
    def test_driver_told_not_to_commit_or_push(self, template_text):
        assert "Do NOT commit and do NOT push" in template_text


class TestMechanicalDeny:
    """R5: "never commit/push" is enforced by the driver, not just asked.

    Each shipped template must carry its driver's deny construct so the
    contract holds even if the model disobeys the brief or a vendor default
    changes.
    """

    def test_claude_disallowed_tools(self):
        text = (TEMPLATES_DIR / "claude-code.gitlab-ci.yml").read_text()
        assert "--disallowedTools" in text
        assert "Bash(git commit:*)" in text and "Bash(git push:*)" in text

    def test_grok_deny_rules(self):
        text = (TEMPLATES_DIR / "grok.gitlab-ci.yml").read_text()
        assert "--deny 'Bash(git commit:*)'" in text
        assert "--deny 'Bash(git push:*)'" in text

    def test_opencode_permission_map_deny(self):
        text = (TEMPLATES_DIR / "opencode.gitlab-ci.yml").read_text()
        assert '"git commit *": "deny"' in text
        assert '"git push *": "deny"' in text

    def test_copilot_deny_tool_rules(self):
        text = (TEMPLATES_DIR / "copilot.gitlab-ci.yml").read_text()
        assert "--deny-tool 'shell(git commit)'" in text
        assert "--deny-tool 'shell(git push)'" in text
        # Scoped grants: reads, writes and read-only git only — everything
        # else is auto-denied in -p mode (no prompt, no hang).
        assert "--allow-tool 'read,write'" in text
        assert "--allow-tool 'shell(git:*)'" in text

    def test_opencode_headless_hang_sources_allowed(self):
        # `external_directory` and `doom_loop` default to "ask" — an
        # unattended lane that hits one of them hangs forever (R5).
        text = (TEMPLATES_DIR / "opencode.gitlab-ci.yml").read_text()
        assert '"external_directory": "allow"' in text
        assert '"doom_loop": "allow"' in text

    def test_claude_no_prompt_guarantee_and_turn_budget(self):
        text = (TEMPLATES_DIR / "claude-code.gitlab-ci.yml").read_text()
        assert "--permission-prompts none" in text
        assert "--max-turns 200" in text
        assert "API_TIMEOUT_MS" in text  # timeout budget (R5)

    def test_grok_trust_and_turn_budget(self):
        text = (TEMPLATES_DIR / "grok.gitlab-ci.yml").read_text()
        assert "--trust" in text  # project rules load headlessly (R5)
        assert "--max-turns 200" in text

    def test_actions_lane_mirrors_the_gitlab_contract(self):
        # forge.harness_entry renders the Actions-lane driver scripts (via
        # forge.harnesses.script_render and the package-data .sh templates
        # under src/forge/harnesses/scripts/); those render sources must
        # enforce the same mechanical deny posture.
        src = Path(__file__).resolve().parent.parent / "src" / "forge"
        entry = (src / "harnesses" / "script_render.py").read_text()
        entry += "\n".join(
            path.read_text()
            for path in sorted((src / "harnesses" / "scripts").rglob("*"))
            if path.is_file()
        )
        assert "--disallowedTools" in entry
        assert "Bash(git commit:*)" in entry
        assert "--deny 'Bash(git commit:*)'" in entry
        assert '"git commit *": "deny"' in entry
        assert '"external_directory": "allow"' in entry
        assert "--deny-tool 'shell(git commit)'" in entry
        assert '"copilot"' in entry or "copilot" in entry


class TestMcpProvisioning:
    """ADR-0022: every lane consumes FORGE_HARNESS_MCP in its driver's own
    dialect; claude's strict mode is the constant isolation baseline."""

    def test_claude_strict_mcp_config_always_on(self):
        text = (TEMPLATES_DIR / "claude-code.gitlab-ci.yml").read_text()
        assert "FORGE_HARNESS_MCP" in text
        assert "--mcp-config /tmp/forge-mcp.json --strict-mcp-config" in text
        assert "mcp__${n}__*" in text  # per-server tool grants

    def test_grok_writes_grok_settings(self):
        text = (TEMPLATES_DIR / "grok.gitlab-ci.yml").read_text()
        assert "FORGE_HARNESS_MCP" in text
        assert ".grok/settings.json" in text

    def test_copilot_writes_mcp_config(self):
        text = (TEMPLATES_DIR / "copilot.gitlab-ci.yml").read_text()
        assert "FORGE_HARNESS_MCP" in text
        assert ".copilot/mcp-config.json" in text

    def test_opencode_merges_translated_mcp(self):
        text = (TEMPLATES_DIR / "opencode.gitlab-ci.yml").read_text()
        assert "FORGE_HARNESS_MCP" in text
        assert '"remote"' in text  # http -> remote translation

    def test_actions_lane_passes_the_variable_through(self):
        text = (TEMPLATES_DIR / "forge-harness.github.yml").read_text()
        assert "FORGE_HARNESS_MCP" in text


class TestDriverFilter:
    """ADR-0023 §7: every shipped GitLab template carries its driver filter
    so a repo including MULTIPLE templates runs exactly one lane; a
    single-driver repo (FORGE_HARNESS_DRIVER unset) behaves as before via
    the `== ""` arm."""

    DRIVER_IDS = {
        "claude-code.gitlab-ci.yml": "claude-code",
        "grok.gitlab-ci.yml": "grok-build",
        "opencode.gitlab-ci.yml": "opencode",
        "copilot.gitlab-ci.yml": "copilot",
        "claude-sdk-lane.gitlab-ci.yml": "claude-sdk-lane",
        "codex-sdk-lane.gitlab-ci.yml": "codex-sdk-lane",
        "opencode-sdk-lane.gitlab-ci.yml": "opencode-sdk-lane",
        "dotnet-lane.gitlab-ci.yml": "dotnet-lane",
        "copilot-sdk-lane.gitlab-ci.yml": "copilot-sdk-lane",
    }

    def test_every_template_carries_its_own_filter(self, template_doc, request):
        template = request.node.callspec.params["template_doc"]
        expected = (
            f'$FORGE_RUN_ID && ($FORGE_HARNESS_DRIVER == "" '
            f'|| $FORGE_HARNESS_DRIVER == "{self.DRIVER_IDS[template]}")'
        )
        assert template_doc["rules"] == [{"if": expected}]

    def test_filter_keeps_the_forge_run_id_gate(self, template_doc):
        rule_if = template_doc["rules"][0]["if"]
        assert rule_if.startswith("$FORGE_RUN_ID && ")

    def test_unset_driver_still_selects_every_template(self, template_text):
        # Single-driver back-compat: the empty-driver arm of the rule.
        assert '$FORGE_HARNESS_DRIVER == ""' in template_text

    def test_filters_use_the_shipped_driver_ids_only(self):
        from forge.runs.harness_selection import SHIPPED_DRIVERS

        assert set(self.DRIVER_IDS.values()) == set(SHIPPED_DRIVERS)


class TestEventFilters:
    def test_universal_filter_emits_usage_receipts(self):
        text = (TEMPLATES_DIR / "harness-log-filter.mjs").read_text()
        assert "FORGE_USAGE:" in text
        assert "usage.json" in text
        assert 'completeness: "aggregate"' in text  # JS object-literal form

    def test_all_lanes_reference_the_universal_filter(self):
        for name in (
            "grok.gitlab-ci.yml",
            "claude-code.gitlab-ci.yml",
            "opencode.gitlab-ci.yml",
            "copilot.gitlab-ci.yml",
        ):
            text = (TEMPLATES_DIR / name).read_text()
            assert "harness-log-filter.mjs" in text, name

    def test_filters_write_the_usage_file_templates_read(self):
        for name in ("grok.gitlab-ci.yml", "claude-code.gitlab-ci.yml"):
            text = (TEMPLATES_DIR / name).read_text()
            assert ".forge/usage.json" in text, name


class TestDotnetLaneRecipe:
    """R28-21: the reproducible .NET recipe — one complete runtime lane,
    not a file-extension allowlist addition. The template pins every input
    (image digest, global.json, nuget.lock.json) and the render arm
    produces a valid script (npm pin → SDK check → dotnet test).

    NOTE: deliberately NOT using the parametrized ``template_doc`` /
    ``template_text`` fixtures — those sweep every shipped template; this
    class pins the one .NET lane file directly.
    """

    def _text(self) -> str:
        return (TEMPLATES_DIR / "dotnet-lane.gitlab-ci.yml").read_text()

    def _doc(self) -> dict:
        import yaml

        return yaml.safe_load(self._text())["forge-agent-dotnet"]

    def test_image_is_pinned_by_digest(self):
        image = self._doc()["image"]
        assert image.startswith("mcr.microsoft.com/dotnet/sdk:")
        assert "@sha256:" in image, "the lane image must be digest-pinned (R28-21)"

    def test_restore_and_build_run_locked_mode(self):
        text = self._text()
        assert "dotnet restore --locked-mode" in text
        assert "dotnet build --no-restore --locked-mode" in text

    def test_tests_emit_trx_the_verifier_reads(self):
        text = self._text()
        # NEXT-16: LogFilePrefix (a UNIQUE report per project/framework —
        # a fixed LogFileName let multi-target runs overwrite each other).
        assert '--logger "trx;LogFilePrefix=$FORGE_DOTNET_TRX_PREFIX"' in text
        assert self._doc()["variables"]["FORGE_DOTNET_TRX_PREFIX"] == "forge_"
        assert "FORGE_DOTNET_TRX:" not in text  # the fixed-name knob is gone
        assert "--results-directory .forge/testresults" in text
        # the AGGREGATED TRX counters land in the candidate meta (the
        # verifier's surface — docs/harnesses/dotnet-lane.md).
        assert '"verification"' in text
        assert "ResultSummary" in text and "Counters" in text
        assert "test_projects" in text and "total_passed" in text and "total_failed" in text

    def test_pinned_inputs_fail_closed_before_the_paid_call(self):
        text = self._text()
        # global.json and a committed lock file are prerequisites: the lane
        # refuses to run without them, BEFORE the agent burns a token.
        assert "global.json is required" in text
        assert "no nuget.lock.json found" in text
        assert "dotnet --version" in text

    def test_environment_failures_separated_from_test_failures(self):
        text = self._text()
        assert "FORGE_RESTORE_EXIT" in text
        assert "FORGE_BUILD_EXIT" in text
        assert "FORGE_TEST_EXIT" in text

    def test_render_arm_produces_a_valid_script(self):
        import shlex
        import shutil
        import subprocess

        from forge.harness_entry import render_driver_script
        from forge.harnesses.prompt import TASK_PROMPT

        script = render_driver_script("dotnet-lane", "glm-5.3-flash", ".forge/brief.md")
        # npm pin → SDK check → dotnet test, in order.
        assert "npm install -g --no-fund --no-audit @anthropic-ai/claude-code@" in script
        preamble = script.index("claude --version")
        assert script.index("dotnet --version") > preamble
        assert script.index("dotnet restore --locked-mode") > script.index("dotnet --version")
        assert "dotnet test --no-build --logger" in script
        # the agent keeps the claude-code unattended contract.
        assert shlex.quote(TASK_PROMPT) in script
        assert "tee -a .forge/events.jsonl" in script
        bash = shutil.which("bash")
        if bash is not None:
            proc = subprocess.run(
                [bash, "-n"], input=script.encode(), capture_output=True, check=False
            )
            assert proc.returncode == 0, proc.stderr.decode(errors="replace")

    def test_render_arm_records_the_trx_block(self):
        from forge.harness_entry import render_driver_script

        script = render_driver_script("dotnet-lane", "", ".forge/brief.md")
        assert ".forge/verify.json" in script
        # NEXT-16: the aggregated report contract (format v2 — the
        # single-report v1 shape is superseded).
        assert "dotnet-trx/2" in script
        assert "LogFilePrefix" in script
        # the mechanical deny rides this lane too (its agent is a CLI).
        assert '--disallowedTools "Bash(git commit:*)" "Bash(git push:*)"' in script


class TestDotnetTrxAggregation:
    """NEXT-16 — the collector aggregates EVERY ``forge_*.trx`` report.

    The review's finding: "the TRX collector takes the first found
    report; multi-project/multi-target tests overwrite." The lane now
    tests with ``--logger trx;LogFilePrefix=forge_`` (unique report
    names, nothing overwritten) and the collector aggregates the WHOLE
    identified population. These tests EXECUTE the real embedded
    collectors — the python3 heredoc inside the shipped GitLab template
    AND the one inside the rendered driver script — against golden TRX
    fixtures, so the aggregation behavior itself is pinned, not just its
    presence in the text.
    """

    FIXTURES = Path(__file__).resolve().parent / "fixtures" / "dotnet-trx"
    # The terminator may be indented (the YAML block scalar) or at column
    # zero (the .sh heredoc) — both shapes carry the same collector.
    _HEREDOC_RE = re.compile(r"python3 - <<'PYEOF'\n(.*?)\n[ ]*PYEOF\s*$", re.DOTALL | re.MULTILINE)

    def _template_heredoc(self) -> str:
        return self._extract(self._text_from_template())

    def _text_from_template(self) -> str:
        return (TEMPLATES_DIR / "dotnet-lane.gitlab-ci.yml").read_text()

    def _script_heredoc(self) -> str:
        from forge.harness_entry import render_driver_script

        return self._extract(render_driver_script("dotnet-lane", "", ".forge/brief.md"))

    def _extract(self, text: str) -> str:
        match = self._HEREDOC_RE.search(text)
        assert match is not None, "the dotnet lane must carry its python3 heredoc"
        import textwrap

        return textwrap.dedent(match.group(1))

    def _stage(
        self, tmp_path: Path, *fixture_names: str, legacy: str | None = None, junk: bool = False
    ) -> Path:
        import shutil

        results = tmp_path / ".forge" / "testresults"
        results.mkdir(parents=True)
        for name in fixture_names:
            shutil.copy(self.FIXTURES / name, results / name)
        if legacy is not None:
            (results / legacy).write_text(
                '<?xml version="1.0"?><TestRun xmlns='
                '"http://microsoft.com/schemas/VisualStudio/TeamTest/2010"/>'
            )
        if junk:
            (results / "forge_corrupt.trx").write_text("this is not xml at all <<<")
        return tmp_path

    def _run_collector(self, code: str, cwd: Path) -> None:
        import os
        import subprocess
        import sys

        proc = subprocess.run(
            [sys.executable, "-"],
            input=code,
            cwd=str(cwd),
            env={**os.environ, "FORGE_DOTNET_TRX_PREFIX": "forge_"},
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr

    def _collect(self, tmp_path: Path, *fixture_names: str, **kwargs) -> tuple[dict, dict]:
        """Run BOTH collectors (template → candidate.meta.json, script →
        verify.json) over the same staged results; return the two
        verification blocks."""
        outputs = []
        for index, (code, output) in enumerate(
            (
                (self._template_heredoc(), ".forge/candidate.meta.json"),
                (self._script_heredoc(), ".forge/verify.json"),
            )
        ):
            staged = self._stage(tmp_path / f"run-{index}", *fixture_names, **kwargs)
            self._run_collector(code, staged)
            document = json.loads((staged / output).read_text())
            outputs.append(document.get("verification", document))
        return outputs[0], outputs[1]

    def test_two_reports_are_both_parsed_counted_and_neither_dropped(self, tmp_path):
        # A two-project run (one green, one with a failure): BOTH reports
        # are attributed, counted and kept — the failing project cannot
        # disappear because the other report sorted first.
        template_block, script_block = self._collect(
            tmp_path,
            "forge_Api.Tests_net9.0.trx",
            "forge_Domain.Tests_net8.0.trx",
        )
        for verification in (template_block, script_block):
            assert verification["kind"] == "dotnet-trx/2"
            assert verification["test_projects"] == 2
            assert verification["total_passed"] == 19  # 12 + 7
            assert verification["total_failed"] == 1  # 0 + 1
            assert verification["reports_absent"] is False
            per_file = {entry["file"]: entry for entry in verification["projects"]}
            api = per_file[".forge/testresults/forge_Api.Tests_net9.0.trx"]
            domain = per_file[".forge/testresults/forge_Domain.Tests_net8.0.trx"]
            assert (api["passed"], api["failed"], api["outcome"]) == (12, 0, "Completed")
            assert (domain["passed"], domain["failed"], domain["outcome"]) == (7, 1, "Failed")
            assert api["sha256"] != domain["sha256"]  # per-report content pins
            assert len({entry["sha256"] for entry in verification["projects"]}) == 2

    def test_a_stale_fixed_name_report_is_visible_but_never_counted(self, tmp_path):
        # The negative case verbatim: "Leave an old forge.trx in the
        # results directory" — it is recorded as a stale legacy report and
        # stays OUT of the aggregate (it may predate this attempt).
        template_block, script_block = self._collect(
            tmp_path,
            "forge_Api.Tests_net9.0.trx",
            "forge_Domain.Tests_net8.0.trx",
            legacy="forge.trx",
        )
        for verification in (template_block, script_block):
            assert verification["test_projects"] == 2  # the legacy one is not counted
            assert verification["total_passed"] == 19
            assert verification["legacy_reports"] == [".forge/testresults/forge.trx"]

    def test_zero_reports_is_an_explicit_condition_never_success(self, tmp_path):
        # The negative case verbatim: "a test process that exits zero but
        # writes no required report" — absent reports are SAID, and the
        # counters stay honestly zero rather than inherited from anything.
        template_block, script_block = self._collect(tmp_path)
        for verification in (template_block, script_block):
            assert verification["test_projects"] == 0
            assert verification["total_passed"] == 0
            assert verification["total_failed"] == 0
            assert verification["reports_absent"] is True

    def test_an_unparseable_report_is_recorded_and_never_fatal(self, tmp_path):
        # One corrupted report must not eat the healthy one's verdict.
        template_block, script_block = self._collect(
            tmp_path, "forge_Api.Tests_net9.0.trx", junk=True
        )
        for verification in (template_block, script_block):
            assert verification["test_projects"] == 1
            assert verification["total_passed"] == 12
            assert [failure["file"] for failure in verification["parse_failures"]] == [
                ".forge/testresults/forge_corrupt.trx"
            ]

    def test_a_failing_project_cannot_disappear_behind_a_passing_one(self, tmp_path):
        # Sorted-first is the PASSING report: the old collector (first
        # match wins) would have reported zero failures for this run.
        template_block, script_block = self._collect(
            tmp_path,
            "forge_Api.Tests_net9.0.trx",
            "forge_Domain.Tests_net8.0.trx",
        )
        for verification in (template_block, script_block):
            assert verification["total_failed"] == 1
