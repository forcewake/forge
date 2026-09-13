# ADR-0016: CandidateBundle and the trusted publisher

Status: accepted (2026-09-13)
Context: external review of v0.1.0 ([review](../../reviews/2026-09-13-v0.1.0/)), findings F01–F04, F07, F20, F21.

## Decision

1. **Harnesses become proposal-only.** A harness job never receives remote
   write credentials. It reads a frozen source snapshot, works in a local
   ephemeral workspace, and uploads its result as a **CandidateBundle**:
   attempt base OID, changes manifest (path, operation, old blob OID, new
   content digest), artifact reference + digest, driver exit classification,
   log and usage references. The `.git/config` of the workspace carries no
   push credentials; the raw agent git history is not the deliverable.
2. **One trusted publisher for all backends.** builtin, Claude Code, Codex,
   Grok Build and OpenCode candidates go through the same validation →
   publication boundary. The publisher does not execute code from the
   candidate: it validates the net diff against the approved ChangeSet
   policy (denied paths, CI/config files, size limits), checks the base OID
   against the approved snapshot, checks the publication grant, and only
   then performs the journaled remote write.
3. **Publication intents.** Every remote write is preceded by a persisted
   intent: operation key, run/attempt, expected parent OID, manifest digest.
   Ambiguous outcomes (lost response, 5xx after side effect) resolve by
   reconciliation against native identity + parent + content — never by an
   automatic retry of the POST, and never by commit message matching alone.
4. **Branch base is frozen.** Factory branches are created from the approved
   source snapshot OID, not from the live target branch. Repair attempts
   build on the last verified candidate OID (`attempt_base_oid`), distinct
   from the approved source base (`source_base_oid`); full-result review
   still diffs against the approved source base.

## Consequences

- `FORGE_BOT_TOKEN` disappears from harness job environments; the runner
  template keeps a read-only fetch credential at most.
- `materialize()` operates only on complete authoritative blobs — bounded
  evidence copies for the model are a separate concern
  ([F01]; oversized files refuse rather than truncate).
- A correct `FORGE_RESULT` head proves identity of an already-written
  branch, which is exactly why that write must not have been possible from
  the lane in the first place.
- Transition plan: the current direct-push templates remain available as an
  explicitly marked `unsupported-for-untrusted-workloads` lab profile until
  the publisher ships (v0.2).
