"""Prose-only comparisons reject semantic changes and preserve tool controls."""

from __future__ import annotations

import subprocess

import pytest

from tests._scripts import load_script, write_tree

checker = load_script("ast_equivalence")


def test_only_true_leading_docstrings_and_comments_are_ignored():
    before = '''"""Old module story."""
# Old history.
class Thing:
    """Long class history."""
    def run(self):
        """A narrative."""
        return 3
async def go():
    """Old async narrative."""
    return Thing()
'''
    after = before.replace("Old module story.", "Module purpose.").replace("# Old history.\n", "")
    after = after.replace("Long class history.", "Value.").replace(
        "A narrative.", "Return a value."
    )
    after = after.replace("Old async narrative.", "Construct a value.")
    assert checker.differences(before, after) == []


@pytest.mark.parametrize(
    "before,after",
    [
        ('"""old"""\nvalue = 1\n', '"""new"""\nvalue = 2\n'),
        ('value = 1\n"old literal"\n', 'value = 1\n"new literal"\n'),
        ('f"old"\nvalue = 1\n', 'f"new"\nvalue = 1\n'),
        ("value: int = 1\n", "value: str = 1\n"),
        ("if False:\n    value = 1\n", "if False:\n    value = 2\n"),
        ("value = 1  # type: int\n", "value = 1  # type: str\n"),
    ],
)
def test_real_code_changes_never_pass_as_docstring_edits(before, after):
    assert "non-docstring AST changed" in checker.differences(before, after)


@pytest.mark.parametrize(
    "directive",
    [
        "# noqa: F401",
        "# type: ignore[assignment]",
        "# prose-lint: allow",
        "# pragma: no cover",
        "# nosec",
        "# fmt: skip",
        "# isort: skip",
        "# ruff: noqa",
        "# pyright: ignore[reportAssignmentType]",
    ],
)
def test_directive_bytes_and_code_target_are_preserved(directive):
    before = f'"""Long\nmodule\nnarration."""\nfirst = 1  {directive}\nsecond = 2\n'
    after = f'"""Purpose."""\nfirst = 1  {directive}\nsecond = 2\n'
    assert checker.differences(before, after) == []
    assert checker.differences(before, after.replace(directive, ""))
    assert checker.differences(before, f"first = 1\nsecond = 2  {directive}\n")


def test_inline_ignore_cannot_move_to_a_standalone_comment():
    assert checker.differences("value = 1  # type: ignore\n", "value = 1\n# type: ignore\n")


def test_docstring_only_scope_does_not_gain_a_pass_statement():
    assert checker.differences('class Empty:\n    """Old."""\n', "class Empty:\n    pass\n")


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    write_tree(
        tmp_path,
        {"src/m.py": '"""Old."""\nvalue = 1\n', "packages/p/src/p.py": '"""Old."""\nvalue = 1\n'},
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    return tmp_path


def test_worktree_cli_checks_both_source_roots_and_detects_a_real_mutation(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src/m.py").write_text('"""Short purpose."""\nvalue = 1\n')
    (repo / "packages/p/src/p.py").write_text('"""Short purpose."""\nvalue = 1\n')
    assert checker.main(["--root", str(repo), "--base", "HEAD"]) == 0
    (repo / "packages/p/src/p.py").write_text('"""Short purpose."""\nvalue = 2\n')
    assert checker.main(["--root", str(repo), "--base", "HEAD"]) == 1


@pytest.mark.parametrize("change", ["add", "delete"])
def test_missing_and_untracked_source_cannot_silently_pass(tmp_path, change):
    repo = _repo(tmp_path)
    if change == "add":
        (repo / "src/new.py").write_text("value = 1\n")
    else:
        (repo / "src/m.py").unlink()
    assert checker.main(["--root", str(repo), "--base", "HEAD"]) == 1


def test_unknown_base_is_a_failure_instead_of_a_skipped_gate(tmp_path):
    repo = _repo(tmp_path)
    assert checker.main(["--root", str(repo), "--base", "missing"]) == 2
    assert checker.main(["--root", str(repo), "--base", "HEAD", "--head", "missing"]) == 2


def test_committed_comparison_does_not_read_a_dirty_worktree(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src/m.py").write_text("value = 999\n")
    assert checker.main(["--root", str(repo), "--base", "HEAD", "--head", "HEAD"]) == 0


def test_unicode_docstring_does_not_change_an_inline_directive_target():
    before = '"""é"""; value = 1  # noqa: F841\n'
    after = '"""Purpose."""; value = 1  # noqa: F841\n'
    assert checker.differences(before, after) == []


@pytest.mark.parametrize("value,expected", [(1, 0), (2, 1)])
def test_committed_prose_guard_reads_both_revisions(tmp_path, value, expected):
    repo = _repo(tmp_path)
    base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    (repo / "src/m.py").write_text(f'"""Purpose."""\nvalue = {value}\n')
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qam",
            "change",
        ],
        check=True,
    )
    assert checker.main(["--root", str(repo), "--base", base, "--head", "HEAD"]) == expected


def test_changed_runtime_template_is_not_ignored_by_a_python_only_proof(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src/prompt.j2").write_text("Change executable prompt instructions.")
    assert checker.main(["--root", str(repo), "--base", "HEAD"]) == 1
