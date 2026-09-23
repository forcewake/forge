# Research-quality cohort — 2026-09-23

First task-specific evaluation of the bounded research-harness discovery
mode (R28-16's third mode) against the two incumbents, built to the shape
the E2E-qualification research prescribes (topics 3 §4 + 4 §4): a small
cohort with SEEDED findings, three-value verdicts, merged rubric
dimensions, and an evidence pack replayable offline. The module is
`src/forge/adaptive/research_cohort.py`; the recorded evidence lives in
`evaluation/research_cohort/`; the replayed report is `report.json` in
this directory.

## What the cohort measures

Not "did the research pass produce text" — "did the richer mode produce
BETTER PLANS on tasks whose ground truth the fixture owns". Every task
names its expected impacted surface (repo + path), so recall grades
against ground truth, never luck; the four archetypes each pin one
failure shape the review called out:

| Archetype | The seeded trap | Tasks |
|---|---|---|
| `neighbor_dependency` | the contract lives only in a NEIGHBOR repo (Billing owns the event schema / retry policy Orders depends on) | RC-01, RC-02 (held) |
| `ambiguous_requirement` | a decision is missing on purpose (grace window 7 vs 30 days; retention 30/60/90) — the graded behavior is a SPECIFIC question, and an invented default scores worse | RC-03, RC-06 (held) |
| `deep_file_evidence` | the relevant handler sits at line ~24 of the handlers file; first-line reads and shallow greps cite the wrong line | RC-04, RC-07 (held) |
| `irrelevant_repository` | `docs-site` is in the authorized set and holds nothing either service reads; dragging it into the surface is spurious scope | RC-05 |

Seven tasks, four promotable (one per archetype — validation refuses a
cohort where an archetype exists only held back) and three held back as
an unseen reserve: held-back tasks are graded and reported SEPARATELY
and can never move the verdict.

## The modes

`none`, `lexical`, `research` compared on the SAME frozen snapshots
under the SAME agreed budget (10 tool calls / 90 s wall — recorded in
the spec and re-checked per run; a mismatched budget breaks
comparability and forces HOLD). `research` is the cohort spelling of
the planner's `research-harness`.

## The rubric — five MERGED dimensions

Correlated candidate dimensions are merged, not accumulated
(`RUBRIC_MERGE_NOTES` in the module records each fold):

1. **evidence accuracy** — each important plan claim is re-checked
   against the ACTUAL snapshot bytes at its cited repo/path/line. A
   syntactically valid citation whose window does not carry the claim's
   content earns NOTHING (`syntactic_only` ≠ credit). Reviewer-recorded
   importance decides which claims matter; the support verdict is
   computed by the module, never read off the recording.
2. **impacted-surface recall** — found vs the task's expected surface,
   with scope precision folded in: each surface entry inside the task's
   irrelevant repositories is a spurious penalty, not a sixth dimension.
3. **unjustified assumptions** — severity-weighted count (high 3 /
   medium 2 / low 1) with invented defaults penalized +2: a missing
   decision answered by an invented default scores worse than one left
   as a question.
4. **question quality** — where a decision is missing, only a SPECIFIC
   question naming it scores; generic questions score zero.
5. **human plan-correction effort** — the reviewer's recorded minute
   estimate, published BESIDE the quality score as its counterweight
   and never blended into the overall (the speed/quality pairing rule).

## How to replay offline

The runner is a PURE function over recorded artifacts — research
documents, ToolObservation traces, plans, budgets, costs, reviewer
grades — with no live LLM, no network, no production system:

```bash
uv run python - <<'PY'
from pathlib import Path
from forge.adaptive.research_cohort import CohortRunner

root = Path("evaluation/research_cohort")
runner = CohortRunner.from_directory(
    root / "cohort-v1.json", root / "snapshots", root / "recorded"
)
report = runner.run()
print(report.verdict, report.document["promotion"]["reasons"])
report.write(Path("report.json"))
PY
```

Layout: `cohort-v1.json` (the versioned spec), `snapshots/*.json`
(multi-repo blobs, keyed by their re-derived `repo_set_digest`),
`recorded/<task>/<mode>.json` (one run per mode: attempts with cost,
research document, observations, plan, reviewer grades) and
`recorded/<task>/mutation.json` (the contract-mutation arm).

Before anything can influence the verdict the replay re-validates every
binding: snapshot digests re-derive, runs bind to the task's authorized
digest, every attempt carries cost, budgets equal the agreed budget,
research-plan citations resolve to the research document's own findings,
and every recorded mutation digest matches a re-applied
`apply_contract_mutation`. Any issue → HOLD, never a silent pass.

## Honest semantics, structurally

- **Exhaustion is never relabeled complete**: RC-05's research pass
  burned its call budget on `docs-site` listings first and stopped with
  `complete: false` / `max_calls`; the report keeps exactly that, grades
  the plan it still produced, and counts the exhaustion.
- **Failed attempts appear WITH cost**: RC-04's first research attempt
  died on a gateway error with UNKNOWN token usage (the provider had
  accepted the request) — both attempts keep their spend, and the
  aggregate's token totals stay lower bounds (`input: null`,
  `input_lower_bound: 35988`, `unknown_usage_calls: 1`). RC-07's
  research attempt (held back) failed outright: reported with cost, no
  fabricated dimension scores.
- **The mutation hook is tracked, not auto-judged**: the runner
  re-applies the contract mutation (Billing moves the shared event
  `order.expired.v2` → `v3`), recomputes plan digests, and records
  whether each mode's plan changed — lexical's did not (it never read
  the neighbor contract), research's did — with the reviewer's
  "is the change reason substantive" flag recorded beside, never
  inferred.
- **The injection probe stays adversarial**:
  `inject_valid_but_irrelevant_citation` adds a syntactically valid
  citation (a real line, real bytes) to a recorded research document;
  a plan leaning on it scores `syntactic_only` — accuracy cannot be
  inflated by valid-but-empty citations.

## What promotion requires

The three-value verdict (PASS / HOLD-with-expiry / ROLLBACK):

- **PASS** only when `research` beats `lexical` on the weighted overall
  by at least the agreed margin (0.05), keeps evidence accuracy at/above
  the floor (0.8), invents no more defaults than the baseline, and every
  run stayed inside the agreed budget.
- **HOLD** routes to the named owner (`R32 review lead (adaptive)`) and
  EXPIRES (`2026-10-07`) — for incomplete evidence (missing runs, cost
  gaps, tampered digests, budget mismatches), sub-margin improvement, or
  broken budget comparability. An expired HOLD without a recorded human
  decision is a ROLLBACK, never a silent pass.
- **ROLLBACK** for regressions: overall below lexical, accuracy under
  the floor, or more invented defaults than the baseline.

The replayed example verdicts **PASS** (`research` 0.9719 vs `lexical`
0.6865 overall, accuracy 1.0 vs 0.75, one exhausted pass and one failed
attempt kept in view) — see `report.json`.

## Honest limitations

- **These recorded artifacts are EXAMPLES that exercise every grading
  path, not a real partner cohort.** The plans, research documents and
  reviewer grades were authored as fixtures; the PASS demonstrates the
  machinery end to end, not evidence about production behavior. A real
  cohort records live runs against a partner's frozen snapshots under
  the same contract.
- Reviewer-recorded grades (claim importance, question specificity,
  assumption severity, correction minutes) are human input; the module
  aggregates them honestly but cannot audit the humans.
- The semantic support check is token-coverage against the cited window
  (`SEMANTIC_TOKEN_COVERAGE = 0.6`) — it pins citations to content, it
  does not prove the claim's broader reasoning.
- Goodhart applies: once this rubric drives optimization, expect gaming;
  re-record a fresh cohort before any promotion decision that matters,
  and do not judge the candidate mode during the J-curve dip — the
  measurement window is the one pre-committed in the spec.
