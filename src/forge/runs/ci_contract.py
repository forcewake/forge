"""CI quality contract (ADR-0008): classify failures, evaluate "done".

Two pure functions used by the run service's ``evaluating_ci`` branch:

- :func:`classify_failure` decides whether a negative CI verdict is a *code*
  failure (the only kind that may trigger a bounded LLM repair), an
  *infrastructure* failure (runner down, job stuck), a *config* failure or
  *unknown* (empty evidence) — LLM repairs are never burned on the latter
  three.
- :func:`evaluate_quality_contract` decides whether the run may call itself
  done: pipeline success is necessary but not sufficient — every required
  job must exist AND have succeeded (skipped/manual/allow-failed required
  jobs do not count, per ADR-0008).
"""

from __future__ import annotations

from forge.gitlab.schemas import Job, Pipeline

#: ADR-0008 failure classification buckets. "unknown" covers evidence that
#: proves nothing (no failed jobs at all) — block, never repair.
FailureClass = str  # "code" | "infrastructure" | "config" | "unknown"

#: Job failure_reason values that mean the *execution environment* failed —
#: never the code. Repairing code because a runner died is forbidden.
_INFRASTRUCTURE_REASONS: frozenset[str] = frozenset(
    {
        "runner_system_failure",
        "stuck",
        "scheduler_failure",
        "unknown_failure",
    }
)

#: Substrings of failure_reason values that mean broken CI *configuration*
#: (bad .gitlab-ci.yml, missing config) rather than broken code.
_CONFIG_REASON_SUBSTRINGS: tuple[str, ...] = ("config", "yaml")

#: The failure_reason value that means the job's script (the code) failed.
_SCRIPT_FAILURE = "script_failure"


def classify_failure(jobs: list[Job]) -> FailureClass:
    """Classify why a pipeline failed, from its jobs' ``failure_reason`` fields.

    Priority: infrastructure > config > code — if a runner died while a
    script also failed, the honest verdict is infrastructure (do not repair).
    An empty/unknown failure reason is infrastructure as well: unknown means
    the evidence does not blame the code (ADR-0008; seen live when a cancel
    or a runner death killed a job without a recorded reason).

    Empty evidence is never code: no failed job to read a reason from — the
    job list is empty or holds only canceled/skipped jobs — means the verdict
    is ``"unknown"``: nothing here proves the code is wrong, so the run is
    blocked instead of entering the repair loop.
    """
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
    """Evaluate the ADR-0008 quality contract for a finished pipeline.

    Returns ``(ok, reason)``. ``ok`` requires:

    - the pipeline status is exactly ``success`` (a failed/canceled/skipped
      pipeline never satisfies the contract), and
    - every name in *required_jobs* exists among *jobs* with status exactly
      ``success`` — ``skipped``, ``manual``, ``canceled`` or a missing job
      all fail the contract even when the pipeline icon is green.

    An empty *required_jobs* means a successful pipeline status is enough.
    """
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
