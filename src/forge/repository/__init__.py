"""Repository write surface (M1): ChangeSet contract + GitLab changeset writer.

Public surface:

- :class:`forge.repository.changeset.ChangeSet` / ``Change`` / ``Operation``
  and :func:`validate_changeset` — the trusted ADR-0001 write contract.
- :class:`forge.repository.writer.ChangesetWriter` — journaled, reconcilable
  commits via the GitLab Commits API (ADR-0005).
"""

from forge.repository.changeset import (
    DENIED_PATHS,
    DENIED_PREFIXES,
    LOCKFILE_SUFFIX,
    MAX_CHANGES,
    MAX_CHANGE_BYTES,
    Change,
    ChangeSet,
    Operation,
    is_lockfile,
    validate_changeset,
)
from forge.repository.writer import ChangesetWriter, WriteOutcome, WriteResult

__all__ = [
    "DENIED_PATHS",
    "DENIED_PREFIXES",
    "LOCKFILE_SUFFIX",
    "MAX_CHANGES",
    "MAX_CHANGE_BYTES",
    "Change",
    "ChangeSet",
    "ChangesetWriter",
    "Operation",
    "WriteOutcome",
    "WriteResult",
    "is_lockfile",
    "validate_changeset",
]
