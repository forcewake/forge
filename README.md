# forge

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
  commands and steps live in Postgres; a crash at any transition or after
  any external effect converges — proven by a failure-injection suite (two
  workers, real Postgres, kill at six checkpoints).
- **Proposal-only execution lane**
  ([ADR-0016](docs/adr/0016-candidate-bundle-trusted-publisher.md)): coding
  agents run in ephemeral CI with **no write credentials and no forge
  secrets**; their output is a candidate artifact that a trusted publisher
  validates and applies.
- **One trusted publisher** for every backend — builtin LLM ChangeSets and
  CLI agents share the same validation, policy, and journal.
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

## Four harness drivers, one contract

Every driver implements the same proposal-only contract — mechanical
commit/push deny (driver-native rules, not just the brief), scoped tool
grants, candidate artifact handoff, usage receipts where the vendor
provides them (unknown ≠ zero), and optional MCP servers
(`FORGE_HARNESS_MCP`, [ADR-0022](docs/adr/0022-harness-mcp-integration.md):

| Driver | Headless posture | Notes |
|---|---|---|
| **Claude Code** | `-p` + stream-json, `--strict-mcp-config` | live-verified on all three providers |
| **Grok Build** | `--always-approve` + deny rules, hardened npm preamble | platform-binary hang workaround |
| **opencode** | permission map via injected config | schema-translated MCP |
| **GitHub Copilot CLI** | `-p` + deny-wins tool rules | subscription auth (fine-grained PAT) |

## Task-aware harness selection ([ADR-0023](docs/adr/0023-dynamic-harness-selection.md))

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

**v0.9.0** — three providers live-verified end-to-end, four harness
drivers, task-aware selection, scoped MCP run surface, delivery-ladder
metrics, images on GHCR (`ghcr.io/forcewake/forge:0.9.0`). 1727 tests;
failure-injection-proven durable core; mypy-clean over the typed core.
**Pre-production**: expect breaking changes before 1.0. The
[CHANGELOG](CHANGELOG.md) has the full history.

## Quick start

### 1. Run the image (or build from source)

```bash
docker run -d --name forge -p 8420:8420 \
  --env-file .env ghcr.io/forcewake/forge:0.9.0
# or from source:
git clone https://github.com/forcewake/forge && cd forge
uv sync && set -o pipefail && .venv/bin/python -m pytest -q
```

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

- **GitLab CE:** [docs/operations/onboarding.md](docs/operations/onboarding.md)
  (webhook, bot PAT, harness template include, `forge doctor`).
- **GitHub:** [docs/github-setup.md](docs/github-setup.md)
  (GitHub App registration, webhook, secrets, harness workflow, label
  trigger, `forge doctor`).
- **Azure DevOps:** [docs/azure-setup.md](docs/azure-setup.md)
  (service account + PAT scopes, service hooks, lane pipeline, branch
  policy, live-verification checklist).

### 5. First run

Comment `/implement` on an issue (or a work item on Azure DevOps). forge
posts a plan; reply with `/go <run-id>` (or assign the `forge` label on
GitHub). When the run reaches `ready_for_human`, the evidence comment
carries everything a reviewer needs. The merge button stays yours.

## For AI agents

This repository is built to be worked on by coding agents:

- **[AGENTS.md](AGENTS.md)** — the agent entry point: rules, repo map,
  gotchas, verification gates.
- **[docs/onboarding-prompt.md](docs/onboarding-prompt.md)** — a
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

| Doc | Scope |
|-----|-------|
| [AGENTS.md](AGENTS.md) | agent entry point + repo conventions |
| [docs/onboarding-prompt.md](docs/onboarding-prompt.md) | bootstrap prompt for coding agents |
| [docs/operations/onboarding.md](docs/operations/onboarding.md) | GitLab CE project onboarding |
| [docs/github-setup.md](docs/github-setup.md) | GitHub App + project setup |
| [docs/azure-setup.md](docs/azure-setup.md) | Azure DevOps: PAT scopes, service hooks, lane, branch policy |
| [docs/faq.md](docs/faq.md) | frequently asked questions (all providers) |
| [docs/harness-onboarding.md](docs/harness-onboarding.md) | harness CLIs: setup + triage |
| [docs/operations/](docs/operations/) | backup/restore, upgrade, token rotation, retention |
| [docs/adr/](docs/adr/) | architecture decisions (0000–0024) |
| [docs/research/](docs/research/) | live API research (GitHub, Actions, Azure DevOps, harnesses, MCP) |
| [demo/](demo/) | sales demo script + regeneration skill |

## License

BSD-3-Clause — see [LICENSE](LICENSE). Provenance and third-party notices:
[UPSTREAM.md](UPSTREAM.md), [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
