# AGENTS.md

Instructions for AI coding agents (Claude Code, opencode, Grok Build, Codex,
Cursor, ...) working in this repository. Humans benefit too.

**Ready-to-use bootstrap prompt for your agent: [docs/reference/onboarding-prompt.md](docs/reference/onboarding-prompt.md).**
Task-specific playbooks live in `.claude/skills/` (any agent can read them as
markdown): [setup](.claude/skills/forge-setup/SKILL.md),
[lab](.claude/skills/forge-lab/SKILL.md),
[debug a run](.claude/skills/forge-debug-run/SKILL.md),
[onboard a project](.claude/skills/forge-onboard-project/SKILL.md).

## What forge is

An agentic software factory for GitLab CE, GitHub, and Azure DevOps: an
authorized issue (`/implement`) becomes a factory branch with code, a green
pipeline, and a Draft MR/PR — **the bot never merges**. Enforced by
capabilities and platform permissions, never by prompts
([ADR-0003](docs/adr/0003-no-merge-is-enforceable.md)).

## Operator surface (v0.11)

The provider-neutral operator commands are `/implement` `/go` `/cancel`
`/retry` `/status` `/why-blocked` `/reconcile` `/security` — one table of
effect × authorization × provider parity, the `forge` label semantics
(on = plan, off = cancel-at-gate), auto-revive, and the honest gaps:
[docs/operations/operator-commands.md](docs/operations/operator-commands.md).

Config knobs added since the early milestones (all in `src/forge/config.py`,
annotated in `.env.example`):

- `FORGE_RUN_AUTO_REVIVE_LIMIT` / `FORGE_RUN_REVIVE_BACKOFF_SECONDS` —
  Tier-1 auto-revive budget (default 2) and backoff-ladder base (60s).
- `FORGE_HARNESS_DISCOVERY_MAX_ATTEMPTS` — bounds lost-dispatch discovery
  before the run parks blocked.
- `FORGE_DRIVER_VERSIONS` — JSON driver→CLI version pins; the lane falls
  back to forge's baked-in known-good defaults when unset.
- `FORGE_AVAILABLE_DRIVERS` — JSON manifest of onboarded drivers; caps the
  selection chain, never extends it.
- `FORGE_BUDGET_PROFILES` — JSON named numeric budget ceilings, resolved at
  RunSpec freeze.
- `FORGE_WRITE_PROFILES` / `FORGE_PIPELINE_ENTRYPOINTS` — custom write
  profiles and per-project pipeline entrypoints (denied under every
  profile).
- `FORGE_MCP_TOKEN_REPOS` — per-token repository-target allowlist for
  scoped MCP principals.
- `FORGE_SECURITY_TRIAGERS` / `FORGE_SECURITY_AUTO_ACCEPT` — who may
  confirm AI triage verdicts; machine auto-accept is off by default.
- `FORGE_VERIFICATION_GRACE_SECONDS` — grace before a fresh PR's missing
  checks conclude "no CI configured".
- `FORGE_MAX_THINKING_TOKENS` — lane-side: caps Claude thinking on repair
  re-dispatches (consumed inside the harness job).

## Non-negotiable rules for agents

1. **Run the tests before committing**: `set -o pipefail && .venv/bin/python -m pytest -q`,
   then `ruff format . && ruff check src tests`. Never commit with a red suite.
2. **Secrets never enter the repo or the conversation record.** `.env` is
   gitignored; tokens live in GitLab CI variables. If the user pastes a
   token, use it for the task, never echo it into files, commits, or tests.
3. **Never** push to a protected branch of a *target* project, never merge,
   never modify GitLab permissions, never weaken the verification layers
   (SHA verification, gate consumption, classification) to make a test pass.
4. **ADRs in `docs/adr/` are binding.** If a change contradicts one, propose
   an ADR amendment instead of silently diverging.
5. Prefer the smallest change that satisfies the request; match the code's
   existing style; add regression tests for every bug you fix.

## Repo map

```
src/forge/
  gateway/     webhook endpoint + routing (/implement, /go, /cancel)
  worker/      Redis task queue + worker loop + reconciler boot
  runs/        RunService lifecycle, ci_harness/builtin backends, repair loop,
               CI quality contract, reconciler
  durable/     controller: transitions, gates (consume-once), outbox, leases
  repository/  ChangeSet (ADR-0001) + ChangesetWriter (Commits API)
  factory/     LLM planner / implementer / reviewer (LiteLLM HTTP)
  orchestrator/ legacy reactive flows (review, pipeline debug, chat)
  doctor.py    environment verification (this is your setup oracle)
ci/templates/  harness CI jobs (claude-code, opencode, grok) + event filters
tests/         2600+ tests; fixtures/fake_gitlab.py is the GitLab fake
docs/adr/      architecture decisions (read 0001-0027 before redesigning)
```

## Environment setup (dev)

```bash
uv sync                      # Python 3.13+, deps incl. dev
set -o pipefail
.venv/bin/python -m pytest -q # must be green before any commit
.venv/bin/ruff format . && .venv/bin/ruff check src tests
```

No database or services are needed for the unit suite (fakes only).

## Verification oracle

After any setup or wiring change:

```bash
uv run python -m forge.doctor --json   # add --project <id> for a target project
```

Exit code 0 = environment is complete. `--json` is for agents; the human
format prints PASS/WARN/FAIL lines. Never print CI variable *values*.

## Lab (GitLab CE + local stack)

The integration lab runs forge in podman against a real GitLab CE. Bring-up,
container recreation, and image rebuild are scripted in
[.claude/skills/forge-lab/SKILL.md](.claude/skills/forge-lab/SKILL.md).
Summary: `forge-postgres` (5433), `forge-redis` (6379), `forge-litellm`
(4000), `forge-app` (8420), `forge-worker`; app/worker are recreated with
explicit `-e` env from `.env` (never `--env-file`, the URLs differ:
containers reach host services via `host.containers.internal`).
Image rebuild after code changes:

```bash
printf 'FROM localhost/forge:dev\nCOPY --chown=forge:forge src /app/src\n' > /tmp/f.Containerfile
podman build -t localhost/forge:dev -f /tmp/f.Containerfile .
```

## Known gotchas (learned the hard way)

- **zsh does not word-split unquoted variables** — never `for x in $VAR`
  over multi-line command output; use `while IFS= read -r` loops.
- **pytest piped to tail hides exit codes** — always `set -o pipefail` first.
- GitLab CE 18.x Commits API **400s if `start_branch` is present** and the
  branch exists — `ensure_branch` owns creation; the writer never passes it.
- Claude Code on z.ai: model ids need the `[1m]` suffix for the 1M window
  (bare `glm-5.3-flash` is treated as 200k).
- Harness CLIs in CI must install their **platform binary explicitly**
  (npm optionalDependencies flake → silent futex hang). See the grok
  template's before_script for the pattern.
- Long-lived AI streams from some runner networks crawl (226s vs 2s via a
  proxy on a fast path). The harness templates support
  `FORGE_HARNESS_HTTPS_PROXY`.
- Redis `forge:worker:<id>` heartbeat keys persist ~30s after a worker
  dies — `/metrics` `workers_active` lags; check logs, not just metrics.
- DLQ (`forge:tasks:dead`) holds exhausted *tasks*; run-level outcomes live
  in Postgres `flow_runs`. A DLQ entry is not a stuck run by itself.
- Full run triage recipe: [.claude/skills/forge-debug-run/SKILL.md](.claude/skills/forge-debug-run/SKILL.md).

## Where work stands

Milestones M0–M4 are complete (durable run loop, real LLM agents,
ci_harness backends, harness repair, failure-injection drills, release
engineering); the current state of the factory and its history live in the
[CHANGELOG](CHANGELOG.md) and [docs/README.md](docs/README.md). The live
lab (`/implement` → branch → green CI → Draft MR → `ready_for_human`) is
the definition of done for any lifecycle change.
