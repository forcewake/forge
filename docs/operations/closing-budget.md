# Closing budget: settled spend, safe reserves, the protected review (Q39-06, R40-03)

How forge keeps the REQUIRED closing review affordable: the
implementation phases cannot consume the allowance the review needs,
unresolved spend is reserved at a worst-case envelope (never a lower
bound, never a streamed subtotal), and a budget-blocked review recovers
through an explicit, auditable operator action — never a hidden retry.
Module: `forge.adaptive.closing_budget`; the exposure fold and cap
check: `forge.adaptive.usage_ingestion.exposure_fold` /
`spend_cap_check`; tests: `tests/test_closing_budget.py`,
`tests/test_usage_ingestion.py` (issues #325 and #339; the useful-WIP
and combined-steering traces both ended `blocked (budget_exhausted)` at
the reviewer leg after the candidate and independent CI had succeeded —
the guard was right, nothing protected the closing review).

## The exposure model — finality settles, never value presence (R40-03)

`spend_cap_check()` classifies every ingested receipt by its FINALITY
marker, never by whether it happens to carry a cost figure, and reports
— and the hard cap consults — DISTINCT numbers, never folded into one:

| quantity | meaning |
| --- | --- |
| `settled_usd` | FINAL rows' costs. A settlement releases its interval's envelope exactly once (a replayed identical final is a no-op by the natural-key contract). |
| `accrued_unsettled_usd` | the NONFINAL rows' streamed subtotals — a streaming provider's intermediate figures. Observed and reported, never treated as settled. |
| `unknown_lower_bound` | the unresolved intervals' floors (a partial's own subtotal is its floor) — never zero when something is known, but a lower bound can never bound spend from ABOVE. |
| `retained_liability_usd` | the worst-case retained ENVELOPE over the unresolved intervals: each contributes `max(its finite upper envelope, its accrued cost, its lower bound)`. |
| `settlement_release_usd` | the net envelope headroom the settlements returned — the envelope each settled row would still fence as nonfinal, minus its settled cost. |

Unresolved intervals are nonfinal rows (partials, whatever cost they
already carry) AND final rows that never reported a cost. Each retains
`max(finite upper envelope, accrued cost, lower bound)` — the accrued
subtotal rides INSIDE the envelope, never added on top and never
released by it.

The hard cap refuses when `settled_usd + retained_liability_usd +
projection_usd > cap_usd`. The P01 counterexample (issue #339) cannot
pass: cap 10, settled 8, a partial at 0.5 inside an envelope of 3,
projection 1 — the pre-R40-03 value-presence math "allowed" 9.5 while
the bounded exposure is 8 + 3 + 1 = 12, so the corrected check refuses
(exposure 11 before the projection).

- An unresolved interval with NO finite upper bound — neither its own
  `cost_upper_bound_usd` nor the policy ceiling
  (`unknown_interval_ceiling_usd`), a nonfinal row that already carries
  a cost included — is a typed `unbounded-exposure` finding that sets
  `requires_bounded_policy` and BLOCKS the next chargeable action until
  an explicit bounded-policy ceiling is supplied or the interval settles.
- An upper bound BELOW the interval's observed accrued cost is a typed
  `incoherent-bound` inconsistency — surfaced as its own finding/field,
  never free headroom (the interval still retains at least its accrued
  cost).
- NaN/infinite/negative caps, projections, ceilings, costs and bounds
  surface as typed findings and refuse — never silent defaults (`+inf`
  as the cap is the deliberate un-bounded fold arm the closing report
  uses when no cap is configured, and is not a finding).
- Monotonicity: a newer partial with a higher subtotal never reduces
  liability; an out-of-order older partial cannot erase it; a final
  settles exactly once and releases the envelope exactly once.

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
  `lower_bound_usd`, `reserved_liability_usd`, `unknown_intervals` —
  extended by the R40-03 finality split: `settled_usd`,
  `accrued_unsettled_usd`, `retained_liability_usd`,
  `settlement_release_usd`, `unbounded_intervals`;
- the visible reserve (`budget.closing_reserve`), the unresolved
  upper-bound count (`budget.unresolved_upper_bound`) and the phase
  exhaustion flag (`budget.phase_exhaustion`).

If the reserve is INTACT (the implementation phases left it
un-consumed), the run stays in the precise NON-READY `reviewing` state
with the reserve visible; only a reserve that cannot cover the review
parks `blocked` with the shortage named. A standing unreleased block
guards the leg: scanner re-drives stand down instead of re-attempting
the paid review call — never a hidden retry.

## Review-only continuation + the explicit amendment

```
# the native spelling (#340 — one DISTINCT axis, the originating
# command's identity; both provider legs):
service.continue_review_only(run_id, operator="human:<who>",
                             command_id="run:continue_review:<project>:<note-id>",
                             axis="calls", amount=1,
                             reason="close the promised review")
# the pinned #325 spelling (compat — a usd top-up whose command
# identity is the legacy derived key):
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
required fresh verification.

The amendment is applied BEFORE the re-drive through the ONE durable
`budget_amendments` table ([ADR-0034](../adr/0034-execution-ownership-map.md)
§2): keyed by the ORIGINATING native command identity — two identical
amount/reason commands are two decisions, a redelivery of one applies
exactly once, across refusal cycles too. Count-axis amendments move the
enforcing `run_budgets` limits atomically (re-opening an exhausted
guard); usd amendments raise the closing gate's effective cap. The
native spelling without a command identity is refused typed
(`amendment_requires_command_identity`); the legacy top-up spelling
aliases onto the usd axis with the legacy derived key as its command
identity, preserving its replay semantics at the table.

## Observability

- `budget.closing_reserve` — the held allowance, on every decision;
- `budget.unresolved_upper_bound` — unresolved intervals with no finite
  envelope;
- `budget.phase_exhaustion` — the implementation phases consumed the
  reserve / the closing review does not fit;
- `budget.accrued_unsettled_usd` — the nonfinal rows' streamed
  subtotals (provisional accrued, never settled);
- `budget.retained_liability_usd` — the envelope still fenced for the
  unresolved intervals;
- `budget.settlement_release_usd` — the envelope headroom settlements
  returned (exactly once each);
- `budget.unbounded_intervals` — unresolved intervals with no finite
  envelope (the `unbounded-exposure` arm);
- `delivery.review_only_recovery` — the review-only continuation's
  outcome (including the typed staleness refusals).
