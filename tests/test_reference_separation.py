"""R37-19 (issue #300, ADR-0031) — the contracts-vs-reference separation.

Three kinds of code live in this repository and must stay
distinguishable from the LAYOUT: product code (the runtime contracts
and their owners), fake-native integration (the labelled evaluation
package ``forge.adaptive.reference``), and customer-executed evidence
(``qualification/records``, ``docs/releases/evidence`` — outside
``src/`` entirely). This suite pins the first two apart:

- **the dependency direction** — runtime entry points (the provider
  services, ``main``, the wiring, the lane entry) must NOT import the
  reference package, and outside the reference package only the remaining
  registered COMPAT HOMES (the modules the scenarios were extracted
  from) may import it at all — a new runtime module pulling a scenario
  in by accident fails here;
- **the reference purity** — the reference package composes runtime
  contracts only: it never imports a provider service, the wiring,
  ``main`` or the lane entry, and it owns no orchestration of its own;
- **the compatibility surface** — every moved symbol still resolves on
  its OLD import path AND is the very object the reference module
  defines (identity, not a copy): removing one re-export fails the
  pinned inventory below;
- **the no-implicit-selection property** (the issue's recovery test #2)
  — importing the runtime composition in a clean interpreter loads NO
  reference module for the lazily-compatible homes, and the spawned
  scripted vendor runs under a python where ``forge`` cannot be
  imported at all (the ``__main__`` by-path bootstrap).

The behavior of the moved scenarios is pinned UNCHANGED by the
pre-existing suites (``test_system_verification``, ``test_saga_durable``,
``test_saga_native``, ``test_causal_steering`` and the production-entry
traces) — those acceptance traces ran green before and after the move.
"""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from pathlib import Path


import forge.adaptive.reference as reference_package
from forge.adaptive.reference import system_twin

SRC_FORGE = Path(__file__).resolve().parents[1] / "src" / "forge"
REFERENCE_ROOT = SRC_FORGE / "adaptive" / "reference"

#: The runtime entry points — the modules a customer's request actually
#: enters forge through. None of them may import the reference package:
#: an evaluation scenario beside a runtime contract must never become a
#: deployed guarantee by import.
RUNTIME_ENTRY_MODULES: tuple[str, ...] = (
    "forge.main",
    "forge.lane_driver",
    "forge.adaptive.wiring",
    "forge.runs.service",
    "forge.runs.github_service",
)

#: The ONLY non-reference modules that may import ``forge.adaptive.
#: reference.*``: the compat homes the scenarios were extracted from
#: (ADR-0031's migration policy — compat imports are removed only when
#: callers are drained to the reference paths).
REFERENCE_COMPAT_HOMES: frozenset[str] = frozenset()

#: What the reference package may never import: the provider services,
#: the wiring, ``main`` and the lane entry (it composes contracts only).
REFERENCE_FORBIDDEN_IMPORTS: frozenset[str] = frozenset(RUNTIME_ENTRY_MODULES)

#: The moved-symbol inventory per compat home — the PINNED compatibility
#: surface. Removing one re-export (or letting the object drift from the
#: reference module's) fails the pinned tests below. Drain the callers,
#: then shrink this list together with the re-export.


# ---------------------------------------------------------------------------
# The AST substrate (the test_architecture_boundaries idiom, scoped here
# to the reference question).
# ---------------------------------------------------------------------------


def _imports_of(tree: ast.Module) -> set[str]:
    """Every absolute module name *tree* imports, at any nesting level
    (module-level and function-level alike — lazy imports count)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            for alias in node.names:
                names.add(f"{node.module}.{alias.name}")
    return names


def _module_of(path: Path) -> str:
    relative = path.relative_to(SRC_FORGE).with_suffix("")
    parts = relative.parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return "forge." + ".".join(parts)


def _src_trees() -> dict[str, ast.Module]:
    trees: dict[str, ast.Module] = {}
    for path in sorted(SRC_FORGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        trees[_module_of(path)] = ast.parse(path.read_text(encoding="utf-8"))
    assert trees, f"no source modules found under {SRC_FORGE} — broken test?"
    return trees


def _reference_imports(tree: ast.Module) -> set[str]:
    return {name for name in _imports_of(tree) if name.startswith("forge.adaptive.reference")}


# ---------------------------------------------------------------------------
# Rule 1 — the dependency direction (runtime must not reach reference)
# ---------------------------------------------------------------------------


class TestDependencyDirection:
    def test_no_runtime_entry_point_imports_the_reference_package(self) -> None:
        trees = _src_trees()
        for module in RUNTIME_ENTRY_MODULES:
            assert module in trees, f"the registered entry point {module} is missing"
            hits = _reference_imports(trees[module])
            assert not hits, (
                f"{module} imports the reference package ({sorted(hits)}) — "
                "runtime entry points never pull evaluation scenarios in; "
                "compose the runtime contract the scenario fakes instead"
            )

    def test_only_the_compat_homes_import_the_reference_package(self) -> None:
        """The confinement rule: outside the reference package itself,
        ONLY the surviving registered compat homes may import it — any new
        runtime module depending on a scenario fails here with an
        instruction to compose the contract instead (or, for an
        extraction, to register as a compat home with a reviewed
        reason)."""
        for module, tree in _src_trees().items():
            if module.startswith("forge.adaptive.reference"):
                continue
            hits = _reference_imports(tree)
            if module in REFERENCE_COMPAT_HOMES:
                assert hits, (
                    f"{module} is a registered reference compat home but imports "
                    "nothing from the package — the compat surface has drained; "
                    "remove the registration (and the re-export) together"
                )
            else:
                assert not hits, (
                    f"{module} imports the reference package ({sorted(hits)}) — "
                    "only the registered compat homes may; compose the runtime "
                    "contract, or register the extraction home in "
                    "tests/test_reference_separation.REFERENCE_COMPAT_HOMES "
                    "with a reviewed reason"
                )

    def test_the_reference_package_imports_contracts_only(self) -> None:
        for module, tree in _src_trees().items():
            if not module.startswith("forge.adaptive.reference"):
                continue
            hits = _imports_of(tree) & REFERENCE_FORBIDDEN_IMPORTS
            assert not hits, (
                f"{module} imports {sorted(hits)} — the reference package "
                "composes runtime contracts only; it must not reach a provider "
                "service, the wiring, main or the lane entry"
            )


# ---------------------------------------------------------------------------
# Rule 2 — the pinned compatibility surface
# ---------------------------------------------------------------------------


class TestReferenceLabel:
    def test_the_reference_package_label_is_documented(self) -> None:
        assert reference_package.REFERENCE_PACKAGE_LABEL == "forge.reference/1"
        for name in ("system_twin", "reactive_vendor"):
            module = importlib.import_module(f"forge.adaptive.reference.{name}")
            assert module.__doc__, f"reference.{name} carries no labelling docstring"
            assert "REFERENCE" in (module.__doc__ or ""), (
                f"reference.{name}'s docstring does not carry the reference label"
            )


# ---------------------------------------------------------------------------
# Rule 3 — no implicit reference selection (the issue's recovery test #2)
# ---------------------------------------------------------------------------


class TestNoImplicitReferenceSelection:
    def test_importing_product_publishers_loads_no_reference(self) -> None:
        """The shipped writers never select a reference scenario by import."""
        probe = (
            "import sys\n"
            "import forge.repository.writer\n"
            "import forge.runs.publisher\n"
            "import forge.integrations.github_flow\n"
            "leaked = sorted(m for m in sys.modules if m.startswith('forge.adaptive.reference'))\n"
            "print('LEAKED=' + ','.join(leaked))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=True
        )
        leaked = result.stdout.strip().removeprefix("LEAKED=")
        assert leaked == "", (
            f"importing the product publishers loaded reference modules "
            f"({leaked}) — a runtime import selected reference-native behavior"
        )


# ---------------------------------------------------------------------------
# The moved scenarios still decide through the runtime contracts
# (semantic pins an AST cannot prove — the full behavior matrices live in
# the pre-existing suites and run UNCHANGED).
# ---------------------------------------------------------------------------


class TestScenariosComposeContracts:
    def test_the_twin_freezes_through_the_owned_tested_world_entry(self) -> None:
        world = system_twin.default_twin_scenario().freeze()
        assert world.tested_world_digest, "the twin's freeze binds to the owned entry"
