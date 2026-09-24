# Delivery measurement — example ledger fixture (2026-09-23)

> **This is AUTHORED FIXTURE DATA, not measured evidence.** Every work id,
> receipt, call and span below was written by hand to exercise the
> identity-linked measurement path end to end (issue #276 / backlog
> R36-17, trace AT-12). Nothing here is a measured tokens/s or
> cost-per-accepted-task claim about any real forge run; per the
> honest-evidence rules, authored example data must be labeled as such.

## What is in this directory

- `ledger.json` — an example `DeliveryLedger` document (schema
  `forge.delivery.measurement/1`) produced by
  `forge.adaptive.delivery_measurement.MeasurementLinker` from the
  authored records below.
- `report.json` — the aggregation of that ledger via `build_report`.
  `tests/test_delivery_measurement.py` replays `ledger.json` through
  `replay_report` and asserts the result is byte-identical to this file —
  the committed pair is the replay-determinism witness.

## The authored scenario

Three works, five attempts, deliberately imperfect:

| Work | Outcome | Attempts | Receipts |
|---|---|---|---|
| `FX-01` | accepted | `FX-01:1` (superseded), `FX-01:2` (accepted) | both present, exact — one OpenAI-shaped, one Anthropic-shaped (disjoint cache counters) |
| `FX-02` | rejected | `FX-02:1` | present, exact — its spend stays in the programme denominator |
| `FX-03` | accepted | `FX-03:1` (abandoned), `FX-03:2` (accepted) | `FX-03:1` has NO receipt — the authored honest gap |

Because `FX-03:1`'s usage receipt is missing, the report's exact
programme cost is `null` with a known lower bound and coverage `0.8` —
exactly the "unknown earlier receipt stays unknown" rule, visible in the
committed artifact. Latency spans cover all six population types
(`model` in both `planner` and `harness` origins, `tool`, `queue`,
`verification`, `human_wait`, `restore_collection`), each aggregate
carrying its `population_identity`; `FX-03:1`'s unmeasurable model span
degrades its population rather than zeroing it.

## Regenerating

The files were produced by feeding the records above to
`MeasurementLinker().link(...)` and `build_report(...)`; regenerating
with different authored inputs must update both files together so the
replay test keeps its meaning.
