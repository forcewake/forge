"""Stage D: the proposal-only template contract (ADR-0016 §1).

The lab templates are the trust boundary made visible: a harness lane must
never carry a write credential, never commit, never push — its only
deliverable is the candidate artifact. These tests grep the shipped
templates so a regression (a reintroduced `git push`, a write token in the
lane) fails CI instead of a live run.
"""

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


@pytest.fixture(params=HARNESS_TEMPLATES)
def template_doc(request) -> dict:
    parsed = yaml.safe_load((TEMPLATES_DIR / request.param).read_text())
    assert isinstance(parsed, dict) and "forge-agent" in parsed
    return parsed["forge-agent"]


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
        # forge.harness_entry renders the Actions-lane driver scripts; it
        # must enforce the same mechanical deny posture.
        entry = (
            Path(__file__).resolve().parent.parent / "src" / "forge" / "harness_entry.py"
        ).read_text()
        assert "--disallowedTools" in entry
        assert "Bash(git commit:*)" in entry
        assert "--deny 'Bash(git commit:*)'" in entry
        assert '"git commit *": "deny"' in entry
        assert '"external_directory": "allow"' in entry
        assert "--deny-tool 'shell(git commit)'" in entry
        assert '"copilot"' in entry or "copilot" in entry


class TestEventFilters:
    def test_filters_emit_usage_receipts(self):
        for name in ("grok-events-filter.mjs", "claude-events-filter.mjs"):
            text = (TEMPLATES_DIR / name).read_text()
            assert "FORGE_USAGE:" in text, name
            assert "usage.json" in text, name
            assert '"completeness": "aggregate"' in text or 'completeness: "aggregate"' in text, (
                name
            )

    def test_filters_write_the_usage_file_templates_read(self):
        for name in ("grok.gitlab-ci.yml", "claude-code.gitlab-ci.yml"):
            text = (TEMPLATES_DIR / name).read_text()
            assert ".forge/usage.json" in text, name
