# R37-08 (#289) — one live single-writer workflow with real tools, model and provider

Executed 2026-09-24 (12:06–13:20 UTC) against the REAL lab: GitLab CE
19.3.2 at `gitlab.forcewake.duckdns.org`, the forge control plane rebuilt
from this repo, the `unraid` runner (id 4), claude-code 2.1.273, and the
glm-5.3-flash model through the customer's own gateway route
(`https://api.z.ai/api/anthropic`, BYOK). Total lane spend: **$0.80**
(SDK usage receipts; cap $2). Everything below is from the executed
trace — `live-run-evidence.json` (the driver's state/evidence bundle)
and `alignment-receipts.json` (the alignment receipts). The frozen
configuration is `qualification/profiles/live-single-writer-v1.md`; the
record is `qualification/records/gitlab-ce-v1@live.json` (strict schema,
zero validation findings).

## 1. The alignment (before any paid call)

Recorded basis (morning inventory): control plane 0.28.0 on a stale dev
image, schema 026 vs repo 027, no budget caps, lane templates at
`2fbc321` — verdict **misaligned**, paid flow refused.
`scripts/align_lab.py --apply` executed, receipted per step:

| Axis | Before | After | Receipt |
| --- | --- | --- | --- |
| `/health` version | 0.28.0 | **0.36.0** (== repo `__version__`) | verify: match |
| Schema | 026 | **027** (migrated on the new image BEFORE consumers started) | migrate step, 4.3 s |
| Budget caps | absent | `FORGE_BUDGET_PROFILES` (trivial/standard/heavy) + `FORGE_LANE_BUDGET_SECONDS=1800` numerical on BOTH consumers | verify: match |
| Lane template (project 68) | `2fbc321` | **`v0.36.0`** tag refs (runbook §5) | inventory: match |
| Control-plane image | `localhost/forge:dev @ sha256:a09ad6a0…` (0.28.0-era) | rebuilt from the repo: `sha256:e2a26c52…` | build 38.7–50.8 s |
| Image digest vs promoted `d776da71…` | mismatch | **stays mismatched — deliberate**: the working tree carries the #288 dispatch envelope the promoted release predates | inventory: mismatch (honest) |

Plus two LIVE-mandated config fixes (both receipted, both infra): the
harness preference chain `claude-sdk-lane,claude-code` (the profile's
frozen exact-resume lane; the batch lane cannot restore WIP) and the
lane-control URL `https://forge.forcewake.me` (reachable from the
external runner; `host.containers.internal` is not). And one LIVE-found
lab defect: the worker ran WITHOUT the shared `/app/data` volume, so the
app (which received checkpoint uploads) and the worker (which looks them
up) used different stores — `/retry` found "no checkpoint" and parked.
Restored with `--worker-mount` (the compose file's intended wiring).
Rollback: the pre-alignment image stays tagged
`localhost/forge:pre-r3708-*`; a pg_dump backup sits in `backups/`.
Preflight (the app's own doctor, on the new project) green before any
paid run — with the honest note that the litellm `/health` probe rides
its 5 s timeout boundary (observed 1.6–2.2 s typical, occasional spikes;
the driver re-probes read-only).

## 2. The task

Implement `slugify(text: str) -> str` in `src/utils/text.py` (lowercase;
non-alphanumeric runs → one `-`; no edges; empty-in → empty-out). The
independent oracle — six exact cases in the repo's own `smoke` CI job,
committed BEFORE any run — is the acceptance; PR creation is not. The
candidate diff may not touch `.gitlab-ci.yml` or `tests/`. Disposable
project `forge-live-qual-2026-09-24` (created via the API, deleted after
capture).

## 3. Arm 1 — the uninterrupted flow: GREEN

- issue #3 (native) → `@forge /implement` (the approver's own note, via
  the real webhook) → plan note: run `60b9de7f58094f2abe74dadad83565ba`,
  **Harness: claude-sdk-lane · model glm-5.3-flash**, budget class
  standard (40 calls / 200k tokens frozen in the RunSpec).
- `/go 60b9de7f…` → the REAL dispatch: pipeline **380**, lane job
  **726**, fresh-resume envelope digest `7f1fed630bc0` (worker journal).
- The lane (runner id 4): claude-code **2.1.273** installed
  (`claude --version` in-trace), forge lane_driver from the immutable
  `git+…@4af6b331…` pin, `FORGE_CANDIDATE` marker emitted, candidate
  diff + meta uploaded. Usage receipt: 36,125 in / 136,768 cached /
  2,305 out — **$0.2180**.
- Trusted publisher → **Draft MR !1** (`Draft: Add slugify() …`) on
  `factory/3/60b9de7f`.
- Independent verification: the smoke oracle GREEN on the EXACT
  candidate `e670d42f…` (verification pipeline **382**); changed paths
  exactly `src/utils/__init__.py`, `src/utils/text.py`; no oracle
  tampering. Run terminal state `ready_for_human` (the reviewer raised
  concerns — recorded; merge stays a human decision, the MR stays a
  Draft).

## 4. Arm 2 — the deliberate interruption: mechanics GREEN, delivery FAILED (honest)

- issue #4 → plan (run `905194f0e4314e9aa126325b37637d1b`) → `/go` →
  pipeline **384**, lane job **730**; `/pause` posted while the lane job
  was running.
- The pause APPLIED mid-turn (lane steering journal: `interrupt
  acknowledged`, `pause_status: paused`, latency 0.002 s) and a
  **verified WIP checkpoint** landed: `3cb49a16…` (`remote_ref
  905194f0@3cb49a16…`, verified=true, files=0, sequence 0) — durably in
  the filesystem authority store (`data/checkpoints/works/905194f0….json`
  + blob).
- The pause fence itself ended the lane job (the job-level cancel —
  job-level only, never container-level — was therefore moot: HTTP 201,
  status `failed`). The worker classified the run `blocked` from the job
  event with ZERO LLM repair.
- First `/retry` (before the volume fix): PARKED, honestly — "continuation
  source unknown (uncertain), nothing dispatched" because the worker
  could not see the checkpoint store. This is the finding that surfaced
  the missing worker volume (§1).
- After the fix, `/retry` → **the exact-resume envelope dispatched LIVE**:
  worker journal 13:00:30Z — "attempt 1 dispatched a **required resume**
  (checkpoint `3cb49a16…`, decision `89f5bc3d…`) — envelope
  `2af524274422`"; pipeline **385** job **731** (then a further operator
  retry: pipeline **386** job **732**).
- A second lane restored the checkpoint and continued the turn with the
  REAL model: 32,344 in / 140,544 cached / 2,152 out (**$0.2005**) and
  19,163 in / 119,936 cached (**$0.1499**) — the large cache-read is the
  restored conversation context.
- **Delivery FAILED**: both resumed turns completed with an EMPTY
  candidate diff → run-level `repair_no_effect` → the run stays
  `blocked`; no Draft MR 2. Root causes (diagnosed, in the record's
  `refusal_resolution`): (1) the `/pause` landed ~5 s into turn 1,
  before any file edits existed — the preserved-WIP surface was empty by
  drill timing; (2) the resumed glm-5.3-flash turns on restored context
  completed without working-tree changes twice. Cross-runner
  preservation of new/modified/deleted FILES is therefore NOT
  demonstrated by a delivered diff on this trace.

## 5. The failed attempts (recorded, never removed)

| Attempt | Outcome | Root cause → fix |
| --- | --- | --- |
| flow 1 (issue #1) | refused BEFORE any model call | worker's bot token got 404 — the bot was not a member of the private project → setup now grants Developer membership |
| flow 2 (issue #2, run `ca78cda2`, job 723) | lane GREEN, run `blocked: harness_artifact_missing`; $0.2354 spent | the v0.36.0 SDK-lane template's unconditional `exit "$_driver_rc"` ends a SUCCESSFUL job before `candidate.diff` is built → MANUAL RESCUE: the disposable repo installs the pinned template with a one-line guarded-exit patch (upstream fix owed); ALSO the steering journal showed the lane-control URL unresolvable from the runner → the tunnel URL |
| flow 3 (issue #3) | **GREEN** (§3) | — |
| interrupt (issue #4) | mechanics green, delivery failed (§4) | the pause/checkpoint timing + resumed-turn model behavior |

## 6. Spend (cap $2 — honored)

| Lane job | What | SDK receipt |
| --- | --- | --- |
| 723 | flow attempt 2 (template-bug victim) | $0.2354 |
| 726 | arm 1, the green delivery | $0.2180 |
| 730 | arm 2, the paused turn (0 tokens — cut 5 s in) | $0.0000 |
| 731 | arm 2, resumed turn 1 | $0.2005 |
| 732 | arm 2, resumed turn 2 | $0.1499 |
| **total** | | **$0.8038** |

Plus planner/reviewer turns through the litellm gateway on the same
price class (durable `run_budgets`: 5 calls / ~182k tokens across the
three runs). Honest coverage gap: `usage_receipts` rows stayed EMPTY for
harness-lane runs — the SDK meta receipts and `run_budgets` counters are
the actual coverage (`cost.coverage`: partial).

## 7. The record and its derived verdict

`qualification/records/gitlab-ce-v1@live.json` — strict schema
(`legacy: false`), **zero validation findings**, loads cleanly in the
append-only store. The ONE claimed capability is `real-provider-e2e`
(live-provider, pass — the green arm 1); derived verdict over it:
**`supported`**. The interruption arm is recorded as INFORMATIONAL
evidence with outcome FAIL (mechanics live-proven, delivery failed) — a
committed failing entry on a CLAIMED capability would refuse the profile
gate, so the honest drill outcome rides as typed informational evidence
plus the refusal-resolution matrix. The #299-style promotion is still
refused twice over: `derive_evidence_tier` over committed traces reports
tier `none` and HOLDS the capability (no live `TraceRecord` is committed
under `qualification/traces/`), and the manifest's human gate lists the
profile **pending-approval**. Human approval on the promotion decision:
**pending**.

## 8. What a second engineer repeats

See `qualification/profiles/live-single-writer-v1.md` §5 — align
(idempotent, receipted), inventory, then `setup → preflight → flow →
interrupt → collect → teardown`. Authorized credentials only; the
disposable project is created and deleted by the driver; every operator
action is a native GitLab note by the configured approver.

## 9. Deviations from the plan (honest list)

1. The control plane is the working-tree build, not the promoted image —
   required for the #288 envelope; the digest axis stays mismatched.
2. The lane template ran with the one-line guarded-exit patch (manual
   rescue; upstream fix owed).
3. The interruption arm's job-level cancel was moot — the pause fence
   itself ended the job (recorded as such).
4. The flow needed three attempts (two diagnosed root causes, both fixed
   within the ≤3 fix-retry budget); the interruption arm needed the
   worker-volume infra fix and delivered no second candidate.
5. The disposable project was deleted after capture (per plan) — MR/job
   URLs in the bundle are dead links; the identities (ids, digests,
   traces) are the evidence.
