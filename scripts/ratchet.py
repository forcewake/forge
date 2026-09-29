"""Size ratchet: oversized code may only shrink, and shrinking is locked in.

``scripts/ratchet-baseline.json`` lists the current debt: files over 600
lines and functions over 80 lines under ``src/`` and ``packages/*/src/``.
The check fails when:

- a listed file or function grew beyond its recorded size;
- any other file exceeds 600 lines, or any other function exceeds 80 lines;
- the baseline is stale: debt shrank or disappeared and was not recorded;
- a module other than the allowed three names ``WritePort``
  (DESIGN §6.1: only the effects layer ever holds the write port).

Usage::

    uv run python scripts/ratchet.py                  # check (CI)
    uv run python scripts/ratchet.py --update         # record shrunk or repaid debt
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
COMMAND = "uv run python scripts/ratchet.py"

Sizes = dict[str, dict[str, int]]


def measure(root: Path) -> tuple[Sizes, dict[str, int]]:
    """Current sizes, and the line where each function starts."""
    files: dict[str, int] = {}
    functions: dict[str, int] = {}
    starts: dict[str, int] = {}
    for src_dir in cb.source_dirs(root):
        for path in cb.iter_python_files(src_dir):
            rel = path.relative_to(root).as_posix()
            files[rel] = cb.count_lines(path)
            for size in cb.function_sizes(path):
                key = f"{rel}::{size.qualname}"
                functions[key] = size.lines
                starts[key] = size.lineno
    return {"files": files, "functions": functions}, starts


def debt(current: Sizes) -> Sizes:
    return {
        kind: {key: lines for key, lines in current[kind].items() if lines > LIMITS[kind]}
        for kind in LIMITS
    }


def update(current: Sizes, baseline: Sizes, accept: tuple[str, ...] = ()) -> Sizes:
    """Lower shrunk debt, drop repaid or vanished debt, and record accepted new keys.

    ``accept`` only adds keys that are not in the baseline yet (code that was
    moved or renamed); it never raises an existing number.
    """
    updated: Sizes = {kind: {} for kind in LIMITS}
    for kind, limit in LIMITS.items():
        for key, old in baseline[kind].items():
            new = min(old, current[kind].get(key, 0))
            if new > limit:
                updated[kind][key] = new
        for key, lines in current[kind].items():
            if key.split("::", 1)[0] in accept and key not in baseline[kind] and lines > limit:
                updated[kind][key] = lines
    return updated


def check(current: Sizes, baseline: Sizes, starts: dict[str, int]) -> list[str]:
    problems: list[str] = []
    for kind, limit in LIMITS.items():
        for key, lines in sorted(current[kind].items()):
            where = f"{kind[:-1]} {key}" + (f" (line {starts[key]})" if key in starts else "")
            if key in baseline[kind]:
                if lines > baseline[kind][key]:
                    problems.append(
                        f"{where} grew to {lines} lines (allowed {baseline[kind][key]}): "
                        "shrink it back or split it"
                    )
            elif lines > limit:
                path = key.split("::", 1)[0]
                problems.append(
                    f"{where} has {lines} lines (limit {limit}): split it; for a reviewed move "
                    f"of existing debt run '{COMMAND} --update --accept {path}'"
                )
    if update(current, baseline) != baseline:
        problems.append(
            f"the baseline is stale because debt shrank: run '{COMMAND} --update' "
            f"and commit {BASELINE_NAME}"
        )
    return problems


def write_port_violations(root: Path) -> list[str]:
    problems = []
    for src_dir in cb.source_dirs(root):
        for path in cb.iter_python_files(src_dir):
            if path.relative_to(src_dir).as_posix() in WRITE_PORT_ALLOWED:
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            for number, line in enumerate(lines, 1):
                if WRITE_PORT.search(line):
                    problems.append(
                        f"{path.relative_to(root).as_posix()}:{number} names WritePort: "
                        "only engine/effects.py may hold the write port (use ctx.effects)"
                    )
                    break
    return problems


def _normalise(root: Path, raw: str) -> str:
    path = Path(raw)
    if path.is_absolute():
        path = path.resolve().relative_to(root.resolve())
    return path.as_posix()


def _save(path: Path, sizes: Sizes) -> None:
    path.write_text(json.dumps(sizes, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def _run_update(root: Path, current: Sizes, baseline: Sizes, raw_accept: list[str]) -> int:
    try:
        accept = tuple(_normalise(root, raw) for raw in raw_accept)
    except ValueError as exc:
        print(f"ratchet: {exc}")
        return 2
    updated = update(current, baseline, accept)
    added = {
        key: lines
        for kind in LIMITS
        for key, lines in updated[kind].items()
        if key not in baseline[kind]
    }
    idle = [path for path in accept if not any(k.split("::", 1)[0] == path for k in added)]
    if idle:
        print(f"ratchet: nothing new to accept in {', '.join(idle)}; nothing was written")
        return 2
    for key, lines in sorted(added.items()):
        print(f"ratchet: accepted {key} ({lines} lines)")
    _save(root / BASELINE_NAME, updated)
    print("ratchet: baseline updated")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", type=Path, default=cb.repo_root())
    parser.add_argument("--init", action="store_true", help="write the first baseline")
    parser.add_argument("--force", action="store_true", help="with --init: overwrite")
    parser.add_argument("--update", action="store_true", help="record shrunk or repaid debt")
    parser.add_argument(
        "--accept",
        action="append",
        default=[],
        metavar="PATH",
        help="with --update: record new oversized code in PATH (a reviewed move)",
    )
    args = parser.parse_args(argv)
    if args.accept and not args.update:
        parser.error("--accept requires --update")
    if args.force and not args.init:
        parser.error("--force requires --init")
    baseline_path = args.root / BASELINE_NAME
    current, starts = measure(args.root)
    if args.init:
        if baseline_path.exists() and not args.force:
            print(f"ratchet: {BASELINE_NAME} exists; --init would forgive all growth (--force)")
            return 2
        _save(baseline_path, debt(current))
        print(f"ratchet: baseline written to {BASELINE_NAME}")
        return 0
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    if args.update:
        return _run_update(args.root, current, baseline, args.accept)
    problems = check(current, baseline, starts) + write_port_violations(args.root)
    for problem in problems:
        print(f"ratchet: {problem}")
    if problems:
        print("ratchet: FAILED")
        return 1
    debt_now = debt(current)
    print(
        f"ratchet: ok — debt {len(debt_now['files'])} files > {LIMITS['files']} lines, "
        f"{len(debt_now['functions'])} functions > {LIMITS['functions']} lines"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
