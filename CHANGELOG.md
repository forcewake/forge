# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.15.0] - 2026-09-21

### Fixed — authority boundary coherence (fifth external review 44cdae: all 12 findings closed — evidence in `python -m forge.release_manifest`)

- **D01 — config cache identity**: the project-config cache keys on the
  canonical authority identity (provider class, repository, requested ref,
  path) — two repositories of one project (or two refs of one repository)
  no longer cross policies through the cache; cached absence of one repo
  never masks another's restrictions.
- **D02 — strict policy schema**: `implement.paths: "src/**"` (a string)
  used to fall through to `[]` == whole repository, widening a typo into
  UNRESTRICTED scope; `forge: []` raised AttributeError through the typed
  reader. Both are typed invalid now — malformed never means "allow more".
- **D03 — the frozen scope reaches the commit boundary**: both GitHub
  publish sites pass the spec's allowed_paths; an out-of-scope candidate
  results in ZERO commits and ZERO PRs (service-bound regression,
  mutation-verified).
- **D04 — cancel-before-dispatch forbids the write**: the publication
  grant is re-checked immediately before the native dispatch — a cancel
  landing during the paid propose refuses the commit (the branch CAS
  checks the expected head, not the run's right to publish).
- **D05 — factory branches from the attempt OID**: a moved main no longer
  shifts the execution context past the approval (target_branch stays the
  MR destination).
- **D06 — a declared manifest, even empty, is the boundary**: unset
  manifests keep the legacy default; an explicit {'drivers': []} allows
  no driver (callers widen only None).
- **D07 — the cohort rate is a ratio of sums** over the whole joined
  population (run+attempt identity survives the flattening) — the old
  form divided the LAST attempt's numbers (order-dependent; crashed on
  an empty cohort).
- **D08 — receipts provenance**: workspace-file command receipts are
  stamped `self_reported` — telemetry, never wrapper-observed proof.
- **D09/D11** — agree-collision semantics documented (D09 development
  note); `_publish_candidate_run_aware` is the run-aware publication
  entry — the only way the service publishes, loading approved policy by
  run identity with the grant re-check inline.
- **D10 — mutation guards run baseline-then-mutant** on identical traces.
- **D12 — README version/test-count/image tracked by tests** (found stale
  at 0.11.0 AND 0.13.0 in consecutive reviews; now CI-enforced).

## [0.14.0] - 2026-09-21

### Fixed — contract hand-off coherence (fourth external review 7f0139e: all 12 findings closed — evidence in `python -m forge.release_manifest`)

- **C01 — identity-preserving CI verdicts**: a display name claimed by
  several workflow identities with DISAGREEING conclusions is
  ambiguous_check_identity (never verified; agreeing collisions are fine);
  has_pending is decided after the authoritative collapse over the PROOF
  set — a stale/optional pending run no longer holds completed required
  checks hostage.
- **C02 — one budget truth**: the spec builders use the selection's
  RESOLVED ceilings (pinned to the opened RunBudget when the planner moved
  the class) — RunBudget == RunSpec == selection; the class name is never
  re-resolved at freeze.
- **C03 — the credential registry is the contract**: the AzDO recipe maps
  grok-build→FORGE_GROK_AUTH (not XAI, not 'grok'), opencode→ZAI_API_KEY,
  copilot→its token alone, claude-code→the Anthropic surface; both shipped
  recipes AND the GitHub workflow are cross-checked against
  DRIVER_CREDENTIAL_VARS by tests. An enforced dispatch missing the read
  token or any envelope input FAILS CLOSED — a stale pre-provisioned brief
  never executes.
- **C04 — Azure recovery is repository-scoped** (B08's twin): the config/
  attempt recovery scans select only the bound repo; the durable subject
  column is populated at AzDO start (the scans could never match before).
- **C05 — frozen-spec dispatch on GitLab**: BackendStartSpec — the harness
  backend executes the approved model/target (legacy callers fall back
  loudly); the Draft MR target comes from the frozen spec too. Settings
  drift after approval cannot move a dispatch or an MR.
- **C06 — a declared capability manifest is a strict upper bound**: an
  empty/disjoint intersection raises at compile time instead of silently
  degrading to the default driver.
- **C07 — identity-joined effective output rate**: the cohort rate divides
  tokens by the SAME calls' durations (attempt-id join); count equality
  proves nothing and yields None; the name is honest (full-request
  duration ≠ decode speed).
- **C08 — bounded MR I/O under the reservation lock**
  (FORGE_MR_IO_TIMEOUT_SECONDS): a hung list fails CLOSED, a hung create
  records unknown_outcome.
- **C09 — honest 019 downgrade**: genuine multi-observation history refuses
  ('forward-only from here') instead of failing mid-index or deleting audit
  rows; proven on real Postgres both ways.
- **C10 — ObservedExecution command receipts**: the trusted wrapper's
  (argv, exit, report) rows from .forge/commands.tsv ride the candidate
  meta — 'allowed' stays declared vocabulary, these are the proof it ran.
- **C11 — mutation guards**: reverting C01's ambiguity map or B01's epoch
  makes the guards FAIL (the regression tests keep their bite).
- **C12 — README/quick-start synced** to the current release (was
  0.11.0/2601 tests — a new user could install a release predating every
  fix the docs describe).

## [0.13.0] - 2026-09-21

### Fixed — recovery/evidence/recipe coherence (third external review e53ffd2: all 15 findings closed — evidence in `python -m forge.release_manifest`)

- **B01 — immutable verification deadlines**: the wait anchors to a persisted
  verification_epoch {candidate_sha, started_at} (GitHub+Azure), never to
  FlowRun.updated_at — every observation merges evidence and slid the deadline
  forever (probe: 480 observations / 7200 simulated seconds, no timeout).
- **B02 — authoritative CI evidence**: the newest RUN (run_number, id) is the
  authoritative occurrence (run_attempt orders attempts WITHIN a run and must
  never order across runs — an old rerun's success masked a newer failure);
  observations group by workflow identity before the display-name merge.
- **B03 — MR intent vs immutable attempts** (migration 019): mr_reservations
  — the ONE-MR-per-run+branch logical intent, committed BEFORE provider I/O,
  FOR UPDATE-serialized — separate from the action journal; the lost-response
  window adopts via its OWN observation row (no InvalidActionTransition
  deadlock); a failed MR-list read fails CLOSED (no create on an unknown
  surface).
- **B04 — the AzDO lane renders the brief ENFORCED**: the plan leg freezes the
  BriefEnvelope, embeds the approved sections in the plan comment and
  dispatches plan_note_id/envelope_digest/spec_digest (service, handle and
  crash-re-dispatch); an edited comment/work item fails the lane closed — no
  stale-brief fallback on the enforced path.
- **B05 — the shipped AzDO recipe is dispatch-only for real**: explicit
  `trigger: none` (its absence left the IMPLIED CI trigger queueing the lane
  on every push); repair_context flows parameter→variable→env (the
  $(repair_context) macro referenced an undefined variable); harness
  credentials scope to the selected driver via conditional LANE_* variables.
- **B06 — verification waivers freeze into the spec** at approval: a
  post-approval global waiver flip cannot loosen an approved run (empty
  frozen set = NO waivers — no settings fallback on v3+).
- **B07 — required-mode verification**: a non-empty frozen required list with
  no observed checks is a missing MANDATORY GATE (wait → deadline block),
  never an unverified READY; an unreadable PR head surfaces
  freshness_unknown in the evidence.
- **B08 — repository-scoped recovery**: every repo-bound recovery scan
  (revivals, attempt recovery, config blocks) selects only the bound
  repository's runs — a provider-wide scan drove repo B's runs through repo
  A's reader/client.
- **B09 — honest cohort economics**: all-unknown durations/repairs are None
  (never or-0); a receipt-less attempt makes the unit's exact cost unknown
  with a priced LOWER BOUND (cost_exact=false); rates build only over
  proven-matched populations.
- **B10 — pass-1 evidence backfill**: the committed ledger/report carry the
  known execution profile; usage stays honestly unknown — no zero-cost
  claims.
- **B11 — current-plan selection**: the task-aware harness selection compiles
  against the CURRENT plan (the pre-plan compile only reserves the planning
  budget) — a reused planner object can never leak a previous run's plan.
- **B12 — ObservedExecution**: the candidate meta carries what the lane
  ACTUALLY did (driver, exit, usage completeness, candidate-changed) — the
  observed twin of the declared profile; allowed-but-unexecuted is never
  executed.
- **B13 — state-specific status comments**: the post-publish note says
  "candidate published — verification pending" (not a premature ready);
  the verified/unverified ready note follows verification + review.
- **B14 — machine-validated finding coverage**: B01-B15 closures in the
  release manifest with grep-verifiable markers; the suite runs
  warning-clean (the aiosqlite worker-thread teardown artifact is
  documented and narrowly ignored).
- **B15 — recovery-scan boundary pinned by test**: provider services never
  re-implement their own blocked-run scans; the shared dispatcher helpers
  own them.

## [0.12.1] - 2026-09-20

### Fixed — live leg checks (2026-09-20: GitLab + GitHub + Azure DevOps driven end-to-end against real instances)

- **GitHub — launch correlation**: the runs-list call sent the filter as the undocumented
  `head_branch=` param (the API wants `branch=` and silently drops unknown names), so
  concurrent dispatches sharing one re-frozen attempt base latched each other's workflow runs
  — whole batches died `harness_artifact_missing` in the grace window. Client sends `branch=`;
  the executor re-verifies branch equality client-side (e7b308a).
- **Cohort seed CI**: the seed shipped forge's own ci.yml into fixture repos (no
  pyproject.toml → permanently red baseline, repair loop could never converge). The seed now
  renders a per-unit checks workflow FROM the predeclared acceptance checks (927021b).
- **Publication intents**: completing a terminal intent with the SAME effect id is now an
  idempotent no-op instead of a raise that aborted the publish step mid-transaction (the
  aborted journaling then degraded the review to "(diff unavailable)" — fixed separately by
  falling back to the base..candidate compare when the PR ref is missing) (22e855a).
- **Cohort ledger**: attempt rows record once per drive; the cancel procedure targets the
  in-flight row instead of a stale historical one (82901e9).
- **Azure lane packaging**: the lane published all of `.forge/` including forensic logs; the
  archive allowlist is CLOSED (exactly candidate.diff + candidate.meta.json) — now staged
  into a clean `forge-output/` (5b2add4).
- **Azure dispatch/revival**: the dispatch identity is journaled BEFORE the Runs-API call
  (A12); a revival that still lacks identity re-parks the run blocked instead of stranding a
  proposing zombie (69e9799).
- **Azure self-trigger**: every forge comment carries a hidden `forge:authored` marker and
  the gateway skips marker-bearing comments BEFORE the author check — on single-PAT
  deployments forge posts under the operator's identity and the author check alone both
  missed forge's own notes AND would drop the operator's real commands (0acda79).
- **Azure candidate diff**: the emit step scrubs `__pycache__`/`.pyc` and emits a text-only
  diff — a staged .pyc failed every candidate `binary_not_supported` (2cbdf4e).
- **Azure verification read**: the builds list requires `repositoryType=TfsGit` next to
  `repositoryId` AND a repository GUID (a name 400s) — without these the verification pass
  read NO builds and every run died `verification_timeout` (06b5554, b36d31f).

## [0.12.0] - 2026-09-19

### Changed — guarantee parity (second external review d16f523: all 18 findings closed — evidence in `python -m forge.release_manifest`)

- **A01/A02 — same spec, same verification everywhere**: GitHub and Azure freeze and consume
  the same executable RunSpec v3 as GitLab; verification is positive proof that the REQUIRED
  checks from the frozen contract ran (skipped/neutral/unknown need a waiver; cancel/timeout
  are infrastructure, not code repair; the actual head is re-read before verified-ready).
- **A03 — the lane executes the approved bytes**: BriefEnvelope digests bind task/plan;
  any post-approval edit fails the lane closed (re-approval required).
- **A04/A05 — ownership through the whole step**: guarded CAS is the only write path;
  the publisher validates claim ownership before the native call; the sequential worker
  claims one step at a time; heartbeat loss fails the handler closed.
- **A06/A07 — object-level authorization**: MCP run tools honor repository allowlists;
  full-ID operator commands carry the same subject scope as every other form.
- **A08/A09 — shipped-recipe fixes**: non-hidden artifact staging; tokenized permission
  rules (the glued-string bug that defeated the allowlist expansion).
- **A10/A11 — durable concurrency**: concurrent budget creation via ON CONFLICT arbiter;
  retry/auto-revive as durable single transitions with idempotency by delivery id.
- **A12 — effect certainty**: negative probes open a bounded settle window (never treated
  as proof of absence); GitLab parks unknown, GitHub/Azure lean on native CAS.
- **A13 — config scope**: read failures park the run; only confirmed absence earns the
  default profile; provenance frozen in the spec.
- **A14/A16 — conformance and evidence**: composed scenarios on real legs; a verifiable
  release-evidence manifest (28 entries, honest levels, fail-closed guards) as the source
  of truth for capability claims.
- **A15 — ADR-0027 slice 2**: ObserveVerification extracted as one provider-neutral use case.
- **A17/A18 — measurement and profiles**: a 14-task delivery evaluation cohort harness and
  a versioned execution profile (target-contract lanes, bootstrap classification).

## [0.11.0] - 2026-09-18

### Added — Phase B/C/D complete (external review e8bf381: every finding closed — evidence-scoped)

"Closed" above means the fix and its landed test evidence are in this tree
(per-finding evidence levels: `python -m forge.release_manifest`); it does
NOT mean every capability is exercised at every runtime depth — nightly OS
failure injection and the real-provider dogfood loop stay on their own
evidence classes and gates.

- **A16** release-evidence manifest: capability/status claims are generated
  from a verifiable registry (`python -m forge.release_manifest`), not
  hand-written absolutes — per capability, provider/backend, level
  (implemented / contract_tested / live_canary_tested / not_run) with the
  evidence pointer, CI job and gate; boot canary, subprocess FI, coroutine FI
  and real-provider e2e stay separate evidence classes; unknown stays
  explicit (`not_run`), never converted to pass; pyproject/`__version__`
  consistency is asserted at manifest build time (the R30 discipline, now
  failing in tests, not only in the release workflow).
- **A02/A13** verified in-tree and recorded as the first d16f523-review
  closures: GitHub and Azure freeze the same executable spec v3
  (`EXECUTABLE_SPEC_SCHEMA_VERSION`), and project-config reads are typed —
  only a confirmed absence earns the default profile. A01, A04-A12 remain
  unclaimed by the manifest until their fixes verify in-tree.

- **R04** ExecutableRunSpec v3: the gate approves bytes that execute — task/plan/model/
  policy/budgets frozen content-addressed, digest-verified on every read.
- **R07** bounded steps with replay: every non-deterministic step checkpoints; a crash
  never re-calls the model or re-derives published work.
- **R10** ExecutionClaim + guarded transitions + publication grants (cancel fences claims).
- **R11** PublicationIntent persisted before HTTP on all three providers; probe-first
  reconciliation adopts lost pushes (the live branch_drift case now adopts).
- **R13** numeric budget profiles enforced on the standard path (planning reserves).
- **R14** BlobReadResult: only a confirmed 404 proves absence.
- **R24** honest delivery metrics (accepted/rejected/rework, time decomposition).
- **R29** operator commands: /status, /why-blocked, /reconcile (all providers).
- **R31** capability-aware harness selection (manifest + policy-bound planner proposal +
  numeric budget binding).

### Fixed
- Provider-namespaced unique index; Alembic live-head startup gate replaces the
  stale marker; MCP classic-tool scope bypass; worker reaper parks exhausted
  steps; late-callback supersede widened to all terminal states.

### Added — terminal-failure revival (Tier 1 auto-revive + Tier 2 `/retry`)

- **Tier 1 — automatic revival of transient deaths**: every `failed`
  terminalization is classified (`forge.runs.revival`) before the run parks.
  A *transient* cause (dispatch/CI 5xx, network, timeout, rate limits, runner
  startup, an empty harness-start error) parks `blocked` with a revival stamp
  in its evidence; the provider reconciler re-dispatches the SAME branch after
  bounded backoff (60s doubling, capped), at most
  `FORGE_RUN_AUTO_REVIVE_LIMIT` (default 2) times, journaled as `auto_revive`
  actions. No issue comment, no operator. `FORGE_RUN_REVIVE_BACKOFF_SECONDS`
  sets the ladder base; `0` disables auto-revive.
- **Tier 2 — `@forge /retry [run-id]`** on GitLab, GitHub and Azure DevOps
  (bare = the issue's latest `failed`/`blocked` run): approver-authorized like
  `/go`, it walks the run back to `proposing` through the explicit revival
  graph edge, grants ONE operator cycle (may exceed `FORGE_MAX_COMMIT_CYCLES`)
  and re-dispatches the same branch with the terminal reason and the last
  verification evidence as the repair context. Cancelled runs, and runs that
  never committed a candidate, are rejected with an actionable note pointing
  at `/implement`.
- **Fatal failures park `blocked`, not `failed`**: config errors (4xx input
  mismatches, missing workflow), driver quality signals and exhausted cycles
  carry a precise, actionable `status_reason` — nobody watches a run flap.
  ADR-0004 amended with the revival edge (`Controller.revive_transition`,
  audited via `authorized_by` in the outbox payload).

## [0.10.0] - 2026-09-18

### Added — Phase-A correctness alignment (external review e8bf381: all Phase-A P1s closed)

- **R01 — one publication boundary (ADR-0026)**: `publish_validated_candidate`
  owns validate → intent → native adapter; `ValidatedCandidate` is the capability
  to publish (raw ChangeSets refused by type). The GitHub builtin path now crosses
  strict materialization + policy validation before any commit-API call.
  28 negative conformance tests: 8 deny scenarios × 3 publish paths, zero
  commit-API calls asserted.
- **R02 — verification parity**: Azure publication parks at `waiting_ci` with a
  reconciler pass correlating Builds by candidate sha (lane excluded);
  red → repair/blocked, grace → honest unverified. GitLab empty profiles label
  ready as unverified. Unified `VerificationResult` evidence on all providers.
- **R03 — provider-namespaced identity**: migration 012 rebuilds the active-run
  unique index over (provider, project_id, issue_iid); every lifecycle scan and
  guard filters provider — a GitHub repo-id 5 no longer collides with a GitLab
  project 5.
- **R05 — brief binding**: the lane renders the brief from the EXACT journaled
  plan-comment id (dispatch input → addressed fetch → fail-closed on
  missing/wrong-author/wrong-run); the 100-comment heuristic is a loud legacy
  fallback.
- **R08/R09 — patch engine**: discriminated representations
  (Create/Delete/FullReplacement/UnifiedPatch) with `base_blob_digest` +
  `intended_digest` verification; POSIX zero-context placement, `\n`-only
  splitting (CRLF/U+2028 safe), typed rejections (mode/rename/binary/
  corrupt/stale); 31-case differential suite against `git apply` as the oracle.
- **R16 — artifact recipe**: clean non-hidden output dir, meta schema v2
  (attempt id, manifest digest, usage receipt), control-plane ZIP caps/
  allowlist/validation with one bounded retry.
- **R17 — liveness**: deadline/cancel evaluated BEFORE provider I/O on all
  three providers; bounded dispatch discovery; the reaper parks exhausted and
  deadline-exceeded steps as dead; late callbacks to terminal runs are
  superseded evidence, never READY.
- **R19 — MCP authorization**: default-deny wrapper on every tool, repo-target
  allowlist (`FORGE_MCP_TOKEN_REPOS`), denial auditing.
- **Lifecycle parity (#38/#29)**: `issue_edited` auto-replan and `unlabeled`
  gate-cancel on all three providers; the parity test whitelist is empty.
- **Operator tooling**: `/retry` (same-branch revival, operator-granted cycle),
  Tier-1 auto-revive of transient deaths, superseded-PR janitor.
- **Lane hardening**: `bypassPermissions` + mechanical deny (the allowlist
  whack-a-mole is retired), ephemeral `CLAUDE_CONFIG_DIR` (no cross-run memory
  bleed), `uv sync --frozen` from the target lock (the agent runs the gates CI
  runs), pinned toolchain, lane job ceiling 120m.
- **Research base**: docs/research/{patch-application, remote-effect-reconciliation,
  actions-artifacts-usage, schema-upgrade-gates}.md.

## [0.9.0] - 2026-09-16

### Added — task-aware harness selection (ADR-0023) + Azure DevOps adapter beta (ADR-0024)

- **Task-aware harness selection**: the implementer harness is no longer a
  single pin. `.forge.yml` / `FORGE_HARNESS_PREFERENCE` carry an ordered
  preference list; a pure compiler freezes `backend_config{harness,
  harness_fallbacks, budget_class, selection_reason}` into the immutable
  RunSpec (schema v2, bound by the policy digest — changing the chain
  invalidates pending gates). The plan comment gains an "Implementation"
  block, so `/go` authorizes the execution shape, not just the plan.
  Dispatch-time fallback is opt-in, off by default, infrastructure-only,
  journaled, and bounded to the frozen chain. All four GitLab templates
  gained driver-filter rules so multi-driver repos run exactly one lane;
  `forge doctor` reports the compilable chain. Research:
  docs/research/harness-selection.md.
- **Azure DevOps adapter (beta)** — the third provider, same iron
  contract: `/implement` on a work item or PR comment → plan as a
  work-item comment → `/go` → harness lane in Azure Pipelines
  (dispatch-only proposal-only lane, candidate artifact) → trusted
  publisher via the native CAS (`oldObjectId`) → Draft PR → branch-policy
  CI → reactive review via PR iterations (sticky marker thread, inline
  findings) → `ready_for_human`; build failures → durable debug lane.
  Fail-closed service-hooks ingress (Basic credentials, constant-time;
  no HMAC exists), connection-scoped approvers, identity-enforced
  no-merge. `forge doctor` AzDO checks; docs/azure-setup.md runbook with
  the live-verification checklist. Live verification pending a user PAT
  (tests: 190+ new across client/ingress/service/executor/lanes/joins).
  Research: docs/research/azure-devops.md (12 payload fixtures; three
  documented-API contradictions found and corrected before implementation).

### Fixed

- Pre-existing mypy errors in gateway/stores; stale uv.lock; three
  ruff-format violations in the MCP modules.

## [0.8.0] - 2026-09-15

### Added — MCP modernization + delivery metrics + fourth harness (v0.8, ADR-0021)

- **GitHub Copilot CLI as the fourth harness driver** (R6 research,
  `docs/research/harness-config-best-practices.md` §8): proposal-only
  `copilot` lane (`ci/templates/copilot.gitlab-ci.yml`) + Actions-lane
  driver in `forge.harness_entry` — headless `copilot -p`, scoped grants
  (`read,write` + `shell(git:*)`) with deny-wins `--deny-tool` on
  commit/push, env-token auth (fine-grained PAT with "Copilot Requests";
  classic `ghp_` unsupported), optional `COPILOT_MODEL` (RunSpec model
  routes do not map onto Copilot model names), usage stays unknown.
- **Scoped MCP run surface** (ADR-0021 §4 / R3 §8.1):
  `FORGE_MCP_SCOPED_TOKENS` defines per-token principals over a closed
  scope set (`forge:read`, `forge:runs:write`, `forge:approvals:write`,
  `forge:admin`); `run_list` / `run_get` / `plan_get` / `run_evidence_get`
  read durable state directly — the run surface never acts with forge's
  provider tokens (platform-token passthrough killed for reads).
  Per-call scope enforcement with model-actionable denials + an audit log
  (`forge.mcp_server.audit`). Malformed config or unknown scopes fail
  startup.
- **Delivery ladder metrics (F34)**:
  `forge_delivery_ladder{stage=started|planned|gate_approved|candidate_published|ci_passed|ready_for_human}`
  gauges in `/metrics.prometheus` and JSON `/metrics` — where work
  packages stand on the acceptance ladder (the merged rung lives
  provider-side; the bot never merges).

### Fixed

- **The mounted MCP endpoint was broken in production twice over**: the
  FastMCP session manager never ran under the FastAPI mount (every /mcp
  request failed with "Task group is not initialized"), and the SDK's
  DNS-rebinding Host check answered 421 to proxied requests.
  `FORGE_MCP_ALLOWED_HOSTS` lists the public host behind a proxy.
- Harness templates: repaired the YAML a NO_PROXY insert broke (dropped
  list dash), restored the "Do NOT commit and do NOT push" contract
  phrase; opencode config allows `external_directory`/`doom_loop`
  ("ask"-by-default headless hang sources).
- GitHub webhook payloads now captured under `FORGE_CAPTURE_DIR` (the
  GitLab route captured; the GitHub one never did) — live routing gaps
  are diagnosable from `data/captured/`.

### Changed — R5 harness config hardening (research top-5, all lanes)

- Mechanical commit/push deny in every driver (not just the brief):
  Claude `--disallowedTools`, Grok `--deny` (survives
  `--always-approve`), opencode permission-map denies, Copilot
  `--deny-tool` — the contract holds even if the model disobeys.
- Claude: `--permission-prompts none` (explicit no-prompt guarantee),
  `--max-turns 200`, vendor timeout budgets (`API_TIMEOUT_MS`,
  `BASH_*_TIMEOUT_MS`), retrying npm preamble with
  `FORGE_CLAUDE_VERSION` pin.
- Grok: `--trust` (project rules load headlessly) + `--max-turns 200`.
- `NO_PROXY` declared unconditionally in every lane.
- The Actions lane (`forge.harness_entry`) mirrors the full posture;
  contract tests (`TestMechanicalDeny`) now REQUIRE the deny constructs.

## [0.7.0] - 2026-09-14

### Added — reactive parity + complex workloads (v0.7, ADR-0021)

- **GitHub reactive review engine**: pull_request opened/synchronize on
  tracked repos → readonly LLM review posted as a native GitHub review
  (inline severity comments, REQUEST_CHANGES for critical); incremental
  via synchronize before/after SHAs; sticky progress comments never
  duplicate; forge-authored PRs skipped (recursion guard).
- **Actions failure debugging**: failed Actions jobs on PR heads →
  durable debug step → pipeline-debugger agent → sticky root-cause
  comment (fork-safe PR correlation; forge's own harness failures
  excluded — they have their own triage).
- **GitLab durable pipeline-debug**: failed pipelines on non-forge
  branches → root-cause MR notes (the repair loop keeps its own log
  ingestion).
- **Security findings ingestion + triage** (research:
  ci-security-surface.md): GitLab CE gl-sast/secret-detection artifacts +
  GitHub code/secret scanning + Dependabot alerts → forge-owned triage
  state keyed by forge-computed fingerprints (CE has no vulnerabilities
  API); `/security` command → bounded security-triage agent → grouped
  comment; remote dismissal opt-in
  (`FORGE_SECURITY_REMOTE_DISMISS`, default off).
- **Monorepo path-scoped packages**: `.forge.yml` implement.paths globs
  frozen into the RunSpec and enforced at validation and the publisher;
  the plan prompt carries the scope; unscoped projects byte-identical.

41 new tests; 1328 passed / 2 skipped.

## [0.6.0] - 2026-09-14

### Added — trust surface (v0.6, ADR-0021)

- **Budget reservation** (F22, ADR-0018 §5): run budgets opened at RunSpec
  freeze; every model call reserves before dispatch (refusal =
  `budget_exhausted`, provider never touched); harness usage receipts
  reconcile actuals; unknown completeness stays unknown — never zero.
  Migration 009.
- **Evidence policy** (F23): deny-pattern redaction + char caps on repair
  contexts and harness evidence; canary tests prove secrets cannot reach
  prompts or evidence comments.
- **Connection-scoped approvers**: FORGE_GITHUB_APPROVERS vs GitLab
  approvers — provider logins are separate identities (the @demo
  cross-provider leak, found live, closed systematically).

### Fixed — CI hardening (F29)

- Typecheck is blocking on six core packages (110 errors fixed, including
  a real PAT-mode crash: GitHubStaticCredentials field/method collision).
- New required `integration` CI job: the failure-injection exit-bar runs
  on real Postgres service containers.

## [0.9.0] - 2026-09-16

### Added — task-aware harness selection (ADR-0023) + Azure DevOps adapter beta (ADR-0024)

- **Task-aware harness selection**: the implementer harness is no longer a
  single pin. `.forge.yml` / `FORGE_HARNESS_PREFERENCE` carry an ordered
  preference list; a pure compiler freezes `backend_config{harness,
  harness_fallbacks, budget_class, selection_reason}` into the immutable
  RunSpec (schema v2, bound by the policy digest — changing the chain
  invalidates pending gates). The plan comment gains an "Implementation"
  block, so `/go` authorizes the execution shape, not just the plan.
  Dispatch-time fallback is opt-in, off by default, infrastructure-only,
  journaled, and bounded to the frozen chain. All four GitLab templates
  gained driver-filter rules so multi-driver repos run exactly one lane;
  `forge doctor` reports the compilable chain. Research:
  docs/research/harness-selection.md.
- **Azure DevOps adapter (beta)** — the third provider, same iron
  contract: `/implement` on a work item or PR comment → plan as a
  work-item comment → `/go` → harness lane in Azure Pipelines
  (dispatch-only proposal-only lane, candidate artifact) → trusted
  publisher via the native CAS (`oldObjectId`) → Draft PR → branch-policy
  CI → reactive review via PR iterations (sticky marker thread, inline
  findings) → `ready_for_human`; build failures → durable debug lane.
  Fail-closed service-hooks ingress (Basic credentials, constant-time;
  no HMAC exists), connection-scoped approvers, identity-enforced
  no-merge. `forge doctor` AzDO checks; docs/azure-setup.md runbook with
  the live-verification checklist. Live verification pending a user PAT
  (tests: 190+ new across client/ingress/service/executor/lanes/joins).
  Research: docs/research/azure-devops.md (12 payload fixtures; three
  documented-API contradictions found and corrected before implementation).

### Fixed

- Pre-existing mypy errors in gateway/stores; stale uv.lock; three
  ruff-format violations in the MCP modules.

## [0.8.0] - 2026-09-15

### Added — MCP modernization + delivery metrics + fourth harness (v0.8, ADR-0021)

- **GitHub Copilot CLI as the fourth harness driver** (R6 research,
  `docs/research/harness-config-best-practices.md` §8): proposal-only
  `copilot` lane (`ci/templates/copilot.gitlab-ci.yml`) + Actions-lane
  driver in `forge.harness_entry` — headless `copilot -p`, scoped grants
  (`read,write` + `shell(git:*)`) with deny-wins `--deny-tool` on
  commit/push, env-token auth (fine-grained PAT with "Copilot Requests";
  classic `ghp_` unsupported), optional `COPILOT_MODEL` (RunSpec model
  routes do not map onto Copilot model names), usage stays unknown.
- **Scoped MCP run surface** (ADR-0021 §4 / R3 §8.1):
  `FORGE_MCP_SCOPED_TOKENS` defines per-token principals over a closed
  scope set (`forge:read`, `forge:runs:write`, `forge:approvals:write`,
  `forge:admin`); `run_list` / `run_get` / `plan_get` / `run_evidence_get`
  read durable state directly — the run surface never acts with forge's
  provider tokens (platform-token passthrough killed for reads).
  Per-call scope enforcement with model-actionable denials + an audit log
  (`forge.mcp_server.audit`). Malformed config or unknown scopes fail
  startup.
- **Delivery ladder metrics (F34)**:
  `forge_delivery_ladder{stage=started|planned|gate_approved|candidate_published|ci_passed|ready_for_human}`
  gauges in `/metrics.prometheus` and JSON `/metrics` — where work
  packages stand on the acceptance ladder (the merged rung lives
  provider-side; the bot never merges).

### Fixed

- **The mounted MCP endpoint was broken in production twice over**: the
  FastMCP session manager never ran under the FastAPI mount (every /mcp
  request failed with "Task group is not initialized"), and the SDK's
  DNS-rebinding Host check answered 421 to proxied requests.
  `FORGE_MCP_ALLOWED_HOSTS` lists the public host behind a proxy.
- Harness templates: repaired the YAML a NO_PROXY insert broke (dropped
  list dash), restored the "Do NOT commit and do NOT push" contract
  phrase; opencode config allows `external_directory`/`doom_loop`
  ("ask"-by-default headless hang sources).
- GitHub webhook payloads now captured under `FORGE_CAPTURE_DIR` (the
  GitLab route captured; the GitHub one never did) — live routing gaps
  are diagnosable from `data/captured/`.

### Changed — R5 harness config hardening (research top-5, all lanes)

- Mechanical commit/push deny in every driver (not just the brief):
  Claude `--disallowedTools`, Grok `--deny` (survives
  `--always-approve`), opencode permission-map denies, Copilot
  `--deny-tool` — the contract holds even if the model disobeys.
- Claude: `--permission-prompts none` (explicit no-prompt guarantee),
  `--max-turns 200`, vendor timeout budgets (`API_TIMEOUT_MS`,
  `BASH_*_TIMEOUT_MS`), retrying npm preamble with
  `FORGE_CLAUDE_VERSION` pin.
- Grok: `--trust` (project rules load headlessly) + `--max-turns 200`.
- `NO_PROXY` declared unconditionally in every lane.
- The Actions lane (`forge.harness_entry`) mirrors the full posture;
  contract tests (`TestMechanicalDeny`) now REQUIRE the deny constructs.

## [0.7.0] - 2026-09-14

### Added — reactive parity + complex workloads (v0.7, ADR-0021)

- **GitHub reactive review engine**: pull_request opened/synchronize on
  tracked repos → readonly LLM review posted as a native GitHub review
  (inline severity comments, REQUEST_CHANGES for critical); incremental
  via synchronize before/after SHAs; sticky progress comments never
  duplicate; forge-authored PRs skipped (recursion guard).
- **Actions failure debugging**: failed Actions jobs on PR heads →
  durable debug step → pipeline-debugger agent → sticky root-cause
  comment (fork-safe PR correlation; forge's own harness failures
  excluded — they have their own triage).
- **GitLab durable pipeline-debug**: failed pipelines on non-forge
  branches → root-cause MR notes (the repair loop keeps its own log
  ingestion).
- **Security findings ingestion + triage** (research:
  ci-security-surface.md): GitLab CE gl-sast/secret-detection artifacts +
  GitHub code/secret scanning + Dependabot alerts → forge-owned triage
  state keyed by forge-computed fingerprints (CE has no vulnerabilities
  API); `/security` command → bounded security-triage agent → grouped
  comment; remote dismissal opt-in
  (`FORGE_SECURITY_REMOTE_DISMISS`, default off).
- **Monorepo path-scoped packages**: `.forge.yml` implement.paths globs
  frozen into the RunSpec and enforced at validation and the publisher;
  the plan prompt carries the scope; unscoped projects byte-identical.

41 new tests; 1328 passed / 2 skipped.

## [0.6.0] - 2026-09-14

### Added — trust surface (v0.6, ADR-0021)

- **Budget reservation** (F22, ADR-0018 §5): run budgets opened at RunSpec
  freeze; every model call reserves before dispatch (refusal =
  `budget_exhausted`, provider never touched); harness usage receipts
  reconcile actuals; unknown completeness stays unknown — never zero.
  Migration 009.
- **Evidence policy** (F23): deny-pattern redaction + char caps on repair
  contexts and harness evidence; canary tests prove secrets cannot reach
  prompts or evidence comments.
- **Connection-scoped approvers**: FORGE_GITHUB_APPROVERS vs GitLab
  approvers — provider logins are separate identities.

### Fixed — CI hardening (F29)

- Typecheck is blocking on six core packages (110 errors fixed, including
  a real PAT-mode crash: GitHubStaticCredentials field/method collision).
- New required `integration` CI job: the failure-injection exit-bar runs
  on real Postgres service containers.

## [0.5.0] - 2026-09-14

### Added — GitHub path at parity (ADR-0019/0020, F32 completion)

- **Plan + human gate on GitHub**: /implement → plan comment (digest +
  approve instruction) → immutable RunSpec + pending decision with TTL →
  `/go <run-id>` consumes it → publish → Draft PR → sha-bound readonly
  review → ready_for_human. `/cancel` = cancel-as-revoke. One active run
  per (repo, issue); uuid FlowRuns with provider identity (migration 008).
- **GitHub Actions execution adapter** ([ADR-0020](docs/adr/0020-github-actions-executor.md)):
  `/go` can dispatch the real coding-agent harness (Claude Code / Grok
  Build / opencode) into the target repo's Actions runner — proposal-only
  lane (no write credentials, push disabled), candidate artifacts
  (diff + meta + usage) downloaded and applied by the trusted publisher.
- **Shared quality-prompt builder**: one brief structure (role / task /
  constraints / quality bar / lane-specific output contract) rendered for
  all drivers; skills via AGENTS.md / CLAUDE.md conventions.
- **Label trigger**: `issues.labeled` with FORGE_TRIGGER_LABEL (default
  `forge`) starts runs like /implement.

### Fixed — found live during GitHub verification

- Wake-task identity mismatch double-executed run commands (two runs/PRs
  per comment); known-identity-without-step no longer falls back to
  direct execution.
- Candidate meta-key drift (attempt_base_oid vs attempt_base) rejected
  every Actions candidate.
- GitHub-subject runs are no longer polled by the GitLab CI reconciler.
- claude/opencode drivers install themselves in the Actions lane
  (retrying preambles); claude gateway env (AUTH_TOKEN/BASE_URL)
  passthrough.
- Approver-list hygiene: FORGE_APPROVERS are provider logins — never share
  the list across GitLab and GitHub identities.

### Docs

- README rewritten for the two-provider reality; docs/github-setup.md
  (App registration → harness → FAQ); docs/faq.md.

## [0.4.0] - 2026-09-13

### Added — GitHub App adapter, same-repository slice (ADR-0019, F32)

- **GitHub App identity**: RS256 JWT → installation tokens (cached, re-mint
  on 401), least-privilege permissions, private key held as a credential
  reference. Per-installation rate limits with `Retry-After` handling —
  no hot loops.
- **Fail-closed webhook ingress** (`/webhook/github`): `X-Hub-Signature-256`
  over raw bytes (constant-time; official test vector covered by tests),
  503 when disabled, ping, redelivery dedup via the durable inbox.
  `issue_comment` commands route into the same durable step path as GitLab;
  PR comments are distinguishable; installation-deleted disables the
  connection.
- **Publishing**: factory branch cut from the expected head,
  `createCommitOnBranch` with `expectedHeadOid` CAS (STALE_DATA → drift,
  never retried), Draft PR created find-by-head-first (never duplicated).
  Same trusted-publisher/validation semantics as GitLab; human gate is the
  documented next step (this slice is the adapter + flow foundation).
- 57 tests over a CAS-faithful fake, recorded webhook payloads, and JWT
  shape assertions.

### Added — operations read model and API correctness (F26–F28, F30)

- `GET /runs` + `GET /runs/{id}`: durable run read model (steps, evidence
  summary) with optional bearer auth; the legacy `/flows/{id}` route is
  removed. Prometheus exposition at `/metrics.prometheus` (F30).
- DB lifecycle (F26): engines keyed per database URL, async dispose on
  shutdown, `schema_version` compatibility gate (migration 007b) — an
  incompatible existing database refuses to start with upgrade pointers.
- GitLab API correctness (F27/F28): raw-diff endpoint with legacy
  fallback, head checks via single branch GET (no history pagination),
  pagination-cap warning so incomplete evidence is honest. Two raw-diff
  contract xfails closed.

## [0.3.0] - 2026-09-13

### Added — proposal-only harnesses and the trusted publisher (ADR-0016)

- **No write credentials in the execution lane**: harness jobs check out the
  frozen attempt base (detached), run the driver with push disabled by
  construction (`git remote set-url --push origin FORBIDDEN`), and upload
  their result as CI artifacts. `FORGE_BOT_READ_TOKEN` (read-only) replaces
  the write token in the lane; `forge doctor` fails when a write token is
  exposed to it (F04/F21).
- **CandidateBundle**: the runner produces a `git diff --binary` artifact
  plus meta (attempt base, driver, model, exit classification, usage); the
  backend parses it with a strict no-fuzz unified-diff applier against
  authoritative base blobs — binaries, renames and oversize files are
  rejected explicitly (F20).
- **Trusted publisher** (src/forge/runs/publisher.py): grant check (cancel/
  spec digest/fence) → policy validation → single journaled write with
  expected_head. Builtin proposals route through the same validation.
  Nonzero driver exits are never adopted; no-op repairs block as
  `repair_no_effect` (F20).
- **Usage receipts** (F22): harness usage lands in `llm_calls` with
  driver/model/completeness (exact | aggregate | unknown — never
  fabricated); migration 007.

### Changed

- All three harness templates (Claude Code, opencode, Grok) reworked to the
  candidate contract; live-verified end-to-end on the lab: red smoke on the
  seeded bug → bounded repair → review → ready_for_human, with usage
  receipts recorded per attempt.

## [0.2.0] - 2026-09-13

### Added — durable step runtime is now the execution path (ADR-0017)

- **Transactional ingress**: `/implement` and `/go` are answered 202 only
  after the webhook identity and the first scheduled step are committed in
  one Postgres transaction. Redis is a wake-up accelerator, not the
  authority; a crash between receive and execution can no longer lose a
  command (F08).
- **Atomic step ownership**: due steps are claimed with `FOR UPDATE SKIP
  LOCKED` + conditional-UPDATE ownership (portable across PG/SQLite),
  holding a per-step lease (owner, 120s expiry, monotonic fence token)
  renewed by heartbeat. Zombie workers lose the fenced CAS; completion by
  a stale owner is rejected (F09/F10).
- **Full-state recovery**: crash at any transition or after any external
  effect converges — journaled commits and Draft MRs are adopted on resume,
  stuck proposing legs are re-driven, missing READY evidence notes are
  re-posted once (F11). Proven by the new failure-injection suite: two
  real worker processes on live Postgres, hard-kill at six checkpoints,
  cancel-vs-publish race, and 10-way concurrent `/implement` — 8/8
  scenarios, exact effect counting (tests/test_failure_injection.py).
- **DB invariants**: one nonterminal run per (project, issue) via partial
  unique index; one gate per (run, generation) (F12).

### Added — approvals and policy (ADR-0018)

- **Immutable RunSpec** frozen at plan acceptance (subject, source base,
  plan/task digests, extended policy digest, backend config, budgets) — a
  settings change mid-run cannot silently alter an approved execution;
  `/go` validates the spec digest (F14).
- **Pending decision with deadline**: the gate is created at plan
  publication (`FORGE_DECISION_TTL_SECONDS`, default 7 days) and consumed
  at `/go`; expired or spec-drifted decisions are invalid (F15).
- **Admission before spend**: `/implement` from a non-approver is blocked
  before any LLM call; bot-in-approvers is a config contradiction (F16).
- **Cancel-as-revoke**: cancel withdraws scheduled steps, stands down an
  in-flight proposal, and marks late harness results superseded (F13).
- **Verification profile**: empty required-jobs is an explicit warning
  (never a silent pass); branch head re-checked after review — drift
  blocks `candidate_drift_after_review` (F19).

### Migration

- Alembic 005 (step runtime columns + invariants) and 006 (run_specs,
  gate digests, cancel_requested). Migrate before starting the new app/
  worker (see docs/operations/upgrade.md).

## [0.1.1] - 2026-09-13

### Fixed — Stage A safety hotfix (external review, docs/reviews/2026-09-13-v0.1.0/)

- **F01 (P0):** ChangeSet materialization no longer operates on truncated
  blobs — updates to large files cannot silently drop their tail;
  oversized files refuse instead of truncating.
- **F02:** repair cycles build on the last verified candidate
  (`attempt_base`), not the original approved base.
- **F03 (P0):** factory branches are cut from the frozen attempt base with
  an expected-head check (`BranchDriftError`) before committing.
- **F06 (P0):** pipeline / merge-request / note POSTs no longer auto-retry
  (non-idempotent: lost responses are reconciled, not repeated).
- **F07:** unknown commit outcomes resolve by operation marker + parent
  OID; a previous cycle's commit can never be misattributed.
- **F17:** the durable CI deadline fires even for a permanently running
  pipeline or a failing API.
- **F18:** empty / canceled-only CI evidence classifies as `unknown` and
  never triggers an LLM repair.
- **F24:** `ForgeConfig` deep-copies nested defaults (no cross-instance
  mutation).
- **F05 (P0):** the MCP server is fail-closed — not mounted without
  `FORGE_MCP_KEY`; explicit `FORGE_MCP_ENABLED` opt-out.

## [0.1.0] - 2026-09-13

First tagged milestone: the durable factory loop, live-accepted end-to-end
against a real GitLab CE, plus the reactive bot core inherited from
[Codeward](https://github.com/Relrin/codeward) (imported at `fd63ec8`, see
[UPSTREAM.md](UPSTREAM.md)). **Pre-production** — the API surface will move.

### Added — durable run loop (the factory)

- `/implement` → planner (LLM) → **human `/go` gate** (single-use, bound to
  plan digest + base SHA + policy, expiring) → implementer → atomic commit →
  **Draft MR before CI** → reconciler-watched pipeline → readonly LLM review
  → `ready_for_human` with evidence bound to the exact candidate SHA.
  The bot never merges ([ADR-0003](docs/adr/0003-no-merge-is-enforceable.md)).
- Durable execution core ([ADR-0004](docs/adr/0004-controller-owns-lifecycle-implementer-proposes.md),
  [ADR-0005](docs/adr/0005-durable-execution-and-unknown-outcome.md)):
  Postgres-backed lifecycle with a strict transition graph, journaled
  external writes (intent → outcome), unknown-outcome blocking, consume-once
  gates, idempotent webhook inbox, fencing leases, reconciler-polled
  worker-free waits (`waiting_ci`, `waiting_harness`).
- `@forge /cancel [run-id]` (approver-gated, short-id prefixes accepted) and
  a one-active-run-per-issue guard.
- ChangeSet model with exact-match replacement and deterministic
  materialization; Commits API writer with exact-SHA correlation
  ([ADR-0001](docs/adr/0001-commits-api-write-backend-changeset-contract.md)).
- Quality contract
  ([ADR-0008](docs/adr/0008-quality-contract-instead-of-pipeline-status.md)):
  pipeline success + every required job succeeded; failures classified
  code / infrastructure / config — only *code* failures trigger the bounded
  repair loop (`FORGE_MAX_COMMIT_CYCLES`); unknown causes never blame the code.
- Usage ledger (`llm_calls`) for every model call including failures.
- Harness backends
  ([ADR-0015](docs/adr/0015-pluggable-implementer-backends.md)):
  `builtin` (forge-side LLM ChangeSets) and `ci_harness` — Claude Code,
  opencode, and Grok Build CLI running **in the target project's CI**
  (ephemeral containers, credentials only as project CI variables), with
  independent branch-head verification of the claimed result,
  harness-backed repair, streaming job traces, and an optional
  `FORGE_HARNESS_HTTPS_PROXY` for throttled runner networks.

### Added — reactive bot core (from Codeward)

- Code review (inline comments, severity, incremental reviews with thread
  resolution), pipeline debugging, security triage, `@mention` chat, MCP
  server + client, model routing through a LiteLLM proxy
  ([ADR-0014](docs/adr/0014-llm-http-client-over-agno.md): thin HTTP client
  for the factory agents; Agno stays reactive-only).

### Added — operations

- `forge doctor` — read-only environment verification (tokens, Redis,
  database incl. schema presence, LiteLLM, and per-project onboarding:
  webhook, CI variable names, active runner). Human and `--json` output;
  exit code 0 = ready. Never prints secret values.
- AI-ready onboarding: [AGENTS.md](AGENTS.md),
  [onboarding prompt](docs/reference/onboarding-prompt.md), agent skills
  (`.claude/skills/`), and operational runbooks under
  [docs/operations/](docs/operations/).
- Lab-validated failure drills: provider outage, worker crash mid-run,
  harness-job cancellation, duplicate commands, `/go` burst.

### Notes

- 815+ tests (unit/contract), CI on Python 3.13 + 3.14, ruff clean.
- Not yet: budget enforcement beyond the commit-cycle cap, redaction at
  every agent boundary, drift policies beyond block, production hardening.
  See the [README](README.md#status) for the honest gap list.
