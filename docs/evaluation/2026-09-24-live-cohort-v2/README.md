# Live cohort v2 — 2026-09-24

The R37-11 (issue #292) deliverable: a **genuinely live planning
comparison** — every arm of the research-quality cohort captured through
the REAL planner path with the REAL model, under a pre-registered
generation-2 contract — with the blind human-review package prepared and
honestly **pending**: the machine cannot grade what only humans may.

Artifacts: `evaluation/research_cohort/live-cohort-v2/` (the frozen
`preregistration.json` generation 2, the `cohort-live-v2.json` spec, the
`snapshots/` frozen bytes, the `recorded/` captured arms, the
`capture-log.json` per-arm outcomes and spend, the `report.json`, the
`review-package.json`). Machinery: `src/forge/adaptive/research_cohort_live.py`
(the live capture driver, the spend ledger, the plan-synthesis seam) and
`src/forge/adaptive/research_cohort.py` (preregistration, grading, report);
definitions, writers and tests: `tests/test_research_cohort_live.py`.

## What is new relative to gen-1 (#271)

Generation 1 captured every arm `offline-scripted-model` with
deterministic plan synthesis and null human grades — correct then, HOLD.
Generation 2 supersedes that contract (recorded `supersedes` digest,
`change_reason` names exactly what changed) and re-captures:

- **the REAL model in EVERY arm.** Under a live provider the PLAN is
  synthesized by the model in the `none`, `lexical` AND `research` arms —
  only the discovery phase differs (nothing / deterministic identifier
  probes / the real `run_research_pass` investigation loop). The captured
  comparison is planner-MODE, not model.
- **the #290-style hard spend accounting.** Every model call rides one
  shared `SpendLedger`: worst-case dollar projection BEFORE the provider
  is contacted (refuses to start rather than over-spend), a receipt AFTER
  it from the usage the gateway reported, unknown usage charged at the
  worst case — never zero. Caps: 12 tool calls / 300 s wall / 3000 tokens
  per research call / 8000 tokens per synthesis, **$0.40 per arm, $3.00
  total** (structurally enforced: the preregistration refuses a
  `max_usd_total` above 3.0).
- **failures preserved, never pooled, never zeroed.** A synthesis that
  fails, truncates or hits a cap stays in the record with its receipts
  and an honest `stopped_reason` (`spend_cap`, `gateway_error: …`,
  `malformed_or_invalid_plan_json`); live-model and offline-scripted
  results are never pooled (per-mode provenance is uniform and checked).

## What actually ran live

Model `fast` (`openai/glm-5.3-flash`) through the lab litellm gateway at
`http://localhost:4000` (the same endpoint and model identity the R37-09
discovery-live run used; reachability-checked before capture). One
completion per research turn, one per plan synthesis; 38 gateway calls
in total across the cohort, each with a usage receipt.

15 arms captured — 5 tasks × {none, lexical, research} — **15/15 graded**
(no failed attempts this run), 4 of the 5 research investigations
stopped honestly at `max_calls` (exhaustion retained as exhaustion:
`complete: false`, partial findings kept, plans synthesized from what
was actually read).

| mode | promotable recall (mechanical) | spend, all 5 tasks | research docs |
| --- | --- | --- | --- |
| none | 0.00 | $0.0902 | — |
| lexical | 0.50 | $0.1441 | — |
| research | 0.75 | $0.5388 | 1 complete, 4 exhausted at `max_calls` |

Total recorded spend **$0.773064** of the $3.00 hard cap (per-arm peak
$0.152, under the $0.40 per-arm cap). Two disclosures the receipts
cannot carry: a first capture attempt aborted on a gateway transport
timeout after roughly four arms (~$0.15, estimated — its in-memory
ledger died with the process; the crash is what motivated per-arm
streaming writes and gateway-error preservation in the synthesis seam),
and ~$0.17 of single-arm smoke tests validated the seam beforehand.
Total vendor spend across everything ≈ **$1.09** — inside the $3
ceiling, with the recorded $0.773 the only spend the report counts.

Mechanical grades only: evidence support verdicts and surface recall
compute against the frozen snapshot bytes; the reviewer-graded
dimensions (claim importance, assumption severity, question specificity,
correction minutes) are `None` everywhere — nothing estimated, nothing
fabricated.

## The verdict, and what waits for humans

The report's promotion verdict is **HOLD — pending human review**, and
that is the correct result today: every promotable graded run is
`review_pending` (12 named task/mode entries), the aggregate overalls
stay `None`, and the human promotion decision field is unrecorded. Under
the existing rule a machine PASS over live evidence without the human
decision caps at HOLD anyway — here the machinery itself has not even
computed a PASS, because the reviewer dimensions it would blend are
unknown.

`review-package.json` is ready for the humans: 12 anonymized,
counterbalanced units (seeded order recorded in the package; positions
rotate so no position correlates with an arm), each carrying the task
statement, the frozen per-task acceptance criteria and an **UNFILLED**
rubric form, with `review_status: pending-human`. The arm-inferrability
scan over the shipped package is empty (no arm-naming keys, values or
machinery substrings anywhere). Real reviewer identities and timestamps
cannot exist in this artifact — they land as structured records via
`merge_review_grades` when actual people fill actual forms, and until
then correction effort stays unknown, never zero.

## Sample limitations (read before quoting anything)

- The repositories are FIXTURE service repos (the discovery-live frozen
  trees), not customer repositories; the tasks are authored to exercise
  four archetypes plus a holdout, not a sampled workload.
- Five tasks, four promotable — small-sample descriptive numbers only;
  the mechanical recall column above is a bounded observation, not a
  statistical claim, and the review-pending dimensions have no numbers
  at all yet.
- One model, one gateway, one day; the research arms ride the same
  12-call budget a production run would, so the three `max_calls`
  exhaustions describe THIS budget, not the mode's ceiling.
- Prices are conservative over-estimates used to enforce the cap; the
  receipts carry the gateway-reported usage, not vendor billing.
- The grader is mechanical where it can be (citations, recall, caps) and
  refuses to be mechanical where it cannot (usefulness, severity,
  correction effort) — those wait for the named human reviewers the
  preregistration's eligibility rules require (capture author
  ineligible).

## Replay

`CohortRunner.from_directory(...)` over the shipped artifacts re-derives
the report byte-identically with zero model calls (`replay.live_calls:
0`), asserted by `tests/test_research_cohort_live.py`
(`TestShippedLiveCohortV2`), which also asserts: the generation-2
preregistration loads and supersedes generation 1 exactly, every shipped
run is `live-model` under generation 2, provenance never mixes, costs
match the receipts and the caps, the package re-derives from the
artifacts scan-clean and unfilled, and the verdict stays HOLD with the
human decision unrecorded.
