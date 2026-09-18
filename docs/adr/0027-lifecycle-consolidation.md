# ADR-0027: Lifecycle consolidation, incrementally — one lifecycle, not three copies of the factory

Status: accepted (incremental) (2026-09-17)

Context: review finding R27 — the GitLab, GitHub and Azure DevOps lanes
must differ by adapter *configuration*, not by three hand-maintained
lifecycle engines (the same finding class as
[review plan §1](../reviews/2026-09-13-v0.1.0/plan.md) and F31: extract
core contracts and independent adapters **without a big-bang rewrite**).
The review is explicit about the failure mode: every provider service
re-implements the same lifecycle invariants by hand, and each copy drifts
on its own schedule. Two shipped bugs came from exactly this:

- the `/retry` guard miss — one lane's revival logic diverged from the
  others';
- the GitHub-only ship — GitHub's verification evidence fragment was
  hand-rolled with a `candidate_sha`/`checks` key spelling while GitLab and
  Azure recorded the unified `VerificationResult` shape
  (`tested_oid`/`surface`), so GitHub's resume path needed a bilingual
  reader and the R02 contract ("verified means the same thing on GitLab,
  GitHub and Azure DevOps") held only by convention.

A full rewrite (one `RunLifecycle` class hierarchy, provider strategies)
would exchange drift for a migration cliff across three live lanes and
~4,000-line service modules each. The agreed path is incremental: extract
the shared *invariants* into provider-neutral core first, rewire the
services onto them one slice at a time, and let the use-case boundaries
emerge from the extracted code instead of a big-bang redesign.

## Decision

1. **The lifecycle is ONE use-case ladder, owned by core.** Every
   provider lane is an adapter that walks the same seven use cases over
   the durable
   `Controller`/`FlowRun` state machine:

   | Use case | Invariant core owns | GitLab home today | GitHub/Azure home today |
   |---|---|---|---|
   | `PlanRun` | task snapshot digest, plan evidence, harness selection (ADR-0023) | `forge.runs.service` planning leg | same leg, provider I/O swapped |
   | `ApproveDecision` | gate TTL/expiry, one-shot consumption, approver policy (ADR-0009) | `forge.runs.service` gate leg | same, per-connection approvers |
   | `ProposeCandidate` | immutable RunSpec freeze + digest verification (R04, ADR-0018 §1) | `forge.runs.spec` + validating leg | publisher boundary (ADR-0026) |
   | `PublishCandidate` | the single validated write boundary (ADR-0016/0026) | `forge.runs.publisher` | shared, migrating incrementally |
   | `ObserveVerification` | verification profile + unified verdict vocabulary (R02, F19) | `forge.runs.verification` + gate | same vocabulary, provider surface |
   | `ReviewCandidate` | readonly review bound to the candidate sha, replayed exactly once | each `_review_and_ready` half | same half, duplicated |
   | `FinalizeEvidence` | ready evidence shape, ready reason wording, iron finalization checks | **`forge.runs.consistency` (this slice)** | **shared — rewired onto it** |

2. **Slice 1 (done today): `FinalizeEvidence` consolidates into
   `forge.runs.consistency`** — the highest-duplication, already-drifted
   invariants, extracted as pure provider-neutral functions:
   - `ready_evidence(verified, tested_oid, producer, summary)` — the ONE
     verification evidence fragment; GitHub's `candidate_sha`/`checks`
     variant is deleted and both historical key spellings stay readable;
   - `ready_reason(verified, review_verdict, summary)` — the exact ready
     reason strings: the `unverified — …` prefix rule, the
     `checks passed; merge is a human decision` tail, and the R02 rule
     that an unverified run NEVER says "checks passed" (which GitLab's
     local copy violated for the empty-profile case);
   - `ready_closing_line(verified)` — the evidence-comment closing pair,
     same honesty rule on the comment surface;
   - `assert_ready_invariants(run_status, evidence, candidate_sha,
     reviewed_sha, reason)` — the iron checks: superseded/terminal runs
     never finalize; ready requires a review bound to the exact candidate
     sha (ADR-0008); a verdict for another commit never finalizes;
     unverified reasons never claim checks passed. Violations raise
     `ReadyInvariantError` — reported, never repaired.
   All three services' finalization legs (`_review_and_ready`, the
   verification gates, the resume paths) now call these functions; their
   local copies are deleted. The ready reason is now byte-identical for
   the same inputs on every provider — GitLab and GitHub/Azure verified
   readies previously said "checks passed; merge is a human decision" and
   "merge is a human decision" respectively.
3. **The import boundary is the enforceable core/adapter split, tested.**
   `forge.runs.consistency` imports no `forge.integrations.*` and no
   `forge.gateway.*`;
   `tests/test_consistency.py` parses the module's imports and fails the
   suite if a provider package leaks in. This is F31's acceptance test
   ("core does not import GitLab/GitHub SDK") applied to the first
   extracted module, so the boundary holds by CI, not by review vigilance.
4. **Sequencing of the remaining slices** (each: extract invariant →
   rewire three services → pin with a cross-provider identical-output
   test → extend the import-boundary test):
   - Slice 2 — `ObserveVerification`: the `evaluate_waiting_ci_one` triplication
     (grace window, deadline-before-I/O R17, pending/red classification,
     repair-vs-block);
   - Slice 3 — `ReviewCandidate`: the reviewing leg (persisted-review
     replay once per (run, candidate), sha binding, budget-refusal handling) —
     today duplicated near-verbatim in three modules;
   - Slice 4 — `ApproveDecision` + `PlanRun`/`ProposeCandidate`: gate
     consumption/expiry and spec-freezing legs;
   - `PublishCandidate` is already consolidated (ADR-0016/0026) and is
     the template: a named core module, services as callers, conformance
     tests as the fence.
5. **Non-goals for this slice**: no class hierarchy, no service merge, no
   signature changes to public entry points (`RunService`,
   `GitHubRunService`, `AzureRunService` keep their shapes); provider
   situational detail (the text after the `unverified — ` prefix, the
   producer constant) stays at the call site; legacy evidence written
   before this ADR stays readable (`verification_bound_sha` tolerates both
   key spellings) — no data migration.

## Consequences

- The three drift classes R27 names are closed for finalization: one
  reason table (parametrized exact-string tests), one evidence shape
  (GitHub's fragment now carries `tested_oid`/`surface`/`producer` like
  the others), one set of iron checks (raises pinned by unit tests), and
  a cross-provider suite that drives all three services' real finalization
  legs and asserts identical outputs for identical inputs.
- An unverified GitLab ready reason no longer claims "checks passed" — a
  deliberate R02 fix that falls out of having one source; GitHub/Azure
  verified readies now carry the fuller "checks passed; merge is a human
  decision" tail — the unification, visible in
  `tests/test_azure_runs.py`.
- `ReadyInvariantError` inside a finalization leg surfaces as a failed
  leg (the services' crash handling parks the run visibly) — the iron
  checks are a fence against future drift, not a new failure mode: every
  rewire keeps the pre-existing graceful `review_sha_mismatch` block
  ahead of them.
- Future lanes (a fourth provider) inherit the invariants by calling the
  same functions; a lane that bypasses them fails the identical-output
  suite, the same enforcement pattern as ADR-0026 §5.
- The remaining duplication (verification gates, review legs, gate
  legs) is now enumerable as slices 2-4 above, each small enough to land
  in one sitting — the incremental path R27 requires.
