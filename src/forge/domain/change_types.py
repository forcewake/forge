"""Materialized atomic repository changes without transport or storage."""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from .blob import BlobContent


class Operation(StrEnum):
    """An atomic repository file action."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


@dataclass(frozen=True)
class Change:
    """Full immutable text or exact blob bytes; a delete carries no content."""

    path: str
    operation: Operation
    content: str | BlobContent | None = None


@dataclass(frozen=True, init=False)
class ChangeSet:
    """A proposed atomic commit: branch, message and the file actions."""

    branch: str
    commit_message: str
    changes: tuple[Change, ...]
    attempt_base_oid: str | None = None

    def __init__(
        self,
        branch: str,
        commit_message: str,
        changes: Iterable[Change],
        attempt_base_oid: str | None = None,
    ) -> None:
        object.__setattr__(self, "branch", branch)
        object.__setattr__(self, "commit_message", commit_message)
        object.__setattr__(self, "changes", tuple(changes))
        object.__setattr__(self, "attempt_base_oid", attempt_base_oid)


class MaterializationError(Exception):
    """A raw ChangeSet draft cannot be materialized against the base content."""
