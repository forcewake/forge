# forge

[![CI](https://github.com/forcewake/forge/actions/workflows/ci.yml/badge.svg)](https://github.com/forcewake/forge/actions/workflows/ci.yml)
[![Release](https://github.com/forcewake/forge/actions/workflows/release.yml/badge.svg)](https://github.com/forcewake/forge/actions/workflows/release.yml)
[![GitHub release](https://img.shields.io/github/v/tag/forcewake/forge?label=release&sort=semver)](https://github.com/forcewake/forge/releases)

**forge** is an agentic software factory for **GitLab CE**, **GitHub**, and
**Azure DevOps**: an authorized issue becomes a plan, a human-approved branch
with code, a green pipeline, and a Draft merge request — ready for human
review. You bring the model (any provider via [LiteLLM](https://docs.litellm.ai/)
or native harness CLIs — Claude Code, Grok Build, opencode, GitHub Copilot
CLI); forge runs on your own infrastructure and never merges.

All three providers are **live-verified end-to-end**: `/implement` → plan →
human gate → coding agent in ephemeral CI → trusted publisher → Draft MR/PR →
review → `ready_for_human` — on real GitLab CE, real GitHub (App + Actions),
and a real Azure DevOps organization (work items, service hooks, Pipelines).
Per-capability verification depth varies — see the
[guarantee matrix](#guarantee-levels).

## The iron principle: the bot never merges

forge proposes changes, drives the pipeline, and assembles evidence bound to
a specific commit. It never merges, never pushes to a protected target
branch, and never changes permissions. Review and merge are always performed
by a human. This is enforced by the executor's capabilities and platform
permissions — not by prompt instructions. See
[ADR-0003](docs/adr/0003-no-merge-is-enforceable.md).

## How a run works

```
issue comment /implement  →  durable plan (LLM)  →  HUMAN /go GATE
  →  coding agent in an ephemeral CI runner (no write credentials)
  →  trusted publisher validates the candidate  →  Draft MR / PR
  →  pipeline + required checks  →  readonly LLM review
  →  ready_for_human + evidence bound to the exact commit
```

- **Durable by design** ([ADR-0017](docs/adr/0017-durable-step-runtime.md)):
  commands and steps live in Postgres; a worker crash at the six
  failure-injection checkpoints (ingress, claim, gate, publish, draft,
  evidence) converges — proven by two failure-injection suites: a coroutine
  profile (two in-process workers, real Postgres, kill at each checkpoint)
  and an OS-process profile that SIGKILLs real `python -m forge.worker`
  subprocesses at each checkpoint and requires a fresh process to adopt the
  durable state (nightly CI; `FORGE_PG_TEST_URL` +
  `FORGE_OS_FI_REDIS_URL`).
- **Proposal-only execution lane**
  ([ADR-0016](docs/adr/0016-candidate-bundle-trusted-publisher.md)): coding
  agents run in ephemeral CI with **no write credentials and no forge
  secrets**; their output is a candidate artifact that a trusted publisher
  validates and applies.
- **Shared publisher contract** — builtin LLM ChangeSets and CLI agents
  share the same validation, policy, and journal; how strongly each backend
  enforces it differs — see the [guarantee matrix](#guarantee-levels).
- **Human gate** ([ADR-0018](docs/adr/0018-immutable-run-spec.md)): the plan
  is frozen into an immutable RunSpec; the decision has a deadline; cancel
  revokes the publication grant before it stops the runner. The plan comment
  carries an **Implementation block** — the gate approves the execution
  shape (harness, model, fallbacks, budget class), not just the plan text.
- **Quality contract**
  ([ADR-0008](docs/adr/0008-quality-contract-instead-of-pipeline-status.md)):
  pipeline success + required jobs green; failures classified code /
  infrastructure / config / unknown — only *code* failures trigger the
  bounded repair loop; unknown evidence never blames the code.

## Providers

Setup guides: [GitLab CE](docs/getting-started/gitlab.md) ·
[GitHub](docs/getting-started/github.md) ·
[Azure DevOps](docs/getting-started/azure-devops.md).

| Capability | GitLab CE | GitHub | Azure DevOps |
|---|---|---|---|
| Commands (`/implement`, `/go`, `/cancel`) | ✅ comments | ✅ comments + `forge` label | ✅ work-item + PR comments |
| Plan comment + human gate | ✅ | ✅ | ✅ (work-item comment) |
| Harness execution | ✅ project CI (docker executor) | ✅ Actions (`workflow_dispatch`) | ✅ Pipelines (Runs-API dispatch) |
| Builtin LLM implementer (no CI needed) | ✅ | ✅ | ✅ |
| Trusted publisher | Commits API | GraphQL CAS (`expectedHeadOid`) | Push API CAS (`oldObjectId`) |
| Draft MR / PR before CI | ✅ | ✅ | ✅ (`isDraft: true`) |
| Readonly LLM review | ✅ MR notes | ✅ native reviews | ✅ PR threads (inline, sticky) |
| CI-failure debug lane | ✅ | ✅ (Actions timeline) | ✅ (Pipelines timeline + task logs) |
| Reactive review on push | ✅ | ✅ (incremental via before/after) | ✅ (incremental via PR iterations) |
| MCP servers in the lane | ✅ | ✅ | ✅ |
| Identity | bot user + PAT | GitHub App installation (+ PAT lab mode) | service account + PAT (Entra SPN = upgrade path) |
| Webhook authenticity | secret token | HMAC signature | Basic credentials (no HMAC exists) over HTTPS |

Architecture: four orthogonal adapters — source, execution, harness driver,
model route ([ADR-0019](docs/adr/0019-source-execution-adapters.md)).
Adding a provider is an adapter, not a second factory.

## Guarantee levels

What each capability is verified at today: **implemented** (code + unit
tests) · **contract-tested** (CI contract suite, faked platform) ·
**live** (exercised against the real platform in the dogfooding loop).

The machine-readable source of truth for these claims is the release-evidence
manifest — regenerate it with `python -m forge.release_manifest`. It records
per capability, provider/backend: level, the evidence class, the exact test
files / CI jobs, and per-finding closure evidence. Its levels are stricter
than the prose rows above, which are the broad-stroke summary:

- **implemented** — code + unit tests; no CI contract suite yet.
- **contract-tested** — CI suite over fakes/stubs (PR gate), or a real-runtime
  failure-injection suite (`coroutine_fi` on the `CI / integration` job;
  `subprocess_fi` nightly on `CI / integration-os`). The platform/agent is
  not real in the loop.
- **live-canary-tested** — exercised against the real runtime: the built
  release artifact (boot/migrate canary, `scripts/canary_smoke.py`) or the
  real provider in the dogfood loop. Maintainer-run live exercises are
  recorded at this level only when their artifacts live in this tree;
  otherwise they are **not-run**.
- **not-run** — no CI-reproducible evidence exists; recorded explicitly and
  never converted to pass.

Evidence classes stay separate: a green boot canary is not an SDLC e2e claim,
and boot canary, subprocess failure injection, coroutine failure injection,
and real-provider e2e are never merged into one check. Where the manifest
says `not_run`, the row above is the prose claim, not CI-verifiable evidence.

| Capability | GitLab CE | GitHub | Azure DevOps |
|---|---|---|---|
| Publication policy — builtin lane | live · enforced | contract-tested · validated at publish, not platform-enforced (known gap) | contract-tested |
| Publication policy — harness lane | live | contract-tested (shared publisher validation) | contract-tested |
| CI verification gate (`waiting_ci`) | contract-tested · required-jobs profile | live · `waiting_ci` + checks | contract-tested · parity in progress |
| Repair-in-place | implemented · contract-tested | implemented · contract-tested | implemented · contract-tested |
| Operator `/retry` + auto-revive | live | live | live |

"Enforced" means the platform itself cannot apply a candidate that failed
publisher validation. GitHub's CAS check (`expectedHeadOid`) happens at
publish time, inside forge — platform-side enforcement is a known gap.
GitLab CI verification keys on the required-jobs profile
([ADR-0008](docs/adr/0008-quality-contract-instead-of-pipeline-status.md));
the Azure checks gate is GitHub parity, in progress.

## Four harness drivers, one contract

Every driver implements the same proposal-only contract — mechanical
commit/push deny (driver-native rules, not just the brief), scoped tool
grants, candidate artifact handoff, usage receipts where the vendor
provides them (unknown ≠ zero), and optional MCP servers
(`FORGE_HARNESS_MCP`; [ADR-0022](docs/adr/0022-harness-mcp-integration.md)).
Per-driver setup guides: [docs/harnesses/](docs/harnesses/README.md).

| Driver | Headless posture | Notes |
|---|---|---|
| **[Claude Code](docs/harnesses/claude-code.md)** | `-p` + stream-json, `--strict-mcp-config` | live-verified on all three providers |
| **[Grok Build](docs/harnesses/grok-build.md)** | `--always-approve` + deny rules, hardened npm preamble | platform-binary hang workaround |
| **[opencode](docs/harnesses/opencode.md)** | permission map via injected config | schema-translated MCP |
| **[GitHub Copilot CLI](docs/harnesses/copilot-cli.md)** | `-p` + deny-wins tool rules | subscription auth (fine-grained PAT) |

## Task-aware harness selection
([full guide](docs/harnesses/README.md) ·
[ADR-0023](docs/adr/0023-dynamic-harness-selection.md))

Instead of one pinned implementer, a project declares an ordered preference:

```yaml
forge:
  implement:
    harnesses: [claude-code, grok-build]
```

At plan time forge compiles the chain against the lanes the project
onboarded (`forge doctor` reports exactly that), freezes the selected
harness + fallback tail + budget class into the RunSpec, and shows them in
the plan's **Implementation** block — `/go` approves the execution shape.
The planner may propose an entry of the list with a one-line reason; it can
reorder, never extend. Dispatch-time fallback down the frozen chain exists,
is **off by default**, fires only on infrastructure-classified failures
before any candidate, and journals every advance.

## MCP, both directions

- **forge as an MCP server** (ADR-0021/§4): a scoped run surface —
  `run_list` / `run_get` / `plan_get` / `run_evidence_get` — over durable
  state with per-token principals (`FORGE_MCP_SCOPED_TOKENS`), per-call
  scope enforcement, and an audit log. The surface never acts with forge's
  provider tokens. Plus the classic GitLab helper tools.
- **MCP servers inside every harness lane**: one CI variable
  (`FORGE_HARNESS_MCP`) rendered into each driver's native config;
  `--strict-mcp-config` on Claude means repo-supplied `.mcp.json` files are
  never loaded — servers cross the boundary as CI variables, never as repo
  content (live-verified with Context7 and Microsoft Learn on GitLab CI).

## Observability

`/metrics.prometheus` exposes queue depth, worker heartbeats, runs by
lifecycle status, and the **delivery ladder** (`forge_delivery_ladder`):
started → planned → gate_approved → candidate_published → ci_passed →
ready_for_human — where work packages actually stand. Every model call is
journaled (`llm_calls`), every external write is an intent-first
`action_log` entry, raw webhook payloads land in `FORGE_CAPTURE_DIR`, and
MCP calls carry an audit line. `forge doctor` verifies the environment and
any target project's onboarding — read-only, exit code 0 means done.

## Status

**v0.34.0** — three providers live-verified end-to-end, four harness
drivers with registry-checked credential recipes, task-aware selection,
identity-preserving CI verification, reserved Draft-MR publication (one MR
per run, proven under failure injection), frozen-spec dispatch on every
provider, scoped MCP run surface, honest delivery-cohort economics,
provider-neutral operator commands (`/retry`, `/status`, `/why-blocked`,
`/reconcile`) with bounded auto-revive, the 64-story adaptive roadmap
complete (contracts, wiring, pilot, runbook), REAL interactive-driver
clients for the execution lane (claude-agent-sdk with the harness hacks
ported, codex app-server over JSON-RPC stdio, opencode serve over HTTP+SSE
in `forge.adaptive.drivers`, LIVE-verified against real vendor binaries with the evidence-backed DriverMatrix seed; the edf938c review campaign landed 12 assembly slices — discovery spliced into /implement, durable Postgres mailbox, verified pause/checkpoint transaction, effect-intent steering, one-transaction revision approval, complete tested-world fingerprinting — all honestly tiered in `forge doctor --capabilities`), and the 0fca1b7 E2E-qualification wave: qualification profiles with single digests and egress probe pairs, an offline-replayable research-quality cohort, a fail-closed release promotion gate (evidence archived per release), the design-partner pilot kit, the two-writer kill-at-step-k qualification matrix, a CAS-protected operator view with support bundles, and the ADR-0029 composition boundaries; images on GHCR
<!-- generated by scripts/generate_template_pins.py -- begin -->
(`ghcr.io/forcewake/forge:0.34.0` — digest `sha256:64025dc783ed4aa4e7cd75b23b10077679de6451a43cb763c3934d530955c416`, verdict `promote`,
evidence `docs/releases/evidence/v0.34.0/promotion.json`, qualifying CI run `35871693344`).
<!-- generated by scripts/generate_template_pins.py -- end --> 5917 tests;
failure-injection-proven durable core; mypy-clean over the typed core.
**Pre-production**: expect breaking changes before 1.0. The
[CHANGELOG](CHANGELOG.md) has the full history.

## Quick start

### 1. Run the image (or build from source)

<!-- generated by scripts/generate_template_pins.py -- begin -->
```bash
docker run -d --name forge -p 8420:8420 \
  --env-file .env ghcr.io/forcewake/forge:0.34.0
# pin the release digest instead of the mutable tag (R30/R32-19):
#   docker run -d --name forge -p 8420:8420 --env-file .env ghcr.io/forcewake/forge@sha256:64025dc783ed4aa4e7cd75b23b10077679de6451a43cb763c3934d530955c416
# or from source:
git clone https://github.com/forcewake/forge && cd forge
uv sync && set -o pipefail && .venv/bin/python -m pytest -q
```
<!-- generated by scripts/generate_template_pins.py -- end -->

### 2. Configure

```bash
cp .env.example .env   # then edit: GITLAB_URL/TOKEN or GitHub App values
                       # or FORGE_AZDO_* — plus the model key,
                       # DATABASE_URL (Postgres), REDIS_URL, LITELLM_URL
```

### 3. Migrate and run

```bash
python -m forge.migrate             # apply schema migrations
uv run uvicorn forge.main:app --host 0.0.0.0 --port 8420   # app
uv run python -m forge.worker                              # worker
curl localhost:8420/health
```

### 4. Connect a project

- **GitLab CE:** [docs/getting-started/gitlab.md](docs/getting-started/gitlab.md)
  (webhook, bot PAT, harness template include, `forge doctor`).
- **GitHub:** [docs/getting-started/github.md](docs/getting-started/github.md)
  (GitHub App registration, webhook, secrets, harness workflow, label
  trigger, `forge doctor`).
- **Azure DevOps:** [docs/getting-started/azure-devops.md](docs/getting-started/azure-devops.md)
  (service account + PAT scopes, service hooks, lane pipeline, branch
  policy, live-verification checklist).

### 5. First run

Comment `/implement` on an issue (or a work item on Azure DevOps). forge
posts a plan; reply with `/go <run-id>` (or assign the `forge` label on
GitHub). When the run reaches `ready_for_human`, the evidence comment
carries everything a reviewer needs. The merge button stays yours.

## Dogfooding

forge is developed **through forge**: issues on this repo run the full loop
(`/implement` → gate → agent in Actions → Draft PR) — see
[docs/reference/dogfooding.md](docs/reference/dogfooding.md).

## For AI agents

This repository is built to be worked on by coding agents:

- **[AGENTS.md](AGENTS.md)** — the agent entry point: rules, repo map,
  gotchas, verification gates.
- **[docs/onboarding-prompt.md](docs/reference/onboarding-prompt.md)** — a
  copy-paste prompt that takes a fresh clone to a verified dev environment.
- **[.claude/skills/](.claude/skills/)** — task playbooks (readable by any
  agent): [`forge-setup`](.claude/skills/forge-setup/SKILL.md),
  [`forge-lab`](.claude/skills/forge-lab/SKILL.md),
  [`forge-debug-run`](.claude/skills/forge-debug-run/SKILL.md),
  [`forge-onboard-project`](.claude/skills/forge-onboard-project/SKILL.md),
  [`forge-demo`](.claude/skills/forge-demo/SKILL.md).
- **`forge doctor`** — the setup oracle: `uv run python -m forge.doctor
  [--project <id>] [--json]`; exit code 0 means the environment (or a
  target project's onboarding) is complete. Read-only; never prints secret
  values.

## Documentation

The full index lives at **[docs/README.md](docs/README.md)**. Highlights:

| Doc | Scope |
|-----|-------|
| [docs/harnesses/](docs/harnesses/README.md) | the four drivers + multi-harness auto-selection (how it works, fallback, rejected alternatives) |
| [docs/harnesses/claude-code.md](docs/harnesses/claude-code.md) | Claude Code: variables, flags, MCP, gotchas |
| [docs/harnesses/grok-build.md](docs/harnesses/grok-build.md) | Grok Build: auth rotation, the npm hang fix, deny rules |
| [docs/harnesses/opencode.md](docs/harnesses/opencode.md) | opencode: injected permission map, MCP translation |
| [docs/harnesses/copilot-cli.md](docs/harnesses/copilot-cli.md) | Copilot CLI: fine-grained PAT, scoped grants |
| [docs/harnesses/onboarding.md](docs/harnesses/onboarding.md) | shared harness setup: includes, MCP servers, writing the task |
| [docs/getting-started/gitlab.md](docs/getting-started/gitlab.md) | GitLab CE project onboarding |
| [docs/getting-started/github.md](docs/getting-started/github.md) | GitHub App + project setup |
| [docs/getting-started/azure-devops.md](docs/getting-started/azure-devops.md) | Azure DevOps setup + the live-verification checklist |
| [docs/operations/operator-commands.md](docs/operations/operator-commands.md) | the operator surface: `/implement` `/go` `/cancel` `/retry` `/status` `/why-blocked` `/reconcile` `/security`, the `forge` label, auto-revive |
| [docs/faq.md](docs/reference/faq.md) | frequently asked questions (all providers) |
| [docs/adr/](docs/adr/) | architecture decisions (0000–0027) |
| [demo/](demo/) | sales demo script + regeneration skill |

## License

BSD-3-Clause — see [LICENSE](LICENSE). Provenance and third-party notices:
[UPSTREAM.md](UPSTREAM.md), [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
