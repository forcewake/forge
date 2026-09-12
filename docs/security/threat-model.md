# Threat model

This is the generic threat model for forge. It describes what forge trusts,
where the boundaries are, which risks are accepted, and which architectural
decisions mitigate which risks. It applies to any installation; per-project
details (execution profiles, approver lists, path rules) are set during
onboarding.

forge is an agentic sidecar: it accepts commands from GitLab, produces code
with an LLM, writes branches and Draft merge requests, and runs pipelines.
It never merges and never holds merge authority
([ADR-0003](../adr/0003-no-merge-is-enforceable.md)).

## Trust boundaries

```text
                       ┌──────────────────────────────────────────┐
 GitLab instance       │  forge control plane                     │
 ───────────────       │  ─────────────────────                   │
  webhooks ────────────► B1 webhook ingress (auth, project scope) │
  users/actors ────────► B5 gate authorization                    │
                       │                                          │
  .forge.yml in repo ──► B4 configuration sources                 │
  group/instance cfg ──► (admin-controlled, trusted)              │
                       │                                          │
  LLM provider ◄───────┤ B2 model boundary (outbound context)     │
  (untrusted output)───►                                            │
                       │                                          │
  CI runners ◄─────────┤ B3 CI execution (LLM-written code runs)  │
  (untrusted jobs) ────►                                           │
                       │   Postgres = source of truth; labels,    │
                       │   webhooks, CI status = signals          │
                       └──────────────────────────────────────────┘
```

- **B1 — Webhook ingress.** Every event from GitLab is untrusted until its
  secret is validated (timing-safe comparison), the project scope is
  resolved, and the actor is checked. Events are deduplicated and journaled;
  labels and comment text are input signals, never authorization.
- **B2 — Model boundary.** All outbound context (files, logs, issue text,
  tool results) crosses one policy/redaction/budget layer; all inbound model
  output is untrusted data validated by a typed contract.
- **B3 — CI execution.** Pipelines execute repository content, including
  content an LLM wrote. GitLab CI is effectively a remote-code-execution
  surface; its security is defined by the project's execution profile, not by
  forge's prompts.
- **B4 — Configuration sources.** Instance and group policy are
  admin-controlled and trusted. Project `.forge.yml` is repository content —
  effectively attacker-adjacent — and may only tighten.
- **B5 — Management and gate authorization.** The management API and human
  gates verify scope, actor identity, and currency on every call; a prior
  approval or label is not a standing permission.

## Key risks and mitigations

| # | Risk | Mitigation | ADR |
|---|------|------------|-----|
| R1 | **Prompt injection** via issue text, CI logs, or tool results steering the agent into harmful actions | Repo text is data, never authorization; uniform policy layer on every source; reads bound to pinned SHAs; injection cannot reach capabilities that do not exist | [0002](../adr/0002-ci-execution-environment-explicit-execution-profiles.md), [0003](../adr/0003-no-merge-is-enforceable.md), [0012](../adr/0012-context-and-redaction-at-every-boundary.md) |
| R2 | **CI as remote code execution** — an LLM-written or attacker-written change runs arbitrary code in CI | Onboarding execution profiles: no production secrets/control-plane credentials/Docker socket/privileged runners in agent jobs; no onboarding without an approved profile | [0002](../adr/0002-ci-execution-environment-explicit-execution-profiles.md) |
| R3 | **Secret egress** to LLM providers via context, logs, or agent-written comments | Redaction on every outbound boundary; files with detected secrets excluded from full-file editing so placeholders cannot be committed; all model routes are operator-configured | [0012](../adr/0012-context-and-redaction-at-every-boundary.md) |
| R4 | **Lost or duplicated side effects** (commit/MR created twice, or assumed cancelled) | Durable outbox, action journal, `unknown_outcome` reconciliation before retry, honest no-exactly-once guarantee | [0005](../adr/0005-durable-execution-and-unknown-outcome.md) |
| R5 | **Races on branches** — overwriting human commits, interleaved bot writers | Branch per run, expected-head checks before write, single writer per bot branch, no force pushes, `blocked_external_change` on conflict | [0006](../adr/0006-snapshot-isolation-and-race-protection.md) |
| R6 | **Unauthorized or replayed approvals** — old/forwarded approval unlocks new work, wrong actor approves | Gates bind run ID, plan digest, base SHA, config digest, approver, and expiry; single consumption; approvers from trusted config only; issue author is not auto-approver | [0009](../adr/0009-human-gates-authorize-specific-decision.md) |
| R7 | **Pipeline greenwash** — `allow_failure`, skipped jobs, stale-SHA success read as verification | Quality contract: required jobs + evidence artifacts per exact candidate SHA; any new commit invalidates the verdict | [0008](../adr/0008-quality-contract-instead-of-pipeline-status.md) |
| R8 | **Privilege escalation via project config** — repository content widens its own authority | Strict downward narrowing: project `.forge.yml` may only tighten; unknown keys and template errors fail loudly; per-run config digest pinning | [0011](../adr/0011-config-never-delegates-security-downward.md) |
| R9 | **Cost and workload runaway** — repair loops, duplicate pipelines, unbounded concurrency | Per-call usage ledger including failures; reserve-then-reconcile budgets; separate code-repair vs infrastructure-retry budgets; admission control considers CI/review backlog | [0013](../adr/0013-budgets-and-usage-ledger-in-core.md), [0004](../adr/0004-controller-owns-lifecycle-implementer-proposes.md) |
| R10 | **State corruption via CE label semantics** — concurrent/mutually exclusive labels assumed | Labels are projection + signals only; Postgres is the source of truth; plain CE-compatible label names | [0010](../adr/0010-ce-compatible-labels.md) |

## Explicitly out of scope

These are not mitigated by forge and require environment-level controls:

- **A compromised CI runner or execution profile.** If the execution
  environment is hostile, no forge control helps; onboarding refuses such
  projects ([ADR-0002](../adr/0002-ci-execution-environment-explicit-execution-profiles.md)).
- **A compromised control-plane host or Postgres.** forge's durable state is
  only as trustworthy as the host and database it runs on.
- **A malicious GitLab administrator** or GitLab instance compromise.
- **Prompt injection that turns into harmful CI code** is narrowed by R1–R3
  but not eliminated: the model can still propose code that CI executes.
  Containment (profiles, no secrets in jobs) is the mitigation, not
  prevention.
- **A locally hosted model** reduces data egress but does not remove R1 or
  the R2 execution risk ([ADR-0012](../adr/0012-context-and-redaction-at-every-boundary.md)).

## Review cadence

This model is revisited whenever a new ADR changes a trust boundary, a new
input channel is added (e.g. MCP tool sources), or the execution profile
model changes.
