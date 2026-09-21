# Forge — system-aware planning and adaptive delivery backlog

Baseline: `0.15.0 / 05868e989a5ab3ae214f905ef0681f224c1dfe5f`. Review date: 2026-09-21.

**64 proposed work items across eight epics.** This is a staged product backlog, not 64 release blockers. No repository issues were created. Proposed command names and interfaces are not current Forge features.

## Priority and execution rules

P1 is required before enabling the affected capability, not a claim that every story fixes an existing vulnerability. P2 is planned product work or hardening. Relative sizes are engineering sizing hints, not elapsed-time estimates.

Implement a vertical customer slice; do not finish every epic before demonstrating value. Follow item dependencies, not just the numeric phase labels. Existing run IDs, approval decisions and remote effects must remain recoverable during rollout.

## Global definition of done

- State the invariant and demonstrate a failing baseline before fixing an existing defect.
- Use the production service entry or real adapter constructor for boundary tests; helper-only tests are supplementary.
- Persist versioned inputs, authorization, outputs and lineage; do not derive authority from model output.
- Cover duplicate delivery, cancellation, stale revision and missing evidence wherever the story crosses those boundaries.
- Keep provider writes behind the trusted publisher and never add auto-merge or production deployment rights.
- Update runtime/profile conformance and documentation; record performed, skipped and unsupported evidence honestly.
- Maintain backward compatibility or a versioned migration; do not silently reinterpret old approvals.
- No hard performance/cost claim without the corresponding measurement population and completeness.

## Delivery phases

### P0
Foundation corrections and compatibility. Read-only discovery prototyping can proceed in parallel; do not enable new privileged write/control paths before their gates.

### P1
Repository-aware planning and questions. One writable target, one tested runtime, explicit evidence. Some planning scaffolding is shared with P2.

### P2
Adaptive single-repository delivery: revision decisions, pause/resume/steer, checkpoint portability and independent evidence.

### P3
System-level read context and bounded two/three-repository coordinated work. Reuse child runs; no generic workflow language.

### P4
Additional runtime adapters, representative ten-service acceptance, operational hardening and design-partner promotion.

## Epic FND — Authority and durable foundation

### FND-01 — Use canonical repository identity for authority reads

**P1 · defect · P0 · size M · owner: Core/platform engineer**

**Problem / outcome.** The new cache-key helper understands _owner/_repo but the real Azure reader exposes _project/_repo. Two repositories in one project can still reuse one policy entry.

**Implementation scope**

1. Introduce a public RepositoryIdentity value containing tenant, connection, provider, native repository ID and display locator
2. Implement identity() on the real GitLab, GitHub and Azure adapters; eliminate private-attribute probing
3. Resolve branch names to immutable OIDs before policy reads; key the authority cache by identity, OID, config path and policy schema
4. Recheck current read authorization on cache hits; invalidate authorization independently of immutable content caching

**Acceptance criteria**

- [ ] Actual AzureRepositoryReader instances for two repositories in one project produce different keys
- [ ] Different hosts with identical owner/repo names never share content or policy
- [ ] A new source OID cannot hit the old ref policy cache
- [ ] A revoked principal cannot retrieve cached data
- [ ] Cached absence in repository A never grants unrestricted writes in repository B

**Negative and recovery tests**

- Run the real reader constructors over fake HTTP transports; one repository has no config and the other restricts src/b/**
- Repeat with one host name, two connections and a revoked principal; assert zero unauthorized bytes and zero publish calls

**Dependencies:** None.
**Evidence/design sources:** R05, R06. See `evidence/SOURCES.md`.

### FND-02 — Fence publication at the final native-effect boundary

**P1 · defect · P0 · size L · owner: Core/platform engineer**

**Problem / outcome.** The run-aware wrapper checks cancellation before the bridge performs awaited authoritative reads and branch setup. A pause or cancel during those operations can still precede a new write.

**Implementation scope**

1. Split candidate preparation from native effect dispatch; return a validated manifest with base and policy digests
2. Require a current execution claim and publication epoch after all long reads, and serialize native-effect authorization with pause/cancel in Postgres
3. Persist the publication intent before dispatch; record accepted-but-superseded outcomes without retrying blindly
4. Load the target from the approved spec rather than live service defaults; apply the same rule to adopted PR creation

**Acceptance criteria**

- [ ] Cancel during propose, blob hydration or branch lookup results in zero subsequent commit dispatches
- [ ] A stale claim cannot publish after a new plan revision activates
- [ ] Scope is loaded from the approved run, never supplied by the agent
- [ ] A cancel after dispatch records uncertain or superseded evidence without claiming the remote effect was undone
- [ ] No adapter exposes an unfenced production write shortcut

**Negative and recovery tests**

- Pause the fake blob reader on a barrier, revoke the run in a separate transaction, then release the barrier
- Delay provider application until after local cancellation and verify reconciliation retains exactly the observed effect, not a fabricated rollback

**Dependencies:** FND-01
**Evidence/design sources:** R04, R07. See `evidence/SOURCES.md`.

### FND-03 — Make authoritative-read failures fail closed in every publisher

**P1 · defect · P0 · size M · owner: Core/platform engineer**

**Problem / outcome.** The GitHub bridge still catches broad GitHubAPIError during base reads. A read failure is not evidence that a path is absent.

**Implementation scope**

1. Use the shared typed blob result in all materializers and publishers
2. Distinguish confirmed absence from forbidden, transient failure, incomplete content and unsupported file modes
3. Validate full blob digests and strict encoding before candidate application
4. Record per-path read evidence without logging raw private file content by default

**Acceptance criteria**

- [ ] 403, 429, 5xx and truncated content never become create permission
- [ ] Missing files allow create only after an authoritative absence result
- [ ] A proposed update cannot change its operation because evidence was unavailable
- [ ] Invalid UTF-8, symlinks and oversized blobs produce explicit supported/unsupported decisions

**Negative and recovery tests**

- Inject every read failure into builtin and harness publication paths on all providers
- A failure after some files were materialized produces no partial commit

**Dependencies:** FND-01, FND-02
**Evidence/design sources:** R07, R03. See `evidence/SOURCES.md`.

### FND-04 — Define versioned capability and credential profiles

**P1 · enabler · P0 · size M · owner: Core/platform engineer**

**Problem / outcome.** Interactive planning requires role-specific controls; a driver name is not a promise of live steering, resumability or arbitrary BYOK compatibility.

**Implementation scope**

1. Create a capability record for read tools, structured output, interrupt, live input, checkpoint export, questions and usage completeness
2. Bind driver package/image digest, provider route and credential mode to a tested profile
3. Preserve absent versus explicitly empty manifests and reject incompatible role/profile combinations
4. Reference credentials by broker-owned ID; never put secret values in RunSpec or artifacts

**Acceptance criteria**

- [ ] Discovery cannot select a write-enabled profile silently
- [ ] A CLI with checkpoint-only control is never advertised as native interactive
- [ ] Unsupported SDK/provider/credential combinations fail during onboarding
- [ ] A missing selected credential does not fall back to another provider key

**Negative and recovery tests**

- Registry-driven sentinel-credential tests cover all shipped recipes
- A profile loses a capability after an upgrade; existing runs retain their pinned profile or block with an explicit incompatibility

**Dependencies:** FND-01
**Evidence/design sources:** R08, R09, R11, X03, X08. See `evidence/SOURCES.md`.

### FND-05 — Persist immutable evidence and workspace checkpoints safely

**P1 · enabler · P0 · size L · owner: Core/platform engineer**

**Problem / outcome.** Long human waits and runner recycling require durable artifacts, not local CI paths or vendor session IDs.

**Implementation scope**

1. Add a content-addressed artifact store interface with tenant-scoped authorization, retention and encryption configuration
2. Store metadata and grants in Postgres; store large diffs, logs and approved evidence in an object store
3. Enforce archive size, path traversal, symlink and content-type checks before extraction
4. Keep vendor session files optional and redact or exclude credential-bearing state

**Acceptance criteria**

- [ ] Checkpoint references resolve after the original runner is destroyed
- [ ] Cross-tenant digest equality does not grant content access
- [ ] Expired artifacts produce a recoverable explicit state, not a fresh empty workspace
- [ ] WIP manifests contain tracked changes, untracked files, deletions and source OIDs

**Negative and recovery tests**

- Test malicious archive entries, overlarge expansions and unauthorized object-store URLs
- Kill upload between content and metadata commit; recovery never advertises an incomplete checkpoint

**Dependencies:** FND-01
**Evidence/design sources:** X06, R11. See `evidence/SOURCES.md`.

### FND-06 — Make checkpoint and control-event deduplication database-enforced

**P1 · enabler · P0 · size L · owner: Core/platform engineer**

**Problem / outcome.** Interactive retries create more races than a one-shot command. A query-then-insert convention is insufficient for replay keys.

**Implementation scope**

1. Add unique keys for run/attempt/step/input digest and command source delivery ID
2. Use transactional insert-or-read behavior and explicit first-result-wins semantics
3. Record publication, checkpoint and control epochs independently
4. Separate immutable event history from mutable projections; never overwrite a historical decision

**Acceptance criteria**

- [ ] Two workers recording the same checkpoint preserve one authoritative result
- [ ] Redelivered commands do not spend another iteration or grant another approval
- [ ] Duplicate tool callbacks resolve against one question ID
- [ ] Recovery can rebuild projections without rerunning model calls whose result is durable

**Negative and recovery tests**

- Use two real PostgreSQL sessions with barriers around competing inserts
- Kill the process after event commit but before projection update; rebuilding yields the same state

**Dependencies:** FND-05
**Evidence/design sources:** R04, R10, X09, X10. See `evidence/SOURCES.md`.

### FND-07 — Add a compatibility migration for legacy and adaptive runs

**P1 · enabler · P0 · size M · owner: Core/platform engineer**

**Problem / outcome.** Existing v3 approvals must not acquire new adaptation privileges just because the software was upgraded.

**Implementation scope**

1. Introduce a new adaptive contract schema alongside v3 without rewriting old approved bytes
2. Add opt-in feature flags per connection/project and safe drain behavior for in-flight old runs
3. Define schema downgrade constraints and forward-recovery procedures for new event history
4. Validate release artifacts against both old-run replay and new-run startup

**Acceptance criteria**

- [ ] A v3 run continues under its original authority or explicitly requests reapproval
- [ ] No migration invents system read scope or plan-change permission
- [ ] Disabling adaptive mode leaves existing evidence readable
- [ ] Previous release data migrates with actual nonempty history

**Negative and recovery tests**

- Upgrade while one run is at approval, one at publication unknown and one waiting for a question
- Attempt downgrade with adaptive history and require a preflight refusal rather than partial destructive changes

**Dependencies:** FND-06
**Evidence/design sources:** R10, R13, R14. See `evidence/SOURCES.md`.

### FND-08 — Establish a production-path invariant test suite

**P1 · validation · P0 · size L · owner: Core/platform engineer**

**Problem / outcome.** The last reviews exposed defects that narrow helper tests did not see. New planning and control paths must share the same negative acceptance traces.

**Implementation scope**

1. Create provider-neutral fixtures that invoke actual service entry points and real adapter constructors
2. Run baseline and mutant on identical clocks/events; kill mutants by unchanged assertions
3. Add two-repository, two-ref, malformed-policy, stale-claim and cancel-during-read cases
4. Publish performed/skipped/unsupported results per profile and pinned SHA

**Acceptance criteria**

- [ ] Disabling the final publication fence makes the original negative test fail
- [ ] Replacing repository identity with project ID makes a real-adapter test fail
- [ ] Success at boot is never labeled live SDLC verification
- [ ] A test registration marker alone cannot close a finding

**Negative and recovery tests**

- Run selected traces on two OS worker processes and a durable fake provider service
- Reorder and duplicate deliveries; final state and authorized effects remain invariant

**Dependencies:** FND-01, FND-02, FND-03, FND-06
**Evidence/design sources:** R13, R15, R16. See `evidence/SOURCES.md`.

## Epic DSC — System-aware discovery and context

### DSC-01 — Introduce an explicit discovery stage before planning

**P1 · feature · P1 · size L · owner: Context/runtime engineer**

**Problem / outcome.** The current planner has no repository evidence. Increasing its prompt limit alone does not create project understanding.

**Implementation scope**

1. Add DiscoveryRun as a durable pre-plan stage with its own read authorization and spend allowance
2. Keep the existing text-only planner as an explicitly limited fast path for trivial or fully supplied tasks
3. Dispatch discovery through an existing CI execution profile, not inside the privileged API process
4. Return a structured evidence bundle and unresolved questions rather than code changes

**Acceptance criteria**

- [ ] Normal customer tasks cannot receive an evidence-backed plan without discovery evidence
- [ ] Discovery creates no source-control branch, commit or PR
- [ ] Restart resumes an existing discovery result instead of paying for it again
- [ ] Fast-path plans are visibly marked as not repository-researched

**Negative and recovery tests**

- Attempt a write tool or arbitrary new repository read during discovery and verify refusal
- Crash after discovery result persistence and ensure the planner consumes the same bundle

**Dependencies:** FND-04, FND-05, FND-06
**Evidence/design sources:** R02, X01, X16. See `evidence/SOURCES.md`.

### DSC-02 — Materialize immutable read-only repository snapshots

**P1 · feature · P1 · size L · owner: Context/runtime engineer**

**Problem / outcome.** A plan must cite a stable version of the code; live branch reads across a long session produce an incoherent basis.

**Implementation scope**

1. Create a SnapshotSet of authorized repository IDs and exact commit OIDs
2. Clone or hydrate snapshots only in the execution environment; keep source mounts read-only and use a separate scratch directory
3. Validate submodule, LFS, symlink and external-fetch policies explicitly
4. Strip source-control credentials after hydration; retain only artifact upload and scoped control access

**Acceptance criteria**

- [ ] Every evidence reference belongs to one recorded snapshot
- [ ] Moving main during discovery does not change the mounted source
- [ ] Discovery cannot edit sources or retrieve an unregistered submodule
- [ ] The source writer can remain API-based; clone is not added to the control plane

**Negative and recovery tests**

- Change default branches while the discovery job is running
- Seed repository hooks and external submodule URLs; verify no uncontrolled execution or download

**Dependencies:** FND-01, FND-04, DSC-01
**Evidence/design sources:** X04, X16. See `evidence/SOURCES.md`.

### DSC-03 — Provide bounded read, search and symbol tools

**P1 · feature · P1 · size L · owner: Context/runtime engineer**

**Problem / outcome.** The model needs to inspect implementations and references, not guess paths from the issue description.

**Implementation scope**

1. Expose read_file, list_paths, grep, find_symbol and find_references through a snapshot-aware tool API
2. Start with lexical search and language-native symbols; add a lightweight parser only where needed
3. Return source OID, exact path/range and completeness for each result
4. Apply authorization before search ranking and again before content hydration

**Acceptance criteria**

- [ ] No search result exposes names or snippets from an unauthorized repository
- [ ] Truncated results are explicitly marked and can be paged
- [ ] Exact symbol or API route queries locate the seeded implementation and its tests
- [ ] Output budgets never erase a failure or completeness marker

**Negative and recovery tests**

- Use ambiguous class names across repositories and ensure returned identities remain distinct
- Query beyond result/page limits and inspect a denied path via a symbol alias

**Dependencies:** DSC-02
**Evidence/design sources:** X01, R03. See `evidence/SOURCES.md`.

### DSC-04 — Extract a practical project map and test inventory

**P1 · feature · P1 · size M · owner: Context/runtime engineer**

**Problem / outcome.** Useful planning needs entry points, build commands, ownership hints, contracts and existing tests before a large semantic index.

**Implementation scope**

1. Extract manifests, solution/project files, CI definitions, public API/event schemas, migrations and test locations
2. Produce observed facts with citations and inferred relations with confidence labels
3. Keep commands as discovered metadata until a trusted execution profile validates them
4. Exclude generated/vendor artifacts by a declared policy while recording coverage gaps

**Acceptance criteria**

- [ ] The project map names actual runnable test/build candidates and their source definitions
- [ ] A README claim is distinguishable from a verified CI command
- [ ] Missing schemas or migrations appear as unknowns, not fabricated paths
- [ ] Regeneration is deterministic for a fixed SnapshotSet and extractor version

**Negative and recovery tests**

- Use a stale README and an updated CI file and require both facts with the conflict visible
- Test a monorepo with generated code and multiple test runners

**Dependencies:** DSC-03
**Evidence/design sources:** X01, X11. See `evidence/SOURCES.md`.

### DSC-05 — Build evidence-backed plans with machine-checkable citations

**P1 · feature · P1 · size L · owner: Context/runtime engineer**

**Problem / outcome.** The current plan is a short JSON guess from issue text and path hints. The proposed output must connect each material step to inspected evidence.

**Implementation scope**

1. Run a tool-using planning profile over the discovery snapshot and scratch space
2. Return plan steps, evidence_refs, assumptions, affected interfaces, tests, unknowns and decision requests
3. Validate all evidence IDs, paths, ranges and hashes outside the model
4. Preserve a concise human summary without truncating the executable structured plan

**Acceptance criteria**

- [ ] Every implementation-affecting factual assertion has resolvable evidence or an explicit assumption label
- [ ] A nonexistent files_hint cannot become a claimed existing implementation
- [ ] The human sees impacted services, testing strategy and unresolved decisions before approval
- [ ] Structured plan size is independent from the old 1,500-character display summary

**Negative and recovery tests**

- Seed a convincing but nonexistent endpoint name in the issue and require discovery to reject or qualify it
- Give a truncated tool result and ensure the plan records the gap rather than citing unseen content

**Dependencies:** DSC-03, DSC-04, PLN-01, PLN-02
**Evidence/design sources:** R02, R03, X01. See `evidence/SOURCES.md`.

### DSC-06 — Run bounded verification probes during discovery

**P2 · feature · P1 · size L · owner: Context/runtime engineer**

**Problem / outcome.** Some questions cannot be answered by file reads, but read-only planning must not acquire arbitrary execution or production access.

**Implementation scope**

1. Allow the planner to request named baseline probes from the trusted test executor
2. Run requested build/tests in disposable writable copies with synthetic dependencies and no production credentials
3. Record baseline failures separately from candidate regressions
4. Require an approved discovery budget and tool/network profile for each probe

**Acceptance criteria**

- [ ] A requested probe maps to an approved command template rather than an arbitrary shell string
- [ ] Failing baseline tests are attached before implementation begins
- [ ] A probe cannot publish changes or modify persistent shared services
- [ ] The planner receives logs and reports with execution identity and redaction

**Negative and recovery tests**

- Request a migration against a production endpoint and verify refusal
- Run a repository test with a malicious network side effect under the restricted profile

**Dependencies:** DSC-02, VER-01, VER-02
**Evidence/design sources:** X14, X04. See `evidence/SOURCES.md`.

### DSC-07 — Register systems and read-only cross-repository dependencies

**P1 · feature · P3 · size L · owner: Context/runtime engineer**

**Problem / outcome.** A customer with ten services needs system awareness before cross-repository writes. Repository count and write scope are separate dimensions.

**Implementation scope**

1. Introduce a versioned SystemManifest mapping services, repositories, APIs, events, resources and dependency edges
2. Import a simple administrative YAML first; optionally translate Backstage entities
3. Preserve provenance and distinguish declared, observed and inferred edges
4. Treat owner metadata as routing information only; authorize reads through actual connection/project policy

**Acceptance criteria**

- [ ] A work item can read ten approved repositories while only one repository is writable
- [ ] Unknown or inaccessible dependencies are visible without leaking private names/content
- [ ] An imported owner field does not grant approval rights
- [ ] Cyclic service topology is accepted as topology rather than forced into a work DAG

**Negative and recovery tests**

- Import a catalog entry that references an unauthorized repository and verify no fetch
- Rename a service and preserve stable repository/resource identities

**Dependencies:** FND-01, DSC-04, PLN-01
**Evidence/design sources:** X11. See `evidence/SOURCES.md`.

### DSC-08 — Compute bounded impact slices and retrieval coverage

**P1 · feature · P3 · size L · owner: Context/runtime engineer**

**Problem / outcome.** Loading all repositories into one context is expensive and still misses implicit contracts. Discovery should follow the change impact, not repository size alone.

**Implementation scope**

1. Traverse API/event/schema dependency edges to propose a relevant read set
2. Separate required evidence from optional exploration and cap depth, tools and bytes
3. Use small independent discovery specialists only for separable investigations; one root planner resolves contradictions
4. Store omitted dependencies, uncertainty and retrieval coverage for review

**Acceptance criteria**

- [ ] A change to a producer event discovers registered consumers and contract tests
- [ ] Parallel reports cannot extend read authority or rewrite the approved goal
- [ ] A low-confidence edge triggers an explicit question or further bounded probe
- [ ] An impact report records why unaffected services were excluded

**Negative and recovery tests**

- Create an indirect event consumer two edges away and a disconnected service; confirm appropriate selection
- Supply contradictory specialist reports and require a recorded resolution or blocking question

**Dependencies:** DSC-05, DSC-07
**Evidence/design sources:** X01, X17. See `evidence/SOURCES.md`.

## Epic PLN — Versioned plans and controlled adaptation

### PLN-01 — Separate the approved work contract from the implementation plan

**P1 · feature · P1 · size L · owner: Core/architecture engineer**

**Problem / outcome.** Freezing an entire plan as if every tactic were permanent makes legitimate adaptation indistinguishable from unauthorized scope change.

**Implementation scope**

1. Define WorkContract with goal, invariants, read/write repositories and paths, forbidden effects, budgets and acceptance rules
2. Define PlanRevision as a versioned proposal describing how to satisfy that contract
3. Assign independent digests and approval records; retain old v3 semantics for legacy runs
4. Add a plan-policy field specifying which bounded tactical changes may proceed automatically

**Acceptance criteria**

- [ ] Reordering internal implementation steps does not require silently mutating the approved contract
- [ ] Adding a write repository or weakening an acceptance rule always needs a new authorized decision
- [ ] A revision cannot grant capabilities absent from its WorkContract
- [ ] Auditors can reconstruct both the approved contract and the active plan at any point

**Negative and recovery tests**

- Attempt to hide a new database migration under a renamed tactical step
- Replay an old v3 run after upgrade and verify it receives no new adaptation privileges

**Dependencies:** FND-07
**Evidence/design sources:** R10, R04. See `evidence/SOURCES.md`.

### PLN-02 — Define a structured PlanRevision and PlanStep schema

**P1 · feature · P1 · size M · owner: Core/architecture engineer**

**Problem / outcome.** A Markdown summary is insufficient for dependency-aware execution, evidence lineage and review of changes to the plan.

**Implementation scope**

1. Define stable step IDs, objectives, repository targets, dependencies, evidence references, verification obligations and assumptions
2. Mark planned, running, done, invalidated and superseded steps without rewriting old revisions
3. Preserve human-readable renderings as projections of the structured artifact
4. Validate unknown fields and forbidden authority-bearing content strictly

**Acceptance criteria**

- [ ] The same revision renders reproducibly and retains a stable digest
- [ ] All referenced evidence and repository IDs resolve
- [ ] A step points to a goal/test obligation rather than only a generated filename
- [ ] New revisions preserve lineage of completed steps and explain invalidations

**Negative and recovery tests**

- Reorder JSON fields and verify canonical digest behavior
- Introduce a dependency cycle in execution steps and require a useful validation error

**Dependencies:** PLN-01, FND-05
**Evidence/design sources:** R02, X02. See `evidence/SOURCES.md`.

### PLN-03 — Add typed ChangeProposal and drift classification

**P1 · feature · P2 · size L · owner: Core/architecture engineer**

**Problem / outcome.** An implementer needs a sanctioned way to report that new evidence makes the current plan unsafe or incomplete.

**Implementation scope**

1. Add submit_change_proposal with reason, new evidence, affected steps, proposed contract/plan patch and preserved work
2. Classify changes using deterministic rules for repositories, paths, interfaces, data migrations, credentials, budgets and tests
3. Allow a semantic reviewer to advise, but escalate unknown classifications instead of letting the model self-authorize
4. Freeze candidate publication while a material proposal is awaiting a decision

**Acceptance criteria**

- [ ] Internal refactoring within approved scope can remain a recorded tactical revision
- [ ] API/event changes and schema migrations are material by default
- [ ] Missing classification never defaults to allowed
- [ ] The proposal includes the useful WIP and tests already completed

**Negative and recovery tests**

- Agent labels a new network destination as a trivial refactor; deterministic policy still blocks
- Submit the same proposal twice and require one pending decision

**Dependencies:** PLN-02, FND-02, FND-06
**Evidence/design sources:** X05, R04. See `evidence/SOURCES.md`.

### PLN-04 — Apply non-material revisions within preapproved bounds

**P2 · feature · P2 · size M · owner: Core/architecture engineer**

**Problem / outcome.** Asking a human to approve every renamed helper or reordered step makes the workflow slower without improving authority control.

**Implementation scope**

1. Implement a revision classifier output for bounded tactical changes
2. Commit an immutable revision and event before activating it
3. Preserve read/write scope, numerical budgets, external contracts and acceptance tests exactly
4. Show a concise change log to the operator without generating an approval notification for every tactic

**Acceptance criteria**

- [ ] A valid tactical revision activates without a new authority grant
- [ ] Scope, public contracts and test obligations compare equal before activation
- [ ] Completed step outputs remain reusable only when their input/evidence digests still match
- [ ] An ambiguous tactic is promoted to a material ChangeProposal

**Negative and recovery tests**

- Attempt to delete a required test while changing only a step description
- Crash between revision persistence and activation; recovery selects exactly one active revision

**Dependencies:** PLN-03
**Evidence/design sources:** X02. See `evidence/SOURCES.md`.

### PLN-05 — Approve material revisions with compare-and-swap semantics

**P1 · feature · P2 · size L · owner: Core/architecture engineer**

**Problem / outcome.** Approval must bind to the exact proposed change, not to whichever plan happens to be current when a webhook arrives.

**Implementation scope**

1. Create a revision decision containing parent revision, proposed revision, WorkContract digest, SnapshotSet and authorization epoch
2. Resolve approver identity server-side and enforce per-repository and sensitive-domain rules
3. Activate the new revision atomically while fencing all old publication rights
4. Retain rejection/expiry history and the prior usable checkpoint

**Acceptance criteria**

- [ ] Approval for revision 2 cannot activate revision 3
- [ ] A stale approval after cancellation cannot revive execution
- [ ] A newly added repository needs its own authorized scope decision
- [ ] Rejection preserves WIP and returns an actionable blocked state

**Negative and recovery tests**

- Two approvers race with different decisions; the configured decision policy yields one activation
- Replay a consumed approval after a later amendment and assert zero effect

**Dependencies:** PLN-03, FND-06
**Evidence/design sources:** R04, X09. See `evidence/SOURCES.md`.

### PLN-06 — Manage clarification questions as durable decisions

**P1 · feature · P2 · size L · owner: Core/architecture engineer**

**Problem / outcome.** An agent must be able to ask about business meaning or constraints without inventing an answer or holding a runner forever.

**Implementation scope**

1. Persist question ID, reason, options/free-text schema, required actor scope and revision context
2. Route questions to the parent coordinator, not independently to every subagent
3. Store answers as authorized events with provenance, not raw chat history changes
4. Set separate answer expiry and active-compute/overall-work deadlines

**Acceptance criteria**

- [ ] One question produces one visible thread and one durable answer
- [ ] An answer may resolve ambiguity but cannot grant extra repository permissions implicitly
- [ ] Expired or answered questions reject duplicate/stale answers clearly
- [ ] A waiting question releases the runner after checkpoint according to policy

**Negative and recovery tests**

- Answer the correct text to the wrong question ID and verify rejection
- A subagent attempts to impersonate an approver; the control plane refuses it

**Dependencies:** PLN-02, CTL-01, CTL-02, FND-05
**Evidence/design sources:** X05. See `evidence/SOURCES.md`.

### PLN-07 — Invalidate evidence precisely after plan or source changes

**P1 · feature · P2 · size L · owner: Core/architecture engineer**

**Problem / outcome.** Reusing a green test or review from a prior revision is unsafe when the candidate, contracts or environment changed.

**Implementation scope**

1. Associate step outputs and verification records with input, source, plan and environment digests
2. Compute an explicit invalidation set when a revision activates
3. Preserve unaffected evidence and clearly mark superseded evidence rather than deleting it
4. Prevent stale native callbacks from advancing the active revision

**Acceptance criteria**

- [ ] A changed producer contract invalidates its affected consumer verification
- [ ] A wording-only display change does not force a rebuild when executable inputs are unchanged
- [ ] A late old-run success is visible history but never current proof
- [ ] Replanning can explain which work was preserved and why

**Negative and recovery tests**

- Activate a new plan while an old integration job completes
- Change the environment profile without changing source and require verification invalidation

**Dependencies:** PLN-05, FND-06
**Evidence/design sources:** X12, R04. See `evidence/SOURCES.md`.

### PLN-08 — Record decision rationale without storing hidden reasoning

**P2 · feature · P2 · size M · owner: Core/architecture engineer**

**Problem / outcome.** Long-running work needs durable decisions and evidence, not unrestricted model transcripts or a fragile memory summary.

**Implementation scope**

1. Persist concise decision records containing alternatives, chosen option, evidence, assumptions and approval identity
2. Keep derived summaries linked to immutable source records and label them as summaries
3. Exclude secrets and internal model reasoning; retain only operationally useful outputs
4. Reconstruct a fresh-session brief from the current contract, revision, decisions and checkpoint

**Acceptance criteria**

- [ ] A new runner can continue without the old vendor conversation file
- [ ] A summary cannot override the underlying approved decision
- [ ] Data retention and access rules apply to decision records as to source evidence
- [ ] Contradictory prior decisions are surfaced rather than silently overwritten

**Negative and recovery tests**

- Drop vendor session state and continue from application-owned artifacts
- Inject an instruction into an evidence excerpt that tries to rewrite a decision

**Dependencies:** PLN-02, FND-05
**Evidence/design sources:** X02, X06. See `evidence/SOURCES.md`.

## Epic CTL — Durable human control

### CTL-01 — Add a durable command mailbox and event stream

**P1 · feature · P1 · size L · owner: Core/runtime engineer**

**Problem / outcome.** Issue comments cannot safely be injected into a running process without ordering, deduplication and acknowledgements.

**Implementation scope**

1. Define command IDs, monotonic per-work sequence, expected revision/attempt and idempotency key
2. Persist commands and outbox notifications in one transaction
3. Define separate received, authorized, applied, checkpointed, rejected and expired outcomes
4. Store runner events with source identity and offset for reconnect replay

**Acceptance criteria**

- [ ] HTTP or webhook acceptance means persisted, not already applied
- [ ] Duplicate delivery returns the original command result
- [ ] Commands for a superseded attempt cannot change a new attempt
- [ ] Status can be reconstructed without contacting the model

**Negative and recovery tests**

- Disconnect between persistence and ACK and redeliver
- Receive events out of order and ensure projections converge

**Dependencies:** FND-06, PLN-01
**Evidence/design sources:** X09. See `evidence/SOURCES.md`.

### CTL-02 — Authorize human commands against the full work subject

**P1 · feature · P1 · size M · owner: Core/runtime engineer**

**Problem / outcome.** Multi-repository plans need object-level authorization for every intervention, not just a username allowlist on the originating issue.

**Implementation scope**

1. Normalize actor identity and platform delivery provenance at ingress
2. Bind commands to connection, repository/work package, question/revision and expected generation
3. Separate read, steer, pause, resume, amend and approve capabilities
4. Revalidate current permissions for sensitive actions and reject agent-authored approvals

**Acceptance criteria**

- [ ] A user who can comment but not approve cannot expand write scope
- [ ] A full run ID never bypasses repository checks
- [ ] A runner control token cannot submit human approvals
- [ ] Revoked approvers cannot activate a pending revision

**Negative and recovery tests**

- Replay an authorized command from another repository or connection
- Use bot-loop markers, quoted commands and forged actor fields to test ingress parsing

**Dependencies:** CTL-01, FND-01
**Evidence/design sources:** R04, X11. See `evidence/SOURCES.md`.

### CTL-03 — Expose questions and plan decisions through native comments

**P1 · feature · P1 · size M · owner: Core/runtime engineer**

**Problem / outcome.** The customer needs a usable control surface without waiting for a separate web dashboard.

**Implementation scope**

1. Render one status thread with active revision, pending question/decision and concise valid command examples
2. Add proposed /answer and /approve-revision parsing with explicit IDs
3. Keep original plan and later revisions linked rather than replacing the evidence trail
4. Escape user content so quotations and generated instructions are not parsed as new commands

**Acceptance criteria**

- [ ] The operator can answer a pending question from GitLab, GitHub or Azure comments
- [ ] Exactly one current projection identifies the active revision
- [ ] User sees command receipt and later application separately
- [ ] Unsupported commands are rejected explicitly, not silently ignored

**Negative and recovery tests**

- Duplicate/edit/reorder comments while the runner reconnects
- A model-generated example containing /approve-revision never becomes authorization

**Dependencies:** CTL-02, PLN-02
**Evidence/design sources:** R04, X05. See `evidence/SOURCES.md`.

### CTL-04 — Implement discovery clarification wait and resume

**P1 · feature · P1 · size L · owner: Core/runtime engineer**

**Problem / outcome.** Planning should ask about a blocking business ambiguity before a misleading implementation plan is approved.

**Implementation scope**

1. Connect discovery question events to the durable question service
2. Persist the evidence gathered before the question and release resources on the configured wait threshold
3. Resume with the original SnapshotSet plus the authorized answer
4. Mark unresolved critical questions as a planning block rather than inventing defaults

**Acceptance criteria**

- [ ] A question raised during discovery survives worker and runner restart
- [ ] The resumed plan cites the answer as a decision source
- [ ] Discovery budget is not reset when the human replies
- [ ] New source commits require a deliberate snapshot refresh decision

**Negative and recovery tests**

- Wait longer than the CI job limit and resume in a new workspace
- Answer after question expiry and verify it cannot silently start a new paid run

**Dependencies:** PLN-06, CTL-03, EXE-03
**Evidence/design sources:** X05, X06. See `evidence/SOURCES.md`.

### CTL-05 — Implement pause as revoke, interrupt and checkpoint

**P1 · feature · P2 · size L · owner: Core/runtime engineer**

**Problem / outcome.** Pause cannot mean only a UI status change; publication must stop even when a process is slow to acknowledge interruption.

**Implementation scope**

1. Increment publication epoch and persist pause_requested before sending a runtime interrupt
2. Stop new tool starts at cooperative boundaries and drain the active turn
3. Capture WIP and last applied command sequence before marking paused
4. On timeout, terminate the runner and expose the last recoverable checkpoint, not a false clean pause

**Acceptance criteria**

- [ ] No new effect authorization is created after the pause transaction; the acknowledgement lists any previously authorized unresolved effects
- [ ] Paused means a checkpoint was durably stored or a clear work-loss boundary was recorded
- [ ] An already-dispatched remote effect is reconciled and never claimed undone
- [ ] A waiting human does not consume an idle runner indefinitely

**Negative and recovery tests**

- Pause during a long file read, test process, artifact upload and native commit dispatch
- Force an uncooperative driver and verify timeout/termination plus honest checkpoint status

**Dependencies:** CTL-01, CTL-02, EXE-02, EXE-03, FND-02
**Evidence/design sources:** X03, X06. See `evidence/SOURCES.md`.

### CTL-06 — Resume from a confirmed checkpoint with a fresh epoch

**P1 · feature · P2 · size L · owner: Core/runtime engineer**

**Problem / outcome.** Restarting an interrupted session must restore code as well as conversation and reject stale late output.

**Implementation scope**

1. Validate checkpoint integrity, snapshot availability, active plan and current permissions
2. Create a new execution attempt/epoch before restoring workspace
3. Resume native session only for a compatible pinned profile; otherwise reconstruct from durable task artifacts
4. Keep historical spend and pending remote effects attached to the same work

**Acceptance criteria**

- [ ] Resume preserves confirmed WIP edits and does not start from main
- [ ] A late artifact from the paused attempt cannot overwrite the resumed candidate
- [ ] Corrupt or missing checkpoints block with an actionable reason
- [ ] A provider/model change requires explicit session-reset semantics and any needed approval

**Negative and recovery tests**

- Resume on another host with no vendor session files
- Advance the target branch during pause and require explicit reconcile/rebase handling without force overwrite

**Dependencies:** CTL-05, PLN-07, EXE-03
**Evidence/design sources:** X06. See `evidence/SOURCES.md`.

### CTL-07 — Deliver bounded steering instructions without granting new authority

**P1 · feature · P2 · size L · owner: Core/runtime engineer**

**Problem / outcome.** The customer needs to correct implementation direction during a run, but arbitrary chat text must not become a privilege change.

**Implementation scope**

1. Add /steer with expected plan/attempt and classify instruction scope
2. Deliver non-material guidance through the runtime adapter or next checkpoint boundary
3. Promote new constraints, repository changes or public-contract changes into a ChangeProposal
4. Record whether the instruction was accepted, applied or deferred and at which turn

**Acceptance criteria**

- [ ] A steering note never expands read/write or tool permissions
- [ ] The operator sees the boundary at which guidance took effect
- [ ] Unsupported live steering falls back to explicit checkpoint-and-resume, not terminal keystroke injection
- [ ] Two steering messages are applied in durable sequence order

**Negative and recovery tests**

- Send a steer for an old native turn after a reconnect
- Ask through /steer to disable tests or add an unapproved service; require a material decision

**Dependencies:** CTL-06, PLN-03, EXE-02
**Evidence/design sources:** X03, X07, X08. See `evidence/SOURCES.md`.

### CTL-08 — Preserve a final cancel contract across all interactive states

**P1 · feature · P2 · size M · owner: Core/runtime engineer**

**Problem / outcome.** Adding pause/resume/question states creates new opportunities for late callbacks to resurrect cancelled work.

**Implementation scope**

1. Apply cancellation generation to discovery, implementation, questions, revisions, verification and child work items
2. Cancel outstanding runner tasks where supported and revoke future tool/publication grants
3. Retain immutable artifacts and correlate effects that were already accepted
4. Require a new work command for any intentional post-cancel restart

**Acceptance criteria**

- [ ] Cancelled work cannot be resumed by an old answer or approval
- [ ] Cancelled child work cannot move its parent to ready
- [ ] Running native jobs are cancelled best-effort with observed status recorded
- [ ] Unknown remote effects remain visible and never trigger blind compensating writes

**Negative and recovery tests**

- Cancel while answer, revision approval and candidate callback race
- Replay all earlier command events after cancellation

**Dependencies:** CTL-05, PLN-05, FND-06
**Evidence/design sources:** R04, X09. See `evidence/SOURCES.md`.

## Epic EXE — Interactive and resumable harness execution

### EXE-01 — Define a role-aware HarnessRuntime protocol

**P1 · enabler · P1 · size L · owner: Harness/platform engineer**

**Problem / outcome.** Planning, implementation and review should reuse runtime integration without inheriting the same permissions.

**Implementation scope**

1. Define start, send_input, interrupt, snapshot, resume, events and terminate with an explicit capability matrix
2. Add roles discovery, implementer and readonly reviewer with separate tool, filesystem and network profiles
3. Keep native session/turn IDs opaque and separate from Forge run/attempt IDs
4. Normalize outcomes: completed, needs_input, change_proposal, checkpointed, failed and unsupported

**Acceptance criteria**

- [ ] A role change cannot reuse wider credentials implicitly
- [ ] Every runtime event carries run, attempt, epoch and native correlation
- [ ] Missing capabilities are detected before dispatch
- [ ] Existing batch CLIs remain usable via an honest checkpoint-restart adapter

**Negative and recovery tests**

- Select a batch-only adapter for required live steering and reject or negotiate explicit downgrade
- Feed a native event from another session and verify it cannot affect the run

**Dependencies:** FND-04, FND-05
**Evidence/design sources:** X03, X07, X08. See `evidence/SOURCES.md`.

### EXE-02 — Implement the first interactive driver with ClaudeSDKClient

**P1 · feature · P2 · size L · owner: Harness/platform engineer**

**Problem / outcome.** Forge already has a Claude Code lane, so it is the smallest practical first integration for structured interactive sessions.

**Implementation scope**

1. Run a pinned SDK/CLI in the execution environment behind HarnessRuntime
2. Use ClaudeSDKClient for continuous messages and interrupt; drain the interrupted turn before processing the next response
3. Route questions and tool approvals through durable control requests
4. Gate each sensitive tool via hooks and OS/process isolation; validate the chosen BYOK route independently

**Acceptance criteria**

- [ ] Interrupt produces one well-defined native turn boundary and no stale result is treated as the next turn
- [ ] The same session can receive an authorized clarification
- [ ] Discovery denies edits/remote writes despite model instructions
- [ ] A failed SDK/proxy compatibility probe prevents claiming the profile supported

**Negative and recovery tests**

- Interrupt during streaming output, during a tool and after completion
- Return permission callback failures and reconnect midway through question delivery

**Dependencies:** EXE-01, CTL-01, CTL-02
**Evidence/design sources:** X03, X04, X05. See `evidence/SOURCES.md`.

### EXE-03 — Implement portable workspace checkpoints and session restoration

**P1 · feature · P1 · size L · owner: Harness/platform engineer**

**Problem / outcome.** Vendor conversation persistence alone does not restore the filesystem or prevent replay against a different code snapshot.

**Implementation scope**

1. Capture source SnapshotSet, WIP manifest, untracked files, decision/evidence references and last applied command sequence
2. Include optional encrypted native session state only when supported by the pinned adapter
3. Restore into a fresh isolated workspace and verify every digest before starting tools
4. Support session-reconstruction fallback from application-owned artifacts

**Acceptance criteria**

- [ ] Destroying the original CI runner does not lose the last confirmed checkpoint
- [ ] A checkpoint cannot be restored onto an unrelated base
- [ ] Hidden credentials and control tokens are not copied into artifacts
- [ ] A reported recovery point identifies any changes after it that may have been lost

**Negative and recovery tests**

- Restore with missing untracked files, changed permissions, corrupt archive and stale session version
- Fork a conversation without restoring files and require the adapter to refuse claiming a complete resume

**Dependencies:** FND-05, EXE-01, PLN-08
**Evidence/design sources:** X06, X02. See `evidence/SOURCES.md`.

### EXE-04 — Connect runners with an outbound authenticated control channel

**P1 · feature · P2 · size L · owner: Harness/platform engineer**

**Problem / outcome.** Self-managed CI environments may not allow inbound callbacks, and long-running jobs need durable command delivery.

**Implementation scope**

1. Use outbound HTTPS long polling and event batches as the initial transport; add SSE only where helpful
2. Issue short-lived run/attempt-scoped credentials through a broker
3. Rotate tokens without transferring provider write credentials to the lane
4. Distinguish transport reconnect from command replay and authenticate every artifact/event upload

**Acceptance criteria**

- [ ] No public inbound agent port is required
- [ ] A runner can read only its own commands and upload only its own results
- [ ] Lost responses are retried with stable event IDs
- [ ] Expired/revoked tokens stop new tools and publication without silently changing credential mode

**Negative and recovery tests**

- Partition the control channel while a tool runs; enforce bounded continuation policy
- Reuse a token for a second work package and verify denial

**Dependencies:** CTL-01, EXE-01, FND-05
**Evidence/design sources:** X09, X16. See `evidence/SOURCES.md`.

### EXE-05 — Support checkpoint-restart control for batch-only harnesses

**P2 · feature · P2 · size M · owner: Harness/platform engineer**

**Problem / outcome.** Not every existing CLI provides a safe live input API. Portability requires truthful degraded behavior rather than pretending all agents are interactive.

**Implementation scope**

1. Wrap batch execution in bounded episodes and capture checkpoints between them
2. Queue steering for the next safe episode and expose deferred status
3. Cancel the current job when a hard pause is required; resume from the last confirmed bundle
4. Record unsupported native operations in the capability manifest

**Acceptance criteria**

- [ ] A batch-only driver never receives simulated terminal keystrokes as control
- [ ] The UI distinguishes next-checkpoint guidance from already-applied guidance
- [ ] Episode boundaries preserve budget and plan versions
- [ ] Loss of uncheckpointed work is explicitly reported

**Negative and recovery tests**

- Send multiple commands while the driver cannot read input
- Terminate halfway through diff capture and recover only from the last complete checkpoint

**Dependencies:** EXE-03, EXE-04, CTL-05
**Evidence/design sources:** R11, X19. See `evidence/SOURCES.md`.

### EXE-06 — Add a Codex App Server interactive adapter

**P2 · feature · P4 · size L · owner: Harness/platform engineer**

**Problem / outcome.** Codex should be a second adapter over the established contract, not a parallel controller.

**Implementation scope**

1. Pin the app-server protocol and negotiate advertised capabilities
2. Map Forge attempts to threads/turns; use turn/steer with expectedTurnId and turn/interrupt
3. Treat workspace/model/sandbox changes as new execution-profile decisions rather than steering
4. Preserve the same candidate, checkpoint, permissions and budget contracts as the first adapter

**Acceptance criteria**

- [ ] Steering an outdated turn is rejected safely
- [ ] Changing a sandbox policy is impossible through free-text steer
- [ ] Existing conformance traces pass unchanged apart from adapter fixtures
- [ ] Unsupported experimental protocol fields are never required silently

**Negative and recovery tests**

- Restart app-server between thread creation and turn execution
- Interrupt and deliver an old completed event after a new turn begins

**Dependencies:** EXE-02, EXE-03, EXE-04, FND-08
**Evidence/design sources:** X07. See `evidence/SOURCES.md`.

### EXE-07 — Add an OpenCode server adapter for tested BYOK profiles

**P2 · feature · P4 · size L · owner: Harness/platform engineer**

**Problem / outcome.** Provider portability should be explicit at the harness/model boundary, not assumed from an OpenAI-compatible URL.

**Implementation scope**

1. Map session, prompt, abort and event endpoints to HarnessRuntime
2. Validate structured-output, tool-call and usage behavior for each supported provider/model route
3. Apply the same runner isolation and scoped control-token model
4. Keep Grok/Copilot on checkpoint mode until their native control contracts pass equivalent tests

**Acceptance criteria**

- [ ] A route enters the supported matrix only after end-to-end conformance
- [ ] Invalid tool output is rejected before it becomes a plan/candidate artifact
- [ ] Provider failover cannot cross data-residency or budget policy
- [ ] Credential modes are named separately from model IDs

**Negative and recovery tests**

- Run the same discovery/question/resume trace through two BYOK routes
- Simulate a gateway response that reports aggregate usage only and preserve partial accounting

**Dependencies:** EXE-01, EXE-03, EXE-04, FND-08
**Evidence/design sources:** X08, X18. See `evidence/SOURCES.md`.

### EXE-08 — Isolate tools, egress and privileged test execution

**P1 · enabler · P2 · size L · owner: Harness/platform engineer**

**Problem / outcome.** Read-only prompts, CLI deny rules and a forbidden push URL are not sufficient isolation for arbitrary code execution.

**Implementation scope**

1. Use isolated per-run processes/containers with separate source and writable scratch mounts
2. Restrict egress, package mirrors, DNS and secret injection per execution role
3. Keep Docker/Kubernetes control credentials and source write tokens in trusted brokers/executors
4. Scan artifact boundaries for secret-bearing files and record capability decisions

**Acceptance criteria**

- [ ] The agent cannot reach production DBs, the Docker socket or the trusted publisher token
- [ ] Discovery source mounts remain immutable even through shell tools
- [ ] Test dependencies use disposable credentials and data
- [ ] Operator-installed MCP servers cannot silently broaden runtime scope

**Negative and recovery tests**

- Attempt writes via Python, symlinks and alternate git remotes rather than only blocked CLI strings
- Attempt metadata-endpoint and unauthorized HTTP requests from repository tests

**Dependencies:** FND-04, EXE-01, FND-05
**Evidence/design sources:** X04, X16, X14. See `evidence/SOURCES.md`.

## Epic MRP — Coordinated multi-repository work

### MRP-01 — Introduce WorkPackage as a parent of repository work items

**P1 · feature · P3 · size L · owner: Workflow/integration engineer**

**Problem / outcome.** One FlowRun per issue/repository cannot represent one business change spanning several services without losing approval and evidence lineage.

**Implementation scope**

1. Add a parent WorkPackage with goal, approved system scope, budget and active PlanRevision
2. Link per-repository work items to existing run machinery instead of replacing it
3. Keep discovery read set separate from the planned write set
4. Define parent states for planning, blocked, executing, partially published, verifying and ready for human

**Acceptance criteria**

- [ ] A parent can read ten repositories while creating only two child write runs
- [ ] Child failures do not erase successful candidates or spend
- [ ] Parent readiness requires the declared child and integration obligations
- [ ] No parent operation merges child PRs automatically

**Negative and recovery tests**

- Cancel a parent while one child publishes and another waits for approval
- Recover parent projections from immutable child events after a restart

**Dependencies:** DSC-07, PLN-02, CTL-08
**Evidence/design sources:** X11, R04. See `evidence/SOURCES.md`.

### MRP-02 — Compile bounded cross-repository execution dependencies

**P1 · feature · P3 · size L · owner: Workflow/integration engineer**

**Problem / outcome.** Service topology may be cyclic; the execution order for one change must still be explicit and schedulable.

**Implementation scope**

1. Build a small work-item DAG from artifact, contract and candidate dependencies
2. Separate build order, verification order and human merge/deploy recommendations
3. Validate graph cycles at the work-item level and allow deliberate compatibility phases to break cycles
4. Avoid adding a general workflow DSL in the first multi-repo release

**Acceptance criteria**

- [ ] A cyclic service dependency graph does not automatically block an additive change plan
- [ ] A work item starts only when its declared input artifacts are available
- [ ] Partial completion and waiting dependencies are visible
- [ ] Replanning invalidates only affected downstream nodes

**Negative and recovery tests**

- Construct a true execution cycle and require a clear plan error
- Reorder independent child completions and verify deterministic parent readiness

**Dependencies:** MRP-01, PLN-07
**Evidence/design sources:** X15. See `evidence/SOURCES.md`.

### MRP-03 — Assign one writable repository per child execution lane

**P1 · feature · P3 · size L · owner: Workflow/integration engineer**

**Problem / outcome.** Giving every agent write access to the entire system makes ownership and validation unmanageable.

**Implementation scope**

1. Mount the child target repository writable only inside its sandbox; mount related snapshots read-only
2. Pass per-repository contract outputs as immutable dependencies
3. Issue capability tokens and candidate scopes for exactly one child/attempt
4. Detect competing writers and human pushes without force overwrite

**Acceptance criteria**

- [ ] One child cannot emit a publishable candidate for a sibling repository
- [ ] Read-only neighbors remain available for reference and tests
- [ ] External branch movement yields a visible reconciliation decision
- [ ] Parallelism is bounded by system WIP and repository ownership constraints

**Negative and recovery tests**

- Agent tries to patch a mounted neighboring service
- Human edits the child branch while its candidate is materializing

**Dependencies:** MRP-02, EXE-08, FND-02
**Evidence/design sources:** X16. See `evidence/SOURCES.md`.

### MRP-04 — Publish coordinated candidates as an explicit saga

**P1 · feature · P3 · size L · owner: Workflow/integration engineer**

**Problem / outcome.** Source-control providers do not provide one atomic transaction across multiple repositories.

**Implementation scope**

1. Prepare and validate all required child candidates before coordinated publication begins
2. Persist per-repository publication intents and a parent CandidateSet draft
3. Publish Draft MR/PRs independently with idempotent reconciliation
4. Expose partial publication and safe operator actions; never delete human edits as compensation

**Acceptance criteria**

- [ ] A failure on the second repository leaves the first PR visible as partial, not an all-or-nothing success
- [ ] Reconciliation does not create duplicate commits or PRs
- [ ] Human review remains the only merge authority
- [ ] Prepared candidates reference the same active plan/contract generation

**Negative and recovery tests**

- Apply the first remote effect, lose the response, then fail the second provider
- Resume after one PR was manually updated and require conflict handling rather than forced rollback

**Dependencies:** MRP-03, FND-02, FND-06
**Evidence/design sources:** X15, R04. See `evidence/SOURCES.md`.

### MRP-05 — Freeze CandidateSet identity for integration verification

**P1 · feature · P3 · size L · owner: Workflow/integration engineer**

**Problem / outcome.** Ten independently green commits do not prove that the selected versions work together.

**Implementation scope**

1. Compute CandidateSet digest over repository candidate OIDs, unchanged baseline image digests, contracts, tests and environment profile
2. Bind every integration result to this exact set
3. Store publication completeness separately from verification status
4. Invalidate integration evidence when any member changes

**Acceptance criteria**

- [ ] Changing one service image changes CandidateSet identity
- [ ] A result from an old set cannot mark the current package ready
- [ ] Baseline neighbors are pinned, not pulled as latest
- [ ] Evidence names the exact tests and environment that ran

**Negative and recovery tests**

- Rerun tests with the same sources but a changed broker/database image
- Deliver a successful result for a superseded set after a repair

**Dependencies:** MRP-04, PLN-07
**Evidence/design sources:** X12, X14. See `evidence/SOURCES.md`.

### MRP-06 — Require compatibility plans for API, event and database changes

**P1 · feature · P3 · size L · owner: Workflow/integration engineer**

**Problem / outcome.** A multi-service feature often needs a safe transition between old and new versions, not just a successful final-state test.

**Implementation scope**

1. Add expand/migrate/contract steps and explicit old/new consumer/provider combinations
2. Require separate approval for destructive schema changes, data migrations and new external infrastructure
3. Generate human merge/deploy sequencing notes and rollback constraints
4. Keep production migration execution outside the coding-agent capability

**Acceptance criteria**

- [ ] The plan identifies mixed-version states that will exist during rollout
- [ ] An event rename includes a compatibility decision rather than only changing both repositories
- [ ] Destructive DB cleanup is not bundled into an automatic tactical revision
- [ ] Rollback limitations are stated when new data cannot be reversed safely

**Negative and recovery tests**

- Test old consumer/new producer and new consumer/old producer according to the plan
- Introduce a schema change that passes fresh DB tests but fails upgrade from the baseline schema

**Dependencies:** MRP-02, PLN-03, VER-04
**Evidence/design sources:** X15, X12. See `evidence/SOURCES.md`.

### MRP-07 — Handle partial completion, human changes and selective replan

**P2 · feature · P4 · size L · owner: Workflow/integration engineer**

**Problem / outcome.** Long system changes will encounter accepted child work, rejected work and moving branches. Restarting the entire package wastes effort and evidence.

**Implementation scope**

1. Reconcile each child against current native PR/branch identity
2. Preserve accepted work and generate targeted new revisions for unresolved obligations
3. Mark human-modified candidates as requiring fresh validation instead of silently taking ownership
4. Support explicit abandon or detach decisions without automatic branch deletion

**Acceptance criteria**

- [ ] A rejected child can be replanned without rerunning unrelated accepted children
- [ ] A manual commit invalidates only applicable evidence
- [ ] A partially merged package remains visible with mixed-version obligations
- [ ] Parent cancellation does not pretend already merged code was rolled back

**Negative and recovery tests**

- Merge one child manually and then reject another
- Force a concurrent plan amendment while branch reconciliation is pending

**Dependencies:** MRP-05, MRP-06, CTL-08
**Evidence/design sources:** X15, R04. See `evidence/SOURCES.md`.

### MRP-08 — Add hierarchical budgets, admission and fair scheduling

**P1 · feature · P4 · size L · owner: Workflow/integration engineer**

**Problem / outcome.** Independent child budgets can collectively exceed a customers total allowance or saturate shared CI/review capacity.

**Implementation scope**

1. Reserve child allowances atomically against a WorkPackage budget
2. Track discovery, planning, native CLI, tests, repair and review separately while retaining total spend
3. Bound concurrent writable lanes and expensive environments per tenant/system
4. Preserve unknown CLI spend conservatively and define active-compute versus human-wait clocks

**Acceptance criteria**

- [ ] Two child admissions cannot reserve more than the remaining parent budget
- [ ] Pausing does not reset consumed or unresolved spend
- [ ] One large work package cannot starve unrelated admitted work indefinitely
- [ ] Partial accounting is exposed rather than reported as a hard exact cap

**Negative and recovery tests**

- Race reservations from multiple children and force provider throttling
- Leave a question unanswered beyond the overall deadline while active compute remains low

**Dependencies:** MRP-01, FND-06, CTL-05
**Evidence/design sources:** R04, X18. See `evidence/SOURCES.md`.

## Epic VER — Independent system verification

### VER-01 — Separate verification execution from coding-agent execution

**P1 · enabler · P1 · size L · owner: Test-platform engineer**

**Problem / outcome.** An agent can change tests or claim that a command ran. Acceptance evidence must come from a separately controlled producer.

**Implementation scope**

1. Define a trusted verification job/executor with pinned commands and a minimal credential set
2. Execute accepted test bundles against published candidate OIDs or validated candidate images
3. Record producer identity, command exit, report digest and completeness
4. Keep local agent test output useful but explicitly non-authoritative for final gates

**Acceptance criteria**

- [ ] Agent-authored commands.tsv never substitutes for independent acceptance evidence
- [ ] Verification cannot obtain production credentials
- [ ] A test job without required reports is inconclusive even if the wrapper exits zero
- [ ] The candidate writer cannot silently weaken the verifier profile

**Negative and recovery tests**

- Forge a successful local receipt without executing tests
- Modify a candidate test configuration to skip the required suite

**Dependencies:** FND-04, FND-05
**Evidence/design sources:** X16, X14. See `evidence/SOURCES.md`.

### VER-02 — Build a baseline and focused dependency test recipe

**P1 · feature · P1 · size L · owner: Test-platform engineer**

**Problem / outcome.** The first useful customer slice needs realistic tests without booting all ten services for every coding turn.

**Implementation scope**

1. Provide a versioned recipe for the customers selected language/runtime and a focused database/broker subset
2. Execute a baseline before candidate work when feasible
3. Use synthetic fixtures, health checks, readiness deadlines and isolated networks
4. Pin dependency images and record startup/configuration failures as infrastructure

**Acceptance criteria**

- [ ] A candidate regression is distinguishable from a red baseline
- [ ] Dependency startup failure never invokes code repair automatically
- [ ] The recipe can run independently outside the coding-agent session
- [ ] Resource cleanup occurs after success, failure and cancellation

**Negative and recovery tests**

- Broker never becomes ready; assert infrastructure classification
- Seed a baseline test failure and confirm it is not attributed to the new candidate

**Dependencies:** VER-01, EXE-08
**Evidence/design sources:** X14. See `evidence/SOURCES.md`.

### VER-03 — Add HTTP and message contract verification

**P1 · feature · P3 · size L · owner: Test-platform engineer**

**Problem / outcome.** Cross-repository plans need executable interface compatibility checks before expensive system tests.

**Implementation scope**

1. Integrate existing customer contract tooling or a versioned Pact profile
2. Bind consumer/provider contracts to candidate and baseline versions
3. Support message schema compatibility while explicitly excluding broker-delivery guarantees
4. Export compatibility matrix evidence into CandidateSet verification

**Acceptance criteria**

- [ ] Old/new version pairs are tested as declared by the compatibility plan
- [ ] A contract from another commit cannot be reused as current evidence
- [ ] Missing provider verification blocks a required compatibility gate
- [ ] Message payload success does not claim real broker delivery behavior

**Negative and recovery tests**

- Use a stale contract version that would falsely pass a changed consumer
- Change an optional event field into a required one and require incompatibility evidence

**Dependencies:** MRP-05, VER-01
**Evidence/design sources:** X12, X13. See `evidence/SOURCES.md`.

### VER-04 — Test real database upgrades and asynchronous failure semantics

**P1 · feature · P3 · size L · owner: Test-platform engineer**

**Problem / outcome.** Contract tests alone do not validate migrations, transactions, duplicate delivery, ordering or retry behavior.

**Implementation scope**

1. Run schema upgrade tests from the pinned baseline with representative synthetic data
2. Test relevant idempotency, duplicate events, out-of-order messages, redelivery and bounded retry scenarios
3. Exercise outbox/inbox behavior only where the design uses it and report unsupported semantics explicitly
4. Separate reversible schema expansion from destructive cleanup and production deployment

**Acceptance criteria**

- [ ] Fresh schema success cannot replace a required upgrade-path result
- [ ] Duplicate delivery does not create a duplicate business side effect in the selected scenario
- [ ] A broker restart produces a deterministic recovery assertion
- [ ] Data fixtures contain no copied production secrets or personal data

**Negative and recovery tests**

- Crash a consumer between database commit and acknowledgement
- Reorder two business events and verify the intended conflict policy

**Dependencies:** VER-02, VER-03
**Evidence/design sources:** X13, X14, X15. See `evidence/SOURCES.md`.

### VER-05 — Run focused CandidateSet integration environments

**P1 · feature · P3 · size L · owner: Test-platform engineer**

**Problem / outcome.** The work package must demonstrate that changed services and pinned unchanged neighbors work together.

**Implementation scope**

1. Assemble environments from CandidateSet image digests and baseline manifests
2. Start only impacted services and required dependencies for the fast integration tier
3. Run full-system smoke at an explicit slower tier, not on every model turn
4. Capture endpoints, versions, resource limits and teardown evidence

**Acceptance criteria**

- [ ] The environment cannot pull an unpinned latest image
- [ ] Every result resolves to one CandidateSet and test-bundle digest
- [ ] A changed child candidate invalidates the prior integration result
- [ ] Cancellation tears down resources or records an actionable cleanup failure

**Negative and recovery tests**

- Change an image tag after scheduling and verify digest pinning prevents drift
- Fail teardown and verify the resource remains tracked for a janitor

**Dependencies:** MRP-05, VER-02, VER-04
**Evidence/design sources:** X14. See `evidence/SOURCES.md`.

### VER-06 — Introduce typed verification selectors and freshness policy

**P1 · enabler · P2 · size L · owner: Test-platform engineer**

**Problem / outcome.** Name-only workflow conclusions are too weak for complex repositories with several workflows, jobs and synthetic merge revisions.

**Implementation scope**

1. Define required selectors by provider-native workflow/check/job identity and expected event/ref semantics
2. Preserve subject head, target base and tested OID separately
3. Make unknown producer, missing required proof and stale candidate explicit decisions
4. Recheck freshness before readiness and define behavior for an unreadable head

**Acceptance criteria**

- [ ] Unrelated same-named green workflow cannot satisfy a required selector
- [ ] Optional pending jobs do not indefinitely block completed required proof
- [ ] A synthetic merge test records its source/target relationship accurately
- [ ] An unreadable required freshness check never becomes an implicit success

**Negative and recovery tests**

- Seed old reruns, duplicate names, wrong event types and reordered API pages
- Move target or source during review and verify correct invalidation

**Dependencies:** FND-08, VER-01
**Evidence/design sources:** R04, X12. See `evidence/SOURCES.md`.

### VER-07 — Make review evidence-aware and independent from implementation claims

**P2 · feature · P2 · size M · owner: Test-platform engineer**

**Problem / outcome.** An LLM review should assess the actual candidate against the active contract, not certify a self-reported plan completion.

**Implementation scope**

1. Build review packets from trusted diffs, active PlanRevision, decisions and verification reports
2. Keep review tools read-only and disallow acceptance-test editing
3. Require findings to reference exact candidate evidence and distinguish unknown coverage
4. Route material discrepancies into ChangeProposal or blocked states rather than silently revising the goal

**Acceptance criteria**

- [ ] Review of candidate A cannot be reused for candidate B
- [ ] Missing required evidence is visible rather than summarized as clean
- [ ] Reviewer cannot approve a plan amendment or merge
- [ ] Findings preserve severity, evidence and the applicable obligation

**Negative and recovery tests**

- Implementation summary claims a test passed while the trusted report failed
- Present a diff with a hidden compatibility change and require the obligation mismatch

**Dependencies:** PLN-07, VER-01
**Evidence/design sources:** R03, R04, X16. See `evidence/SOURCES.md`.

### VER-08 — Create a ten-service benchmark system with seeded hazards

**P1 · validation · P4 · size L · owner: Test-platform engineer**

**Problem / outcome.** Ten repositories in a diagram do not prove that Forge can safely deliver a representative system change.

**Implementation scope**

1. Build a synthetic but realistic ten-service fixture with a documented dependency graph, at least one DB and one broker
2. Define single-repo, two-repo and three-repo tasks with predeclared acceptance checks
3. Seed missing contract, stale schema, duplicate-event, wrong-repo, pause/replan and partial-publication cases
4. Evaluate read coverage, adaptation correctness, recovery, useful WIP retention and accepted outcomes

**Acceptance criteria**

- [ ] All safety hazards in the fixed acceptance suite cause the expected stop or approval request
- [ ] The fixture exposes exact candidate/test/environment versions
- [ ] Passing a small cohort is reported as pilot evidence, not a universal success rate
- [ ] The same tasks can be repeated across tested harness profiles

**Negative and recovery tests**

- Run faults while discovery, approval, publication and integration overlap
- Swap a baseline version mid-run and verify the evidence chain catches it

**Dependencies:** MRP-07, MRP-08, VER-05, VER-06, VER-07
**Evidence/design sources:** X12, X14, R13. See `evidence/SOURCES.md`.

## Epic OPS — Evaluation, operations and customer delivery

### OPS-01 — Establish plan-quality evaluation before model selection

**P1 · validation · P1 · size M · owner: Platform/evaluation engineer**

**Problem / outcome.** The customer objection is about plan quality. Improving runtime sophistication without measuring plan usefulness could only add cost.

**Implementation scope**

1. Create a small expert-reviewed set of representative tasks with hidden impact/contract checks
2. Compare current issue-only planning, evidence-pack planning and tool-using discovery under matched constraints
3. Score factual citations, dependency recall, feasibility, unknown handling and reviewer corrections
4. Record latency/spend distributions and avoid declaring a winner from one showcase

**Acceptance criteria**

- [ ] Every evaluated plan resolves cited paths at its SnapshotSet
- [ ] The benchmark includes a task that requires a clarification rather than guessing
- [ ] Expert corrections are retained, not deleted after acceptance
- [ ] Results separate provider/model profile from planning architecture

**Negative and recovery tests**

- Add a misleading issue path and a relevant consumer not named in the issue
- Repeat with limited retrieval budgets and compare coverage gaps honestly

**Dependencies:** DSC-05, PLN-06
**Evidence/design sources:** R02, X01, X17. See `evidence/SOURCES.md`.

### OPS-02 — Implement end-to-end usage lineage and budget reporting

**P1 · feature · P2 · size L · owner: Platform/evaluation engineer**

**Problem / outcome.** Current tokens/s or cost calculations are not meaningful without joining the same calls, attempts and output populations.

**Implementation scope**

1. Assign trace IDs from WorkPackage through role episode, attempt, model call and receipt
2. Keep input, cached input, cache writes, output, reasoning and completeness separate where reported
3. Record TTFT, full request duration, active execution, CI wait and human wait independently
4. Report accepted-work economics including failed, cancelled and superseded attempts

**Acceptance criteria**

- [ ] No input/cache class is counted twice
- [ ] Unmatched receipt/timing records produce unknown rates, not an invented average
- [ ] Reordering reports does not change totals
- [ ] Hard and partial budget enforcement are separately labeled for builtin and CLI profiles

**Negative and recovery tests**

- Use missing, duplicate, aggregate-only and delayed receipts
- Pause for hours and verify model-active time and overall elapsed time remain distinct

**Dependencies:** FND-06, MRP-08
**Evidence/design sources:** R12, X18. See `evidence/SOURCES.md`.

### OPS-03 — Provide operator diagnostics and one coherent status projection

**P1 · feature · P2 · size M · owner: Platform/evaluation engineer**

**Problem / outcome.** Interactive work is unusable when comments say only started or failed, or when an accepted command is mistaken for applied control.

**Implementation scope**

1. Project durable states into one native status comment and read-only API
2. Show current revision, last checkpoint, command ACK, blocked reason, remaining budget and native run links
3. Explain verified, unverified, partial publication and awaiting human states separately
4. Rate-limit notifications and preserve history links for audit

**Acceptance criteria**

- [ ] Pause requested and paused are visibly different
- [ ] The user can identify the latest valid checkpoint without reading raw JSON
- [ ] A recovery replay does not spam duplicate contradictory status comments
- [ ] Parent and child readiness do not disagree silently

**Negative and recovery tests**

- Race final verification with a late pause and inspect the projection
- Rebuild all projections from history after deleting their cached state

**Dependencies:** CTL-01, CTL-03, PLN-07
**Evidence/design sources:** R04, X09. See `evidence/SOURCES.md`.

### OPS-04 — Enforce evidence retention, residency and deletion policy

**P1 · enabler · P2 · size L · owner: Platform/evaluation engineer**

**Problem / outcome.** Cross-repository discovery and session checkpoints can contain more sensitive data than a final diff.

**Implementation scope**

1. Classify source snippets, task descriptions, decisions, transcripts, WIP and test reports
2. Apply tenant/project retention, encryption and model-route residency rules
3. Implement deletion/tombstone behavior that preserves minimal audit identity without retaining prohibited content
4. Prevent stale cached copies from outliving revocation or retention policy

**Acceptance criteria**

- [ ] An excluded repository never reaches an external model route
- [ ] Deleting a checkpoint removes associated session data under the declared policy
- [ ] Audit metadata does not leak deleted source snippets
- [ ] Operators can inspect where each artifact was stored and which model route received it

**Negative and recovery tests**

- Revoke access while a cached evidence pack exists
- Expire an artifact needed for resume and require an explicit recovery decision

**Dependencies:** FND-05, DSC-03, EXE-04
**Evidence/design sources:** X06, X16. See `evidence/SOURCES.md`.

### OPS-05 — Add capacity controls and failure-oriented service objectives

**P2 · feature · P4 · size L · owner: Platform/evaluation engineer**

**Problem / outcome.** A system-wide workload can saturate CI, provider quotas and human reviewers before raw model throughput is exhausted.

**Implementation scope**

1. Measure queue delay, time to command ACK/application, checkpoint latency, unknown effects and accepted-work lead time
2. Implement bounded retries, fairness and provider circuit-breaking without dropping durable commands
3. Size discovery and integration concurrency independently from coding workers
4. Build load tests with realistic long contexts and waiting periods rather than short-prompt token benchmarks

**Acceptance criteria**

- [ ] An unavailable provider does not block unrelated eligible work indefinitely
- [ ] Queue and budget limits are enforced before expensive job dispatch
- [ ] Load-test reports distinguish peak assumptions from measured behavior
- [ ] Human review backlog is visible as a separate bottleneck

**Negative and recovery tests**

- Inject sustained 429s, long CI queue times and a disconnected control channel
- Verify no retry storm and no duplicate effect under burst admission

**Dependencies:** MRP-08, OPS-02, OPS-03
**Evidence/design sources:** X09, R13. See `evidence/SOURCES.md`.

### OPS-06 — Generate release claims from versioned executed evidence

**P1 · validation · P0 · size M · owner: Platform/evaluation engineer**

**Problem / outcome.** Greppable closure markers and a green boot canary do not prove every claimed production behavior on every adapter.

**Implementation scope**

1. Emit a manifest combining declared capabilities with actual test/profile results for the pinned SHA and image digest
2. Generate README version/image examples from one release source
3. Mark not_run, skipped and unsupported distinctly; include migration and compatibility results
4. Require release-specific evidence for any promoted customer capability

**Acceptance criteria**

- [ ] A file containing a finding ID cannot alone close that finding
- [ ] A boot canary is never represented as multi-repo SDLC e2e
- [ ] README/quick-start image version agrees with the release or is explicitly labeled a compatibility example
- [ ] The manifest identifies executed test commands, jobs and artifact digests

**Negative and recovery tests**

- Release with a skipped interactive suite and ensure the corresponding capability stays unverified
- Intentionally mismatch package/tag/docs version and block promotion

**Dependencies:** FND-08, FND-07
**Evidence/design sources:** R13, R14, R17. See `evidence/SOURCES.md`.

### OPS-07 — Run a bounded design-partner pilot with explicit success criteria

**P1 · validation · P4 · size L · owner: Platform/evaluation engineer**

**Problem / outcome.** The customer should see realistic system work without being promised unrestricted autonomous delivery of ten services.

**Implementation scope**

1. Agree one business slice, authorized repositories, dependencies, test data, cost ceiling and human decision owners
2. Start with multi-repo reads and one writable target; progress to two/three write repositories only after the first gate
3. Measure plan corrections, accepted outcomes, interventions, WIP retained and total spend
4. Record limitations and customer acceptance evidence before broadening the supported matrix

**Acceptance criteria**

- [ ] The pilot scope excludes production deployment and autonomous merge
- [ ] At least one task requires an intentional material replan and one an operator pause/steer
- [ ] Unknown telemetry is visible and not treated as a free run
- [ ] Expansion is tied to observed exit criteria rather than repository-count marketing

**Negative and recovery tests**

- Run a staged demonstration where a hidden migration need is discovered after coding starts
- Stop the run mid-implementation, resume elsewhere and demonstrate preserved valid work

**Dependencies:** OPS-01, OPS-02, VER-08, OPS-06
**Evidence/design sources:** R01, X12, X14. See `evidence/SOURCES.md`.

### OPS-08 — Produce supported recipes and an architecture/runbook handoff

**P2 · feature · P4 · size M · owner: Platform/evaluation engineer**

**Problem / outcome.** The product needs reproducible onboarding and clear boundaries, not only a working maintainer environment.

**Implementation scope**

1. Publish one tested end-to-end recipe per promoted provider/runtime profile
2. Document safe control commands, lifecycle meanings, recovery, credentials, data flow and unsupported combinations
3. Include the decision record for retaining the current Postgres controller and criteria for later Temporal evaluation
4. Provide a read-only doctor that checks permissions, control-channel reachability, profile compatibility and required verification

**Acceptance criteria**

- [ ] A fresh operator can onboard the selected pilot without copying private maintainer state
- [ ] Doctor never mutates repository permissions or creates infrastructure silently
- [ ] Documentation distinguishes system read scope, write coordination and production deployment
- [ ] Every example is versioned and tested or clearly labeled illustrative

**Negative and recovery tests**

- Run onboarding from an empty environment with only documented prerequisites
- Remove one prerequisite and require a precise diagnostic before the first paid agent job

**Dependencies:** OPS-03, OPS-04, OPS-06, OPS-07
**Evidence/design sources:** R14, X09. See `evidence/SOURCES.md`.
