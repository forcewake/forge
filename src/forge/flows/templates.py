"""Template engine for flow YAML definitions.

Resolves dotted-path placeholders like {review.summary} and {branch.name}
against the flow state dictionary.
"""

from __future__ import annotations

import re
from typing import Any

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}")


def render_template(template: str, state: dict) -> str:
    """Render a template string by resolving {dotted.path} placeholders.

    Missing keys are left as-is (placeholder intact).
    """

    def _replace(match: re.Match) -> str:
        path = match.group(1)
        value = _resolve_path(path, state)
        if value is None:
            return match.group(0)  # Leave placeholder intact
        return str(value)

    return _PLACEHOLDER_RE.sub(_replace, template)


def _resolve_path(path: str, state: dict) -> Any:
    """Walk a dotted path through nested dicts."""
    parts = path.split(".")
    current: Any = state
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                return None
            current = current[part]
        else:
            return None
    return current
