# Supported-profile runbook (R38-06 / #307)

How a second engineer installs the ONE exact supported deployment
composition from immutable artifacts — and repeats the recorded result
**without reading source to repair it**. The composition is frozen in
`qualification/profiles/supported-gitlab-ce-v1.json` (stamp
`forge.supported.profile/1`, written by
`scripts/freeze_supported_profile.py`); this runbook only ever installs
what THAT manifest pins.

> **What "supported" means here.** The manifest binds the exact tested
> composition with every value traced to an actual receipt. It is
> INSTALL evidence, not a support promise: the human support decision
> is a separate, still-`pending` record
> (`evidence_records.human_support_approval` in the manifest), the
> supported-profiles verdict view (`python -m
> forge.profile_qualification manifest`) keeps `gitlab-ce-v1` at
> `pending-approval`, and merge/deploy stay human decisions.

## 1. The frozen composition (identity card)

Every axis below is a field in the manifest; `manifest_digest` (sha256
over the canonical document) vouches for the whole file.

| Axis | Frozen value | Receipt |
| --- | --- | --- |
| Promoted release | **0.37.0**, source `1ca2656…` | `docs/releases/evidence/v0.37.0/promotion.json` |
| Promoted image | `ghcr.io/forcewake/forge` @ `sha256:b2e290e5…` | same |
| Lane install | the promoted **wheel** `forge-0.37.0-py3-none-any.whl` @ `sha256:a44b884f…` (the R36-07 ladder — by sha, never by tag) | same |
| Executed lab build (evidence, NOT the install source) | `localhost/forge:dev` @ `sha256:20e9cdcd…` (image id `58e0bd3e…`, rollback tag `pre-r3708-20260924T224849Z`) | the #306 alignment receipts + `qualification/inventory-2026-09-25.json` |
| Schema | head **027**, declared predecessor **026** | `alembic/versions/` chain |
| Target template | `ci/templates/claude-sdk-lane.gitlab-ci.yml` @ `sha256:95a799b1…` — **the bytes are frozen INTO the manifest** (the wheel ships no `ci/templates/`) | recovered from the disposable project's committed CI, cross-checked against the live trace |
| Runner | GitLab runner **id 4 `unraid`**, docker executor, online | `qualification/inventory-2026-09-25.json` |
| Harness | **claude-code 2.1.273** | `qualification/records/gitlab-ce-v1@0.37.0.json` |
| Model route | litellm **`fast`** → `openai/glm-5.3-flash`; lane model `glm-5.3-flash` via the z.ai Anthropic-compatible gateway | `litellm-config.yaml` + the record |
| Credential route | `gitlab-protected-variable` + `runner-redemption` (#303) | `forge.adaptive.credential_broker.PROFILE_DELIVERY_MODES` |
| Verification contract | required job **`smoke`**: six exact slugify cases + app rewired + legacy deleted; candidate may not touch `.gitlab-ci.yml` or `tests/` | the live trace + the record |

**The named divergence (read this first).** The promoted v0.37.0
artifacts are NOT the bytes the green live trace executed: the trace
ran the working-tree build (`20e9cdcd…`) and lane git sha `59ba869…`.
The same version string identified two compositions — the exact R38-06
defect. The manifest binds BOTH identities explicitly
(`control_plane.promoted` vs `control_plane.executed_lab`); installs
reproduce the **promoted** bytes, and the executed-live composition
stays bound as evidence. Neither is silently substituted for the other.

## 2. Prerequisites

1. Python 3.13 + [uv](https://docs.astral.sh/uv/) on a clean host.
2. Outbound access to exactly: `github.com` /
   `release-assets.githubusercontent.com` (the wheel), `pypi.org` /
   `files.pythonhosted.org` (dependencies). Nothing else — the model
   gateway is not contacted by any check here.
3. For the GitLab legs: a GitLab CE 19.3.2 instance with an online
   docker-executor runner, and a token with `api` scope for the
   disposable target project (read via `forge.config.Settings` — put
   `GITLAB_URL` / `GITLAB_TOKEN` in `.env`).
4. For `verify`/`upgrade`: `podman` (the lab's runtime). `upgrade`
   starts its OWN disposable Postgres (`postgres:17-alpine`) — **the
   lab database is never touched**.

## 3. Fresh install (the documented steps, and all of them)

```bash
# from a checkout of this repository:
uv run python scripts/freeze_supported_profile.py --check   # the manifest vouches for itself
uv run python scripts/cold_install_check.py --mode fresh
```

What the check does (all receipted, zero model calls — the lane job is
gated on `$FORGE_RUN_ID`, which a cold install never sets):

1. **Clean environment** — `uv venv --seed --python 3.13` into a fresh
   temp dir, `pip --no-cache-dir` (empty caches).
2. **Wheel by sha256** — downloads the manifest's wheel URL and
   verifies the bytes reproduce `lane.wheel.sha256` BEFORE anything
   installs: a different wheel under the same version string is the
   mutable-tag refusal.
3. **Identity gate** — the installed package's `forge.__version__`
   must equal the wheel's version (R36-07).
4. **Template from the manifest** — renders the disposable target
   project's `.gitlab-ci.yml` from the manifest's frozen bytes
   (round-trip-checked); the moving working tree is recorded as a named
   drift, never used as the recipe source.
5. **Preflight, offline** — `python -m forge.doctor --capabilities`
   from the INSTALLED package, plus the execution-spec composition
   preflight (`gitlab x gitlab-sdk-lane x claude-sdk-lane x
   gitlab-protected-variable`, resume required). Both must be green
   BEFORE anything else runs. (Honest seam: the promoted wheel predates
   `forge.adaptive.execution_spec` — the composition preflight runs
   against the working tree's matrix and the divergence is named in the
   findings, never merged.)
6. **Smoke** — creates a disposable GitLab project, commits the
   generated CI + the task's GOLD state (the already-completed module
   the oracle asserts), runs the pipeline and requires exactly one
   green job: `smoke`. A green smoke proves the delivery surface
   (template → runner → oracle) on the frozen composition; it is NOT a
   model-turn claim. The project is scheduled for deletion afterwards
   (GitLab deletes asynchronously).

Useful flags: `--skip-gitlab` (local identity/preflight arms only),
`--receipt-out <path>` (persist the JSON receipt).

## 4. Data-bearing upgrade (separate proof, disposable DB)

```bash
uv run python scripts/cold_install_check.py --mode upgrade
```

The chain, on a throwaway `postgres:17-alpine` container:

1. `alembic upgrade head` → `alembic downgrade 026` (the declared
   predecessor — via the repo chain, never the lab DB);
2. seed real-shaped rows at 026 (the canary seed set: a run mid-flight,
   its spec, steps, a control command, a publication intent);
3. fingerprint (per-table row counts + sha256 over ordered identity
   strings — the canary pattern);
4. `alembic upgrade head`;
5. require: the **actual** transition `026 -> 027` (an unchanged head
   is refused as an upgrade claim) and every fingerprint preserved
   EXACTLY (a changed table is named, never averaged away).

## 5. Verify an installed environment (read-only)

```bash
uv run python scripts/cold_install_check.py --mode verify
```

Read-only against the lab: podman inspect (image digests, mounts,
caps env), `/health`, a `SELECT` on `alembic_version`, the runner API,
and both GitLab projects' installed templates. Each axis must match ONE
of the manifest's bound identities exactly — semver equality is never
consulted. Exit 0 with named divergences is the expected honest outcome
for the current lab (it runs the executed-lab build, not the promoted
digest; the integration project's template is not the frozen recipe).
A refusal (exit 1) fires when an axis matches NEITHER bind or a
negative arm trips: an old worker image beside the newer app, the
worker's shared checkpoint mount removed, or a mismatched target
template.

## 6. The four evidence records (kept distinct)

The manifest's `evidence_records` block — never merged into one blob:

1. **Source review** — the promoted sha's required CI checks, with the
   earlier cancelled run retained on record (`conditional`).
2. **Release canary** — the release-image canary stages; its upgrade
   stage ran `027 -> 027` (same-head preservation, honestly NOT a
   schema transition — the data-bearing transition is §4's separate
   proof).
3. **Native workflow qualification** — the green live useful-WIP trace
   (`docs/evaluation/2026-09-25-useful-wip-resume/`): the pre-pause
   checkpoint carried all three file shapes, a second runner restored
   it exactly, the final candidate passed the precommitted oracle on
   the exact candidate sha; Draft MR left for human review.
4. **Human support approval** — `pending`. This freeze approves
   nothing; approvals live in `qualification/profile-approvals.json`
   (a human-maintained artifact), and the supported-profiles manifest
   holds `gitlab-ce-v1` at `pending-approval` until then.

Cross-reference the manifest against the record store:

```bash
uv run python -m forge.profile_qualification binding
```

## 7. Troubleshooting

| Symptom | Cause → fix |
| --- | --- |
| `REFUSED: … is not a hex64 sha256` / self-inconsistent manifest | the manifest was hand-edited → re-run `scripts/freeze_supported_profile.py` (never edit it by hand) |
| `a DIFFERENT wheel under the SAME version string` | the release asset was re-uploaded under the same tag → re-freeze against the new promotion record; do NOT install the bytes |
| `MISMATCHED template` refusal | something rendered the template from the working tree or a foreign recipe → render only via `cold_install_check.py` / the manifest's frozen bytes |
| `OLD worker image beside a newer control plane` | the worker missed the alignment recreate → re-run `scripts/align_lab.py --apply` (both consumers recreate from ONE image) |
| `the WORKER's shared checkpoint mount … is absent` | the worker lost its `/app/data` bind → re-run the alignment with `--worker-mount <repo>/data:/app/data`; the doctor refuses a retry until the authority is consistent |
| `UNCHANGED head` refusal in upgrade mode | the disposable DB never left head → the check deliberately downgrades to 026 first; do not skip that step |
| doctor `--capabilities` nonzero on the installed venv | a broken wheel install → delete the temp env, re-run fresh mode |
| The smoke pipeline hangs | the runner is offline → `GET /api/v4/runners/4` must show `online`; the lane job needs `$FORGE_RUN_ID` to even consider running (it must stay absent in a cold install) |

## 8. What stays unsupported (the exclusions)

The manifest's `exclusions` list is normative; in short:

- Installs reproduce the **promoted** v0.37.0 bytes; the green live
  trace ran the working-tree build + lane git sha `59ba869…` (bound as
  evidence) — the two converge only at the next promotion that carries
  the #302/#303/#305 code.
- No committed live `TraceRecord` yet → the supported-profiles manifest
  holds the profile at `pending-approval` (the #298 trace-tier hold).
- `usage_receipts` coverage for harness-lane runs is partial (lane meta
  receipts + `run_budgets` counters).
- The staged lane closure pins forge 0.35.0 — the composition is
  wheel-pinned, not closure-pinned.
- The **batch** lane templates cannot restore WIP (a required-resume
  refuses in the template); only the SDK lane recipes are in scope.
- No enterprise/fleetwide readiness; merge/deploy stay human.

## 9. Re-freezing (when the composition legitimately moves)

1. Land the new promotion record under `docs/releases/evidence/` and
   the new live evidence; do NOT edit the manifest by hand.
2. `uv run python scripts/freeze_supported_profile.py` — re-captures
   from the receipts, fail-closed on any contradiction (including a
   template recovery that does not reproduce the traced sha).
3. Re-run §3/§4/§5 — the three proofs are per-freeze, not eternal.
