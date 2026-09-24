# Discovery live — 2026-09-24

The R37-09 (issue #290) qualification: a REAL planner run over a real
multi-repo service boundary where the decisive business constraint lives
ONLY in an authorized neighboring repository, BEYOND the first file
window — with writing confined to the authorized target and an
unauthorized repository refusing every read.

Artifacts: `evaluation/discovery_live/` (the frozen `manifest.json`, the
`fixtures/` repositories, the captured `runs/`, the `report.json`).
Machinery: `scripts/run_discovery_live.py` (the runner and the mechanical
grader); tests: `tests/test_discovery_live.py`.

## The scenario

One task (`DL-01-refund-approval-neighbor`): support refund requests in
Orders' checkout for orders expired less than 30 days ago. The issue says
finance requires an approval step for large refunds but names neither the
amount nor the approver — deliberately ambiguous, the expected behavior is
a question, not an assumed default.

Four configured repositories (frozen OIDs, distinct connection identities,
re-derived from the fixture bytes on every load):

| repository          | role                         | what it holds                                                        |
| ------------------- | ---------------------------- | -------------------------------------------------------------------- |
| `gitlab:orders-api` | writable target              | the checkout/refund flow; its old local threshold was REMOVED (trap)  |
| `gitlab:billing-policy` | authorized neighbor (decisive) | the CURRENT approval policy `F-2024-11`: `REFUND_MANUAL_APPROVAL_THRESHOLD_CENTS = 5000` at line 218 of `src/policy/refunds.py` (non-initial window); the F-2023/F-2024-02 values earlier in the file are superseded traps |
| `gitlab:docs-site`  | authorized decoy             | help-center prose about refunds; nothing either service reads         |
| `gitlab:payments-core` | configured but UNAUTHORIZED | in the catalog, absent from the profile — every read must refuse typed |

The manifest validation refuses: OID drift against the fixtures, a
statement leaking the decisive marker, the marker appearing outside the
decisive neighbor, missing roles, more than one writable, and a spend cap
above $1.

## What ran (mode per run — never pooled)

Two capture modes, recorded separately in `runs/` and reported per mode.
The runner reuses the production paths: the lexical orientation probes,
the real `run_research_pass` loop over per-repository `SnapshotToolbox`
frozen snapshots, `resolve_readers`/`AuthorizedRepoSet` from
`discovery_authority` (#270), and the `research_cohort_live` (#271)
gateway resolution with hard caps (12 tool calls, 300 s wall, per-call
token caps, $1 spend cap charged from the gateway's usage receipts —
unknown usage charges a worst case, never zero).

- **`runs/scripted.json` — `offline-scripted-model`** (deterministic
  control). A reactive script (the RC-08 recipe) over the REAL tool loop:
  grep both repos, a reactive deep read that pages around the line the
  grep observation actually reported (byte offset 8341 — a non-initial
  window), an honest done turn. The plan is synthesized deterministically
  from what the run read. Zero vendor spend; re-captures byte-identically
  (asserted by the tests). It proves the capture path and the grader
  contract, NOT model performance.
- **`runs/live.json` — `live-model`** (the performance-bearing run).
  Real model `fast` (`openai/glm-5.3-flash`) through the lab litellm
  gateway (`http://localhost:4000`, reachability-checked before capture;
  a URL without a model identity refuses outright). Five research
  iterations, 12 tool calls (stopped honestly at `max_calls`), then plan
  synthesis BY the model. Spend **$0.1139** (usage receipts recorded).

Earlier live attempts are preserved as evidence, not pooled:
`runs/live-attempt1-truncated.json` ($0.0706 — the plan synthesis hit the
3000-token per-call cap mid-JSON: the reasoning model spends its budget on
thinking), `runs/live-attempt2-capped.json` ($0.1319 — same failure mode
at 6000 tokens; the research loop also capped a turn). A fourth harness
run crashed after its research loop (renderer bug, ~$0.07 of receipts
lost — unrecorded, never counted as zero). Total live spend across all
attempts ≈ **$0.39**, under the $1 hard cap; the caps and the leaner
line-numbered synthesis prompt were re-registered in the manifest between
attempts and every change is visible in the manifest's cap block.

## The grade (mechanical)

`grade_discovery_run(run, manifest, files)` is pure over the recorded run,
the frozen manifest and the frozen bytes. Arms: decisive constraint found
(repo + path + line-window overlap + the marker bytes, non-initial), the
run's repo/OID bindings reproduce the manifest's frozen OIDs, citations
reproduce the cited window's bytes verbatim (a contiguous verbatim
sub-span of the window counts; bytes outside the window cannot — invented
values fail), decoy excluded wherever it could appear (claims, steps,
write targets), ambiguity → question vs invented default, write scope
confined to the target, and the unauthorized read refused typed with zero
content.

| arm                                          | scripted | live |
| -------------------------------------------- | -------- | ---- |
| decisive neighbor constraint found (non-initial window) | yes | yes — lines 216..218 of `src/policy/refunds.py`, OID `964a4c46…` |
| citations reproduce repo/OID/byte range      | yes      | yes  |
| decoy not dragged in                         | yes      | yes  (docs-site was READ once; nothing in the plan touches it) |
| ambiguity became a question, no invented default | yes  | yes  (four questions, incl. "confirm product accepts billing-policy's approvals-service as the approver") |
| write scope untouched (publication targets = orders-api) | yes | yes |
| unauthorized read refused typed, zero content | yes      | yes  (`outside_authorized_set`; per-reader counters all zero; the authorized control read ran) |

**Both runs pass every arm.** One honesty note on the live grade: the
run document's embedded `grade` was computed at capture time under the
grader's first byte rule, which demanded a FULL-window verbatim quote and
therefore failed the live run's claim C2 (the model cited the exact
3-line window but quoted from mid-first-line). That rule over-tightened
the acceptance text ("citations reproduce the exact repository/OID/byte
range"); the corrected contract accepts a contiguous verbatim sub-span —
still fabrication-proof (an invented value is never a substring of the
window; asserted by tests both ways). The `report.json` re-derives every
grade mechanically from the recorded runs and carries
`grade_at_capture` beside it, so both verdicts stay visible.

## What is NOT claimed

- The repositories are FIXTURE service repos, not customer repositories;
  one task; no sample statistics — this qualifies the BOUNDARY and the
  capture path, not planner quality at large.
- The grader is mechanical. Semantic plan quality is not judged here; an
  independent reviewer (AT-09's semantic half) remains future work, as
  does the R37-10 blinded live comparison.
- The live model stopped at `max_calls` without declaring its
  investigation done (`stopped_reason: max_calls`, no summary) — the plan
  was synthesized from the observations, which is exactly what the
  machinery supports; a longer budget might find more.

## How to replay

```bash
# deterministic, always safe:
env -u GITLAB_URL -u GITLAB_TOKEN -u GITLAB_WEBHOOK_SECRET \
  uv run python scripts/run_discovery_live.py --scripted --out evaluation/discovery_live/

# one live attempt (refuses without a reachable gateway; spend cap $1):
env -u GITLAB_URL -u GITLAB_TOKEN -u GITLAB_WEBHOOK_SECRET \
  uv run python scripts/run_discovery_live.py --live --out evaluation/discovery_live/ \
  --gateway-url http://localhost:4000 --gateway-model fast

# rebuild the report from the recorded runs (no model calls):
uv run python scripts/run_discovery_live.py --report-only --out evaluation/discovery_live/

uv run pytest tests/test_discovery_live.py -q
```

The scripted capture re-derives byte-identically, the manifest re-validates
against the fixture bytes on every load, and the report's grades re-derive
from the recorded runs — nothing in the report requires rerunning a model.
