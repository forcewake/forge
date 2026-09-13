# ADR-0018: Immutable RunSpec and admission

Status: accepted (2026-09-13)
Context: review findings F13–F16, F20, F22; approval and execution policy
in v0.1 are reconstructed from mutable settings.

## Decision

1. **RunSpec** is an immutable, versioned document created at plan
   acceptance and frozen by the gate: subject (connection, provider,
   repository, issue), source snapshot OIDs, plan/task digests, policy and
   verification-profile digests, execution profile (executor, image,
   template digests), model route + credential **references** (never secret
   values), budgets (wallclock, calls, tokens, commit cycles), approval
   generation, expiry. Changing a setting mid-run never changes an approved
   RunSpec — the run executes its spec or requests re-approval.
2. **Pending decision with a deadline.** The gate decision is created when
   the plan is published (not at `/go`): it carries the plan/task/spec
   digests and an absolute deadline. `/go` validates and consumes the
   decision; an expired decision blocks the run with a reason.
3. **Admission before spend.** `/implement` passes an admission check
   (project onboarded and profile approved, actor authority, quotas, bot≠
   human-approver identity) before the first paid LLM call. `doctor`
   reports findings; the runtime enforces them.
4. **Cancel = revoke publication grant first.** A durable cancel request
   invalidates the run's publication grant, then best-effort cancels the
   external execution; late artifacts are `superseded` and can never become
   commits or READY even if the executor could not be stopped.
5. **Usage receipts.** Harness drivers return usage evidence with an
   explicit completeness flag (exact / aggregate / unknown); cached tokens
   are never double-counted against inclusive input; failed and superseded
   attempts stay in the ledger. Budget reservation happens before dispatch.

## Consequences

- Evidence retention: RunSpec stores digests and references; captures with
  raw content remain under the [retention policy](../operations/audit-retention.md).
- `_policy_digest` grows from the approvers list to the effective execution
  policy; a drift between approval and execution is detectable, not silent.
