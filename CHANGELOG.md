# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
