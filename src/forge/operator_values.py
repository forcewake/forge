"""Credential values never enter operator evidence or native status replies."""

import re

_SECRET_KEY = re.compile(
    r"(?:token|secret|password|passwd|api[-_]?key|private[-_]?key|credential|auth|authorization)$",
    re.I,
)
_SECRET_VALUE = re.compile(
    r"(?:Bearer|Basic)\s+\S|(?:sk-|gh[pousr]_|github_pat_)[A-Za-z0-9_]{8,}|glpat-"
    r"|://[^/\s:@]+:[^/\s@]+@|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?:authorization|api[-_]?key|password|private[-_]?key)\s*[:=]\s*\S",
    re.I,
)


def safe_text(value: str | None) -> str | None:
    return "[redacted]" if value is not None and _SECRET_VALUE.search(value) else value


def redact(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if _SECRET_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return safe_text(value) if isinstance(value, str) else value
