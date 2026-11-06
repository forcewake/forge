# Independent CI failure evidence

Forge repairs a failed candidate only when an explicitly trusted verification
job provides positive machine-readable test-failure evidence for that exact
native execution. A red job or `script_failure` alone is insufficient: setup,
runner, network and unknown failures do not authorize another paid attempt.

Set the repository's `verification_failure_jobs` to an explicit subset of its
required verification check names. Keep independent verification workflow
paths configured. The coding lane's own result never counts as verification.
The proof must retain the native job, workflow/pipeline, attempt, current
candidate SHA and bounded observation time. Missing, foreign, stale or
malformed evidence stays unknown.

## GitHub

The [verification example](../../templates/github/verification.yml) is a
separate push workflow. Adapt its dependency installation and test command to
the target project; keep its nonzero test exit and native failure status.
Use one unique configured job name. The coding lane keeps read-only repository
permissions. Only the independent reporting job receives `actions: read` and
`checks: write`, as defined by [GitHub's permission contract](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#permissions).

The release manifest provides `verification_reporter_url` and
`verification_reporter_sha256`. Provision those exact values as the example's
repository variables. Its reporter is a standalone verified release asset;
neither the server package nor a vendor SDK is needed in the verification job.
It publishes only the raw UTF-8 JUnit report into the exact native Actions
job's CheckRun `output.text`. It does not change status or conclusion. The
native API reference is [CheckRun output](https://docs.github.com/en/rest/checks/runs#update-a-check-run).

Reports must fit 65,535 UTF-8 bytes and contain real failed test cases, matching
suite counters and no execution errors, DTD or entity declarations. The reader
also checks the native Actions application, check suite, job, SHA, completion
and attempt. A report on a different check cannot authorize repair.

## GitLab

The [verification example](../../templates/gitlab/verification.yml) publishes
JUnit through native `artifacts:reports:junit`, separately from the coding
lane. Forge reads the [native test report summary](https://docs.gitlab.com/api/pipelines/#retrieve-a-test-report-summary-for-a-pipeline)
and requires the suite's exact native build ID, positive failure count, no
execution/suite errors and current pipeline/job identity. No free-form log
classification or new report webhook is used.

## Live qualification

Exercise both a passing implementation and an explicitly approved controlled
repair task. The first controlled candidate returns the wrong answer; the
unchanged independent oracle must record the actual assertion failure. The
repair must then change that source behavior and pass on a new exact candidate.
A second commit alone does not prove a repair. Synthetic HTTP fixtures verify
the local protocol; hosted reporting and paid-driver acceptance require a
separate authorized sandbox run.
