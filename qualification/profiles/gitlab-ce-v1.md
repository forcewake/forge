# Qualified profile: `gitlab-ce-v1`

Issue **#268 / R36-09** (external review `16339c2`, acceptance trace
**AT-10**). This document freezes ONE customer configuration end to end:
everything a second engineer needs to install, onboard, drive and repair
the setup — WITHOUT reading implementation files. The qualification
driver is `scripts/qualify_gitlab_ce.py`; its evidence bundle lands in
`qualification/profiles/gitlab-ce-v1-evidence.json`.

> **Status (2026-09-23): PARTIALLY QUALIFIED — see "Known limitation".**
> The cold install, native entry arc (issue → plan → `/go` → harness
> dispatch → candidate → Draft MR → independent verification) and every
> negative arm are qualified live and offline. The cross-RUNNER resume
> drill is qualified OFFLINE only: the GitLab dispatch seam does not yet
> carry the lane-resume contract (see §8). Nothing here claims fleetwide
> or enterprise readiness, and merge/deploy stay human decisions.

## 1. The frozen combination (identity card)

| Axis | Frozen value |
| --- | --- |
| Native platform | **GitLab CE 19.3.2** (`revision 34042bf7d00`; verified live at the lab instance) |
| API surface | GitLab REST **v4** (`/api/v4`, `PRIVATE-TOKEN` auth) |
| Runner | An **online runner with the docker executor**, untagged or matching the lane job; ephemeral containers, no privileged mode, no Docker socket (ADR-0002) |
| Lane template | `ci/templates/claude-sdk-lane.gitlab-ci.yml` (job `forge-agent-claude-sdk`, gated on `$FORGE_RUN_ID && $FORGE_HARNESS_DRIVER == "claude-sdk-lane"`) |
| Harness / driver | **claude-code** CLI pinned **2.1.273** (template default `FORGE_CLAUDE_VERSION`), driven by forge's own lane driver (`python -m forge.lane_driver`) |
| Runtime recipe | **python-3.13** — the lane bootstraps uv → standalone 3.13 → venv at `/tmp/forge-lane-venv` |
| Model route | `glm-5.3-flash[1m]` through the **z.ai Anthropic-compatible gateway** (`ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic`, BYOK `ANTHROPIC_AUTH_TOKEN`) |
| Credential mode | BYOK env-token for the harness; dedicated **bot PAT** for the control plane; **read-only PAT** for the lane's clone (proposal-only, ADR-0016) |
| Control-plane lane install | **Wheel route (R36-07 ladder, default-wheel)** — see §3 |
| Verification contract | One required job named **`smoke`** (`FORGE_REQUIRED_JOBS=smoke`) — the disposable repo's own independent check, green on the CURRENT candidate sha |
| Approver | The human in `FORGE_APPROVERS` (the lab: `forcewake`) |

## 2. Infrastructure prerequisites

1. A reachable GitLab CE 19.3.2 instance (`GET /api/v4/version` answers
   `version`, `enterprise:false`).
2. At least one **online** runner available to the target project with
   the docker executor (`GET /api/v4/runners/all` shows `online:true`,
   `status:"online"`; `GET /projects/:id` shows `builds_access_level:"enabled"`).
   A paused/offline dedicated runner is acceptable ONLY while a shared
   instance runner serves the project — preflight records which.
3. Network egress from the runner job to: the GitLab instance, the
   model gateway (`api.z.ai`), and the forge control plane (the webhook
   host). The lab additionally routes harness egress through an HTTP
   proxy (`FORGE_HARNESS_HTTPS_PROXY`).
4. The forge control plane deployed and reachable FROM the GitLab
   instance by webhook (the lab tunnel: `https://forge.forcewake.me/webhook`).

## 3. Installing the control plane from RELEASE artifacts (R36-07)

The promoted wheel is the default and the only production-qualified
route:

```
FORGE_LANE_PROMOTED_WHEEL_URL="https://github.com/forcewake/forge/releases/download/v0.35.0/forge-0.35.0-py3-none-any.whl"
FORGE_LANE_PROMOTED_WHEEL_SHA256="1e365612473426a2130000784a6cd8c707ffcedae7f8f6bcb34c3f75791700d9"
```

Cold-install steps (from a CLEAN temp venv, python 3.13):

1. `python -m venv /tmp/forge-qual-venv && /tmp/forge-qual-venv/bin/pip install -U pip`
2. `pip download --no-deps -d .forge/wheel "$FORGE_LANE_PROMOTED_WHEEL_URL"`
3. Verify **exactly one** `.whl` is present and its sha256 equals the pin
   (any mismatch or multiple candidates is a refusal BEFORE any package
   executes — AT-08).
4. `pip install .forge/wheel/forge-0.35.0-py3-none-any.whl`
5. Receipt contract: the install records
   `.forge/lane_install.json` `{"pin":"wheel","route":"default-wheel",
   "expected_sha256":…,"actual_sha256":…,"qualified":true,"version":"0.35.0"}`
   and `.forge/install-identity.json`
   `{"route":"default-wheel","expected_version":"0.35.0","installed_version":"0.35.0"}`
   — the imported `forge.__version__` must equal the wheel's version
   (R36-07 identity gate; runs before any model call).

Pre-release/dev route (NOT production-qualified): build the working
tree with `uv build`, install the produced wheel recording its own
sha256, and mark `qualified:false` — a moving source ref is never a
pinned identity. The qualification evidence records which route ran.

Runtime configuration (the control plane's env): `GITLAB_URL`,
`GITLAB_TOKEN` (bot PAT), `GITLAB_WEBHOOK_SECRET`, `FORGE_BOT_TOKEN`
(dedicated bot identity — forge must never speak with an approver's
credentials), `FORGE_APPROVERS`, `DATABASE_URL` (PostgreSQL),
`REDIS_URL`, `FORGE_IMPLEMENTER_BACKEND=ci_harness:claude-code`,
`FORGE_HARNESS_MODEL=glm-5.3-flash[1m]`, `FORGE_REQUIRED_JOBS=smoke`,
`FORGE_HARNESS_TIMEOUT_SECONDS=5400`, `FORGE_LITELLM_URL` (or the
gateway env the planner uses). Budget caps (see §7) must parse.

## 4. Onboarding the disposable repository (generated assets only)

A small, disposable, non-production repo — one tiny module plus its own
independent check. The lab's shape (`forcewake/forge-lab`, project id
68, default branch `main`):

1. `.gitlab-ci.yml` includes the lane template ONCE, plus the repo's own
   verification job:

   ```yaml
   include:
     - remote: 'https://raw.githubusercontent.com/forcewake/forge/<pinned-ref>/ci/templates/claude-sdk-lane.gitlab-ci.yml'
   stages: [test, harness]
   smoke:                      # the INDEPENDENT verification contract
     stage: test
     image: python:3.13-slim
     script:
       - python3 -c "from greeting import greet; assert greet('pilot') == 'Hello, pilot!'"
   ```

2. Project CI/CD variables (masked; never forge's own write credential
   in the lane — ADR-0015 §4): `ANTHROPIC_AUTH_TOKEN`,
   `ANTHROPIC_BASE_URL`, `FORGE_BOT_READ_TOKEN` (read-only PAT),
   optionally `FORGE_HARNESS_HTTPS_PROXY`, `FORGE_HARNESS_MCP`,
   `FORGE_LANE_REF` (pinned lane ref).
3. Webhook: `python scripts/setup_gitlab.py --project-id <id>
   --webhook-url https://<control-plane>/webhook` — registers push,
   MR, note, pipeline and job events with the shared secret, and creates
   the `ai-reviewed` / `ai-needs-changes` / `security-critical` labels.
4. Minimal token permissions:
   - **Bot PAT (control plane):** `api` scope on the target group/project
     (issues+notes, pipelines, commits API, merge requests) and webhook
     administration for onboarding. `read_repository` + `write_repository`
     + `read_api` is the floor; the lab token carries broader scopes and
     the evidence records the delta.
   - **Lane read PAT (`FORGE_BOT_READ_TOKEN`):** `read_repository` only —
     the proposal-only lane receives NO write credential (ADR-0016).

## 5. Driving the flow (native surfaces only)

Every operator action is a GitLab comment; the driver never calls a
service directly:

1. Create the issue on the disposable project (title + description of a
   TINY task).
2. An approver comments `@forge /implement` → the run parks at the human
   gate with the evidence-backed plan comment (digest, Implementation
   block, the exact `/go <run-id>` command).
3. The approver comments `@forge /go <run-id>` → the harness pipeline is
   dispatched on `factory/<iid>/<short-id>` with the frozen brief
   (`FORGE_PLAN`, `FORGE_ATTEMPT_BASE`, `FORGE_HARNESS_MODEL`,
   `FORGE_HARNESS_DRIVER`); the issue gets a taken-into-work note.
4. The lane job checks out the frozen attempt base, runs the driver
   WITHOUT write access, uploads `.forge/candidate.diff` +
   `.forge/candidate.meta.json` as artifacts. The control plane
   downloads them, publishes through the trusted publisher (one commit
   on the factory branch) and opens the **Draft:** MR.
5. The repo's own pipeline runs on the candidate commit; when the
   required `smoke` job is green on the CURRENT candidate sha, the run
   reaches `ready_for_human` with the verification evidence comment.
6. **The human reviews and merges the Draft MR.** Forge never merges.

## 6. The runner-loss drill (controlled failure)

With the lane mid-turn: pause (the lane captures its WIP and uploads a
verified checkpoint to the control plane), then KILL the runner context
by cancelling the CI JOB through the GitLab API
(`POST /projects/:id/jobs/:jid/cancel`) — never by stopping shared lab
containers. The worker classifies the loss (blocked, zero forge-side
model calls — no LLM repair); the operator's `@forge /retry` is admitted
because the durable checkpoint exists, and a second runner continues
from the exact generation: restored WIP + the resumed turn, collected by
the shipped collector into the SAME Draft MR.

## 7. Budgets and bounds (the caps that must be present)

- `FORGE_HARNESS_TIMEOUT_SECONDS=5400` — the durable harness deadline.
- Commit cycles: `FORGE_MAX_COMMIT_CYCLES=3` (default) — the repair bound.
- A run budget profile MUST parse (`FORGE_BUDGET_PROFILES`, e.g.
  `{"standard":{"max_calls":40,"max_tokens":400000,"wallclock_s":5400}}`)
  — enforcement on harness lanes is honestly `partial` (episodes + wall
  clock at dispatch; call/token ceilings reconciled from the usage
  receipt). A qualification flow run REFUSES to start without caps.
- The qualification driver itself bounds every wait (default 900 s) and
  uses the cheapest model route.

## 8. Known limitation (recorded honestly — AT-10's cross-runner arm)

The **GitLab** dispatch seam (`CITharnessBackend.start`) dispatches
`FORGE_RUN_ID`, `FORGE_ISSUE_IID`, `FORGE_ISSUE_TITLE`, `FORGE_PLAN`,
`FORGE_HARNESS_MODEL`, `FORGE_HARNESS_DRIVER`, `FORGE_ATTEMPT_BASE` —
it does NOT yet carry the lane-resume contract (`FORGE_LANE_RESUME` /
`lane_resume_mode` plus lane-control URL/token) that the GitHub lane
dispatches (R32-04). Consequences, live:

- A `/retry` after a runner loss re-dispatches a FRESH lane (the
  checkpoint still gates admission and rides the continuity decision,
  but the CI job is not told to restore it).
- Therefore the full pause → kill → resume-exact-WIP drill is proven
  OFFLINE (production-entry trace CE-2: the resumed runner's lane
  subprocess is driven with the resume environment the dispatch will
  carry once the parity lands) and the LIVE flow stage stops after the
  runner loss at `blocked` + honest `/retry` behavior.

Required source change (NOT part of this profile; tracked for the
GitHub-parity item): extend the GitLab dispatch with the same three-mode
resume contract and the lane-control credentials the GitHub lane ships.

## 9. Evidence bundle and observability

`qualification/profiles/gitlab-ce-v1-evidence.json` (written by
`scripts/qualify_gitlab_ce.py --stage report`) carries: exact releases
(forge wheel URL+sha256+version, GitLab CE version, lane ref, driver
version pin), native job/pipeline/MR ids and URLs, artifact digests
(sha256 of the candidate diff/meta), per-stage outcomes (`green` /
`refused` with the reason — an honest refused stage beats a fabricated
green), manual interventions (honestly counted), and the named human
review outcome (the Draft MR is LEFT AS DRAFT — merge is the human's
decision). Counters mapped to the backlog:
`profile.cold_install_success`, `delivery.manual_rescue_count`,
`resume.cross_runner_success`, `verification.current_candidate_pass`.

## 10. What this profile does NOT claim

- No blanket enterprise readiness; no fleetwide rollout support.
- No automatic merge or deploy (the run ends at a reviewed Draft MR).
- Warm-lab success ≠ supported installation: repeat installs run from a
  fresh volume and a cache-empty runner (the install-check stage always
  provisions a CLEAN temp venv).

## 11. Closure pinning and the execution boundary (R36-10 / #269 — additive)

This profile's record format gains ADDITIVE fields (existing frozen
records keep their meaning; documents without the fields thaw to the
pre-R36-10 values):

| Field | Meaning |
| --- | --- |
| `dependency_closure_digest` | The `closure_digest` of a hash-locked wheelhouse built by `scripts/build_lane_closure.py` — forge plus its whole frozen-lock dependency set, every wheel sha256-pinned in one `closure-manifest.json`. Empty (this profile's current state) = wheel-pinned, NOT closure-pinned: pip resolved the runtime dependencies at install time, and the boundary report says so honestly instead of claiming otherwise. |
| credential staging (documented, not a digest axis) | The credential names the lane stages, recorded NAMES-ONLY in the scope receipt: `ANTHROPIC_AUTH_TOKEN` (BYOK harness), `FORGE_BOT_READ_TOKEN` (read-only clone PAT), `GITHUB_TOKEN`-class runner tokens where applicable. The forbidden set — every publisher/control-plane credential (`FORGE_BOT_TOKEN`, `GITLAB_TOKEN`, webhook secrets) — is asserted ABSENT from the agent workspace by `verify_credential_isolation`; a violation refuses before execution and never quotes values. |

Operators move from wheel-pinned to closure-pinned by building and
staging a closure and pinning its digest:

```bash
uv run python scripts/build_lane_closure.py                       # from the promoted record
uv run python scripts/build_lane_closure.py --verify dist/lane-closure
```

The lane then installs through the additive `closure-wheel` route
(`FORGE_LANE_CLOSURE_SHA256` + `FORGE_LANE_CLOSURE_DIR`, conflict-
rejecting against every other explicit install input) with
`--no-index --find-links`, so the disposable repository's own lockfile
cannot replace the collector runtime. The full contract — build,
verify, supply-chain binding to the promotion record, the egress/
filesystem/credential boundary statement and who enforces what (the
runner platform enforces; forge verifies and probes) — is
`docs/operations/lane-closure.md`.
