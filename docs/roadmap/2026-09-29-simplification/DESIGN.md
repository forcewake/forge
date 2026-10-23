# forge simplification program — design

- **Status:** Accepted (2026-09-29), together with
  [ADR-0035](../../adr/0035-simplification-program.md).
- **Date:** 2026-09-29
- **Baseline:** `main` at `7ed64ba` (v0.42.0)
- **Companion:** [ROADMAP.md](ROADMAP.md), the phase-by-phase task
  breakdown.

## 1. Summary

forge's safety core is worth keeping: the bot never merges, there is one
validated write path, intent is journaled before every effect, approval is
consume-once, and budgets are enforced. The problem is the weight around
that core:

- about 28% of `src/` is off every runtime path;
- the lifecycle is implemented three times;
- the CI lane imports the whole control plane;
- prose and qualification ceremony dominate change cost.

This program keeps the core and removes the weight in four phases, with the
product deployable after each one:

1. **Subtract.** Delete test-only code, retire the qualification machinery
   from the package, remove Azure (moved to `release/0.42.x`), builtin,
   Codeward legacy and deferred features, and cut prose.
2. **Separate.** Build `forge-protocol` and `forge-lane`: one runner and one
   OIDC-authenticated HTTP protocol.
3. **Unify.** Build one provider-neutral lifecycle engine over narrow
   ports, with an `effects` layer that makes intent-before-effect
   structural. Migrate GitHub first, then GitLab, and delete the three
   provider services.
4. **Productize.** A 30-minute install, `forge init` and `forge onboard`,
   documentation under 40K words, pilots, 1.0, and the EPAM Marketplace.

## 2. Baseline (measured 2026-09-29)

| Measure | Value | Method |
| --- | --- | --- |
| `src/forge` | 248 modules, 190,383 lines | `wc -l` |
| Runtime-reachable code (control plane plus lane entry points) | 201 modules, 137,820 lines | Static import closure from `forge.main`, `forge.worker`, `forge.migrate`, `forge.harness_entry`, `forge.lane_driver` and `forge.candidate_collector`; lazy imports included |
| Reached only by tooling (doctor, the `forge` console command, release modules, `scripts/`) | 21 modules, 33,155 lines (26,036 in `adaptive/`) | Same analysis; `[project.scripts]` modules count as tooling roots |
| Reached only by tests | 26 modules, 19,408 lines | Same analysis. Examples: `adaptive.publication_saga`, `saga_durable`, `saga_native`, `two_writer_qualification`, `workpackage_service`, `runtime`, `boundary_registry`, `closure_matrix`, `verification_executor`, `dotnet_recipe`, `reference/*` |
| Code reachable from the lane entry points | 187 modules, 132,772 lines | `lane_driver` imports `adaptive.wiring`; `harness_entry` imports `runs.execution_profile` |
| Lifecycle services | `service.py` 11,433, `github_service.py` 8,350, `azure_service.py` 6,110 lines | `wc -l` |
| Functions longer than 100, 200 and 400 lines | 234, 56 and 8 | AST |
| Longest functions | `service._advance_harness` 701, `github_service._advance_harness` 669, `harness_entry.main` 553, `azure_service._advance_harness` 401 | AST |
| Prose share of source lines | ~28%; at least 3,216 review, backlog and issue references and 705 ADR references | Tokenizer plus docstrings; the `scripts/_codebase.py` patterns |
| Most-changed files (commits) | `github_service` 76, `service` 72, `harness_entry` 47, `azure_service` 42 | `git log` |
| Tests | 9,275 passed, 8 failed, 75 skipped, 16.5 minutes on macOS | Full `pytest`. All 8 failures assume `python` on `PATH` (`tests/test_lane_install_contract.py`) |
| Test hygiene | Orphaned `fake_native_server.py` and `durable_ingress_server.py` processes, up to about 5 days old, from earlier runs | `ps` |
| `forge.doctor` without `.env` | Crashes with a pydantic `ValidationError` | `config.py:21-23` requires the `GITLAB_*` settings; `doctor.py:1103` does not catch the error |
| Documentation | ~319K words of Markdown; the CHANGELOG is 2,302 lines | `wc -w` |
| Divergent executor-input contracts | GitLab uses `resolve_approved_input` (`service.py:6089`, `:8004`); GitHub uses `compose_attempt_start` (import `github_service.py:133`, call `:4011`) | grep |
| Legacy-only dependencies | `agno` and the `mcp` package (only legacy agents and the MCP server import them); the `litellm` Python package (listed in `pyproject`, used by agno's LiteLLM model) | grep |

## 3. Goals and non-goals

Goals, in order:

1. **Simplify.** One lifecycle and clear boundaries, at a size a human and
   an agent can hold in their head.
2. **Users, GitHub-first.** A new team gets its first Draft PR within 30
   minutes. Then pilots, 1.0 and the EPAM Marketplace.

Non-goals:

- rewrite from scratch;
- change language;
- add providers or drivers;
- multi-repository writes before 1.0;
- keep historical qualification records current.

## 4. Non-negotiable invariants

Each invariant is registered in `tests/invariants/registry.py` with the test
files that prove it, and every test in those files is marked `invariant`.
The `invariants` CI job is required from Phase 0 and must stay green through
every phase.

| # | Invariant | Current evidence | Target home |
| --- | --- | --- | --- |
| I1 | The bot never merges. No provider client exposes or calls a merge API, and the lane holds no write credential. | ADR-0003. The absence of merge calls is not yet tested. | New static test that scans provider adapters for merge endpoints (`/merge`, `mergePullRequest`, `merge_when_pipeline_succeeds`, `autoMerge`). Lane templates assert push is disabled and no write token is present. |
| I2 | Only the publisher writes to repositories, and every write is validated: deny list, scope, count and size limits, strict patch apply, digests. | `tests/test_publication_boundary.py`, `test_publisher.py`, `test_patch_differential.py` | `publishing/`, plus `WritePort` injection: only the `effects` layer ever holds a `WritePort` (§6.1). |
| I3 | Intent is journaled before every external write. Unknown outcomes are resolved by probing, never by a blind retry. | `test_publication_intents.py`, `test_failure_injection.py` | `engine/effects.py` |
| I4 | Approval is consume-once and bound to the plan, base and spec digests. The executor receives exactly the approved bytes. | `test_durable_gates.py`, `test_runs_spec.py`, `test_revision_executor_proof.py` | `domain/approval.py`, `domain/approved_input.py` |
| I5 | For metered drivers, budget is reserved from the run's cumulative pool before spend and enforced at the gateway. Receipts are idempotent, unknown spend is never zero, and the closing reserve is partitioned before coding. For unmetered drivers (Copilot CLI subscription), wall-clock and turn caps are enforced and spend is recorded as `unmetered`, never as `0`. | `test_budgets.py`, `test_closing_budget.py`, `test_budget_amendment.py`, `test_usage_receipts.py` | `domain/budget.py`, `budget/gateway.py` |
| I6 | Exactly one Draft PR/MR per run, including under concurrent publication. | `tests/production_entry/test_concurrent_publication.py` (commit `536905c`) | `engine/effects.py`: keyed `ensure_draft` plus a database uniqueness constraint |
| I7 | Killing the worker at any step loses no work and duplicates no effect. | `test_failure_injection.py`, `test_failure_injection_os.py` | Engine failure-injection harness that kills at every step boundary (§7) |
| I8 | Verification is bound to the exact candidate SHA; a moved head is stale. | `test_runs_verification.py`, `test_verification_binding.py` | `verification/` |

One further rule is carried as a named test because it is a live-found
invariant (ADR-0028): **bot-authored notes never trigger commands.**

## 5. Scope for 1.0

| Capability | Decision | Notes |
| --- | --- | --- |
| Core loop: `/implement` → plan → `/go` → lane → candidate → Draft PR → CI plus repair → read-only review → `ready_for_human` | Keep | The engine |
| Plans with citations: bounded read-only discovery tools and read-only neighbour repositories | Keep, simplify | One research planner over `RepoPort` reads at the frozen SHA. Cohorts are removed. |
| `/fix`, `/ask` and `/approve-revision` review rounds | Keep and bring to GitHub | A provider-neutral engine feature |
| `/status`, `/why-blocked`, `/cancel`, `/retry`, `/reconcile` and the `forge` label | Keep | Engine commands |
| Budgets: reserve, closing reserve, amendments | Keep, simplify | Hard caps are enforced through gateway keys (§6.9) |
| `/pause` and exact-WIP resume across runners | Keep, simplify | One checkpoint format in `forge-lane` |
| `/steer` | Simplify | Only drivers that declare the `steer` capability (Claude Code) |
| Operator surfaces | Simplify | One projection behind `/status`, `/why-blocked`, a read-only JSON API and Prometheus metrics. Operator view, snapshot and timeline, ops limits, support bundle, audit export and the economics ledger are replaced. |
| Reactive review on human pushes to the Draft PR | Keep | A durable executor on `ReviewPort` |
| CI-debug lane, `/security` triage, forge MCP server | Defer until after 1.0 | Removed from `main` and recoverable from the tag `pre-simplification`. The lane-side MCP client configuration stays. |
| Codeward legacy: `orchestrator/`, `flows/`, `src/forge/agents/`, top-level `agents/`, chat | Remove | `orchestrator/project_config.py` moves out first |
| Builtin LLM implementer | Remove | `factory/implementer.py` and the builtin backend path |
| Multi-repository work packages | Defer until after 1.0 | |
| Azure DevOps | Move to `release/0.42.x` | Security fixes only. It returns after 1.0 as a port adapter of about 3K lines. |
| Codex, opencode, Grok | Contrib | `forge_lane.contrib`, smoke-tested only |
| Qualification machinery | Retire from the package | Replaced by the release process in §9 |

## 6. Target architecture

### 6.1 Packages and dependency rules

One repository, one uv workspace, three distributions:

| Distribution | Path | Size budget | Depends on |
| --- | --- | --- | --- |
| `forge-protocol` | `packages/forge-protocol/src/forge_protocol/` | ≤ 1.5K lines | `pydantic` only |
| `forge-lane` | `packages/forge-lane/src/forge_lane/` | ≤ 8K lines | `forge-protocol`, `httpx`; driver SDKs as extras (`claude`, `copilot`, `contrib`) |
| `forge` (server) | `src/forge/` | ≤ 75K lines | `forge-protocol`, FastAPI, SQLAlchemy, alembic, httpx, pydantic-settings, pyyaml, pyjwt |

import-linter contracts:

- `forge_lane` never imports `forge`.
- `forge` never imports `forge_lane`.
- `forge.domain` imports nothing from `forge` except itself.
- `forge.providers.github` and `forge.providers.gitlab` never import each
  other.
- `forge.engine`, `forge.planning`, `forge.review`, `forge.publishing`,
  `forge.verification`, `forge.budget` and `forge.control` never import
  `forge.providers.*`. They see providers only through `forge.ports`.
- Only `forge.api.app` (the composition root) imports concrete adapters.

**Write access is structural.** The provider write operations implement
`WritePort` (§6.4). The composition root passes the `WritePort` only to
the constructor of `engine.effects.Effects`. `StepContext` exposes
`ctx.effects`, never a `WritePort`. A ratchet check, part of
`scripts/ratchet.py`, fails if any module other than `engine/effects.py`
and `api/app.py` names `WritePort`.

All guardrails (ratchets, prose lint, mypy, coverage) cover `src/` **and**
`packages/*/src/`.

### 6.2 Server layout

```
src/forge/
  api/            FastAPI app composition: webhooks, lane v1 API, operator API, /metrics, /health
  engine/         lifecycle engine: step runner, handlers, commands, rounds, revival, reconciler, effects
  domain/         pure: states, transitions, RunSpec, ApprovedInput, Plan, Candidate, PathPolicy, budget math, verdicts
  ports.py        Protocols and typed outcomes (§6.4)
  providers/
    github/       ingress.py, subject.py, repo.py (read), write.py, review.py, ci.py, actor.py, auth.py
    gitlab/       same shape
    fake/         in-memory adapter with fault injection (tests and dev)
  planning/       research planner, read-only tools, citations
  review/         read-only reviewer, reactive review executor
  publishing/     candidate validation → ChangeSet (ADR-0001) → effects.commit / effects.ensure_draft
  verification/   checks → CheckVerdict, staleness, repair context
  budget/         reservations, closing partition, amendments, gateway client, usage
  control/        pause, resume, steer mailbox; checkpoint index and blob store (server side)
  operator/       projection behind /status, /why-blocked, JSON API and metrics
  store/          SQLAlchemy models, repositories, alembic (additive eng_* tables; squashed baseline after Phase 3)
  worker/         step claims (fenced leases), wake-ups
  config.py       settings with optional provider sections
  doctor.py       environment report; never raises
```

Size rules for new code: files ≤ 600 lines and functions ≤ 80 lines. The
ratchet keeps existing code from growing (§8).

### 6.3 Lifecycle engine

**States.** The vocabulary shrinks; sub-steps live in the step journal.
The product term `ready_for_human` is kept.

| New state | Old states |
| --- | --- |
| `queued` | `accepted`, `preflight` |
| `planning` | `planning` |
| `awaiting_approval` | `waiting_approval` |
| `running` | `proposing`, `waiting_harness` |
| `publishing` | `validating`, `committing`, `ensuring_draft_mr` |
| `verifying` | `waiting_ci`, `evaluating_ci` |
| `reviewing` | `reviewing` |
| `ready_for_human` | `ready_for_human` |
| `blocked`, `failed`, `cancelled` | same |

Transitions:

```
queued → planning → awaiting_approval →(/go, consume-once)→ running → publishing → verifying
verifying →(code failure, pool left)→ running          # repair: same branch, error context in the next brief
verifying →(passed)→ reviewing → ready_for_human
any non-terminal → blocked | failed | cancelled
blocked | failed →(/retry or auto-revive)→ the state that owns the failed handler
    # planning failure → planning; lane or publication failure → running; review failure → reviewing
```

The run record stores `failed_handler`. Revival re-enters the owning state
through an explicit edge per state. It never jumps to a later state and
never re-runs an earlier one.

**Engine collaborators** are interfaces in `ports.py`. Fakes exist from
the start so that handlers can be built and tested before the real
implementations land:

- `Planner.plan(task, snapshot) → Plan`, implemented by `planning/`;
- `Reviewer.review(candidate_diff, approved_input) → ReviewVerdict`,
  implemented by `review/`;
- `BudgetGateway` (mint, update, block, read spend), implemented by
  `budget/gateway.py`.

**Steps.** Each state is driven by one or more handlers:
`async def handle(ctx: StepContext) -> StepResult`. A handler:

- reads durable state;
- calls ports: reads directly, writes only through `ctx.effects`;
- returns either the next step or a typed park reason.

The step runner claims steps with fenced leases (CAS on `steps.lease_epoch`).
**Recording a step's result and creating its successor steps happen in one
transaction, fenced by the lease epoch.** A worker that lost its lease
cannot record a result or schedule anything. A handler returning normally
is its terminal outcome. A retryable observation raises `Retryable` and is
rescheduled with bounded backoff.

**Handlers:**

| Handler | Work |
| --- | --- |
| `plan` | Freeze `RunSpec`; post the plan comment with its Implementation block; open the gate |
| `approve` | `/go` consumes the gate once; verifies plan, base and spec digests and TTL; builds `ApprovedInput` |
| `dispatch` | Reserve from the run's pool (§6.9); `gw_mint` through effects; `dispatch_lane` through effects, with a single-use nonce |
| `await_lane` | Lane report or candidate via the lane API; polling `CIPort.lane_status` is the fallback |
| `publish` | `publishing.validate` → `effects.commit` → `effects.ensure_draft` |
| `verify` | `CIPort.checks_for(candidate_sha)` → `CheckVerdict`; staleness; bounded repair decision |
| `review` | Reviewer on the closing reserve → `effects.post_review` |
| `ready` | Final consistency check (candidate-bound evidence) → `ready_for_human` |

**Commands.** Provider ingress parses notes, labels and events into
`Command` values: `implement`, `go`, `cancel`, `retry`, `status`,
`why_blocked`, `reconcile`, `pause`, `resume`, `steer`, `fix`, `ask` and
`approve_revision`. The engine authorizes them through `ActorPort` and the
connection-scoped approver lists. One renderer turns every typed refusal
into a reply. Bot-authored notes are never commands.

**Review rounds.** `/fix` classifies against the frozen write scope and
stages a revision behind the existing gate. `/approve-revision` activates
it. The round is a child run from the exact current head, with its own
budget pool and closing partition. It publishes to the **root run's branch
and root `ReviewRef`**, the same Draft PR, so it never creates a second
draft. The draft effect key is the root run id. Round uniqueness is one
outstanding round per root `ReviewRef`. A moved head with human commits is
a typed `branch_drift` conflict that preserves those commits. `/ask`
records a clarification and replies; it never dispatches.

**Revival.** One classification table decides between transient and fatal.
Transient failures auto-revive with backoff (60s → 900s cap, at most
`FORGE_RUN_AUTO_REVIVE_LIMIT`) into the owning state. Fatal failures park
`blocked` with the precise reason. `/retry` grants one operator cycle in
the owning state, on the same branch.

**Reconciler.** A periodic pass over:

- due steps;
- expired leases;
- effects in state `dispatched` without an outcome, which are probed;
- lanes and checks that need polling when webhooks are missing.

### 6.4 Ports

```python
class SubjectPort(Protocol):
    async def get_task(self, subject: SubjectRef) -> TaskSnapshot: ...
    async def find_comment(self, subject: SubjectRef, marker: str) -> CommentRef | None: ...

class RepoPort(Protocol):
    async def head(self, repo: RepoRef, branch: str) -> Sha | None: ...
    async def read_tree(self, repo: RepoRef, sha: Sha, prefix: str = "") -> Tree: ...
    async def read_blob(self, repo: RepoRef, sha: Sha, path: str) -> bytes | None: ...
    async def find_commit_by_marker(self, repo: RepoRef, branch: str, marker: str) -> Sha | None: ...

class ReviewPort(Protocol):
    async def find_draft(self, repo: RepoRef, head_branch: str) -> ReviewRef | None: ...
    async def review_head(self, review: ReviewRef) -> Sha: ...
    async def list_feedback(self, review: ReviewRef, after: Cursor | None) -> list[FeedbackNote]: ...

class CIPort(Protocol):
    async def find_lane(self, repo: RepoRef, correlation_id: str) -> LaneHandle | None: ...
    async def lane_status(self, handle: LaneHandle) -> LaneStatus: ...
    async def checks_for(self, repo: RepoRef, sha: Sha) -> list[CheckResult]: ...

class ActorPort(Protocol):
    async def is_approver(self, actor: Actor, subject: SubjectRef) -> bool: ...
    def bot_identity(self) -> Actor: ...
```

**Write operations** form `WritePort`. Each provider implements it in
`providers/<name>/write.py`. Only the `effects` layer holds an instance
(§6.1):

```python
class WritePort(Protocol):
    async def post_comment(self, subject: SubjectRef, body: str, marker: str) -> CommentOutcome: ...
    async def update_comment(self, comment: CommentRef, body: str) -> CommentOutcome: ...
    async def set_label(self, subject: SubjectRef, label: str, present: bool) -> LabelOutcome: ...
    async def ensure_branch(self, repo: RepoRef, branch: str, base: Sha) -> BranchOutcome: ...
    async def commit(self, repo: RepoRef, change: ChangeSet, expected_head: Sha, marker: str) -> CommitOutcome: ...
    async def ensure_draft(self, repo: RepoRef, head_branch: str, title: str, body: str) -> DraftOutcome: ...
    async def post_review(self, review: ReviewRef, body: ReviewBody, marker: str) -> CommentOutcome: ...
    async def reply(self, note: FeedbackNote, body: str, marker: str) -> CommentOutcome: ...
    async def dispatch_lane(self, repo: RepoRef, dispatch: LaneDispatch) -> DispatchOutcome: ...
```

Every write returns a typed outcome. For example:
`CommitOutcome = Committed(sha, evidence) | Conflict(actual_head, evidence) | Unknown(evidence)`.
`evidence` is an opaque, provider-native record that is journaled but never
interpreted by the engine.

**Semantics the ports must not flatten.** The adapters keep these:

- GitLab: the Commits API with settling and negative probes;
  `ensure_branch` owns branch creation and `start_branch` is never passed
  (GitLab CE 18.x returns 400).
- GitHub: GraphQL `createCommitOnBranch` with `expectedHeadOid`; a stale
  head is a `Conflict`, never retried blindly.
- GitHub: workflow identity is resolved by workflow path, not display
  name. App installation tokens and actor permissions are GitHub-specific.
- GitLab: pipelines and jobs are normalized; occupancy excludes
  `merge_request_event` verification pipelines.
- Azure (when it returns): Push API `oldObjectId`; template parameters are
  strings and never carry secrets; there is no webhook HMAC.

The per-provider call sites of today's operations are inventoried in
[ports-inventory.md](ports-inventory.md). The largest duplicated flows
(`_advance_harness`, plan and publish, `/go`, publication, CI evaluation,
review, revival) share one provider-neutral algorithm; only the port calls
differ. The estimate is that 55–60% of each service is provider-neutral
orchestration.

### 6.5 The `effects` layer

```python
outcome = await ctx.effects.perform(
    kind="commit", key=f"{run}:{attempt}:{generation}:commit",
    call=lambda w: w.commit(repo, change, expected_head=base, marker=marker),
    probe=probe_commit_by_marker,          # mandatory for every kind
)
```

`perform()` runs as a sequence of **separately committed transitions**.
No database transaction is ever held open across a network call.

| # | Transition (own transaction) | Guard |
| --- | --- | --- |
| T1 | Insert `effects(kind, key, state='requested', payload_digest, step_id, lease_epoch)` | `key` unique. On conflict, load the existing row and go to **Resume**. |
| T2 | `requested → dispatched` | The step's lease epoch is still current (fence). If it is not, stop without calling. |
| — | Network call through the `WritePort` | — |
| T3 | `dispatched → committed / conflict / failed`, with the native evidence | Unfenced: an observed outcome is always recorded, even by a worker that lost its lease |

**Resume.** On a key conflict, or when the reconciler finds an effect with
no outcome:

- `requested`: the call never started, so continuing with T2 is safe.
- `dispatched`: run `probe`, which must return a typed result:
  - `Found(evidence)` → `adopted`;
  - `DefinitelyAbsent` → back to `requested`; re-dispatch is allowed.
    Only kinds whose probe can prove absence may return this, for example
    a marker search on a branch whose head the adapter has read after the
    call window.
  - `Inconclusive` → `unknown`; the run parks `blocked(effect_unknown)`.
    `/reconcile` re-probes.
- A recorded outcome is returned as is. A duplicate key never makes a
  second call.

**Probes per kind:**

| Kind | Probe |
| --- | --- |
| `commit` | Marker on the branch |
| `ensure_branch` | Branch head |
| `ensure_draft` | Draft by head branch |
| `post_comment`, `post_review`, `reply` | Marker in the body |
| `dispatch_lane` | Lane by correlation nonce |
| `set_label` | Read labels |
| `gw_mint` | Key by alias |
| `gw_update` | Read `max_budget` |
| `gw_block` | Read the blocked flag |

Kinds without a probe are not allowed.

**Draft uniqueness (I6).** The draft effect key is the **root** run id
(§6.3, review rounds), plus a database uniqueness constraint on
`(repo, head_branch)` in `effects` for kind `ensure_draft`.

### 6.6 `ApprovedInput`

There is one contract for what the executor receives:
`ApprovedInput(brief_text, approved_digest, plan_digest, spec_digest,
base_sha, revision)`. It is created at `/go` from the frozen `RunSpec` and
the approved plan. A material revision (`/approve-revision`) creates a new
`ApprovedInput` with `revision + 1` through the same gate. The lane fetches
it with `GET /lane/v1/brief`, recomputes `approved_digest` and refuses on
mismatch. The dispatch record, the served brief and the lane's echoed
digest must match. This is the three-way digest, kept as one contract. It
replaces the GitLab revisions seam and the GitHub `AttemptStartSpec`.

### 6.7 Lane protocol v1

All endpoints are under `/lane/v1`. JSON bodies are `forge_protocol` models
with `protocol: "forge.lane/1"`.

| Endpoint | Purpose |
| --- | --- |
| `POST /session` | Exchange a CI OIDC token for a short-lived lane session token bound to `(forge_run_id, attempt, generation)` |
| `GET /brief` | The `ApprovedInput` brief and digests |
| `GET /controls?after=<seq>` / `POST /controls/{id}/ack` | pause, resume, steer, interrupt and checkpoint commands, with sequence and idempotency |
| `PUT /blobs/{sha256}` / `GET /blobs/{sha256}` | Content-addressed blobs for checkpoints and candidates |
| `PUT /checkpoints/{checkpoint_id}` / `GET /checkpoints/latest` | `CheckpointManifest`; `checkpoint_id` is the sha256 of the manifest |
| `POST /candidate` | `CandidateBundle` (diff blob sha plus meta), idempotent by diff sha |
| `POST /report` | `LaneReport`: terminal status, classification, driver, model and optional usage |
| `POST /model-key` | Redeem this attempt's gateway key once; audited |
| `POST /heartbeat` | Liveness |

**Authentication.** Identities are named explicitly:
`forge_run_id`, `attempt` and `generation` on the forge side;
`gh_run_id`/`gh_run_attempt` or `gl_pipeline_id`/`gl_job_id` on the
provider side. Binding works as follows:

- **GitHub Actions.** `dispatch_lane` generates a single-use random
  `nonce`. It sends the nonce as a `workflow_dispatch` input and records it
  on the dispatch record. The lane requests its OIDC token with
  **audience** `"{FORGE_PUBLIC_URL}/lane/{nonce}"` (`permissions:
  id-token: write`). The server verifies the issuer, the JWKS signature,
  the expiry and `aud`. The audience must equal a pending, unexpired
  dispatch nonce. `repository` and the explicitly registered workflow
  claim (path and allowed ref) must match the dispatch record. Reusable
  workflows use `job_workflow_ref`; directly dispatched workflows use
  `workflow_ref`. The server never falls back between claims when the
  registered claim is missing. GitHub documents this distinction in its
  [OIDC reference](https://docs.github.com/en/actions/reference/security/oidc).
  The caller's `ref` must independently match the canonical branch ref
  (`refs/heads/<branch>`) or explicit tag ref recorded by dispatch. Pinning
  a reusable workflow's own ref does not authorize a different caller ref.
  On the first successful session,
  the server records `gh_run_id`/`gh_run_attempt` and consumes the nonce
  (single use). Later sessions of the same attempt (reconnects) must
  present tokens with the same `gh_run_id` and `gh_run_attempt`. A token's `jti` is accepted
  only once (replay cache until expiry).
- **GitLab CI.** forge creates the pipeline through the API and records the
  returned `gl_pipeline_id`. The lane job requests `id_tokens` with
  `aud: $FORGE_PUBLIC_URL`. The server verifies the issuer (GitLab URL),
  JWKS, expiry and `aud`, then requires `pipeline_id == gl_pipeline_id`,
  plus matching `project_path` and `ref`. It records `gl_job_id` on the
  first session. The `jti` replay rule is the same.
- **No shared-secret fallback in 1.0.** OIDC is required: GitHub.com or GHES
  with OIDC, and GitLab ≥ 15.7. A hardened fallback for environments
  without OIDC is a post-1.0 backlog item.

**`CheckpointManifest`.** Entries are `(path, mode, sha256 | null,
state ∈ {new, modified, deleted})` relative to the attempt base. Restore
is exact: files are created, modified and deleted as listed, and every blob
is verified before write. Size limits are enforced on both sides.

**`CandidateBundle`.** The lane collects the diff independently of the
working index. It stages the whole tree into a temporary index
(`GIT_INDEX_FILE=<tmp> git add -A -- . ':!.forge'`) and runs
`git diff --cached --binary <base_sha>`. Untracked and binary files are
therefore included. The bundle carries `diff_sha256`, `base_sha`,
`driver`, `model`, `terminal_reason` and optional usage. Candidate
identity and idempotency key:
`(forge_run_id, attempt, generation, base_sha, approved_digest, diff_sha256)`.
The server re-validates everything in `publishing/` (I2).

### 6.8 `forge-lane` runner and drivers

`forge-lane run` performs these steps:

1. Session (OIDC).
2. Fetch the brief and verify its digests.
3. Restore the checkpoint if resuming.
4. Write the driver config: MCP servers (ADR-0022) and permission and deny
   rules.
5. Start the driver session.
6. Supervise concurrently: the driver turn, the control drain, the
   wall-clock budget and heartbeats.
7. Checkpoint on pause and every 10 minutes
   (`FORGE_LANE_CHECKPOINT_INTERVAL`, default 600s).
8. Collect the candidate.
9. Upload and report.

Exit codes are deterministic. Logs go through the collapsible trace filter
(ADR-0025).

**Pause and resume semantics:**

- `/pause` → the lane interrupts the driver, writes a checkpoint, reports
  `paused` and **exits**. No runner is held while paused.
- `/resume` → the engine dispatches a new generation (`generation + 1`) of
  the same attempt. The lane restores the checkpoint and restarts the
  driver with the same `ApprovedInput` plus a resume note: the list of
  files changed so far, derived from the checkpoint.
- **Guarantee:** the filesystem work-in-progress is restored exactly.
  Conversational state is best-effort. Drivers that declare
  `resume_session` (for example Claude SDK session resume) may reattach;
  the others start a fresh session over the restored tree.

```python
class Driver(Protocol):
    name: str
    capabilities: frozenset[Capability]   # {"interrupt", "steer", "resume_session"}
    async def start(self, brief: Brief, workspace: Path, env: DriverEnv) -> Session: ...

class Session(Protocol):
    def events(self) -> AsyncIterator[DriverEvent]: ...
    async def interrupt(self) -> None: ...
    async def steer(self, text: str) -> None: ...   # only when "steer" in capabilities
```

The first-class drivers are `claude_code` (claude-agent-sdk: interrupt and
steer) and `copilot_cli` (ACP: interrupt only). Contrib drivers are `codex`
(app-server JSON-RPC), `opencode` (serve HTTP/SSE) and `grok` (CLI batch).
They are ported from `adaptive/drivers/*` and
`harnesses/scripts/drivers/*.sh`. CLI versions stay pinned, and npm CLIs
install the platform binary explicitly.

### 6.9 Budgets and model credentials through the gateway

**Pool algebra (cumulative per run).** At freeze, the run limit `B` (USD,
plus the calls, tokens and wall-clock axes) is split once
(`closing-partition/1`) into the implementer pool `P = B − C` and the
closing reserve `C`. Every attempt of the run draws from the **same** `P`:
the initial attempt, repair cycles, `/retry` and resume generations.

```
held(P)      = Σ reservations of open attempts
             + Σ unknown usage (held at the full reservation)
             + Σ accrued-unsettled usage (held at the reservation until final)
available(P) = P + Σ applied amendments(P) − Σ settled(P) − held(P)
```

An attempt's reservation is `r = min(requested_allowance,
available(P))`. If `r` is below the minimum useful allowance, the run
parks `blocked(budget_exhausted)`. The reviewer and planner draw from `C`
and from a planning allowance frozen in the `RunSpec`, with the same
algebra. Review-round child runs have their own `B`, split the same way.

**Keys: minting, delivery and recovery.** Every gateway operation goes
through `effects`:

| Kind | Operation | Probe |
| --- | --- | --- |
| `gw_mint` | Mint a key | Look up by alias |
| `gw_update` | Change the key's `max_budget` | Read `max_budget` |
| `gw_block` | Block the key | Read the blocked flag |

- **Mint.** When the attempt is dispatched, forge mints a LiteLLM virtual
  key with:
  - a deterministic `key_alias = forge:{run}:{attempt}:{generation}`;
  - `max_budget = r`;
  - `duration` = the attempt deadline;
  - a `models` allowlist from the `RunSpec` route;
  - metadata with the run, attempt and generation.
- **Storage.** The key value is stored **encrypted at rest** in
  `gateway_keys` (AES-GCM, server key `FORGE_SECRET_KEY`) until the attempt
  generation ends; then the ciphertext is purged.
- **Delivery.** `POST /lane/v1/model-key` returns the same key to the
  **same authenticated lane session** any number of times. This is
  idempotent and survives a lost response. A new generation (resume, or a
  runner restart through re-dispatch) gets a new key. The previous key is
  blocked first.
- **Logging.** Keys never appear in logs, comments or evidence.
- **Claude Code** uses `ANTHROPIC_BASE_URL=<gateway>` and
  `ANTHROPIC_AUTH_TOKEN=<key>`.

**Enforcement.** The gateway rejects requests past `max_budget`, so the
hard cap holds even if the lane misbehaves. The LiteLLM version is pinned.
A CI integration job runs a real LiteLLM container against a stub upstream
and checks three things:

- budget rejection;
- expiry;
- that spend is visible by alias.

**Settlement finality.** When the attempt ends (report, cancel or
deadline):

1. `gw_block` is performed.
2. After a settle window, `2 × FORGE_GATEWAY_SPEND_FLUSH_SECONDS` (default
   2 × 60s), forge reads the key's spend twice, at least 30s apart.
3. Two equal reads mark usage `settled`, and the reservation is released
   down to the settled amount.
4. Otherwise usage stays `accrued` and is held at the full reservation.
5. If the gateway is unreachable, usage is `unknown` and stays held
   (unknown ≠ 0).
6. The reconciler keeps retrying. `/status` shows accrued and unknown
   amounts explicitly.

**Amendments.** An operator amendment first writes one durable row per
`(run, command)`, applied once, which raises `P` or `C`. Then `gw_update`
raises the live key's `max_budget`. Until `gw_update` is recorded as
committed, the effective cap is the old one. The failure mode is too
strict, never too loose.

**Unmetered driver (the scoped I5 exception).** Copilot CLI runs on a
GitHub Copilot subscription token that the customer supplies as an Actions
secret. It never receives a gateway key. Its attempt budget is wall-clock
plus a turn limit, both enforced by the lane supervisor and by the
dispatch deadline. Its usage is recorded as `unmetered`, never as `0`, and
does not draw from `P`.

**Removed:**

- the credential broker and grant routes;
- the redemption audit routes;
- finality folding in usage ingestion.

forge no longer delivers model credentials through native secrets or
protected variables. The only model credential stored in a target
repository is the optional, customer-managed Copilot token.

### 6.10 Persistence and cutover

The schema can break (lab and dogfooding only). **During the transition,
the engine's tables coexist with the legacy tables in the same database.**
Additive migrations in the existing chain (033 onward) create tables with
the `eng_` prefix, so the legacy GitLab services keep running while GitHub
moves.

| Table | Purpose |
| --- | --- |
| `eng_runs` | Identity, subject, state, `failed_handler`, reason, root and parent (review rounds), attempt and generation counters |
| `eng_run_specs` | Frozen `RunSpec` plus digest |
| `eng_approved_inputs` | `ApprovedInput` revisions |
| `eng_gates` | Consume-once approvals |
| `eng_steps` | Queue and journal: handler, due time, lease epoch, attempts, result |
| `eng_effects` | Intent, outcome and probe evidence, keyed and unique; draft uniqueness on `(repo, head_branch)` |
| `eng_candidates` | Bundle meta and identity (§6.7), validation result, published sha |
| `eng_budget_pools`, `eng_budget_reservations`, `eng_budget_amendments`, `eng_usage` | Money (§6.9) |
| `eng_gateway_keys` | Alias, attempt and generation, encrypted value (purged at end), block state |
| `eng_control_commands` | Mailbox for pause, resume and steer |
| `eng_checkpoints`, `eng_blobs` | Manifest index; blob storage is filesystem or S3-compatible |
| `eng_review_rounds` | Round lineage and slot per root `ReviewRef` |
| `eng_lane_sessions` | Nonce, OIDC identity binding and `jti` replay cache |

`event_inbox` is shared: one dedup table for both paths.

**Routing and fencing during the transition:**

- Each provider has a flag `FORGE_ENGINE_<PROVIDER>=v2`. It routes **new**
  runs of that provider to the engine.
- A subject with an active legacy run stays on the legacy path until that
  run terminates. Its commands route by run ownership.
- The engine's admission also checks legacy `flow_runs`, so one active run
  per subject holds across both paths.

**Rollback (flag off) needs no database restore:**

1. New runs route to the legacy path again.
2. In-flight engine runs of that provider are cancelled with an
   explanatory comment. Their branches and Draft PRs stay for humans to
   close or reuse.
3. Engine tables keep their history.

**After both providers migrate (P3-20):**

1. The legacy tables are dropped.
2. The `eng_` prefix is removed by migration.
3. The alembic chain is squashed into a fresh baseline.
4. Old runs are exported to JSON first (`forge export-runs`).

### 6.11 Configuration, doctor, observability

- **Settings.** A provider's section is validated only when its
  `FORGE_<PROVIDER>_ENABLED` flag is on. `FORGE_GITLAB_ENABLED` joins the
  existing GitHub and Azure flags in Phase 0. New settings:
  - `FORGE_PUBLIC_URL`;
  - `FORGE_GATEWAY_URL` and `FORGE_GATEWAY_ADMIN_KEY`;
  - `FORGE_GATEWAY_SPEND_FLUSH_SECONDS`;
  - `FORGE_SECRET_KEY` (encryption at rest for attempt keys, signing for
    lane session tokens);
  - `FORGE_LANE_CHECKPOINT_INTERVAL`;
  - `FORGE_ENGINE_<PROVIDER>`.

  The configuration reference is generated from `Settings`.
- **doctor** never raises. Each check yields PASS, WARN or FAIL with a fix
  hint: settings, database and migrations, gateway health and key minting
  (dry run), each enabled provider (App installation or token scopes,
  webhook), and per-repository onboarding (`--repo`).
- **Logs** are structured JSON with `run_id`, `step` and `attempt`.
- **Metrics:** `forge_runs{state}`, `forge_step_seconds{handler}`,
  `forge_effects_total{kind,outcome}`, `forge_spend_usd_total{kind}` and
  `forge_unknown_effects`.

### 6.12 Queue and Redis (decision point in Phase 3)

The durable queue is the `steps` table: claims with
`FOR UPDATE SKIP LOCKED` plus fenced leases. Worker wake-ups use Postgres
`LISTEN/NOTIFY`, with polling as the fallback. Redis is retained only while
the old services run. The default is to **remove Redis from the required
stack at the end of Phase 3**, unless the Phase 3 load test shows a need.
That test is 50 concurrent runs with a step-latency p95 under 2 seconds.

## 7. Test strategy

| Layer | Scope | Runs |
| --- | --- | --- |
| `domain` unit and property tests | Transitions are closed, digests are stable, budget math, path policy | Every PR, seconds |
| Engine scenarios on `providers/fake` | Commands plus faked CI and lane outcomes → expected state trace and effects journal. This is the bulk of behavior tests. | Every PR |
| Adapter contract tests | Every port method against recorded HTTP fixtures (pytest-httpx), for GitHub and GitLab | Every PR |
| Conformance | The same engine scenarios through both adapters over their fakes | Every PR |
| Failure injection | Automated enumeration of step boundaries and of the effect transitions T1, T2 and T3 (§6.5): kill → recover → assert I3, I6 and I7 | Every PR (in-process); OS-level SIGKILL nightly |
| Lane | Protocol golden files, checkpoint round-trip property test, driver conformance with fake SDKs | Every PR; driver smoke with real CLIs nightly |
| Live end-to-end smoke | One task per provider on sandbox repositories, run against the built artifact | Release workflow and nightly |
| Invariant Suite | `tests/invariants/` (§4) | Required on every PR |

Rules:

- The default suite finishes in ≤ 5 minutes (≤ 8 minutes until Phase 3
  ends). Slow tests are marked `slow` and run nightly and on `main`.
- Tests are hermetic:
  - use `sys.executable` shims instead of `python` on `PATH`;
  - stop subprocesses at teardown, make helper processes exit when pytest
    dies, and fail the session if any child survives;
  - never use the network.
- The **test disposition ledger** (`test-ledger.csv`, created in Phase 0)
  records for every current test file: keep, port (to engine scenarios or
  adapter contracts) or drop (with the deleted code). A test file is
  deleted only when its ledger row says so.
- The **edge-case register** (`docs/reference/edge-cases.md`) lists every
  hard-won gotcha with the test that pins it. A module is deleted only
  after its gotchas are registered.

## 8. Guardrails and working agreement for AI agents

- **Unit of work.** One task is one issue with acceptance criteria and one
  PR of about 500 net lines or fewer. Pure deletions are exempt.
- **PR template:**
  - invariants touched and their tests;
  - metrics delta (lines, largest file, functions over 80 lines);
  - ADR impact;
  - the ledger rows changed.
- **CI checks:**
  - `ruff`;
  - `mypy --strict` on `domain`, `engine`, `ports` and `forge_protocol`;
  - `lint-imports`;
  - `scripts/ratchet.py`: per-file and per-function line counts may not
    grow, and new files and functions must be within the limits;
  - `scripts/prose_lint.py`: added lines contain no review or issue IDs;
  - `deptry`;
  - diff coverage ≥ 85% on changed lines;
  - `scripts/ast_equivalence.py` for PRs labelled `prose-diet`: the AST
    without docstrings is identical before and after.
- **ADRs** are one page, written only when a boundary changes, and state
  the decision, not the history.
- **Owner review checklist:**
  - Is the invariant evidence intact?
  - Did the metrics move the right way?
  - Is anything outside the task scope?
- **Weekly:** run `scripts/metrics.py` and update
  [metrics.md](metrics.md).

## 9. Release and evidence

The release workflow is:

1. Build the wheel, sdist and image once, and record their sha256 digests.
2. Run the tests and the Invariant Suite against the built wheel,
   installed in a fresh venv.
3. Boot the built image on a fresh database, run migrations and check
   health.
4. **Stage a release candidate.** Publish a GitHub pre-release
   `vX.Y.Z-rc.N` carrying the exact wheels (including `forge-lane`) and
   push the image by digest.
5. **Run the live smokes on the RC.** Dispatch inputs carry the RC
   `forge-lane` wheel URL and sha256; the lane installs by URL with a hash
   check.
   - GitHub: sandbox repository.
   - GitLab: a sandbox project on gitlab.com, which supports `id_tokens`
     with shared runners, or a self-hosted runner attached to the lab.

   Both smokes are mandatory and automated. Each writes a small JSON
   result (run id, pipeline, digests, outcome, spend).
6. **Release report:** a short signed `release-report.json` with the
   digests, the test and invariant results and the smoke results. Also an
   SBOM, build provenance (`actions/attest-build-provenance`) and the
   cosign image signature.
7. **Promote the same bytes:** the final release reuses the RC digests; it
   does not rebuild.

The tested bytes are the shipped bytes by construction: the smokes use the
RC artifacts built in step 1, and promotion never rebuilds. No separate
exact-composition gate is needed.

The following are removed from the package and from CI:

- `release_manifest`, `release_promotion`, `profile_qualification` and
  `capability_manifest`;
- mutation gates, the promotion verdicts and supported-profile freezing;
- `qualification/` records.

Historical evidence (`qualification/`, `evaluation/`, `docs/evaluation/`,
`docs/releases/evidence/`) is archived on the branch
`archive/evidence-0.42` and the tag `pre-simplification`, and removed from
`main`.

## 10. Migration and cutover

| Step | What | Rollback |
| --- | --- | --- |
| Phase 1 release (0.43.0) | Subtracted code; GitHub and GitLab still on the old services | Redeploy 0.42.0 (the schema is unchanged in Phase 1) |
| Phase 2 | `forge-lane` is live only in smoke tests; old lanes unchanged | — |
| Phase 3 core | Additive `eng_*` tables (migrations 033+); the engine exists but routes nothing | Migrations are additive; nothing to roll back |
| Phase 3a | New GitHub runs route to the engine behind `FORGE_ENGINE_GITHUB=v2` (subjects with an active legacy run stay on legacy). Dogfooded on `forcewake/forge`, then made the default; after one week, old GitHub code is deleted. | Flag off: new runs go to legacy; in-flight engine runs are cancelled with a comment; no database restore (§6.10) |
| Phase 3c | Same for GitLab with `FORGE_ENGINE_GITLAB=v2` | Same |
| Phase 3 end | Legacy tables dropped, prefix removed, chain squashed into a fresh baseline after `forge export-runs` | Restore the pre-squash backup (taken automatically by the migration command) |

## 11. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Hard-won edge cases are lost with deleted code | Edge-case register and characterization tests before deletion; tag `pre-simplification` |
| The switch breaks the working product | Per-provider flags route only new runs, and subjects with an active legacy run stay on legacy; dogfood first; the old path is kept one week after the default flips; rollback needs no database restore |
| The gateway is a single point of failure, or spend readback lags | Runs park `blocked(gateway_unavailable)`; reservations stay held; keys have TTLs; doctor checks the gateway |
| No OIDC on some installations | Documented requirements (GitHub.com or GHES with OIDC, GitLab ≥ 15.7). A hardened fallback is a post-1.0 backlog item; 1.0 has no shared-secret path. |
| OIDC token misuse by another job in the same repository | Per-dispatch audience nonce (GitHub) or pipeline-id binding (GitLab), single-use nonce, first-session binding of the provider run id, `jti` replay cache (§6.7) |
| Effects crash windows | Separately committed T1/T2/T3 transitions, fencing before the call, mandatory typed probes, atomic step result plus successor (§6.3, §6.5); the failure-injection harness enumerates every boundary |
| Gateway spend reporting lags | Settle window plus two equal reads; otherwise usage stays `accrued` and fully held; LiteLLM pinned and integration-tested (§6.9) |
| Bloat returns through agent-generated code | Ratchets, PR size limit, weekly metrics |
| The owner's review bandwidth | Small PRs, checklists, automatic metrics in PR descriptions, batched deletions |
| Copilot CLI spend is not metered | Wall-clock and turn caps; recorded as `unmetered` |
| Parity gaps when GitHub moves first | Characterization checklist from the old `github_service` tests in the ledger; shadow period on the forge repository |

## 12. Success metrics per phase

| Metric | Baseline | Phase 1 exit | Phase 2 exit | Phase 3 exit | Phase 4 exit |
| --- | --- | --- | --- | --- | --- |
| Server `src/forge` lines | 190K | ≤ 110K | ≤ 110K | ≤ 75K | ≤ 75K |
| `forge-lane` lines, zero server imports | — | — | ≤ 8K | ≤ 8K | ≤ 8K |
| Largest file | 11,433 | unchanged (services still exist) | unchanged | ≤ 1,000 | ≤ 1,000 |
| Functions > 100 lines | 234 | ≤ 200 | ≤ 190 | ≤ 20 | ≤ 20 |
| Default test suite | 16.5 min, not hermetic | ≤ 8 min, hermetic | ≤ 8 min | ≤ 5 min | ≤ 5 min |
| Active documentation | ~319K words | ≤ 60K | ≤ 60K | ≤ 50K | ≤ 40K |
| Prose share in `src` | ~28% | ≤ 18% | ≤ 16% | ≤ 12% | ≤ 12% |
| Direct runtime dependencies | 14 | ≤ 10 | ≤ 10 | ≤ 9 (Redis removed) | ≤ 9 |
| First Draft PR on a fresh GitHub repository | not measured | — | — | ≤ 60 min | ≤ 30 min, by a non-owner |

## 13. Edge-case register (seed)

These entries each get a test before the related code is touched:

- GitLab CE 18.x: `start_branch` with an existing branch returns 400;
  `ensure_branch` owns creation.
- npm CLIs: optionalDependencies flake → a silent futex hang; install the
  platform binary explicitly.
- Claude Code on z.ai: model ids need the `[1m]` suffix for the 1M window.
- Exactly one Draft MR under concurrent publication (commit `536905c`).
- Bot-authored notes never trigger commands (ADR-0028).
- GitHub workflow identity is resolved by path, not display name.
- GitLab verification pipelines (`merge_request_event`) are not lane
  occupancy.
- podman short image id (12 hex) versus inspect id (64 hex): compare
  normalized ids.
- Redis heartbeat keys outlive a dead worker by about 30s (relevant only
  while Redis exists).
- An unknown publication outcome is resolved by marker lookup, never by a
  second write.
- A human commit on the factory branch during a round is a `branch_drift`
  conflict and is never reverted.
- A corrupted checkpoint on a required resume halts before any model
  session.

## 14. Decision log (owner, 2026-09-29)

| Question | Decision |
| --- | --- |
| Primary goal | All, in order: simplification first, then users |
| Radicality | Strangler toward the target architecture; the product is always working |
| Providers | GitHub primary, GitLab second, Azure frozen (maintenance branch `release/0.42.x`) |
| Drivers | Claude Code and Copilot CLI first-class; Codex, opencode and Grok contrib; builtin removed |
| Qualification machinery | Move out of the package and simplify: CI, signed release, one live end-to-end smoke per provider, short release notes |
| Executors | Mostly AI agents under the owner's control; the owner reviews and decides |
| Data compatibility | Lab and dogfooding only: the schema may break; export or recreate; drain or cancel active runs |
| Approach | A: subtract → separate → unify → productize |
| Budgets and credentials | Through LiteLLM virtual keys, with OIDC for lane authentication |
| Document location | In the repository, in English |
