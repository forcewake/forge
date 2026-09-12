# 0010 — CE-compatible labels; state is not stored in labels

- **Status:** Accepted
- **Date:** 2026-09-12

## Context

forge uses issue labels both as input signals (a trigger label starts work)
and as a human-readable projection of run state. GitLab's scoped labels —
names containing `::` with mutually exclusive semantics within a scope — are
a Premium/Ultimate feature. On GitLab CE, `factory:state::running` is just a
string: nothing enforces that only one `factory:state::…` label is applied at
a time. A design that relies on scoped-label semantics silently breaks on CE,
and the presence of `::` in a name must not be mistaken for an enforced state
machine.

Labels are also a poor database: they can be edited by anyone with label
permissions, they have no transactional history, and webhook delivery of
label events is not guaranteed.

## Decision

forge uses plain, CE-compatible label names and keeps state out of labels:

- Plain names namespaced by colons, for example: `factory:trigger:ready`,
  `factory:state:running`, `factory:gate:approved`,
  `factory:state:blocked`.
- Trigger, state, and approval are **not** merged into a single scope; they
  are separate namespaces precisely because CE will not enforce exclusivity
  for forge.
- **Postgres is the source of truth.** Labels are a projection forge
  synchronizes for humans, and one of several input signals. A label change
  is a suggestion that goes through the same webhook authentication,
  project-scope, and gate checks as any other event; a missing or
  contradictory label never changes durable state by itself.
- Forge actively removes/reconciles its own stale labels so the projection
  does not drift into lying.

## Consequences

- **Positive:** behavior is identical on GitLab CE, Premium, and Ultimate;
  label edits by other users cannot corrupt run state; the state machine
  remains transactional and auditable in Postgres.
- **Negative:** the projection can drift and needs reconciliation; users
  familiar with Premium scoped labels get no exclusivity guarantee from the
  naming convention alone; label sync adds API calls.
- Input signals from labels are subject to the same authorization as all
  commands ([ADR-0009](0009-human-gates-authorize-specific-decision.md)); the
  durable state machine they project is defined in
  [ADR-0004](0004-controller-owns-lifecycle-implementer-proposes.md).
