"""Shared static analysis of the forge source tree (stdlib only).

Used by ``scripts/metrics.py``, ``scripts/ratchet.py`` and
``scripts/build_test_ledger.py``. Everything here reads files; nothing imports
forge, so the scripts run without the project environment.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
import tomllib
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

CONTROL_PLANE_ROOTS = ("forge.main", "forge.worker.__main__", "forge.worker", "forge.migrate")
LANE_ROOTS = ("forge.harness_entry", "forge.lane_driver", "forge.candidate_collector", "forge_lane")
# Declared roots that do not exist yet; their absence is expected.
FUTURE_ROOTS = frozenset({"forge_lane"})
TOOLING_ROOTS = (
    "forge.doctor",
    "forge.release_manifest",
    "forge.release_promotion",
    "forge.profile_qualification",
    "forge.capability_manifest",
)
# History that belongs in git and ADRs, not in code: review, backlog and
# acceptance item ids, issue references and "review <sha>". Short forms such as
# R17 or A12 are too ambiguous to count, so this is a lower bound.
REVIEW_REF = re.compile(
    r"\b(?:[RQ]\d{2}-\d{2}|NE?XT-\d{2,3}|AT-\d{2})\b"
    r"|(?<![\w/&])#\d{3,}\b"
    r"|\b[Rr]eview\s+`?[0-9a-f]{7,40}\b"
)
ADR_REF = re.compile(r"\bADR-\d{4}\b")


@dataclass(frozen=True)
class Module:
    name: str
    path: Path
    lines: int


@dataclass(frozen=True)
class FunctionSize:
    qualname: str
    lineno: int
    lines: int


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def source_dirs(root: Path) -> list[Path]:
    """``src/`` plus every workspace package's ``src/`` (Phase 2 onward)."""
    dirs = [root / "src", *sorted((root / "packages").glob("*/src"))]
    return [d for d in dirs if d.is_dir()]


def iter_python_files(base: Path) -> Iterator[Path]:
    for path in sorted(base.rglob("*.py")):
        if "__pycache__" not in path.parts:
            yield path


def count_lines(path: Path) -> int:
    with path.open(encoding="utf-8") as handle:
        return sum(1 for _ in handle)


def module_name(path: Path, src_dir: Path) -> str:
    parts = list(path.relative_to(src_dir).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def discover_modules(root: Path) -> dict[str, Module]:
    modules: dict[str, Module] = {}
    for src_dir in source_dirs(root):
        for path in iter_python_files(src_dir):
            name = module_name(path, src_dir)
            modules[name] = Module(name, path, count_lines(path))
    return modules


def function_sizes(path: Path) -> list[FunctionSize]:
    """Every function and method, nested ones included, with a unique qualname."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    sizes: list[FunctionSize] = []
    seen: dict[str, int] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                qual = f"{prefix}{child.name}"
                seen[qual] = seen.get(qual, 0) + 1
                unique = qual if seen[qual] == 1 else f"{qual}#{seen[qual]}"
                end = child.end_lineno or child.lineno
                sizes.append(FunctionSize(unique, child.lineno, end - child.lineno + 1))
                visit(child, f"{qual}.")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return sizes


def prose_lines(path: Path) -> int:
    """Comment lines plus docstring lines."""
    source = path.read_text(encoding="utf-8")
    comments = sum(
        1
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type == tokenize.COMMENT
    )
    docstrings = 0
    for node in ast.walk(ast.parse(source, filename=str(path))):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings += doc.count("\n") + 1
    return comments + docstrings


def _resolve(name: str, modules: dict[str, Module]) -> str | None:
    parts = name.split(".")
    while parts:
        candidate = ".".join(parts)
        if candidate in modules:
            return candidate
        parts.pop()
    return None


def _from_target(node: ast.ImportFrom, current: str | None, is_package: bool) -> str:
    """The absolute module named by a ``from ... import`` statement."""
    if not (node.level and current is not None):
        return node.module or ""
    base = current.split(".")
    if not is_package:
        base = base[:-1]
    if node.level > 1:
        base = base[: -(node.level - 1)]
    return ".".join([*base, node.module] if node.module else base)


_REEXPORTS: dict[Path, dict[str, str]] = {}


def reexports(package: Module, modules: dict[str, Module]) -> dict[str, str]:
    """Names a package ``__init__`` imports at top level, mapped to their module."""
    if package.path not in _REEXPORTS:
        tree = ast.parse(package.path.read_text(encoding="utf-8"), filename=str(package.path))
        exports: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                full = _from_target(node, package.name, is_package=True)
                for alias in node.names:
                    target = _resolve(f"{full}.{alias.name}", modules) or _resolve(full, modules)
                    if target is not None and target != package.name:
                        exports[alias.asname or alias.name] = target
        _REEXPORTS[package.path] = exports
    return _REEXPORTS[package.path]


def imports_of(path: Path, modules: dict[str, Module], current: str | None = None) -> set[str]:
    """Internal modules imported by a file (lazy imports, literal
    ``importlib.import_module`` calls and names re-exported by a package
    ``__init__`` included; parents implied)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    is_package = path.name == "__init__.py"
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if (hit := _resolve(alias.name, modules)) is not None:
                    found.add(hit)
        elif isinstance(node, ast.ImportFrom):
            full = _from_target(node, current, is_package)
            for alias in node.names:
                hit = _resolve(f"{full}.{alias.name}", modules) or _resolve(full, modules)
                if hit is None:
                    continue
                found.add(hit)
                if hit == full and modules[hit].path.name == "__init__.py":
                    if (source := reexports(modules[hit], modules).get(alias.name)) is not None:
                        found.add(source)
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            called = getattr(func, "attr", None) or getattr(func, "id", None)
            first = node.args[0]
            if (
                called == "import_module"
                and isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and (hit := _resolve(first.value, modules)) is not None
            ):
                found.add(hit)
    parents = {".".join(m.split(".")[:i]) for m in found for i in range(1, len(m.split(".")))}
    return found | {p for p in parents if p in modules}


def import_graph(modules: dict[str, Module]) -> dict[str, set[str]]:
    return {name: imports_of(mod.path, modules, name) for name, mod in modules.items()}


def reachable(roots: Iterable[str], graph: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    queue = deque(r for r in roots if r in graph)
    while queue:
        name = queue.popleft()
        if name not in seen:
            seen.add(name)
            queue.extend(graph[name])
    return seen


def roots_from_files(files: Iterable[Path], modules: dict[str, Module]) -> set[str]:
    roots: set[str] = set()
    for path in files:
        try:
            roots |= imports_of(path, modules)
        except SyntaxError:
            continue
    return roots


def console_script_modules(root: Path) -> set[str]:
    """Modules behind ``[project.scripts]`` in the root and workspace packages."""
    found: set[str] = set()
    for pyproject in [
        root / "pyproject.toml",
        *sorted((root / "packages").glob("*/pyproject.toml")),
    ]:
        if pyproject.is_file():
            config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            scripts = config.get("project", {}).get("scripts", {})
            found |= {target.split(":", 1)[0] for target in scripts.values()}
    return found


@dataclass(frozen=True)
class Reachability:
    runtime: set[str]
    lane: set[str]
    tooling_only: set[str]
    tests_only: set[str]
    unreached: set[str]
    missing_roots: tuple[str, ...] = ()


def classify(root: Path, modules: dict[str, Module]) -> Reachability:
    graph = import_graph(modules)
    control = reachable(CONTROL_PLANE_ROOTS, graph)
    lane = reachable(LANE_ROOTS, graph)
    runtime = control | lane
    tooling_roots = {*TOOLING_ROOTS, *console_script_modules(root)}
    script_roots = roots_from_files((root / "scripts").glob("*.py"), modules)
    tools_roots = roots_from_files((root / "tools").rglob("*.py"), modules)
    tooling = reachable((*tooling_roots, *script_roots, *tools_roots), graph) - runtime
    test_roots = roots_from_files((root / "tests").rglob("*.py"), modules)
    tests_only = reachable(test_roots, graph) - runtime - tooling
    unreached = set(modules) - runtime - tooling - tests_only
    declared = (*CONTROL_PLANE_ROOTS, *LANE_ROOTS, *sorted(tooling_roots))
    missing = tuple(r for r in declared if r not in modules and r not in FUTURE_ROOTS)
    return Reachability(runtime, lane, tooling, tests_only, unreached, missing)


def total_lines(names: Iterable[str], modules: dict[str, Module]) -> int:
    return sum(modules[n].lines for n in names)
