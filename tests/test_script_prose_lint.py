"""scripts/prose_lint.py: no review, backlog or issue archaeology in added source lines."""

from __future__ import annotations

import subprocess

import pytest

from tests._scripts import load_script

prose_lint = load_script("prose_lint")

HEADER = (
    "diff --git a/src/forge/x.py b/src/forge/x.py\n--- a/src/forge/x.py\n+++ b/src/forge/x.py\n"
)
DIFF = (
    HEADER + "@@ -10,0 +11,5 @@\n"
    "+# R42-16: the parity rule\n"
    "+x = 1  # see ADR-0035\n"
    "+# fixed in #377\n"
    "+# as agreed in review `9e8d6c6`\n"
    "+# carried over from NXT-10\n"
)


def test_review_backlog_and_issue_references_are_rejected():
    found = prose_lint.violations(DIFF)
    assert [item.split(":")[1] for item in found] == ["11", "13", "14", "15"]


def test_adr_references_are_allowed():
    assert not any("ADR-0035" in item for item in prose_lint.violations(DIFF))


def test_removed_lines_are_ignored():
    assert prose_lint.violations(HEADER + "@@ -3,1 +2,0 @@\n-# R42-16 old comment\n") == []


def test_line_numbers_ignore_no_newline_markers():
    diff = HEADER + "@@ -1 +1,2 @@\n-x = 1\n\\ No newline at end of file\n+x = 1\n+# R42-16\n"
    assert [item.split(":")[1] for item in prose_lint.violations(diff)] == ["2"]


def test_the_allow_marker_keeps_a_literal():
    diff = HEADER + '@@ -0,0 +1 @@\n+COLOUR = "#009966"  # prose-lint: allow\n'
    assert prose_lint.violations(diff) == []


def test_a_missing_or_zero_base_is_a_no_op():
    assert prose_lint.main(["--base", ""]) == 0
    assert prose_lint.main(["--base", "0" * 40]) == 0


def _git(repo, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q", "-b", "main")
    for key, value in (
        ("user.email", "lint@example.test"),
        ("user.name", "lint"),
        ("commit.gpgsign", "false"),
        ("core.hooksPath", "/dev/null"),
    ):
        _git(tmp_path, "config", key, value)
    (tmp_path / "src").mkdir()
    (tmp_path / "src/m.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _change(repo, text: str) -> None:
    (repo / "src/m.py").write_text(text)
    _git(repo, "commit", "-q", "-am", "change")


def test_a_clean_change_passes(repo):
    _change(repo, "x = 1\ny = 2  # see ADR-0035\n")
    assert prose_lint.main(["--base", "HEAD~1"]) == 0


def test_an_added_review_id_fails(repo, capsys):
    _change(repo, "x = 1\n# R42-16 parity\n")
    assert prose_lint.main(["--base", "HEAD~1"]) == 1
    assert "src/m.py:2: # R42-16 parity" in capsys.readouterr().out


def test_a_moved_file_counts_as_added(repo, capsys):
    _change(repo, "x = 1\n# R42-16 parity\n")
    _git(repo, "mv", "src/m.py", "src/n.py")
    _git(repo, "commit", "-q", "-m", "move")
    assert prose_lint.main(["--base", "HEAD~1"]) == 1
    assert "src/n.py:2: # R42-16 parity" in capsys.readouterr().out


def test_an_unknown_base_is_reported_not_raised(repo, capsys):
    assert prose_lint.main(["--base", "1234567890abcdef1234567890abcdef12345678"]) == 0
    assert "is not in this checkout" in capsys.readouterr().out
