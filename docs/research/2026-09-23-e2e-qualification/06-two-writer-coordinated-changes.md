# Two-writer coordinated change qualification — research (2026-09-23)

> E2E-qualification research for forge, topic 6 of 6. Sources:
> consumer-driven contract testing practice (Pact/broker/can-i-deploy,
> 2026), saga compensation testing guides, coordinated-deployment and
> publication-saga patterns, agent-lifecycle compensation research;
> fetched 2026-09-23. Confidence marks:
> **[documented]** / **[observed]** / **[inference]**.

## Why it matters for forge

R32-22 is the two-writer WorkPackage: "authorize reads across ten
repositories; first keep one writer; then introduce a two/three-
repository WorkPackage with **compatibility steps, publication saga
and CandidateSet integration tests**" (architecture plan §). The
industry has a precise answer to each of those three nouns:
compatibility steps = consumer-driven contracts with a compatibility
matrix; publication saga = saga with compensating transactions;
CandidateSet integration tests = the curated cross-service E2E layer.
None of it needs to be invented.

## Findings

### 1. Consumer-driven contracts: per-edge verification instead of full-stack E2E

[documented] ([contract testing for microservices](https://api-contract-testing.com/api-contract-fundamentals-tool-selection/contract-testing-for-microservices-architectures),
[CDC guide](https://johal.in/consumer-driven-contract-testing-guide),
[Pact Broker docs](https://docs.pact.io/pact_broker))

- The model: consumers record expectations (pacts, with **matchers,
  never literal values**), providers replay them against the real
  service in their own CI, a **broker** stores contracts +
  verification results + the **deployment matrix** (consumer versions
  × provider versions × environments × verification results). "It
  replaces the combinatorial integration problem with per-edge
  verification" — you never boot the whole fleet to know the seams
  hold.
- Message pacts extend the same machinery to event/command edges, so
  request-driven and event-driven boundaries feed **one deployment
  decision**. Bi-directional mode (consumer pact vs provider OpenAPI,
  static comparison) covers edges where the provider can't run a
  verifier.
- The caution list: pacts test **usage, not the whole API** ("don't
  mistake green pacts for full API coverage"); the broker is the
  source of truth for deployment safety — forgetting to publish
  verification results or record deployments makes gates falsely
  blocked or falsely allowed; matchers-not-literals prevents brittleness.
- What contracts deliberately do not cover (multi-step workflows,
  business behavior, latency/auth) stays with a **thin E2E layer** —
  the honeycomb split from topic 1.

### 2. can-i-deploy: coordinated release as a query against the matrix

[documented] ([contract testing senior level](https://senior-stack.uz/Roadmap/Programming/quality-engineering/testing/05-contract-testing/senior),
[contract testing mechanics](https://aiwisdom.dev/articles/testing-engineering/contract-testing-pact))

- `can-i-deploy --pacticipant P --version SHA --to-environment prod`
  answers: "is **every consumer currently in production** still
  verified against this version?" It is **a lookup over recorded
  results, not a test run** — "that's why it is instant and reliable."
  A failing run *names the unsatisfied consumer*, which is what a
  pipeline needs.
- The gate sequence: verification results recorded → can-i-deploy
  **before** deploy → `record-deployment` **after** deploy ("the gate
  is only as accurate as the broker's view of what's in production").
  `--retry-while-unknown` handles the two-writer race where both
  sides' pipelines run concurrently.
- The matrix makes **who-must-ship-first explicit**: if provider v3
  drops a field that staging consumer v8 reads, v3 can still deploy
  to prod (v8 isn't there yet) — but **v8 is now blocked until v3
  ships and v8 adapts. The gate stays consistent in both
  directions.** Coordinated breaking changes ride "the same change
  train" — explicit version bumps, feature flags, or dual-write
  periods.
- Expand-contract applies to APIs exactly as to databases: "add
  alongside, migrate consumers visibly (the registry shows WHO uses
  WHAT), remove only after **verified-zero usage**."

### 3. Testing the publication saga: failure injection at every step

[documented] ([saga pattern testing](https://helpmetest.com/blog/saga-pattern-testing),
[saga transaction tests](https://tessl.io/registry/testland/saga-transaction-tests),
[saga compensation state machine](http://techinterview.org/post/3233468856/lld-saga-compensation))

- The test suite for an N-step saga is the **failure-point matrix**:
  fail at step k (parametrized 1..N) → assert compensations ran for
  all completed prior steps, in reverse order, and that later steps
  were never invoked. Plus: **idempotency of every compensation**
  ("call release twice → count is 0, not −1"), **compensation-failure
  does not prevent other compensations**, and timeout-triggered
  compensation for hung steps.
- State machine persisted after every transition (PENDING →
  STEP_n_STARTED → … → COMPENSATING → COMPENSATED /
  COMPENSATION_FAILED); "if the orchestrator crashes, it resumes from
  the last durable state." Compensation is **semantic undo, not
  Ctrl+Z** — "refunding a payment does not erase the charge; it
  creates a refund record."
- The four rules a compensating action must satisfy (agent-flavored
  restatement, applies unchanged to repo-writing steps): idempotent;
  **safe to call even if the forward step never completed**; has an
  observable completion condition; must not create a new partial
  failure ("if a compensation has multiple side effects, it is also a
  saga"). One side effect per step — "test ergonomics, observability,
  and compensability all degrade by an order of magnitude when you
  stuff steps." ([compensating transactions for partial failure](https://antigravitylab.net/en/articles/agents/antigravity-agent-saga-compensation-partial-failure-recovery))
- Practical conventions: `compensation_failed` is its **own state
  with an alert** — "auto-retry three times, then page someone";
  timeouts mean "unknown result" → query upstream truth before
  compensating or retrying; deterministic idempotency keys derived
  from the saga ID; the compensations ledger written **in the same
  transaction as the side effect**. ([saga in agent SDKs](https://claudelab.net/en/articles/api-sdk/claude-agent-sdk-saga-pattern-distributed-transactions))
- Pivot classification decides recovery direction *before* the
  outage: **compensable / pivot (point of no return) / retriable**
  steps — "do not discover the pivot during an outage."

### 4. Two-writer specifics: ordering, compatibility windows, partial failure

[documented] ([DB migration compatibility matrix](https://qaskills.sh/blog/database-testing-migration-rollback-safety),
[deploy approvals practice](https://senior-stack.uz/Roadmap/Programming/quality-engineering/quality-gates/04-deploy-approvals-and-signoffs/middle))

- Every coordinated change lives inside a **compatibility window**
  ("old and new code must survive while the system is between
  shapes"), and the A0/A1 × D0/D1 matrix from topic 2 generalizes:
  writer-A-old × writer-B-new must be a *tested* state, not an
  assumed one. Mixed-state testing is where "many production bugs
  hide."
- Promotion-not-rebuild: "**the exact artifact that passed staging is
  the one promoted** — if you rebuild between stages you have
  invalidated every test that ran"; and exclusive-lock semantics for
  the shared environment ("only one change train at a time; run
  latest cancels queued").
- Cross-repo case study shape: a provider team renames one field,
  three consumers pass their own suites, "staging was green between
  deploys, and tracking pages went blank for two hours" — the failure
  contracts + matrix exist to prevent; the fix path was "pilot pair
  → prove the flow → rollout by integration criticality, E2E demoted
  to thin smoke as contracts covered each pair."
- For agent fleets writing to two repos, the emerging guidance is
  reconciliation over perfect delivery: "rather than relying on
  perfect event delivery for cascade cleanup, run periodic
  reconcilers that scan for terminal-state work whose associated
  resources were not cleaned up" — best-effort compensation fallback
  for missed events, crashed orchestrators, split-brain.
  ([agent lifecycle compensation patterns](https://zylos.ai/research/2026-05-29-agent-lifecycle-compensation-patterns))

## Concrete recommendations (ranked by effort/impact)

1. **A WorkContract registry that is forge's pact broker
   (medium-high effort, high impact — R32-22 core).** The
   architecture plan already names WorkContract; make it the unit of
   compatibility evidence: consumer repo declares what it expects of
   the peer's change (branch, schema, API, migration), provider-side
   verification runs per edge in CI, results land in a matrix keyed
   by (repo, candidate version). Promotion of a two-writer
   WorkPackage then asks the can-i-deploy question: "does passing
   verification exist for every edge of this CandidateSet at the
   versions being promoted?" — a query, not a re-run.
   [documented pattern, direct transplant]
2. **Publication saga with per-step compensations from writer #2
   (medium effort).** Model the two-repo publication as an explicit
   step list (prepare A → prepare B → integrate/verify CandidateSet →
   publish A → publish B), each step with a compensation that is
   idempotent, safe-if-forward-never-ran, and observable; persist the
   saga state in the journal after every transition (the durable core
   already supports this). Classify steps compensable/pivot/retriable
   at design time — for forge, "PR opened" is compensable (close),
   "PR merged" is the pivot after which recovery is forward-only.
   [documented]
3. **The failure-point matrix as the qualification suite for the
   saga (medium effort).** Parametrized kill/fail at each publication
   step: assert compensations for prior steps ran in reverse order,
   later steps never started, compensations are idempotent under
   replay, and compensation-failure raises its own alarm state rather
   than being folded into generic failure. This is forge's SIGKILL
   failure-injection discipline applied to the WorkPackage layer.
   [documented, composes with existing suites]
4. **Mixed-state CandidateSet integration tests (medium effort).**
   A small curated set of tests that run old-writer × new-writer
   combinations against the synthetic environments from topic 2 —
   the compatibility-window coverage that per-repo green CI cannot
   give. Cap the count (honeycomb rule); assert on the test-probe
   end-state. [documented pattern, inference composition]
5. **Ordering rule encoded, not remembered (low effort).** Encode
   who-ships-first in the matrix-driven gate: the provider of a
   breaking change ships first, consumers are blocked until they
   adapt, expand-contract with verified-zero-usage before removal.
   Surface blocked-by relationships in the operator view (topic 5).
   [documented]
6. **Reconciliation sweep as the fallback (low effort).** A periodic
   reconciler that scans for WorkPackages in terminal states whose
   remote effects (branches, PRs, deployments) were not cleaned up —
   catches missed events and crashed orchestrators without trusting
   perfect delivery. Natural `forge doctor` check.
   [documented, inference application]

Relationship to existing plans: implements the compatibility steps /
publication saga / CandidateSet integration named by architecture
plan §9.2 and the two-writer slice in R32-22; consumes topic 2's
environments and feeds topic 5's blocked-by surfacing.
