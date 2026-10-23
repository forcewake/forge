# Closing reserve and review-only recovery

Implementation cannot consume the allowance reserved for closing review.
The coder's effective USD ceiling is the run cap minus the closing reserve;
closing review uses the full cap. An explicit absolute reserve
`FORGE_CLOSING_RESERVE_USD` wins over
`FORGE_CLOSING_RESERVE_FRACTION`; the fraction resolves against
`FORGE_SPEND_CAP_USD`. The default fraction is provisional and needs actual
workload calibration. A missing reserve policy is reported, never silently
converted to zero or unlimited funds. Already approved ceilings stay frozen.

## Exposure

Finality, not presence of a number, decides settlement:

- `settled_usd`: final reported costs; duplicate final receipts settle once.
- `accrued_unsettled_usd`: streamed subtotals, observed but not settled.
- `retained_liability_usd`: each unresolved interval retains the maximum
  of its finite upper envelope, accrued cost and known lower bound.
- `settlement_release_usd`: envelope headroom released exactly once.

The hard cap refuses when settled cost + retained liability + projected
cost exceeds the cap. Subtotals stay inside their envelope, not on top of
it, and never release liability. Unknown spend is not zero. An unresolved
interval without a finite upper bound blocks the next chargeable action
with `unbounded-exposure`. Bounds below observed cost are incoherent and
retain at least the observed cost. Invalid negative/nonfinite numeric
inputs refuse; no silent fallback creates headroom. Later partials cannot
reduce liability, and older out-of-order receipts cannot erase it.

## Recover a budget-blocked review

The run records `review_budget_block` against its candidate and tested SHA,
with reserve, settled/unsettled exposure and the limiting axis. An intact
reserve can leave the run non-ready in `reviewing`; insufficient allowance
parks it blocked. An unreleased block prevents scanner-driven paid retries.

Both provider services expose `continue_review_only`. The operator names
one axis, an amount, a reason and the originating native command identity.
The durable amendment applies before continuation and changes the enforcing
limit atomically. Redelivery of one command applies once; two independently
issued commands remain two decisions. Missing command identity refuses.
The legacy USD top-up spelling retains its derived-key replay semantics.

Continuation reviews only the same candidate/tested identity: no coder,
commit or new implementation reservation. A moved live head or tested SHA
returns `review_shortcut_stale` and requires fresh verification. Never bypass
that fence with a re-plan or hidden retry.

Diagnostics expose reserve, phase exhaustion, unknown upper-bound count,
accrued cost, retained liability, released headroom and unresolved intervals.
Historical calibration measurements remain at original paths
`docs/operations/closing-budget.md` and
`docs/operations/budget-calibration.md` in the [archive](../archive.md).
They are not universal budget recommendations.
