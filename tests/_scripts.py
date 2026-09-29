"""Import helper for the stdlib scripts under ``scripts/``."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load_script(name: str) -> ModuleType:
    """Import ``scripts/<name>.py`` (the scripts import their siblings)."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def write_tree(root: Path, files: dict[str, str]) -> Path:
    """Create ``files`` (relative path → text) under ``root``."""
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root
