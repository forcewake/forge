# Coverage audit — forge v0.5.0 vs "everything a factory needs"

Coordinator's pre-research gap map from codebase knowledge (2026-09-14).
To be refined/overridden by docs/research/* findings, then turned into
ADR-0021 + milestones. Status tags: GAP (missing), PARTIAL, OK.

## A. Provider surface (GitLab CE / GitHub)

| Area | GitLab CE | GitHub |
|---|---|---|
| Durable run loop (plan/gate/agent/publish/review) | OK | OK |
| Harness execution in CI | OK (project CI) | OK (Actions) |
| Inline review comments (durable path) | PARTIAL (summary comment; reactive core has inline, durable path doesn't use it) | GAP (summary comment only; no PR review object) |
| Reactive: code review / chat | OK (legacy flows, GitLab-only) | GAP |
| Reactive: pipeline debugging | PARTIAL (GitLab legacy flow) | GAP (Actions failures not ingested) |
| Reactive: security triage | PARTIAL (agent exists; no findings source wired) | GAP |
| Test-report ingestion (JUnit) | GAP | GAP |
| Security findings ingestion (SAST/code scanning/Dependabot/secret detection) | GAP (CE limits to document) | GAP |
| Merge trains / merge queue awareness | GAP | GAP |
| Child pipelines / reusable-workflow correlation | GAP | n/a |
| Assignment/agent-directory UX | n/a | Partner-program only (documented) |

## B. Complex workloads

| Area | Status |
|---|---|
| Monorepo path scoping (issue → allowed paths in RunSpec) | GAP |
| Nested instruction files (AGENTS.md per dir) | PARTIAL (CLIs resolve natively; forge's evidence reader unaware) |
| Affected-area/test selection | GAP (whole-suite verification only) |
| Work-package decomposition (big issue → package DAG) | GAP (one plan = one package) |
| Parallel agents on one repo (conflict graph) | GAP (one active run per issue, sequential) |
| Multi-repo work packages | GAP |
| Migration/modernization campaigns (inventory → waves → equivalence evidence) | GAP (the original codeward sizing was for this; forge has no campaign model) |
| Dependency-upgrade profile (allowlist + canary + breaking-change triage) | GAP (planned backlog) |
| Evidence budgets: repo maps / symbol retrieval | PARTIAL (char budgets, tree list, no map) |

## C. MCP

| Area | Status |
|---|---|
| MCP server mounted | OK but fail-closed; legacy tools only (reactive), global GitLab token |
| Durable-run MCP tools (status/approve/cancel/evidence) | GAP |
| Per-tool identity/repo scope | GAP |
| Streamable HTTP transport (current spec) | PARTIAL (transport from import era) |
| MCP client usage inside durable runs | GAP (legacy agno only) |
| ACP | Not started (deferred by ADR-0019) |

## D. Platform/ops

| Area | Status |
|---|---|
| Budget reservation (pre-dispatch, provider receipts) | GAP (F22) |
| Blocking typecheck in CI | GAP (F29) |
| Packaging split / extras / pinned runner images | GAP (F33) |
| Delivery metrics (accepted-work-package lineage) | GAP (F34) |
| Connection-scoped approver lists | GAP (live-found @demo leak) |
| Secrets redaction at agent boundaries | PARTIAL (F23) |
| Prometheus + /runs read model | OK (v0.4) |
| Auth on management API | PARTIAL (read token only) |

## Method

Research docs (docs/research/*) refine this map → ADR-0021 (roadmap
consolidation) → milestones v0.6 (parity + pipelines + security),
v0.7 (complex workloads), v0.8 (MCP + platform) — sequencing per
impact/effort and the review's dependency graph.
