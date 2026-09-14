# ADR-0021: Roadmap consolidation v0.6–v0.8 — trust surface, complex workloads, MCP

Status: accepted (2026-09-14)
Context: four research passes (docs/research/: ci-security-surface,
complex-projects, mcp-surface, github-reactive) against the coverage audit
(docs/specs/coverage-audit.md). The durable core and both providers are
done; what remains is (a) the trust/accounting surface, (b) the reactive
core on GitHub, (c) complex-workload modeling, (d) MCP modernization.

## Decisions

1. **Trust surface first (v0.6)** — budget reservation with provider
   receipts (F22: reserve → dispatch → reconcile; unknown ≠ zero),
   one evidence policy at every model/log boundary with canary-secret
   tests (F23), blocking typecheck on contracts/core + a Postgres
   integration CI stage (F29), **connection-scoped approver lists**
   (FORGE_GITHUB_APPROVERS vs GitLab approvers — the @demo cross-provider
   leak), and the GitHub reactive review engine (inline review comments,
   incremental via synchronize before/after SHAs, sticky progress comment
   with hidden markers, bot-sender recursion guard — per
   research/github-reactive.md).
2. **Pipelines & findings (v0.6)** — Actions failure debugging
   (`workflow_job` webhook, per-step conclusions, job-log fetch, check-run
   annotations), GitHub code-scanning alert ingestion → security-triage
   flow (dismiss/comment/link-fix), GitLab CE security artifact parsing
   (`gl-*-report.json` job artifacts) with forge-owned triage state
   (self-computed fingerprints — CE has no vulnerabilities API), and
   JUnit test-report ingestion into run evidence.
3. **Complex workloads (v0.7)** — path-scoped work packages (RunSpec
   `allowed_paths`, publisher-enforced, CODEOWNERS-aware), affected-area
   verification adapters (Nx/Bazel/make contract: (base,head) → test
   targets), stack semantics for parallel agents (conflict graph over path
   leases; best-of-N verification instead of swarms), migration campaigns
   (inventory → waves with keep/fix/exclude ledger + source oracle),
   dependency-upgrade profile, acceptance telemetry
   (candidate → CI-passed → ready → merged-without-rework ladder).
4. **MCP modernization (v0.8)** — server transport/authz to the
   2026-07-28 spec (stateless, `Mcp-Protocol-Version` header, no
   token-passthrough — per-tool authz with audience-bound tokens), a
   durable-run tool surface (run_start/get/list/cancel, plan_get,
   run_evidence_get; gate_approve/reject as human-identity-bound tools
   with step-up scope), run events/evidence as resources; the MCP client
   modernized on the current Python SDK for external tool servers. ACP
   stays out (editor-session protocol, not factory control).
5. **Packaging/delivery (v0.8)** — uv workspace split along the
   proven module boundaries, pinned harness runner images, GHCR release
   pipeline (clean-image build → smoke → publish same digest), delivery
   metrics (work-package lineage ladder), blocking typecheck everywhere.

## Sequencing rationale

Trust surface before complex workloads: every new workload multiplies
whatever accounting/redaction guarantees exist. Reactive parity before
campaigns: campaigns need the review/debug surface to be credible.
MCP last for surface, but its security bar (fail-closed authz) is already
enforced. GHCR publication remains blocked on the token scope — a user
action, tracked separately.

## Rejected

Multi-agent swarms, repo-wide default CI for agent branches, MCP as the
factory's internal control bus, auto-install of plugins from project
config, auto-merge — per the review and research docs.
