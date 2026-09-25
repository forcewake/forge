# Closing budget: known spend, safe reserves, the protected review (Q39-06)

How forge keeps the REQUIRED closing review affordable: the
implementation phases cannot consume the allowance the review needs,
unknown spend is reserved at a worst-case envelope (never a lower
bound), and a budget-blocked review recovers through an explicit,
auditable operator action — never a hidden retry. Module:
`forge.adaptive.closing_budget`; the corrected cap check:
`forge.adaptive.usage_ingestion.spend_cap_check`; tests:
`tests/test_closing_budget.py`, `tests/test_usage_ingestion.py` (issue
#325; the useful-WIP and combined-steering traces both ended
`blocked (budget_exhausted)` at the reviewer leg after the candidate
and independent CI had succeeded — the guard was right, nothing
protected the closing review).

## The three separated quantities

`spend_cap_check()` reports — and the hard cap consults — three
DISTINCT numbers, never folded into one:

| quantity | meaning |
| --- | --- |
| `known_spend` | every known billed/estimated cost |
| `unknown_lower_bound` | the known lower bounds of the unknown intervals (never zero when something is known — but a lower bound can never bound spend from ABOVE) |
| `reserved_liability` | the worst-case retained ENVELOPE: each unknown interval's own `cost_upper_bound_usd`, else the policy ceiling (`unknown_interval_ceiling_usd`) |

The hard cap refuses when `known_spend + reserved_liability +
projection_usd > cap_usd`. An unknown interval with NO finite upper
bound sets `requires_bounded_policy` and BLOCKS the next chargeable
action until an explicit bounded-policy ceiling is supplied or the
interval reconciles to a final cost — the P03 counterexample (cap 10,
known 8, lower bound 0.5, projection 1 "allowed" 9.5 while the interval
could settle at 3 for an actual 12) cannot pass. Reconciliation
releases an interval's envelope exactly once (a replayed identical
final is a no-op by the natural-key contract).

## The closing reserve

A profile-specific allowance for the mandatory review + final evidence,
un-consumable by the implementation phases: the CODER's cap check sees
`cap - closing_reserve` as its effective ceiling
(`coder_cap_check()`); the closing review itself is judged against the
FULL cap (`closing_budget_report()`), where the intact reserve is
exactly what covers it.

Policy fields (environment, per deployment profile — no new model
columns):

- `FORGE_CLOSING_RESERVE_USD` — the absolute allowance (wins);
- `FORGE_CLOSING_RESERVE_FRACTION` — a fraction of the cap (default
  **0.15, PROVISIONAL** — sized from the observed live workloads, whose
  lane receipts run $0.15–$0.80 per attempt; re-size from your own
  observed workloads rather than treating the default as a universal
  multiplier);
- `FORGE_SPEND_CAP_USD` — the USD spend cap the fraction resolves
  against.

With none configured the reviewer-leg budget decision honestly reports
"no closing reserve policy" (never a silent zero reserve) and parks the
run with that precise reason.

## The reviewer-leg budget decision (both lanes)

When the budget guard refuses the reviewer's call
(`LLMError("budget_exhausted")` — on the GitHub lane this used to be
misclassified as `review_failed`), the leg consults the closing policy
over the run's durable `usage_receipts` and records a
`review_budget_block` evidence document:

- the budget decision and the candidate binding (candidate sha + the
  verification's tested identity);
- the five-field budget report — `exact_usd` (final provider-reported/
  reconciled only), `known_subtotal_usd` (estimates included),
  `lower_bound_usd`, `reserved_liability_usd`, `unknown_intervals`;
- the visible reserve (`budget.closing_reserve`), the unresolved
  upper-bound count (`budget.unresolved_upper_bound`) and the phase
  exhaustion flag (`budget.phase_exhaustion`).

If the reserve is INTACT (the implementation phases left it
un-consumed), the run stays in the precise NON-READY `reviewing` state
with the reserve visible; only a reserve that cannot cover the review
parks `blocked` with the shortage named. A standing unreleased block
guards the leg: scanner re-drives stand down instead of re-attempting
the paid review call — never a hidden retry.

## Review-only continuation + the explicit top-up

```
service.continue_review_only(run_id, operator="human:<who>",
                             top_up_usd=0.50,
                             top_up_reason="close the review within its reserve")
```

The operator command (GitLab `RunService` and `GitHubRunService`)
repeats ONLY the review of the SAME candidate/tested identity: zero
coder dispatches, zero commits — the path never touches the implementer
or the writer. The candidate is re-checked against the LIVE branch/PR
head first: a moved head (or tested identity) invalidates the shortcut
with the typed `review_shortcut_stale` and the run parks for the
required fresh verification. The top-up is recorded BEFORE the re-drive
(amount + reason + operator, all required) and is replay-idempotent by
its derived key — the same command retried adds its amount exactly
once, across refusal cycles too.

## Observability

- `budget.closing_reserve` — the held allowance, on every decision;
- `budget.unresolved_upper_bound` — unknown intervals with no finite
  envelope;
- `budget.phase_exhaustion` — the implementation phases consumed the
  reserve / the closing review does not fit;
- `delivery.review_only_recovery` — the review-only continuation's
  outcome (including the typed staleness refusals).
