# Phase 0 — Guardrails and Baseline: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install the guardrails of the simplification program (ADR-0035,
ROADMAP Phase 0): hermetic test tiers, the Invariant Suite, import
contracts, a size ratchet, a prose lint, diff coverage, a doctor that never
raises, the edge-case register, the test disposition ledger and the
measured baseline. The product's behavior does not change.

**Architecture:**

- Stdlib-only scripts under `scripts/` share one static-analysis module,
  `scripts/_codebase.py`.
- Test-suite hygiene lives in `tests/_hygiene.py` and is wired through
  `tests/conftest.py`.
- The Invariant Suite registry is `tests/invariants/registry.py`.
- Each guardrail becomes a CI step or job in `.github/workflows/ci.yml`.
- The only production-code changes: GitLab settings become optional behind
  `FORGE_GITLAB_ENABLED`, GitLab ingress fails closed when unconfigured, and
  `forge doctor` reports settings errors instead of raising.

**Tech Stack:** Python 3.13, uv, pytest 9, pydantic-settings, import-linter
2.1, pytest-cov 7.1.0, diff-cover 10.6.0, GitHub Actions.

**Provenance:** every code block below was implemented and run in a scratch
worktree of `main` at `330b200`, where the new tests, the whole default tier
and the slow tier passed. Where a step says "Expected", that is the observed
output.

---

## Before you start

- Work from the repository root on a feature branch. The environment is
  `uv sync` (Python 3.13); run commands with `uv run`.
- Use a bash-compatible shell. In zsh, run `setopt interactivecomments`
  first so that `#` comments in the command blocks work.
- Use `set -o pipefail` when piping test output.
- Every commit carries the trailer
  `Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>`.
- **Do the tasks in order.** Two ordering constraints matter:
  - Task 7 changes `src/forge/config.py` and `src/forge/doctor.py`.
    Task 10 then records the size baseline, so those changes are already
    inside it.
  - Task 6 builds on the tier hook from Task 5.
- Never weaken an existing test to make a step pass. If a step's expected
  output differs, stop and report the difference.

## File structure

| Path | Kind | Responsibility |
| --- | --- | --- |
| `tests/test_adr_status_banners.py` | new test | ADRs affected by ADR-0035 carry a banner |
| `docs/adr/00{02,13,15,19,20,21,22,23,24,25,27,28,29,30,31,32,33,34}-*.md` | modified docs | Banner line under the title |
| `docs/operations/maintenance-0.42.md` | new doc | The `release/0.42.x` policy |
| `docs/operations/README.md` | modified doc | Lists the maintenance runbook |
| `tests/test_lane_install_contract.py` | modified test | Hermetic `python` for the install fragment |
| `tests/production_entry/orphan_guard.py` | new test helper | Exit helper processes whose parent died |
| `tests/production_entry/{durable_ingress_server,fake_native_server,fake_vendor,production_matrix_worker}.py` | modified test helpers | Install the orphan guard |
| `tests/test_orphan_guard.py` | new test | The guard works across a real SIGKILL |
| `tests/_hygiene.py`, `tests/test_hygiene.py` | new | Tier markers by path; leaked-process check |
| `tests/conftest.py` | modified | Collection hook and session-finish hook |
| `docs/reference/testing.md` | new doc | Tiers and subprocess hygiene |
| `tests/invariants/{__init__,registry,test_registry_integrity,test_i1_no_merge}.py` | new | The Invariant Suite |
| `tests/_scripts.py` | new test helper | Import `scripts/*.py` in tests |
| `scripts/assert_no_skips.py`, `tests/test_script_assert_no_skips.py` | new | An invariant run must not skip |
| `src/forge/config.py`, `src/forge/gateway/validator.py`, `src/forge/doctor.py`, `.env.example` | modified | Optional GitLab; fail-closed ingress; doctor never raises |
| `tests/test_settings_gitlab_optional.py`, `tests/test_doctor_settings.py` | new tests | The above |
| `pyproject.toml`, `uv.lock` | modified | Markers, dev deps, import-linter contracts |
| `scripts/_codebase.py`, `scripts/metrics.py`, `tests/test_script_metrics.py` | new | Shared analysis; metrics |
| `docs/roadmap/2026-09-29-simplification/{metrics.md,metrics-history.json}` | generated | Measured history |
| `scripts/ratchet.py`, `scripts/ratchet-baseline.json`, `tests/test_script_ratchet.py` | new | Size ratchet |
| `scripts/prose_lint.py`, `tests/test_script_prose_lint.py` | new | Prose lint |
| `tests/_nodeids.py`, `docs/reference/edge-cases.md`, `tests/test_edge_case_register.py` | new | Edge-case register |
| `scripts/build_test_ledger.py`, `tests/test_script_build_test_ledger.py`, `…/test-ledger.csv` | new | Test disposition ledger |
| `.github/pull_request_template.md`, `AGENTS.md`, `.claude/skills/forge-setup/SKILL.md` | new/modified | Working agreement |
| `.github/workflows/ci.yml` | modified | Trigger, tiers, invariants, imports, ratchet, prose lint, coverage |

---

### Task 1: ADR status banners (ROADMAP P0-01)

**Files:**
- Create: `tests/test_adr_status_banners.py`
- Modify: the 18 ADRs listed in the test (one banner line under the title)

- [ ] **Step 1: Write the failing test**

```python
"""Every ADR that ADR-0035 supersedes or amends says so at the top."""

from __future__ import annotations

from pathlib import Path

import pytest

ADR_DIR = Path(__file__).resolve().parents[1] / "docs" / "adr"
LINK = "[ADR-0035](0035-simplification-program.md)"
EXPECTED = {
    "Superseded by": ("0019", "0021", "0027", "0029", "0030", "0031", "0033", "0034"),
    "Amended by": ("0002", "0013", "0015", "0020", "0022", "0023", "0025", "0028", "0032"),
    "Superseded on main by": ("0024",),
}
CASES = [(verb, number) for verb, numbers in EXPECTED.items() for number in numbers]


def _adr(number: str) -> Path:
    (path,) = ADR_DIR.glob(f"{number}-*.md")
    return path


@pytest.mark.parametrize(("verb", "number"), CASES, ids=[n for _, n in CASES])
def test_affected_adr_carries_its_banner(verb: str, number: str):
    head = _adr(number).read_text(encoding="utf-8").splitlines()[:4]
    assert any(line.startswith(f"> **{verb} {LINK}** (2026-09-29).") for line in head)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest -q tests/test_adr_status_banners.py`
Expected: `18 failed`.

- [ ] **Step 3: Insert the banners**

Save this as `/tmp/adr_banners.py` and run `uv run python /tmp/adr_banners.py`
from the repository root:

```python
from pathlib import Path

link = "[ADR-0035](0035-simplification-program.md)"
groups = {
    "Superseded by": ("0019", "0021", "0027", "0029", "0030", "0031", "0033", "0034"),
    "Amended by": ("0002", "0013", "0015", "0020", "0022", "0023", "0025", "0028", "0032"),
    "Superseded on main by": ("0024",),
}
for verb, numbers in groups.items():
    for number in numbers:
        (path,) = Path("docs/adr").glob(f"{number}-*.md")
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        assert lines[0].startswith("# "), path
        banner = f"> **{verb} {link}** (2026-09-29).\n"
        if verb == "Superseded on main by":
            banner = f"> **{verb} {link}** (2026-09-29). It remains valid on `release/0.42.x`.\n"
        lines[1:1] = ["\n", banner]
        path.write_text("".join(lines), encoding="utf-8")
```

- [ ] **Step 4: Run the test again**

Run: `uv run pytest -q tests/test_adr_status_banners.py`
Expected: `18 passed`. Check one file: `head -4 docs/adr/0024-azure-devops-adapter.md`
shows the title, a blank line and the banner.

- [ ] **Step 5: Commit**

```bash
git add tests/test_adr_status_banners.py docs/adr
git commit -m "docs(adr): mark the ADRs superseded or amended by ADR-0035" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 2: Maintenance line for 0.42.x (ROADMAP P0-02)

**Files:**
- Create: `docs/operations/maintenance-0.42.md`
- Modify: `.github/workflows/ci.yml` (push trigger), `docs/operations/README.md` (runbook table row)

- [ ] **Step 1: Write the policy document**

````markdown
# Maintenance line 0.42.x

ADR-0035 moves Azure DevOps and the pre-simplification architecture to a
maintenance line while `main` is simplified.

| Item | Value |
| --- | --- |
| Branch | `release/0.42.x`, cut from `7ed64ba`: the `v0.42.0` tag plus its release evidence and lane pins |
| Recovery tag | `pre-simplification`: the last `main` commit before the Phase 1 deletions. It is created at the Phase 0 exit. |
| Scope | Security fixes only; no features |
| Azure DevOps | Supported only on this line once it leaves `main` (ROADMAP P1-11), until the post-1.0 port adapter lands |
| CI | `ci.yml` runs on pushes to `release/**` and on pull requests against it. The schedule-only jobs (`integration-os`, `release-canary`) do not run here. They are advisory, not promotion-required, and were already failing at the branch point. |
| Releases | None planned. The line exists for security fixes, in case an installation still depends on it. |

## Creating the branch (owner, after Phase 0 merges)

GitHub reads a workflow's triggers from the pushed commit, so the branch
needs the `release/**` trigger itself. Cherry-pick only the trigger commit,
because later `ci.yml` changes on `main` depend on files the branch does
not have. Then take the current version of this policy:

```bash
git fetch origin --tags
git switch -c release/0.42.x 7ed64ba
git cherry-pick -x <main sha of "docs(ops): the 0.42.x maintenance line; CI runs on release branches">
git checkout origin/main -- docs/operations/maintenance-0.42.md
git commit -m "docs(ops): the current maintenance policy"
git push -u origin release/0.42.x
```

Expected: the first push run of `ci.yml` on the branch is green. The push
run on `7ed64ba` was green.

## Backporting a fix

```bash
git fetch origin
git switch -c backport/<topic> origin/release/0.42.x
git cherry-pick -x <sha>       # or commit the fix directly when the code is already gone from main
gh pr create --base release/0.42.x
```

## If a 0.42.N release becomes necessary

The branch keeps the 0.42 release machinery, and it is documented on the
branch:

- **The promotion gate and evidence:** `docs/releases/README.md`.
- **Before the release:** the version bump (`src/forge/__init__.py` and
  `pyproject.toml`), the qualification record and the wheel receipt that
  the exact-composition gate requires. See `docs/releases/profile-records.md`
  and `docs/operations/supported-profile-runbook.md` §9.
- **After the release:** commit `docs/releases/evidence/v0.42.N/` and
  regenerate the lane pins with `scripts/generate_template_pins.py`, as
  `7ed64ba` did for 0.42.0.

Tag only a commit whose push run is green, and do not dispatch extra runs
on that commit. The promotion gate reads the latest attempt of each
required check on that SHA.

Known follow-ups on the branch before any 0.42.N release after 0.43.0 ships:

- `release.yml` moves `ghcr.io/forcewake/forge:latest` and canaries an
  upgrade from it. Change it to tag only `:0.42.N` and to canary against
  the previous `0.42.x` image.
- `release.yml` creates the GitHub Release without `--latest=false`.
- The `release-canary` job in `ci.yml` hard-codes
  `--previous-image ghcr.io/forcewake/forge:latest`.
- `release-canary` and `integration-os` were already failing on the nightly
  schedule at the branch point. Repair them before relying on them.

## Recovering removed code

Once the `pre-simplification` tag exists, code that is later removed from
`main` can be read or restored from it:

```bash
git fetch origin tag pre-simplification
git show pre-simplification:src/forge/runs/azure_service.py
git restore --source=pre-simplification -- src/forge/runs/azure_service.py
```

Fixes made on `main` after the tag exist only in `main`'s history.
````

- [ ] **Step 1b: List the runbook**

In `docs/operations/README.md`, add a row to the runbook table directly after the `[Upgrade](upgrade.md)` row:

```markdown
| [Maintenance line 0.42.x](maintenance-0.42.md) | ADR-0035: the release/0.42.x branch (Azure DevOps, pre-simplification architecture), creating it, backports, 0.42.N releases, recovering removed code | Written |
```

- [ ] **Step 2: Run CI on `release/**` pushes**

In `.github/workflows/ci.yml`, replace the `on.push` block so that it reads:

```yaml
on:
  push:
    branches: [main, "release/**"]
```

- [ ] **Step 3: Validate the workflow file**

Run: `uv run python -c "import yaml; d = yaml.safe_load(open('.github/workflows/ci.yml')); print(d[True]['push'])"`
Expected: `{'branches': ['main', 'release/**']}`

- [ ] **Step 4: Commit**

```bash
git add docs/operations/maintenance-0.42.md .github/workflows/ci.yml docs/operations/README.md
git commit -m "docs(ops): the 0.42.x maintenance line; CI runs on release branches" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

- [ ] **Step 5: Create the branch (the owner, with push rights, after Phase 0 merges)**

```bash
git fetch origin --tags
git switch -c release/0.42.x 7ed64ba
git cherry-pick -x <main sha of "docs(ops): the 0.42.x maintenance line; CI runs on release branches">
git checkout origin/main -- docs/operations/maintenance-0.42.md
git commit -m "docs(ops): the current maintenance policy"
git push -u origin release/0.42.x
```

Expected: the first push run of `ci.yml` on the branch is green (the push run on `7ed64ba` was green). The `pre-simplification` tag is created in Task 16, after Phase 0 merges.

### Task 3: Hermetic lane-install tests (ROADMAP P0-03)

**Files:**
- Modify: `tests/test_lane_install_contract.py` (the `lane_runner` fixture)

The install fragment calls `python`, so the tests depend on the caller's
`PATH`:

- Under `uv run pytest`, `.venv/bin` is on `PATH`, so `python` is the
  project interpreter, not the fixture's seeded one. The tests pass, but not
  hermetically.
- Under `.venv/bin/python -m pytest` (the AGENTS.md command) on hosts that
  ship only `python3` (macOS), 8 tests fail with `python: command not found`.

The fixture already seeds a venv interpreter; expose it as `python` on the
stub `PATH` through a small exec wrapper. A symlink would not work: Python
finds the venv's `pyvenv.cfg` from the path it is started by, so a symlinked
`python` runs the base interpreter, with host and user site-packages.

- [ ] **Step 1: Reproduce the failure without the venv on `PATH` (macOS; on Linux, go to Step 2)**

The fixture needs `uv` to seed its interpreter, so `uv`'s directory stays on `PATH`.

Run: `env -u VIRTUAL_ENV PATH="$(dirname "$(command -v uv)"):/usr/bin:/bin" .venv/bin/python -m pytest -q tests/test_lane_install_contract.py -k RealBash`
Expected on macOS: `8 failed, 15 passed` with
`bash: line 145: python: command not found` or
`FORGE_LANE_INSTALL_IDENTITY_MISMATCH … '<not importable>'`.

- [ ] **Step 2: Apply the fix**

```diff
diff --git a/tests/test_lane_install_contract.py b/tests/test_lane_install_contract.py
index 7b15d94..ad6708c 100644
--- a/tests/test_lane_install_contract.py
+++ b/tests/test_lane_install_contract.py
@@ -661,6 +661,11 @@ def lane_runner(tmp_path_factory):
     (stub / "pip").write_text("#!/bin/sh\nexit 0\n")
     (stub / "pip").chmod((stub / "pip").stat().st_mode | stat.S_IEXEC)
     python = root / "venv" / "bin" / "python"
+    # The fragment calls ``python``; hosts that only ship ``python3`` (macOS)
+    # must still resolve it — to the seeded venv, never the host's. A wrapper,
+    # not a symlink: Python finds the venv's pyvenv.cfg from the path it runs.
+    (stub / "python").write_text(f'#!/bin/sh\nexec "{python}" "$@"\n')
+    (stub / "python").chmod((stub / "python").stat().st_mode | stat.S_IEXEC)
     return {"STUB": str(stub), "PYTHON": str(python)}
```

- [ ] **Step 3: Run the file, with and without the venv on `PATH`**

Run: `uv run pytest -q tests/test_lane_install_contract.py`
Expected: `61 passed`.

Run: `env -u VIRTUAL_ENV PATH="$(dirname "$(command -v uv)"):/usr/bin:/bin" .venv/bin/python -m pytest -q tests/test_lane_install_contract.py -k RealBash`
Expected: `23 passed, 38 deselected`.

- [ ] **Step 4: Commit**

```bash
git add tests/test_lane_install_contract.py
git commit -m "test: resolve python to the seeded interpreter in the lane install fragment" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 4: Orphan guard for test helper processes (ROADMAP P0-04, part 1)

**Files:**
- Create: `tests/production_entry/orphan_guard.py`, `tests/test_orphan_guard.py`
- Modify: `tests/production_entry/durable_ingress_server.py`, `fake_native_server.py`, `fake_vendor.py`, `production_matrix_worker.py`

The helper processes are re-parented to init when pytest dies abruptly, and
then they run for days. The guard makes them exit when their parent pid
changes.

- [ ] **Step 1: Write the failing test**

```python
"""A guarded test helper exits when the process that spawned it is killed."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

GUARD_DIR = Path(__file__).resolve().parent / "production_entry"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_for(predicate, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, message
        time.sleep(0.05)


def test_a_guarded_child_exits_when_its_parent_is_killed(tmp_path):
    pid_file = tmp_path / "child.pid"
    child_code = (
        f"import os, sys, time; sys.path.insert(0, {str(GUARD_DIR)!r}); "
        "import orphan_guard; orphan_guard.install(0.1); "
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    )
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); time.sleep(60)"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_code])
    try:
        _wait_for(lambda: pid_file.is_file() and pid_file.read_text(), 15, "child never started")
        child = int(pid_file.read_text())
        parent.kill()
        parent.wait(timeout=10)
        _wait_for(lambda: not _alive(child), 10, "the orphaned child outlived its parent")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest -q tests/test_orphan_guard.py`
Expected: FAIL with `AssertionError: child never started`. The child cannot
`import orphan_guard`.

- [ ] **Step 3: Create the guard**

```python
"""Exit a test helper process as soon as the process that spawned it dies.

The fake servers, the ingress server, the matrix worker and the fake vendor
run as separate processes. When pytest is killed abruptly its fixtures never
run their teardown, the children are re-parented to init, and they would
otherwise run forever. ``install()`` starts a daemon thread that polls the
parent pid and ends the process when it changes.
"""

from __future__ import annotations

import os
import threading
import time

POLL_SECONDS = 0.5


def install(poll_seconds: float = POLL_SECONDS) -> threading.Thread:
    parent = os.getppid()

    def watch() -> None:
        while True:
            if os.getppid() != parent:
                os._exit(0)
            time.sleep(poll_seconds)

    thread = threading.Thread(target=watch, name="orphan-guard", daemon=True)
    thread.start()
    return thread
```

- [ ] **Step 4: Run the test again**

Run: `uv run pytest -q tests/test_orphan_guard.py`
Expected: `1 passed`.

- [ ] **Step 5: Install the guard in the four helper scripts**

Each script's `main()` starts with the same three lines:

```diff
diff --git a/tests/production_entry/durable_ingress_server.py b/tests/production_entry/durable_ingress_server.py
index 7e0ca6f..b1329ae 100644
--- a/tests/production_entry/durable_ingress_server.py
+++ b/tests/production_entry/durable_ingress_server.py
@@ -333,6 +333,9 @@ async def _serve(app, ready_file: Path) -> None:
 
 
 def main() -> None:
+    import orphan_guard  # sibling module: this file runs as a script
+
+    orphan_guard.install()
     parser = argparse.ArgumentParser(description=__doc__)
     parser.add_argument("--ready-file", required=True)
     parser.add_argument("--db-url", required=True, help="the shared durable database URL")
```

```diff
diff --git a/tests/production_entry/fake_native_server.py b/tests/production_entry/fake_native_server.py
index 4038c89..1253144 100644
--- a/tests/production_entry/fake_native_server.py
+++ b/tests/production_entry/fake_native_server.py
@@ -1101,6 +1101,9 @@ def make_gitlab_handler(state: GitLabState) -> type[BaseHTTPRequestHandler]:
 
 
 def main(argv: list[str] | None = None) -> int:
+    import orphan_guard  # sibling module: this file runs as a script
+
+    orphan_guard.install()
     parser = argparse.ArgumentParser(description="The production-entry fake native server")
     parser.add_argument("--ready-file", required=True, help="where the chosen port is written")
     parser.add_argument(
```

```diff
diff --git a/tests/production_entry/fake_vendor.py b/tests/production_entry/fake_vendor.py
index 70327f9..69629e7 100755
--- a/tests/production_entry/fake_vendor.py
+++ b/tests/production_entry/fake_vendor.py
@@ -213,6 +213,9 @@ def _emit(frame: dict) -> None:
 
 
 def main(argv: list[str]) -> int:
+    import orphan_guard  # sibling module: this file runs as a script
+
+    orphan_guard.install()
     if argv and argv[0] == "app-server":
         return run_app_server()
     if "--once" in argv:
```

```diff
diff --git a/tests/production_entry/production_matrix_worker.py b/tests/production_entry/production_matrix_worker.py
index bc8a9c1..1c79d0f 100644
--- a/tests/production_entry/production_matrix_worker.py
+++ b/tests/production_entry/production_matrix_worker.py
@@ -436,6 +436,9 @@ async def _run(args, settings, ready_file: Path) -> None:  # noqa: ANN001
 
 
 def main() -> int:
+    import orphan_guard  # sibling module: this file runs as a script
+
+    orphan_guard.install()
     parser = argparse.ArgumentParser(description=__doc__)
     parser.add_argument("--ready-file", required=True)
     parser.add_argument("--db-url", required=True, help="the shared durable database URL")
```

- [ ] **Step 6: Check that a production-entry suite still works**

Run: `uv run pytest -q tests/production_entry/test_concurrent_publication.py`
Expected: `2 passed`.

- [ ] **Step 7: Commit**

```bash
git add tests/production_entry tests/test_orphan_guard.py
git commit -m "test: helper processes exit when the pytest process that spawned them dies" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 5: Test tiers and the leaked-process check (ROADMAP P0-05, P0-04 part 2)

**Files:**
- Create: `tests/_hygiene.py`, `tests/test_hygiene.py`, `docs/reference/testing.md`
- Modify: `tests/conftest.py`, `pyproject.toml` (markers), `.github/workflows/ci.yml` (test job, new `tests-slow` job)

Design note: there is no `addopts = -m "not slow"`. Scripts such as
`scripts/pg_gate.py` invoke pytest with explicit selections, and an
`addopts` filter would silently deselect them. CI passes `-m` explicitly
instead.

- [ ] **Step 1: Write the failing tests**

```python
"""Tier markers by path, and the leaked-process check (tests/_hygiene.py)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time

import pytest

from tests import _hygiene

needs_pgrep = pytest.mark.skipif(shutil.which("pgrep") is None, reason="pgrep is unavailable")


def test_process_heavy_suites_are_slow():
    assert "slow" in _hygiene.tiers_for("tests/production_entry/test_x.py::test_a")
    assert "slow" in _hygiene.tiers_for("tests/fi_os/test_y.py::test_b")
    assert "slow" in _hygiene.tiers_for("tests/test_failure_injection_os.py::test_c")


def test_ordinary_tests_have_no_tier():
    assert _hygiene.tiers_for("tests/test_ordinary.py::TestThing::test_a") == set()


@needs_pgrep
def test_a_running_child_is_killed_and_reported():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert child.pid in _hygiene.reap_and_kill_leaks()
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


@needs_pgrep
def test_a_finished_child_is_reaped_not_reported():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    time.sleep(1.0)  # let it exit without being waited on (a zombie)
    assert child.pid not in _hygiene.reap_and_kill_leaks()
    child.wait(timeout=10)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest -q tests/test_hygiene.py`
Expected: collection error `cannot import name '_hygiene' from 'tests'`.

- [ ] **Step 3: Create the hygiene module**

```python
"""Test-suite hygiene: tier markers and leaked-process detection (DESIGN §7)."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess

SLOW_PREFIXES = ("tests/production_entry/", "tests/fi_os/")
SLOW_FILES = frozenset({"tests/test_failure_injection_os.py"})


def tiers_for(nodeid: str) -> set[str]:
    """The tier markers a collected test carries, from its file path."""
    path = nodeid.split("::", 1)[0]
    tiers = set()
    if path.startswith(SLOW_PREFIXES) or path in SLOW_FILES:
        tiers.add("slow")
    return tiers


def child_pids(pid: int | None = None) -> list[int]:
    if shutil.which("pgrep") is None:
        return []
    result = subprocess.run(
        ["pgrep", "-P", str(pid or os.getpid())], capture_output=True, text=True, check=False
    )
    return [int(p) for p in result.stdout.split()]


def reap_and_kill_leaks() -> list[int]:
    """Reap finished children; kill, reap and return the ones still running."""
    leaked = []
    for pid in child_pids():
        try:
            finished, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            continue
        if finished == 0:
            leaked.append(pid)
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
    return leaked
```

- [ ] **Step 4: Run the tests again**

Run: `uv run pytest -q tests/test_hygiene.py`
Expected: `4 passed`.

- [ ] **Step 5: Wire the hooks into `tests/conftest.py`**

```diff
diff --git a/tests/conftest.py b/tests/conftest.py
index 109b67f..b35688f 100644
--- a/tests/conftest.py
+++ b/tests/conftest.py
@@ -8,6 +8,7 @@ from pydantic import SecretStr
 from forge.config import Settings
 from forge.database import reset_engine
 from forge.main import create_app
+from tests import _hygiene
 
 # Suppress debug logs from libraries whose background threads can write to
 # stderr after pytest has already torn down the stream, causing
@@ -153,3 +154,21 @@ async def client(app) -> AsyncClient:
     transport = ASGITransport(app=app)
     async with AsyncClient(transport=transport, base_url="http://test") as ac:
         yield ac
+
+
+def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
+    """Mark tiers by path: ``slow`` (real processes) and ``invariant`` (DESIGN §4)."""
+    for item in items:
+        for tier in sorted(_hygiene.tiers_for(item.nodeid)):
+            item.add_marker(tier)
+
+
+def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
+    """Fail the run when a test leaked a running child process."""
+    leaked = _hygiene.reap_and_kill_leaks()
+    if leaked:
+        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
+        message = f"test hygiene: killed leaked child processes {leaked}"
+        if reporter is not None:
+            reporter.write_line(message, red=True)
+        session.exitstatus = pytest.ExitCode.TESTS_FAILED
```

- [ ] **Step 6: Register the markers in `pyproject.toml`**

Replace the `markers = [...]` list under `[tool.pytest.ini_options]` with:

```toml
markers = [
    "production_entry: Q35-09 production-entry invariant traces (AT-01..AT-06 at the process/DB level)",
    "slow: spawns real processes or services; the default CI job runs -m 'not slow'",
    "invariant: evidence for the non-negotiable invariants I1-I8 (DESIGN §4); run with -m invariant",
]
```

- [ ] **Step 7: Check tier selection**

Run: `uv run pytest -q -m slow --co | tail -1`
Expected: about `189/… tests collected`.

Run: `uv run pytest -q -m "not slow" --co | tail -1`
Expected: the remainder.

- [ ] **Step 8: Split CI into tiers**

In `.github/workflows/ci.yml`, change the `test` job's last step to:

```yaml
      - name: Run tests (default tier)
        run: uv run pytest -q -m "not slow"
```

Then add this job right before the `integration` job:

```yaml
  # The slow tier: real processes and services (tests/production_entry,
  # tests/fi_os). Kept off the pull-request path; pg_gate still runs the
  # required production-entry selection on every pull request.
  tests-slow:
    if: github.event_name != 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - name: Install redis-server (the durable-ingress matrix spawns the real binary)
        run: sudo apt-get update -q && sudo apt-get install -y -q redis-server
      - uses: actions/checkout@v4

      - name: Set up uv
        uses: astral-sh/setup-uv@v5

      - name: Install dependencies
        run: uv sync

      - name: Run tests (slow tier)
        run: uv run pytest -q -m slow
```

- [ ] **Step 9: Document the tiers**

````markdown
# Testing forge

## Tiers

| Command | What runs | Where |
| --- | --- | --- |
| `uv run pytest -q -m "not slow"` | The default tier: fast, hermetic | Every pull request (`test` job) |
| `uv run pytest -q -m slow` | Real processes and services (`tests/production_entry`, `tests/fi_os`) | `main` and nightly (`tests-slow` job) |
| `uv run pytest -q -m invariant $(uv run python -m tests.invariants.registry)` | The Invariant Suite I1–I8 | Every pull request (`invariants` job, with Postgres) |
| `uv run pytest -q` | Everything | Locally, before large changes |

Tiers are assigned by path in `tests/_hygiene.py`; the invariant files are
listed in `tests/invariants/registry.py`.

## Subprocess hygiene

Tests that start helper processes must stop them in fixture teardown. Two
guards back this up:

- helper scripts under `tests/production_entry/` call
  `orphan_guard.install()`, so they exit when pytest dies abruptly;
- `pytest_sessionfinish` fails the run if a child process of pytest is
  still running at the end.

Leftovers from older runs (before these guards) can be found and stopped:

```bash
pgrep -fl "tests/production_entry/(fake_native_server|durable_ingress_server|production_matrix_worker)"
kill <pid>   # for each pid listed above
```
````

- [ ] **Step 10: Run the default tier once**

Run: `set -o pipefail && uv run pytest -q -m "not slow" | tail -3`
Expected: no failures. There is no `test hygiene: killed leaked child
processes` line; if one appears, the listed pid names a fixture that did not
stop its subprocess, and that must be fixed first.

- [ ] **Step 11: Commit**

```bash
git add tests/_hygiene.py tests/test_hygiene.py tests/conftest.py pyproject.toml docs/reference/testing.md .github/workflows/ci.yml
git commit -m "test: slow and default tiers; fail the session on leaked child processes" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 6: The Invariant Suite (ROADMAP P0-06)

**Files:**
- Create: `tests/invariants/__init__.py` (empty), `tests/invariants/registry.py`, `tests/invariants/test_registry_integrity.py`, `tests/invariants/test_i1_no_merge.py`, `tests/_scripts.py`, `scripts/assert_no_skips.py`, `tests/test_script_assert_no_skips.py`
- Modify: `tests/_hygiene.py`, `tests/test_hygiene.py`, `.github/workflows/ci.yml` (new `invariants` job)

- [ ] **Step 1: Write the registry integrity test (failing)**

```python
"""The Invariant Suite registry cannot lose its evidence silently."""

from __future__ import annotations

from pathlib import Path

from tests.invariants.registry import INVARIANTS

REPO = Path(__file__).resolve().parents[2]


def test_i1_to_i8_are_all_registered():
    assert {f"I{n}" for n in range(1, 9)} <= {inv.ident for inv in INVARIANTS}


def test_every_invariant_names_evidence():
    assert all(inv.files for inv in INVARIANTS)


def test_every_registered_file_exists():
    missing = [f for inv in INVARIANTS for f in inv.files if not (REPO / f).is_file()]
    assert missing == []
```

Also create an empty `tests/invariants/__init__.py`.

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest -q tests/invariants/test_registry_integrity.py`
Expected: `ModuleNotFoundError: No module named 'tests.invariants.registry'`.

- [ ] **Step 3: Create the registry**

```python
"""The Invariant Suite registry (DESIGN §4).

Each invariant names the test files that prove it. The collection hook in
``tests/conftest.py`` marks every test in these files, and every test under
``tests/invariants/``, with ``invariant``. ``test_registry_integrity``
fails when a listed file disappears, so evidence cannot vanish silently.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Invariant:
    ident: str
    rule: str
    files: tuple[str, ...]


INVARIANTS: tuple[Invariant, ...] = (
    Invariant(
        "I1",
        "The bot never merges; the lane holds no write credential",
        ("tests/invariants/test_i1_no_merge.py",),
    ),
    Invariant(
        "I2",
        "Only the publisher writes, and every write is validated",
        (
            "tests/test_publication_boundary.py",
            "tests/test_publisher.py",
            "tests/test_patch_differential.py",
        ),
    ),
    Invariant(
        "I3",
        "Intent before effect; unknown outcomes are probed, never retried blindly",
        ("tests/test_publication_intents.py", "tests/test_failure_injection.py"),
    ),
    Invariant(
        "I4",
        "Approval is consume-once and digest-bound; the executor gets the approved bytes",
        (
            "tests/test_durable_gates.py",
            "tests/test_runs_spec.py",
            "tests/test_revision_executor_proof.py",
        ),
    ),
    Invariant(
        "I5",
        "Budget before spend; idempotent receipts; unknown spend is never zero",
        (
            "tests/test_budgets.py",
            "tests/test_closing_budget.py",
            "tests/test_budget_amendment.py",
            "tests/test_usage_receipts.py",
        ),
    ),
    Invariant(
        "I6",
        "Exactly one Draft PR/MR per run",
        ("tests/production_entry/test_concurrent_publication.py",),
    ),
    Invariant(
        "I7",
        "A killed worker loses no work and duplicates no effect",
        ("tests/test_failure_injection.py",),
    ),
    Invariant(
        "I8",
        "Verification is bound to the exact candidate SHA",
        ("tests/test_runs_verification.py", "tests/test_verification_binding.py"),
    ),
    Invariant(
        "SELF-TRIGGER",
        "Bot-authored notes are never commands",
        ("tests/test_bot_loop.py",),
    ),
)

INVARIANT_FILES: frozenset[str] = frozenset(f for inv in INVARIANTS for f in inv.files)


if __name__ == "__main__":
    # CI selects the suite by file so unrelated module-level skips never count.
    print(" ".join(["tests/invariants", *sorted(INVARIANT_FILES)]))
```

- [ ] **Step 4: Write the I1 test**

```python
"""I1 — the bot never merges (ADR-0003).

Two mechanical checks: no provider client or writer contains a merge API
call, and every lane template disables push and grants no write access.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PROVIDER_SOURCES = (
    "src/forge/gitlab",
    "src/forge/integrations",
    "src/forge/repository",
    "src/forge/providers",
)
MERGE_PATTERNS = (
    re.compile(r"/merge_requests/[^\"'\s]*/merge\b"),  # GitLab: accept a merge request
    re.compile(r"\bmerge_when_pipeline_succeeds\b"),
    re.compile(r"\bauto_merge\b"),
    re.compile(r"/pulls/[^\"'\s]*/merge\b"),  # GitHub REST: merge a pull request
    re.compile(r"\bmergePullRequest\b"),
    re.compile(r"\benablePullRequestAutoMerge\b"),
    re.compile(r"\bautoCompleteSetBy\b"),  # Azure DevOps auto-complete
    re.compile(r"\bcompletionOptions\b"),
)
LANE_TEMPLATES = sorted(
    [
        *REPO.glob("ci/templates/*.yml"),
        REPO / ".github/workflows/forge-harness.yml",
        REPO / "forge-lane.azure-pipelines.yml",
    ]
)
GITHUB_TEMPLATES = [p for p in LANE_TEMPLATES if "github" in p.name or p.parent.name == "workflows"]


def _provider_files() -> list[Path]:
    return sorted(
        path
        for base in PROVIDER_SOURCES
        if (REPO / base).is_dir()
        for path in (REPO / base).rglob("*.py")
    )


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def test_the_scanner_recognises_merge_calls():
    samples = (
        'await self._put(f"/projects/{pid}/merge_requests/{iid}/merge")',
        'await self._put(f"/repos/{repo}/pulls/{number}/merge")',
        "mutation { mergePullRequest(input: $input) { clientMutationId } }",
    )
    assert all(any(p.search(s) for p in MERGE_PATTERNS) for s in samples)


@pytest.mark.parametrize("path", _provider_files(), ids=_rel)
def test_provider_code_contains_no_merge_call(path: Path):
    text = path.read_text(encoding="utf-8")
    hits = [p.pattern for p in MERGE_PATTERNS if p.search(text)]
    assert hits == [], f"{_rel(path)} uses a merge API: {hits}"


@pytest.mark.parametrize("path", LANE_TEMPLATES, ids=_rel)
def test_lane_template_disables_push_and_grants_no_write(path: Path):
    text = path.read_text(encoding="utf-8")
    assert "git remote set-url --push origin FORBIDDEN" in text
    assert not re.search(r"contents:\s*write", text)


@pytest.mark.parametrize("path", GITHUB_TEMPLATES, ids=_rel)
def test_github_lane_does_not_persist_checkout_credentials(path: Path):
    assert "persist-credentials: false" in path.read_text(encoding="utf-8")
```

- [ ] **Step 5: Run the invariants package**

Run: `uv run pytest -q tests/invariants`
Expected: `32 passed`. This is characterization: the current code complies.
`test_the_scanner_recognises_merge_calls` proves that the scanner is not
vacuous.

- [ ] **Step 6: Mark invariant tests by path — failing test first**

Add this test to `tests/test_hygiene.py`, after
`test_ordinary_tests_have_no_tier`:

```python
def test_registered_invariant_files_are_marked():
    assert _hygiene.tiers_for("tests/test_publisher.py::test_a") == {"invariant"}
    assert _hygiene.tiers_for("tests/invariants/test_i1_no_merge.py::test_x") == {"invariant"}
    assert _hygiene.tiers_for("tests/production_entry/test_concurrent_publication.py::t") == {
        "slow",
        "invariant",
    }
```

Run: `uv run pytest -q tests/test_hygiene.py`
Expected: FAIL (`set()` is not `{'invariant'}`).

- [ ] **Step 7: Extend `tests/_hygiene.py`**

Add the import after the module imports:

```python
from tests.invariants.registry import INVARIANT_FILES
```

In `tiers_for`, before `return tiers`, add:

```python
    if path.startswith("tests/invariants/") or path in INVARIANT_FILES:
        tiers.add("invariant")
```

Run: `uv run pytest -q tests/test_hygiene.py`
Expected: `5 passed`.

- [ ] **Step 8: The skip guard — failing tests first**

```python
"""Import helper for the stdlib scripts under ``scripts/``."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load_script(name: str) -> ModuleType:
    """Import ``scripts/<name>.py`` (the scripts import their siblings)."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def write_tree(root: Path, files: dict[str, str]) -> Path:
    """Create ``files`` (relative path → text) under ``root``."""
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root
```

```python
"""scripts/assert_no_skips.py: the Invariant Suite must execute, not skip."""

from __future__ import annotations

from tests._scripts import load_script

no_skips = load_script("assert_no_skips")


def _report(tmp_path, cases: str) -> str:
    path = tmp_path / "report.xml"
    path.write_text(f'<testsuites><testsuite name="pytest">{cases}</testsuite></testsuites>')
    return str(path)


def test_all_cases_executed_passes(tmp_path):
    report = _report(
        tmp_path, '<testcase classname="t" name="a"/><testcase classname="t" name="b"/>'
    )
    assert no_skips.main([report]) == 0


def test_a_skipped_case_fails(tmp_path, capsys):
    report = _report(
        tmp_path, '<testcase classname="t" name="a"><skipped message="no pg"/></testcase>'
    )
    assert no_skips.main([report]) == 1
    assert "skipped t::a" in capsys.readouterr().out


def test_an_empty_report_fails(tmp_path):
    assert no_skips.main([_report(tmp_path, "")]) == 1


def test_usage_error_without_a_report():
    assert no_skips.main([]) == 2
```

Run: `uv run pytest -q tests/test_script_assert_no_skips.py`
Expected: `ModuleNotFoundError: No module named 'assert_no_skips'`.

- [ ] **Step 9: Create the skip guard**

```python
"""Fail when a JUnit XML report contains skipped or no tests.

The Invariant Suite must execute, not skip: a skipped invariant is not
evidence. Used by the ``invariants`` CI job.

Usage::

    uv run python scripts/assert_no_skips.py invariants.xml
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET


def skipped_cases(path: str) -> tuple[int, list[str]]:
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    skipped = [
        f"{case.get('classname')}::{case.get('name')}"
        for case in cases
        if case.find("skipped") is not None
    ]
    return len(cases), skipped


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: assert_no_skips.py REPORT.xml")
        return 2
    total, skipped = skipped_cases(args[0])
    for name in skipped:
        print(f"assert-no-skips: skipped {name}")
    if total == 0 or skipped:
        print(f"assert-no-skips: FAILED — {total} cases, {len(skipped)} skipped")
        return 1
    print(f"assert-no-skips: ok — {total} cases executed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Run: `uv run pytest -q tests/test_script_assert_no_skips.py`
Expected: `4 passed`.

- [ ] **Step 10: Add the CI job**

Add before the `integration` job:

```yaml
  # ADR-0035 / DESIGN §4: the Invariant Suite (I1-I8) must execute, never skip.
  invariants:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:17-alpine
        env:
          POSTGRES_USER: forge
          POSTGRES_PASSWORD: forge
          POSTGRES_DB: forge
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U forge -d forge"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 10
      redis:
        image: redis:7-alpine
        ports:
          - 6379:6379
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 10
    env:
      FORGE_PG_TEST_URL: postgresql+asyncpg://forge:forge@127.0.0.1:5432/forge
    steps:
      - uses: actions/checkout@v4

      - name: Set up uv
        uses: astral-sh/setup-uv@v5

      - name: Install dependencies (with the postgres extra)
        run: uv sync --extra postgres

      - name: Invariant Suite
        run: >-
          uv run pytest -q -m invariant --junitxml=invariants.xml
          $(uv run python -m tests.invariants.registry)

      - name: Every invariant test executed
        run: uv run python scripts/assert_no_skips.py invariants.xml
```

The suite is selected **by file** through `python -m tests.invariants.registry`.
With `-m invariant` alone, unrelated modules that skip at import time (for
example `tests/test_failure_injection_os.py`) would show up as skips.

- [ ] **Step 11: Run the suite locally**

Run: `uv run pytest -q -m invariant --junitxml=/tmp/inv.xml $(uv run python -m tests.invariants.registry)`
Expected without Postgres: about `475 passed, 3 skipped`. The skips are the
`FORGE_PG_TEST_URL`-gated tests.

Run: `uv run python scripts/assert_no_skips.py /tmp/inv.xml`
Expected locally: `FAILED` listing those 3 tests. In CI, with Postgres
provided, the expected output is `ok`.

- [ ] **Step 12: Commit**

```bash
git add tests/invariants tests/_hygiene.py tests/test_hygiene.py tests/_scripts.py scripts/assert_no_skips.py tests/test_script_assert_no_skips.py .github/workflows/ci.yml
git commit -m "test: the Invariant Suite I1-I8 as a required CI job that may not skip" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 7: Optional GitLab settings, fail-closed ingress, a doctor that never raises (ROADMAP P0-11)

**Files:**
- Create: `tests/test_settings_gitlab_optional.py`, `tests/test_doctor_settings.py`
- Modify: `src/forge/config.py`, `src/forge/gateway/validator.py`, `src/forge/doctor.py`, `.env.example`

The flag follows the existing `FORGE_GITHUB_ENABLED` and
`FORGE_AZDO_ENABLED` convention. It defaults to `true`, so existing
deployments behave exactly as before.

- [ ] **Step 1: Write the failing tests**

```python
"""GitLab settings are required only while GitLab is enabled."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from forge.config import Settings
from forge.gateway.validator import validate_webhook_token

GITLAB_ENV = ("GITLAB_URL", "GITLAB_TOKEN", "GITLAB_WEBHOOK_SECRET", "FORGE_GITLAB_ENABLED")


@pytest.fixture()
def clean_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env file is read
    for name in GITLAB_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_gitlab_is_enabled_by_default_and_requires_its_settings(clean_env):
    with pytest.raises(ValidationError) as caught:
        Settings()
    assert "GITLAB_URL, GITLAB_TOKEN, GITLAB_WEBHOOK_SECRET must be set" in str(caught.value)


def test_a_github_only_deployment_needs_no_gitlab_settings(clean_env):
    clean_env.setenv("FORGE_GITLAB_ENABLED", "false")
    settings = Settings()
    assert settings.FORGE_GITLAB_ENABLED is False
    assert settings.GITLAB_URL == ""


def test_complete_gitlab_settings_validate(clean_env):
    settings = Settings(
        GITLAB_URL="https://gitlab.test", GITLAB_TOKEN="t", GITLAB_WEBHOOK_SECRET="w"
    )
    assert settings.GITLAB_WEBHOOK_SECRET.get_secret_value() == "w"


def test_unconfigured_gitlab_ingress_refuses_even_an_empty_token():
    with pytest.raises(HTTPException) as caught:
        validate_webhook_token("", "")
    assert caught.value.status_code == 503


def test_configured_gitlab_ingress_still_checks_the_token():
    assert validate_webhook_token("s3cret", "s3cret") == "s3cret"
    with pytest.raises(HTTPException) as caught:
        validate_webhook_token("s3cret", "wrong")
    assert caught.value.status_code == 401
```

```python
"""forge doctor reports missing settings as FAIL lines, never a traceback."""

from __future__ import annotations

import json

import pytest

from forge import doctor
from forge.config import Settings


@pytest.fixture()
def empty_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env file is read
    for name in ("GITLAB_URL", "GITLAB_TOKEN", "GITLAB_WEBHOOK_SECRET", "FORGE_GITLAB_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_missing_settings_are_reported_not_raised(empty_env, capsys):
    assert doctor.main([]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "GITLAB_URL" in out
    assert "Traceback" not in out


def test_missing_settings_in_json(empty_env, capsys):
    assert doctor.main(["--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    assert report["checks"][0]["name"] == "settings"
    assert "GITLAB_URL" in report["checks"][0]["detail"]


async def test_disabled_gitlab_is_a_warning_not_a_network_call(empty_env):
    empty_env.setenv("FORGE_GITLAB_ENABLED", "false")
    results = await doctor.check_gitlab_identity(Settings())
    assert [(r.name, r.status) for r in results] == [("gitlab.token", doctor.WARN)]
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest -q tests/test_settings_gitlab_optional.py tests/test_doctor_settings.py`
Expected failures:
- the settings tests: the validation message differs, and
  `FORGE_GITLAB_ENABLED` does not exist yet;
- the validator test: `validate_webhook_token("", "")` returns instead of
  raising 503;
- the doctor tests: the `ValidationError` traceback propagates, and
  `check_gitlab_identity` does not exist.

- [ ] **Step 3: Make GitLab settings optional behind the flag**

```diff
diff --git a/src/forge/config.py b/src/forge/config.py
index 08693c6..436ca03 100644
--- a/src/forge/config.py
+++ b/src/forge/config.py
@@ -6,7 +6,7 @@ from pathlib import Path
 from typing import Any
 
 import yaml
-from pydantic import SecretStr
+from pydantic import SecretStr, model_validator
 from pydantic_settings import BaseSettings, SettingsConfigDict
 
 from forge.repository.changeset import BUILTIN_WRITE_PROFILES
@@ -17,12 +17,32 @@ class Settings(BaseSettings):
 
     model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
 
-    # GitLab connection
-    GITLAB_URL: str
-    GITLAB_TOKEN: SecretStr
-    GITLAB_WEBHOOK_SECRET: SecretStr
+    # GitLab connection. Required while FORGE_GITLAB_ENABLED is true (the
+    # default); a GitHub-only deployment sets FORGE_GITLAB_ENABLED=false.
+    FORGE_GITLAB_ENABLED: bool = True
+    GITLAB_URL: str = ""
+    GITLAB_TOKEN: SecretStr = SecretStr("")
+    GITLAB_WEBHOOK_SECRET: SecretStr = SecretStr("")
     FORGE_BOT_USERNAME: str = "forge-bot"
 
+    @model_validator(mode="after")
+    def _require_gitlab_settings_when_enabled(self) -> Settings:
+        if self.FORGE_GITLAB_ENABLED:
+            missing = [
+                name
+                for name, value in (
+                    ("GITLAB_URL", self.GITLAB_URL),
+                    ("GITLAB_TOKEN", self.GITLAB_TOKEN.get_secret_value()),
+                    ("GITLAB_WEBHOOK_SECRET", self.GITLAB_WEBHOOK_SECRET.get_secret_value()),
+                )
+                if not value
+            ]
+            if missing:
+                raise ValueError(
+                    f"{', '.join(missing)} must be set while FORGE_GITLAB_ENABLED is true"
+                )
+        return self
+
     # LiteLLM proxy
     LITELLM_URL: str = "http://litellm:4000"
```

- [ ] **Step 4: Fail closed when GitLab ingress is unconfigured**

```diff
diff --git a/src/forge/gateway/validator.py b/src/forge/gateway/validator.py
index 2d184a7..e4f5701 100644
--- a/src/forge/gateway/validator.py
+++ b/src/forge/gateway/validator.py
@@ -18,7 +18,9 @@ def validate_webhook_token(
     expected_secret: str,
     x_gitlab_token: str | None = Header(None),
 ) -> str:
-    """Raise 401 if the token is missing or does not match."""
+    """Raise 503 if GitLab ingress is unconfigured, 401 if the token is missing or wrong."""
+    if not expected_secret:
+        raise HTTPException(status_code=503, detail="GitLab ingress is not configured")
     if x_gitlab_token is None:
         raise HTTPException(status_code=401, detail="Missing X-Gitlab-Token header")
     if not hmac.compare_digest(x_gitlab_token, expected_secret):
```

- [ ] **Step 5: Doctor reports instead of raising**

```diff
diff --git a/src/forge/doctor.py b/src/forge/doctor.py
index 87569ca..c9e9736 100644
--- a/src/forge/doctor.py
+++ b/src/forge/doctor.py
@@ -36,6 +36,7 @@ from dataclasses import asdict, dataclass
 from typing import Any
 
 import httpx
+from pydantic import ValidationError
 
 from forge.config import Settings
 from forge.gitlab.client import GitLabClient, GitLabAPIError
@@ -97,6 +98,23 @@ async def check_bot_token(settings: Settings) -> CheckResult:
         return _result("forge.bot_token", False, "", _short(exc))
 
 
+async def check_gitlab_identity(settings: Settings) -> list[CheckResult]:
+    """The GitLab token checks, or one warning when GitLab is disabled."""
+    if not getattr(settings, "FORGE_GITLAB_ENABLED", True):
+        return [CheckResult("gitlab.token", WARN, "GitLab disabled (FORGE_GITLAB_ENABLED=false)")]
+    return [await check_gitlab(settings), await check_bot_token(settings)]
+
+
+def settings_failures(exc: ValidationError) -> list[CheckResult]:
+    """One FAIL line per invalid or missing setting, never a traceback."""
+    results = []
+    for error in exc.errors():
+        location = ".".join(str(part) for part in error["loc"])
+        name = f"settings.{location}" if location else "settings"
+        results.append(CheckResult(name, FAIL, error["msg"]))
+    return results
+
+
 async def check_redis(settings: Settings) -> CheckResult:
     from forge.utils.redis_client import RedisManager
 
@@ -1028,8 +1046,7 @@ async def run_checks(settings: Settings, project_id: int | None = None) -> list[
     results = [
         CheckResult("python.version", PASS, f"{sys.version_info.major}.{sys.version_info.minor}")
     ]
-    results.append(await check_gitlab(settings))
-    results.append(await check_bot_token(settings))
+    results.extend(await check_gitlab_identity(settings))
     results.append(await check_redis(settings))
     results.append(await check_database(settings))
     results.append(await check_litellm(settings))
@@ -1100,8 +1117,12 @@ def main(argv: list[str] | None = None) -> int:
         # Offline mode: the support matrix owns its own exit verdict.
         return _support_matrix_mode(args.json)
 
-    settings = Settings()  # type: ignore[call-arg]
-    results = asyncio.run(run_checks(settings, args.project))
+    try:
+        settings = Settings()  # type: ignore[call-arg]
+    except ValidationError as exc:
+        results = settings_failures(exc)
+    else:
+        results = asyncio.run(run_checks(settings, args.project))
     failed = any(r.status == FAIL for r in results)
 
     if args.json:
```

`getattr(…, True)` keeps `tests/test_doctor.py`, which passes a bare
`object()` as settings, working unchanged.

- [ ] **Step 6: Document the flag**

```diff
diff --git a/.env.example b/.env.example
index 8fd5fb1..f97c03a 100644
--- a/.env.example
+++ b/.env.example
@@ -1,4 +1,7 @@
 # GitLab
+# GitLab is enabled by default; a GitHub-only deployment sets FORGE_GITLAB_ENABLED=false
+# and may then leave the three GITLAB_* values unset.
+FORGE_GITLAB_ENABLED=true
 GITLAB_URL=https://gitlab.example.com
 GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
 GITLAB_WEBHOOK_SECRET=change-me-to-a-random-string
```

- [ ] **Step 7: Run the new and the neighbouring tests**

Run: `uv run pytest -q tests/test_settings_gitlab_optional.py tests/test_doctor_settings.py tests/test_doctor.py tests/test_bot_loop.py`
Expected: `40 passed`.

Run: `cd "$(mktemp -d)" && uv run --project "$OLDPWD" python -m forge.doctor; cd "$OLDPWD"`
Expected: `FAIL settings  Value error, GITLAB_URL, GITLAB_TOKEN, GITLAB_WEBHOOK_SECRET must be set while FORGE_GITLAB_ENABLED is true`
and exit code 1, with no traceback.

- [ ] **Step 8: Typecheck the blocking set**

Run: `uv run mypy src/forge/durable src/forge/runs src/forge/repository src/forge/factory src/forge/execution src/forge/integrations`
Expected: `Success: no issues found in 49 source files`. The field types are
unchanged (`str` and `SecretStr`).

- [ ] **Step 9: Commit**

```bash
git add src/forge/config.py src/forge/gateway/validator.py src/forge/doctor.py .env.example tests/test_settings_gitlab_optional.py tests/test_doctor_settings.py
git commit -m "feat(config): GitLab settings optional behind FORGE_GITLAB_ENABLED; doctor never raises" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 8: Import contracts (ROADMAP P0-07)

**Files:**
- Modify: `pyproject.toml` (dev dependency, `[tool.importlinter]`), `uv.lock`, `.github/workflows/ci.yml` (lint step)

- [ ] **Step 1: Add the tool**

Run: `uv add --dev "import-linter==2.1"`
Expected: `pyproject.toml` lists `"import-linter==2.1"` under
`[dependency-groups] dev`, and `uv.lock` is updated.

- [ ] **Step 2: Add the contracts at the end of `pyproject.toml`**

```toml

# ADR-0035 guardrail: mechanical import boundaries. The ignore lists record
# existing debt and may only shrink (ROADMAP P0-07).
[tool.importlinter]
root_package = "forge"

[[tool.importlinter.contracts]]
name = "durable does not import the run services, adaptive, gateway or integrations"
type = "forbidden"
source_modules = ["forge.durable"]
forbidden_modules = ["forge.runs", "forge.adaptive", "forge.gateway", "forge.integrations"]
ignore_imports = [
    "forge.durable.budgets -> forge.adaptive.usage_ingestion",
    "forge.durable.budgets -> forge.runs.candidate",
]

[[tool.importlinter.contracts]]
name = "harnesses do not import the provider lifecycle services"
type = "forbidden"
source_modules = ["forge.harnesses"]
forbidden_modules = ["forge.runs.service", "forge.runs.github_service", "forge.runs.azure_service"]
```

The two `ignore_imports` entries are the only current violations: lazy
imports in `durable/budgets.py` at lines 1362 and 1437. They may only
disappear, never grow.

- [ ] **Step 3: Run the contracts**

Run: `uv run lint-imports`
Expected: `Contracts: 2 kept, 0 broken.`

- [ ] **Step 4: Prove that the contract bites**

Temporarily delete the first `ignore_imports` entry and run
`uv run lint-imports`. Expected: `BROKEN` with
`forge.durable.budgets -> forge.adaptive.usage_ingestion (l.1362)`. Restore
the entry.

- [ ] **Step 5: Add the CI step**

In the `lint` job, after `Ruff format check`, add:

```yaml
      - name: Import contracts
        run: uv run lint-imports
```

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock .github/workflows/ci.yml
git commit -m "build: import-linter contracts as a CI gate (ratchet mode)" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 9: Shared analysis and the metrics script (ROADMAP P0-10)

**Files:**
- Create: `scripts/_codebase.py`, `scripts/metrics.py`, `tests/test_script_metrics.py`
- Generate: `docs/roadmap/2026-09-29-simplification/metrics-history.json`, the measured block in `metrics.md`

- [ ] **Step 1: Write the failing tests**

```python
"""scripts/metrics.py: the simplification-program measurements."""

from __future__ import annotations

import json

from tests._scripts import load_script, write_tree

metrics = load_script("metrics")

TREE = {
    "pyproject.toml": '[project]\nname = "x"\ndependencies = ["a", "b"]\n',
    "src/forge/__init__.py": "",
    "src/forge/main.py": "from forge import core\n",
    "src/forge/core.py": "def f():\n    return 1\n",
    "src/forge/doctor.py": "from forge import diag\n",
    "src/forge/diag.py": "X = 1\n",
    "src/forge/orphan.py": "Y = 2\n",
    "tests/test_orphan.py": "from forge import orphan\n",
    "docs/a.md": "one two three\n",
}


def test_reachability_splits_runtime_tooling_and_test_only_code(tmp_path):
    snapshot = metrics.measure(write_tree(tmp_path, TREE))
    assert snapshot["modules"] == 6
    assert snapshot["runtime_lines"] == 3  # forge/__init__ (0) + main (1) + core (2)
    assert snapshot["tooling_only_lines"] == 2  # doctor + diag
    assert snapshot["tests_only_lines"] == 1  # orphan
    assert snapshot["direct_dependencies"] == 2
    assert snapshot["docs_words"] == 3


def test_who_imports_lists_direct_importers(tmp_path):
    assert metrics.who_imports(write_tree(tmp_path, TREE), "forge.core") == ["forge.main"]


def test_write_appends_history_and_regenerates_one_block(tmp_path):
    root = write_tree(tmp_path, {metrics.METRICS_FILE.as_posix(): "# Metrics\n\nTargets.\n"})
    metrics.write(root, {"date": "2026-09-30", "sha": "abc1234", "src_lines": 10})
    metrics.write(root, {"date": "2026-10-07", "sha": "def5678", "src_lines": 8})
    text = (root / metrics.METRICS_FILE).read_text()
    assert text.count(metrics.BEGIN) == 1
    assert "| `src/forge` lines | 10 | 8 |" in text
    assert len(json.loads((root / metrics.HISTORY_FILE).read_text())) == 2
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest -q tests/test_script_metrics.py`
Expected: `ModuleNotFoundError: No module named 'metrics'`.

- [ ] **Step 3: Create the shared analysis module**

```python
"""Shared static analysis of the forge source tree (stdlib only).

Used by ``scripts/metrics.py``, ``scripts/ratchet.py`` and
``scripts/test_ledger.py``. Everything here reads files; nothing imports
forge, so the scripts run without the project environment.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

CONTROL_PLANE_ROOTS = ("forge.main", "forge.worker.__main__", "forge.worker", "forge.migrate")
LANE_ROOTS = ("forge.harness_entry", "forge.lane_driver", "forge.candidate_collector", "forge_lane")
TOOLING_ROOTS = (
    "forge.doctor",
    "forge.release_manifest",
    "forge.release_promotion",
    "forge.profile_qualification",
    "forge.capability_manifest",
)
REVIEW_REF = re.compile(r"\b[RQ]\d{2}-\d{2}\b|(?<![\w/&])#\d{3,}\b")
ADR_REF = re.compile(r"\bADR-\d{4}\b")


@dataclass(frozen=True)
class Module:
    name: str
    path: Path
    lines: int


@dataclass(frozen=True)
class FunctionSize:
    qualname: str
    lineno: int
    lines: int


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def source_dirs(root: Path) -> list[Path]:
    """``src/`` plus every workspace package's ``src/`` (Phase 2 onward)."""
    dirs = [root / "src", *sorted((root / "packages").glob("*/src"))]
    return [d for d in dirs if d.is_dir()]


def iter_python_files(base: Path) -> Iterator[Path]:
    for path in sorted(base.rglob("*.py")):
        if "__pycache__" not in path.parts:
            yield path


def count_lines(path: Path) -> int:
    with path.open(encoding="utf-8") as handle:
        return sum(1 for _ in handle)


def module_name(path: Path, src_dir: Path) -> str:
    parts = list(path.relative_to(src_dir).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def discover_modules(root: Path) -> dict[str, Module]:
    modules: dict[str, Module] = {}
    for src_dir in source_dirs(root):
        for path in iter_python_files(src_dir):
            name = module_name(path, src_dir)
            modules[name] = Module(name, path, count_lines(path))
    return modules


def function_sizes(path: Path) -> list[FunctionSize]:
    """Every function and method, nested ones included, with a unique qualname."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    sizes: list[FunctionSize] = []
    seen: dict[str, int] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                qual = f"{prefix}{child.name}"
                seen[qual] = seen.get(qual, 0) + 1
                unique = qual if seen[qual] == 1 else f"{qual}#{seen[qual]}"
                end = child.end_lineno or child.lineno
                sizes.append(FunctionSize(unique, child.lineno, end - child.lineno + 1))
                visit(child, f"{qual}.")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return sizes


def prose_lines(path: Path) -> int:
    """Comment lines plus docstring lines."""
    source = path.read_text(encoding="utf-8")
    comments = sum(
        1
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type == tokenize.COMMENT
    )
    docstrings = 0
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings += doc.count("\n") + 1
    return comments + docstrings


def _resolve(name: str, modules: dict[str, Module]) -> str | None:
    parts = name.split(".")
    while parts:
        candidate = ".".join(parts)
        if candidate in modules:
            return candidate
        parts.pop()
    return None


def imports_of(path: Path, modules: dict[str, Module], current: str | None = None) -> set[str]:
    """Internal modules imported by a file (lazy imports and literal
    ``importlib.import_module`` calls included; parents implied)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    is_package = path.name == "__init__.py"
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if (hit := _resolve(alias.name, modules)) is not None:
                    found.add(hit)
        elif isinstance(node, ast.ImportFrom):
            if node.level and current is not None:
                base = current.split(".")
                if not is_package:
                    base = base[:-1]
                if node.level > 1:
                    base = base[: -(node.level - 1)]
                full = ".".join([*base, node.module] if node.module else base)
            else:
                full = node.module or ""
            for alias in node.names:
                hit = _resolve(f"{full}.{alias.name}", modules) or _resolve(full, modules)
                if hit is not None:
                    found.add(hit)
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            called = getattr(func, "attr", None) or getattr(func, "id", None)
            first = node.args[0]
            if (
                called == "import_module"
                and isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and (hit := _resolve(first.value, modules)) is not None
            ):
                found.add(hit)
    parents = {".".join(m.split(".")[:i]) for m in found for i in range(1, len(m.split(".")))}
    return found | {p for p in parents if p in modules}


def import_graph(modules: dict[str, Module]) -> dict[str, set[str]]:
    return {name: imports_of(mod.path, modules, name) for name, mod in modules.items()}


def reachable(roots: Iterable[str], graph: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    queue = deque(r for r in roots if r in graph)
    while queue:
        name = queue.popleft()
        if name not in seen:
            seen.add(name)
            queue.extend(graph[name])
    return seen


def roots_from_files(files: Iterable[Path], modules: dict[str, Module]) -> set[str]:
    roots: set[str] = set()
    for path in files:
        try:
            roots |= imports_of(path, modules)
        except SyntaxError:
            continue
    return roots


@dataclass(frozen=True)
class Reachability:
    runtime: set[str]
    lane: set[str]
    tooling_only: set[str]
    tests_only: set[str]
    unreached: set[str]


def classify(root: Path, modules: dict[str, Module]) -> Reachability:
    graph = import_graph(modules)
    control = reachable(CONTROL_PLANE_ROOTS, graph)
    lane = reachable(LANE_ROOTS, graph)
    runtime = control | lane
    script_roots = roots_from_files((root / "scripts").glob("*.py"), modules)
    tools_roots = roots_from_files((root / "tools").rglob("*.py"), modules)
    tooling = reachable((*TOOLING_ROOTS, *script_roots, *tools_roots), graph) - runtime
    test_roots = roots_from_files((root / "tests").rglob("*.py"), modules)
    tests_only = reachable(test_roots, graph) - runtime - tooling
    unreached = set(modules) - runtime - tooling - tests_only
    return Reachability(runtime, lane, tooling, tests_only, unreached)


def total_lines(names: Iterable[str], modules: dict[str, Module]) -> int:
    return sum(modules[n].lines for n in names)
```

- [ ] **Step 4: Create the metrics script**

```python
"""Measure the simplification-program metrics (DESIGN §2 and §12).

Usage::

    uv run python scripts/metrics.py                      # print the snapshot as JSON
    uv run python scripts/metrics.py --junit report.xml   # include the suite duration
    uv run python scripts/metrics.py --write              # append to the history and
                                                          # regenerate metrics.md
    uv run python scripts/metrics.py --who-imports forge.adaptive.ops_limits
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import _codebase as cb

PROGRAM_DIR = Path("docs/roadmap/2026-09-29-simplification")
HISTORY_FILE = PROGRAM_DIR / "metrics-history.json"
METRICS_FILE = PROGRAM_DIR / "metrics.md"
BEGIN = "<!-- metrics:begin -->"
END = "<!-- metrics:end -->"

ROWS: tuple[tuple[str, str], ...] = (
    ("src_lines", "`src/forge` lines"),
    ("modules", "Modules"),
    ("runtime_lines", "Runtime-reachable lines"),
    ("tooling_only_lines", "Tooling-only lines"),
    ("tests_only_lines", "Test-only lines"),
    ("lane_reachable_lines", "Lines reachable from lane entry points"),
    ("largest_file", "Largest file"),
    ("functions_over_80", "Functions > 80 lines"),
    ("functions_over_100", "Functions > 100 lines"),
    ("functions_over_200", "Functions > 200 lines"),
    ("prose_share", "Prose share in `src`"),
    ("review_refs", "Review and issue references in `src`"),
    ("adr_refs", "ADR references in `src`"),
    ("direct_dependencies", "Direct runtime dependencies"),
    ("docs_words", "Markdown words under `docs/`"),
    ("suite_seconds", "Default suite seconds"),
)


def _git_sha(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or "unknown"


def _suite_seconds(junit: Path | None) -> float | None:
    if junit is None:
        return None
    root = ET.parse(junit).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    return round(sum(float(s.get("time", "0")) for s in suites), 1)


def _direct_dependencies(root: Path) -> int:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return len(data["project"]["dependencies"])


def _docs_words(root: Path) -> int:
    return sum(
        len(path.read_text(encoding="utf-8", errors="replace").split())
        for path in (root / "docs").rglob("*.md")
    )


def measure(root: Path, junit: Path | None = None) -> dict[str, object]:
    modules = cb.discover_modules(root)
    forge_modules = {n: m for n, m in modules.items() if n == "forge" or n.startswith("forge.")}
    reach = cb.classify(root, modules)
    sizes = [
        size.lines for module in forge_modules.values() for size in cb.function_sizes(module.path)
    ]
    largest = max(forge_modules.values(), key=lambda m: m.lines)
    src_lines = cb.total_lines(forge_modules, modules)
    prose = sum(cb.prose_lines(m.path) for m in forge_modules.values())
    text = "".join(m.path.read_text(encoding="utf-8") for m in forge_modules.values())
    return {
        "date": dt.date.today().isoformat(),
        "sha": _git_sha(root),
        "src_lines": src_lines,
        "modules": len(forge_modules),
        "runtime_lines": cb.total_lines(reach.runtime, modules),
        "tooling_only_lines": cb.total_lines(reach.tooling_only, modules),
        "tests_only_lines": cb.total_lines(reach.tests_only, modules),
        "lane_reachable_lines": cb.total_lines(reach.lane, modules),
        "largest_file": f"{largest.lines} ({largest.path.relative_to(root)})",
        "functions_over_80": sum(1 for n in sizes if n > 80),
        "functions_over_100": sum(1 for n in sizes if n > 100),
        "functions_over_200": sum(1 for n in sizes if n > 200),
        "prose_share": f"{prose / src_lines:.0%}" if src_lines else "n/a",
        "review_refs": len(cb.REVIEW_REF.findall(text)),
        "adr_refs": len(cb.ADR_REF.findall(text)),
        "direct_dependencies": _direct_dependencies(root),
        "docs_words": _docs_words(root),
        "suite_seconds": _suite_seconds(junit),
    }


def render_history(history: list[dict[str, object]], last: int = 8) -> str:
    shown = history[-last:]
    header = "| Metric | " + " | ".join(f"{s['date']} (`{s['sha']}`)" for s in shown) + " |"
    divider = "| --- |" + " --- |" * len(shown)
    lines = [header, divider]
    for key, label in ROWS:
        cells = " | ".join("—" if s.get(key) is None else str(s[key]) for s in shown)
        lines.append(f"| {label} | {cells} |")
    return "\n".join(lines)


def write(root: Path, snapshot: dict[str, object]) -> None:
    history_path = root / HISTORY_FILE
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else []
    history.append(snapshot)
    history_path.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")
    metrics_path = root / METRICS_FILE
    text = metrics_path.read_text(encoding="utf-8")
    block = f"{BEGIN}\n{render_history(history)}\n{END}"
    if BEGIN in text:
        text = re.sub(re.escape(BEGIN) + ".*?" + re.escape(END), lambda _: block, text, flags=re.S)
    else:
        text = text.rstrip() + "\n\n## Measured history\n\n" + block + "\n"
    metrics_path.write_text(text, encoding="utf-8")


def who_imports(root: Path, target: str) -> list[str]:
    modules = cb.discover_modules(root)
    graph = cb.import_graph(modules)
    return sorted(name for name, deps in graph.items() if target in deps)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=cb.repo_root())
    parser.add_argument("--junit", type=Path, default=None)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--who-imports", default=None)
    args = parser.parse_args(argv)
    if args.who_imports:
        print("\n".join(who_imports(args.root, args.who_imports)))
        return 0
    snapshot = measure(args.root, args.junit)
    if args.write:
        write(args.root, snapshot)
    print(json.dumps(snapshot, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run the tests again**

Run: `uv run pytest -q tests/test_script_metrics.py`
Expected: `3 passed`.

- [ ] **Step 6: Reproduce the DESIGN §2 baseline**

Run: `uv run python scripts/metrics.py`
Expected, measured at `330b200` before Task 7:

| Key | Value |
| --- | --- |
| `modules` | 248 |
| `src_lines` | 190,383 |
| `runtime_lines` | 137,820 |
| `tooling_only_lines` | 33,082 |
| `tests_only_lines` | 19,481 |
| `lane_reachable_lines` | 132,772 |
| `functions_over_100` | 234 |
| `functions_over_200` | 56 |
| `prose_share` | 28% |
| `direct_dependencies` | 14 |

After Task 7, `src_lines` is about 43 higher. Line and module counts must
be within ±1% of the baseline. `review_refs` and `adr_refs` are new, split
metrics, so they have no baseline to match.

- [ ] **Step 7: Record the first measured snapshot**

Run: `uv run python scripts/metrics.py --write`
Expected: `metrics-history.json` is created, and `metrics.md` gains a
`## Measured history` section between `<!-- metrics:begin -->` and
`<!-- metrics:end -->`.

- [ ] **Step 8: Commit**

```bash
git add scripts/_codebase.py scripts/metrics.py tests/test_script_metrics.py docs/roadmap/2026-09-29-simplification/metrics.md docs/roadmap/2026-09-29-simplification/metrics-history.json
git commit -m "tools: metrics script and the first measured snapshot" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 10: Size ratchet (ROADMAP P0-08, part 1)

**Files:**
- Create: `scripts/ratchet.py`, `tests/test_script_ratchet.py`, `scripts/ratchet-baseline.json` (generated)
- Modify: `.github/workflows/ci.yml` (lint step)

Rule: files over 600 lines and functions over 80 lines are recorded debt
that may only shrink. Everything else must stay within those limits. Only
`forge/ports.py`, `forge/engine/effects.py` and `forge/api/app.py` may name
`WritePort`; this check is active from Phase 3 on.

- [ ] **Step 1: Write the failing tests**

```python
"""scripts/ratchet.py: oversized code may only shrink; new code stays small."""

from __future__ import annotations

import json
from pathlib import Path

from tests._scripts import load_script, write_tree

ratchet = load_script("ratchet")


def _function(name: str, lines: int) -> str:
    """A function that is exactly ``lines`` lines long."""
    return f"def {name}():\n" + "    x = 1\n" * (lines - 2) + "    return x\n"


def _baseline(root: Path) -> dict:
    return json.loads((root / "scripts/ratchet-baseline.json").read_text())


def _init(root: Path) -> None:
    (root / "scripts").mkdir(exist_ok=True)
    assert ratchet.main(["--root", str(root), "--init"]) == 0


def test_small_code_needs_no_baseline_entry(tmp_path):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("f", 10)})
    _init(root)
    assert _baseline(root) == {"files": {}, "functions": {}}
    assert ratchet.main(["--root", str(root)]) == 0


def test_oversized_function_is_recorded_and_may_not_grow(tmp_path, capsys):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("big", 90)})
    _init(root)
    assert _baseline(root)["functions"] == {"src/forge/m.py::big": 90}
    (root / "src/forge/m.py").write_text(_function("big", 91))
    assert ratchet.main(["--root", str(root)]) == 1
    assert "src/forge/m.py::big grew: 91 lines (allowed 90)" in capsys.readouterr().out


def test_new_function_over_the_limit_fails(tmp_path, capsys):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("f", 10)})
    _init(root)
    (root / "src/forge/n.py").write_text(_function("huge", 81))
    assert ratchet.main(["--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "src/forge/n.py::huge exceeds the limit: 81 lines (allowed 80)" in out


def test_small_function_may_grow_up_to_the_limit(tmp_path):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("f", 10)})
    _init(root)
    (root / "src/forge/m.py").write_text(_function("f", 80))
    assert ratchet.main(["--root", str(root)]) == 0


def test_update_lowers_shrunk_debt_and_drops_repaid_entries(tmp_path):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("a", 120) + _function("b", 90)})
    _init(root)
    (root / "src/forge/m.py").write_text(_function("a", 100) + _function("b", 40))
    assert ratchet.main(["--root", str(root), "--update"]) == 0
    assert _baseline(root)["functions"] == {"src/forge/m.py::a": 100}


def test_accept_records_a_reviewed_move(tmp_path):
    root = write_tree(tmp_path, {"src/forge/old.py": _function("big", 90)})
    _init(root)
    (root / "src/forge/old.py").unlink()
    (root / "src/forge/new.py").write_text(_function("big", 90))
    assert ratchet.main(["--root", str(root)]) == 1
    assert ratchet.main(["--root", str(root), "--update", "--accept", "src/forge/new.py"]) == 0
    assert ratchet.main(["--root", str(root)]) == 0


def test_only_the_effects_layer_may_name_the_write_port(tmp_path, capsys):
    root = write_tree(
        tmp_path,
        {
            "src/forge/ports.py": "class WritePort: ...\n",
            "src/forge/engine/effects.py": "from forge.ports import WritePort\n",
            "src/forge/engine/handlers.py": "from forge.ports import WritePort\n",
        },
    )
    _init(root)
    assert ratchet.main(["--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "src/forge/engine/handlers.py names WritePort" in out
    assert "effects.py names WritePort" not in out
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest -q tests/test_script_ratchet.py`
Expected: `ModuleNotFoundError: No module named 'ratchet'`.

- [ ] **Step 3: Create the ratchet**

```python
"""Size ratchet: oversized code may only shrink; new code stays small.

``scripts/ratchet-baseline.json`` lists only the current debt: files over
600 lines and functions over 80 lines under ``src/`` and
``packages/*/src/``. The check fails when:

- a listed file or function grew beyond its recorded size;
- any other file exceeds 600 lines, or any other function exceeds 80 lines;
- a module other than the allowed three names ``WritePort``
  (DESIGN §6.1: only the effects layer ever holds the write port).

Usage::

    uv run python scripts/ratchet.py                  # check (CI)
    uv run python scripts/ratchet.py --update         # lower entries that shrank
    uv run python scripts/ratchet.py --update --accept src/forge/project_config.py
                                                      # record a reviewed move of oversized code
    uv run python scripts/ratchet.py --init           # write the first baseline
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import _codebase as cb

BASELINE_NAME = "scripts/ratchet-baseline.json"
LIMITS = {"files": 600, "functions": 80}
WRITE_PORT = re.compile(r"\bWritePort\b")
WRITE_PORT_ALLOWED = ("forge/ports.py", "forge/engine/effects.py", "forge/api/app.py")

Sizes = dict[str, dict[str, int]]


def measure(root: Path) -> Sizes:
    files: dict[str, int] = {}
    functions: dict[str, int] = {}
    for src_dir in cb.source_dirs(root):
        for path in cb.iter_python_files(src_dir):
            rel = path.relative_to(root).as_posix()
            files[rel] = cb.count_lines(path)
            for size in cb.function_sizes(path):
                functions[f"{rel}::{size.qualname}"] = size.lines
    return {"files": files, "functions": functions}


def debt(current: Sizes) -> Sizes:
    return {
        kind: {key: lines for key, lines in current[kind].items() if lines > LIMITS[kind]}
        for kind in LIMITS
    }


def check(current: Sizes, baseline: Sizes) -> list[str]:
    problems: list[str] = []
    for kind, limit in LIMITS.items():
        for key, lines in sorted(current[kind].items()):
            allowed = baseline[kind].get(key, limit)
            if lines > allowed:
                what = "grew" if key in baseline[kind] else "exceeds the limit"
                problems.append(f"{kind[:-1]} {key} {what}: {lines} lines (allowed {allowed})")
    return problems


def update(current: Sizes, baseline: Sizes, accept: tuple[str, ...] = ()) -> Sizes:
    """Lower shrunk entries, drop repaid or vanished ones, record accepted moves."""
    updated: Sizes = {kind: {} for kind in LIMITS}
    for kind, limit in LIMITS.items():
        for key, old in baseline[kind].items():
            new = min(old, current[kind].get(key, 0))
            if new > limit:
                updated[kind][key] = new
        for key, lines in current[kind].items():
            if key.split("::", 1)[0] in accept and lines > limit:
                updated[kind][key] = lines
    return updated


def write_port_violations(root: Path) -> list[str]:
    problems = []
    for src_dir in cb.source_dirs(root):
        for path in cb.iter_python_files(src_dir):
            if path.relative_to(src_dir).as_posix() in WRITE_PORT_ALLOWED:
                continue
            if WRITE_PORT.search(path.read_text(encoding="utf-8")):
                problems.append(f"{path.relative_to(root)} names WritePort (only effects may)")
    return problems


def _save(path: Path, sizes: Sizes) -> None:
    path.write_text(json.dumps(sizes, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=cb.repo_root())
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--accept", action="append", default=[], metavar="PATH")
    args = parser.parse_args(argv)
    baseline_path = args.root / BASELINE_NAME
    current = measure(args.root)
    if args.init:
        _save(baseline_path, debt(current))
        print(f"ratchet: baseline written to {BASELINE_NAME}")
        return 0
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    if args.update:
        _save(baseline_path, update(current, baseline, tuple(args.accept)))
        print("ratchet: baseline updated")
        return 0
    problems = check(current, baseline) + write_port_violations(args.root)
    for problem in problems:
        print(f"ratchet: {problem}")
    if problems:
        print("ratchet: FAILED — split the code, or --update --accept PATH for a reviewed move")
        return 1
    debt_now = debt(current)
    print(
        f"ratchet: ok — debt {len(debt_now['files'])} files > {LIMITS['files']} lines, "
        f"{len(debt_now['functions'])} functions > {LIMITS['functions']} lines"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests again**

Run: `uv run pytest -q tests/test_script_ratchet.py`
Expected: `7 passed`.

- [ ] **Step 5: Record the baseline and check it**

Run: `uv run python scripts/ratchet.py --init && uv run python scripts/ratchet.py`
Expected: `ratchet: ok — debt 99 files > 600 lines, 322 functions > 80 lines`.
The baseline file is about 27 KB.

- [ ] **Step 6: Add the CI step**

In the `lint` job, after `Import contracts`, add:

```yaml
      - name: Size ratchet (oversized code may only shrink)
        run: uv run python scripts/ratchet.py
```

- [ ] **Step 7: Commit**

```bash
git add scripts/ratchet.py scripts/ratchet-baseline.json tests/test_script_ratchet.py .github/workflows/ci.yml
git commit -m "tools: size ratchet — oversized code may only shrink" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 11: Diff coverage (ROADMAP P0-08, part 2)

**Files:**
- Modify: `pyproject.toml`, `uv.lock`, `.github/workflows/ci.yml` (new `coverage` job)

- [ ] **Step 1: Add the tools**

Run: `uv add --dev "pytest-cov==7.1.0" "diff-cover==10.6.0"`
Expected: both appear under `[dependency-groups] dev`.

- [ ] **Step 2: Add the CI job**

Add before the `integration` job:

```yaml
  # Diff coverage: new and changed lines need at least 85% coverage.
  coverage:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Set up uv
        uses: astral-sh/setup-uv@v5

      - name: Install dependencies
        run: uv sync

      - name: Default tier with coverage
        run: uv run pytest -q -m "not slow" --cov=forge --cov-report=xml

      - name: Diff coverage (at least 85% of changed lines)
        run: >-
          uv run diff-cover coverage.xml
          --compare-branch=origin/${{ github.base_ref }} --fail-under=85
```

- [ ] **Step 3: Try it locally on this branch**

Run: `uv run pytest -q -m "not slow" --cov=forge --cov-report=xml && uv run diff-cover coverage.xml --compare-branch=origin/main --fail-under=85`
Expected: diff-cover reports the Task 7 lines in `config.py`, `validator.py`
and `doctor.py` as covered, at or above 85%. Do not commit `coverage.xml`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock .github/workflows/ci.yml
git commit -m "ci: diff coverage of at least 85% on changed lines" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 12: Prose lint (ROADMAP P0-09)

**Files:**
- Create: `scripts/prose_lint.py`, `tests/test_script_prose_lint.py`
- Modify: `.github/workflows/ci.yml` (lint job: full history and the new step)

- [ ] **Step 1: Write the failing tests**

```python
"""scripts/prose_lint.py: no review or issue archaeology in added source lines."""

from __future__ import annotations

from tests._scripts import load_script

prose_lint = load_script("prose_lint")

DIFF = """diff --git a/src/forge/x.py b/src/forge/x.py
--- a/src/forge/x.py
+++ b/src/forge/x.py
@@ -10,0 +11,4 @@
+# R42-16: the parity rule
+x = 1  # see ADR-0035
+# fixed in #377
+# as agreed in review `9e8d6c6`
"""


def test_review_ids_issue_refs_and_review_shas_are_rejected():
    found = prose_lint.violations(DIFF)
    assert [item.split(":")[1] for item in found] == ["11", "13", "14"]


def test_adr_references_are_allowed():
    assert not any("ADR-0035" in item for item in prose_lint.violations(DIFF))


def test_removed_lines_are_ignored():
    diff = "+++ b/src/forge/x.py\n@@ -3,1 +2,0 @@\n-# R42-16 old comment\n"
    assert prose_lint.violations(diff) == []


def test_a_missing_or_zero_base_is_a_no_op():
    assert prose_lint.main(["--base", ""]) == 0
    assert prose_lint.main(["--base", "0" * 40]) == 0
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest -q tests/test_script_prose_lint.py`
Expected: `ModuleNotFoundError: No module named 'prose_lint'`.

- [ ] **Step 3: Create the lint**

```python
"""Reject review, issue and commit archaeology in added source lines.

History belongs in git and ADRs, not in code comments. The check reads the
lines added under ``src/`` and ``packages/`` since ``--base`` and rejects:

- review item ids such as ``R42-16`` or ``Q35-09``;
- issue references such as ``#377``;
- "review <sha>" references.

``ADR-0035`` style references are allowed.

Usage::

    uv run python scripts/prose_lint.py --base origin/main
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterator

PATTERNS = (
    re.compile(r"\b[RQ]\d{2}-\d{2}\b"),
    re.compile(r"(?<![\w/&])#\d{3,}\b"),
    re.compile(r"\breview\s+`?[0-9a-f]{7,40}\b", re.IGNORECASE),
)
PATHS = ("src", "packages")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def added_lines(diff: str) -> Iterator[tuple[str, int, str]]:
    path = ""
    lineno = 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif match := HUNK.match(line):
            lineno = int(match.group(1))
        elif line.startswith("+") and path:
            yield path, lineno, line[1:]
            lineno += 1
        elif not line.startswith("-"):
            lineno += 1


def violations(diff: str) -> list[str]:
    found = []
    for path, lineno, text in added_lines(diff):
        if any(pattern.search(text) for pattern in PATTERNS):
            found.append(f"{path}:{lineno}: {text.strip()}")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="", help="git ref to diff against")
    args = parser.parse_args(argv)
    if not args.base.strip("0"):
        print("prose-lint: no base ref, nothing to check")
        return 0
    diff = subprocess.run(
        ["git", "diff", "--unified=0", "--no-color", f"{args.base}...HEAD", "--", *PATHS],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    found = violations(diff)
    for item in found:
        print(f"prose-lint: {item}")
    if found:
        print("prose-lint: FAILED — keep review and issue history in git and ADRs")
        return 1
    print("prose-lint: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests and the lint on this branch**

Run: `uv run pytest -q tests/test_script_prose_lint.py`
Expected: `4 passed`.

Run: `uv run python scripts/prose_lint.py --base origin/main`
Expected: `prose-lint: ok`. The Phase 0 changes add no review ids.

- [ ] **Step 5: Wire it into the lint job**

The `lint` job's checkout needs full history, and the step goes after
`Size ratchet`:

```yaml
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0 # the prose lint diffs against the base commit
      # … existing steps …
      - name: Prose lint (added lines)
        run: >-
          uv run python scripts/prose_lint.py
          --base "${{ github.event.pull_request.base.sha || github.event.before }}"
```

- [ ] **Step 6: Commit**

```bash
git add scripts/prose_lint.py tests/test_script_prose_lint.py .github/workflows/ci.yml
git commit -m "tools: prose lint — no review or issue ids in added source lines" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 13: Edge-case register (ROADMAP P0-12)

**Files:**
- Create: `tests/_nodeids.py`, `docs/reference/edge-cases.md`, `tests/test_edge_case_register.py`

- [ ] **Step 1: Write the failing test**

```python
"""Every edge-case register entry still names a real test or file."""

from __future__ import annotations

import re

import pytest

from tests._nodeids import REPO, nodeid_exists

REGISTER = REPO / "docs" / "reference" / "edge-cases.md"
ROW = re.compile(r"^\| (?P<ident>(?:EC|OP)-\d{2}) \|.*\| `(?P<target>[^`]+)` \|$")


def _rows() -> list[tuple[str, str]]:
    rows = []
    for line in REGISTER.read_text(encoding="utf-8").splitlines():
        if match := ROW.match(line):
            rows.append((match["ident"], match["target"]))
    return rows


def test_the_register_is_not_empty():
    assert len([ident for ident, _ in _rows() if ident.startswith("EC-")]) >= 10


@pytest.mark.parametrize(("ident", "target"), _rows(), ids=[i for i, _ in _rows()])
def test_every_entry_points_at_something_real(ident: str, target: str):
    if ident.startswith("EC-"):
        assert nodeid_exists(target), f"{ident}: pinning test {target} is gone"
    else:
        assert (REPO / target).exists(), f"{ident}: reference {target} is gone"


def test_the_node_id_check_rejects_a_missing_test():
    assert not nodeid_exists("tests/test_bot_loop.py::test_there_is_no_such_test")
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest -q tests/test_edge_case_register.py`
Expected: `ModuleNotFoundError: No module named 'tests._nodeids'`.

- [ ] **Step 3: Create the node-id helper**

```python
"""Static existence check for pytest node ids (no collection, no imports)."""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def nodeid_exists(nodeid: str, root: Path = REPO) -> bool:
    """True when ``path::Class::test`` names a real test function or class."""
    path, *names = nodeid.split("::")
    file = root / path
    if not file.is_file():
        return False
    scope: list[ast.stmt] = ast.parse(file.read_text(encoding="utf-8")).body
    for name in (n.split("[", 1)[0] for n in names):
        match = next(
            (
                node
                for node in scope
                if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name == name
            ),
            None,
        )
        if match is None:
            return False
        scope = match.body if isinstance(match, ast.ClassDef) else []
    return True
```

- [ ] **Step 4: Create the register**

```markdown
# Edge-case register

Hard-won gotchas, each pinned by a test or a runbook reference. Before a
module is deleted or rewritten (ROADMAP Phase 1 and Phase 3), its entries
here must still hold in the new code. `tests/test_edge_case_register.py`
fails when a pinning test disappears.

## Code-level entries (each pinned by a test)

| ID | Symptom | Rule | Pinning test |
| --- | --- | --- | --- |
| EC-01 | GitLab CE 18.x Commits API returns 400 when `start_branch` names an existing branch | `ensure_branch` owns branch creation; commits never pass `start_branch` | `tests/test_repository_writer.py::test_no_start_branch_when_branch_pre_exists` |
| EC-02 | npm `optionalDependencies` flake leaves a harness CLI in a silent futex hang | Install the platform binary explicitly | `tests/test_harness_entry.py::TestRenderGrokBuild::test_script_installs_the_platform_binary_explicitly` |
| EC-03 | The platform binary drifts from the pinned wrapper version | Pin the platform binary to the wrapper's version | `tests/test_harness_entry.py::TestDriverVersionPins::test_grok_platform_binary_follows_the_pinned_wrapper` |
| EC-04 | Two concurrent publishers create two Draft MRs | Create idempotently by intent; the database arbitrates uniqueness | `tests/production_entry/test_concurrent_publication.py::TestConcurrentDraftMrCreators::test_two_direct_concurrent_creators_collapse_to_one_mr` |
| EC-05 | forge's own notes contain command lines and could trigger forge | Bot-authored events are skipped | `tests/test_bot_loop.py::test_webhook_skips_bot_event` |
| EC-06 | GitHub workflow display names are not identities | Resolve the harness workflow by path | `tests/test_github_runs.py::TestPositiveVerification::test_harness_lane_is_excluded_by_path_not_display_name` |
| EC-07 | GitLab verification pipelines (`merge_request_event`) look like lane activity | Exclude them from coding occupancy | `tests/test_adaptive_lease_occupancy.py::TestBranchSearchCorrelation::test_verification_pipeline_is_not_coding_occupancy` |
| EC-08 | A lost publication response tempts a second write | Resolve the unknown outcome by marker-and-parent lookup | `tests/test_publication_intents.py::TestProbeDecisionTable::test_exactly_one_marker_and_parent_match_adopts` |
| EC-09 | A human commit lands on the factory branch during a review round | A foreign tip is a typed conflict and is never reverted | `tests/test_round_head_classification.py::TestForeignEffect::test_a_foreign_tip_above_the_own_effect_is_foreign` |
| EC-10 | A required resume over an unusable checkpoint could start a model session on stale work | Halt before any vendor session | `tests/test_application_regressions.py::TestR32ARestoredGenerationIsTheExecutionWorkspace::test_a_required_resume_that_cannot_land_halts_before_any_vendor_session` |

## Operational entries (no code path; follow the reference)

| ID | Symptom | Rule | Reference |
| --- | --- | --- | --- |
| OP-01 | Claude Code on z.ai treats a bare model id as a 200K context window | Use the `[1m]` suffix for the 1M window | `docs/harnesses/claude-code.md` |
| OP-02 | Redis `forge:worker:<id>` heartbeat keys outlive a dead worker by about 30 seconds | Check worker logs, not only `/metrics` | `AGENTS.md` |
| OP-03 | podman reports a 12-hex short image id; `inspect` reports the 64-hex id | Normalize ids before comparing them | `scripts/align_lab.py` |
| OP-04 | zsh does not word-split unquoted variables | Iterate with `while IFS= read -r` | `AGENTS.md` |
| OP-05 | `pytest` piped to `tail` hides the exit code | Run `set -o pipefail` first | `AGENTS.md` |
```

- [ ] **Step 5: Run the test again**

Run: `uv run pytest -q tests/test_edge_case_register.py`
Expected: `17 passed`.

- [ ] **Step 6: Commit**

```bash
git add tests/_nodeids.py tests/test_edge_case_register.py docs/reference/edge-cases.md
git commit -m "docs: the edge-case register, each entry pinned by a test or a reference" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 14: Test disposition ledger (ROADMAP P0-13)

**Files:**
- Create: `scripts/build_test_ledger.py`, `tests/test_script_build_test_ledger.py`
- Generate: `docs/roadmap/2026-09-29-simplification/test-ledger.csv`

- [ ] **Step 1: Write the failing tests**

```python
"""scripts/build_test_ledger.py: every test file gets a curated disposition."""

from __future__ import annotations

import csv

from tests._scripts import load_script, write_tree

ledger = load_script("build_test_ledger")

TREE = {
    "src/forge/__init__.py": "",
    "src/forge/main.py": "from forge import core\n",
    "src/forge/core.py": "X = 1\n",
    "src/forge/only_tests.py": "Y = 1\n",
    "tests/test_core.py": "from forge import core\n\n\ndef test_a():\n    assert core.X\n",
    "tests/test_only.py": "from forge import only_tests\n\n\ndef test_b():\n    assert only_tests.Y\n",
}


def _rows(root):
    with (root / ledger.LEDGER).open(encoding="utf-8", newline="") as handle:
        return {row["file"]: row for row in csv.DictReader(handle)}


def test_default_dispositions(tmp_path):
    root = write_tree(tmp_path, TREE)
    (root / ledger.LEDGER).parent.mkdir(parents=True)
    assert ledger.main(["--root", str(root)]) == 0
    rows = _rows(root)
    assert rows["tests/test_core.py"]["disposition"] == "keep"
    assert rows["tests/test_only.py"]["disposition"] == "drop-with-code"
    assert rows["tests/test_only.py"]["tests"] == "1"


def test_curated_columns_survive_regeneration(tmp_path):
    root = write_tree(tmp_path, TREE)
    (root / ledger.LEDGER).parent.mkdir(parents=True)
    ledger.main(["--root", str(root)])
    rows = list(_rows(root).values())
    rows[0].update(disposition="port-engine", phase="P3", note="owner decision")
    with (root / ledger.LEDGER).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ledger.FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    ledger.main(["--root", str(root)])
    kept = _rows(root)[rows[0]["file"]]
    assert (kept["disposition"], kept["phase"], kept["note"]) == (
        "port-engine",
        "P3",
        "owner decision",
    )
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest -q tests/test_script_build_test_ledger.py`
Expected: `ModuleNotFoundError: No module named 'build_test_ledger'`.

- [ ] **Step 3: Create the generator**

```python
"""Generate the test disposition ledger (ROADMAP P0-13).

One row per test file under ``tests/``. The measured columns are recomputed
on every run; the curated columns (``disposition``, ``phase``, ``note``)
are preserved for files already in the ledger.

Usage::

    uv run python scripts/build_test_ledger.py      # writes docs/roadmap/2026-09-29-simplification/test-ledger.csv
"""

from __future__ import annotations

import argparse
import ast
import csv
import sys
from pathlib import Path

import _codebase as cb

LEDGER = Path("docs/roadmap/2026-09-29-simplification/test-ledger.csv")
FIELDS = (
    "file",
    "tests",
    "loc",
    "imports_runtime",
    "imports_tests_only",
    "area",
    "disposition",
    "phase",
    "note",
)
SERVICES = ("forge.runs.service", "forge.runs.github_service", "forge.runs.azure_service")
PROVIDER_CLIENTS = ("forge.gitlab", "forge.integrations", "forge.repository.writer")
CURATED = ("disposition", "phase", "note")


def count_tests(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test")
    )


def area(rel: str) -> str:
    parts = Path(rel).parts
    if len(parts) > 2:
        return parts[1]
    stem = Path(rel).stem.removeprefix("test_")
    return stem.split("_", 1)[0]


def default_disposition(rel: str, imported: set[str], reach: cb.Reachability) -> tuple[str, str]:
    """A starting point for the owner to curate — never the final word.

    ``imported`` holds leaf modules only (packages are excluded), so a test
    that imports nothing but test-only or tooling-only code is recognised.
    """
    if imported and imported <= (reach.tests_only | reach.tooling_only):
        return "drop-with-code", "P1"
    if any(name.startswith(SERVICES) for name in imported) or "production_entry" in rel:
        return "port-engine", "P3"
    if any(name.startswith(PROVIDER_CLIENTS) for name in imported):
        return "port-adapter", "P3"
    return "keep", "-"


def build_rows(root: Path, previous: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    modules = cb.discover_modules(root)
    reach = cb.classify(root, modules)
    rows = []
    for path in sorted((root / "tests").rglob("test*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        imported = {
            m
            for m in cb.imports_of(path, modules)
            if m.startswith("forge") and modules[m].path.name != "__init__.py"
        }
        disposition, phase = default_disposition(rel, imported, reach)
        row = {
            "file": rel,
            "tests": str(count_tests(path)),
            "loc": str(cb.count_lines(path)),
            "imports_runtime": str(bool(imported & reach.runtime)).lower(),
            "imports_tests_only": str(bool(imported & reach.tests_only)).lower(),
            "area": area(rel),
            "disposition": disposition,
            "phase": phase,
            "note": "",
        }
        if rel in previous:
            row.update({key: previous[rel][key] for key in CURATED})
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=cb.repo_root())
    args = parser.parse_args(argv)
    target = args.root / LEDGER
    previous: dict[str, dict[str, str]] = {}
    if target.exists():
        with target.open(encoding="utf-8", newline="") as handle:
            previous = {row["file"]: row for row in csv.DictReader(handle)}
    rows = build_rows(args.root, previous)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["disposition"]] = counts.get(row["disposition"], 0) + 1
    print(f"test-ledger: {len(rows)} files → {LEDGER} {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests again**

Run: `uv run pytest -q tests/test_script_build_test_ledger.py`
Expected: `2 passed`.

- [ ] **Step 5: Generate the ledger**

Run: `uv run python scripts/build_test_ledger.py`
Expected: about `test-ledger: 300 files → … {'keep': ~182,
'port-adapter': 45, 'port-engine': 49, 'drop-with-code': 24}`. The exact
counts depend on the tests added by Tasks 1–13.

- [ ] **Step 6: Owner review (human step)**

The owner reviews every `drop-with-code` row and records the decision in
the `note` column. Re-running the generator keeps `disposition`, `phase` and
`note`.

- [ ] **Step 7: Commit**

```bash
git add scripts/build_test_ledger.py tests/test_script_build_test_ledger.py docs/roadmap/2026-09-29-simplification/test-ledger.csv
git commit -m "tools: the test disposition ledger" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 15: PR template, AGENTS.md, forge-setup skill (ROADMAP P0-14)

**Files:**
- Create: `.github/pull_request_template.md`
- Modify: `AGENTS.md`, `.claude/skills/forge-setup/SKILL.md`

- [ ] **Step 1: Create the PR template**

```markdown
## What and why

<!-- One paragraph. Link the issue and the ROADMAP task id (for example P1-03). -->

## Invariants (DESIGN §4)

- [ ] No invariant touched
- [ ] Touched: <!-- I1–I8 --> — evidence: <!-- test ids -->

## Metrics delta (`uv run python scripts/metrics.py`)

| Metric | Before | After |
| --- | --- | --- |
| `src/forge` lines | | |
| Largest file | | |
| Functions > 80 lines | | |

## ADR impact

- [ ] None
- [ ] Amends or supersedes: <!-- ADR ids; add the banner in the ADR -->

## Test ledger rows changed (`test-ledger.csv`)

<!-- file → disposition, or "none" -->

## Out of scope

- [ ] This PR changes nothing outside its task
```

- [ ] **Step 2: Update AGENTS.md**

````diff
diff --git a/AGENTS.md b/AGENTS.md
index e4a2938..0a0e007 100644
--- a/AGENTS.md
+++ b/AGENTS.md
@@ -53,8 +53,9 @@ annotated in `.env.example`):
 
 ## Non-negotiable rules for agents
 
-1. **Run the tests before committing**: `set -o pipefail && .venv/bin/python -m pytest -q`,
-   then `ruff format . && ruff check src tests`. Never commit with a red suite.
+1. **Run the tests before committing**: `set -o pipefail && .venv/bin/python -m pytest -q -m "not slow"`
+   (add `-m slow` for lifecycle changes), then `ruff format . && ruff check src tests`.
+   Never commit with a red suite.
 2. **Secrets never enter the repo or the conversation record.** `.env` is
    gitignored; tokens live in GitLab CI variables. If the user pastes a
    token, use it for the task, never echo it into files, commits, or tests.
@@ -65,6 +66,15 @@ annotated in `.env.example`):
    an ADR amendment instead of silently diverging.
 5. Prefer the smallest change that satisfies the request; match the code's
    existing style; add regression tests for every bug you fix.
+6. **Follow the simplification program** ([ADR-0035](docs/adr/0035-simplification-program.md)):
+   work items come from `docs/roadmap/2026-09-29-simplification/ROADMAP.md`; one task
+   per PR, about 500 net lines at most (pure deletions are exempt); fill in the PR template.
+7. **The guardrails are gates**: `uv run lint-imports`, `uv run python scripts/ratchet.py`
+   (oversized code may only shrink; new files ≤ 600 lines, new functions ≤ 80 lines) and
+   `uv run python scripts/prose_lint.py --base origin/main` (no review or issue ids in
+   code — history lives in git and ADRs) must pass.
+8. **Never weaken the Invariant Suite** (`tests/invariants/registry.py`): removing or
+   skipping evidence for I1–I8 needs an ADR.
 
 ## Repo map
 
@@ -80,8 +90,8 @@ src/forge/
   orchestrator/ legacy reactive flows (review, pipeline debug, chat)
   doctor.py    environment verification (this is your setup oracle)
 ci/templates/  harness CI jobs (claude-code, opencode, grok) + event filters
-tests/         2600+ tests; fixtures/fake_gitlab.py is the GitLab fake
-docs/adr/      architecture decisions (read 0001-0027 before redesigning)
+tests/         ~9.4K tests, tiered (docs/reference/testing.md); fixtures/fake_gitlab.py is the GitLab fake
+docs/adr/      architecture decisions 0000-0035 (start with 0003, 0005, 0016, 0026, 0035)
 ```
 
 ## Environment setup (dev)
@@ -89,8 +99,10 @@ docs/adr/      architecture decisions (read 0001-0027 before redesigning)
 ```bash
 uv sync                      # Python 3.13+, deps incl. dev
 set -o pipefail
-.venv/bin/python -m pytest -q # must be green before any commit
+.venv/bin/python -m pytest -q -m "not slow"   # default tier; must be green before any commit
+.venv/bin/python -m pytest -q -m slow         # real processes (production_entry, fi_os)
 .venv/bin/ruff format . && .venv/bin/ruff check src tests
+uv run lint-imports && uv run python scripts/ratchet.py
 ```
 
 No database or services are needed for the unit suite (fakes only).
````

- [ ] **Step 3: Update the forge-setup skill**

```diff
diff --git a/.claude/skills/forge-setup/SKILL.md b/.claude/skills/forge-setup/SKILL.md
index 9bb6413..ffe1754 100644
--- a/.claude/skills/forge-setup/SKILL.md
+++ b/.claude/skills/forge-setup/SKILL.md
@@ -9,13 +9,15 @@ description: Set up the forge dev environment from a fresh clone and verify it w
    If missing, install uv via the official installer; use the system Python
    3.13+ (pyenv/homebrew acceptable).
 2. `uv sync` from the repository root.
-3. Verify: `set -o pipefail && .venv/bin/python -m pytest -q` — expect ~815
-   passed, 1 skipped, 3 xfailed (xfails are pinned upstream defects; never
-   unpin them casually).
+3. Verify: `set -o pipefail && .venv/bin/python -m pytest -q -m "not slow"` — the
+   default tier must pass with zero failures (counts change as the
+   simplification program deletes code; `docs/reference/testing.md` explains
+   the tiers). xfails are pinned upstream defects; never unpin them casually.
 4. Lint: `.venv/bin/ruff format . && .venv/bin/ruff check src tests`.
 5. Final gate: `uv run python -m forge.doctor` — exit code 0. Without a
-   `.env`, the service checks (gitlab/redis/database/litellm) may WARN or
-   FAIL; that is expected for a pure-dev clone. What must pass: unit suite
+   `.env`, doctor prints FAIL lines for the missing settings (for example
+   `GITLAB_URL`) and the service checks (gitlab/redis/database/litellm) may
+   WARN or FAIL; that is expected for a pure-dev clone. What must pass: unit suite
    and lint. If the user provides a `.env`, `forge doctor` should go fully
    green (`--project <id>` adds target-project checks).
```

- [ ] **Step 4: Check that no stale numbers remain**

Run: `grep -n "2600+\|0001-0027\|~815" AGENTS.md .claude/skills/forge-setup/SKILL.md docs/README.md`
Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add .github/pull_request_template.md AGENTS.md .claude/skills/forge-setup/SKILL.md
git commit -m "docs: PR template and the agent working agreement for the simplification program" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 16: Phase 0 exit

**Files:**
- Modify: `docs/roadmap/2026-09-29-simplification/metrics.md`, `metrics-history.json`, `ROADMAP.md`

- [ ] **Step 1: Full local verification**

```bash
set -o pipefail
uv run ruff format --check . && uv run ruff check src tests scripts
uv run lint-imports
uv run python scripts/ratchet.py
uv run python scripts/prose_lint.py --base origin/main
uv run pytest -q -m "not slow" --junitxml=/tmp/default.xml | tail -3
uv run pytest -q -m slow | tail -3
```

Expected:
- every command succeeds;
- the default tier shows no failures and no leaked-process message;
- the slow tier passes (it needs `redis-server` on `PATH`).

- [ ] **Step 2: Record the measured suite duration**

Run: `uv run python scripts/metrics.py --junit /tmp/default.xml --write`
Expected: a second column in `metrics.md` with `Default suite seconds`
filled in. The scratch validation on a MacBook measured 533 s
(`9227 passed, 38 skipped`). The target is ≤ 480. If the value is above
480, run `uv run pytest -q -m "not slow" --durations=30`, add the paths of
the process-spawning tests to `SLOW_FILES` in `tests/_hygiene.py`, and do it
in a separate PR.

- [ ] **Step 3: Check that no orphans remain**

Run: `pgrep -fl "tests/production_entry/" || echo none`
Expected: `none`.

- [ ] **Step 4: Mark Phase 0 done in the roadmap**

In `ROADMAP.md`, under `## Phase 0`, add the line
`**Status:** done (<date>, <merge sha>); exit criteria recorded in metrics.md.`

- [ ] **Step 5: Commit and open the PR**

```bash
git add docs/roadmap/2026-09-29-simplification
git commit -m "docs(roadmap): Phase 0 exit — guardrails in place, metrics recorded" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

- [ ] **Step 6: After merge (the owner): the recovery tag**

```bash
git fetch origin
git tag pre-simplification origin/main
git push origin pre-simplification
```

Expected: the tag exists before any Phase 1 deletion lands.

## Self-review record

- **Spec coverage.** P0-01 → Task 1; P0-02 → Task 2 (+ Task 16 tag); P0-03
  → Task 3; P0-04 → Tasks 4–5; P0-05 → Task 5; P0-06 → Task 6; P0-07 → Task
  8; P0-08 → Tasks 10–11; P0-09 → Task 12; P0-10 → Task 9; P0-11 → Task 7;
  P0-12 → Task 13; P0-13 → Task 14; P0-14 → Task 15.
- **Deviations from ROADMAP wording, all deliberate:**
  - There is no `addopts` tier filter. It would silently deselect the
    explicit selections of `scripts/pg_gate.py`.
  - `FORGE_GITLAB_ENABLED` replaces a `FORGE_PROVIDERS` list, matching the
    existing `FORGE_GITHUB_ENABLED` and `FORGE_AZDO_ENABLED` flags.
  - Metrics are recorded at phase gates with `--write`, not by a weekly
    workflow (YAGNI).
  - The ledger script is `build_test_ledger.py`, so pytest can never
    collect a script named `test_*`.
- **Consistency.** Script names, CLI flags, marker names and file paths are
  identical in every task. `tests/_scripts.py` is created in Task 6, before
  its first use in Tasks 9–14.
