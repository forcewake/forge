# The bounded delivery cohort (A17)

Measure ACCEPTED work, not READY counts. A faster harness that drafts more
PRs while leaking more human fixes is not a better harness — raw tokens/s and
the READY counter hide exactly that. This runbook defines forge's bounded
delivery evaluation cohort (external review 2026-09-18, item A17): **14
bounded tasks**, each with a fixture repo seed and PREDECLARED mechanical
acceptance checks, driven through forge's public surface, with every attempt
— failed, blocked, cancelled, superseded — kept in the ledger and the
economics computed **per accepted unit**.

This is an evaluation harness (spec + runner + aggregation), not a live
benchmark result: the cohort is the *proposed* measurement instrument. It
lives in [`evaluation/cohort/`](../../evaluation/cohort/), never imports the
`forge` package, and talks to a running forge instance exactly the way an
operator does — an issue, `/implement`, `/go`, `/status`, `/cancel`,
`/retry` — over the GitHub `gh` CLI.

## The honesty rules

These rules are pinned by the code (see `tests/test_cohort_runner.py`) and
are the reason the cohort exists:

1. **Same tasks, same verification contract.** Every pass runs the same 14
   units under `contract_version = forge.delivery-cohort/1`. Two passes
   (two harnesses, two models, two budget classes) are comparable only when
   the contract version matches and only the `profile` differs. The checks
   are the verification contract — never renegotiated mid-pass, never
   replaced by the agent's say-so.
2. **Unique unit IDs, independent acceptance.** Each unit (`CU-01` …
   `CU-14`) carries its own seed and predeclared checks. Acceptance is an
   explicit HUMAN verdict (`accept` subcommand) against those checks — the
   agent's self-reported success is never a verdict, and a READY status is
   never an acceptance.
3. **The denominator is accepted units.** Cost/time per unit divides by the
   count of units with an `accepted` verdict. READY counts, drafted-PR
   counts and attempt counts are never denominators. When nothing was
   accepted the per-accepted block is omitted with a note — it is
   undefined, not zero.
4. **All-attempt spend stays in.** Failed, blocked, cancelled and
   superseded attempts keep their receipts in the ledger and their spend in
   the cohort totals forever. A later successful sibling never erases a
   failure (the R24 denominator rule). The per-accepted-unit view prices a
   unit by ALL of its attempts — repairs and dead lanes included.
5. **Token classes stay separate.** Input, cached-input, cache-write and
   output tokens are four separate sums with per-class unknown counts.
   Unknown stays unknown, never zero (ADR-0013/R23); cache counters are
   never folded into input.
6. **One rate only, and it is decode.** The only throughput the report
   builds is known `output_tokens` over known `llm_calls.duration_ms`.
   Logical traffic (input + cache) is never divided by decode time, and no
   mixed "total tokens/s" is ever produced.

## The 14 tasks

| Unit ID | Axis | Task (seed → work) | Predeclared acceptance (mechanical) |
|---|---|---|---|
| `CU-01-create-greeting` | create | create `greeting.py::greet` | `greet('World') == 'Hello, World!'` |
| `CU-02-create-calculator` | create | create `calculator.py::add/mul` | `add(2,3) == 5`, `mul(3,4) == 12` |
| `CU-03-update-slugify` | update | fold underscores to hyphens in `slugify` | new + preserved behaviour asserted |
| `CU-04-update-retry-config` | update | add `MAX_RETRIES`, thread it through `client.attempt` | constant exists; output string carries it |
| `CU-05-repair-temperature` | repair | fix regressed `celsius_to_fahrenheit` | predeclared failing unittest passes; test file sha-identical |
| `CU-06-repair-email` | repair | fix dotted-domain rejection in the validator | predeclared failing unittest passes; test file sha-identical |
| `CU-07-large-log-analyze` | large-file | `count_errors` must read a ~147KB log beyond line 100 | equals an independent recount over the raw file |
| `CU-08-large-dictionary` | large-file | add `count_prefix` over a ~108KB word list | equals an independent recount; `lookup` intact |
| `CU-09-monorepo-billing-scope` | monorepo | 10%-floor `discount_cents` in `services/billing` ONLY | billing behaviour + shipping sha-identical and behaviourally intact |
| `CU-10-monorepo-shared-banner` | monorepo | route the CLI banner through `packages/shared` ONLY | banner contract + `vendor/` sha-identical |
| `CU-11-tests-stringutil` | tests-only | write the missing unittest suite for 3 documented functions | suite passes; every function exercised by name |
| `CU-12-tests-ratios` | tests-only | write the missing unittest suite incl. both error types | suite passes; functions + exceptions exercised by name |
| `CU-13-infra-retry` | infra-failure | create `env_report.py` — with the lane KILLED once mid-run; the unit is delivered by the retry | delivery checks pass on the retry; BOTH attempts + spend stay in the ledger |
| `CU-14-cancel-mid-run` | cancel-mid-run | create `metrics_export.py` — operator `/cancel`s mid-run; never accepted | verdict is `cancelled` BY CONTRACT; its spend stays in totals as wasted spend |

Definitions live in `evaluation/cohort/tasks.py`. Every check is an argv
list run inside the candidate worktree; check oracles recompute expected
values from the seed data (a raw-file recount, a sha pin) so no agent output
is trusted.

## Running one cohort pass

Prerequisites: a disposable GitHub lab repo (e.g. `owner/forge-lab-gh`) with
forge fully onboarded (see [harness onboarding](../harnesses/onboarding.md)),
`gh` authenticated, and `gh auth setup-git` run once (the seed push uses
git-over-HTTPS). Units run **sequentially** — each unit's seed is
force-pushed onto the lab repo's default branch before its issue is opened.

```bash
# 0. open a pass (records the harness profile under test)
.venv/bin/python -m evaluation.cohort.runner new \
    --repo owner/forge-lab-gh --driver claude-code --model glm-5.3-flash \
    --ledger lab/runs/2026-09-17-claude.ledger.json

# 1. drive one unit end to end (seed → issue → /implement → /go → observe)
.venv/bin/python -m evaluation.cohort.runner run CU-01-create-greeting \
    --repo owner/forge-lab-gh \
    --ledger lab/runs/2026-09-17-claude.ledger.json

# 2. special procedures
#    CU-13: after /go, kill the lane run once; let Tier-1 revive (or /retry).
#    CU-14: after /go, post /cancel <run-id> while the lane is running
#           (the runner's cancel_unit does this and stamps the attempt).

# 3. attach usage from the R23 receipt-ledger export (see below)
.venv/bin/python -m evaluation.cohort.runner observe \
    --ledger lab/runs/2026-09-17-claude.ledger.json \
    --export lab/runs/2026-09-17-claude.export.json --all

# 4. run the predeclared checks in a checkout of each accepted candidate
git clone --depth 1 --branch <forge-branch> owner/forge-lab-gh lab/worktrees/CU-01
.venv/bin/python -m evaluation.cohort.runner checks CU-01-create-greeting \
    --ledger lab/runs/2026-09-17-claude.ledger.json --workdir lab/worktrees/CU-01

# 5. the HUMAN verdict, per unit
.venv/bin/python -m evaluation.cohort.runner accept CU-01-create-greeting \
    --ledger lab/runs/2026-09-17-claude.ledger.json \
    --verdict accepted --decided-by operator --notes "merged the draft PR"

# 6. the report artifact
.venv/bin/python -m evaluation.cohort.runner report \
    --ledger lab/runs/2026-09-17-claude.ledger.json \
    --pricebook lab/prices.json --out lab/runs/2026-09-17-claude.report.json
```

The whole pass is 14 units; expect it to take as long as the runs take.
Nothing in the pass may edit the checks, the seeds or a verdict after the
fact — a pass whose contract version differs is not comparable.

### The usage export (R23 receipt ledger)

The runner is stdlib + `gh` only and never opens forge's database. Usage
arrives as a JSON export of the durable tables the run loop already keeps:

```json
{
  "schema": "forge.cohort.export/1",
  "runs": {
    "<run_id>": {
      "status": "ready_for_human",
      "commit_cycle": 2,
      "usage_receipts": [
        {"model": "glm-5.3-flash", "input_tokens": 15234,
         "cached_input_tokens": 9120, "cache_write_tokens": 512,
         "output_tokens": 1874, "completeness": "aggregate",
         "attempt_id": "911:1", "receipt_id": "<sha256>"}
      ],
      "llm_calls": [
        {"duration_ms": 41222, "first_token_ms": 812, "status": "ok", "role": "implement"}
      ]
    }
  }
}
```

`usage_receipts` rows mirror `flow_runs`' ingested R23 receipts; `llm_calls`
rows mirror the LLM ledger (`duration_ms` is the latency source;
`first_token_ms` is optional — when the driver did not report a time-to-
first-token, leave it out and it counts as unknown, never zero). A psql
export over the same column names is sufficient; the join key is the run id.

## Artifacts

- **Ledger** (`forge.cohort.ledger/1`) — the pass record: profile, per unit
  the attempts (run id, stamps, terminal status, rework/superseded linkage,
  commit cycle, receipts, llm calls) and the acceptance block (verdict,
  decider, checks). Append-only on attempts; verdicts are human-only.
- **Report** (`forge.cohort.report/1`) — the aggregation:

```
counts              units / attempts / per-verdict unit counts /
                    rejected_attempts / rework_count / repairs_total
all_attempt_spend   usage  (4 token-class sums + per-class unknown counts)
                    llm    (active seconds, call counts, TTFT p50/p95 + unknowns)
per_accepted_unit   denominator · token_classes_per_unit ·
                    llm_active_s_per_unit · wall_s_mean · repairs_per_unit ·
                    cost_usd_mean (only with a pricebook, only fully priced)
latency             decode_output_tokens_per_s   (output / llm seconds ONLY)
honesty             denominator statement · attempts_retained ·
                    token_classes_separate · decode_rate_scope ·
                    unknown_usage_attempts · zero_accepted_note
units               per-unit rollups with every attempt kept
```

Cost appears only with an explicit `--pricebook` (USD per million tokens
per class, keyed by model) and only over receipts whose four token classes
are all known — a partially-reported receipt is never averaged in with a
fabricated remainder.

## What the cohort does NOT claim

- It is not a calibrated benchmark; it is a bounded instrument for
  like-for-like harness comparison under identical safety contracts.
- Fourteen units bound what it can resolve: differences smaller than the
  per-unit noise floor are not signal.
- The infra-failure and cancel units measure the COST of failure paths,
  not failure frequency; the frequency depends on the injection.
- Acceptance checks are mechanical but narrow; "accepted" means "met the
  predeclared contract", not "production-quality".
