"""R36-14 — bind independent verification to the EXACT candidate and world.

Candidate integrity and harness success are necessary but different from
acceptance evidence. The collector binds the candidate to the active
generation (R36-01) and the envelope binds dispatch identity (R36-09);
this module binds the VERDICT to what actually got tested:

- :class:`VerificationSubject` — the frozen identity a verdict names:
  the candidate digest (the collector's diff digest over the exact
  ``candidate.diff`` bytes), the source OID (the published review head),
  the tested OID where the provider tests a synthetic merge (recorded
  SEPARATELY — the verdict binds the provider-verified sha, the subject
  remembers which candidate that sha answers for), the generation
  identity the diff was collected from, the plan/revision digest and the
  tested environment/profile digest. Constructed at publication/
  verification time and stamped into the verdict evidence as
  ``verification.subject_identity``.
- :func:`subject_freshness` — the freshness gate: a passed record whose
  subject does not bind the CURRENT candidate renders ``stale``, never
  ``verified_ready``; a record without a subject is honestly
  ``unknown`` (the ``verification.freshness_unknown`` observability
  token) so pre-R36-14 rows keep their existing meaning.
- :class:`RequiredReportInventory` — the expected-report set frozen
  WITH the work contract (built from the qualification
  :class:`~forge.adaptive.qualification.ExpectedReports` machinery via
  :func:`forge.adaptive.qualification.freeze_report_inventory`). At
  verdict time the observed reports reconcile against it: a MISSING
  report, a SKIPPED required check or a report from an OLDER attempt
  (subject-digest mismatch on the report) can never produce
  ``verified_ready``. The file-based TRX world reuses
  :func:`forge.adaptive.qualification.reconcile_reports` — a
  deliberately failing secondary test project cannot disappear behind a
  passing first report.
- :func:`classify_repair_failure` / :class:`InfraRetryBudget` /
  :class:`InfraRetryLedger` — infrastructure-PREREQUISITE failures
  (runner unavailable, report transport error) classify as
  ``infrastructure``, NOT code defects: they never consume the bounded
  code-repair iterations blindly; they ride their own bounded distinct
  budget (the ``repair.failure_class`` observability token).
- :func:`harness_green_verifies` — the standing assertion that a green
  harness job ALONE never satisfies independent verification; the
  required-checks positive proof over the frozen spec stays the only
  ``verified_ready`` source.
- :class:`BoundEvidence` / :func:`invalidate_for_environment_change` —
  applicability invalidation in the verification_sets style: a changed
  tested-environment/profile digest invalidates ONLY the evidence that
  claimed the previous digest; history is retained for audit.

R37-04 (#285) — the STRICT report-inventory contract
(``forge.verification.report-inventory/2``): observed reports match a
FROZEN obligation row on the full identity (subject, report path,
bundle digest, producer), never on the test-project NAME alone; a
report without a subject identity is a typed ``unbound`` outcome, never
satisfaction; attempts order NUMERICALLY (native integer-shaped ids,
with the persisted monotonic ``attempt_ordinal`` recorded at ingest as
the fallback); duplicate report keys with contradictory outcomes are a
``duplicate_report_conflict``; and a passed report whose required
command exited nonzero combines to ``failed`` with the exit recorded.
The schema/1 inventory keeps its approved weaker (legacy) reading.

R37-05 (#286) — the COMPLETE applicability contract:
:func:`subject_freshness` compares the FULL typed subject (candidate
digest, source and base OIDs, tested OID, revision digest, environment
when present) — missing strict fields render ``unknown``, never a
looser success — and :func:`applicability` is the ONE pure reuse
decision both the service finalization and the operator rendering
route through: an editorial plan revision PRESERVES applicability
(naming the inputs that stayed equivalent) while a changed executable
source, base, tested revision, required contract or environment
invalidates it with the moved inputs named. Superseded proof is
archived with its supersession reason, never deleted.

Pure stdlib, frozen data throughout — a verification binding must never
mutate under the verdict that cites it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge.adaptive.qualification import (
    REPORT_INVENTORY_SCHEMA,
    ExpectedReports,
    ReportReconciliation,
    reconcile_reports,
)

__all__ = [
    "APPLICABILITY_INVALIDATED",
    "APPLICABILITY_PRESERVED",
    "APPLICABILITY_UNKNOWN",
    "ApplicabilityDecision",
    "ApplicabilityRequest",
    "BoundEvidence",
    "GENERATION_BINDING_EXACT",
    "GENERATION_BINDING_FRESH",
    "GENERATION_BINDING_UNBOUND",
    "GenerationBinding",
    "InfraRetryBudget",
    "InfraRetryLedger",
    "DEFAULT_INFRA_RETRIES",
    "FAILURE_CLASS_CODE",
    "FAILURE_CLASS_INFRASTRUCTURE",
    "FAILURE_CLASS_VALUES",
    "INFRASTRUCTURE_PREREQUISITE_PATTERNS",
    "ObservedReport",
    "REPORT_INVENTORY_SCHEMA",
    "REPORT_INVENTORY_SCHEMA_STRICT",
    "REPORT_OUTCOME_FAILED",
    "REPORT_OUTCOME_MISSING",
    "REPORT_OUTCOME_PASSED",
    "REPORT_OUTCOME_SKIPPED",
    "ReportInventoryCoverage",
    "RequiredReportInventory",
    "SUBJECT_SCHEMA",
    "VerificationSubject",
    "applicability",
    "candidate_digest_of",
    "classify_repair_failure",
    "freeze_report_inventory_strict",
    "harness_green_verifies",
    "ingest_report_file",
    "invalidate_for_environment_change",
    "observed_report_from_document",
    "subject_freshness",
    "FRESHNESS_CURRENT",
    "FRESHNESS_STALE",
    "FRESHNESS_UNKNOWN",
    "FreshnessVerdict",
    "MATCH_SATISFIED",
    "MATCH_MISSING",
    "MATCH_SKIPPED",
    "MATCH_UNBOUND",
    "MATCH_UNMATCHED",
    "MATCH_OLDER_ATTEMPT",
    "MATCH_FAILED",
    "MATCH_DUPLICATE_CONFLICT",
]

#: The versioned discriminator every verification subject carries. A
#: breaking change to the subject's meaning bumps the tag; pinned
#: verdicts keep the version they were stamped with.
SUBJECT_SCHEMA = "forge.verification.subject/1"

#: The subject freshness vocabulary (R36-14): ``current`` — the record's
#: subject binds the exact current candidate; ``stale`` — the record
#: names a DIFFERENT candidate (or world) and can never produce
#: ``verified_ready``; ``unknown`` — the record carries no subject
#: identity (a pre-R36-14 row, or one written before the binding
#: landed) and keeps its existing, separately-gated meaning.
FRESHNESS_CURRENT = "current"
FRESHNESS_STALE = "stale"
FRESHNESS_UNKNOWN = "unknown"

#: R37-04 (#285): the STRICT report-inventory discriminator. A schema/2
#: inventory is matched by the exact per-row identity (subject digest,
#: report path, bundle digest, producer) with numeric attempt ordering,
#: typed ``unbound``/``unmatched``/``duplicate_report_conflict`` outcomes
#: and the command-exit combination rule. The schema/1 inventory keeps
#: the weaker contract the run was approved under (the versioned legacy
#: mode) — its reading never broadens, and only inventories FROZEN with
#: this tag are judged strict.
REPORT_INVENTORY_SCHEMA_STRICT = "forge.verification.report-inventory/2"

#: R37-04: the per-obligation report-match outcome vocabulary (the
#: ``verification.report_match_outcome`` token). ``satisfied`` is the
#: only satisfying value; every other value names exactly WHY the
#: obligation is not satisfied, so missing/skipped/malformed
#: (unbound)/nonzero-exit (failed)/conflicting reports stay
#: distinguishable in the verdict and the operator diagnostics.
MATCH_SATISFIED = "satisfied"
MATCH_MISSING = "missing"
MATCH_SKIPPED = "skipped"
MATCH_UNBOUND = "unbound"
MATCH_UNMATCHED = "unmatched"
MATCH_OLDER_ATTEMPT = "older_attempt"
MATCH_FAILED = "failed"
MATCH_DUPLICATE_CONFLICT = "duplicate_report_conflict"

#: R37-05 (#286): the applicability vocabulary (the
#: ``verification.applicability`` token). ``preserved`` — the recorded
#: evidence still speaks for the current typed subject (an editorial
#: plan revision alone never moves it); ``invalidated`` — a named input
#: moved (``verification.invalidated_inputs``) and the evidence must be
#: re-produced; ``unknown`` — a strict identity field is missing on
#: either side, so reuse is not decidable (never a looser success).
APPLICABILITY_PRESERVED = "preserved"
APPLICABILITY_INVALIDATED = "invalidated"
APPLICABILITY_UNKNOWN = "unknown"

#: The repair failure-class vocabulary (``repair.failure_class``):
#: ``code`` — the change is blamed, the bounded repair cycle may run;
#: ``infrastructure`` — a PREREQUISITE failed (runner unavailable,
#: report transport error): the execution environment, never the change,
#: and it never blindly consumes the code-repair iterations.
FAILURE_CLASS_CODE = "code"
FAILURE_CLASS_INFRASTRUCTURE = "infrastructure"
FAILURE_CLASS_VALUES = (FAILURE_CLASS_CODE, FAILURE_CLASS_INFRASTRUCTURE)

#: Text signatures of infrastructure-PREREQUISITE failures — the things
#: that must exist BEFORE any check can blame the change: a runner to
#: execute on and a transport to carry the reports back. Matched
#: case-insensitively against the failure's composed detail text.
INFRASTRUCTURE_PREREQUISITE_PATTERNS: tuple[str, ...] = (
    "runner unavailable",
    "waiting for a runner",
    "no runner matching",
    "runner never picked up",
    "self-hosted runner offline",
    "report transport",
    "artifact transport",
    "checks transport",
    "report upload failed",
    "runner came back",  # deliberate marker for lane-side recovery notes
)

#: The bounded distinct budget for infrastructure-prerequisite retries:
#: how many times an infra-classified verification failure may keep the
#: run waiting for the prerequisite to recover BEFORE it blocks
#: honestly. Distinct from (and never consuming) the spec-frozen
#: ``commit_cycles`` code-repair budget.
DEFAULT_INFRA_RETRIES = 2


def _canonical_digest(document: Mapping[str, Any]) -> str:
    """sha256 over the canonical (sorted-key) JSON of *document*."""
    payload = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


#: The generation-binding vocabulary — the same three shapes the
#: collector records (:mod:`forge.candidate_collector`): ``exact``
#: (pointer digest == dispatched checkpoint), ``unbound`` (dispatch
#: carried no checkpoint identity), ``fresh`` (no pointer — collected
#: from the checkout itself).
GENERATION_BINDING_EXACT = "exact"
GENERATION_BINDING_UNBOUND = "unbound"
GENERATION_BINDING_FRESH = "fresh"


@dataclass(frozen=True)
class GenerationBinding:
    """WHERE the collected diff came from — the collector's own binding.

    Mirrors :class:`forge.candidate_collector.CollectionResult`: the work
    id the collected tree is bound to, the checkpoint that minted the
    generation, how the attempt was bound (``exact`` / ``unbound`` /
    ``fresh``) and whether the bytes came from a restored generation or
    the checkout itself. Empty ``checkpoint_id`` is the honest unknown —
    never guessed.
    """

    work_id: str = ""
    checkpoint_id: str = ""
    checkpoint_binding: str = ""
    source: str = ""

    @classmethod
    def from_document(cls, document: Mapping[str, Any] | None) -> GenerationBinding:
        """Read a binding from an evidence fragment — tolerant: a missing
        or malformed fragment is the empty binding (all facts unknown),
        never a raise (a verdict must not crash on an unreadable
        provenance note)."""
        if not isinstance(document, Mapping):
            return cls()
        return cls(
            work_id=str(document.get("work_id") or ""),
            checkpoint_id=str(document.get("checkpoint_id") or ""),
            checkpoint_binding=str(document.get("checkpoint_binding") or ""),
            source=str(document.get("source") or ""),
        )

    def as_document(self) -> dict[str, str]:
        return {
            "work_id": self.work_id,
            "checkpoint_id": self.checkpoint_id,
            "checkpoint_binding": self.checkpoint_binding,
            "source": self.source,
        }


def candidate_digest_of(
    *,
    source_oid: str,
    base_oid: str,
    plan_digest: str,
    recorded_digest: str = "",
) -> str:
    """The candidate's content-binding digest.

    The PRIMARY form is the collector's diff digest — the sha256 over
    the exact ``candidate.diff`` bytes (what the candidate meta's
    ``manifest_digest`` carries); the caller passes it as
    *recorded_digest* when the publication recorded one. When it did
    not (a row written before that wiring, or a builtin lane), the
    digest is DERIVED over the durable publication identity — the
    published oid, the frozen base and the plan digest — so any repair,
    revision or re-collection still moves the digest. Both sides of a
    freshness comparison must come from this one function.
    """
    recorded = str(recorded_digest or "").strip().lower()
    if recorded.startswith("sha256:"):
        recorded = recorded[len("sha256:") :]
    if recorded:
        return recorded
    return _canonical_digest(
        {"base_oid": base_oid, "plan_digest": plan_digest, "source_oid": source_oid}
    )


@dataclass(frozen=True)
class VerificationSubject:
    """The exact thing a verification verdict vouches for (R36-14).

    Frozen at construction; every field is part of the identity — any
    mutation is a different subject (and therefore a different digest).
    """

    #: The candidate's content binding — the collector's diff digest
    #: (or its derived form; see :func:`candidate_digest_of`).
    candidate_digest: str
    #: The published candidate oid — the review head the diff became.
    source_oid: str
    #: The sha the PROVIDER tested. EQUAL to *source_oid* normally;
    #: DIFFERENT where the provider tests a synthetic merge — recorded
    #: separately precisely so that difference can never be lost.
    tested_oid: str
    #: The generation the collected diff came from (collector binding).
    generation: GenerationBinding = field(default_factory=GenerationBinding)
    #: The plan/revision digest the candidate answers to.
    plan_revision_digest: str = ""
    #: The tested environment/profile digest (the spec's frozen
    #: execution profile) — the world the checks ran in.
    environment_profile_digest: str = ""
    #: R37-05: the BASE the candidate was cut from. The candidate digest
    #: is often the collector's DIFF digest — identical patch bytes on a
    #: DIFFERENT base keep that digest (probe P06), so the base is an
    #: explicit, separately compared identity field. Additive: legacy
    #: subjects (and every pre-R37-05 row) simply carry the honest empty
    #: string — never an invented base.
    source_base_oid: str = ""

    def __post_init__(self) -> None:
        for name in ("candidate_digest", "source_oid"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must be non-empty — the subject names it")

    @property
    def subject_digest(self) -> str:
        """sha256 over the subject's canonical document (excluding the
        digest itself) — the one value a verdict's applicability binds
        to. Every identity field participates, so any mutation of the
        subject is a different digest by construction."""
        return _canonical_digest(self._document_body())

    def _document_body(self) -> dict[str, Any]:
        """The document WITHOUT its own digest (what the digest covers)."""
        return {
            "schema": SUBJECT_SCHEMA,
            "candidate_digest": self.candidate_digest,
            "source_oid": self.source_oid,
            "tested_oid": self.tested_oid,
            "generation": self.generation.as_document(),
            "plan_revision_digest": self.plan_revision_digest,
            "environment_profile_digest": self.environment_profile_digest,
            "source_base_oid": self.source_base_oid,
        }

    def as_document(self) -> dict[str, Any]:
        """The JSON-ready evidence fragment (``verification.
        subject_identity``)."""
        return {**self._document_body(), "subject_digest": self.subject_digest}

    @classmethod
    def from_document(cls, document: Mapping[str, Any] | None) -> VerificationSubject | None:
        """Read a subject from an evidence fragment; ``None`` when the
        fragment carries no usable binding (absent, malformed or a
        foreign schema) — the caller treats that as freshness UNKNOWN,
        never as a raise.

        R37-05: a fragment that CARRIES a ``subject_digest`` is verified
        against the canonical digest of its own serialized content — a
        digest/content mismatch is REFUSED (``None``), never normalized
        into a plausible current subject. The verification recomputes
        over the stored field set itself, so rows written by any version
        of this class self-verify and history is not silently
        invalidated by an additive field."""
        if not isinstance(document, Mapping):
            return None
        if str(document.get("schema") or "") != SUBJECT_SCHEMA:
            return None
        candidate_digest = str(document.get("candidate_digest") or "")
        source_oid = str(document.get("source_oid") or "")
        if not candidate_digest or not source_oid:
            return None
        stored_digest = str(document.get("subject_digest") or "")
        if stored_digest:
            body = {key: value for key, value in document.items() if key != "subject_digest"}
            if _canonical_digest(body) != stored_digest:
                return None
        generation = document.get("generation")
        return cls(
            candidate_digest=candidate_digest,
            source_oid=source_oid,
            tested_oid=str(document.get("tested_oid") or source_oid),
            generation=GenerationBinding.from_document(
                generation if isinstance(generation, Mapping) else None
            ),
            plan_revision_digest=str(document.get("plan_revision_digest") or ""),
            environment_profile_digest=str(document.get("environment_profile_digest") or ""),
            source_base_oid=str(document.get("source_base_oid") or ""),
        )

    def binds_candidate(self, *, candidate_digest: str) -> bool:
        """Whether this subject is the CURRENT candidate's subject.

        Compared on the CANDIDATE DIGEST (the content binding), never a
        display name or a run number: a re-collected or repaired
        candidate derives a different digest and therefore never
        satisfies an old verdict.
        """
        return bool(candidate_digest) and self.candidate_digest == candidate_digest


@dataclass(frozen=True)
class FreshnessVerdict:
    """The freshness gate's answer for one recorded verdict."""

    status: str
    reason: str

    @property
    def current(self) -> bool:
        return self.status == FRESHNESS_CURRENT

    @property
    def stale(self) -> bool:
        return self.status == FRESHNESS_STALE


def subject_freshness(
    verification: Mapping[str, Any] | None,
    *,
    candidate_digest: str = "",
    environment_profile_digest: str = "",
    current_subject: VerificationSubject | None = None,
) -> FreshnessVerdict:
    """Is this recorded verdict applicable to the CURRENT candidate?

    Two shapes, one honest answer:

    - the LEGACY shape (R36-14 — *candidate_digest* strings): the record
      carries NO subject identity → ``unknown`` (the
      ``verification.freshness_unknown`` observability token): the legacy
      ADR-0008 sha binding decides, exactly as before; the subject's
      candidate digest differs from the current candidate's → ``stale``;
      the environment/profile digest known on BOTH sides and differing →
      ``stale``; otherwise ``current``.
    - the TYPED shape (R37-05 — *current_subject*): the FULL subject is
      compared — the candidate digest, the source OID, the tested OID,
      the base OID, the plan/revision digest and (when both sides record
      one) the environment digest. A strict identity field MISSING on
      either side renders ``unknown``, never a looser success: the same
      diff bytes on a different base can no longer ride a matching diff
      digest (probe P06), and a stored record with its strict fields
      deleted cannot fall back to the two-string comparison.
    """
    fragment = (
        (verification or {}).get("subject_identity") if isinstance(verification, Mapping) else None
    )
    recorded = VerificationSubject.from_document(fragment)
    if current_subject is not None:
        if recorded is None:
            return FreshnessVerdict(
                FRESHNESS_UNKNOWN,
                "the recorded verdict carries no verifiable subject_identity — "
                "freshness cannot be decided for it (verification.freshness_unknown)",
            )
        missing = [
            name
            for name, value in (
                ("source_base_oid", (recorded.source_base_oid, current_subject.source_base_oid)),
                (
                    "plan_revision_digest",
                    (
                        recorded.plan_revision_digest,
                        current_subject.plan_revision_digest,
                    ),
                ),
            )
            if not value[0].strip() or not value[1].strip()
        ]
        if missing:
            return FreshnessVerdict(
                FRESHNESS_UNKNOWN,
                "strict subject identity incomplete ("
                + ", ".join(missing)
                + ") — freshness cannot be decided without inventing fields "
                "(verification.freshness_unknown)",
            )
        differences: list[tuple[str, str, str]] = []
        for name, left, right in (
            ("candidate_digest", recorded.candidate_digest, current_subject.candidate_digest),
            ("source_oid", recorded.source_oid, current_subject.source_oid),
            ("tested_oid", recorded.tested_oid, current_subject.tested_oid),
            ("source_base_oid", recorded.source_base_oid, current_subject.source_base_oid),
            (
                "plan_revision_digest",
                recorded.plan_revision_digest,
                current_subject.plan_revision_digest,
            ),
        ):
            if left != right:
                differences.append((name, left, right))
        recorded_env = recorded.environment_profile_digest
        current_env = current_subject.environment_profile_digest
        if recorded_env and current_env and recorded_env != current_env:
            differences.append(("environment_profile_digest", recorded_env, current_env))
        if differences:
            name, left, _right = differences[0]
            return FreshnessVerdict(
                FRESHNESS_STALE,
                f"the recorded verdict's subject differs on {name} "
                f"({left[:12] or '?'} vs the current) — fresh verification required",
            )
        return FreshnessVerdict(FRESHNESS_CURRENT, "the verdict binds the current subject")
    if recorded is None:
        return FreshnessVerdict(
            FRESHNESS_UNKNOWN,
            "the recorded verdict carries no subject_identity — "
            "freshness cannot be decided for it (verification.freshness_unknown)",
        )
    if not recorded.binds_candidate(candidate_digest=candidate_digest):
        return FreshnessVerdict(
            FRESHNESS_STALE,
            "the recorded verdict names candidate "
            f"{recorded.candidate_digest[:12]}, not the current "
            f"{str(candidate_digest)[:12] or '?'} — fresh verification required",
        )
    recorded_env = recorded.environment_profile_digest
    if (
        recorded_env
        and str(environment_profile_digest or "")
        and recorded_env != environment_profile_digest
    ):
        return FreshnessVerdict(
            FRESHNESS_STALE,
            f"the recorded verdict was judged under environment profile "
            f"{recorded_env[:12]}, not the current "
            f"{str(environment_profile_digest)[:12]} — the tested world moved",
        )
    return FreshnessVerdict(FRESHNESS_CURRENT, "the verdict binds the current candidate")


# ----------------------------------------------------------------------
# R37-05 (#286): the applicability contract — ONE pure reuse decision the
# service finalization and the operator rendering both route through.
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ApplicabilityRequest:
    """The typed applicability question: may the RECORDED evidence be
    reused for the CURRENT subject?

    *recorded* is the persisted evidence fragment (the verdict's
    ``subject_identity`` document — digests are verified against the
    canonical content on read; a mismatched fragment is refused, which
    renders the decision ``unknown``). *current* is the typed subject
    being decided for. The contract digests are optional: a required
    contract/policy version participates exactly when both sides carry
    one.
    """

    current: VerificationSubject
    recorded: Mapping[str, Any] | None = None
    recorded_subject: VerificationSubject | None = None
    #: The required contract the CURRENT decision is asking under.
    required_contract_digest: str = ""
    #: The contract the recorded evidence was produced under.
    recorded_contract_digest: str = ""


@dataclass(frozen=True)
class ApplicabilityDecision:
    """The applicability answer (the ``verification.applicability``
    observability fragment).

    ``preserved`` — the named *equivalent_inputs* stayed identical, so
    the evidence still speaks for the current subject (an editorial plan
    revision alone never moves this). ``invalidated`` — the named
    *invalidated_inputs* moved; the evidence must be re-produced.
    ``unknown`` — a strict identity field is missing on one side (legacy
    provenance is preserved, fields are never invented), so reuse is
    not decidable and never silently granted.
    """

    status: str
    reason: str
    equivalent_inputs: tuple[str, ...] = ()
    invalidated_inputs: tuple[str, ...] = ()

    @property
    def preserved(self) -> bool:
        return self.status == APPLICABILITY_PRESERVED

    @property
    def invalidated(self) -> bool:
        return self.status == APPLICABILITY_INVALIDATED

    def as_document(self) -> dict[str, Any]:
        return {
            "applicability": self.status,
            "reason": self.reason,
            "equivalent_inputs": list(self.equivalent_inputs),
            "invalidated_inputs": list(self.invalidated_inputs),
        }


def applicability(request: ApplicabilityRequest) -> ApplicabilityDecision:
    """The pure reuse decision over one recorded subject versus the
    current typed subject (R37-05).

    Rules (each named input is the ``verification.invalidated_inputs``
    vocabulary):

    - no usable recorded subject (absent, malformed, or a serialized
      digest that does not match its content) → ``unknown`` — legacy
      provenance keeps its separate meaning, nothing is invented;
    - the strict base identity is missing on either side → ``unknown``:
      the candidate digest is often a DIFF digest, so without the base
      the same patch on a different base cannot be told apart (probe
      P06) and reuse is not decidable;
    - a changed candidate digest, source OID, tested OID, base OID or
      (when both sides record one) environment/required-contract digest
      → ``invalidated`` with every moved input named;
    - the plan/revision digest is DELIBERATELY excluded: it is editorial
      provenance (who asked, under which revision), not executable
      content — a purely editorial plan revision preserves
      applicability, and the decision names the inputs that stayed
      equivalent;
    - a synthetic merge (tested OID differs from the source OID)
      correlates BOTH the head (source/candidate) and the target/base —
      a preserved decision under a synthetic merge says so, because
      both sides were compared, not just the head.
    """
    recorded = request.recorded_subject
    if recorded is None:
        recorded = VerificationSubject.from_document(request.recorded)
    if recorded is None:
        return ApplicabilityDecision(
            APPLICABILITY_UNKNOWN,
            "no usable recorded subject identity — applicability cannot be "
            "decided (legacy provenance keeps its separate meaning)",
        )
    current = request.current
    if not recorded.source_base_oid.strip() or not current.source_base_oid.strip():
        return ApplicabilityDecision(
            APPLICABILITY_UNKNOWN,
            "strict subject identity incomplete (source_base_oid missing on one "
            "side) — the same diff on a different base cannot be told apart, so "
            "reuse is not decidable and never silently granted",
        )
    invalidated: list[str] = []
    equivalent: list[str] = []
    for name, left, right in (
        ("candidate_digest", recorded.candidate_digest, current.candidate_digest),
        ("source_oid", recorded.source_oid, current.source_oid),
        ("tested_oid", recorded.tested_oid, current.tested_oid),
        ("source_base_oid", recorded.source_base_oid, current.source_base_oid),
    ):
        if left != right:
            invalidated.append(name)
        else:
            equivalent.append(name)
    recorded_env = recorded.environment_profile_digest
    current_env = current.environment_profile_digest
    if recorded_env and current_env:
        if recorded_env != current_env:
            invalidated.append("environment_profile_digest")
        else:
            equivalent.append("environment_profile_digest")
    if request.recorded_contract_digest and request.required_contract_digest:
        if request.recorded_contract_digest != request.required_contract_digest:
            invalidated.append("required_contract_digest")
        else:
            equivalent.append("required_contract_digest")
    if invalidated:
        return ApplicabilityDecision(
            APPLICABILITY_INVALIDATED,
            "changed "
            + ", ".join(invalidated)
            + " — the recorded evidence no longer applies and must be re-produced",
            equivalent_inputs=tuple(sorted(equivalent)),
            invalidated_inputs=tuple(sorted(invalidated)),
        )
    reason = (
        "the tested subject is identical on "
        + ", ".join(sorted(equivalent))
        + " — the recorded evidence still applies"
    )
    if recorded.plan_revision_digest != current.plan_revision_digest:
        reason += (
            "; the plan revision moved ("
            f"{recorded.plan_revision_digest[:12] or '?'} -> "
            f"{current.plan_revision_digest[:12] or '?'}) but a purely editorial "
            "revision preserves applicability"
        )
    if recorded.tested_oid != recorded.source_oid:
        reason += "; synthetic merge: the target/base and the head both correlate"
    return ApplicabilityDecision(
        APPLICABILITY_PRESERVED,
        reason,
        equivalent_inputs=tuple(sorted(equivalent)),
    )


# ----------------------------------------------------------------------
# The expected-report inventory frozen with the work contract
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class RequiredReportInventory:
    """The report set a verified verdict must show — frozen at dispatch.

    Built from the qualification :class:`ExpectedReports` machinery
    (``freeze_report_inventory`` records it on the run with the work
    contract); at verdict time the OBSERVED reports reconcile against
    the FROZEN rows, never against a live recomputation — a post-hoc
    edit of the recipe cannot shrink the expected set under a verdict
    that already shipped.

    R37-04: the frozen SCHEMA decides how observed reports may match.
    ``report-inventory/2`` (strict — :func:`freeze_report_inventory_strict`)
    matches each row on its full identity (subject digest, report path,
    bundle digest, producer) with numeric attempt ordering, typed
    unbound/unmatched/conflict outcomes and the command-exit
    combination rule. ``report-inventory/1`` is the versioned LEGACY
    mode — the weaker contract the run was approved under; its reading
    is unchanged and never broadens.
    """

    contract_digest: str
    reports: tuple[tuple[str, str, str, str], ...]
    inventory_digest: str
    schema: str = REPORT_INVENTORY_SCHEMA
    #: The verification producer the strict inventory pins its reports
    #: to (empty only in the legacy shape).
    producer: str = ""

    def __post_init__(self) -> None:
        if self.schema not in (REPORT_INVENTORY_SCHEMA, REPORT_INVENTORY_SCHEMA_STRICT):
            raise ValueError(f"unknown report inventory schema {self.schema!r}")
        if not self.reports:
            raise ValueError("a report inventory expects at least one report")
        projects = [row[0] for row in self.reports]
        paths = [row[1] for row in self.reports]
        if len(set(projects)) != len(projects) or len(set(paths)) != len(paths):
            raise ValueError("one inventory row per test project and report path")

    @property
    def strict(self) -> bool:
        """Whether this inventory judges observed reports by the strict
        (schema/2) contract."""
        return self.schema == REPORT_INVENTORY_SCHEMA_STRICT

    @property
    def test_projects(self) -> tuple[str, ...]:
        return tuple(row[0] for row in self.reports)

    def obligation_id(self, index: int) -> str:
        """The human-stable obligation label for row *index* — display
        only, deliberately SEPARATE from the match key (the frozen row
        identity: path + candidate + bundle + producer)."""
        project, path, _candidate, _bundle = self.reports[index]
        return f"{project}@{path}"

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "schema": self.schema,
            "contract_digest": self.contract_digest,
            "reports": [
                {
                    "test_project": row[0],
                    "report_path": row[1],
                    "candidate_id": row[2],
                    "bundle_digest": row[3],
                }
                for row in self.reports
            ],
            "inventory_digest": self.inventory_digest,
        }
        if self.strict:
            document["producer"] = self.producer
        return document

    @classmethod
    def from_document(cls, document: Mapping[str, Any] | None) -> RequiredReportInventory | None:
        """Read the frozen inventory off a run's evidence; ``None`` when
        the run froze none (legacy/builtin lanes — no inventory gate) or
        the document names a foreign schema."""
        if not isinstance(document, Mapping):
            return None
        schema = str(document.get("schema") or REPORT_INVENTORY_SCHEMA)
        if schema not in (REPORT_INVENTORY_SCHEMA, REPORT_INVENTORY_SCHEMA_STRICT):
            return None
        rows = document.get("reports")
        if not isinstance(rows, list) or not rows:
            return None
        reports: list[tuple[str, str, str, str]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                return None
            reports.append(
                (
                    str(row.get("test_project") or ""),
                    str(row.get("report_path") or ""),
                    str(row.get("candidate_id") or ""),
                    str(row.get("bundle_digest") or ""),
                )
            )
        if any(not project or not path for project, path, _, _ in reports):
            return None
        return cls(
            contract_digest=str(document.get("contract_digest") or ""),
            reports=tuple(reports),
            inventory_digest=str(document.get("inventory_digest") or ""),
            schema=schema,
            producer=str(document.get("producer") or ""),
        )

    def as_expected_reports(self) -> ExpectedReports:
        """The qualification-machinery form (for the file-based TRX
        reconciliation)."""
        from forge.adaptive.qualification import ExpectedReport

        return ExpectedReports(
            reports=tuple(
                ExpectedReport(
                    test_project=project,
                    report_path=path,
                    candidate_id=candidate_id,
                    bundle_digest=bundle_digest,
                )
                for project, path, candidate_id, bundle_digest in self.reports
            )
        )

    def reconcile_found_dir(self, found_dir: Path) -> ReportReconciliation:
        """Reconcile a report DIRECTORY against the frozen inventory —
        the qualification :func:`reconcile_reports` semantics: a MISSING
        report is ``missing_report`` (never zero failures), a leftover
        from another run is ``stale_report``, a failing secondary test
        project cannot disappear behind a passing first TRX."""
        return reconcile_reports(self.as_expected_reports(), found_dir)

    def coverage(self, observed: Sequence[ObservedReport]) -> ReportInventoryCoverage:
        """Reconcile OBSERVED reports (already ingested evidence rows)
        against the frozen inventory at verdict time.

        Strict inventories (schema/2) judge per-row identities; legacy
        inventories (schema/1) keep the approved weaker reading."""
        if self.strict:
            return self._coverage_strict(observed)
        return self._coverage_legacy(observed)

    def _coverage_legacy(self, observed: Sequence[ObservedReport]) -> ReportInventoryCoverage:
        """The schema/1 reading — byte-for-byte the R36-14 semantics the
        run was approved under (name-keyed, blank subjects tolerated,
        lexicographic attempt selection)."""
        by_project: dict[str, ObservedReport] = {}
        for report in observed:
            existing = by_project.get(report.test_project)
            if existing is None or report.attempt_key >= existing.attempt_key:
                by_project[report.test_project] = report
        satisfied: list[str] = []
        missing: list[str] = []
        skipped: list[str] = []
        older_attempt: list[str] = []
        failed: list[str] = []
        outcomes: list[tuple[str, str, int | None]] = []
        archived: dict[str, tuple[str, ...]] = {}
        for index, (project, _path, _candidate, _bundle) in enumerate(self.reports):
            obligation_id = self.obligation_id(index)
            found = by_project.get(project)
            if found is not None and found.report_digest:
                archived[obligation_id] = (found.report_digest,)
            if found is None or found.outcome == REPORT_OUTCOME_MISSING:
                missing.append(project)
                outcomes.append((obligation_id, MATCH_MISSING, None))
            elif found.outcome == REPORT_OUTCOME_SKIPPED:
                skipped.append(project)
                outcomes.append((obligation_id, MATCH_SKIPPED, None))
            elif found.subject_digest and found.subject_digest != self._subject_digest:
                older_attempt.append(project)
                outcomes.append((obligation_id, MATCH_OLDER_ATTEMPT, None))
            elif found.outcome != REPORT_OUTCOME_PASSED:
                failed.append(project)
                outcomes.append((obligation_id, MATCH_FAILED, found.exit_code))
            else:
                satisfied.append(project)
                outcomes.append((obligation_id, MATCH_SATISFIED, found.exit_code))
        return ReportInventoryCoverage(
            expected=self.test_projects,
            satisfied=tuple(satisfied),
            missing=tuple(missing),
            skipped=tuple(skipped),
            older_attempt=tuple(older_attempt),
            failed=tuple(failed),
            outcomes=tuple(outcomes),
            report_digests=tuple(
                (oid, digests) for oid, digests in sorted(archived.items()) if digests
            ),
        )

    def _coverage_strict(self, observed: Sequence[ObservedReport]) -> ReportInventoryCoverage:
        """The schema/2 reading (R37-04): every obligation row is judged
        on the identity THAT row froze — the observed subject digest,
        report path, bundle digest and producer — never the first row's
        candidate and never the test-project name alone.

        A report without a subject identity (or path/bundle identity)
        is a typed ``unbound`` outcome, never satisfaction; a
        right-subject report at the wrong path/bundle/producer is
        ``unmatched`` evidence beside the obligation; reports naming
        another candidate are ``older_attempt`` evidence; duplicate
        report keys with contradictory outcomes are a
        ``duplicate_report_conflict`` — never resolved by response
        order; attempts order NUMERICALLY (native integer-shaped ids,
        the persisted monotonic ``attempt_ordinal`` as the fallback), so
        attempt 10 supersedes attempt 9 and a prior success cannot hide
        a newer required failure; and a passed report whose required
        command exited nonzero combines to ``failed`` with the exit
        recorded."""
        per_project: dict[str, list[ObservedReport]] = {}
        for report in observed:
            per_project.setdefault(report.test_project, []).append(report)
        satisfied: list[str] = []
        missing: list[str] = []
        skipped: list[str] = []
        older_attempt: list[str] = []
        failed: list[str] = []
        unbound: list[str] = []
        unmatched: list[str] = []
        conflict: list[str] = []
        exits: dict[str, int] = {}
        outcomes: list[tuple[str, str, int | None]] = []
        archived: dict[str, tuple[str, ...]] = {}
        for index, (project, path, candidate_id, bundle) in enumerate(self.reports):
            obligation_id = self.obligation_id(index)
            pool = per_project.get(project) or []
            # the RAW report digests are archived with the verdict — the
            # evidence stays inspectable beside the decision over it
            archived[obligation_id] = tuple(
                sorted({report.report_digest for report in pool if report.report_digest})
            )
            bound: list[ObservedReport] = []
            classes: list[str] = []
            for report in pool:
                outcome_class = _strict_match_class(
                    report,
                    path=path,
                    candidate_id=candidate_id,
                    bundle=bundle,
                    producer=self.producer,
                )
                if outcome_class == MATCH_SATISFIED:
                    bound.append(report)
                else:
                    classes.append(outcome_class)
            if bound:
                by_order: dict[int, set[str]] = {}
                for report in bound:
                    by_order.setdefault(_attempt_order(report), set()).add(report.outcome)
                if any(len(distinct) > 1 for distinct in by_order.values()):
                    # the same report key (same attempt) carrying
                    # contradictory outcomes — never resolved by
                    # response order.
                    conflict.append(project)
                    outcomes.append((obligation_id, MATCH_DUPLICATE_CONFLICT, None))
                    continue
                newest = max(bound, key=_attempt_order)
                decided = newest
                if (
                    decided.outcome == REPORT_OUTCOME_PASSED
                    and decided.exit_code is not None
                    and decided.exit_code != 0
                ):
                    # the combination rule: a passed report whose required
                    # command exited nonzero does not pass silently.
                    failed.append(project)
                    exits[obligation_id] = decided.exit_code
                    outcomes.append((obligation_id, MATCH_FAILED, decided.exit_code))
                    continue
                if decided.outcome == REPORT_OUTCOME_MISSING:
                    missing.append(project)
                    outcomes.append((obligation_id, MATCH_MISSING, decided.exit_code))
                elif decided.outcome == REPORT_OUTCOME_SKIPPED:
                    skipped.append(project)
                    outcomes.append((obligation_id, MATCH_SKIPPED, decided.exit_code))
                elif decided.outcome != REPORT_OUTCOME_PASSED:
                    failed.append(project)
                    outcomes.append((obligation_id, MATCH_FAILED, decided.exit_code))
                else:
                    satisfied.append(project)
                    outcomes.append((obligation_id, MATCH_SATISFIED, decided.exit_code))
                continue
            # no bound report: the most specific honest problem wins —
            # an identity-less report is UNBOUND (probe P04), a report
            # naming another candidate is an OLDER ATTEMPT, a
            # right-subject/wrong-artifact report is UNMATCHED.
            if MATCH_UNBOUND in classes:
                unbound.append(project)
                outcomes.append((obligation_id, MATCH_UNBOUND, None))
            elif MATCH_OLDER_ATTEMPT in classes:
                older_attempt.append(project)
                outcomes.append((obligation_id, MATCH_OLDER_ATTEMPT, None))
            elif MATCH_UNMATCHED in classes:
                unmatched.append(project)
                outcomes.append((obligation_id, MATCH_UNMATCHED, None))
            else:
                missing.append(project)
                outcomes.append((obligation_id, MATCH_MISSING, None))
        return ReportInventoryCoverage(
            expected=self.test_projects,
            satisfied=tuple(satisfied),
            missing=tuple(missing),
            skipped=tuple(skipped),
            older_attempt=tuple(older_attempt),
            failed=tuple(failed),
            unbound=tuple(unbound),
            unmatched=tuple(unmatched),
            duplicate_conflict=tuple(conflict),
            exit_codes=tuple((oid, code) for oid, code in sorted(exits.items())),
            outcomes=tuple(outcomes),
            report_digests=tuple(
                (oid, digests) for oid, digests in sorted(archived.items()) if digests
            ),
        )

    @property
    def _subject_digest(self) -> str:
        #: The candidate binding the reports themselves must carry. The
        #: frozen rows' ``candidate_id`` IS that binding (the
        #: qualification identity the reports were promised under).
        return self.reports[0][2] if self.reports else ""


def freeze_report_inventory_strict(
    expected: ExpectedReports,
    *,
    contract_digest: str,
    producer: str,
) -> dict:
    """Freeze the STRICT (schema/2) expected-report inventory (R37-04).

    Same frozen rows as :func:`forge.adaptive.qualification.
    freeze_report_inventory`, PLUS the verification *producer* the
    reports must come from. A run whose evidence carries THIS document
    is judged by :meth:`RequiredReportInventory._coverage_strict` at
    verdict time; runs frozen with the schema/1 document keep the
    weaker approved contract (the versioned legacy mode). The digest
    covers schema + contract digest + producer + rows, so a tampered
    row or producer is detectable against the recorded digest.
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
    payload = {
        "schema": REPORT_INVENTORY_SCHEMA_STRICT,
        "contract_digest": str(contract_digest or ""),
        "producer": str(producer or ""),
        "reports": rows,
    }
    from forge.adaptive.qualification import canonical_json_digest

    return {**payload, "inventory_digest": canonical_json_digest(payload)}


def _strict_match_class(
    report: ObservedReport,
    *,
    path: str,
    candidate_id: str,
    bundle: str,
    producer: str,
) -> str:
    """Classify ONE observed report against ONE frozen obligation row.

    ``satisfied`` here means ONLY "this report binds this row's
    identity" — the outcome (passed/failed/skipped/missing) is decided
    afterwards by attempt ordering. Everything else is a typed
    non-binding class: ``unbound`` (identity not established — no
    subject digest, path or bundle), ``older_attempt`` (names another
    candidate), ``unmatched`` (right candidate at the wrong
    path/bundle/producer)."""
    if not report.subject_digest or not report.report_path or not report.bundle_digest:
        return MATCH_UNBOUND
    if report.subject_digest != candidate_id:
        return MATCH_OLDER_ATTEMPT
    if report.report_path != path:
        return MATCH_UNMATCHED
    if bundle and report.bundle_digest != bundle:
        return MATCH_UNMATCHED
    if producer and report.producer and report.producer != producer:
        return MATCH_UNMATCHED
    return MATCH_SATISFIED


#: One observed report's outcome vocabulary.
REPORT_OUTCOME_PASSED = "passed"
REPORT_OUTCOME_FAILED = "failed"
REPORT_OUTCOME_SKIPPED = "skipped"
REPORT_OUTCOME_MISSING = "missing"


def _attempt_order(report: ObservedReport) -> int:
    """The newest-attempt ordering key (R37-04).

    Native run/attempt ids shaped like integers parse NUMERICALLY —
    attempt 10 supersedes attempt 9, where the lexicographic reading
    inverted them. A non-integer-shaped id falls back to the PERSISTED
    MONOTONIC ``attempt_ordinal`` the ingestion freezes at record time;
    neither known → 0 (the honest oldest)."""
    raw = str(report.attempt or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return int(report.attempt_ordinal or 0)


@dataclass(frozen=True)
class ObservedReport:
    """One report the verification world actually produced, as evidence.

    ``subject_digest`` is the candidate binding the REPORT ITSELF claims
    (the sidecar identity the qualification world writes beside the
    TRX): a report from an OLDER attempt — a different candidate digest
    — can never answer for the current one, however green it is.
    ``exit_code``/``report_digest`` preserve the tool exit code and the
    report's own digest (the .NET profile's full evidence set).

    R37-04 adds the rest of the per-row match identity, additively:
    ``report_path``/``bundle_digest`` (WHERE the report landed and WHICH
    test bundle produced it — compared against THAT obligation row,
    never the first row's candidate), ``producer`` (the reporting
    system behind the row) and ``attempt_ordinal`` (the persisted
    monotonic fallback frozen at record time). Empty identity fields
    are the honest unknown — under the strict profile they classify the
    report ``unbound``, never satisfied.
    """

    test_project: str
    outcome: str
    subject_digest: str = ""
    report_path: str = ""
    exit_code: int | None = None
    report_digest: str = ""
    attempt: str = ""
    bundle_digest: str = ""
    producer: str = ""
    attempt_ordinal: int = 0

    def __post_init__(self) -> None:
        if self.outcome not in (
            REPORT_OUTCOME_PASSED,
            REPORT_OUTCOME_FAILED,
            REPORT_OUTCOME_SKIPPED,
            REPORT_OUTCOME_MISSING,
        ):
            raise ValueError(f"unknown observed report outcome {self.outcome!r}")

    @property
    def attempt_key(self) -> str:
        """The LEGACY (schema/1) newest-attempt ordering key —
        lexicographic by attempt id; absent attempt sorts oldest. Kept
        byte-compatible for the approved weaker contract; the strict
        profile orders via :func:`_attempt_order` instead."""
        return self.attempt or ""

    @property
    def attempt_order(self) -> int:
        """The strict ordering key (numeric native ids, persisted
        monotonic fallback)."""
        return _attempt_order(self)

    def as_document(self) -> dict[str, Any]:
        """The persisted evidence-row shape (``observed_reports``) — the
        exact keys :func:`observed_report_from_document` reads back."""
        return {
            "test_project": self.test_project,
            "outcome": self.outcome,
            "subject_digest": self.subject_digest,
            "report_path": self.report_path,
            "exit_code": self.exit_code,
            "report_digest": self.report_digest,
            "attempt": self.attempt,
            "bundle_digest": self.bundle_digest,
            "producer": self.producer,
            "attempt_ordinal": self.attempt_ordinal,
        }


def observed_report_from_document(document: Mapping[str, Any] | None) -> ObservedReport | None:
    """Read one observed-report row off run evidence; ``None`` for a
    malformed row (an unreadable observation is DROPPED from coverage —
    which leaves the expected report MISSING, the honest direction)."""
    if not isinstance(document, Mapping):
        return None
    project = str(document.get("test_project") or "")
    outcome = str(document.get("outcome") or "")
    if not project or outcome not in (
        REPORT_OUTCOME_PASSED,
        REPORT_OUTCOME_FAILED,
        REPORT_OUTCOME_SKIPPED,
        REPORT_OUTCOME_MISSING,
    ):
        return None
    exit_code = document.get("exit_code")
    ordinal = document.get("attempt_ordinal")
    return ObservedReport(
        test_project=project,
        outcome=outcome,
        subject_digest=str(document.get("subject_digest") or ""),
        report_path=str(document.get("report_path") or ""),
        exit_code=int(exit_code) if isinstance(exit_code, int) else None,
        report_digest=str(document.get("report_digest") or ""),
        attempt=str(document.get("attempt") or ""),
        bundle_digest=str(document.get("bundle_digest") or ""),
        producer=str(document.get("producer") or ""),
        attempt_ordinal=int(ordinal) if isinstance(ordinal, int) else 0,
    )


def ingest_report_file(
    *,
    test_project: str,
    report_path: str,
    found_dir: Path,
    exit_code: int | None = None,
    attempt: str = "",
    attempt_ordinal: int = 0,
    producer: str = "",
) -> ObservedReport:
    """Read ONE real report file (plus its identity sidecar) into an
    :class:`ObservedReport` — the production ingestion shape R37-04
    wires into the verdict gate.

    Mirrors the qualification reconciliation semantics as EVIDENCE: the
    file is absent → ``missing``; the identity sidecar is
    absent/unreadable → the row carries NO subject/bundle (the strict
    profile's typed ``unbound``); a parseable TRX with failures (or
    zero executed tests) → ``failed``; otherwise ``passed``. The exit
    code of the required command and the report's own content digest
    ride the row — the evaluator combines a passed outcome with a
    nonzero exit, and the raw report digest is archived with the
    verdict. The *attempt_ordinal* is the persisted monotonic fallback
    frozen HERE, at record time."""
    from forge.adaptive.qualification import _parse_trx_counters, _read_identity

    path = found_dir / report_path
    row = ObservedReport(
        test_project=test_project,
        outcome=REPORT_OUTCOME_MISSING,
        report_path=report_path,
        exit_code=exit_code,
        attempt=attempt,
        attempt_ordinal=attempt_ordinal,
        producer=producer,
    )
    if not path.is_file():
        return row
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    identity = _read_identity(found_dir / (report_path + ".identity.json"))
    counters = _parse_trx_counters(path)
    subject = str((identity or {}).get("candidate_id") or "")
    bundle = str((identity or {}).get("bundle_digest") or "")
    if counters is None:
        outcome = REPORT_OUTCOME_FAILED  # unparseable is a verdict, never zero failures
    elif counters["executed"] <= 0 or counters["failures"] > 0:
        outcome = REPORT_OUTCOME_FAILED
    else:
        outcome = REPORT_OUTCOME_PASSED
    return ObservedReport(
        test_project=test_project,
        outcome=outcome,
        subject_digest=subject,
        report_path=report_path,
        exit_code=exit_code,
        report_digest=digest,
        attempt=attempt,
        attempt_ordinal=attempt_ordinal,
        bundle_digest=bundle,
        producer=producer,
    )


@dataclass(frozen=True)
class ReportInventoryCoverage:
    """The verdict-time coverage answer over a frozen inventory.

    R37-04 adds the typed strict buckets — ``unbound`` (identity-less
    reports), ``unmatched`` (right candidate, wrong path/bundle/
    producer), ``duplicate_conflict`` (same report key, contradictory
    outcomes) — plus the per-obligation ``outcomes`` rows (the
    ``verification.obligation_id`` / ``verification.report_match_outcome``
    pair, with the recorded exit and the archived report digest) and
    the nonzero ``exit_codes`` the passed/exit combination demoted.
    Legacy inventories leave the new buckets empty."""

    expected: tuple[str, ...]
    satisfied: tuple[str, ...]
    missing: tuple[str, ...]
    skipped: tuple[str, ...]
    older_attempt: tuple[str, ...]
    failed: tuple[str, ...]
    unbound: tuple[str, ...] = ()
    unmatched: tuple[str, ...] = ()
    duplicate_conflict: tuple[str, ...] = ()
    exit_codes: tuple[tuple[str, int], ...] = ()
    outcomes: tuple[tuple[str, str, int | None], ...] = ()
    #: The RAW report content digests archived per obligation — the
    #: evidence stays inspectable beside the decision made over it.
    report_digests: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def verifies(self) -> bool:
        """True only when EVERY expected report was observed for THIS
        candidate, identity-bound, un-skipped, un-conflicted and passing
        — missing, unbound, unmatched, older-attempt, conflicting or
        failed evidence can never appear green."""
        return not (
            self.missing
            or self.skipped
            or self.older_attempt
            or self.failed
            or self.unbound
            or self.unmatched
            or self.duplicate_conflict
        )

    @property
    def problems(self) -> tuple[str, ...]:
        """One line per non-satisfying project — every problem distinct."""
        lines: list[str] = []
        for project in self.missing:
            lines.append(f"{project}: missing_report — no report observed this run")
        for project in self.skipped:
            lines.append(f"{project}: skipped_required_check — the check was skipped")
        for project in self.older_attempt:
            lines.append(
                f"{project}: older_attempt_report — the report belongs to another "
                "candidate and cannot answer for this one"
            )
        for project in self.failed:
            lines.append(f"{project}: failed_report — the report carries failures")
        for project in self.unbound:
            lines.append(
                f"{project}: unbound_report — the report carries no subject/path/"
                "bundle identity, so it cannot satisfy the obligation however "
                "green it claims to be"
            )
        for project in self.unmatched:
            lines.append(
                f"{project}: unmatched_report — the report's path/bundle/producer "
                "does not bind this obligation's frozen identity"
            )
        for project in self.duplicate_conflict:
            lines.append(
                f"{project}: duplicate_report_conflict — the same report key "
                "carries contradictory outcomes; response order cannot decide"
            )
        return tuple(lines)

    def as_document(self) -> dict[str, Any]:
        """The ``verification.expected_report_coverage`` observability
        fragment."""
        return {
            "expected": list(self.expected),
            "satisfied": list(self.satisfied),
            "missing": list(self.missing),
            "skipped": list(self.skipped),
            "older_attempt": list(self.older_attempt),
            "failed": list(self.failed),
            "unbound": list(self.unbound),
            "unmatched": list(self.unmatched),
            "duplicate_conflict": list(self.duplicate_conflict),
            "exit_codes": [
                {"obligation_id": obligation_id, "exit_code": code}
                for obligation_id, code in self.exit_codes
            ],
            "outcomes": [
                {
                    "obligation_id": obligation_id,
                    "report_match_outcome": outcome,
                    "exit_code": exit_code,
                }
                for obligation_id, outcome, exit_code in self.outcomes
            ],
            "report_digests": [
                {"obligation_id": obligation_id, "digests": list(digests)}
                for obligation_id, digests in self.report_digests
            ],
            "complete": self.verifies,
        }


def expected_report_coverage_of(
    evidence: Mapping[str, Any] | None,
) -> ReportInventoryCoverage | None:
    """The run's verdict-time report coverage, or ``None`` when the run
    froze no inventory (no gate — the legacy behavior)."""
    if not isinstance(evidence, Mapping):
        return None
    inventory = RequiredReportInventory.from_document(evidence.get("expected_report_inventory"))
    if inventory is None:
        return None
    rows = evidence.get("observed_reports")
    observed = [
        report
        for report in (
            observed_report_from_document(row if isinstance(row, Mapping) else None)
            for row in (rows if isinstance(rows, Sequence) else [])
        )
        if report is not None
    ]
    return inventory.coverage(observed)


# ----------------------------------------------------------------------
# Repair failure classification: infrastructure prerequisites are not
# code defects (a bounded DISTINCT budget)
# ----------------------------------------------------------------------


def classify_repair_failure(
    detail: str,
    *,
    surface: Iterable[Mapping[str, Any]] = (),
    default: str = FAILURE_CLASS_CODE,
) -> str:
    """Classify a verification failure for the repair path.

    ``code`` (the default — fail toward the repair budget, never toward
    silence) unless the composed *detail* text (plus each surface row's
    name/conclusion text) carries an infrastructure-PREREQUISITE
    signature: the runner the check needed was unavailable, or the
    transport that carries the reports failed. Those blame the world,
    not the change: they never consume the bounded code-repair
    iterations blindly — they ride the distinct
    :class:`InfraRetryBudget`.
    """
    haystack = " ".join(
        [str(detail or "")]
        + [
            f"{row.get('name', '')} {row.get('conclusion') or ''}"
            for row in surface
            if isinstance(row, Mapping)
        ]
    ).lower()
    for pattern in INFRASTRUCTURE_PREREQUISITE_PATTERNS:
        if pattern in haystack:
            return FAILURE_CLASS_INFRASTRUCTURE
    return default if default in FAILURE_CLASS_VALUES else FAILURE_CLASS_CODE


@dataclass(frozen=True)
class InfraRetryBudget:
    """The bounded distinct budget for infrastructure-prerequisite
    retries — separate from (and never consuming) the spec-frozen
    ``commit_cycles`` code-repair budget."""

    max_retries: int = DEFAULT_INFRA_RETRIES

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0 — a budget is bounded, not negative")

    def allows(self, ledger: InfraRetryLedger) -> bool:
        return ledger.retries < self.max_retries


@dataclass(frozen=True)
class InfraRetryLedger:
    """How many infrastructure-prerequisite retries a run has spent.

    Persisted on the run's evidence (``repair_ledger``) so restarts and
    repeated observations cannot reset it. Only the DISTINCT infra
    counter lives here — the code budget stays where it already is
    (``commit_cycle``, spec-frozen).
    """

    retries: int = 0

    def record(self) -> InfraRetryLedger:
        return InfraRetryLedger(retries=self.retries + 1)

    def exhausted(self, budget: InfraRetryBudget) -> bool:
        return self.retries >= budget.max_retries

    def as_document(self) -> dict[str, int]:
        return {"infrastructure_retries": self.retries}

    @classmethod
    def from_document(cls, document: Mapping[str, Any] | None) -> InfraRetryLedger:
        if not isinstance(document, Mapping):
            return cls()
        raw = document.get("infrastructure_retries")
        try:
            retries = max(0, int(raw))  # type: ignore[arg-type,misc]
        except (TypeError, ValueError):
            retries = 0
        return cls(retries=retries)


# ----------------------------------------------------------------------
# Harness green is never independent verification
# ----------------------------------------------------------------------


def harness_green_verifies(required_jobs: Sequence[str]) -> bool:
    """The standing R36-14 assertion: a green harness job ALONE never
    satisfies independent verification.

    The harness lane is EXECUTION, not evidence (the adapter already
    excludes it by spec-frozen identity). Whatever *required_jobs* the
    frozen spec carries, the ONLY ``verified_ready`` source is the
    required-checks POSITIVE PROOF over that frozen list — so this
    function returns False unconditionally. It exists so call sites and
    tests can say the rule in one place instead of re-deriving it.
    """
    return False


# ----------------------------------------------------------------------
# Applicability invalidation (verification_sets style): a changed
# tested-environment digest invalidates ONLY the evidence that claimed
# the previous one — history retained for audit.
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class BoundEvidence:
    """One piece of verification evidence and the world it claimed.

    ``invalidated`` marks withdrawn authority, never deletion: the row
    stays inspectable forever with the recorded reason (the audit
    history an operator replays after an environment move)."""

    evidence_id: str
    subject_digest: str
    environment_profile_digest: str
    invalidated: bool = False
    invalidated_reason: str = ""


def invalidate_for_environment_change(
    records: Sequence[BoundEvidence],
    *,
    previous_digest: str,
    current_digest: str,
) -> tuple[BoundEvidence, ...]:
    """Mark ONLY the evidence judged under *previous_digest* invalid.

    A changed tested-environment/profile digest breaks exactly the
    records that claimed the previous digest — records that never
    carried one (``""`` — the honest unknown) or already recorded the
    new one are untouched, and invalidated rows are RETAINED (flagged,
    never dropped). An unchanged digest is a no-op."""
    if previous_digest == current_digest:
        return tuple(records)
    updated: list[BoundEvidence] = []
    for record in records:
        if (
            not record.invalidated
            and record.environment_profile_digest
            and record.environment_profile_digest == previous_digest
        ):
            updated.append(
                BoundEvidence(
                    evidence_id=record.evidence_id,
                    subject_digest=record.subject_digest,
                    environment_profile_digest=record.environment_profile_digest,
                    invalidated=True,
                    invalidated_reason=(
                        "tested environment/profile moved "
                        f"{previous_digest[:12]} -> {current_digest[:12]} — "
                        "evidence invalidated, retained for audit"
                    ),
                )
            )
        else:
            updated.append(record)
    return tuple(updated)
