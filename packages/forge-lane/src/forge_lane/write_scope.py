"""Apply the frozen write-scope knobs without importing the server domain."""

from fnmatch import fnmatchcase

from forge_protocol.spec import FrozenWriteScope

LOCKFILES = frozenset(
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


def writable(scope: FrozenWriteScope, path: str) -> bool:
    if path in scope.sensitive_paths:
        return False
    permitted = any(fnmatchcase(path, pattern) for pattern in scope.permitted_globs)
    if not permitted and (
        path in scope.denied_paths
        or any(path.startswith(prefix) for prefix in scope.denied_prefixes)
        or any(fnmatchcase(path, pattern) for pattern in scope.denied_globs)
        or scope.deny_lockfiles
        and (path.rsplit("/", 1)[-1].lower() in LOCKFILES or path.lower().endswith(".lock"))
    ):
        return False
    return not scope.allowed_globs or any(
        fnmatchcase(path, pattern) for pattern in scope.allowed_globs
    )
