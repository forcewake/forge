# 0008 — Quality contract instead of pipeline.status

- **Status:** Accepted
- **Date:** 2026-09-12

## Context

A green pipeline icon is not evidence. GitLab's `allow_failure` keeps the
overall pipeline green while an individual job has failed; skipped and manual
jobs can leave a pipeline green without the work having run; and a success
for a previous SHA says nothing about the current candidate. Treating
`pipeline.status == "success"` as proof that the required tests passed lets
unverified changes reach `ready` state.

The required checks differ per project; there is no universal test matrix
forge can assume.

## Decision

"Done" is defined by an explicit **quality contract**, not by pipeline status.
The project's trusted profile declares the required jobs and the evidence
artifacts they must produce (for example: build, unit tests, and the
task-specific acceptance test; additional mandatory checks for
security-sensitive tasks).

A run may declare itself **ready** only when all of the following hold:

- the candidate SHA matches the verified snapshot exactly;
- every required job actually ran and succeeded (skipped, manual, and
  allow-failed required jobs do not count);
- the expected evidence artifacts exist;
- the change scope is within the allowed paths;
- the readonly review approved the **same** SHA;
- the run is not cancelled or expired.

A pipeline with `success` for an older SHA, an empty/skipped pipeline, or a
green icon over an allow-failed required job does not satisfy the contract.

Additional rules:

- **Any new commit invalidates the previous verdict.** If the target drifts,
  the run follows the project's drift policy (warn or block); without a
  merged-result check, integration with a moved target is never declared
  proven.
- **Failures are classified**, and only code failures trigger bounded repair:
  code failure, flaky-test suspicion, infrastructure failure (runner
  unavailable, network), configuration failure (missing test secret, broken
  CI config), and policy failure are distinguished. A flaky or
  infrastructure failure does not consume code-repair attempts, and forge
  does not "fix code" three times because a runner was down.

## Consequences

- **Positive:** `ready_for_human` means a specific SHA passed a specific set
  of checks with artifacts to show for it; greenwash (allow_failure, skipped
  jobs, stale SHA success) cannot reach a human as "done"; infra flaps do not
  burn repair cycles.
- **Negative:** the contract makes projects responsible for declaring required
  jobs and artifacts at onboarding, and `doctor` must re-verify them; runs
  block or re-loop when the contract is not met, which is more honest but
  less "green" than reading one status field.
- The evidence and SHA binding build on
  [ADR-0006](0006-snapshot-isolation-and-race-protection.md); the review that
  must approve the same SHA is the readonly reviewer from
  [ADR-0004](0004-controller-owns-lifecycle-implementer-proposes.md).
