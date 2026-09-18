# ADR-0026: One publication boundary for every provider × backend

Status: accepted (2026-09-17)
Context: review finding R01 — the builtin GitHub publish leg
(`GitHubPublishFlow.publish_proposal`) called the proposer and went straight
to the commit API: no `validate_changeset`, no `allowed_paths` scope, no
denied-path/lockfile/size-cap checks, and none of the R08/R09 digest
verification the rebuilt :mod:`forge.runs.candidate` provides. The same
candidate was safe on GitLab builtin (the service's validating leg +
[ADR-0016](0016-candidate-bundle-trusted-publisher.md) trusted publisher) and
on both harness lanes (materialize → `validate_changeset` → allowed_paths in
the GitHub/Azure services) but could touch `.gitlab-ci.yml` or
`.github/workflows/` on GitHub builtin. A security property that depends on
which provider×backend pair happened to run is not a property — [ADR-0016]
§2 said the publisher is *the single write boundary*; only the GitLab path
actually crossed it.

## Decision

1. **One application-service entry**:
   :func:`forge.runs.publisher.publish_validated_candidate`(run, candidate,
   …) owns the whole publication sequence — run-state checks (frozen attempt
   base, publication grant, RunSpec digest, Stage-B fence) → boundary
   validation → the native adapter (the journaled/reconcilable writer, or a
   provider transport callable) — and returns the machine-readable
   :class:`PublishResult`. `publish_candidate` becomes its GitLab-native
   binding (base-content reads + `ChangesetWriter`); other backends migrate
   onto it incrementally without changing who may publish or what was
   approved ([contracts-v0.2](../specs/contracts-v0.2.md) invariant).
2. **The pure boundary check is shared**:
   :func:`forge.runs.publisher.validate_candidate_bundle`(bundle,
   base_contents, branch, commit_message, allowed_paths) is the one policy
   implementation, used by every backend:
   - strict materialization of the :class:`CandidateBundle` against the
     AUTHORITATIVE full base contents — `base_blob_digest` verified before
     anything is applied (`stale_base`), `intended_digest` after
     (`result_digest_mismatch`), hunks applied with no fuzz (R08/R09);
   - :func:`validate_changeset` — denied paths/prefixes/lockfiles,
     `MAX_CHANGES`, `MAX_CHANGE_BYTES`, existence rules, and the RunSpec's
     frozen `allowed_paths` globs.
   Failures raise :class:`CandidateError` (materialization) or
   :class:`PolicyViolation` (write policy) — reported, never repaired.
3. **The validated wrapper is the capability**:
   :class:`forge.runs.publisher.ValidatedCandidate` is constructed only by
   the boundary check, and provider transports accept ONLY it —
   `GitHubPublishFlow.publish_validated` refuses a raw `ChangeSet` by type.
   There is no route from a candidate to a commit API that does not cross
   the check.
4. **The builtin GitHub bridge crosses the boundary too**:
   `publish_proposal` wraps the proposer's output with
   `bundle_from_changeset` and materializes it against base contents read at
   the frozen `expected_head` before ANY commit-API call; the legacy
   `publish_changeset` transport re-runs the same boundary on everything it
   receives (it never trusts its caller's claim of validation — defense in
   depth while the frozen harness-lane caller still passes a raw
   `ChangeSet`). A rejection is the blocked-class outcome
   (`candidate_invalid` / `changeset_invalid`) with **zero** mutations.
5. **The negative conformance suite is the enforcement**
   (`tests/test_publication_boundary.py`): parametrized over the publish
   paths the test fakes support, every policy-violating shape (denylisted
   path, `.github/` prefix, lockfile, out-of-scope path, too many files,
   oversized content, traversal) must produce zero commit-API calls on every
   path, and the positive path must publish exactly the validated manifest.
   A path that bypasses the boundary fails the suite, not just an audit.

## Consequences

- Denylisted/out-of-scope/oversized/corrupt candidates are unpublishable on
  every provider×backend — the lego test holds for the write policy, not
  just for identity and evidence.
- The builtin GitHub path gains R08/R09 digest verification for free; a
  stale base or a bad applier is a blocked run instead of a silently
  corrupted commit. The branch-CAS commit and Draft PR dedup semantics
  (ADR-0016 §3) are unchanged.
- GitHub builtin rejections set the outcome's blocked-class flag (`drift`)
  so the caller's existing drift→BLOCKED mapping lands them where the other
  paths land: a blocked run with a `candidate_invalid`/`changeset_invalid`
  reason.
- Base-content reads moved inside the boundary for GitHub (contents API via
  the repository reader); they are read-only and idempotent, so validation
  before write adds no new failure modes.
- Non-goals: rewiring the Azure service and the run services onto
  `publish_validated_candidate` (they own their validating legs today and
  migrate incrementally); deleting the raw-`ChangeSet` `publish_changeset`
  signature (blocked on the frozen harness caller — its guard is that the
  boundary re-validates whatever arrives); provider-side allowed_paths
  resolution on the bridge (the frozen scope is passed in by the caller).
