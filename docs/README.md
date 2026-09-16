# forge documentation

**forge** is an agentic software factory for GitLab CE, GitHub, and Azure
DevOps: an authorized issue becomes a plan, a human gate, a coding agent in
ephemeral CI, a trusted publisher, and a Draft MR/PR ready for human
review. The bot never merges.

Start at the [main README](../README.md); this index maps the docs tree.

## Getting started

| Guide | Covers |
|---|---|
| [GitLab CE project onboarding](operations/onboarding.md) | execution profile, bot identity, webhook, CI variables, harness include, `forge doctor` |
| [GitHub setup](github-setup.md) | GitHub App registration, webhook, secrets, harness workflow, label trigger |
| [Azure DevOps setup](azure-setup.md) | service account + PAT scopes, service hooks, lane pipeline, branch policy, live-verification checklist |

## Harnesses (coding agents in your CI)

| Doc | Covers |
|---|---|
| [Harness onboarding](harness-onboarding.md) | shared setup: includes, common variables, MCP servers, how a run flows, writing the task, triage |
| [Multi-harness & auto-selection](harnesses/README.md) | the preference list, the compiler, the gate's Implementation block, fallback |
| [Claude Code](harnesses/claude-code.md) | variables, flags, MCP dialect, receipts, gotchas |
| [Grok Build](harnesses/grok-build.md) | subscription auth rotation, the npm hang fix, deny rules |
| [opencode](harnesses/opencode.md) | injected permission map, model routing, MCP translation |
| [GitHub Copilot CLI](harnesses/copilot-cli.md) | fine-grained PAT, scoped grants, deny-wins rules |

## Operations

| Doc | Covers |
|---|---|
| [Operations index](operations/README.md) | runbooks overview |
| [Backup & restore](operations/backup-restore.md) | Postgres durability, restore drills |
| [Upgrade](operations/upgrade.md) | migration ordering, rollback |
| [Token rotation](operations/token-rotation.md) | every credential forge and the lanes touch |
| [Audit retention](operations/audit-retention.md) | evidence, action log, captures |

## Reference

| Tree | Covers |
|---|---|
| [ADR](adr/) | architecture decisions 0000–0024 (the "why") |
| [Research](research/) | live-verified API studies: [GitHub API](research/github-api.md), [Actions executor](research/github-actions-executor.md), [Azure DevOps payloads](research/azure-devops.md), [MCP ecosystem](research/mcp-surface.md), [harness interfaces](research/harness-interfaces.md), [harness config best practices](research/harness-config-best-practices.md), [harness selection prior art](research/harness-selection.md), [reactive reviews](research/github-reactive.md), [durable execution](research/durable-execution.md), [CI security surface](research/ci-security-surface.md), [complex projects](research/complex-projects.md), [MCP lane live evidence](research/mcp-lane-live-evidence.md) |
| [Specs](specs/) | stage briefs and contracts the milestones were built from |
| [Security](security/threat-model.md) | the threat model |
| [Reviews](reviews/) | external review passes and their dispositions |
| [FAQ](faq.md) | short answers, all providers |
| [Onboarding prompt](onboarding-prompt.md) | copy-paste bootstrap for coding agents working ON forge |

## Design pillars (one line each, full story in the ADRs)

- The bot never merges ([ADR-0003](adr/0003-no-merge-is-enforceable.md)).
- The controller owns the lifecycle; implementers propose
  ([ADR-0004](adr/0004-controller-owns-lifecycle-implementer-proposes.md)).
- Durable execution with unknown-outcome reconciliation
  ([ADR-0005](adr/0005-durable-execution-and-unknown-outcome.md),
  [ADR-0017](adr/0017-durable-step-runtime.md)).
- Quality contract instead of pipeline status
  ([ADR-0008](adr/0008-quality-contract-instead-of-pipeline-status.md)).
- Human gates authorize one specific decision
  ([ADR-0009](adr/0009-human-gates-authorize-specific-decision.md)).
- Proposal-only lanes + one trusted publisher
  ([ADR-0016](adr/0016-candidate-bundle-trusted-publisher.md)).
- Immutable RunSpec, admission before spend
  ([ADR-0018](adr/0018-immutable-run-spec.md)).
- Source/execution/harness/model-route adapters
  ([ADR-0019](adr/0019-source-execution-adapters.md)).
- MCP into lanes ([ADR-0022](adr/0022-harness-mcp-integration.md)) and
  task-aware harness selection
  ([ADR-0023](adr/0023-dynamic-harness-selection.md)).
