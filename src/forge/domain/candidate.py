"""Pure candidate representations and the validated publication capability."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

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
from .paths import PathPolicy, normalize_repo_path, validate_changeset


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


ChangeEntry = CreateFile | DeleteFile | FullReplacement | UnifiedPatch


@dataclass(frozen=True)
class ChangeManifestEntry:
    """The completed, flat form:meth:`CandidateBundle.materialize` returns."""

    path: str
    operation: CandidateOperation
    new_content: str | None = None
    mode: str = "100644"
    hunks: tuple[DiffHunk, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "hunks", tuple(self.hunks))


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

    def materialize(self, base_contents: dict[str, str]) -> list[ChangeManifestEntry]:
        """Complete every entry against the authoritative base texts."""
        completed: list[ChangeManifestEntry] = []
        for entry in self.entries:
            completed.append(self._materialize_entry(entry, base_contents))
        return completed

    @staticmethod
    def _materialize_entry(
        entry: ChangeEntry, base_contents: dict[str, str]
    ) -> ChangeManifestEntry:
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
        content: str | None = change.content
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
                content=entry.new_content,
            )
            for entry in entries
        ],
        attempt_base_oid=attempt_base_oid or None,
    )


def validate_candidate_bundle(
    bundle: CandidateBundle,
    *,
    base_contents: dict[str, str],
    branch: str,
    commit_message: str,
    allowed_paths: Sequence[str] = (),
    policy: PathPolicy | None = None,
) -> ValidatedCandidate:
    """The pure publication-boundary check, shared by EVERY backend (ADR-0026)."""
    if bundle.is_empty:
        raise CandidateError("empty_candidate", "candidate contains no changes")
    _reject_duplicate_paths(bundle)
    entries = tuple(bundle.materialize(base_contents))
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
