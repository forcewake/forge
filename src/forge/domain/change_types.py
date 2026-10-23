"""Materialized atomic repository changes without transport or storage."""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class Operation(StrEnum):
    """File action supported by the GitLab Commits API."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


@dataclass(frozen=True)
class Change:
    """One file action. For ``create`` ``content`` is the full file text; for
    ``update`` it is the materialized replacement (base with ``old_text``
    swapped for ``new_text``); ``delete`` carries no content."""

    path: str
    operation: Operation
    content: str | None = None


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
