"""harness_entry tests (E3b): the Actions lane's driver runner.

The rendered scripts are asserted against the contract the GitLab templates
establish (``ci/templates/*.gitlab-ci.yml``; interface ground truth:
``docs/research/harness-interfaces.md``) — same flags, same unattended
posture, same hardened grok preamble. The subprocess is exercised end-to-end
with a fake driver script, no CLIs and no network.
"""

import json
from pathlib import Path

import pytest

from forge.harness_entry import DRIVERS, main, parse_usage, render_driver_script

BRIEF = ".forge/brief.md"


# ----------------------------------------------------------------------
# Per-driver script rendering
# ----------------------------------------------------------------------


class TestRenderClaudeCode:
    def test_script_follows_the_unattended_contract(self):
        script = render_driver_script("claude-code", "glm-5.3-flash[1m]", BRIEF)

        assert "claude -p " in script  # headless print mode
        assert "--model 'glm-5.3-flash[1m]'" in script
        assert "--permission-mode acceptEdits" in script  # edits auto-accepted
        assert "--setting-sources ''" in script  # no external settings load
        assert "--output-format stream-json" in script  # normalized event stream
        assert "--allowedTools" in script and "Bash(git status:*)" in script  # git-only shell
        assert BRIEF in script

    def test_empty_model_omits_the_model_flag(self):
        script = render_driver_script("claude-code", "", BRIEF)

        assert "--model" not in script


class TestRenderGrokBuild:
    def test_script_installs_the_platform_binary_explicitly(self):
        """The wrapper declares the platform binary as an optionalDependency:
        a flaky registry silently skips it and the CLI hangs forever — both
        packages are installed explicitly, with retries (verified live)."""
        script = render_driver_script("grok-build", "grok-4.6", BRIEF)

        assert "npm install -g --no-fund --no-audit @xai-official/grok" in script
        assert "@xai-official/grok-linux-x64@${GROK_VER}" in script
        assert "for attempt in 1 2 3" in script  # retries
        assert "test -d /usr/local/lib/node_modules/@xai-official/grok-linux-x64" in script

    def test_script_follows_the_unattended_contract(self):
        script = render_driver_script("grok-build", "grok-4.6", BRIEF)

        assert "grok --no-auto-update --always-approve --no-alt-screen" in script
        # --always-approve is REQUIRED: headless grok hangs without it.
        assert "--output-format streaming-json" in script
        assert "--debug-file .forge/grok-debug.log" in script
        assert "-p " in script


class TestRenderOpencode:
    def test_script_uses_auto_approval(self):
        script = render_driver_script("opencode", "zai/glm-5.3-flash", BRIEF)

        assert "opencode run --auto " in script  # unattended flag
        assert BRIEF in script
        # The model is config-owned on this driver (opencode.json), not a
        # CLI flag — mirror of the GitLab template.
        assert "--model" not in script


class TestRenderCommon:
    def test_every_driver_keeps_the_audit_trail_streaming(self):
        """The event log is tee'd so a failed driver still leaves its stream
        for the artifact step (the workflow uploads ``if: always()``)."""
        for driver in DRIVERS:
            script = render_driver_script(driver, "m", BRIEF)
            assert ".forge/events.jsonl" in script
            assert "tee -a" in script

    def test_unknown_driver_is_rejected(self):
        with pytest.raises(ValueError, match="unknown driver"):
            render_driver_script("codex", "", BRIEF)

    def test_brief_paths_are_shell_quoted(self):
        """The prompt embedding the brief path is quoted as one shell word
        (drivers that interpolate the path — claude-code does)."""
        script = render_driver_script("claude-code", "", "/tmp/wei rd brief.md")

        assert "'/tmp/wei rd brief.md" in script or "-p '/tmp/wei rd brief.md" in script


# ----------------------------------------------------------------------
# Usage receipt aggregation (unknown stays unknown, never zero)
# ----------------------------------------------------------------------


class TestParseUsage:
    def test_claude_per_turn_receipts_are_summed(self):
        log = (
            '{"type":"system","subtype":"init"}\n'
            '{"type":"assistant","message":{}}\n'
            '{"type":"result","usage":{"input_tokens":100,'
            '"cache_read_input_tokens":50,"output_tokens":20}}\n'
            '{"type":"result","usage":{"input_tokens":30,"output_tokens":10}}\n'
        )

        usage = parse_usage("claude-code", log)

        assert usage == {
            "input_tokens": 130,
            "cached_input_tokens": 50,
            "output_tokens": 30,
            "driver": "claude-code",
            "completeness": "aggregate",
            "source": "stream-json",
        }

    def test_grok_end_aggregate_wins_over_per_response_events(self):
        log = (
            '{"type":"usage","usage":{"input_tokens":100,"output_tokens":20}}\n'
            '{"type":"usage","usage":{"input_tokens":10,"output_tokens":5}}\n'
            '{"type":"end","usage":{"input_tokens":110,"output_tokens":25}}\n'
        )

        usage = parse_usage("grok-build", log)

        assert usage["input_tokens"] == 110  # NOT 110+110 — no double counting
        assert usage["output_tokens"] == 25

    def test_opencode_has_no_receipt(self):
        assert parse_usage("opencode", '{"type":"message","part":{}}\n') is None

    def test_garbage_lines_are_skipped(self):
        usage = parse_usage("claude-code", "not json\n{broken\n")

        assert usage is None

    def test_tokens_that_never_appear_stay_none(self):
        usage = parse_usage("claude-code", '{"type":"result","usage":{}}\n')

        assert usage is None


# ----------------------------------------------------------------------
# main(): the CLI contract
# ----------------------------------------------------------------------


@pytest.fixture()
def lane(tmp_path: Path) -> Path:
    """A workspace shaped like the Actions lane: brief + control dir."""
    (tmp_path / ".forge").mkdir()
    (tmp_path / BRIEF).write_text("# the plan\n")
    return tmp_path


def run_main(lane: Path, monkeypatch: pytest.MonkeyPatch, driver_script: str, **kwargs):
    """Run main() inside *lane* with *driver_script* as the rendered script."""
    monkeypatch.chdir(lane)
    monkeypatch.setattr("forge.harness_entry.render_driver_script", lambda *a, **k: driver_script)
    argv = ["--exit-file", ".forge/exit"]
    for name, value in kwargs.items():
        argv += [f"--{name}", value]
    return main(argv)


class TestMain:
    def test_successful_driver_writes_completed_and_usage(self, lane, monkeypatch):
        script = (
            "echo 'some agent output' >&2\n"
            "printf '%s\\n' "
            '\'{"type":"result","usage":{"input_tokens":42,"output_tokens":7}}\' '
            ">> .forge/events.jsonl\n"
        )

        rc = run_main(lane, monkeypatch, script, driver="claude-code", model="m", brief=BRIEF)

        assert rc == 0
        assert (lane / ".forge" / "exit").read_text().strip() == "completed"
        usage = json.loads((lane / ".forge" / "usage.json").read_text())
        assert usage["input_tokens"] == 42
        assert usage["completeness"] == "aggregate"

    def test_failing_driver_writes_failed_but_still_exits_zero(self, lane, monkeypatch):
        """The lane's audit trail outranks the step's color: the workflow's
        candidate steps run ``if: always()`` and forge classifies from the
        meta exit — the GitLab templates' exact contract."""
        script = "echo 'agent exploded' >&2\nexit 3\n"

        rc = run_main(lane, monkeypatch, script, driver="claude-code", brief=BRIEF)

        assert rc == 0
        assert (lane / ".forge" / "exit").read_text().strip() == "failed"

    def test_missing_brief_fails_before_running_anything(self, lane, monkeypatch):
        rc = run_main(
            lane, monkeypatch, "echo should never run", driver="claude-code", brief=".forge/nope.md"
        )

        assert rc == 1
        assert (lane / ".forge" / "exit").read_text().strip() == "failed"

    def test_unknown_driver_fails_before_running_anything(self, lane, monkeypatch):
        rc = run_main(lane, monkeypatch, "echo should never run", driver="codex", brief=BRIEF)

        assert rc == 1
        assert (lane / ".forge" / "exit").read_text().strip() == "failed"

    def test_env_fallbacks_mirror_the_workflow_template(self, lane, monkeypatch):
        """The template passes FORGE_DRIVER / FORGE_MODEL / FORGE_BRIEF /
        FORGE_EXIT_FILE as env — the CLI must accept those names."""

        monkeypatch.chdir(lane)
        monkeypatch.setenv("FORGE_DRIVER", "opencode")
        monkeypatch.setenv("FORGE_MODEL", "zai/glm")
        monkeypatch.setenv("FORGE_BRIEF", BRIEF)
        monkeypatch.setenv("FORGE_EXIT_FILE", ".forge/exit")
        monkeypatch.setattr("forge.harness_entry.render_driver_script", lambda *a, **k: "true")

        rc = main([])

        assert rc == 0
        assert (lane / ".forge" / "exit").read_text().strip() == "completed"
