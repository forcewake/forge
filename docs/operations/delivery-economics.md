# Delivery economics: accepted items priced by real identities (R37-13)

How forge explains what an accepted work item — and the whole programme
— actually cost, joining REAL recorded ledgers to the executions and
acceptance decisions that produced them. Module:
`forge.adaptive.delivery_economics`; tests:
`tests/test_delivery_economics.py`; builder:
`scripts/build_economics_report.py`. The executed report over the lab
pilot's real recorded ledgers is committed at
[`evaluation/economics/lab-economics-v1.json`](../evaluation/economics/lab-economics-v1.json).

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
