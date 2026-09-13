"""Verification profile (F19, ADR-0018 §5).

The ADR-0008 quality contract asked "did the required jobs pass?" — but an
EMPTY ``FORGE_REQUIRED_JOBS`` list silently meant "a green pipeline icon is
enough". The profile makes that stance explicit and inspectable:

- :class:`VerificationProfile` is the effective verification policy built
  from settings (sorted required jobs, producer allowlist placeholder,
  freshness window);
- :func:`evaluate` keeps the ``(ok, reason)`` contract of
  :func:`forge.runs.ci_contract.evaluate_quality_contract`, but an empty
  profile never fails the run — the caller must label the evidence comment
  with the "no verification profile configured" warning instead.
"""

from __future__ import annotations

from dataclasses import dataclass

from forge.config import Settings
from forge.gitlab.schemas import Job, Pipeline
from forge.runs.ci_contract import evaluate_quality_contract

#: Seconds a verification verdict stays fresh. Placeholder for the producer
#: allowlist wave: the reconciler evaluates on pipeline completion, so the
#: window bounds nothing today — it is part of the frozen profile shape.
DEFAULT_FRESHNESS_WINDOW_SECONDS = 60


@dataclass(frozen=True)
class VerificationProfile:
    """The effective verification policy a run's CI verdict is judged by."""

    #: CI job names that must exist AND succeed (ADR-0008). Empty means the
    #: deployment has no verification profile — pipeline success only.
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
    profile is ``ok=True`` with a warning reason — the caller appends the
    "no verification profile configured" warning to the evidence comment. A
    non-empty profile enforces every required job exactly as before.
    """
    if not profile.required_jobs:
        return True, "no verification profile configured (warning)"
    return evaluate_quality_contract(pipeline, jobs, profile.required_jobs)
