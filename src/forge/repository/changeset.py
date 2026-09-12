"""ChangeSet: the trusted write contract between proposers and GitLab (ADR-0001).

A proposer (stub in M1, an implementer agent later) emits a typed ChangeSet;
:func:`validate_changeset` is the trusted validation layer that checks it
before anything reaches the Commits API. Authority is never derived from the
proposal — paths, project and branch are constrained by trusted policy here.

All limits and denylists are module-level constants so tests (and future
policy configuration) can monkeypatch them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Operation(StrEnum):
    """File action supported by the GitLab Commits API in M1."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


@dataclass(frozen=True)
class Change:
    """One file action. ``content`` is required for create, optional for
    update (full replacement text), forbidden for delete."""

    path: str
    operation: Operation
    content: str | None = None


@dataclass(frozen=True)
class ChangeSet:
    """A proposed atomic commit: branch, message and the file actions."""

    branch: str
    commit_message: str
    changes: list[Change]


# --- Trusted validation policy (module-level so tests can monkeypatch) ------

#: Exact paths forge is never allowed to write (CI and forge's own config).
DENIED_PATHS: frozenset[str] = frozenset({".gitlab-ci.yml", ".forge.yml"})

#: Path prefixes forge is never allowed to write (any file beneath them).
DENIED_PREFIXES: tuple[str, ...] = (".github/",)

#: Exact well-known lockfile names (any casing).
DENIED_LOCKFILE_NAMES: frozenset[str] = frozenset(
    {
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "cargo.lock",
        "poetry.lock",
        "uv.lock",
        "gemfile.lock",
        "composer.lock",
        "pipfile.lock",
    }
)

#: Any path ending with this suffix is treated as a lockfile.
LOCKFILE_SUFFIX = ".lock"

#: Maximum number of file actions per commit.
MAX_CHANGES = 20

#: Maximum UTF-8 size of a single change's content (256 KiB).
MAX_CHANGE_BYTES = 256 * 1024


def is_lockfile(path: str) -> bool:
    """Return True when *path* looks like a dependency lockfile."""
    name = path.rsplit("/", 1)[-1].lower()
    return name in DENIED_LOCKFILE_NAMES or name.endswith(LOCKFILE_SUFFIX)


def validate_changeset(cs: ChangeSet) -> list[str]:
    """Validate *cs* against the M1 write policy and return all violations.

    An empty list means the ChangeSet may be materialized and committed.
    Every violation is a human-readable string; validation never raises for
    proposal content (it reports instead), only trusted callers decide what a
    violation means for the run.
    """
    violations: list[str] = []

    if not cs.commit_message.strip():
        violations.append("commit_message must not be empty")

    if not cs.changes:
        violations.append("changeset must contain at least one change")

    if len(cs.changes) > MAX_CHANGES:
        violations.append(f"changeset contains {len(cs.changes)} changes (max {MAX_CHANGES})")

    for change in cs.changes:
        where = f"change {change.path!r} ({change.operation.value})"

        if not change.path or not change.path.strip():
            violations.append("change path must not be empty")
            continue

        if change.path.startswith("/") or (len(change.path) > 1 and change.path[1] == ":"):
            violations.append(f"{where}: absolute paths are not allowed")

        if ".." in change.path.split("/"):
            violations.append(f"{where}: path traversal ('..') is not allowed")

        if change.path in DENIED_PATHS:
            violations.append(f"{where}: path is denylisted")

        if any(change.path.startswith(prefix) for prefix in DENIED_PREFIXES):
            violations.append(f"{where}: path is under a denylisted prefix")

        if is_lockfile(change.path):
            violations.append(f"{where}: lockfiles are denylisted")

        if change.operation is Operation.CREATE and change.content is None:
            violations.append(f"{where}: create requires content")

        if change.operation is Operation.DELETE and change.content is not None:
            violations.append(f"{where}: delete must not carry content")

        if change.content is not None and len(change.content.encode("utf-8")) > MAX_CHANGE_BYTES:
            violations.append(f"{where}: content exceeds {MAX_CHANGE_BYTES} bytes")

    return violations
