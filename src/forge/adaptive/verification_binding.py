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
    "REPORT_OUTCOME_FAILED",
    "REPORT_OUTCOME_MISSING",
    "REPORT_OUTCOME_PASSED",
    "REPORT_OUTCOME_SKIPPED",
    "ReportInventoryCoverage",
    "RequiredReportInventory",
    "SUBJECT_SCHEMA",
    "VerificationSubject",
    "candidate_digest_of",
    "classify_repair_failure",
    "harness_green_verifies",
    "invalidate_for_environment_change",
    "observed_report_from_document",
    "subject_freshness",
    "FRESHNESS_CURRENT",
    "FRESHNESS_STALE",
    "FRESHNESS_UNKNOWN",
    "FreshnessVerdict",
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
        never as a raise."""
        if not isinstance(document, Mapping):
            return None
        if str(document.get("schema") or "") != SUBJECT_SCHEMA:
            return None
        candidate_digest = str(document.get("candidate_digest") or "")
        source_oid = str(document.get("source_oid") or "")
        if not candidate_digest or not source_oid:
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
    candidate_digest: str,
    environment_profile_digest: str = "",
) -> FreshnessVerdict:
    """Is this recorded verdict applicable to the CURRENT candidate?

    - the record carries NO subject identity → ``unknown`` (the
      ``verification.freshness_unknown`` observability token): the
      legacy ADR-0008 sha binding decides, exactly as before;
    - the subject's candidate digest differs from the current
      candidate's → ``stale``: a passed record for another candidate
      (a repair landed, a revision bumped, the diff was re-collected)
      can never produce ``verified_ready`` — fresh verification is
      required;
    - the environment/profile digest the record was judged under is
      known on BOTH sides and differs → ``stale`` as well: the checks
      ran in a different world than the one being decided (the
      per-evidence applicability invalidation handles the finer grain;
      this is the verdict-level gate);
    - otherwise → ``current``.
    """
    subject = VerificationSubject.from_document(
        (verification or {}).get("subject_identity") if isinstance(verification, Mapping) else None
    )
    if subject is None:
        return FreshnessVerdict(
            FRESHNESS_UNKNOWN,
            "the recorded verdict carries no subject_identity — "
            "freshness cannot be decided for it (verification.freshness_unknown)",
        )
    if not subject.binds_candidate(candidate_digest=candidate_digest):
        return FreshnessVerdict(
            FRESHNESS_STALE,
            "the recorded verdict names candidate "
            f"{subject.candidate_digest[:12]}, not the current "
            f"{str(candidate_digest)[:12] or '?'} — fresh verification required",
        )
    recorded_env = subject.environment_profile_digest
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
    """

    contract_digest: str
    reports: tuple[tuple[str, str, str, str], ...]
    inventory_digest: str

    def __post_init__(self) -> None:
        if not self.reports:
            raise ValueError("a report inventory expects at least one report")
        projects = [row[0] for row in self.reports]
        paths = [row[1] for row in self.reports]
        if len(set(projects)) != len(projects) or len(set(paths)) != len(paths):
            raise ValueError("one inventory row per test project and report path")

    @property
    def test_projects(self) -> tuple[str, ...]:
        return tuple(row[0] for row in self.reports)

    def as_document(self) -> dict[str, Any]:
        return {
            "schema": REPORT_INVENTORY_SCHEMA,
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

    @classmethod
    def from_document(cls, document: Mapping[str, Any] | None) -> RequiredReportInventory | None:
        """Read the frozen inventory off a run's evidence; ``None`` when
        the run froze none (legacy/builtin lanes — no inventory gate)."""
        if not isinstance(document, Mapping):
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
        against the frozen inventory at verdict time."""
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
        for project, _path, _candidate, _bundle in self.reports:
            found = by_project.get(project)
            if found is None or found.outcome == REPORT_OUTCOME_MISSING:
                missing.append(project)
            elif found.outcome == REPORT_OUTCOME_SKIPPED:
                skipped.append(project)
            elif found.subject_digest and found.subject_digest != self._subject_digest:
                older_attempt.append(project)
            elif found.outcome != REPORT_OUTCOME_PASSED:
                failed.append(project)
            else:
                satisfied.append(project)
        return ReportInventoryCoverage(
            expected=self.test_projects,
            satisfied=tuple(satisfied),
            missing=tuple(missing),
            skipped=tuple(skipped),
            older_attempt=tuple(older_attempt),
            failed=tuple(failed),
        )

    @property
    def _subject_digest(self) -> str:
        #: The candidate binding the reports themselves must carry. The
        #: frozen rows' ``candidate_id`` IS that binding (the
        #: qualification identity the reports were promised under).
        return self.reports[0][2] if self.reports else ""


#: One observed report's outcome vocabulary.
REPORT_OUTCOME_PASSED = "passed"
REPORT_OUTCOME_FAILED = "failed"
REPORT_OUTCOME_SKIPPED = "skipped"
REPORT_OUTCOME_MISSING = "missing"


@dataclass(frozen=True)
class ObservedReport:
    """One report the verification world actually produced, as evidence.

    ``subject_digest`` is the candidate binding the REPORT ITSELF claims
    (the sidecar identity the qualification world writes beside the
    TRX): a report from an OLDER attempt — a different candidate digest
    — can never answer for the current one, however green it is.
    ``exit_code``/``report_digest`` preserve the tool exit code and the
    report's own digest (the .NET profile's full evidence set).
    """

    test_project: str
    outcome: str
    subject_digest: str = ""
    report_path: str = ""
    exit_code: int | None = None
    report_digest: str = ""
    attempt: str = ""

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
        """The newest-attempt ordering key (lexicographic by attempt id;
        absent attempt sorts oldest)."""
        return self.attempt or ""


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
    return ObservedReport(
        test_project=project,
        outcome=outcome,
        subject_digest=str(document.get("subject_digest") or ""),
        report_path=str(document.get("report_path") or ""),
        exit_code=int(exit_code) if isinstance(exit_code, int) else None,
        report_digest=str(document.get("report_digest") or ""),
        attempt=str(document.get("attempt") or ""),
    )


@dataclass(frozen=True)
class ReportInventoryCoverage:
    """The verdict-time coverage answer over a frozen inventory."""

    expected: tuple[str, ...]
    satisfied: tuple[str, ...]
    missing: tuple[str, ...]
    skipped: tuple[str, ...]
    older_attempt: tuple[str, ...]
    failed: tuple[str, ...]

    @property
    def verifies(self) -> bool:
        """True only when EVERY expected report was observed for THIS
        candidate, un-skipped and passing — missing evidence can never
        appear green."""
        return not (self.missing or self.skipped or self.older_attempt or self.failed)

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
