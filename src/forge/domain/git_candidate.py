"""Byte-preserving Git candidate materialization against an authoritative snapshot."""

from collections.abc import Mapping
from typing import Literal
from dataclasses import replace

from .blob import BlobContent
from .candidate import (
    BinaryPatch,
    BlobReplacement,
    CandidateBundle,
    ChangeEntry,
    CreateFile,
    DeleteFile,
    ModeChange,
    UnifiedPatch,
)
from .candidate_parser import parse_diff_bytes
from .patch import CandidateError, apply_unified_hunks, _verify_base_digest
from .paths import MAX_CHANGES


def _native_diff(diff: bytes, base_sha: str) -> CandidateBundle:
    if len(diff) > 16 * 1024 * 1024:
        raise CandidateError("candidate_too_large", "candidate diff exceeds protocol blob bound")
    parsed = parse_diff_bytes(diff, base_sha)
    if len(set(entry.path for entry in parsed.entries)) > MAX_CHANGES:
        raise CandidateError("too_many_changes", "candidate exceeds the path count bound")
    return parsed


def candidate_paths(diff: bytes, *, base_sha: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(entry.path for entry in _native_diff(diff, base_sha).entries))


def parse_git_candidate(
    diff: bytes,
    *,
    base_sha: str,
    base_contents: Mapping[str, bytes],
) -> CandidateBundle:
    # Latin-1 is a byte bijection, including invalid UTF-8 and CR/no-final-LF. It is
    # local parser state only; every persisted post-image is immutable base64 bytes.
    parsed = _native_diff(diff, base_sha)
    entries = _coalesce(tuple(_snapshot(entry, base_contents) for entry in parsed.entries))
    return CandidateBundle(base_sha, "completed", entries)


def _snapshot(entry: ChangeEntry, base: Mapping[str, bytes]) -> ChangeEntry:
    if isinstance(entry, DeleteFile):
        if not entry.base_blob_digest.startswith("blob:"):
            raise CandidateError(
                "full_index_required", "native delete requires its full original blob identity"
            )
        return entry
    if isinstance(entry, (BinaryPatch, BlobReplacement, ModeChange)):
        return entry
    operation: Literal["create", "modify"]
    if isinstance(entry, CreateFile):
        content = entry.new_content.encode("latin1")
        operation = "create"
        base_digest = ""
    elif isinstance(entry, UnifiedPatch):
        current = base.get(entry.path)
        if current is None:
            raise CandidateError("stale_base", "text patch has no authoritative base blob")
        if not entry.base_blob_digest.startswith("blob:"):
            raise CandidateError(
                "full_index_required", "native patch requires its full original blob identity"
            )
        _verify_base_digest(entry.path, current, entry.base_blob_digest)
        content = apply_unified_hunks(
            current.decode("latin1"), entry.hunks, path=entry.path
        ).encode("latin1")
        operation = "modify"
        base_digest = entry.base_blob_digest
    else:
        raise CandidateError(
            "ambiguous_representation", "Git diff produced a non-native representation"
        )
    if not entry.intended_digest.startswith("blob:") or (
        operation == "modify" and not base_digest.startswith("blob:")
    ):
        raise CandidateError(
            "full_index_required", "native candidate requires full Git old and new blob identities"
        )
    return BlobReplacement(
        entry.path,
        BlobContent(raw=content),
        operation,
        entry.mode,
        base_digest,
        entry.intended_digest,
    )


def _coalesce(entries: tuple[ChangeEntry, ...]) -> tuple[ChangeEntry, ...]:
    grouped: dict[str, list[ChangeEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.path, []).append(entry)
    output: list[ChangeEntry] = []
    for group in grouped.values():
        deletes = [entry for entry in group if isinstance(entry, DeleteFile)]
        creates = [
            entry
            for entry in group
            if isinstance(entry, (BinaryPatch, BlobReplacement)) and entry.operation == "create"
        ]
        if len(group) == 2 and len(deletes) == len(creates) == 1:
            if not deletes[0].base_blob_digest.startswith("blob:"):
                raise CandidateError(
                    "full_index_required", "type change requires a full original blob identity"
                )
            output.append(
                replace(
                    creates[0], operation="modify", base_blob_digest=deletes[0].base_blob_digest
                )
            )
        else:
            output.extend(group)
    return tuple(output)
