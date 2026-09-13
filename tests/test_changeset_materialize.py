"""Tests for ChangeSet materialization from raw drafts (ADR-0001)."""

import pytest

from forge.repository.changeset import (
    Change,
    ChangeSet,
    MaterializationError,
    Operation,
    materialize,
    validate_changeset,
)


def base() -> dict[str, str]:
    return {
        "src/app.py": "x = 1\ny = 2\n",
        "docs/readme.md": "# readme\n",
    }


class TestMaterializeCreate:
    def test_create_with_content_ok(self):
        cs = materialize(
            {
                "branch": "factory/7/abcd1234",
                "commit_message": "forge: implement 7 (run abcd1234)",
                "changes": [
                    {"path": "forge-demo/new.md", "operation": "create", "content": "# hi"}
                ],
            },
            base(),
        )
        assert cs.branch == "factory/7/abcd1234"
        assert len(cs.changes) == 1
        change = cs.changes[0]
        assert change.operation is Operation.CREATE
        assert change.content == "# hi"

    def test_create_without_content_raises(self):
        with pytest.raises(MaterializationError):
            materialize(
                {
                    "branch": "b",
                    "commit_message": "m",
                    "changes": [{"path": "new.md", "operation": "create"}],
                },
                base(),
            )

    def test_create_of_existing_file_raises(self):
        with pytest.raises(MaterializationError):
            materialize(
                {
                    "branch": "b",
                    "commit_message": "m",
                    "changes": [
                        {"path": "src/app.py", "operation": "create", "content": "overwrite"}
                    ],
                },
                base(),
            )


class TestMaterializeUpdate:
    def test_exact_single_match_is_replaced(self):
        cs = materialize(
            {
                "branch": "b",
                "commit_message": "m",
                "changes": [
                    {
                        "path": "src/app.py",
                        "operation": "update",
                        "old_text": "x = 1\n",
                        "new_text": "x = 42\n",
                    }
                ],
            },
            base(),
        )
        (change,) = cs.changes
        assert change.content == "x = 42\ny = 2\n"

    def test_zero_matches_raise(self):
        with pytest.raises(MaterializationError):
            materialize(
                {
                    "branch": "b",
                    "commit_message": "m",
                    "changes": [
                        {
                            "path": "src/app.py",
                            "operation": "update",
                            "old_text": "not in the file",
                            "new_text": "x",
                        }
                    ],
                },
                base(),
            )

    def test_two_matches_with_expected_matches_2_ok(self):
        git_base = {"dup.txt": "same\nsame\n"}
        cs = materialize(
            {
                "branch": "b",
                "commit_message": "m",
                "changes": [
                    {
                        "path": "dup.txt",
                        "operation": "update",
                        "old_text": "same\n",
                        "new_text": "changed\n",
                        "expected_matches": 2,
                    }
                ],
            },
            git_base,
        )
        assert cs.changes[0].content == "changed\nchanged\n"

    def test_two_matches_with_default_expected_raise(self):
        git_base = {"dup.txt": "same\nsame\n"}
        with pytest.raises(MaterializationError):
            materialize(
                {
                    "branch": "b",
                    "commit_message": "m",
                    "changes": [
                        {
                            "path": "dup.txt",
                            "operation": "update",
                            "old_text": "same\n",
                            "new_text": "changed\n",
                        }
                    ],
                },
                git_base,
            )

    def test_update_of_missing_file_raises(self):
        with pytest.raises(MaterializationError):
            materialize(
                {
                    "branch": "b",
                    "commit_message": "m",
                    "changes": [
                        {"path": "nope.py", "operation": "update", "old_text": "a", "new_text": "b"}
                    ],
                },
                base(),
            )


class TestMaterializeDeleteAndShape:
    def test_delete_existing_file_ok(self):
        cs = materialize(
            {
                "branch": "b",
                "commit_message": "m",
                "changes": [{"path": "docs/readme.md", "operation": "delete"}],
            },
            base(),
        )
        assert cs.changes[0].operation is Operation.DELETE
        assert cs.changes[0].content is None

    def test_delete_of_missing_file_raises(self):
        with pytest.raises(MaterializationError):
            materialize(
                {
                    "branch": "b",
                    "commit_message": "m",
                    "changes": [{"path": "nope.py", "operation": "delete"}],
                },
                base(),
            )

    def test_unknown_operation_raises(self):
        with pytest.raises(MaterializationError):
            materialize(
                {
                    "branch": "b",
                    "commit_message": "m",
                    "changes": [{"path": "x", "operation": "move"}],
                },
                base(),
            )

    def test_missing_branch_or_message_raises(self):
        with pytest.raises(MaterializationError):
            materialize(
                {
                    "commit_message": "m",
                    "changes": [{"path": "x", "operation": "create", "content": "c"}],
                },
                base(),
            )
        with pytest.raises(MaterializationError):
            materialize({"branch": "b", "changes": []}, base())

    def test_model_supplied_branch_is_overridden_by_caller_in_service(self):
        """materialize itself is neutral; the docstring contract is that the
        trusted layer pins identity. Verify it keeps whatever it is given."""
        cs = materialize(
            {
                "branch": "evil/../branch",
                "commit_message": "m",
                "changes": [{"path": "new.md", "operation": "create", "content": "c"}],
            },
            base(),
        )
        assert cs.branch == "evil/../branch"  # trusted callers pin/validate this


class TestValidateWithGitBase:
    def _cs(self, *changes: Change) -> ChangeSet:
        return ChangeSet(branch="b", commit_message="m", changes=list(changes))

    def test_update_of_known_file_ok(self):
        cs = self._cs(Change(path="src/app.py", operation=Operation.UPDATE, content="new"))
        assert validate_changeset(cs, base()) == []

    def test_update_of_missing_file_violates(self):
        cs = self._cs(Change(path="nope.py", operation=Operation.UPDATE, content="new"))
        violations = validate_changeset(cs, base())
        assert any("does not exist" in violation for violation in violations)

    def test_delete_of_missing_file_violates(self):
        cs = self._cs(Change(path="nope.py", operation=Operation.DELETE))
        violations = validate_changeset(cs, base())
        assert any("does not exist" in violation for violation in violations)

    def test_git_base_omitted_keeps_m1_behaviour(self):
        """Without a snapshot, update/delete pass as in M1 (no existence check)."""
        cs = self._cs(Change(path="nope.py", operation=Operation.UPDATE))
        assert validate_changeset(cs) == []

    def test_denylist_still_enforced_with_git_base(self):
        cs = self._cs(Change(path=".gitlab-ci.yml", operation=Operation.UPDATE, content="hack:"))
        violations = validate_changeset(cs, base())
        assert any("denylisted" in violation for violation in violations)
