# Handoff: the forge simplification program

This page is for the next AI coding agent, or engineer, who picks up the
simplification program. It says where the work stands, what to do next, how
work is done here, and what was learned the hard way. [AGENTS.md](../../../AGENTS.md)
stays the authoritative guide; read it first.

This page is a snapshot from 2026-10-11: the **State** section, the next
steps, the task notes and the kickoff prompt describe that day. The live
status is the **Status** line of each phase (and of each finished task) in
[ROADMAP.md](ROADMAP.md) and the measured history in [metrics.md](metrics.md).
Keep those current as work lands, not this page.

## Read this first

In this order (about an hour):

1. [AGENTS.md](../../../AGENTS.md): rules 1–9, the repo map, the commands
   and the gotchas.
2. [ADR-0035](../../adr/0035-simplification-program.md): why the program
   exists and what it changes.
3. [DESIGN.md](DESIGN.md): §1 summary, §4 invariants I1–I8, §5 scope for
   1.0, §6 target architecture, §7 test strategy, §8 guardrails, §10
   migration and rollback, and §14, the owner's decisions.
4. [ROADMAP.md](ROADMAP.md): the Definition of Done at the top, the Phase 0
   status line, all of Phase 1, and Tracking at the end.
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
| 0 | Guardrails and baseline | Code complete; the checks that need GitHub or the owner are open |
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
- Data lives only in the lab and dogfooding, so the schema may break. Even
  so, Phase 1 keeps the schema unchanged, so that its release rolls back by
  redeploying 0.42.0 (DESIGN §10).
- Documents live in the repository, in English.

## State on 2026-10-11

### What Phase 0 delivered

All 16 tasks of [PLAN-phase-0.md](PLAN-phase-0.md) were executed, so the
code of ROADMAP P0-01 to P0-14 is in place. The **Done when** checks that
need GitHub or the owner are still open (see "What is not done").

| Guardrail | Where it lives | Enforced by |
| --- | --- | --- |
| Test tiers (`slow` and the default tier) | `tests/_hygiene.py`, `tests/conftest.py` | `test` and `tests-slow` CI jobs |
| Leaked-process check and orphan guard | `pytest_sessionfinish` in `tests/conftest.py`, `tests/production_entry/orphan_guard.py` | Every pytest run, except inside `scripts/pg_gate.py` (a known gap) |
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
- the slow tier passed (152 passed, 39 skipped);
- every local gate was green, and no process leaked.

The history reads well: `git log --oneline 7ed64ba..bf06fd6` shows the
planning documents, then the commits of each task, interleaved with
`docs(roadmap): sync ...` commits that kept the plan equal to the code.

### What is not done

- **GitHub has not seen the work.** It was merged into the owner's local
  `main` and had not been pushed. In the owner's clone, check with
  `git fetch origin && git log --oneline origin/main..main`. Until `main`
  is pushed, a fresh clone from GitHub has none of this work, this page
  included.
- **The new CI jobs have never run on GitHub.** The first pull request and
  the first push to `main` are their first runs, so expect CI-only failures
  on Linux, on Python 3.14 and with Postgres. `TestRealPostgresRaces` in
  `tests/test_budget_amendment.py` runs in CI for the first time, in the
  `invariants` job.
- **The nightly run on GitHub was already red.** Every scheduled `ci.yml`
  run on `7ed64ba` from 2026-09-30 to 2026-10-10 failed. `integration-os`
  and `release-canary` fail every night (maintenance-0.42.md notes both),
  and some nights `integration` or `test (3.14)` failed too. The push run
  on `7ed64ba` was green. Compare a failure with these before blaming
  Phase 0.
- **Phase 0 Done when checks that need GitHub** (ROADMAP):
  - the lane-install tests pass on Linux CI (P0-03);
  - the default tier takes at most 8 minutes on CI, and `tests-slow` is
    green on `main` (P0-05);
  - `invariants` is required, with a local mutation per registered file
    noted in the pull request (P0-06);
  - CI enforces the import contracts (P0-07);
  - a synthetic growth pull request fails the ratchet (P0-08);
  - a synthetic pull request with an `R42-16` comment fails the prose lint
    (P0-09).
- **Owner-only steps** (the Phase 0 status line in ROADMAP):
  - push `release/0.42.x` as
    [maintenance-0.42.md](../../operations/maintenance-0.42.md) describes;
  - tag `pre-simplification` on `main` before any Phase 1 deletion lands;
  - review `test-ledger.csv`. P0-13 is done only when the owner has
    reviewed every `drop-*` row and every row with a `review` value. The
    curated columns `disposition`, `phase` and `note` belong to the owner,
    and regeneration keeps them;
  - make `invariants` and `coverage` required once they have run green on
    a pull request. `lint` (which now runs the import contracts, the
    ratchet and the prose lint), `typecheck`, both `test` jobs and
    `integration` are required already.
- **Tracking does not exist yet.** ROADMAP "Tracking" plans issues
  labelled `simplification` and `phase-N` plus a Project board. Create
  them only when the owner asks.
- **Follow-ups** are listed at the end of PLAN-phase-0.md. None blocks
  Phase 1. The default-tier time comes first, because the Phase 1 exit
  criterion is 8 minutes at most.
- **Open owner decisions** besides the ledger review:
  - `tests/test_lane_install_contract.py` (60 tests) is marked to be
    dropped with P1-07, but it also covers the install route and identity.
    Decide whether to port or drop it before P1-07.
  - The legacy tables that P1-12 leaves unused (see the task notes).

## What to do next

1. **Get Phase 0 green on GitHub, with the owner.**
   - No single event runs every new job: `coverage` runs only on pull
     requests, and `tests-slow` never does. The owner pushes the work to a
     branch and opens a pull request into `main`, and runs
     `gh workflow run ci.yml --ref <branch>` for `tests-slow`. A dispatch
     also runs `integration-os` and `release-canary`, which were already
     red.
   - Read every job's result, and fix CI-only failures at the root in
     small commits. Never skip a test or weaken a gate to get green.
   - `main` requires a linear history, so GitHub can only squash or rebase
     the pull request. Both give new SHAs to the commits that ROADMAP,
     metrics.md, metrics-history.json and PLAN-phase-0.md cite, and a
     squash also loses the trigger commit that maintenance-0.42.md
     cherry-picks. The owner fast-forwards `main` instead
     (`git push origin <branch>:main`).
   - Then the owner cuts `release/0.42.x`, tags `pre-simplification`, makes
     the new jobs required and reviews the ledger.
2. **Plan Phase 1.** Write `PLAN-phase-1.md` in the format of
   PLAN-phase-0.md:
   - test-first steps, exact commands and their expected output;
   - every code block run in a scratch worktree before it is written down.

   Plan in batches (P1-01 to P1-06 first), and get the owner's approval
   before executing a batch. Execute a task only when its **Depends** are
   done by their **Done when**:
   - P1-01 and P1-10 need all of Phase 0;
   - P1-02 needs the owner's ledger review (P0-13);
   - P1-05 needs CI enforcing the contracts (P0-07).

   Deletions start only after the `pre-simplification` tag exists.
3. **Execute Phase 1 one ROADMAP task per pull request** (P1-03 and P1-16
   take several, as their Work says), in an order that respects every
   **Depends** line, for example:
   P1-01, P1-02, P1-03, P1-04, P1-06, P1-05, P1-10, P1-11, P1-13, P1-12,
   P1-14, P1-08, P1-07, P1-09, P1-15, P1-16, P1-17, P1-18.
4. **Record each finished task where the next agent looks.** In its pull
   request, add `- **Status:** done on <date>` under the task in
   ROADMAP.md, unless the owner has asked for the tracking issues. The next
   task is the first one in the order above without that line.
5. **Close Phase 1 only when every Phase 1 exit criterion holds** (ROADMAP
   "Tracking"):
   - full verification;
   - `.venv/bin/python -m pytest -q -m "not slow" --junitxml=<report>`,
     then `uv run python scripts/metrics.py --junit <report> --write`;
   - a **Status** line under the phase in ROADMAP.

   Task 16 of PLAN-phase-0.md shows the steps. It left Phase 0 code
   complete with its GitHub and owner checks open, not closed.

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
  - Before deleting, register the gotchas the code carries (AGENTS.md
    rule 9).
  - The ledger rows whose `deleted_by` names the task are the test files
    that import code the task deletes; the column does not say that a file
    goes.
  - Delete a test file only when its `disposition` is `drop-with-code` or
    `drop-obsolete`. A `keep` or `port-*` row whose `review` says
    `partly deleted` stays: remove only what depends on the deleted code,
    or ask the owner to change its disposition.
  - The `protected` column marks the Invariant Suite, invariant evidence
    and edge-case pins. They never go with the code; port them instead.
  - `disposition`, `phase` and `note` belong to the owner. Propose
    changes; do not make them.
  - After deleting, regenerate the ledger and run `ratchet.py --update`:
    shrunk debt must be recorded.
  - Check the register rows whose **Code** column points at the deleted
    code, then run the Invariant Suite.
- **P1-02 and P1-05 touch test files the ledger keeps.**
  - `tests/test_saga_durable.py` (`keep`) and `tests/test_saga_native.py`
    (`port-adapter`) import modules that P1-02 deletes.
  - P1-05's Work deletes `tests/test_architecture_boundaries.py`, which
    the ledger marks `keep`.

  The owner's ledger review settles these rows first.
- **P1-05** turns `adaptive/boundary_registry.py` and
  `tests/test_architecture_boundaries.py` into import-linter contracts.
  `tests/test_import_contract_debt.py` caps the contracts' ignore list
  at 2.
- **P1-07**:
  - keeps the program's own scripts (`_codebase.py`, `metrics.py`,
    `ratchet.py`, `prose_lint.py`, `assert_no_skips.py`,
    `build_test_ledger.py`);
  - deletes `generate_template_pins.py` and `gate_conformance.py`, which
    two `lint` steps run, so those steps go in the same change;
  - needs the owner's decision on `test_lane_install_contract.py`.
- **P1-08** rewrites `release.yml` and simplifies CI. Keep every Phase 0
  gate.
- **P1-11** needs `release/0.42.x` to exist. The register has no Azure rows
  yet. Register the gotchas of the Azure code first (AGENTS.md rule 9),
  then move the Azure-only rows to **Retired** with the reason.
- **P1-12** runs after P1-11 and P1-13, which delete the last non-legacy
  importers of `forge.agents`. Its task text does not mention `stores/` or
  the legacy models `AgentRun`, `Conversation` and `ReviewState`, which
  lose their last users with `orchestrator/`. Settle their tables with the
  owner:
  - DESIGN §14 allows breaking the lab schema;
  - DESIGN §10 keeps the schema unchanged in Phase 1, so that 0.43.0 rolls
    back by redeploying 0.42.0;
  - §6.10 drops the legacy tables only after P3-20.
- **P1-16** needs `scripts/ast_equivalence.py`, which does not exist yet,
  for the `prose-diet` pull requests.

## How work is done here

### The loop for one task

1. Take one ROADMAP task. Restate its **Goal**, **Work** and **Done when**,
   and check its **Depends**.
2. Work test-first. Write the failing test, or for a deletion the check
   that proves the code is gone and nothing else broke. See it fail, then
   make it pass.
3. Run the gates below and fix every finding; never weaken a gate. A
   lifecycle change (for example P1-10 to P1-13) is also done only after a
   live-lab run: `/implement` → branch → green CI → Draft MR →
   `ready_for_human`. The lab follows `.claude/skills/forge-lab/SKILL.md`
   and needs secrets from the owner.
4. Commit in small, explained steps. Subjects follow the repository's
   style, `area: what changed`, for example `tools:`, `test:`, `docs:`,
   `docs(roadmap):`, `ci:` or `feat(config):`. Run the prose lint right
   after committing; it reads commits.
5. Fill in the PR template: invariants touched, metrics delta, ADR impact,
   ledger and register rows, size and scope.
6. Get a review, and re-review after fixes. In Phase 0, reviews found real
   defects in nearly every task, including the "simple" ones.
7. The owner merges. Do not push to `main`, merge, tag, release or change
   repository settings unless the owner asks for exactly that. Pushing a
   branch and opening a pull request are owner decisions too, until the
   owner says otherwise.
8. Record the task as done (see "What to do next", step 4).

### The gates

| Gate | Command | Fails when | What to do |
| --- | --- | --- | --- |
| Tests | `set -o pipefail && .venv/bin/python -m pytest -q -m "not slow"`, plus `.venv/bin/python -m pytest -q -m slow` in a separate run for lifecycle changes | A test fails, or a child process leaked | Fix the code or the leak |
| Lint | `uv run ruff format . && uv run ruff check .` | Style or lint errors | Fix them |
| Types | `uv run mypy src/forge/durable src/forge/runs src/forge/repository src/forge/factory src/forge/execution src/forge/integrations` | A type error in the core packages (the required `typecheck` job) | Fix the types |
| Import contracts | `uv run lint-imports` | A forbidden import | Restructure; never add an ignore |
| Size ratchet | `uv run python scripts/ratchet.py` | Listed debt grew, new code exceeds 600 lines per file or 80 per function, or the baseline is stale | Split the code. After shrinking, run `--update`; use `--update --accept <path>` only for a reviewed move |
| Prose lint | `uv run python scripts/prose_lint.py --base origin/main`, after committing | An added line under `src/` carries a review, backlog or issue id | Remove the reference; use `prose-lint: allow` only for a literal that must match |
| Invariant Suite | `.venv/bin/python -m pytest -q $(uv run python -m tests.invariants.registry)` | A failure; in CI, also any skip | Fix the code; removing or skipping evidence needs an ADR (AGENTS.md rule 8) |
| Diff coverage | See [testing.md](../../reference/testing.md) | Less than 85% of the changed lines are covered | Add tests |
| The rest of the `lint` job | `uv run python scripts/gate_public_artifacts.py`, `uv run python scripts/generate_template_pins.py --exact-composition` and `uv run python scripts/gate_conformance.py --report <a file outside the repository>` | A tracked database archive, a release-composition mismatch, or a lane template that fails the conformance checks | Fix the cause. P1-07 retires the last two |

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

These are not obvious from AGENTS.md. Add the lasting ones to the
operational table of the edge-case register, as AGENTS.md asks for every
new gotcha.

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
- **Process quirks.**
  - `os.kill(pid, 0)` succeeds for a zombie, on Linux too.
  - macOS `pgrep -P` does not list zombies.
  - macOS has no `timeout` command.
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
- **The ratchet's `--init`** would record every current size as the
  baseline, forgiving all growth and all new oversized code. It refuses to
  overwrite the baseline without `--force`; do not use it on this
  repository.
- **`scripts/_codebase.py` classifies modules by reachability** in a static
  import graph, lazy imports and literal `import_module` calls included:
  - runtime: reachable from the control plane (`forge.main`,
    `forge.worker`, `forge.migrate`) or from the lane entry points
    (`forge.harness_entry`, `forge.lane_driver`,
    `forge.candidate_collector`, and later `forge_lane`);
  - tooling-only: reachable, outside runtime, from `forge.doctor`, the
    release modules, the console scripts, or a module that `scripts/*.py`
    or `tools/**/*.py` imports;
  - test-only: reachable only from `tests/`. Phase 1 deletes those.

  Deleting a script can turn the modules that only it imported into
  test-only ones. Package re-exports are followed; star imports are not.
- **Plans with embedded code.**
  - While a plan is being executed, keep its code blocks byte-identical to
    the committed files.
  - Embedded diffs must keep their single-space context lines, or they
    stop applying.
  - Once a phase's code is complete, its plan becomes a record.
    PLAN-phase-0.md matches the code at `bf06fd6` and is not synced after
    that.
- **`ruff format` rewrites files**, so a patch that matches exact old text
  can miss after formatting. Re-read a file before patching it.
- **Long runs.**
  - The default tier takes about 9 minutes; run it in the background.
  - The slow tier takes about 7.5 minutes and needs `redis-server`.
  - The default tier's skips are expected: mostly Postgres proofs gated on
    `FORGE_PG_TEST_URL`, plus a few lab-only tests.
- **Leftover processes** from older runs: when no test run is in progress
  in this checkout, list them from the repository root with
  `pgrep -lf "$PWD/tests/production_entry/"` on macOS, or
  `pgrep -af "$PWD/tests/production_entry/"` on Linux, then `kill <pid>`
  each one. Without `$PWD`, the pattern also matches other checkouts, such
  as another agent's running suite.

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

Paste this to the next agent, in the owner's clone or a worktree of it, on
a branch that contains this page. Until `main` is pushed, a clone from
GitHub has none of this work.

```text
You are an AI coding agent continuing the forge simplification program
(ADR-0035) in this repository. Before doing anything, read AGENTS.md, then
docs/roadmap/2026-09-29-simplification/HANDOFF.md, then everything in its
"Read this first" list, in that order.

Then:
1. Set up and verify the environment with .claude/skills/forge-setup/SKILL.md:
   the default test tier, lint and the guardrails. Report the results.
2. Find out whether main has been pushed and whether CI is green on GitHub
   (git fetch origin; git log origin/main..main; gh run list). Compare
   failures with the nightly failures that HANDOFF.md lists. If a job fails
   because of this work, find the root cause and propose the fix as small
   commits on a branch. Never weaken a gate.
3. Write docs/roadmap/2026-09-29-simplification/PLAN-phase-1.md for P1-01
   to P1-06 in the format of PLAN-phase-0.md, running every code block in
   a scratch worktree first. Ask the owner to approve it. Execute a task
   only when its Depends lines are met; for this batch that means Phase 0
   green and required on GitHub, the pre-simplification tag, and the
   owner's ledger review.

Rules: one ROADMAP task per pull request; run the gates before every
commit, and the prose lint right after it; never push, merge, tag or change
GitHub settings unless the owner asks for exactly that; keep the Invariant
Suite, the edge-case register and the ledger intact (AGENTS.md rules 8 and
9). Answer the owner in the language of the message.
```
