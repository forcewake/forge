"""Pure candidate representations and the validated publication capability."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import posixpath
from typing import Any, Literal

from .blob import BlobContent
from .git_binary import BinaryBlock
from .candidate_usage import HarnessUsage
from .change_types import Change, ChangeSet, Operation
from .patch import (
    CandidateError,
    DiffHunk,
    _enforce_cap,
    _sha256_digest,
    _validated_digest,
    _verify_base_digest,
    _verify_result_digest,
    apply_unified_hunks,
)
from .paths import PathPolicy, normalize_repo_path, private_repo_path, validate_changeset


CandidateOperation = Literal["create", "delete", "modify"]


@dataclass(frozen=True)
class CreateFile:
    """A file the candidate creates: the new-file diff IS the whole file."""

    path: str
    new_content: str
    mode: str = "100644"
    #: Digest of the expected result — verified before anything publishes.
    intended_digest: str = ""

    def __post_init__(self) -> None:
        _validated_digest(self.intended_digest)
        if not self.intended_digest:
            object.__setattr__(self, "intended_digest", _sha256_digest(self.new_content))

    @property
    def operation(self) -> CandidateOperation:
        return "create"

    @property
    def hunks(self) -> tuple[DiffHunk, ...]:
        return ()


@dataclass(frozen=True)
class DeleteFile:
    """A file the candidate deletes; the write path removes it wholesale."""

    path: str
    mode: str = "100644"
    #: Digest of the blob being deleted, when the artifact carries it — verified against the
    #: authoritative base before the delete publishes.
    base_blob_digest: str = ""

    def __post_init__(self) -> None:
        _validated_digest(self.base_blob_digest)

    @property
    def operation(self) -> CandidateOperation:
        return "delete"

    @property
    def new_content(self) -> str | None:
        return None

    @property
    def hunks(self) -> tuple[DiffHunk, ...]:
        return ()


@dataclass(frozen=True)
class FullReplacement:
    """A file the candidate rewrites wholesale (the representation)."""

    path: str
    new_content: str
    mode: str = "100644"
    #: Digest of the base content the replacement was built against.
    base_blob_digest: str = ""
    #: Digest of the expected result — verified before anything publishes.
    intended_digest: str = ""

    def __post_init__(self) -> None:
        _validated_digest(self.base_blob_digest)
        _validated_digest(self.intended_digest)
        if not self.intended_digest:
            object.__setattr__(self, "intended_digest", _sha256_digest(self.new_content))

    @property
    def operation(self) -> CandidateOperation:
        return "modify"

    @property
    def hunks(self) -> tuple[DiffHunk, ...]:
        return ()


@dataclass(frozen=True)
class UnifiedPatch:
    """A file the candidate modifies by applying unified hunks to the base."""

    path: str
    hunks: tuple[DiffHunk, ...]
    mode: str = "100644"
    #: Digest of the base content the hunks were diffed against.
    base_blob_digest: str = ""
    #: Digest of the expected post-image (the diff's own new blob OID when the capture is
    #: ``--full-index``) — an applier bug becomes a detected rejection instead of silent
    #: corruption.
    intended_digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "hunks", tuple(self.hunks))
        if not self.hunks:
            raise CandidateError(
                "ambiguous_representation",
                f"{self.path}: modify entry without hunks or full content",
            )
        _validated_digest(self.base_blob_digest)
        _validated_digest(self.intended_digest)

    @property
    def operation(self) -> CandidateOperation:
        return "modify"

    @property
    def new_content(self) -> str | None:
        return None


@dataclass(frozen=True)
class BinaryPatch:
    path: str
    forward: BinaryBlock
    operation: CandidateOperation
    mode: str = "100644"
    base_blob_digest: str = ""
    intended_digest: str = ""

    def __post_init__(self) -> None:
        _validated_digest(self.base_blob_digest)
        _validated_digest(self.intended_digest)
        if self.operation not in ("create", "modify") or not self.intended_digest:
            raise CandidateError("corrupt_binary_patch", "binary post-image requires a full digest")
        if self.operation == "modify" and not self.base_blob_digest:
            raise CandidateError("stale_base", "binary update requires a full base digest")


@dataclass(frozen=True)
class BlobReplacement:
    path: str
    new_blob: BlobContent
    operation: CandidateOperation
    mode: str = "100644"
    base_blob_digest: str = ""
    intended_digest: str = ""

    def __post_init__(self) -> None:
        _validated_digest(self.base_blob_digest)
        _validated_digest(self.intended_digest)
        if self.operation not in ("create", "modify") or not self.intended_digest:
            raise CandidateError("ambiguous_representation", "blob post-image requires its digest")


@dataclass(frozen=True)
class ModeChange:
    path: str
    old_mode: str
    mode: str

    def __post_init__(self) -> None:
        if self.old_mode == self.mode:
            raise CandidateError("empty_candidate", "mode-only entry does not change a mode")

    @property
    def operation(self) -> CandidateOperation:
        return "modify"


ChangeEntry = (
    CreateFile
    | DeleteFile
    | FullReplacement
    | UnifiedPatch
    | BinaryPatch
    | BlobReplacement
    | ModeChange
)


@dataclass(frozen=True)
class ChangeManifestEntry:
    """The completed, flat form:meth:`CandidateBundle.materialize` returns."""

    path: str
    operation: CandidateOperation
    new_content: str | None = None
    mode: str = "100644"
    hunks: tuple[DiffHunk, ...] = ()
    new_blob: BlobContent | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "hunks", tuple(self.hunks))
        if self.mode not in ("100644", "100755", "120000"):
            raise CandidateError(
                "unsupported_mode", "manifest mode is not a supported Git blob mode"
            )
        if self.new_blob is not None and self.new_content is not None:
            raise CandidateError("ambiguous_representation", "manifest has two post-images")

    @property
    def content_bytes(self) -> bytes | None:
        if self.new_blob is not None:
            return self.new_blob.raw
        return self.new_content.encode("utf-8") if self.new_content is not None else None


@dataclass(frozen=True)
class CandidateBundle:
    """A parsed harness candidate: diff manifest + trusted context."""

    attempt_base_oid: str
    driver_exit: str
    entries: tuple[ChangeEntry, ...] = ()
    usage: HarnessUsage | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))

    @property
    def is_empty(self) -> bool:
        return not self.entries

    @property
    def paths(self) -> list[str]:
        return [entry.path for entry in self.entries]

    def materialize(
        self, base_contents: Mapping[str, str | bytes], base_modes: Mapping[str, str] | None = None
    ) -> list[ChangeManifestEntry]:
        """Complete every entry against the authoritative base texts."""
        completed: list[ChangeManifestEntry] = []
        for entry in self.entries:
            completed.append(self._materialize_entry(entry, base_contents, base_modes))
        return completed

    @staticmethod
    def _materialize_entry(
        entry: ChangeEntry,
        base_contents: Mapping[str, str | bytes],
        base_modes: Mapping[str, str] | None = None,
    ) -> ChangeManifestEntry:
        if isinstance(entry, (BinaryPatch, BlobReplacement, ModeChange)):
            return _materialize_binary(entry, base_contents, base_modes)
        if isinstance(entry, DeleteFile):
            base = base_contents.get(entry.path)
            if base is not None and entry.base_blob_digest:
                _verify_base_digest(entry.path, base, entry.base_blob_digest)
            return ChangeManifestEntry(
                path=entry.path, operation="delete", new_content=None, mode=entry.mode
            )
        if isinstance(entry, CreateFile):
            _enforce_cap(entry.path, entry.new_content)
            _verify_result_digest(entry.path, entry.new_content, entry.intended_digest)
            return ChangeManifestEntry(
                path=entry.path,
                operation="create",
                new_content=entry.new_content,
                mode=entry.mode,
            )
        # FullReplacement / UnifiedPatch both need the authoritative base.
        base = base_contents.get(entry.path)
        if base is None:
            raise CandidateError(
                "patch_does_not_apply",
                f"{entry.path}: no authoritative base content at the attempt base",
            )
        if entry.base_blob_digest:
            _verify_base_digest(entry.path, base, entry.base_blob_digest)
        if isinstance(entry, FullReplacement):
            content = entry.new_content
            hunks: tuple[DiffHunk, ...] = ()
        else:
            if isinstance(base, bytes):
                try:
                    base = base.decode("utf-8")
                except UnicodeDecodeError:
                    raise CandidateError(
                        "binary_requires_binary_patch", "text patch targets a non-UTF-8 blob"
                    ) from None
            content = apply_unified_hunks(base, entry.hunks, path=entry.path)
            hunks = entry.hunks
        _verify_result_digest(entry.path, content, entry.intended_digest)
        _enforce_cap(entry.path, content)
        return ChangeManifestEntry(
            path=entry.path,
            operation="modify",
            new_content=content,
            mode=entry.mode,
            hunks=hunks,
        )


def bundle_from_changeset(cs: ChangeSet, *, attempt_base_oid: str) -> CandidateBundle:
    """Wrap an already-materialized ChangeSet as a candidate bundle."""
    # ChangeSet operation (create/update/delete) → manifest operation; typed so the manifest
    # Literal is checked at this boundary, not erased to str.
    operation_map: dict[str, CandidateOperation] = {
        "create": "create",
        "update": "modify",
        "delete": "delete",
    }
    entries: list[ChangeEntry] = []
    for change in cs.changes:
        path: str = change.path
        operation = operation_map[change.operation.value]
        content = change.content
        if isinstance(content, BlobContent):
            raise CandidateError(
                "binary_requires_native_manifest",
                "binary changes require exact-base native manifest validation",
            )
        if operation == "delete":
            entries.append(DeleteFile(path=path))
        elif content is None:
            raise CandidateError(
                "ambiguous_representation",
                f"{path}: {operation} entry without full content",
            )
        elif operation == "create":
            entries.append(CreateFile(path=path, new_content=content))
        else:
            entries.append(FullReplacement(path=path, new_content=content))
    return CandidateBundle(
        attempt_base_oid=attempt_base_oid, driver_exit="completed", entries=tuple(entries)
    )


def candidate_base_from_meta(meta: dict[str, Any]) -> str:
    """Read the lane's base claim without accepting conflicting aliases."""
    primary = str(meta.get("attempt_base") or "")
    alias = str(meta.get("attempt_base_oid") or "")
    if primary and alias and primary != alias:
        raise CandidateError("attempt_base_conflict", "candidate attempt base aliases disagree")
    return primary or alias


_OPERATION_MAP: dict[str, Operation] = {
    "create": Operation.CREATE,
    "modify": Operation.UPDATE,
    "delete": Operation.DELETE,
}


class PolicyViolation(Exception):
    """The publication boundary rejected a candidate on write policy."""

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__("; ".join(violations))


@dataclass(frozen=True)
class ValidatedCandidate:
    """A candidate that crossed the publication boundary (ADR-0026)."""

    bundle: CandidateBundle
    #: The completed manifest (full contents) the digests were verified on.
    manifest: tuple[ChangeManifestEntry, ...]
    #: The native-adapter payload mapped 1:1 from the manifest.
    changeset: ChangeSet
    #: The frozen scope the paths were validated against (empty = unscoped).
    allowed_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "manifest", tuple(self.manifest))
        object.__setattr__(self, "allowed_paths", tuple(self.allowed_paths))


def manifest_to_changeset(
    entries: Iterable[ChangeManifestEntry],
    *,
    branch: str,
    commit_message: str,
    attempt_base_oid: str = "",
) -> ChangeSet:
    """Map a completed manifest onto the write-path :class:`ChangeSet`."""
    return ChangeSet(
        branch=branch,
        commit_message=commit_message,
        changes=[
            Change(
                path=entry.path,
                operation=_OPERATION_MAP[entry.operation],
                content=entry.new_blob if entry.new_blob is not None else entry.new_content,
            )
            for entry in entries
        ],
        attempt_base_oid=attempt_base_oid or None,
    )


def validate_candidate_bundle(
    bundle: CandidateBundle,
    *,
    base_contents: Mapping[str, str | bytes],
    branch: str,
    commit_message: str,
    allowed_paths: Sequence[str] = (),
    policy: PathPolicy | None = None,
    base_modes: Mapping[str, str] | None = None,
) -> ValidatedCandidate:
    """The pure publication-boundary check, shared by EVERY backend (ADR-0026)."""
    if bundle.is_empty:
        raise CandidateError("empty_candidate", "candidate contains no changes")
    _reject_duplicate_paths(bundle)
    entries = tuple(bundle.materialize(base_contents, base_modes))
    for entry in entries:
        if entry.mode == "120000" and entry.operation != "delete":
            _validate_link_target(entry)
    changeset = manifest_to_changeset(
        entries,
        branch=branch,
        commit_message=commit_message,
        attempt_base_oid=bundle.attempt_base_oid,
    )
    violations = validate_changeset(
        changeset,
        base_contents,
        allowed_paths=list(allowed_paths),
        policy=policy,
    )
    violations.extend(
        f"change {entry.path!r} (create): file already exists in the base snapshot"
        for entry in entries
        if entry.operation == "create" and entry.path in base_contents
    )
    if violations:
        raise PolicyViolation(violations)
    return ValidatedCandidate(
        bundle=bundle,
        manifest=entries,
        changeset=changeset,
        allowed_paths=tuple(allowed_paths),
    )


def _reject_duplicate_paths(bundle: CandidateBundle) -> None:
    """Refuse a bundle with two entries for one canonical path."""
    seen: set[str] = set()
    for path in bundle.paths:
        canonical = normalize_repo_path(path)
        if canonical and canonical in seen:
            raise CandidateError(
                "duplicate_path",
                f"{path}: duplicate path ({canonical!r}) — one entry per path per candidate",
            )
        seen.add(canonical)


def _materialize_binary(
    entry: BinaryPatch | BlobReplacement | ModeChange,
    base_contents: Mapping[str, str | bytes],
    base_modes: Mapping[str, str] | None,
) -> ChangeManifestEntry:
    if entry.mode not in ("100644", "100755", "120000"):
        raise CandidateError("unsupported_mode", "candidate mode is not a supported Git blob mode")
    base = base_contents.get(entry.path)
    if isinstance(base, str):
        base = base.encode("utf-8")
    if isinstance(entry, ModeChange):
        if base is None or base_modes is None or base_modes.get(entry.path) != entry.old_mode:
            raise CandidateError(
                "stale_base", "mode change requires the exact authoritative base mode"
            )
        if entry.old_mode not in ("100644", "100755", "120000"):
            raise CandidateError(
                "unsupported_mode", "candidate old mode is not a supported blob mode"
            )
        content = base
    else:
        if entry.operation == "modify" and base is None:
            raise CandidateError("stale_base", "binary update lacks an authoritative base")
        if entry.base_blob_digest:
            _verify_base_digest(entry.path, base or b"", entry.base_blob_digest)
        content = (
            entry.new_blob.raw
            if isinstance(entry, BlobReplacement)
            else entry.forward.apply(base or b"")
        )
        _verify_result_digest(entry.path, content, entry.intended_digest)
    return ChangeManifestEntry(
        entry.path, entry.operation, mode=entry.mode, new_blob=BlobContent(raw=content)
    )


def _validate_link_target(entry: ChangeManifestEntry) -> None:
    try:
        target = (entry.content_bytes or b"").decode("utf-8")
    except UnicodeDecodeError:
        raise CandidateError("unsafe_symlink", "symlink target must be UTF-8") from None
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(entry.path), target))
    if (
        not target
        or target.startswith(("/", "\\"))
        or "\\" in target
        or ":" in target
        or any(ord(char) < 32 or ord(char) == 127 for char in target)
        or resolved in (".", "..")
        or resolved.startswith("../")
        or private_repo_path(resolved)
    ):
        raise CandidateError("unsafe_symlink", "symlink target escapes repository authority")
