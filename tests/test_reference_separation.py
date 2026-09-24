"""R37-19 (issue #300, ADR-0031) — the contracts-vs-reference separation.

Three kinds of code live in this repository and must stay
distinguishable from the LAYOUT: product code (the runtime contracts
and their owners), fake-native integration (the labelled evaluation
package ``forge.adaptive.reference``), and customer-executed evidence
(``qualification/records``, ``docs/releases/evidence`` — outside
``src/`` entirely). This suite pins the first two apart:

- **the dependency direction** — runtime entry points (the provider
  services, ``main``, the wiring, the lane entry) must NOT import the
  reference package, and outside the reference package only the three
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
import asyncio
import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import forge.adaptive.reference as reference_package
import forge.adaptive.saga_durable as saga_durable
import forge.adaptive.steering_causality as steering_causality
from forge.adaptive.reference import native_shaped_remote, reactive_vendor, system_twin

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
    "forge.runs.azure_service",
)

#: The ONLY non-reference modules that may import ``forge.adaptive.
#: reference.*``: the compat homes the scenarios were extracted from
#: (ADR-0031's migration policy — compat imports are removed only when
#: callers are drained to the reference paths).
REFERENCE_COMPAT_HOMES: frozenset[str] = frozenset(
    {
        "forge.adaptive.system_verification",
        "forge.adaptive.saga_durable",
        "forge.adaptive.steering_causality",
    }
)

#: What the reference package may never import: the provider services,
#: the wiring, ``main`` and the lane entry (it composes contracts only).
REFERENCE_FORBIDDEN_IMPORTS: frozenset[str] = frozenset(RUNTIME_ENTRY_MODULES)

#: The moved-symbol inventory per compat home — the PINNED compatibility
#: surface. Removing one re-export (or letting the object drift from the
#: reference module's) fails the pinned tests below. Drain the callers,
#: then shrink this list together with the re-export.
MOVED_SYMBOLS: dict[str, tuple[tuple[str, str], ...]] = {
    # system_verification → reference/system_twin.py
    "forge.adaptive.system_verification": tuple(
        (name, "forge.adaptive.reference.system_twin")
        for name in (
            "TWIN_CONTRACT_BUNDLE_SCHEMA",
            "TWIN_TEST_BUNDLE_SCHEMA",
            "TWIN_ENVIRONMENT_PROFILE_SCHEMA",
            "TWIN_REFERENCE_LABEL",
            "BASELINE_API_V2",
            "BASELINE_API_V3",
            "CrashWindowOpened",
            "DeliveryOutcome",
            "DoubleDeliveryHarness",
            "SchemaUpgradeOutcome",
            "SystemEdge",
            "TwinContract",
            "TwinMigration",
            "TwinScenario",
            "TwinService",
            "default_twin_scenario",
            "execute_schema_upgrade",
            # private pre-move attributes the tests import directly
            "_synthetic_seed_rows",
        )
    ),
    # saga_durable → reference/native_shaped_remote.py
    "forge.adaptive.saga_durable": (
        ("NativeShapedRemote", "forge.adaptive.reference.native_shaped_remote"),
    ),
    # steering_causality → reference/reactive_vendor.py (the pure grader
    # stays; the scenario constants, the grammar and the executable moved)
    "forge.adaptive.steering_causality": tuple(
        (name, "forge.adaptive.reference.reactive_vendor")
        for name in steering_causality._MOVED_SCENARIO_SYMBOLS
    ),
}


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
        ONLY the three registered compat homes may import it — any new
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


class TestCompatibilitySurface:
    @pytest.mark.parametrize(
        ("home_name", "symbol", "reference_name"),
        [
            (home, symbol, reference)
            for home, moves in MOVED_SYMBOLS.items()
            for symbol, reference in moves
        ],
        ids=[
            f"{home.rsplit('.', 1)[-1]}.{symbol}"
            for home, moves in MOVED_SYMBOLS.items()
            for symbol, _ in moves
        ],
    )
    def test_every_moved_symbol_resolves_identically_on_its_old_path(
        self, home_name: str, symbol: str, reference_name: str
    ) -> None:
        home = importlib.import_module(home_name)
        reference = importlib.import_module(reference_name)
        assert hasattr(reference, symbol), (
            f"{reference_name} no longer defines {symbol} — the reference "
            "module and this pin drifted together; update both"
        )
        assert getattr(home, symbol) is getattr(reference, symbol), (
            f"{home_name}.{symbol} is not {reference_name}.{symbol} — the "
            "compatibility re-export drifted from the moved object (a copy, "
            "not a re-export, would fork the decision)"
        )

    def test_the_old_from_import_spellings_still_work(self) -> None:
        """The exact from-import forms the pre-move callers used."""
        from forge.adaptive.saga_durable import NativeShapedRemote, NativeCommit  # noqa: F401
        from forge.adaptive.system_verification import (  # noqa: F401
            TWIN_REFERENCE_LABEL,
            TwinMigration,
            _synthetic_seed_rows,
        )
        from forge.adaptive.steering_causality import (  # noqa: F401
            SCRIPTED_CAUSAL_PROVENANCE,
            vendor_main,
        )

    def test_the_lazy_homes_do_not_over_expose(self) -> None:
        """The ``__getattr__`` re-exports answer ONLY the moved names —
        an arbitrary attribute is still an AttributeError, never a
        silent passthrough to the reference module."""
        with pytest.raises(AttributeError):
            _ = steering_causality.definitely_not_a_moved_symbol
        with pytest.raises(AttributeError):
            _ = saga_durable.definitely_not_a_moved_symbol

    def test_the_reference_package_label_is_documented(self) -> None:
        assert reference_package.REFERENCE_PACKAGE_LABEL == "forge.reference/1"
        for name in ("system_twin", "native_shaped_remote", "reactive_vendor"):
            module = importlib.import_module(f"forge.adaptive.reference.{name}")
            assert module.__doc__, f"reference.{name} carries no labelling docstring"
            assert "REFERENCE" in (module.__doc__ or ""), (
                f"reference.{name}'s docstring does not carry the reference label"
            )


# ---------------------------------------------------------------------------
# Rule 3 — no implicit reference selection (the issue's recovery test #2)
# ---------------------------------------------------------------------------


class TestNoImplicitReferenceSelection:
    def test_importing_the_runtime_saga_composition_loads_no_reference(self) -> None:
        """A clean interpreter importing the durable saga + the native
        adapters (the production composition) must load NO reference
        module — the compat home's re-export is lazy precisely so an
        import can never select the reference-native remote."""
        probe = (
            "import sys\n"
            "import forge.adaptive.saga_durable\n"
            "import forge.adaptive.saga_native\n"
            "import forge.adaptive.publication_saga\n"
            "leaked = sorted(m for m in sys.modules if m.startswith('forge.adaptive.reference'))\n"
            "print('LEAKED=' + ','.join(leaked))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=True
        )
        leaked = result.stdout.strip().removeprefix("LEAKED=")
        assert leaked == "", (
            f"importing the production saga composition loaded reference modules "
            f"({leaked}) — a runtime import selected reference-native behavior"
        )

    def test_importing_the_pure_grader_loads_no_reference(self) -> None:
        probe = (
            "import sys\n"
            "import forge.adaptive.steering_causality\n"
            "leaked = sorted(m for m in sys.modules if m.startswith('forge.adaptive.reference'))\n"
            "print('LEAKED=' + ','.join(leaked))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=True
        )
        leaked = result.stdout.strip().removeprefix("LEAKED=")
        assert leaked == "", (
            f"importing the pure grader loaded reference modules ({leaked}) — "
            "the spawned file must stay stdlib-only at import time"
        )

    def test_the_spawned_vendor_runs_where_forge_cannot_be_imported(self, tmp_path: Path) -> None:
        """The lane spawn arm: ``steering_causality.py`` doubles as the
        subprocess ``CODEX_BINARY`` executed under the system python
        (the production-entry trace spawns it via its shebang), where NO
        forge import is guaranteed to resolve. Simulated here by copying
        the spawned file and its reference sibling out of the package
        and running them under ``python -I`` from a scratch directory —
        if the spawn arm ever grew a forge import, this fails."""
        spawn_root = tmp_path / "spawn"
        (spawn_root / "reference").mkdir(parents=True)
        shutil.copy(
            SRC_FORGE / "adaptive" / "steering_causality.py", spawn_root / "steering_causality.py"
        )
        shutil.copy(
            REFERENCE_ROOT / "reactive_vendor.py", spawn_root / "reference" / "reactive_vendor.py"
        )
        workdir = tmp_path / "work"
        workdir.mkdir()
        eventlog = tmp_path / "events.jsonl"
        actions = json.dumps(
            [{"op": "write", "path": reactive_vendor.POLICY_PATH, "content": "k = 1\n"}]
        )
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            reactive_vendor.ACTIONS_ENV: actions,
            reactive_vendor.EVENTLOG_ENV: str(eventlog),
        }
        result = subprocess.run(
            [sys.executable, "-I", str(spawn_root / "steering_causality.py"), "--once"],
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"the isolated spawn failed: {result.stderr}"
        events = [json.loads(line) for line in eventlog.read_text().splitlines()]
        assert [event["kind"] for event in events] == ["vendor_once", "vendor_edits"]
        assert events[0]["provenance"] == reactive_vendor.SCRIPTED_CAUSAL_PROVENANCE
        assert (workdir / reactive_vendor.POLICY_PATH).read_text() == "k = 1\n"


# ---------------------------------------------------------------------------
# The moved scenarios still decide through the runtime contracts
# (semantic pins an AST cannot prove — the full behavior matrices live in
# the pre-existing suites and run UNCHANGED).
# ---------------------------------------------------------------------------


class TestScenariosComposeContracts:
    def test_the_twin_freezes_through_the_owned_tested_world_entry(self) -> None:
        world = system_twin.default_twin_scenario().freeze()
        assert world.tested_world_digest, "the twin's freeze binds to the owned entry"

    def test_the_reference_remote_speaks_the_owned_error_vocabulary(self) -> None:
        remote = native_shaped_remote.NativeShapedRemote()
        remote.seed("repo", "main", "a" * 40)
        remote.pin_expected_head("repo", "main", "b" * 40)
        with pytest.raises(saga_durable.ProviderRejectedError, match="422"):
            asyncio.run(remote.commit("repo", "main", "marker"))

    def test_the_grader_uses_the_scenario_grammar_it_moved(self) -> None:
        target = steering_causality.parse_instruction(steering_causality.STEERING_TASK_INSTRUCTION)
        assert target is not None and target.old == "refund_limit"
