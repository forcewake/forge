# 0001 — Commits API as write backend, ChangeSet as LLM output contract

- **Status:** Accepted
- **Date:** 2026-09-12

## Context

forge does not execute user code on the control plane and does not maintain a
git working copy of target repositories. Changes must therefore reach GitLab
through an API that applies an atomic set of file actions per commit — the
GitLab Commits API.

Two failure modes must be avoided at the same time:

1. Regenerating entire files on every change is wasteful and fragile for
   large files: every repair cycle would re-emit the whole content and pay
   for it in tokens, latency, and risk of unintended rewrites.
2. Naive LLM-generated diffs (fuzzy patch application, "best effort" line
   matching) are unsafe: a patch applied at the wrong place or to the wrong
   file version produces code that was never reviewed in the form it was
   generated.

The model output is untrusted input: it must not be able to address arbitrary
projects, branches, or paths just by naming them.

## Decision

GitLab's Commits API remains the write backend: final changes are delivered
as an atomic set of file actions in a single commit. The model, however,
never speaks the GitLab API dialect. It proposes a typed **ChangeSet**, which
a trusted validation layer checks and materializes before anything is sent to
GitLab:

- **New files:** full file text is allowed.
- **Existing files:** restricted exact-match replacements, each bound to the
  SHA-256 of the current file content (`base_content_sha256`) and an
  `expected_matches` count. Ambiguous operations (zero or multiple matches)
  are rejected. Fuzzy apply is forbidden.
- Large files, binary files, LFS objects, and submodules are explicitly out
  of scope in v1.

Example of the proposed contract (illustrative JSON; forge's internal
format, not GitLab syntax):

```json
{
  "schema_version": 1,
  "base_sha": "<approved snapshot>",
  "changes": [
    {
      "path": "src/example.py",
      "operation": "replace_exact",
      "base_content_sha256": "<digest>",
      "old_text": "<exact existing text>",
      "new_text": "<replacement>",
      "expected_matches": 1
    }
  ],
  "commit_message": "Implement the approved change"
}
```

Before calling the Commits API, the service assembles the final content of
every affected file itself, so the bytes committed are the bytes the
validator produced — not a diff the model hoped would apply.

Authority is never derived from the proposal: paths, project, and target
branch are constrained by trusted policy regardless of what the ChangeSet
contains. Presence of fields in the JSON grants no permissions.

The token advantage over full-file regeneration (editing one region without
re-emitting the whole file) is treated as a hypothesis to be measured on
forge's own tasks, not as a fixed percentage to be assumed.

## Consequences

- **Positive:** every change is mechanically verifiable against a known base
  snapshot; ambiguous or stale proposals are rejected instead of silently
  misapplied; local edits do not require regenerating entire large files.
- **Negative:** the ChangeSet vocabulary is deliberately narrow — some
  refactors are not expressible in v1; rejection means an extra bounded
  repair cycle; the service must still materialize full file contents before
  the API call.
- The base snapshot discipline ties into
  [ADR-0006](0006-snapshot-isolation-and-race-protection.md); the trusted
  validator is part of the executor that enforces
  [ADR-0003](0003-no-merge-is-enforceable.md).
