"""ChangeSet: the trusted write contract between proposers and GitLab (ADR-0001).

A proposer (LLM implementer agent) emits a strict-JSON ChangeSet draft;
:func:`materialize` turns it into a typed ChangeSet against the actual base
content, and :func:`validate_changeset` is the trusted validation layer that
checks it before anything reaches the Commits API. Authority is never derived
from the proposal — paths, project and branch are constrained by trusted
policy here.

All limits and denylists are module-level constants so tests (and future
policy configuration) can monkeypatch them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Any


class Operation(StrEnum):
    """File action supported by the GitLab Commits API in M1."""

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


@dataclass(frozen=True)
class ChangeSet:
    """A proposed atomic commit: branch, message and the file actions.

    ``attempt_base_oid`` records the snapshot the proposal was materialized
    against (which commit a repair extends); trusted proposers set it —
    like branch and message it is never taken from the model's word alone.
    """

    branch: str
    commit_message: str
    changes: list[Change]
    attempt_base_oid: str | None = None


class MaterializationError(Exception):
    """A raw ChangeSet draft cannot be materialized against the base content.

    Raised on zero or ambiguous ``old_text`` matches, on a missing
    base file for update/delete, or on a create without content — ADR-0001
    forbids fuzzy matching, ever.
    """


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

#: Default exact-match count an ``update``'s ``old_text`` must have in the
#: base content (ADR-0001: ambiguous operations are rejected).
DEFAULT_EXPECTED_MATCHES = 1

_OPERATION_ALIASES: dict[str, Operation] = {op.value: op for op in Operation}


def is_lockfile(path: str) -> bool:
    """Return True when *path* looks like a dependency lockfile."""
    name = path.rsplit("/", 1)[-1].lower()
    return name in DENIED_LOCKFILE_NAMES or name.endswith(LOCKFILE_SUFFIX)


def in_path_scope(path: str, allowed_paths: list[str]) -> bool:
    """Whether *path* matches at least one of the *allowed_paths* globs.

    Monorepo path scoping (docs/research/complex-projects.md §1): a work
    package may carry a path allowlist; a change outside it is a failed run,
    not a review comment. Globs are fnmatch-style and matched against the
    repo-relative path — ``*`` also spans ``/``, so ``services/api/*``
    covers nested files without needing ``**``.
    """
    return any(fnmatchcase(path, glob) for glob in allowed_paths if glob)


def materialize(cs_raw: dict[str, Any], git_base: dict[str, str]) -> ChangeSet:
    """Materialize a raw (untrusted, usually LLM-emitted) ChangeSet dict.

    *git_base* maps path -> current file content at the caller's base
    snapshot — the attempt base, read by the trusted caller (ADR-0006; a
    repair cycle reads its previous candidate, not the gate-approved base).
    ADR-0001 rules, with no fuzzy matching ever:

    - ``create``: full ``content`` required; the file must not exist in the
      base snapshot.
    - ``update``: ``old_text`` must occur exactly ``expected_matches`` times
      (default 1) in the base content; the materialized content is
      ``base.replace(old_text, new_text, expected_matches)``.
    - ``delete``: no content; the file must exist in the base snapshot.

    An optional ``attempt_base_oid`` string on the draft is carried through
    as trusted metadata (the snapshot *git_base* was read at); anything else
    is dropped.

    Raises :class:`MaterializationError` (zero/>expected matches, wrong
    shapes) — the caller decides what that means for the run.
    """
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
    for raw in raw_changes:
        changes.append(_materialize_change(raw, git_base))
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


def validate_changeset(
    cs: ChangeSet,
    git_base: dict[str, str] | None = None,
    allowed_paths: list[str] | None = None,
) -> list[str]:
    """Validate *cs* against the write policy and return all violations.

    An empty list means the ChangeSet may be committed. Every violation is a
    human-readable string; validation never raises for proposal content (it
    reports instead), only trusted callers decide what a violation means for
    the run.

    When *git_base* (path -> base content at the caller's snapshot) is given,
    ADR-0001 existence rules are enforced on top of the path policy: an
    ``update``/``delete`` must address a file that exists in the snapshot.
    The snapshot is the ATTEMPT base (a repair's previous candidate), so a
    file an earlier cycle created passes this check (R06).

    When *allowed_paths* (v0.7 monorepo path scoping, complex-projects.md §1)
    is non-empty, every change must fall under at least one glob — a change
    outside the work package's scope is rejected with a clear reason (the
    publisher and the builtin validation both enforce this; the plan prompt
    carries the same restriction so agents aim inside it from the start).
    Nested per-directory instruction files (CLAUDE.md / AGENTS.md) need no
    forge-side resolution: coding CLIs load them natively for the paths they
    touch (complex-projects.md §1.3) — the scope check stays purely on the
    write boundary.
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

        if allowed_paths and not in_path_scope(change.path, allowed_paths):
            violations.append(
                f"{where}: path is outside the allowed scope "
                f"(allowed_paths: {', '.join(allowed_paths)})"
            )

        if change.operation is Operation.CREATE and change.content is None:
            violations.append(f"{where}: create requires content")

        if change.operation is Operation.DELETE and change.content is not None:
            violations.append(f"{where}: delete must not carry content")

        if change.content is not None and len(change.content.encode("utf-8")) > MAX_CHANGE_BYTES:
            violations.append(f"{where}: content exceeds {MAX_CHANGE_BYTES} bytes")

        if git_base is not None and change.operation in (Operation.UPDATE, Operation.DELETE):
            if change.path not in git_base:
                violations.append(f"{where}: file does not exist in the attempt base snapshot")

    return violations
