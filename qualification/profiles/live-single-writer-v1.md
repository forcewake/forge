# Live qualification profile: `live-single-writer-v1`

Issue **#289 / R37-08** (external review `4af6b33`). This document freezes
the ONE live single-writer configuration that produced
`qualification/records/gitlab-ce-v1@live.json` — everything a second
engineer needs to repeat the trace. The driver is
`scripts/run_live_qualification.py`; the alignment that made the paid run
legal is `scripts/align_lab.py` (receipts:
`docs/evaluation/2026-09-24-live-single-writer/alignment-receipts.json`).

> **Status (2026-09-24): EXECUTED — arm 1 GREEN, arm 2 mechanics GREEN /
> delivery FAILED (honest).** The uninterrupted flow is live-qualified
> end to end with the real model on the real provider. The interruption
> drill's mechanics (pause → verified checkpoint → honest blocked →
> `/retry` → exact-resume envelope → restored turn on a second lane) are
> live-proven, but both resumed turns delivered an EMPTY candidate
> (`repair_no_effect`) — cross-runner preservation of edited FILES is NOT
> demonstrated by a delivered diff. The record's claimed capability
> (`real-provider-e2e`, the green flow) derives **supported**
> (the lattice's honest minimum — one failed capability). Human approval
> on any promotion decision: **pending**.
>
> **Update (2026-09-25, R38-05/R38-06): the open delivery arm was
> DELIVERED by the useful-WIP trace, and the composition is now
> FROZEN.** `docs/evaluation/2026-09-25-useful-wip-resume/` re-ran the
> interrupted arm with the pause AFTER observed useful work (through the
> #302-finalized template): the pre-pause checkpoint carried all three
> file shapes with verified digests, a second runner restored the EXACT
> authorized checkpoint, the final candidate passed the precommitted
> oracle on the exact candidate sha and landed as a Draft MR — outcome
> `useful-wip-continued`, zero validation findings. R38-06 (#307) then
> froze the exact composition into
> `qualification/profiles/supported-gitlab-ce-v1.json` (stamp
> `forge.supported.profile/1`, every value from an actual receipt) and
> proved the cold installs (fresh / upgrade / verify) via
> `scripts/cold_install_check.py` — see
> `docs/operations/supported-profile-runbook.md` and
> `qualification/profiles/gitlab-ce-v1.md` §12. This document's
> historical arm-2 failure stays on record verbatim: superseded negative
> evidence is never rewritten. Human approval: still **pending**.

## 1. The frozen combination (identity card)

| Axis | Frozen value (observed 2026-09-24) |
| --- | --- |
| Provider | **GitLab CE 19.3.2** (revision `34042bf7d00`, enterprise=false) |
| Runner | GitLab runner id 4 `unraid` — instance (shared), docker executor, online |
| Control plane | repo working tree @ `4af6b331f703ac2b662edcedd9e1fc1dd80a5059` built as `localhost/forge:dev` (`sha256:e2a26c52…`) — carries the #288 dispatch envelope the promoted v0.36.0 image predates |
| Schema | alembic head **027** (migrated 026 → 027 before consumers started) |
| Lane driver | `claude-sdk-lane` — pinned via `FORGE_HARNESS_PREFERENCE=claude-sdk-lane,claude-code` (the chain must keep the backend driver per ADR-0015) |
| Harness | **claude-code CLI 2.1.273** (`FORGE_CLAUDE_VERSION`), driven by `python -m forge.lane_driver` |
| Lane install | `forge[interactive] @ git+https://github.com/forcewake/forge@4af6b331…` — the immutable sha pinned as the project CI variable `FORGE_LANE_REF` (no mutable tag) |
| Model route | **glm-5.3-flash** via the z.ai Anthropic-compatible gateway (`ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic`, BYOK token; harness egress via the lab proxy `http://192.168.1.18:3128`) |
| Lane-control | `FORGE_LANE_CONTROL_URL=https://forge.forcewake.me` (must be reachable FROM THE RUNNER — `host.containers.internal` does not resolve there; LIVE-found) |
| Budget caps | `FORGE_BUDGET_PROFILES` trivial/standard/heavy + `FORGE_LANE_BUDGET_SECONDS=1800` + grace 60 + commit cycles 3 — on BOTH consumers |
| Lane template | the v0.36.0 tag include, patched with ONE guarded-exit fix (see §4 — MANUAL RESCUE) |
| Verification | the `smoke` job: six exact `slugify` cases against `src/utils/text.py`, committed BEFORE any run, skipped only in the dispatch pipeline (`$FORGE_RUN_ID` — no candidate exists yet there) |
| Approver | `forcewake` (every command note is the approver's; the bot identity `@forge` is a Developer member of the disposable project — a LIVE-found prerequisite) |

## 2. The frozen acceptance task

Implement `slugify(text: str) -> str` in `src/utils/text.py` — lowercase;
every run of non-alphanumerics collapses to ONE `-`; no leading/trailing
`-`; empty-in → empty-out. The oracle (six exact cases) lives in the
repo's `.gitlab-ci.yml` smoke job and `tests/test_slugify.py`, committed
before any run; a candidate that touches either file FAILS the arm. The
full task text is frozen in `scripts/run_live_qualification.py`
(`ISSUE_BODY`).

## 3. What executed (2026-09-24, disposable project `forge-live-qual-2026-09-24`)

**Arm 1 — the uninterrupted flow: GREEN.** Native issue → `/implement` →
plan (run `60b9de7f…`, harness claude-sdk-lane, model glm-5.3-flash) →
`/go` → the real dispatch (pipeline 380, job 726, envelope digest
`7f1fed630bc0`) → the real lane (claude 2.1.273 + lane_driver, $0.22 SDK
receipt) → candidate artifacts → trusted publisher → **Draft MR !1** →
the oracle GREEN on the exact candidate `e670d42f…` (verification
pipeline 382; changed paths exactly `src/utils/__init__.py` +
`src/utils/text.py`). Run terminal state `ready_for_human`; the MR stays
a Draft — merge is a human decision.

**Arm 2 — the deliberate interruption: mechanics GREEN / delivery
FAILED.** `/pause` mid-turn applied (lane journal: interrupt
acknowledged, `pause_status: paused`); verified WIP checkpoint
`3cb49a16…` (`remote_ref 905194f0@3cb49a16…`, verified=true, files=0);
the pause fence itself ended the lane job (the job-level cancel was
therefore moot — HTTP 201, status `failed`); the run classified
`blocked` from the job event with zero LLM repair; `/retry` dispatched
the **exact-resume envelope LIVE** (attempt 1, checkpoint `3cb49a16…`,
decision `89f5bc3d…`, envelope `2af524274422`); a second lane restored
the checkpoint and continued with the real model ($0.20 + $0.15
receipts) — but BOTH resumed turns completed with an empty diff
(`repair_no_effect`); no Draft MR 2. Root causes recorded in the
record's `refusal_resolution`: the pause landed before any edits existed
(files=0), and the resumed glm-5.3-flash turns completed without
working-tree changes.

## 4. LIVE-found defects and the manual rescue (recorded, never removed)

1. **v0.36.0 SDK-lane template exit bug**: the job script ends with an
   unconditional `exit "$_driver_rc"` — a SUCCESSFUL driver run exits
   before `.forge/candidate.diff` is built (job 723: green lane, meta
   uploaded, diff absent, run `blocked: harness_artifact_missing`).
   MANUAL RESCUE: the disposable repo installs the pinned template with
   ONE guarded-exit patch (`_patched_lane_override` in the driver — the
   fetched template is patched verbatim, sha256-recorded). The upstream
   template must be fixed; until then `template_defaults_digest` in the
   live record names the PATCHED install.
2. **Bot membership**: a private project is invisible to the worker's
   bot token until the bot is a member (start_run 404 ×3, zero model
   calls). The setup phase now grants Developer membership.
3. **Lane-control URL**: the dispatched `host.containers.internal:8420`
   does not resolve from the external runner (steering journal
   `ConnectError`). Fixed to the tunnel `https://forge.forcewake.me`.
4. **Worker checkpoint-store sharing**: the lab worker ran WITHOUT the
   shared `/app/data` volume, so `/retry`'s checkpoint lookup
   (`authority=filesystem`) found nothing and parked (`uncertain`).
   Restored via `scripts/align_lab.py --worker-mount <repo>/data:/app/data`
   (the compose file's intended wiring).
5. **Usage receipts**: `usage_receipts` stayed empty for harness-lane
   runs — cost coverage comes from the lane meta SDK receipts (four
   jobs, **$0.80 total**, under the $2 cap) and `run_budgets` counters.
   `cost.coverage`: partial.

## 5. Repeating the trace (a second engineer)

1. `uv run python scripts/align_lab.py --apply --extra-env
   FORGE_HARNESS_PREFERENCE=claude-sdk-lane,claude-code --extra-env
   FORGE_LANE_CONTROL_URL=https://forge.forcewake.me --worker-mount
   <repo>/data:/app/data` — idempotent; every step receipts.
2. `uv run python scripts/inventory_lab.py --stage all` — version
   0.36.0, schema 027, template v0.36.0, caps numerical must MATCH (the
   image-digest axis stays honestly mismatched while the lab runs the
   working tree).
3. `uv run python scripts/run_live_qualification.py setup` then
   `preflight` (the app's own doctor must be green — note the litellm
   `/health` probe rides its 5 s timeout boundary; the driver re-probes
   read-only, three attempts recorded).
4. `flow` (paid, ~$0.25) then `interrupt` (paid, ~$0.55) then `collect`
   then `teardown` — every phase resumable; the evidence bundle
   (`live-run-evidence.json`) is the state.

## 6. What this profile does NOT claim

- No cross-runner FILE preservation claim (arm 2 delivered no diff).
- No promotion: the #298 trace-tier hold (no committed live trace file)
  plus the human gate keep the manifest at `pending-approval`; human
  approval is pending; the #298 trace-file join has no committed live
  traces yet, so the supported-profiles manifest holds the profile.
- No claim about the un-patched v0.36.0 template (it is broken on the
  success path — see §4.1).
- Spend cap $2: honored ($0.80 lane + planner/reviewer on the same
  price class).
