"""Size ratchet: oversized code may only shrink; new code stays small.

``scripts/ratchet-baseline.json`` lists only the current debt: files over
600 lines and functions over 80 lines under ``src/`` and
``packages/*/src/``. The check fails when:

- a listed file or function grew beyond its recorded size;
- any other file exceeds 600 lines, or any other function exceeds 80 lines;
- a module other than the allowed three names ``WritePort``
  (DESIGN §6.1: only the effects layer ever holds the write port).

Usage::

    uv run python scripts/ratchet.py                  # check (CI)
    uv run python scripts/ratchet.py --update         # lower entries that shrank
    uv run python scripts/ratchet.py --update --accept src/forge/project_config.py
                                                      # record a reviewed move of oversized code
    uv run python scripts/ratchet.py --init           # write the first baseline
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import _codebase as cb

BASELINE_NAME = "scripts/ratchet-baseline.json"
LIMITS = {"files": 600, "functions": 80}
WRITE_PORT = re.compile(r"\bWritePort\b")
WRITE_PORT_ALLOWED = ("forge/ports.py", "forge/engine/effects.py", "forge/api/app.py")

Sizes = dict[str, dict[str, int]]


def measure(root: Path) -> Sizes:
    files: dict[str, int] = {}
    functions: dict[str, int] = {}
    for src_dir in cb.source_dirs(root):
        for path in cb.iter_python_files(src_dir):
            rel = path.relative_to(root).as_posix()
            files[rel] = cb.count_lines(path)
            for size in cb.function_sizes(path):
                functions[f"{rel}::{size.qualname}"] = size.lines
    return {"files": files, "functions": functions}


def debt(current: Sizes) -> Sizes:
    return {
        kind: {key: lines for key, lines in current[kind].items() if lines > LIMITS[kind]}
        for kind in LIMITS
    }


def check(current: Sizes, baseline: Sizes) -> list[str]:
    problems: list[str] = []
    for kind, limit in LIMITS.items():
        for key, lines in sorted(current[kind].items()):
            allowed = baseline[kind].get(key, limit)
            if lines > allowed:
                what = "grew" if key in baseline[kind] else "exceeds the limit"
                problems.append(f"{kind[:-1]} {key} {what}: {lines} lines (allowed {allowed})")
    return problems


def update(current: Sizes, baseline: Sizes, accept: tuple[str, ...] = ()) -> Sizes:
    """Lower shrunk entries, drop repaid or vanished ones, record accepted moves."""
    updated: Sizes = {kind: {} for kind in LIMITS}
    for kind, limit in LIMITS.items():
        for key, old in baseline[kind].items():
            new = min(old, current[kind].get(key, 0))
            if new > limit:
                updated[kind][key] = new
        for key, lines in current[kind].items():
            if key.split("::", 1)[0] in accept and lines > limit:
                updated[kind][key] = lines
    return updated


def write_port_violations(root: Path) -> list[str]:
    problems = []
    for src_dir in cb.source_dirs(root):
        for path in cb.iter_python_files(src_dir):
            if path.relative_to(src_dir).as_posix() in WRITE_PORT_ALLOWED:
                continue
            if WRITE_PORT.search(path.read_text(encoding="utf-8")):
                problems.append(f"{path.relative_to(root)} names WritePort (only effects may)")
    return problems


def _save(path: Path, sizes: Sizes) -> None:
    path.write_text(json.dumps(sizes, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=cb.repo_root())
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--accept", action="append", default=[], metavar="PATH")
    args = parser.parse_args(argv)
    baseline_path = args.root / BASELINE_NAME
    current = measure(args.root)
    if args.init:
        _save(baseline_path, debt(current))
        print(f"ratchet: baseline written to {BASELINE_NAME}")
        return 0
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    if args.update:
        _save(baseline_path, update(current, baseline, tuple(args.accept)))
        print("ratchet: baseline updated")
        return 0
    problems = check(current, baseline) + write_port_violations(args.root)
    for problem in problems:
        print(f"ratchet: {problem}")
    if problems:
        print("ratchet: FAILED — split the code, or --update --accept PATH for a reviewed move")
        return 1
    debt_now = debt(current)
    print(
        f"ratchet: ok — debt {len(debt_now['files'])} files > {LIMITS['files']} lines, "
        f"{len(debt_now['functions'])} functions > {LIMITS['functions']} lines"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
