"""Classify required independent verification checks."""

from __future__ import annotations

from forge.gitlab.schemas import Job, Pipeline

#: ADR-0008 failure classification buckets. "unknown" covers evidence that proves nothing
#: (no failed jobs at all) — block, never repair.
FailureClass = str  # "code" | "infrastructure" | "config" | "unknown"

#: Job failure_reason values that mean the *execution environment* failed — never the code.
#: Repairing code because a runner died is forbidden.
_INFRASTRUCTURE_REASONS: frozenset[str] = frozenset(
    {
        "runner_system_failure",
        "stuck",
        "scheduler_failure",
        "unknown_failure",
    }
)

#: Substrings of failure_reason values that mean broken CI *configuration*
#: (bad.gitlab-ci.yml, missing config) rather than broken code.
_CONFIG_REASON_SUBSTRINGS: tuple[str, ...] = ("config", "yaml")

#: The failure_reason value that means the job's script (the code) failed.
_SCRIPT_FAILURE = "script_failure"


def classify_failure(jobs: list[Job]) -> FailureClass:
    """Classify why a pipeline failed, from its jobs' ``failure_reason`` fields."""
    failed = [job for job in jobs if job.status == "failed"]
    if not failed:
        return "unknown"

    for job in failed:
        reason = (job.failure_reason or "").strip().lower()
        if not reason or reason in _INFRASTRUCTURE_REASONS:
            return "infrastructure"

    for job in failed:
        reason = (job.failure_reason or "").strip().lower()
        if any(marker in reason for marker in _CONFIG_REASON_SUBSTRINGS):
            return "config"

    return "code"


def evaluate_quality_contract(
    pipeline: Pipeline,
    jobs: list[Job],
    required_jobs: list[str] | tuple[str, ...],
) -> tuple[bool, str]:
    """Evaluate the ADR-0008 quality contract for a finished pipeline."""
    if pipeline.status != "success":
        return False, f"pipeline status is {pipeline.status!r}, not 'success'"

    by_name = {job.name: job for job in jobs}
    for name in required_jobs:
        job = by_name.get(name)
        if job is None:
            return False, f"required job {name!r} not found in pipeline"
        if job.status != "success":
            return False, f"required job {name!r} has status {job.status!r} (must be 'success')"

    return True, "quality contract satisfied"
