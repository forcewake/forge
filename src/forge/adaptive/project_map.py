"""Practical project map and test inventory from a snapshot (DSC-04).

Planning needs to know what a repository *is* — where its manifests,
CI, public API surface, event schemas, migrations and tests live — before
any plan step runs. This module extracts that map from snapshot files
with two provenance rules baked in:

- **Observed facts carry citations** (:func:`fact_with_citation`) — every
  fact names the repository, ``source_oid`` and path it was read from, so
  a reviewer can re-derive it. **Inferred relations carry confidence**
  (:func:`inferred_relation`) — a guess must never travel dressed as an
  observation.
- **Generated/vendor content is excluded by declared policy** —
  ``node_modules/``, ``vendor/``, ``dist/``, ``.venv/`` and
  ``__pycache__`` never reach a bucket; a dependency's manifest is not
  the repository's manifest.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import PurePosixPath

import yaml

#: Markers of generated/vendored trees. Matched as ``marker in f"/{path}/"``
#: so only directory components match — a root file named ``dist.py`` is
#: real content, ``dist/bundle.js`` is not.
_EXCLUDED_MARKERS = ("node_modules/", "vendor/", "dist/", ".venv/", "__pycache__/")

#: Public-API declaration shapes: python defs/classes plus JS exports.
_API_LINE_RE = re.compile(r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_]\w*)")
_EXPORT_LINE_RE = re.compile(
    r"^\s*export\s+(?:default\s+)?(?:async\s+)?"
    r"(?:function|const|let|var|class)\s+([A-Za-z_$][\w$]*)"
)

#: Confidence labels for the public-api heuristic: a declaration in an
#: ``api/`` tree is stronger evidence than one in a file whose *name*
#: merely smells like routing.
_API_PATH_CONFIDENCE = 0.7
_API_NAME_CONFIDENCE = 0.5

#: Makefile target line: a non-recipe, non-assignment, non-special target.
_MAKEFILE_TARGET_RE = re.compile(r"^([A-Za-z0-9_][^:=]*?):(?!=)")

#: The gap marker emitted for a snapshot with no tests at all — the one
#: coverage fact that is always safe to state without running anything.
_COVERAGE_GAP_MARKER = {
    "scope": "repository",
    "reason": "no test locations discovered",
    "detail": "no test_*.py, *_test.py or tests/ paths in the snapshot",
}


def extract_project_map(files: dict[str, str]) -> dict:
    """Scan one repository snapshot into a practical project map.

    Returns the ``forge.project-map/1`` shape with buckets
    ``manifests``, ``ci_definitions``, ``public_api``, ``event_schemas``,
    ``migrations``, ``test_locations``, ``generated_excluded`` and
    ``coverage_gaps``. Excluded paths are classified once, first, and
    never re-examined — an excluded path can only ever land in
    ``generated_excluded``. ``coverage_gaps`` flags the examined
    repository when the snapshot yields zero test locations (the caller
    aggregates the markers per repository id).
    """
    manifests: list[str] = []
    ci_definitions: list[str] = []
    public_api: list[dict[str, object]] = []
    event_schemas: list[str] = []
    migrations: list[str] = []
    test_locations: list[str] = []
    generated_excluded: list[str] = []

    for path in sorted(files):
        if _is_excluded(path):
            generated_excluded.append(path)
            continue
        name = PurePosixPath(path).name
        at_root = "/" not in path
        if (
            (at_root and (name.endswith((".toml", ".json")) or _is_requirements(name)))
            or name == "pyproject.toml"
            or name == "package.json"
        ):
            manifests.append(path)
        if path.startswith(".github/workflows/") or name in (
            ".gitlab-ci.yml",
            "azure-pipelines.yml",
        ):
            ci_definitions.append(path)
        public_api.extend(_public_api_lines(path, files[path]))
        if "event" in path.lower() and path.endswith((".json", ".avro", ".proto")):
            event_schemas.append(path)
        if "/migrations/" in f"/{path}" or "/alembic/versions/" in f"/{path}":
            migrations.append(path)
        if _is_test_location(path):
            test_locations.append(path)

    coverage_gaps: list[dict[str, str]] = [] if test_locations else [_COVERAGE_GAP_MARKER]
    return {
        "schema": "forge.project-map/1",
        "manifests": manifests,
        "ci_definitions": ci_definitions,
        "public_api": public_api,
        "event_schemas": event_schemas,
        "migrations": migrations,
        "test_locations": test_locations,
        "generated_excluded": generated_excluded,
        "coverage_gaps": coverage_gaps,
    }


def fact_with_citation(
    kind: str,
    value: str,
    repository_id: str,
    source_oid: str,
    path: str,
    line_no: int | None = None,
) -> dict[str, object]:
    """An observed fact with the citation it was read from.

    Observed facts carry provenance (repository, ``source_oid``, path,
    optional line), never a confidence — the map's whole separation is
    that observations and guesses stay distinguishable downstream.
    """
    return {
        "kind": kind,
        "value": value,
        "repository_id": repository_id,
        "source_oid": source_oid,
        "path": path,
        "line_no": line_no,
    }


def inferred_relation(kind: str, source: str, target: str, confidence: float) -> dict[str, object]:
    """An inferred relation with its confidence label (0.0-1.0).

    Inferences are allowed — discovering a map needs heuristics — but
    they must travel labeled, so a consumer can route anything below its
    threshold to a human instead of acting on it. Out-of-range confidence
    is refused: a label outside 0.0-1.0 is a bug, not a judgment call.
    """
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence must be within 0.0-1.0, got {confidence}")
    return {"kind": kind, "source": source, "target": target, "confidence": confidence}


def commands_as_metadata(files: dict[str, str]) -> list[dict[str, object]]:
    """Extract Makefile targets and CI script commands as metadata only.

    Every entry is ``{"command", "source_path", "validated": False}`` —
    discovered commands stay *metadata* until a trusted execution profile
    validates them. Nothing here decides what may run; it only records
    what the repository itself claims to run. Unparseable YAML is skipped,
    never fatal: a metadata scan must not break discovery.
    """
    commands: list[dict[str, object]] = []
    for path in sorted(files):
        if _is_excluded(path):
            continue
        name = PurePosixPath(path).name
        content = files[path]
        if name in ("Makefile", "makefile", "GNUmakefile"):
            for target in _makefile_targets(content):
                commands.append(
                    {"command": f"make {target}", "source_path": path, "validated": False}
                )
        elif name == ".gitlab-ci.yml" or (
            path.startswith(".github/workflows/") and name.endswith((".yml", ".yaml"))
        ):
            commands.extend(
                {"command": command, "source_path": path, "validated": False}
                for command in _ci_script_commands(content)
            )
    return commands


# -- internals -------------------------------------------------------------


def _is_excluded(path: str) -> bool:
    """True when the path sits inside a declared generated/vendor tree."""
    padded = f"/{path}/"
    return any(marker in padded for marker in _EXCLUDED_MARKERS)


def _is_requirements(name: str) -> bool:
    return name.startswith("requirements") and name.endswith(".txt")


def _is_test_location(path: str) -> bool:
    """A test file by name, or any path inside a ``tests/`` directory."""
    name = PurePosixPath(path).name
    by_name = (name.startswith("test_") or name.endswith("_test.py")) and name.endswith(".py")
    return by_name or "/tests/" in f"/{path}/"


def _public_api_lines(path: str, content: str) -> list[dict[str, object]]:
    """Heuristic public-API declarations, each labeled with confidence.

    A file qualifies when it lives under ``api/`` (stronger signal) or
    its name mentions ``router``/``endpoint`` (weaker signal); only then
    are its declaration lines collected, each with the matching basis.
    """
    lower = path.lower()
    under_api = lower.startswith("api/") or "/api/" in lower
    name_signal = "router" in PurePosixPath(lower).name or "endpoint" in PurePosixPath(lower).name
    if not under_api and not name_signal:
        return []
    confidence = _API_PATH_CONFIDENCE if under_api else _API_NAME_CONFIDENCE
    basis = "api-path" if under_api else "router-or-endpoint-name"
    entries: list[dict[str, object]] = []
    for line_no, text in enumerate(content.splitlines(), start=1):
        declaration = _API_LINE_RE.match(text) or _EXPORT_LINE_RE.match(text)
        if declaration is None:
            continue
        entries.append(
            {
                "path": path,
                "line_no": line_no,
                "symbol": declaration.group(1),
                "kind": _declaration_kind(declaration.group(0)),
                "confidence": confidence,
                "basis": basis,
            }
        )
    return entries


def _declaration_kind(declaration: str) -> str:
    """``class`` -> ``class``, ``def``/``async def`` -> ``function``, exports -> ``export``."""
    first = declaration.split()[0].lstrip()
    if first == "class":
        return "class"
    if first in ("def", "async"):
        return "function"
    return "export"


def _makefile_targets(content: str) -> Iterator[str]:
    """Yield Makefile target names — recipes, variables and specials excluded.

    A target line starts at column zero with a name and a ``:`` that is
    not ``:=``; recipe lines (tab-indented), comments, dot-specials
    (``.PHONY``) and variable assignments never qualify.
    """
    for line in content.splitlines():
        if line.startswith((" ", "\t", "#", ".")):
            continue
        match = _MAKEFILE_TARGET_RE.match(line)
        if match is None:
            continue
        target = match.group(1).strip()
        # "$" targets are pattern/variable rules, not runnable names.
        if target and "$" not in target:
            yield target


def _ci_script_commands(content: str) -> list[str]:
    """Pull ``run:``/``script:`` entries out of a CI YAML document.

    Walks the parsed document recursively so GitHub's nested
    ``jobs.<id>.steps[].run`` and GitLab's ``<job>.script`` both fall out
    with one rule. A malformed document yields nothing — the metadata
    scan must not break discovery.
    """
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError:
        return []
    return list(_script_commands(document))


def _script_commands(node: object) -> Iterator[str]:
    """Recursively yield command lines under ``run``/``script`` keys."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("run", "script") and isinstance(value, (str, list)):
                yield from _command_lines(value)
            else:
                yield from _script_commands(value)
    elif isinstance(node, list):
        for item in node:
            yield from _script_commands(item)


def _command_lines(value: str | list) -> Iterator[str]:
    """Non-empty, non-comment lines of one script entry."""
    lines = value.splitlines() if isinstance(value, str) else [str(item) for item in value]
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            yield stripped
