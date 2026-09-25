# Conformance gate — native templates, secret consumers, consumer contracts

Status: Written (#317 / R38-16; extended by #323 / Q39-04 and #327 /
Q39-08). Audience: operators and maintainers who need to know what the
release gate actually executes, how to read its report, and what to do
when it refuses.

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
  executed-ID manifest; neither converts a failure into a skip. The
  accounting-race contracts (#322 the CAS projection, #324 the
  partial→final reconcile) are REQUIRED traces on the PG gate's
  `accounting-races` profile — see its own docs.

## The five checks

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
   to the remaining SDK lanes is #305's scope). Q39-08 (#327) adds the
   **comment-only marker spoof** arm per recipe: the block's every line
   re-spelled as a comment — every expected string still present — must
   be REFUSED by the extraction (a marker in comments is not a
   consumer), while the shipped block survives the same comment-stripped
   lens.
4. **native-locators** (#323 / Q39-04) — the collision-safe locator
   dimension: the P05 pair and its variants map to DISTINCT locators
   within the provider charset; every driver's OWN recipe validates the
   negotiated route; the installed-template digest inventory is recorded
   per validated template. The self-test arms (the legacy lossy encoder,
   a wrong driver's template, a digest mismatch, an unknown driver) must
   each be CAUGHT.
5. **consumer-contracts** (#327 / Q39-08) — the new contracts (#320 the
   operation grant, #321 the revision rebind) through their ACTUAL
   consumers, offline and deterministic, sentinel values only:
   - the grant redeemed through the REAL ASGI endpoint
     (`GET /lane/credentials/redeem` on the real `create_app` app over
     `httpx.ASGITransport`): the granted route redeems the delivered
     sentinel, the SIBLING route of the same project refuses typed
     `grant_route_mismatch` with ZERO broker calls, the durable audit is
     value-free, and the issue's named trap is a mutation arm — a grant
     document whose every DTO value is intact but parked where the
     production caller cannot load it authorizes NOTHING;
   - the runner-side typed verification (#320's CD-9 shape) as REAL
     `python -m forge.lane_driver` subprocesses against a CANNED
     redemption endpoint: the correct document's baseline passes the
     SAME trace first, then a wrong-slot and an expired ANSWER each halt
     the lane (`credential_redemption_failed`) with ZERO calls at the
     fake model endpoint — the vendor client is never constructed and
     the ambient key never substitutes;
   - the #321 rebind digest through a REAL `RunService` dispatch over
     the fake native server (GitLab mode) under an ACTIVE revision: the
     persisted `revision.executor_input_digest` equals the digest
     recomputed from the RECORDED dispatch variables, and the dispatched
     `FORGE_BRIEF_ENVELOPE_DIGEST` verifies over the dispatched
     `FORGE_PLAN` bytes — the offline three-way equality (the PE trace
     in `tests/production_entry/test_gitlab_revision_rebind.py` stays
     the deeper proof). The plan-binding-removal and
     source-identity-swap mutations replay the #321 counterexample on
     the SAME recorded trace and must be CAUGHT.

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
| 0 | green — every arm passed, all mutation self-tests caught, zero findings |
| 2 | prerequisite — templates/captures missing or unparseable, bash/git unavailable |
| 3 | a shipped recipe's executed shell missed its contract |
| 4 | dispatch-schema findings |
| 5 | a secret-consumer sentinel arm failed |
| 6 | mutation escape — the gate could NOT catch a defect it exists to catch |
| 7 | native-locator conformance failure (#323) |
| 8 | consumer-contract failure (#327): the grant ASGI arms, the runner verification, or the rebind digest |

## Reading the report

`conformance-gate.json` carries, per check: every template's executed
arms with their typed expectations and job exit codes, the mutation
self-test outcomes, the schema findings per provider, the sentinel arm
outcomes — and `executed_ids`, the manifest of WHAT actually executed
(the #267 pattern: never an aggregate green alone). A refusal records
its type, detail and exit code under `qualification.refusals`.

Q39-08 (#327) adds three report blocks:

- `identities` — the exact source/template/runtime identity per check
  and per arm class, with the offline / native / paid execution classes
  DISTINGUISHABLE: every arm this gate runs is offline (the lint job);
  the native class is the PG gate's required selection (integration
  job); the paid class is the triggered live-qualification job. A green
  offline arm can never pose as a live one.
- `observability` — `conformance.executed_case_count` (the executed-ID
  manifest's size), `conformance.required_case_missing` (the REQUIRED
  consumer cases that did not execute — MUST stay `[]` for green) and
  `ci.critical_path_seconds` (the gate's own wall clock).
- `checks.consumer_contracts` — the check-5 arms, their boolean
  expectations, and the rebind drive's recorded variable NAMES (never
  values; the identity variables are digests by construction).

## The accounting races on the PG gate (#327)

The conformance gate proves the grant and the rebind offline; the
accounting races need REAL row-level isolation and live on the PG gate
(`scripts/pg_gate.py`) as the `accounting-races` profile:
`tests/test_credential_audit.py` (the #322 CAS projection — concurrent
redemption and evidence writers must BOTH survive) and
`tests/test_usage_ingestion.py` (the #324 partial→final reconcile under
real isolation). Their PG-gated arms are REQUIRED traces tracked by
executed critical ID: a collection missing them, or a `FORGE_PG_TEST_URL`
skip on them, REFUSES the gate — never a silent green skip.

## Lifecycle discipline (#299)

The gate's fixtures close what they open: subprocess runs are awaited
(killed children are joined), temporary checkouts/workroots are removed
in `finally`, the capture harness disposes every engine and terminates
every fake native server process, and no warning filter is touched.
