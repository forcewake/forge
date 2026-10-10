# Handoff: the forge simplification program

This page is for the next AI coding agent, or engineer, who picks up the
simplification program. It says where the work stands, what to do next, how
work is done here, and what was learned the hard way. [AGENTS.md](../../../AGENTS.md)
stays the authoritative guide; read it first.

The **State** section is a snapshot from 2026-10-11. The live status is the
**Status** line of each phase in [ROADMAP.md](ROADMAP.md) and the measured
history in [metrics.md](metrics.md). Keep those current as work lands, not
this snapshot.

## Read this first

In this order (about an hour):

1. [AGENTS.md](../../../AGENTS.md): rules 1–9, the repo map, the commands
   and the gotchas.
2. [ADR-0035](../../adr/0035-simplification-program.md): why the program
   exists and what it changes.
3. [DESIGN.md](DESIGN.md): §1 summary, §4 invariants I1–I8, §5 scope for
   1.0, §6 target architecture, §7 test strategy, §8 guardrails and §14,
   the owner's decisions.
4. [ROADMAP.md](ROADMAP.md): the Phase 0 status line, then all of Phase 1.
5. [testing.md](../../reference/testing.md) and the
   [edge-case register](../../reference/edge-cases.md).
6. Skim [PLAN-phase-0.md](PLAN-phase-0.md): the shape a phase plan takes
   here. Read its "Self-review record" and "Follow-ups" sections in full.

## The product and the program in brief

forge turns an authorized issue (`/implement`, then `/go`) into a factory
branch with code, a green pipeline and a Draft MR or PR on GitHub, GitLab CE
or Azure DevOps. The bot never merges (ADR-0003). Its safety core is worth
keeping: one validated write path, intent journaled before every effect,
consume-once approval, enforced budgets. The weight around that core is not:
much of `src/forge` is off every runtime path, the lifecycle exists once per
provider, and prose and qualification ceremony dominate the cost of change.

The program keeps the core and removes the weight in phases, with the
product deployable after each one:

| Phase | Content | State on 2026-10-11 |
| --- | --- | --- |
| 0 | Guardrails and baseline | Code complete; owner steps open |
| 1 | Subtract: test-only code, the qualification machinery, Azure (to `release/0.42.x`), the builtin implementer, the Codeward legacy, deferred features; prose and docs diets | Not started |
| 2 | Separate: `forge-protocol` and `forge-lane` | Not started |
| 3 | Unify: one lifecycle engine over provider ports, the `effects` layer | Not started |
| 4 | Productize: install, docs, pilots, 1.0 | Not started |

The owner's decisions (DESIGN §14) are settled; do not reopen them without
the owner:

- GitHub is the primary provider and GitLab the second. Azure DevOps is
  frozen on `release/0.42.x`.
- Claude Code and Copilot CLI are the first-class drivers.
- The approach is a strangler: the product keeps working, with no big-bang
  rewrite.
- Data lives only in the lab and dogfooding, so the schema may break.
- Documents live in the repository, in English.

## State on 2026-10-11

### What Phase 0 delivered

All 16 tasks of [PLAN-phase-0.md](PLAN-phase-0.md) (ROADMAP P0-01 to P0-14)
are done:

| Guardrail | Where it lives | Enforced by |
| --- | --- | --- |
| Test tiers (`slow` and the default tier) | `tests/_hygiene.py`, `tests/conftest.py` | `test` and `tests-slow` CI jobs |
| Leaked-process check and orphan guard | `pytest_sessionfinish` in `tests/conftest.py`, `tests/production_entry/orphan_guard.py` | Every test run |
| Invariant Suite (I1–I8 and the self-trigger rule) | `tests/invariants/registry.py` | `invariants` job (Postgres; any skip fails) |
| Import contracts | `[tool.importlinter]` in `pyproject.toml` | `lint` job |
| Size ratchet | `scripts/ratchet.py`, `scripts/ratchet-baseline.json` | `lint` job |
| Prose lint | `scripts/prose_lint.py` | `lint` job |
| Diff coverage of at least 85% | `coverage` job in `.github/workflows/ci.yml` | Pull requests |
| Metrics | `scripts/metrics.py`, `metrics.md`, `metrics-history.json` | By hand, weekly and at phase gates |
| Edge-case register | `docs/reference/edge-cases.md` | `tests/test_edge_case_register.py` |
| Test disposition ledger | `scripts/build_test_ledger.py`, `test-ledger.csv` | Regenerated when tests change |
| Optional GitLab; a doctor that never raises | `FORGE_GITLAB_ENABLED`, `src/forge/doctor.py` | Default tier |
| Working agreement | `AGENTS.md`, the PR template, the forge-setup skill | Review |

The GitLab flag and the doctor change are the only production-code
changes; the CHANGELOG lists them under [Unreleased].

At the Phase 0 exit on a MacBook:

- the default tier passed (9,562 passed, 38 skipped). On an idle machine it
  takes about 543 s, against a 480 s target;
- the slow tier passed (152 tests);
- every gate was green, and no process leaked.

The history reads well: `git log --oneline 7ed64ba..bf06fd6` shows the
planning documents, then the commits of each task, interleaved with
`docs(roadmap): sync ...` commits that kept the plan equal to the code.

### What is not done

- **GitHub has not seen the work.** It was merged into the owner's local
  `main` and had not been pushed. Check with
  `git log --oneline origin/main..main`.
- **The new CI jobs have never run on GitHub.** The first push or pull
  request is their first run, so expect CI-only failures on Linux, on
  Python 3.14 and with Postgres. `TestRealPostgresRaces` in
  `tests/test_budget_amendment.py` runs in CI for the first time, in the
  `invariants` job.
- **Owner-only steps** (the Phase 0 status line in ROADMAP):
  - push `release/0.42.x` as
    [maintenance-0.42.md](../../operations/maintenance-0.42.md) describes;
  - tag `pre-simplification` on `main` before any Phase 1 deletion lands;
  - review `test-ledger.csv` (its `note` column belongs to the owner);
  - make the new jobs required once they are green.
- **Tracking does not exist yet.** ROADMAP "Tracking" plans issues
  labelled `simplification` and `phase-N` plus a Project board. Create
  them only when the owner asks.
- **Follow-ups** are listed at the end of PLAN-phase-0.md. None blocks
  Phase 1. The default-tier time comes first, because the Phase 1 exit
  criterion is 8 minutes at most.
- **One open owner decision.** `tests/test_lane_install_contract.py`
  (60 tests) is marked to be dropped with P1-07, but it also covers the
  install route and identity. Decide whether to port or drop it before
  P1-07.

## What to do next

1. **Get Phase 0 green on GitHub, with the owner.**
   - The owner pushes `main` or opens a pull request from it.
   - Read every job's result, and fix CI-only failures at the root in
     small commits. Never skip a test or weaken a gate to get green.
   - Then the owner cuts `release/0.42.x`, tags `pre-simplification` and
     marks the jobs required.
2. **Plan Phase 1.** Write `PLAN-phase-1.md` in the format of
   PLAN-phase-0.md:
   - test-first steps, exact commands and their expected output;
   - every code block run in a scratch worktree before it is written down.

   Plan in batches (P1-01 to P1-06 first), and get the owner's approval
   before executing a batch. Deletions start only after the
   `pre-simplification` tag exists.
3. **Execute Phase 1 one ROADMAP task per pull request**, in an order that
   respects every **Depends** line, for example:
   P1-01, P1-02, P1-03, P1-04, P1-06, P1-05, P1-10, P1-11, P1-13, P1-12,
   P1-14, P1-08, P1-07, P1-09, P1-15, P1-16, P1-17, P1-18.
4. **Close Phase 1 the way Task 16 closed Phase 0:**
   - full verification;
   - `uv run python scripts/metrics.py --junit <report> --write`;
   - a **Status** line under the phase in ROADMAP.

### Notes for specific Phase 1 tasks

- **P1-01** moves `orchestrator/project_config.py`.
  - Its `_parse_project_config` has 88 lines, so the ratchet reports new
    debt at the new path. Record the reviewed move with
    `uv run python scripts/ratchet.py --update --accept src/forge/project_config.py`.
  - The prose lint counts the moved file as added, so its review
    references must go. One of them is inside a `Field(description=...)`
    string.
  - The ledger already treats the module as moved (`MOVED` in
    `scripts/build_test_ledger.py`).
- **Every deletion task** (P1-02 to P1-07, P1-10 to P1-13, P1-15):
  - The ledger's `deleted_by` column names the test files that the task
    deletes.
  - The `protected` column marks invariant evidence and edge-case pins.
    They never go with the code; port them instead.
  - After deleting, regenerate the ledger and run `ratchet.py --update`:
    shrunk debt must be recorded.
  - Check the register rows whose **Code** column points at the deleted
    code, then run the Invariant Suite.
- **P1-05** turns `adaptive/boundary_registry.py` and
  `tests/test_architecture_boundaries.py` into import-linter contracts.
  `tests/test_import_contract_debt.py` caps the contracts' ignore list
  at 2.
- **P1-07** keeps the program's own scripts (`_codebase.py`, `metrics.py`,
  `ratchet.py`, `prose_lint.py`, `assert_no_skips.py`,
  `build_test_ledger.py`) and needs the owner's decision on
  `test_lane_install_contract.py`.
- **P1-08** rewrites `release.yml` and simplifies CI. Keep every Phase 0
  gate.
- **P1-11** needs `release/0.42.x` to exist. It moves the Azure-only rows
  of the edge-case register to **Retired**.
- **P1-12** runs after P1-11 and P1-13, which delete the last non-legacy
  importers of `forge.agents`. Its task text does not mention `stores/` or
  the legacy models `AgentRun`, `Conversation` and `ReviewState`, which
  lose their last users with `orchestrator/`. Settle their tables with the
  owner; DESIGN §14 allows breaking the lab schema.
- **P1-16** needs `scripts/ast_equivalence.py`, which does not exist yet,
  for the `prose-diet` pull requests.

## How work is done here

### The loop for one task

1. Take one ROADMAP task. Restate its **Goal**, **Work** and **Done when**,
   and check its **Depends**.
2. Work test-first. Write the failing test, or for a deletion the check
   that proves the code is gone and nothing else broke. See it fail, then
   make it pass.
3. Run the gates below and fix every finding. Never weaken a gate.
4. Commit in small, explained steps. Subjects follow the repository's
   style, `area: what changed`, for example `tools:`, `test:`, `docs:`,
   `docs(roadmap):`, `ci:` or `feat(config):`.
5. Fill in the PR template: invariants touched, metrics delta, ADR impact,
   ledger and register rows, size and scope.
6. Get a review, and re-review after fixes. In Phase 0, reviews found real
   defects in nearly every task, including the "simple" ones.
7. The owner merges. Do not push to `main`, merge, tag, release or change
   repository settings unless the owner asks for exactly that.

### The gates

| Gate | Command | Fails when | What to do |
| --- | --- | --- | --- |
| Tests | `set -o pipefail && uv run pytest -q -m "not slow"`, plus `-m slow` in a separate run for lifecycle changes | A test fails, or a child process leaked | Fix the code or the leak |
| Lint | `uv run ruff format . && uv run ruff check .` | Style or lint errors | Fix them |
| Import contracts | `uv run lint-imports` | A forbidden import | Restructure; never add an ignore |
| Size ratchet | `uv run python scripts/ratchet.py` | Listed debt grew, new code exceeds 600 lines per file or 80 per function, or the baseline is stale | Split the code. After shrinking, run `--update`; use `--update --accept <path>` only for a reviewed move |
| Prose lint | `uv run python scripts/prose_lint.py --base origin/main`, after committing | An added line under `src/` carries a review, backlog or issue id | Remove the reference; use `prose-lint: allow` only for a literal that must match |
| Invariant Suite | `uv run pytest -q $(uv run python -m tests.invariants.registry)` | A failure; in CI, also any skip | Fix the code; changing the evidence needs an ADR |
| Diff coverage | See [testing.md](../../reference/testing.md) | Less than 85% of the changed lines are covered | Add tests |

The ledger is not a gate, but its measured columns drift when tests
change. Regenerate it with `uv run python scripts/build_test_ledger.py` and
commit it with the test change.

### Rules that carry the most weight

- ADRs are binding. A change that contradicts one starts with an ADR
  amendment.
- One task is one pull request of about 500 net lines at most; pure
  deletions are exempt.
- Code carries no review, backlog or issue ids; history lives in git and
  ADRs. Documents carry no counts that go stale with the next deletion.
- Deleted code keeps its lessons: the edge-case register and the ledger
  decide what may go (AGENTS.md rule 9).

## Gotchas learned during Phase 0

These are not obvious from AGENTS.md:

- **`uv run` hides `PATH` bugs.** It puts `.venv/bin` first on `PATH`, so a
  test that shells out to `python` passes under `uv run` and fails
  elsewhere. To reproduce such a bug, run
  `env -u VIRTUAL_ENV PATH="$(dirname "$(command -v uv)"):/usr/bin:/bin" .venv/bin/python -m pytest <test>`.
  Some agent shells have no `python` at all; use `uv run python` or
  `.venv/bin/python`.
- **A symlink to the venv's `python` does not activate the venv**, because
  `pyvenv.cfg` is looked up next to the invoked path. Use a small exec
  wrapper, as the `lane_runner` fixture in
  `tests/test_lane_install_contract.py` does.
- **macOS process quirks.**
  - `os.kill(pid, 0)` succeeds for a zombie.
  - `pgrep -P` does not list zombies.
  - There is no `timeout` command.
- **pytest.**
  - `-m` keeps only its last value, so `-m "not slow" -m slow` runs only
    the slow tier.
  - The leak check is a `trylast` `pytest_sessionfinish` hook, so it runs
    after fixture teardown; keep it that way.
- **Timing under load.** While the full suite runs, or another agent
  works, starting a subprocess can take more than a second. One default
  tier took 774 s instead of 543 s for that reason. Avoid tight time
  windows in tests, and measure suite time on an idle machine.
- **import-linter** reads `.importlinter` and `setup.cfg` before
  `pyproject.toml`. Keep its configuration in `pyproject.toml` only.
- **GitHub Actions.**
  - `github.event.pull_request.base.sha` can be stale, so the prose lint
    diffs a pull request against `HEAD^1` of the merge commit.
  - On a push, `github.event.before` is all zeros for a new branch, and the
    lint skips.
- **The ratchet's `--init`** would forgive all existing debt. It refuses to
  run without `--force`; do not use it on this repository.
- **`scripts/_codebase.py` classifies modules by reachability.** It decides
  what is runtime, tooling-only or test-only from three sets of roots:
  - the control plane: `forge.main`, `forge.worker` and `forge.migrate`;
  - the lane entry points;
  - the console scripts, which count as tooling.

  A module that only tests import is test-only; Phase 1 deletes those.
  Package re-exports are followed; star imports are not.
- **Plans with embedded code.**
  - While a plan is being executed, keep its code blocks byte-identical to
    the committed files.
  - Embedded diffs must keep their single-space context lines, or they
    stop applying.
  - Once a phase closes, its plan becomes a record. PLAN-phase-0.md
    matches the code at `bf06fd6` and is not synced after that.
- **`ruff format` rewrites files**, so a patch that matches exact old text
  can miss after formatting. Re-read a file before patching it.
- **Long runs.**
  - The default tier takes about 9 minutes; run it in the background.
  - The slow tier takes about 7.5 minutes and needs `redis-server`.
  - The default tier's skips are expected: mostly Postgres proofs gated on
    `FORGE_PG_TEST_URL`, plus a few lab-only tests.
- **Leftover processes** from older runs: list them with
  `pgrep -lf "tests/production_entry/"` on macOS, or
  `pgrep -af "tests/production_entry/"` on Linux, then `kill <pid>` each
  one.

## Working with the owner

- The owner writes in Russian or English; answer in the language of the
  message. Repository documents stay in English.
- The owner runs agents with little supervision and expects routine
  matters to be decided, with the assumptions stated.
- Bring real decisions to the owner: scope, product behaviour, and
  anything irreversible or outside the repository, such as pushes, tags,
  releases, repository settings and issues.
- Designs are approved before code: ADR-0035 was accepted before any
  Phase 0 code was written.

## Kickoff prompt

Paste this to the next agent, in a checkout of the repository:

```text
You are an AI coding agent continuing the forge simplification program
(ADR-0035) in this repository. Before doing anything, read, in order:
AGENTS.md; docs/roadmap/2026-09-29-simplification/HANDOFF.md;
docs/adr/0035-simplification-program.md; DESIGN.md §1, §4 to §8 and §14;
ROADMAP.md (the Phase 0 status line and all of Phase 1).

Then:
1. Set up and verify the environment with .claude/skills/forge-setup/SKILL.md:
   the default test tier, lint and the guardrails. Report the results.
2. Find out whether main has been pushed and whether CI is green on GitHub
   (git log origin/main..main; gh run list). If a job fails, find the root
   cause and propose the fix as small commits on a branch. Never weaken a
   gate.
3. Write docs/roadmap/2026-09-29-simplification/PLAN-phase-1.md for P1-01
   to P1-06 in the format of PLAN-phase-0.md, running every code block in
   a scratch worktree first. Ask the owner to approve it; execute only
   after the pre-simplification tag exists.

Rules: one ROADMAP task per pull request; run the gates before every
commit; never push to main, merge, tag or change GitHub settings unless the
owner asks for exactly that; keep the Invariant Suite, the edge-case
register and the ledger intact (AGENTS.md rules 8 and 9). Answer the owner
in the language of the message.
```
