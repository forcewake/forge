"""Verify independent checks for the exact current candidate."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from dataclasses import dataclass, field
from datetime import datetime, timezone

from forge.config import Settings
from forge.gitlab.schemas import Job, Pipeline
from forge.runs.ci_contract import evaluate_quality_contract

#: Seconds a verification verdict stays fresh.
DEFAULT_FRESHNESS_WINDOW_SECONDS = 60

#: The unified verdict vocabulary. ``passed`` is the ONLY verified status;
#: ``unverified``/``not_configured`` are the honest no-CI forms; ``stale`` is the
#: re-rendering of a verdict whose subject no longer binds the current candidate — retained
#: evidence, withdrawn authority, never ``verified_ready``.
STATUS_PENDING = "pending"
STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_UNKNOWN = "unknown"
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_UNVERIFIED = "unverified"
STATUS_STALE = "stale"

#: The subject freshness vocabulary: a recorded verdict is ``current`` for the candidate it
#: names, ``stale`` for any other, and ``unknown`` when it carries no subject identity at
#: all (a row written before the binding landed — its legacy sha binding still decides).
FRESHNESS_CURRENT = "current"
FRESHNESS_STALE = "stale"
FRESHNESS_UNKNOWN = "unknown"

#: The reporting system behind a verdict — part of the evidence so an operator can tell WHO
#: claimed a check passed.
PRODUCER_GITLAB_PIPELINE = "gitlab-pipeline"
PRODUCER_GITHUB_CHECKS = "github-checks"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class VerificationResult:
    """A provider-independent verification verdict for one candidate commit."""

    #: One of the ``STATUS_*`` constants above.
    status: str
    #: The candidate commit oid the verdict is bound to (ADR-0008: a verdict approves a
    #: *specific* commit, never a branch).
    tested_oid: str
    #: ISO-8601 UTC moment the verdict was observed.
    observed_at: str
    #: The reporting system (a ``PRODUCER_*`` constant or provider equivalent).
    producer: str
    #: Human-readable one-liner (failed check names, contract reason, ...).
    summary: str = ""
    #: Optional per-check details (name/status or name/result entries).
    surface: tuple[dict, ...] = field(default=())
    #: Exact candidate and execution identity; absent on verdicts that recorded no subject.
    subject_identity: Mapping[str, Any] | None = None

    def as_evidence(self) -> dict:
        """The evidence-fragment dict (the unified shape)."""
        evidence: dict = {
            "status": self.status,
            "tested_oid": self.tested_oid,
            "observed_at": self.observed_at,
            "producer": self.producer,
        }
        if self.summary:
            evidence["summary"] = self.summary
        if self.surface:
            evidence["surface"] = [dict(entry) for entry in self.surface]
        if self.subject_identity is not None:
            evidence["subject_identity"] = dict(self.subject_identity)
        return evidence

    @property
    def verified(self) -> bool:
        """only a passed verdict counts as verified — nothing else."""
        return self.status == STATUS_PASSED

    # -- constructors (one per verdict, so call sites read as sentences) ----

    @classmethod
    def pending(cls, tested_oid: str, producer: str, *, summary: str = "") -> VerificationResult:
        return cls(STATUS_PENDING, tested_oid, _utc_now_iso(), producer, summary)

    @classmethod
    def passed(
        cls,
        tested_oid: str,
        producer: str,
        *,
        summary: str = "",
        surface: tuple[dict, ...] = (),
        subject_identity: Mapping[str, Any] | None = None,
    ) -> VerificationResult:
        return cls(
            STATUS_PASSED,
            tested_oid,
            _utc_now_iso(),
            producer,
            summary,
            surface,
            subject_identity,
        )

    @classmethod
    def failed(
        cls,
        tested_oid: str,
        producer: str,
        *,
        summary: str = "",
        surface: tuple[dict, ...] = (),
    ) -> VerificationResult:
        return cls(STATUS_FAILED, tested_oid, _utc_now_iso(), producer, summary, surface)

    @classmethod
    def unknown(cls, tested_oid: str, producer: str, *, summary: str = "") -> VerificationResult:
        return cls(STATUS_UNKNOWN, tested_oid, _utc_now_iso(), producer, summary)

    @classmethod
    def not_configured(
        cls, tested_oid: str, producer: str, *, summary: str = ""
    ) -> VerificationResult:
        return cls(STATUS_NOT_CONFIGURED, tested_oid, _utc_now_iso(), producer, summary)

    @classmethod
    def unverified(cls, tested_oid: str, producer: str, *, summary: str = "") -> VerificationResult:
        return cls(STATUS_UNVERIFIED, tested_oid, _utc_now_iso(), producer, summary)


@dataclass(frozen=True)
class VerificationProfile:
    """The effective verification policy a run's CI verdict is judged by."""

    #: CI job names that must exist AND succeed (ADR-0008).
    required_jobs: tuple[str, ...]
    #: Placeholder for the producer allowlist (who may report a verdict).
    producers: tuple[str, ...] = ()
    #: How long (seconds) a verification verdict is considered fresh.
    freshness_window: int = DEFAULT_FRESHNESS_WINDOW_SECONDS

    @classmethod
    def from_settings(cls, settings: Settings) -> VerificationProfile:
        """Build the profile from ``FORGE_REQUIRED_JOBS`` (comma-separated)."""
        raw = getattr(settings, "FORGE_REQUIRED_JOBS", "") or ""
        required = tuple(sorted(name.strip() for name in raw.split(",") if name.strip()))
        return cls(required_jobs=required)


def evaluate(
    pipeline: Pipeline,
    jobs: list[Job],
    profile: VerificationProfile,
) -> tuple[bool, str]:
    """Evaluate the verification profile for a finished pipeline."""
    if not profile.required_jobs:
        return True, "no verification profile configured (warning)"
    return evaluate_quality_contract(pipeline, jobs, profile.required_jobs)


# the positive-proof contract (GitHub Actions)
# Verification is
# POSITIVE PROOF that the required checks RAN and SUCCEEDED for the exact candidate commit —
# never "nothing failed". The frozen executable spec (``forge.runs.spec``) carries the
# required-checks list and the harness lane identity (``harness_workflow`` /
# ``lane_pipeline_id``); the provider services observe the candidate's checks and classify
# every conclusion through :func:`evaluate_positive_proof` below.

#: GitHub Actions run conclusions that PROVE a check succeeded.
GITHUB_SUCCESS_CONCLUSIONS: frozenset[str] = frozenset({"success"})
#: GitHub Actions conclusions that carry evidence the CHANGE is wrong (the only repair
#: triggers).
GITHUB_CODE_FAILURE_CONCLUSIONS: frozenset[str] = frozenset(
    {"failure", "action_required", "startup_failure"}
)
#: GitHub Actions conclusions that mean the EXECUTION died (cancelled by a human/system,
#: runner timeout) — infrastructure, never code repair.
GITHUB_INFRA_CONCLUSIONS: frozenset[str] = frozenset({"cancelled", "timed_out"})


#: The per-check proof states :func:`evaluate_positive_proof` assigns.
PROOF_SUCCEEDED = "succeeded"
PROOF_WAIVED = "waived"
PROOF_MISSING = "missing"
PROOF_INCONCLUSIVE = "inconclusive"
PROOF_CODE_FAILURE = "code_failure"
PROOF_INFRA_FAILURE = "infra_failure"

#: the blocked-reason token for a cancelled/timed_out verification surface — infrastructure
#: evidence, never a code-repair trigger (the ``verification_timeout``-family of honest
#: parked reasons).
VERIFICATION_INFRA_REASON = "verification_infrastructure"


def verification_epoch(
    evidence: Mapping[str, Any] | None, candidate_sha: str, now: datetime
) -> tuple[dict[str, str], bool]:
    """The verification epoch for *candidate_sha* — a B01 fix."""
    stored = (evidence or {}).get("verification_epoch")
    if isinstance(stored, Mapping):
        if str(stored.get("candidate_sha") or "") == candidate_sha and stored.get("started_at"):
            return dict(stored), False
    return {"candidate_sha": candidate_sha, "started_at": now.isoformat()}, True


def epoch_started_at(epoch: Mapping[str, Any]) -> datetime | None:
    """Parse the epoch's ``started_at`` (ISO) into an aware datetime."""
    raw = str((epoch or {}).get("started_at") or "")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def waived_conclusions_from_settings(settings: Settings) -> frozenset[str]:
    """The policy waiver set from ``FORGE_VERIFICATION_WAIVE_CONCLUSIONS``."""
    raw = str(getattr(settings, "FORGE_VERIFICATION_WAIVE_CONCLUSIONS", "") or "")
    return frozenset(name.strip().lower() for name in raw.split(",") if name.strip())


@dataclass(frozen=True)
class PositiveProof:
    """The positive-proof evaluation over one candidate's observations."""

    required: tuple[str, ...]
    succeeded: tuple[str, ...]
    waived: tuple[str, ...]
    missing: tuple[str, ...]
    inconclusive: tuple[str, ...]
    code_failures: tuple[str, ...]
    infra_failures: tuple[str, ...]
    #: C01: checks whose name matched MULTIPLE distinct workflow identities with DISAGREEING
    #: conclusions — which workflow is "the check" cannot be decided by picking the newest
    #: run number; the proof stays unknown.
    ambiguous: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        return not (
            self.missing
            or self.inconclusive
            or self.code_failures
            or self.infra_failures
            or self.ambiguous
        )

    @property
    def unproven(self) -> tuple[str, ...]:
        """Required checks with no explicit verdict — unknown, never green."""
        return tuple(sorted((*self.missing, *self.inconclusive, *self.ambiguous)))

    def summary(self) -> str:
        """The human one-liner for the honest ``unknown`` verdict."""
        if self.verified:
            proven = ", ".join(self.succeeded)
            detail = f"required checks succeeded: {proven}"
            if self.waived:
                detail += f" (waived conclusions: {', '.join(self.waived)})"
            return detail
        parts = []
        if self.missing:
            parts.append(f"required checks not run: {', '.join(self.missing)}")
        if self.inconclusive:
            parts.append(f"required checks inconclusive: {', '.join(self.inconclusive)}")
        if self.ambiguous:
            parts.append(
                "ambiguous_check_identity (same display name, disagreeing workflows): "
                f"{', '.join(self.ambiguous)}"
            )
        return "; ".join(parts)


def evaluate_positive_proof(
    required_jobs: Sequence[str],
    observations: Mapping[str, str | None],
    *,
    success_conclusions: frozenset[str] = GITHUB_SUCCESS_CONCLUSIONS,
    code_failure_conclusions: frozenset[str] = GITHUB_CODE_FAILURE_CONCLUSIONS,
    infra_conclusions: frozenset[str] = GITHUB_INFRA_CONCLUSIONS,
    waived_conclusions: frozenset[str] = frozenset(),
    ambiguous_checks: Mapping[str, Sequence[str]] | None = None,
) -> PositiveProof:
    """Classify one candidate's observed check conclusions into the proof."""
    success = frozenset(name.lower() for name in success_conclusions)
    code = frozenset(name.lower() for name in code_failure_conclusions)
    infra = frozenset(name.lower() for name in infra_conclusions)
    names = tuple(sorted(required_jobs)) if required_jobs else tuple(sorted(observations))
    succeeded: list[str] = []
    waived: list[str] = []
    missing: list[str] = []
    inconclusive: list[str] = []
    code_failures: list[str] = []
    infra_failures: list[str] = []
    ambiguous: list[str] = []
    for name in names:
        if name in (ambiguous_checks or {}):
            # C01: the name matched multiple workflow identities whose conclusions disagree
            # — no single occurrence is THE check.
            ambiguous.append(name)
            continue
        if name not in observations:
            missing.append(name)
            continue
        conclusion = (observations[name] or "").strip()
        lowered = conclusion.lower()
        if lowered in success:
            succeeded.append(name)
        elif lowered in infra:
            infra_failures.append(name)
        elif lowered in code:
            code_failures.append(name)
        elif lowered in waived_conclusions:
            waived.append(name)
        else:
            # skipped/neutral/stale/unknown/absent conclusion: no proof.
            inconclusive.append(name)
    return PositiveProof(
        required=names,
        succeeded=tuple(succeeded),
        waived=tuple(waived),
        missing=tuple(missing),
        inconclusive=tuple(inconclusive),
        code_failures=tuple(code_failures),
        infra_failures=tuple(infra_failures),
        ambiguous=tuple(sorted(ambiguous)),
    )


# subject binding + freshness — a verdict is applicable ONLY to the EXACT candidate (and
# tested world) its subject names.


def subject_digest_of_evidence(verification: Mapping[str, Any] | None) -> str:
    """The candidate digest a persisted verdict's subject claims ("" when
    the verdict carries no usable subject identity)."""
    if not isinstance(verification, Mapping):
        return ""
    subject = verification.get("subject_identity")
    if not isinstance(subject, Mapping):
        return ""
    return str(subject.get("candidate_digest") or "")


def verdict_freshness(
    verification: Mapping[str, Any] | None,
    candidate_digest: str,
    *,
    environment_profile_digest: str = "",
) -> str:
    """The freshness status of a PERSISTED verdict for the current candidate (one of the ``FRESHNESS_*`` constants)."""
    if not isinstance(verification, Mapping):
        return FRESHNESS_UNKNOWN
    subject = verification.get("subject_identity")
    if not isinstance(subject, Mapping):
        return FRESHNESS_UNKNOWN
    recorded_candidate = str(subject.get("candidate_digest") or "")
    if not recorded_candidate:
        return FRESHNESS_UNKNOWN
    if not candidate_digest or recorded_candidate != candidate_digest:
        return FRESHNESS_STALE
    recorded_env = str(subject.get("environment_profile_digest") or "")
    if recorded_env and environment_profile_digest and recorded_env != environment_profile_digest:
        return FRESHNESS_STALE
    return FRESHNESS_CURRENT


def render_stale(
    verification: Mapping[str, Any],
    reason: str,
    *,
    now: datetime | None = None,
    applicability: Mapping[str, Any] | None = None,
) -> dict:
    """Re-render a persisted verdict whose subject no longer binds."""
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    rendered: dict = {
        "status": STATUS_STALE,
        "tested_oid": str(verification.get("tested_oid") or ""),
        "observed_at": stamp,
        "producer": str(verification.get("producer") or ""),
        "summary": f"stale: {reason}",
        "superseded_reason": str(reason),
    }
    subject = verification.get("subject_identity")
    if isinstance(subject, Mapping):
        rendered["subject_identity"] = dict(subject)
    if applicability is not None:
        rendered["applicability"] = dict(applicability)
    return rendered
