"""Immutable write policy and pure canonical-path validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from typing import Any

from .blob import BlobContent
from .change_types import Change, ChangeSet, Operation

DENIED_PATHS: frozenset[str] = frozenset({".gitlab-ci.yml", ".forge.yml"})


DENIED_PREFIXES: tuple[str, ...] = (".github/",)


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


LOCKFILE_SUFFIX = ".lock"


MAX_CHANGES = 20


MAX_CHANGE_BYTES = 256 * 1024


DEFAULT_WRITE_PROFILE = "no_dependencies"


BUILTIN_WRITE_PROFILES: tuple[str, ...] = (
    "no_dependencies",
    "code_only",
    "dependency_update",
    "ci_change",
)


CODE_ONLY_SUFFIXES: tuple[str, ...] = (
    ".py",
    ".pyi",
    ".rb",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".kts",
    ".swift",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".hpp",
    ".cs",
    ".m",
    ".mm",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".php",
    ".pl",
    ".sh",
    ".bash",
    ".zsh",
    ".ps1",
    ".sql",
    ".lua",
    ".dart",
    ".scala",
    ".ex",
    ".exs",
    ".zig",
    ".vue",
    ".svelte",
)


CODE_ONLY_GLOBS: tuple[str, ...] = tuple(f"*{suffix}" for suffix in CODE_ONLY_SUFFIXES)


CI_PERMITTED_GLOBS: tuple[str, ...] = (".gitlab-ci.yml", ".github/*")


@dataclass(frozen=True)
class PathPolicy:
    """One named write policy — the deny/permit knobs of a profile."""

    name: str
    denied_paths: frozenset[str]
    denied_prefixes: tuple[str, ...]
    #: Provider-sensitive paths (e.g. the project's pipeline entrypoints): denied under
    #: EVERY profile — even where a permitted glob would lift the ordinary denies.
    sensitive_paths: frozenset[str] = frozenset()
    denied_globs: tuple[str, ...] = ()
    permitted_globs: tuple[str, ...] = ()
    allowed_globs: tuple[str, ...] = ()
    deny_lockfiles: bool = True
    #: Publishing under this profile requires the operator approver set (``FORGE_APPROVERS``
    #: and friends) — enforced by the publisher, which refuses the publication when the run
    #: carries no operator-approved frozen plan and the caller asserts no explicit approval.
    require_special_approval: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("write policy name must not be empty")
        for name in ("denied_paths", "sensitive_paths"):
            object.__setattr__(self, name, frozenset(getattr(self, name)))
        for name in ("denied_prefixes", "denied_globs", "permitted_globs", "allowed_globs"):
            object.__setattr__(self, name, tuple(getattr(self, name)))


def normalize_repo_path(path: str) -> str:
    """Canonical repo-relative spelling of *path*."""
    unified = path.replace("\\", "/")
    parts = [part for part in unified.split("/") if part and part != "."]
    return "/".join(parts)


def _builtin_policy(name: str) -> PathPolicy:
    """The built-in profile *name*, resolved from the LIVE module constants
    (so a monkeypatched denylist shapes the builtins, as before)."""
    if name == "no_dependencies":
        return PathPolicy(
            name=name,
            denied_paths=DENIED_PATHS,
            denied_prefixes=DENIED_PREFIXES,
        )
    if name == "code_only":
        return PathPolicy(
            name=name,
            denied_paths=DENIED_PATHS,
            denied_prefixes=DENIED_PREFIXES,
            allowed_globs=CODE_ONLY_GLOBS,
        )
    if name == "dependency_update":
        # Lockfiles and manifests are writable; everything else stays per the base denies
        # (CI and forge's own config).
        return PathPolicy(
            name=name,
            denied_paths=DENIED_PATHS,
            denied_prefixes=DENIED_PREFIXES,
            deny_lockfiles=False,
        )
    if name == "ci_change":
        return PathPolicy(
            name=name,
            denied_paths=frozenset({".forge.yml"}),
            denied_prefixes=(),
            permitted_globs=CI_PERMITTED_GLOBS,
            require_special_approval=True,
        )
    raise ValueError(f"unknown write profile {name!r}")  # pragma: no cover - guarded caller


def _policy_from_custom(name: str, entry: Mapping[str, Any]) -> PathPolicy:
    """A custom profile from parsed config (tighten-only over the base)."""
    denied_globs = tuple(
        normalize_repo_path(str(glob))
        for glob in (entry.get("denied_paths") or [])
        if str(glob).strip()
    )
    permitted_globs = tuple(
        normalize_repo_path(str(glob))
        for glob in (entry.get("allowed_paths") or [])
        if str(glob).strip()
    )
    special = bool(entry.get("require_special_approval", False))
    return PathPolicy(
        name=name,
        denied_paths=DENIED_PATHS,
        denied_prefixes=DENIED_PREFIXES,
        denied_globs=denied_globs,
        permitted_globs=permitted_globs,
        deny_lockfiles=True,
        require_special_approval=special,
    )


def resolve_write_policy(
    name: str | None = None,
    *,
    custom_profiles: Mapping[str, Mapping[str, Any]] | None = None,
    extra_denied_paths: Sequence[str] = (),
) -> PathPolicy:
    """The effective:class:`PathPolicy` for profile *name*."""
    key = (name or DEFAULT_WRITE_PROFILE).strip() or DEFAULT_WRITE_PROFILE
    profiles = dict(custom_profiles or {})
    if key in profiles and key in BUILTIN_WRITE_PROFILES:
        raise ValueError(f"write profile {key!r} shadows a built-in profile — choose another name")
    if key in BUILTIN_WRITE_PROFILES:
        policy = _builtin_policy(key)
    elif key in profiles:
        policy = _policy_from_custom(key, profiles[key])
    else:
        known = ", ".join((*BUILTIN_WRITE_PROFILES, *sorted(profiles)))
        raise ValueError(f"unknown write profile {key!r} (known profiles: {known})")
    sensitive = frozenset(
        normalize_repo_path(str(path)) for path in extra_denied_paths if str(path).strip()
    )
    if sensitive:
        policy = replace(policy, sensitive_paths=frozenset(policy.sensitive_paths) | sensitive)
    return policy


def is_lockfile(path: str) -> bool:
    """Return True when *path* looks like a dependency lockfile."""
    name = path.rsplit("/", 1)[-1].lower()
    return name in DENIED_LOCKFILE_NAMES or name.endswith(LOCKFILE_SUFFIX)


def in_path_scope(path: str, allowed_paths: list[str]) -> bool:
    """Whether *path* matches at least one of the *allowed_paths* globs."""
    return any(fnmatchcase(path, glob) for glob in allowed_paths if glob)


def private_repo_path(path: str) -> bool:
    parts = tuple(part.casefold() for part in path.split("/"))
    directories = {".git", ".forge", ".claude", ".copilot", ".ssh", ".aws", ".kube"}
    names = {
        ".forge.yml",
        ".mcp.json",
        ".gitconfig",
        ".gitmodules",
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials",
        "id_rsa",
        "id_ed25519",
    }
    return any(
        part in directories
        or part in names
        or part.startswith(".env")
        or part.endswith((".pem", ".key"))
        for part in parts
    )


def validate_changeset(
    cs: ChangeSet,
    git_base: Mapping[str, str | bytes] | None = None,
    allowed_paths: list[str] | None = None,
    policy: PathPolicy | None = None,
) -> list[str]:
    """Validate *cs* against the write policy and return all violations."""
    effective = policy if policy is not None else resolve_write_policy()
    violations: list[str] = []

    if not cs.commit_message.strip():
        violations.append("commit_message must not be empty")

    if not cs.changes:
        violations.append("changeset must contain at least one change")

    if len(cs.changes) > MAX_CHANGES:
        violations.append(f"changeset contains {len(cs.changes)} changes (max {MAX_CHANGES})")

    seen_paths: set[str] = set()
    for change in cs.changes:
        violations.extend(_validate_change(change, effective, git_base, allowed_paths, seen_paths))

    return violations


# Legacy callers retain the same value identity.
WritePolicy = PathPolicy


def _validate_change(
    change: Change,
    effective: PathPolicy,
    git_base: Mapping[str, str | bytes] | None,
    allowed_paths: list[str] | None,
    seen_paths: set[str],
) -> list[str]:
    violations: list[str] = []
    where = f"change {change.path!r} ({change.operation.value})"

    if not change.path or not change.path.strip():
        violations.append("change path must not be empty")
        return violations

    canonical = normalize_repo_path(change.path)
    if any(ord(char) < 32 or ord(char) == 127 for char in change.path):
        violations.append(f"{where}: control characters are not allowed")
    if private_repo_path(canonical):
        violations.append(f"{where}: private metadata or credential path")
    if canonical == ".forge" or canonical.startswith(".forge/"):
        violations.append(f"{where}: reserved forge runtime namespace")

    if canonical and canonical in seen_paths:
        violations.append(f"{where}: duplicate path ({canonical!r} is changed more than once)")
    seen_paths.add(canonical)

    if change.path.startswith(("/", "\\")) or (len(change.path) > 1 and change.path[1] == ":"):
        violations.append(f"{where}: absolute paths are not allowed")

    if ".." in canonical.split("/"):
        violations.append(f"{where}: path traversal ('..') is not allowed")

    # Provider-sensitive paths (pipeline entrypoints) are denied even where a profile's
    # permitted glob would lift the ordinary denies.
    if canonical in effective.sensitive_paths:
        violations.append(f"{where}: path is a protected pipeline entrypoint")
        permitted = False
    else:
        permitted = any(fnmatchcase(canonical, glob) for glob in effective.permitted_globs)

    if not permitted:
        if canonical in effective.denied_paths:
            violations.append(f"{where}: path is denylisted")

        if any(canonical.startswith(prefix) for prefix in effective.denied_prefixes):
            violations.append(f"{where}: path is under a denylisted prefix")

        if any(fnmatchcase(canonical, glob) for glob in effective.denied_globs):
            violations.append(f"{where}: path matches a denylisted pattern")

        if effective.deny_lockfiles and is_lockfile(canonical):
            violations.append(f"{where}: lockfiles are denylisted")

    if effective.allowed_globs and not any(
        fnmatchcase(canonical, glob) for glob in effective.allowed_globs
    ):
        violations.append(
            f"{where}: path is outside the {effective.name!r} write profile "
            f"(only the profile's paths are writable)"
        )

    if allowed_paths and not in_path_scope(canonical, allowed_paths):
        violations.append(
            f"{where}: path is outside the allowed scope "
            f"(allowed_paths: {', '.join(allowed_paths)})"
        )

    violations.extend(_validate_action(change, git_base, where))
    return violations


def _validate_action(
    change: Change, git_base: Mapping[str, str | bytes] | None, where: str
) -> list[str]:
    violations: list[str] = []
    if change.operation is Operation.CREATE and change.content is None:
        violations.append(f"{where}: create requires content")

    if change.operation is Operation.DELETE and change.content is not None:
        violations.append(f"{where}: delete must not carry content")

    content = change.content
    size = (
        content.size
        if isinstance(content, BlobContent)
        else len(content.encode("utf-8"))
        if content is not None
        else 0
    )
    if size > MAX_CHANGE_BYTES:
        violations.append(f"{where}: content exceeds {MAX_CHANGE_BYTES} bytes")

    if git_base is not None and change.operation in (Operation.UPDATE, Operation.DELETE):
        if change.path not in git_base:
            violations.append(f"{where}: file does not exist in the approved base snapshot")
    return violations
