"""Task-aware harness selection (ADR-0023): the pure compiler + fallback step.

Selection happens at PLAN time and is frozen into the RunSpec — it is part
of the decision the human gate approves (ADR-0023 Decision 1). This module
owns the deterministic side of that decision:

- :func:`compile_harness_selection` — preference ∩ available drivers ∪
  (optional) planner proposal → the frozen :class:`HarnessSelection`. The
  planner may reorder, never extend: it is never the authority (rule 4).
  R31: the available set is the project's capability manifest
  (:func:`resolve_available_drivers`) — a driver the project did not
  onboard is never selected, whatever the preference or the proposal says.
- :func:`advance_harness_fallback` — the shared dispatch-time fallback step
  (ADR-0023 Decision 3): pure; given the current selection and a classified
  lane failure it returns the next selection down the frozen chain, or None
  (OFF by default, infrastructure-only, pre-candidate-only).

Everything here is deterministic (no clocks, no randomness, no I/O): same
inputs → identical output, so *same RunSpec → same selection*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from forge.config import ForgeConfig, Settings

__all__ = [
    "BUDGET_CLASSES",
    "BudgetCeilings",
    "DEFAULT_DRIVER",
    "DRIVER_CREDENTIAL_VARS",
    "SHIPPED_DRIVERS",
    "HarnessSelection",
    "advance_harness_fallback",
    "compile_harness_selection",
    "current_driver",
    "implementation_block",
    "parse_driver_entry",
    "parse_preference",
    "resolve_available_drivers",
    "resolve_preference",
    "selection_from_spec_document",
    "validate_preference",
]

#: The shipped harness driver ids — one GitLab template (and one Actions-lane
#: script) each. A preference id outside this set is a config error; the
#: compiler additionally caps everything by ``available_lanes``, so an
#: unshipped id can never be selected even if a caller passes it as a lane.
SHIPPED_DRIVERS: frozenset[str] = frozenset({"claude-code", "grok-build", "opencode", "copilot"})

#: The driver selected when no explicit one is configured (ADR-0015: the bare
#: ``ci_harness`` backend and the builtin backend both land here).
DEFAULT_DRIVER = "claude-code"

#: Planner budget classes (ADR-0023 Decision 1): anything else degrades to
#: the compiler's ``default_budget_class``.
BUDGET_CLASSES: frozenset[str] = frozenset({"trivial", "standard", "heavy"})

#: Per-driver credential variable NAMES (never values — ADR-0015 §4): what
#: ``forge doctor`` checks per preference entry (brief §8) and what a lane
#: without creds looks like (the job fails infrastructure at the CI layer).
DRIVER_CREDENTIAL_VARS: dict[str, tuple[str, ...]] = {
    "claude-code": ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"),
    "grok-build": ("FORGE_GROK_AUTH",),
    "opencode": ("ZAI_API_KEY",),
    "copilot": ("COPILOT_GITHUB_TOKEN",),
}

#: OPTIONAL per-driver credential surfaces (C03): a recipe may map them
#: beside the required ones (the Actions recipe maps ANTHROPIC_API_KEY for
#: key-auth gateways); doctor does NOT require them.
DRIVER_OPTIONAL_CREDENTIAL_VARS: dict[str, tuple[str, ...]] = {
    "claude-code": ("ANTHROPIC_API_KEY",),
}

#: A version/dist-tag token in a pinned chain entry (``driver@version``):
#: letters, digits, dot, underscore, dash — never shell metacharacters.
_ENTRY_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class BudgetCeilings:
    """The numeric ceilings a budget class resolves to at freeze time (R31).

    The local mirror of the R13 profile axes
    (:func:`forge.durable.budgets.resolve_budget_limits` — same names, same
    ``None`` = unlimited semantics); kept module-local so this pure module
    stays stdlib-only. Attached to a selection by the service AFTER the
    class resolves against the configured profiles, so the selection
    evidence shows the exact ceilings the gate approves.
    """

    max_calls: int | None = None
    max_tokens: int | None = None
    wallclock_s: int | None = None


@dataclass(frozen=True)
class HarnessSelection:
    """The frozen harness decision the gate approves (ADR-0023 Decision 1).

    ``harness`` is the selected driver; ``fallbacks`` is the frozen ordered
    tail (subset of the allowed chain, recorded in the spec even when the
    dispatch-time fallback switch is OFF — the spec describes the chain, the
    switch is a separate runtime policy). ``budget_ceilings`` (R31, optional)
    records the numeric ceilings ``budget_class`` resolved to at freeze time
    (R13 profiles); ``None`` — nothing configured, the run is unlimited and
    the evidence carries no ceiling block.
    """

    harness: str
    fallbacks: tuple[str, ...]
    budget_class: str
    reason: str
    budget_ceilings: BudgetCeilings | None = None

    def as_document(self) -> dict:
        """The ``backend_config`` fragment frozen into the RunSpec."""
        document: dict = {
            "harness": self.harness,
            "harness_fallbacks": list(self.fallbacks),
            "budget_class": self.budget_class,
            "selection_reason": self.reason,
        }
        if self.budget_ceilings is not None:
            document["budget_ceilings"] = {
                "max_calls": self.budget_ceilings.max_calls,
                "max_tokens": self.budget_ceilings.max_tokens,
                "wallclock_s": self.budget_ceilings.wallclock_s,
            }
        return document


def implementation_block(
    selection: HarnessSelection,
    *,
    model: str,
    commit_cycles: int,
) -> str:
    """The plan comment's gate-visible execution shape (brief §4).

    Five fixed lines, rendered between the plan body and the ``/go``
    footer: ``/go`` authorizes the execution shape, not just the plan text
    (this strengthens ADR-0009 — today the harness is invisible at the
    gate). ``Fallbacks:`` reads ``none`` when the frozen tail is empty.
    """
    model_note = f" · model {model}" if model else ""
    fallback_note = ", ".join(selection.fallbacks) if selection.fallbacks else "none"
    return (
        "## Implementation\n"
        f"- Harness: **{selection.harness}**{model_note}\n"
        f"- Fallbacks: {fallback_note}\n"
        f"- Budget class: {selection.budget_class}\n"
        f"- Commit cycles: {commit_cycles}\n"
        f"- Selection reason: {selection.reason}\n"
    )


def current_driver(backend: str | None) -> str:
    """The driver id behind a ``FORGE_IMPLEMENTER_BACKEND`` value.

    ``ci_harness:<driver>`` names its driver; the bare ``ci_harness`` and
    every other value (``builtin``) land on :data:`DEFAULT_DRIVER`.
    """
    raw = str(backend or "").strip()
    if raw.startswith("ci_harness:"):
        suffix = raw.partition(":")[2].strip()
        if suffix:
            return suffix
    return DEFAULT_DRIVER


def parse_preference(raw: str | None) -> list[str]:
    """The comma-separated env form of the preference list (deduplicated,
    order preserved). Empty/None → ``[]`` (the config default applies)."""
    seen: list[str] = []
    for entry in str(raw or "").split(","):
        driver = entry.strip()
        if driver and driver not in seen:
            seen.append(driver)
    return seen


def parse_driver_entry(entry: str) -> tuple[str, str | None]:
    """Split a preference-chain entry into ``(driver, version)`` (R15).

    ``grok-build@1.0.30`` → ``("grok-build", "1.0.30")``; a bare id →
    ``(id, None)``. A malformed pin — empty driver, or a version with
    characters outside ``[A-Za-z0-9._-]`` — raises ``ValueError``.

    The version is accepted-and-validated metadata ONLY: the frozen
    selection and the dispatch contract stay bare-driver, and the pin's
    enforcement point is the lane's ``FORGE_DRIVER_VERSIONS`` variable
    (forge.harness_entry pins the actual npm installs). Carrying the pin
    through the dispatch inputs is deliberately not in this slice.
    """
    text = str(entry or "").strip()
    name, sep, version = text.partition("@")
    if not sep:
        return text, None
    if not name or not _ENTRY_VERSION_RE.match(version):
        raise ValueError(
            f"malformed pinned harness entry {text!r} — expected driver@version "
            "(version: letters, digits, dot, underscore, dash)"
        )
    return name, version


def resolve_preference(config: ForgeConfig, settings: Settings) -> list[str]:
    """The project's ordered preference list (brief §1).

    ``ForgeConfig.implement.harnesses`` (the YAML form) wins; the
    ``FORGE_HARNESS_PREFERENCE`` env form is the lab/CI alternative. Empty —
    the existing backend as a one-element list (byte-compatible).
    """
    from_config = list(config.harness_preference)
    if from_config:
        return from_config
    return parse_preference(str(getattr(settings, "FORGE_HARNESS_PREFERENCE", "") or ""))


def resolve_available_drivers(config: ForgeConfig, settings: Settings) -> set[str]:
    """The project's available-driver set — the R31 capability manifest.

    ``ForgeConfig.implement.available_drivers`` (the YAML form) wins; the
    ``FORGE_AVAILABLE_DRIVERS`` JSON is the lab/CI alternative (the same
    precedence as :func:`resolve_preference`). Unset everywhere — every
    shipped driver is available, which is exactly the set the compiler was
    handed before R31 existed (byte-compat). This set only ever CAPS the
    chain: a driver outside it is dropped before anything else, so a
    preference entry or a planner proposal can never select it. An R15 pin
    (``FORGE_DRIVER_VERSIONS``) presupposes the capability — a pinned entry
    never widens this set.
    """
    from_config = list(config.available_drivers)
    if from_config:
        return set(from_config)
    from forge.config import parse_available_drivers

    return set(parse_available_drivers(str(getattr(settings, "FORGE_AVAILABLE_DRIVERS", "") or "")))


def validate_preference(preference: list[str], driver: str | None = None) -> None:
    """Config-validation time checks (brief §1), tighten-only (ADR-0015).

    Ids must be shipped drivers, always — a R15 pinned entry
    (``driver@version``, :func:`parse_driver_entry`) validates by its
    driver part. When *driver* names the backend's harness driver
    (``ci_harness[:<driver>]`` — the builtin backend passes None, it
    dispatches no harness), the list must include it: the list tightens,
    never deselects, the configured backend. Raises ``ValueError`` — a
    contradictory configuration is refused, never silently repaired (the
    same posture as the backend factory's unknown backend). An empty
    preference is always valid (the backend default).
    """
    names = [parse_driver_entry(entry)[0] for entry in preference]
    unknown = [entry for entry, name in zip(preference, names) if name not in SHIPPED_DRIVERS]
    if unknown:
        shipped = ", ".join(sorted(SHIPPED_DRIVERS))
        raise ValueError(
            f"unknown harness driver(s) in implement.harnesses: {', '.join(unknown)} "
            f"(shipped: {shipped})"
        )
    if preference and driver is not None and driver not in names:
        raise ValueError(
            f"implement.harnesses must include the configured backend driver "
            f"{driver!r} — the list tightens, never deselects, the backend (ADR-0015)"
        )


def compile_harness_selection(
    preference: list[str],
    current_backend: str,
    available_lanes: set[str],
    planner_proposal: dict | None,
    default_budget_class: str = "standard",
) -> HarnessSelection:
    """Compile the frozen harness decision (brief §2, rules 1–6).

    *preference* is the project's ordered list (may be empty);
    *available_lanes* the drivers the project onboarded — the R31 capability
    manifest (:func:`resolve_available_drivers`) feeds this from the service,
    so an un-onboarded driver is never selected even when preferred or
    proposed; *planner_proposal* the optional structured
    ``{"harness", "budget_class", "reason"}`` from the planner. The
    proposal is policy-constrained ranking, never free choice: a harness
    outside preference ∩ available is ignored, and the budget class is
    validated against the closed :data:`BUDGET_CLASSES` set independently.
    """
    driver = current_driver(current_backend)

    # Rules 1+3: the lanes cap the chain — a proposal can reorder, never
    # extend, and non-onboarded entries are dropped before anything else.
    # R15: a pinned entry (driver@version) enters the chain by its bare
    # driver id — the frozen selection and the dispatch contract stay
    # bare-driver; the pin's enforcement point is the lane's
    # FORGE_DRIVER_VERSIONS variable. Duplicates collapse on the bare id
    # (first occurrence wins).
    chain: list[str] = []
    for entry in preference:
        name = parse_driver_entry(entry)[0]
        if name in available_lanes and name not in chain:
            chain.append(name)
    if not chain:
        # Rule 2 (byte-compat) + the ADR-0015 floor: the configured backend
        # is always runnable — an empty/fully-dropped preference degrades to
        # the one-element list today's behavior implies.
        chain = [driver]

    budget_class = default_budget_class if default_budget_class in BUDGET_CLASSES else "standard"
    reason = "default"
    selected = chain[0]

    # Rule 4: the proposal is honored iff its harness is in preference ∩
    # available (i.e. an actual member of the compiled chain); its budget
    # class is validated independently; the reason defaults when absent.
    proposal = planner_proposal if isinstance(planner_proposal, dict) else None
    if proposal is not None:
        proposed_harness = str(proposal.get("harness") or "").strip()
        if proposed_harness in chain:
            selected = proposed_harness
            reason = str(proposal.get("reason") or "").strip() or "planner selection"
        proposed_budget = str(proposal.get("budget_class") or "").strip()
        if proposed_budget in BUDGET_CLASSES:
            budget_class = proposed_budget

    # Rule 5: the frozen tail after the selected harness (∩ available) —
    # always recorded, even when the fallback switch is OFF.
    fallbacks = tuple(chain[chain.index(selected) + 1 :])
    return HarnessSelection(
        harness=selected,
        fallbacks=fallbacks,
        budget_class=budget_class,
        reason=reason,
    )


def selection_from_spec_document(document: dict | None) -> HarnessSelection | None:
    """Rebuild the selection frozen in a RunSpec document (post-gate reads).

    None for pre-v2 documents (no ``backend_config.harness`` key) — callers
    fall back to the configured backend, which keeps every pre-ADR-0023 run
    dispatching exactly as before. The optional ``budget_ceilings`` block
    (R31) round-trips when present; older documents without it read as
    ``None`` (the run's ceilings then live only in the spec's ``budgets``
    block, as they always have).
    """
    backend_config = (document or {}).get("backend_config")
    if not isinstance(backend_config, dict):
        return None
    harness = str(backend_config.get("harness") or "").strip()
    if not harness:
        return None
    fallbacks = backend_config.get("harness_fallbacks")
    ceilings_raw = backend_config.get("budget_ceilings")
    ceilings = None
    if isinstance(ceilings_raw, dict):
        ceilings = BudgetCeilings(
            max_calls=_optional_int(ceilings_raw.get("max_calls")),
            max_tokens=_optional_int(ceilings_raw.get("max_tokens")),
            wallclock_s=_optional_int(ceilings_raw.get("wallclock_s")),
        )
    return HarnessSelection(
        harness=harness,
        fallbacks=tuple(str(entry) for entry in (fallbacks or []) if str(entry).strip()),
        budget_class=str(backend_config.get("budget_class") or "standard"),
        reason=str(backend_config.get("selection_reason") or "default"),
        budget_ceilings=ceilings,
    )


def _optional_int(value: object) -> int | None:
    """A documented ceiling (a real int) or ``None`` — anything else in a
    re-read document is absent, never zero (unknown ≠ unlimited-spent)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def advance_harness_fallback(
    selection: HarnessSelection,
    *,
    failed_driver: str,
    failure_kind: str,
    fallback_enabled: bool,
    candidate_exists: bool,
) -> HarnessSelection | None:
    """One dispatch-time fallback step down the frozen chain (brief §6).

    Pure: returns the next :class:`HarnessSelection` (head advanced to the
    frozen chain's next entry) or None — the caller keeps the existing
    blocked/failed semantics. Every None case is a hard invariant:

    - the switch is OFF (default — must be explicit policy),
    - the failure is not ``infrastructure``-kind (a code failure is a signal
      about the CHANGE, ADR-0008; repair-leg switching is v1.0+),
    - a candidate exists (one frozen attempt → one candidate → one producer,
      ADR-0016 — never switch mid-candidate),
    - the event names a driver other than the current head (stale),
    - the chain is exhausted (fail visibly, wait for a human).
    """
    if not fallback_enabled:
        return None
    if failure_kind != "infrastructure":
        return None
    if candidate_exists:
        return None
    if failed_driver != selection.harness:
        return None
    if not selection.fallbacks:
        return None
    return replace(
        selection,
        harness=selection.fallbacks[0],
        fallbacks=selection.fallbacks[1:],
        reason=(f"fallback: {failed_driver} infrastructure failure"),
    )
