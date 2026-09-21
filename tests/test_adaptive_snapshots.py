"""DSC-02: snapshots materialize frozen, read-only, credential-stripped.

The tests prove the three immutability guards: policy gates refuse
unregistered repositories and disallowed hydration capabilities before
anything clones; a mounted tree is a frozen copy that later mutation of
the source cannot touch; and source-control credentials do not outlive
hydration.
"""

from __future__ import annotations

import os

import pytest

from forge.adaptive.models import Snapshot, SnapshotSet
from forge.adaptive.snapshots import (
    SnapshotWorkspace,
    check_hydration_rules,
    strip_credentials,
    validate_policies,
)

_OID = "1" * 40
_OTHER_OID = "2" * 40
_DIGEST = "3" * 64


def _snapshot(repository_id: str, source_oid: str = _OID) -> Snapshot:
    return Snapshot(
        repository_id=repository_id,
        source_oid=source_oid,
        resolved_from="refs/heads/main",
        config_digest=_DIGEST,
    )


def _snapshot_set(*snapshots: Snapshot) -> SnapshotSet:
    return SnapshotSet(snapshot_set_id="ss-1", snapshots=list(snapshots))


class TestValidatePolicies:
    def test_registered_repositories_produce_no_violations(self):
        snapshot_set = _snapshot_set(_snapshot("repo-a"), _snapshot("repo-b"))
        policy = {"repo-a": ["**"], "repo-b": ["src/**"], "repo-extra": ["**"]}
        assert validate_policies(snapshot_set, policy) == []

    def test_unregistered_repository_is_reported_by_name(self):
        snapshot_set = _snapshot_set(_snapshot("repo-a"), _snapshot("repo-x"))
        violations = validate_policies(snapshot_set, {"repo-a": ["**"]})
        assert len(violations) == 1
        assert "repo-x" in violations[0]


class TestCheckHydrationRules:
    def test_everything_allowed_yields_an_empty_list(self):
        rules = {
            "allow_submodules": True,
            "allow_lfs": True,
            "allow_symlinks": True,
            "allow_external_fetch": True,
        }
        assert check_hydration_rules(rules) == []

    def test_disallowed_capabilities_are_named_without_the_prefix(self):
        rules = {
            "allow_submodules": False,
            "allow_lfs": True,
            "allow_symlinks": False,
            "allow_external_fetch": True,
        }
        assert check_hydration_rules(rules) == ["submodules", "symlinks"]

    def test_empty_rules_disallow_nothing(self):
        assert check_hydration_rules({}) == []


class TestSnapshotWorkspace:
    def test_mount_freezes_content_against_later_source_mutation(self, tmp_path):
        workspace = SnapshotWorkspace(tmp_path / "ws")
        files = {"src/app.py": "print('hi')\n"}
        mount_path = workspace.mount(_snapshot("repo-a"), files)

        # Moving main (or mutating the in-memory dict) after the mount
        # must not reach the frozen copy.
        files["src/app.py"] = "print('MUTATED')\n"
        files["extra.py"] = "new = True\n"
        assert (mount_path / "src/app.py").read_text(encoding="utf-8") == "print('hi')\n"
        assert not (mount_path / "extra.py").exists()

    def test_mount_is_read_only(self, tmp_path):
        workspace = SnapshotWorkspace(tmp_path / "ws")
        mount_path = workspace.mount(_snapshot("repo-a"), {"src/app.py": "x = 1\n"})

        assert not os.access(mount_path / "src/app.py", os.W_OK)
        assert not os.access(mount_path / "src", os.W_OK)
        assert not os.access(mount_path, os.W_OK)
        assert os.access(mount_path / "src/app.py", os.R_OK)

    def test_mount_creates_the_repository_directory_shape(self, tmp_path):
        workspace = SnapshotWorkspace(tmp_path / "ws")
        mount_path = workspace.mount(
            _snapshot("repo-a"), {"README.md": "", "pkg/mod.py": "y = 2\n"}
        )
        assert mount_path == tmp_path / "ws" / "repo-a"
        assert (mount_path / "pkg/mod.py").is_file()

    def test_remounting_a_repository_is_refused(self, tmp_path):
        workspace = SnapshotWorkspace(tmp_path / "ws")
        workspace.mount(_snapshot("repo-a"), {"a.txt": "1\n"})
        with pytest.raises(ValueError, match="frozen"):
            workspace.mount(_snapshot("repo-a"), {"a.txt": "2\n"})

    def test_paths_that_escape_the_mount_are_refused(self, tmp_path):
        workspace = SnapshotWorkspace(tmp_path / "ws")
        with pytest.raises(ValueError, match="escape"):
            workspace.mount(_snapshot("repo-a"), {"../outside.txt": "nope\n"})
        with pytest.raises(ValueError, match="escape"):
            workspace.mount(_snapshot("repo-b"), {"/etc/passwd": "nope\n"})

    def test_evidence_ref_binds_to_the_recorded_source_oid(self, tmp_path):
        workspace = SnapshotWorkspace(tmp_path / "ws")
        workspace.mount(_snapshot("repo-a", source_oid=_OID), {"src/app.py": "x = 1\n"})
        workspace.mount(_snapshot("repo-b", source_oid=_OTHER_OID), {"lib.py": "y = 2\n"})

        reference = workspace.evidence_ref("repo-a", "src/app.py")
        assert reference == {
            "repository_id": "repo-a",
            "source_oid": _OID,
            "path": "src/app.py",
        }
        assert workspace.evidence_ref("repo-b", "lib.py")["source_oid"] == _OTHER_OID

    def test_evidence_ref_is_none_for_an_unmounted_repository(self, tmp_path):
        workspace = SnapshotWorkspace(tmp_path / "ws")
        workspace.mount(_snapshot("repo-a"), {"a.txt": "1\n"})
        assert workspace.evidence_ref("not-mounted", "a.txt") is None

    def test_unmount_all_restores_the_root_and_the_registry(self, tmp_path):
        workspace = SnapshotWorkspace(tmp_path / "ws")
        workspace.mount(_snapshot("repo-a"), {"src/app.py": "x = 1\n"})
        root = workspace.root

        workspace.unmount_all()
        assert not root.exists()
        assert workspace.evidence_ref("repo-a", "src/app.py") is None

        # The workspace is reusable afterwards: permissions went back to
        # writable before the removal walk, so nothing is left frozen.
        workspace.mount(_snapshot("repo-a"), {"b.txt": "2\n"})
        assert (root / "repo-a/b.txt").read_text(encoding="utf-8") == "2\n"

    def test_unmount_all_without_mounts_is_a_no_op(self, tmp_path):
        workspace = SnapshotWorkspace(tmp_path / "ws")
        workspace.unmount_all()
        assert workspace.mounted_repository_ids == []


class TestStripCredentials:
    def test_credential_keys_are_removed_regardless_of_case(self):
        env = {
            "GITHUB_TOKEN": "gh-secret",
            "ci_secret": "s",
            "DB_PASSWORD": "p",
            "SSH_KEY": "k",
            "API_KEY_ID": "id",
            "FORGE_RUN_ID": "run-42",
            "PATH": "/bin:/usr/bin",
        }
        assert strip_credentials(env) == {"FORGE_RUN_ID": "run-42", "PATH": "/bin:/usr/bin"}

    def test_marker_matching_is_substring_based(self):
        env = {"MY_MONKEY_POOL": "safe", "FORGE_RUN_ID": "run-42"}
        # "MONKEY" contains KEY, so it falls — substring matching errs on
        # the side of stripping; a false positive costs nothing.
        assert strip_credentials(env) == {"FORGE_RUN_ID": "run-42"}

    def test_plain_environment_survives(self):
        env = {"FORGE_RUN_ID": "run-42", "HOME": "/home/agent", "LANG": "C"}
        assert strip_credentials(env) == env
