# Conformance gate — native templates and secret consumers

Status: Written (#317 / R38-16). Audience: operators and maintainers who
need to know what the release gate actually executes, how to read its
report, and what to do when it refuses.

## Why this gate exists

Two defects shipped through green CI at the template/secret boundary:

- **R38-01 (#302)** — the GitLab SDK lanes' packaged shell ended with an
  unconditional driver-rc `exit`: a SUCCESSFUL driver left a green job
  with no candidate artifact. The regression suite
  (`tests/test_gitlab_sdk_lane_finalization.py`) executes the packaged
  shell; this gate PROMOTES that execution into the release gate so the
  same class cannot ship again just because the suite selection changed.
- **R38-02 (#303)** — the credential delivery contract landed with fake
  native ledgers that accept dispatch inputs the shipped workflow does
  not declare, and no consumer-side proof that the credential the
  receipt claims was the credential the lane actually consumed.

## What runs, and where

```bash
uv run python scripts/gate_conformance.py --report conformance-gate.json
```

- **CI**: the lint job, on EVERY push and PR (`Native template &
  secret-consumer conformance` step; the report uploads as the
  `conformance-gate-report` artifact on `always()`).
- **PG-gate relationship** (`scripts/pg_gate.py`, #267): the conformance
  gate is deliberately standalone. It runs on every push because it
  needs no database and no provider; the PG profiles run on the
  integration job against real PostgreSQL. Both produce an
  executed-ID manifest; neither converts a failure into a skip.

## The three checks

1. **shipped-recipes** — every `ci/templates/*` recipe carrying the
   finalization/driver block (the four SDK lanes) is YAML-parsed and its
   finalization script extracted VERBATIM, then executed under REAL
   bash in a REAL git checkout through #302's own test fixtures (the
   stub `FORGE_LANE_PYTHON` driver leg + the REAL packaged collector):
   success / failure / no-op / restored-generation arms. The #302
   mutation — an unconditional early exit restored before collection —
   is replayed as a per-template SELF-TEST and must be caught; if it
   were not, the gate refuses with exit 6 (it proved itself insensitive,
   which is worse than any recipe failing).
2. **dispatch-schema** — the shipped templates' declared dispatch
   surface (GitHub `on.workflow_dispatch.inputs`, Azure `parameters`,
   the GitLab recipes' consumed pipeline variables) is validated against
   the CAPTURED production dispatch payloads in
   `scripts/conformance_dispatch_captures.json`:
   - an **undeclared key the service sends** (the fake ledger accepted
     it; the real provider answers a dispatch-wide 422) — a finding;
   - a **declared key never sent** and not covered by a source-verified
     conditional annotation — a finding (dead input or template drift);
   - a **GitLab variable no shipped recipe consumes** — a finding.
3. **secret-consumers** — every recipe carrying a credential-consumption
   block (the #303 env mappings) is rendered with a DELIVERED sentinel
   in the carrier and a DIFFERENT ambient sentinel in the parent env,
   then executed: the consumed slot must carry exactly the delivered
   sentinel, the ambient must never reach it, an empty carrier must fail
   CLOSED, and the dropped-consumer-mapping mutation must be CAUGHT
   despite a correct-looking receipt. Recipes without a block are
   recorded `absent` (visible, never silently skipped; the block rollout
   to the remaining SDK lanes is #305's scope).

## The dispatch capture fixture

`scripts/conformance_dispatch_captures.json` is the recorded production
truth: the REAL GitHub/GitLab/Azure run services driven through the
production-entry fakes, fronted by DECLARING WRAPPER CLIENTS
(`tests/test_gate_conformance.py`) that refuse, at the HTTP boundary, a
dispatch carrying keys outside the shipped templates' declared surface —
the early guard for the "fake ledgers accept anything" failure mode.
KEYS only are recorded, never values.

- Regenerate after an INTENTIONAL dispatch-surface change:
  `uv run python scripts/gate_conformance.py --regenerate-captures`,
  then review the diff before committing.
- Drift is pinned both ways: the capture test re-drives the services and
  must match the fixture exactly; the gate (lint) validates the
  templates against the fixture on every push.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | green — every arm passed, both mutation self-tests caught, zero findings |
| 2 | prerequisite — templates/captures missing or unparseable, bash/git unavailable |
| 3 | a shipped recipe's executed shell missed its contract |
| 4 | dispatch-schema findings |
| 5 | a secret-consumer sentinel arm failed |
| 6 | mutation escape — the gate could NOT catch a defect it exists to catch |

## Reading the report

`conformance-gate.json` carries, per check: every template's executed
arms with their typed expectations and job exit codes, the mutation
self-test outcomes, the schema findings per provider, the sentinel arm
outcomes — and `executed_ids`, the manifest of WHAT actually executed
(the #267 pattern: never an aggregate green alone). A refusal records
its type, detail and exit code under `qualification.refusals`.

## Lifecycle discipline (#299)

The gate's fixtures close what they open: subprocess runs are awaited
(killed children are joined), temporary checkouts/workroots are removed
in `finally`, the capture harness disposes every engine and terminates
every fake native server process, and no warning filter is touched.
