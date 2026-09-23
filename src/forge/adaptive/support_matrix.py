"""NEXT-26/R32-12 — the support matrix from EXECUTED profile evidence.

The review's refusal, twice over: "live batch claude-code evidence does
not certify Copilot ACP, .NET system tests or all adaptive combinations"
(NEXT-26), and then "the matrix's dimensions are misnamed: provider in
the live rows is the MODEL provider route, not the source-control
platform; recipe is the credential mode, not a RuntimeRecipe" (R32-12).
A green CI run is not a support claim; a claim needs its own evidence,
of its own class, for the exact combination it names — and the
combination must be named by the dimensions that actually vary.

:func:`support_matrix` reads three evidence sources and folds them into
one matrix keyed per ``(driver, source_platform, runtime_recipe)`` —
each dimension measured by its own axis:

- ``driver`` — the harness driver id (from the capability manifest's
  ``harness/batch-X`` / ``sdk-lane/X`` rows, the shipped-driver set and
  the binary-sharing table);
- ``source_platform`` — the SOURCE-CONTROL platform whose provider
  gateway dispatches the lane (``gitlab`` / ``github`` / ``azure``, from
  :mod:`forge.gateway` — never the model route a registration used);
- ``runtime_recipe`` — the :mod:`forge.runs.execution_profile` recipe
  the shipped wiring pins for the cell (``dotnet-9`` for the .NET lane's
  digest-pinned image, ``node-22`` for the scripted GitLab CI templates,
  ``python-3-13`` for the hosted-python Actions/Azure lanes — never the
  credential mode, which stays a diagnostic note).

The three evidence sources:

- the CAPABILITY MANIFEST (:mod:`forge.capability_manifest`) — what the
  tree declares reachable, with its evidence pointers and tiers;
- the LIVE REGISTRATIONS (:mod:`forge.adaptive.drivers.live_registrations`)
  — which sdk binaries a real vendor smoke verified, against which
  binary, citing which artifact (the registration's MODEL route and
  credential mode ride the row's NOTE as diagnostics — they are not
  matrix dimensions);
- the PLATFORM GATEWAYS (:mod:`forge.gateway`) — which source platforms
  actually carry the driver, proved by each gateway's contract tests
  existing in the checkout.

The status vocabulary is closed, and each rung is exactly as strong as
its evidence class:

``tested``
    a live registration covers the driver's binary AND its evidence
    artifact exists in the checkout AND the platform gateway's contract
    tests exist — the driver leg and the platform leg both carry
    evidence, for the cell the wiring actually runs.
``supported``
    no live registration, but the capability manifest carries the driver
    at ``production_wiring`` or higher WITH a wired entry point AND an
  entry-point test pointer, and the platform gateway is contract-tested
    — wired and contract-tested, a real-provider run not recorded.
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
- a registration only promotes the CELL THE WIRING RUNS: the recipe
  axis comes from the execution profile, so a Claude smoke cannot
  certify a ``dotnet-9`` cell and a GitLab template's ``node-22`` image
  cannot pass for the Actions lane's ``python-3-13``;
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
    "SOURCE_PLATFORMS",
    "STATUS_VALUES",
    "SUPPORTED",
    "SourcePlatform",
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
    SUPPORTED: "capability-manifest row at production_wiring or higher with a "
    "wired entry point and an entry-point test pointer; the platform gateway "
    "is contract-tested; no recorded real-provider run of the combination",
    TESTED: "a live registration verified this driver's binary, its evidence "
    "artifact exists in the checkout, and the source platform's gateway "
    "contract tests exist — for the runtime recipe the wiring pins",
}


@dataclass(frozen=True)
class SourcePlatform:
    """One source-control platform axis value (R32-12).

    ``gateway`` is the production ingress entry point that dispatches
    lanes for the platform; ``evidence`` holds the gateway's contract-test
    pointers — the platform leg of a ``tested`` cell. This is the
    SOURCE-CONTROL platform, never the model provider route a driver
    registration smoked against.
    """

    platform: str
    gateway: str
    evidence: tuple[str, ...] = ()


#: The source platforms the provider gateways wire (R32-12): every
#: advertised platform gets its own column of cells, with its own
#: evidence — support is never inherited across platforms.
SOURCE_PLATFORMS: dict[str, SourcePlatform] = {
    "gitlab": SourcePlatform(
        platform="gitlab",
        gateway="forge.gateway.router -> forge.runs.service.RunService.run_command",
        evidence=("tests/test_slash_routing.py",),
    ),
    "github": SourcePlatform(
        platform="github",
        gateway="forge.gateway.github_webhook",
        evidence=("tests/test_github_webhook.py",),
    ),
    "azure": SourcePlatform(
        platform="azure",
        gateway="forge.gateway.azure_webhook",
        evidence=("tests/test_azure_webhook.py",),
    ),
}

#: The repo root the on-disk evidence-existence checks resolve against
#: (the same derivation :mod:`forge.adaptive.drivers.live_registrations`
#: uses — src/forge/adaptive/support_matrix.py → parents[3]).
_REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class SupportRow:
    """One ``(driver, source_platform, runtime_recipe)`` cell of the matrix.

    ``source_platform`` is the source-control platform whose gateway
    dispatches the lane (``""`` never happens — every row names one).
    ``runtime_recipe`` is the execution-profile recipe the shipped
    wiring pins for the cell. The registration's MODEL route and
    credential mode are diagnostics in ``note``, not dimensions.
    ``evidence`` carries the repo-relative pointers the status rests on.
    """

    driver: str
    source_platform: str
    runtime_recipe: str
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
            "source_platform": self.source_platform,
            "runtime_recipe": self.runtime_recipe,
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
        """Every row naming *driver* (a driver has one row per source
        platform it is wired on — each its own evidence)."""
        return tuple(row for row in self.rows if row.driver == driver)

    def of_cell(self, driver: str, source_platform: str, runtime_recipe: str) -> SupportRow | None:
        """The exact ``(driver, source_platform, runtime_recipe)`` cell,
        or None when the matrix holds no such row — a combination the
        wiring does not carry cannot be looked up as support."""
        for row in self.rows:
            if (
                row.driver == driver
                and row.source_platform == source_platform
                and row.runtime_recipe == runtime_recipe
            ):
                return row
        return None


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


def _wired_platforms(driver: str) -> tuple[str, ...]:
    """The source platforms the shipped wiring carries *driver* on.

    Every shipped lane has a GitLab CI template; the Actions workflow
    and the Azure pipeline carry every driver EXCEPT the .NET lane
    (whose digest-pinned image and locked build are GitLab-template
    only — ``docs/harnesses/dotnet-lane.md``).
    """
    if driver == "dotnet-lane":
        return ("gitlab",)
    return ("gitlab", "github", "azure")


def _wired_runtime_recipe(source_platform: str, driver: str) -> str:
    """The execution-profile recipe the shipped wiring pins for the cell.

    From the templates + :mod:`forge.runs.execution_profile`: the .NET
    lane's digest-pinned image is the ``dotnet-9`` recipe; the scripted
    GitLab CI templates run on the pinned ``node-22`` image; the Actions
    workflow (``setup-python`` 3.13) and the Azure hosted lanes pin
    ``python-3-13``. A recipe the vocabulary does not carry cannot be
    wired — the caller validates against ``RUNTIME_RECIPES``.
    """
    if driver == "dotnet-lane":
        return "dotnet-9"
    if source_platform == "gitlab":
        return "node-22"
    return "python-3-13"


def support_matrix(
    *,
    manifest_rows: Sequence[Any] | None = None,
    registrations: Sequence[Any] | None = None,
    driver_sdk: Mapping[str, str] | None = None,
    shipped: Collection[str] | None = None,
    source_platforms: Mapping[str, SourcePlatform] | None = None,
    driver_recipes: Mapping[tuple[str, str], str] | None = None,
    evidence_root: Path | None = None,
) -> SupportMatrix:
    """Fold the evidence sources into one :class:`SupportMatrix`.

    Parameters default to the live tree (the seeded manifest, the live
    registrations, the binary-sharing table, the shipped-driver set, the
    gateway platforms and the template-pinned recipes) and are injectable
    so tests can pin the honesty rules without touching production data:

    - *manifest_rows* — capability rows (``Capability`` objects);
    - *registrations* — ``LiveRegistration`` objects;
    - *driver_sdk* — the harness-driver → registered-sdk mapping;
    - *shipped* — the shipped driver ids (the declaration floor);
    - *source_platforms* — the platform → gateway wiring (R32-12's new
      axis; override to pin a platform's evidence in a tmp root);
    - *driver_recipes* — ``(driver, platform)`` → recipe overrides, so a
      test can ask about a cell the default wiring does not pin (the
      value must still be in the execution profile's vocabulary);
    - *evidence_root* — where evidence pointers resolve (the repo root).

    The rules, in evidence order: a registration whose evidence file is
    missing cannot claim ``tested`` (it degrades to the declaration rung
    and the absence is a problem, not a silent pass); ``tested`` needs
    the registration AND the platform gateway's contract tests, for the
    recipe the wiring pins; ``supported`` needs the manifest row at
    ``production_wiring`` or higher WITH a wired entry point AND a
    ``tests/`` pointer, on a contract-tested platform; anything the
    inputs name that nothing declares is ``unsupported``.
    """
    from forge.adaptive.drivers.live_registrations import (
        DRIVER_SDK_OF,
        LIVE_REGISTRATIONS,
    )
    from forge.capability_manifest import capabilities
    from forge.runs.execution_profile import RUNTIME_RECIPES
    from forge.runs.harness_selection import SHIPPED_DRIVERS

    rows_manifest = list(manifest_rows) if manifest_rows is not None else list(capabilities())
    regs = list(registrations) if registrations is not None else list(LIVE_REGISTRATIONS)
    sdk_of = dict(driver_sdk) if driver_sdk is not None else dict(DRIVER_SDK_OF)
    shipped_set = set(shipped) if shipped is not None else set(SHIPPED_DRIVERS)
    platforms = dict(source_platforms) if source_platforms is not None else dict(SOURCE_PLATFORMS)
    recipes = dict(driver_recipes) if driver_recipes is not None else {}
    root = evidence_root if evidence_root is not None else _REPO_ROOT

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

    # The registered binaries: sdk → the drivers the sharing table maps to
    # it, with the registration's evidence present in the checkout.
    registered: set[str] = set()
    for reg in regs:
        for driver in sorted(d for d, sdk in sdk_of.items() if sdk == reg.sdk):
            pointer = str(getattr(reg, "evidence", "") or "")
            if pointer and (root / pointer).is_file():
                registered.add(driver)
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

    def _platform_evidence_of(platform: SourcePlatform) -> tuple[str, ...]:
        """The platform's contract-test pointers that EXIST in the root —
        a gateway whose tests are absent from the checkout carries no
        platform evidence (the cells stay un-proven, never silently
        promoted)."""
        return tuple(p for p in platform.evidence if (root / p).is_file())

    def _wants_platform_evidence(platform: SourcePlatform) -> bool:
        return bool(platform.evidence) and len(_platform_evidence_of(platform)) == len(
            platform.evidence
        )

    rows: list[SupportRow] = []
    for driver in sorted(named):
        manifest_row = manifest_by_driver.get(driver)
        manifest_evidence = tuple(getattr(manifest_row, "evidence", ()) or ())
        tier = str(getattr(manifest_row, "tier", "") or "")
        entry_point = getattr(manifest_row, "entry_point", None)
        test_pointers = tuple(p for p in manifest_evidence if p.startswith("tests/"))
        provenance = _provenance_note(driver)
        for platform_name in sorted(platforms):
            if (
                platform_name not in _wired_platforms(driver)
                and (
                    driver,
                    platform_name,
                )
                not in recipes
            ):
                continue  # no shipped wiring carries this cell
            recipe = recipes.get((driver, platform_name)) or _wired_runtime_recipe(
                platform_name, driver
            )
            if recipe not in RUNTIME_RECIPES:
                problems.append(
                    f"{driver}/{platform_name}: wired runtime recipe {recipe!r} is"
                    f" not in the execution profile's vocabulary"
                    f" {tuple(sorted(RUNTIME_RECIPES))} — the cell is not"
                    " declared"
                )
                continue
            platform = platforms[platform_name]
            platform_evidence = _platform_evidence_of(platform)
            platform_wired = _wants_platform_evidence(platform)
            if driver in registered and platform_wired:
                reg = next(r for r in regs if r.sdk == sdk_of.get(driver))
                pointer = str(getattr(reg, "evidence", "") or "")
                rows.append(
                    SupportRow(
                        driver=driver,
                        source_platform=platform_name,
                        runtime_recipe=recipe,
                        status=TESTED,
                        evidence=(pointer, *platform_evidence, *manifest_evidence),
                        note=(
                            f"live smoke {getattr(reg, 'date', '')} verified the"
                            f" binary the {driver} lane installs"
                            f" ({', '.join(reg.verified_against)}) on model route"
                            f" {reg.provider_route} with credential mode"
                            f" {reg.credential_mode}; the {platform_name} gateway"
                            f" is contract-tested ({platform.gateway});{provenance}"
                        ),
                    )
                )
            elif (
                tier in ("production_wiring", "real_provider_scenario", "cross_process_recovery")
                and entry_point
                and test_pointers
                and platform_wired
            ):
                # Wired + contract-tested (the entry-point test pointer is the
                # wiring evidence class); a real-provider run of THIS surface
                # is not recorded, or it would have carried a registration.
                rows.append(
                    SupportRow(
                        driver=driver,
                        source_platform=platform_name,
                        runtime_recipe=recipe,
                        status=SUPPORTED,
                        evidence=(*platform_evidence, *manifest_evidence),
                        note=(
                            f"capability-manifest tier {tier} with a wired entry"
                            " point and an entry-point test — wired and"
                            " contract-tested, no recorded real-provider run of"
                            f" it on {platform_name}"
                        ),
                    )
                )
            elif driver in shipped_set or manifest_row is not None:
                rows.append(
                    SupportRow(
                        driver=driver,
                        source_platform=platform_name,
                        runtime_recipe=recipe,
                        status=DECLARED_ONLY,
                        evidence=(*platform_evidence, *manifest_evidence),
                        note=(
                            "shipped as a declared recipe"
                            + (f" (manifest tier {tier})" if tier else "")
                            + " — no test-class evidence"
                        ),
                    )
                )
            else:
                rows.append(
                    SupportRow(
                        driver=driver,
                        source_platform=platform_name,
                        runtime_recipe=recipe,
                        status=UNSUPPORTED,
                        evidence=(),
                        note="no shipped recipe, no manifest row, no registration covers this driver",
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
        f"forge support matrix — {len(matrix.rows)} rows over"
        " (driver, source_platform, runtime_recipe)"
        " (evidence ladder: unsupported → declared_only → supported → tested)",
        "",
    ]
    for row in matrix.rows:
        where = f"{row.driver}/{row.source_platform}"
        lines.append(
            f"  {where:<{driver_w + 20}}  {row.status:<{status_w}}  recipe: {row.runtime_recipe}"
        )
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
