"""Verification profile + verdict evidence (F19, ADR-0018 §5; R02 honesty).

Two concerns live here:

- :class:`VerificationProfile` is the effective verification policy built
  from settings (sorted required jobs, producer allowlist placeholder,
  freshness window). :func:`evaluate` keeps the ``(ok, reason)`` contract of
  :func:`forge.runs.ci_contract.evaluate_quality_contract`; an empty profile
  never *fails* the run, but R02 forbids presenting pipeline success as
  verified — the caller labels the run with :meth:`VerificationResult.unverified`.
- :class:`VerificationResult` is the ONE evidence dict every provider
  records under ``flow_runs.evidence["verification"]`` (``status``,
  ``tested_oid``, ``observed_at``, ``producer``). R02: verified must mean
  the same thing on GitLab, GitHub and Azure DevOps — only
  ``status="passed"`` is verified; every other form (``pending``,
  ``failed``, ``unknown``, ``not_configured``, ``unverified``) reaches the
  human labeled as what it is, never as a green check.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from forge.config import Settings
from forge.gitlab.schemas import Job, Pipeline
from forge.runs.ci_contract import evaluate_quality_contract

#: Seconds a verification verdict stays fresh. Placeholder for the producer
#: allowlist wave: the reconciler evaluates on pipeline completion, so the
#: window bounds nothing today — it is part of the frozen profile shape.
DEFAULT_FRESHNESS_WINDOW_SECONDS = 60

#: The unified verdict vocabulary (R02). ``passed`` is the ONLY verified
#: status; ``unverified``/``not_configured`` are the honest no-CI forms.
STATUS_PENDING = "pending"
STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_UNKNOWN = "unknown"
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_UNVERIFIED = "unverified"

#: The reporting system behind a verdict — part of the evidence so an
#: operator can tell WHO claimed a check passed.
PRODUCER_GITLAB_PIPELINE = "gitlab-pipeline"
PRODUCER_GITHUB_CHECKS = "github-checks"
PRODUCER_AZURE_BUILD = "azure-build"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class VerificationResult:
    """A provider-independent verification verdict for one candidate commit.

    ``as_evidence`` is the exact dict stored under
    ``flow_runs.evidence["verification"]`` — the same key shape on every
    provider (R02). ``summary``/``surface`` are optional detail: the failed
    check names, or the per-job/build results backing the verdict.
    """

    #: One of the ``STATUS_*`` constants above.
    status: str
    #: The candidate commit oid the verdict is bound to (ADR-0008: a verdict
    #: approves a *specific* commit, never a branch).
    tested_oid: str
    #: ISO-8601 UTC moment the verdict was observed.
    observed_at: str
    #: The reporting system (a ``PRODUCER_*`` constant or provider equivalent).
    producer: str
    #: Human-readable one-liner (failed check names, contract reason, ...).
    summary: str = ""
    #: Optional per-check details (name/status or name/result entries).
    surface: tuple[dict, ...] = field(default=())

    def as_evidence(self) -> dict:
        """The evidence-fragment dict (the unified R02 shape)."""
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
        return evidence

    @property
    def verified(self) -> bool:
        """R02: only a passed verdict counts as verified — nothing else."""
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
    ) -> VerificationResult:
        return cls(STATUS_PASSED, tested_oid, _utc_now_iso(), producer, summary, surface)

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

    #: CI job names that must exist AND succeed (ADR-0008). Empty means the
    #: deployment has no verification profile — pipeline success only, which
    #: R02 labels honestly as *unverified* (never as verified).
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
    """Evaluate the verification profile for a finished pipeline.

    Same ``(ok, reason)`` contract as the ADR-0008 quality contract. An empty
    profile is ``ok=True`` with a warning reason — the caller records the
    honest :meth:`VerificationResult.unverified` verdict instead of claiming
    a verification that never ran. A non-empty profile enforces every
    required job exactly as before.
    """
    if not profile.required_jobs:
        return True, "no verification profile configured (warning)"
    return evaluate_quality_contract(pipeline, jobs, profile.required_jobs)


# ----------------------------------------------------------------------
# A01: the positive-proof contract (GitHub Actions / Azure Pipelines)
# ----------------------------------------------------------------------
#
# Verification is POSITIVE PROOF that the required checks RAN and SUCCEEDED
# for the exact candidate commit — never "nothing failed". The frozen
# executable spec (``forge.runs.spec``) carries the required-checks list and
# the harness lane identity (``harness_workflow`` / ``lane_pipeline_id``);
# the provider services observe the candidate's checks and classify every
# conclusion through :func:`evaluate_positive_proof` below. Verdict rules:
#
# - verified ONLY when every required check is PRESENT with an explicitly
#   successful conclusion for the candidate oid (or an explicitly waived
#   inconclusive one — the FORGE_VERIFICATION_WAIVE_CONCLUSIONS policy);
# - a green OPTIONAL check never substitutes for a missing required one,
#   and (missing/inconclusive) proof keeps the run waiting — it never
#   verifies and never repairs;
# - a conclusion that BLAMES THE CHANGE (failure-class) is the only repair
#   trigger; CANCELLED/TIMED_OUT conclusions are infrastructure evidence —
#   they block, never repair (the repair budget is for code failures only);
# - the verdict binds to the sha the PROVIDER verified (each observed
#   run/build carries its own head oid), never a self-asserted one.

#: GitHub Actions run conclusions that PROVE a check succeeded.
GITHUB_SUCCESS_CONCLUSIONS: frozenset[str] = frozenset({"success"})
#: GitHub Actions conclusions that carry evidence the CHANGE is wrong (the
#: only repair triggers).
GITHUB_CODE_FAILURE_CONCLUSIONS: frozenset[str] = frozenset(
    {"failure", "action_required", "startup_failure"}
)
#: GitHub Actions conclusions that mean the EXECUTION died (cancelled by a
#: human/system, runner timeout) — infrastructure, never code repair.
GITHUB_INFRA_CONCLUSIONS: frozenset[str] = frozenset({"cancelled", "timed_out"})

#: Azure Pipelines build results that PROVE a check succeeded.
AZURE_SUCCESS_RESULTS: frozenset[str] = frozenset({"succeeded"})
#: Azure build results that blame the change (not fully green is not green —
#: branch policy fails ``partiallySucceeded`` too).
AZURE_CODE_FAILURE_RESULTS: frozenset[str] = frozenset({"failed", "partiallySucceeded"})
#: Azure build results that mean the EXECUTION died (canceled/abandoned
#: validation) — infrastructure, never code repair.
AZURE_INFRA_RESULTS: frozenset[str] = frozenset({"canceled", "abandoned"})

#: The per-check proof states :func:`evaluate_positive_proof` assigns.
PROOF_SUCCEEDED = "succeeded"
PROOF_WAIVED = "waived"
PROOF_MISSING = "missing"
PROOF_INCONCLUSIVE = "inconclusive"
PROOF_CODE_FAILURE = "code_failure"
PROOF_INFRA_FAILURE = "infra_failure"

#: A01: the blocked-reason token for a cancelled/timed_out verification
#: surface — infrastructure evidence, never a code-repair trigger (the
#: ``verification_timeout``-family of honest parked reasons).
VERIFICATION_INFRA_REASON = "verification_infrastructure"


def waived_conclusions_from_settings(settings: Settings) -> frozenset[str]:
    """The policy waiver set from ``FORGE_VERIFICATION_WAIVE_CONCLUSIONS``.

    Comma-separated provider conclusions/results a deployment explicitly
    accepts on a REQUIRED check (e.g. ``"skipped,neutral"``). Lower-cased so
    either vocabulary spelling matches. Empty (default) — a skipped/neutral/
    unknown conclusion on a required check never verifies the run.
    """
    raw = str(getattr(settings, "FORGE_VERIFICATION_WAIVE_CONCLUSIONS", "") or "")
    return frozenset(name.strip().lower() for name in raw.split(",") if name.strip())


@dataclass(frozen=True)
class PositiveProof:
    """The A01 positive-proof evaluation over one candidate's observations.

    ``verified`` is True only when every required check is proven: present
    with an explicitly successful conclusion (or an explicitly waived
    inconclusive one). ``code_failures``/``infra_failures`` name the proof
    checks whose conclusion blames the change / the execution environment;
    ``unproven`` names the checks a green surface could not prove.
    """

    required: tuple[str, ...]
    succeeded: tuple[str, ...]
    waived: tuple[str, ...]
    missing: tuple[str, ...]
    inconclusive: tuple[str, ...]
    code_failures: tuple[str, ...]
    infra_failures: tuple[str, ...]

    @property
    def verified(self) -> bool:
        return not (self.missing or self.inconclusive or self.code_failures or self.infra_failures)

    @property
    def unproven(self) -> tuple[str, ...]:
        """Required checks with no explicit verdict — unknown, never green."""
        return tuple(sorted((*self.missing, *self.inconclusive)))

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
        return "; ".join(parts)


def evaluate_positive_proof(
    required_jobs: Sequence[str],
    observations: Mapping[str, str | None],
    *,
    success_conclusions: frozenset[str] = GITHUB_SUCCESS_CONCLUSIONS,
    code_failure_conclusions: frozenset[str] = GITHUB_CODE_FAILURE_CONCLUSIONS,
    infra_conclusions: frozenset[str] = GITHUB_INFRA_CONCLUSIONS,
    waived_conclusions: frozenset[str] = frozenset(),
) -> PositiveProof:
    """Classify one candidate's observed check conclusions into the proof.

    *observations* maps check name → the NEWEST conclusion observed for that
    name (the provider service owns the newest-attempt rule). The proof set
    is *required_jobs* when the frozen spec carries one; when the deployment
    froze NO contract, every OBSERVED check becomes the proof set — so even
    a lone ``skipped`` workflow never verifies without the waiver (A01 AC2).

    The waiver applies ONLY to genuinely inconclusive conclusions
    (skipped/neutral/unknown): a waiver can never turn a failure-class or
    cancelled/timed_out conclusion into proof. Conclusion matching is
    case-insensitive (GitHub's vocabulary is lowercase, Azure's is
    camelCase — ``partiallySucceeded``).
    """
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
    for name in names:
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
    )
