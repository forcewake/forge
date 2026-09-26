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

## 1. The pinned composition (identity card)

Everything below is pinned **together** from the CURRENT committed
qualification state: the freeze manifest
`qualification/profiles/supported-gitlab-ce-v1.json` (stamp
`forge.supported.profile/1`) and the v0.40.0 promotion records
(`docs/releases/evidence/v0.40.0/promotion.json`). **The manifest is
normative** — if this table and the manifest ever disagree, the manifest
wins and this runbook must be re-read; never install from a table.

| Axis | Pinned value | Receipt |
| --- | --- | --- |
| Freeze manifest | digest `bc147b0904cdad753cce959edefe407c923f16c5a8bcb45472743f7e7aa2bcf0` (frozen 2026-09-26 — the v0.40.0 release prep re-freeze) | the manifest vouches for itself (`freeze_supported_profile.py --check`) |
| Promoted release | **v0.40.0**, source `68f22b81…`, image `ghcr.io/forcewake/forge` @ `sha256:45de67f57c00…`, wheel sha256 `150bf797a7496955…` (byte-identical to the qualification wheel — the moving-tree gap stays closed), sdist sha256 `a61a23fe…` | `docs/releases/evidence/v0.40.0/promotion.json` |
| Control plane + worker (one build) | the promotion's image above; the executed-lab bind (`localhost/forge:dev` @ `sha256:ddcb9137…`, reports 0.39.0, rollback tag `pre-r3708-20260926T132558Z`) is the OTHER bound identity — each image axis must match ONE of them exactly | manifest `control_plane` |
| Lane wheel (what a cold install installs) | `forge-0.40.0-py3-none-any.whl` @ sha256 `150bf797a74969555a41e4cbbce680afffa52acb2f976a6807825afe135c72ff` (`dist/forge-0.40.0-py3-none-any.whl` — the qualification build; the released URL carries the SAME bytes — the v0.40.0 promotion record proves the identity) | manifest `lane.wheel` + the promotion |
| Schema | head **031**, declared predecessor **030** (the supported upgrade is exactly one step, 030 → 031) | manifest `control_plane.schema_revision` |
| Target template | `ci/templates/claude-sdk-lane.gitlab-ci.yml` @ sha256 `3d74be378bc70120…` — the bytes are frozen INTO the manifest; the install renders from the manifest, never from the working tree | manifest `target_template.frozen` |
| Runner | GitLab runner **id 4 `unraid`**, docker executor, online | `qualification/inventory-2026-09-25-v2.json` |
| Runtime / harness | python 3.13 (uv), **claude-code 2.1.273** | manifest `harness` |
| Model route | litellm `fast` → `openai/glm-5.3-flash` (lane model `glm-5.3-flash` via the z.ai Anthropic-compatible gateway) — **never contacted by any machine step in §3** | manifest `model_route` |
| Credential route | `gitlab-protected-variable` + `runner-redemption` | manifest `credential_route` |
| Verification contract | the required `smoke` job: six exact slugify cases + app rewired + legacy deleted; the candidate may not touch `.gitlab-ci.yml` or `tests/` | manifest `verification_contract` |

**Where v0.40.0 will re-pin.** At the next release the promotion lands
under `docs/releases/evidence/v0.40.0/promotion.json`,
`scripts/freeze_supported_profile.py` re-freezes the manifest (the old
one is archived first at
`qualification/profiles/supported-gitlab-ce-v1@<composition>.json` —
history is archived, never rewritten), and the pins that move together
are: `manifest_digest`, the promoted release block, `lane.wheel`
(name/path/sha — the version string changes), the schema heads (if a
`032` migration lands) and the frozen template sha (if the recipe
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
# forge-expects: exit 0 and the line "manifest verified" with digest 24df5a01…
uv run python scripts/freeze_supported_profile.py --check
```

**M3 — the lane wheel's identity.** The committed qualification bytes,
not a rebuild (see the failure-mode table: a rebuild of a moved tree
under the same version string is the mutable-tag refusal).

```bash
# forge-step: wheel-identity | machine
# forge-expects: the pinned sha256 78711c2887510a22e9cc698ef37c766ddf1d07d0cddf8b87d462e7651fdb36de appears for dist/forge-0.39.0-py3-none-any.whl
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
the declared predecessor 030, one-step migration to head 031, per-table
fingerprints preserved).

```bash
# forge-step: upgrade-disposable | machine
# forge-expects: exit 0; receipt upgrade.json shows seeded_at_head 030 and schema_after 031
uv run python scripts/cold_install_check.py --mode upgrade --receipt-out "$FORGE_COLD_EVIDENCE/upgrade.json"
```

**M7 — rollback + forward-recovery rehearsal** (on a second disposable
database: 031 → 030 rollback, fingerprints survive, 030 → 031 forward
recovery, seed == recovered).

```bash
# forge-step: rollback-recovery | machine
# forge-expects: exit 0; receipt rollback-recovery.json shows rollback_edge "031 -> 030", forward_edge "030 -> 031", fingerprints seed == recovered
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
calls. Sibling issue #343 owns the lab this window; a second engineer
with their OWN GitLab + runner runs these as part of H1, and the
maintainer runs them against the lab outside the freeze window.

**L1 — the GitLab smoke leg** (creates a disposable project on the
shared GitLab CE and runs the smoke job on the lab runner):

```bash
# forge-step: smoke-gitlab | lab
# forge-blocked: needs the shared lab's GitLab CE + runner id 4 (sibling #343 owns the lab this window); a second engineer runs it against THEIR GitLab with THEIR token
# forge-expects: exit 0; the smoke pipeline is green on exactly one job (smoke) and the lane job never ran
uv run python scripts/cold_install_check.py --mode fresh --receipt-out "$FORGE_COLD_EVIDENCE/fresh-gitlab.json"
```

**L2 — verify an installed environment** (read-only against the running
consumers):

```bash
# forge-step: verify-installed | lab
# forge-blocked: reads the shared lab containers forge-app/forge-worker (read-only, but the lab is sibling #343's window); the maintainer runs it after the freeze window
# forge-expects: exit 0 — every identity axis matches one of the manifest's two bound image identities, schema head 031, caps numerical, shared mount present
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
| The database is too old / foreign | `INCOMPATIBLE SCHEMA; preflight refuses BEFORE the migration runs` | the supported paths are fresh-at-head-031 or the single 030 → 031 step; an N-2-or-older database walks the recorded chain deliberately, never inside a cold install |
| Hand-edited manifest | `self-inconsistent` (and `does not vouch for itself`) | never edit the manifest; re-run `scripts/freeze_supported_profile.py` |
| Template rendered from the tree / foreign recipe | `a MISMATCHED template` | render only via `cold_install_check.py` (the manifest's frozen bytes are the recipe) |
| Upgrade reported no transition | `an UNCHANGED head` | the deliberate downgrade-to-030 step is load-bearing; do not skip it |
| Old worker beside a newer app (verify) | `OLD worker image beside a newer control plane` | re-run the alignment (`scripts/align_lab.py --apply`) — both consumers recreate from ONE image |
| Worker lost the data mount (verify) | `the WORKER's shared checkpoint mount` … absent | re-align with `--worker-mount <repo>/data:/app/data` |
| The lane job ran in a cold install | `the lane job must not run in a cold install` | `$FORGE_RUN_ID` must be absent in a cold install; investigate who set it — no model spend is allowed here |
| The disposable Postgres will not start | `the disposable postgres did not start` | install/start podman; the shared lab DB is never a fallback |

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
  schema_head_after_upgrade: <031>
  oracle_result: <slugify oracle: 6/6 OK>

lab_steps:                          # only if YOU ran §5 (own GitLab / supervised)
  smoke-gitlab: {ran: <bool>, pipeline: <id>, jobs: [<names>]}
  verify-installed: {ran: <bool>, findings: <match/divergence/refusal counts>}
  wip-continuation: {ran: <bool>, record: <path>, outcome: <reviewed-ready | …>}

qualification.support_decision: pending   # H4 — the maintainer's, separate, never filled here
```

## 8. Rollback, forward recovery, restore — what to do when it goes wrong

- **The disposable drills are the rehearsal.** M7 proves the data
  survives the declared rollback edge (031 → 030) and the forward
  recovery (030 → 031) on disposable data; M9 proves a restore refuses
  mismatched halves and wrong schema heads with ZERO model turns before
  the dispatch gate opens. Repeat M7+M9 after ANY restore on real data
  before dispatch resumes.
- **The lab's rollback tag** (`pre-r3708-20260925T092917Z`, recorded in
  the manifest's executed-lab block) is the deployed-composition
  equivalent; `docs/operations/upgrade.md` and
  `docs/operations/backup-restore.md` own the deployed procedures.
- **Never** roll back by editing YAML or source — a rollback that is
  not the declared edge is the `an UNCHANGED head` / undisclosed-edge
  refusal.

## 9. Machine-verified evidence for this kit (executed, dated)

Executed 2026-09-26 by `uv run python scripts/cold_install_check.py
--mode from-runbook` on the frozen working tree (macOS/arm64, podman
5.8.0): **9 machine steps executed as written, every documented
observable reproduced, 0 refusals; 4 human-step markers counted, 3
blocked-on-lab markers recorded.** Step-level observables:

- M4 fresh: observed wheel sha equals the pin `78711c28…`;
  doctor capabilities exit 0 from the installed venv.
- M5 delivery oracle: `slugify oracle: 6/6 OK` + `shape oracle: app
  rewired, legacy deleted` under the installed package's venv python,
  zero model calls.
- M6 upgrade: seeded at 030 → head 031; five seeded tables preserved
  (counts + sha256 fingerprints equal).
- M7 rollback rehearsal: `031 -> 030` rollback + `030 -> 031` forward
  recovery; seed == recovered on every fingerprint.
- M8 negative arms: all four fired — missing permission (HTTP 403
  stand-in), runner offline (stand-in), stale wheel (corrupted copy of
  the real bytes, ZERO pip invocations), incompatible schema N-2
  (disposable Postgres at 029, nothing migrated).
- M9 restore rehearsal: both drills pass; mismatched halves detected;
  wrong-schema (029) restore refused; `model_turns_before_refusals: 0`;
  the dispatch gate opened only after the consistent restore at head
  031 verified.

Blocked-on-lab (named): L1 smoke-gitlab, L2 verify-installed,
L3 wip-continuation (the shared lab and the paid lane are outside this
window — §5 records each reason).

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
