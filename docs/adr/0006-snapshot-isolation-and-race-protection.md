# 0006 — Snapshot isolation and race protection

- **Status:** Accepted
- **Date:** 2026-09-12

## Context

forge writes to branches that live in the same repository humans work in. A
run that plans its changes against one version of the code must not land them
on a different version, and must never overwrite a commit made by a human.
Several facts make this hard:

- GitLab branches offer no branch-level compare-and-swap. In the Commits API,
  `last_commit_id` is a **per-file** version precondition for update/move/
  delete actions, and `start_sha` only seeds a **new** branch. Neither gives
  an atomic "write only if the branch head is still X".
- The check-then-write sequence (read head → validate → commit) is not
  atomic: a human push can land between the check and the write.
- Multiple runs on the same issue, or two writers on the same branch, would
  interleave commits and destroy any evidence trail.

## Decision

Every run is isolated by a snapshot and owns its branch:

- **Branch per run.** The work branch is derived from the run identity, e.g.
  `factory/<issue-iid>/<run-id>`. No user-supplied text appears in the
  mandatory part of the ref.
- **Pinned snapshot.** At planning time the run records the target SHA, the
  base content hashes of every file it reads, the expected source head, and
  all candidate commit SHAs. All validation and review refer to these SHAs.
- **Expected head check before every write.** The current branch head must
  match the recorded expected head before a commit is attempted; file-level
  preconditions (`last_commit_id` for update/move/delete) are sent as well.
- **Single writer.** At most one active write-run per issue, and exactly one
  writer per bot branch. Force pushes are never used.
- **Post-write verification.** After a commit, the returned commit SHA and
  its parent are verified against expectations.
- **External change blocks, never overwrites.** If the branch head changed
  unexpectedly, the run enters `blocked_external_change` and stops writing.
  A human commit on the branch is never reverted, amended, or pushed over.
- **Honest about the race window.** Check-then-write is not declared atomic.
  The supported policy forbids concurrent external writes to an active bot
  branch; a detected race stops the run instead of being papered over.

## Consequences

- **Positive:** every verdict, review, and evidence packet is bound to an
  exact SHA; human commits on a bot branch are surfaced as
  `blocked_external_change` instead of being silently clobbered; concurrent
  runs cannot interleave commits on one branch.
- **Negative:** a human push to a bot branch stops the run and requires human
  conflict resolution — there is no automatic rebase; the expected-head check
  is optimistic (a narrow TOCTOU window remains, covered by policy and post
  -write verification, not by an atomic primitive GitLab CE does not offer).
- The pinned snapshot is produced by the ChangeSet flow in
  [ADR-0001](0001-commits-api-write-backend-changeset-contract.md); the write
  journaling and reconciliation behind these checks come from
  [ADR-0005](0005-durable-execution-and-unknown-outcome.md).
