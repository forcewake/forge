# Design-partner pilot LADDER — 2026-09-24

Issue **#293 / R37-12**: execute a design-partner pilot rather than
another laboratory substitute. The review's basis was correct and this
report does not dispute it: the lab pilot (#275) validated integration
seams, not customer demand — its 12/12 says nothing about whether a
customer would use or pay for the workflow.

## The honest boundary (read this first)

A design-partner pilot needs a **NAMED external customer** — a
buyer/user plus a code owner — and only the maintainer can recruit one.
The machinery cannot fabricate a customer, and nothing here pretends
otherwise:

- the shipped contract carries `customer: pending-recruitment` (the
  honest state, validated as such);
- the `supervised-batch` and `operational-sample` stages **fail
  closed** while the customer, the code owner's task-set approval and
  the OBSERVED baseline are pending (`LearningContract.validate_for_stage`);
- the decision-review template ships **unfilled**; the adoption
  verdict is forced to `unknown-pending-partner` until a partner
  exists, because nobody has been asked.

The maximum honest executable NOW is therefore: the staged-ladder
machinery, the customer-baseline measurement format, the
decision-record machinery, and the ladder's first stages executed
against the LAB (the machinery-validation leg). The external-partner
execution is the explicitly-recorded next step, not a claim.

## The ladder contract

`src/forge/adaptive/pilot_ladder.py` implements the staged contract:

```
contract → one-observed-task → supervised-batch → operational-sample
```

- **Gates between stages** — ordering, stop-rule clarity, the
  contract's per-stage prerequisites and the budget state. A stage may
  not start while a stop rule is tripped; a tripped rule preserves the
  verbatim state with its digest (`PilotLadder.trip_stop`) and the
  ladder is never silently retried.
- **Stop rules** (closed vocabulary): `authority_violation`,
  `budget_overrun`, `stop_rule_outcome`, `customer_stop`. The budget
  rule is enforced, not hoped-at: recorded spend past the contract cap
  trips the stop — "no next paid batch starts automatically" is a gate
  predicate. The kit (`pilot.py`) gained the matching
  `VIOLATION_BUDGET_OVERRUN` stop so a pilot tracker halts on it too.
- **The customer baseline** (`CustomerBaseline`) — the customer's
  ACTUAL workflow measured on four separate typed axes
  (assisted-coding minutes, reviewer minutes, ops-intervention
  minutes, waiting minutes), each with provenance `observed | pending`.
  An **authored** baseline is refused outright (the lab pilot's
  authored baseline was valid for ITS scope; the partner ladder
  requires observed), and a pending axis never advances the ladder.
- **The decision record** (`DecisionReview`) — continue / narrow /
  redesign / stop, with reasons and retained evidence pointers, and
  two SEPARATE typed assessments: engineering feasibility
  (`demonstrated | partial | not-demonstrated`) and adoption
  willingness (`willing | undecided | unwilling |
  unknown-pending-partner`). The final decision separates feasibility
  from adoption willingness; they are never blended.
- **Observability** — `pilot.accepted_tasks`, `pilot.manual_rescue_count`,
  `pilot.customer_review_minutes`, `pilot.abandoned_tasks`,
  `pilot.stop_reason`, `pilot.continuation_decision` (the issue's
  named gauges, folded from the records; customer review minutes are
  `null` — unknown, never zero — until a partner review exists).
- **Replay determinism** — the ladder state
  (`forge.partner-pilot.ladder-state/1`) round-trips byte-identically.

The shipped artifacts live in `evaluation/pilot/partner-pilot-v1/`:
`contract.json` (the narrow learning contract: task eligibility,
acceptance by the independent oracle, available data, the $40 spend
cap with per-stage caps — $1 for the observed task, prohibited
operations, stop rules), `tasks.json` (the staged task set: ONE
observed task, a 5-task supervised batch, a 14-task operational sample
covering ambiguity, a neighbor dependency, intervention, a controlled
infrastructure failure, a customer runbook recovery and one
human-rejection-despite-green-CI task), `stages/*.json` (the stage
records), `ladder-state.json` and `report.json`.

## Stage 1 (one observed task) — outcome: PENDING-LAB

`scripts/run_pilot_stage.py --stage one-observed-task` runs a
read-only preflight (reusing the #287 inventory probe layer verbatim)
whose arms are OBSERVED, never inferred: the lab aligned to its
intended profile, the model gateway reachable, the numerical budget
caps configured, the stage spend cap bounded.

Executed on 2026-09-24 against the real lab, the preflight observed
the lab **mid-alignment** (the #289 alignment work was in flight).
The latest observation (the ledger keeps every earlier one): version
0.36.0, schema 027, lane template ref and budget caps all MATCH, but
two checks keep the verdict misaligned — the app container still runs
a non-promoted image digest, and the installed lane wheel is not
observable from the control host (it installs per CI job on the
runner; runbook §6 names the observation path). A paid observed task
against a misaligned lab would qualify nothing, so the stage record is
honestly `pending-lab` with exactly those preconditions and their
runbook resolutions (`docs/operations/lab-alignment-runbook.md`);
re-running the same command re-checks every arm.

Had the preflight passed, the runner would have executed ONE live task
end-to-end through the qualified path (the lab's real services + the
real model via the gateway, spend bounded at $1.00, disposable
project): file the @forge issue, watch the control plane pick it up,
approve the plan with a NAMED approver's note (the runner refuses to
self-approve a human gate), wait within the wall-clock bound, then
judge acceptance by the INDEPENDENT ORACLE — the verification job
green on the candidate sha plus the contracted content check on the
candidate branch. PR/MR creation is recorded and is NEVER acceptance.
Usage receipts ride the stage record in the #276 ledger shape; spend
stays `unknown` until the wire reports it — never zero.

## The ladder state as shipped

| stage | status | the exact blocker |
|---|---|---|
| `contract` | **complete** | — (validated, digest frozen, customer state stated) |
| `one-observed-task` | **pending-lab** | lab mid-alignment: the app image digest is not the promoted one; the lane wheel is unobservable from the control host |
| `supervised-batch` | **pending-partner** | named customer; task-set approval by the code owner; OBSERVED baseline |
| `operational-sample` | **pending-partner** | the stages above |

## What remains (the explicitly-recorded next steps)

1. **Recruit the named design partner** (maintainer's act) — the
   buyer/user and the code owner; re-freeze the contract with the
   customer named (a new contract digest).
2. **Approve the task set** — the named code owner approves
   `tasks.json` (an acceptance criterion, not a formality).
3. **Measure the customer's baseline** — all four axes, observed, with
   their sources and window; an authored number is refused.
4. **Finish the lab alignment (#289)** and re-run
   `scripts/run_pilot_stage.py --stage one-observed-task` — the
   machinery leg, bounded at $1.00.
5. **Run the partner stages under supervision** — the supervised
   batch, then the 12–20 task operational sample, with ambiguity,
   neighbor dependency, intervention, one controlled infrastructure
   failure, one customer runbook recovery and one human rejection
   despite green CI — every task, attempt, rejection, abandonment and
   rescue recorded, failures and drops staying in the ledger and the
   budget.
6. **Hold the decision review** — fill the template in `report.json`:
   continue / narrow / redesign / stop, with reasons and retained
   evidence, feasibility and adoption judged separately.

## Where the machinery is tested

`tests/test_pilot_ladder.py` (the ladder, the contract gates, the
baseline provenance enforcement, the stop rules, the budget cap, the
decision record's shape and forced-honesty rules, the report's
honest-status rendering, the runner's preflight arms — aligned /
misaligned / caps-absent / gateway-down each producing a pending-lab
record with the exact reasons — and the replay determinism) plus the
budget-overrun and spend-fold additions in `tests/test_adaptive_pilot.py`.
