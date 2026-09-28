# Cold-install runbook — the second engineer's kit (R40-12 / #348)

> **Read this first — what this kit is, and is not.** This is the ONE
> entry document a second engineer follows to cold-install the selected
> release profile start-to-finish **without reading Forge source**. The
> machine part of the path below is **proven executable as written**
> (`uv run python scripts/cold_install_check.py --mode from-runbook`
> parses THIS file and executes its marked commands verbatim — §10
> records the executed evidence). What this kit is **not**: it is not
> "second-engineer-verified" — the second engineer's own install, their
> observed setup effort, and the bounded support decision are HUMAN
> deliverables (§4), deliberately outside what a machine can prove. The
> package status is **ready for the second engineer**, never more.

Every command lives in a marked block. A block starts with a
`# forge-step:` line that names the step and its kind:

| Marker | Meaning |
| --- | --- |
| `# forge-step: <id> \| machine` | the `from-runbook` mode executes these commands verbatim and checks the `# forge-expects:` observable — the machine-provable path |
| `# forge-step: <id> \| human` | a HUMAN deliverable (token UI clicks, the engineer's own install, the support decision) — counted with its observable, never executed |
| `# forge-step: <id> \| lab` | needs the shared lab (its GitLab CE + runner) or a paid model lane this window — recorded `blocked-on-lab` with the `# forge-blocked:` reason, never executed here |

Current honest counts: **9 machine steps, 4 human steps, 3 lab steps.**
The sibling document `docs/operations/supported-profile-runbook.md` is
the maintenance-side runbook (freeze/verify/re-freeze); this one is the
installer's.

---

## 1. The pinned composition (identity card — THE INSTALL MANIFEST)

Everything below is pinned **together** from the CURRENT committed
qualification state: the freeze manifest
`qualification/profiles/supported-gitlab-ce-v1.json` (stamp
`forge.supported.profile/1`) and the promotion records
(`docs/releases/evidence/v0.40.0/promotion.json` — the newest promotion;
the v0.41.0 promotion runs against this tag and re-binds on the
follow-up release commit). **The manifest is normative** — if this table
and the manifest ever disagree, the manifest wins and this runbook must
be re-read; never install from a table.

| Axis | Pinned value | Receipt |
| --- | --- | --- |
| Freeze manifest | digest `a365b101e0e90ca32724af1deb1cf6d3f97c4d32be5bb229682ff66f1d3c001a` (frozen 2026-09-27 — the v0.41.0 release follow-up re-freeze) | the manifest vouches for itself (`freeze_supported_profile.py --check`) |
| Promoted release | **v0.41.0**, source `536905c4…`, image `ghcr.io/forcewake/forge` @ `sha256:f1316aafe12d03…`, wheel sha256 `00919993453832008696fd7c4d8e4a67da122fbfd2d778dfdec8703975f7c05e` (built from the release tree carrying the cycle-end correctness fixes; the live traces executed the pre-final build 223d0f25 — the freeze and the record name both, honestly), sdist sha256 `e53f9a02…` | `docs/releases/evidence/v0.41.0/promotion.json` |
| Control plane + worker (one build) | the promotion's image above; the executed-lab bind (`localhost/forge:dev` @ `sha256:11c4bb30…`, reports 0.41.0, schema head 032, rollback tag `pre-r3708-20260927T024816Z`) is the OTHER bound identity — each image axis must match ONE of them exactly | manifest `control_plane` |
| Lane wheel (what a cold install installs) | `forge-0.41.0-py3-none-any.whl` @ sha256 `00919993453832008696fd7c4d8e4a67da122fbfd2d778dfdec8703975f7c05e` (`dist/forge-0.41.0-py3-none-any.whl` — the `uv build` of the CURRENT tree incl. the R41-10 preflight + the R41-09 live-found publisher branch patch; the committed receipt `qualification/profiles/receipts/working-tree-wheel-v2.json` covers a dist-less checkout) | manifest `lane.wheel` + the wheel receipt |
| Lane runtime + dependencies | python **3.13** (uv standalone in the lane job); the wheel's dependency set resolved by uv from the committed `uv.lock` at the freeze (exact pins, e.g. `fastapi==0.135.2`, `uvicorn[standard]==0.42.0`); the harness CLI **claude-code 2.1.273** pinned inside the lane job | `pyproject.toml` + `uv.lock` + manifest `harness` |
| Schema | head **032**, declared predecessor **031** (the supported upgrade is exactly one step, 031 → 032; guarded downgrades — see §8) | manifest `control_plane.schema_revision` |
| Target template | `ci/templates/claude-sdk-lane.gitlab-ci.yml` @ sha256 `3d74be378bc70120…` — the bytes are frozen INTO the manifest; the install renders from the manifest, never from the working tree | manifest `target_template.frozen` |
| Runner | GitLab runner **id 4 `unraid`**, docker executor, online | `qualification/inventory-2026-09-25-v2.json` |
| Harness | **claude-code 2.1.273** (the lane's pinned harness binary) | manifest `harness` |
| Model route | litellm `fast` → `openai/glm-5.3-flash` (lane model `glm-5.3-flash` via the z.ai Anthropic-compatible gateway) — **never contacted by any machine step in §3** | manifest `model_route` |
| Credential mode | `gitlab-protected-variable` + `runner-redemption` (both declared delivery modes; §5's live legs are where they are exercised) | manifest `credential_route` |
| Delivery preflight | `forge doctor --project <id>` validates the SELECTED mode through the same resolver the dispatch uses (per-mode carrier/grant/scope rules — see docs/operations/supported-profile-runbook.md §8b); the #364 duplicate-ambient workaround is retired from the recipe (machine-proven by tests; the runner-bound live re-verification is #377's window) | doctor checks `credential.*` + `onboarding.review_scope` |
| Verification policy | the verification is INDEPENDENT, PRECOMMITTED and CANDIDATE-BOUND: the target project's own `smoke` job (six exact slugify cases + app rewired + legacy deleted) is committed BEFORE any run and must be green on the CURRENT candidate sha; the candidate may not touch `.gitlab-ci.yml` or `tests/`; the merge decision stays human (the bot never merges); and every §3 machine step here spends ZERO model calls — a cold install proves delivery surfaces, never model turns | manifest `verification_contract` + §6 |

**The evidence kinds this kit separates (never blended).** The record
behind this profile (`qualification/records/gitlab-ce-v1@0.41.0.json`)
marks every arrow by its evidence kind AND composition:

| Evidence kind | What it is here | Status on this freeze |
| --- | --- | --- |
| Source review | the required CI checks on the tagged sha (lint/typecheck/test matrices) | the v0.40.0 promotion's checks stand; the v0.41.0 checks run with this tag's release |
| Release canary | the promotion canary stages (fresh install + seeded previous-head upgrade on the released image) | v0.40.0's canary (029→031 with data preservation) stands; v0.41.0's expected edge is 031→032 |
| Native transport qualification | the deployment-ops drills + cold-install proofs bound to THIS manifest digest (native note ingress, dispatch, rollback/restore drills) | CURRENT composition — re-executed at this freeze (see §9) |
| Model-task execution | the live model-consuming traces (the v2 delivery loop, the redemption trace) | HISTORICAL compositions (the v0.39.0-promoted tree; the `ddcb9137` lab) — the current-composition live legs are this cycle's #364 (the correction round) and #365 (the grant pairing); the live /fix leg stays PENDING |
| External acceptance / human approval | the pilot records and the maintainer's bounded support decision | SEPARATE human artifacts (`qualification/profile-approvals.json`) — this kit approves nothing |

**Where v0.41.0 will re-pin.** At the release the promotion lands
under `docs/releases/evidence/v0.41.0/promotion.json`,
`scripts/freeze_supported_profile.py` re-freezes the manifest bound to
the promoted digest (the old one is archived first at
`qualification/profiles/supported-gitlab-ce-v1@<composition>.json` —
history is archived, never rewritten), and the pins that move together
are: `manifest_digest`, the promoted release block, `lane.wheel`
(name/path/sha — the version string changes), the schema heads (if a
`033` migration lands) and the frozen template sha (if the recipe
changes). §9 of `docs/operations/supported-profile-runbook.md` owns the
re-freeze procedure. This runbook's identity card is then refreshed;
the commands in §3 never change with a re-pin.

## 2. Prerequisites

**Host** (a clean machine or a disposable account on one):

1. Python **3.13** and [uv](https://docs.astral.sh/uv/) on `PATH`
   (`uv --version` answers).
2. `podman` ≥ 4 (the disposable Postgres legs pull
   `postgres:17-alpine`; the shared lab's runtime is NOT used).
3. `bash`, `shasum` (macOS) / `sha256sum` (Linux — substitute in §3
   M3), and a checkout of this repository at the frozen state.

**Network reachability** — outbound to exactly:

- `pypi.org` + `files.pythonhosted.org` (the wheel's dependencies);
- `github.com` + `release-assets.githubusercontent.com` **only if** you
  install the URL-pinned wheel instead of the committed `dist/` bytes.

The model gateway (`api.z.ai`) is **never contacted** by any §3 step.
The shared lab containers (`forge-app`, `forge-worker`, `forge-postgres`,
`forge-redis`, `forge-litellm`) are **never touched** by any §3 step —
every database is disposable (`forge-cold-*` names, deleted afterwards).

**Tokens** — only the §5 lab steps need one: a GitLab personal access
token with **`api` scope** for a disposable target project. Creating it
is UI clicks: human step H2.

## 3. The machine steps (execute in order; every command verbatim)

Set a 2-hour block aside, run `date -u +%FT%TZ` before and after each
step, and write the timestamps into the observation report (§7). The
machine steps' measured wall time on the frozen tree is recorded in §9
(~1 minute for M1–M9 on the 2026-09-26 execution; plan for first-run
image pulls).

**M1 — evidence directory.** Every later step writes its receipt here.

```bash
# forge-step: evidence-dir | machine
# forge-expects: the directory exists and is writable; every later receipt lands in it
export FORGE_COLD_EVIDENCE="${FORGE_COLD_EVIDENCE:-$(mktemp -d)}"
echo "$FORGE_COLD_EVIDENCE"
```

**M2 — the manifest vouches for itself.**

```bash
# forge-step: manifest-selfcheck | machine
# forge-expects: exit 0 and the line "manifest verified" with digest 3fcab8bc…
uv run python scripts/freeze_supported_profile.py --check
```

**M3 — the lane wheel's identity.** The committed qualification bytes,
not a rebuild (see the failure-mode table: a rebuild of a moved tree
under the same version string is the mutable-tag refusal).

```bash
# forge-step: wheel-identity | machine
# forge-expects: the pinned sha256 bb8f18a8490612954827c848479a0d609d620054cba25613d3d319534117f95b appears for dist/forge-0.41.0-py3-none-any.whl
shasum -a 256 dist/*.whl
```

**M4 — fresh install into a disposable venv** (clean venv, wheel by
sha256, template from the manifest, offline preflights; no GitLab, no
model calls).

```bash
# forge-step: fresh-disposable | machine
# forge-expects: exit 0; receipt fresh.json shows the observed wheel sha equals the pin and doctor_capabilities_exit 0
uv run python scripts/cold_install_check.py --mode fresh --skip-gitlab --receipt-out "$FORGE_COLD_EVIDENCE/fresh.json"
```

**M5 — the initial-delivery oracle, from the installed artifacts.** The
smoke job's OWN script (extracted verbatim from the manifest-rendered
CI) runs on the seeded GOLD state under the INSTALLED package's venv
python. This is the install-level delivery trace — gold state, zero
model calls; the model-turn traces are §5's lab steps.

```bash
# forge-step: delivery-oracle | machine
# forge-expects: exit 0; receipt oracle-replay.json shows oracle exit 0 and "slugify oracle: 6/6 OK"
uv run python scripts/cold_install_check.py --mode oracle-replay --receipt-out "$FORGE_COLD_EVIDENCE/oracle-replay.json"
```

**M6 — the data-bearing upgrade on a disposable Postgres** (seeded at
the declared predecessor 031, one-step migration to head 032, per-table
fingerprints preserved).

```bash
# forge-step: upgrade-disposable | machine
# forge-expects: exit 0; receipt upgrade.json shows seeded_at_head 031 and schema_after 032
uv run python scripts/cold_install_check.py --mode upgrade --receipt-out "$FORGE_COLD_EVIDENCE/upgrade.json"
```

**M7 — rollback + forward-recovery rehearsal** (on a second disposable
database: the honest-downgrade guard REFUSES the 032 → 031 rollback while
linkage rows exist — observed as the typed §8a refusal — then the
documented operator path archives the counted rows into the receipt and
the declared edge executes; fingerprints survive; 031 → 032 forward
recovery; seed == recovered).

```bash
# forge-step: rollback-recovery | machine
# forge-expects: exit 0; receipt rollback-recovery.json shows the rollback_guard typed refusal + archived rows, rollback_edge "032 -> 031", forward_edge "031 -> 032", fingerprints seed == recovered
uv run python scripts/cold_install_check.py --mode upgrade --rollback-rehearsal --receipt-out "$FORGE_COLD_EVIDENCE/rollback-recovery.json"
```

**M8 — the four negative arms** (each documented failure mode fired its
typed refusal BEFORE the unsafe or paid action; see §6 for the texts).

```bash
# forge-step: negative-arms | machine
# forge-expects: exit 0; receipt negative-arms.json shows all four arms refusal_fired=true with zero pip installs past the wheel gate and the N-2 database never migrated
uv run python scripts/cold_install_check.py --mode negative-arms --receipt-out "$FORGE_COLD_EVIDENCE/negative-arms.json"
```

**M9 — the restore rehearsal** (works/checkpoints/pins consistency on
disposable data; the mismatched-halves and wrong-schema restores refuse
with ZERO model turns before the dispatch gate opens).

```bash
# forge-step: restore-rehearsal | machine
# forge-expects: exit 0; report restore-rehearsal.json shows both drills pass and model_turns_before_refusals 0
uv run python scripts/cold_install_restore_rehearsal.py --report "$FORGE_COLD_EVIDENCE/restore-rehearsal.json"
```

**Done.** If M1–M9 are green you have installed the pinned composition
from immutable artifacts, delivered the gold state through the
independent oracle, upgraded data across the declared schema edge,
rehearsed rollback/forward recovery and the restore gate, and watched
every documented failure mode refuse in time. Fill in §7.

## 4. The human steps (what a machine cannot do — counted, not executed)

**H1 — the second engineer's install.** A second engineer — not the
maintainer, not a script — performs §2+§3 on a clean host and records
what actually happened. The observable: **their completed §7
observation report**, deviations and manual fixes included. The issue's
acceptance ("installation completes without editing Forge source or
hand-fixing target YAML") is proven HERE, by a human, once.

```bash
# forge-step: second-engineer-install | human
# forge-expects: a second engineer completes §2+§3 on a clean host without editing source or hand-fixing YAML; their §7 report exists with observed timings
```

**H2 — token provisioning (UI clicks).** Creating the GitLab personal
access token with `api` scope is a browser flow a machine must not
automate. The observable:

```bash
# forge-step: token-provisioning | human
# forge-expects: a token with api scope exists and answers 200 — curl -s -o /dev/null -w '%{http_code}' -H "PRIVATE-TOKEN: $GITLAB_TOKEN" "$GITLAB_URL/api/v4/user"
```

**H3 — the observation report.** The engineer fills §7 with OBSERVED
values (timestamps from the run, deviations as they happened). The
observable: the completed report attached to the issue/PR, with
`onboarding.time_to_first_reviewable` and `onboarding.manual_fixes`
filled from the actual run.

```bash
# forge-step: observation-report | human
# forge-expects: the §7 report is filled with observed setup time, deviations and manual-fix count (aim: 0) — not estimates
```

**H4 — the bounded support decision.** Whether THIS exact profile gets
supported is the maintainer's separate, bounded decision naming the
exclusions (§1 of `docs/operations/support-agreement.md`; the manifest's
`evidence_records.human_support_approval` stays `pending` until then;
approvals live in `qualification/profile-approvals.json`). The
observable: the decision record, with exclusions named.

```bash
# forge-step: support-decision | human
# forge-expects: qualification/profile-approvals.json carries the maintainer's bounded decision for gitlab-ce-v1 naming exclusions (until then the manifest honestly holds human_support_approval pending)
```

## 5. The lab-bound steps (blocked-on-lab this window — named, not run)

These need the shared lab (its GitLab CE + runner id 4) or paid model
calls. Sibling issues #364/#365 own the lab's live traces this
window; a second engineer
with their OWN GitLab + runner runs these as part of H1, and the
maintainer runs them against the lab outside the freeze window.

**L1 — the GitLab smoke leg** (creates a disposable project on the
shared GitLab CE and runs the smoke job on the lab runner):

```bash
# forge-step: smoke-gitlab | lab
# forge-blocked: needs the shared lab's GitLab CE + runner id 4 (siblings #364/#365 own the lab's live traces this window); a second engineer runs it against THEIR GitLab with THEIR token
# forge-expects: exit 0; the smoke pipeline is green on exactly one job (smoke) and the lane job never ran
uv run python scripts/cold_install_check.py --mode fresh --receipt-out "$FORGE_COLD_EVIDENCE/fresh-gitlab.json"
```

**L2 — verify an installed environment** (read-only against the running
consumers):

```bash
# forge-step: verify-installed | lab
# forge-blocked: reads the shared lab containers forge-app/forge-worker (read-only, but the lab's live traces are siblings #364/#365's window); the maintainer runs it after the freeze window
# forge-expects: exit 0 — every identity axis matches one of the manifest's two bound image identities, schema head 032, caps numerical, shared mount present
uv run python scripts/cold_install_check.py --mode verify --receipt-out "$FORGE_COLD_EVIDENCE/verify.json"
```

**L3 — the useful-WIP continuation trace (the #326 playbook).** The
review's second named trace: one installed task reaches reviewed-ready
with exact verification and zero automatic merges; one cross-runner
continuation preserves new/modified/deleted files with the collected
candidate from the right generation. The recorded shape is the v2 trace
(`docs/evaluation/2026-09-25-supported-composition-v2/`, outcome
reviewed-ready) and the driver is `scripts/run_useful_wip_resume.py`
(its phases: setup → preflight → interrupt → revision → interrupt →
review → collect → teardown). It genuinely needs the lab: the app
ingress at `localhost:8420` (forge-app), the pinned runner id 4, and
PAID lane model calls — none of which a disposable install provides.

```bash
# forge-step: wip-continuation | lab
# forge-blocked: needs the shared lab app ingress (localhost:8420 = forge-app), the lab runner id 4, and PAID lane model calls — the #326 playbook cannot run disposable-only; the recorded v2 trace (docs/evaluation/2026-09-25-supported-composition-v2/) is the shape the second engineer repeats under the maintainer's supervision
# forge-expects: the useful-wip-resume record ends reviewed-ready with the candidate verified on the exact sha and zero automatic merges (see docs/operations/supported-profile-runbook.md §10)
uv run python scripts/run_useful_wip_resume.py setup --evidence "$FORGE_COLD_EVIDENCE/wip-evidence.json" --record "$FORGE_COLD_EVIDENCE/wip-record.json" --project-name forge-second-engineer-wip
```

## 6. Failure modes and their typed refusals

Every row's **typed refusal** is a verbatim substring of what the tool
actually prints — `tests/test_cold_install_runbook.py` holds the tool
to this table. If you see the refusal, apply the next action; never
work around a refusal by editing source or YAML.

| Symptom | Typed refusal (verbatim) | Next action |
| --- | --- | --- |
| Token missing / rejected by GitLab | `MISSING PERMISSION` … `preflight refuses BEFORE the first project is created or any model call` | re-provision the token with `api` scope (H2); nothing was created, nothing was spent |
| The runner is down | `RUNNER UNAVAILABLE; preflight refuses BEFORE dispatching the paid lane job` | bring runner id 4 online (`GET /api/v4/runners/4` shows `online`), re-run; no dispatch was paid |
| A rebuilt wheel hashes differently | `a DIFFERENT wheel under the SAME version string` … `refusing BEFORE anything installs or executes` | do NOT install those bytes: use the committed `dist/` file (M3), or re-freeze (§9 of the ops runbook) — the tree moved under the frozen version |
| `dist/` has no pinned wheel | `but the file is absent` | `uv build` ON THE FROZEN TREE only; on a moved tree the rebuild triggers the mutable-tag refusal above (correct behavior) |
| The database is too old / foreign | `INCOMPATIBLE SCHEMA; preflight refuses BEFORE the migration runs` | the supported paths are fresh-at-head-032 or the single 031 → 032 step; an N-2-or-older database walks the recorded chain deliberately, never inside a cold install |
| Hand-edited manifest | `self-inconsistent` (and `does not vouch for itself`) | never edit the manifest; re-run `scripts/freeze_supported_profile.py` |
| Template rendered from the tree / foreign recipe | `a MISMATCHED template` | render only via `cold_install_check.py` (the manifest's frozen bytes are the recipe) |
| Upgrade reported no transition | `an UNCHANGED head` | the deliberate downgrade-to-031 step is load-bearing; do not skip it |
| Old worker beside a newer app (verify) | `OLD worker image beside a newer control plane` | re-run the alignment (`scripts/align_lab.py --apply`) — both consumers recreate from ONE image |
| Worker lost the data mount (verify) | `the WORKER's shared checkpoint mount` … absent | re-align with `--worker-mount <repo>/data:/app/data` |
| The lane job ran in a cold install | `the lane job must not run in a cold install` | `$FORGE_RUN_ID` must be absent in a cold install; investigate who set it — no model spend is allowed here |
| The disposable Postgres will not start | `the disposable postgres did not start` | install/start podman; the shared lab DB is never a fallback |
| A rollback refused while round/grant linkage rows exist | `is never dropped by a downgrade` | that is the honest-downgrade guard working (§8a): archive the counted rows explicitly — accepting the named loss — then retry the declared edge; never force past it |

**Credential-delivery preflight failures (R42-03 — prose, not tool
output).** `forge doctor --project <id>` (an onboarding command, not one
of §3's machine steps) additionally validates the selected delivery
mode and names its own remedies: a missing native carrier
(`native_carrier_absent` — provision the `FORGE_MODEL_*` CI/CD variable
once; an unrelated ambient variable does not satisfy the route), a
protected-only carrier on unprotected factory refs
(`carrier_ref_incompatible` — MR pipelines cannot see protected
variables; mask-not-protect the carrier or protect the refs), and a
correction-enabled target without `implement.paths` (`scope_missing` —
declare the scope BEFORE readiness, or every `/fix` needs the
material-revision route). These never require the ambient
`ANTHROPIC_AUTH_TOKEN` duplicate the #364 run provisioned just to keep
doctor green; see docs/operations/supported-profile-runbook.md §8b for
the full rule table and the honest bounds of the retirement.

## 7. The observation report (fill with OBSERVED values)

Copy this template into the issue/PR when H1 completes. Observed —
timestamps, counts, and what actually happened; never estimates.

```yaml
schema: forge.onboarding.observation/1
engineer: <name>                    # the SECOND engineer (not the maintainer)
date: <YYYY-MM-DD>
host: <os / cpu / ram>
tool_versions: {uv: <v>, podman: <v>, python: <3.13.x>}

onboarding.time_to_first_reviewable: <wall time from §2 start to M5 green — OBSERVED>
onboarding.manual_fixes: <count>    # aim: 0
manual_fixes:                       # one entry per fix, or empty
  - {step: <M#>, what: <what you had to fix by hand>, cause: <root cause>}
deviations:                         # anything that differed from §3, or empty
  - {step: <M#>, deviation: <what differed>}
step_timings:                       # date -u +%FT%TZ before/after each step
  evidence-dir:      {start: <ts>, end: <ts>}
  manifest-selfcheck: {start: <ts>, end: <ts>}
  wheel-identity:    {start: <ts>, end: <ts>}
  fresh-disposable:  {start: <ts>, end: <ts>}
  delivery-oracle:   {start: <ts>, end: <ts>}
  upgrade-disposable: {start: <ts>, end: <ts>}
  rollback-recovery: {start: <ts>, end: <ts>}
  negative-arms:     {start: <ts>, end: <ts>}
  restore-rehearsal: {start: <ts>, end: <ts>}

qualification.artifact_identity:    # paste from your run, must equal §1
  manifest_digest: <hex64>
  lane_wheel_sha256: <hex64>
  target_template_sha256: <hex64>
  schema_head_after_upgrade: <032>
  oracle_result: <slugify oracle: 6/6 OK>

lab_steps:                          # only if YOU ran §5 (own GitLab / supervised)
  smoke-gitlab: {ran: <bool>, pipeline: <id>, jobs: [<names>]}
  verify-installed: {ran: <bool>, findings: <match/divergence/refusal counts>}
  wip-continuation: {ran: <bool>, record: <path>, outcome: <reviewed-ready | …>}

qualification.support_decision: pending   # H4 — the maintainer's, separate, never filled here
```

## 8. Rollback, forward recovery, restore — what to do when it goes wrong

- **The disposable drills are the rehearsal.** M7 proves BOTH halves of
  the rollback story on disposable data: the honest-downgrade guard
  REFUSES the declared edge (032 → 031) while linkage rows exist (the
  typed §8a refusal, observed), and the operator path — archive the
  counted rows explicitly, then retry — carries the edge through with
  every seeded fingerprint preserved across the forward recovery
  (031 → 032, seed == recovered). M9 proves a restore refuses mismatched
  halves and wrong schema heads with ZERO model turns before the
  dispatch gate opens. Repeat M7+M9 after ANY restore on real data
  before dispatch resumes.
- **The lab's rollback tag** (`pre-r3708-20260926T132558Z`, recorded in
  the manifest's executed-lab block) is the deployed-composition
  equivalent; `docs/operations/upgrade.md` and
  `docs/operations/backup-restore.md` own the deployed procedures.
- **Never** roll back by editing YAML or source — a rollback that is
  not the declared edge is the `an UNCHANGED head` / undisclosed-edge
  refusal.

### 8a. Rollback while a child review round executes — the typed refusal

The schema edge 031→032 is the round/collaboration edge, and its
downgrades are GUARDED: a rollback that would destroy linkage evidence
REFUSES typed BEFORE any change (the guard counts rows first; the
schema is left untouched — the refusal is itself the safe state):

| Attempted downgrade | Typed refusal (verbatim prefix) | Why it refuses |
| --- | --- | --- |
| 032 → 031 while round/collaboration rows exist | `collaboration_targets holds N row(s) — the target linkage is branch-identity evidence and is never dropped by a downgrade` | an active target row is the only record that root and round children share ONE collaboration surface; post-032 children have independent ids that would re-derive DIFFERENT branches |
| 031 → 030 while round rows exist | `review_rounds holds N round row(s) — the round linkage is supersession evidence and is never dropped by a downgrade` | round rows are the only record that a ready delivery was superseded by a later correction round |
| 030 → 029 while amendment rows exist | `budget_amendments holds N amendment row(s) — the amendment records are budget authorization evidence …` | dropped rows would leave raised limits reading as originally approved |
| 029 → 028 while grant rows exist | `operation_grants holds N authority row(s) — the keyed grant records are authorization evidence and are never dropped by a downgrade` | re-persisting the same attempt+route would mint FRESH windows over standing ones |

**A child round executing** means its `review_rounds` row AND its
`collaboration_targets` linkage exist — so a mid-round rollback attempt
refuses on BOTH guards. The operator path: let the round settle (it
ends `ready_for_human` or parks typed on its own budget), or cancel it
through the NATIVE surface (the cancellation leaves delivery 1 and the
MR readable); then, if a downgrade is truly required, archive the rows
EXPLICITLY (accepting the named loss — the refusal text says exactly
what is lost) before re-running the downgrade. Both round-edge guards
are tested: the 032 guard
(`tests/test_collaboration_target.py::…test_the_downgrade_guard_refuses_while_rows_exists`)
and the 031 guard
(`tests/test_review_rounds.py::TestRoundSchemaDowngradeGuard`); the
030/029 guards follow the same precedent.

### 8b. Grants in flight across a rollback

- A minted operation grant carries an ABSOLUTE deadline fixed at
  dispatch authorization — it survives control-plane restarts and image
  rollbacks unchanged (proven live on the redemption trace: a cold
  restart mid-lane preserved the deadline and the idempotent replay
  held).
- The schema-level guards above mean a rollback never SILENTLY drops
  the grant ledger: while `operation_grants` holds rows, the 029
  downgrade refuses typed. Revocation is NOT a rollback side-effect —
  generations are superseded only through the broker's rotation path
  (a superseded-generation token is refused on redemption; the audit
  trail keeps both).
- A lane whose redemption is refused (expired grant, rotated
  generation, mismatched ref) fails CLOSED — `credential_redemption_failed`,
  ZERO model turns, no ambient fallback — and re-enters through the
  native `/retry <run> restart`. An in-flight lane during a
  control-plane rollback therefore parks typed; it never spends and
  never loops.

## 9. Machine-verified evidence for this kit (executed, dated)

Executed 2026-09-27 by `uv run python scripts/cold_install_check.py
--mode from-runbook` at the R41-08/#363 release-candidate freeze
(manifest `ab08a317…`, wheel `forge-0.41.0` @ `bb8f18a8…`; macOS/arm64,
podman; the CURRENT pins above are the R41-10/#365 re-freeze
`57d3839f…` / `2616d221…`): **9 machine steps executed as written,
every documented observable reproduced, 0 refusals; 4 human-step
markers counted, 3 blocked-on-lab markers recorded.** Step-level
observables:

- M2 manifest self-check: the freeze verified the manifest at digest
  `ab08a317…` (it vouches for itself).
- M3 wheel identity: the pinned sha256 `bb8f18a8…` observed for
  `dist/forge-0.41.0-py3-none-any.whl`.
- M4 fresh: the observed wheel sha equals the pin; doctor capabilities
  exit 0 from the installed venv.
- M5 delivery oracle: `slugify oracle: 6/6 OK` + `shape oracle: app
  rewired, legacy deleted` under the installed package's venv python,
  zero model calls.
- M6 upgrade: seeded at 031 → head 032; five seeded tables preserved
  (counts + sha256 fingerprints equal) — migration 032's target backfill
  materialized the seeded run's collaboration target (1 row, provenance
  legacy), untouched by the fingerprints.
- M7 rollback rehearsal: the honest-downgrade guard REFUSED the
  `032 -> 031` edge first (`collaboration_targets holds 1 row(s) — …
  never dropped by a downgrade`, the §8a typed refusal, observed with
  the schema untouched); the operator path then archived the counted
  row into the receipt, the declared edge executed (`032 -> 031`
  rollback + `031 -> 032` forward recovery) and seed == recovered on
  every fingerprint.
- M8 negative arms: all four fired — missing permission (HTTP 403
  stand-in), runner offline (stand-in), stale wheel (corrupted copy of
  the real bytes, ZERO pip invocations), incompatible schema N-2
  (disposable Postgres at 029, nothing migrated).
- M9 restore rehearsal: both drills pass; mismatched halves detected;
  wrong-schema (029) restore refused; `model_turns_before_refusals: 0`;
  the dispatch gate opened only after the consistent restore at head
  032 verified.

Beside the kit, at the same freeze: `--mode verify` (read-only against
the running lab) rendered **8 match, 1 named divergence (the
integration project's own template), 0 refusals** — the deployed lab
head 031 matches the manifest's executed-lab bind (the two-bound schema
identity), and both consumers match the `ddcb9137…` image bind.

Blocked-on-lab (named): L1 smoke-gitlab, L2 verify-installed,
L3 wip-continuation (the shared lab and the paid lane are outside this
window — §5 records each reason; the sibling issues #364/#365 own this
cycle's live traces).

This section is evidence for the KIT, not for H1: the second
engineer's own observed numbers land in §7.

## 10. The support boundary (unchanged by this kit)

Install evidence is not a support promise. The manifest's
`evidence_records.human_support_approval` stays **pending**; the
bounded decision (H4) is the maintainer's, recorded in
`qualification/profile-approvals.json`, and must name the exclusions
(the manifest's `exclusions` list is normative — among others: the
batch lane templates cannot restore WIP; merge/deploy stay human).
Cross-references: `docs/operations/support-agreement.md`,
`docs/operations/supported-profile-runbook.md` (§6 keeps the four
evidence records distinct), `docs/operations/backup-restore.md`,
`docs/operations/ops-drills.md`.
