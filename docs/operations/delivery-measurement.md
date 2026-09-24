# Delivery measurement: the identity-linked ledger (R36-17)

How forge turns recorded delivery facts into a measured, comparable
economics report without hiding failed work or calling unknown usage
zero. Module: `forge.adaptive.delivery_measurement`; tests:
`tests/test_delivery_measurement.py`. A committed example ledger
(authored fixture data, labeled as such) lives in
[`docs/evaluation/2026-09-23-delivery-measurement/`](../evaluation/2026-09-23-delivery-measurement/README.md).

## The join chain

`MeasurementLinker.link()` joins records by IDENTITY — work id →
execution attempt id → model-call/receipt identity → provider route —
over shapes the repo already produces:

- **works**: `{work_id, outcome, model_version, harness_version,
  acceptance_contract}` — outcome is one of accepted / rejected /
  cancelled / superseded / abandoned (unknown stays unknown);
- **attempts**: the evidence `attempts` contract
  (`forge.adaptive.delivery_metrics`), plus `work_id`;
- **receipts**: the `.forge/usage.json` / candidate-meta usage block
  with `receipt_id`, `attempt_id`, driver/model route, token counters,
  `total_cost_usd`, `completeness`;
- **calls**: `llm_calls`-shaped rows (`id`, `flow_run_id`, `attempt_id`,
  provider/model, `duration_ms`, tokens);
- **spans**: typed latency windows (below).

`ledger_records_from_delivery_metrics(delivery_metrics_for_run(...))`
is the connected path from the durable run evidence into the linker.

## The honesty rules (pinned by tests)

1. **Exact or unknown, never zero.** Every total is exact only when
   every expected receipt contributed; otherwise `null` with a known
   lower bound and coverage. An unknown EARLIER receipt stays unknown
   after a later known one.
2. **Cache never double-counted.** OpenAI-shaped inclusive input keeps
   the cache inside `input_tokens`; Anthropic-shaped disjoint counters
   sum. Reasoning is a breakdown inside output — conventions are
   recorded on the report.
3. **Populations never mix.** Spans are typed `model | tool | queue |
   verification | human_wait | restore_collection` and origin-scoped
   (`planner` / `harness` / `operator`); every aggregate carries its
   `population_identity`. Output ÷ total-request latency can never be
   labeled decode throughput (`RATE_LABEL_SPACE` +
   `measured_rate` raise `RateLabelError`).
4. **Programme vs accepted unit.** Programme cost folds every attempt
   of every work — a rejected task's budget stays in the programme
   denominator; the accepted unit is priced by its own attempt chain.
5. **Conflicts surface, never average.** Duplicate receipts collapse by
   identity; disagreeing claims become conflict rows; a decreasing
   cumulative counter is a counter reset; duplicated call ids across
   attempts collapse to one attribution and are surfaced.
6. **Order invariance and replay.** The linker sorts and folds by
   identity/content, never arrival order; `replay_report` re-aggregates
   the stored ledger document byte-identically (schema stamp
   `forge.delivery.measurement/1`).
7. **AT-12 trace.** `trace_accepted_task(ledger, work_id)` walks one
   task from its cost total to every included receipt and every
   excluded/unknown record, both listed.

## Observability keys

The report's `observability` block carries the four issue-mandated
gauges verbatim: `usage.exact_coverage`,
`usage.known_cost_lower_bound`, `delivery.accepted_cost_distribution`,
`latency.population_identity`.
