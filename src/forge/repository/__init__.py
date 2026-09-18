"""Repository write surface: ChangeSet contract + GitLab changeset writer.

Public surface:

- :class:`forge.repository.changeset.ChangeSet` / ``Change`` / ``Operation``,
  :func:`materialize` and :func:`validate_changeset` — the trusted ADR-0001
  write contract (strict JSON draft -> materialized, validated ChangeSet).
- :class:`forge.repository.writer.ChangesetWriter` — journaled, reconcilable
  commits via the GitLab Commits API (ADR-0005).
"""

from forge.repository.changeset import (
    BUILTIN_WRITE_PROFILES,
    CODE_ONLY_SUFFIXES,
    DEFAULT_EXPECTED_MATCHES,
    DEFAULT_WRITE_PROFILE,
    DENIED_PATHS,
    DENIED_PREFIXES,
    LOCKFILE_SUFFIX,
    MAX_CHANGES,
    MAX_CHANGE_BYTES,
    Change,
    ChangeSet,
    MaterializationError,
    Operation,
    WritePolicy,
    changeset_from_document,
    changeset_to_document,
    in_path_scope,
    is_lockfile,
    materialize,
    normalize_repo_path,
    resolve_write_policy,
    validate_changeset,
)
from forge.repository.writer import ChangesetWriter, WriteOutcome, WriteResult

__all__ = [
    "BUILTIN_WRITE_PROFILES",
    "CODE_ONLY_SUFFIXES",
    "DEFAULT_EXPECTED_MATCHES",
    "DEFAULT_WRITE_PROFILE",
    "DENIED_PATHS",
    "DENIED_PREFIXES",
    "LOCKFILE_SUFFIX",
    "MAX_CHANGES",
    "MAX_CHANGE_BYTES",
    "Change",
    "ChangeSet",
    "ChangesetWriter",
    "MaterializationError",
    "Operation",
    "WriteOutcome",
    "WritePolicy",
    "WriteResult",
    "changeset_from_document",
    "changeset_to_document",
    "in_path_scope",
    "is_lockfile",
    "materialize",
    "normalize_repo_path",
    "resolve_write_policy",
    "validate_changeset",
]
