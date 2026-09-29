"""Static existence check for pytest node ids (no collection, no imports).

Limits: tests inherited from a base class, or defined under ``if``, are
reported missing (the check fails safe); a parameter id in ``[...]`` is not
checked, only the function name.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def nodeid_exists(nodeid: str, root: Path = REPO) -> bool:
    """True when ``path::Class::test`` names a real test function or class."""
    path, *names = nodeid.split("::")
    file = root / path
    if not file.is_file():
        return False
    scope: list[ast.stmt] = ast.parse(file.read_text(encoding="utf-8")).body
    for name in (n.split("[", 1)[0] for n in names):
        match = next(
            (
                node
                for node in scope
                if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name == name
            ),
            None,
        )
        if match is None:
            return False
        scope = match.body if isinstance(match, ast.ClassDef) else []
    return True
