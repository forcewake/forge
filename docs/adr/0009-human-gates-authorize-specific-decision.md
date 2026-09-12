# 0009 — Human gates authorize a specific decision

- **Status:** Accepted
- **Date:** 2026-09-12

## Context

The factory pauses for human approval before implementing an approved plan.
If "approval" is a loose comment, a label, or any state that can be replayed,
then an old approval can unlock new work it was never given for: the plan
changed, the target moved, the policy changed, or the approval came from
someone who should not have authority. An approval must authorize one
decision, not a bot.

Two further facts constrain the design:

- GitLab's issue author is not automatically a trusted approver; authority
  must come from configuration, not from who happened to file the issue.
- Cancellation is not a time machine. When `/cancel` arrives, HTTP requests
  already sent to GitLab may still complete; assuming they were cancelled
  would desynchronize forge's state from reality.

## Decision

A human gate authorizes **a specific decision**, recorded as a durable record
with single consumption:

- The gate binds: the run ID, the plan digest, the base SHA, the effective
  policy/config digest, the approving user ID, the source note/event ID, an
  expiry time, and a one-shot `expires_at`/`consumed_at` lifecycle.
- A command such as `@forge /go <run-id> <plan-digest>` consumes the gate.
  Consumption is single: a gate cannot approve a second run or a second plan.
- **The approver list comes from trusted configuration.** The issue author is
  not an approver by default. On every webhook, the project scope, the
  actor's identity, and the currency of the gate are re-verified.
- **Changed plan or sensitive policy invalidates the approval.** A stale
  approval label does not unlock new work; approval is not a standing
  permission.
- **`/cancel` stops new side effects, not sent ones.** Already-dispatched HTTP
  is reconciled after the fact: the actual outcome is recorded, CI is stopped
  where possible, and any resulting branches/MRs are explained. forge does
  not silently delete branches or MRs as if the work had never happened.

## Consequences

- **Positive:** every transition past a human gate is attributable to a
  specific user, for a specific plan, at a specific base state, within a
  validity window; replayed or forwarded approvals are rejected; cancellation
  leaves an honest, reconciled trail instead of a fiction.
- **Negative:** approvals expire and must be re-given when plans change —
  deliberate friction; operators must maintain the approver configuration;
  cancel reconciliation can leave residual GitLab artifacts that a human must
  close, which is the intended alternative to destructive auto-cleanup.
- The config digest bound into the gate is defined by
  [ADR-0011](0011-config-never-delegates-security-downward.md); the gate is a
  state in the lifecycle owned by the controller
  ([ADR-0004](0004-controller-owns-lifecycle-implementer-proposes.md)).
