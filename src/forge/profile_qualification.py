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

R37-06 (#287) adds the STRICT record-schema slice (the validation half of
the #298 overlap — the rest of #298 stays open):

- **Distinct timestamp/hash/source fields.** The v1 schema defined an
  evidence entry's ``executed_at`` as "the SHA it executed at" — which let
  the historical ``gitlab-ce-v1`` records carry a wheel sha in a field
  named like a timestamp. The strict shape separates them: ``executed_at``
  is an ISO-8601 UTC timestamp (WHEN), the new per-entry
  ``artifact_sha256`` names the artifact identity it executed at (WHAT),
  and ``covers`` keeps carrying the trace ids/paths (WHERE). Record-level
  hash fields (``wheel_sha256``, ``closure_digest``, ``image_digest``,
  ``artifact_sha256``) must be hex64 (an optional ``sha256:`` prefix is
  normalized away), and version fields must be semantic.
- **Validation is typed, not prose.** :func:`validate_record` returns
  :class:`ValidationFinding` values from a closed vocabulary
  (:data:`VALIDATION_FINDING_KINDS`) — a digest in a timestamp field, a
  non-hex hash, a non-semantic version, or an empty requalification
  fingerprint under a ``supported`` verdict each name their exact path.
- **Legacy is reported, never silently trusted.** A record marked
  ``legacy: true`` (the default — the whole v1 store predates the strict
  schema, and history is not rewritten) is still validated and its
  findings REPORTED under the observability name
  :data:`RECORD_VALIDATION_OBSERVABILITY` (``qualification.record_validation``),
  but it loads as history. A record that declares ``legacy: false`` (the
  opt-in strict schema) with ANY finding REFUSES the whole load
  fail-closed — a new record may never carry v1-era shape defects.
- **Refusal resolutions are typed record content** (R37-06): the
  ``refusal_resolution`` entries carry each recorded preflight refusal →
  the concrete action + owner + status, so a record can say what it will
  take to stop being refused without prose parsing.

R37-17 (#298) adds the promotion half — profiles are promoted from EXECUTED
evidence without upgrading the evidence class:

- **Evidence tiers derive from executed traces.** :class:`TraceRecord`
  (stamp ``forge.trace/1``, committed under ``qualification/traces/``) is a
  trace file with its own PROVENANCE stamp — ``scripted``, ``live`` or
  ``refused``. :func:`derive_evidence_tier` joins a record's required
  capabilities onto those traces: a scripted capture STAYS scripted even
  when it invokes real protocol code; a live trace (real provider + model
  route provenance) upgrades ONLY its own capability, and only in the
  provider's own records; a refusal trace never upgrades anything. A
  fixture-only record carrying a real-provider capability label is HELD
  (:data:`LIVE_REQUIRED_CAPABILITIES`, the negative arm) — the label never
  upgrades the trace, the trace caps the label.
- **The supported-profile manifest is human-gated.**
  :func:`build_supported_profiles` (stamp ``forge.profile.manifest/1``)
  renders per profile: the DERIVED verdict, the trace-derived evidence
  tiers, the limitations (the refusal-resolution matrix, unpinned axes,
  holds), the requalification triggers (a changed dependency fingerprint /
  template / WHEEL or IMAGE identity / provider behavior withdraws the
  affected claim, :data:`MANIFEST_TRIGGER_AXES`) — and a
  ``human_approved_by`` field that is REQUIRED for any ``supported``
  verdict: an unapproved profile is listed ``pending-approval``, never
  supported. Approvals are a SEPARATE human-maintained artifact
  (:func:`load_profile_approvals`), because evidence records are
  machine-written and approval is a human act.
- **Upgrade-claim honesty is enforced, not just derived.** A record whose
  ``upgrade.claim`` mislabels its facts (a 027→027 canary labelled a schema
  upgrade; a transition that seeded nothing asserting any claim) gets a
  typed ``upgrade-claim-mislabelled`` finding from :func:`validate_record`.
- **Field-shape conflation is its own finding kind.** A sha256 in a version
  field (``sha-in-version-field``) or a version string in a hash field
  (``version-in-hash-field``) names the conflation — the tested sha
  (:data:`TESTED_SHA_OBSERVABILITY`) never stands in for the deployed
  artifact identity, and each binding axis carries its OWN field with its
  OWN hash.
- **The store is a typed, queryable, append-only view.**
  :class:`ProfileRecordStore` refuses overwrites of existing record files
  with :class:`ProfileRecordImmutableError` naming the versioned filename,
  and keeps historical records separately queryable (``history`` /
  ``latest`` / ``record``) after new ones land.

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
    python -m forge.profile_qualification capabilities  # per-profile verdict + tier table
    python -m forge.profile_qualification manifest      # the supported-profiles manifest

Pure stdlib; frozen data throughout (a qualification record must never
mutate under the promotion that cites it).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal, Mapping, Sequence, cast

__all__ = [
    "APPROVAL_STAMP",
    "CAPABILITIES_BEGIN",
    "CAPABILITIES_END",
    "EVIDENCE_CLASSES",
    "EVIDENCE_CLASS_APPLICABILITY",
    "EVIDENCE_TIERS",
    "INSTALLED_FINGERPRINT_OBSERVABILITY",
    "LIVE_REQUIRED_CAPABILITIES",
    "MANIFEST_STAMP",
    "MANIFEST_STATUSES",
    "MANIFEST_TRIGGER_AXES",
    "MANIFEST_TRIGGER_AXIS_LABELS",
    "OUTCOMES",
    "PROFILE_RECORD_STAMP",
    "RECORD_VALIDATION_OBSERVABILITY",
    "REQUALIFICATION_AXES",
    "REQUALIFICATION_AXIS_LABELS",
    "TESTED_SHA_OBSERVABILITY",
    "TRACE_PROVENANCES",
    "TRACE_RECORD_STAMP",
    "UNMET_CAPABILITIES_OBSERVABILITY",
    "VALIDATION_FINDING_KINDS",
    "VERDICTS",
    "WITHDRAWN_OBSERVABILITY",
    "CapabilityTier",
    "EvidenceEntry",
    "EvidenceTierReport",
    "ProfileApproval",
    "ProfilePromotionRefusal",
    "ProfileQualificationRecord",
    "ProfileRecordError",
    "ProfileRecordImmutableError",
    "ProfileRecordStore",
    "RefusalResolution",
    "SupportedProfileEntry",
    "SupportedProfilesManifest",
    "TraceRecord",
    "UpgradeClaim",
    "UpgradeFacts",
    "ValidationFinding",
    "VerdictReport",
    "archive_reference_gaps",
    "build_supported_profiles",
    "derive_evidence_tier",
    "derive_verdict",
    "evaluate_record",
    "load_profile_approvals",
    "load_profile_records",
    "load_trace_records",
    "manifest_trigger_triggers",
    "profile_promotion_refusals",
    "requalification_required",
    "requalification_triggers",
    "render_capabilities",
    "render_json",
    "upgrade_claim",
    "validate_record",
    "write_profile_record",
]

#: Versioned stamp of the profile-qualification record document.
PROFILE_RECORD_STAMP: Final[str] = "forge.profile.qualification/1"

#: Versioned stamp of the executed-trace record document (R37-17): a trace
#: file/JSON that carries its own PROVENANCE, committed under
#: ``qualification/traces/``. Distinct from the qualification record — a
#: record CLAIMS, a trace EXECUTED.
TRACE_RECORD_STAMP: Final[str] = "forge.trace/1"

#: Versioned stamp of the supported-profiles manifest (R37-17) — the
#: derived, human-gated view over the record store. The manifest is never
#: committed: it is RE-DERIVED from the records on every read, like the
#: verdict itself.
MANIFEST_STAMP: Final[str] = "forge.profile.manifest/1"

#: Versioned stamp of the human-approval document (R37-17) — the ONE input
#: of :func:`build_supported_profiles` that is written by a person, not by
#: evidence collection (``qualification/profile-approvals.json``).
APPROVAL_STAMP: Final[str] = "forge.profile.approvals/1"

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

#: The trace-derived evidence tiers, weakest first (R37-17). A tier comes
#: ONLY from an executed trace record's provenance stamp — never from an
#: evidence-class label, a test filename or a note.
EVIDENCE_TIERS: Final[tuple[str, ...]] = ("none", "scripted", "live")

_TIER_RANK: Final[dict[str, int]] = {tier: rank for rank, tier in enumerate(EVIDENCE_TIERS)}

#: The trace provenances (R37-17): ``scripted`` — authored scenario/fakes,
#: stays scripted even when it invokes real protocol code; ``live`` — real
#: task or recovery results against the real provider AND model route the
#: trace names; ``refused`` — an executed observation of a refusal, which
#: never upgrades anything.
TRACE_PROVENANCES: Final[tuple[str, ...]] = ("scripted", "live", "refused")

#: Capability slugs whose NAME promises a real provider (R37-17's negative
#: arm): a record claiming one of these holds at HOLD unless the
#: trace-derived tier for it is ``live`` — a fixture-only record carrying a
#: real-provider label is never promoted.
LIVE_REQUIRED_CAPABILITIES: Final[frozenset[str]] = frozenset({"real-provider-e2e"})

#: The manifest statuses (R37-17), weakest first. ``pending-approval`` is
#: the human gate: a profile whose evidence derives ``supported`` but has
#: no named human approver is listed pending-approval, NEVER supported.
MANIFEST_STATUSES: Final[tuple[str, ...]] = (
    "unqualified",
    "withdrawn",
    "declared_only",
    "lab-qualified",
    "pending-approval",
    "supported",
)

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

#: The manifest's withdrawal axes (R37-17): the requalification axes PLUS
#: the release-artifact identity — a changed wheel or image identity under
#: a constant version string withdraws the profile's claim too (the
#: negative test: a template/wheel identity change under a constant version
#: string must be detected). Unpinned artifact axes fail closed on any
#: change, exactly like unpinned fingerprints.
MANIFEST_TRIGGER_AXES: Final[tuple[str, ...]] = (
    *REQUALIFICATION_AXES,
    "wheel_sha256",
    "image_digest",
)

MANIFEST_TRIGGER_AXIS_LABELS: Final[Mapping[str, str]] = {
    **REQUALIFICATION_AXIS_LABELS,
    "wheel_sha256": "changed lane wheel identity (a different wheel under the same version)",
    "image_digest": "changed server image identity",
}

_PROVIDERS: Final[frozenset[str]] = frozenset({"gitlab", "github", "azure", "*"})
_VERSION_RE: Final[re.Pattern[str]] = re.compile(r"\d+\.\d+\.\d+([ab.rc]+\d*)?")

#: Observability name for every record-validation outcome in this module
#: (R37-06): a refused strict record carries it in its error, a legacy
#: record's findings are logged under it. Nothing else emits this name.
RECORD_VALIDATION_OBSERVABILITY: Final[str] = "qualification.record_validation"

#: Observability names R37-17 is the source of: unmet (held) real-provider
#: capabilities; the withdrawal of a profile claim on a manifest trigger;
#: the release-tested artifact identity (never conflated with the deployed
#: one); the installed fingerprint the manifest judges a change against.
UNMET_CAPABILITIES_OBSERVABILITY: Final[str] = "qualification.unmet_capabilities"
WITHDRAWN_OBSERVABILITY: Final[str] = "qualification.withdrawn"
TESTED_SHA_OBSERVABILITY: Final[str] = "release.tested_sha"
INSTALLED_FINGERPRINT_OBSERVABILITY: Final[str] = "profile.installed_fingerprint"

#: The closed vocabulary of strict-schema finding kinds (R37-06, the
#: validation half of the #298 overlap). A finding's kind names WHAT rule
#: fired; its path names WHERE; its message says what was found instead.
#: R37-17 adds the conflation kinds: a sha256 in a VERSION field and a
#: version string in a HASH field are their OWN kinds (the tested sha and
#: the deployed artifact are distinct identities), and a mislabelled
#: ``upgrade.claim`` is a typed finding of its own.
VALIDATION_FINDING_KINDS: Final[tuple[str, ...]] = (
    "timestamp-not-iso",
    "hash-not-hex64",
    "version-not-semantic",
    "sha-in-version-field",
    "version-in-hash-field",
    "upgrade-claim-mislabelled",
    "fingerprint-empty-for-supported-verdict",
)

#: An ISO-8601 timestamp with a date, a time and a zone — the strict shape
#: of every ``executed_at`` field. Anything else in a timestamp field (a
#: bare date, a unix epoch, a commit sha or an image digest) is a finding.
_ISO_TIMESTAMP_RE: Final[re.Pattern[str]] = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})"
)

#: A hex64 sha256, with an optional ``sha256:`` prefix (the prefix is an
#: artifact-ref convention, not part of the digest — normalized away).
_HEX64_RE: Final[re.Pattern[str]] = re.compile(r"(?:sha256:)?([0-9a-f]{64})")

#: The loose semantic shape accepted for ``harness_version`` (a tool
#: version, not a release version): 2-4 numeric components, optional
#: ``v`` prefix and optional pre-release/build suffix. A digest or a path
#: here is a finding — a version field names a version.
_SEMVERISH_RE: Final[re.Pattern[str]] = re.compile(
    r"v?\d+(\.\d+){1,3}(-[A-Za-z0-9][A-Za-z0-9.]*)?(\+[A-Za-z0-9][A-Za-z0-9.]*)?"
)

_REFUSAL_RESOLUTION_STATUSES: Final[frozenset[str]] = frozenset(
    {"open", "in_progress", "resolved", "kept-refused"}
)

_LOGGER = logging.getLogger(__name__)


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _is_iso_timestamp(value: str) -> bool:
    """Whether *value* is a full ISO-8601 instant (date + time + zone)."""
    if not _ISO_TIMESTAMP_RE.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _is_hex256(value: str) -> bool:
    """Whether *value* is a sha256 (hex64, optional ``sha256:`` prefix)."""
    return _HEX64_RE.fullmatch(value.strip()) is not None


def _is_version_shaped(value: str) -> bool:
    """Whether *value* is version-shaped (``0.36.0``, ``v2.1.3-rc1``)."""
    return bool(_VERSION_RE.fullmatch(value) or _SEMVERISH_RE.fullmatch(value))


#: The version fields — each names a VERSION, so a sha256 in any of them is
#: the ``sha-in-version-field`` conflation (R37-17: the tested sha never
#: stands in for a version, and never for the deployed artifact identity).
_VERSION_FIELDS: Final[tuple[str, ...]] = (
    "release_version",
    "harness_version",
    "provider_version",
    "authority_contract_version",
)

#: The hash fields — each carries its OWN sha256 identity, so a
#: version-shaped value in any of them is the ``version-in-hash-field``
#: conflation (release.tested_sha ≠ the deployed artifact, assumed nowhere).
_HASH_FIELDS: Final[tuple[str, ...]] = ("closure_digest", "wheel_sha256", "image_digest")


class ProfileRecordError(Exception):
    """A profile-qualification record is unusable (bad shape, bad stamp)."""


class ProfileRecordImmutableError(ProfileRecordError):
    """An attempt to overwrite an EXISTING record file with different bytes
    (R37-17: the typed immutability error — it names the versioned
    filename; fixing evidence means landing a NEW record, never editing
    one)."""


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
    the trace ids/paths of WHAT was covered.

    Provenance is split into DISTINCT fields (R37-06, the strict schema):

    - ``executed_at`` — WHEN: an ISO-8601 UTC timestamp. The v1 schema
      defined this field as "the SHA it executed at"; the strict schema
      moves the artifact identity to ``artifact_sha256`` and lets
      :func:`validate_record` flag any record that still carries a digest
      in a timestamp field. (Legacy v1 records keep their historical bytes
      and are reported, never rewritten.)
    - ``artifact_sha256`` — WHAT ran: the commit sha, image digest or
      wheel sha256 the evidence actually executed at (hex64, an optional
      ``sha256:`` prefix is normalized); empty only on legacy records.
    """

    evidence_class: str
    capability: str
    outcome: str
    covers: str
    executed_at: str
    artifact_sha256: str = ""

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
        if self.artifact_sha256 and not _is_hex256(self.artifact_sha256):
            raise ProfileRecordError(
                "artifact_sha256 must be the hex64 sha256 of the artifact the evidence "
                "executed at (an optional sha256: prefix is fine) — never a partial "
                "digest or a foreign value"
            )

    def to_json(self) -> dict[str, object]:
        return {
            "class": self.evidence_class,
            "capability": self.capability,
            "outcome": self.outcome,
            "covers": self.covers,
            "executed_at": self.executed_at,
            "artifact_sha256": self.artifact_sha256,
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
                artifact_sha256=str(document.get("artifact_sha256", "")),
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

    R37-17: ``claim`` is the record's own ASSERTED label
    (``same-head-preservation`` / ``schema-transition`` /
    ``schema-upgrade``) — empty when the record asserts nothing and lets
    the derivation speak. When set, :func:`validate_record` checks it
    against the derived claim and a mislabel (a 027→027 canary calling
    itself a schema upgrade) is a typed finding, so an honest description
    is enforced, not merely derived.
    """

    source_schema: str
    target_schema: str
    seeded_records: tuple[str, ...] = ()
    preservation_checks: tuple[str, ...] = ()
    evidence_ref: str = ""
    claim: str = ""

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
            "claim": self.claim,
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
                claim=str(document.get("claim", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProfileRecordError(f"malformed upgrade facts: {exc}") from exc


@dataclass(frozen=True)
class RefusalResolution:
    """ONE recorded preflight refusal and its concrete resolution (R37-06).

    The refusal-resolution matrix is typed record content, not prose: each
    entry names the recorded refusal (``refusal`` — the exact observed
    refusal, e.g. "deployed control plane reports 0.28.0 while the profile
    pins the promoted release"), the CONCRETE action that resolves it
    (``action`` — a command or a bounded task, never "investigate"),
    the ``owner`` accountable for it, and the ``status`` from
    :data:`_REFUSAL_RESOLUTION_STATUSES` — a refusal that stays unmet is
    recorded ``kept-refused``, never renamed into evidence.
    """

    refusal: str
    action: str
    owner: str
    status: str

    def __post_init__(self) -> None:
        for name in ("refusal", "action", "owner"):
            if not str(getattr(self, name)).strip():
                raise ProfileRecordError(
                    f"a refusal resolution needs a non-empty {name} — an unnamed "
                    "refusal or an unowned action resolves nothing"
                )
        if self.status not in _REFUSAL_RESOLUTION_STATUSES:
            raise ProfileRecordError(
                f"refusal resolution status {self.status!r} is not one of "
                f"{sorted(_REFUSAL_RESOLUTION_STATUSES)}"
            )

    def to_json(self) -> dict[str, object]:
        return {
            "refusal": self.refusal,
            "action": self.action,
            "owner": self.owner,
            "status": self.status,
        }

    @classmethod
    def from_json(cls, document: Mapping[str, object]) -> RefusalResolution:
        try:
            return cls(
                refusal=str(document["refusal"]),
                action=str(document["action"]),
                owner=str(document["owner"]),
                status=str(document["status"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProfileRecordError(f"malformed refusal resolution: {exc}") from exc


@dataclass(frozen=True)
class TraceRecord:
    """One EXECUTED trace with its own provenance stamp (R37-17).

    A qualification record's evidence entry CLAIMS a capability in an
    evidence class; the trace record is the executed artifact behind such a
    claim — a committed JSON file under ``qualification/traces/`` stamped
    :data:`TRACE_RECORD_STAMP`. The join back to the record is by
    ``capability`` (plus ``record_id`` when the trace names the record it
    was executed for), so a trace upgrades only the capability it actually
    exercised, in the record it actually belongs to.

    ``provenance`` is the whole point — one of :data:`TRACE_PROVENANCES`:

    - ``scripted`` — authored scenario, fakes or fixtures. It STAYS
      scripted even when ``invokes_real_protocol_code`` is true: driving
      the real production-entry code against authored responses is a
      stronger scripted trace, never live provenance.
    - ``live`` — real task or recovery results against the real provider
      and model route the trace names (a live trace therefore MUST carry
      ``provider`` and ``model_route`` — that provenance is what makes it
      live). A live trace upgrades ONLY its own capability, and only in a
      record for its own provider.
    - ``refused`` — an executed observation of a refusal (the live-flow
      preflight refusal). Refusal evidence is honest and loadable, and it
      NEVER upgrades anything.
    """

    trace_id: str
    capability: str
    provenance: str
    executed_at: str
    artifact_sha256: str = ""
    record_id: str = ""
    provider: str = ""
    provider_version: str = ""
    model_route: str = ""
    invokes_real_protocol_code: bool = False
    note: str = ""

    def __post_init__(self) -> None:
        if self.provenance not in TRACE_PROVENANCES:
            raise ProfileRecordError(
                f"trace provenance {self.provenance!r} is not one of {list(TRACE_PROVENANCES)} — "
                "the provenance stamp is the tier's only source"
            )
        for name in ("trace_id", "capability", "executed_at"):
            if not str(getattr(self, name)).strip():
                raise ProfileRecordError(
                    f"a trace record needs a non-empty {name} — an executed trace "
                    "without provenance qualifies nothing"
                )
        if not _is_iso_timestamp(self.executed_at):
            raise ProfileRecordError(
                f"trace {self.trace_id!r}: executed_at must be an ISO-8601 timestamp — "
                "trace records postdate the v1 schema and get no legacy lane; the "
                "artifact identity belongs in artifact_sha256"
            )
        if self.artifact_sha256 and not _is_hex256(self.artifact_sha256):
            raise ProfileRecordError(
                f"trace {self.trace_id!r}: artifact_sha256 must be hex64 (sha256: prefix "
                "allowed) — the identity the trace executed at"
            )
        if self.provenance == "live" and not (self.provider.strip() and self.model_route.strip()):
            raise ProfileRecordError(
                f"trace {self.trace_id!r} claims live provenance without naming its "
                "provider AND model_route — real provider/model provenance is exactly "
                "what makes a trace live"
            )

    def to_json(self) -> dict[str, object]:
        return {
            "stamp": TRACE_RECORD_STAMP,
            "trace_id": self.trace_id,
            "capability": self.capability,
            "provenance": self.provenance,
            "executed_at": self.executed_at,
            "artifact_sha256": self.artifact_sha256,
            "record_id": self.record_id,
            "provider": self.provider,
            "provider_version": self.provider_version,
            "model_route": self.model_route,
            "invokes_real_protocol_code": self.invokes_real_protocol_code,
            "note": self.note,
        }

    @classmethod
    def from_json(cls, document: Mapping[str, object]) -> TraceRecord:
        stamp = document.get("stamp")
        if stamp != TRACE_RECORD_STAMP:
            raise ProfileRecordError(f"trace record stamp {stamp!r} != {TRACE_RECORD_STAMP!r}")
        try:
            return cls(
                trace_id=str(document["trace_id"]),
                capability=str(document["capability"]),
                provenance=str(document["provenance"]),
                executed_at=str(document["executed_at"]),
                artifact_sha256=str(document.get("artifact_sha256", "")),
                record_id=str(document.get("record_id", "")),
                provider=str(document.get("provider", "")),
                provider_version=str(document.get("provider_version", "")),
                model_route=str(document.get("model_route", "")),
                invokes_real_protocol_code=bool(document.get("invokes_real_protocol_code", False)),
                note=str(document.get("note", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProfileRecordError(f"malformed trace record: {exc}") from exc


def _iter_traces(
    trace_refs: Mapping[str, TraceRecord] | Sequence[TraceRecord],
) -> tuple[TraceRecord, ...]:
    """Normalize a trace mapping (id → trace) or sequence into a tuple."""
    if isinstance(trace_refs, Mapping):
        return tuple(trace_refs.values())
    return tuple(trace_refs)


def _trace_dir(root: Path) -> Path:
    return root / "qualification" / "traces"


def load_trace_records(root: Path) -> tuple[TraceRecord, ...]:
    """Every committed executed-trace record, by trace id.

    Reads ``qualification/traces/*.json``; a missing directory is simply no
    traces (the derived tiers are then all ``none`` — fail-closed, never a
    crash). A malformed trace (bad stamp, digest in ``executed_at``, live
    without provider/model route) REFUSES the whole load: trace records are
    new artifacts with no legacy lane, so a bad one is a modelling error,
    not history.
    """
    base = _trace_dir(root)
    if not base.is_dir():
        return ()
    traces = [
        TraceRecord.from_json(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(base.glob("*.json"))
    ]
    traces.sort(key=lambda trace: trace.trace_id)
    return tuple(traces)


@dataclass(frozen=True)
class ValidationFinding:
    """One typed strict-schema defect found by :func:`validate_record`.

    ``path`` is the JSON path inside the record document (e.g.
    ``evidence[0].executed_at``), ``kind`` is one of
    :data:`VALIDATION_FINDING_KINDS`, and ``message`` states what was found
    and what the field demands.
    """

    path: str
    kind: str
    message: str

    def __post_init__(self) -> None:
        if self.kind not in VALIDATION_FINDING_KINDS:
            raise ProfileRecordError(
                f"validation finding kind {self.kind!r} is not one of "
                f"{list(VALIDATION_FINDING_KINDS)} — the vocabulary is closed so a "
                "finding can never smuggle an unnamed rule"
            )
        for name in ("path", "message"):
            if not str(getattr(self, name)).strip():
                raise ProfileRecordError(f"a validation finding needs a non-empty {name}")

    def render(self) -> str:
        return f"{self.path} [{self.kind}]: {self.message}"

    def to_json(self) -> dict[str, object]:
        return {"path": self.path, "kind": self.kind, "message": self.message}


def _hash_field_message(field: str, value: str) -> str:
    """The typed message for a bad value in a hash field, by its shape."""
    if _is_version_shaped(value):
        return (
            f"{value!r} is a VERSION string in a hash field — {field} carries its own "
            "sha256 artifact identity; a version here conflates the tested identity "
            f"with the deployed artifact ({TESTED_SHA_OBSERVABILITY} assumed nowhere)"
        )
    return f"{value[:16]!r}… is not a hex64 sha256 (sha256: prefix allowed)"


#: The upgrade-claim labels a record may ASSERT (R37-17). ``schema-upgrade``
#: is accepted as a synonym of ``schema-transition`` (both appear in the
#: historical prose); the derived vocabulary stays the two-kind one.
_ASSERTED_CLAIM_LABELS: Final[Mapping[str, str]] = {
    "same-head-preservation": "same-head-preservation",
    "schema-transition": "schema-transition",
    "schema-upgrade": "schema-transition",
}


def _derive_upgrade_claim(facts: UpgradeFacts) -> UpgradeClaim | None:
    """The honest claim for *facts* alone (no record needed)."""
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


def _upgrade_claim_finding(document: Mapping[str, object]) -> ValidationFinding | None:
    """The typed finding for an upgrade claim that mislabels its facts.

    None when the record asserts no claim (the derivation speaks for it) or
    the assertion matches the derivation. Everything else is a mislabel:
    the enforced honesty of "a 027→027 canary is never labeled a schema
    upgrade" and "a transition that seeded nothing makes NO claim at all".
    """
    upgrade = document.get("upgrade")
    if not isinstance(upgrade, Mapping):
        return None
    asserted = str(upgrade.get("claim", "") or "").strip()
    if not asserted:
        return None
    normalized = _ASSERTED_CLAIM_LABELS.get(asserted)
    if normalized is None:
        return ValidationFinding(
            path="upgrade.claim",
            kind="upgrade-claim-mislabelled",
            message=(
                f"{asserted!r} is not an upgrade-claim label — the vocabulary is "
                f"{sorted(set(_ASSERTED_CLAIM_LABELS))} (or empty, letting the "
                "derivation speak)"
            ),
        )
    try:
        facts = UpgradeFacts.from_json(upgrade)
    except ProfileRecordError:
        return None  # shape defects are the loader's refusal, not findings
    derived = _derive_upgrade_claim(facts)
    if derived is None:
        return ValidationFinding(
            path="upgrade.claim",
            kind="upgrade-claim-mislabelled",
            message=(
                f"the upgrade facts make NO claim (seeded {len(facts.seeded_records)} "
                f"record(s), executed {len(facts.preservation_checks)} check(s)) — an "
                "upgrade claim must name what a real deployment held and what was "
                f"actually checked; asserting {asserted!r} mislabels empty facts"
            ),
        )
    if normalized != derived.kind:
        detail = (
            f"a {facts.source_schema}→{facts.target_schema} run is a "
            f"{derived.kind}, never a {normalized}"
            if derived.kind == "same-head-preservation"
            else f"an actual {facts.source_schema}→{facts.target_schema} transition is not "
            f"a {normalized}"
        )
        return ValidationFinding(
            path="upgrade.claim",
            kind="upgrade-claim-mislabelled",
            message=(
                f"asserted {asserted!r} but the executed facts derive "
                f"{derived.kind} — {detail}; upgrade claims are derived, never asserted"
            ),
        )
    return None


def validate_record(document: Mapping[str, object]) -> list[ValidationFinding]:
    """Validate one record document against the STRICT schema (R37-06).

    Typed checks, weakest-to-strongest field discipline:

    - ``timestamp-not-iso`` — every ``evidence[*].executed_at`` must be a
      full ISO-8601 instant. A commit sha, wheel sha256 or image digest in
      a timestamp field is the exact historical defect this flags: the
      finding names the offending value so the reader sees it is a digest.
    - ``hash-not-hex64`` — ``closure_digest``, ``wheel_sha256``,
      ``image_digest`` and every ``evidence[*].artifact_sha256`` must be
      hex64 (an optional ``sha256:`` prefix is normalized), or empty where
      the field is optional-by-design (image-only releases keep an empty
      ``wheel_sha256``).
    - ``version-not-semantic`` — ``release_version`` must match the
      release-version grammar; ``harness_version`` the looser tool-version
      grammar (:data:`_SEMVERISH_RE`).
    - ``sha-in-version-field`` (R37-17) — a hex64 value inside a VERSION
      field (:data:`_VERSION_FIELDS`): the tested sha is not a version and
      never stands in for one; each identity gets its own field.
    - ``version-in-hash-field`` (R37-17) — a version-shaped value inside a
      HASH field: a version string where the artifact's own sha256 belongs
      is exactly the release.tested_sha vs deployed-artifact conflation.
    - ``upgrade-claim-mislabelled`` (R37-17) — an ASSERTED
      ``upgrade.claim`` that contradicts the derived claim: a same-head
      N→N run labelled a schema upgrade, a transition labelled a
      preservation, or any label on facts that seeded/checked nothing.
    - ``fingerprint-empty-for-supported-verdict`` — a record whose DERIVED
      verdict (:func:`derive_verdict`) claims ``supported`` must pin every
      requalification axis (:data:`REQUALIFICATION_AXES`); an empty axis on
      a supported claim is a finding naming the axis.

    The function never raises on field defects — it REPORTS them (the
    loader decides refusal vs legacy reporting). It assumes the document
    already parses as a record (stamp/shape); :func:`load_profile_records`
    enforces that part first.
    """
    findings: list[ValidationFinding] = []
    evidence = cast("Sequence[Mapping[str, object]]", document.get("evidence") or ())
    for index, raw in enumerate(evidence):
        if not isinstance(raw, Mapping):
            continue  # a non-object entry is a shape error, caught by from_json
        executed_at = str(raw.get("executed_at", ""))
        if executed_at and not _is_iso_timestamp(executed_at):
            findings.append(
                ValidationFinding(
                    path=f"evidence[{index}].executed_at",
                    kind="timestamp-not-iso",
                    message=(
                        f"{executed_at[:16]!r}… is not an ISO-8601 timestamp — it looks "
                        "like a digest/commit in a timestamp field; the strict schema "
                        "keeps the artifact identity in artifact_sha256 and WHEN the "
                        "evidence ran in executed_at"
                    ),
                )
            )
        artifact = str(raw.get("artifact_sha256", ""))
        if artifact and not _is_hex256(artifact):
            findings.append(
                ValidationFinding(
                    path=f"evidence[{index}].artifact_sha256",
                    kind=(
                        "version-in-hash-field"
                        if _is_version_shaped(artifact)
                        else "hash-not-hex64"
                    ),
                    message=_hash_field_message(f"evidence[{index}].artifact_sha256", artifact),
                )
            )
    for field in _HASH_FIELDS:
        value = str(document.get(field, "") or "")
        if value and not _is_hex256(value):
            findings.append(
                ValidationFinding(
                    path=field,
                    kind="version-in-hash-field" if _is_version_shaped(value) else "hash-not-hex64",
                    message=_hash_field_message(field, value),
                )
            )
    for field in _VERSION_FIELDS:
        value = str(document.get(field, "") or "")
        if not value:
            continue
        if _is_hex256(value):
            findings.append(
                ValidationFinding(
                    path=field,
                    kind="sha-in-version-field",
                    message=(
                        f"{value[:16]!r}… is a sha256 in a VERSION field — {field} names a "
                        "version; the artifact identity belongs in its own hash field "
                        f"({TESTED_SHA_OBSERVABILITY} is never the deployed artifact, "
                        "assumed nowhere)"
                    ),
                )
            )
            continue
        if field == "release_version" and not _VERSION_RE.fullmatch(value):
            findings.append(
                ValidationFinding(
                    path="release_version",
                    kind="version-not-semantic",
                    message=f"{value!r} is not a semantic release version (X.Y.Z)",
                )
            )
        elif field == "harness_version" and not _SEMVERISH_RE.fullmatch(value):
            findings.append(
                ValidationFinding(
                    path="harness_version",
                    kind="version-not-semantic",
                    message=(
                        f"{value[:16]!r}… is not a tool version — a version field "
                        "names a version; an artifact identity belongs in a hash field"
                    ),
                )
            )
    upgrade_finding = _upgrade_claim_finding(document)
    if upgrade_finding is not None:
        findings.append(upgrade_finding)
    # Fingerprints under a supported claim: derive the verdict from the
    # document's own evidence (verdicts are never stored).
    try:
        record = ProfileQualificationRecord.from_json(document)
    except ProfileRecordError:
        return findings  # shape defects are the loader's refusal, not findings
    if derive_verdict(record) == "supported":
        for axis in REQUALIFICATION_AXES:
            if not str(getattr(record, axis)).strip():
                findings.append(
                    ValidationFinding(
                        path=axis,
                        kind="fingerprint-empty-for-supported-verdict",
                        message=(
                            f"the record's evidence derives a supported verdict but pins "
                            f"no {axis} — an unpinned axis cannot absorb a change "
                            f"({REQUALIFICATION_AXIS_LABELS[axis]})"
                        ),
                    )
                )
    return findings


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

    R37-06 fields: ``legacy`` marks a record written under the v1 schema
    (``executed_at`` as a sha, no ``artifact_sha256``); legacy records are
    validated and REPORTED (:func:`validate_record`) but load as history —
    history is never rewritten to satisfy a newer schema. A record that
    declares ``legacy: false`` opts into the strict schema and is REFUSED
    at load on any finding. ``refusal_resolution`` carries the typed
    preflight-refusal matrix (:class:`RefusalResolution`).
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
    legacy: bool = True
    refusal_resolution: tuple[RefusalResolution, ...] = ()

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
        for resolution in self.refusal_resolution:
            if not isinstance(resolution, RefusalResolution):
                raise ProfileRecordError(
                    "refusal_resolution entries are RefusalResolution values — the "
                    "matrix is typed content, never free JSON"
                )
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
            "legacy": self.legacy,
            "refusal_resolution": [resolution.to_json() for resolution in self.refusal_resolution],
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
                legacy=bool(document.get("legacy", True)),
                refusal_resolution=tuple(
                    RefusalResolution.from_json(entry)
                    for entry in cast(
                        "Sequence[Mapping[str, object]]", document.get("refusal_resolution", ())
                    )
                ),
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


# ---------------------------------------------------------------------------
# R37-17: evidence tiers DERIVED from executed trace records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityTier:
    """One required capability's tier, derived from its executed traces.

    ``tier`` is one of :data:`EVIDENCE_TIERS` and comes ONLY from the
    provenance stamps of the :class:`TraceRecord` values that reference
    this capability in this record; ``trace_ids`` names them;
    ``reasons`` carry every cap, skip and refusal observed on the way.
    """

    capability: str
    tier: str
    trace_ids: tuple[str, ...]
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.tier not in EVIDENCE_TIERS:
            raise ProfileRecordError(
                f"tier {self.tier!r} is not one of {list(EVIDENCE_TIERS)} — the tier "
                "vocabulary is closed so a tier can never smuggle a stronger claim"
            )
        if not str(self.capability).strip():
            raise ProfileRecordError("a capability tier needs a non-empty capability")

    def to_json(self) -> dict[str, object]:
        return {
            "capability": self.capability,
            "tier": self.tier,
            "traces": list(self.trace_ids),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class EvidenceTierReport:
    """The per-capability trace-derived tiers plus the HOLD reasons.

    ``holds`` (observed under :data:`UNMET_CAPABILITIES_OBSERVABILITY`)
    fire when a capability whose label promises a real provider is not
    backed by a live trace, or when an evidence entry claims
    live-provider-or-stronger but the executed traces cap the tier below
    live — the negative arm: a fixture-only record with a real-provider
    capability label is HELD, never promoted.
    """

    per_capability: tuple[CapabilityTier, ...]
    holds: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "per_capability": [tier.to_json() for tier in self.per_capability],
            "holds": list(self.holds),
        }


def derive_evidence_tier(
    record: ProfileQualificationRecord,
    trace_refs: Mapping[str, TraceRecord] | Sequence[TraceRecord],
) -> EvidenceTierReport:
    """Derive each required capability's tier from the EXECUTED traces.

    The rules (R37-17 — tiers come from trace provenance, never labels):

    - a ``scripted`` trace keeps the capability ``scripted`` even when it
      invokes real protocol code — driving real code against authored
      responses is a stronger scripted trace, never live provenance;
    - a ``live`` trace (real provider + model route provenance) upgrades
      ONLY its own capability, and only in a record for its own provider —
      a github live trace never upgrades a gitlab record;
    - a ``refused`` trace (an executed observation of a refusal) NEVER
      upgrades anything — it is honest evidence of a gap, recorded as such;
    - an entry with no trace, or a capability with no passing entry, holds
      the tier at ``none`` (fail-closed: a tier is derived only from
      executed traces);
    - HOLD: a capability in :data:`LIVE_REQUIRED_CAPABILITIES` whose tier
      is not ``live`` (a fixture-only record with a real-provider label),
      and an entry claiming live-provider-or-stronger whose traces cap the
      tier below live — the trace record's provenance is the source of
      truth; a label never upgrades a scripted trace.
    """
    traces = _iter_traces(trace_refs)
    tiers: list[CapabilityTier] = []
    holds: list[str] = []
    for capability in record.capabilities:
        reasons: list[str] = []
        entries = [entry for entry in record.evidence if entry.capability == capability]
        passing = [entry for entry in entries if entry.outcome == "pass"]
        for entry in entries:
            if entry.outcome == "skip":
                reasons.append(
                    f"capability {capability!r}: evidence ({entry.evidence_class}) SKIPPED — "
                    "recorded as a skip, never an upgrade"
                )
            elif entry.outcome == "fail":
                reasons.append(
                    f"capability {capability!r}: evidence ({entry.evidence_class}) FAILED — "
                    "the failure is on record, never an upgrade"
                )
        if not entries:
            reasons.append(
                f"capability {capability!r}: no evidence entry — an uncovered capability "
                "has no tier"
            )
        elif not passing:
            reasons.append(
                f"capability {capability!r}: no passing evidence entry — the tier is none"
            )
        applicable: list[TraceRecord] = []
        for trace in traces:
            if trace.capability != capability:
                continue
            if trace.record_id and trace.record_id != record.record_id:
                continue  # the trace was executed for another record — history is per-record
            if (
                trace.provenance == "live"
                and trace.provider
                and record.provider not in ("*", trace.provider)
            ):
                reasons.append(
                    f"live trace {trace.trace_id!r} names provider {trace.provider!r} — a "
                    "live trace upgrades only its own provider's capability, never this "
                    f"record's ({record.provider!r})"
                )
                continue
            applicable.append(trace)
        tier = "none"
        trace_ids: list[str] = []
        if passing:
            for trace in applicable:
                trace_ids.append(trace.trace_id)
                if trace.provenance == "live":
                    contribution = "live"
                elif trace.provenance == "scripted":
                    contribution = "scripted"
                    reasons.append(
                        f"trace {trace.trace_id!r} is scripted provenance — it stays "
                        "scripted even when it invokes real protocol code "
                        f"(invokes_real_protocol_code={trace.invokes_real_protocol_code}); "
                        "authored fixtures never carry live provenance"
                    )
                else:  # refused
                    contribution = "none"
                    reasons.append(
                        f"trace {trace.trace_id!r} records a REFUSAL — refusal evidence "
                        "never upgrades anything"
                    )
                if _TIER_RANK[contribution] > _TIER_RANK[tier]:
                    tier = contribution
            if not applicable:
                reasons.append(
                    f"capability {capability!r}: no executed trace record references it — "
                    "tiers derive ONLY from executed traces (fail-closed)"
                )
            strongest_class = max(_CLASS_TIER[entry.evidence_class] for entry in passing)
            if strongest_class >= 3 and tier != "live":
                holds.append(
                    f"capability {capability!r}: evidence claims live-provider-or-stronger "
                    f"but the executed traces cap the tier at {tier!r} — the trace "
                    "record's provenance is the source of truth; a label never upgrades "
                    "a scripted trace"
                )
        else:
            trace_ids.extend(trace.trace_id for trace in applicable)
        if capability in LIVE_REQUIRED_CAPABILITIES and tier != "live":
            holds.append(
                f"{UNMET_CAPABILITIES_OBSERVABILITY}: capability {capability!r} is a "
                f"real-provider capability — the trace-derived tier is {tier!r}; a "
                "fixture-only record carrying a real-provider label is HELD, never promoted"
            )
        tiers.append(
            CapabilityTier(
                capability=capability, tier=tier, trace_ids=tuple(trace_ids), reasons=tuple(reasons)
            )
        )
    return EvidenceTierReport(per_capability=tuple(tiers), holds=tuple(holds))


def _axis_triggers(
    record: ProfileQualificationRecord,
    changes: Mapping[str, str],
    axes: tuple[str, ...],
    labels: Mapping[str, str],
) -> tuple[str, ...]:
    triggers: list[str] = []
    for axis in axes:
        current = changes.get(axis)
        if current is None:
            continue
        pinned = str(getattr(record, axis))
        if not pinned:
            triggers.append(
                f"{labels[axis]}: this record pins no {axis} but the current value is "
                f"{current!r} — an unpinned axis cannot absorb a change; pin the axis "
                "and requalify"
            )
        elif str(current) != pinned:
            triggers.append(
                f"{axis} — {labels[axis]}: record pins {pinned!r}, "
                f"current value {current!r} — requalification required"
            )
    return tuple(triggers)


def requalification_triggers(
    record: ProfileQualificationRecord, changes: Mapping[str, str]
) -> tuple[str, ...]:
    """The NAMED triggers for every changed axis this record must answer to.

    Per axis in :data:`REQUALIFICATION_AXES`: a change naming a DIFFERENT
    value than the record pins is a trigger; a change naming an axis the
    record left UNPINNED is a trigger too (fail-closed — an unpinned axis
    never silently absorbs a change). Unnamed axes trigger nothing.
    """
    return _axis_triggers(record, changes, REQUALIFICATION_AXES, REQUALIFICATION_AXIS_LABELS)


def manifest_trigger_triggers(
    record: ProfileQualificationRecord, changes: Mapping[str, str]
) -> tuple[str, ...]:
    """The manifest's withdrawal triggers (R37-17) — the requalification
    axes PLUS the release-artifact identity (:data:`MANIFEST_TRIGGER_AXES`).

    A changed wheel or image identity — including a different wheel under a
    CONSTANT version string — withdraws the profile's claim in the
    supported-profiles manifest (:func:`build_supported_profiles`), exactly
    like a changed dependency fingerprint or provider behavior. Same
    fail-closed rule: a change naming an axis the record left unpinned is a
    trigger (an image-only record never silently absorbs a wheel identity).
    """
    return _axis_triggers(record, changes, MANIFEST_TRIGGER_AXES, MANIFEST_TRIGGER_AXIS_LABELS)


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
    R37-17: when the record ASSERTS an ``upgrade.claim`` label,
    :func:`validate_record` checks it against this derivation — a
    mislabelled claim (a 027→027 canary calling itself a schema upgrade)
    is a typed finding.
    """
    facts = record.upgrade
    if facts is None:
        return None
    return _derive_upgrade_claim(facts)


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
    record file is refused with :class:`ProfileRecordImmutableError` — the
    typed error naming the VERSIONED FILENAME — unless ``replace=True``
    (the same discipline as the release archive; a customer-acceptance
    record is never regenerated from fixtures, AT-12).
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
            raise ProfileRecordImmutableError(
                f"{path.name} already holds DIFFERENT evidence — profile records are "
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

    R37-06 strict validation: every document is validated
    (:func:`validate_record`). A record that opted into the strict schema
    (``legacy: false``) with ANY finding refuses the whole load with the
    typed diagnostics under the observability name
    :data:`RECORD_VALIDATION_OBSERVABILITY`. A legacy record (``legacy:
    true``, the default for the v1 store) with findings is LOADED as
    history — its findings are reported (logged) under the same name, so
    a legacy-malformed record is visible without rewriting it. The choice
    is deliberate: rewriting historical records to satisfy a newer schema
    would destroy exactly the provenance the store exists to keep.
    """
    base = _store_dir(root)
    if not base.is_dir():
        return ()
    records: list[ProfileQualificationRecord] = []
    for path in sorted(base.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        record = ProfileQualificationRecord.from_json(document)
        findings = validate_record(document)
        if findings and not record.legacy:
            raise ProfileRecordError(
                f"{RECORD_VALIDATION_OBSERVABILITY}: {path.name} REFUSED — the record "
                "declares the strict schema (legacy: false) and carries "
                f"{len(findings)} finding(s): "
                + "; ".join(finding.render() for finding in findings)
            )
        if findings:
            _LOGGER.warning(
                "%s: %s is legacy-marked and malformed — loaded as history, never "
                "promotable as-is (%s)",
                RECORD_VALIDATION_OBSERVABILITY,
                path.name,
                "; ".join(finding.render() for finding in findings),
            )
        records.append(record)
    records.sort(key=lambda record: (version_key(record.release_version), record.record_id))
    return tuple(records)


# ---------------------------------------------------------------------------
# R37-17: human approvals, the typed store, the supported-profiles manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileApproval:
    """One HUMAN approval of one profile's support claim (R37-17).

    Approvals live in their own document (``qualification/
    profile-approvals.json``, stamp :data:`APPROVAL_STAMP``) — written by a
    person, never by evidence collection — because a supported verdict is
    a human act on top of executed evidence, not another evidence class.
    ``approved_by`` names the approver, ``approved_at`` is the ISO-8601
    instant of the decision, and ``note`` may state its scope.
    """

    profile: str
    approved_by: str
    approved_at: str
    note: str = ""

    def __post_init__(self) -> None:
        for name in ("profile", "approved_by"):
            if not str(getattr(self, name)).strip():
                raise ProfileRecordError(
                    f"a profile approval needs a non-empty {name} — an anonymous "
                    "approval gates nothing"
                )
        if not _is_iso_timestamp(self.approved_at):
            raise ProfileRecordError(
                "a profile approval needs an ISO-8601 approved_at — the decision is an "
                "event in time, and a digest or a bare date here is the same conflation "
                "the record schema refuses"
            )

    def to_json(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "note": self.note,
        }


def load_profile_approvals(root: Path) -> tuple[ProfileApproval, ...]:
    """The human approvals for profile support claims, oldest first.

    ``qualification/profile-approvals.json``; a MISSING file is simply no
    approvals (every profile then renders ``pending-approval`` at best —
    the human gate holds). A malformed approval document REFUSES the load:
    it is small, human-maintained and new, so there is no legacy lane.
    """
    path = root / "qualification" / "profile-approvals.json"
    if not path.is_file():
        return ()
    document = json.loads(path.read_text(encoding="utf-8"))
    stamp = document.get("stamp")
    if stamp != APPROVAL_STAMP:
        raise ProfileRecordError(f"approval document stamp {stamp!r} != {APPROVAL_STAMP!r}")
    approvals = [
        ProfileApproval(
            profile=str(entry["profile"]),
            approved_by=str(entry["approved_by"]),
            approved_at=str(entry["approved_at"]),
            note=str(entry.get("note", "")),
        )
        for entry in cast("Sequence[Mapping[str, object]]", document.get("approvals", ()))
    ]
    approvals.sort(key=lambda approval: (approval.approved_at, approval.profile))
    return tuple(approvals)


class ProfileRecordStore:
    """A typed, append-only view over ``qualification/`` (R37-17).

    Reads: :func:`load_profile_records` (every record, oldest release
    first) plus per-profile queries — ``history(profile)`` (every record
    ever landed for the profile, oldest first: historical records stay
    separately queryable after new ones), ``latest(profile)`` and
    ``record(record_id)`` (an older record is still loadable by id). Writes
    go through :func:`write_profile_record`: overwriting an existing
    record file with different bytes raises
    :class:`ProfileRecordImmutableError` naming the versioned filename.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def load(self) -> tuple[ProfileQualificationRecord, ...]:
        return load_profile_records(self.root)

    def write(self, record: ProfileQualificationRecord, *, replace: bool = False) -> Path:
        return write_profile_record(self.root, record, replace=replace)

    def history(self, profile: str) -> tuple[ProfileQualificationRecord, ...]:
        """Every record ever landed for *profile*, oldest release first."""
        return tuple(record for record in self.load() if record.profile == profile)

    def latest(self, profile: str) -> ProfileQualificationRecord | None:
        records = self.history(profile)
        return records[-1] if records else None

    def record(self, record_id: str) -> ProfileQualificationRecord | None:
        """One record by its exact id — including superseded history."""
        return next((record for record in self.load() if record.record_id == record_id), None)

    def load_traces(self) -> tuple[TraceRecord, ...]:
        return load_trace_records(self.root)

    def load_approvals(self) -> tuple[ProfileApproval, ...]:
        return load_profile_approvals(self.root)


@dataclass(frozen=True)
class SupportedProfileEntry:
    """One profile's row in the supported-profiles manifest (R37-17).

    ``derived_verdict`` is what the evidence alone supports
    (:func:`evaluate_record`); ``status`` is the manifest's verdict AFTER
    the gates: a trigger withdrawal (``withdrawn``, under
    :data:`WITHDRAWN_OBSERVABILITY`), the human gate
    (``pending-approval`` — ``supported`` REQUIRES ``human_approved_by``),
    or the derived verdict itself. ``tiers`` are the trace-derived evidence
    tiers per capability; ``limitations`` state the refusal-resolution
    matrix, the unpinned axes and every hold; ``requalification`` renders
    the pinned identity of every withdrawal axis
    (:data:`MANIFEST_TRIGGER_AXES`, observed under
    :data:`INSTALLED_FINGERPRINT_OBSERVABILITY`).
    """

    profile: str
    record_id: str
    release_version: str
    provider: str
    derived_verdict: str
    status: str
    human_approved_by: str
    tiers: tuple[CapabilityTier, ...]
    limitations: tuple[str, ...]
    requalification: tuple[str, ...]
    withdrawn: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.status not in MANIFEST_STATUSES:
            raise ProfileRecordError(
                f"manifest status {self.status!r} is not one of {list(MANIFEST_STATUSES)}"
            )
        if self.status == "supported" and not self.human_approved_by.strip():
            raise ProfileRecordError(
                "a supported manifest entry carries human_approved_by — the human gate "
                "is structural, never bypassed by constructing the entry directly"
            )

    def to_json(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "record_id": self.record_id,
            "release_version": self.release_version,
            "provider": self.provider,
            "derived_verdict": self.derived_verdict,
            "status": self.status,
            "human_approved_by": self.human_approved_by,
            "evidence_tiers": [tier.to_json() for tier in self.tiers],
            "limitations": list(self.limitations),
            "requalification_triggers": list(self.requalification),
            "withdrawn": list(self.withdrawn),
        }


@dataclass(frozen=True)
class SupportedProfilesManifest:
    """The derived, human-gated view over the record store (R37-17).

    Built ONLY by :func:`build_supported_profiles`; the stamp
    (:data:`MANIFEST_STAMP`) rides ``to_json`` because the manifest is a
    VIEW, re-derived on every read — never committed beside the records it
    judges (a committed manifest could go stale and then lie).
    """

    entries: tuple[SupportedProfileEntry, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "stamp": MANIFEST_STAMP,
            "profiles": [entry.to_json() for entry in self.entries],
        }


def build_supported_profiles(
    store: ProfileRecordStore | Path,
    *,
    changes: Mapping[str, str] | None = None,
    root: Path | None = None,
    trace_refs: Mapping[str, TraceRecord] | Sequence[TraceRecord] | None = None,
    approvals: Mapping[str, ProfileApproval] | None = None,
) -> SupportedProfilesManifest:
    """Build the supported-profiles manifest from the executed evidence.

    Per profile (only each profile's LATEST record is judged — history
    stays queryable but never wins), the manifest states:

    - the DERIVED verdict (:func:`evaluate_record`, archive references
      checked against the store root — a deleted reference degrades);
    - the trace-derived evidence tiers (:func:`derive_evidence_tier`);
    - the withdrawal view: any fired manifest trigger (a changed dependency
      fingerprint / template / wheel or image identity / provider behavior,
      :func:`manifest_trigger_triggers`) withdraws the profile's claim —
      ``status: withdrawn`` with every trigger named;
    - the HUMAN gate: a derived ``supported`` verdict without a matching
      :class:`ProfileApproval` renders ``pending-approval`` — NEVER
      ``supported``; holds (unmet real-provider capabilities) cap the same
      way. Approvals come from ``qualification/profile-approvals.json``
      unless passed explicitly.

    The manifest never mutates the store: it is a view over frozen records
    plus the current world (``changes``), computed on every read.
    """
    typed_store = store if isinstance(store, ProfileRecordStore) else ProfileRecordStore(store)
    archive_root = typed_store.root if root is None else root
    traces = _iter_traces(trace_refs) if trace_refs is not None else typed_store.load_traces()
    approval_map: Mapping[str, ProfileApproval] = (
        approvals
        if approvals is not None
        else {approval.profile: approval for approval in typed_store.load_approvals()}
    )
    entries: list[SupportedProfileEntry] = []
    for record in latest_record_per_profile(typed_store.load()):
        report = evaluate_record(record, root=archive_root)
        tier_report = derive_evidence_tier(record, traces)
        approval = approval_map.get(record.profile)
        approved_by = approval.approved_by if approval is not None else ""
        fired = manifest_trigger_triggers(record, changes) if changes is not None else ()
        limitations: list[str] = [
            f"refusal-resolution [{resolution.status}]: {resolution.refusal} → "
            f"{resolution.action} (owner: {resolution.owner})"
            for resolution in record.refusal_resolution
        ]
        limitations.extend(tier_report.holds)
        limitations.extend(
            f"unpinned {axis} — an unpinned axis cannot absorb a change "
            f"({MANIFEST_TRIGGER_AXIS_LABELS[axis]})"
            for axis in MANIFEST_TRIGGER_AXES
            if not str(getattr(record, axis)).strip()
        )
        if fired:
            status = "withdrawn"
            withdrawn = tuple(f"{WITHDRAWN_OBSERVABILITY}: {trigger}" for trigger in fired)
            limitations.extend(withdrawn)
        elif report.verdict == "supported" and not approved_by:
            status = "pending-approval"
            withdrawn = ()
            limitations.append(
                "human approval absent — a supported verdict in the manifest requires a "
                "named approver (qualification/profile-approvals.json); listed "
                "pending-approval, never supported"
            )
        elif report.verdict == "supported" and tier_report.holds:
            status = "pending-approval"
            withdrawn = ()
            limitations.append(
                "held — unmet real-provider capabilities; never supported while held"
            )
        else:
            status = report.verdict
            withdrawn = ()
        entries.append(
            SupportedProfileEntry(
                profile=record.profile,
                record_id=record.record_id,
                release_version=record.release_version,
                provider=record.provider,
                derived_verdict=report.verdict,
                status=status,
                human_approved_by=approved_by,
                tiers=tier_report.per_capability,
                limitations=tuple(limitations),
                requalification=tuple(
                    f"{INSTALLED_FINGERPRINT_OBSERVABILITY}: {axis} = "
                    f"{str(getattr(record, axis)) or 'UNPINNED'} — "
                    f"{MANIFEST_TRIGGER_AXIS_LABELS[axis]}"
                    for axis in MANIFEST_TRIGGER_AXES
                ),
                withdrawn=withdrawn,
            )
        )
    return SupportedProfilesManifest(entries=tuple(entries))


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


def render_capabilities(
    records: tuple[ProfileQualificationRecord, ...],
    trace_refs: Mapping[str, TraceRecord] | Sequence[TraceRecord] = (),
) -> str:
    """The per-profile verdict table — derived, never self-declared.

    Every verdict comes from :func:`derive_verdict` over the committed
    records' evidence classes; runtime proof is never inferred from a test
    filename or a label. The ``Tiers`` column (R37-17) is the
    trace-derived evidence tier per required capability
    (:func:`derive_evidence_tier`) — ``none`` means no executed trace
    record references the capability, which is a gap, not an accident of
    formatting.
    """
    traces = _iter_traces(trace_refs)
    rows = [
        "| Profile | Provider | Release | Verdict | Strongest evidence | "
        "Tiers (executed traces) | Upgrade claim |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for record in records:
        claim = upgrade_claim(record)
        claim_text = (
            f"{claim.kind} {claim.source_schema}→{claim.target_schema}"
            if claim is not None
            else "—"
        )
        tier_report = derive_evidence_tier(record, traces)
        tiers_text = "; ".join(
            f"{tier.capability}: {tier.tier}" for tier in tier_report.per_capability
        )
        rows.append(
            f"| {record.profile} | {record.provider} | v{record.release_version} "
            f"| {derive_verdict(record)} | {_limiting_evidence(record)} "
            f"| {tiers_text} | {claim_text} |"
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
    root = Path(args.root)
    records = load_profile_records(root)
    if not records:
        print(
            "profile-qualification capabilities: no committed records under qualification/records/",
            file=sys.stderr,
        )
        return 1
    sys.stdout.write(render_capabilities(records, load_trace_records(root)) + "\n")
    return 0


def _cmd_manifest(args: argparse.Namespace) -> int:
    manifest = build_supported_profiles(Path(args.root))
    sys.stdout.write(render_json(manifest.to_json()))
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

    manifest = sub.add_parser(
        "manifest",
        help=(
            "print the supported-profiles manifest (forge.profile.manifest/1): derived "
            "verdicts, trace-derived tiers, limitations, requalification triggers and "
            "the human-approval gate"
        ),
    )
    manifest.add_argument("--root", default=".", type=Path)
    manifest.set_defaults(func=_cmd_manifest)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ProfileRecordError as exc:
        print(f"profile-qualification: REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
