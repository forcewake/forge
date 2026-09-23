# E2E qualification layers for CI/CD pipelines — research (2026-09-23)

> E2E-qualification research for forge, topic 1 of 6. Sources: modern
> test-pyramid/honeycomb practice write-ups (2026), Azure DevOps
> approvals-and-checks documentation, ephemeral-environment guides,
> CI evidence-collection practice, CI secrets-management guidance;
> fetched 2026-09-23. Confidence marks:
> **[documented]** / **[observed]** / **[inference]**.

## Why it matters for forge

forge qualifies *itself* the way it will ask customers to qualify
*it*: recipes + harnesses must be provably good before they are
promoted (R32-13), release artifacts must be assembled from executed
evidence (R32-19), and the design-partner pilot (R32-21) will be judged
on evidence forge produces about its own runs. Today forge has smoke
suites, failure-injection suites and the DriverMatrix seeded from live
evidence — but no codified layering (smoke → contract → integration →
acceptance) that says *which suite gates which promotion*. The industry
answer to "when is a change qualified" is remarkably converged and
directly transplantable.

## Findings

### 1. The canonical layer stack and where the weight belongs

[documented] ([modern test pyramid 2026](https://sdetlab.com/blog/modern-test-pyramid-2026-complete-strategy),
[microservices honeycomb](https://softwaretestpilot.com/blog/api-testing/microservices-testing),
[testing approaches blueprint](https://forgesdlc.com/discipline-testing.html))

- The 2026 consensus is **no longer a strict pyramid** for
  distributed/multi-service systems: it is a **honeycomb** — heavy
  middle band of **integration + contract tests** (Pact +
  Testcontainers ≈ 55 %), lean unit layer (~40 %), and a *small,
  curated* E2E layer (≤ 5 %, "one E2E test per revenue-critical
  journey, no more than ~30 total for a mid-size platform").
  Rationale: in multi-service systems "most bugs live between
  processes — wrong payload shapes, missing headers, retries that
  duplicate a charge", so the seam tests deserve the mass.
- Sequencing is fail-fast: static analysis + unit on every commit →
  integration + contract on MR → deploy to ephemeral/staging →
  **smoke on deploy** → performance gate → nightly full E2E +
  chaos. A 45-minute PR pipeline "trains the team to skip tests; a
  6-minute one trains them to write more".
- The anti-pattern with a name: the **ice-cream cone** (E2E-heavy,
  unit-thin) — slow, flaky, half the suite skipped. forge's equivalent
  risk is qualifying everything through full live-driver E2E runs when
  a DriverMatrix-style smoke + per-driver contract tests would catch
  the same drift cheaper.

### 2. What a "gate" actually is (and how it gets teeth)

[documented] ([CI/CD test gating](https://frontendcomponent.com/ci-cd-gating),
[API testing in CI/CD](https://totalshiftleft.ai/blog/api-test-automation-with-ci-cd-step-by-step-guide),
[CI/CD checklist: gates, approvals, rollback](https://kuryzhev.cloud/2026/07/23/ci-cd-checklist-quality-gates-approvals-and-rollback-paths))

- Vocabulary that prevents misconfiguration: a **stage** produces
  results, a **gate** aggregates them, a **required status check**
  makes the gate binding, **threshold tiers** (hard-fail / warn /
  auto-approve) decide what counts as failure. "Break any link and the
  gate leaks."
- The load-bearing pattern on GitHub Actions: an aggregate **`gate`
  job with `needs: [all jobs]` and `if: always()`** that fails if any
  leg failed — and branch protection requires *that one job*, never
  individual matrix legs (leg names change when you edit the matrix,
  silently un-gating the pipeline). GitLab equivalent: a final gate
  stage + "Pipelines must succeed" on MR settings.
- Four ways gates rot: **advisory drift** (red on main for weeks),
  **flaky-so-ignore** (re-run-until-green muscle memory), **slow-so-skip**,
  **matrix blind spots** (required check only sees one leg).
- Blocking set in practice: block on smoke failure, contract failure,
  security criticals, schema-drift breakages; **warn** on flake
  thresholds and sub-budget performance regressions; policy expressed
  in code (pipeline YAML or OPA/Conftest) so gates are auditable.

### 3. Azure DevOps environments: qualification gates owned by the resource, not the pipeline

[documented] ([Azure Pipelines approvals & checks](https://docs.microsoft.com/en-us/azure/devops/pipelines/process/approvals),
[environments/gates practice](https://grizzlypeaksoftware.com/library/deployment-gates-and-pre-deployment-approvals-erqe6g8n))

- Checks live **on the environment resource (web UI, resource owner),
  not in the YAML** — a deliberate security boundary: "the pipeline
  author writes only `environment: production`; a developer editing
  the pipeline file cannot grant themselves a shortcut past
  production." Static checks (branch control, required template,
  evaluate artifact) run first, then approvals, then dynamic checks
  (Invoke REST API, Azure Monitor alerts, business hours), then
  exclusive lock.
- **Approvals gate *who*; checks gate *what*** — mature production
  doors have both; self-approval must be restricted or separation of
  duties collapses.
- Gates **re-evaluate on a schedule** (delay → interval → timeout →
  minimum duration/soak); "minimum duration" means a health gate must
  pass *consistently* across evaluations, not once — sustained-health
  semantics, not point-in-time.
- REST-API checks make **"did the qualification evidence pass" an
  external, machine-answerable query** — the pattern forge's promotion
  gate should mirror (query the evidence store, don't re-run tests).
- Deployment history on environments = the audit ledger: "which
  pipeline, which commit, which work items, at what time" — one
  accountable record per environment.

### 4. Ephemeral vs persistent environments — the decision matrix

[documented] ([ephemeral environments guide](https://qajobfit.com/resources/ephemeral-test-environments-complete-guide),
[previews vs staging](https://netbayhosts.in/blogs/cicd-test-environments-previews-vs-staging),
[test env management](https://astaqc.com/software-testing-blog/test-environment-management-2026-isolate-provision-teardown))

- Three models: **shared** (cheap, high-contention, accumulates
  state), **ephemeral per run/PR** (isolated, parallel-safe, needs
  provisioning+teardown tooling), **per-branch preview** (ephemeral
  scoped to branch lifetime, supports human review). Ephemeral is "the
  standard model for CI/CD pipelines that run tests on multiple
  branches concurrently".
- The two-pattern healthy flow: **per-PR ephemeral previews** answer
  "does this change work?", **one persistent staging** answers "does
  the merged artifact compose with everything else?" — same immutable
  artifact promoted by digest; "only a green staging run earns the
  right to promote".
- Ephemeral environment as **state machine** (requested → building →
  provisioning → ready → testing → retained → deleting), every
  operation idempotent/convergent; isolation ladder:
  namespace-per-PR (practical baseline) → cluster-per-PR →
  account-per-PR (compliance-grade).
- **Teardown is the most-neglected part**: trigger (CI event/PR close)
  + reverse-IaC script + **safety net** (TTL reaper job + tag-based
  cleanup + cost alerts); "test cleanup through deliberate failure
  injection". Export evidence *before* deleting the environment.

### 5. Credentials in test environments

[documented] ([secrets management in CI for tests](https://qajobfit.com/resources/secrets-management-in-ci-for-tests),
[OIDC for preview environments](https://full-stack-app.com/preview-environments-environment-parity/secrets-injection-for-preview-environments),
[CI/CD secrets patterns](https://devsecopsatlas.com/guides/ci-cd-secrets-management-patterns))

- Stack ranking: **OIDC/workload identity first** (job proves identity,
  receives minutes-lived scoped credential — "nothing durable to
  leak"), then environment-scoped secrets behind a vault, then dynamic
  per-job credentials (e.g. Vault database roles). "The best CI/CD
  secret is the one the pipeline never has."
- **Purpose-built least-privilege test accounts** per flow
  (`qa.smoke.reader`, `qa.checkout.buyer`, `qa.api.contract`) instead
  of one shared admin password; "rotation then becomes an all-hands
  outage" is the failure being avoided. forge already ships
  registry-driven sentinel-credential tests — this is the same
  principle generalized.
- Preview-environment rules that transfer to agent lanes: the preview
  role reads **only the preview path, never a production path, even
  read-only**; credential TTL ≈ environment lifetime; identity bound
  to the PR context, not the repo; injection at boot (env/secret
  mount), never baked into the image. Fork-PR/untrusted-code rule:
  secrets unavailable, mocks/public fixtures only.
- The erosion path is social: "someone widens the preview role to
  reach a production path 'just to check something', the check works,
  and the widening is never reverted because nothing fails
  afterwards." Guard with policy-as-code, not review memory.

### 6. Evidence collection as a first-class pipeline stage

[documented] ([test evidence for regulated teams](https://buildpulse.io/blog/test-evidence-compliance-ci-audit-trail),
[publish test evidence as CI artifacts](https://qajobfit.com/resources/publish-test-evidence-ci-artifacts),
[SOC 2 CC7.1 test evidence](https://industrialmonitordirect.com/zh-hant/blogs/knowledgebase/proving-soc2-cc71-application-testing-evidence-for-audits))

- Auditors' four asks map exactly to forge's promotion-gate needs:
  **retention** (raw test output tied to commit SHA + build ID — "a
  screenshot of a green pipeline is not an artifact"), **coverage of
  defined requirements** (tests labeled to requirements, not just
  passing), **immutability** (S3 Object Lock / signed attestations),
  and **honest pass/fail semantics**.
- The quiet-corruption trap: **retries overwrite evidence**. "A test
  that failed twice and passed on the third attempt reports green; the
  JUnit XML from the final retry has overwritten the failure data."
  For a qualification gate, a retry-passed run must be recorded as a
  *conditional* pass with the failure history retained — forge's
  idempotent-receipt ledger discipline is the right substrate; the
  rule to add is "attempt history is part of the verdict".
- Upload evidence `if: always()` (failures are evidence too); split
  retention by diagnostic weight (JUnit long, traces/screenshots
  short); scan evidence for planted sentinel secrets before publish.
- Every run should emit a **run manifest / provenance record**:
  git SHA, runner version, environment identity, tool versions,
  timestamps, seeds — "if it varies run-to-run, it is an artifact, not
  source" ([auditable API test runs](https://dev.tools/blog/auditable-api-test-runs-what-to-store-junit-logs-har-yaml-artifacts)).

## Concrete recommendations (ranked by effort/impact)

1. **Name forge's qualification layers and bind them to promotions
   (low effort, high impact).** Adopt the four-layer vocabulary —
   smoke (per driver/recipe, minutes), contract (per driver↔forge
   boundary), integration (Testcontainers DB/broker), acceptance
   (curated live-driver journeys, capped count) — and state in the
   promotion rules which layer's evidence gates which transition
   (recipe experimental → qualified; harness candidate → promoted).
   The honeycomb's "≤ 5 % E2E, curated" rule directly answers how many
   live-driver E2E runs a qualification requires before they stop
   adding signal. [inference — implements R32-13/R32-19]
2. **Aggregate-gate pattern for forge's own CI (low effort).** Add a
   single aggregate `gate` job (`if: always()`, checks all legs) per
   lane workflow and make branch protection require that job only —
   prevents matrix-leg drift from silently un-gating. Mirrors
   [documented] GitHub/GitLab practice.
3. **Resource-owned checks, not pipeline-owned (medium effort,
   strategic).** Follow the Azure DevOps split: the promotion
   authority (capability manifest, evidence store) owns the checks;
   the pipeline author only *names* the target. Implement "qualified?"
   as an external machine query ("does passing evidence exist for this
   exact recipe×harness×version tuple") — the `can-i-deploy` shape
   from topic 6 — rather than re-running suites at promote time.
   [inference]
4. **Attempt-honest verdicts in the evidence ledger (medium effort).**
   Record per-run retry/flake history and make retry-passed
   qualification runs a distinct verdict class (conditional pass)
   instead of overwriting with green. This is the single cheapest
   defense of evidence trustworthiness. [documented pattern, inference
   application]
5. **Ephemeral-per-qualification-run environments with TTL reaper
   (medium effort).** forge's E2E qualification runs already need
   disposable service graphs; adopt the state-machine + idempotent
   teardown + TTL-reaper + evidence-before-delete lifecycle so
   qualification runs never share mutable state. Persistent "staging"
   analog = the pinned reference environment for composition
   acceptance only. [documented patterns; see topic 2]
6. **Sentinel-credential discipline per environment class (low
   effort).** Extend the existing registry-driven sentinel-credential
   tests with the scoping rules: preview/qualification credentials
   read only preview paths, never production, even read-only; TTL tied
   to run lifetime; seed guard rejects non-qualification endpoints
   (the "refusing to seed a non-preview database" pattern).
   [documented, aligns with existing forge practice]

Relationship to existing plans: this topic supplies the layer
vocabulary and gate mechanics for R32-13 (recipe+harness
qualification) and R32-19 (release artifacts from qualified evidence);
topics 3 (evidence chains) and 6 (two-writer gating) build on it.
