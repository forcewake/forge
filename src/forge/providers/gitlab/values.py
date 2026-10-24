"""Strict native observations and credential-free GitLab evidence."""

import json
import re
from typing import cast
from urllib.parse import quote

from pydantic import TypeAdapter, ValidationError

from forge.domain.plan import RepoPath
from forge.port_values import ReadUnavailable

MAX_BYTES = 16 * 1024 * 1024
PATH = TypeAdapter(RepoPath)


def evidence(operation: str, request_id: str = "", **details: str) -> str:
    safe_id = request_id if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id) else ""
    return json.dumps(
        {"provider": "gitlab", "operation": operation, "request_id": safe_id, **details},
        sort_keys=True,
        separators=(",", ":"),
    )


def unavailable(reason: str) -> ReadUnavailable:
    return ReadUnavailable(reason, evidence("read", reason=reason))


def obj(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise unavailable("invalid_object")
    return cast(dict[str, object], value)


def array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise unavailable("invalid_array")
    return cast(list[object], value)


def text(value: object, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value) or len(value) > MAX_BYTES:
        raise unavailable("invalid_text")
    return value


def native_id(value: object) -> str:
    if type(value) is not int or value <= 0:
        raise unavailable("invalid_native_id")
    return str(value)


def numeric(value: str) -> str:
    if not value.isascii() or not value.isdecimal() or int(value) <= 0 or str(int(value)) != value:
        raise unavailable("invalid_native_id")
    return value


def sha(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None:
        raise unavailable("invalid_git_object_id")
    return value


def path(value: object) -> str:
    try:
        return PATH.validate_python(text(value))
    except ValidationError:
        raise unavailable("invalid_repository_path") from None


def branch(value: str) -> str:
    if (
        not value
        or len(value) > 300
        or any(ord(c) < 33 or ord(c) == 127 or c in "~^:?*[\\" for c in value)
        or ".." in value
        or "@{" in value
        or value.endswith(("/", "."))
        or any(
            not part or part.startswith(".") or part.endswith(".lock") for part in value.split("/")
        )
    ):
        raise unavailable("invalid_branch")
    return quote(value, safe="")


def marker(body: str, value: str) -> bool:
    if not value or len(value) > 1024 or value.strip() != value or "\n" in value or "\r" in value:
        raise unavailable("invalid_marker")
    count = sum(line.strip() == value for line in body.splitlines())
    if count > 1:
        raise unavailable("duplicate_marker_lines")
    return count == 1


def safe_body(value: str) -> str:
    text(value, empty=True)
    if any(line.lstrip().startswith("/") for line in value.splitlines()):
        raise unavailable("gitlab_quick_action_refused")
    return value


def marked(body: str, value: str) -> str:
    safe_body(body)
    marker("", value)
    if value in body:
        raise unavailable("marker_already_in_body")
    return safe_body(body + "\n\n" + value)
