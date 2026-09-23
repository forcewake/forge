"""NEXT-26 — the release support matrix from EXECUTED profile evidence.

The review's refusal: "live batch claude-code evidence does not certify
Copilot ACP, .NET system tests or all adaptive combinations. Completion
must be stated per capability/profile and evidence class." A green CI
run is not a support claim; a claim needs its own evidence, of its own
class, for the exact combination it names.

:func:`support_matrix` reads three evidence sources and folds them into
one matrix keyed per ``(driver, provider, recipe)``:

- the CAPABILITY MANIFEST (:mod:`forge.capability_manifest`) — what the
  tree declares reachable, with its evidence pointers and tiers;
- the LIVE REGISTRATIONS (:mod:`forge.adaptive.drivers.live_registrations`)
  — which (sdk, provider_route, credential_mode) combinations a real
  vendor smoke verified, against which binary, citing which artifact;
- the PROVENANCE REPORT (:func:`provenance_report`) — the three-way
  reconciliation of what the dispatch DECLARED, what the registration
  VERIFIED and what the runner INSTALLED (the matrix consumes its
  unregistered/unknown-present verdicts as notes, never as support).

The status vocabulary is closed, and each rung is exactly as strong as
its evidence class:

``tested``
    a live registration covers the driver's binary on this provider
    route AND its evidence artifact exists in the checkout (the same
    existence rule :func:`seed_live_matrix` enforces — a stripped
    release cannot claim what it cannot show).
``supported``
    no live registration, but the capability manifest carries the
    driver at ``production_wiring`` or higher WITH an entry-point test
    pointer — wired and contract-tested, a real-provider run not
    recorded.
``declared_only``
    shipped as a template/recipe, but no test-class evidence: intent,
    not proof.
``unsupported``
    nothing declares it — a driver id the inputs name that no shipped
    recipe, manifest row or registration covers.

Honesty rules, made structural:

- the matrix never claims more than the evidence: a registration whose
    evidence file is MISSING degrades (to the declaration rung, with a
    loud problem) instead of claiming ``tested``;
- a registration maps to drivers only through the explicit
    ``DRIVER_SDK_OF`` binary-sharing table — no support is inherited
    from a sibling sdk that did not verify the same binary;
- every row carries its evidence pointers, so "why is this tested?" is
    always one lookup away.

``forge doctor --support-matrix`` prints the matrix;
``doctor --capabilities --json`` carries it as the additive
``support_matrix`` field.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "DECLARED_ONLY",
    "STATUS_VALUES",
    "SUPPORTED",
    "SupportMatrix",
    "SupportRow",
    "TESTED",
    "UNSUPPORTED",
    "format_support_matrix",
    "support_matrix",
]

#: The closed status vocabulary, weakest → strongest evidence class.
UNSUPPORTED = "unsupported"
DECLARED_ONLY = "declared_only"
SUPPORTED = "supported"
TESTED = "tested"
STATUS_VALUES: tuple[str, ...] = (UNSUPPORTED, DECLARED_ONLY, SUPPORTED, TESTED)

STATUS_LEGEND: dict[str, str] = {
    UNSUPPORTED: "nothing declares this combination — no shipped recipe, "
    "no manifest row, no registration",
    DECLARED_ONLY: "shipped as a declared recipe/template, but no test-class "
    "evidence — intent, not proof",
    SUPPORTED: "capability-manifest row at production_wiring or higher with "
    "an entry-point test pointer; no recorded real-provider run of it",
    TESTED: "a live registration verified this driver's binary on this "
    "provider route, and its evidence artifact exists in the checkout",
}

#: The repo root the on-disk evidence-existence checks resolve against
#: (the same derivation :mod:`forge.adaptive.drivers.live_registrations`
#: uses — src/forge/adaptive/support_matrix.py → parents[3]).
_REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class SupportRow:
    """One (driver, provider, recipe) cell of the support matrix.

    ``provider`` is the provider route a registration verified (``""``
    when only ambient-env declarations exist — the recipe declares no
    route). ``recipe`` is the runtime recipe the row is about: the
    registration's credential mode for ``tested`` rows, the template's
    required-credential recipe otherwise. ``evidence`` carries the
    repo-relative pointers the status rests on.
    """

    driver: str
    provider: str
    recipe: str
    status: str
    evidence: tuple[str, ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        if self.status not in STATUS_VALUES:
            raise ValueError(
                f"unknown support status {self.status!r}; vocabulary is {STATUS_VALUES}"
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "driver": self.driver,
            "provider": self.provider,
            "recipe": self.recipe,
            "status": self.status,
            "evidence": list(self.evidence),
            "note": self.note,
        }


@dataclass(frozen=True)
class SupportMatrix:
    """The folded support matrix plus its honesty problems."""

    rows: tuple[SupportRow, ...] = ()
    problems: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "legend": dict(STATUS_LEGEND),
            "rows": [row.to_json() for row in self.rows],
            "problems": list(self.problems),
        }

    def of_driver(self, driver: str) -> tuple[SupportRow, ...]:
        """Every row naming *driver* (a driver may have several provider
        routes — each its own evidence)."""
        return tuple(row for row in self.rows if row.driver == driver)


def _manifest_driver_of(row_name: str) -> str:
    """The harness driver id a capability-manifest row names, or ''.

    Only the two driver-carrying row families map: ``harness/batch-X``
    → the batch driver ``X``; ``sdk-lane/X`` → the interactive lane
    ``X-sdk-lane``. Operator-command and adaptive-substrate rows name no
    driver and return '' (they are not recipe rows).
    """
    if row_name.startswith("harness/batch-"):
        return row_name[len("harness/batch-") :]
    if row_name.startswith("sdk-lane/"):
        return f"{row_name[len('sdk-lane/') :]}-sdk-lane"
    return ""


def _declared_recipe(driver: str, credential_vars: Mapping[str, Sequence[str]]) -> str:
    """The template's required-credential recipe for *driver*."""
    required = credential_vars.get(driver) or ()
    return "+".join(required) if required else "no-required-credentials"


def support_matrix(
    *,
    manifest_rows: Sequence[Any] | None = None,
    registrations: Sequence[Any] | None = None,
    driver_sdk: Mapping[str, str] | None = None,
    shipped: Collection[str] | None = None,
    evidence_root: Path | None = None,
) -> SupportMatrix:
    """Fold the three evidence sources into one :class:`SupportMatrix`.

    Parameters default to the live tree (the seeded manifest, the live
    registrations, the binary-sharing table and the shipped-driver set)
    and are injectable so tests can pin the honesty rules without
    touching production data:

    - *manifest_rows* — capability rows (``Capability`` objects);
    - *registrations* — ``LiveRegistration`` objects;
    - *driver_sdk* — the harness-driver → registered-sdk mapping;
    - *shipped* — the shipped driver ids (the declaration floor);
    - *evidence_root* — where evidence pointers resolve (the repo root).

    The rules, in evidence order: a registration whose evidence file is
    missing cannot claim ``tested`` (it degrades to the declaration
    rung and the absence is a problem, not a silent pass); a driver is
    ``supported`` only when the manifest carries it at
    ``production_wiring`` or higher WITH a ``tests/`` pointer; anything
    the inputs name that nothing declares is ``unsupported``.
    """
    from forge.adaptive.drivers.live_registrations import (
        DRIVER_SDK_OF,
        LIVE_REGISTRATIONS,
    )
    from forge.capability_manifest import capabilities
    from forge.runs.harness_selection import DRIVER_CREDENTIAL_VARS, SHIPPED_DRIVERS

    rows_manifest = list(manifest_rows) if manifest_rows is not None else list(capabilities())
    regs = list(registrations) if registrations is not None else list(LIVE_REGISTRATIONS)
    sdk_of = dict(driver_sdk) if driver_sdk is not None else dict(DRIVER_SDK_OF)
    shipped_set = set(shipped) if shipped is not None else set(SHIPPED_DRIVERS)
    root = evidence_root if evidence_root is not None else _REPO_ROOT
    credential_vars = DRIVER_CREDENTIAL_VARS

    problems: list[str] = []
    # driver → the manifest row naming it (first wins; one row per driver
    # in the seeded tree — a second row for the same driver is a problem).
    manifest_by_driver: dict[str, Any] = {}
    for row in rows_manifest:
        driver = _manifest_driver_of(str(getattr(row, "name", "")))
        if not driver:
            continue
        if driver in manifest_by_driver:
            problems.append(
                f"{driver}: two capability-manifest rows name one driver — the"
                " support row cannot say which evidence is its"
            )
            continue
        manifest_by_driver[driver] = row

    # The declaration vocabulary: everything the inputs NAME as a driver.
    named = set(shipped_set) | set(manifest_by_driver) | set(sdk_of)
    # Providers named by the registrations, per driver, via the sdk table.
    drivers_of_sdk: dict[str, list[str]] = {}
    for driver, sdk in sdk_of.items():
        drivers_of_sdk.setdefault(sdk, []).append(driver)

    rows: list[SupportRow] = []
    claimed_tested: set[str] = set()

    for reg in regs:
        for driver in sorted(drivers_of_sdk.get(reg.sdk, ())):
            manifest_row = manifest_by_driver.get(driver)
            manifest_evidence = tuple(getattr(manifest_row, "evidence", ()) or ())
            provenance = _provenance_note(driver)
            pointer = str(getattr(reg, "evidence", "") or "")
            if pointer and (root / pointer).is_file():
                rows.append(
                    SupportRow(
                        driver=driver,
                        provider=str(reg.provider_route),
                        recipe=str(reg.credential_mode),
                        status=TESTED,
                        evidence=(pointer, *manifest_evidence),
                        note=(
                            f"live smoke {getattr(reg, 'date', '')} verified the"
                            f" binary the {driver} lane installs ({', '.join(reg.verified_against)})"
                            f" on this provider route;{provenance}"
                        ),
                    )
                )
                claimed_tested.add(driver)
            else:
                # The one honesty teeth of this module: a registration
                # whose artifact is not in the checkout claims NOTHING —
                # the row falls to the declaration rung below and the
                # absence is said loudly.
                problems.append(
                    f"{driver}: live registration ({reg.sdk}/{reg.provider_route})"
                    f" cites {pointer or '(no evidence pointer)'}, which is not"
                    " in this checkout — the combination is NOT tested here"
                )

    for driver in sorted(named - claimed_tested):
        manifest_row = manifest_by_driver.get(driver)
        manifest_evidence = tuple(getattr(manifest_row, "evidence", ()) or ())
        tier = str(getattr(manifest_row, "tier", "") or "")
        test_pointers = tuple(p for p in manifest_evidence if p.startswith("tests/"))
        if (
            tier in ("production_wiring", "real_provider_scenario", "cross_process_recovery")
            and test_pointers
        ):
            # Wired + contract-tested (the entry-point test pointer is the
            # wiring evidence class); a real-provider run of THIS surface
            # is not recorded, or it would have carried a registration.
            status = SUPPORTED
            note = (
                f"capability-manifest tier {tier} with an entry-point test —"
                " wired and contract-tested, no recorded real-provider run of it"
            )
        elif driver in shipped_set or manifest_row is not None:
            status = DECLARED_ONLY
            note = (
                "shipped as a declared recipe"
                + (f" (manifest tier {tier})" if tier else "")
                + " — no test-class evidence"
            )
        else:
            status = UNSUPPORTED
            note = "no shipped recipe, no manifest row, no registration covers this driver"
        rows.append(
            SupportRow(
                driver=driver,
                provider="",
                recipe=_declared_recipe(driver, credential_vars),
                status=status,
                evidence=manifest_evidence,
                note=note,
            )
        )

    return SupportMatrix(rows=tuple(rows), problems=tuple(problems))


def _provenance_note(driver: str) -> str:
    """The provenance-report reconciliation for *driver*'s row note.

    The report compares the DECLARED pin against the registration's
    recorded binary; with no runner fingerprint at hand it answers
    ``unknown_present``/``unregistered`` — honest PRESENT-tense
    uncertainty the row surfaces as a note (never as support).
    """
    from forge.adaptive.drivers.live_registrations import provenance_report

    report = provenance_report(driver)
    status = str(report.get("registration_status") or "")
    if status == "match":
        return " the runner fingerprint matched the registration"
    if status == "unknown_present":
        return " the installed binary was not reported — present-tense unverified"
    if status == "unregistered":
        return " no registration covers this driver's sdk"
    return ""


def format_support_matrix(matrix: SupportMatrix) -> str:
    """The human-readable matrix ``forge doctor --support-matrix`` prints."""
    driver_w = max((len(row.driver) for row in matrix.rows), default=6)
    status_w = max((len(row.status) for row in matrix.rows), default=6)
    lines = [
        f"forge support matrix — {len(matrix.rows)} rows "
        f"(evidence ladder: unsupported → declared_only → supported → tested)",
        "",
    ]
    for row in matrix.rows:
        where = f"{row.driver}/{row.provider}" if row.provider else row.driver
        lines.append(f"  {where:<{driver_w + 20}}  {row.status:<{status_w}}  recipe: {row.recipe}")
        if row.evidence:
            lines.append(f"  {'':<{driver_w + 20}}  evidence: {', '.join(row.evidence)}")
        if row.note:
            lines.append(f"  {'':<{driver_w + 20}}  {row.note}")
    by_status = {
        status: sum(1 for row in matrix.rows if row.status == status) for status in STATUS_VALUES
    }
    lines.append("")
    lines.append("  " + ", ".join(f"{count} {status}" for status, count in by_status.items()))
    if matrix.problems:
        lines.append("")
        for problem in matrix.problems:
            lines.append(f"  PROBLEM: {problem}")
    return "\n".join(lines)
