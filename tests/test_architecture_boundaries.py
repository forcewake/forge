"""ADR-0030 / R36-20 (issue #279): mechanically enforced authority-boundary ownership.

The 16339c2 review's thesis: repeated findings are the SAME decision
made in two places. Every R36 sibling landed ONE owning module per
decision; this suite makes the ownership VISIBLE and MECHANICAL — no
new framework, no extraction wave:

- **the registry** (``forge.adaptive.boundary_registry``) declares the
  six owning boundaries, their owners and their REGISTERED production
  callers. A module that imports an owner without registration fails
  the boundary check with an instruction to register or route through
  the owner — the intentional-legacy-call trap proves detection;
- **the legacy escape hatch is confined**: ``revival._legacy_http_
  lookup``, ``CheckpointStore._load_index`` and direct ``CheckpointStore``
  construction are reachable ONLY from the enumerated allow-set, and
  inside ``runs.revival`` only from the opt-in adapter itself
  (``architecture.legacy_call_sites``);
- **the composition monopolies**: ``resolve_repository`` is the one
  checkpoint-authority composition point; ``AttemptStartSpec`` is
  constructed only by its home and the one adopted adapter (services
  compose through ``compose_attempt_start``);
- **continuation modes are decided, not re-derived**: only
  ``continuation.decide_continuation`` / ``parse_recovery_request``
  produce modes; the dispatch vocabulary constants in
  ``github_service`` may be referenced only as the documented
  initial-dispatch default and the vocabulary validation;
- **blob unlinks happen only inside the locked sweep entries**
  (``_sweep_locked`` / ``_asweep_locked``); the pins/journal overlay and
  atomic-write temp files are the enumerated exemptions;
- **provider conformance** (``contract.provider_conformance``): the
  same negative contract inputs — a fenced authority, a typed
  checkpoint-unavailable, a stale epoch — produce equivalent typed
  refusals on the GitHub path and the SHARED core
  (``retry_rejection`` / ``why_blocked_reply`` that GitLab and Azure
  consume verbatim); the GitLab/Azure lane-resume gap (#268) is
  asserted as the documented HONEST state, never fabricated parity.
"""

from __future__ import annotations

import ast
import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pytest

from forge.adaptive import boundary_registry
from forge.adaptive.checkpoint_repository import (
    LOOKUP_ABSENT,
    LOOKUP_CORRUPT,
    LOOKUP_UNAVAILABLE,
    LOOKUP_UNAUTHORIZED,
    CheckpointLookupOutcome,
    MutationsFencedError,
)
from forge.adaptive.composition_adoption import compose_attempt_start
from forge.adaptive.continuation import (
    ContinuationMode,
    decide_continuation,
    evidence_from_record,
    normalize_checkpoint_result,
)
from forge.durable import FlowRun, FlowStatus
from forge.runs import revival
from forge.runs.composition import CompositionBoundaryError
from forge.runs.revival import (
    RetryRejection,
    RetryRefusalCode,
    retry_rejection,
    why_blocked_reply,
)

SRC_FORGE = Path(__file__).resolve().parents[1] / "src" / "forge"

#: The two modules whose GC unlink surface is confined (the store and
#: the repository that wraps it) — the sweep seam lives in both.
_GC_CONFINED_MODULES = ("forge.api_checkpoint_channel", "forge.adaptive.checkpoint_repository")

_MODE_LITERALS = frozenset({"fresh", "required", "restart"})

#: The dispatch-vocabulary mode constants the re-derivation check scopes
#: to (LANE_RESUME_MODE_INPUT is the workflow input NAME — a payload key,
#: not a mode constant).
_MODE_CONSTANTS = frozenset(
    {
        "LANE_RESUME_MODES",
        "LANE_RESUME_MODE_FRESH",
        "LANE_RESUME_MODE_REQUIRED",
        "LANE_RESUME_MODE_RESTART",
    }
)

# ---------------------------------------------------------------------------
# R37-19 (#300) — the contracts-vs-reference separation constants. The
# labelled evaluation package ``forge.adaptive.reference`` holds the
# deterministic scenario builders and provider-shaped reference remotes;
# the runtime composition must reach them only through the registered
# compat homes (the modules the scenarios were extracted from).
# ---------------------------------------------------------------------------

#: The runtime entry points — the modules a customer's request enters
#: forge through. NONE of them may import the reference package: an
#: evaluation scenario beside a runtime contract must never become a
#: deployed guarantee by import.
REFERENCE_ENTRY_POINTS: tuple[str, ...] = (
    "forge.main",
    "forge.lane_driver",
    "forge.adaptive.wiring",
    "forge.runs.service",
    "forge.runs.github_service",
    "forge.runs.azure_service",
)

#: The ONLY non-reference modules that may import
#: ``forge.adaptive.reference.*``: the compat homes the scenarios were
#: extracted from (ADR-0031's migration policy — a compat import is
#: removed only when its callers are drained to the reference path).
REFERENCE_COMPAT_HOMES: frozenset[str] = frozenset(
    {
        "forge.adaptive.system_verification",
        "forge.adaptive.saga_durable",
        "forge.adaptive.steering_causality",
    }
)

#: What the reference package may never import — the provider services,
#: the wiring, ``main`` and the lane entry (it composes runtime
#: contracts only; ADR-0031 §2).
REFERENCE_FORBIDDEN_IMPORTS: frozenset[str] = frozenset(REFERENCE_ENTRY_POINTS)


# ---------------------------------------------------------------------------
# The AST scan substrate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _NodeContext:
    """Where a node sits: the enclosing function and class names."""

    functions: tuple[str, ...]
    classes: tuple[str, ...]


class _ModuleIndex:
    """One parsed production module with node → context indexing."""

    def __init__(self, module: str, tree: ast.Module) -> None:
        self.module = module
        self.tree = tree
        self._context: dict[int, _NodeContext] = {}
        self._walk(tree, (), ())

    def _walk(self, node: ast.AST, functions: tuple[str, ...], classes: tuple[str, ...]) -> None:
        self._context[id(node)] = _NodeContext(functions, classes)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                self._walk(child, (*functions, child.name), classes)
            elif isinstance(child, ast.ClassDef):
                self._walk(child, functions, (*classes, child.name))
            else:
                self._walk(child, functions, classes)

    def functions_of(self, node: ast.AST) -> tuple[str, ...]:
        context = self._context.get(id(node))
        return context.functions if context else ()

    def classes_of(self, node: ast.AST) -> tuple[str, ...]:
        context = self._context.get(id(node))
        return context.classes if context else ()

    def calls(self) -> Iterator[tuple[ast.Call, str]]:
        """Every call site with its dotted callee (``""`` when dynamic)."""
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                yield node, _dotted(node.func)

    def imported_modules(self) -> set[str]:
        """The absolute module names this module imports (incl. lazy)."""
        names: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module)
                for alias in node.names:
                    names.add(f"{node.module}.{alias.name}")
        return names


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _module_name(path: Path) -> str:
    relative = path.relative_to(SRC_FORGE).with_suffix("")
    parts = relative.parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return "forge." + ".".join(parts)


@functools.lru_cache(maxsize=1)
def _src_modules() -> dict[str, _ModuleIndex]:
    """Every production module under ``src/forge``, parsed and indexed."""
    modules: dict[str, _ModuleIndex] = {}
    for path in sorted(SRC_FORGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        module = _module_name(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # pragma: no cover — a broken module is a finding
            raise AssertionError(f"{path} does not parse: {exc}") from exc
        modules[module] = _ModuleIndex(module, tree)
    assert modules, f"no source modules found under {SRC_FORGE} — broken test?"
    return modules


def _synthetic(module: str, source: str) -> dict[str, _ModuleIndex]:
    """A one-module mapping for the intentional-violation trap tests."""
    return {module: _ModuleIndex(module, ast.parse(source))}


# ---------------------------------------------------------------------------
# Rule 1 — the legacy checkpoint lookup chain is confined
# ---------------------------------------------------------------------------


def _check_legacy_lookup_confinement(modules: dict[str, _ModuleIndex]) -> list[str]:
    """``architecture.legacy_call_sites``: the retired chain's allow-set.

    ``revival._legacy_http_lookup`` may be referenced ONLY by
    ``forge.runs.revival`` (the opt-in gate itself); ``_load_index`` and
    direct ``CheckpointStore`` construction ONLY by the enumerated
    chain modules — and inside ``runs.revival`` only from the confined
    adapter function. Anything else is a new dispatch path reaching
    around the configured authority.
    """
    violations: list[str] = []
    chain_modules = set(boundary_registry.LEGACY_LOOKUP_CHAIN_MODULES)
    confined_function = boundary_registry.LEGACY_LOOKUP_CONFINED_FUNCTION
    for index in modules.values():
        for node in ast.walk(index.tree):
            references_legacy = (
                isinstance(node, ast.Name) and node.id == "_legacy_http_lookup"
            ) or (isinstance(node, ast.Attribute) and node.attr == "_legacy_http_lookup")
            if references_legacy and index.module != "forge.runs.revival":
                violations.append(
                    f"{index.module}:{node.lineno} references the retired "
                    "revival._legacy_http_lookup — the retry/revival chain "
                    "goes through revival.durable_checkpoint_outcome (the "
                    "configured async authority), never the legacy adapter"
                )
            if isinstance(node, ast.Attribute) and node.attr == "_load_index":
                if index.module == "forge.api_checkpoint_channel":
                    continue  # the store's own method
                if index.module in chain_modules:
                    if confined_function not in index.functions_of(node):
                        violations.append(
                            f"{index.module}:{node.lineno} reads CheckpointStore."
                            "_load_index outside "
                            f"{confined_function}() — inside the allow-set the "
                            "legacy chain is reachable only from the opt-in adapter"
                        )
                else:
                    violations.append(
                        f"{index.module}:{node.lineno} reads CheckpointStore."
                        "_load_index — a checkpoint index read outside the "
                        "boundary's allow-set "
                        f"({sorted(boundary_registry.LEGACY_LOOKUP_CHAIN_MODULES)})"
                    )
        for call, callee in index.calls():
            if callee.split(".")[-1] != "CheckpointStore":
                continue
            if index.module in chain_modules and index.module != "forge.runs.revival":
                continue
            if index.module == "forge.runs.revival" and confined_function in index.functions_of(
                call
            ):
                continue
            violations.append(
                f"{index.module}:{call.lineno} constructs CheckpointStore directly "
                "— the one composition point is checkpoint_repository."
                "resolve_repository (wrap the store through the boundary, or "
                "register a versioned adapter in the allow-set)"
            )
    return violations


# ---------------------------------------------------------------------------
# Rule 2 — resolve_repository is the one authority composition point
# ---------------------------------------------------------------------------


def _check_resolve_repository_monopoly(modules: dict[str, _ModuleIndex]) -> list[str]:
    violations: list[str] = []
    allowed = set(boundary_registry.RESOLVE_REPOSITORY_CALLERS)
    for index in modules.values():
        for call, callee in index.calls():
            if callee != "resolve_repository" and not callee.endswith(".resolve_repository"):
                continue
            if index.module not in allowed:
                violations.append(
                    f"{index.module}:{call.lineno} calls {callee or 'resolve_repository'}() "
                    "— resolve_repository is the ONE checkpoint-authority "
                    "composition point; add the caller to "
                    "boundary_registry.RESOLVE_REPOSITORY_CALLERS only with a "
                    "reviewed reason (or route through an already-composed seam)"
                )
    return violations


# ---------------------------------------------------------------------------
# Rule 3 — AttemptStartSpec is composed, never hand-built in a service
# ---------------------------------------------------------------------------


def _check_attempt_start_construction(modules: dict[str, _ModuleIndex]) -> list[str]:
    violations: list[str] = []
    allowed = set(boundary_registry.ATTEMPT_START_CONSTRUCTORS)
    for index in modules.values():
        for call, callee in index.calls():
            if callee.split(".")[-1] != "AttemptStartSpec":
                continue
            if index.module not in allowed:
                violations.append(
                    f"{index.module}:{call.lineno} constructs AttemptStartSpec "
                    "directly — services compose the dispatch envelope through "
                    "composition_adoption.compose_attempt_start (the axis guard "
                    "and the pre-effect assert run there)"
                )
    # The registry's claim runs both ways: every registered composed
    # dispatch entry ACTUALLY composes through the adapter.
    for entry in boundary_registry.COMPOSED_DISPATCH_ENTRIES:
        index = modules.get(entry)
        if index is None:
            violations.append(
                f"the registered composed dispatch entry {entry} does not exist "
                "under src/forge — a stale registration; remove it"
            )
            continue
        callees = {callee for _, callee in index.calls()}
        if not any(callee.endswith("compose_attempt_start") for callee in callees):
            violations.append(
                f"{entry} is registered as a composed dispatch entry but never "
                "calls compose_attempt_start — the registration is the claim "
                "that this entry builds envelopes at the pre-effect boundary"
            )
    return violations


# ---------------------------------------------------------------------------
# Rule 4 — blob unlinks only inside the locked sweep entries
# ---------------------------------------------------------------------------


def _check_gc_unlink_confinement(modules: dict[str, _ModuleIndex]) -> list[str]:
    """CAS blob unlinking is the sweep entry points' exclusive right.

    Every ``unlink`` in the two confined modules must classify as one
    of: ``os.unlink(tmp_name)`` (an atomic-write temp file), the pins
    overlay file, the pending-GC journal file, or a ``self._cas_path``
    blob unlink INSIDE ``_sweep_locked``/``_asweep_locked`` (the volume
    lock holds from the final reference scan through the last unlink).
    """
    violations: list[str] = []
    sweeps = set(boundary_registry.GC_SWEEP_ENTRYPOINTS)
    seen_sweep_blobs: set[str] = set()
    for index in modules.values():
        if index.module not in _GC_CONFINED_MODULES:
            continue
        for call, _callee in index.calls():
            func = call.func
            if isinstance(func, ast.Name) and func.id == "unlink":  # from os import unlink
                pass
            elif isinstance(func, ast.Attribute) and func.attr == "unlink":
                pass
            else:
                continue
            if isinstance(func, ast.Attribute) and _dotted(func.value) == "os":
                # ``os.unlink(target)`` — the only exempt spelling is the
                # atomic-write temp file's cleanup.
                if len(call.args) == 1 and isinstance(call.args[0], ast.Name | ast.Constant):
                    target = _dotted(call.args[0])
                else:  # pragma: no cover — a computed target
                    target = ""
                if target == "tmp_name":
                    continue  # the atomic-write temp file — never a blob
                violations.append(
                    f"{index.module}:{call.lineno} os.unlink({target or '<expr>'}) — "
                    "only the atomic-write temp file (tmp_name) is exempt from "
                    "the sweep confinement"
                )
                continue
            if not isinstance(func, ast.Attribute):
                violations.append(
                    f"{index.module}:{call.lineno} bare unlink(...) — unclassifiable; "
                    "route blob deletion through the sweep entry points"
                )
                continue
            receiver = func.value
            # ``self._cas_path(d).unlink()``: the receiver is a CALL whose
            # callee is the path builder — classify on the callee.
            receiver_dotted = (
                _dotted(receiver.func) if isinstance(receiver, ast.Call) else (_dotted(receiver))
            )
            if receiver_dotted == "self._cas_path":
                enclosing = index.functions_of(call)
                if set(enclosing) & sweeps:
                    seen_sweep_blobs.update(set(enclosing) & sweeps)
                    continue
                violations.append(
                    f"{index.module}:{call.lineno} unlinks a CAS blob outside "
                    f"the sweep entry points {sorted(sweeps)} — a blob unlink "
                    "must hold the volume-wide GC reference/delete lock from "
                    "the final reference scan (R36-04)"
                )
                continue
            if receiver_dotted == "self._pins_path":
                continue  # the pins overlay file — never a CAS blob
            if isinstance(receiver, ast.Name) and receiver.id == "path":
                if "CheckpointGcJournal" in index.classes_of(call):
                    continue  # the pending-GC journal record — never a CAS blob
                violations.append(
                    f"{index.module}:{call.lineno} unlinks 'path' outside "
                    "CheckpointGcJournal — the only Name-path unlink exempt "
                    "from the sweep confinement is the journal record's own file"
                )
                continue
            violations.append(
                f"{index.module}:{call.lineno} unlinks an unclassified receiver "
                f"({receiver_dotted or '<expr>'}) — enumerate the exemption in "
                "boundary_registry.GC_UNLINK_EXEMPT_RECEIVERS or route the "
                "unlink through the sweep entry points"
            )
    # The inverse guard: every registered sweep entry point must EXIST and
    # actually unlink CAS blobs — renaming the seam may not hollow the rule.
    for entrypoint in sweeps:
        if entrypoint not in seen_sweep_blobs:
            violations.append(
                f"the registered GC sweep entry point {entrypoint!r} was not "
                "seen unlinking CAS blobs in "
                f"{sorted(_GC_CONFINED_MODULES)} — the registry or the seam "
                "renamed; update boundary_registry.GC_SWEEP_ENTRYPOINTS"
            )
    return violations


# ---------------------------------------------------------------------------
# Rule 5 — continuation modes are decided by the owner, never re-derived
# ---------------------------------------------------------------------------


def _collect_module_level_facts(tree: ast.Module) -> tuple[set[int], set[int], set[int], set[int]]:
    """(definition node ids, vocab definition ids, default-value ids, vocab names).

    Definitions include annotated assignments (``LANE_RESUME_MODE_FRESH:
    Final = "fresh"``); the vocabulary-definition ids cover the targets
    AND the values of the module-level mode constants (the one place the
    literals may appear).
    """
    definition_ids: set[int] = set()
    vocab_value_ids: set[int] = set()
    for stmt in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign):
            targets = list(stmt.targets)
            value = stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets = [stmt.target]
            value = stmt.value
        else:
            continue
        is_vocab = isinstance(targets[0], ast.Name) and targets[0].id in _MODE_CONSTANTS
        for target in targets:
            for node in ast.walk(target):
                if isinstance(node, ast.Name):
                    definition_ids.add(id(node))
                    if is_vocab:
                        vocab_value_ids.add(id(node))
        if is_vocab and value is not None:
            for node in ast.walk(value):
                vocab_value_ids.add(id(node))
    default_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            args = node.args
            # NOTE: ``kw_defaults`` is a list of expr-or-None aligned with
            # kwonlyargs — not a list of keyword nodes.
            for default in (*args.defaults, *(kw for kw in args.kw_defaults if kw is not None)):
                for sub in ast.walk(default):
                    if isinstance(sub, ast.Name):
                        default_ids.add(id(sub))
    vocab_names: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "LANE_RESUME_MODES":
            vocab_names.add(id(node))
    return definition_ids, vocab_value_ids, default_ids, vocab_names


def _check_continuation_mode_selection(modules: dict[str, _ModuleIndex]) -> list[str]:
    violations: list[str] = []
    owner = "forge.adaptive.continuation"
    boundary = boundary_registry.boundary_by_name("continuation_authorization")
    consumers = set(boundary.allowed_dependents)
    mode_builders = set(boundary_registry.CONTINUATION_MODE_CONSTRUCTION_MODULES)
    for index in modules.values():
        if index.module != owner and owner in index.imported_modules():
            if index.module not in consumers:
                violations.append(
                    f"{index.module} imports the continuation owner "
                    f"({owner}) but is not registered in "
                    "boundary_registry (the continuation_authorization "
                    "boundary's allowed_dependents) — the continuation "
                    "decision is owned; register the caller or consume its "
                    "persisted document"
                )
        for call, callee in index.calls():
            if callee.split(".")[-1] == "ContinuationMode" and index.module not in mode_builders:
                violations.append(
                    f"{index.module}:{call.lineno} constructs ContinuationMode — "
                    "modes are produced by continuation.decide_continuation / "
                    "matching_decision (and re-materialized by the compat "
                    "reader), never re-derived elsewhere"
                )
    home = modules.get(boundary_registry.MODE_VOCABULARY_HOME)
    if home is None:  # pragma: no cover — the vocabulary home must exist
        violations.append(
            f"the mode vocabulary home {boundary_registry.MODE_VOCABULARY_HOME} "
            "was not found under src/forge"
        )
        return violations
    definition_ids, vocab_value_ids, default_ids, vocab_names = _collect_module_level_facts(
        home.tree
    )
    for node in ast.walk(home.tree):
        if isinstance(node, ast.Name) and node.id in _MODE_CONSTANTS:
            if id(node) in vocab_names | definition_ids:
                continue  # the vocabulary definition / the validation set itself
            if node.id == "LANE_RESUME_MODE_FRESH" and id(node) in default_ids:
                continue  # the documented initial-dispatch default
            violations.append(
                f"{home.module}:{node.lineno} references {node.id} outside its "
                "definition — the dispatch vocabulary constants exist for the "
                "workflow-input contract; a mode a dispatch carries is the "
                "continuation decision's resume_mode() (or the documented "
                "initial-dispatch default), never re-derived from a constant"
            )
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in _MODE_LITERALS
            and id(node) not in vocab_value_ids
        ):
            violations.append(
                f"{home.module}:{node.lineno} hard-codes the mode literal "
                f"{node.value!r} — modes are decided by the continuation owner "
                "and passed through; the literals live only in LANE_RESUME_MODES"
            )
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg != "resume_mode":
                    continue
                value = keyword.value
                if isinstance(value, ast.Call) and _dotted(value.func).endswith(".resume_mode"):
                    continue  # the decision's selected mode — the pass-through
                if isinstance(value, ast.Name) and value.id == "resume_mode":
                    continue  # re-passing the dispatch parameter unchanged
                violations.append(
                    f"{home.module}:{node.lineno} passes resume_mode="
                    f"{ast.unparse(value)} — a dispatch carries a mode the "
                    "continuation decision selected (decision.resume_mode()) or "
                    "its own inherited parameter, never a fresh expression"
                )
    return violations


# ---------------------------------------------------------------------------
# Rule 6 — boundary registration: a new module entering an owned decision
# ---------------------------------------------------------------------------


def _check_boundary_registration(modules: dict[str, _ModuleIndex]) -> list[str]:
    violations: list[str] = []
    for boundary in boundary_registry.BOUNDARIES:
        owners = set(boundary.owner_modules)
        allowed = owners | set(boundary.allowed_dependents)
        for index in modules.values():
            if index.module in owners:
                continue
            hit = owners & index.imported_modules()
            if hit and index.module not in allowed:
                violations.append(
                    f"boundary {boundary.name!r}: {index.module} imports the "
                    f"owner ({sorted(hit)}) but is not registered in "
                    "boundary_registry — add the caller to the boundary's "
                    "allowed_dependents with a reviewed reason, or route the "
                    f"decision through the owner ({boundary.negative_contract})"
                )
        # The registry stays honest in the other direction too: a registered
        # dependent that no longer imports anything the boundary owns is a
        # stale registration.
        for dependent in boundary.allowed_dependents:
            index = modules.get(dependent)
            if index is None:
                violations.append(
                    f"boundary {boundary.name!r}: registered dependent "
                    f"{dependent} does not exist under src/forge"
                )
            elif not (owners & index.imported_modules()):
                violations.append(
                    f"boundary {boundary.name!r}: registered dependent "
                    f"{dependent} imports none of the owners {sorted(owners)} "
                    "— a stale registration; remove it"
                )
    return violations


# ---------------------------------------------------------------------------
# Rule 7 — the contracts-vs-reference separation (R37-19 / #300)
# ---------------------------------------------------------------------------


def _reference_hits(index: _ModuleIndex) -> set[str]:
    """The reference-package imports of *index* (module-level AND lazy)."""
    return {
        name for name in index.imported_modules() if name.startswith("forge.adaptive.reference")
    }


def _check_reference_separation(modules: dict[str, _ModuleIndex]) -> list[str]:
    """`architecture.owner_violations` (the reference axis): the runtime
    composition must not reach the labelled evaluation package except
    through the registered compat homes.

    - a RUNTIME ENTRY POINT importing the reference package is the
      review's defect class outright (a scenario's assumptions becoming
      a deployed guarantee by import);
    - any OTHER non-reference module importing it must be a registered
      compat home (the extraction's compatibility surface, pinned by
      ``tests/test_reference_separation.py``);
    - a registered compat home importing NOTHING from the package is a
      drained surface — remove the registration with the re-export.
    """
    violations: list[str] = []
    for index in modules.values():
        if index.module.startswith("forge.adaptive.reference"):
            continue  # the package's own imports are the purity rule below
        hits = _reference_hits(index)
        if not hits:
            continue
        if index.module in REFERENCE_ENTRY_POINTS:
            violations.append(
                f"{index.module} imports the reference package ({sorted(hits)}) — "
                "a runtime entry point never pulls an evaluation scenario in; "
                "compose the runtime contract the scenario fakes instead"
            )
        elif index.module not in REFERENCE_COMPAT_HOMES:
            violations.append(
                f"{index.module} imports the reference package ({sorted(hits)}) — "
                "only the registered compat homes may; route the decision "
                "through the runtime contract, or register the extraction "
                "home in REFERENCE_COMPAT_HOMES with a reviewed reason"
            )
    for home in sorted(REFERENCE_COMPAT_HOMES):
        index = modules.get(home)
        if index is None:
            violations.append(
                f"the registered reference compat home {home} does not exist "
                "under src/forge — a stale registration; remove it"
            )
        elif not _reference_hits(index):
            violations.append(
                f"{home} is registered as a reference compat home but imports "
                "nothing from the package — the compatibility surface has "
                "drained; remove the registration and the re-export together"
            )
    return violations


def _check_reference_purity(modules: dict[str, _ModuleIndex]) -> list[str]:
    """The reference package composes runtime CONTRACTS only: it never
    imports a provider service, the wiring, ``main`` or the lane entry,
    and adds no orchestration of its own."""
    violations: list[str] = []
    for index in modules.values():
        if not index.module.startswith("forge.adaptive.reference"):
            continue
        hits = index.imported_modules() & REFERENCE_FORBIDDEN_IMPORTS
        if hits:
            violations.append(
                f"{index.module} imports {sorted(hits)} — the reference "
                "package composes runtime contracts only; it must not reach "
                "a provider service, the wiring, main or the lane entry"
            )
    return violations


ALL_CHECKS = (
    ("legacy lookup confinement", _check_legacy_lookup_confinement),
    ("resolve_repository monopoly", _check_resolve_repository_monopoly),
    ("AttemptStartSpec construction", _check_attempt_start_construction),
    ("GC unlink confinement", _check_gc_unlink_confinement),
    ("continuation mode selection", _check_continuation_mode_selection),
    ("boundary registration", _check_boundary_registration),
    ("reference separation", _check_reference_separation),
    ("reference purity", _check_reference_purity),
)


class TestAuthorityBoundaryRules:
    """Every rule, over the real production tree — zero violations."""

    @pytest.mark.parametrize(
        ("name", "check"),
        [(name, check) for name, check in ALL_CHECKS],
        ids=[name for name, _ in ALL_CHECKS],
    )
    def test_rule_holds_over_the_production_tree(self, name: str, check) -> None:
        violations = check(_src_modules())
        assert not violations, f"{name} violations:\n" + "\n".join(violations)

    def test_the_registry_six_boundaries_are_the_adr_thirty_set(self) -> None:
        assert [boundary.name for boundary in boundary_registry.BOUNDARIES] == [
            "repository_identity_checkpoint_lifecycle",
            "continuation_authorization",
            "native_occupancy",
            "candidate_publication",
            "verification_applicability",
            "operator_projection",
        ]

    def test_every_boundary_declares_owners_callers_and_negative_contract(self) -> None:
        for boundary in boundary_registry.BOUNDARIES:
            assert boundary.owner_modules, f"{boundary.name} names no owner"
            assert boundary.decision and boundary.enforcement and boundary.negative_contract


class TestIntentionalViolationTraps:
    """The negative tests the issue demands: a legacy call introduced → FAIL.

    Each trap feeds the checker a SYNTHETIC module carrying exactly the
    violation the rule exists to catch, and asserts detection — the
    checkers may never pass vacuously.
    """

    def test_a_new_dispatch_path_dialing_the_legacy_lookup_is_caught(self) -> None:
        modules = _synthetic(
            "forge.gateway.new_path",
            "from forge.runs import revival\n"
            "\n"
            "\n"
            "async def lookup(run_id: str) -> bool:\n"
            "    outcome = await revival._legacy_http_lookup(run_id)\n"
            "    return outcome.is_exact\n",
        )
        violations = _check_legacy_lookup_confinement(modules)
        assert any("_legacy_http_lookup" in text for text in violations)

    def test_a_raw_index_read_outside_the_allow_set_is_caught(self) -> None:
        modules = _synthetic(
            "forge.worker.new_sweep",
            "from forge.api_checkpoint_channel import CheckpointStore\n"
            "\n"
            "\n"
            "def peek(work_id: str) -> bool:\n"
            "    index = CheckpointStore('/tmp')._load_index(work_id)\n"
            "    return bool(index.get('checkpoints'))\n",
        )
        violations = _check_legacy_lookup_confinement(modules)
        assert any("_load_index" in text for text in violations)
        assert any("CheckpointStore" in text for text in violations)

    def test_the_legacy_chain_inside_revival_outside_the_adapter_is_caught(self) -> None:
        modules = _synthetic(
            "forge.runs.revival",
            "from forge.api_checkpoint_channel import CheckpointStore\n"
            "\n"
            "\n"
            "def _modern_path(run_id: str) -> bool:\n"
            "    index = CheckpointStore('/tmp')._load_index(run_id)\n"
            "    return bool(index.get('checkpoints'))\n",
        )
        violations = _check_legacy_lookup_confinement(modules)
        assert any("outside" in text and "_load_index" in text for text in violations)

    def test_an_unregistered_resolve_repository_call_is_caught(self) -> None:
        modules = _synthetic(
            "forge.worker.new_sweep",
            "from forge.adaptive.checkpoint_repository import resolve_repository\n"
            "\n"
            "\n"
            "def build():\n"
            "    return resolve_repository()\n",
        )
        violations = _check_resolve_repository_monopoly(modules)
        assert any("resolve_repository" in text for text in violations)

    def test_a_service_hand_building_an_attempt_start_spec_is_caught(self) -> None:
        modules = _synthetic(
            "forge.runs.azure_service",
            "from forge.runs.composition import AttemptStartSpec\n"
            "\n"
            "\n"
            "def envelope(run_id: str):\n"
            "    return AttemptStartSpec(run_id=run_id)\n",
        )
        violations = _check_attempt_start_construction(modules)
        assert any("AttemptStartSpec" in text for text in violations)

    def test_a_blob_unlink_outside_the_sweep_entries_is_caught(self) -> None:
        trap = (
            "import os\n"
            "\n"
            "\n"
            "def _cleanup(digest: str) -> None:\n"
            "    self_path = type('S', (), {'_cas_path': lambda _, __: __})\n"
            "    os.unlink(digest)\n"
            "\n"
            "\n"
            "class Store:\n"
            "    def _cas_path(self, digest: str):\n"
            "        return digest\n"
            "\n"
            "    def _cleanup(self, digest: str) -> None:\n"
            "        self._cas_path(digest).unlink(missing_ok=True)\n"
            "\n"
            "    def _sweep_locked(self, digests: list[str]) -> set[str]:\n"
            "        for digest in digests:\n"
            "            self._cas_path(digest).unlink(missing_ok=True)\n"
            "        return set(digests)\n"
        )
        violations = _check_gc_unlink_confinement(_synthetic("forge.api_checkpoint_channel", trap))
        assert any("outside" in text and "sweep" in text for text in violations)
        # ...and the SAME unlink inside the sweep entry point is not flagged:
        assert not any(":13" in text or "line 13" in text for text in violations)

    def test_a_mode_rederivation_is_caught(self) -> None:
        home = _src_modules()[boundary_registry.MODE_VOCABULARY_HOME]
        extra = ast.parse(
            "class _Trap:\n"
            "    def dispatch(self, run_id, decision):\n"
            "        self._advance(run_id, resume_mode='required')\n"
            "        self._advance(run_id, resume_mode=decision.resume_mode())\n"
            "        mode = 'fresh'\n"
            "        self._advance(run_id, resume_mode=mode)\n"
            "        self._advance(run_id, resume_mode=LANE_RESUME_MODE_RESTART)\n"
        )
        grafted = ast.Module(
            body=[*home.tree.body, *extra.body],
            type_ignores=[],
        )
        modules = {home.module: _ModuleIndex(home.module, grafted)}
        violations = _check_continuation_mode_selection(modules)
        joined = "\n".join(violations)
        assert "hard-codes the mode literal 'required'" in joined
        assert "hard-codes the mode literal 'fresh'" in joined
        assert "LANE_RESUME_MODE_RESTART" in joined
        assert (
            "resume_mode=mode" in joined
        )  # a Name pass-through of a literal is still re-derivation
        # the decision's resume_mode() pass-through (line 4 of the trap) is
        # NOT flagged — only the re-derivations are:
        assert not any(text.startswith("forge.runs.github_service:4 ") for text in violations), (
            joined
        )

    def test_an_unregistered_boundary_dependent_is_caught(self) -> None:
        modules = _synthetic(
            "forge.runs.some_new_service",
            "from forge.adaptive.checkpoint_repository import (\n"
            "    resolve_checkpoint_lookup_authority,\n"
            ")\n"
            "\n"
            "\n"
            "def authority():\n"
            "    return resolve_checkpoint_lookup_authority()\n",
        )
        violations = _check_boundary_registration(modules)
        assert any(
            "repository_identity_checkpoint_lifecycle" in text and "some_new_service" in text
            for text in violations
        )

    def test_a_stale_registration_is_caught(self) -> None:
        modules = _synthetic("forge.adaptive.continuation", "X = 1\n")
        violations = _check_boundary_registration(modules)
        # With the whole tree absent, every registered dependent is stale
        # or missing — the continuation boundary's dependent flags either way.
        assert any("stale registration" in text or "does not exist" in text for text in violations)

    def test_a_runtime_entry_point_importing_the_reference_package_is_caught(self) -> None:
        modules = _synthetic(
            "forge.runs.service",
            "from forge.adaptive.reference.native_shaped_remote import NativeShapedRemote\n"
            "\n"
            "\n"
            "def build():\n"
            "    return NativeShapedRemote()\n",
        )
        violations = _check_reference_separation(modules)
        assert any(
            "runtime entry point" in text and "reference package" in text for text in violations
        )

    def test_an_unregistered_module_importing_the_reference_package_is_caught(self) -> None:
        modules = _synthetic(
            "forge.orchestrator.new_leg",
            "from forge.adaptive.reference.system_twin import default_twin_scenario\n"
            "\n"
            "\n"
            "def scenario():\n"
            "    return default_twin_scenario()\n",
        )
        violations = _check_reference_separation(modules)
        assert any("compat home" in text and "new_leg" in text for text in violations)

    def test_a_lazy_compat_import_is_still_an_import(self) -> None:
        """The confinement counts FUNCTION-LEVEL imports too — the lazy
        re-export pattern must stay registered, not hide."""
        modules = _synthetic(
            "forge.gateway.hidden_leg",
            "def build():\n"
            "    from forge.adaptive.reference.system_twin import TwinScenario\n"
            "\n"
            "    return TwinScenario\n",
        )
        violations = _check_reference_separation(modules)
        assert any("hidden_leg" in text for text in violations)

    def test_a_drained_compat_home_registration_is_caught(self) -> None:
        modules = _synthetic("forge.adaptive.saga_durable", "X = 1\n")
        violations = _check_reference_separation(modules)
        assert any("drained" in text or "does not exist" in text for text in violations)

    def test_a_reference_module_importing_the_wiring_is_caught(self) -> None:
        modules = _synthetic(
            "forge.adaptive.reference.new_scenario",
            "from forge.adaptive.wiring import compose_runtime\n"
            "\n"
            "\n"
            "def build():\n"
            "    return compose_runtime()\n",
        )
        violations = _check_reference_purity(modules)
        assert any("composes runtime contracts only" in text for text in violations)


# ---------------------------------------------------------------------------
# Provider conformance — `contract.provider_conformance`
# ---------------------------------------------------------------------------


def _dead_run(**overrides: Any) -> FlowRun:
    """A dead GitHub run for the refusal table (no DB needed)."""
    values: dict[str, Any] = dict(
        id="a" * 32,
        project_id=99183,
        provider="github",
        issue_iid=12,
        status=FlowStatus.FAILED.value,
        status_reason="harness_timeout",
        candidate_shas=[],
        cancel_requested=False,
    )
    values.update(overrides)
    return FlowRun(**values)


class _FencedRepository:
    """A repository whose authority is FENCED mid-cutover (R36-05).

    ``lookup_outcome`` raises :class:`MutationsFencedError` — a subtype
    of :class:`CheckpointRepositoryUnavailable` — which is exactly what
    an already-running old-authority process hits after the marker
    flips. The conformance question: does every provider's retry path
    surface that as the TYPED authority-unavailable refusal, never as
    "no checkpoint"?
    """

    async def lookup_outcome(self, work_id: str) -> CheckpointLookupOutcome:  # pragma: no cover
        raise MutationsFencedError(
            "authority fenced: configured=postgres active=filesystem (cutover in progress)"
        )


def _typed_outcomes() -> dict[str, CheckpointLookupOutcome]:
    return {
        "typed_unavailable": CheckpointLookupOutcome.missing(
            LOOKUP_UNAVAILABLE, authority="postgres", detail="database outage"
        ),
        "corrupt": CheckpointLookupOutcome.missing(
            LOOKUP_CORRUPT, authority="postgres", detail="stored bytes no longer hash"
        ),
        "unauthorized": CheckpointLookupOutcome.missing(
            LOOKUP_UNAUTHORIZED, authority="checkpoint-channel", detail="credential refused"
        ),
        "absent": CheckpointLookupOutcome.missing(
            LOOKUP_ABSENT, authority="postgres", detail="proven absence"
        ),
    }


class TestProviderConformance:
    """The same negative inputs, equivalent typed refusals everywhere.

    The SHARED core is ``revival.retry_rejection`` / ``why_blocked_reply``
    — the one refusal table the GitLab, Azure and GitHub handlers all
    call. The GitHub path additionally composes the continuation
    decision; these tests pin that BOTH paths answer the same typed
    codes for the same inputs, with the native detail retained.
    """

    @pytest.mark.parametrize("state", ["typed_unavailable", "corrupt", "unauthorized"])
    async def test_an_unprovable_checkpoint_refuses_the_same_everywhere(self, state: str) -> None:
        outcome = _typed_outcomes()[state]
        run = _dead_run()
        # The SHARED core (GitLab/Azure/GitHub handlers all call this):
        rejection = retry_rejection(run, checkpoint=outcome)
        assert isinstance(rejection, RetryRejection)
        assert rejection.code == RetryRefusalCode.CHECKPOINT_AUTHORITY_UNAVAILABLE.value
        # ...and the GitHub continuation path over the SAME outcome never
        # turns an unprovable state into a dispatch decision:
        committed, digest = normalize_checkpoint_result(outcome)
        assert committed is None and digest is None
        evidence = await evidence_from_record(death_reason="harness_timeout", run_id=run.id)
        decision = decide_continuation(evidence)
        assert decision.mode is ContinuationMode.UNCERTAIN
        assert not decision.dispatchable
        with pytest.raises(ValueError, match="UNCERTAIN"):
            decision.resume_mode()

    async def test_a_proven_absence_stays_distinct_from_unavailability(self) -> None:
        """The contrast arm: absent is the ONLY state that refuses with
        NOTHING_TO_RETRY — the two refusals may never converge, on any
        provider (that convergence was the R36-03 defect)."""
        outcome = _typed_outcomes()["absent"]
        rejection = retry_rejection(_dead_run(), checkpoint=outcome)
        assert isinstance(rejection, RetryRejection)
        assert rejection.code == RetryRefusalCode.NOTHING_TO_RETRY.value
        committed, digest = normalize_checkpoint_result(outcome)
        assert committed is False and digest is None

    async def test_a_fenced_authority_surfaces_as_typed_unavailable(self) -> None:
        """The fence refusal (a cutover mid-flight) may never read as
        absence on ANY retry path: the GitHub-consumed adapter answers
        the typed unavailable, and the shared core words it as such."""
        outcome = await revival.durable_checkpoint_outcome("a" * 32, repository=_FencedRepository())
        assert outcome.state == LOOKUP_UNAVAILABLE
        assert "fenced" in outcome.detail.lower()
        rejection = retry_rejection(_dead_run(), checkpoint=outcome)
        assert isinstance(rejection, RetryRejection)
        assert rejection.code == RetryRefusalCode.CHECKPOINT_AUTHORITY_UNAVAILABLE.value
        assert "unavailable" in rejection

    @pytest.mark.parametrize("state", ["typed_unavailable", "corrupt", "unauthorized"])
    def test_why_blocked_reports_the_authority_not_a_guess(self, state: str) -> None:
        """The read-only operator surface consumes the SAME table — the
        authority word (native detail) is retained per state."""
        body = why_blocked_reply(_dead_run(), checkpoint=_typed_outcomes()[state])
        assert "retry" in body.lower()
        assert "unavailable" in body or "corrupt" in body or "refused" in body

    def test_the_one_refusal_table_is_what_every_provider_calls(self) -> None:
        """AST parity: all three provider services consult the ONE
        ``retry_rejection`` and the ONE typed lookup adapter; only
        ``runs.revival`` constructs refusals — no provider re-derives a
        refusal table (the review's 'same decision in two places')."""
        modules = _src_modules()
        for service in (
            "forge.runs.service",
            "forge.runs.azure_service",
            "forge.runs.github_service",
        ):
            callees = {callee for _, callee in modules[service].calls()}
            assert "retry_rejection" in callees, f"{service} must consult the shared refusal table"
            assert any(callee.endswith("durable_checkpoint_outcome") for callee in callees), (
                f"{service} must consume the typed configured authority"
            )
        for index in modules.values():
            for _, callee in index.calls():
                assert not (
                    callee.endswith("RetryRejection") and index.module != "forge.runs.revival"
                ), f"{index.module} constructs RetryRejection — the table is runs.revival's"

    def test_the_gitlab_lane_resume_seam_and_the_azure_gap_are_the_documented_state(self) -> None:
        """#268's finding, updated by R37-07 (#288): the lane-resume dispatch
        contract is now wired on GitHub AND the GitLab CE lane (the
        pipeline-variable envelope; the decision still comes from the ONE
        owner — runs.service references the vocabulary and passes
        ``resume_mode=decision.resume_mode()``, never a mode of its own
        derivation). Azure remains the honest gap — fabricating parity
        would be worse than the gap; the registry records it and this
        pins it."""
        modules = _src_modules()
        gitlab = modules["forge.runs.service"]
        resume_mode_passes = [
            keyword
            for node in ast.walk(gitlab.tree)
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg == "resume_mode"
        ]
        assert resume_mode_passes, "the GitLab lane must dispatch the resume contract (R37-07)"
        for keyword in resume_mode_passes:
            value = ast.unparse(keyword.value)
            assert value.endswith(".resume_mode()") or value == "resume_mode", (
                f"runs.service passes resume_mode={value} — a dispatch carries the "
                "decision's selected mode, never a fresh expression"
            )
        vocabulary = {node.id for node in ast.walk(gitlab.tree) if isinstance(node, ast.Name)}
        assert any("RESUME_MODE" in name for name in vocabulary)
        for service in ("forge.runs.azure_service",):
            for node in ast.walk(modules[service].tree):
                if isinstance(node, ast.Name) and "RESUME_MODE" in node.id:
                    raise AssertionError(
                        f"{service} references {node.id} — the lane-resume contract "
                        "is not wired on Azure; wire the seam or record the gap"
                    )
                if isinstance(node, ast.Call):
                    for keyword in node.keywords:
                        assert keyword.arg != "resume_mode", (
                            f"{service}:{keyword.lineno} passes a resume mode — "
                            "the lane-resume dispatch contract is not wired on Azure"
                        )
        gap = boundary_registry.boundary_by_name("continuation_authorization").honest_gaps
        assert "Azure remains the honest gap" in gap


class TestStaleEpochConformance:
    """The third negative input: a stale authority epoch.

    Two owners produce the typed refusals and NO provider re-derives
    either: the lane-control credential oracle (``api_lane_control``)
    and the envelope's authority axis (``composition_adoption``).
    """

    def test_the_superseded_generation_oracle_names_the_retired_epoch(self) -> None:
        from forge.api_lane_control import _superseded_generation, lane_control_token

        secret = "conformance-secret"  # noqa: S105 — test value
        work_id = "a" * 32
        retired = lane_control_token(secret, work_id, generation=2)
        current = lane_control_token(secret, work_id, generation=3)
        assert _superseded_generation(secret, retired, work_id, 3) == 2
        assert _superseded_generation(secret, current, work_id, 3) is None

    def test_the_epoch_oracle_has_exactly_one_owner(self) -> None:
        modules = _src_modules()
        for index in modules.values():
            for node in ast.walk(index.tree):
                if isinstance(node, ast.Name | ast.Attribute) and (
                    getattr(node, "id", None) == "_superseded_generation"
                    or getattr(node, "attr", None) == "_superseded_generation"
                ):
                    assert index.module == "forge.api_lane_control", (
                        f"{index.module} re-derives credential staleness — the "
                        "oracle is api_lane_control's (one owner, all providers)"
                    )

    def test_the_envelope_refuses_a_bad_epoch_and_encodes_a_good_one(self) -> None:
        kwargs: dict[str, Any] = dict(
            run_id="a" * 32,
            repo_full_name="acme/widgets",
            project_id=99183,
            attempt_oid="e" * 40,
            authority_epoch=3,
            attempt_ordinal=3,
            profile_digest="6" * 64,
            fallback_profile_digest="5" * 64,
            resume_mode="fresh",
            lease_id="lease-1",
        )
        with pytest.raises(CompositionBoundaryError, match="authority axis"):
            compose_attempt_start(**{**kwargs, "authority_epoch": -1})
        # A good epoch is INSIDE the envelope digest: a different epoch is a
        # different authorization — and identical durable inputs reconstruct
        # the identical envelope (intent vs repeat delivery, R36-06).
        first = compose_attempt_start(**kwargs)
        same_again = compose_attempt_start(**kwargs, prior_document=first.document)
        assert same_again.spec.envelope_digest() == first.spec.envelope_digest()
        assert same_again.unchanged is True
        bumped = compose_attempt_start(**{**kwargs, "authority_epoch": 4, "attempt_ordinal": 4})
        assert bumped.spec.envelope_digest() != first.spec.envelope_digest()
