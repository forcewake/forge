"""Shared harness brief tests (scope extension): forge.harnesses.prompt.

The brief is THE prompt both lanes render — role/task/constraints/quality
bar/output contract, the plan verbatim with its digest, lane-specific
output contracts, and the skills/conventions direction (AGENTS.md /
CLAUDE.md present in the workspace must be named).
"""

from pathlib import Path

import pytest

from forge.harnesses.prompt import (
    TASK_PROMPT,
    BriefContext,
    BriefPolicy,
    convention_files,
    render_brief,
)


def make_context(**overrides) -> BriefContext:
    values = dict(
        plan="1. Add the reset endpoint.\n2. Wire it into the app.",
        plan_digest="ab" * 32,
        issue_title="Add password reset",
        issue_body="Users cannot reset their password.",
        issue_number=42,
        driver="claude-code",
        model="glm-5.3-flash[1m]",
    )
    values.update(overrides)
    return BriefContext(**values)


# ----------------------------------------------------------------------
# Section structure
# ----------------------------------------------------------------------


class TestSections:
    def test_role_task_plan_constraints_quality_contract_all_present(self):
        brief = render_brief(make_context())

        assert "## Role" in brief
        assert "staff engineer" in brief
        assert "APPROVED" in brief
        assert "## Task" in brief
        assert "**Issue #42:** Add password reset" in brief
        assert "Users cannot reset their password." in brief
        assert "## Approved plan" in brief
        assert "1. Add the reset endpoint." in brief  # the plan, VERBATIM
        assert "## Constraints" in brief
        assert "## Quality bar" in brief
        assert "## Output contract" in brief

    def test_plan_digest_is_bound_into_the_brief(self):
        brief = render_brief(make_context())

        assert "ab" * 32 in brief
        assert "human approved" in brief

    def test_empty_plan_degrades_honestly(self):
        brief = render_brief(make_context(plan="  ", plan_digest=""))

        assert "no plan text" in brief

    def test_empty_issue_body_still_carries_the_title(self):
        brief = render_brief(make_context(issue_body="  "))

        assert "**Issue #42:** Add password reset" in brief


# ----------------------------------------------------------------------
# Constraints + quality bar content
# ----------------------------------------------------------------------


class TestConstraints:
    def test_denied_paths_exclude_ci_and_config_files(self):
        brief = render_brief(make_context())

        assert ".gitlab-ci.yml" in brief
        assert ".github/workflows/**" in brief
        assert "NEVER create or modify" in brief

    def test_dependency_policy_is_rendered_and_overridable(self):
        default = render_brief(make_context())
        assert "Do not add new dependencies" in default

        custom = render_brief(
            make_context(),
            policy=BriefPolicy(dependency_policy="Only stdlib additions."),
        )
        assert "Only stdlib additions." in custom

    def test_style_and_minimal_diff_are_required(self):
        brief = render_brief(make_context())

        assert "type hints and docstrings" in brief
        assert "Keep the diff minimal" in brief

    def test_quality_bar_demands_a_green_test_suite(self):
        brief = render_brief(make_context())

        assert "test suite" in brief
        assert "GREEN" in brief


# ----------------------------------------------------------------------
# Skills: the CLIs' native conventions (AGENTS.md / CLAUDE.md)
# ----------------------------------------------------------------------


class TestSkills:
    def test_workspace_with_agents_md_names_it(self, tmp_path: Path):
        (tmp_path / "AGENTS.md").write_text("# conventions\n")

        brief = render_brief(make_context(), repo_root=tmp_path)

        assert "`AGENTS.md`" in brief
        assert "read" in brief and "follow" in brief

    def test_workspace_with_claude_md_names_it(self, tmp_path: Path):
        (tmp_path / "CLAUDE.md").write_text("# conventions\n")

        brief = render_brief(make_context(), repo_root=tmp_path)

        assert "`CLAUDE.md`" in brief

    def test_both_files_are_named(self, tmp_path: Path):
        (tmp_path / "AGENTS.md").write_text("x")
        (tmp_path / "CLAUDE.md").write_text("x")

        brief = render_brief(make_context(), repo_root=tmp_path)

        assert "`AGENTS.md` or `CLAUDE.md`" in brief

    def test_without_conventions_the_brief_stays_conditional(self, tmp_path: Path):
        brief = render_brief(make_context(), repo_root=tmp_path)  # empty workspace

        assert "if `AGENTS.md` or `CLAUDE.md` exists" in brief

    def test_convention_files_helper(self, tmp_path: Path):
        assert convention_files(None) == []
        assert convention_files(tmp_path) == []
        (tmp_path / "CLAUDE.md").write_text("x")
        assert convention_files(tmp_path) == ["CLAUDE.md"]


# ----------------------------------------------------------------------
# Lane-specific output contract
# ----------------------------------------------------------------------


class TestWorkingMethod:
    """Turn-economy discipline (LIVE: 130 calls where ~40 suffice)."""

    def test_turn_budget_discipline_is_always_present(self):
        brief = render_brief(make_context())

        assert "## Working method" in brief
        assert "~60 calls" in brief
        assert "read the relevant range of the file ONCE" in brief
        assert "NARROWEST relevant test subset" in brief

    def test_codegraph_section_only_when_policy_enabled(self):
        off = render_brief(make_context())
        on = render_brief(make_context(), policy=BriefPolicy(codegraph=True))

        assert "codegraph" not in off
        assert "## Code navigation — codegraph MCP" in on
        assert "mcp__codegraph__*" in on
        assert "codegraph_explore" in on


class TestLaneContract:
    def test_ci_lane_is_proposal_only(self):
        brief = render_brief(make_context(), lane="ci_lane")

        assert "NOT** commit and do **NOT** push" in brief
        assert "working tree" in brief
        assert "forge publisher collects" in brief

    def test_dev_lane_commits_but_never_pushes(self):
        brief = render_brief(make_context(), lane="dev")

        assert "Commit your changes" in brief
        assert "Never push" in brief
        assert "NOT** commit" not in brief

    def test_unknown_lane_is_rejected(self):
        with pytest.raises(ValueError, match="unknown lane"):
            render_brief(make_context(), lane="wizard")


# ----------------------------------------------------------------------
# The short per-CLI prompt
# ----------------------------------------------------------------------


def test_task_prompt_is_short_and_points_at_the_brief():
    assert ".forge/brief.md" in TASK_PROMPT
    assert "Read it first" in TASK_PROMPT
    assert len(TASK_PROMPT) < 200  # the quality lives in the brief file
