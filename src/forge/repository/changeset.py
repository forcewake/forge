"""Legacy repository API delegates to the pure domain owners."""

from forge.domain.change_types import (
    Operation as Operation,
    Change as Change,
    ChangeSet as ChangeSet,
    MaterializationError as MaterializationError,
)

from forge.domain.changes import (
    DEFAULT_EXPECTED_MATCHES as DEFAULT_EXPECTED_MATCHES,
    materialize as materialize,
    changeset_to_document as changeset_to_document,
    changeset_from_document as changeset_from_document,
)

from forge.domain.paths import (
    DENIED_PATHS as DENIED_PATHS,
    DENIED_PREFIXES as DENIED_PREFIXES,
    DENIED_LOCKFILE_NAMES as DENIED_LOCKFILE_NAMES,
    LOCKFILE_SUFFIX as LOCKFILE_SUFFIX,
    MAX_CHANGES as MAX_CHANGES,
    MAX_CHANGE_BYTES as MAX_CHANGE_BYTES,
    DEFAULT_WRITE_PROFILE as DEFAULT_WRITE_PROFILE,
    BUILTIN_WRITE_PROFILES as BUILTIN_WRITE_PROFILES,
    CODE_ONLY_SUFFIXES as CODE_ONLY_SUFFIXES,
    CODE_ONLY_GLOBS as CODE_ONLY_GLOBS,
    CI_PERMITTED_GLOBS as CI_PERMITTED_GLOBS,
    WritePolicy as WritePolicy,
    normalize_repo_path as normalize_repo_path,
    resolve_write_policy as resolve_write_policy,
    is_lockfile as is_lockfile,
    in_path_scope as in_path_scope,
    validate_changeset as validate_changeset,
)

from forge.domain.patch import (
    FORGE_MATERIALIZE_MAX_FILE_CHARS as FORGE_MATERIALIZE_MAX_FILE_CHARS,
)
