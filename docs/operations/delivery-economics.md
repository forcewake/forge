# Delivery economics: accepted items priced by real identities (R37-13, Q39-11, R41-13)

How forge explains what an accepted work item — and the whole programme
— actually cost, joining REAL recorded ledgers to the executions and
acceptance decisions that produced them. Module:
`forge.adaptive.delivery_economics`; tests:
`tests/test_delivery_economics.py`; builders:
`scripts/build_economics_report.py` (the economics report) and
`scripts/build_accepted_task_ledger.py` (the accepted-task ledger,
Q39-11, and the review-loop lineage fold, R41-13). The executed report
over the lab pilot's real recorded ledgers is committed at
[`evaluation/economics/lab-economics-v1.json`](../evaluation/economics/lab-economics-v1.json);
the accepted-task ledger over the useful-WIP resume trace plus the #310
SDK receipts is committed at
[`evaluation/economics/accepted-task-ledger-v1.json`](../evaluation/economics/accepted-task-ledger-v1.json);
the lineage-folded v2 ledger over the #364 review-and-correction
capture is committed at
[`evaluation/economics/accepted-task-ledger-v2.json`](../evaluation/economics/accepted-task-ledger-v2.json)
(the v1 artifact is never overwritten).

## The join chain

`EconomicsLinker.link()` extends the #276 measurement join by one link —
the acceptance decision — and joins by STABLE IDS only:

```
work id → execution attempt id → usage receipt identity → acceptance record
```

- **ledger** (`forge.adaptive.delivery_measurement` `DeliveryLedger`
  or its stored document) is the SPEND authority;
- **acceptance records** (`AcceptanceRecord`-shaped mappings from the
  pilot's task evidence: task id, the human `decided_by`, per-attempt
  outcomes, the `acceptance_contract` check names) are the OUTCOME
  authority — when the two disagree, the conflict is surfaced and the
  acceptance record wins;
- **profile versions** (`recipe_id@spec-digest`, `harness_id`) ride
  every work, so two reports cannot silently compare across a changed
  profile;
- an attempt the acceptance record names but the ledger never saw stays
  in the coverage denominator as an UNOBSERVED attempt — a gap, never a
  dropped row.

## The honesty rules (pinned by tests)

1. **Evidence classes.** Every receipt is labelled `live-model` (a real
   gateway served it; the only class a throughput number may rest on)
   or `synthetic-vendor-counter` (scripted-vendor wire counters — real
   wire shapes, no measured inference, no spend; the lab pilot's
   `lab-lane/vendor-wire` source). Unclassifiable stays `unknown` and is
   never promoted. A synthetic counter inside a throughput comparison
   raises `EvidenceClassError` — excluded, never averaged in.
2. **No decode label on request-latency populations.** The #276
   `RATE_LABEL_SPACE` rule, extended: `decode_throughput()` (and
   `throughput_comparison()`) first require a model-span population
   (`RateLabelError` otherwise — output ÷ total-request latency is not
   decode speed), then require purely `live-model` evidence.
   `assert_latency_guards()` re-checks any document's throughput rows at
   read time.
3. **Billing vs estimate, versioned.** A receipt that carries a cost
   figure is priced `billing`; everything else is priced from a
   versioned `RateCard` and labelled `estimate` with the card version
   on every entry. The columns never sum into one another. The card's
   cache pricing follows the #276 conventions (OpenAI-shaped cached
   tokens ride inside the inclusive input; Anthropic-shaped disjoint
   counters price separately — never both).
4. **Unknown costs are never zero.** A partially observed population
   renders `billed_usd: null` with a known lower bound, a
   `receipt_coverage` fraction and `unknown_cost_receipts` counts. The
   lab pilot (scripted vendor, no paid models) shows exactly this
   shape.
5. **Failed work stays.** An accepted item's total prices ALL its
   attempts (`failed_or_superseded_attempts_kept`); the programme
   cost per accepted item includes rejected/cancelled/superseded/
   abandoned work in the numerator and the accepted count in the
   denominator.
6. **Latency stages separated.** `latency.stage_seconds` folds the
   ledger's typed spans per stage — model / tool / queue /
   verification / human_wait (/ restore_collection) — each population
   named by `span:origin`; unmeasured stages are `measured: false`
   with a note, never zero; a stage spanning multiple origins
   withholds its total (populations never mix); whole-task windows
   (setup, completion latency) are never re-typed into stages.
7. **Identity, order, replay.** Duplicate receipt delivery collapses by
   identity (disagreeing claims under one identity surface as a
   conflict and degrade exactness). A receipt id delivered under two
   works is a `CrossRunJoin` — counted once under its first sorted
   attribution, refused for the other, both exact totals degraded.
   Reordering every input changes no byte; `EconomicsReport.
   from_document()` replays the aggregation from the stored document.

## The operator surface

- `operator_summary(report)` — a compact, redacted fold of the report
  (join counts, cost columns with bases, coverage, latency stages,
  evidence-class census, `repeated_work_attempts`) with no prompt,
  tool or task content: `redact_for_operator()` drops sensitive keys
  at every depth. There is no second metrics truth store.
- `reconcile_with_budget(report, budget)` — the customer-facing
  reconciliation: one line per cost column against the agreed cap,
  each `within` / `over` / `unreconcilable` with its reason. A lower
  bound or an estimate can never certify a cap; ids, numbers and
  reasons only — no code, prompts or secrets.

## Building the report

```
uv run python scripts/build_economics_report.py \
    --pilot evaluation/pilot/lab-pilot-v1 \
    --rates evaluation/economics/rate-card-lab-v1.json \
    --out evaluation/economics/lab-economics-v1.json
```

The script reads the pilot's real recorded artifacts (per-task usage
ledgers, operator decisions, spec/tasks), links them through
`MeasurementLinker` → `EconomicsLinker`, and writes the stamped report
including the measurement ledger it was built from, the operator
summary and the budget reconciliation against the pilot's summed
per-task budget ceilings.

When the discovery-live captures (sibling issue #290,
`evaluation/discovery_live/runs/`) have landed with receipts, the
script joins them as a SEPARATE population under
`populations.discovery_live` — never folded into the pilot's aggregates.
Those captures are research runs with mechanical grades, not accepted
delivery items: no acceptance decision exists, so no accepted items are
claimed; their real gateway receipts join as `live-model` evidence (the
only live evidence in-tree) beside the scripted run's
`synthetic-vendor-counter` label; their per-call receipts aggregate to
one usage receipt per attempt, priced from the card's discovery rate
(the capture's own recorded cap-enforcement prices) so the card-priced
estimates reproduce the capture's arithmetic exactly — estimates,
never billing.

## The committed lab report's honest numbers

- 12 works / 19 attempts / 12 accepted items; 7 tasks carry a kept
  failed first attempt.
- `receipt_coverage` 1.0 (19/19 receipts present), but 19/19 are
  `unknown_cost_receipts`: the scripted vendor bills nothing, so
  `billed_usd` is `null` and the lower bound is stated with coverage —
  never an exact zero.
- All 19 receipts are `synthetic-vendor-counter`; zero `live-model`.
- Estimate (card `lab-estimate-v1`, an authored assumption): $0.04326
  programme / $0.003605 per accepted item — labelled estimate, not
  spend.
- `latency.stage_seconds`: model 10.836 s, human_wait (reviewer) 0.846
  s; tool / queue / verification unmeasured and named as gaps.
- Budget cap $60.00 (the summed per-task ceilings) → `unreconcilable`:
  unknown spend cannot certify a cap.
- The discovery-live population (separate): 2 works / 4 attempts, 3
  `live-model` receipts + 1 `synthetic-vendor-counter`, card estimate
  $0.31634 (reproducing the capture's own recorded arithmetic
  0.070568 + 0.131920 + 0.113852), 0 accepted items — research
  captures carry grades, not acceptance decisions.

## Observability keys

The report's `observability` block carries the issue-mandated gauges
verbatim: `usage.receipt_coverage`, `cost.lower_bound`,
`cost.accepted_item_total`, `cost.programme_per_accepted_item`,
`latency.stage_seconds`.

## The accepted-task ledger (Q39-11 / #330)

`AcceptedTaskLedgerBuilder.build()` joins the economics report (the
spend authority) with the identity links the durable evidence carries
and emits an `AcceptedTaskLedger` (stamp
`forge.delivery.accepted-ledger/1`) — ONE document with the complete
evidence chain per task:

```
run → attempt → model-call receipt → native job → candidate
    → verification → human decision
```

- **Every link joins by stable id.** A job naming an attempt the spend
  ledger never saw, a verification naming an unknown candidate, a human
  decision naming an unknown work — each surfaces in `identity_gaps`,
  never a silent drop. Duplicate deliveries collapse by identity (the
  canonical form wins); two DIFFERING records under one identity
  surface as a conflict, never a merge.
- **Three cost columns, never blended** (`costs.columns`):
  `provider_reported_usd` (the SDK's own meter), `price_card_estimate_usd`
  (a versioned card), `billing_reconciliation_usd` (a billing export).
  Every priced entry lands in at most ONE column; a column total is
  exact only when EVERY attempt of the population contributed a figure
  to THAT column — a mixed-basis population leaves every column a lower
  bound with its coverage, and the columns are never summed together.
  An empty column renders `null`, never a readable `0.0`.
- **The two measures, distinct.** `costs.accepted_all_attempt` prices
  every attempt of an accepted work — failed, paused and superseded
  attempts stay in the total; `costs.programme_per_accepted_item`
  divides the whole programme's column (rejected and abandoned work
  included) by the accepted count. `costs.all_attempt_totals` keeps
  every work's own row whatever its decision state.
- **The human decision point is labelled, never guessed.** A delivered,
  verified candidate still awaiting its human (a draft MR) carries
  state `pending`: the work stays OUT of the accepted population, the
  per-accepted economics stay undefined — never zero. CI green is
  never acceptance.
- **Seven time measures** (`time_measures`): model time, tool time, CI
  queue, CI runtime, operator wait, reviewer effort and setup effort
  each fold only their own recorded windows (the raw windows ride the
  document); populations never mix inside a measure, an unknown window
  degrades to a lower bound, an unmeasured measure is a named gap, and
  `delivery.human_minutes` is `null` whenever reviewer effort is
  unknown. No tokens/s figure exists anywhere without matched measured
  model time and the #276 token convention —
  `assert_no_unmatched_rates()` raises on a tampered document.
- **Budget refusal and review-only recovery stay separately visible**
  (`budget` per work): the #325 closing-budget five fields (exact /
  known subtotal / lower bound / reserved liability / unknown
  intervals), the closing reserve and the coder ceiling, the recorded
  `budget_refusals`, and the `review_only_recovery` verdict — the #325
  shortcut evaluated against the recorded candidate binding (a moved
  candidate head refuses with `review_shortcut_stale`).
- **Coverage beside every aggregate**: `receipts_expected` /
  `receipts_received` / `receipt_coverage` / `unknown_cost_receipts` at
  programme, accepted-population and per-work level. A streamed partial
  receipt keeps its figures as a lower bound until the late final
  receipt reconciles the identity (then the rebuilt ledger's totals
  update — the #324 durable contract).
- **Order and replay invariance**: shuffling every input list, or
  delivering every artifact twice, changes no byte;
  `AcceptedTaskLedger.from_document()` replays the document.

### Building the ledger

```
uv run python scripts/build_accepted_task_ledger.py \
    --out evaluation/economics/accepted-task-ledger-v1.json
```

The script reads the useful-WIP resume trace
(`docs/evaluation/2026-09-25-useful-wip-resume/` — the #306 drill
evidence) and the #310 live single-writer SDK receipts (ingested
idempotently exactly as `build_economics_report.py` does), and joins
each as its own population — never folded together.

### The committed ledger's honest numbers

- The primary population: 2 works / 4 attempts, receipt coverage 1.0,
  every SDK receipt provider-reported — **$0.815235** programme
  (exact); run `04bca389…`'s all-attempt total **$0.559887** over 3
  attempts (2 superseded kept), the sibling run `d58082a2…` **$0.255348**.
- The price-card column is EMPTY (no receipt lacked a figure to
  estimate; the drill's own recorded price class rides the document as
  the versioned card `useful-wip-priceclass-v1`, armed but unused) and
  the billing-reconciliation column is `null` (no billing export
  exists) — stated, never blended into the provider figure.
- Both human decision points are **pending** (draft MRs; a human
  merges, forge never does): 0 accepted items, per-accepted economics
  undefined — never zero.
- Time, from recorded timestamps only: CI runtime lower bound 254.70 s
  (three lane-job windows across DIFFERENT clocks — the total is
  withheld; the uninterrupted arm's 87.3 s turn exists only as README
  prose and stays unknown), operator wait 347.05 s (two decision gaps),
  setup effort 58.20 s (the alignment window); model/tool/CI-queue time
  and the reviewer's effort are named gaps — the reviewer never ran
  (the budget refused its call), so `delivery.human_minutes` is `null`.
- The run's terminal budget refusal is recorded as a budget event; the
  #325 review-only continuation over the unchanged, verified candidate
  evaluates `allowed` with **zero coder dispatches, zero commits**.
- The #310 population (separate): 3 works / 5 attempts, provider-reported
  lower bound **$0.803749**, receipt coverage 0.8 — the killed job730
  keeps its attempt with no receipt: spend unknown, never zero.

The ledger's `observability` block carries the issue's gauges:
`delivery.accepted_all_attempt_cost`, `delivery.programme_cost_per_accepted`,
`cost.coverage`, `delivery.human_minutes`, plus `budget.closing_reserve`,
`budget.phase_exhaustion` and `delivery.review_only_recovery`.

## The review-lineage fold (R41-13 / #368)

`fold_review_lineage()` / `accepted_ledger_v2_document()` extend the
accepted-task ledger with the correction-loop shape (stamp
`forge.delivery.accepted-ledger/2`; the identity chain gains the
`root_run` link). The join: every run of a lineage names its
`root_run_id` — the #359 collaboration identity the round rows carry
and the lineage branch (`factory/<issue>/<root-short>`) names — plus a
`root_join_basis` naming HOW it joined (a round row's `root_run_id`,
the typed conflict's branch, the capture's own failed-attempt
attribution). Correction rounds and prior failed attempts fold under
the ONE root task; a run of another lineage stays programme-side, never
folded into a root.

- **No double counting across parent and child.** Each receipt id,
  native job id and delivery label is counted ONCE over the root task's
  member chains — the fold emits the proof (`no_double_count` with
  distinct-vs-total counts, `delivery_labels_per_lineage: 1`); a repeat
  receipt identity refuses exactly as the report's cross-run guard does
  (counted under its first sorted attribution, the second scope's exact
  total degrades to unknown — never a second copy of the spend). The
  lineage's ONE MR is recorded once at the root (`collaboration_label`).
- **Independent candidate outcomes.** Every round row carries its OWN
  `round_outcome` (`ready_for_human` / `blocked` / …), its candidate
  sha and its incremental cost — the round's own receipts, never a
  re-slice of the parent's.
- **The budget window rides the root, versioned.** Each round's OWN
  finite budget (limits, status, the closing partition's reserved
  calls/tokens) is listed; the #340 `budget_amendments` rows ride with
  their `limit_before`/`limit_after` history; the #339 exposure
  quantities ride twice — per-axis (`consumed`/`limit`/`exhausted`,
  the closing-review-stood-down fence) and the finality-aware closing
  fold (`settled`/`accrued_unsettled`/`retained_liability`) over the
  root task's receipts under the window cap. The policy block carries
  an EXPLICIT `version` (the receipted budget-profile pin); a later
  change writes new rows and documents — history is never rewritten.
- **Windows are separate measures.** `manual_rescue` (the
  kill-recovery gap) joins the seven v1 measures as its own window;
  `human.review_and_rescue_minutes` stays `null` while reviewer effort
  is unmeasured (a partial is never presented as a total — the measured
  components ride `time_measures`).
- **Unknown spend stays bounded, never zero.** The window
  reconciliation states the recorded totals beside the folded receipts;
  the residual is attributed to the unreceipted lane runs and BOUNDED
  there — never added to a cost column (a figure the SDK never reported
  is a bound, not spend).
- **The human acceptance is an identified decision.** The reviewer
  resolutions in a capture (discussions resolved through the API,
  closing-review verdicts) are model/CI decisions over the candidate —
  the HUMAN acceptance of the lineage is its own pending point, named;
  per-accepted economics stay undefined at 0 accepted items, with the
  denominators stated.

### Building the v2 artifact

```
uv run python scripts/build_accepted_task_ledger.py \
    --population review-loop \
    --out evaluation/economics/accepted-task-ledger-v2.json
```

The script reads the #364 capture
(`docs/evaluation/2026-09-27-review-loop/` — the evidence bundle plus
every lane job's trace) and the durable record
(`qualification/records/review-loop-2026-09-27.json`), and is
byte-deterministic on rerun; reordering every input list changes no
byte; `AcceptedTaskLedger.from_document()` replays the stored document
and re-folds the lineage byte-identically (there is no second truth
store).

### The committed v2 ledger's honest numbers

- 8 works / 8 attempts; 7 receipted (coverage 0.875) — the
  unreceipted one is the foreign-lineage blocked child (`6e0fdf34`, the
  wrong-branch run of the #361 publisher defect; its SDK figure never
  reached the record).
- The root task `7319478e` (issue 5's lineage, MR !4): **7 attempts**
  — 3 honestly-failed delivery attempts + delivery 1 + rounds 2/3 +
  the blocked round 4 — all-attempt provider-reported **$1.256064
  EXACT** (every attempt SDK-receipted; the bootstrap-fence attempt's
  zero is RECORDED, never inferred). Round incremental costs:
  delivery 1 $0.2112472, round 2 $0.1980048, round 3 $0.1968864,
  round 4 (blocked) $0.2394448.
- The recorded window totals reconcile: qualifying rounds fold to
  $0.845583 against the recorded $0.8455 (4dp); the recorded
  all-attempt $1.4797 leaves a **$0.223636 unreceipted residual**
  attributed to `6e0fdf34` and bounded there — never a column entry.
  The programme keeps a $1.256064 lower bound with coverage 0.875.
- The budget window: each round's own 600k-token budget (rounds 2/3
  with the `closing-partition/1` reserve of 90000 tokens / 6 calls),
  the prior attempt's exhausted 200k fence (196312/200000 consumed, the
  closing review stood down), and the amendment
  `standard.max_tokens 200000→600000` under
  `forge.budget-profiles/1@2026-09-27-align`.
- The human decision is **pending** (draft MR !4; the reviewer
  resolutions are model/CI decisions): 0 accepted items, per-accepted
  economics undefined — never zero, denominators stated.
- Time, from recorded timestamps only: native runtime (trace
  envelopes) lower bound 492.905 s over five traced jobs (three
  untraced windows stay unknown); human wait 1364.815 s exact (four
  dispatch→terminal windows); manual rescue 283.072 s exact (the
  round-3 kill→recovered-ready window); setup lower bound 55.621 s
  (three different lab clocks — the total is withheld); model, tool
  and CI-queue time and reviewer effort are named gaps;
  `human.review_and_rescue_minutes` is `null` (rescue 4.718 min
  measured, reviewer effort unknown); no throughput rows exist —
  receipts and durations never join, so every rate stays null.
- Observability: `usage.coverage_by_source` (7 SDK receipts, 1
  unreceipted attempt), `accepted_work.total_cost_lower_bound`
  ($1.256064 task-local, pending, beside the programme bound),
  `human.review_and_rescue_minutes`, `review_round.incremental_cost`
  and `identity.gaps` (0 — every link joined).
- Run `76a1088a` (issue 4's delivery, job 1148) also ran in-window
  (its trace is recorded) but the capture's own spend population
  excludes it — noted, never folded into this window's totals.
