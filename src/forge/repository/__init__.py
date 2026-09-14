"""Repository write surface: ChangeSet contract + GitLab changeset writer.

Public surface:

- :class:`forge.repository.changeset.ChangeSet` / ``Change`` / ``Operation``,
  :func:`materialize` and :func:`validate_changeset` — the trusted ADR-0001
  write contract (strict JSON draft -> materialized, validated ChangeSet).
- :class:`forge.repository.writer.ChangesetWriter` — journaled, reconcilable
  commits via the GitLab Commits API (ADR-0005).
"""

from forge.repository.changeset import (
    DEFAULT_EXPECTED_MATCHES,
    DENIED_PATHS,
    DENIED_PREFIXES,
    LOCKFILE_SUFFIX,
    MAX_CHANGES,
    MAX_CHANGE_BYTES,
    Change,
    ChangeSet,
    MaterializationError,
    Operation,
    in_path_scope,
    is_lockfile,
    materialize,
    validate_changeset,
)
from forge.repository.writer import ChangesetWriter, WriteOutcome, WriteResult

__all__ = [
    "DEFAULT_EXPECTED_MATCHES",
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
    "WriteResult",
    "in_path_scope",
    "is_lockfile",
    "materialize",
    "validate_changeset",
]
