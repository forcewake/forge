# 0007 — Draft MR created before waiting on required CI

- **Status:** Accepted
- **Date:** 2026-09-12

## Context

forge verifies candidate changes through merge request pipelines. GitLab
semantics create an ordering trap here:

- A pipeline configured for merge requests (`merge_request_event` jobs) is
  only triggered **by the existence of an open MR**. Waiting for such a
  pipeline before creating the MR deadlocks: the run waits forever on a
  pipeline that can never start.
- MR pipelines validate the source branch against the target; they do not
  automatically prove the result of merging into an up-to-date target.
- Conversely, projects whose pipelines run automatically on push already have
  a pipeline after every commit. Blindly calling the create-pipeline API on
  top of that duplicates work and confuses evidence collection.
- Draft MR rules can also skip jobs; a project where required checks are
  skipped for drafts will never produce the evidence forge needs.

## Decision

The Draft MR is created **before** the run waits on required CI:

1. After the first real candidate commit, forge creates (or finds) the Draft
   merge request for the run's branch. No MR is created before there is a
   real change — never an empty MR just to start a pipeline.
2. The run then waits for / creates the correct pipeline for the project's
   chosen mode, with reconciliation if creation was needed.
3. **One pipeline mode per project, chosen at onboarding:** either pipelines
   arise automatically from push/MR events, or forge explicitly creates them
   through the correct API. The run never blindly calls create-pipeline after
   a commit when a push/MR has already started a suitable pipeline.
4. The onboarding `doctor` also detects rules that skip required jobs for
   Draft MRs.
5. A missing pipeline is **`blocked_ci_configuration`**, never treated as
   success. Silence from CI does not prove anything.

## Consequences

- **Positive:** no deadlock between "waiting for CI" and "no MR exists";
  evidence is collected from the pipeline that actually belongs to the
  candidate SHA; duplicate pipelines are avoided; misconfigured projects are
  flagged at onboarding instead of mid-run.
- **Negative:** a Draft MR exists earlier, so humans see work in progress
  (intended — it is the review artifact); projects must pick and keep a
  single pipeline mode, and mode drift must be re-checked by `doctor`;
  explicit pipeline creation needs reconciliation because creation may
  succeed even if the response is lost
  ([ADR-0005](0005-durable-execution-and-unknown-outcome.md)).
- What counts as "the pipeline passed" is governed by the quality contract in
  [ADR-0008](0008-quality-contract-instead-of-pipeline-status.md).
