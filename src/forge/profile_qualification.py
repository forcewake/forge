"""R36-22 (#281) — profile qualification as a SEPARATE evidence record.

A promoted release artifact (see :mod:`forge.release_promotion`) proves the
DIGEST qualified: the required CI checks passed on the tagged sha and the
canary stages passed on the exact image. That says NOTHING about whether a
given provider/recipe/harness combination works — the v0.35.0 canary ran
``027 -> 027`` (same-head preservation, not a schema-upgrade proof), and an
authored model fixture is a different evidence class from a live task or a
recovery result. This module is the OTHER record: the profile-qualification
record, stamp ``forge.profile.qualification/1``, committed under
``qualification/records/``.

The doctrine (the review's applicability demand):

- **Evidence classes never substitute.** ``binary-smoke``, ``model-fixture``,
  ``pg-integration``, ``offline-operational``, ``live-provider`` and
  ``customer-acceptance`` are distinct classes with distinct applicability.
  A record claiming ``supported`` must carry at least one
  live-provider-OR-STRONGER evidence entry for EVERY required capability;
  model-fixture evidence alone caps the record at ``declared_only``;
  offline-operational (and pg-integration) cap at ``lab-qualified``.
- **Verdicts are derived, never asserted.** The stored record carries no
  verdict field at all — :func:`derive_verdict` / :func:`evaluate_record`
  compute it from the evidence entries at every read, so an edited record
  cannot smuggle a stronger claim than its evidence supports.
- **A record references, never copies, its evidence archives** — repo-relative
  pointers to the release-evidence archive and the profile evidence bundles.
  A deleted reference degrades the verdict (:func:`evaluate_record` with a
  ``root``); it never crashes the loader and never mutates history.
- **Requalification is triggered by named changes** — a changed runtime
  dependency fingerprint, template defaults, authority contract (the
  checkpoint repository protocol version) or provider behavior degrades the
  affected record's verdict to ``unqualified`` with the trigger named
  (:func:`requalification_triggers`). Degradation is a VIEW: records are
  frozen data, the store is append-only, replaying an older record never
  resurrects a verdict.
- **Upgrade claims are honest by construction.** :func:`upgrade_claim`
  distinguishes ``same-head-preservation`` (a 027→027 canary) from
  ``schema-transition`` (an actual N-1→N migration with representative
  seeded records and preservation checks actually executed). A same-head
  test NEVER labels itself a schema upgrade; a transition that seeded
  nothing and checked nothing makes NO claim at all.
- **The design-partner outcome is a separate class** —
  ``customer-acceptance`` entries record REAL partner outcomes; the store's
  write path (:func:`write_profile_record`) is immutable-by-default like the
  release archive, so an acceptance record is never regenerated from
  fixtures (AT-12).

The promotion-side join lives in :mod:`forge.release_promotion`:
:func:`forge.release_promotion.qualification_gaps` grows a profile-records
channel — a capability whose manifest ``evidence_class`` requires profile
qualification stays a NAMED gap until an executed record covers it at the
release being checked. The enforcement hook is
:func:`profile_promotion_refusals` (+ the ``gate`` CLI): a record whose
required evidence is marked ``skip`` refuses the profile's promotion even
with core CI green.

Observability names this module is the source of:
``release.artifact_identity`` (the record's digest/wheel/closure identity),
``profile.qualification_class`` (the derived verdict + limiting evidence
class — :func:`render_capabilities`), ``qualification.missing_evidence``
(the gaps' reasons), ``upgrade.executed_schema_edge`` (an executed
schema-transition claim's source→target edge).

CLI::

    python -m forge.profile_qualification gate          # refuse on skipped evidence
    python -m forge.profile_qualification capabilities  # per-profile verdict table

Pure stdlib; frozen data throughout (a qualification record must never
mutate under the promotion that cites it).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Mapping

__all__ = [
    "CAPABILITIES_BEGIN",
    "CAPABILITIES_END",
    "EVIDENCE_CLASSES",
    "EVIDENCE_CLASS_APPLICABILITY",
    "OUTCOMES",
    "PROFILE_RECORD_STAMP",
    "REQUALIFICATION_AXES",
    "REQUALIFICATION_AXIS_LABELS",
    "VERDICTS",
    "EvidenceEntry",
    "ProfilePromotionRefusal",
    "ProfileQualificationRecord",
    "ProfileRecordError",
    "UpgradeClaim",
    "UpgradeFacts",
    "VerdictReport",
    "archive_reference_gaps",
    "derive_verdict",
    "evaluate_record",
    "load_profile_records",
    "profile_promotion_refusals",
    "requalification_required",
    "requalification_triggers",
    "render_capabilities",
    "render_json",
    "upgrade_claim",
    "write_profile_record",
]

#: Versioned stamp of the profile-qualification record document.
PROFILE_RECORD_STAMP: Final[str] = "forge.profile.qualification/1"

#: The evidence classes, weakest first. Each names a DIFFERENT applicability
#: — no class substitutes for another (the R36-22 substitution rules).
EVIDENCE_CLASSES: Final[tuple[str, ...]] = (
    "binary-smoke",
    "model-fixture",
    "pg-integration",
    "offline-operational",
    "live-provider",
    "customer-acceptance",
)

EVIDENCE_CLASS_APPLICABILITY: Final[Mapping[str, str]] = {
    "binary-smoke": "the release artifact itself boots and passes its smoke "
    "recipe (canary-style): proves the binary on this recipe, never a "
    "provider workflow",
    "model-fixture": "authored cohort/model fixtures: the scenario and its "
    "responses were written by hand — proves the contract, never the "
    "provider or the model",
    "pg-integration": "real PostgreSQL with authored fixtures: proves the "
    "database contract against a real engine, never provider behavior",
    "offline-operational": "process-level operational scenarios executed in "
    "the lab (recovery drills, resume, restarts) without the live provider",
    "live-provider": "real task or recovery results against the live "
    "provider and model route named by the record",
    "customer-acceptance": (
        "a REAL design partner accepted the work on this profile — recorded "
        "once from the partner's decision, never regenerated from fixtures (AT-12)"
    ),
}

#: Record-level outcomes for one evidence entry. A ``skip`` is recorded as a
#: skip and never counted as a pass; the promotion-refusal hook treats a
#: skip on a required capability as a refusal.
OUTCOMES: Final[frozenset[str]] = frozenset({"pass", "fail", "skip"})

#: The verdicts, weakest first. DERIVED from the evidence (never stored).
VERDICTS: Final[tuple[str, ...]] = ("unqualified", "declared_only", "lab-qualified", "supported")

_VERDICT_RANK: Final[dict[str, int]] = {verdict: rank for rank, verdict in enumerate(VERDICTS)}

#: The qualification tier an evidence class contributes toward a required
#: capability (the substitution lattice):
#: 1 = declared (the profile is declared, its fixtures executed),
#: 2 = lab (executed in the lab: pg-integration / offline-operational),
#: 3 = live (live-provider or stronger — the only tier that supports).
_CLASS_TIER: Final[dict[str, int]] = {
    "binary-smoke": 1,
    "model-fixture": 1,
    "pg-integration": 2,
    "offline-operational": 2,
    "live-provider": 3,
    "customer-acceptance": 3,
}

_TIER_VERDICT: Final[dict[int, str]] = {1: "declared_only", 2: "lab-qualified", 3: "supported"}

#: The requalification axes: a changed value on any pinned axis degrades the
#: record's verdict to ``unqualified`` with the trigger named.
REQUALIFICATION_AXES: Final[tuple[str, ...]] = (
    "runtime_dependency_fingerprint",
    "template_defaults_digest",
    "authority_contract_version",
    "provider_behavior_fingerprint",
)

REQUALIFICATION_AXIS_LABELS: Final[Mapping[str, str]] = {
    "runtime_dependency_fingerprint": "changed runtime dependency fingerprint",
    "template_defaults_digest": "changed template defaults",
    "authority_contract_version": (
        "changed authority contract (checkpoint repository protocol version)"
    ),
    "provider_behavior_fingerprint": "changed provider behavior",
}

_PROVIDERS: Final[frozenset[str]] = frozenset({"gitlab", "github", "azure", "*"})
_VERSION_RE: Final[re.Pattern[str]] = re.compile(r"\d+\.\d+\.\d+([ab.rc]+\d*)?")


class ProfileRecordError(Exception):
    """A profile-qualification record is unusable (bad shape, bad stamp)."""


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def version_key(version: str) -> tuple[int, ...]:
    """The numeric sort key of a release version (``0.35.0`` -> (0, 35, 0))."""
    return tuple(int(p) for p in version.split(".")[:3] if p.isdigit())


# ---------------------------------------------------------------------------
# The record and its evidence entries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceEntry:
    """One executed evidence artifact, in its class, for one capability.

    ``evidence_class`` names the applicability (see
    :data:`EVIDENCE_CLASS_APPLICABILITY`) — the substitution rules are
    ENFORCED on it, not on prose. ``capability`` is the slug the entry
    speaks to (a manifest capability like ``real-provider-e2e`` or a profile
    capability; entries for capabilities outside the record's required set
    are informational and never feed the verdict). ``outcome`` is
    ``pass`` | ``fail`` | ``skip`` — a skip is a skip. ``covers`` carries
    the trace ids/paths of WHAT was covered. ``executed_at`` is the SHA it
    executed at — the commit sha, image digest or wheel sha256 that was
    actually running, never a moving ref.
    """

    evidence_class: str
    capability: str
    outcome: str
    covers: str
    executed_at: str

    def __post_init__(self) -> None:
        if self.evidence_class not in _CLASS_TIER:
            raise ProfileRecordError(
                f"unknown evidence class {self.evidence_class!r} — the vocabulary is "
                f"{list(EVIDENCE_CLASSES)}; an off-vocabulary class is a modelling "
                "error, never a stronger claim"
            )
        if self.outcome not in OUTCOMES:
            raise ProfileRecordError(
                f"evidence outcome {self.outcome!r} is not one of {sorted(OUTCOMES)}"
            )
        for name in ("capability", "covers", "executed_at"):
            if not str(getattr(self, name)).strip():
                raise ProfileRecordError(
                    f"evidence entry ({self.evidence_class}) needs a non-empty {name} — "
                    "evidence without provenance qualifies nothing"
                )

    def to_json(self) -> dict[str, object]:
        return {
            "class": self.evidence_class,
            "capability": self.capability,
            "outcome": self.outcome,
            "covers": self.covers,
            "executed_at": self.executed_at,
        }

    @classmethod
    def from_json(cls, document: Mapping[str, object]) -> EvidenceEntry:
        try:
            return cls(
                evidence_class=str(document["class"]),
                capability=str(document["capability"]),
                outcome=str(document["outcome"]),
                covers=str(document["covers"]),
                executed_at=str(document["executed_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProfileRecordError(f"malformed evidence entry: {exc}") from exc


@dataclass(frozen=True)
class UpgradeFacts:
    """The executed upgrade facts a record's upgrade claim is DERIVED from.

    ``source_schema`` / ``target_schema`` are the alembic heads the upgrade
    actually ran between (``027`` -> ``027`` is a same-head preservation).
    ``seeded_records`` names the representative seeded records the migration
    had to preserve; ``preservation_checks`` names the checks actually
    executed over them (counts, fingerprints). ``evidence_ref`` points at
    the archived evidence (the promotion record's canary rows).
    """

    source_schema: str
    target_schema: str
    seeded_records: tuple[str, ...] = ()
    preservation_checks: tuple[str, ...] = ()
    evidence_ref: str = ""

    def __post_init__(self) -> None:
        for name in ("source_schema", "target_schema"):
            if not str(getattr(self, name)).strip():
                raise ProfileRecordError(f"upgrade facts need a non-empty {name}")
        for name in ("seeded_records", "preservation_checks"):
            for value in getattr(self, name):
                if not str(value).strip():
                    raise ProfileRecordError(f"upgrade facts' {name} entries are non-empty")

    def to_json(self) -> dict[str, object]:
        return {
            "source_schema": self.source_schema,
            "target_schema": self.target_schema,
            "seeded_records": list(self.seeded_records),
            "preservation_checks": list(self.preservation_checks),
            "evidence_ref": self.evidence_ref,
        }

    @classmethod
    def from_json(cls, document: Mapping[str, object]) -> UpgradeFacts:
        try:
            return cls(
                source_schema=str(document["source_schema"]),
                target_schema=str(document["target_schema"]),
                seeded_records=tuple(str(item) for item in document.get("seeded_records", ())),
                preservation_checks=tuple(
                    str(item) for item in document.get("preservation_checks", ())
                ),
                evidence_ref=str(document.get("evidence_ref", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProfileRecordError(f"malformed upgrade facts: {exc}") from exc


@dataclass(frozen=True)
class ProfileQualificationRecord:
    """ONE provider/recipe/harness combination qualified on ONE release.

    The identity half names the world the qualification ran in: provider +
    provider version, runtime recipe, harness binary + version, credential
    route, control capabilities, verification contract, and the closure
    digest (the R36-10/#269 field — the sha256 identity of the hash-locked
    wheelhouse the qualification installed from; empty means wheel-pinned,
    stated, never silently defaulted). ``image_digest`` / ``wheel_sha256``
    pin the release artifact identity (either may be empty — an image-only
    release keeps its wheel honestly absent).

    The evidence half: ``capabilities`` is the REQUIRED set this record
    claims to qualify (the substitution rules run per capability), and
    ``evidence`` lists the executed entries in their classes. The four
    requalification axes pin what the record observed so a later change can
    be NAMED as a trigger. ``evidence_refs`` reference (never copy) the
    evidence archives the record is judged with.

    There is deliberately NO verdict field: the verdict is derived on every
    read (:func:`derive_verdict`), so the record cannot assert what its
    evidence does not support.
    """

    record_id: str
    profile: str
    provider: str
    release_version: str
    provider_version: str
    runtime_recipe: str
    harness_binary: str
    harness_version: str
    credential_route: str
    verification_contract: str
    capabilities: tuple[str, ...]
    evidence: tuple[EvidenceEntry, ...]
    control_capabilities: tuple[str, ...] = ()
    closure_digest: str = ""
    image_digest: str = ""
    wheel_sha256: str = ""
    runtime_dependency_fingerprint: str = ""
    template_defaults_digest: str = ""
    authority_contract_version: str = ""
    provider_behavior_fingerprint: str = ""
    evidence_refs: tuple[str, ...] = ()
    upgrade: UpgradeFacts | None = None
    note: str = ""

    def __post_init__(self) -> None:
        for name in ("record_id", "profile"):
            if not str(getattr(self, name)).strip():
                raise ProfileRecordError(f"{name} must be non-empty — a record names itself")
        if self.provider not in _PROVIDERS:
            raise ProfileRecordError(f"bad provider {self.provider!r}")
        if not _VERSION_RE.fullmatch(self.release_version):
            raise ProfileRecordError(f"bad release version {self.release_version!r}")
        # An artifact-level profile (provider "*") has no provider version;
        # a provider profile must name the exact version it qualified on.
        if self.provider != "*" and not self.provider_version.strip():
            raise ProfileRecordError(
                "a provider profile names its provider_version — the exact "
                "provider build the qualification ran against"
            )
        for name in (
            "runtime_recipe",
            "harness_binary",
            "harness_version",
            "credential_route",
            "verification_contract",
        ):
            if not str(getattr(self, name)).strip():
                raise ProfileRecordError(f"{name} must be non-empty — a qualified record names it")
        if self.closure_digest and not _is_sha256(self.closure_digest):
            raise ProfileRecordError(
                "closure_digest must be the sha256 closure digest of the installed "
                "wheelhouse (R36-10 LaneClosureManifest.closure_digest), or empty for "
                "a wheel-pinned record — never a partial or foreign digest"
            )
        for name in ("capabilities", "control_capabilities"):
            values = getattr(self, name)
            if any(not str(value).strip() for value in values):
                raise ProfileRecordError(f"{name} entries must be non-empty")
            if len(values) != len(set(values)):
                raise ProfileRecordError(f"{name} carries duplicates — one entry per name")
        if not self.capabilities:
            raise ProfileRecordError(
                "a qualification record claims at least one capability — a record "
                "qualifying nothing is a declaration, and this store is not one"
            )
        for name in ("evidence", "evidence_refs"):
            for value in getattr(self, name):
                if not str(value).strip():
                    raise ProfileRecordError(f"{name} entries must be non-empty")
        for ref in self.evidence_refs:
            if ref.startswith("/") or ".." in ref:
                raise ProfileRecordError(
                    f"evidence ref {ref!r} must be repo-relative — the store references "
                    "archives, it never reaches outside the tree"
                )

    def to_json(self) -> dict[str, object]:
        return {
            "stamp": PROFILE_RECORD_STAMP,
            "record_id": self.record_id,
            "profile": self.profile,
            "provider": self.provider,
            "release_version": self.release_version,
            "provider_version": self.provider_version,
            "runtime_recipe": self.runtime_recipe,
            "harness_binary": self.harness_binary,
            "harness_version": self.harness_version,
            "credential_route": self.credential_route,
            "control_capabilities": list(self.control_capabilities),
            "verification_contract": self.verification_contract,
            "closure_digest": self.closure_digest,
            "image_digest": self.image_digest,
            "wheel_sha256": self.wheel_sha256,
            "capabilities": list(self.capabilities),
            "evidence": [entry.to_json() for entry in self.evidence],
            "runtime_dependency_fingerprint": self.runtime_dependency_fingerprint,
            "template_defaults_digest": self.template_defaults_digest,
            "authority_contract_version": self.authority_contract_version,
            "provider_behavior_fingerprint": self.provider_behavior_fingerprint,
            "evidence_refs": list(self.evidence_refs),
            "upgrade": self.upgrade.to_json() if self.upgrade is not None else None,
            "note": self.note,
        }

    @classmethod
    def from_json(cls, document: Mapping[str, object]) -> ProfileQualificationRecord:
        stamp = document.get("stamp")
        if stamp != PROFILE_RECORD_STAMP:
            raise ProfileRecordError(f"profile record stamp {stamp!r} != {PROFILE_RECORD_STAMP!r}")
        try:
            upgrade_doc = document.get("upgrade")
            return cls(
                record_id=str(document["record_id"]),
                profile=str(document["profile"]),
                provider=str(document["provider"]),
                release_version=str(document["release_version"]),
                provider_version=str(document.get("provider_version", "")),
                runtime_recipe=str(document["runtime_recipe"]),
                harness_binary=str(document["harness_binary"]),
                harness_version=str(document["harness_version"]),
                credential_route=str(document["credential_route"]),
                verification_contract=str(document["verification_contract"]),
                capabilities=tuple(str(item) for item in document["capabilities"]),
                evidence=tuple(
                    EvidenceEntry.from_json(entry) for entry in document.get("evidence", ())
                ),
                control_capabilities=tuple(
                    str(item) for item in document.get("control_capabilities", ())
                ),
                closure_digest=str(document.get("closure_digest", "")),
                image_digest=str(document.get("image_digest", "")),
                wheel_sha256=str(document.get("wheel_sha256", "")),
                runtime_dependency_fingerprint=str(
                    document.get("runtime_dependency_fingerprint", "")
                ),
                template_defaults_digest=str(document.get("template_defaults_digest", "")),
                authority_contract_version=str(document.get("authority_contract_version", "")),
                provider_behavior_fingerprint=str(
                    document.get("provider_behavior_fingerprint", "")
                ),
                evidence_refs=tuple(str(item) for item in document.get("evidence_refs", ())),
                upgrade=(
                    UpgradeFacts.from_json(upgrade_doc)
                    if isinstance(upgrade_doc, Mapping)
                    else None
                ),
                note=str(document.get("note", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProfileRecordError(f"malformed profile record: {exc}") from exc


# ---------------------------------------------------------------------------
# Verdict derivation: the substitution lattice, enforced
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerdictReport:
    """The derived verdict plus the reasons and per-capability breakdown."""

    verdict: str
    reasons: tuple[str, ...]
    per_capability: tuple[tuple[str, str], ...]  # (capability, contribution)

    def to_json(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "per_capability": [
                {"capability": capability, "verdict": verdict}
                for capability, verdict in self.per_capability
            ],
        }


def _capability_contribution(
    record: ProfileQualificationRecord, capability: str, reasons: list[str]
) -> str:
    entries = [entry for entry in record.evidence if entry.capability == capability]
    if not entries:
        reasons.append(
            f"capability {capability!r}: no evidence entry — an uncovered capability is "
            "unqualified, never auto-supported by a sibling capability"
        )
        return "unqualified"
    passing = [entry for entry in entries if entry.outcome == "pass"]
    for entry in entries:
        if entry.outcome == "skip":
            reasons.append(
                f"capability {capability!r}: evidence ({entry.evidence_class}) SKIPPED — "
                "recorded as a skip, never counted as a pass"
            )
    if not passing:
        outcomes = ", ".join(sorted({entry.outcome for entry in entries}))
        reasons.append(f"capability {capability!r}: no passing evidence (outcomes: {outcomes})")
        return "unqualified"
    best = max(passing, key=lambda entry: _CLASS_TIER[entry.evidence_class])
    tier = _CLASS_TIER[best.evidence_class]
    contribution = _TIER_VERDICT[tier]
    if tier < 3:
        reasons.append(
            f"capability {capability!r}: strongest passing evidence is {best.evidence_class} "
            f"— capped at {contribution} ({EVIDENCE_CLASS_APPLICABILITY[best.evidence_class]}); "
            "supported needs live-provider-or-stronger evidence"
        )
    return contribution


def requalification_triggers(
    record: ProfileQualificationRecord, changes: Mapping[str, str]
) -> tuple[str, ...]:
    """The NAMED triggers for every changed axis this record must answer to.

    Per axis in :data:`REQUALIFICATION_AXES`: a change naming a DIFFERENT
    value than the record pins is a trigger; a change naming an axis the
    record left UNPINNED is a trigger too (fail-closed — an unpinned axis
    never silently absorbs a change). Unnamed axes trigger nothing.
    """
    triggers: list[str] = []
    for axis in REQUALIFICATION_AXES:
        current = changes.get(axis)
        if current is None:
            continue
        pinned = str(getattr(record, axis))
        if not pinned:
            triggers.append(
                f"{REQUALIFICATION_AXIS_LABELS[axis]}: this record pins no "
                f"{axis} but the current value is {current!r} — an unpinned axis "
                "cannot absorb a change; pin the axis and requalify"
            )
        elif str(current) != pinned:
            triggers.append(
                f"{axis} — {REQUALIFICATION_AXIS_LABELS[axis]}: record pins {pinned!r}, "
                f"current value {current!r} — requalification required"
            )
    return tuple(triggers)


def requalification_required(
    record: ProfileQualificationRecord, changes: Mapping[str, str]
) -> bool:
    """Whether ANY named trigger fires for *record* under *changes*.

    When it does, the affected record's derived verdict degrades to
    ``unqualified`` with the trigger named — see
    :func:`requalification_triggers` / :func:`evaluate_record`.
    """
    return bool(requalification_triggers(record, changes))


def evaluate_record(
    record: ProfileQualificationRecord,
    changes: Mapping[str, str] | None = None,
    root: Path | None = None,
) -> VerdictReport:
    """Derive the verdict — the only place a verdict comes from.

    - per required capability: the strongest PASSING evidence entry's tier
      decides the contribution (substitution ENFORCED — see
      :data:`_CLASS_TIER`); no passing entry, or none at all, is
      ``unqualified``;
    - the record verdict is the WEAKEST contribution (a profile is as
      qualified as its least-qualified required capability);
    - ``changes`` (the current world) degrades the verdict to
      ``unqualified`` with every trigger :func:`requalification_triggers`
      names — degradation is a view, the record itself never mutates;
    - ``root`` (when given) checks the referenced evidence archives: a
      deleted reference degrades to ``unqualified``, naming the path.
    """
    reasons: list[str] = []
    per_capability = tuple(
        (capability, _capability_contribution(record, capability, reasons))
        for capability in record.capabilities
    )
    verdict = min((contribution for _, contribution in per_capability), key=_VERDICT_RANK.get)

    triggers = requalification_triggers(record, changes) if changes is not None else ()
    for trigger in triggers:
        reasons.append(trigger)
    missing_refs = archive_reference_gaps(record, root) if root is not None else ()
    for missing in missing_refs:
        reasons.append(missing)

    if triggers or missing_refs:
        verdict = "unqualified"
    return VerdictReport(
        verdict=verdict,
        reasons=tuple(reasons),
        per_capability=per_capability,
    )


def derive_verdict(
    record: ProfileQualificationRecord,
    changes: Mapping[str, str] | None = None,
    root: Path | None = None,
) -> str:
    """The derived verdict alone (see :func:`evaluate_record`)."""
    return evaluate_record(record, changes=changes, root=root).verdict


def archive_reference_gaps(record: ProfileQualificationRecord, root: Path) -> tuple[str, ...]:
    """Referenced evidence artifacts that no longer exist under *root*.

    A record REFERENCES its archives (never copies them); a deleted
    reference is a degradation reason naming the path — the loader never
    crashes on it and the record on disk is never touched.
    """
    gaps: list[str] = []
    for ref in record.evidence_refs:
        if not (root / ref).exists():
            gaps.append(
                f"referenced evidence artifact {ref!r} does not exist — the record's "
                "evidence cannot be consulted; degraded, history untouched"
            )
    return tuple(gaps)


# ---------------------------------------------------------------------------
# Upgrade-claim honesty
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpgradeClaim:
    """The DERIVED upgrade claim — ``same-head-preservation`` or
    ``schema-transition``.

    Constructed only by :func:`upgrade_claim` (derive, never assert): a
    same-head run (``027`` -> ``027``) is a PRESERVATION claim and NEVER
    labels itself a schema upgrade; a real transition names its source
    schema, target schema, representative seeded records and the
    preservation checks actually executed.
    """

    kind: Literal["same-head-preservation", "schema-transition"]
    source_schema: str
    target_schema: str
    seeded_records: tuple[str, ...]
    preservation_checks: tuple[str, ...]
    evidence_ref: str

    def __post_init__(self) -> None:
        if self.kind == "schema-transition" and not (
            self.seeded_records and self.preservation_checks
        ):
            raise ProfileRecordError(
                "a schema-transition claim names its representative seeded records "
                "and the preservation checks actually executed — a transition that "
                "seeded nothing proves nothing about data"
            )
        if self.kind == "same-head-preservation" and self.source_schema != self.target_schema:
            raise ProfileRecordError(
                "a same-head-preservation claim has identical source and target heads"
            )

    @property
    def is_schema_upgrade(self) -> bool:
        """True only for a real schema transition (never a preservation run)."""
        return self.kind == "schema-transition"

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "source_schema": self.source_schema,
            "target_schema": self.target_schema,
            "seeded_records": list(self.seeded_records),
            "preservation_checks": list(self.preservation_checks),
            "evidence_ref": self.evidence_ref,
        }


def upgrade_claim(record: ProfileQualificationRecord) -> UpgradeClaim | None:
    """Derive the upgrade claim from the record's executed facts.

    ``None`` (NO claim — the honest answer) when the record carries no
    upgrade facts, or when the transition executed neither representative
    seeded records nor preservation checks: an upgrade claim must name what
    a real deployment held and what was actually checked (the v0.33.0
    canary migrated 024→026 but seeded nothing, so it makes no claim).
    """
    facts = record.upgrade
    if facts is None:
        return None
    if not (facts.seeded_records and facts.preservation_checks):
        return None
    kind: Literal["same-head-preservation", "schema-transition"] = (
        "same-head-preservation"
        if facts.source_schema == facts.target_schema
        else "schema-transition"
    )
    return UpgradeClaim(
        kind=kind,
        source_schema=facts.source_schema,
        target_schema=facts.target_schema,
        seeded_records=facts.seeded_records,
        preservation_checks=facts.preservation_checks,
        evidence_ref=facts.evidence_ref,
    )


# ---------------------------------------------------------------------------
# The promotion-refusal hook: a skipped required test refuses the profile
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfilePromotionRefusal:
    """One profile whose promotion is refused, with the reasons."""

    profile: str
    record_id: str
    reasons: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "record_id": self.record_id,
            "reasons": list(self.reasons),
        }


def latest_record_per_profile(
    records: tuple[ProfileQualificationRecord, ...],
) -> tuple[ProfileQualificationRecord, ...]:
    """The freshest record per profile (highest release version, then id).

    The store is append-only: an older record replayed beside a newer one
    never wins — ordering comes from the version, not from file order.
    """
    latest: dict[str, ProfileQualificationRecord] = {}
    for record in records:
        current = latest.get(record.profile)
        if current is None or (
            version_key(record.release_version),
            record.record_id,
        ) > (version_key(current.release_version), current.record_id):
            latest[record.profile] = record
    return tuple(latest[profile] for profile in sorted(latest))


def profile_promotion_refusals(
    records: tuple[ProfileQualificationRecord, ...],
    changes: Mapping[str, str] | None = None,
    root: Path | None = None,
) -> tuple[ProfilePromotionRefusal, ...]:
    """Refuse the promotion of any profile whose evidence did not execute.

    The enforcement hook (R36-22 negative test): a record whose required
    evidence is marked ``skip`` — or failed, or missing, or whose derived
    verdict is ``unqualified`` — refuses that PROFILE's promotion, even
    with core CI green. Only each profile's LATEST record is judged;
    superseded records are history. Fixing a refusal means landing a NEW
    record, never editing one.
    """
    refusals: list[ProfilePromotionRefusal] = []
    for record in latest_record_per_profile(records):
        reasons: list[str] = []
        for capability in record.capabilities:
            for entry in record.evidence:
                if entry.capability != capability:
                    continue
                if entry.outcome == "skip":
                    reasons.append(
                        f"required profile evidence for capability {capability!r} "
                        f"({entry.evidence_class}) is SKIPPED — the profile's promotion "
                        "is refused even with core CI green; a skip is never a pass"
                    )
                elif entry.outcome == "fail":
                    reasons.append(
                        f"required profile evidence for capability {capability!r} "
                        f"({entry.evidence_class}) FAILED — the profile's promotion "
                        "is refused"
                    )
        report = evaluate_record(record, changes=changes, root=root)
        if report.verdict == "unqualified":
            reasons.extend(report.reasons)
        if reasons:
            refusals.append(
                ProfilePromotionRefusal(
                    profile=record.profile, record_id=record.record_id, reasons=tuple(reasons)
                )
            )
    return tuple(refusals)


# ---------------------------------------------------------------------------
# The record store: qualification/records/ (committed, append-only)
# ---------------------------------------------------------------------------


def render_json(document: dict[str, object]) -> str:
    """Deterministic JSON: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _store_dir(root: Path) -> Path:
    return root / "qualification" / "records"


def write_profile_record(
    root: Path, record: ProfileQualificationRecord, replace: bool = False
) -> Path:
    """Commit *record* into the store; immutable by default.

    Byte-identical rewrites are no-ops; DIFFERENT content over an existing
    record file is refused unless ``replace=True`` (the same discipline as
    the release archive — a customer-acceptance record is never
    regenerated from fixtures, AT-12).
    """
    target = _store_dir(root)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{record.record_id}.json"
    content = render_json(record.to_json())
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing == content:
            return path
        if not replace:
            raise ProfileRecordError(
                f"{path} already holds DIFFERENT evidence — profile records are "
                "immutable; land a NEW record to supersede (replace=True only for "
                "an explicit rewrite)"
            )
    path.write_text(content, encoding="utf-8")
    return path


def load_profile_records(root: Path) -> tuple[ProfileQualificationRecord, ...]:
    """Every committed profile record, oldest release first.

    Reads ``qualification/records/*.json``; a missing store is simply no
    records. A bad stamp or malformed record refuses the whole load
    (fail-closed: the promotion side must never guess past unreadable
    evidence).
    """
    base = _store_dir(root)
    if not base.is_dir():
        return ()
    records: list[ProfileQualificationRecord] = []
    for path in sorted(base.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        records.append(ProfileQualificationRecord.from_json(document))
    records.sort(key=lambda record: (version_key(record.release_version), record.record_id))
    return tuple(records)


# ---------------------------------------------------------------------------
# The capabilities render: per-profile verdicts from the records
# ---------------------------------------------------------------------------

CAPABILITIES_BEGIN: Final[str] = (
    "<!-- generated by python -m forge.profile_qualification capabilities -- begin -->"
)
CAPABILITIES_END: Final[str] = (
    "<!-- generated by python -m forge.profile_qualification capabilities -- end -->"
)


def _limiting_evidence(record: ProfileQualificationRecord) -> str:
    """The strongest evidence class per required capability, for display."""
    parts: list[str] = []
    for capability in record.capabilities:
        passing = [
            entry
            for entry in record.evidence
            if entry.capability == capability and entry.outcome == "pass"
        ]
        if not passing:
            parts.append(f"{capability}: none")
            continue
        best = max(passing, key=lambda entry: _CLASS_TIER[entry.evidence_class])
        parts.append(f"{capability}: {best.evidence_class}")
    return "; ".join(parts)


def render_capabilities(records: tuple[ProfileQualificationRecord, ...]) -> str:
    """The per-profile verdict table — derived, never self-declared.

    Every verdict comes from :func:`derive_verdict` over the committed
    records' evidence classes; runtime proof is never inferred from a test
    filename or a label.
    """
    rows = [
        "| Profile | Provider | Release | Verdict | Strongest evidence | Upgrade claim |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for record in records:
        claim = upgrade_claim(record)
        claim_text = (
            f"{claim.kind} {claim.source_schema}→{claim.target_schema}"
            if claim is not None
            else "—"
        )
        rows.append(
            f"| {record.profile} | {record.provider} | v{record.release_version} "
            f"| {derive_verdict(record)} | {_limiting_evidence(record)} | {claim_text} |"
        )
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_gate(args: argparse.Namespace) -> int:
    root = Path(args.root)
    records = load_profile_records(root)
    if not records:
        print(
            "profile-qualification gate: no committed records under "
            "qualification/records/ — nothing profile-qualified; the release gate "
            "alone decides",
            file=sys.stderr,
        )
        return 0
    refusals = profile_promotion_refusals(records, root=root)
    if refusals:
        for refusal in refusals:
            print(
                f"profile-qualification gate: REFUSED — profile {refusal.profile!r} "
                f"({refusal.record_id}):",
                file=sys.stderr,
            )
            for reason in refusal.reasons:
                print(f"  - {reason}", file=sys.stderr)
        print(
            "profile-qualification gate: the profile promotions above are refused even "
            "with core CI green; land NEW records to supersede, never edit history",
            file=sys.stderr,
        )
        return 1
    for record in latest_record_per_profile(records):
        print(
            f"profile-qualification gate: {record.profile} ({record.record_id}) — "
            f"verdict {derive_verdict(record, root=root)}"
        )
    return 0


def _cmd_capabilities(args: argparse.Namespace) -> int:
    records = load_profile_records(Path(args.root))
    if not records:
        print(
            "profile-qualification capabilities: no committed records under qualification/records/",
            file=sys.stderr,
        )
        return 1
    sys.stdout.write(render_capabilities(records) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m forge.profile_qualification",
        description=(
            "Profile qualification records (R36-22): verdicts derived from evidence "
            "classes that never substitute for one another."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gate = sub.add_parser(
        "gate",
        help="refuse profile promotion on skipped/failed/missing required evidence",
    )
    gate.add_argument("--root", default=".", type=Path)
    gate.set_defaults(func=_cmd_gate)

    capabilities = sub.add_parser(
        "capabilities", help="print the per-profile verdict table from the records"
    )
    capabilities.add_argument("--root", default=".", type=Path)
    capabilities.set_defaults(func=_cmd_capabilities)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ProfileRecordError as exc:
        print(f"profile-qualification: REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
