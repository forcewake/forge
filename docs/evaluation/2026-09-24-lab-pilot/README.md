# Lab-recorded operational pilot — 2026-09-24

Issue **#275 / R36-16**: execute a bounded design-partner pilot with
pre-agreed acceptance and stop rules. The pilot KIT
(`src/forge/adaptive/pilot.py`, R32-21) froze the contract, the task
range and the stop rules; what this run adds is an EXECUTED pilot — the
operating experiment, recorded by the pilot machinery end to end.

## Why a LAB pilot and not a partner pilot (the honest boundary)

The lab GitLab CE qualification (#268) REFUSED its own live flow, for
reasons this pilot inherits as its live blockers:

1. the deployed control plane reports **0.28.0** while the profile pins
   the promoted wheel **v0.35.0** (a paid flow against another build
   qualifies nothing);
2. **no budget caps** exist on the lab project (no `FORGE_BUDGET_PROFILES`,
   no `--max-budget-json` — an uncapped paid run is not qualification);
3. the GitLab **dispatch seam carries no lane-resume contract**
   (`FORGE_LANE_RESUME`/lane-control credentials are GitHub-only,
   R32-04; profile `gitlab-ce-v1.md` §8).

A paid live pilot against that lab would qualify nothing. The honest
executable pilot is this **lab-recorded operational pilot**: real task
execution driven through the offline production-entry-grade seams, with
the pilot machinery recording everything a partner pilot would record,
and the evidence class explicitly bounded.

`evidence_class: lab-operational` — the verdict below is a statement
about operating the seams, never about partner outcomes.

## What ran for real

Every task in the pilot executed on the same seams the
`tests/production_entry/` layer drives (reused BY IMPORT from the
runner — `scripts/run_lab_pilot.py`):

- real git checkouts at a frozen base;
- the REAL lane subprocess (`python -m forge.lane_driver --driver codex`)
  whose vendor is the controlled scripted executable speaking the REAL
  codex app-server JSON-RPC wire (token counts are real wire
  observations: 518 in / 231 out per turn);
- the SHIPPED collector subprocess
  (`python -m forge.harness_entry --collect-candidate`);
- the REAL `capture_wip`/`restore_wip(promote="generation")` checkpoint
  machinery uploaded/downloaded over real HTTP through the lane-control
  + checkpoint-channel routers on uvicorn;
- real durable sqlite state (a "restarted worker" is a genuinely fresh
  engine/session factory over the same rows);
- the REAL `GitHubRunService` + `GitHubClient` over real HTTP against
  the fake native server's recorded dispatch ledger (AT-02);
- the REAL durable control surface (`OperatorControlService` over
  `PostgresMailbox`: steer / pause / resume / answer command rows).

## The pilot (pre-agreed, frozen in the spec)

- **Spec**: `evaluation/pilot/lab-pilot-v1/spec.json`
  (`forge.pilot.spec/1`) — ONE narrow workflow (offline adaptive
  delivery on the production-entry seams), the LAB as the named
  environment (explicitly NOT a design-partner customer;
  `provenance_labels: [lab-operational-pilot, evidence-class:
  lab-operational, scripted-vendor, no-paid-models]`), an UNPAID fee
  note (the fee-as-demand-test is honestly unexecuted), the four-stage
  ladder, three criteria from the kit's closed vocabulary, the window
  2026-09-22..2026-09-23, an AUTHORED lab baseline (see limitations).
- **Tasks**: `evaluation/pilot/lab-pilot-v1/tasks.json` — 12 tasks:
  ordinary change ×3 (test-repair, cold-reinstall, adaptive-idle),
  ambiguity/question-first ×2, neighbor-dependency ×2, operator
  intervention ×2 (steer; pause+resume), infrastructure failure +
  retry ×2 (runner loss with fresh re-dispatch; revoked checkpoint blob
  with a loud halt), cross-runner resume ×1. Every kit scenario tag is
  exercised.
- **Acceptance per task**: the AT-01..AT-06 seam outcomes (files,
  dispatch modes, checkpoint identities — the production-entry
  assertions), executed as independent mechanical checks over
  artifacts (diffs that apply cleanly onto the frozen base, candidates
  that compile, dispatch ledgers, checkpoint ids, generation pointers),
  plus the contracted verification checks from the task definitions.
  Acceptance is recorded by the lab operator on those check outcomes —
  never inferred from PR creation or a model summary (none exist).
- **Human-decision points**: recorded as PENDING where a human would
  decide, then with the actual decision made (the question asked and
  answered, the steer text, the pause/resume/retry calls, the
  acceptance verdict) — see each task's `operator_decisions` in
  `records/<task>/task-evidence.json`.

## How to run / replay

    uv run python scripts/run_lab_pilot.py --out evaluation/pilot/lab-pilot-v1/
    uv run python scripts/run_lab_pilot.py --rebuild-from evaluation/pilot/lab-pilot-v1/

The rebuild reproduces `report.json` byte-identically from
`records/tracker-snapshot.json` alone (records precede the decision by
construction). Stop rules are enforced after every task: any scope or
authority violation preserves diagnostics verbatim and halts the
remaining tasks (`records/run-state.json` carries the stop reason).

## The outcomes (2026-09-24 run)

All 12 tasks ran; **12 accepted, 0 unaccepted, 0 blocked** — per-task
seam checks and evidence under `records/`. The five metrics against the
frozen (authored) baseline, and the 2-of-3 verdict AT THE LAB SCOPE,
live in `report.json`; `lab-bounding.json` carries the evidence-class
frame:

- `autonomy_rate` **1.0** (no human code change on any deliverable);
- `intervention_rate` **0.4167** (5 of 12 tasks: two question-answers,
  one steer, one pause+resume, one cross-runner resume);
- `cost_per_accepted_usd` **unknown** — no paid models ran; every
  attempt's spend is recorded `null` with `cost_state: "unknown"` in
  the #276 ledger shape (`usage_ledger` per task), so the cost
  criterion is UNJUDGEABLE and is named as such, never failed;
- `cycle_time_vs_baseline` ≈ 0.002 against the authored 30-minute lab
  baseline — bounded by that authorship, never headlined;
- reviewer load = the measured independent-verification time per task.

**Verdict: 2-of-3 met** (autonomy, intervention-bounded) → the kit's
decision math says **expand**. Bounded to `evidence_class:
lab-operational`, the operative reading is **HOLD→narrow-expand on the
same seams**: the seam machinery demonstrably supports the full task
shape (including the unhappy paths), so the next product decision is a
REAL paid design-partner pilot on the qualified profile once the #268
live blockers clear — not more lab evidence of the same class.

## Limitations (the honest section)

- **No paid models.** The vendor is the controlled scripted executable
  on the real wire; token counts are real observations but spend is
  unknown — never zero — and the cost criterion stays unjudgeable.
- **Scripted vendor, scripted causality.** The scripted vendor performs
  the scenario's edits; an operator steer's causal effect on the
  deliverable cannot be demonstrated (the vendor's turn also completes
  faster than any poll cadence, so the mid-turn steer delivery lands
  as the journal's honest `error` outcome — the durable command row,
  the real drain over HTTP and the journal state are the evidence).
  A landed steer needs a real vendor.
- **Lab infrastructure.** The native surface is the fake native server
  (real process, state surviving worker death — but not GitHub or
  GitLab); the database is sqlite; the baseline is AUTHORED (the
  operator's planned manual time), not the partner's own frozen
  pre-pilot numbers.
- **No customer.** The decision owner is the maintainer as lab
  operator; no buyer/user problem was agreed with a customer, no fee
  was paid, and no customer decided usefulness. The window is one
  execution day, not 4–8 weeks — no J-curve, no adoption learning.
- **A partner pilot remains the next step**, on the qualified profile,
  after the #268 blockers (control-plane version pin, budget caps,
  GitLab lane-resume dispatch parity) clear.

## File map

| Path | What it is |
|---|---|
| `evaluation/pilot/lab-pilot-v1/spec.json` | the frozen learning contract (`forge.pilot.spec/1`) |
| `evaluation/pilot/lab-pilot-v1/tasks.json` | the 12 task definitions + per-task driver scenarios |
| `evaluation/pilot/lab-pilot-v1/records/<task>/` | per-task evidence: seam checks, candidate diff, vendor event log, usage ledger, operator decisions |
| `evaluation/pilot/lab-pilot-v1/records/tracker-snapshot.json` | the verbatim records the report rebuilds from |
| `evaluation/pilot/lab-pilot-v1/records/run-state.json` | the stop reason + rebuild inputs |
| `evaluation/pilot/lab-pilot-v1/report.json` | the kit report (`forge.pilot.report/1`) |
| `evaluation/pilot/lab-pilot-v1/lab-bounding.json` | the evidence-class bounding document |
| `scripts/run_lab_pilot.py` | the runner (seam reuse by import from `tests/production_entry`) |
| `tests/test_lab_pilot.py` | the runner's machinery tests |
