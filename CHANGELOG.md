# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
  ([ADR-0001](docs/adr/0001-changesets-exact-replacements.md)).
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
  [onboarding prompt](docs/onboarding-prompt.md), agent skills
  (`.claude/skills/`), and operational runbooks under
  [docs/operations/](docs/operations/).
- Lab-validated failure drills: provider outage, worker crash mid-run,
  harness-job cancellation, duplicate commands, `/go` burst.

### Notes

- 815+ tests (unit/contract), CI on Python 3.13 + 3.14, ruff clean.
- Not yet: budget enforcement beyond the commit-cycle cap, redaction at
  every agent boundary, drift policies beyond block, production hardening.
  See the [README](README.md#status) for the honest gap list.
