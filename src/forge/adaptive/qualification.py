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

R36-10 (#269) extends the same doctrine from the wheel FILE to the
runtime dependency CLOSURE and to the security boundary OUTSIDE the
model:

- :class:`LaneClosureManifest` — the hash-locked wheelhouse contract
  (``forge.lane.closure/1``): every artifact name + sha256, the forge
  wheel identity, the canonical resolution command.
  :attr:`LaneClosureManifest.closure_digest` is the sha256 over the
  manifest's canonical JSON — the closure's IDENTITY, so a pinned
  digest and the closure it names cannot drift apart silently.
  :func:`verify_closure_dir` verifies a wheelhouse against its
  manifest: every artifact present and hash-matching, NO undeclared
  files (a poisoned cache fixture is a typed refusal).
- :func:`verify_artifact_supply_chain` — the promotion-record binding:
  the forge wheel inside the closure must be the wheel the release
  record vouches for; an image-only record refuses wheel claims
  HONESTLY (``not_built``), a different digest refuses.
- :func:`verify_credential_isolation` /
  :class:`CredentialScopeReceipt` — WHICH credential names the lane
  stages (names only, never values) plus the assertion that publisher
  credentials are absent from the agent workspace; a forbidden name
  among the staged ones is a :class:`CredentialIsolationViolation`.
- :func:`resolve_closure_install_route` /
  :func:`enforce_closure_install` — the OPTIONAL ``closure-wheel``
  install route (additive to the R36-07 template ladder): a staged
  wheelhouse installs with ``--no-index --find-links``, so a
  target-repo lockfile cannot replace the collector runtime, and the
  identity gate compares the INSTALLED set against the closure
  manifest.
- :func:`runtime_boundary_report` — the one document that says what
  the execution environment IS: closure digest binding, installed
  fingerprint verdict, both egress legs, credential isolation — the
  ``runtime.installed_fingerprint`` evidence, honestly partial when a
  leg never ran.

Pure stdlib at module scope; frozen data throughout (a qualification
record must never mutate under the promotion that cites it).
"""

from __future__ import annotations

import hashlib
import json
import os
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterable, Mapping, Sequence
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
    "REPORT_INVENTORY_SCHEMA",
    "ReportReconciliation",
    "ReportVerdict",
    "TRACE_SCHEMA",
    "TRACE_VERDICT_NOT_QUALIFIED",
    "TRACE_VERDICT_QUALIFIED",
    "acceptance_within_budget",
    "assemble_qualification_trace",
    "freeze_report_inventory",
    "probe_egress_pair",
    "profile_staleness",
    "reconcile_reports",
    "recipe_document_digest",
    "verify_installed_fingerprints",
    "BOUNDARY_OUTSIDE",
    "BOUNDARY_WITHIN",
    "BOUNDARY_VERDICT_VALUES",
    "CLOSURE_ARTIFACT_SUFFIX",
    "CLOSURE_BOUND",
    "CLOSURE_BINDING_STATUS_VALUES",
    "CLOSURE_FORGE_WHEEL_SOURCES",
    "CLOSURE_INSTALL_ROUTE",
    "CLOSURE_MANIFEST_FILENAME",
    "CLOSURE_MISMATCH",
    "CLOSURE_UNPINNED",
    "CLOSURE_MANIFEST_SCHEMA",
    "CREDENTIAL_RECEIPT_SCHEMA",
    "FORGE_LANE_CLOSURE_SHA256_ENV",
    "FORGE_LANE_CLOSURE_DIR_ENV",
    "CREDENTIAL_ISOLATED",
    "CREDENTIAL_ISOLATION_VIOLATED",
    "CREDENTIAL_NOT_RECEIVED",
    "ClosureArtifact",
    "ClosureInstallRoute",
    "ClosureVerificationError",
    "CredentialIsolationViolation",
    "CredentialScopeReceipt",
    "LaneClosureManifest",
    "LaneInstallRouteConflict",
    "RUNTIME_BOUNDARY_SCHEMA",
    "RuntimeBoundaryReport",
    "SUPPLY_CHAIN_BOUND",
    "SUPPLY_CHAIN_REFUSAL_REASONS",
    "SupplyChainBinding",
    "SupplyChainVerificationError",
    "closure_digest_of_document",
    "closure_install_argv",
    "closure_pin_of_wheel_name",
    "enforce_closure_install",
    "hash_file_sha256",
    "read_closure_manifest_file",
    "resolve_closure_install_route",
    "runtime_boundary_report",
    "verify_artifact_supply_chain",
    "verify_closure_dir",
    "verify_credential_isolation",
    "write_closure_manifest_file",
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
    evidence promotes. ``dependency_closure_digest`` (R36-10, optional
    — empty on pre-R36-10 records) pins the hash-locked wheelhouse the
    qualification installed FROM: the :attr:`LaneClosureManifest
    .closure_digest` of the lane closure. A profile that pins it
    demands the ``closure-wheel`` install route (``--no-index
    --find-links``); a profile that leaves it empty is wheel-pinned,
    not closure-pinned, and the boundary report says so honestly.

    .. versionchanged:: R36-10
       ``dependency_closure_digest`` added (additive; ``""`` keeps the
       pre-R36-10 meaning exactly).
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
    dependency_closure_digest: str = ""

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
        if self.dependency_closure_digest and not _is_sha256(self.dependency_closure_digest):
            raise ValueError(
                "dependency_closure_digest must be the sha256 closure digest of a "
                "LaneClosureManifest (see build_lane_closure.py), or empty for a "
                "wheel-pinned profile — never a partial or foreign digest"
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
            "dependency_closure_digest": self.dependency_closure_digest,
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
                dependency_closure_digest=str(document.get("dependency_closure_digest") or ""),
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


#: The versioned discriminator of a frozen report inventory (R36-14):
#: the expected-report set recorded on the run AT DISPATCH, together
#: with the work-contract digest it was frozen under. A breaking change
#: to the inventory's meaning bumps the tag.
REPORT_INVENTORY_SCHEMA = "forge.verification.report-inventory/1"


def freeze_report_inventory(expected: ExpectedReports, *, contract_digest: str) -> dict:
    """Freeze the expected-report inventory WITH the work contract.

    R36-14: the report set a verified verdict must show is decided at
    DISPATCH time — from the qualification's :class:`ExpectedReports`
    machinery and the digest of the work contract (the executable
    spec) it ships with — and recorded as one document on the run. At
    VERDICT time the observed reports reconcile against THIS frozen
    document, never against a live recomputation: a post-hoc recipe
    edit cannot shrink the expected set under a verdict, and a missing
    report / skipped required check / report from an older attempt can
    never produce ``verified_ready``.

    The document carries its own ``inventory_digest`` (canonical-JSON
    sha256 over schema + contract digest + rows) so a tampered row is
    detectable against the digest that was recorded with it.
    """
    rows = [
        {
            "test_project": report.test_project,
            "report_path": report.report_path,
            "candidate_id": report.candidate_id,
            "bundle_digest": report.bundle_digest,
        }
        for report in expected.reports
    ]
    inventory_digest = canonical_json_digest(
        {
            "schema": REPORT_INVENTORY_SCHEMA,
            "contract_digest": str(contract_digest or ""),
            "reports": rows,
        }
    )
    return {
        "schema": REPORT_INVENTORY_SCHEMA,
        "contract_digest": str(contract_digest or ""),
        "reports": rows,
        "inventory_digest": inventory_digest,
    }


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


# -- R36-10: the hash-locked lane closure (#269) ---------------------------------
#
# The Forge WHEEL is hash-pinned (the R36-07 ladder), but pip still
# resolves the wheel's RUNTIME dependencies at install time — a
# reproducible-FILE guarantee, not a reproducible-ENVIRONMENT one. The
# closure pins the whole wheelhouse: every artifact the lane runtime
# consists of, by name and sha256, in ONE manifest whose canonical-JSON
# digest IS the closure identity. The builder is
# scripts/build_lane_closure.py (uv export from the frozen lock → pip
# download with --require-hashes); this module owns the CONTRACT so the
# runtime can verify a staged wheelhouse without importing a script.

#: The versioned discriminator every lane-closure manifest carries. A
#: breaking change to the manifest's meaning bumps the tag; pinned
#: closures keep the version they were built with.
CLOSURE_MANIFEST_SCHEMA = "forge.lane.closure/1"

#: The manifest's file name inside the wheelhouse directory — the one
#: file beside the artifacts themselves.
CLOSURE_MANIFEST_FILENAME = "closure-manifest.json"

#: The closure is a WHEELhouse: every artifact is a wheel. (The builder
#: downloads with ``--only-binary :all:``, so an sdist in the directory
#: is a foreign artifact, not a closure member.)
CLOSURE_ARTIFACT_SUFFIX = ".whl"

#: Where the forge wheel inside the closure came from — the manifest
#: states it, never infers it. ``local-uv-build`` is the development
#: shape (a moving source ref, honestly less qualified, exactly like
#: the ladder's dev-source route); ``pinned-url`` is an explicitly
#: pinned artifact URL; ``promotion-record`` is the wheel the archived
#: release record vouches for (the authoritative route).
CLOSURE_FORGE_WHEEL_SOURCES = (
    "local-uv-build",
    "pinned-url",
    "promotion-record",
)


def closure_pin_of_wheel_name(name: str) -> tuple[str, str]:
    """The ``(name, version)`` pin a wheel FILE name carries.

    Wheel file names are ``name-version[-build]-python-abi-platform.whl``
    with dashes inside *name* escaped to underscores, so the first two
    ``-``-separated segments ARE the pin (PEP 427/440). Anything that
    does not parse as at least ``name-version`` is refused — a closure
    member that cannot be named cannot be pinned, and unpinned members
    are what this whole section exists to refuse.
    """
    stem = (
        str(name)[: -len(CLOSURE_ARTIFACT_SUFFIX)]
        if str(name).endswith(CLOSURE_ARTIFACT_SUFFIX)
        else str(name)
    )
    parts = stem.split("-")
    if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
        raise ValueError(
            f"artifact name {name!r} does not carry a name-version wheel pin — "
            "a closure member that cannot be pinned is not a closure member"
        )
    return parts[0].replace("_", "-").lower(), parts[1]


def hash_file_sha256(path: Path) -> str:
    """The sha256 of a file's bytes, chunked (closure artifacts are tens
    of megabytes; the digest must not slurp them)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ClosureArtifact:
    """One wheelhouse member: file name + sha256 (the pin pair)."""

    name: str
    sha256: str

    def __post_init__(self) -> None:
        if not self.name.endswith(CLOSURE_ARTIFACT_SUFFIX):
            raise ValueError(
                f"closure artifact {self.name!r} is not a wheel — the closure is a "
                f"wheelhouse ({CLOSURE_ARTIFACT_SUFFIX} members only)"
            )
        if not _is_sha256(self.sha256):
            raise ValueError(
                f"closure artifact {self.name!r} carries a non-sha256 digest — an "
                "unhashable member is an unverifiable member"
            )

    @property
    def pin(self) -> tuple[str, str]:
        """The ``(name, version)`` pin the artifact's file name states."""
        return closure_pin_of_wheel_name(self.name)


@dataclass(frozen=True)
class LaneClosureManifest:
    """The hash-locked wheelhouse contract (``forge.lane.closure/1``).

    ``forge_wheel``/``forge_version``/``forge_source`` name the forge
    wheel inside the closure and where it came from.
    ``resolution_command`` is the CANONICAL command pair that produced
    the dependency set (the frozen-lock export plus the hash-checked
    download — a logical command, no absolute paths, so the digest is
    reproducible wherever the wheelhouse is built).
    ``artifacts`` lists EVERY wheel in the wheelhouse, forge included,
    sorted by name. :attr:`closure_digest` — the sha256 over the
    canonical JSON of :meth:`to_document` — is the closure IDENTITY:
    the digest a :class:`QualificationProfile` pins as
    ``dependency_closure_digest`` and the lane install route demands.
    """

    forge_wheel: ClosureArtifact
    forge_version: str
    forge_source: str
    resolution_command: str
    artifacts: tuple[ClosureArtifact, ...]

    def __post_init__(self) -> None:
        if self.forge_source not in CLOSURE_FORGE_WHEEL_SOURCES:
            raise ValueError(
                f"unknown forge wheel source {self.forge_source!r}; vocabulary is "
                f"{CLOSURE_FORGE_WHEEL_SOURCES}"
            )
        if not self.forge_version.strip():
            raise ValueError("the manifest names the forge wheel's version")
        if not str(self.resolution_command).strip():
            raise ValueError(
                "the manifest records the resolution command that produced the "
                "closure — an unrecorded resolution is an unauditable one"
            )
        if not self.artifacts:
            raise ValueError("a closure carries at least the forge wheel")
        names = [artifact.name for artifact in self.artifacts]
        if len(names) != len(set(names)):
            raise ValueError("one pin per closure artifact, no duplicate names")
        if sorted(names) != names:
            raise ValueError(
                "closure artifacts are sorted by name — the manifest is a digest "
                "target, and order is not allowed to be a digest axis"
            )
        if self.forge_wheel.name not in names:
            raise ValueError(
                f"the declared forge wheel {self.forge_wheel.name!r} is not among "
                "the closure artifacts — the runtime being closed over must be a "
                "member of its own closure"
            )
        if closure_pin_of_wheel_name(self.forge_wheel.name)[1] != self.forge_version:
            raise ValueError(
                f"the declared forge version {self.forge_version!r} disagrees with "
                f"the wheel name {self.forge_wheel.name!r}"
            )

    @property
    def pins(self) -> tuple[tuple[str, str], ...]:
        """Every artifact's ``(name, version)`` pin — the declared set
        :func:`verify_installed_fingerprints` checks an installation
        against (the exact-set contract, closure edition)."""
        return tuple(artifact.pin for artifact in self.artifacts)

    def to_document(self) -> dict:
        """The canonical digest target (sorted-key JSON over this dict,
        WITHOUT the digest — :meth:`closure_digest` adds it over the
        result)."""
        return {
            "schema": CLOSURE_MANIFEST_SCHEMA,
            "forge": {
                "wheel": {"name": self.forge_wheel.name, "sha256": self.forge_wheel.sha256},
                "version": self.forge_version,
                "source": self.forge_source,
            },
            "resolution": {"command": self.resolution_command},
            "artifacts": [
                {"name": artifact.name, "sha256": artifact.sha256} for artifact in self.artifacts
            ],
        }

    @property
    def closure_digest(self) -> str:
        """sha256 over the manifest's canonical JSON — the closure
        identity. Deterministic over identical closures; ANY change to
        any artifact, pin or the resolution command is a different
        digest, so a pinned digest and the closure it names cannot drift
        apart silently."""
        return closure_digest_of_document(self.to_document())

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> LaneClosureManifest:
        """Rebuild the manifest from its canonical document (the freeze
        round-trip; a document with the wrong schema tag is refused —
        it is not this contract)."""
        try:
            if document.get("schema") != CLOSURE_MANIFEST_SCHEMA:
                raise ValueError(
                    f"schema must be {CLOSURE_MANIFEST_SCHEMA!r}, got {document.get('schema')!r}"
                )
            forge = document["forge"]
            wheel = forge["wheel"]
            artifacts = document["artifacts"]
            if not isinstance(artifacts, list) or not artifacts:
                raise ValueError("artifacts must be a non-empty list")
            parsed = [
                ClosureArtifact(name=str(a["name"]), sha256=str(a["sha256"])) for a in artifacts
            ]
            manifest = cls(
                forge_wheel=ClosureArtifact(name=str(wheel["name"]), sha256=str(wheel["sha256"])),
                forge_version=str(forge["version"]),
                forge_source=str(forge["source"]),
                resolution_command=str(document["resolution"]["command"]),
                artifacts=tuple(sorted(parsed, key=lambda a: a.name)),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"not a lane closure manifest document: {exc}") from None
        if tuple(a.name for a in parsed) != tuple(a.name for a in manifest.artifacts):
            raise ValueError(
                "closure artifacts must arrive sorted by name — the document is a "
                "digest target and the order is part of the bytes"
            )
        return manifest


def closure_digest_of_document(document: Mapping[str, Any]) -> str:
    """The closure digest of a manifest document: sha256 over its
    canonical (sorted-key) JSON — the same pattern
    :attr:`QualificationProfile.qualification_digest` uses. The stored
    ``closure_digest`` key, when present, is EXCLUDED: a digest never
    covers itself."""
    body = {key: value for key, value in document.items() if key != "closure_digest"}
    return canonical_json_digest(dict(body))


def read_closure_manifest_file(path: Path) -> tuple[LaneClosureManifest, str]:
    """Read a ``closure-manifest.json`` from disk.

    Returns ``(manifest, stored_digest)`` — the rebuilt manifest and the
    digest the file CLAIMS for itself. Malformed JSON, a non-dict
    document or a non-sha256 stored digest raises :class:`ValueError`:
    the caller (the verify route) converts each into its typed refusal.
    """
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("a closure manifest document is a JSON object")
    stored = str(document.get("closure_digest") or "")
    if stored and not _is_sha256(stored):
        raise ValueError(
            f"the stored closure_digest {stored[:16]!r} is not a sha256 — the "
            "manifest cannot vouch for itself"
        )
    return LaneClosureManifest.from_document(document), stored


def write_closure_manifest_file(path: Path, manifest: LaneClosureManifest) -> str:
    """Write the manifest (with its self-declared ``closure_digest``)
    to *path* as sorted-key JSON. Returns the digest."""
    document = dict(manifest.to_document())
    digest = manifest.closure_digest
    document["closure_digest"] = digest
    Path(path).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return digest


class ClosureVerificationError(ValueError):
    """A wheelhouse refuses its own manifest — raised by
    :func:`verify_closure_dir` (and the closure install enforcement)
    when the directory and the manifest disagree.

    ``problems`` lists each one: a MISSING artifact, a TAMPERED one
    (hash mismatch), an UNDECLARED file (the poisoned-cache fixture —
    a foreign wheel planted beside the closure), or a manifest that
    cannot vouch for itself (absent, unparseable, digest mismatch).
    The refusal is total and PRE-EXECUTION: nothing from the directory
    is installed or imported once it fires."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        listing = "; ".join(self.problems)
        super().__init__(
            f"the lane closure refuses verification ({len(self.problems)} problem(s)): {listing}"
        )


def verify_closure_dir(closure_dir: Path) -> LaneClosureManifest:
    """Verify a staged wheelhouse against its own manifest.

    Exact-set semantics (the refuse doctrine, wheelhouse edition):

    - the manifest file is present and parses, its schema tag is the
      closure contract, and its stored ``closure_digest`` reproduces
      from the manifest body (a tampered manifest is detectable against
      the digest recorded beside it);
    - EVERY declared artifact is present with the exact sha256;
    - the directory carries NOTHING else besides the manifest and the
      declared artifacts — an undeclared file is a poisoned cache, and
      a poisoned cache is a refusal, not a warning.

    Any problem raises :class:`ClosureVerificationError` listing each
    one; a return value means the wheelhouse IS the closure its digest
    names. Pure filesystem + hashlib — no network, no imports from the
    directory (refusal happens before execution).
    """
    closure_dir = Path(closure_dir)
    problems: list[str] = []
    manifest_path = closure_dir / CLOSURE_MANIFEST_FILENAME
    manifest: LaneClosureManifest | None = None
    stored_digest = ""
    if not manifest_path.is_file():
        problems.append(
            f"{CLOSURE_MANIFEST_FILENAME} is absent from {closure_dir} — a "
            "wheelhouse without its manifest is a pile of wheels, not a closure"
        )
    else:
        try:
            manifest, stored_digest = read_closure_manifest_file(manifest_path)
        except (OSError, ValueError) as exc:
            problems.append(f"{CLOSURE_MANIFEST_FILENAME} is unreadable: {exc}")
            manifest = None
        if manifest is not None:
            recomputed = closure_digest_of_document(manifest.to_document())
            if stored_digest and stored_digest != recomputed:
                problems.append(
                    f"the manifest's stored closure_digest {stored_digest[:12]} does "
                    f"not reproduce from its own body ({recomputed[:12]}) — the "
                    "manifest was tampered with or is corrupt"
                )
    present = {entry.name for entry in closure_dir.iterdir()} if closure_dir.is_dir() else set()
    if manifest is not None:
        for artifact in manifest.artifacts:
            path = closure_dir / artifact.name
            if not path.is_file():
                problems.append(f"missing artifact: {artifact.name} is not in the closure")
                continue
            actual = hash_file_sha256(path)
            if actual != artifact.sha256:
                problems.append(
                    f"tampered artifact: {artifact.name} hashes {actual[:12]}, the "
                    f"manifest pins {artifact.sha256[:12]} — the bytes are not the "
                    "qualified ones"
                )
        undeclared = sorted(
            present - {CLOSURE_MANIFEST_FILENAME} - {a.name for a in manifest.artifacts}
        )
        for name in undeclared:
            problems.append(
                f"undeclared file: {name} is in the wheelhouse but not in the "
                "manifest — a poisoned cache, not a closure member"
            )
    if problems:
        raise ClosureVerificationError(problems)
    assert manifest is not None  # no problems ⇒ the manifest parsed
    return manifest


# -- the promotion-record supply-chain binding -----------------------------------


#: The supply-chain verdicts: the closure's forge wheel IS the wheel
#: the release record vouches for (``bound``), or the check refused.
SUPPLY_CHAIN_BOUND = "bound"

#: Why a supply-chain check refuses — a closed vocabulary so the
#: refusal is classifiable, not free text. ``not_built`` is the honest
#: image-only-record refusal: the record cannot vouch for a wheel it
#: never built, and pretending otherwise is fabrication.
SUPPLY_CHAIN_REFUSAL_REASONS = (
    "not_built",
    "digest_mismatch",
    "identity_mismatch",
    "unreadable_record",
)


class SupplyChainVerificationError(ValueError):
    """The closure's forge wheel is not the artifact the promotion
    record vouches for — refused BEFORE execution (the manifest hash
    check already refuses tampered bytes; this composes the RECORD
    binding: the right bytes from the wrong release are still wrong).

    ``reason`` is one of :data:`SUPPLY_CHAIN_REFUSAL_REASONS`."""

    def __init__(self, reason: str, detail: str) -> None:
        if reason not in SUPPLY_CHAIN_REFUSAL_REASONS:
            raise ValueError(
                f"unknown supply-chain refusal reason {reason!r}; vocabulary is "
                f"{SUPPLY_CHAIN_REFUSAL_REASONS}"
            )
        self.reason = reason
        self.detail = detail
        super().__init__(f"supply-chain verification refused ({reason}): {detail}")


@dataclass(frozen=True)
class SupplyChainBinding:
    """The green outcome: the closure's forge wheel bound to the
    promotion record that vouches for it."""

    record_version: str
    record_wheel_sha256: str
    manifest_wheel_sha256: str
    verdict: str = SUPPLY_CHAIN_BOUND

    def to_document(self) -> dict:
        return {
            "verdict": self.verdict,
            "record_version": self.record_version,
            "record_wheel_sha256": self.record_wheel_sha256,
            "manifest_wheel_sha256": self.manifest_wheel_sha256,
        }


def _release_wheel_identity(release_record: Any) -> tuple[str, str | None, str | None]:
    """``(version, wheel_name, wheel_sha256)`` from a release record —
    the archived promotion.json document shape (a Mapping) or the
    :class:`forge.release_promotion.PromotionRecord` object shape."""
    if isinstance(release_record, Mapping):
        version = str(release_record.get("version") or "")
        sha = release_record.get("wheel_sha256")
        wheel = release_record.get("wheel")
        name = None
        if isinstance(wheel, Mapping) and isinstance(wheel.get("wheel"), Mapping):
            name = wheel["wheel"].get("name")
            sha = sha or wheel["wheel"].get("sha256")
        return version, (str(name) if name else None), (str(sha) if sha else None)
    version = str(getattr(release_record, "version", "") or "")
    sha = getattr(release_record, "wheel_sha256", None)
    wheel = getattr(getattr(release_record, "wheel", None), "wheel", None)
    name = getattr(wheel, "name", None) if wheel is not None else None
    return version, (str(name) if name else None), (str(sha) if sha else None)


def verify_artifact_supply_chain(
    closure_manifest: LaneClosureManifest, release_record: Any
) -> SupplyChainBinding:
    """Bind the closure's forge wheel to the promotion record.

    The wheel inside the closure must be the wheel the record
    vouches for — by sha256 (the authority) and, when the record names
    it, by file name. Refusals (all :class:`SupplyChainVerificationError`,
    all pre-execution):

    - ``not_built`` — the record built no wheel (an image-only
      release). HONEST refusal: the record cannot vouch for a wheel
      claim, so none is verified — never a fabricated green;
    - ``digest_mismatch`` — the closure carries different bytes than
      the record pinned (the right name, the wrong release — or a
      wheel from nowhere the record ever saw);
    - ``identity_mismatch`` — the digests agree but the file identity
      (name) does not;
    - ``unreadable_record`` — the record carries no version at all.
    """
    version, name, sha = _release_wheel_identity(release_record)
    if not version.strip():
        raise SupplyChainVerificationError(
            "unreadable_record",
            "the release record carries no version — it is not a promotion record "
            "and can vouch for nothing",
        )
    if not sha:
        raise SupplyChainVerificationError(
            "not_built",
            f"the promotion record for v{version} built no wheel (image-only "
            "release) — it cannot vouch for the wheel the closure carries; build "
            "and record a wheel release before claiming a wheel closure",
        )
    if closure_manifest.forge_wheel.sha256 != sha:
        raise SupplyChainVerificationError(
            "digest_mismatch",
            f"the closure's forge wheel {closure_manifest.forge_wheel.name} hashes "
            f"{closure_manifest.forge_wheel.sha256[:12]}, but the v{version} "
            f"promotion record pins {sha[:12]} — a wheel the release never "
            "qualified is in the lane runtime",
        )
    if name and name != closure_manifest.forge_wheel.name:
        raise SupplyChainVerificationError(
            "identity_mismatch",
            f"the promotion record names wheel {name!r}, the closure carries "
            f"{closure_manifest.forge_wheel.name!r} — same digest, different "
            "identity is a modelling error, not a pass",
        )
    return SupplyChainBinding(
        record_version=version,
        record_wheel_sha256=sha,
        manifest_wheel_sha256=closure_manifest.forge_wheel.sha256,
    )


# -- credential scope receipts (names only, never values) ------------------------


#: The versioned discriminator of a frozen credential scope receipt.
CREDENTIAL_RECEIPT_SCHEMA = "forge.qualification.credential-scope/1"

#: The receipt verdicts: every forbidden name absent (``isolated``) or
#: the receipt was never constructible (``violated`` — the violation
#: carries the evidence instead); ``not_received`` is the honest leg
#: status when no receipt was produced at all.
CREDENTIAL_ISOLATED = "isolated"
CREDENTIAL_ISOLATION_VIOLATED = "violated"
CREDENTIAL_NOT_RECEIVED = "not_received"


class CredentialIsolationViolation(ValueError):
    """A forbidden credential name is staged into the lane — raised by
    :func:`verify_credential_isolation`.

    The message carries NAMES ONLY, never values: a refusal that
    quoted a secret to explain itself would leak the thing it exists
    to keep out of the workspace."""

    def __init__(self, leaked: Sequence[str]) -> None:
        self.leaked = tuple(sorted(leaked))
        listing = ", ".join(self.leaked)
        super().__init__(
            f"credential isolation violated: {len(self.leaked)} forbidden name(s) "
            f"staged into the lane ({listing}) — names only, never values; the "
            "lane must not receive publisher credentials"
        )


@dataclass(frozen=True)
class CredentialScopeReceipt:
    """WHAT the lane stages, by name only: the credential names the
    lane's environment carries, the names forbidden from it, and the
    isolated verdict. Constructed via :func:`verify_credential_isolation`
    (the only route to a receipt — a self-declared "isolated" receipt
    is exactly the fabrication this record refuses)."""

    staged_names: tuple[str, ...]
    forbidden_names: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.forbidden_names:
            raise ValueError(
                "a scope receipt asserts an isolation boundary — an empty "
                "forbidden set asserts nothing and proves nothing"
            )
        if len(set(self.staged_names)) != len(self.staged_names) or len(
            set(self.forbidden_names)
        ) != len(self.forbidden_names):
            raise ValueError("credential names appear once per receipt, no duplicates")

    @property
    def verdict(self) -> str:
        """``isolated`` — the only verdict a constructible receipt can
        carry (a violation raises instead of constructing one)."""
        return CREDENTIAL_ISOLATED

    def to_document(self) -> dict:
        return {
            "schema": CREDENTIAL_RECEIPT_SCHEMA,
            "verdict": self.verdict,
            "staged_names": list(self.staged_names),
            "forbidden_names": list(self.forbidden_names),
        }


def verify_credential_isolation(
    staged_names: Iterable[str], forbidden_names: Iterable[str]
) -> CredentialScopeReceipt:
    """Verify that NO forbidden credential name is among the staged
    ones.

    *staged_names* are the credential (env) names the lane's process
    environment actually carries; *forbidden_names* are the publisher /
    control-plane credentials that must NEVER reach an agent workspace.
    Pure name-set arithmetic — values never enter this function, so
    they can never leave it through a log, an exception or a receipt.
    Any intersection raises :class:`CredentialIsolationViolation`
    naming each leaked NAME; a return value is the freezable receipt.
    """
    staged = sorted({str(name).strip() for name in staged_names if str(name).strip()})
    forbidden = sorted({str(name).strip() for name in forbidden_names if str(name).strip()})
    if not forbidden:
        raise ValueError(
            "an isolation assertion with no forbidden names proves nothing — name "
            "the credentials that must stay out of the agent workspace"
        )
    leaked = sorted(set(staged) & set(forbidden))
    if leaked:
        raise CredentialIsolationViolation(leaked)
    return CredentialScopeReceipt(staged_names=tuple(staged), forbidden_names=tuple(forbidden))


# -- the optional closure-wheel install route (additive to the R36-07 ladder) -----


#: The env pair the closure route rides (mirrored in lockstep with the
#: template's R36-07 ladder — see docs/operations/lane-closure.md).
#: ``FORGE_LANE_CLOSURE_SHA256`` is the closure digest the staged
#: wheelhouse must reproduce; ``FORGE_LANE_CLOSURE_DIR`` names the
#: wheelhouse directory. Set-but-empty behaves as unset (the R36-07
#: normalization — an emptied Actions variable selects no route).
FORGE_LANE_CLOSURE_SHA256_ENV = "FORGE_LANE_CLOSURE_SHA256"
FORGE_LANE_CLOSURE_DIR_ENV = "FORGE_LANE_CLOSURE_DIR"

#: The route name this section adds to the install ladder. It is the
#: MOST qualified wheel route: the promoted wheel plus its whole
#: dependency closure, hash-locked in one manifest.
CLOSURE_INSTALL_ROUTE = "closure-wheel"

#: The route inputs the closure pin conflicts with — every OTHER
#: explicit source in the R36-07 ladder.
_LANE_DEV_FLAG_ENV = "FORGE_LANE_DEV_SOURCE_INSTALL"
_LANE_WHEEL_ENV = "FORGE_LANE_WHEEL"
_LANE_REF_ENV = "FORGE_LANE_REF"


class LaneInstallRouteConflict(ValueError):
    """The closure pin was declared beside another explicit install
    route — refused BEFORE any download or install (the R36-07
    doctrine: a declared override is applied or rejected, never
    silently ignored)."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"contradictory lane install routes: {detail}")


@dataclass(frozen=True)
class ClosureInstallRoute:
    """The resolved install route for the closure question.

    ``selected`` False means the closure inputs are absent/unset-shaped
    and the ladder proceeds exactly as before (this route is OPTIONAL
    and additive — nothing about the existing routes changes). True
    means the lane installs from the staged wheelhouse with
    :attr:`install_argv` — ``--no-index`` so pip can never consult a
    registry, and the target repository's own lockfile cannot replace
    the collector runtime: the ONLY forge pip can see is the hashed
    wheel inside the verified closure."""

    selected: bool
    mode: str
    closure_dir: str
    closure_digest: str
    install_argv: tuple[str, ...]

    @property
    def receipt_route(self) -> str:
        """The route name a lane install receipt records."""
        return CLOSURE_INSTALL_ROUTE if self.selected else ""


def closure_install_argv(closure_dir: str, forge_version: str) -> tuple[str, ...]:
    """The pip argv the closure route installs with: ``--no-index`` +
    ``--find-links`` over the wheelhouse — no registry, no resolution,
    the manifest's hashed artifacts or nothing."""
    return (
        "pip",
        "install",
        "--no-index",
        "--find-links",
        str(closure_dir),
        f"forge=={forge_version}",
    )


def resolve_closure_install_route(
    env: Mapping[str, str], *, forge_version: str
) -> ClosureInstallRoute:
    """Resolve the OPTIONAL closure-wheel route from the lane env.

    Selected when :data:`FORGE_LANE_CLOSURE_SHA256_ENV` is set
    non-empty (and names the wheelhouse via
    :data:`FORGE_LANE_CLOSURE_DIR_ENV`); the digest must be a sha256.
    It CONFLICTS with every other explicit route input — the dev source
    flag, an explicit wheel pin, an explicit ref — and each conflict
    raises :class:`LaneInstallRouteConflict` naming both variables,
    before anything downloads or installs. Unselected is the honest
    default: the ladder's own routes proceed unchanged."""
    digest = str(env.get(FORGE_LANE_CLOSURE_SHA256_ENV) or "").strip()
    if not digest:
        return ClosureInstallRoute(
            selected=False, mode="", closure_dir="", closure_digest="", install_argv=()
        )
    if not _is_sha256(digest):
        raise ValueError(
            f"{FORGE_LANE_CLOSURE_SHA256_ENV} must be a 64-hex sha256 closure digest "
            f"(see build_lane_closure.py), got {digest[:16]!r}"
        )
    closure_dir = str(env.get(FORGE_LANE_CLOSURE_DIR_ENV) or "").strip()
    if not closure_dir:
        raise ValueError(
            f"{FORGE_LANE_CLOSURE_SHA256_ENV} is set but {FORGE_LANE_CLOSURE_DIR_ENV} "
            "does not name the wheelhouse directory — the closure route has nothing "
            "to install from"
        )
    explicit: list[tuple[str, str]] = []
    dev_flag = str(env.get(_LANE_DEV_FLAG_ENV) or "").strip()
    if dev_flag and dev_flag not in ("false", "0"):
        explicit.append((_LANE_DEV_FLAG_ENV, dev_flag))
    wheel = str(env.get(_LANE_WHEEL_ENV) or "").strip()
    if wheel:
        explicit.append((_LANE_WHEEL_ENV, wheel))
    ref = str(env.get(_LANE_REF_ENV) or "").strip()
    if ref:
        explicit.append((_LANE_REF_ENV, ref))
    if explicit:
        listing = ", ".join(f"{name}={value!r}" for name, value in explicit)
        raise LaneInstallRouteConflict(
            f"{FORGE_LANE_CLOSURE_SHA256_ENV}={digest[:12]!r} beside {listing} — "
            "the closure route IS the wheel route (the promoted wheel ships inside "
            "the closure); pick one explicit source, never silently ignore one"
        )
    if not str(forge_version).strip():
        raise ValueError("the closure route needs the forge version to pin the install")
    return ClosureInstallRoute(
        selected=True,
        mode=CLOSURE_INSTALL_ROUTE,
        closure_dir=closure_dir,
        closure_digest=digest,
        install_argv=closure_install_argv(closure_dir, str(forge_version)),
    )


def enforce_closure_install(
    expected_digest: str,
    closure_manifest: LaneClosureManifest,
    installed: Mapping[str, str],
) -> FingerprintMatch:
    """The closure edition of the identity gate: the manifest the lane
    verified must be the closure the profile PINNED, and the set that
    landed must be the manifest's exact pin set.

    1. ``closure_manifest.closure_digest`` must equal *expected_digest*
       (the profile's ``dependency_closure_digest`` / the route's staged
       pin) — else :class:`ClosureVerificationError`: a verified
       wheelhouse that is not the pinned closure is a different
       runtime.
    2. :func:`verify_installed_fingerprints` over the manifest's pins
       vs *installed* (name → version, e.g. from
       ``importlib.metadata``) — exact-set semantics, divergences
       listed, nothing partial. Under ``--no-index --find-links`` a
       target-repo lockfile cannot replace the collector runtime:
       pip never sees an index, and any drift that STILL lands is
       refused here, before the first model call.
    """
    if not _is_sha256(str(expected_digest)):
        raise ValueError(
            "the expected closure digest must be a sha256 — an unpinnable closure "
            "is not an enforceable one"
        )
    if closure_manifest.closure_digest != expected_digest:
        raise ClosureVerificationError(
            [
                f"the verified closure {closure_manifest.closure_digest[:12]} is not "
                f"the pinned closure {expected_digest[:12]} — a different runtime "
                "than the one the profile qualified"
            ]
        )
    return verify_installed_fingerprints(dict(closure_manifest.pins), installed)


# -- the composite runtime boundary report ---------------------------------------


#: The versioned discriminator of the runtime boundary report.
RUNTIME_BOUNDARY_SCHEMA = "forge.qualification.boundary/1"

#: The report verdicts: every leg complete and within its boundary
#: (``within_boundary``), or anything less (``outside_boundary`` — the
#: honest catch-all: a missing leg never reads as within).
BOUNDARY_WITHIN = "within_boundary"
BOUNDARY_OUTSIDE = "outside_boundary"
BOUNDARY_VERDICT_VALUES = (BOUNDARY_WITHIN, BOUNDARY_OUTSIDE)

#: How the profile binds to the closure manifest: the profile pins the
#: manifest's digest (``bound``), pins none — wheel-pinned, not
#: closure-pinned (``unpinned``), or pins a different one
#: (``mismatch``).
CLOSURE_BOUND = "bound"
CLOSURE_UNPINNED = "unpinned"
CLOSURE_MISMATCH = "mismatch"
CLOSURE_BINDING_STATUS_VALUES = (CLOSURE_BOUND, CLOSURE_UNPINNED, CLOSURE_MISMATCH)


@dataclass(frozen=True)
class RuntimeBoundaryReport:
    """ONE document stating what the execution environment IS: the
    closure identity it installed from, the fingerprints that landed,
    both egress legs, and the credential isolation — the
    ``runtime.installed_fingerprint`` evidence (R36-10's
    observability: ``runtime.installed_fingerprint``,
    ``security.egress_probe_results``, ``credential.staged_scope_receipt``).

    Honest by construction, the trace's discipline: a leg that never
    ran keeps its ``not_*`` status, lands in :attr:`problems`, and
    holds the verdict at ``outside_boundary``. A partial boundary
    report is an honest report, never a green one.
    """

    schema: str
    profile_digest: str
    closure_binding: str
    dependency_closure_digest: str
    fingerprint_status: str
    fingerprint_divergences: tuple[FingerprintDivergence, ...] = ()
    egress_status: str = EGRESS_NOT_PROBED
    egress_permitted_status: str = ""
    egress_denied_status: str = ""
    egress_problems: tuple[str, ...] = ()
    credential_status: str = CREDENTIAL_NOT_RECEIVED
    credential_staged_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema != RUNTIME_BOUNDARY_SCHEMA:
            raise ValueError(f"schema must be {RUNTIME_BOUNDARY_SCHEMA!r}, got {self.schema!r}")
        if self.closure_binding not in CLOSURE_BINDING_STATUS_VALUES:
            raise ValueError(
                f"unknown closure binding {self.closure_binding!r}; vocabulary is "
                f"{CLOSURE_BINDING_STATUS_VALUES}"
            )
        if self.fingerprint_status not in FINGERPRINT_STATUS_VALUES:
            raise ValueError(
                f"unknown fingerprint status {self.fingerprint_status!r}; "
                f"vocabulary is {FINGERPRINT_STATUS_VALUES}"
            )
        if self.egress_status not in EGRESS_STATUS_VALUES:
            raise ValueError(
                f"unknown egress status {self.egress_status!r}; "
                f"vocabulary is {EGRESS_STATUS_VALUES}"
            )

    @property
    def problems(self) -> tuple[str, ...]:
        problems: list[str] = []
        if self.closure_binding == CLOSURE_UNPINNED:
            problems.append(
                "the profile pins no dependency closure digest — the install is "
                "wheel-pinned, not closure-pinned; pip resolved the runtime "
                "dependencies at install time"
            )
        elif self.closure_binding == CLOSURE_MISMATCH:
            problems.append(
                f"the profile pins closure {self.dependency_closure_digest[:12]} but "
                "the manifest under the report is a different closure — the runtime "
                "is not the one the profile qualified"
            )
        if self.fingerprint_status == FINGERPRINT_MISMATCHED:
            problems.extend(f"fingerprint: {d}" for d in self.fingerprint_divergences)
        elif self.fingerprint_status == FINGERPRINT_NOT_CHECKED:
            problems.append(
                "the installed set was not checked against the closure — an "
                "unchecked installation is not a verified boundary"
            )
        if self.egress_status == EGRESS_INCONSISTENT:
            problems.extend(f"egress: {problem}" for problem in self.egress_problems)
        elif self.egress_status == EGRESS_NOT_PROBED:
            problems.append(
                "the egress pair was not probed — an unprobed boundary is an unknown boundary"
            )
        if self.credential_status != CREDENTIAL_ISOLATED:
            problems.append(
                "no credential scope receipt — what the lane stages is unstated, "
                "and unstated is not isolated"
            )
        return tuple(problems)

    @property
    def verdict(self) -> str:
        """``within_boundary`` iff :attr:`problems` is empty."""
        return BOUNDARY_WITHIN if not self.problems else BOUNDARY_OUTSIDE

    def to_document(self) -> dict:
        return {
            "schema": self.schema,
            "verdict": self.verdict,
            "profile_digest": self.profile_digest,
            "dependency.closure": {
                "binding": self.closure_binding,
                "closure_digest": self.dependency_closure_digest,
            },
            "runtime.installed_fingerprint": {
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
            "security.egress_probe_results": {
                "status": self.egress_status,
                "permitted_status": self.egress_permitted_status,
                "denied_status": self.egress_denied_status,
                "problems": list(self.egress_problems),
            },
            "credential.staged_scope_receipt": {
                "status": self.credential_status,
                "staged_names": list(self.credential_staged_names),
            },
            "problems": list(self.problems),
        }


def runtime_boundary_report(
    profile: QualificationProfile,
    egress: EgressProbePair | None,
    closure_manifest: LaneClosureManifest | None,
    *,
    installed: Mapping[str, str] | None = None,
    credential_receipt: CredentialScopeReceipt | None = None,
) -> RuntimeBoundaryReport:
    """Assemble the runtime boundary report — one document binding the
    closure, the installed fingerprints, both egress legs and the
    credential isolation to the profile.

    Each leg accepts its typed evidence or ``None`` (a leg that never
    ran stays honestly partial). The closure binding compares the
    profile's ``dependency_closure_digest`` against
    *closure_manifest*; the fingerprint leg checks the INSTALLED set
    (name → version) against the manifest's pins via
    :func:`verify_installed_fingerprints` — a caught
    :class:`FingerprintMismatch` is RECORDED (refusal is evidence),
    never swallowed. The verdict is derived: ``within_boundary`` only
    when every leg is complete and passing."""
    if closure_manifest is None:
        binding = CLOSURE_UNPINNED if not profile.dependency_closure_digest else CLOSURE_MISMATCH
        closure_digest = profile.dependency_closure_digest
    elif not profile.dependency_closure_digest:
        binding = CLOSURE_UNPINNED
        closure_digest = closure_manifest.closure_digest
    elif profile.dependency_closure_digest == closure_manifest.closure_digest:
        binding = CLOSURE_BOUND
        closure_digest = closure_manifest.closure_digest
    else:
        binding = CLOSURE_MISMATCH
        closure_digest = profile.dependency_closure_digest

    if installed is None or closure_manifest is None:
        fingerprint_status = FINGERPRINT_NOT_CHECKED
        divergences: tuple[FingerprintDivergence, ...] = ()
    else:
        try:
            verify_installed_fingerprints(dict(closure_manifest.pins), installed)
            fingerprint_status = FINGERPRINT_MATCHED
            divergences = ()
        except FingerprintMismatch as mismatch:
            fingerprint_status = FINGERPRINT_MISMATCHED
            divergences = mismatch.divergences

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

    if credential_receipt is None:
        credential_status = CREDENTIAL_NOT_RECEIVED
        staged: tuple[str, ...] = ()
    else:
        credential_status = credential_receipt.verdict
        staged = credential_receipt.staged_names

    return RuntimeBoundaryReport(
        schema=RUNTIME_BOUNDARY_SCHEMA,
        profile_digest=profile.qualification_digest,
        closure_binding=binding,
        dependency_closure_digest=closure_digest,
        fingerprint_status=fingerprint_status,
        fingerprint_divergences=divergences,
        egress_status=egress_status,
        egress_permitted_status=egress_permitted,
        egress_denied_status=egress_denied,
        egress_problems=egress_problems,
        credential_status=credential_status,
        credential_staged_names=staged,
    )
