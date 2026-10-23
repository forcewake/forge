"""Validate candidate paths, operations and bounds before publication."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Any

FORGE_MATERIALIZE_MAX_FILE_CHARS = 400_000


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


@dataclass(frozen=True)
class ChangeSet:
    """A proposed atomic commit: branch, message and the file actions."""

    branch: str
    commit_message: str
    changes: list[Change]
    attempt_base_oid: str | None = None


class MaterializationError(Exception):
    """A raw ChangeSet draft cannot be materialized against the base content."""


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

#: Default exact-match count an ``update``'s ``old_text`` must have in the base content
#: (ADR-0001: ambiguous operations are rejected).
DEFAULT_EXPECTED_MATCHES = 1

_OPERATION_ALIASES: dict[str, Operation] = {op.value: op for op in Operation}


# Write-policy profiles A profile names ONE
# write policy: what may never be written, what is explicitly permitted despite a deny, and
# whether publishing under it needs an extra operator gate.


#: The write profile used when nothing else is configured.
DEFAULT_WRITE_PROFILE = "no_dependencies"

#: The closed set of built-in profile names (custom profiles must not shadow them — a
#: same-named override could silently RELAX the default).
BUILTIN_WRITE_PROFILES: tuple[str, ...] = (
    "no_dependencies",
    "code_only",
    "dependency_update",
    "ci_change",
)

#: Source-code file suffixes writable under ``code_only`` — everything else (docs,
#: manifests, lockfiles, images, CI) is outside that profile.
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

#: fnmatch globs for the suffixes above (``*`` spans ``/`` in fnmatch, so a suffix glob
#: matches at any depth).
CODE_ONLY_GLOBS: tuple[str, ...] = tuple(f"*{suffix}" for suffix in CODE_ONLY_SUFFIXES)

#: CI files the ``ci_change`` profile permits despite the base denies: exactly what the base
#: denylist names.
CI_PERMITTED_GLOBS: tuple[str, ...] = (".gitlab-ci.yml", ".github/*")


@dataclass(frozen=True)
class WritePolicy:
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


def normalize_repo_path(path: str) -> str:
    """Canonical repo-relative spelling of *path*."""
    unified = path.replace("\\", "/")
    parts = [part for part in unified.split("/") if part and part != "."]
    return "/".join(parts)


def _builtin_policy(name: str) -> WritePolicy:
    """The built-in profile *name*, resolved from the LIVE module constants
    (so a monkeypatched denylist shapes the builtins, as before)."""
    if name == "no_dependencies":
        return WritePolicy(
            name=name,
            denied_paths=DENIED_PATHS,
            denied_prefixes=DENIED_PREFIXES,
        )
    if name == "code_only":
        return WritePolicy(
            name=name,
            denied_paths=DENIED_PATHS,
            denied_prefixes=DENIED_PREFIXES,
            allowed_globs=CODE_ONLY_GLOBS,
        )
    if name == "dependency_update":
        # Lockfiles and manifests are writable; everything else stays per the base denies
        # (CI and forge's own config).
        return WritePolicy(
            name=name,
            denied_paths=DENIED_PATHS,
            denied_prefixes=DENIED_PREFIXES,
            deny_lockfiles=False,
        )
    if name == "ci_change":
        return WritePolicy(
            name=name,
            denied_paths=frozenset({".forge.yml"}),
            denied_prefixes=(),
            permitted_globs=CI_PERMITTED_GLOBS,
            require_special_approval=True,
        )
    raise ValueError(f"unknown write profile {name!r}")  # pragma: no cover - guarded caller


def _policy_from_custom(name: str, entry: Mapping[str, Any]) -> WritePolicy:
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
    return WritePolicy(
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
) -> WritePolicy:
    """The effective:class:`WritePolicy` for profile *name*."""
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


def materialize(cs_raw: dict[str, Any], git_base: dict[str, str]) -> ChangeSet:
    """Materialize a raw (untrusted, usually LLM-emitted) ChangeSet dict."""
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
    seen_paths: set[str] = set()
    for raw in raw_changes:
        change = _materialize_change(raw, git_base)
        canonical = normalize_repo_path(change.path)
        if canonical and canonical in seen_paths:
            # Two actions for one path in one commit is ambiguous for the Commits API — a
            # spelling difference does not hide it.
            raise MaterializationError(
                f"change {change.path!r}: duplicate path ({canonical!r}) — "
                f"one action per path per changeset"
            )
        seen_paths.add(canonical)
        changes.append(change)
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
    policy: WritePolicy | None = None,
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
        where = f"change {change.path!r} ({change.operation.value})"

        if not change.path or not change.path.strip():
            violations.append("change path must not be empty")
            continue

        canonical = normalize_repo_path(change.path)
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

        if change.operation is Operation.CREATE and change.content is None:
            violations.append(f"{where}: create requires content")

        if change.operation is Operation.DELETE and change.content is not None:
            violations.append(f"{where}: delete must not carry content")

        if change.content is not None and len(change.content.encode("utf-8")) > MAX_CHANGE_BYTES:
            violations.append(f"{where}: content exceeds {MAX_CHANGE_BYTES} bytes")

        if git_base is not None and change.operation in (Operation.UPDATE, Operation.DELETE):
            if change.path not in git_base:
                violations.append(f"{where}: file does not exist in the approved base snapshot")

    return violations


# Document round-trip (durable attempt manifest)
#


def changeset_to_document(cs: ChangeSet) -> dict[str, Any]:
    """Serialize a materialized ChangeSet into a JSON-safe document."""
    return {
        "branch": cs.branch,
        "commit_message": cs.commit_message,
        "attempt_base_oid": cs.attempt_base_oid,
        "changes": [
            {
                "path": change.path,
                "operation": change.operation.value,
                "content": change.content,
            }
            for change in cs.changes
        ],
    }


def changeset_from_document(data: object) -> ChangeSet | None:
    """Rebuild the ChangeSet *data* was written from; None when it is not one."""
    if not isinstance(data, dict):
        return None
    branch = data.get("branch")
    commit_message = data.get("commit_message")
    raw_changes = data.get("changes")
    if not isinstance(branch, str) or not isinstance(commit_message, str):
        return None
    if not isinstance(raw_changes, list) or not raw_changes:
        return None
    changes: list[Change] = []
    for raw in raw_changes:
        if not isinstance(raw, dict):
            return None
        operation = _OPERATION_ALIASES.get(str(raw.get("operation") or ""))
        path = raw.get("path")
        content = raw.get("content")
        if operation is None or not isinstance(path, str) or not path:
            return None
        if content is not None and not isinstance(content, str):
            return None
        changes.append(Change(path=path, operation=operation, content=content))
    attempt_base_oid = data.get("attempt_base_oid")
    if not isinstance(attempt_base_oid, str):
        attempt_base_oid = None
    return ChangeSet(
        branch=branch,
        commit_message=commit_message,
        changes=changes,
        attempt_base_oid=attempt_base_oid,
    )
