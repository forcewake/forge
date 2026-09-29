"""Measure the simplification-program metrics (DESIGN §2 and §12).

Usage::

    uv run python scripts/metrics.py                      # print the snapshot as JSON
    uv run python scripts/metrics.py --junit report.xml   # include the suite duration
    uv run python scripts/metrics.py --write              # append to the history and
                                                          # regenerate metrics.md
    uv run python scripts/metrics.py --who-imports forge.adaptive.ops_limits
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import _codebase as cb

PROGRAM_DIR = Path("docs/roadmap/2026-09-29-simplification")
HISTORY_FILE = PROGRAM_DIR / "metrics-history.json"
METRICS_FILE = PROGRAM_DIR / "metrics.md"
BEGIN = "<!-- metrics:begin -->"
END = "<!-- metrics:end -->"

ROWS: tuple[tuple[str, str], ...] = (
    ("src_lines", "`src/forge` lines"),
    ("modules", "Modules"),
    ("runtime_lines", "Runtime-reachable lines"),
    ("tooling_only_lines", "Tooling-only lines"),
    ("tests_only_lines", "Test-only lines"),
    ("lane_reachable_lines", "Lines reachable from lane entry points"),
    ("largest_file", "Largest file"),
    ("functions_over_80", "Functions > 80 lines"),
    ("functions_over_100", "Functions > 100 lines"),
    ("functions_over_200", "Functions > 200 lines"),
    ("prose_share", "Prose share in `src`"),
    ("review_refs", "Review and issue references in `src`"),
    ("adr_refs", "ADR references in `src`"),
    ("direct_dependencies", "Direct runtime dependencies"),
    ("docs_words", "Markdown words under `docs/`"),
    ("suite_seconds", "Default suite seconds"),
)


def _git_sha(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or "unknown"


def _suite_seconds(junit: Path | None) -> float | None:
    if junit is None:
        return None
    root = ET.parse(junit).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    return round(sum(float(s.get("time", "0")) for s in suites), 1)


def _direct_dependencies(root: Path) -> int:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return len(data["project"]["dependencies"])


def _docs_words(root: Path) -> int:
    return sum(
        len(path.read_text(encoding="utf-8", errors="replace").split())
        for path in (root / "docs").rglob("*.md")
    )


def measure(root: Path, junit: Path | None = None) -> dict[str, object]:
    modules = cb.discover_modules(root)
    forge_modules = {n: m for n, m in modules.items() if n == "forge" or n.startswith("forge.")}
    reach = cb.classify(root, modules)
    sizes = [
        size.lines for module in forge_modules.values() for size in cb.function_sizes(module.path)
    ]
    largest = max(forge_modules.values(), key=lambda m: m.lines)
    src_lines = cb.total_lines(forge_modules, modules)
    prose = sum(cb.prose_lines(m.path) for m in forge_modules.values())
    text = "".join(m.path.read_text(encoding="utf-8") for m in forge_modules.values())
    return {
        "date": dt.date.today().isoformat(),
        "sha": _git_sha(root),
        "src_lines": src_lines,
        "modules": len(forge_modules),
        "runtime_lines": cb.total_lines(reach.runtime, modules),
        "tooling_only_lines": cb.total_lines(reach.tooling_only, modules),
        "tests_only_lines": cb.total_lines(reach.tests_only, modules),
        "lane_reachable_lines": cb.total_lines(reach.lane, modules),
        "largest_file": f"{largest.lines} ({largest.path.relative_to(root)})",
        "functions_over_80": sum(1 for n in sizes if n > 80),
        "functions_over_100": sum(1 for n in sizes if n > 100),
        "functions_over_200": sum(1 for n in sizes if n > 200),
        "prose_share": f"{prose / src_lines:.0%}" if src_lines else "n/a",
        "review_refs": len(cb.REVIEW_REF.findall(text)),
        "adr_refs": len(cb.ADR_REF.findall(text)),
        "direct_dependencies": _direct_dependencies(root),
        "docs_words": _docs_words(root),
        "suite_seconds": _suite_seconds(junit),
    }


def render_history(history: list[dict[str, object]], last: int = 8) -> str:
    shown = history[-last:]
    header = "| Metric | " + " | ".join(f"{s['date']} (`{s['sha']}`)" for s in shown) + " |"
    divider = "| --- |" + " --- |" * len(shown)
    lines = [header, divider]
    for key, label in ROWS:
        cells = " | ".join("—" if s.get(key) is None else str(s[key]) for s in shown)
        lines.append(f"| {label} | {cells} |")
    return "\n".join(lines)


def write(root: Path, snapshot: dict[str, object]) -> None:
    history_path = root / HISTORY_FILE
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else []
    history.append(snapshot)
    history_path.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")
    metrics_path = root / METRICS_FILE
    text = metrics_path.read_text(encoding="utf-8")
    block = f"{BEGIN}\n{render_history(history)}\n{END}"
    if BEGIN in text:
        text = re.sub(re.escape(BEGIN) + ".*?" + re.escape(END), lambda _: block, text, flags=re.S)
    else:
        text = text.rstrip() + "\n\n## Measured history\n\n" + block + "\n"
    metrics_path.write_text(text, encoding="utf-8")


def who_imports(root: Path, target: str) -> list[str]:
    modules = cb.discover_modules(root)
    graph = cb.import_graph(modules)
    return sorted(name for name, deps in graph.items() if target in deps)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=cb.repo_root())
    parser.add_argument("--junit", type=Path, default=None)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--who-imports", default=None)
    args = parser.parse_args(argv)
    if args.who_imports:
        print("\n".join(who_imports(args.root, args.who_imports)))
        return 0
    snapshot = measure(args.root, args.junit)
    if args.write:
        write(args.root, snapshot)
    print(json.dumps(snapshot, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
