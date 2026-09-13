# Contracts spec (v0.2 target)

Versioned DTOs for the forge core — plain dataclasses/Pydantic, no GitLab or
GitHub SDKs, no ORM, no FastAPI ([ADR-0016](../adr/0016-candidate-bundle-trusted-publisher.md),
[ADR-0017](../adr/0017-durable-step-runtime.md),
[ADR-0018](../adr/0018-immutable-run-spec.md),
[ADR-0019](../adr/0019-source-execution-adapters.md)).
Schemas below are the target shape; `schema_version` gates every wire.

## RunSpec (immutable, frozen at gate)

```
RunSpec
  schema_version
  run_id, attempt_id
  subject:            connection_id, provider, repository_id, issue_id
  source_snapshot:    source_base_oid, attempt_base_oid, target_branch
  plan_digest, task_digest
  policy_digest, verification_profile_digest, config_digest
  execution_profile:  executor, image_digest, template_digest, version
  harness_id, harness_version
  model_route_ref, credential_ref          # references, never secrets
  budgets:           wallclock_s, max_calls, max_tokens, commit_cycles
  approval_generation, approved_at, expires_at
```

Digests are over the canonical serialization of effective settings, not file
names. A drift between the stored spec and current settings blocks the run
(or requires re-approval) — it is never silently bridged.

## ExecutionHandle

```
ExecutionHandle
  executor_id, connection_id
  native_run_id, attempt
  run_spec_digest
  launched_at, deadline_at
```

`poll(handle)` consults only the handle. After a lost launch response,
`reconcile_launch(intent)` finds the existing native run by operation
correlation; it never speculatively creates a second pipeline.

## CandidateBundle

```
CandidateBundle
  schema_version, run_id, attempt_id, run_spec_digest
  source_base_oid, attempt_base_oid
  changes_manifest:  [ { path, operation, old_blob_oid, new_content_digest, mode } ]
  artifact_ref, artifact_digest, size_bytes
  driver_exit_status, result_classification   # completed | partial | failed | aborted
  logs_ref, raw_usage_ref                     # completeness flagged
```

Artifact references resolve only from the approved artifact store; the
publisher never executes candidate content and never trusts the raw agent
git history. Unsupported features (binary, modes, LFS, submodules) are
rejected explicitly, not lossily converted.

## VerificationResult

```
VerificationResult
  run_id, attempt_id, candidate_artifact_digest
  subject_head_oid, target_base_oid, tested_oid    # three distinct revisions
  verification_profile_digest
  native_run_id, native_attempt, check_producer
  required_checks: [ { name, status, producer, definition_digest } ]
  evidence_complete: bool
  decided_at
```

`ready_for_human` requires: profile satisfied, evidence complete, head
freshness re-checked at decision time. A push after review invalidates the
result (existing invalidation, generalized).

## Invariants (the "lego" test)

Changing source host (GitLab↔GitHub), executor (CI↔Actions) or harness
(builtin↔Claude↔Codex↔Grok↔OpenCode) changes none of: who may publish, what
was approved, how cancel works, what evidence READY requires.
