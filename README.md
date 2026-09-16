# forge

**forge** is an agentic software-factory sidecar for **self-hosted GitLab CE**,
**GitHub**, and **Azure DevOps (beta)**: an authorized issue becomes a plan, a
human-approved branch with code, a green pipeline, and a Draft merge request —
ready for human review. You bring the model (any provider via
[LiteLLM](https://docs.litellm.ai/) or native harness CLIs); forge runs on
your own infrastructure and never merges.

forge is based on the open-source project
[Codeward](https://github.com/Relrin/codeward). See [UPSTREAM.md](UPSTREAM.md)
for provenance and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for
license notices.

## The iron principle: the bot never merges

forge proposes changes, drives the pipeline, and assembles evidence bound to
a specific commit. It never merges, never pushes to a protected target
branch, and never changes permissions. Review and merge are always performed
by a human in GitLab/GitHub. This is enforced by the executor's capabilities
and platform permissions — not by prompt instructions. See
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
  agents (Claude Code, Grok Build, opencode) run in ephemeral CI containers
  with **no write credentials and no forge secrets**; their output is a
  candidate artifact that a trusted publisher validates and applies.
- **One trusted publisher** for every backend — builtin LLM ChangeSets and
  CLI agents share the same validation, policy, and journal.
- **Human gate** ([ADR-0018](docs/adr/0018-immutable-run-spec.md)): the plan
  is frozen into an immutable RunSpec; the decision has a deadline; cancel
  revokes the publication grant before it stops the runner.
- **Quality contract**
  ([ADR-0008](docs/adr/0008-quality-contract-instead-of-pipeline-status.md)):
  pipeline success + required jobs green; failures classified code /
  infrastructure / config / unknown — only *code* failures trigger the
  bounded repair loop; unknown evidence never blames the code.

## Providers

The AzDO column is **beta**: the full loop is proven by the test suite
(gate machinery, lane, reactive lanes, cross-slice joins over recorded
payload shapes); live verification against a real organization is
pending a user PAT — the checklist in
[docs/azure-setup.md](docs/azure-setup.md) lists exactly what to
confirm.

| Capability | GitLab CE | GitHub | Azure DevOps (beta) |
|---|---|---|---|
| Commands (`/implement`, `/go`, `/cancel`) | ✅ comments | ✅ comments + `forge` label | ✅ work-item + PR comments (label trigger: —) |
| Plan comment + human gate | ✅ | ✅ | ✅ (work-item comment) |
| Harness execution (Claude Code / Grok Build / opencode) | ✅ project CI (docker executor) | ✅ GitHub Actions (`workflow_dispatch`) | ✅ Azure Pipelines (Runs-API dispatch; verified in tests, live pending PAT) |
| Builtin LLM implementer (no CI needed) | ✅ | ✅ | ✅ |
| Trusted publisher | Commits API | GraphQL `createCommitOnBranch` + `expectedHeadOid` CAS | Push API CAS (`oldObjectId`) |
| Draft MR / PR before CI | ✅ | ✅ | ✅ (`isDraft: true`) |
| Readonly LLM review | ✅ | ✅ | ✅ reactive PR threads (beta) |
| CI-failure debug lane | ✅ | ✅ | ✅ timeline + task logs (beta) |
| Fork/`pull_request_target` flows | — | intentionally out of scope (first beta) | — |
| Identity | bot user + PAT | GitHub App installation (+ PAT lab mode) | service account + PAT (Entra SPN = upgrade path) |
| Webhook authenticity | secret token | HMAC signature | Basic credentials (no HMAC exists) over HTTPS |

Architecture: four orthogonal adapters — source, execution, harness driver,
model route ([ADR-0019](docs/adr/0019-source-execution-adapters.md)).
Adding a provider is an adapter, not a second factory.

## Harness selection (ADR-0023)

Instead of one pinned implementer harness, a project declares an ordered
preference (`forge.yml`):

```yaml
forge:
  implement:
    harnesses: [claude-code, grok-build]
```

(the env form is `FORGE_HARNESS_PREFERENCE=claude-code,grok-build`; empty —
today's configured backend alone). At plan time forge compiles the chain
against the lanes the project onboarded (`forge doctor` reports exactly
that), freezes the selected harness + fallback tail + budget class into the
RunSpec, and shows them in the plan comment's **Implementation** block — the
`/go` approves the execution shape, not just the plan text. The planner may
propose an entry of the list with a one-line reason; it can reorder, never
extend. Dispatch sets `FORGE_HARNESS_DRIVER` (GitLab) / the `driver` input
(Actions) so multi-template repos run exactly one lane. Dispatch-time
fallback down the frozen chain exists, is **off by default**
(`FORGE_HARNESS_FALLBACK=true`), fires only on infrastructure-classified
failures before any candidate, and journals every advance.

## Status

**v0.5.0** — both providers live-verified end-to-end (plan → gate → agent →
candidate → Draft MR/PR → review → ready_for_human), failure-injection
proven durable runtime, AI-ready onboarding. **Pre-production**: expect
breaking changes before 1.0. Honest gap list: budget reservation beyond the
commit-cycle cap, redaction at every agent boundary, drift policies beyond
block, packaging split. The
[CHANGELOG](CHANGELOG.md) has the full history.

## Quick start

### 1. Clone and verify (no services needed)

```bash
git clone https://github.com/forcewake/forge && cd forge
uv sync
set -o pipefail && .venv/bin/python -m pytest -q   # unit suite over fakes
.venv/bin/ruff format . && .venv/bin/ruff check src tests
```

### 2. Configure

```bash
cp .env.example .env   # then edit: GITLAB_URL/TOKEN or GitHub App values,
                       # model key, DATABASE_URL (Postgres), REDIS_URL,
                       # LITELLM_URL
```

### 3. Run the stack

```bash
# Postgres + Redis + LiteLLM (any way you like; podman example in docs)
python -m forge.migrate                 # apply schema migrations
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
- **Azure DevOps (beta):** [docs/azure-setup.md](docs/azure-setup.md)
  (service account + PAT scopes, service hooks, lane pipeline, branch
  policy, live-verification checklist).

### 5. First run

Comment `/implement` on an issue (or a work item on Azure DevOps). forge
posts a plan; reply `@forge /go <run-id>` (or assign the `forge` label on
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
| [docs/github-setup.md](docs/github-setup.md) | GitHub App + project setup + FAQ |
| [docs/azure-setup.md](docs/azure-setup.md) | Azure DevOps (beta): PAT scopes, service hooks, lane, branch policy |
| [docs/faq.md](docs/faq.md) | frequently asked questions (all providers) |
| [docs/harness-onboarding.md](docs/harness-onboarding.md) | harness CLIs: setup + triage |
| [docs/operations/](docs/operations/) | backup/restore, upgrade, token rotation, retention |
| [docs/adr/](docs/adr/) | architecture decisions (0000–0024) |
| [docs/research/](docs/research/) | live API research (GitHub, Actions, Azure DevOps, harnesses) |
| [demo/](demo/) | sales demo script + regeneration skill |

## License & provenance

BSD-3-Clause — see [LICENSE](LICENSE), [UPSTREAM.md](UPSTREAM.md),
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
