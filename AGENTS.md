# AGENTS.md

Forge 1.0 turns an authorized issue into an approved plan, bounded CI execution,
and a verified Draft PR/MR. The bot never merges. GitHub and GitLab CE share
one lifecycle engine. Claude Code and Copilot CLI are the supported drivers.

Read [ADR-0035](docs/adr/0035-simplification-program.md),
[DESIGN](docs/roadmap/2026-09-29-simplification/DESIGN.md) and
[ROADMAP](docs/roadmap/2026-09-29-simplification/ROADMAP.md) for the target.
[The installation guide](docs/onboarding/cold-install-runbook.md) describes
actual current entry points. Configuration is closed TOML; secrets come only
from explicitly named environment variables. There is no automatic `.env`
loader, Redis worker, old-runtime routing flag or compatibility API.

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

## Source map

- `src/forge/domain`: immutable policy, plans, approval, candidates and verdicts.
- `ports.py`, `port_values.py`: typed provider contracts and outcomes.
- `engine`: one lifecycle and its journaled external-effects boundary.
- `store`: normalized SQL authority, fenced claims, commands, approvals,
  controls, exact checkpoints and the single packaged schema baseline.
- `providers/github`, `providers/gitlab`: native authentication, reads,
  Git publication, CI, reviews and ingress.
- `planning`, `review`, `verification`, `budget`: bounded model work,
  independent evidence and enforced reservations.
- `runtime`: closed configuration, composition, webhooks, operator API,
  health, metrics, diagnostics and the worker loop.
- `api`: signed lane sessions and the versioned execution API.
- `packages/forge-protocol`: immutable lane wire contracts and schemas.
- `packages/forge-lane`: independent runner, exact WIP capture/restore and
  supported native drivers; never imports the server.

The old runtime is removed. Its exact recoverable source is listed in
[the retirement manifest](docs/archive/runtime-0.42.json).

## Development and operation

Use Python 3.13+ and `uv sync --all-packages --extra postgres --extra claude`.
The current CLI is `forge --config forge.toml`: explicit `install` for an
empty database, read-only `doctor`/`onboard`, then separate `serve` and
`worker` processes. Startup checks the schema and performs no DDL.
`forge-schema` owns explicit initialization, checks and offline history
export/cutover. Never run a migration against the owner's lab implicitly.

Run Ruff, strict typing across all three distributions, all import contracts,
`python scripts/check_authority_boundaries.py pyproject.toml`, the size
ratchet and protocol-schema drift check. Run prose lint immediately after
every commit. Keep every gate definition and threshold intact.

## Current owner direction

The owner authorized local 1.0 implementation before GitHub prerequisites,
and reviewed all 129 flagged/drop ledger rows with existing dispositions
unchanged. Do not wait for hosted required checks, a maintenance-branch push
or a recovery tag to implement locally. This does not authorize pushing,
merging, tagging, publishing or changing GitHub settings.

The owner subsequently stopped repeated full test runs, and on 2026-10-24
explicitly directed completion of the working product instead of continuing
old-test ports. Finish the sole runtime and release artifacts. Use focused
verification and one assembled invariant/working-scenario check. Preserve
the Invariant Suite, edge-case register, curated ledger fields and historical
test sources; report old corpus migration separately without masking failures.
Do not reintroduce an old service to make a historical test import succeed.

Owner environment files, databases and running lab services are outside this
local implementation. Use only explicitly owned disposable resources for
verification. No paid model calls or target-provider writes are authorized.
