# 0011 — Config inherits defaults but never delegates security downward

- **Status:** Accepted
- **Date:** 2026-09-12

## Context

forge is configured at three levels: instance defaults, group-level policy,
and per-project preferences. Two structural facts define the threat model:

- A GitLab group has no implicit working directory — there is no built-in
  `.forge.yml` at group level. Group policy therefore needs an explicit,
  admin-controlled storage location.
- Project-level configuration (`.forge.yml`) lives **inside the repository**
  the agent works on. It is readable — and modifiable — by anyone who can
  push a commit, and its content flows into prompts. A project config file
  that could grant authority would let repository content escalate its own
  privileges: add an approver, enable a dangerous tool, or bypass path
  restrictions with one commit.

## Decision

Configuration forms a strictly downward-narrowing hierarchy:

1. **Instance policy** — the operator-controlled defaults and hard limits.
2. **Group policy** — maintained by administrators, stored as an explicit
   mapping of group ID → config path/ref inside an admin-controlled
   configuration repository. It is not read from a group "working directory",
   because none exists.
3. **Project preferences** — `.forge.yml`, read from the **trusted target
   snapshot** of the project (a pinned SHA, not a live mutable ref), so the
   config a run used is the config that can be audited.

The tightening rule: project configuration may only **reduce** limits and
**select** from options the upper layers allow (e.g. pick an approved
profile). It may never **widen** authority: no new GitLab endpoints, no new
approvers, no additional MCP tools, no arbitrary model endpoints, no new
token scopes, and no bypass of path rules.

Per-run pinning: every run records the configuration schema version, the
digest of the effective configuration, and the flow/prompt/model-route
versions. A new version of the YAML does not retroactively change an already
approved run.

Fail loudly: unknown configuration keys, missing template variables, and
condition evaluation errors are hard errors — never silently skipped or
treated as pass.

## Consequences

- **Positive:** repository content cannot escalate its own privileges by
  editing `.forge.yml`; the effective configuration of any historical run can
  be reconstructed from its digest; operator defaults cannot be eroded
  project by project.
- **Negative:** legitimate project needs (a new integration, a different
  profile) require an admin-level change to group/instance policy; group
  policy adds an admin-maintained config repository; strict parsing means
  typos block runs loudly — that is the point.
- The effective-config digest is one of the bindings of a human gate
  ([ADR-0009](0009-human-gates-authorize-specific-decision.md)); the tool and
  capability surface that configuration cannot expand is bounded by
  [ADR-0002](0002-ci-execution-environment-explicit-execution-profiles.md)
  and [ADR-0003](0003-no-merge-is-enforceable.md).
