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
