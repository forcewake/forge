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
| `src/forge/config.py`, `src/forge/gateway/validator.py`, `src/forge/gateway/router.py`, `src/forge/doctor.py`, `.env.example`, `docs/getting-started/github.md` | modified | Optional GitLab; ingress closed when off or unconfigured; doctor never raises |
| `tests/test_settings_gitlab_optional.py`, `tests/test_doctor_settings.py` | new tests | The above |
| `pyproject.toml`, `uv.lock` | modified | Markers, dev deps, import-linter contracts |
| `scripts/_codebase.py`, `scripts/metrics.py`, `tests/test_script_metrics.py`, `tests/test_script_codebase.py` | new | Shared analysis; metrics |
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
import signal
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
    # The parent reaps its child, so a guard that exits early leaves no zombie
    # behind that would still look alive to ``os.kill(pid, 0)``.
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]).wait(); time.sleep(60)"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_code])
    child = None
    try:
        _wait_for(lambda: pid_file.is_file() and pid_file.read_text(), 15, "child never started")
        child = int(pid_file.read_text())
        time.sleep(0.3)  # several guard polls: the guard must not exit on its own
        assert _alive(child), "the guard exited while its parent was alive"
        parent.kill()
        parent.wait(timeout=10)
        _wait_for(lambda: not _alive(child), 10, "the orphaned child outlived its parent")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)
        if child is not None:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
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

Design notes:

- There is no `addopts = -m "not slow"`. Scripts such as
  `scripts/pg_gate.py` invoke pytest with explicit selections, and an
  `addopts` filter would silently deselect them. CI passes `-m` explicitly
  instead.
- The leak check runs after fixture teardown (`trylast=True`), reports the
  command lines of what it kills, and only turns an otherwise green run
  red; interrupted or failed runs keep their exit code.
- The unit tests pass `only=` so they never kill another test's children.

- [ ] **Step 1: Write the failing tests**

```python
"""Tier markers by path, and the leaked-process check (tests/_hygiene.py)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from tests import _hygiene

needs_pgrep = pytest.mark.skipif(shutil.which("pgrep") is None, reason="pgrep is unavailable")
SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


def test_process_heavy_suites_are_slow():
    assert "slow" in _hygiene.tiers_for("tests/production_entry/test_x.py::test_a")
    assert "slow" in _hygiene.tiers_for("tests/fi_os/test_y.py::test_b")
    assert "slow" in _hygiene.tiers_for("tests/test_failure_injection_os.py::test_c")


def test_ordinary_tests_have_no_tier():
    assert _hygiene.tiers_for("tests/test_ordinary.py::TestThing::test_a") == set()


@needs_pgrep
def test_a_running_child_is_killed_and_reported_with_its_command():
    child = subprocess.Popen(SLEEPER)
    try:
        leaked = _hygiene.reap_and_kill_leaks(only={child.pid})
        assert list(leaked) == [child.pid]
        assert "time.sleep(60)" in leaked[child.pid]
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


@needs_pgrep
def test_a_finished_child_is_reaped_not_reported():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)  # exited, not yet reaped
    assert _hygiene.reap_and_kill_leaks(only={child.pid}) == {}
    child.wait(timeout=10)


@needs_pgrep
def test_children_outside_the_filter_are_left_alone():
    bystander = subprocess.Popen(SLEEPER)
    try:
        assert _hygiene.reap_and_kill_leaks(only=set()) == {}
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait(timeout=10)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest -q tests/test_hygiene.py`
Expected: collection error `cannot import name '_hygiene' from 'tests'`.

- [ ] **Step 3: Create the hygiene module**

```python
"""Test-suite hygiene: tier markers and leaked-process detection (DESIGN §7)."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
from collections.abc import Collection

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
    """Direct children of ``pid`` (default: this process); empty without ``pgrep``."""
    if shutil.which("pgrep") is None:
        return []
    result = subprocess.run(
        ["pgrep", "-P", str(pid or os.getpid())], capture_output=True, text=True, check=False
    )
    return [int(p) for p in result.stdout.split()]


def command_lines(pids: Collection[int]) -> dict[int, str]:
    """``pid → command line`` for running processes, read before anything is killed."""
    if not pids:
        return {}
    result = subprocess.run(
        ["ps", "-o", "pid=,args=", "-p", ",".join(str(p) for p in pids)],
        capture_output=True,
        text=True,
        check=False,
    )
    lines = (line.strip().split(maxsplit=1) for line in result.stdout.splitlines())
    return {int(parts[0]): (parts[1] if len(parts) > 1 else "") for parts in lines if parts}


def reap_and_kill_leaks(only: Collection[int] | None = None) -> dict[int, str]:
    """Reap finished children; kill and reap the running ones.

    Returns ``pid → command line`` of the killed processes. ``only`` limits the
    check to those pids, so a unit test never touches another test's children.
    """
    running = []
    for pid in child_pids():
        if only is not None and pid not in only:
            continue
        try:
            finished, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            continue
        if finished == 0:
            running.append(pid)
    leaked = command_lines(running)
    for pid in running:
        with contextlib.suppress(ProcessLookupError, ChildProcessError):
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        leaked.setdefault(pid, "")
    return leaked
```

- [ ] **Step 4: Run the tests again**

Run: `uv run pytest -q tests/test_hygiene.py`
Expected: `5 passed`.

- [ ] **Step 5: Wire the hooks into `tests/conftest.py`**

```diff
diff --git a/tests/conftest.py b/tests/conftest.py
index 109b67f..a440666 100644
--- a/tests/conftest.py
+++ b/tests/conftest.py
@@ -8,6 +8,7 @@ from pydantic import SecretStr
 from forge.config import Settings
 from forge.database import reset_engine
 from forge.main import create_app
+from tests import _hygiene
 
 # Suppress debug logs from libraries whose background threads can write to
 # stderr after pytest has already torn down the stream, causing
@@ -153,3 +154,26 @@ async def client(app) -> AsyncClient:
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
+@pytest.hookimpl(trylast=True)
+def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
+    """After fixture teardown, kill leaked child processes; fail an otherwise green run."""
+    leaked = _hygiene.reap_and_kill_leaks()
+    if not leaked:
+        return
+    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
+    if reporter is not None:
+        reporter.write_line("")
+        reporter.write_sep("!", "test hygiene: killed leaked child processes", red=True)
+        for pid, command in leaked.items():
+            reporter.write_line(f"  {pid} {command}", red=True)
+    if session.exitstatus == pytest.ExitCode.OK:
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
        run: uv run pytest -q -m "not slow" --durations=30
```

Then add this job right before the `integration` job:

```yaml
  # The slow tier: real processes and services (tests/production_entry,
  # tests/test_failure_injection_os.py). Not run on pull requests, except the
  # files the Invariant Suite registers (the invariants job) and pg_gate's
  # production-entry selection; pg_gate ignores the leaked-process check.
  tests-slow:
    if: github.event_name != 'pull_request'
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.13", "3.14"]
    steps:
      - name: Install redis-server (the durable-ingress matrix spawns the real binary)
        run: sudo apt-get update -q && sudo apt-get install -y -q redis-server
      - uses: actions/checkout@v4

      - name: Set up uv
        uses: astral-sh/setup-uv@v5
        with:
          python-version: ${{ matrix.python-version }}

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
| `uv run pytest -q -m "not slow"` | The default tier: fast, hermetic | Every pull request and push (`test` job, Python 3.13 and 3.14) |
| `uv run pytest -q -m slow` | Real processes and services: `tests/production_entry/` and `tests/test_failure_injection_os.py` (which drives `tests/fi_os/`) | Pushes to `main` and `release/**`, nightly and manual runs (`tests-slow` job, Python 3.13 and 3.14) |
| `uv run pytest -q $(uv run python -m tests.invariants.registry)` | The Invariant Suite I1–I8 | Every pull request (`invariants` job, with Postgres) |
| `uv run pytest -q` | Everything | Locally, before large changes |

Tiers are assigned by path in `tests/_hygiene.py`; the invariant files are
listed in `tests/invariants/registry.py`. On pull requests the
production-entry suites also run inside `scripts/pg_gate.py` (the
`integration` job).

## Subprocess hygiene

Tests that start helper processes must stop them in fixture teardown. Two
guards back this up:

- helper scripts under `tests/production_entry/` call
  `orphan_guard.install()`, so they exit when pytest dies abruptly;
- after fixture teardown, `pytest_sessionfinish` kills every direct child
  of pytest that is still running, prints its command line and fails an
  otherwise green run. It needs `pgrep`; without it the check is skipped.

Known gaps (ROADMAP P0-04):

- `scripts/pg_gate.py` does not fail on the leaked-process check yet, so a
  leak in the production-entry suites is caught after merge, by
  `tests-slow`;
- `redis-server` and the `python -m forge.worker` processes started by
  the slow suites are not guarded against an abrupt pytest exit.

Leftovers from older runs can be found and stopped:

```bash
pgrep -fl "tests/production_entry/"   # macOS; on Linux use pgrep -af
kill <pid>                            # for each pid listed above
```
````

- [ ] **Step 10: Run the default tier once**

Run: `set -o pipefail && uv run pytest -q -m "not slow" --durations=30 2>&1 | tail -40`
Expected: no failures (validated: `9155 passed, 38 skipped, 189 deselected`
in about 530 s), and no `test hygiene: killed leaked child processes` block.
If one appears, its command lines name the processes a fixture did not stop;
fix that fixture first.

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


def test_i1_to_i8_and_the_self_trigger_rule_are_registered():
    required = {f"I{n}" for n in range(1, 9)} | {"SELF-TRIGGER"}
    assert required <= {inv.ident for inv in INVARIANTS}


def test_every_invariant_names_evidence():
    assert all(inv.files for inv in INVARIANTS)


def test_registered_paths_are_clean_and_repo_relative():
    paths = [f for inv in INVARIANTS for f in inv.files]
    unclean = [
        f
        for f in paths
        if f != Path(f).as_posix() or not f.startswith("tests/") or ".." in Path(f).parts
    ]
    assert unclean == []


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

One registry rather than one module per invariant: most evidence already
lives in focused test files. ``tests/test_failure_injection_os.py`` is not
listed: it needs real workers and Redis and runs nightly in the
``integration-os`` job, while the ``invariants`` job fails on any skip.
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
        ("tests/test_bot_loop.py", "tests/test_github_webhook.py"),
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

Two mechanical checks: no source file contains a merge API call, and every
lane template disables push and holds no write credential.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]

# Each merge API, with one sample of how code would call it.
MERGE_APIS: dict[str, tuple[str, str]] = {
    "GitLab: accept a merge request": (
        r"/merge_requests/[^\"'\s]*/merge\b",
        'await self._put(f"/projects/{pid}/merge_requests/{iid}/merge")',
    ),
    "any REST path ending in /merge": (
        r"(?m)^(?!.*\brefs/).*?/merge[\"'?]",
        'await self._put(f"{mr_path}/merge")',
    ),
    "GitLab: merge when the pipeline succeeds": (
        r"\bmerge_when_pipeline_succeeds\b",
        '{"merge_when_pipeline_succeeds": True}',
    ),
    "GitLab: merge trains": (
        r"/merge_trains\b",
        'f"/projects/{pid}/merge_trains/merge_requests/1"',
    ),
    "GitLab GraphQL: accept a merge request": (
        r"\bmergeRequestAccept\b",
        "mutation { mergeRequestAccept(input: $input) { errors } }",
    ),
    "REST auto-merge flag": (r"\bauto_merge\b", '{"auto_merge": True}'),
    "auto-merge flag (DESIGN §4 spelling)": (r"\bautoMerge\b", '{"autoMerge": True}'),
    "GitHub REST: merge a pull request": (
        r"/pulls/[^\"'\s]*/merge\b",
        'await self._put(f"/repos/{repo}/pulls/{number}/merge")',
    ),
    "GitHub REST: merge a branch": (r"/merges\b", 'await self._post(f"/repos/{repo}/merges")'),
    "GitHub GraphQL: merge a pull request": (
        r"\bmergePullRequest\b",
        "mutation { mergePullRequest(input: $input) { clientMutationId } }",
    ),
    "GitHub GraphQL: enable auto-merge": (
        r"\benablePullRequestAutoMerge\b",
        "mutation { enablePullRequestAutoMerge(input: $input) { clientMutationId } }",
    ),
    "GitHub GraphQL: merge a branch": (
        r"\bmergeBranch\b",
        "mutation { mergeBranch(input: $input) { clientMutationId } }",
    ),
    "GitHub GraphQL: merge queue": (
        r"\benqueuePullRequest\b",
        "mutation { enqueuePullRequest(input: $input) { clientMutationId } }",
    ),
    "Azure DevOps: auto-complete": (r"\bautoCompleteSetBy\b", '{"autoCompleteSetBy": {"id": me}}'),
    "Azure DevOps: completion options": (
        r"\bcompletionOptions\b",
        '{"completionOptions": {"deleteSourceBranch": True}}',
    ),
    "Azure DevOps: complete a pull request": (
        r"[\"']status[\"']\s*:\s*[\"']completed[\"']",
        'await self._patch(f"{repo}/pullRequests/{pr}", json={"status": "completed"})',
    ),
    "CLI: merge a pull or merge request": (
        r"\b(?:gh\s+pr|glab\s+mr)\s+merge\b|[\"'](?:pr|mr)[\"']\s*,\s*[\"']merge[\"']",
        'subprocess.run(["gh", "pr", "merge", str(number)])',
    ),
    "CLI: complete an Azure DevOps pull request": (
        r"--auto-complete\b|--status[\"']?(?:\s*,\s*[\"']|\s+)completed\b",
        'run(["az", "repos", "pr", "update", "--status", "completed"])',
    ),
}
PATTERNS = {name: re.compile(pattern) for name, (pattern, _) in MERGE_APIS.items()}
# A pattern listed here only counts in files that also match the context:
# "status": "completed" is also the normal state of check runs and builds.
CONTEXT = {"Azure DevOps: complete a pull request": re.compile(r"(?i)pullrequests")}
# Reviewed exceptions: (path, API name) -> why it is not a merge. Never reword
# code to dodge a pattern; add an entry here instead.
ALLOWED: dict[tuple[str, str], str] = {}
NOT_MERGES = (
    'f"/projects/{pid}/merge_requests/{iid}/notes"',
    "await gitlab.list_merge_requests(project_id)",
    'f"/projects/{pid}/merge_requests/{iid}/merge_ref"',
    '{"merge_request_iid": iid, "status": "in_progress"}',
    '{"name": "ci", "status": "completed", "conclusion": "success"}',
    'source_branch = f"refs/pull/{number}/merge"',
    're.fullmatch(r"refs/merge-requests/(\\d+)/merge", ref)',
)
LANE_TEMPLATES = sorted(
    [
        *REPO.glob("ci/templates/*.yml"),
        REPO / ".github/workflows/forge-harness.yml",
        REPO / "forge-lane.azure-pipelines.yml",
    ]
)
GITHUB_TEMPLATES = [p for p in LANE_TEMPLATES if "github" in p.name or p.parent.name == "workflows"]
AZURE_TEMPLATES = [p for p in LANE_TEMPLATES if p.name.endswith(".azure-pipelines.yml")]
READ_ONLY = {"read", "none"}


def merge_apis_in(text: str) -> list[str]:
    return [
        name
        for name, pattern in PATTERNS.items()
        if pattern.search(text) and (name not in CONTEXT or CONTEXT[name].search(text))
    ]


def _mappings(node: Any) -> Iterator[dict[str, Any]]:
    """Every mapping in a parsed YAML document, depth first."""
    if isinstance(node, dict):
        yield node
        node = list(node.values())
    if isinstance(node, list):
        for child in node:
            yield from _mappings(child)


def _source_files() -> list[Path]:
    return sorted(
        p
        for p in (REPO / "src").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix not in {".pyc", ".pyo"}
    )


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


@pytest.mark.parametrize("name", sorted(MERGE_APIS))
def test_each_pattern_recognises_its_merge_call(name: str):
    assert name in merge_apis_in(MERGE_APIS[name][1])


@pytest.mark.parametrize("sample", NOT_MERGES)
def test_ordinary_merge_request_calls_are_not_flagged(sample: str):
    assert merge_apis_in(sample) == []


@pytest.mark.parametrize("path", _source_files(), ids=_rel)
def test_source_contains_no_merge_call(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    hits = [name for name in merge_apis_in(text) if (_rel(path), name) not in ALLOWED]
    assert hits == [], f"{_rel(path)} uses a merge API: {hits}"


@pytest.mark.parametrize("path", LANE_TEMPLATES, ids=_rel)
def test_lane_template_disables_push(path: Path):
    assert "git remote set-url --push origin FORBIDDEN" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", GITHUB_TEMPLATES, ids=_rel)
def test_github_lane_token_is_read_only(path: Path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    checkouts = [
        m for m in _mappings(doc) if str(m.get("uses", "")).startswith("actions/checkout@")
    ]
    assert checkouts, "no actions/checkout step found"
    assert all((m.get("with") or {}).get("persist-credentials") is False for m in checkouts)
    for name, job in doc["jobs"].items():
        permissions = job.get("permissions", doc.get("permissions"))
        if permissions == "read-all":
            continue
        assert isinstance(permissions, dict), f"job {name} does not pin its token permissions"
        assert set(permissions.values()) <= READ_ONLY, f"job {name}: {permissions}"


@pytest.mark.parametrize("path", AZURE_TEMPLATES, ids=_rel)
def test_azure_lane_holds_no_access_token(path: Path):
    text = path.read_text(encoding="utf-8")
    checkouts = [m for m in _mappings(yaml.safe_load(text)) if "checkout" in m]
    assert checkouts, "no checkout step found"
    assert all(m.get("persistCredentials") is False for m in checkouts)
    assert not re.search(r"system[._]accesstoken", text, re.IGNORECASE)
```

- [ ] **Step 5: Run the invariants package**

Run: `uv run pytest -q tests/invariants`
Expected: `307 passed`. This is characterization: the current code complies.
`test_each_pattern_recognises_its_merge_call` proves that no pattern is
vacuous, and `test_ordinary_merge_request_calls_are_not_flagged` guards
against false positives.

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
Expected: `6 passed`.

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
  # The registry selects the files; every test in them runs.
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
          uv run pytest -q --junitxml=invariants.xml
          $(uv run python -m tests.invariants.registry)

      - name: Every invariant test executed
        run: uv run python scripts/assert_no_skips.py invariants.xml
```

The suite is selected **by file** through `python -m tests.invariants.registry`,
without a `-m` filter, so every test in a registered file runs. A marker
filter could only drop tests silently, because deselected tests never appear
in the JUnit report. The `invariant` marker is for local selection.

- [ ] **Step 11: Run the suite locally**

Run: `uv run pytest -q --junitxml=/tmp/inv.xml $(uv run python -m tests.invariants.registry)`
Expected without Postgres: about `795 passed, 3 skipped`. The skips are the
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
- Modify: `src/forge/config.py`, `src/forge/gateway/validator.py`, `src/forge/gateway/router.py`, `src/forge/doctor.py`, `.env.example`, `docs/getting-started/github.md`

The flag follows the existing `FORGE_GITHUB_ENABLED` and
`FORGE_AZDO_ENABLED` convention: it defaults to `true`, so existing
deployments behave exactly as before, and when it is off the GitLab webhook
answers 503 even if a secret is still configured.

- [ ] **Step 1: Write the failing tests**

```python
"""GitLab settings are required only while GitLab is enabled."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from forge.config import Settings
from forge.database import reset_engine
from forge.gateway.validator import validate_webhook_token
from forge.main import create_app

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


def test_settings_errors_never_echo_secret_values(clean_env):
    with pytest.raises(ValidationError) as caught:
        Settings(GITLAB_URL="https://gitlab.test", GITLAB_TOKEN="glpat-do-not-print")
    assert "glpat-do-not-print" not in str(caught.value)


async def test_disabled_gitlab_ingress_refuses_even_a_matching_token(test_settings):
    settings = test_settings.model_copy(update={"FORGE_GITLAB_ENABLED": False})
    token = settings.GITLAB_WEBHOOK_SECRET.get_secret_value()
    reset_engine()
    app = create_app(settings=settings)
    try:
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/webhook",
                    headers={"X-Gitlab-Token": token, "X-Gitlab-Event": "Note Hook"},
                    json={},
                )
    finally:
        reset_engine()
    assert response.status_code == 503
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
    assert not report["checks"][0]["detail"].startswith("Value error")


async def test_disabled_gitlab_is_a_warning_not_a_network_call(empty_env):
    empty_env.setenv("FORGE_GITLAB_ENABLED", "false")
    results = await doctor.check_gitlab_identity(Settings())
    assert [(r.name, r.status) for r in results] == [("gitlab.token", doctor.WARN)]


async def test_project_checks_refuse_when_gitlab_is_disabled(empty_env):
    empty_env.setenv("FORGE_GITLAB_ENABLED", "false")
    results = await doctor.check_gitlab_project(Settings(), 68)
    assert [(r.name, r.status) for r in results] == [("project", doctor.FAIL)]
    assert "FORGE_GITLAB_ENABLED=false" in results[0].detail
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest -q tests/test_settings_gitlab_optional.py tests/test_doctor_settings.py`
Expected failures:
- the settings tests: the validation message differs, and
  `FORGE_GITLAB_ENABLED` does not exist yet;
- the validator test: `validate_webhook_token("", "")` returns instead of
  raising 503, and a disabled GitLab still accepts a matching token (202);
- the secret-echo test: the validation error contains the token;
- the doctor tests: the `ValidationError` traceback propagates, and
  `check_gitlab_identity` and `check_gitlab_project` do not exist.

- [ ] **Step 3: Make GitLab settings optional behind the flag**

```diff
diff --git a/src/forge/config.py b/src/forge/config.py
index 08693c6..01f041b 100644
--- a/src/forge/config.py
+++ b/src/forge/config.py
@@ -6,7 +6,7 @@ from pathlib import Path
 from typing import Any
 
 import yaml
-from pydantic import SecretStr
+from pydantic import SecretStr, model_validator
 from pydantic_settings import BaseSettings, SettingsConfigDict
 
 from forge.repository.changeset import BUILTIN_WRITE_PROFILES
@@ -15,14 +15,37 @@ from forge.repository.changeset import BUILTIN_WRITE_PROFILES
 class Settings(BaseSettings):
     """Core settings loaded from environment / .env file."""
 
-    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
-
-    # GitLab connection
-    GITLAB_URL: str
-    GITLAB_TOKEN: SecretStr
-    GITLAB_WEBHOOK_SECRET: SecretStr
+    # hide_input_in_errors: a validation error must never echo a token.
+    model_config = SettingsConfigDict(
+        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
+    )
+
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

- [ ] **Step 4: Fail closed when GitLab ingress is off or unconfigured**

```diff
diff --git a/src/forge/gateway/router.py b/src/forge/gateway/router.py
index dbb3929..16ab557 100644
--- a/src/forge/gateway/router.py
+++ b/src/forge/gateway/router.py
@@ -774,8 +774,9 @@ async def webhook(
 ) -> dict[str, Any]:
     """Accept GitLab webhook events."""
     settings = request.app.state.settings
+    gitlab_enabled = getattr(settings, "FORGE_GITLAB_ENABLED", True)
     validate_webhook_token(
-        expected_secret=settings.GITLAB_WEBHOOK_SECRET.get_secret_value(),
+        expected_secret=settings.GITLAB_WEBHOOK_SECRET.get_secret_value() if gitlab_enabled else "",
         x_gitlab_token=x_gitlab_token,
     )
 
diff --git a/src/forge/gateway/validator.py b/src/forge/gateway/validator.py
index 2d184a7..4eb1492 100644
--- a/src/forge/gateway/validator.py
+++ b/src/forge/gateway/validator.py
@@ -18,7 +18,9 @@ def validate_webhook_token(
     expected_secret: str,
     x_gitlab_token: str | None = Header(None),
 ) -> str:
-    """Raise 401 if the token is missing or does not match."""
+    """Raise 503 if GitLab ingress is off or unconfigured, 401 if the token is missing or wrong."""
+    if not expected_secret:
+        raise HTTPException(status_code=503, detail="GitLab ingress is disabled or not configured")
     if x_gitlab_token is None:
         raise HTTPException(status_code=401, detail="Missing X-Gitlab-Token header")
     if not hmac.compare_digest(x_gitlab_token, expected_secret):
```

- [ ] **Step 5: Doctor reports instead of raising**

```diff
diff --git a/src/forge/doctor.py b/src/forge/doctor.py
index 87569ca..a27ee5f 100644
--- a/src/forge/doctor.py
+++ b/src/forge/doctor.py
@@ -36,6 +36,7 @@ from dataclasses import asdict, dataclass
 from typing import Any
 
 import httpx
+from pydantic import ValidationError
 
 from forge.config import Settings
 from forge.gitlab.client import GitLabClient, GitLabAPIError
@@ -97,6 +98,30 @@ async def check_bot_token(settings: Settings) -> CheckResult:
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
+        results.append(CheckResult(name, FAIL, error["msg"].removeprefix("Value error, ")))
+    return results
+
+
+async def check_gitlab_project(settings: Settings, project_id: int) -> list[CheckResult]:
+    """Target-project checks, or one failure when GitLab is disabled."""
+    if not getattr(settings, "FORGE_GITLAB_ENABLED", True):
+        return [CheckResult("project", FAIL, "--project needs GitLab; FORGE_GITLAB_ENABLED=false")]
+    return await check_project(settings, project_id)
+
+
 async def check_redis(settings: Settings) -> CheckResult:
     from forge.utils.redis_client import RedisManager
 
@@ -1028,8 +1053,7 @@ async def run_checks(settings: Settings, project_id: int | None = None) -> list[
     results = [
         CheckResult("python.version", PASS, f"{sys.version_info.major}.{sys.version_info.minor}")
     ]
-    results.append(await check_gitlab(settings))
-    results.append(await check_bot_token(settings))
+    results.extend(await check_gitlab_identity(settings))
     results.append(await check_redis(settings))
     results.append(await check_database(settings))
     results.append(await check_litellm(settings))
@@ -1043,7 +1067,7 @@ async def run_checks(settings: Settings, project_id: int | None = None) -> list[
     # earlier ordering guarantees stay untouched.
     results.extend(await check_checkpoint_authority(settings))
     if project_id is not None:
-        results.extend(await check_project(settings, project_id))
+        results.extend(await check_gitlab_project(settings, project_id))
     # NXT-02: offline honesty check, appended last so existing ordering
     # guarantees (python.version, gitlab.token first) are untouched.
     results.append(check_capabilities())
@@ -1100,8 +1124,12 @@ def main(argv: list[str] | None = None) -> int:
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
index 8fd5fb1..d72afa7 100644
--- a/.env.example
+++ b/.env.example
@@ -1,4 +1,7 @@
 # GitLab
+# GitLab is enabled by default. A GitHub-only deployment sets FORGE_GITLAB_ENABLED=false
+# and deletes or blanks the three GITLAB_* values; GitLab webhooks then get 503.
+FORGE_GITLAB_ENABLED=true
 GITLAB_URL=https://gitlab.example.com
 GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
 GITLAB_WEBHOOK_SECRET=change-me-to-a-random-string
diff --git a/docs/getting-started/github.md b/docs/getting-started/github.md
index ab96cf9..91c29c6 100644
--- a/docs/getting-started/github.md
+++ b/docs/getting-started/github.md
@@ -57,6 +57,7 @@ Add to forge's environment (`.env` / container env):
 
 ```bash
 FORGE_GITHUB_ENABLED=true
+FORGE_GITLAB_ENABLED=false                       # GitHub-only: no GITLAB_* settings needed
 FORGE_GITHUB_WEBHOOK_SECRET=<same value as the App webhook secret>
 FORGE_GITHUB_APP_ID=<client id or app id>
 FORGE_GITHUB_PRIVATE_KEY=/path/to/app.pem        # PEM text or a file path
```

- [ ] **Step 7: Run the new and the neighbouring tests**

Run: `uv run pytest -q tests/test_settings_gitlab_optional.py tests/test_doctor_settings.py tests/test_doctor.py tests/test_bot_loop.py`
Expected: `43 passed`.

Run: `cd "$(mktemp -d)" && uv run --project "$OLDPWD" python -m forge.doctor; cd "$OLDPWD"`
Expected: `FAIL settings  GITLAB_URL, GITLAB_TOKEN, GITLAB_WEBHOOK_SECRET must be set while FORGE_GITLAB_ENABLED is true`
and exit code 1, with no traceback.

- [ ] **Step 8: Typecheck the blocking set**

Run: `uv run mypy src/forge/durable src/forge/runs src/forge/repository src/forge/factory src/forge/execution src/forge/integrations`
Expected: `Success: no issues found in 49 source files`. The field types are
unchanged (`str` and `SecretStr`).

- [ ] **Step 9: Commit**

```bash
git add src/forge/config.py src/forge/gateway/validator.py src/forge/gateway/router.py src/forge/doctor.py .env.example docs/getting-started/github.md tests/test_settings_gitlab_optional.py tests/test_doctor_settings.py
git commit -m "feat(config): GitLab settings optional behind FORGE_GITLAB_ENABLED; doctor never raises" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 8: Import contracts (ROADMAP P0-07)

**Files:**
- Create: `tests/test_import_contract_debt.py`
- Modify: `pyproject.toml` (dev dependency, `[tool.importlinter]`), `uv.lock`, `.github/workflows/ci.yml` (lint step)

- [ ] **Step 1: Add the tool**

Run: `uv add --dev "import-linter==2.15"`
Expected: `pyproject.toml` lists `"import-linter==2.15"` under
`[dependency-groups] dev`, and `uv.lock` is updated.

- [ ] **Step 2: Add the contracts at the end of `pyproject.toml`**

```toml

# ADR-0035 guardrail: mechanical import boundaries. The ignore lists record
# existing debt and may only shrink (ROADMAP P0-07;
# tests/test_import_contract_debt.py counts them).
[tool.importlinter]
root_package = "forge"

[[tool.importlinter.contracts]]
name = "durable does not import runs, adaptive, gateway or integrations"
type = "forbidden"
source_modules = ["forge.durable"]
forbidden_modules = ["forge.runs", "forge.adaptive", "forge.gateway", "forge.integrations"]
# Lazy imports inside durable/budgets.py functions. Each entry covers every
# import between the two modules, and importing forge.runs.candidate loads
# forge.runs (RunService) at runtime.
ignore_imports = [
    "forge.durable.budgets -> forge.adaptive.usage_ingestion",
    "forge.durable.budgets -> forge.runs.candidate",
]

[[tool.importlinter.contracts]]
name = "harnesses do not import forge.runs"
type = "forbidden"
source_modules = ["forge.harnesses"]
# The whole package: forge/runs/__init__.py loads RunService, which imports
# forge.harnesses, so any runs import from here would create a cycle.
forbidden_modules = ["forge.runs"]
```

The two `ignore_imports` entries are the only current violations: lazy
imports in `durable/budgets.py` at lines 1362 and 1437. The harnesses
contract forbids all of `forge.runs`, because `forge/runs/__init__.py`
loads `RunService`, which imports `forge.harnesses`.

- [ ] **Step 2b: Count the debt**

The ignore list may only shrink. Create `tests/test_import_contract_debt.py`:

```python
"""The import-contract debt (``ignore_imports``) may only shrink (ROADMAP P0-07)."""

from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
# Lower this when an entry is removed. Raising it needs an ADR.
MAX_IGNORED_IMPORTS = 2


def test_ignored_imports_only_shrink():
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    contracts = config["tool"]["importlinter"]["contracts"]
    ignored = [entry for contract in contracts for entry in contract.get("ignore_imports", [])]
    assert len(ignored) <= MAX_IGNORED_IMPORTS, ignored
```

Run: `uv run pytest -q tests/test_import_contract_debt.py`
Expected: `1 passed`.

- [ ] **Step 3: Run the contracts**

Run: `uv run lint-imports`
Expected: `Contracts: 2 kept, 0 broken.`

- [ ] **Step 4: Prove that the contract bites**

Temporarily delete the first `ignore_imports` entry and run
`uv run lint-imports`. Expected: `BROKEN` with
`forge.durable.budgets -> forge.adaptive.usage_ingestion (l.1362)`. Restore
the entry.

Then temporarily append `from forge.runs import candidate  # noqa: F401` to
`src/forge/harnesses/mcp.py` and run `uv run lint-imports`. Expected: the
harnesses contract is `BROKEN`. Restore the file with
`git checkout -- src/forge/harnesses/mcp.py`.

- [ ] **Step 5: Add the CI step**

In the `lint` job, after `Ruff format check`, add:

```yaml
      - name: Import contracts
        run: uv run lint-imports
```

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock .github/workflows/ci.yml tests/test_import_contract_debt.py
git commit -m "build: import-linter contracts as a CI gate (ratchet mode)" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 9: Shared analysis and the metrics script (ROADMAP P0-10)

**Files:**
- Create: `scripts/_codebase.py`, `scripts/metrics.py`, `tests/test_script_metrics.py`, `tests/test_script_codebase.py`
- Generate: `docs/roadmap/2026-09-29-simplification/metrics-history.json`, the measured block in `metrics.md`

- [ ] **Step 1: Write the failing tests**

```python
"""scripts/metrics.py: the simplification-program measurements."""

from __future__ import annotations

import json

import pytest

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


def test_breakdown_adds_up_and_the_lane_package_is_counted_separately(tmp_path):
    tree = {**TREE, "packages/forge-lane/src/forge_lane/__init__.py": "A = 1\nB = 2\n"}
    snapshot = metrics.measure(write_tree(tmp_path, tree))
    parts = ("runtime_lines", "tooling_only_lines", "tests_only_lines", "unreached_lines")
    assert sum(snapshot[key] for key in parts) == snapshot["src_lines"]
    assert snapshot["forge_lane_lines"] == 2


def test_who_imports_leaves_out_the_target_package_itself(tmp_path):
    tree = {
        **TREE,
        "src/forge/pkg/__init__.py": "from forge.pkg import a\n",
        "src/forge/pkg/a.py": "",
        "src/forge/user.py": "from forge import pkg\n",
    }
    assert metrics.who_imports(write_tree(tmp_path, tree), "forge.pkg") == ["forge.user"]


def test_write_is_idempotent_per_commit_and_refuses_broken_markers(tmp_path):
    root = write_tree(tmp_path, {metrics.METRICS_FILE.as_posix(): "# Metrics\n"})
    metrics.write(root, {"date": "2026-09-30", "sha": "abc1234", "src_lines": 10})
    metrics.write(root, {"date": "2026-09-30", "sha": "abc1234", "src_lines": 9})
    assert len(json.loads((root / metrics.HISTORY_FILE).read_text())) == 1
    assert "| `src/forge` lines | 9 |" in (root / metrics.METRICS_FILE).read_text()
    (root / metrics.METRICS_FILE).write_text(f"# Metrics\n{metrics.BEGIN}\n")
    with pytest.raises(ValueError):
        metrics.write(root, {"date": "2026-10-01", "sha": "def5678", "src_lines": 8})
    assert len(json.loads((root / metrics.HISTORY_FILE).read_text())) == 1


def test_the_sha_is_unknown_without_git(tmp_path, monkeypatch):
    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(metrics.subprocess, "run", no_git)
    assert metrics._git_sha(tmp_path) == "unknown"


def test_suite_seconds_reads_a_pytest_junit_report(tmp_path):
    report = tmp_path / "report.xml"
    report.write_text(
        '<testsuites name="pytest tests"><testsuite name="pytest" time="12.5"/></testsuites>'
    )
    assert metrics._suite_seconds(report) == 12.5
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest -q tests/test_script_metrics.py`
Expected: `ModuleNotFoundError: No module named 'metrics'`.

- [ ] **Step 2b: Write the failing tests for the shared analysis**

```python
"""scripts/_codebase.py: the static analysis shared by the program's scripts."""

from __future__ import annotations

import pytest

from tests._scripts import load_script, write_tree

cb = load_script("_codebase")

PACKAGE = {
    "src/forge/__init__.py": "",
    "src/forge/top.py": "X = 1\n",
    "src/forge/pkg/__init__.py": "from . import sibling\nfrom .. import top\n",
    "src/forge/pkg/sibling.py": "Y = 1\n",
    "src/forge/pkg/deep.py": "",
    "src/forge/pkg/other.py": "",
    "src/forge/pkg/mod.py": (
        "import importlib\n"
        "from .sibling import Y\n"
        "from ..top import X\n"
        "def lazy():\n"
        "    from forge.pkg import deep\n"
        "    return importlib.import_module('forge.pkg.other')\n"
    ),
}


@pytest.fixture()
def modules(tmp_path):
    return cb.discover_modules(write_tree(tmp_path, PACKAGE))


def test_package_level_relative_imports_resolve_with_parents(modules):
    imported = cb.imports_of(modules["forge.pkg"].path, modules, "forge.pkg")
    assert imported == {"forge", "forge.pkg", "forge.pkg.sibling", "forge.top"}


def test_module_level_relative_lazy_and_literal_dynamic_imports(modules):
    imported = cb.imports_of(modules["forge.pkg.mod"].path, modules, "forge.pkg.mod")
    assert {"forge.pkg.sibling", "forge.top", "forge.pkg.deep", "forge.pkg.other"} <= imported


def test_function_sizes_cover_nested_conditional_and_duplicate_definitions(tmp_path):
    source = (
        "def outer():\n"
        "    def inner():\n"
        "        return 1\n"
        "    return inner\n"
        "class A:\n"
        "    class B:\n"
        "        def m(self):\n"
        "            return 2\n"
        "    @property\n"
        "    def p(self):\n"
        "        return 3\n"
        "    @p.setter\n"
        "    def p(self, value):\n"
        "        pass\n"
        "if True:\n"
        "    def guarded():\n"
        "        pass\n"
        "try:\n"
        "    def in_try():\n"
        "        pass\n"
        "except ImportError:\n"
        "    def in_try():\n"
        "        pass\n"
    )
    path = write_tree(tmp_path, {"m.py": source}) / "m.py"
    sizes = {size.qualname: size.lines for size in cb.function_sizes(path)}
    assert sizes == {
        "outer": 4,
        "outer.inner": 2,
        "A.B.m": 2,
        "A.p": 2,
        "A.p#2": 2,
        "guarded": 2,
        "in_try": 2,
        "in_try#2": 2,
    }


def test_syntax_errors_name_the_file(tmp_path):
    path = write_tree(tmp_path, {"broken.py": "def f(:\n"}) / "broken.py"
    with pytest.raises(SyntaxError) as caught:
        cb.function_sizes(path)
    assert caught.value.filename == str(path)


def test_prose_lines_count_comments_and_docstrings(tmp_path):
    source = '"""Module doc\nsecond line."""\n# a comment\nx = 1  # trailing\n'
    source += 'def f():\n    """One line."""\n'
    path = write_tree(tmp_path, {"m.py": source}) / "m.py"
    assert cb.prose_lines(path) == 5


@pytest.mark.parametrize(
    ("text", "hits"),
    [
        ("# R42-16: the parity rule", 1),
        ("# Q35-09, NXT-10, NEXT-05 and AT-08", 4),
        ("# fixed in #377", 1),
        ("# as agreed in review `9e8d6c6`", 1),
        ("# ADR-0035 decides this", 0),
        ('url = "https://example.test/page#123"', 0),
        ('html = "&#123;"', 0),
        ('color = "#abc"', 0),
    ],
)
def test_review_reference_pattern(text: str, hits: int):
    assert len(cb.REVIEW_REF.findall(text)) == hits


def test_console_scripts_are_tooling_roots(tmp_path):
    root = write_tree(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "x"\n[project.scripts]\nforge = "forge.cli:main"\n',
            "src/forge/__init__.py": "",
            "src/forge/cli.py": "import importlib\n",
            "tests/test_cli.py": "from forge import cli\n",
        },
    )
    reach = cb.classify(root, cb.discover_modules(root))
    assert "forge.cli" in reach.tooling_only
    assert "forge.cli" not in reach.tests_only


def test_declared_roots_that_do_not_exist_are_reported(tmp_path):
    root = write_tree(tmp_path, {"src/forge/__init__.py": "", "src/forge/main.py": ""})
    reach = cb.classify(root, cb.discover_modules(root))
    assert "forge.main" not in reach.missing_roots
    assert "forge.migrate" in reach.missing_roots
    assert "forge_lane" not in reach.missing_roots
```

Run: `uv run pytest -q tests/test_script_codebase.py`
Expected: `ModuleNotFoundError: No module named '_codebase'`.

- [ ] **Step 3: Create the shared analysis module**

```python
"""Shared static analysis of the forge source tree (stdlib only).

Used by ``scripts/metrics.py``, ``scripts/ratchet.py`` and
``scripts/build_test_ledger.py``. Everything here reads files; nothing imports
forge, so the scripts run without the project environment.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
import tomllib
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

CONTROL_PLANE_ROOTS = ("forge.main", "forge.worker.__main__", "forge.worker", "forge.migrate")
LANE_ROOTS = ("forge.harness_entry", "forge.lane_driver", "forge.candidate_collector", "forge_lane")
# Declared roots that do not exist yet; their absence is expected.
FUTURE_ROOTS = frozenset({"forge_lane"})
TOOLING_ROOTS = (
    "forge.doctor",
    "forge.release_manifest",
    "forge.release_promotion",
    "forge.profile_qualification",
    "forge.capability_manifest",
)
# History that belongs in git and ADRs, not in code: review, backlog and
# acceptance item ids, issue references and "review <sha>". Short forms such as
# R17 or A12 are too ambiguous to count, so this is a lower bound.
REVIEW_REF = re.compile(
    r"\b(?:[RQ]\d{2}-\d{2}|NE?XT-\d{2,3}|AT-\d{2})\b"
    r"|(?<![\w/&])#\d{3,}\b"
    r"|\b[Rr]eview\s+`?[0-9a-f]{7,40}\b"
)
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
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
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
    for node in ast.walk(ast.parse(source, filename=str(path))):
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
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
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


def console_script_modules(root: Path) -> set[str]:
    """Modules behind ``[project.scripts]`` in the root and workspace packages."""
    found: set[str] = set()
    for pyproject in [
        root / "pyproject.toml",
        *sorted((root / "packages").glob("*/pyproject.toml")),
    ]:
        if pyproject.is_file():
            config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            scripts = config.get("project", {}).get("scripts", {})
            found |= {target.split(":", 1)[0] for target in scripts.values()}
    return found


@dataclass(frozen=True)
class Reachability:
    runtime: set[str]
    lane: set[str]
    tooling_only: set[str]
    tests_only: set[str]
    unreached: set[str]
    missing_roots: tuple[str, ...] = ()


def classify(root: Path, modules: dict[str, Module]) -> Reachability:
    graph = import_graph(modules)
    control = reachable(CONTROL_PLANE_ROOTS, graph)
    lane = reachable(LANE_ROOTS, graph)
    runtime = control | lane
    tooling_roots = {*TOOLING_ROOTS, *console_script_modules(root)}
    script_roots = roots_from_files((root / "scripts").glob("*.py"), modules)
    tools_roots = roots_from_files((root / "tools").rglob("*.py"), modules)
    tooling = reachable((*tooling_roots, *script_roots, *tools_roots), graph) - runtime
    test_roots = roots_from_files((root / "tests").rglob("*.py"), modules)
    tests_only = reachable(test_roots, graph) - runtime - tooling
    unreached = set(modules) - runtime - tooling - tests_only
    declared = (*CONTROL_PLANE_ROOTS, *LANE_ROOTS, *sorted(tooling_roots))
    missing = tuple(r for r in declared if r not in modules and r not in FUTURE_ROOTS)
    return Reachability(runtime, lane, tooling, tests_only, unreached, missing)


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
    ("unreached_lines", "Unreached lines"),
    ("lane_reachable_lines", "Lines reachable from lane entry points"),
    ("forge_lane_lines", "`forge-lane` lines"),
    ("missing_roots", "Declared entry points not found"),
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
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "unknown"
    return result.stdout.strip() or "unknown"


def _suite_seconds(junit: Path | None) -> float | None:
    if junit is None:
        return None
    root = ET.parse(junit).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    return round(sum(float(s.get("time", "0")) for s in suites), 1)


def _direct_dependencies(root: Path) -> int:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return len(data["project"]["dependencies"])


def _docs_words(root: Path) -> int:
    return sum(
        len(path.read_text(encoding="utf-8", errors="replace").split())
        for path in (root / "docs").rglob("*.md")
    )


def _in_package(name: str, package: str) -> bool:
    return name == package or name.startswith(package + ".")


def measure(root: Path, junit: Path | None = None) -> dict[str, object]:
    modules = cb.discover_modules(root)
    forge_modules = {n: m for n, m in modules.items() if _in_package(n, "forge")}
    forge_names = set(forge_modules)
    lane_package = {n for n in modules if _in_package(n, "forge_lane")}
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
        "runtime_lines": cb.total_lines(reach.runtime & forge_names, modules),
        "tooling_only_lines": cb.total_lines(reach.tooling_only & forge_names, modules),
        "tests_only_lines": cb.total_lines(reach.tests_only & forge_names, modules),
        "unreached_lines": cb.total_lines(reach.unreached & forge_names, modules),
        "lane_reachable_lines": cb.total_lines(reach.lane & forge_names, modules),
        "forge_lane_lines": cb.total_lines(lane_package, modules),
        "missing_roots": ", ".join(reach.missing_roots) or "none",
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
    metrics_path = root / METRICS_FILE
    text = metrics_path.read_text(encoding="utf-8")
    if (BEGIN in text) != (END in text):
        raise ValueError(f"{METRICS_FILE}: unbalanced {BEGIN} / {END} markers")
    history_path = root / HISTORY_FILE
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else []
    if history and history[-1].get("sha") == snapshot.get("sha"):
        history[-1] = snapshot  # re-measuring the same commit replaces its entry
    else:
        history.append(snapshot)
    block = f"{BEGIN}\n{render_history(history)}\n{END}"
    if BEGIN in text:
        text = re.sub(re.escape(BEGIN) + ".*?" + re.escape(END), lambda _: block, text, flags=re.S)
    else:
        text = text.rstrip() + "\n\n## Measured history\n\n" + block + "\n"
    history_path.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")
    metrics_path.write_text(text, encoding="utf-8")


def who_imports(root: Path, target: str) -> list[str]:
    modules = cb.discover_modules(root)
    graph = cb.import_graph(modules)
    return sorted(
        name for name, deps in graph.items() if target in deps and not _in_package(name, target)
    )


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
    if snapshot["missing_roots"] != "none":
        print(
            f"metrics: declared entry points not found: {snapshot['missing_roots']}",
            file=sys.stderr,
        )
    if args.write:
        write(args.root, snapshot)
    print(json.dumps(snapshot, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run the tests again**

Run: `uv run pytest -q tests/test_script_metrics.py tests/test_script_codebase.py`
Expected: `23 passed`.

- [ ] **Step 6: Reproduce the DESIGN §2 baseline**

Run: `uv run python scripts/metrics.py`
Expected, measured at `330b200` before Task 7:

| Key | Value |
| --- | --- |
| `modules` | 248 |
| `src_lines` | 190,383 |
| `runtime_lines` | 137,820 |
| `tooling_only_lines` | 33,155 |
| `tests_only_lines` | 19,408 |
| `unreached_lines` | 0 |
| `lane_reachable_lines` | 132,772 |
| `forge_lane_lines` | 0 |
| `missing_roots` | none |
| `functions_over_100` | 234 |
| `functions_over_200` | 56 |
| `prose_share` | 28% |
| `direct_dependencies` | 14 |

After Task 7, `src_lines` is about 54 higher (runtime +26, tooling +28).
Line and module counts must be within ±1% of the baseline. The console
command `forge.cli` counts as tooling, which is why these numbers differ from
the first draft of DESIGN §2 by 73 lines. `review_refs` (3,216) and
`adr_refs` (705) are unchanged by Task 7.

- [ ] **Step 7: Record the first measured snapshot**

Run: `uv run python scripts/metrics.py --write`
Expected: `metrics-history.json` is created, and `metrics.md` gains a
`## Measured history` section between `<!-- metrics:begin -->` and
`<!-- metrics:end -->`.

- [ ] **Step 8: Commit**

```bash
git add scripts/_codebase.py scripts/metrics.py tests/test_script_metrics.py tests/test_script_codebase.py docs/roadmap/2026-09-29-simplification/metrics.md docs/roadmap/2026-09-29-simplification/metrics-history.json
git commit -m "tools: metrics script and the first measured snapshot" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 10: Size ratchet (ROADMAP P0-08, part 1)

**Files:**
- Create: `scripts/ratchet.py`, `tests/test_script_ratchet.py`, `scripts/ratchet-baseline.json` (generated)
- Modify: `.github/workflows/ci.yml` (lint step)

Rule: files over 600 lines and functions over 80 lines are recorded debt
that may only shrink, and shrinking is locked in: when debt shrinks, the PR
runs `--update` and commits the lower baseline. Everything else must stay
within those limits. `--update --accept PATH` records oversized code that was
moved or renamed, and never raises an existing number. Only
`forge/ports.py`, `forge/engine/effects.py` and `forge/api/app.py` may name
`WritePort`; this check is active from Phase 3 on.

- [ ] **Step 1: Write the failing tests**

```python
"""scripts/ratchet.py: oversized code may only shrink, and shrinking is locked in."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def _check(root: Path) -> int:
    return ratchet.main(["--root", str(root)])


def test_small_code_needs_no_baseline_entry(tmp_path):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("f", 10)})
    _init(root)
    assert _baseline(root) == {"files": {}, "functions": {}}
    assert _check(root) == 0


def test_oversized_function_is_recorded_and_may_not_grow(tmp_path, capsys):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("big", 90)})
    _init(root)
    assert _baseline(root)["functions"] == {"src/forge/m.py::big": 90}
    (root / "src/forge/m.py").write_text(_function("big", 91))
    assert _check(root) == 1
    out = capsys.readouterr().out
    assert "src/forge/m.py::big (line 1) grew to 91 lines (allowed 90)" in out
    assert "--accept" not in out


def test_new_function_over_the_limit_fails(tmp_path, capsys):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("f", 10)})
    _init(root)
    (root / "src/forge/n.py").write_text(_function("huge", 81))
    assert _check(root) == 1
    assert "src/forge/n.py::huge (line 1) has 81 lines (limit 80)" in capsys.readouterr().out


def test_new_file_over_the_limit_fails(tmp_path, capsys):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("f", 10)})
    _init(root)
    (root / "src/forge/big.py").write_text("x = 1\n" * 601)
    assert _check(root) == 1
    assert "file src/forge/big.py has 601 lines (limit 600)" in capsys.readouterr().out


def test_workspace_packages_are_checked(tmp_path, capsys):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("f", 10)})
    _init(root)
    lane = root / "packages/forge-lane/src/forge_lane/run.py"
    lane.parent.mkdir(parents=True)
    lane.write_text(_function("huge", 81))
    assert _check(root) == 1
    assert "packages/forge-lane/src/forge_lane/run.py::huge" in capsys.readouterr().out


def test_small_function_may_grow_up_to_the_limit(tmp_path):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("f", 10)})
    _init(root)
    (root / "src/forge/m.py").write_text(_function("f", 80))
    assert _check(root) == 0


def test_shrinking_is_locked_in(tmp_path, capsys):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("big", 120)})
    _init(root)
    (root / "src/forge/m.py").write_text(_function("big", 90))
    assert _check(root) == 1
    assert "the baseline is stale" in capsys.readouterr().out
    assert ratchet.main(["--root", str(root), "--update"]) == 0
    assert _check(root) == 0
    (root / "src/forge/m.py").write_text(_function("big", 120))
    assert _check(root) == 1


def test_update_lowers_shrunk_debt_and_drops_repaid_entries(tmp_path):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("a", 120) + _function("b", 90)})
    _init(root)
    (root / "src/forge/m.py").write_text(_function("a", 100) + _function("b", 40))
    assert ratchet.main(["--root", str(root), "--update"]) == 0
    assert _baseline(root)["functions"] == {"src/forge/m.py::a": 100}


def test_accept_records_a_reviewed_move(tmp_path, capsys):
    root = write_tree(tmp_path, {"src/forge/old.py": _function("big", 90)})
    _init(root)
    (root / "src/forge/old.py").unlink()
    (root / "src/forge/new.py").write_text(_function("big", 90))
    assert _check(root) == 1
    moved = str(root / "src/forge/new.py")
    assert ratchet.main(["--root", str(root), "--update", "--accept", moved]) == 0
    assert "accepted src/forge/new.py::big (90 lines)" in capsys.readouterr().out
    assert _baseline(root)["functions"] == {"src/forge/new.py::big": 90}
    assert _check(root) == 0


def test_accept_never_raises_existing_debt(tmp_path, capsys):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("big", 90)})
    _init(root)
    (root / "src/forge/m.py").write_text(_function("big", 150))
    assert ratchet.main(["--root", str(root), "--update", "--accept", "src/forge/m.py"]) == 2
    assert "nothing new to accept in src/forge/m.py" in capsys.readouterr().out
    assert _baseline(root)["functions"] == {"src/forge/m.py::big": 90}


def test_accept_requires_update(tmp_path):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("f", 10)})
    _init(root)
    with pytest.raises(SystemExit):
        ratchet.main(["--root", str(root), "--accept", "src/forge/m.py"])


def test_init_refuses_to_overwrite_the_baseline(tmp_path):
    root = write_tree(tmp_path, {"src/forge/m.py": _function("big", 90)})
    _init(root)
    (root / "src/forge/m.py").write_text(_function("big", 150))
    assert ratchet.main(["--root", str(root), "--init"]) == 2
    assert _baseline(root)["functions"] == {"src/forge/m.py::big": 90}
    assert ratchet.main(["--root", str(root), "--init", "--force"]) == 0


def test_only_the_effects_layer_may_name_the_write_port(tmp_path, capsys):
    root = write_tree(
        tmp_path,
        {
            "src/forge/ports.py": "class WritePort: ...\n",
            "src/forge/engine/effects.py": "from forge.ports import WritePort\n",
            "src/forge/engine/handlers.py": "import os\nfrom forge.ports import WritePort\n",
        },
    )
    _init(root)
    assert _check(root) == 1
    out = capsys.readouterr().out
    assert "src/forge/engine/handlers.py:2 names WritePort" in out
    assert "effects.py:1 names WritePort" not in out
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest -q tests/test_script_ratchet.py`
Expected: `ModuleNotFoundError: No module named 'ratchet'`.

- [ ] **Step 3: Create the ratchet**

```python
"""Size ratchet: oversized code may only shrink, and shrinking is locked in.

``scripts/ratchet-baseline.json`` lists the current debt: files over 600
lines and functions over 80 lines under ``src/`` and ``packages/*/src/``.
The check fails when:

- a listed file or function grew beyond its recorded size;
- any other file exceeds 600 lines, or any other function exceeds 80 lines;
- the baseline is stale: debt shrank or disappeared and was not recorded;
- a module other than the allowed three names ``WritePort``
  (DESIGN §6.1: only the effects layer ever holds the write port).

Usage::

    uv run python scripts/ratchet.py                  # check (CI)
    uv run python scripts/ratchet.py --update         # record shrunk or repaid debt
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
COMMAND = "uv run python scripts/ratchet.py"

Sizes = dict[str, dict[str, int]]


def measure(root: Path) -> tuple[Sizes, dict[str, int]]:
    """Current sizes, and the line where each function starts."""
    files: dict[str, int] = {}
    functions: dict[str, int] = {}
    starts: dict[str, int] = {}
    for src_dir in cb.source_dirs(root):
        for path in cb.iter_python_files(src_dir):
            rel = path.relative_to(root).as_posix()
            files[rel] = cb.count_lines(path)
            for size in cb.function_sizes(path):
                key = f"{rel}::{size.qualname}"
                functions[key] = size.lines
                starts[key] = size.lineno
    return {"files": files, "functions": functions}, starts


def debt(current: Sizes) -> Sizes:
    return {
        kind: {key: lines for key, lines in current[kind].items() if lines > LIMITS[kind]}
        for kind in LIMITS
    }


def update(current: Sizes, baseline: Sizes, accept: tuple[str, ...] = ()) -> Sizes:
    """Lower shrunk debt, drop repaid or vanished debt, and record accepted new keys.

    ``accept`` only adds keys that are not in the baseline yet (code that was
    moved or renamed); it never raises an existing number.
    """
    updated: Sizes = {kind: {} for kind in LIMITS}
    for kind, limit in LIMITS.items():
        for key, old in baseline[kind].items():
            new = min(old, current[kind].get(key, 0))
            if new > limit:
                updated[kind][key] = new
        for key, lines in current[kind].items():
            if key.split("::", 1)[0] in accept and key not in baseline[kind] and lines > limit:
                updated[kind][key] = lines
    return updated


def check(current: Sizes, baseline: Sizes, starts: dict[str, int]) -> list[str]:
    problems: list[str] = []
    for kind, limit in LIMITS.items():
        for key, lines in sorted(current[kind].items()):
            where = f"{kind[:-1]} {key}" + (f" (line {starts[key]})" if key in starts else "")
            if key in baseline[kind]:
                if lines > baseline[kind][key]:
                    problems.append(
                        f"{where} grew to {lines} lines (allowed {baseline[kind][key]}): "
                        "shrink it back or split it"
                    )
            elif lines > limit:
                path = key.split("::", 1)[0]
                problems.append(
                    f"{where} has {lines} lines (limit {limit}): split it; for a reviewed move "
                    f"of existing debt run '{COMMAND} --update --accept {path}'"
                )
    if update(current, baseline) != baseline:
        problems.append(
            f"the baseline is stale because debt shrank: run '{COMMAND} --update' "
            f"and commit {BASELINE_NAME}"
        )
    return problems


def write_port_violations(root: Path) -> list[str]:
    problems = []
    for src_dir in cb.source_dirs(root):
        for path in cb.iter_python_files(src_dir):
            if path.relative_to(src_dir).as_posix() in WRITE_PORT_ALLOWED:
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            for number, line in enumerate(lines, 1):
                if WRITE_PORT.search(line):
                    problems.append(
                        f"{path.relative_to(root).as_posix()}:{number} names WritePort: "
                        "only engine/effects.py may hold the write port (use ctx.effects)"
                    )
                    break
    return problems


def _normalise(root: Path, raw: str) -> str:
    path = Path(raw)
    if path.is_absolute():
        path = path.resolve().relative_to(root.resolve())
    return path.as_posix()


def _save(path: Path, sizes: Sizes) -> None:
    path.write_text(json.dumps(sizes, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def _run_update(root: Path, current: Sizes, baseline: Sizes, raw_accept: list[str]) -> int:
    try:
        accept = tuple(_normalise(root, raw) for raw in raw_accept)
    except ValueError as exc:
        print(f"ratchet: {exc}")
        return 2
    updated = update(current, baseline, accept)
    added = {
        key: lines
        for kind in LIMITS
        for key, lines in updated[kind].items()
        if key not in baseline[kind]
    }
    idle = [path for path in accept if not any(k.split("::", 1)[0] == path for k in added)]
    if idle:
        print(f"ratchet: nothing new to accept in {', '.join(idle)}; nothing was written")
        return 2
    for key, lines in sorted(added.items()):
        print(f"ratchet: accepted {key} ({lines} lines)")
    _save(root / BASELINE_NAME, updated)
    print("ratchet: baseline updated")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", type=Path, default=cb.repo_root())
    parser.add_argument("--init", action="store_true", help="write the first baseline")
    parser.add_argument("--force", action="store_true", help="with --init: overwrite")
    parser.add_argument("--update", action="store_true", help="record shrunk or repaid debt")
    parser.add_argument(
        "--accept",
        action="append",
        default=[],
        metavar="PATH",
        help="with --update: record new oversized code in PATH (a reviewed move)",
    )
    args = parser.parse_args(argv)
    if args.accept and not args.update:
        parser.error("--accept requires --update")
    if args.force and not args.init:
        parser.error("--force requires --init")
    baseline_path = args.root / BASELINE_NAME
    current, starts = measure(args.root)
    if args.init:
        if baseline_path.exists() and not args.force:
            print(f"ratchet: {BASELINE_NAME} exists; --init would forgive all growth (--force)")
            return 2
        _save(baseline_path, debt(current))
        print(f"ratchet: baseline written to {BASELINE_NAME}")
        return 0
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    if args.update:
        return _run_update(args.root, current, baseline, args.accept)
    problems = check(current, baseline, starts) + write_port_violations(args.root)
    for problem in problems:
        print(f"ratchet: {problem}")
    if problems:
        print("ratchet: FAILED")
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
Expected: `13 passed`.

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
- Modify: `pyproject.toml`, `uv.lock`, `.gitignore`, `.github/workflows/ci.yml` (new `coverage` job)

- [ ] **Step 1: Add the tools**

Run: `uv add --dev "pytest-cov==7.1.0" "diff-cover==10.6.0"`
Expected: both appear under `[dependency-groups] dev`.

Add `coverage.xml` to `.gitignore`, next to the existing `.coverage` entry.

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
        run: uv run pytest -q -m "not slow" --cov=forge --cov=scripts --cov-report=xml

      - name: Diff coverage (at least 85% of changed lines)
        run: >-
          uv run diff-cover coverage.xml
          --compare-branch=origin/${{ github.base_ref }} --fail-under=85
```

- [ ] **Step 3: Try it locally on this branch**

Run: `uv run pytest -q -m "not slow" --cov=forge --cov=scripts --cov-report=xml && uv run diff-cover coverage.xml --compare-branch=origin/main --fail-under=85`
Expected: diff-cover lists the Phase 0 changes so far (the Task 7 lines in
`config.py`, `validator.py`, `router.py` and `doctor.py`, and the new
scripts) at or above 85% in total. `--cov=scripts` is there because the
program's own scripts are the most-changed code in Phase 0. Do not commit
`coverage.xml`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock .gitignore .github/workflows/ci.yml
git commit -m "ci: diff coverage of at least 85% on changed lines" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 12: Prose lint (ROADMAP P0-09)

**Files:**
- Create: `scripts/prose_lint.py`, `tests/test_script_prose_lint.py`
- Modify: `.github/workflows/ci.yml` (lint job: full history and the new step)

- [ ] **Step 1: Write the failing tests**

```python
"""scripts/prose_lint.py: no review, backlog or issue archaeology in added source lines."""

from __future__ import annotations

import subprocess

import pytest

from tests._scripts import load_script

prose_lint = load_script("prose_lint")

HEADER = (
    "diff --git a/src/forge/x.py b/src/forge/x.py\n--- a/src/forge/x.py\n+++ b/src/forge/x.py\n"
)
DIFF = (
    HEADER + "@@ -10,0 +11,5 @@\n"
    "+# R42-16: the parity rule\n"
    "+x = 1  # see ADR-0035\n"
    "+# fixed in #377\n"
    "+# as agreed in review `9e8d6c6`\n"
    "+# carried over from NXT-10\n"
)


def test_review_backlog_and_issue_references_are_rejected():
    found = prose_lint.violations(DIFF)
    assert [item.split(":")[1] for item in found] == ["11", "13", "14", "15"]


def test_adr_references_are_allowed():
    assert not any("ADR-0035" in item for item in prose_lint.violations(DIFF))


def test_removed_lines_are_ignored():
    assert prose_lint.violations(HEADER + "@@ -3,1 +2,0 @@\n-# R42-16 old comment\n") == []


def test_line_numbers_ignore_no_newline_markers():
    diff = HEADER + "@@ -1 +1,2 @@\n-x = 1\n\\ No newline at end of file\n+x = 1\n+# R42-16\n"
    assert [item.split(":")[1] for item in prose_lint.violations(diff)] == ["2"]


def test_the_allow_marker_keeps_a_literal():
    diff = HEADER + '@@ -0,0 +1 @@\n+COLOUR = "#009966"  # prose-lint: allow\n'
    assert prose_lint.violations(diff) == []


def test_a_missing_or_zero_base_is_a_no_op():
    assert prose_lint.main(["--base", ""]) == 0
    assert prose_lint.main(["--base", "0" * 40]) == 0


def _git(repo, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q", "-b", "main")
    for key, value in (
        ("user.email", "lint@example.test"),
        ("user.name", "lint"),
        ("commit.gpgsign", "false"),
        ("core.hooksPath", "/dev/null"),
    ):
        _git(tmp_path, "config", key, value)
    (tmp_path / "src").mkdir()
    (tmp_path / "src/m.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _change(repo, text: str) -> None:
    (repo / "src/m.py").write_text(text)
    _git(repo, "commit", "-q", "-am", "change")


def test_a_clean_change_passes(repo):
    _change(repo, "x = 1\ny = 2  # see ADR-0035\n")
    assert prose_lint.main(["--base", "HEAD~1"]) == 0


def test_an_added_review_id_fails(repo, capsys):
    _change(repo, "x = 1\n# R42-16 parity\n")
    assert prose_lint.main(["--base", "HEAD~1"]) == 1
    assert "src/m.py:2: # R42-16 parity" in capsys.readouterr().out


def test_an_unknown_base_is_reported_not_raised(repo, capsys):
    assert prose_lint.main(["--base", "1234567890abcdef1234567890abcdef12345678"]) == 0
    assert "is not in this checkout" in capsys.readouterr().out
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest -q tests/test_script_prose_lint.py`
Expected: `ModuleNotFoundError: No module named 'prose_lint'`.

- [ ] **Step 3: Create the lint**

```python
"""Reject review, backlog and issue archaeology in added source lines.

History belongs in git and ADRs, not in code comments. The check reads the
lines added under ``src/`` and ``packages/*/src/`` since ``--base`` and
rejects what ``_codebase.REVIEW_REF`` matches:

- review, backlog and acceptance item ids such as ``R42-16``, ``Q35-09``,
  ``NXT-10``, ``NEXT-05`` or ``AT-08``;
- issue references such as ``#377``;
- "review <sha>" references.

``ADR-0035`` style references are allowed. Moved code counts as added, so a
move is when its references go. A line that must keep a matching literal,
such as the colour ``#009966``, carries the marker ``prose-lint: allow``.

Usage::

    uv run python scripts/prose_lint.py --base origin/main
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterator

import _codebase as cb

PATHS = ("src", ":(glob)packages/*/src/**")
DIFF_OPTIONS = (
    "--unified=0",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--find-renames",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
ALLOW = "prose-lint: allow"


def added_lines(diff: str) -> Iterator[tuple[str, int, str]]:
    path, lineno, in_header = "", 0, False
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            path, in_header = "", True
        elif in_header and line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif match := HUNK.match(line):
            lineno, in_header = int(match.group(1)), False
        elif not in_header and line.startswith("+") and path:
            yield path, lineno, line[1:]
            lineno += 1
        elif not in_header and line.startswith(" "):
            lineno += 1


def violations(diff: str) -> list[str]:
    return [
        f"{path}:{lineno}: {text.strip()}"
        for path, lineno, text in added_lines(diff)
        if cb.REVIEW_REF.search(text) and ALLOW not in text
    ]


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="", help="git ref to diff against")
    args = parser.parse_args(argv)
    if not args.base.strip("0"):
        print("prose-lint: no base ref, nothing to check")
        return 0
    if _git("rev-parse", "--verify", "--quiet", f"{args.base}^{{commit}}").returncode:
        print(f"prose-lint: base {args.base} is not in this checkout (force-push?); skipped")
        return 0
    done = _git("diff", *DIFF_OPTIONS, f"{args.base}...HEAD", "--", *PATHS)
    if done.returncode:
        print(f"prose-lint: git diff failed: {done.stderr.strip()}")
        return 2
    found = violations(done.stdout)
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
Expected: `9 passed`.

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
          --base "${{ github.event_name == 'pull_request' && 'HEAD^1' || github.event.before }}"
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
ANY_ROW = re.compile(r"^\|\s*(?:EC|OP)-\d+\s*\|")
ROW = re.compile(r"^\| (?P<ident>(?:EC|OP)-\d{2}) \|(?P<rest>.*)\|$")
PIN = re.compile(r"`([^`]+)`")


def _rows() -> list[tuple[str, list[str]]]:
    rows = []
    for line in REGISTER.read_text(encoding="utf-8").splitlines():
        if match := ROW.match(line.rstrip()):
            last_cell = match["rest"].rsplit("|", 1)[-1]
            rows.append((match["ident"], PIN.findall(last_cell)))
    return rows


PINS = [(ident, target) for ident, targets in _rows() for target in targets]


def test_every_register_row_is_parsed():
    lines = [
        line for line in REGISTER.read_text(encoding="utf-8").splitlines() if ANY_ROW.match(line)
    ]
    rows = _rows()
    assert len(rows) == len(lines), "a register row does not have the expected shape"
    assert all(targets for _, targets in rows), "a register row names no pin"
    assert len({ident for ident, _ in rows}) == len(rows), "a register id is used twice"


def test_the_register_is_not_empty():
    assert len([ident for ident, _ in _rows() if ident.startswith("EC-")]) >= 11


@pytest.mark.parametrize(("ident", "target"), PINS, ids=[f"{i}-{t}" for i, t in PINS])
def test_every_pin_points_at_something_real(ident: str, target: str):
    if ident.startswith("EC-"):
        assert "::" in target, f"{ident}: {target} must name a test, not a file"
        assert nodeid_exists(target), f"{ident}: pinning test {target} is gone"
    else:
        assert (REPO / target).exists(), f"{ident}: reference {target} is gone"


def test_the_node_id_check():
    own = "tests/test_edge_case_register.py"
    assert nodeid_exists(f"{own}::test_the_register_is_not_empty")
    assert nodeid_exists(f"{own}::test_every_pin_points_at_something_real[EC-01-x]")
    assert not nodeid_exists(f"{own}::test_there_is_no_such_test")
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest -q tests/test_edge_case_register.py`
Expected: `ModuleNotFoundError: No module named 'tests._nodeids'`.

- [ ] **Step 3: Create the node-id helper**

```python
"""Static existence check for pytest node ids (no collection, no imports).

Limits: tests inherited from a base class, or defined under ``if``, are
reported missing (the check fails safe); a parameter id in ``[...]`` is not
checked, only the function name.
"""

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

Hard-won gotchas, each pinned by tests or a reference. Before a module is
deleted or rewritten (ROADMAP Phase 1 and Phase 3), its entries here must
still hold in the new code; the **Code** column says where each rule lives.
`tests/test_edge_case_register.py` fails when a pin disappears.

When a pin breaks:

- port the pinning test to the new code and repoint the row in the same PR;
  the new pin must still fail when the rule is broken;
- when a rule no longer applies (for example an Azure-only rule after
  P1-11), move the row to **Retired** as a bullet with the reason;
- never reuse an ID.

## Code-level entries (each pinned by tests)

| ID | Symptom | Rule | Code | Pinning tests |
| --- | --- | --- | --- | --- |
| EC-01 | GitLab CE 18.x Commits API returns 400 when `start_branch` names an existing branch | `ensure_branch` owns branch creation; commits never pass `start_branch` | `src/forge/repository/writer.py` | `tests/test_repository_writer.py::test_no_start_branch_when_branch_pre_exists`, `tests/test_repository_writer.py::test_no_start_branch_on_fresh_branch` |
| EC-02 | npm `optionalDependencies` flake leaves a harness CLI in a silent futex hang | Install the platform binary explicitly | `src/forge/harnesses/scripts/` | `tests/test_harness_entry.py::TestRenderGrokBuild::test_script_installs_the_platform_binary_explicitly` |
| EC-03 | The platform binary drifts from the pinned wrapper version | Pin the platform binary to the wrapper's version | `src/forge/harnesses/scripts/` | `tests/test_harness_entry.py::TestDriverVersionPins::test_grok_platform_binary_follows_the_pinned_wrapper` |
| EC-04 | Two concurrent publishers create two Draft MRs | Create idempotently by intent; the database arbitrates uniqueness | `src/forge/runs/service.py` | `tests/production_entry/test_concurrent_publication.py::TestConcurrentDraftMrCreators::test_two_direct_concurrent_creators_collapse_to_one_mr` |
| EC-05 | forge's own notes contain command lines and could trigger forge; a GitHub App's login (`name[bot]`) differs from `FORGE_BOT_USERNAME` | Bot-authored events are skipped on every provider | `src/forge/gateway/validator.py`, `src/forge/gateway/github_webhook.py` | `tests/test_bot_loop.py::test_webhook_skips_bot_event`, `tests/test_github_webhook.py::TestGitHubBotLoginLoopGuard::test_bot_login_comment_does_not_create_a_run` |
| EC-06 | GitHub workflow display names are not identities | Resolve the harness workflow by path | `src/forge/runs/github_service.py` | `tests/test_github_runs.py::TestPositiveVerification::test_harness_lane_is_excluded_by_path_not_display_name` |
| EC-07 | GitLab verification pipelines (`merge_request_event`) look like lane activity | Exclude them from coding occupancy | `src/forge/runs/service.py` | `tests/test_adaptive_lease_occupancy.py::TestBranchSearchCorrelation::test_verification_pipeline_is_not_coding_occupancy` |
| EC-08 | A lost publication response tempts a second write | Resolve the unknown outcome by marker-and-parent lookup; never write twice | `src/forge/repository/writer.py`, `src/forge/durable/intents.py` | `tests/test_publication_intents.py::TestGitLabWriterIntents::test_lost_response_is_adopted_by_a_new_process`, `tests/test_publication_intents.py::TestProbeDecisionTable::test_exactly_one_marker_and_parent_match_adopts` |
| EC-09 | A human commit lands on the factory branch during a review round | A foreign tip is a typed conflict; the human commit is preserved, never reverted | `src/forge/runs/service.py` | `tests/test_review_rounds.py::TestRoundOwnEffectRecovery::test_a_human_commit_after_the_candidate_is_a_typed_conflict_preserved` |
| EC-10 | A required resume over an unusable checkpoint could start a model session on stale work | Halt before any vendor session | `src/forge/lane_driver.py` | `tests/test_application_regressions.py::TestR32ARestoredGenerationIsTheExecutionWorkspace::test_a_required_resume_that_cannot_land_halts_before_any_vendor_session` |
| EC-11 | GitHub silently drops an unknown query parameter: `head_branch=` widened the workflow-runs filter, so concurrent dispatches looked correlated (ADR-0028) | Filter runs with `branch=`, and re-check branch equality client-side | `src/forge/integrations/github.py` | `tests/test_github_client.py::TestActionsEndpoints::test_list_workflow_dispatch_runs_filters_and_windows`, `tests/test_github_actions_executor.py::TestLaunch::test_concurrent_batch_sharing_one_attempt_base_correlates_own_branch` |

## Operational entries (the knowledge lives in the row)

| ID | Symptom | Rule | Reference |
| --- | --- | --- | --- |
| OP-01 | Claude Code on z.ai treats a bare model id as a 200K context window | Use the `[1m]` suffix for the 1M window; the default `FORGE_HARNESS_MODEL` carries it | `src/forge/config.py` |
| OP-02 | Redis `forge:worker:<id>` heartbeat keys outlive a dead worker by about 30 seconds | Check worker logs, not only `/metrics` | `AGENTS.md` |
| OP-03 | podman reports a 12-hex short image id; `inspect` reports the 64-hex id | Drop `sha256:` and compare by prefix, never with `!=` | `scripts/run_review_loop_qualification.py` |
| OP-04 | zsh does not word-split unquoted variables | Iterate with `while IFS= read -r` | `AGENTS.md` |
| OP-05 | `pytest` piped to `tail` hides the exit code | Run `set -o pipefail` first | `AGENTS.md` |
| OP-06 | Long-lived AI streams from some runner networks crawl (226 s instead of 2 s) | Route them through `FORGE_HARNESS_HTTPS_PROXY` | `ci/templates/grok.gitlab-ci.yml` |
| OP-07 | The dead-letter queue `forge:tasks:dead` looks like stuck runs | It holds exhausted tasks; read run outcomes from Postgres `flow_runs` | `AGENTS.md` |

## Retired

None yet.
```

- [ ] **Step 5: Run the test again**

Run: `uv run pytest -q tests/test_edge_case_register.py`
Expected: `25 passed`.

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


def test_kept_tooling_and_protected_evidence_are_not_dropped(tmp_path):
    tree = {
        **TREE,
        "pyproject.toml": '[project]\nname = "x"\n[project.scripts]\nforge = "forge.cli:main"\n',
        "src/forge/doctor.py": "X = 1\n",
        "src/forge/cli.py": "Y = 1\n",
        "src/forge/pinned.py": "Z = 1\n",
        "tests/test_doctor.py": "from forge import doctor\n",
        "tests/test_cli.py": "from forge import cli\n",
        "tests/test_pinned.py": "from forge import pinned\n",
        "tests/invariants/registry.py": 'EVIDENCE = frozenset(("tests/test_only.py",))\n',
        "docs/reference/edge-cases.md": "| EC-01 | a | b | c | `tests/test_pinned.py::test_x` |\n",
    }
    root = write_tree(tmp_path, tree)
    (root / ledger.LEDGER).parent.mkdir(parents=True)
    assert ledger.main(["--root", str(root)]) == 0
    rows = _rows(root)
    names = (
        "tests/test_doctor.py",
        "tests/test_cli.py",
        "tests/test_only.py",
        "tests/test_pinned.py",
    )
    assert {name: rows[name]["disposition"] for name in names} == dict.fromkeys(names, "keep")
    assert rows["tests/test_only.py"]["note"] == "invariant evidence"
    assert rows["tests/test_pinned.py"]["note"] == "edge-case pin"
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

The default disposition never drops a file that is invariant evidence
(``tests/invariants/registry.py``) or an edge-case pin
(``docs/reference/edge-cases.md``), nor a test of tooling that stays
(``forge.doctor`` and the ``[project.scripts]`` commands).

Usage::

    uv run python scripts/build_test_ledger.py      # writes docs/roadmap/2026-09-29-simplification/test-ledger.csv
"""

from __future__ import annotations

import argparse
import ast
import csv
import re
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
KEPT_TOOLING_ROOTS = ("forge.doctor",)
INVARIANT_REGISTRY = Path("tests/invariants/registry.py")
EDGE_CASE_REGISTER = Path("docs/reference/edge-cases.md")
PINNED_FILE = re.compile(r"`(tests/[^`:]+\.py)(?:::[^`]*)?`")


def count_tests(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
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


def protected_files(root: Path) -> dict[str, str]:
    """Test files that are invariant evidence or edge-case pins, with the reason."""
    found: dict[str, str] = {}
    registry = root / INVARIANT_REGISTRY
    if registry.is_file():
        tree = ast.parse(registry.read_text(encoding="utf-8"), filename=str(registry))
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            for node in ast.walk(call):
                value = node.value if isinstance(node, ast.Constant) else None
                if isinstance(value, str) and value.startswith("tests/") and value.endswith(".py"):
                    found.setdefault(value, "invariant evidence")
    register = root / EDGE_CASE_REGISTER
    if register.is_file():
        for rel in PINNED_FILE.findall(register.read_text(encoding="utf-8")):
            found.setdefault(rel, "edge-case pin")
    return found


def default_disposition(rel: str, imported: set[str], droppable: set[str]) -> tuple[str, str]:
    """A starting point for the owner to curate — never the final word.

    ``imported`` holds leaf modules only (packages are excluded), so a test
    that imports nothing but code that is going away is recognised.
    """
    if imported and imported <= droppable:
        return "drop-with-code", "P1"
    if any(name.startswith(SERVICES) for name in imported) or "production_entry" in rel:
        return "port-engine", "P3"
    if any(name.startswith(PROVIDER_CLIENTS) for name in imported):
        return "port-adapter", "P3"
    return "keep", "-"


def build_rows(root: Path, previous: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    modules = cb.discover_modules(root)
    reach = cb.classify(root, modules)
    kept_roots = {*KEPT_TOOLING_ROOTS, *cb.console_script_modules(root)}
    kept = cb.reachable(kept_roots, cb.import_graph(modules))
    droppable = reach.tests_only | (reach.tooling_only - kept)
    protected = protected_files(root)
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
        disposition, phase = default_disposition(rel, imported, droppable)
        note = protected.get(rel, "")
        if note and disposition.startswith("drop"):
            disposition, phase = "keep", "-"
        row = {
            "file": rel,
            "tests": str(count_tests(path)),
            "loc": str(cb.count_lines(path)),
            "imports_runtime": str(bool(imported & reach.runtime)).lower(),
            "imports_tests_only": str(bool(imported & reach.tests_only)).lower(),
            "area": area(rel),
            "disposition": disposition,
            "phase": phase,
            "note": note,
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
Expected: `3 passed`.

- [ ] **Step 5: Generate the ledger**

Run: `uv run python scripts/build_test_ledger.py`
Expected: about `test-ledger: 303 files → … {'port-adapter': 45, 'keep': ~186,
'port-engine': 49, 'drop-with-code': 23}`. Invariant evidence and edge-case
pins carry a note and are never `drop-*` by default. The exact counts depend
on the tests added by Tasks 1–13.

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
  - The import contracts run in the `lint` job, not a separate `imports`
    job, and a test (`tests/test_import_contract_debt.py`) is the
    shrink-only counter for their ignore list.
  - The size ratchet also fails on a stale baseline, so shrinking is locked
    in; `--accept` only records new keys, and `--init` refuses to overwrite
    a baseline without `--force`.
  - The prose lint diffs pull requests against `HEAD^1` (the exact base of
    the merge commit) and skips a base that is not in the checkout, such as
    after a force-push.
  - The Invariant Suite is one registry (`tests/invariants/registry.py`)
    over existing focused test files, not one module per invariant, and I6
    is proven by the existing production-entry race test rather than a new
    in-process variant.
  - `tests/test_failure_injection_os.py` is not registered for I7: it needs
    real workers and Redis and runs nightly in `integration-os`, while the
    `invariants` job fails on any skip.
- **Consistency.** Script names, CLI flags, marker names and file paths are
  identical in every task. `tests/_scripts.py` is created in Task 6, before
  its first use in Tasks 9–14.
