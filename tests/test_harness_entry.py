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

from forge.harness_entry import (
    DRIVERS,
    main,
    parse_usage,
    render_brief,
    render_driver_script,
)

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

    def test_the_prompt_is_the_short_shared_pointer(self):
        """The quality lives in the brief file; the -p prompt only points at
        it (forge.harnesses.prompt.TASK_PROMPT), identically for every
        driver."""
        import shlex

        from forge.harnesses.prompt import TASK_PROMPT

        for driver in DRIVERS:
            script = render_driver_script(driver, "m", BRIEF)
            assert shlex.quote(TASK_PROMPT) in script
            assert "brief.md" in script  # the pointer names the brief


# ----------------------------------------------------------------------
# The shared brief builder (single source for both lanes)
# ----------------------------------------------------------------------


class TestRenderBrief:
    def test_brief_carries_role_constraints_and_contract(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        brief = render_brief("Users cannot reset their password.", "1. Add the endpoint.\n")

        assert "## Role" in brief and "staff engineer" in brief
        assert "1. Add the endpoint." in brief  # plan verbatim
        assert "Users cannot reset their password." in brief
        assert "NEVER create or modify" in brief  # denied paths
        assert "working tree" in brief  # ci_lane output contract
        assert "NOT** commit" in brief

    def test_skills_line_names_conventions_files_present_in_the_workspace(
        self, tmp_path: Path, monkeypatch
    ):
        """Fixture-level skills check: a workspace containing AGENTS.md → the
        brief instructs the agent to read it (Claude reads CLAUDE.md the
        same way)."""
        monkeypatch.chdir(tmp_path)

        bare = render_brief("body", "plan")
        assert "if `AGENTS.md` or `CLAUDE.md` exists" in bare  # conditional form

        (tmp_path / "AGENTS.md").write_text("# conventions\n")
        with_agents = render_brief("body", "plan")
        assert "`AGENTS.md`" in with_agents
        assert "read" in with_agents and "follow" in with_agents

    def test_render_brief_mode_fetches_issue_and_plan_and_writes_the_brief(
        self, tmp_path: Path, monkeypatch
    ):
        """--render-brief: the lane fetches its own brief content (issue body
        + the forge plan comment) with the read-only runner token — no
        dispatch-input size limit ever binds."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GITHUB_REPOSITORY", "acme/acme-widget")
        monkeypatch.setenv("GITHUB_TOKEN", "ghs_runner")  # noqa: S105 — fake
        monkeypatch.setenv("FORGE_ISSUE_NUMBER", "42")

        async def _noop_async():
            return None

        def fake_fetch(repo: str, issue_number: int, token: str):
            assert (repo, issue_number, token) == ("acme/acme-widget", 42, "ghs_runner")
            return ("Users cannot reset their password.", "## Forge plan — run `abcd`\n...")

        monkeypatch.setattr("forge.harness_entry.fetch_issue_context", fake_fetch)

        rc = main(["--render-brief"])

        assert rc == 0
        brief = (tmp_path / ".forge" / "brief.md").read_text()
        assert "## Approved plan" in brief
        assert "## Forge plan — run `abcd`" in brief  # the plan, verbatim
        assert "Users cannot reset their password." in brief

    def test_render_brief_mode_requires_repo_issue_and_token(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("FORGE_ISSUE_NUMBER", raising=False)

        rc = main(["--render-brief", "--exit-file", ".forge/exit"])

        assert rc == 1
        assert (tmp_path / ".forge" / "exit").read_text().strip() == "failed"


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
