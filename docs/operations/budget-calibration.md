# Budget calibration: measure first, then profile, never a model-granted ceiling (R42-06)

How forge calibrates task and harness budgets without weakening
verification or confusing token dimensions. Issue #379; the measured
basis: the #364 review-loop capture and the #377 build-once capture
(`docs/evaluation/2026-09-27-review-loop/`,
`docs/evaluation/2026-09-28-r4204-build-once/`) plus the #368 ledger v2
(`evaluation/economics/accepted-task-ledger-v2.json`). The report
artifact — rebuilt byte-deterministically by
`scripts/build_budget_calibration.py` — is
`evaluation/economics/budget-calibration-v1.json`.

The recorded basis, restated: the #364 live experiment raised the
standard token cap 200k→600k after the harness consumed nearly
everything and left no closing headroom. That is ONE useful observation
about ONE observed task shape, NOT proof every task needs 600k. The
planner schema had no `budget_class`, so the compiler default always
applied; a model may REQUEST more, never grant itself more.

## 1. The measurement (before any default changed)

Nine SDK lane receipts across the two captures, every one
Anthropic-shaped (DISJOINT counters — `input_tokens` already excludes
the cache, so the inclusive input is `input + cached`, the cache added
exactly once):

| capture | phase | job | input | cached | output | inclusive | rrf | cost | lane wall |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 09-27 | delivery | 1157 | 32,995 | 155,136 | 2,412 | 188,131 | 0.825 | $0.2112 | 91.1 s |
| 09-27 | round2 | 1162 | 19,997 | 173,184 | 4,169 | 193,181 | 0.896 | $0.1980 | 130.6 s |
| 09-27 | round3 | 1166 | 32,476 | 134,912 | 2,000 | 167,388 | 0.806 | $0.1969 | 79.8 s |
| 09-27 | round4 (negative) | 1169 | 35,144 | 183,744 | 3,106 | 218,888 | 0.839 | $0.2394 | 113.3 s |
| 09-27 | failed attempt `fbe62ad5` | — | 33,061 | 158,784 | 2,944 | 191,845 | 0.828 | $0.2229 | — |
| 09-28 | delivery | 1190 | 33,388 | 176,000 | 2,463 | 209,388 | 0.841 | $0.2180 | 115.1 s |
| 09-28 | round2 | 1196 | 17,762 | 149,888 | 1,967 | 167,650 | 0.894 | $0.1404 | 100.7 s |
| 09-28 | round3 | 1199 | 32,732 | 101,440 | 1,840 | 134,172 | 0.756 | $0.1880 | 94.8 s |
| 09-28 | conflict negative | 1212 | 21,943 | 198,144 | 3,543 | 220,087 | 0.900 | $0.1983 | 165.7 s |
| **total** | | | **259,498** | **1,431,232** | **24,444** | **1,690,730** | **0.847** | **$1.8131** | **891.1 s (lb)** |

What the numbers say:

- **The token axis measures CONTEXT, not task weight.** Every attempt —
  from the one-function delivery to the expired-premise negative —
  served a majority-cached ~167k–220k-token context. The redundant-read
  fraction (`context.redundant_read_fraction` = cached/inclusive) runs
  0.756–0.900, overall **0.847**: five of every six served tokens were
  a re-read of cached conversation. A task-class mapping that ignores
  this starves the closing review on a small cap regardless of how
  trivial the task is.
- **Usage coverage is named, never zero-filled** (`usage.coverage`):
  the #364 ledger window folds 7 SDK receipts over 8 attempts (0.875);
  the unreceipted attempt is the wrong-branch child `6e0fdf34` (the
  #361 publisher defect), whose figure never reached the record. The
  $0.223636 residual stays a BOUND attributed to that run
  (`usage.unresolved_liability`) — never a cost column.
- **Failed attempts and human corrections, named with their own spend**
  (not just the successful lane cost): the #364 root task took 7
  attempts — 3 failed delivery attempts (zero-spend bootstrap fence
  $0.0000; the budget fence `fbe62ad5` $0.2229; the empty-scope
  classification $0.1876) + delivery 1 ($0.2112) + rounds 2/3
  ($0.1980/$0.1969) + the blocked round 4 ($0.2394) — all-attempt
  provider-reported **$1.256064 EXACT**
  (`accepted_task.all_attempt_cost`; the human acceptance is still
  pending on draft MR !4). The #377 window adds its own drift-fenced
  negative ($0.1983). Successful human-correction cost: $0.14–$0.20 per
  round, every round cheaper than one failed delivery attempt.
- **The exhaustion event decomposed**: run `fbe62ad5` consumed
  196,312/200,000 on `standard@200k` — the SDK receipt's 194,789 tokens
  (cache added exactly once) plus 1,523 gateway planner/reviewer tokens
  on the SAME budget row. The window carried NO closing partition, so
  the coder consumed the whole axis and the closing review stood down
  with zero reviewer spend: the designed fence, working as built.

## 2. The dimensions table (per harness route)

Provider-specific conventions preserved (#339/#368): the two shapes
never mix into one fold — `forge.adaptive.usage_ingestion
.normalize_counters` decides the shape per receipt and computes the
inclusive input under the receipt's OWN convention. Cache reads are
never double-added to an inclusive input.

| route | token convention | dimensions reported | measured here | time dimension |
| --- | --- | --- | --- | --- |
| `claude-sdk-lane` | Anthropic-shaped DISJOINT: total = input + cache_read + cache_write + output (cache added exactly once) | input, cached, cache_write, output, per-model usage (costUSD, contextWindow), 1 call per attempt, SDK cost | yes (9 receipts above) | lane job wall window from the trace (lower bound); `FORGE_LANE_BUDGET_SECONDS` on the lane's own clock |
| gateway (planner/reviewer via litellm) | the durable `run_budgets` exposure is INCLUSIVE (consumed + reserved + unresolved); gateway turns ride the SAME run budget as lane receipts | calls, tokens (inclusive exposure), wallclock (anchored deadline) | yes (the 1,523-token planner/reviewer component) | the run budget's anchored wall clock |
| `claude-code` (batch) | Anthropic-shaped DISJOINT (the driver always talks to an Anthropic-compatible endpoint) | input, cached, cache_write, output, cost | not re-measured (the #289 capture measured it: 36,125/136,768/2,305, $0.2180) | harness episode wall clock |
| `grok-build` / `opencode` / `copilot` (OpenAI-compatible) | INCLUSIVE: the cache rides INSIDE `input_tokens`; a cached column is a breakdown, NEVER added on top | input (inclusive), cached (breakdown), output, reasoning/thinking (output breakdown) | no live trace in these captures | harness episode wall clock |

## 3. The structured assessment (the planner REQUESTS, never grants)

The planner output schema now carries the optional assessment
(`forge.factory.planner`): the prompt asks for
`"budget_class": "trivial|standard|heavy"` plus a one-sentence
`budget_reason`, and the parsed plan keeps it on `last_plan` — the
NORMAL planning composition produces it, no test-injected field. The
consumer is the existing R31 proposal path: the service leniently reads
`harness`/`budget_class`/`reason` from `last_plan` and
`compile_harness_selection` re-validates everything against frozen
policy:

- the class is validated against the closed
  `BUDGET_CLASSES` triple (`trivial|standard|heavy`) — anything else is
  the bounded malformed case below, never an exception;
- the assessment may NARROW (a small task asking `trivial`) or REQUEST
  escalation (asking `heavy`) — it names a class, never a number;
- policy ceilings stay OUTSIDE model authority: the class resolves to
  the OPERATOR's configured profile (`FORGE_BUDGET_PROFILES` /
  `budget_profiles:` in forge.yml), the resolved ceilings freeze into
  the RunSpec at plan acceptance, and a suggested expensive route can
  never bypass the approved cap — an unconfigured `heavy` degrades to
  the default class's profile (bounded fallback), never to unlimited,
  never above the configured maximum.

## 4. One profile, resolved before execution, reason recorded

Exactly ONE numeric profile resolves before execution, and the SELECTION
REASON is recorded at two seams:

- **The class selection** (`forge.runs.harness_selection.
  compile_harness_selection`) — `budget_class_reason` on the frozen
  `HarnessSelection`, round-tripped through the RunSpec's
  `backend_config` and the run's `harness_selection` evidence. Three
  DISTINCT bounded cases: `planner assessment: heavy` (the valid
  request), `default: no assessment` (the stub planner / a plan JSON
  without the field), `default: malformed assessment (budget_class=…
  not in trivial|standard|heavy)` (present but garbage). Never an
  exception path — the mapping is total.
- **The numeric profile resolution**
  (`forge.durable.budgets.resolve_budget_profile`) — a
  `BudgetProfileResolution` carrying the class, the profile name it
  actually resolved to, the limits and the REASON
  (`budget.profile_selection_reason`): `configured profile: heavy`,
  `class 'galactic' unconfigured -> standard profile (bounded
  fallback)`, `no budget profiles configured (unlimited)`, or `profile
  … carries no limited axis (unlimited)`.

Freeze-time authority: the resolved ceilings ride the spec the human
gate approves; a changed default (or a changed profile JSON) never
alters an already-approved run — `open_budget` is idempotent per run
with limits frozen at open time, and moving a live run's limits takes
the recorded amendment below.

One named gap (pinned by
`tests/test_budget_calibration.py::TestPlannerAssessment`): the GitLab
`RunService` opens the budget row at the DEFAULT class's numbers
BEFORE the first paid call and recompiles the selection after the plan
— so an assessment-driven class change can leave the spec's budgets
block showing the assessment's ceilings while the durable row keeps
the pre-plan ones. The guard enforces the ROW, which makes the
divergence safe in both directions (an escalation REQUEST cannot raise
the opened ceiling; a narrowing cannot spend below the opened floor
either), and the GitHub service additionally pins the selection's
displayed ceilings to the opened row (its B11 block). The GitLab
display pin is owed in `runs/service.py` — deliberately out of this
issue's file ownership.

## 5. The closing reserve, verified on the real guard

The #340 partition machinery (`closing-partition/1`) exists; the
calibration test (`tests/test_budget_calibration.py`) verifies the
ACTUAL guard enforces both phases using the measured numbers:

- **The recorded negative** (no partition): a coder reconciling the
  measured `fbe62ad5` receipt shape against `standard@200k` exhausts
  the axis, and the closing reservation is refused too — the review
  stood down, exactly as recorded.
- **The partitioned window**: with the v1 share (15% → 30,000 tokens of
  a 200k window) frozen at open time, the implementation purpose cannot
  enter the share — the coder is refused at the boundary while the
  closing purpose still reserves inside it: the reviewer completes
  within its reserved share when coding hits its own ceiling
  (`closing.reserve_remaining` stays > 0 for the review leg, = 0 for
  the coder leg).
- The rounds 2/3 live windows froze exactly this shape (90,000 of
  600,000 tokens + 6 of 40 calls reserved) — the capture carries
  limits/status only, so the artifact reports the FROZEN share, never a
  fabricated measured remainder.

## 6. The amendment workflow (measured insufficiency)

When the measurement shows insufficiency (a run fenced at the token
axis with the closing review stood down, at a cost the operator accepts),
the path is the #340 amendment — NEVER a mid-run self-expansion and
NEVER a silent default change:

1. **The trigger**: the calibration evidence — the exhaustion event
   (consumed/limit, the stood-down review) against the recorded
   profiles. The first calibration report carries usage coverage,
   failed attempts and human corrections, not just the successful lane
   cost.
2. **The operator decision** (the recorded `forge.budget-profiles/1@2026-09-27-align`
   is the worked example): for a LIVE run, `service.continue_review_only(...)`
   applies a `budget_amendments` row (one DISTINCT axis, the originating
   native command identity, replay-idempotent) — count-axis amendments
   move the enforcing `run_budgets` limits atomically with
   `limit_before`/`limit_after` history preserved; prior allowance,
   spend and incomplete liabilities are never rewritten. For the
   DEPLOYMENT's default posture, the profile change itself is receipted
   (the alignment receipt pins the exact `FORGE_BUDGET_PROFILES` bytes
   both consumers run) and versioned — history is never rewritten.
3. **The honesty**: the amended profile stays finite, numerical and
   enforced; already-approved runs keep their frozen ceilings (a fresh
   run resolves the new profile); the review-only continuation repeats
   ONLY the review of the same tested candidate — zero coder
   dispatches, zero commits.

## 7. The 600k honesty

The current 600k standard cap is A RECORDED PROFILE, not a universal
requirement:

| profile version | trivial | standard | heavy | origin |
| --- | --- | --- | --- | --- |
| runbook default (`forge-0.42.0`) | 8 / 40k / 900s | 40 / **200k** / 3600s | 120 / 600k / 10800s | `docs/operations/lab-alignment-runbook.md` — the shipped default posture |
| `forge.budget-profiles/1@2026-09-27-align` | 8 / 40k / 900s | 40 / **600k** / 3600s | 120 / 600k / 10800s | the #364 live window's receipted amendment (run `fbe62ad5` fenced at 200k with the review stood down) — the LAB's posture, one observed task shape |

A changed default never alters an already-approved run without a
recorded amendment: ceilings freeze into the RunSpec at plan acceptance,
the budget row is idempotent per run, and the 600k posture is reachable
only by configuring it (or amending a live run through the ledgered
command). Future re-sizing reads the NEXT calibration report
(`scripts/build_budget_calibration.py` over the newest captures), not
this one window.

## Observability

- `budget.profile_selection_reason` — the recorded reason ONE profile
  resolved (both seams above; the selection document carries
  `budget_class_reason`, the resolution carries `profile_selection_reason`);
- `context.redundant_read_fraction` — the measured share of served
  context that was a cache re-read (0.756–0.900 across the nine
  receipts; 0.847 overall);
- `usage.coverage` + `usage.unresolved_liability` — receipted share and
  the bounded unreceipted residual (0.875; $0.223636 attributed to
  `6e0fdf34`);
- `closing.reserve_remaining` — the closing share the coder cannot
  enter (the frozen share on the recorded windows; the measured
  remainder in the verification test);
- `accepted_task.all_attempt_cost` — every attempt's spend, failed ones
  kept ($1.256064 exact on the #364 root task; the human acceptance
  pending).
