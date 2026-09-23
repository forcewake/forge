"""R32-13 — qualify ONE runtime-recipe + harness combination end to end.

The review's demand: a combination is qualified when the EVIDENCE says
so, assembled by the runner itself — not when a named suite passed at
some point on a developer machine. This module binds one qualified
(recipe, harness) combination as a single record and assembles the
evidence trace that promotes it, on a CLEAN runner with no developer
environment:

- :class:`QualificationProfile` — the frozen record binding the whole
  combination: the lane code ref, the recipe id + recipe DIGEST (the
  same canonical-JSON sha256 pattern
  :attr:`forge.runs.execution_profile.ExecutionProfile.profile_digest`
  uses), the harness id, the model route, the credential mode, the test
  invocation, the bootstrap dependency closure (resolved pins), the
  harness feature support (every feature EXPLICIT — unsupported is
  stated, never inferred from an SDK name) and the qualification layer
  bindings. :attr:`QualificationProfile.qualification_digest` is the
  sha256 over the record's canonical JSON: any mutation is a different
  digest, so a pinned digest and the record it names cannot drift
  apart silently.
- the LAYER VOCABULARY — smoke → contract → integration → acceptance
  (the 2026-09-23 e2e-qualification research, topic 1): each layer is
  bound to the evidence pointers that promote it, bindings stack
  bottom-up with no gaps (fail-fast sequencing), and the acceptance
  layer is CURATED — the honeycomb rule that E2E stays ≤ 5 % of the
  suite (:func:`acceptance_within_budget`).
- :func:`verify_installed_fingerprints` — declared identities (the
  profile's closure) versus what the runner ACTUALLY installed. ANY
  divergence raises :class:`FingerprintMismatch` listing each one:
  refuse semantics, no permissive partial match.
- :func:`reconcile_reports` — the per-test-project report verdicts. A
  MISSING report is ``missing_report`` (never zero failures), a
  leftover report from another run is ``stale_report`` (each report is
  bound to the candidate identity + test bundle digest it must belong
  to), a parsed report carries its failure/success counts. The
  aggregate surfaces EVERY problem distinctly — a failing test project
  cannot disappear behind another project's passing TRX file.
- :func:`probe_egress_pair` — the egress control-probe PAIR: the
  permitted destination must SUCCEED (the producer evidence) and the
  denied destination must be blocked BY THE EXPECTED POLICY, with the
  honest three-way distinction the research demands: denied by policy
  (declaration present + denial observed), denied UNEXPECTEDLY (denial
  without the policy — something else is wrong), or policy
  DECLARED-BUT-NOT-ENFORCED (the env hook retained while the filter
  was disabled — the destination answers). This is
  :func:`forge.runs.execution_profile.verify_network_egress`'s
  three-value honesty, doubled into a pair.
- :class:`QualificationTrace` — the assembled evidence record
  (``forge.qualification.trace/1``): profile digest, fingerprint
  verdict, report reconciliation, the egress pair, the layer bindings,
  timestamps. Honest by construction: any leg unknown or partial is
  said so in :attr:`QualificationTrace.problems`, and the verdict is
  ``qualified`` ONLY when every leg carries passing, complete evidence.

Pure stdlib at module scope; frozen data throughout (a qualification
record must never mutate under the promotion that cites it).
"""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ElementTree
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from forge.adaptive.capability_profiles import CAPABILITIES
from forge.runs.spec import canonical_json_digest

__all__ = [
    "ACCEPTANCE_MAX_SHARE",
    "EGRESS_CONSISTENT",
    "EGRESS_INCONSISTENT",
    "EGRESS_NOT_PROBED",
    "EGRESS_PERMITTED_STATUS_VALUES",
    "EGRESS_DENIED_STATUS_VALUES",
    "EGRESS_STATUS_VALUES",
    "FINGERPRINT_STATUS_VALUES",
    "REPORT_STATUS_VALUES",
    "FINGERPRINT_MATCHED",
    "FINGERPRINT_MISMATCHED",
    "FINGERPRINT_NOT_CHECKED",
    "FingerprintDivergence",
    "FingerprintMismatch",
    "FingerprintMatch",
    "HARNESS_FEATURES",
    "HarnessFeatureSupport",
    "IDENTITY_SIDECAR_SUFFIX",
    "LAYER_ORDER",
    "LayerBinding",
    "ProbeDestination",
    "ProbeLeg",
    "EgressPolicy",
    "EgressProbePair",
    "QualificationLayer",
    "QualificationProfile",
    "QualificationTrace",
    "REPORTS_ALL_PASSED",
    "REPORTS_NOT_RECONCILED",
    "REPORTS_PROBLEMS",
    "REPORT_VERDICT_VALUES",
    "ExpectedReport",
    "ExpectedReports",
    "ReportReconciliation",
    "ReportVerdict",
    "TRACE_SCHEMA",
    "TRACE_VERDICT_NOT_QUALIFIED",
    "TRACE_VERDICT_QUALIFIED",
    "acceptance_within_budget",
    "assemble_qualification_trace",
    "probe_egress_pair",
    "profile_staleness",
    "reconcile_reports",
    "recipe_document_digest",
    "verify_installed_fingerprints",
]

#: The versioned discriminator every qualification trace carries. A
#: breaking change to the trace's meaning bumps the tag; pinned
#: promotions keep the version they were qualified with.
TRACE_SCHEMA = "forge.qualification.trace/1"

# -- the qualification layer vocabulary (R32-13, research topic 1) ------------
#
# The canonical layer stack the 2026-09-23 research pass converged on
# (smoke → contract → integration → acceptance): a qualification names
# WHICH layer's evidence it ran, and the promotion rules bind each
# transition to a layer — never to "a suite that passed somewhere".


class QualificationLayer(str, Enum):
    """One qualification layer — the evidence class that promotes a
    combination one step (research topic 1 §1).

    ``smoke`` — minutes-fast, per driver/recipe, runs on every change.
    ``contract`` — the driver↔forge boundary contract, per surface.
    ``integration`` — the combination against synthetic dependency
    graphs (Testcontainers-class kits, named failpoints).
    ``acceptance`` — the CURATED live-driver journeys, capped by the
    honeycomb rule (≤ :data:`ACCEPTANCE_MAX_SHARE` of the suite).
    """

    SMOKE = "smoke"
    CONTRACT = "contract"
    INTEGRATION = "integration"
    ACCEPTANCE = "acceptance"


#: The canonical stack order, bottom-up. Bindings must bind a PREFIX of
#: this order (fail-fast sequencing: no acceptance evidence without the
# layers beneath it).
LAYER_ORDER: tuple[QualificationLayer, ...] = (
    QualificationLayer.SMOKE,
    QualificationLayer.CONTRACT,
    QualificationLayer.INTEGRATION,
    QualificationLayer.ACCEPTANCE,
)

#: The honeycomb rule (research topic 1 §1): the acceptance layer is a
#: small CURATED E2E band — at most 5 % of the total suite, "one E2E
#: test per revenue-critical journey". Beyond that cap the layer stops
#: adding signal and starts adding ice-cream-cone flake.
ACCEPTANCE_MAX_SHARE = 0.05


def acceptance_within_budget(acceptance_count: int, total_suite_count: int) -> tuple[bool, str]:
    """Whether the acceptance layer is still within the curated ≤ 5 %
    budget (``(ok, reason)`` — the curator calls this BEFORE binding
    the acceptance layer; the binding itself carries evidence, not
    counts, so the budget check stays at curation time).

    Zero-total is refused: a suite with no tests has no share to
    compute, and an uncomputable budget is not a granted one.
    """
    if total_suite_count <= 0:
        return False, "total suite count must be positive — the share is uncomputable"
    if acceptance_count < 0:
        return False, "acceptance count must not be negative"
    share = acceptance_count / total_suite_count
    if share > ACCEPTANCE_MAX_SHARE:
        return False, (
            f"acceptance layer is {acceptance_count}/{total_suite_count} "
            f"({share:.1%}) — over the curated {ACCEPTANCE_MAX_SHARE:.0%} cap; "
            "curate the journeys down (the honeycomb's lean top) instead of "
            "widening the cap"
        )
    return True, (
        f"acceptance layer is {acceptance_count}/{total_suite_count} ({share:.1%}) "
        f"— within the curated {ACCEPTANCE_MAX_SHARE:.0%} cap"
    )


@dataclass(frozen=True)
class LayerBinding:
    """One qualification layer bound to the evidence that promotes it.

    ``layer`` is the :class:`QualificationLayer`; ``evidence`` holds the
    pointers that promote it (test files, artifact paths — repo-relative
    and auditable, the same pointer discipline the support matrix
    uses). A binding without evidence is a declaration, and this record
    is not that: the tuple must be non-empty.
    """

    layer: QualificationLayer
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.layer, QualificationLayer):
            raise ValueError(
                f"layer must be a QualificationLayer, got {self.layer!r}; "
                f"vocabulary is {[layer.value for layer in LAYER_ORDER]}"
            )
        if not self.evidence:
            raise ValueError(
                f"the {self.layer.value} binding carries no evidence — a layer "
                "promoted by nothing is a declaration, and this record is not one"
            )
        for pointer in self.evidence:
            if not str(pointer).strip():
                raise ValueError("evidence pointers must be non-empty")


# -- the harness feature support (EXPLICIT, never inferred) ---------------------


#: The harness features a qualification states explicitly, each mapped
#: onto the closed :data:`forge.adaptive.capability_profiles.CAPABILITIES`
#: vocabulary (steer IS live_input, restore IS checkpoint_export — the
#: interactive-planning names for the same axes). The mapping is checked
#: at import: a drift between the two vocabularies is a modelling error
#: that must fail loudly, not a silent pass-through.
HARNESS_FEATURES: tuple[str, ...] = ("interrupt", "steer", "restore")
_FEATURE_TO_CAPABILITY: dict[str, str] = {
    "interrupt": "interrupt",
    "steer": "live_input",
    "restore": "checkpoint_export",
}
for _feature, _capability in _FEATURE_TO_CAPABILITY.items():
    if _capability not in CAPABILITIES:
        raise ValueError(  # pragma: no cover — vocabulary drift guard
            f"harness feature {_feature!r} maps onto {_capability!r}, which the "
            f"capability vocabulary {CAPABILITIES} does not carry"
        )


@dataclass(frozen=True)
class HarnessFeatureSupport:
    """What the qualified harness can ACTUALLY do — every feature
    EXPLICIT, no defaults.

    ``False`` is a statement, not an absence: a checkpoint-only CLI is
    ``interrupt=False`` no matter what similarly-named methods its SDK
    ships (the X08 doctrine — support is TESTED-for, never inferred
    from a name). Construction therefore demands all three verdicts;
    there is no "unspecified" feature on a qualified record.
    """

    interrupt: bool
    steer: bool
    restore: bool

    def supports(self, feature: str) -> bool:
        """Whether THIS record states *feature* (a
        :data:`HARNESS_FEATURES` name; anything else is a modelling
        error, refused — not silently False)."""
        if feature not in HARNESS_FEATURES:
            raise ValueError(
                f"unknown harness feature {feature!r}; vocabulary is {HARNESS_FEATURES}"
            )
        return bool(getattr(self, feature))

    def to_document(self) -> dict[str, bool]:
        """All three verdicts, True AND False alike — the audit shape
        carries the unsupported features as explicitly as the supported
        ones."""
        return {feature: bool(getattr(self, feature)) for feature in HARNESS_FEATURES}

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> HarnessFeatureSupport:
        missing = [f for f in HARNESS_FEATURES if f not in document]
        if missing:
            raise ValueError(
                f"a feature-support document must state every feature explicitly; "
                f"missing {missing} — an unstated feature is never defaulted"
            )
        return cls(
            interrupt=bool(document["interrupt"]),
            steer=bool(document["steer"]),
            restore=bool(document["restore"]),
        )


# -- the qualification profile --------------------------------------------------


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


@dataclass(frozen=True)
class QualificationProfile:
    """ONE qualified (runtime recipe, harness) combination as a single
    frozen record (R32-13) — what a clean runner installs and executes.

    ``lane_code_ref`` is the source ref of the lane wiring that ran
    (commit sha or branch). ``recipe_id`` + ``recipe_digest`` pin the
    runtime recipe BOTH by name and by content (the digest follows the
    canonical-JSON sha256 pattern of
    :attr:`forge.runs.execution_profile.ExecutionProfile.profile_digest`,
    computed over the recipe's ``to_document()`` — see
    :func:`recipe_document_digest`); :func:`profile_staleness` compares
    the pair against the shipped vocabulary. ``model_route`` and
    ``credential_mode`` pin the route the qualification ran with.
    ``test_invocation`` is the argv the runner executes. The
    ``bootstrap_closure`` is the RESOLVED dependency pin set (name →
    exact version) the qualification installed — what
    :func:`verify_installed_fingerprints` checks the runner against.
    ``feature_support`` states every harness feature explicitly.
    ``layer_bindings`` bind the qualification layers this combination's
    evidence promotes.
    """

    lane_code_ref: str
    recipe_id: str
    recipe_digest: str
    harness_id: str
    model_route: str
    credential_mode: str
    test_invocation: tuple[str, ...]
    bootstrap_closure: tuple[tuple[str, str], ...]
    feature_support: HarnessFeatureSupport
    layer_bindings: tuple[LayerBinding, ...]

    def __post_init__(self) -> None:
        for name in (
            "lane_code_ref",
            "recipe_id",
            "recipe_digest",
            "harness_id",
            "model_route",
            "credential_mode",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must be non-empty — a qualified record names it")
        if not _is_sha256(self.recipe_digest):
            raise ValueError(
                f"recipe_digest must be a sha256 digest over the recipe's canonical "
                f"JSON (see recipe_document_digest), got {self.recipe_digest[:16]!r}"
            )
        if not self.test_invocation or any(not str(part).strip() for part in self.test_invocation):
            raise ValueError(
                "test_invocation is the argv the runner executes — an empty or "
                "partial argv is an unexecutable qualification"
            )
        names = [name for name, _version in self.bootstrap_closure]
        if any(not str(name).strip() for name in names):
            raise ValueError("bootstrap closure entries carry non-empty names")
        if len(names) != len(set(names)):
            raise ValueError("one pin per dependency in the bootstrap closure, no duplicates")
        if not isinstance(self.feature_support, HarnessFeatureSupport):
            raise ValueError(
                "feature_support must be a HarnessFeatureSupport — every feature "
                "stated explicitly, never inferred"
            )
        if not self.layer_bindings:
            raise ValueError(
                "a qualification binds at least one layer — a record promoting "
                "nothing qualifies nothing"
            )
        layers = tuple(binding.layer for binding in self.layer_bindings)
        if len(set(layers)) != len(layers):
            raise ValueError("one binding per layer, no duplicates")
        if layers != LAYER_ORDER[: len(layers)]:
            raise ValueError(
                f"layer bindings must stack bottom-up with no gaps ({layers!r} is "
                f"not a prefix of {[layer.value for layer in LAYER_ORDER]}) — "
                "acceptance evidence without the layers beneath it is the "
                "ice-cream cone, not a qualification"
            )

    def to_document(self) -> dict:
        """The canonical digest target (sorted-key JSON over this dict)."""
        return {
            "lane_code_ref": self.lane_code_ref,
            "recipe_id": self.recipe_id,
            "recipe_digest": self.recipe_digest,
            "harness_id": self.harness_id,
            "model_route": self.model_route,
            "credential_mode": self.credential_mode,
            "test_invocation": list(self.test_invocation),
            "bootstrap_closure": {
                name: version for name, version in sorted(self.bootstrap_closure)
            },
            "feature_support": self.feature_support.to_document(),
            "layer_bindings": [
                {"layer": binding.layer.value, "evidence": list(binding.evidence)}
                for binding in self.layer_bindings
            ],
        }

    @property
    def qualification_digest(self) -> str:
        """sha256 over the canonical JSON of the whole record — any
        mutation of any field is a different digest (the same contract
        as the execution profile's ``profile_digest``)."""
        return canonical_json_digest(self.to_document())

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> QualificationProfile:
        """Rebuild the record from its canonical document (the freeze
        round-trip: ``from_document(profile.to_document()) == profile``
        — a record that cannot survive the round-trip was never
        freezable)."""
        try:
            closure = document["bootstrap_closure"]
            layers = document["layer_bindings"]
            return cls(
                lane_code_ref=str(document["lane_code_ref"]),
                recipe_id=str(document["recipe_id"]),
                recipe_digest=str(document["recipe_digest"]),
                harness_id=str(document["harness_id"]),
                model_route=str(document["model_route"]),
                credential_mode=str(document["credential_mode"]),
                test_invocation=tuple(str(part) for part in document["test_invocation"]),
                bootstrap_closure=tuple(
                    (str(name), str(version)) for name, version in sorted(closure.items())
                ),
                feature_support=HarnessFeatureSupport.from_document(document["feature_support"]),
                layer_bindings=tuple(
                    LayerBinding(
                        layer=QualificationLayer(str(binding["layer"])),
                        evidence=tuple(str(p) for p in binding["evidence"]),
                    )
                    for binding in layers
                ),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"not a qualification profile document: {exc}") from None


def recipe_document_digest(recipe: Any) -> str:
    """The canonical-JSON sha256 over a :class:`RuntimeRecipe`'s
    ``to_document()`` — the digest a :class:`QualificationProfile`
    carries as ``recipe_digest``. The recipe's own audit shape IS the
    digest target, so a recipe whose image pin, version check, build
    tail or report convention changed is a DIFFERENT recipe as far as
    any pinned qualification is concerned."""
    return canonical_json_digest(recipe.to_document())


def profile_staleness(profile: QualificationProfile) -> tuple[str, ...]:
    """Compare the profile's pinned ids/digests against the SHIPPED
    vocabularies; each divergence is a staleness problem (a profile
    qualified against a recipe that has since changed, or a harness
    that no longer ships, must be RE-qualified — the pinned digest and
    the shipped record are not allowed to drift silently apart)."""
    from forge.runs.execution_profile import (
        HARNESS_PROFILES,
        RUNTIME_RECIPES,
    )

    problems: list[str] = []
    recipe = RUNTIME_RECIPES.get(profile.recipe_id)
    if recipe is None:
        problems.append(
            f"recipe {profile.recipe_id!r} is not in the shipped vocabulary "
            f"{tuple(sorted(RUNTIME_RECIPES))} — the qualified runtime no longer "
            "exists as shipped"
        )
    elif recipe_document_digest(recipe) != profile.recipe_digest:
        problems.append(
            f"the shipped {profile.recipe_id} recipe changed since the "
            "qualification pinned its digest — re-qualify against the current "
            "recipe record"
        )
    if profile.harness_id not in HARNESS_PROFILES:
        problems.append(
            f"harness {profile.harness_id!r} is not in the shipped vocabulary "
            f"{tuple(sorted(HARNESS_PROFILES))} — the qualified agent axis no "
            "longer exists as shipped"
        )
    return tuple(problems)


# -- installed-fingerprint verification (refuse semantics) ----------------------


#: One divergence kind per shape: the declared pin disagrees with the
#: installed version, a declared pin is MISSING from the installation,
#: or the installation carries a dependency the closure never declared.
FINGERPRINT_DIVERGENCE_KINDS = (
    "version_mismatch",
    "missing_installed",
    "undeclared_installed",
)


@dataclass(frozen=True)
class FingerprintDivergence:
    """One declared-vs-installed divergence, named for the audit."""

    kind: str
    name: str
    declared: str
    installed: str

    def __post_init__(self) -> None:
        if self.kind not in FINGERPRINT_DIVERGENCE_KINDS:
            raise ValueError(
                f"unknown divergence kind {self.kind!r}; vocabulary is "
                f"{FINGERPRINT_DIVERGENCE_KINDS}"
            )

    def __str__(self) -> str:
        if self.kind == "missing_installed":
            return f"{self.name}: declared {self.declared!r}, NOT INSTALLED"
        if self.kind == "undeclared_installed":
            return f"{self.name}: installed {self.installed!r}, NOT DECLARED"
        return f"{self.name}: declared {self.declared!r}, installed {self.installed!r}"


class FingerprintMismatch(ValueError):
    """The installed toolchain refuses the declared closure — raised by
    :func:`verify_installed_fingerprints` when ANY divergence exists.

    ``divergences`` lists each one; the refusal is total (no permissive
    partial match: a 99 %-matching installation is a mismatch, never a
    "close enough"). Carrying the divergence list on the exception lets
    a caller record the full evidence AND refuse.
    """

    def __init__(self, divergences: Sequence[FingerprintDivergence]) -> None:
        self.divergences = tuple(divergences)
        listing = "; ".join(str(divergence) for divergence in self.divergences)
        super().__init__(
            f"installed toolchain fingerprints refuse the declared closure "
            f"({len(self.divergences)} divergence(s)): {listing}"
        )


@dataclass(frozen=True)
class FingerprintMatch:
    """The verified closure — the declared identities the runner
    actually installed, exactly, with nothing extra."""

    fingerprints: tuple[tuple[str, str], ...]

    def to_document(self) -> dict:
        return {name: version for name, version in self.fingerprints}


def verify_installed_fingerprints(
    declared: Mapping[str, str], installed: Mapping[str, str]
) -> FingerprintMatch:
    """Verify the INSTALLED toolchain against the DECLARED closure.

    Exact-set semantics (the refuse doctrine): every declared
    (name → version) pin must be installed AT that version, and the
    installation must carry NOTHING the closure does not declare. Any
    divergence — version mismatch, missing pin, undeclared install —
    raises :class:`FingerprintMismatch` listing EACH one; a return
    value means the fingerprints match exactly.
    """
    divergences: list[FingerprintDivergence] = []
    for name in sorted(declared):
        if name not in installed:
            divergences.append(
                FingerprintDivergence(
                    kind="missing_installed",
                    name=name,
                    declared=str(declared[name]),
                    installed="",
                )
            )
        elif str(installed[name]) != str(declared[name]):
            divergences.append(
                FingerprintDivergence(
                    kind="version_mismatch",
                    name=name,
                    declared=str(declared[name]),
                    installed=str(installed[name]),
                )
            )
    for name in sorted(set(installed) - set(declared)):
        divergences.append(
            FingerprintDivergence(
                kind="undeclared_installed",
                name=name,
                declared="",
                installed=str(installed[name]),
            )
        )
    if divergences:
        raise FingerprintMismatch(divergences)
    return FingerprintMatch(fingerprints=tuple(sorted((n, str(v)) for n, v in declared.items())))


# -- test-report reconciliation --------------------------------------------------
#
# The quiet-corruption traps the research names (topic 1 §6): retries
# overwrite evidence, leftovers answer for missing runs, and an
# aggregate counts a green file while a failing project's report never
# existed. The reconciliation binds every expected report to the
# (candidate identity, test bundle digest) it must belong to and gives
# each one its OWN verdict — the aggregate lists every problem, so a
# failing test project cannot disappear behind another project's TRX.

#: The suffix of the identity sidecar the qualification runner writes
#: beside each report file (``<report>.identity.json`` carrying the
#: candidate id and the test bundle digest). A report without a
#: matching sidecar cannot be bound to THIS run — it is a leftover.
IDENTITY_SIDECAR_SUFFIX = ".identity.json"

#: The per-report verdict vocabulary.
REPORT_PASSED = "passed"
REPORT_FAILED = "failed"
REPORT_MISSING = "missing_report"
REPORT_STALE = "stale_report"
REPORT_UNPARSEABLE = "unparseable_report"
REPORT_VERDICT_VALUES = (
    REPORT_PASSED,
    REPORT_FAILED,
    REPORT_MISSING,
    REPORT_STALE,
    REPORT_UNPARSEABLE,
)

#: The TRX namespace (the same convention the dotnet-9 recipe's
#: ``report_prefix`` produces and tests/fixtures/dotnet-trx carries).
_TRX_NAMESPACE = "{http://microsoft.com/schemas/VisualStudio/TeamTest/2010}"

#: TRX counter attributes folded into the failure count: a test that
#: errored, timed out or was aborted is not a pass, and counting it as
#: one would be exactly the overwrite-green trap.
_TRX_FAILURE_COUNTERS = ("failed", "error", "timeout", "aborted")


@dataclass(frozen=True)
class ExpectedReport:
    """One test project's expected report file, bound to the candidate
    identity and test bundle digest it must carry.

    ``report_path`` is the file name within the report directory (the
    recipe's ``report_prefix`` + project convention, e.g.
    ``forge_Api.Tests_net9.0.trx``). ``candidate_id`` identifies the
    qualification run; ``bundle_digest`` pins the exact test bundle
    that was executed. Together they are the identity a found report
    must MATCH — anything else in the directory is a leftover.
    """

    test_project: str
    report_path: str
    candidate_id: str
    bundle_digest: str

    def __post_init__(self) -> None:
        for name in ("test_project", "report_path", "candidate_id", "bundle_digest"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must be non-empty — the report binding names it")


@dataclass(frozen=True)
class ExpectedReports:
    """The expected report set: one row per test project, no overlaps.

    ``of_project``/``of_path`` are the lookups the reconciliation and
    the tests use; a duplicate project or path at construction is a
    modelling error (two rows could then answer for one report file).
    """

    reports: tuple[ExpectedReport, ...]

    def __post_init__(self) -> None:
        if not self.reports:
            raise ValueError("a qualification expects at least one test report")
        projects = [report.test_project for report in self.reports]
        paths = [report.report_path for report in self.reports]
        if len(set(projects)) != len(projects):
            raise ValueError("one expected report per test project, no duplicates")
        if len(set(paths)) != len(paths):
            raise ValueError("one expected report per report path, no duplicates")

    def of_path(self, report_path: str) -> ExpectedReport | None:
        for report in self.reports:
            if report.report_path == report_path:
                return report
        return None


@dataclass(frozen=True)
class ReportVerdict:
    """One test project's report verdict. Counts are ``None`` unless
    the report was found, identity-bound AND parsed — a missing or
    stale report NEVER reads as zero failures."""

    test_project: str
    report_path: str
    verdict: str
    detail: str
    total: int | None = None
    executed: int | None = None
    passed: int | None = None
    failed: int | None = None

    def __post_init__(self) -> None:
        if self.verdict not in REPORT_VERDICT_VALUES:
            raise ValueError(
                f"unknown report verdict {self.verdict!r}; vocabulary is {REPORT_VERDICT_VALUES}"
            )

    def __str__(self) -> str:  # pragma: no cover — trivial rendering
        return f"[{self.verdict}] {self.test_project}: {self.detail}"


@dataclass(frozen=True)
class ReportReconciliation:
    """Every expected report's verdict, in expected order — the
    aggregate that surfaces ALL problems distinctly."""

    verdicts: tuple[ReportVerdict, ...]

    @property
    def problems(self) -> tuple[str, ...]:
        """One line per non-passing verdict — the failing project, the
        missing report AND the stale leftover all appear; a passing TRX
        file beside them cancels none of them."""
        return tuple(
            f"{verdict.test_project}: {verdict.verdict} — {verdict.detail}"
            for verdict in self.verdicts
            if verdict.verdict != REPORT_PASSED
        )

    @property
    def is_green(self) -> bool:
        """True only when every expected report was found, identity-
        bound, parsed and failure-free (and at least one was expected —
        :class:`ExpectedReports` enforces that at construction)."""
        return bool(self.verdicts) and all(
            verdict.verdict == REPORT_PASSED for verdict in self.verdicts
        )

    def to_document(self) -> dict:
        return {
            "aggregate": REPORTS_ALL_PASSED if self.is_green else REPORTS_PROBLEMS,
            "verdicts": [
                {
                    "test_project": verdict.test_project,
                    "report_path": verdict.report_path,
                    "verdict": verdict.verdict,
                    "total": verdict.total,
                    "executed": verdict.executed,
                    "passed": verdict.passed,
                    "failed": verdict.failed,
                    "detail": verdict.detail,
                }
                for verdict in self.verdicts
            ],
            "problems": list(self.problems),
        }


def _parse_trx_counters(path: Path) -> dict[str, int] | None:
    """The TRX ``ResultSummary/Counters`` as ints, or None when the
    file is not a parseable TRX with counters (unparseable IS a
    verdict, never zero failures)."""
    try:
        root = ElementTree.parse(path).getroot()
    except (ElementTree.ParseError, OSError):
        return None
    counters = root.find(f"./{_TRX_NAMESPACE}ResultSummary/{_TRX_NAMESPACE}Counters")
    if counters is None:
        return None

    def _count(name: str) -> int:
        raw = counters.get(name, "0") or "0"
        try:
            return int(float(raw))
        except ValueError:
            return 0

    values = {
        name: _count(name)
        for name in ("total", "executed", "passed", "failed", "error", "timeout", "aborted")
        + ("inconclusive",)
    }
    values["failures"] = sum(values[name] for name in _TRX_FAILURE_COUNTERS)
    return values


def _read_identity(path: Path) -> dict[str, str] | None:
    """The identity sidecar's (candidate_id, bundle_digest), or None
    when absent/unreadable (a sidecar that cannot be read cannot bind
    the report to anything)."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    return document


def reconcile_reports(expected: ExpectedReports, found_dir: Path) -> ReportReconciliation:
    """Reconcile the expected reports against one report directory.

    Per expected report, in order:

    - the report file is ABSENT → ``missing_report`` — the project ran
      no report at all, which is never zero failures;
    - the file is present but its identity sidecar (``<report>
      .identity.json``) is absent/unreadable, or names another
      candidate/bundle → ``stale_report`` — a leftover from another
      run answering in this one's place;
    - the file is present and identity-bound but is not a parseable
      TRX with counters → ``unparseable_report`` — unknown counts, and
      unknown is never green;
    - parsed → ``passed``/``failed`` with the counts (failures fold in
      error/timeout/aborted; a report that executed ZERO tests is a
      failure — it proves nothing).
    """
    verdicts: list[ReportVerdict] = []
    for report in expected.reports:
        report_path = found_dir / report.report_path
        sidecar_path = found_dir / (report.report_path + IDENTITY_SIDECAR_SUFFIX)
        if not report_path.is_file():
            verdicts.append(
                ReportVerdict(
                    test_project=report.test_project,
                    report_path=report.report_path,
                    verdict=REPORT_MISSING,
                    detail=(
                        f"no report at {report.report_path} — the test project "
                        "produced nothing this run, and a missing report is "
                        "never zero failures"
                    ),
                )
            )
            continue
        identity = _read_identity(sidecar_path)
        if identity is None:
            verdicts.append(
                ReportVerdict(
                    test_project=report.test_project,
                    report_path=report.report_path,
                    verdict=REPORT_STALE,
                    detail=(
                        f"{sidecar_path.name} is absent or unreadable — the "
                        "report cannot be bound to this candidate/bundle and is "
                        "treated as a leftover from another run"
                    ),
                )
            )
            continue
        found_candidate = str(identity.get("candidate_id") or "")
        found_bundle = str(identity.get("bundle_digest") or "")
        if found_candidate != report.candidate_id or found_bundle != report.bundle_digest:
            verdicts.append(
                ReportVerdict(
                    test_project=report.test_project,
                    report_path=report.report_path,
                    verdict=REPORT_STALE,
                    detail=(
                        f"the report belongs to candidate {found_candidate or '?'}/"
                        f"bundle {found_bundle[:12] or '?'} — this run is "
                        f"{report.candidate_id}/{report.bundle_digest[:12]}; a "
                        "leftover from another run cannot answer for this one"
                    ),
                )
            )
            continue
        counters = _parse_trx_counters(report_path)
        if counters is None:
            verdicts.append(
                ReportVerdict(
                    test_project=report.test_project,
                    report_path=report.report_path,
                    verdict=REPORT_UNPARSEABLE,
                    detail=(
                        f"{report.report_path} is not a parseable TRX with "
                        "ResultSummary counters — its counts are unknown, and "
                        "unknown is never green"
                    ),
                )
            )
            continue
        failures = counters["failures"]
        if counters["executed"] <= 0:
            verdicts.append(
                ReportVerdict(
                    test_project=report.test_project,
                    report_path=report.report_path,
                    verdict=REPORT_FAILED,
                    total=counters["total"],
                    executed=counters["executed"],
                    passed=counters["passed"],
                    failed=failures,
                    detail=(
                        "the report parsed but executed zero tests — it proves "
                        "nothing about the test project"
                    ),
                )
            )
        elif failures > 0:
            verdicts.append(
                ReportVerdict(
                    test_project=report.test_project,
                    report_path=report.report_path,
                    verdict=REPORT_FAILED,
                    total=counters["total"],
                    executed=counters["executed"],
                    passed=counters["passed"],
                    failed=failures,
                    detail=(
                        f"{failures} failing test(s) of {counters['executed']} "
                        "executed"
                        + (
                            f" ({counters['inconclusive']} inconclusive)"
                            if counters["inconclusive"]
                            else ""
                        )
                    ),
                )
            )
        else:
            verdicts.append(
                ReportVerdict(
                    test_project=report.test_project,
                    report_path=report.report_path,
                    verdict=REPORT_PASSED,
                    total=counters["total"],
                    executed=counters["executed"],
                    passed=counters["passed"],
                    failed=failures,
                    detail=f"{counters['passed']}/{counters['executed']} passed",
                )
            )
    return ReportReconciliation(verdicts=tuple(verdicts))


# -- the egress control-probe PAIR ------------------------------------------------
#
# verify_network_egress probes ONE denied leg. A qualification needs
# the PAIR: the permitted destination must SUCCEED (the producer
# evidence — the model route, the package registries) and the denied
# destination must be blocked BY THE EXPECTED POLICY. The research's
# falsification case is built in: "disable the network policy while
# retaining its env variable — the qualification trace must detect the
# difference", which is exactly policy_declared_but_not_enforced.

#: The permitted-leg statuses.
EGRESS_PERMITTED_REACHABLE = "permitted_reachable"
EGRESS_PERMITTED_BLOCKED = "permitted_blocked"
EGRESS_PERMITTED_STATUS_VALUES = (EGRESS_PERMITTED_REACHABLE, EGRESS_PERMITTED_BLOCKED)

#: The denied-leg statuses — the honest three-way distinction plus the
#: two degenerate shapes (no policy at all; a probe destination the
#: declared policy never denies, so nothing can be falsified).
EGRESS_DENIED_BY_POLICY = "denied_by_policy"
EGRESS_DENIED_UNEXPECTED = "denied_unexpected"
EGRESS_POLICY_NOT_ENFORCED = "policy_declared_but_not_enforced"
EGRESS_POLICY_ABSENT = "policy_absent"
EGRESS_PROBE_INDETERMINATE = "probe_indeterminate"
EGRESS_DENIED_STATUS_VALUES = (
    EGRESS_DENIED_BY_POLICY,
    EGRESS_DENIED_UNEXPECTED,
    EGRESS_POLICY_NOT_ENFORCED,
    EGRESS_POLICY_ABSENT,
    EGRESS_PROBE_INDETERMINATE,
)

#: The raw connection outcomes, beside the verdicts (the same pairing
#: NetworkProbeResult carries).
EGRESS_CONNECTED = "connected"
EGRESS_UNREACHABLE = "unreachable"
EGRESS_NOT_PROBED = "not_probed"

#: The default probe timeout — short by design; a qualification probe
#: must never stall the runner.
EGRESS_PROBE_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class EgressPolicy:
    """The declared egress policy the pair probes against.

    ``allowlist`` holds the fnmatch host patterns the policy permits
    (the value :data:`forge.runs.execution_profile.FORGE_EGRESS_ALLOWLIST_ENV`
    carries); ``env_hook`` is the env name the declaration rides —
    present-but-empty means deny-all-declared, ABSENT means the policy
    never reached the runtime at all. The declared patterns and the
    hook's PRESENCE are separate facts: the research case is a hook
    retained while the enforcement behind it is gone.
    """

    allowlist: tuple[str, ...]
    env_hook: str = "FORGE_EGRESS_ALLOWLIST"

    def __post_init__(self) -> None:
        if not self.allowlist:
            raise ValueError("an egress policy declares its permitted patterns")
        for pattern in self.allowlist:
            if not str(pattern).strip():
                raise ValueError("allowlist patterns must be non-empty")

    def permits(self, host: str) -> bool:
        """Whether the declared patterns grant *host* (fnmatch)."""
        from fnmatch import fnmatchcase

        return any(fnmatchcase(host, str(pattern).strip()) for pattern in self.allowlist)


@dataclass(frozen=True)
class ProbeDestination:
    """One probe destination (host, port) — dial-and-close, no payload
    bytes either way."""

    host: str
    port: int

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ValueError("a probe destination carries a non-empty host")
        if not (0 < self.port < 65536):
            raise ValueError(f"port {self.port} is outside 1..65535")

    def __str__(self) -> str:  # pragma: no cover — trivial rendering
        return f"{self.host}:{self.port}"

    @classmethod
    def of(cls, value: ProbeDestination | str) -> ProbeDestination:
        """Accept a ``"host:port"`` spelling or an existing destination."""
        if isinstance(value, ProbeDestination):
            return value
        raw = str(value or "").strip()
        host, sep, port = raw.rpartition(":")
        if not sep or not host.strip():
            raise ValueError(f"destination {value!r} is not a host:port pair")
        try:
            return cls(host=host.strip(), port=int(port))
        except ValueError:
            raise ValueError(f"destination {value!r} carries a non-integer port") from None


def _socket_connector(host: str, port: int, timeout_s: float) -> None:
    """The default probe leg: one TCP connect, closed immediately
    (the same dial :func:`verify_network_egress` uses — established
    means reachable, any OSError spelling means not)."""
    import socket

    with socket.create_connection((host, port), timeout=timeout_s):
        pass  # connected — nothing sent, immediately closed


@dataclass(frozen=True)
class ProbeLeg:
    """One leg of the pair: the honest verdict, the raw connection
    outcome beside it, and the actionable detail."""

    destination: str
    status: str
    connection_outcome: str
    detail: str

    def __post_init__(self) -> None:
        allowed = (*EGRESS_PERMITTED_STATUS_VALUES, *EGRESS_DENIED_STATUS_VALUES)
        if self.status not in allowed:
            raise ValueError(f"unknown probe status {self.status!r}; vocabulary is {allowed}")
        if self.connection_outcome not in (EGRESS_CONNECTED, EGRESS_UNREACHABLE, EGRESS_NOT_PROBED):
            raise ValueError(f"unknown connection outcome {self.connection_outcome!r}")

    def __str__(self) -> str:  # pragma: no cover — trivial rendering
        return f"[{self.status}] {self.destination}: {self.detail}"


@dataclass(frozen=True)
class EgressProbePair:
    """Both legs plus the pair-level honesty.

    ``consistent`` is True only for the one shape the qualification
    accepts: the permitted destination REACHED (and actually granted by
    the declared patterns — a reachable-but-never-granted destination
    is a mis-specified pair, not a pass) and the denied destination
    denied BY THE POLICY. Everything else — a blocked producer, an
    unexpected denial, an unenforced declaration — lands in
    :attr:`problems`.
    """

    permitted: ProbeLeg
    denied: ProbeLeg
    policy_declared: bool
    permitted_granted: bool = True

    @property
    def consistent(self) -> bool:
        return (
            self.permitted.status == EGRESS_PERMITTED_REACHABLE
            and self.denied.status == EGRESS_DENIED_BY_POLICY
            and self.permitted_granted
        )

    @property
    def problems(self) -> tuple[str, ...]:
        problems: list[str] = []
        if self.permitted.status != EGRESS_PERMITTED_REACHABLE:
            problems.append(f"permitted leg: {self.permitted}")
        if self.denied.status != EGRESS_DENIED_BY_POLICY:
            problems.append(f"denied leg: {self.denied}")
        if not self.permitted_granted:
            problems.append(
                f"the permitted destination {self.permitted.destination} is not "
                "granted by the declared policy — the profile/policy pair is "
                "mis-specified; fix the policy or the probe"
            )
        return tuple(problems)

    def to_document(self) -> dict:
        return {
            "policy_declared": self.policy_declared,
            "consistent": self.consistent,
            "permitted_granted": self.permitted_granted,
            "permitted": {
                "destination": self.permitted.destination,
                "status": self.permitted.status,
                "connection_outcome": self.permitted.connection_outcome,
                "detail": self.permitted.detail,
            },
            "denied": {
                "destination": self.denied.destination,
                "status": self.denied.status,
                "connection_outcome": self.denied.connection_outcome,
                "detail": self.denied.detail,
            },
            "problems": list(self.problems),
        }


def probe_egress_pair(
    policy: EgressPolicy,
    permitted_dest: ProbeDestination | str,
    denied_dest: ProbeDestination | str,
    *,
    env: Mapping[str, str] | None = None,
    connector: Any = None,
    timeout_s: float = EGRESS_PROBE_TIMEOUT_S,
) -> EgressProbePair:
    """Probe BOTH legs of the declared egress policy.

    The PERMITTED leg dials a destination the policy grants (the
    producer surface: the model route, the package registries) — it
    must SUCCEED; the success is the producer evidence the trace
    records. The DENIED leg dials a destination the policy denies and
    answers with the honest distinction:

    - ``denied_by_policy`` — the policy hook is present in the runtime
      env, the destination is denied by the declared patterns, and the
      connection was refused: consistent with enforcement (the same
      epistemic bound :func:`verify_network_egress` states — a refused
      probe proves the control may exist, never that it does);
    - ``denied_unexpected`` — the connection was refused but the policy
      is NOT in force (hook absent, or the destination not denied by
      the patterns): something ELSE is wrong, and the policy can claim
      nothing;
    - ``policy_declared_but_not_enforced`` — the hook is present and
      the destination denied by the patterns, and the connection
      SUCCEEDED: enforcement is missing (the research's disabled-
      filter-with-retained-env case — the one shape the probe PROVES);
    - ``policy_absent`` — no policy declaration in the runtime env and
      the destination answered: egress is uncontrolled, not enforced;
    - ``probe_indeterminate`` — the denied destination is actually
      allowlisted by the declared patterns, so the probe cannot
      falsify anything (mis-specified pair, said loudly).

    *connector* overrides both dials (``(host, port, timeout_s) -> None``,
    raising OSError when unreachable) so tests prove every verdict
    without a network.
    """
    source = os.environ if env is None else env
    declared = policy.env_hook in source
    permitted = ProbeDestination.of(permitted_dest)
    denied = ProbeDestination.of(denied_dest)
    dial = connector if connector is not None else _socket_connector

    def _outcome(destination: ProbeDestination) -> str:
        try:
            dial(destination.host, destination.port, timeout_s)
        except OSError:
            return EGRESS_UNREACHABLE
        return EGRESS_CONNECTED

    # -- the denied leg (the falsification leg) --------------------------
    denied_by_patterns = not policy.permits(denied.host)
    if not declared:
        outcome = _outcome(denied)
        if outcome == EGRESS_CONNECTED:
            denied_leg = ProbeLeg(
                destination=str(denied),
                status=EGRESS_POLICY_ABSENT,
                connection_outcome=outcome,
                detail=(
                    f"{policy.env_hook} is absent from the runtime env and the "
                    f"denied destination {denied} answered — egress is "
                    "uncontrolled, not enforced; stage the policy hook before "
                    "probing"
                ),
            )
        else:
            denied_leg = ProbeLeg(
                destination=str(denied),
                status=EGRESS_DENIED_UNEXPECTED,
                connection_outcome=outcome,
                detail=(
                    f"{policy.env_hook} is absent from the runtime env, yet the "
                    f"connection to {denied} was refused — the denial is real "
                    "but this policy cannot claim it; something else blocks "
                    "egress and that something is unqualified"
                ),
            )
    elif not denied_by_patterns:
        outcome = _outcome(denied)
        hint = (
            "the destination answered, which proves nothing about denial"
            if outcome == EGRESS_CONNECTED
            else "the destination was refused even though the policy permits it — "
            "legitimate egress may be broken (see the permitted leg)"
        )
        denied_leg = ProbeLeg(
            destination=str(denied),
            status=EGRESS_PROBE_INDETERMINATE,
            connection_outcome=outcome,
            detail=(
                f"the declared policy ({list(policy.allowlist)}) ALLOWLISTS the "
                f"probe destination {denied} — a permitted destination cannot "
                f"falsify enforcement; {hint}"
            ),
        )
    else:
        outcome = _outcome(denied)
        if outcome == EGRESS_CONNECTED:
            denied_leg = ProbeLeg(
                destination=str(denied),
                status=EGRESS_POLICY_NOT_ENFORCED,
                connection_outcome=outcome,
                detail=(
                    f"the declared policy denies {denied} ({policy.env_hook} is "
                    "present) yet the connection SUCCEEDED — the declaration is "
                    "not enforced (a disabled filter with its env variable "
                    "retained looks exactly like this); enforcement is missing"
                ),
            )
        else:
            denied_leg = ProbeLeg(
                destination=str(denied),
                status=EGRESS_DENIED_BY_POLICY,
                connection_outcome=outcome,
                detail=(
                    f"the policy hook is present, {denied} is denied by the "
                    "declared patterns and the connection was refused — "
                    "consistent with enforcement; a refused probe proves the "
                    "control may exist, never that it does"
                ),
            )

    # -- the permitted leg (the producer evidence) -----------------------
    permitted_outcome = _outcome(permitted)
    granted = policy.permits(permitted.host)
    if permitted_outcome == EGRESS_CONNECTED:
        permitted_leg = ProbeLeg(
            destination=str(permitted),
            status=EGRESS_PERMITTED_REACHABLE,
            connection_outcome=permitted_outcome,
            detail=(
                f"the permitted destination {permitted} was reached — the "
                "producer surface (model route/package registries) is usable "
                "under the declared policy"
            ),
        )
    else:
        permitted_leg = ProbeLeg(
            destination=str(permitted),
            status=EGRESS_PERMITTED_BLOCKED,
            connection_outcome=permitted_outcome,
            detail=(
                f"the permitted destination {permitted} is "
                + (
                    "granted by the declared policy yet unreachable — the "
                    "policy breaks legitimate producer traffic"
                    if granted
                    else "NOT granted by the declared policy — the producer "
                    "surface was never permitted to begin with"
                )
            ),
        )

    # A permitted destination the policy never grants is a mis-specified
    # pair even when it happens to answer — the pair says so structurally
    # (consistent stays False), never absorbs it.
    return EgressProbePair(
        permitted=permitted_leg,
        denied=denied_leg,
        policy_declared=declared,
        permitted_granted=granted,
    )


# -- the assembled evidence trace -------------------------------------------------


#: The fingerprint leg statuses.
FINGERPRINT_MATCHED = "match"
FINGERPRINT_MISMATCHED = "mismatch"
FINGERPRINT_NOT_CHECKED = "not_checked"

#: The report leg statuses.
REPORTS_ALL_PASSED = "all_passed"
REPORTS_PROBLEMS = "problems"
REPORTS_NOT_RECONCILED = "not_reconciled"

#: The egress leg statuses.
EGRESS_CONSISTENT = "consistent"
EGRESS_INCONSISTENT = "inconsistent"
EGRESS_NOT_PROBED = "not_probed"

#: The closed leg-status vocabularies (trace construction validates
#: against these — a status outside them is a modelling error).
FINGERPRINT_STATUS_VALUES = (FINGERPRINT_MATCHED, FINGERPRINT_MISMATCHED, FINGERPRINT_NOT_CHECKED)
REPORT_STATUS_VALUES = (REPORTS_ALL_PASSED, REPORTS_PROBLEMS, REPORTS_NOT_RECONCILED)
EGRESS_STATUS_VALUES = (EGRESS_CONSISTENT, EGRESS_INCONSISTENT, EGRESS_NOT_PROBED)

#: The trace verdicts — ``qualified`` ONLY with every leg complete and
#: passing; an unknown or partial leg keeps the trace ``not_qualified``
#: and says why.
TRACE_VERDICT_QUALIFIED = "qualified"
TRACE_VERDICT_NOT_QUALIFIED = "not_qualified"


@dataclass(frozen=True)
class QualificationTrace:
    """The assembled evidence record for ONE qualification run
    (``forge.qualification.trace/1``).

    The legs are carried as their honest statuses plus the evidence
    details (fingerprint divergences, per-report verdicts, both egress
    legs): a leg that was never run keeps its ``not_*`` status, lands
    in :attr:`problems`, and holds the verdict at ``not_qualified`` —
    a partial trace is an honest trace, never a green one.
    """

    schema: str
    profile_digest: str
    fingerprint_status: str = FINGERPRINT_NOT_CHECKED
    fingerprint_divergences: tuple[FingerprintDivergence, ...] = ()
    report_status: str = REPORTS_NOT_RECONCILED
    report_verdicts: tuple[ReportVerdict, ...] = ()
    report_problems: tuple[str, ...] = ()
    egress_status: str = EGRESS_NOT_PROBED
    egress_permitted_status: str = ""
    egress_denied_status: str = ""
    egress_problems: tuple[str, ...] = ()
    layer_bindings: tuple[LayerBinding, ...] = ()
    started_at: str = ""
    finished_at: str = ""

    def __post_init__(self) -> None:
        if self.schema != TRACE_SCHEMA:
            raise ValueError(f"schema must be {TRACE_SCHEMA!r}, got {self.schema!r}")
        if self.fingerprint_status not in FINGERPRINT_STATUS_VALUES:
            raise ValueError(
                f"unknown fingerprint status {self.fingerprint_status!r}; "
                f"vocabulary is {FINGERPRINT_STATUS_VALUES}"
            )
        if self.report_status not in REPORT_STATUS_VALUES:
            raise ValueError(
                f"unknown report status {self.report_status!r}; "
                f"vocabulary is {REPORT_STATUS_VALUES}"
            )
        if self.egress_status not in EGRESS_STATUS_VALUES:
            raise ValueError(
                f"unknown egress status {self.egress_status!r}; "
                f"vocabulary is {EGRESS_STATUS_VALUES}"
            )

    @property
    def problems(self) -> tuple[str, ...]:
        """EVERY problem, distinctly — the failing project beside the
        missing report beside the stale leftover beside the unenforced
        policy, plus one honesty line per leg that never ran."""
        problems: list[str] = []
        if self.fingerprint_status == FINGERPRINT_MISMATCHED:
            problems.extend(f"fingerprint: {d}" for d in self.fingerprint_divergences)
        elif self.fingerprint_status == FINGERPRINT_NOT_CHECKED:
            problems.append(
                "fingerprint leg not checked — the trace is partial, and a "
                "partial trace is never green"
            )
        if self.report_status == REPORTS_PROBLEMS:
            problems.extend(f"reports: {problem}" for problem in self.report_problems)
        elif self.report_status == REPORTS_NOT_RECONCILED:
            problems.append(
                "report leg not reconciled — the trace is partial, and a "
                "partial trace is never green"
            )
        if self.egress_status == EGRESS_INCONSISTENT:
            problems.extend(f"egress: {problem}" for problem in self.egress_problems)
        elif self.egress_status == EGRESS_NOT_PROBED:
            problems.append(
                "egress leg not probed — the trace is partial, and a partial trace is never green"
            )
        if not self.started_at or not self.finished_at:
            problems.append(
                "trace timestamps incomplete — the run window is unknown, and "
                "an unknown window is not evidence"
            )
        return tuple(problems)

    @property
    def verdict(self) -> str:
        """``qualified`` iff :attr:`problems` is empty — every leg
        complete, passing and timestamped."""
        return TRACE_VERDICT_QUALIFIED if not self.problems else TRACE_VERDICT_NOT_QUALIFIED

    def to_document(self) -> dict:
        return {
            "schema": self.schema,
            "verdict": self.verdict,
            "profile_digest": self.profile_digest,
            "fingerprint": {
                "status": self.fingerprint_status,
                "divergences": [
                    {
                        "kind": d.kind,
                        "name": d.name,
                        "declared": d.declared,
                        "installed": d.installed,
                    }
                    for d in self.fingerprint_divergences
                ],
            },
            "reports": {
                "status": self.report_status,
                "verdicts": [
                    {
                        "test_project": v.test_project,
                        "report_path": v.report_path,
                        "verdict": v.verdict,
                        "total": v.total,
                        "executed": v.executed,
                        "passed": v.passed,
                        "failed": v.failed,
                        "detail": v.detail,
                    }
                    for v in self.report_verdicts
                ],
                "problems": list(self.report_problems),
            },
            "egress": {
                "status": self.egress_status,
                "permitted_status": self.egress_permitted_status,
                "denied_status": self.egress_denied_status,
                "problems": list(self.egress_problems),
            },
            "layer_bindings": [
                {"layer": binding.layer.value, "evidence": list(binding.evidence)}
                for binding in self.layer_bindings
            ],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "problems": list(self.problems),
        }


def assemble_qualification_trace(
    profile: QualificationProfile,
    *,
    fingerprint: FingerprintMatch | FingerprintMismatch | None = None,
    reports: ReportReconciliation | None = None,
    egress: EgressProbePair | None = None,
    started_at: str = "",
    finished_at: str = "",
) -> QualificationTrace:
    """Assemble one qualification run's evidence trace.

    Each leg accepts its typed evidence record — a caught
    :class:`FingerprintMismatch` included, so a refused fingerprint can
    still be RECORDED (the refusal is evidence) — or ``None`` for a leg
    that never ran, which the trace carries as its honest ``not_*``
    status and a problem line. The verdict is derived, never passed in:
    ``qualified`` only when every leg is complete and passing.
    """
    if fingerprint is None:
        fingerprint_status = FINGERPRINT_NOT_CHECKED
        divergences: tuple[FingerprintDivergence, ...] = ()
    elif isinstance(fingerprint, FingerprintMismatch):
        fingerprint_status = FINGERPRINT_MISMATCHED
        divergences = fingerprint.divergences
    else:
        fingerprint_status = FINGERPRINT_MATCHED
        divergences = ()

    if reports is None:
        report_status = REPORTS_NOT_RECONCILED
        report_verdicts: tuple[ReportVerdict, ...] = ()
        report_problems: tuple[str, ...] = ()
    else:
        report_status = REPORTS_ALL_PASSED if reports.is_green else REPORTS_PROBLEMS
        report_verdicts = reports.verdicts
        report_problems = reports.problems

    if egress is None:
        egress_status = EGRESS_NOT_PROBED
        egress_permitted = ""
        egress_denied = ""
        egress_problems: tuple[str, ...] = ()
    else:
        egress_status = EGRESS_CONSISTENT if egress.consistent else EGRESS_INCONSISTENT
        egress_permitted = egress.permitted.status
        egress_denied = egress.denied.status
        egress_problems = egress.problems

    return QualificationTrace(
        schema=TRACE_SCHEMA,
        profile_digest=profile.qualification_digest,
        fingerprint_status=fingerprint_status,
        fingerprint_divergences=divergences,
        report_status=report_status,
        report_verdicts=report_verdicts,
        report_problems=report_problems,
        egress_status=egress_status,
        egress_permitted_status=egress_permitted,
        egress_denied_status=egress_denied,
        egress_problems=egress_problems,
        layer_bindings=profile.layer_bindings,
        started_at=started_at,
        finished_at=finished_at,
    )
