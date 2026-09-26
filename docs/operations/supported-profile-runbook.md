# Supported-profile runbook (R38-06 / #307 · re-frozen Q39-07 / #326)

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

> **The v2 re-freeze (Q39-07 / #326).** The manifest now pins the
> EXACT composition that completed the end-to-end trace on 2026-09-25:
> the working-tree build (control plane + lane wheel) whose live run
> ended `ready_for_human` with the closing review completed inside the
> #325 closing reserve. The v0.37-composition manifest this file first
> documented stays historical, byte-identical, at
> `qualification/profiles/supported-gitlab-ce-v1@v0.37-composition.json`
> — history is archived, never rewritten. What changed concretely:
> the fresh install installs the WORKING-TREE wheel by sha256 (below),
> and the version axis accepts the executed-lab bind (0.38.0) beside
> the promoted one.

## 1. The frozen composition (identity card)

Every axis below is a field in the manifest; `manifest_digest` (sha256
over the canonical document) vouches for the whole file.

| Axis | Frozen value | Receipt |
| --- | --- | --- |
| Promoted release (the historical install pin) | **0.37.0**, source `1ca2656…` | `docs/releases/evidence/v0.37.0/promotion.json` |
| Newest promotion (named, NOT this composition) | **0.38.0**, source `854a5f4…` — `status: promoted, STILL NOT the qualified composition` | `docs/releases/evidence/v0.38.0/promotion.json` |
| **The qualification composition (executed lab)** | `localhost/forge:dev` @ `sha256:33eb4622…` (image id `587e3392…`, rollback tag `pre-r3708-20260925T092917Z`, reports **0.38.0**, schema head **028**) — the WORKING-TREE build carrying #320/#321/#325 | the v2 alignment receipts + `qualification/inventory-2026-09-25-v2.json` |
| **Lane install (the fresh-install target)** | the WORKING-TREE wheel `forge-0.38.0-py3-none-any.whl` @ `sha256:37fb5b0a…` (`lane.wheel`, source `local-uv-build`, path `dist/…`; the committed receipt `qualification/profiles/receipts/working-tree-wheel-v2.json` covers a dist-less checkout) | `uv build` of the same tree |
| Schema | head **028**, declared predecessor **027** | `alembic/versions/` chain |
| Target template | `ci/templates/claude-sdk-lane.gitlab-ci.yml` @ `sha256:3d74be37…` — **the bytes are frozen INTO the manifest** (the wheel ships no `ci/templates/`) | recovered from the v2 trace's committed CI, cross-checked against the trace record |
| Lane of the executed trace | pushed sha `6df4020…` (main at the v0.38.0 evidence archive — the working tree is never pushed) | the v2 trace record `#task.lane_ref` |
| Runner | GitLab runner **id 4 `unraid`**, docker executor, online | `qualification/inventory-2026-09-25-v2.json` |
| Harness | **claude-code 2.1.273** | `qualification/records/gitlab-ce-v1@0.37.0.json` |
| Model route | litellm **`fast`** → `openai/glm-5.3-flash`; lane model `glm-5.3-flash` via the z.ai Anthropic-compatible gateway | `litellm-config.yaml` + the record |
| Credential route | `gitlab-protected-variable` + `runner-redemption` (#303) — BOTH live-qualified: the v2 trace rode the protected-variable mode; the 2026-09-26 redemption trace (R40-07/#343) rode runner-redemption end-to-end (§8a) | `forge.adaptive.credential_broker.PROFILE_DELIVERY_MODES` + `qualification/records/redemption-2026-09-26.json` |
| Closing budget policy | `FORGE_CLOSING_RESERVE_USD=0.50`, `FORGE_SPEND_CAP_USD=2.50` on BOTH consumers; standard budget profile 40 calls / **480 000 tokens** / 3600 s | the v2 alignment receipts (`--extra-env` + `--budget-profiles`) |
| Verification contract | required job **`smoke`**: six exact slugify cases + app rewired + legacy deleted; candidate may not touch `.gitlab-ci.yml` or `tests/`; PLUS the v2 revision marker (`__all__ = ["slugify"]` in `src/utils/text.py`) | the v2 live trace + the record |

**The named divergence (read this first).** The promoted v0.37.0 image
digest and the executed-lab digest DIFFER — and even the newest v0.38.0
promotion predates the working tree this manifest qualifies. The
composition being qualified IS the executed-lab build (the alignment
image + the uv wheel of the same tree); its promotion is PENDING,
never assumed. A cold install from this manifest installs the
working-tree wheel by sha256; version equality never substitutes for
composition equality (the exact Q39-07 defect this re-freeze closes).

## 2. Prerequisites

1. Python 3.13 + [uv](https://docs.astral.sh/uv/) on a clean host, and
   (for the fresh wheel) a checkout of the exact qualified tree — run
   `uv build` once so `dist/forge-0.38.0-py3-none-any.whl` exists and
   hashes to the manifest pin.
2. Outbound access to exactly: `pypi.org` /
   `files.pythonhosted.org` (dependencies). Nothing else — the model
   gateway is not contacted by any check here (a URL-pinned wheel
   would additionally need `github.com` /
   `release-assets.githubusercontent.com`).
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
uv build                                                    # the qualification wheel
uv run python scripts/freeze_supported_profile.py --check   # the manifest vouches for itself
uv run python scripts/cold_install_check.py --mode fresh
```

What the check does (all receipted, zero model calls — the lane job is
gated on `$FORGE_RUN_ID`, which a cold install never sets):

1. **Clean environment** — `uv venv --seed --python 3.13` into a fresh
   temp dir, `pip --no-cache-dir` (empty caches).
2. **Wheel by sha256** — the manifest's lane wheel is the WORKING-TREE
   build: the local file must exist and its bytes must reproduce
   `lane.wheel.sha256` BEFORE anything installs (a drifted tree under
   the same version string is the mutable-tag refusal; `uv build`
   again or refuse).
3. **Identity gate** — the installed package's `forge.__version__`
   must equal the wheel's version (R36-07).
4. **Template from the manifest** — renders the disposable target
   project's `.gitlab-ci.yml` from the manifest's frozen bytes
   (round-trip-checked); the moving working tree is recorded as a named
   drift, never used as the recipe source.
5. **Preflight, offline** — `python -m forge.doctor --capabilities`
   and the execution-spec composition preflight (`gitlab x
   gitlab-sdk-lane x claude-sdk-lane x gitlab-protected-variable`,
   resume required), BOTH from the INSTALLED package — the v2 wheel
   carries `forge.adaptive.execution_spec`, so the #307-era
   "run it from the tree" seam is GONE on this composition.
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

1. `alembic upgrade head` → `alembic downgrade 027` (the declared
   predecessor — via the repo chain, never the lab DB);
2. seed real-shaped rows at 027 (the canary seed set: a run mid-flight,
   its spec, steps, a control command, a publication intent);
3. fingerprint (per-table row counts + sha256 over ordered identity
   strings — the canary pattern);
4. `alembic upgrade head`;
5. require: the **actual** transition `027 -> 028`
   (`028_credential_receipts`) and every fingerprint preserved EXACTLY
   (a changed table is named, never averaged away). The v2 executed
   edge: **027 → 028, all five seeded tables preserved**.

## 5. Verify an installed environment (read-only)

```bash
uv run python scripts/cold_install_check.py --mode verify
```

Read-only against the lab: podman inspect (image digests, mounts, caps
env), `/health`, a `SELECT` on `alembic_version`, the runner API, and
the trace's target project's installed template. Each axis must match
ONE of the manifest's bound identities exactly (the VERSION axis
accepts the promoted OR the executed-lab bind — the composition being
qualified reports the tree's version). The v2 executed outcome: **8
match, 1 named divergence (the integration project's own template), 0
refusals**. A refusal (exit 1) fires when an axis matches NEITHER bind
or a negative arm trips: an old worker image beside the newer app, the
worker's shared checkpoint mount removed, or a mismatched target
template. When the trace's disposable project is deleted after capture
(the teardown's documented disposition), the template axis is verified
against the trace receipt's pinned sha instead — named, never faked.

## 6. The four evidence records (kept distinct)

The manifest's `evidence_records` block — never merged into one blob:

1. **Source review** — the promoted sha's required CI checks, with the
   earlier cancelled run retained on record (`conditional`).
2. **Release canary** — the v0.38.0 canary stages; the data-bearing
   N-1→head edge for THIS manifest is §4's separate proof.
3. **Native workflow qualification** — the v2 green live trace
   (`docs/evaluation/2026-09-25-supported-composition-v2/`,
   outcome **reviewed-ready**): the pre-pause checkpoint carried all
   three file shapes, a second runner restored it exactly under the
   APPROVED revision-2 brief (no rescue steer), the final candidate
   passed the precommitted oracle on the exact sha, AND the closing
   review completed within the #325 reserve so the run ended
   `ready_for_human`. The #306 trace
   (`docs/evaluation/2026-09-25-useful-wip-resume/`) stays historical:
   its terminal `blocked (budget_exhausted)` at the reviewer leg is
   exactly the gap the v2 composition closed.
4. **Human support approval** — `pending`. This freeze approves
   nothing; approvals live in `qualification/profile-approvals.json`
   (a human-maintained artifact), and the supported-profiles manifest
   holds `gitlab-ce-v1` at `pending-approval` until then.

The executed trace's own record —
`qualification/records/supported-composition-v2-2026-09-25.json`
(stamp `forge.profile.qualification/1`, zero validation findings) —
names the qualified drivers/providers/resume modes (AC-07):
gitlab × gitlab-sdk-lane × claude-sdk-lane ×
gitlab-protected-variable, resume mode `required` (the exact-resume
SDK lane), claude-code 2.1.273, glm-5.3-flash via the z.ai gateway.

Cross-reference the manifest against the record store:

```bash
uv run python -m forge.profile_qualification binding
```

## 7. Troubleshooting

| Symptom | Cause → fix |
| --- | --- |
| `REFUSED: … is not a hex64 sha256` / self-inconsistent manifest | the manifest was hand-edited → re-run `scripts/freeze_supported_profile.py` (never edit it by hand) |
| `the manifest pins the working-tree wheel … but the file is absent` | the checkout has no `dist/forge-0.38.0…whl` → `uv build`; the bytes must then reproduce the pin or the install refuses |
| `a DIFFERENT wheel under the SAME version string` | the tree drifted under a frozen version → rebuild (`uv build`) and re-freeze; do NOT install the bytes |
| `MISMATCHED template` refusal | something rendered the template from the working tree or a foreign recipe → render only via `cold_install_check.py` / the manifest's frozen bytes |
| `OLD worker image beside a newer control plane` | the worker missed the alignment recreate → re-run `scripts/align_lab.py --apply` (both consumers recreate from ONE image) |
| `the WORKER's shared checkpoint mount … is absent` | the worker lost its `/app/data` bind → re-run the alignment with `--worker-mount <repo>/data:/app/data` |
| `UNCHANGED head` refusal in upgrade mode | the disposable DB never left head → the check deliberately downgrades to 027 first; do not skip that step |
| doctor `--capabilities` nonzero on the installed venv | a broken wheel install → delete the temp env, re-run fresh mode |
| The smoke pipeline hangs | the runner is offline → `GET /api/v4/runners/4` must show `online`; the lane job needs `$FORGE_RUN_ID` to even consider running (it must stay absent in a cold install) |

## 8. What stays unsupported (the exclusions)

The manifest's `exclusions` list is normative; in short:

- The qualified composition is the WORKING-TREE build — NO release
  carries these bytes yet (v0.38.0 predates them); installs from this
  manifest install the working-tree wheel by sha256 and the promotion
  is pending, never assumed.
- The lane of the executed trace runs the PUSHED sha under
  `lane.executed_live` (the working tree is never pushed), so the lane
  package is one push behind the control-plane build; the brief bytes
  are the payload.
- No committed live `TraceRecord` yet → the supported-profiles manifest
  holds the profile at `pending-approval` (the #298 trace-tier hold).
- `usage_receipts` coverage for harness-lane runs is partial (lane meta
  receipts + `run_budgets` counters).
- The staged lane closure pins forge 0.35.0 — the composition is
  wheel-pinned, not closure-pinned.
- The **batch** lane templates cannot restore WIP (a required-resume
  refuses in the template); only the SDK lane recipes are in scope.
- No enterprise/fleetwide readiness; merge/deploy stay human.

## 8a. The runner-redemption credential route (R40-07 / #343)

The profile's second delivery mode is **live-qualified**: the
[redemption-2026-09-26 record](../../qualification/records/redemption-2026-09-26.json)
and its evidence bundle
([docs/evaluation/2026-09-26-redemption-qualification/](../../docs/evaluation/2026-09-26-redemption-qualification/))
prove the full chain on the real lab: native dispatch → minted operation
grant (never seeded) → lane bootstrap redemption → the model consumer
presenting the BROKER-selected credential.

**Selecting the route (explicit, never defaulted).** Pin on BOTH
consumers through `scripts/align_lab.py --extra-env` (receipted):

```bash
uv run python scripts/align_lab.py --apply \
  --extra-env FORGE_CREDENTIAL_DELIVERY=runner-redemption \
  --extra-env FORGE_CREDENTIAL_BINDINGS=/app/data/credential-bindings.json \
  --extra-env FORGE_CREDENTIAL_TEMPLATE_DIR=/app/data/credential-templates \
  --extra-env ANTHROPIC_AUTH_TOKEN=<the broker-held model credential>
```

- `FORGE_CREDENTIAL_BINDINGS` names the persisted registry document (the
  `data/` volume — gitignored, REFS only). Bind the run's canonical
  subject (`gitlab/-/<project_id>`) per provider route with the shipped
  registry API — never by hand-editing a value.
- **The ref invariant (LIVE-FOUND, #343):** under the default
  `EnvBroker`, the credential ref's env NAME must BE the binding's env
  slot — bind `env:ANTHROPIC_AUTH_TOKEN` for the `anthropic-gateway`
  route, and hold the value in the consumers' `ANTHROPIC_AUTH_TOKEN`.
  A ref named for anything else (e.g. `env:FORGE_BROKER_MODEL_TOKEN`)
  stages under its own name, and the redemption endpoint refuses typed
  `staged_slot_mismatch` with ZERO emitted values — the guard held on
  the first live dispatch; the configuration was wrong.
- `FORGE_CREDENTIAL_TEMPLATE_DIR` must point at the SHIPPED templates
  (the wheel ships no `ci/templates/`; the containers get them through
  the shared `data/` volume).
- The grant window / redemption TTL knobs:
  `FORGE_CREDENTIAL_GRANT_WINDOW_SECONDS` (default 3600 — the ABSOLUTE
  deadline fixed at dispatch authorization, frozen across restarts) and
  `FORGE_CREDENTIAL_REDEEM_TTL_SECONDS` (default 3600 — caps the
  response's `expires_at`). A 20 s window is the documented expiry-drill
  posture; restore the default after the drill.

**Operating facts the live trace established (all receipted):**

- The grant is minted by the native dispatch path BEFORE the provider
  call (assert zero `operation_grants` rows before `/go` when
  re-verifying); the keyed row (work, attempt, route) is the authority.
- Every successful redemption lands in the append-only
  `credential_redemptions` ledger; every typed refusal leaves NO row and
  emits nothing (`staged_slot_mismatch`, `grant_ref_mismatch`,
  `grant_route_mismatch`, `binding_revision_mismatch`, `grant_expired`,
  `attempt_terminal`, superseded-generation tokens).
- The registry document is re-read on EVERY dispatch command and EVERY
  redemption request — a rotation binds the next dispatch immediately,
  no consumer restart; an already-minted grant keeps the revision it
  recorded and is refused on mismatch (the confused-deputy guard).
- A finished (blocked/terminal) attempt redeems nothing — replay
  survival across a cold control-plane restart is proven on a LIVE
  attempt's window (restart mid-lane, then the idempotent replay).
- A lane whose redemption is refused fails CLOSED
  (`credential_redemption_failed`, zero model turns) — there is NO ambient
  fallback; the native `/retry <run> restart` continuation re-enters it.

## 9. Re-freezing (when the composition legitimately moves)

1. Land the new promotion record under `docs/releases/evidence/` and
   the new live evidence; do NOT edit the manifest by hand. The
   previous manifest is archived under
   `qualification/profiles/<name>@<composition>.json` first — history
   stays historical.
2. `uv build`, then `uv run python scripts/freeze_supported_profile.py`
   — re-captures from the receipts, fail-closed on any contradiction
   (including a template recovery that does not reproduce the traced
   sha).
3. Re-run §3/§4/§5 — the three proofs are per-freeze, not eternal.

## 10. The second-operator live repetition (the v2 trace, actual steps)

The recorded live repetition of the full adaptive workflow on this
exact profile (all native GitLab notes by the configured approver;
every phase resumable; REFUSES on any precondition failure):

```bash
# the lab alignment (receipts + rollback tag; pins the closing reserve):
uv build
uv run python scripts/align_lab.py --apply \
  --receipts docs/evaluation/<dir>/alignment-receipts.json \
  --extra-env FORGE_CLOSING_RESERVE_USD=0.50 \
  --extra-env FORGE_SPEND_CAP_USD=2.50 \
  --budget-profiles '{"trivial":{…},"standard":{"max_calls":40,"max_tokens":480000,"wallclock_s":3600},"heavy":{…}}'
uv run python scripts/inventory_lab.py --stage all --out qualification/inventory-<date>-v2.json

# the live trace (the v2 phases; the interrupted arm stops at the
# awaiting-revision gate by design):
V2=docs/evaluation/<dir>
uv run python scripts/run_useful_wip_resume.py setup     --evidence $V2/live-run-evidence.json --record $V2/useful-wip-resume-v2.json --project-name <name>
uv run python scripts/run_useful_wip_resume.py preflight --evidence $V2/live-run-evidence.json --record $V2/useful-wip-resume-v2.json
uv run python scripts/run_useful_wip_resume.py interrupt  --evidence …   # paid; pauses at the gate
uv run python scripts/run_useful_wip_resume.py revision   --evidence …   # stage + NATIVE /approve-revision
uv run python scripts/run_useful_wip_resume.py interrupt  --evidence …   # the resume leg → candidate → Draft MR
uv run python scripts/run_useful_wip_resume.py review     --evidence …   # the closing review settles
uv run python scripts/run_useful_wip_resume.py collect    --evidence … --record $V2/useful-wip-resume-v2.json
uv run python scripts/run_useful_wip_resume.py teardown   --evidence …

# then the freeze + the three cold-install proofs (§3/§4/§5)
```

Setup/intervention effort recorded for the v2 run: one preflight pass
(green first try), one ladder iteration (rung-1 sampled the empty
failure mode; the native /retry re-entered), one drill-side waiter
defect fixed mid-run (root-caused in the trace's `failures`), zero
product patches, zero manual YAML edits. Spend: $0.4032 SDK lane
receipts + the planner/reviewer gateway calls inside the run budget.

## 11. The second engineer's kit (R40-12 / #348 — cross-link)

The installer-facing ENTRY document is
[`docs/onboarding/cold-install-runbook.md`](../onboarding/cold-install-runbook.md):
one document a second engineer follows start-to-finish WITHOUT reading
source — prerequisites (versions, tokens, network reachability), every
command verbatim, every expected observable (wheel sha, template sha,
schema head), the failure table with each TYPED refusal, and the
observation-report template they fill with OBSERVED timings. It pins
the same composition this file freezes (the manifest is normative for
both) and keeps the honest split explicit: the second engineer's own
install, their observed setup effort, and the bounded support decision
are HUMAN deliverables — the package is "ready for the second
engineer", never "second-engineer-verified".

The kit's machine path is executable AS WRITTEN (the mode parses the
runbook's own marked command blocks — never a parallel implementation):

```bash
uv run python scripts/cold_install_check.py --mode from-runbook
```

Current freeze: 9 machine steps executed as written, 4 human-step
markers counted, 3 blocked-on-lab markers recorded (the shared-lab
GitLab smoke leg, the read-only verify, the #326 useful-WIP
continuation). The sibling modes the kit adds to this file's §3/§4/§5:
`--mode oracle-replay` (the initial-delivery install-level trace from
the installed artifacts), `--mode negative-arms` (missing permission /
unavailable runner / stale wheel / incompatible schema N-2 — each
refusing BEFORE the unsafe or paid action), `--mode upgrade
--rollback-rehearsal` (rollback + forward recovery on the disposable
DB), and `scripts/cold_install_restore_rehearsal.py` (the data-bearing
restore drill with the zero-model-turn dispatch gate). This file stays
the maintenance-side runbook (freeze / verify / re-freeze); the
onboarding file is the kit.
