# E2E-qualification research overview — mapping to the R32 P2 items (2026-09-23)

> Master summary of the 2026-09-23 E2E-qualification research pass.
> One file per topic, each with findings, links and ranked
> recommendations:
>
> 1. [01-e2e-qualification-layers.md](01-e2e-qualification-layers.md) — the smoke→contract→integration→acceptance layering, gate mechanics (aggregate gate, resource-owned checks), ephemeral-vs-persistent environments, test credentials, evidence collection
> 2. [02-synthetic-test-environments.md](02-synthetic-test-environments.md) — Testcontainers multi-service patterns, populated-baseline migration testing (A0/A1×D0/D1), broker guarantee tests with deterministic failpoints, compose teardown modes
> 3. [03-evidence-based-capability-qualification.md](03-evidence-based-capability-qualification.md) — continuous control monitoring (SOC 2/FedRAMP ConMon/20x KSIs), evidence chains bound to stable IDs, evidence-reading promotion gates, Pass/Waived/Blocked verdicts
> 4. [04-design-partner-pilot-methodology.md](04-design-partner-pilot-methodology.md) — design-partner cohort/contract/cadence, the paid bounded pilot with 2-of-3 criteria and stop conditions, DORA/SPACE/Core 4 and the five metrics that hold up for agent fleets
> 5. [05-operator-console-patterns.md](05-operator-console-patterns.md) — dual-path consoles (fast reads, guarded writes), derived operator states (running/wedged/paused/blocked/stale/dead), six-plane observability, closed operator loop, audit four-facts
> 6. [06-two-writer-coordinated-changes.md](06-two-writer-coordinated-changes.md) — consumer-driven contracts + compatibility matrix + can-i-deploy, publication saga with compensations, failure-point matrix testing, reconciliation fallback
>
> Confidence marks inside the topic files: **[documented]**
> authoritative source (linked) · **[observed]** demonstrated
> live/measured · **[inference]** this pass's synthesis for forge.

## The one-paragraph synthesis

Every domain studied — CI/CD qualification, compliance evidence,
dev-tool pilots, operator consoles, coordinated multi-writer changes —
converged on the same three moves: (1) **make qualification a query
over recorded evidence, not a re-run of tests** (can-i-deploy over
the matrix; the release checklist "became a dashboard query you
read"; FedRAMP 20x validates KSIs continuously); (2) **bind every
piece of evidence to a stable requirement ID at collection time**
with immutable storage and honest verdicts (attempt history retained,
Waived distinguished from Pass); (3) **test the unhappy coordination
path deterministically** (failpoint matrices for sagas and consumers,
mixed-version states, compensations proven idempotent). forge's
existing primitives — journals with idempotent receipts, the
capability manifest, the epoch/pause protocol, sentinel-credential
tests, failure-injection suites — are exactly the substrate these
patterns assume; what is missing is the layering vocabulary, the
evidence-chain linkage, and the query-shaped gate.

## Mapping to the seven remaining P2 items

### R32-13 — Recipe + harness qualification

**What the research supplies**: the canonical layer stack
(smoke → contract → integration → acceptance) with the honeycomb
weighting rule — curated E2E ≤ 5 %, heavy middle band of contract +
integration (topic 1 §1); the aggregate-gate + threshold-tier gate
mechanics so a suite's verdict actually binds (topic 1 §2); and the
synthetic target systems qualification runs against — versioned
target-system kits per dependency profile, populated-baseline
migration gates with the A0×D1 backward-compatibility rule, broker
guarantee tests via named failpoints (topic 2, recommendations 1–3).
**First moves**: name the layers and bind them to promotion
transitions; build the dependency-profile kits as versioned artifacts.

### R32-14 — Research quality evaluation

**What the research supplies**: the PROMOTE/HOLD/ROLLBACK gate shape
with a small set of non-redundant, threshold-bearing dimensions
(correlated dimensions get merged, not accumulated) and HOLD routed
to a named human with per-build expiry (topic 3 §4); the AI evidence
pack — pinned dataset/prompt versions, failed-cases file,
tool-call traces, override ledger (topic 3 §4); the measurement
rules — pair every speed metric with a quality counterweight, freeze
baselines, expect Goodhart (topic 4 §4).
**First moves**: adopt the three-value verdict + evidence pack as the
output format of every forge evaluation run; HOLD-with-expiry instead
of binary pass/fail for eval gates.

### R32-19 — Release artifacts from qualified evidence

**What the research supplies**: the evidence-chain architecture —
every test run is an evidence event tagged with a stable requirement
ID *at collection time*, immutable storage at deterministic paths,
requirement → latest-passing-build index, the audit package generated
as a *view* with logged chain of custody, monthly drills (topic 3
§3–4); the gaps query as the standing audit of qualification
coverage (topic 3 §2, recommendation 2); evidence freshness and
attempt-honest semantics (retry-passed = conditional pass, never
overwritten green; topic 1 §6).
**First moves**: capability-manifest evidence records with freshness
rules; `forge doctor --qualification-gaps`; release bundle = generated
view over the store, never hand-curated.

### R32-21 — Design-partner pilot

**What the research supplies**: the full pilot playbook — one-page
learning contract, fee as demand test, frozen baseline + staged
capability ladder (read/plan → small fixes → branch+PR → gate),
2-of-3 measurable criteria by a named end date, expand /
extend-once-for-a-named-gap / stop as the only exits, weekly operator
cadence with behavior-anchored feedback questions (topic 4 §1–3, 5);
the metrics — the five that hold up for agent fleets (autonomy rate,
cost per merged PR, defect/rollback rate, intervention rate counting
steering, cycle time vs frozen baseline) plus reviewer load as the
capacity tax, provenance labeled from day one, and the J-curve
pre-commitment that keeps the pilot alive through the adjustment dip
(topic 4 §4).
**First moves**: write the one-page contract with the 2-of-3 rule and
the staged ladder; freeze the partner baseline before kickoff.

### R32-22 — Two-writer WorkPackage

**What the research supplies**: the three nouns of the architecture
plan, matched one-to-one — compatibility steps = consumer-driven
contracts with a verification matrix; publication saga = persisted
step/compensation state machine with the four compensation rules and
pivot classification; CandidateSet integration tests = mixed-state
(old-writer × new-writer) curated tests (topic 6 §1–4). Plus the
query-shaped gate: promotion asks "does passing verification exist
for every edge at the versions being promoted" — the can-i-deploy
pattern with `--retry-while-unknown` for the two-pipelines race —
and the reconciliation sweep as the missed-event fallback.
**First moves**: make WorkContract the unit of matrix evidence;
encode the publication saga steps with idempotent compensations;
parametrized kill-at-step-k suite.

### R32-23 — Operator view

**What the research supplies**: the dual-path console (read-only
fast path over a derived projection; guarded, dry-run-previewed,
idempotent, audit-logged safe path) — read-only by charter, "must
not launch agents, edit code, merge PRs" (topic 5 §1, §3); the
derived operator-state vocabulary including **wedged** (alive but no
semantic transition — the looked-launched-but-stalled failure); thin
evidence links with drill-down instead of inlined logs; the closed
operator loop (status/watch/expand + durable interventions that
retain target, reason, timestamp, result) — forge's epoch/pause/steer
protocol seen from the UI side; the audit four-facts
(who/digest/when/linkage) (topic 5 §2, §4).
**First moves**: dense read-only table over attempt state with
derived states and thin evidence links; the four safe controls
(pause/steer/resume/cancel/rerun) through the confirmed+audited path.

### R32-24 — Composition boundaries

**What the research supplies**: the rules that make "what may
compose with what" enforceable rather than documented: compatibility
matrices record which versions are verified together, and blocking is
directional ("v8 is blocked until v3 ships and v8 adapts — the gate
stays consistent in both directions") (topic 6 §2); expand-contract
for interfaces — "remove only after verified-zero usage; the
registry shows WHO uses WHAT" (topic 6 §1); and the labeling
discipline for the unsupported-combination problem — every example
versioned and tested or explicitly marked illustrative, the same
honesty rule as forge's guarantee matrices, plus inventory-drift-as-
finding ("a capability that exists but has no qualification evidence
in scope reads as loss of control") (topic 3 §2).
**First moves**: encode composition permissions as matrix edges
(verified / untested / unsupported), surface blocked-by edges in the
operator view, and treat drift between shipped combinations and
qualified combinations as a doctor finding.

## Ranked first moves across the whole pass

Ranked by (unblocking effect on the seven P2 items) ÷ effort:

1. **Name the qualification layers and bind them to promotion
   transitions** (R32-13; topic 1) — vocabulary plus gate rules, no
   new infrastructure.
2. **Capability-manifest evidence records with requirement IDs,
   freshness rules, and the gaps doctor check** (R32-19, R32-24,
   feeds R32-14; topic 3) — extends the existing manifest and
   content-addressed store.
3. **One-page pilot contract + frozen baseline + the five metrics**
   (R32-21; topic 4) — mostly process; instrumentation is the only
   build.
4. **Read-only operator projection with derived states and thin
   evidence links** (R32-23; topic 5) — the journal projection
   already exists; the console consumes it.
5. **WorkContract matrix + publication-saga failure-point suite**
   (R32-22; topic 6) — the largest build; starts with a two-repo
   pilot pair exactly as the architecture plan stages it.
6. **Target-system kits per dependency profile** (underpins R32-13
   and R32-22; topic 2) — compose/Testcontainers topologies as
   versioned artifacts.

## What the research says forge should *not* do

- **Do not qualify everything through full live-driver E2E runs** —
  the ice-cream-cone failure mode; the honeycomb says curated
  acceptance on top of a heavy contract/integration band.
  [documented]
- **Do not re-run suites at promotion time** — promotion reads
  recorded evidence ("a dashboard query you read"); re-running
  invites both latency and retry-washed verdicts. [documented]
- **Do not let a retry overwrite a failure** — attempt history is
  part of the verdict; retry-passed is a conditional pass.
  [documented]
- **Do not judge the pilot during the J-curve** — the documented
  pilot-killer; pre-commit the measurement window with the partner.
  [documented]
- **Do not put writes in the operator console** — read-only by
  charter, guarded control path separate; bimodal oversight
  (30 stops or zero) is the failure shape. [documented]
- **Do not promise easy rollback for irreversible steps** — "promise
  tested compatibility, safe forward recovery, and observability";
  classify the pivot at design time. [documented]
