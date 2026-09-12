# 0000 — Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-09-12

## Context

forge is an agentic software factory sidecar for GitLab CE: an authorized
issue should become a branch with code, a green pipeline, and a Draft merge
request ready for human review — and the bot must never merge.

Building this requires making consequential, hard-to-reverse decisions about
how the LLM communicates changes, where code executes, how the lifecycle is
owned, how side effects are made durable, and how security boundaries are
enforced. These decisions cut across modules, outlive individual pull
requests, and are easy to contradict accidentally in day-to-day changes.
New contributors (and future maintainers) need to see not only *what* the
architecture is, but *why* each choice was made and what trade-offs were
accepted.

## Decision

We record architecture decisions as Architecture Decision Records (ADRs)
using Michael Nygard's format, in this directory (`docs/adr/`):

- File names are numbered sequentially: `NNNN-title-with-dashes.md`.
- Each ADR contains the sections **Status**, **Date**, **Context**,
  **Decision**, and **Consequences**.
- Statuses are `Proposed`, `Accepted`, or `Superseded by ADR-XXXX`.
- Once accepted, an ADR is not edited to change its decision. If a decision
  changes, a new ADR is written and the old one is marked `Superseded by
  ADR-XXXX`.
- Context should describe the forces at play (technical, operational,
  security) in a self-contained way; consequences should state both the
  benefits and the accepted costs.

## Consequences

- Every significant architectural decision gets a durable, reviewable
  rationale; code review can refer to ADRs instead of re-litigating basics.
- ADRs are immutable records: changing course means writing a new ADR, which
  makes drift visible and deliberate.
- The ADR set becomes the primary onboarding material for the factory
  controller design, ahead of the implementation that realizes it.
- Writing ADRs is a small ongoing cost on every significant change.

## Index

- [0001 — Commits API as write backend, ChangeSet as LLM output contract](0001-commits-api-write-backend-changeset-contract.md)
- [0002 — GitLab CI is the execution environment, but security comes from explicit execution profiles](0002-ci-execution-environment-explicit-execution-profiles.md)
- [0003 — No-merge is enforceable, not prompt-only](0003-no-merge-is-enforceable.md)
- [0004 — The controller owns the lifecycle; the implementer agent only proposes changes](0004-controller-owns-lifecycle-implementer-proposes.md)
- [0005 — Durable execution and unknown_outcome](0005-durable-execution-and-unknown-outcome.md)
- [0006 — Snapshot isolation and race protection](0006-snapshot-isolation-and-race-protection.md)
- [0007 — Draft MR created before waiting on required CI](0007-draft-mr-before-required-ci.md)
- [0008 — Quality contract instead of pipeline.status](0008-quality-contract-instead-of-pipeline-status.md)
- [0009 — Human gates authorize a specific decision](0009-human-gates-authorize-specific-decision.md)
- [0010 — CE-compatible labels; state is not stored in labels](0010-ce-compatible-labels.md)
- [0011 — Config inherits defaults but never delegates security downward](0011-config-never-delegates-security-downward.md)
- [0012 — Context and redaction checked at every boundary](0012-context-and-redaction-at-every-boundary.md)
- [0013 — Budgets and the usage ledger are part of the core](0013-budgets-and-usage-ledger-in-core.md)
