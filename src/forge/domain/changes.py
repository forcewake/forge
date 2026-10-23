"""Pure materialization and durable JSON encoding of repository changes."""

from __future__ import annotations

from typing import Any

from .change_types import Change, ChangeSet, MaterializationError, Operation
from .paths import normalize_repo_path

DEFAULT_EXPECTED_MATCHES = 1


_OPERATION_ALIASES: dict[str, Operation] = {op.value: op for op in Operation}


def materialize(cs_raw: dict[str, Any], git_base: dict[str, str]) -> ChangeSet:
    """Materialize a raw (untrusted, usually LLM-emitted) ChangeSet dict."""
    if not isinstance(cs_raw, dict):
        raise MaterializationError("changeset draft must be a JSON object")

    branch = cs_raw.get("branch")
    commit_message = cs_raw.get("commit_message")
    if not isinstance(branch, str) or not branch.strip():
        raise MaterializationError("branch is required")
    if not isinstance(commit_message, str) or not commit_message.strip():
        raise MaterializationError("commit_message is required")
    raw_changes = cs_raw.get("changes")
    if not isinstance(raw_changes, list) or not raw_changes:
        raise MaterializationError("changes must be a non-empty list")

    raw_attempt_base = cs_raw.get("attempt_base_oid")
    attempt_base_oid = raw_attempt_base if isinstance(raw_attempt_base, str) else None

    changes: list[Change] = []
    seen_paths: set[str] = set()
    for raw in raw_changes:
        change = _materialize_change(raw, git_base)
        canonical = normalize_repo_path(change.path)
        if canonical and canonical in seen_paths:
            # Two actions for one path in one commit is ambiguous for the Commits API — a
            # spelling difference does not hide it.
            raise MaterializationError(
                f"change {change.path!r}: duplicate path ({canonical!r}) — "
                f"one action per path per changeset"
            )
        seen_paths.add(canonical)
        changes.append(change)
    return ChangeSet(
        branch=branch,
        commit_message=commit_message,
        changes=changes,
        attempt_base_oid=attempt_base_oid,
    )


def _materialize_change(raw: Any, git_base: dict[str, str]) -> Change:
    if not isinstance(raw, dict):
        raise MaterializationError("each change must be a JSON object")

    path = raw.get("path")
    if not isinstance(path, str) or not path.strip():
        raise MaterializationError("change path is required")

    operation_raw = raw.get("operation")
    operation = _OPERATION_ALIASES.get(operation_raw) if isinstance(operation_raw, str) else None
    if operation is None:
        raise MaterializationError(f"change {path!r}: unknown operation {operation_raw!r}")

    base_content = git_base.get(path)

    if operation is Operation.CREATE:
        content = raw.get("content")
        if not isinstance(content, str):
            raise MaterializationError(f"change {path!r}: create requires content")
        if base_content is not None:
            raise MaterializationError(f"change {path!r}: create but file already exists in base")
        return Change(path=path, operation=operation, content=content)

    if operation is Operation.UPDATE:
        old_text = raw.get("old_text")
        new_text = raw.get("new_text")
        expected = raw.get("expected_matches", DEFAULT_EXPECTED_MATCHES)
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            raise MaterializationError(f"change {path!r}: update requires old_text and new_text")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
            raise MaterializationError(f"change {path!r}: expected_matches must be a positive int")
        if base_content is None:
            raise MaterializationError(f"change {path!r}: update but file is not in base snapshot")
        matches = base_content.count(old_text)
        if matches != expected:
            raise MaterializationError(
                f"change {path!r}: old_text matches {matches} time(s), "
                f"expected exactly {expected} — refusing fuzzy apply"
            )
        materialized = base_content.replace(old_text, new_text, expected)
        return Change(path=path, operation=operation, content=materialized)

    # Operation.DELETE
    if base_content is None:
        raise MaterializationError(f"change {path!r}: delete but file is not in base snapshot")
    return Change(path=path, operation=operation, content=None)


def changeset_to_document(cs: ChangeSet) -> dict[str, Any]:
    """Serialize a materialized ChangeSet into a JSON-safe document."""
    return {
        "branch": cs.branch,
        "commit_message": cs.commit_message,
        "attempt_base_oid": cs.attempt_base_oid,
        "changes": [
            {
                "path": change.path,
                "operation": change.operation.value,
                "content": change.content,
            }
            for change in cs.changes
        ],
    }


def changeset_from_document(data: object) -> ChangeSet | None:
    """Rebuild the ChangeSet *data* was written from; None when it is not one."""
    if not isinstance(data, dict):
        return None
    branch = data.get("branch")
    commit_message = data.get("commit_message")
    raw_changes = data.get("changes")
    if not isinstance(branch, str) or not isinstance(commit_message, str):
        return None
    if not isinstance(raw_changes, list) or not raw_changes:
        return None
    changes: list[Change] = []
    for raw in raw_changes:
        if not isinstance(raw, dict):
            return None
        operation = _OPERATION_ALIASES.get(str(raw.get("operation") or ""))
        path = raw.get("path")
        content = raw.get("content")
        if operation is None or not isinstance(path, str) or not path:
            return None
        if content is not None and not isinstance(content, str):
            return None
        changes.append(Change(path=path, operation=operation, content=content))
    attempt_base_oid = data.get("attempt_base_oid")
    if not isinstance(attempt_base_oid, str):
        attempt_base_oid = None
    return ChangeSet(
        branch=branch,
        commit_message=commit_message,
        changes=changes,
        attempt_base_oid=attempt_base_oid,
    )
