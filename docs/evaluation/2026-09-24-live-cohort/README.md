# Live research cohort — 2026-09-24

First LIVE cohort for the research-planning evaluation (R36-12 / issue #271).
Unlike the authored demonstration cohort of
[`2026-09-23-research-cohort`](../2026-09-23-research-cohort/README.md) — whose
fixtures validate the GRADING machinery — this cohort's runs were CAPTURED:
real tool observations and completions produced by the production research
paths against frozen snapshots, under a contract that was pre-registered
BEFORE the first capture. The artifacts live in
`evaluation/research_cohort/live/`; the machinery is
`src/forge/adaptive/research_cohort.py` (pre-registration, blinding, report)
and `src/forge/adaptive/research_cohort_live.py` (the capture driver).

## What is measured — and what is pending

Measured NOW (mechanical, replayed from the stored artifacts):

- every run's provenance, budget and spend — `offline-scripted-model` for
  all arms (no lab gateway was configured at capture time; see below);
- the mechanical grading dimensions: impacted-surface recall (against each
  task's frozen ground truth, including the spurious-surface penalty when a
  plan drags an irrelevant repository in) and per-claim support verdicts
  (semantic / syntactic-only / invalid, re-derived from the snapshot bytes);
- integrity: snapshot digests re-derive, runs bind to the pre-registered
  digests and generation, budgets equal the pre-registered budget, the
  per-mode cost totals sit under the pre-registered cost bound, and
  `live-model` / `offline-scripted-model` results are never pooled.

PENDING (human, and therefore unknown — never estimated):

- claim importance, assumption severity, invented-default flags and
  question specificity — reviewer grades that land through the blind
  package's rubric forms;
- human plan-correction minutes — one procedure for every mode, measured
  by the reviewer, `unknown` until recorded;
- the promotion decision itself. The replayed verdict is **HOLD** with
  `evidence_class: live-cohort:offline-scripted-model`, and it CANNOT move
  off HOLD until the review lands and the human promotion decision is
  recorded: the reviewer-graded dimensions and the overall stay `null`
  while review is pending, and a machine PASS over live evidence never
  promotes itself.

No performance claim is made from this cohort. The offline-scripted
captures prove the CAPTURE path (a reactive script over the real
`run_research_pass` / `_execute_call` loop); they are not model
performance. A `live-model` cohort replaces the script with the lab
gateway (see below) and is reported separately — never pooled.

## The pre-registration

`evaluation/research_cohort/live/preregistration.json`
(`forge.research.preregistration/1`, generation 1) froze, before capture:

- the task set with each task's snapshot digest and its task-specific
  acceptance criteria (the review bar cannot move after capture);
- the eligibility rules — reviewer is a code owner independent of the
  capture author;
- the budget every arm runs under (10 tool calls / 90 s wall) and the
  cost bound (≤ 60 proposed calls per mode over the promotable tasks);
- the arms (`none` / `lexical` / `research`) and the frozen
  prompt/policy digest (the research system prompt, the lexical probe
  policy, the plan-synthesis policy);
- the review procedure — blind, counterbalanced (seed recorded), one
  correction-effort procedure for every mode, evidence checker as a
  consistency check only;
- the promotion criteria — improvement margin 0.05, accuracy floor 0.8,
  no additional invented defaults, inside the cost bound.

The document is hash-recorded; loading re-derives the digest and refuses a
mismatch. Any change between iterations is a NEW generation (`revise`,
with `supersedes` + change reason); captures stay bound to the generation
they ran under, so mixing generations inside one cohort is an integrity
violation, never a pooled improvement. This is the optimization-leak
prevention R36-12 asks for, made mechanical.

## The tasks

Five tasks over the SAME frozen snapshots the v1 fixtures own (shared from
`evaluation/research_cohort/snapshots/`), all four archetypes among the
promotable four plus one held-back reserve:

| Task | Archetype | Snapshot | The trap |
|---|---|---|---|
| RC-08 neighbor deadline policy | `neighbor_dependency` | `snap-offline-neighbor.json` | the decisive policy lives only in Billing, at a non-initial window |
| RC-09 refund grace window | `ambiguous_requirement` | `snap-checkout.json` | the grace-window decision is missing; the graded behavior is a specific question |
| RC-10 irrelevant docs site | `irrelevant_repository` | `snap-checkout.json` | the docs site is authorized but irrelevant; the research run read it and the synthesized plan dragged it into the surface — the spurious penalty is in the report |
| RC-11 deep lapse handler | `deep_file_evidence` | `snap-checkout.json` | the lapse handler sits at line 24 of the handlers module |
| RC-12 holdout neighbor retry (held back) | `neighbor_dependency` | `snap-dispatch.json` | graded but reported separately; never moves the verdict |

## How each arm was captured

`capture_arm(preregistration, task, mode)` drives the REAL paths against
the frozen snapshot:

- `research` — the actual `run_research_pass` loop over one frozen
  `SnapshotToolbox` per authorized repository (the discovery stage's own
  construction). No gateway env was configured, so the model was a
  REACTIVE SCRIPTED investigation (`ScriptedInvestigation`, the RC-08
  recipe): fixed first-turn calls, then a turn that REACTS to the grep /
  symbol observation the harness fed back by paging a read window around
  the matched line, then an honest done turn. Every observation in the
  recordings is what the tool machinery actually returned.
- `lexical` — the discovery stage's deterministic probes
  (`extract_keywords` + `find_symbol`/`find_references`), zero model calls.
- `none` — planner input with discovery off; the plan rests on the
  statement alone and its emptiness is graded.

The captured PLAN is synthesized deterministically from what the run
established: claims cite the findings' anchors with asserted content read
from the snapshot's actual bytes; the surface is what was read. No
reviewer grades are fabricated. With a lab gateway configured
(`FORGE_RESEARCH_LIVE_GATEWAY_URL` + `FORGE_RESEARCH_LIVE_GATEWAY_MODEL`,
the OpenAI-compatible litellm endpoint the planner uses) the same call
executes for real under the pre-registered hard caps and stamps
`live-model` with the model/route identity — an unlabelled live run (URL
without a model) is refused outright.

Re-capturing reproduces every checked-in recording byte-for-byte; the
regeneration test lives in `tests/test_research_cohort_live.py`.

## The blind review package

`evaluation/research_cohort/live/review-package.json`
(`forge.research.blind_review/1`) holds one anonymized unit per promotable
(task, arm): an arm-neutral id, the task statement, the FROZEN acceptance
criteria, the anonymized plan (evidence refs, importance, severity and
specificity stripped — they are reviewer grades) and an UNFILLED rubric
form. Task blocks are ordered by the recorded seed and unit positions
rotate per task, so no position correlates with an arm;
`arm_inferrability_scan` finds nothing to unblind from (asserted by the
tests). Reviewer grades land as structured records via
`merge_review_grades`, which unblinds with the recorded seed and writes
the human decisions onto copies of the run documents — a replay reads
them verbatim and never rewrites them. A missing reviewer leaves
correction effort UNKNOWN, never estimated.

## How to replay offline

```bash
uv run python - <<'PY'
from pathlib import Path
from forge.adaptive.research_cohort import CohortRunner

root = Path("evaluation/research_cohort")
runner = CohortRunner.from_directory(
    root / "live" / "cohort-live-v1.json", root / "snapshots", root / "live" / "recorded"
)
report = runner.run()
print(report.verdict, report.document["promotion"]["evidence_class"])
report.write(Path("live-report.json"))
PY
```

The replay is a pure function of the stored artifacts — no model, no
network — and reproduces the checked-in `report.json` exactly. Layout:
`preregistration.json` and `cohort-live-v1.json` beside each other
(the loader picks the preregistration up automatically),
`recorded/<task>/<mode>.json` per arm (each with its `capture` block),
`review-package.json` and `report.json`. The snapshots are shared with
the v1 fixtures directory. Once the review lands, the filled forms are
merged with `merge_review_grades`, the updated run documents are written
back, and a `promotion-decision.json`
(`{"decision": "accept"|"reject"|"hold", "decided_by": …, "recorded_at": …}`)
inside `recorded/` records the human call the replay then carries.

## Honest limitations

- **Nothing here is model performance.** The captures are
  `offline-scripted-model`: they prove the capture, blinding and reporting
  path end to end. A performance claim needs a `live-model` cohort under
  the same contract — reported separately, never pooled with this one.
- **The review has not happened.** Every reviewer-graded number is
  pending (`null`), the correction effort is unknown, and the verdict is
  HOLD. Sample size: 4 promotable tasks, 3 held-back-in-reserve is 1 —
  far too small for any recommendation; the outcome of this cohort is
  the working measurement path itself.
- **The plan synthesis is a policy, not a planner.** The captured plans
  are derived deterministically from what each run established; a real
  planner's plans replace them in a partner cohort, under the same
  pre-registration.
- The changed-contract sensitivity arm (the mutation hook) is the v1
  cohort's machinery and carries forward unchanged; it is not re-shipped
  here while the review is pending.
- Goodhart and the J-curve caveats of the v1 cohort apply unchanged:
  once this rubric drives optimization, expect gaming; re-record a fresh
  cohort (new preregistration generation, fresh holdouts) before any
  promotion decision that matters.
