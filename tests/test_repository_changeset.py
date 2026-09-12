"""Tests for the ChangeSet contract and trusted validation (ADR-0001)."""

import pytest

from forge.repository.changeset import (
    DENIED_PATHS,
    MAX_CHANGES,
    Change,
    ChangeSet,
    Operation,
    is_lockfile,
    validate_changeset,
)


def _cs(*changes: Change, message: str = "forge: implement 7 (run abcd1234)") -> ChangeSet:
    return ChangeSet(branch="factory/7/abcd1234", commit_message=message, changes=list(changes))


def _create(path: str = "forge-demo/run-abcd1234.md", content: str = "# hello") -> Change:
    return Change(path=path, operation=Operation.CREATE, content=content)


class TestValidChangesets:
    def test_minimal_valid_changeset(self):
        assert validate_changeset(_cs(_create())) == []

    def test_update_without_content_is_allowed_in_m1(self):
        cs = _cs(Change(path="src/app.py", operation=Operation.UPDATE))
        assert validate_changeset(cs) == []

    def test_update_with_full_content_is_allowed(self):
        cs = _cs(Change(path="src/app.py", operation=Operation.UPDATE, content="x = 1\n"))
        assert validate_changeset(cs) == []

    def test_delete_without_content_is_allowed(self):
        cs = _cs(Change(path="src/old.py", operation=Operation.DELETE))
        assert validate_changeset(cs) == []

    def test_multiple_valid_changes(self):
        cs = _cs(
            _create("forge-demo/a.md"),
            _create("forge-demo/b.md"),
            Change(path="src/old.py", operation=Operation.DELETE),
        )
        assert validate_changeset(cs) == []


class TestDenylist:
    def test_gitlab_ci_file_denied(self):
        violations = validate_changeset(_cs(_create(".gitlab-ci.yml")))
        assert any(".gitlab-ci.yml" in v for v in violations)

    def test_forge_config_denied(self):
        violations = validate_changeset(_cs(_create(".forge.yml")))
        assert violations

    def test_github_prefix_denied(self):
        violations = validate_changeset(_cs(_create(".github/workflows/ci.yml")))
        assert violations

    @pytest.mark.parametrize(
        "path",
        [
            "web/package-lock.json",
            "yarn.lock",
            "Cargo.lock",
            "poetry.lock",
            "uv.lock",
            "sub/dir/Gemfile.lock",
        ],
    )
    def test_well_known_lockfiles_denied(self, path):
        violations = validate_changeset(_cs(_create(path)))
        assert any("lockfile" in v for v in violations)

    def test_any_dotlock_suffix_denied(self):
        violations = validate_changeset(_cs(_create("vendor/thing.lock")))
        assert violations

    def test_is_lockfile_helper(self):
        assert is_lockfile("a/b/uv.lock")
        assert not is_lockfile("src/lock.py")

    def test_denylist_constants_are_module_level(self):
        # Tests (and future policy config) monkeypatch these constants.
        assert ".gitlab-ci.yml" in DENIED_PATHS
        assert MAX_CHANGES == 20

    def test_monkeypatched_limit_is_respected(self, monkeypatch):
        from forge.repository import changeset

        monkeypatch.setattr(changeset, "MAX_CHANGES", 2)
        violations = validate_changeset(_cs(_create("a.md"), _create("b.md")))
        assert violations == []


class TestPathSafety:
    def test_path_traversal_denied(self):
        violations = validate_changeset(_cs(_create("../../etc/passwd")))
        assert any("traversal" in v for v in violations)

    def test_nested_traversal_denied(self):
        violations = validate_changeset(_cs(_create("src/../../escape.txt")))
        assert violations

    def test_absolute_path_denied(self):
        violations = validate_changeset(_cs(_create("/etc/passwd")))
        assert any("absolute" in v for v in violations)

    def test_empty_path_denied(self):
        violations = validate_changeset(_cs(_create("")))
        assert violations


class TestContentRules:
    def test_create_requires_content(self):
        cs = _cs(Change(path="forge-demo/x.md", operation=Operation.CREATE))
        violations = validate_changeset(cs)
        assert any("requires content" in v for v in violations)

    def test_delete_must_not_carry_content(self):
        cs = _cs(Change(path="src/x.py", operation=Operation.DELETE, content="oops"))
        assert violations_of_delete(cs)

    def test_oversized_content_denied(self):
        big = "x" * (256 * 1024 + 1)
        violations = validate_changeset(_cs(_create(content=big)))
        assert any("exceeds" in v for v in violations)

    def test_exactly_256kib_allowed(self):
        big = "x" * (256 * 1024)
        assert validate_changeset(_cs(_create(content=big))) == []


def violations_of_delete(cs: ChangeSet) -> bool:
    return any("must not carry content" in v for v in validate_changeset(cs))


class TestStructuralRules:
    def test_empty_changes_denied(self):
        violations = validate_changeset(_cs())
        assert any("at least one change" in v for v in violations)

    def test_empty_commit_message_denied(self):
        violations = validate_changeset(_cs(_create(), message="   "))
        assert any("commit_message" in v for v in violations)

    def test_more_than_20_changes_denied(self):
        changes = [_create(f"forge-demo/f{i}.md") for i in range(21)]
        violations = validate_changeset(_cs(*changes))
        assert any("21 changes" in v for v in violations)

    def test_exactly_20_changes_allowed(self):
        changes = [_create(f"forge-demo/f{i}.md") for i in range(20)]
        assert validate_changeset(_cs(*changes)) == []

    def test_all_violations_reported_at_once(self):
        cs = ChangeSet(
            branch="factory/7/abcd1234",
            commit_message="",
            changes=[
                Change(path=".gitlab-ci.yml", operation=Operation.CREATE, content="x"),
                Change(path="../escape", operation=Operation.CREATE),
            ],
        )
        violations = validate_changeset(cs)
        assert len(violations) >= 3  # message + denylist + traversal + missing content
