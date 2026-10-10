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

## Operator surface

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

1. **Run the tests before committing**: `set -o pipefail && .venv/bin/python -m pytest -q -m "not slow"`
   (for lifecycle changes also run `.venv/bin/python -m pytest -q -m slow`), then
   `.venv/bin/ruff format . && .venv/bin/ruff check .`. Never commit with a red suite.
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
6. **Follow the simplification program** ([ADR-0035](docs/adr/0035-simplification-program.md)):
   [DESIGN](docs/roadmap/2026-09-29-simplification/DESIGN.md) is the target architecture and
   [ROADMAP](docs/roadmap/2026-09-29-simplification/ROADMAP.md) lists the work items. One task
   per PR, about 500 net lines at most (pure deletions are exempt); fill in the PR template.
7. **The guardrails are gates** (the CI `lint` job runs them):
   - `uv run lint-imports` checks the import contracts in `pyproject.toml`;
   - `uv run python scripts/ratchet.py`: oversized code may only shrink, new files stay
     within 600 lines and new functions within 80. After shrinking or deleting oversized
     code, run `uv run python scripts/ratchet.py --update` and commit
     `scripts/ratchet-baseline.json`; `--update --accept <path>` is only for a reviewed
     move of existing oversized code;
   - `uv run python scripts/prose_lint.py --base origin/main`: added lines under `src/`
     and `packages/*/src/` carry no review, backlog or issue ids (history lives in git and
     ADRs). It reads commits, so run it after committing. A line that must keep a matching
     literal, such as the colour `#009966`, carries the marker `prose-lint: allow`.
8. **Never weaken the Invariant Suite** (`tests/invariants/registry.py`, DESIGN §4):
   removing or skipping evidence for I1–I8 or the self-trigger rule (ADR-0028) needs an
   ADR.
9. **Deleted code keeps its lessons.** Before a module is deleted or rewritten, every
   gotcha it carries must be a row in the [edge-case register](docs/reference/edge-cases.md)
   (add the missing ones with their pinning tests), and its rows must still hold: port
   the pinning tests and repoint the rows in the same PR, or retire a row that no longer
   applies. A test file is deleted only when its row in
   [`test-ledger.csv`](docs/roadmap/2026-09-29-simplification/test-ledger.csv) says so.

## Repo map

```
src/forge/
  main.py, api_*.py  the FastAPI app and its operator, lane-control and checkpoint APIs
  config.py    settings (see .env.example)
  database.py, migrate.py, models/
               engine and sessions, schema check, `python -m forge.migrate`, the
               declarative Base and the legacy bot's tables
  gateway/     webhook ingress (GitLab, GitHub, Azure DevOps), command parsing and routing
  worker/      Redis task queue, worker loop, step claims, reconciler boot
  durable/     controller: transitions, gates (consume-once), inbox, outbox, intents,
               budgets
  runs/        RunService per provider (service.py, github_service.py, azure_service.py),
               RunSpec, publisher (the only repository writer), CI verification, repair
               loop, revival, reconciler
  adaptive/    the largest package: discovery and planning, operator control,
               checkpoints, credential broker, usage and budgets, publication saga,
               SDK lane drivers
  repository/  ChangeSet (ADR-0001) + ChangesetWriter (Commits API)
  gitlab/, integrations/
               provider clients: GitLab; GitHub App and Azure DevOps
  execution/   lane executors on GitHub Actions and Azure Pipelines
  harnesses/   lane scripts, prompts and MCP config rendered for the target's CI
  harness_entry.py, lane_driver.py
               the lane side: CLI and SDK drivers inside the target's CI
  factory/     LLM planner / implementer / reviewer (LiteLLM HTTP)
  context/     context assembly, diff and log parsing, redaction
  reactive/    durable review and CI-debug bots on the step lane
  findings/    security findings and /security triage
  mcp_server/  forge's MCP server (token-scoped tools)
  orchestrator/project_config.py
               the project's .forge.yml reader, used by every RunService (P1-01 moves it)
  orchestrator/, flows/, agents/, mcp_client/, stores/
               legacy GitLab reactive bot: review, pipeline debug, chat; agents/ also
               holds the LLM agents that reactive/ CI debug and findings/ triage use
  release_*.py, profile_qualification.py
               release gates
  doctor.py    environment verification (this is your setup oracle)
agents/        YAML agent definitions (FORGE_AGENTS_DIR) and the legacy flows in flows/
alembic/       schema migrations
ci/templates/  lane CI templates (GitLab CI, GitHub Actions, Azure Pipelines) + log filter
scripts/       program guardrails (metrics, ratchet, prose lint, test ledger), lab and
               release tooling
tests/         tiered (docs/reference/testing.md); tests/invariants/registry.py lists the
               Invariant Suite files; fixtures/fake_gitlab.py is the GitLab fake
docs/adr/      architecture decisions (start with 0003, 0005, 0016, 0026, 0035)
docs/roadmap/2026-09-29-simplification/
               DESIGN, ROADMAP, metrics and the test ledger
```

## Environment setup (dev)

```bash
uv sync                      # Python 3.13+, deps incl. dev
set -o pipefail
.venv/bin/python -m pytest -q -m "not slow"   # default tier; must be green before any commit
.venv/bin/python -m pytest -q -m slow         # real processes (production_entry, fi_os)
.venv/bin/ruff format . && .venv/bin/ruff check .
uv run lint-imports && uv run python scripts/ratchet.py
uv run python scripts/prose_lint.py --base origin/main
```

The default tier needs no database or services (fakes only); the slow tier
needs `redis-server` on `PATH`. [docs/reference/testing.md](docs/reference/testing.md)
explains the tiers and how to run the Invariant Suite.

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

The [edge-case register](docs/reference/edge-cases.md) records each known gotcha
with the test or reference that pins it; the IDs below are its rows. Read a row
before removing a guard that looks redundant, and add a row when you learn a
new one.

- **zsh does not word-split unquoted variables** (OP-04) — never
  `for x in $VAR` over multi-line command output; use `while IFS= read -r`
  loops.
- **pytest piped to tail hides exit codes** (OP-05) — always
  `set -o pipefail` first.
- GitLab CE 18.x Commits API **400s if `start_branch` is present** and the
  branch exists (EC-01) — `ensure_branch` owns creation; the writer never
  passes it.
- Claude Code on z.ai (OP-01): model ids need the `[1m]` suffix for the 1M
  window (bare `glm-5.3-flash` is treated as 200k).
- Harness CLIs in CI must install their **platform binary explicitly**,
  pinned to the wrapper's version (EC-02, EC-03: npm optionalDependencies
  flake → silent futex hang). See the grok template's before_script for the
  pattern.
- Long-lived AI streams from some runner networks crawl (OP-06: 226s vs 2s
  via a proxy on a fast path). The grok template supports
  `FORGE_HARNESS_HTTPS_PROXY`.
- Redis `forge:worker:<id>` heartbeat keys persist ~30s after a worker
  dies (OP-02) — `/metrics` `workers_active` lags; check logs, not just
  metrics.
- DLQ (`forge:tasks:dead`) holds exhausted *tasks*; run-level outcomes live
  in Postgres `flow_runs` (OP-07). A DLQ entry is not a stuck run by itself.
- Full run triage recipe: [.claude/skills/forge-debug-run/SKILL.md](.claude/skills/forge-debug-run/SKILL.md).

## Where work stands

The shipped factory and its history live in the [CHANGELOG](CHANGELOG.md)
and [docs/README.md](docs/README.md). The current work is the simplification
program ([ADR-0035](docs/adr/0035-simplification-program.md)):
[DESIGN](docs/roadmap/2026-09-29-simplification/DESIGN.md) is the target,
[ROADMAP](docs/roadmap/2026-09-29-simplification/ROADMAP.md) the phased task
list and [metrics.md](docs/roadmap/2026-09-29-simplification/metrics.md) the
measured progress. The live lab (`/implement` → branch → green CI → Draft
MR → `ready_for_human`) is the definition of done for any lifecycle change.
