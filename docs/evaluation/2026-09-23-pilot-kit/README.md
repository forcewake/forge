# Design-partner pilot kit — 2026-09-23

The bounded-pilot scaffolding for R32-21 (OPS-07): the one-page learning
contract, the bounded task set, the tracker that folds the five metrics
that hold up, the stop conditions, and the report. The module is
`src/forge/adaptive/pilot.py`; the example plan is
`evaluation/pilot/pilot-v1.json`. **This kit is scaffolding plus an
example spec — not a run pilot. No real design-partner tasks are
claimed; nothing here says a partner signed anything.**

## What the kit provides

| Piece | Where | What it pins |
|---|---|---|
| `PilotSpec` (`forge.pilot.spec/1`) | `pilot.py` | the one-page learning contract: identity + fee note, data boundary, unsupported features, 4-stage ladder, exactly 3 criteria, frozen window + baseline, review groups, exits |
| `PilotPlan` (`forge.pilot.plan/1`) | `pilot.py` + `evaluation/pilot/pilot-v1.json` | 12–20 tasks, every scenario tag at least once, staged groups in ladder order |
| `PilotTracker` | `pilot.py` | per-task operator recording (setup, corrections, rescues, latency, ALL attempts' usage, interventions, review minutes, defects/rollbacks) and the metric fold |
| `evaluate_stop` | `pilot.py` | continue / extend-once / expand / stop, with preserved diagnostics |
| `PilotReport` (`forge.pilot.report/1`) | `pilot.py` | per-task outcomes (unaccepted + rescues explicit), per-group review records, metrics vs baseline, 2-of-3 verdict, per-stage readiness, recommendation, limitations |

## The contract shape (research topic 04, the converged playbook)

- **Paid, short, single-outcome.** A fee note rides the spec (payment as
  the demand test); the window is 4–8 weeks; one business slice with ONE
  writable repo first — expansion is a gate decision, not a kickoff
  parameter.
- **Frozen baseline.** The spec carries the partner's own 60–90-day
  pre-pilot numbers (cycle time required; the DORA four verbatim for
  context). Validation refuses a baseline that was not frozen BEFORE
  kickoff — without it none of the five metrics compute.
- **Staged capability ladder.** `read_plan → small_fixes → branch_pr →
  gate`, exactly four stages in order. Full autonomy from week 1 is the
  documented failure mode; the report decides readiness PER STAGE (a
  stage is `ready_to_support` only when every one of its tasks ran, was
  accepted and needed zero manual rescues).
- **2-of-3 measurable criteria by a named end date.** Exactly three
  criteria, each with a metric from a CLOSED vocabulary, a threshold,
  a measurement method and an end date. Promotion is decided by ≥ 2 of
  3; the named end date is the decision date (`spec.decision_date` =
  the latest criterion end date).
- **Three exits only.** Expand / extend-ONCE-for-a-named-gap / stop.
  A second extension is a stop; an unnamed extension is refused.
- **Unsupported features recorded BEFORE onboarding.** The spec carries
  the explicit, versioned list; `record_onboarding(spec)` snapshots it
  with a timestamp; the report REFUSES an onboarding timestamp that is
  not strictly earlier than the first task start — the ordering is
  checked, never trusted.

## The metrics — and why THESE five

PR counts and acceptance rates are documented liars for agent fleets:
under AI, code volume and suggestion throughput are nearly free to
inflate, and suggestion-acceptance is a vendor engagement metric. The
kit computes only the five that hold up read together, plus the
reviewer-load capacity tax (definitions pinned in `METRIC_DEFINITIONS`,
published in every report):

1. **Autonomy rate** — tasks accepted WITHOUT a human code change ÷
   total. Merged-as-is semantics, watched for rubber-stamping via the
   reviewer-load counterweight.
2. **Cost per accepted task** — ALL-attempt spend (unaccepted attempts
   included) ÷ accepted count. A missing spend degrades the total to
   unknown — never zero.
3. **Defect/rollback rate** — (defects + rollbacks on agent-authored
   deliverables) ÷ accepted count. Quality lags speed by 8–12 weeks, so
   this number is revisited after the observation window, not just at
   the end date.
4. **Intervention rate** — tasks with ≥ 1 intervention ÷ total, with
   PARTIAL STEERING counted exactly like a full takeover (forge's own
   /steer semantics made a metric rule). Closed vocabulary:
   steer / pause / resume / cancel / takeover / question_answer.
5. **Cycle time vs the frozen baseline** — mean completion latency ÷
   the frozen pre-pilot cycle time (1.0 is parity).
6. **Reviewer load** (counterweight, never blended) — senior-reviewer
   minutes per task: the hidden tax that individual speedups pool into.

Provenance labels (human / ai-assisted / agent-authored) are part of the
spec from day one — retrofitting provenance is close to impossible.

## The stop rules

`evaluate_stop(tracker, spec)` checks, worst first:

1. **Authority violation** (an attempted automatic merge, a used
   production secret) → instant stop. The tracker snapshot is preserved
   VERBATIM with a digest pointer, and the stopped tracker refuses all
   further recording — the state is retained for reading, never
   silently retried.
2. **Scope violation** (a task touched a repo outside the data
   boundary — recorded automatically from each task's touched repos) →
   stop with preserved diagnostics.
3. **Before the named end date** → continue. The J-curve
   pre-commitment is structural: the measurement window was fixed in
   the spec BEFORE kickoff (and the spec cannot be retargeted once a
   task is recorded), so the pilot is never judged during the expected
   adjustment dip.
4. **At/after the end date** — the 2-of-3 math: ≥ 2 met → **expand**
   (gated on the first gate's single-writable-repo rule); otherwise one
   **extend-once** for a NAMED gap while the extension is available;
   otherwise **stop** with preserved diagnostics. Unjudgeable criteria
   (unknown metrics) count as not met and are named.

## How to instantiate with a real design partner

1. **Freeze the baseline first.** Collect the partner's 60–90-day
   pre-pilot numbers (cycle time at minimum) into a `PilotBaseline`; a
   re-recorded baseline is a different pilot and a new spec.
2. **Write the spec** (`PilotSpec`): one writable repo, neighbor read
   grants, the sentinel-only secrets scope, the honest
   unsupported-features list, the four stages, three criteria from the
   closed metric vocabulary with thresholds/methods/end dates, the
   window, review groups, the exits. `spec.validate()` refuses the
   contract-shaped holes (missing threshold, missing end date, empty
   boundary, empty unsupported list, production secrets, automatic
   merge).
3. **Record onboarding BEFORE kickoff** — `record_onboarding(spec)` →
   `tracker.record_onboarding(...)`; the report later proves the
   ordering.
4. **Load or write the task plan** (`PilotPlan`, 12–20 tasks, every
   scenario tag, staged groups). The shipped `pilot-v1.json` is a
   TEMPLATE: replace the acme/checkout fixtures with the partner's real
   repo, owners and verification contracts.
5. **Record as you run** — one `TaskRecord` per task, ALL attempts'
   usage (unaccepted included), every intervention as it happens,
   review minutes per task. Record honestly: the metrics degrade to
   unknown rather than zero when inputs are missing.
6. **Evaluate at the named end date** — `evaluate_stop(tracker,
   as_of=...)` and `build_pilot_report(plan, tracker, decision=...)`.
   The report is deterministic (same inputs, same bytes) and carries
   the recommendation, the per-stage readiness, and the limitations
   verbatim.

Weekly operator cadence runs per task group throughout, with
behavior-anchored questions ("show me how you handled this step last
week") — silence is never validation.

## Honest limitations

- The kit is scaffolding plus an example spec; `pilot-v1.json` is a
  TEMPLATE, not a claim — no real partner tasks ran and no partner
  signed the example contract.
- The tracker folds OPERATOR-RECORDED facts. Self-reported time
  savings is disqualified as a headline number; the tracker has nowhere
  to enter one.
- Defect/rollback observation windows outlive the pilot (8–12 weeks of
  lag); the end-date number is provisional by construction.
- The 2-of-3 verdict inherits the criteria's honesty: a criterion whose
  metric inputs are missing is reported UNJUDGEABLE, never quietly
  failed and never quietly passed.
- The kit does not wire the harness: it never contacts a repo, a lane
  or a model. Recording remains a human operator act on purpose — the
  five metrics are only as honest as the hand that records them, and
  the report's limitations section says so every time.
