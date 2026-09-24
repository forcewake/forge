# Lab alignment runbook (R37-06 / #287)

How to move the real GitLab CE lab onto the intended supported profile —
and how to see, honestly, that it is not there yet. Written for a second
engineer: **no Python edits, no guessed env vars** (AT-07). Everything
here is podman-first — the lab containers are recreated with `podman`,
never `docker-compose` (the compose file is not how this lab runs).

The lab: `forge-app` (8420), `forge-worker`, `forge-postgres` (5433,
`forge`/`forge`), `forge-redis`, `forge-litellm`, plus `tinyproxy`.
These containers are production for this lab — **this runbook's
observation step is read-only, and the alignment itself is a separate,
approved change** (you will be stopping and recreating the app+worker;
nothing else touches the shared GitLab instance).

## 1. Where the lab actually is (observed 2026-09-24, read-only)

Produced by `scripts/inventory_lab.py` — rerun it any time; it never
writes anything outside `--out`:

```bash
uv run python scripts/inventory_lab.py --stage all --out qualification/inventory-$(date -u +%F).json
```

| Axis | Observed (inventory 2026-09-24) | Intended (pinned) |
| --- | --- | --- |
| Control-plane version (`GET /health`) | **0.28.0** | **0.36.0** (latest promotion) |
| Control-plane image | `localhost/forge:dev` @ `sha256:a09ad6a0…` (local dev build) | `ghcr.io/forcewake/forge` @ `sha256:d776da71…` |
| Schema head (postgres:5433 `alembic_version`) | **026** | **027** (repo chain head) |
| Installed `.gitlab-ci.yml` (project 68, `main`) | remote includes pinned at ref **`2fbc321`** | includes at **`v0.36.0`** tag refs |
| Installed lane wheel | not observable from the control host (lane venv is per-CI-job on the runner) | `forge-0.36.0-py3-none-any.whl` (sha256 `4ecd680a…`) |
| Budget caps in app/worker env | **absent** (`FORGE_BUDGET_PROFILES`, `FORGE_LANE_BUDGET_SECONDS` both unset) | numerical caps (§4) |
| GitLab CE | 19.3.2 (revision `34042bf7d00`, enterprise=false) — matches the profile | unchanged |
| Runners (project 68) | id 3 `forge-lab runner (podman docker executor)` **stale**; id 4 `unraid` online (serves the project) | one online docker-executor runner |

Derived verdict: **misaligned** — the full report with every identity,
digest and check result is `qualification/inventory-2026-09-24.json`
(stamp `forge.lab.inventory/1`). This is why the live flow's preflight
REFUSES: a paid flow against another build qualifies nothing.

## 2. The intended profile

Everything pinned comes from the repo's own evidence — never from source
HEAD and never from memory:

- release artifacts: `docs/releases/evidence/v0.36.0/promotion.json`
  (image digest, wheel sha256, required checks, canary stages);
- profile: `qualification/profiles/gitlab-ce-v1.md` (the frozen
  combination — GitLab CE 19.3.2, docker-executor runner,
  `claude-sdk-lane` template, claude-code 2.1.273, python-3.13, BYOK
  model route, `smoke` verification contract);
- record: `qualification/records/gitlab-ce-v1@0.37.0-preflight.json`
  (this cycle's preflight record with the refusal matrix below).

## 3. Align the control plane (app + worker + schema)

**Approve the change first** (this stops the lab's consumers), then:

```bash
# 0. Backup (mandatory — see backup-restore.md)
podman exec forge-postgres pg_dump -U forge -d forge -Fc -f /tmp/pre-alignment.dump
podman cp forge-postgres:/tmp/pre-alignment.dump ./pre-alignment-$(date -u +%F).dump

# 1. Stop the consumers (NOT postgres/redis/litellm)
podman stop forge-worker forge-app

# 2. Pull the PROMOTED image — the digest from promotion.json, never a tag
podman pull ghcr.io/forcewake/forge@sha256:d776da71fa6bc7ad9b8d2855394a884a8c3d19fc76808c2fa75945d90324188c

# 3. Migrate the schema with the NEW image, BEFORE the consumers start
#    (026 -> 027; the image ships the chain — no source checkout needed).
#    The lab's real wiring: network "podman", postgres reachable at
#    host.containers.internal:5433 (forge/forge).
podman run --rm --network podman \
  -e DATABASE_URL=postgresql+asyncpg://forge:forge@host.containers.internal:5433/forge \
  ghcr.io/forcewake/forge@sha256:d776da71… python -m forge.migrate

# 4. Recreate app + worker on the new image (podman, NOT docker-compose),
#    with the SAME env plus the caps block from §4 appended to .env first.
#    Keep the existing volume wiring (data/ -> /app/data, .secrets/ -> /app/.secrets)
#    and the port map (8420).

# 5. Verify (read-only)
curl -s http://localhost:8420/health            # "version": "0.36.0"
podman exec forge-postgres psql -U forge -d forge -t -A -c \
  "SELECT version_num FROM alembic_version"    # 027
uv run python scripts/inventory_lab.py --stage all   # verdict: aligned
```

Rollback data: the step-0 dump, plus the previous local image
(`localhost/forge:dev` @ `sha256:a09ad6a0…` — do not prune it until the
aligned lab has run one full task). Rolling back means stop consumers,
restore schema by downgrading is NOT supported — instead restore the
dump into a fresh database and recreate the containers on the old image
(see `docs/operations/backup-restore.md` for the restore order and the
reconciler behavior after restore).

## 4. Numerical budget caps (REQUIRED before any paid run)

The caps below are the REAL variables read by the source (grep-verified),
with the component that enforces each. Append to `.env` (the env file
both containers load) — EXAMPLE values, tune per task class:

```bash
# --- forge budget caps (R37-06) -------------------------------------------
# Model/tool caps per run, by budget class. Read by forge.config.Settings
# (FORGE_BUDGET_PROFILES; malformed JSON FAILS STARTUP — fail-closed), frozen
# into the RunSpec at freeze time, and ENFORCED at the component that incurs
# the call: forge.runs.service (RunService refuses planner/proposer/reviewer
# calls with BUDGET_EXHAUSTED — retries draw from the SAME frozen budget),
# with durable tracking/exhaustion in forge.durable.budgets (run_budgets).
FORGE_BUDGET_PROFILES={"trivial":{"max_calls":8,"max_tokens":40000,"wallclock_s":900},"standard":{"max_calls":40,"max_tokens":200000,"wallclock_s":3600},"heavy":{"max_calls":120,"max_tokens":600000,"wallclock_s":10800}}

# Lane wall clock. Read by forge.lane_driver inside the CI lane subprocess
# (the component that incurs the time); on expiry the running turn stops
# after the grace window. Default when unset: 1800s — set it explicitly.
FORGE_LANE_BUDGET_SECONDS=1800
FORGE_LANE_GRACE_SECONDS=60

# Iteration cap on commit cycles (forge.config, enforced by the reconciler:
# a run stops re-cycling failed commits after this many cycles).
FORGE_MAX_COMMIT_CYCLES=3
```

Notes an operator must know:

- A run's budget is frozen in its RunSpec at freeze time — a project
  cannot raise its own ceilings mid-run, and retries do not reset them.
- The budget classes are a closed set: `trivial`, `standard`, `heavy`
  (`forge.runs.harness_selection.BUDGET_CLASSES`); an unknown class name
  falls back to `standard`.
- The qualification driver refuses its PAID flow stage outright without
  caps (`scripts/qualify_gitlab_ce.py --stage flow` needs these env caps
  or `--max-budget-json`).
- **Honest gap:** there is no currency (USD) spend cap in the source —
  the enforced axes are calls/tokens/seconds/cycles. A money-denominated
  cap needs new work (e.g. LiteLLM-side budgets) and is NOT claimed here.

## 5. Refresh the installed CI template

The project's `.gitlab-ci.yml` (project 68) still includes the lane
templates at ref `2fbc321`. Update the four remote includes to the
promoted tag (through the GitLab UI or API — a normal commit to `main`):

```yaml
include:
  - remote: 'https://raw.githubusercontent.com/forcewake/forge/v0.36.0/ci/templates/claude-code.gitlab-ci.yml'
  - remote: 'https://raw.githubusercontent.com/forcewake/forge/v0.36.0/ci/templates/claude-sdk-lane.gitlab-ci.yml'
  - remote: 'https://raw.githubusercontent.com/forcewake/forge/v0.36.0/ci/templates/codex-sdk-lane.gitlab-ci.yml'
  - remote: 'https://raw.githubusercontent.com/forcewake/forge/v0.36.0/ci/templates/opencode-sdk-lane.gitlab-ci.yml'
```

Keep the project's own `smoke` job untouched (it IS the independent
verification contract). Verify read-only:

```bash
uv run python scripts/inventory_lab.py --stage lane
# installed_template.include_refs == ["v0.36.0"]
```

## 6. The lane wheel (R36-07 ladder)

The lane installs the promoted wheel per CI job (clean venv, exactly-one
wheel, sha256 verification, identity gate) — the ladder is frozen in
`qualification/profiles/gitlab-ce-v1.md` §3:

```
FORGE_LANE_PROMOTED_WHEEL_URL="https://github.com/forcewake/forge/releases/download/v0.36.0/forge-0.36.0-py3-none-any.whl"
FORGE_LANE_PROMOTED_WHEEL_SHA256="4ecd680afeb7b23301d079ff06386e91dd5c9b5756560ca88c8db8c2941b2005"
```

Verify the installed identity on the runner (or via
`scripts/qualify_gitlab_ce.py --stage install-check`, which performs the
whole ladder from a clean venv). The control-plane host cannot observe
the runner's venv — that is an honest `unverified` in the inventory, not
a green.

## 7. What this runbook does NOT fix (kept refused, honestly)

- **The GitLab lane-resume dispatch gap** (R37-07 / #288): the GitLab
  dispatch seam does not yet carry `FORGE_LANE_RESUME` / lane-control
  credentials the GitHub lane ships. Cross-runner resume stays
  offline-qualified until that bridge lands; the live flow's runner-loss
  drill stops at `blocked` + honest `/retry`.
- **The stale dedicated runner** (id 3): the online shared runner
  (id 4, `unraid`) currently serves the project, which the profile's §2
  accepts. Re-register the podman runner when a dedicated one is wanted
  again — until then the observation is recorded, not fixed by silence.

## 8. After the alignment: re-qualify, do not assume

1. `uv run python scripts/inventory_lab.py --stage all` — verdict must be
   `aligned` (every check `match`).
2. `uv run python scripts/qualify_gitlab_ce.py --stage preflight` — every
   gate green (caps present, identities aligned).
3. `uv run python scripts/qualify_gitlab_ce.py --stage install-check` —
   the wheel ladder from a clean venv.
4. Only then the PAID flow stage, under the §4 caps, with the Draft MR
   left for human review (forge never merges).
5. Land a NEW profile-qualification record with the observed post-
   alignment fingerprints — supersede, never edit history.

## 9. What was ACTUALLY executed on 2026-09-24 (R37-08 / #289)

`scripts/align_lab.py --apply` ran the alignment above as a receipted,
idempotent plan (every step carries a `receipt_id`; the receipts live in
`docs/evaluation/2026-09-24-live-single-writer/alignment-receipts.json`).
Deviations from §3's default route, all LIVE-found and recorded:

- **The image is the WORKING TREE, not the promoted digest.** The R37-08
  live flow needs the GitLab dispatch envelope (#288), which postdates
  the v0.36.0 promotion — the lab was rebuilt via
  `podman build -t localhost/forge:dev .` (image
  `sha256:e2a26c52…`). The inventory's digest axis therefore stays an
  honest `mismatch` until the next promoted release carries the envelope.
  The pre-alignment image stays tagged `localhost/forge:pre-r3708-*`
  (never pruned); the §3-step-0 pg_dump sits under
  `docs/evaluation/2026-09-24-live-single-writer/backups/`.
- **Two env pins joined the caps**: `FORGE_HARNESS_PREFERENCE=
  claude-sdk-lane,claude-code` (the profile's frozen exact-resume SDK
  lane — the chain must still INCLUDE the backend driver `claude-code`,
  the doctor enforces it) and `FORGE_LANE_CONTROL_URL=
  https://forge.forcewake.me` (the dispatched control URL must be
  reachable FROM THE RUNNER; `host.containers.internal:8420` is not).
- **The worker was missing the shared `/app/data` volume** — the app
  (which receives checkpoint uploads) and the worker (which looks them
  up on the filesystem authority) used DIFFERENT stores, so a `/retry`
  found no checkpoint and parked ("continuation source unknown").
  Restored via `scripts/align_lab.py --worker-mount <repo>/data:/app/data`
  (the compose file always intended this wiring).
- **§5 executed as written**: project 68's four remote includes moved to
  the `v0.36.0` tag refs (a normal commit to `main`).
- Post-alignment inventory: version `0.36.0` MATCH, schema `027` MATCH,
  template `v0.36.0` MATCH, caps MATCH, digest axis mismatched by
  design (above). The paid flow then ran —
  `docs/evaluation/2026-09-24-live-single-writer/README.md` is the
  executed trace.
