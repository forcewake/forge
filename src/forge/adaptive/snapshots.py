"""Immutable read-only repository snapshots (DSC-02).

A plan revision binds to a :class:`~forge.adaptive.models.SnapshotSet`; this
module makes those snapshots *usable* without giving up their defining
property — immutability. Three guards carry that property:

- :func:`validate_policies` / :func:`check_hydration_rules` — nothing
  hydrates from a repository the path policy did not register, and nothing
  hydrates with submodules/LFS/symlinks/external fetches the hydration
  rules disallow. The clone step is refused *before* it runs, not audited
  after.
- :class:`SnapshotWorkspace` — a snapshot materializes as a frozen
  read-only *copy*. Moving ``main`` (or mutating the in-memory file dict)
  cannot change a mounted source, so evidence gathered against the mount
  stays answerable to the recorded ``source_oid`` forever.
- :func:`strip_credentials` — source-control credentials are stripped from
  the environment the moment hydration finishes; they never outlive the
  clone they were needed for.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path, PurePosixPath

from forge.adaptive.models import Snapshot, SnapshotSet

#: Environment-key markers that mark a variable as a credential. Matched
#: case-insensitively as substrings, so GITHUB_TOKEN, CI_SECRET,
#: DB_PASSWORD and SSH_KEY all fall, while FORGE_RUN_ID survives.
_CREDENTIAL_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "KEY")

_DIR_READONLY = 0o555
_FILE_READONLY = 0o444
_DIR_WRITABLE = 0o755
_FILE_WRITABLE = 0o644


def validate_policies(snapshot_set: SnapshotSet, policy: dict[str, list[str]]) -> list[str]:
    """Return violations for snapshots whose repository has no path policy.

    ``policy`` maps ``repository_id`` to the allowed path globs for that
    repository. A snapshot whose repository is absent from the policy is an
    *unregistered repository*: hydration would read paths nobody authorized,
    so the repository_id is reported and the caller refuses to hydrate.
    Extra policy entries are fine — a policy may cover repositories this
    particular snapshot set does not include.
    """
    return [
        f"unregistered repository: {snapshot.repository_id!r} has no path policy"
        for snapshot in snapshot_set.snapshots
        if snapshot.repository_id not in policy
    ]


def check_hydration_rules(rules: dict) -> list[str]:
    """Return the capability names a hydration would need but rules disallow.

    ``rules`` is a dict of ``allow_<capability>`` keys (``allow_submodules``,
    ``allow_lfs``, ``allow_symlinks``, ``allow_external_fetch``) mapping to
    booleans. Every rule present with a falsy value names a capability that
    is disallowed; the list returns those capability names (the rule key
    minus its ``allow_`` prefix) so the caller can intersect them with what
    the clone actually needs. An empty list means everything present is
    allowed — nothing stands between the clone and the rules.
    """
    return [
        name.removeprefix("allow_")
        for name, allowed in rules.items()
        if name.startswith("allow_") and not allowed
    ]


def strip_credentials(env: dict[str, str]) -> dict[str, str]:
    """Drop every env key that names a credential; keep the rest verbatim.

    Runs against the environment the moment hydration finishes: whatever
    the clone needed (tokens, deploy keys, passwords) must not outlive the
    clone into discovery and execution, where it could leak into tool
    output or logs. Keys are matched on substring markers, case-blind.
    """
    return {
        key: value
        for key, value in env.items()
        if not any(marker in key.upper() for marker in _CREDENTIAL_MARKERS)
    }


class SnapshotWorkspace:
    """Materializes snapshots as frozen, read-only directory trees.

    Each mounted snapshot lives at ``root/<repository_id>/`` — a full copy
    of the recorded content, permission-frozen (directories ``0o555``,
    files ``0o444``). Because the mount is a copy, not a working checkout,
    later movement of the source branch cannot rewrite what a plan step
    already read; every evidence reference stays bound to the
    ``source_oid`` recorded at mount time.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._mounted: dict[str, Snapshot] = {}

    @property
    def root(self) -> Path:
        """The workspace root all mounts live under."""
        return self._root

    @property
    def mounted_repository_ids(self) -> list[str]:
        """Repository ids currently mounted, in mount order."""
        return list(self._mounted)

    def mount(self, snapshot: Snapshot, files: dict[str, str]) -> Path:
        """Materialize ``snapshot``'s ``files`` as a read-only tree.

        Returns the mount path ``root/<repository_id>``. Content is written
        first, permissions frozen second, so the tree only ever becomes
        visible-as-frozen. Remounting a repository is refused: a mounted
        source is immutable by contract, and a second mount would silently
        rebind evidence to different content.
        """
        if snapshot.repository_id in self._mounted:
            raise ValueError(
                f"repository {snapshot.repository_id!r} is already mounted; "
                "a mounted snapshot is frozen and cannot be remounted"
            )
        mount_root = self._root / snapshot.repository_id
        if mount_root.exists():
            raise ValueError(
                f"mount path {mount_root} already exists; refusing to alias "
                "leftover state with a fresh snapshot"
            )
        for rel_path, content in files.items():
            target = self._resolve(mount_root, rel_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self._freeze(mount_root)
        self._mounted[snapshot.repository_id] = snapshot
        return mount_root

    def unmount_all(self) -> None:
        """Restore write permissions, then remove the whole tree.

        Permissions go back to writable *before* the removal walk so the
        read-only bits cannot block cleanup on any platform. The mounted
        registry is cleared either way — an empty workspace must be
        indistinguishable from a fresh one.
        """
        if self._root.exists():
            for dirpath, _dirnames, filenames in os.walk(self._root):
                for name in filenames:
                    os.chmod(Path(dirpath) / name, _FILE_WRITABLE)
                os.chmod(dirpath, _DIR_WRITABLE)
            shutil.rmtree(self._root)
        self._mounted.clear()

    def evidence_ref(self, repository_id: str, path: str) -> dict | None:
        """Bind a path to the snapshot it was observed against.

        Returns ``{"repository_id", "source_oid", "path"}`` for a mounted
        repository, or ``None`` when it is not mounted — every evidence
        reference must belong to one recorded snapshot, and an unmounted
        repository has nothing recorded to answer to.
        """
        snapshot = self._mounted.get(repository_id)
        if snapshot is None:
            return None
        return {
            "repository_id": repository_id,
            "source_oid": snapshot.source_oid,
            "path": path,
        }

    @staticmethod
    def _resolve(mount_root: Path, rel_path: str) -> Path:
        """Reject snapshot paths that would escape the mount tree.

        The files dict comes from hydration tooling; a crafted ``..`` or
        absolute path must never write outside ``root/<repository_id>/``.
        """
        pure = PurePosixPath(rel_path)
        if pure.is_absolute() or not pure.parts or ".." in pure.parts:
            raise ValueError(f"snapshot path escapes the mount: {rel_path!r}")
        return mount_root.joinpath(*pure.parts)

    @staticmethod
    def _freeze(mount_root: Path) -> None:
        """chmod the mounted tree read-only, deepest entries first.

        Bottom-up so a directory is frozen only after its contents are;
        the frozen directory bit is what stops a plan step from writing
        into the snapshot by accident.
        """
        for dirpath, _dirnames, filenames in os.walk(mount_root, topdown=False):
            for name in filenames:
                os.chmod(Path(dirpath) / name, _FILE_READONLY)
            os.chmod(dirpath, _DIR_READONLY)
