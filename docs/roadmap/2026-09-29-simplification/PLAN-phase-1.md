# Phase 1 — Subtract: Implementation Plan, first batch

> **For agentic workers:** use one worker and an independent review per
> ROADMAP task. Steps use checkbox syntax. Do not implement an unapproved
> task or treat a successful scratch experiment as permission to execute it.

> **Status:** historical rehearsed first-batch plan. The owner later authorized
> completion of the sole 1.0 runtime and stopped further old-test ports.
> On 2026-10-23 the owner explicitly waived the GitHub prerequisites for
> local work. Local safety gates, internal task dependencies and the
> reviewed ledger still apply. See IMPLEMENTATION-1.0.md for current work.

**Goal:** plan ROADMAP P1-01 through P1-06: move project configuration,
remove the test-only publication and adaptive clusters, remove reference
doubles and the closure matrix, and replace import ownership registration
with import-linter contracts. Preserve I1–I8, the self-trigger rule, the
edge-case pins, the owner's test dispositions and the Phase 1 schema.

**Architecture:** the provider services keep their current lifecycle.
Project configuration becomes a top-level forge module. Import-linter
owns import restrictions; focused tests continue to prove rules about
symbols, call sites and behavior. Deletion PRs repair their retained
consumers and evidence references in the same change.

**Tech Stack:** Python 3.13.11, uv, pytest, Ruff, mypy and import-linter
2.15; the existing static import analysis, ratchet and prose lint.

**Provenance:** the baseline is local `main` at
`0bf2a4e26ec48429c157e789df49db609aa700c3`. The task sections name their
scratch worktrees and observed red/green results. Command blocks are
rehearsal records, not a claim that blocked deletion tasks have passed
the full gates. Current implementation and verification are recorded
separately from those historical rehearsals.

---

## Before you start

- Read AGENTS.md, HANDOFF.md and its ordered reading list. ADR-0035 and
  DESIGN remain binding. Repository documents stay in English.
- The owner requested implementation of this batch on 2026-10-23. Use
  its recommended scope decisions below. Approval does not authorize
  pushing, merging, tagging, releases,
  workflow dispatches, issues, or GitHub settings changes.
- Use a bash-compatible shell and the task's own venv. Run `uv sync` in
  the task worktree; add `--extra postgres` where PostgreSQL is configured.
  The system `python3` in this session is 3.12.7; the uv-managed venv is
  3.13.11. Invoke `.venv/bin/python` or `uv run python` explicitly.
- Check the task's **Depends** against **Done when**, not against code
  presence or a handoff status. The table below applies to the entire
  batch; individual sections add their own dependencies.
- Never change curated ledger columns (`disposition`, `phase`, `note`)
  on the owner's behalf. `deleted_by` describes imports, not permission
  to delete a test. Protected tests must be ported and remain effective.
- Before moving, rewriting or deleting a module, inventory its gotchas
  and give each applicable rule an edge-case pin. A surviving pin must
  still detect the broken rule. Repoint or retire rows in the same PR.
- One ROADMAP task per PR; P1-03 uses several PRs of 3–4 modules. Keep
  about 500 net added/modified lines or fewer, with pure deletions exempt.
  Stop for a narrower split when consumer repairs exceed this budget.
- Run the common gates before every commit, including intermediate
  commits. Run prose lint immediately after every commit because it
  reads committed changes. Do not write source history IDs to satisfy a
  diagnostic. Do not add ignores, skip evidence or relax validators.
- Do not mark a task done until its acceptance checks and review pass.
  A deletion rehearsal that demonstrates a failure is a planning result.

## Entry gate and GitHub audit

Historical read-only audit on 2026-10-11; current evidence follows below:

| Requirement | Observed state | Acceptance evidence still needed |
| --- | --- | --- |
| Phase 0 on GitHub | `origin/main` is `7ed64ba`; local `main` is 66 commits ahead | Owner-authorized publication of the Phase 0 history and CI on those bytes |
| Phase 0 CI green | No run exists for local `0bf2a4e` | Read every new job, including PR-only coverage and non-PR `tests-slow`; default tier at most 480 seconds on Linux |
| Required safety gates | Required: `lint`, `typecheck`, `test (3.13)`, `test (3.14)`, `integration`; `invariants` and `coverage` absent | Green PR evidence, owner settings change and readback; import/ratchet/prose remain inside required `lint` |
| Phase 0 negative proofs | Local guardrail tests exist | Registered-file invariant mutations recorded; synthetic growth and prose PRs rejected on GitHub |
| `release/0.42.x` | Absent locally and remotely | Owner-created branch per maintenance-0.42.md, pushed and green |
| `pre-simplification` | Absent locally and remotely | Owner-created recovery tag at the last pre-deletion commit, available remotely |
| P0-13 owner ledger review | No recorded owner review | Owner reviews every `drop-*` and every nonempty `review` row, including retained tests in this batch |
| Batch approval | Implementation requested on 2026-10-23 | Recorded below; separate ledger review and entry prerequisites remain open |

The latest [10 October nightly](https://github.com/forcewake/forge/actions/runs/38040368966)
has the existing required jobs green, with `integration-os` and
`release-canary` red. The [original push](https://github.com/forcewake/forge/actions/runs/36406618182)
is green. All twelve scheduled runs from 29 September through 10 October
match HANDOFF: both advisory jobs fail every night; Python 3.14 also
fails on 5 and 6 October, and the PostgreSQL integration gate fails on
29 September. All run on `7ed64ba`, before Phase 0.

No observed GitHub failure can therefore be attributed to the unpushed
Phase 0 work. There is no justified Phase 0 fix branch yet. Historical
canary logs show a separate concrete defect: the workflow builds a Docker
image while the smoke script chooses installed Podman first. A future
small `ci:` fix can explicitly choose Docker, without weakening the
canary. The OS failures still need a root-cause investigation.

`git fetch origin` succeeded. `gh run list` initially returned HTTP 401 for the
active CLI credential. It then succeeded with an existing valid credential
passed only to that process; the active login stayed unchanged. Public API
metadata and read-only historical logs independently provided the evidence. Refresh authentication before relying on
the CLI for subsequent owner-authorized work. Do not print credentials.

## Implementation entry — 2026-10-23

The owner requested implementation of this plan, confirmed the review of
all 129 ledger rows without disposition changes, and then explicitly
authorized local work toward 1.0.0 before the GitHub prerequisites are met.
The hosted states below are an audit snapshot, not blockers for local
implementation under that instruction. Remote publication is not authorized.

Fresh read-only evidence:

| Requirement | Current evidence | State |
| --- | --- | --- |
| Phase 0 published | `git fetch origin` succeeded; `main` remains `0bf2a4e`, `origin/main` remains `7ed64ba`; 66 ahead, 0 behind | Open |
| Phase 0 CI | No Actions run exists for `0bf2a4e`; remote CI still lacks the Phase 0 jobs and guardrails | Open |
| Required checks | Only `lint`, `typecheck`, `test (3.13)`, `test (3.14)` and `integration`; no required `invariants` or `coverage` | Open |
| Maintenance branch and recovery tag | Local `release/0.42.x` now preserves `7ed64ba`; no remote branch or tag was created | Hosted acceptance remains open; local work authorized |
| Owner ledger review | Owner confirmed all 129 required rows reviewed on 2026-10-23; existing dispositions unchanged; ledger hash bound below | Met |
| Local P0-06 negative proofs | 20 caught runtime/config mutations covering all 18 registered files; every source restored | Met locally; required GitHub job remains open |
| Synthetic guardrail PRs | No GitHub growth/prose negative-proof PR created | Open; requires separate owner-authorized remote action |
| Batch approval | Owner instruction to implement this plan on 2026-10-23 | Met |

The [22 October nightly](https://github.com/forcewake/forge/actions/runs/39744611433)
runs the unchanged `7ed64ba`: the original five required checks are green,
while `integration-os` and `release-canary` fail. All twelve scheduled
runs from 11 through 22 October have that pattern. They provide no Phase 0
acceptance evidence. Current CLI authentication works without an override.
No branch push, PR, workflow dispatch, tag, merge or settings mutation was made.

Current evidence is retained in the ignored workspace directory
`.forge/verification/phase0-20261023/`. Its `entry-audit/` holds sanitized
GitHub records and the exact read-only owner ledger review queue. All
columns and source order are preserved; the curated ledger remains unchanged.
The queue's five protected rows retain their protection and are not marked
to drop. The owner subsequently confirmed review of all 129 rows with
unchanged dispositions. `owner-review-confirmation.json` binds that answer
to ledger SHA-256 `8d10d4d6a4eeeb4a0fb2aed012dbc0c3afe374479eceacbb88c4a50717abdbda`.
Local prerequisite verification and mutation proofs are recorded there.
They do not claim hosted acceptance. The owner's later instruction allows
the local moves, deletions and contract migration to proceed while hosted
acceptance remains outstanding. Preserve the task order and all local gates;
do not push, merge, tag, dispatch workflows or change GitHub settings.

### Current local prerequisite verification

- Registered Invariant Suite: **807 passed, zero skipped** in 45.01 seconds;
  the unchanged `assert_no_skips.py` gate exited 0. This used a newly created,
  isolated disposable PostgreSQL database, not the configured lab database.
- Ruff, mypy (49 source files), both existing import contracts, size ratchet,
  prose lint, public-artifact, exact-composition and conformance checks
  passed. Conformance exercised 66 arm IDs across 5 checks, with all mutation
  self-tests caught and no schema findings.
- Doctor exited 1 with failures for `gitlab.token` (expired token), `redis`,
  `database`, `litellm` and `azdo.pat` (HTTP 401). Runtime setup remains
  incomplete; `.env` and existing lab containers were unchanged.
- The complete default tier passed: **9,562 passed, 38 skipped,
  189 deselected** in 702.03 seconds under concurrent local verification.
  Coverage of the actual Phase 0 diff against `origin/main` passed at
  **94%** (683 measured lines, 35 missing), above the unchanged 85% gate.
  This local duration does not prove the 480-second Linux CI criterion.
- Local P0-06 negative proofs passed: **20 real runtime/configuration
  mutations across all 18 registered files**. Every focused baseline
  passed, the broken rule failed, and the restored source passed again;
  all 60 JUnit reports had zero errors and skips. Independent inspection
  checked every report, source hash and restored scratch diff. The actual
  invariant tests were not edited. Both I1 clauses and separate crash
  lost-work/duplicate-effect cases for I3/I7 are covered. See
  `.forge/verification/phase0-20261023/invariants/REPORT.md`.

The local publication packet is `.forge/verification/phase0-20261023/README.md`.
It proposes the exact existing `0bf2a4e` history on
`review/phase0-safety-20261023`, one draft PR and one CI dispatch. It requires
explicit owner authorization, including its declared one-time publication
scope spanning the already completed Phase 0 history. New implementation
PRs remain one ROADMAP task each. No proposal here authorizes an action.

## Plan decisions and separate ledger review

1. **P1-02 consumer scope.** Its four modules have a dependent reference
   leaf (`reference/native_shaped_remote.py`) and six retained/port test
   files. Include a narrow ROADMAP scope amendment to remove or port that
   saga-specific reference leaf in P1-02 and migrate only saga-dependent
   test arms. The rest of P1-04 stays separate. The owner completed the
   ledger review on 2026-10-23; importing a module never authorizes deletion.
2. **P1-03 acceptance scope.** Its literal zero-test-only-module check
   includes modules owned by P1-04, P1-05 and P1-06 and modules exposed by
   later tooling retirement. Change P1-03's acceptance to the
   removal of its assigned module inventory, with aggregate zero reserved
   for the Phase 1 exit. Keep the five module clusters separate. Evidence
   rows referenced by closure/release catalogs must be truthfully repointed
   or retired in each deletion PR; their validators stay active.
3. **P1-04 timing.** `steering_causality.py` still imports the reactive
   reference double and remains until P1-07. Adopt the recommended
   P1-07 dependency and defer P1-04 until its tooling caller is retired.
   Include the dependency amendment in that task; do not
   make production code import `tests/fixtures`.
4. **P1-05 non-import evidence.** The architecture test file is ledger
   `keep`. Preserve its focused symbol, call-site and behavior
   checks in test-owned fixtures/tests while replacing only importer
   registration with contracts. Amend ROADMAP's instruction to delete
   the whole test accordingly. A reviewed ledger disposition change is
   needed only if the owner chooses to replace/delete that file instead.
5. **P1-06 reference wording.** Interpret its absence check as
   no importers, live tooling references, evidence pins or broken active
   links. Preserve historical mentions in the accepted design, task list
   and executed Phase 0 plan; do not erase the program's history to make
   a literal repository-wide string search empty.

The implementation request adopts this plan's recommended decisions.
This update records the separate P0-13 confirmation above. The ROADMAP
edit records that review only; apply each scope amendment in its task PR.
No curated ledger value was changed.

Once the entry gate is satisfied, the executable order is
P1-01, P1-02, P1-03 A–E, then P1-06 and P1-05 in either order. P1-04
waits for P1-07 in a later batch. Section numbering follows the ROADMAP;
it does not require waiting at a blocked task when another task's Depends
are satisfied. Recheck the importer and evidence inventories at every PR.

## File structure

| Path | Kind | Responsibility |
| --- | --- | --- |
| `src/forge/project_config.py` | moved source | Same config objects, read policy, parsing and cache |
| Config importers under `src/`, `tests/` | modified | Import the new module without behavior changes |
| `tests/test_project_config_location.py` | proposed new test | Fail before relocation; detect old imports and missing new home |
| `src/forge/adaptive/{publication_saga,saga_durable,saga_native,two_writer_qualification}.py` | proposed deletions | P1-02 cluster, with approved consumer migration |
| P1-03 modules in task inventory | proposed deletions | Five small clusters; retained tests and evidence catalogs repaired |
| `src/forge/adaptive/reference/` | proposed deletion after dependencies | Provider-shaped doubles leave the product |
| `pyproject.toml` | proposed contracts | Protected ownership modules, exact current importer allowlists |
| `tests/test_architecture_boundaries.py` and test-owned policy fixtures | preserved/adapted | Focused rules that imports cannot express |
| `src/forge/adaptive/boundary_registry.py` | proposed deletion | No production registry after the replacement evidence is green |
| `src/forge/adaptive/closure_matrix.py`, `docs/operations/closure-matrix.md` | proposed deletions | Retire the matrix and repair active links |
| `docs/reference/edge-cases.md` | reviewed additions/repointing | Rules and pins survive source movement/deletion |
| `test-ledger.csv`, `scripts/ratchet-baseline.json` | regenerated | Preserve curated columns and record only shrunk/transferred debt |

---

## Deletion rehearsal and constraints (Tasks 2, 3, 4 and 6)

**Provenance:** inspected local `main` at
`0bf2a4e26ec48429c157e789df49db609aa700c3` and rehearsed only in
`/tmp/forge-p1-deletions-rehearsal` (detached HEAD at that commit).
The environment uses a directory symlink to the existing `.venv`, with
`PYTHONPATH=src` so imports and child processes resolve the scratch source.
A direct import resolved to
`/private/tmp/forge-p1-deletions-rehearsal/src/forge/__init__.py`.
No commit, push, merge, tag or settings change was made. Every temporary
deletion was restored. The curated ledger columns, register and invariant
registry were unchanged.

The four absence-check blocks in Tasks 2, 3, 4 and 6 were run against
that deletion scratch baseline. Their red results prove the proposed
checks can detect the current code;
they do **not** establish a green implementation. Conditional deletion and
porting steps remain prose until the owner completes the listed ledger
review. The scope amendments were adopted on 2026-10-23. Rehearse those exact patches and their gates before
executing them on a task branch.
Each of those four blocks also exited 0 when its target paths were absent
in scratch; every path was then restored. This proves the deletion check,
not the complete implementation or its full gates.

### Task 1: Extract project config from the legacy package (ROADMAP P1-01)

**Goal:** unblock the legacy deletion while retaining typed config reads, frozen run scope, cache authority and the distinct tolerant review wrapper.

**Depends:** local Phase 0 gates and the reviewed ledger. The owner explicitly waived the hosted prerequisites for local implementation on 2026-10-23; historical rehearsals alone do not satisfy local verification.

**Files:**
- Move: `src/forge/orchestrator/project_config.py` to `src/forge/project_config.py`.
- Create: `tests/test_project_config_location.py`.
- Modify: every source and test importer below; `scripts/build_test_ledger.py` (the shared-config classification only), `scripts/ratchet-baseline.json`, `docs/reference/edge-cases.md`, the measured columns of `test-ledger.csv`, the repo-map entry in `AGENTS.md`, and the task Status after completion.

**Exact importer inventory at `0bf2a4e`:** 14 production files: `adaptive/system_context.py`, `agents/base.py`, `agents/chat.py`, `doctor.py`, `findings/triage.py`, `flows/runner.py`, `mcp_client/manager.py`, `orchestrator/matcher.py`, `orchestrator/orchestrator.py`, `reactive/ci_debug.py`, `runs/azure_service.py`, `runs/github_service.py`, `runs/revival.py`, `runs/service.py` (all under `src/forge/`). Eleven test importers: `test_adaptive_discovery_stage.py`, `test_adaptive_system_context.py`, `test_azure_runs.py`, `test_budget_wiring.py`, `test_config_scope.py`, `test_doctor.py`, `test_findings_triage.py`, `test_github_runs.py`, `test_matcher.py`, `test_project_config.py`, `test_runs_spec.py` (all under `tests/`). The synthetic pre-move fixture in `test_script_build_test_ledger.py` remains an intentional test of the ledger's historical MOVED classification, rather than an importer of the live module. Re-run inventory before applying this step if main changes.

- [ ] **Step 1: Add the location contract and neighbor-shape pinning tests.** Save as `tests/test_project_config_location.py`:

```python
"""Project configuration is independent of the legacy orchestration package."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


def test_project_config_reader_has_a_shared_home():
    module = importlib.import_module("forge.project_config")
    assert module.ProjectConfig.__module__ == "forge.project_config"
    assert module.ConfigReadResult.__module__ == "forge.project_config"
    assert callable(module.read_project_config)


def test_project_config_has_no_legacy_importers():
    root = Path(__file__).resolve().parents[1]
    assert not (root / "src/forge/orchestrator/project_config.py").exists()
    old_name = "forge.orchestrator." + "project_config"
    remaining = [
        str(path.relative_to(root))
        for directory in ("src", "tests")
        for path in (root / directory).rglob("*.py")
        if old_name in path.read_text(encoding="utf-8")
    ]
    assert not remaining, remaining


@pytest.mark.parametrize("content", ["neighbors: wrong-shape\n", "neighbors: [1]\n"])
def test_malformed_neighbor_authorization_is_rejected(content):
    module = importlib.import_module("forge.project_config")
    with pytest.raises(ValueError, match="neighbors"):
        module._parse_project_config(content)


async def test_default_triage_runner_passes_shared_config_and_preserves_failure(
    monkeypatch, test_settings, tmp_path
):
    from forge.agents.models import AgentResult, SecurityTriageResult
    from forge.agents.security_triage import SecurityTriageAgent
    from forge.config import ForgeConfig
    from forge.findings.models import SecurityFinding
    from forge.findings.triage import _default_triage_runner

    project_config = importlib.import_module("forge.project_config").ProjectConfig
    settings = test_settings.model_copy(
        update={"FORGE_AGENTS_DIR": str(Path(__file__).resolve().parents[1] / "agents")}
    )
    finding = SecurityFinding(
        provider="gitlab",
        scope="42",
        source="gitlab_sast",
        fingerprint="f" * 64,
        severity="high",
        title="SQL injection",
        path="app/db.py",
        line=7,
    )
    diagnosis = SecurityTriageResult(summary="Use a parameterized query", risk_level="high")
    outcomes = [
        AgentResult(success=True, security_triage=diagnosis),
        AgentResult(success=True),
    ]
    constructed = []

    async def run(agent):
        constructed.append(agent)
        assert type(agent.project_config) is project_config
        assert agent.project_config.implement_paths == []
        assert agent.project_config.review_rules == []
        assert agent.project_config.enabled_agents is None
        assert agent.context.project_path == "group/project"
        assert agent.context.event_type == "security_triage"
        assert agent.context.security_reports[0].findings[0].id == finding.fingerprint
        assert "SQL injection" in agent._build_user_message()
        assert agent.definition.name == "security-triage"
        assert agent.model.id == "openai/strong"
        assert agent.gitlab is None
        return outcomes.pop(0)

    monkeypatch.setattr(SecurityTriageAgent, "run", run)
    config = ForgeConfig(tmp_path / "missing-forge.yml")
    result = await _default_triage_runner(settings, config, [finding], project_path="group/project")
    assert result is diagnosis
    constructed[0].project_config.review_rules.append("first call only")
    with pytest.raises(RuntimeError, match="security-triage agent failed: no output"):
        await _default_triage_runner(settings, config, [finding], project_path="group/project")
    assert len(constructed) == 2
    assert constructed[0].project_config is not constructed[1].project_config


async def test_default_debug_runner_passes_shared_config_and_preserves_failure(
    monkeypatch, test_settings, tmp_path
):
    from forge.agents.models import AgentResult, PipelineDebugResult
    from forge.agents.pipeline_debugger import PipelineDebuggerAgent
    from forge.config import ForgeConfig
    from forge.gitlab.schemas import Job
    from forge.reactive.ci_debug import _default_debug_runner

    project_config = importlib.import_module("forge.project_config").ProjectConfig
    settings = test_settings.model_copy(
        update={"FORGE_AGENTS_DIR": str(Path(__file__).resolve().parents[1] / "agents")}
    )
    jobs = [
        Job(id=11, name="unit-tests", stage="test", status="failed"),
        Job(id=12, name="missing-log", stage="test", status="failed"),
    ]
    logs = {11: "ValueError: incompatible dependency\n"}
    diagnosis = PipelineDebugResult(summary="Align the dependency versions")
    outcomes = [
        AgentResult(success=True, pipeline_debug=diagnosis),
        AgentResult(success=False, error="model failed"),
    ]
    constructed = []

    async def run(agent):
        constructed.append(agent)
        assert type(agent.project_config) is project_config
        assert agent.project_config.implement_paths == []
        assert agent.project_config.review_rules == []
        assert agent.project_config.enabled_agents is None
        assert agent.context.project_path == "group/project"
        assert agent.context.event_type == "pipeline"
        assert agent.context.failed_jobs == jobs
        assert agent.context.job_logs == logs
        prompt = agent._build_user_message()
        assert "ValueError: incompatible dependency" in prompt
        assert "missing-log" in prompt and "(no log available)" in prompt
        assert agent.definition.name == "pipeline-debugger"
        assert agent.model.id == "openai/strong"
        assert agent.gitlab is None
        return outcomes.pop(0)

    monkeypatch.setattr(PipelineDebuggerAgent, "run", run)
    config = ForgeConfig(tmp_path / "missing-forge.yml")
    result = await _default_debug_runner(settings, config, jobs, logs, project_path="group/project")
    assert result is diagnosis
    constructed[0].project_config.review_rules.append("first call only")
    with pytest.raises(RuntimeError, match="pipeline-debugger agent failed: model failed"):
        await _default_debug_runner(settings, config, jobs, logs, project_path="group/project")
    assert len(constructed) == 2
    assert constructed[0].project_config is not constructed[1].project_config
```

- [ ] **Step 2: Observe the red result.**

```bash
set -o pipefail
.venv/bin/python -m pytest -q tests/test_project_config_location.py
```

Expected, observed on pristine `main` at `0bf2a4e`: `6 failed`, exit 1. The new shared module does not exist and the legacy path remains. The malformed-neighbor cases fail to import that shared module; existing scope/cache behavior is pinned independently by the existing tests listed below.

- [ ] **Step 3: Register the config gotchas before moving the module.** Reserve EC-12 to EC-14; if another accepted task has taken an ID, obtain new unused IDs and re-rehearse the register. Save as `/tmp/forge-p1-config-edge-cases.py`:

```python
from pathlib import Path

path = Path('docs/reference/edge-cases.md')
text = path.read_text(encoding='utf-8')
rows = '''| EC-12 | An unreadable or invalid project config could widen an /implement run to the default scope | Only confirmed absence earns defaults; unreadable and invalid reads block before paid calls, and post-freeze paths use the frozen scope | `src/forge/project_config.py` | `tests/test_config_scope.py::TestConfigGateStartPath::test_403_parks_config_unreadable_before_any_paid_call`, `tests/test_config_scope.py::TestPermissionsNeverWiden::test_frozen_scope_survives_an_unreadable_config` |
| EC-13 | Two repositories in one Azure project or two provider connections could share a cached config | Cache by canonical provider, connection, repository, ref and config path; never use a bare project id or private adapter attributes | `src/forge/project_config.py` | `tests/test_project_config.py::TestRepositoryIdentityContractFND01::test_real_reader_identities_key_the_authority_cache`, `tests/test_project_config.py::TestCacheIdentityD01::test_two_refs_of_one_repository_are_different_snapshots` |
| EC-14 | A malformed scope or neighbor authorization could be silently treated as unrestricted or absent | Reject non-mapping config sections, malformed implement paths and malformed neighbor entries; preserve the legacy review wrapper's separate tolerant policy | `src/forge/project_config.py` | `tests/test_project_config.py::TestStrictPolicySchemaD02::test_a_string_paths_value_is_invalid_not_unrestricted`, `tests/test_project_config_location.py::test_malformed_neighbor_authorization_is_rejected`, `tests/test_config_scope.py::TestReadProjectConfig::test_legacy_wrapper_still_degrades_to_defaults` |
'''
assert '| EC-12 |' not in text and '| EC-13 |' not in text and '| EC-14 |' not in text
marker = '\n## Operational entries (the knowledge lives in the row)'
assert marker in text
path.write_text(text.replace(marker, '\n' + rows + marker), encoding='utf-8')
print('Registered EC-12 to EC-14 with existing scope/cache pins and neighbor-shape pins.')
```

Run `uv run python /tmp/forge-p1-config-edge-cases.py`. Expected: `Registered EC-12 to EC-14 ...`. The rows pin fail-closed scope, provider/connection/repository/ref cache identity and malformed authorization shapes, including the review wrapper's separate tolerant policy.

- [ ] **Step 4: Move the reader and update all importers.** Save as `/tmp/forge-p1-config-move.py`:

```python
from pathlib import Path
import ast

root = Path.cwd()
old_name = "forge.orchestrator.project_config"
new_name = "forge.project_config"
source = root / "src/forge/orchestrator/project_config.py"
target = root / "src/forge/project_config.py"
assert source.exists() and not target.exists()
text = source.read_text(encoding="utf-8")
replacements = {
    "shape the R14 blob-read contract": "shape the typed blob-read contract",
    "the R14 taxonomy": "the typed blob-read taxonomy",
    " (review finding A13)": "",
    "Policy (A13):": "Policy:",
    "# D01: the key": "# The key",
    "# cache crossed repository policies (review 44cdae D01, probe P01/P02).": "# cache must never cross repository policies.",
    "The canonical cache identity of one authority read (D01 → FND-01).": "The canonical cache identity of one authority read.",
    "contract owns the identity — adapters implement ``identity()``; this\n    helper NEVER probes private attributes (the 44cdae helper recognized\n    GitHub-style ``_owner``/``_repo`` but the REAL Azure reader carries\n    ``_project``/``_repo``, so two repositories of one project fell into\n    the project-id fallback and could share one policy entry — the first\n    remaining defect of the 05868e9 review).": "contract owns the identity — adapters implement ``identity()``.\n    Private adapter attributes cannot establish repository identity:\n    repositories within one Azure project must have separate policies.",
    "(A13):": ":",
    " (A13).": ".",
    " (A13 policy)": "",
    " (A13 §4)": "",
    "The authorized read-only neighbor repositories (NEXT-21 / R32-10, ": "The authorized read-only neighbor repositories (",
    '# Support both top-level and nested under "forge" key. D02: a PRESENT': '# Support both top-level and nested under "forge" key. A PRESENT',
    "# error handling (probe P04).": "# error handling.",
    "# D02: the nested structure": "# The nested structure",
    "# used to widen a typo into UNRESTRICTED scope, probe P03).": "# must never widen a typo into UNRESTRICTED scope).",
    "# NEXT-21 / R32-10: the authorized read-only NEIGHBORS — carried RAW": "# The authorized read-only NEIGHBORS — carried RAW",
    "# validated here, the same D02 doctrine `implement.paths` applies: a": "# validated here, the same `implement.paths` rule applies: a",
    "(reusing the R14\n    primitives — the config is fetched over the same provider surface).": "(the config is fetched\n    over the same provider surface as base-content reads).",
}
for before, after in replacements.items():
    assert before in text, before
    text = text.replace(before, after)

def without_docs(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr):
                value = node.body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    node.body = node.body[1:]
    return tree

before = without_docs(ast.parse(source.read_text(encoding="utf-8")))
after = without_docs(ast.parse(text))
for node in ast.walk(before):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        node.value = node.value.replace(
            "The authorized read-only neighbor repositories (NEXT-21 / R32-10, ",
            "The authorized read-only neighbor repositories (",
        )
assert ast.dump(before, include_attributes=False) == ast.dump(after, include_attributes=False)
target.write_text(text, encoding="utf-8")
source.unlink()
for directory in ("src", "tests"):
    for path in (root / directory).rglob("*.py"):
        content = path.read_text(encoding="utf-8")
        if old_name in content:
            path.write_text(content.replace(old_name, new_name), encoding="utf-8")
ledger_script = root / "scripts/build_test_ledger.py"
content = ledger_script.read_text(encoding="utf-8")
content = content.replace('    "forge.orchestrator.project_config",', '    "forge.project_config",')
ledger_script.write_text(content, encoding="utf-8")
print("Config moved; importers updated; executable AST preserved except Field.description.")
```

Run `uv run python /tmp/forge-p1-config-move.py`. Expected: `Config moved; importers updated; executable AST preserved except Field.description.` The script removes review references from comments and docstrings, including the `neighbors` Field description. It verifies that no executable expression changes apart from that explanatory description. Keep the original `MOVED` exemption in the ledger script: it describes the pre-move legacy package and is tested with a synthetic old tree.

- [ ] **Step 5: Observe and transfer only the reviewed size debt.**

Run `uv run python scripts/ratchet.py` before updating. Expected, observed: exit 1 for the new 88-line `_parse_project_config` and the stale debt at the old path. Do not split or rewrite the parser as part of this move. Its existing 88-line debt transfers, without a larger allowance:

```bash
uv run python scripts/ratchet.py --update --accept src/forge/project_config.py
uv run python scripts/ratchet.py
```

Expected, observed: `accepted ...::_parse_project_config (88 lines)`, followed by a green ratchet. This is the reviewed-move exception in AGENTS.md rule 7, not a growth waiver.

- [ ] **Step 6: Verify the shared reader and its existing consumers.**

```bash
set -o pipefail
.venv/bin/python -m pytest -q tests/test_project_config_location.py tests/test_project_config.py tests/test_config_scope.py tests/test_matcher.py tests/test_adaptive_system_context.py tests/test_script_build_test_ledger.py tests/test_edge_case_register.py
```

Expected, observed with the three new register rows: `145 passed`. The two default-runner tests execute real registry loading, model construction, contexts, shared config and prompt builders; only LLM agent execution is replaced. They pin typed config/default policies, fresh per-call config, diagnosis propagation, failure refusal and missing-log fallback. Appending their focused coverage to a copy of the full-run data raised diff coverage of the eight changed measured lines from 75% to 100%. The full default tier and Invariant Suite remain mandatory in the common gates; this focused run does not replace them.

- [ ] **Step 7: Preserve ledger ownership, document the move, and use the common completion steps.**

Run `uv run python scripts/build_test_ledger.py`, then check that the curated columns remain unchanged for every pre-existing row:

```python
import csv
import io
import subprocess
from pathlib import Path

path = 'docs/roadmap/2026-09-29-simplification/test-ledger.csv'
before = {row['file']: row for row in csv.DictReader(io.StringIO(subprocess.check_output(['git', 'show', f'HEAD:{path}'], text=True)))}
after = {row['file']: row for row in csv.DictReader(Path(path).open())}
columns = ('disposition', 'phase', 'note')
assert all(tuple(row[key] for key in columns) == tuple(after[file][key] for key in columns) for file, row in before.items())
print(f'Curated columns preserved in all {len(before)} pre-existing ledger rows.')
```

Expected, observed on this main snapshot: all 303 pre-existing rows preserve `disposition`, `phase` and `note`. Update the AGENTS repo-map entry to the new shared path; its import and behavior stay unchanged. One task, one PR: use the common gates before each commit and prose lint immediately afterward. No push or PR publication until the owner requests it.

### Task 2: Remove the publication-saga cluster (ROADMAP P1-02)

**Goal:** remove authority code reached only by tests; retain the working
publisher's intent, recovery and human-edit evidence.

**Depends:** ROADMAP P0-12 and P0-13, plus local Phase 0 verification.
The owner confirmed the ledger review and waived the hosted prerequisites
for local implementation on 2026-10-23.
The owner ledger review is a real prerequisite: `deleted_by` alone never
authorizes a test-file deletion.

**Files:**
- Delete: `src/forge/adaptive/{publication_saga,saga_durable,saga_native,two_writer_qualification}.py`.
- Delete only after owner review permits it: `tests/test_adaptive_publication_saga.py`.
- Retain/port or obtain an explicit owner disposition change: the six
  files in the table below.
- Modify as necessary: `src/forge/adaptive/boundary_registry.py`,
  `tests/test_architecture_boundaries.py`, `tests/test_reference_separation.py`,
  the edge-case register, the measured ledger columns and ratchet baseline.
- Scope decision: `src/forge/adaptive/reference/native_shaped_remote.py`
  depends on two modules this task deletes; moving/removing that double
  early overlaps P1-04; the implementation request adopts this narrow scope adjustment.

| Ledger file | Current disposition | Required treatment |
| --- | --- | --- |
| `tests/test_adaptive_publication_saga.py` | `drop-with-code` | Whole-file deletion only after the owner's ledger review |
| `tests/test_adaptive_two_writer.py` | `keep` | Characterize surviving digest, evidence and credential-scope behavior on runtime owners; owner decides obsolete multiwriter-only cases |
| `tests/test_saga_durable.py` | `keep` | Port surviving intent/recovery/human-edit assertions to the real publisher; owner decides obsolete saga-only cases |
| `tests/test_saga_native.py` | `port-adapter` | Keep provider CAS, marker/parent, delayed-apply, negative-probe, scoped-token and human-edit assertions on real writer/client seams over `FakeGitLab` and `FakeGitHub` |
| `tests/production_entry/test_two_writer_durable.py` | `port-engine` | Keep the file until the owner chooses current single-run recovery characterization versus the future engine scenario home |
| `tests/production_entry/test_two_writer_native.py` | `port-engine` | Keep the file; it also imports P1-03 code. Owner chooses which live multiwriter-only cases retire and which provider publication races remain |
| `tests/test_system_verification.py` | `keep` | Port its lazy two-writer-readiness interoperability test to the surviving `EvidenceLedger`/verification owner or explicitly retire that obsolete case; the rest remains until P1-03 |
| `tests/test_reference_separation.py` | `drop-with-code` | Partly removed by this task; retain its system-twin and steering checks until their remaining subjects are removed |

None of these eight rows currently has `protected` set. Existing protected
evidence in the Invariant Suite and edge-case register still cannot be
removed. New pins added here become protected when the ledger regenerates.

- [ ] **Step 1: Run the failing deletion check**

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
from pathlib import Path

paths = [
    Path("src/forge/adaptive") / f"{name}.py"
    for name in (
        "publication_saga", "saga_durable", "saga_native",
        "two_writer_qualification",
    )
]
remaining = [path.as_posix() for path in paths if path.exists()]
assert not remaining, f"publication-saga code remains: {remaining}"
PY
```

Observed before deletion: exit 1, listing all four source files. After an
approved implementation this same check must exit 0.

- [ ] **Step 2: Audit and characterize before removing imports**

The static graph classifies all four modules as test-only. However,
`reference/native_shaped_remote.py` imports `publication_saga` and
`saga_durable`, and `saga_durable` lazily re-exports that double. Searching
only tests misses this source caller. `boundary_registry` also holds saga
owner/dependent records and reference compatibility homes by string.
With only the four modules removed, its validation produced **2 failed,
68 passed**: boundary registration and reference separation both detected
stale records. Repair the affected `allowed_dependents` and
`REFERENCE_COMPAT_HOMES` values while preserving checks on all survivors.

Baseline collection of the six retained/port files produced 218 tests.
Deleting only the four source modules in scratch produced **5 collection
errors and 60 collected tests**; the latter hides one additional lazy
failure. Running
`tests/test_system_verification.py::TestFrozenShapes::test_the_ledger_interops_with_the_two_writer_readiness`
then produced **1 failed**, because it imports
`forge.adaptive.two_writer_qualification` inside the test.

Before deletion, write or reuse characterization on the live publisher
for every retained behavior: intent before the call; a lost answer adopted
by marker and parent; ambiguous/negative/unavailable probes keep unknown
outcomes without redispatch; stale CAS/human edits preserve the foreign
head; one draft per run; committed effects remain visible after cancellation.
See each new test fail with a local mutation of the relevant runtime rule.
Keep the existing EC-08 and EC-09 pins and I3/I6/I7 evidence unchanged.

- [ ] **Step 3: Settle the double and retained-test scope**

Recommended disposition: use existing `tests/fixtures/fake_gitlab.py` and
`fake_github.py` for the ported writer/client assertions. If a ported test
still needs `NativeShapedRemote`, move the provider-shaped double into
`tests/fixtures/native_shaped_remote.py` with fixture-local record/error
types and **no** import of deleted saga modules; remove the source double
as required importer repair in P1-02. This is an explicit P1-04 scope
adjustment adopted on 2026-10-23. Moving the double alone does not repair any of the
six retained test files; each must use a surviving production seam.
Do not copy the obsolete coordinator or qualification implementation into
fixtures merely to keep its old tests green.

If an entire retained file only proves deferred multi-repository behavior,
the owner must change its disposition before it can go. No port target
should be invented for an engine that has not landed yet. Keep P1-02
pending until those choices are recorded.

- [ ] **Step 4: Register lessons, then delete and repair same-task callers**

Audit the four module docstrings, refusal types, tests and failure windows
for additional gotchas. EC-08 already covers adopt-before-republish; EC-09
covers preserving a human tip. Record saga-specific forward-only recovery,
read-only neighbour credential confinement and partial committed effects
in rows before removal. Port a row to a real publisher test if that rule
still applies; otherwise retire it with the reason that multi-repository
writes are deferred by ADR-0035, preserving its historical pin and origin.
Use fresh IDs from EC-15 onward; EC-12 through EC-14 are reserved for P1-01.

Remove the four planned modules, the separately owner-approved
`reference/native_shaped_remote.py` importer repair, and only
owner-authorized test files/cases.
Drain `test_reference_separation` compatibility checks for deleted saga
paths while preserving all remaining separation assertions. Remove only
the registry owners/dependents and reference compatibility homes whose
subjects vanished; keep the registry validator and every surviving rule.
Repoint/remove active documentation links to deleted files truthfully;
historical planning records remain historical.

- [ ] **Step 5: Verify and close one P1-02 PR**

Repeat the absence check, collect and run every retained/ported file, run
register and invariant integrity checks, the Invariant Suite and the
shared precommit gates. Regenerate measured ledger columns and lower the
ratchet baseline. The PR reports the before/after metrics, characterization
mutations, all retired/ported rows, and the approved double scope change.
Do not mark this task done while any retained import or missing pin remains.

### Task 3: Remove test-only adaptive modules (ROADMAP P1-03, five PRs)

**Goal:** remove the 18 named test-only modules, without deleting retained
behavior tests or leaving dishonest catalog evidence.

**Depends:** P1-02 completed by its Done when, plus the batch prerequisites.

**Files and PR clusters:** each PR belongs only to P1-03 and contains 3–4
modules. The table is an inventory at the rehearsal base, not a production
size claim to keep current after deletion.

| P1-03 PR | Source modules | Test files affected |
| --- | --- | --- |
| A | `adaptive/workpackage_service.py`, `runtime.py`, `dedup.py`, `snapshots.py` | `test_adaptive_workpackage_service.py` (**keep**); `test_adaptive_runtime.py`, `test_adaptive_dedup.py`, `test_adaptive_snapshots.py` (**drop-with-code**). The first source loses its saga caller only after P1-02 |
| B | `adaptive/compat.py`, `compat_fixtures.py`, `read_guards.py`, `project_map.py` | `test_adaptive_compat_fixtures.py` (**keep**); `test_adaptive_compat.py`, `test_adaptive_read_guards.py`, `test_adaptive_project_map.py` (**drop-with-code**) |
| C | `adaptive/benchmark.py`, `system_manifest.py`, `impact.py` | `test_adaptive_benchmark.py`, `test_adaptive_system_manifest.py`, `test_adaptive_impact.py` (**drop-with-code**); delete `impact` and its imported `system_manifest` together |
| D | `adaptive/dotnet_recipe.py`, `verification_executor.py`, `system_verification.py`, `independent_checks.py` | `test_verification_executor.py`, `test_system_verification.py`, `test_adaptive_independent_checks.py` (**keep**); `test_dotnet_recipe.py`, `production_entry/test_system_verification_executor.py` (**drop-with-code**). `dotnet_recipe` imports `verification_executor`, which imports `system_verification`; delete together |
| E | `adaptive/executed_evidence.py`, `runs/conformance.py`, `agents/classifier.py` | `test_adaptive_executed_evidence.py`, `test_classifier.py` (**drop-with-code**); `test_conformance.py` (**port-adapter**) |

All test paths in the table are under `tests/`. Shared rows also affected:
`tests/production_entry/test_two_writer_native.py` (**port-engine**, P1-02
and P1-03) and `tests/test_reference_separation.py` (**drop-with-code**,
P1-02/03/04/07). None of the 21 P1-03 rows currently has `protected` set.
Keep/port files stay unless the owner changes their curated disposition.

Other files needing same-task reference repair:
`src/forge/adaptive/boundary_registry.py`,
`src/forge/adaptive/closure_matrix.py`, `src/forge/release_manifest.py`,
`src/forge/capability_manifest.py`, their existing validation tests, affected
active docs, the ledger, register and ratchet baseline. These are narrow
repairs for removed subjects, not permission to delete P1-05/06/07 modules
or weaken their validators early.

- [ ] **Step 1: Run the failing removal check for the full inventory**

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
from pathlib import Path

adaptive = (
    "workpackage_service", "runtime", "compat", "compat_fixtures", "dedup",
    "snapshots", "impact", "project_map", "read_guards", "benchmark",
    "system_manifest", "system_verification", "executed_evidence",
    "independent_checks", "verification_executor", "dotnet_recipe",
)
paths = [Path("src/forge/adaptive") / f"{name}.py" for name in adaptive]
paths += [Path("src/forge/runs/conformance.py"), Path("src/forge/agents/classifier.py")]
remaining = [path.as_posix() for path in paths if path.exists()]
assert not remaining, f"planned test-only modules remain: {remaining}"
PY
```

Observed at baseline: exit 1 listing all 18 files. Run the corresponding
3–4-file subset in each PR first, see it fail, then remove that cluster
only. The full check must pass when all five P1-03 PRs are complete.

- [ ] **Step 2: Audit dynamic, string and tooling references in each cluster**

Search the module's full name, basename, literal `import_module`,
`python -m` launches, templates, `[project.scripts]`, `.github/`, `ci/`,
`scripts/`, `tools/` and all of `tests/`, including helper files.
`verification_executor` launches its own module in child processes,
`dotnet_recipe` has a `python -m` entry, and runtime comments/catalog
strings refer to `compat_fixtures`, `dedup` and verification helpers even
though they do not import them. Source name searches supplement the static
graph; neither substitutes for retained-suite collection and execution.

- [ ] **Step 3: Decide and characterize the retained files cluster by cluster**

PR A: `test_adaptive_workpackage_service` is entirely about a deferred
multiwriter coordinator. Recommend an owner-reviewed `drop-with-code`
decision for those cases only after the child intent-before-dispatch,
idempotence and unknown-outcome lessons have living single-run pins or
are retired as no longer applicable. Keeping its current row without a
real port target means this cluster stays blocked.

PR B: keep immutable serialized compatibility examples under
`tests/fixtures/`; test real remaining RunSpec/checkpoint/control readers
for unknown versions, audit-only legacy views, exact checkpoint binding,
digest refusal and ordered index recovery. Avoid moving a second
compatibility interpreter from production into fixtures. The owner decides
which old fixture-only interpretations no longer describe a shipped reader.

PR D: preserve candidate path/authority/usage/base rejection scenarios from
`test_adaptive_independent_checks` on actual candidate validation,
publication-intent and mailbox owners, and digest/staleness/unknown
evidence from `test_system_verification` on actual verification owners.
Retire only the owner's chosen multi-repository/twin/executor-only cases.
`test_verification_executor`'s separate verification process is itself being
removed; do not fake a port to a nonexistent product verifier. The owner
must decide that file's surviving assertions or drop disposition first.

PR E: keep `test_conformance` behavioral batteries on real GitHub/GitLab
and current Azure publication seams until P1-11. Replace production
capability/attestation bookkeeping with explicit test parameters and
scenario coverage; retain stale-head, deny-list, approval-digest,
cancelled-generation, required-CI and exact validated-manifest assertions.
This file also imports `test_azure_runs` helpers; do not remove its Azure
coverage ahead of the authorized Azure task.

- [ ] **Step 4: Register every module's lessons before deletion**

The characterization review must cover these categories, as an explicit
module-to-register mapping in each PR:

| Module(s) | Lessons to port to live owners or retire explicitly |
| --- | --- |
| `workpackage_service` | Child start intent precedes row creation, crash replay is idempotent, read-only members get no run, only terminal outcomes at the frozen world advance |
| `runtime` | Unknown driver support fails closed, pause is applied at a checkpoint, foreign/mismatched restore and path traversal are refused |
| `dedup`, `snapshots` | Cross-transaction insert/read idempotence, sequence wins over delivery order, immutable read-only snapshots, path confinement, credential stripping |
| `compat`, `compat_fixtures` | A flag alone grants nothing, active legacy runs prevent cutover, a skip never becomes pass, unknown/corrupt document versions and invented identity are refused |
| `read_guards`, `project_map` | A read failure is never absence; digest verification precedes update; generated/vendor paths excluded; observed citations differ from inferred confidence; discovered commands stay unvalidated |
| `benchmark`, `system_manifest`, `impact` | Synthetic observations remain labeled, unknown/credential-shaped topology input refused, directed consumer/provider edges and depth-capped omissions stay explicit |
| `dotnet_recipe`, `verification_executor` | Floating package/image pins, stale/missing/zero-test reports, missing SDK, unavailable isolation positive-control, credential leakage and wheel digest mismatch never pass |
| `system_verification`, `independent_checks` | Evidence binds complete immutable world identity; schema upgrade preserves seeded data; crash/redelivery is exactly-once; candidate authority/path/receipt/base tampering fails closed; unknown tables/evidence stay explicit |
| `executed_evidence`, `runs.conformance`, `agents.classifier` | Boot/skip/unknown is not executed evidence; required scenarios cannot self-attest without passing; providers retain native CAS semantics; unknown/failed classifier output falls back to `general` |

Existing source/test pins are the starting evidence, not a new untested
promise. Inspect all tests/refusal branches, not only the examples above.
For each surviving rule, choose an existing runtime owner and a new or
existing meaningful pin; demonstrate a broken-rule mutation fails it.
Retire a row only when ADR-0035 actually removes that behavior. Existing
I1–I8/self-trigger evidence and EC-01 through EC-11 remain intact.

- [ ] **Step 5: Repair evidence catalogs in the same PR**

Scratch removal of nine currently `drop-with-code` test files used by
these catalogs produced **7 failed, 67 passed** in the focused catalog
tests: two closure-matrix validations, four release-manifest validations
and one capability-manifest validation. It is not safe to leave their
repair until P1-06/P1-07.

For each deleted evidence pointer, use a replacement only if a living test
proves the same capability/evidence level. Otherwise retire that specific
unsupported claim (or honestly lower it to pending with a surviving
reference, using the catalog's existing schema). Keep every existing
validator and missing-evidence negative test. Keep the closure-matrix
module/doc/test until P1-06; repair its executor cell when PR D removes its
pin, and regenerate its published matrix view. Do not change unrelated
claims or pretend a model/fixture proof is native executed evidence.

- [ ] **Step 6: Verify each cluster and close P1-03 accurately**

Run that cluster's absence check, every retained/ported suite, all affected
catalog/boundary validations and the shared default/Invariant/gate checks.
Regenerate the measured ledger and lower the ratchet baseline in every PR.
The task's existing Done when says zero test-only modules. At this base,
after the proposed P1-02 reference-leaf repair and the 18 P1-03 modules
go, `reference.system_twin`, the boundary registry and the closure matrix
remain test-only and belong to P1-04/05/06. If the owner chooses a different
reference-leaf repair, inventory that remaining set again. Thus
zero cannot be achieved by P1-03 alone, and P1-04/P1-06 depend on P1-03.

**ROADMAP correction adopted on 2026-10-23; include in the task PR:** P1-03 Done when becomes
“all 18 named modules are absent; no additional test-only module is left
without a later task; the remaining explicit P1-04/05/06 set is listed.”
Keep the Phase 1 exit criterion of **zero** test-only modules unchanged.
Do not silently mark the current impossible criterion done or add a fake
helper exemption to the metrics classifier.

### Task 4: Remove reference twins (ROADMAP P1-04)

**Goal:** reference doubles are absent from product source; remaining
behavior tests use fixtures and actual product seams.

**Depends:** P1-03 and P1-07 under the dependency repair adopted on
2026-10-23. Include the amendment below in the task PR; this task is deferred.

**Files:** `src/forge/adaptive/reference/{__init__,native_shaped_remote,system_twin,reactive_vendor}.py`;
`tests/test_reference_separation.py` (`drop-with-code`); actual surviving
double callers. `native_shaped_remote` may already be removed by the
approved P1-02 importer repair; do not repeat it.

- [ ] **Step 1: Run the failing deletion check**

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
from pathlib import Path

reference = Path("src/forge/adaptive/reference")
assert not reference.exists(), f"reference package remains: {reference}"
PY
```

Observed at baseline: exit 1. Deleting the reference directory alone in
scratch made causal-steering collection fail: **1 collection error and 39
collected tests**, because `steering_causality.__getattr__` still imports
`reference.reactive_vendor`. Static reachability classifies the reference
package and reactive vendor as tooling-only, not test-only.

- [ ] **Step 2: Resolve the remaining tooling dependency**

`scripts/run_steering_qualification.py` and `run_combined_steering.py` use
`adaptive/steering_causality.py`. That production tooling module imports
the reactive vendor for types, grammar and lazy re-exports, and loads it
by file path in its standalone spawn mode. The same module and scripts
are removed in P1-07. Its callers include
`test_causal_steering.py` (`drop-with-code`),
`test_combined_steering.py` (`keep`) and
`production_entry/test_causal_steering.py` (`port-engine`).

Moving `reactive_vendor` into `tests/fixtures` while keeping those source
imports would make the shipped module depend on tests, breaking the
package boundary. Extracting its grammar into new production code solely
to keep soon-to-be-deleted tooling alive adds scope and complexity.

**Narrow ROADMAP amendment adopted on 2026-10-23:** make P1-04 depend
on P1-07 as well as P1-03, and perform the whole reference-package removal
after P1-07 drains those callers. P1-07 depends on P1-06 and P1-08, so this
does not create a dependency cycle. If the owner requires P1-04 to finish
in this batch, a separately approved port of the entire qualification
tooling to `tools/` (including fixture ownership and standalone spawn)
must be planned and rehearsed; the current plan does not invent that move.

- [ ] **Step 3: Port surviving doubles and separation evidence**

After dependencies are satisfied, search direct/lazy/type-only/by-path
callers and use `tests/fixtures` fakes only from test files. Register the
double's native non-idempotent commit behavior, expected-head refusal,
lost-answer injection, duplicate-draft semantics and no-ambient-package
spawn lessons before removal; port living publisher/lane rules or retire
qualification-only behavior with reasons. Source protection remains in the
I1 and import-contract evidence. Delete `test_reference_separation` only
when its last supported subjects are removed and its owner-reviewed row
permits it. Keep retained steering tests until their disposition/port is
settled by P1-07.

- [ ] **Step 4: Verify and close P1-04**

The absence check must pass, no live source/tooling import or by-path
launch may reference `forge.adaptive.reference`, and all surviving test
fakes must live under `tests/fixtures`. Run shared gates and the Invariant
Suite; regenerate the measured ledger and ratchet baseline. The PR must
record its approved dependency amendment and actual remaining caller set.

### Task 5: Replace enforceable boundary registration with import contracts (ROADMAP P1-05)

**Goal:** one import boundary mechanism; retain every non-import safety rule that still applies.

**Depends:** local P0-07 import-contract verification and the reviewed ledger. The owner waived the hosted prerequisite for local implementation on 2026-10-23. The implementation request adopts retention of the kept architecture test and all surviving non-import checks. Registry retirement still requires a green replacement patch; historical candidate contracts alone are insufficient.

**Files:**
- Modify: `[tool.importlinter.contracts]` in `pyproject.toml`; ledger measured columns; ROADMAP Status only when the complete task lands.
- Create: `tests/test_boundary_contract_translation.py`.
- Preserve pending owner decision: `src/forge/adaptive/boundary_registry.py`, `tests/test_architecture_boundaries.py`, and all its violation traps and behavior tests.
- Registry removal will also need the `adaptive/execution_spec.py` registry reference updated, its surviving gotchas registered and test-only confinement constants rehomed. No untested deletion script is supplied as if this conflict were already resolved.

**Feasibility and contract semantics:** Import-linter 2.15's built-in `protected` contract expresses the existing registry's direct owner-importer rule. Use `as_packages = false` for exact module scope: permitting `forge.runs` as a package would inadvertently admit every run-service descendant. Include the declared owners themselves in `allowed_importers`, since co-owners were allowed to import each other by the original registry. Keep the two original ignore entries and add none. The direct-only `usecases` rule uses `allow_indirect_imports = true` because its existing rule forbids direct provider/gateway imports; the stronger existing Phase 0 durable/harness contracts are untouched.

The eleven ownership groups translate to eleven protected contracts: checkpoint authority, continuation, native occupancy, publication saga, verification applicability, operator projection, execution-delivery spec, durable budget amendment, feedback admission, command dedup and round admission. Add the reference importers/direction and fixture isolation contracts while their modules still exist, plus the provider-neutral usecases rule. After approved P1-02/P1-03/P1-04 deletions, omit owners and importers that have actually been removed in those tasks; retain all surviving ownership rules. Re-run this generator and its tests against the new base before execution. Module absence must come from an approved completed deletion task, never from hiding a violating importer.

**Provider writes and lease acquisition:** today's provider clients combine read and write methods, and package facades re-export authority symbols. Therefore “only publisher imports provider write paths” and “only admission acquires leases” cannot be enforced at symbol or call level by an import contract on those mixed modules. Protect the actual admission owner imports now; preserve the existing I2 publication tests and lease/database proofs. The ports/effects structure in Phase 3 will make write ownership an import-level rule. Do not substitute a broad provider-client whitelist or add ignored violations.

- [ ] **Step 1: Write the contract-preservation tests.** Save as `tests/test_boundary_contract_translation.py`:

```python
"""Authority import contracts preserve exact module scope and reviewed callers."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OWNERS = {
    "repository_identity_checkpoint_lifecycle": (
        "forge.adaptive.checkpoint_repository",
        "forge.api_checkpoint_channel",
    ),
    "continuation_authorization": ("forge.adaptive.continuation",),
    "native_occupancy": ("forge.adaptive.admission", "forge.runs.admission"),
    "candidate_publication": ("forge.adaptive.publication_saga", "forge.adaptive.saga_durable"),
    "verification_applicability": (
        "forge.adaptive.verification_binding",
        "forge.adaptive.verification_sets",
        "forge.runs.usecases",
    ),
    "operator_projection": ("forge.adaptive.operator_view", "forge.adaptive.operator_snapshot"),
    "execution_delivery_spec": ("forge.adaptive.execution_spec",),
    "budget_amendment_application": ("forge.durable.budgets",),
    "feedback_admission": ("forge.gateway.feedback",),
    "command_acceptance_dedup": ("forge.gateway.durable_ingress",),
    "review_round_admission": ("forge.runs.round_admission",),
}


def _module_path(module):
    base = ROOT / "src" / Path(*module.split("."))
    return base.with_suffix(".py") if base.with_suffix(".py").exists() else base / "__init__.py"


def _contracts():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return config["tool"]["importlinter"]["contracts"]


def _authorities():
    return {
        contract["id"].removeprefix("authority-"): contract
        for contract in _contracts()
        if contract.get("id", "").startswith("authority-")
    }


@pytest.mark.parametrize("name", OWNERS)
def test_every_surviving_authority_has_an_import_contract(name):
    owners = {module for module in OWNERS[name] if _module_path(module).exists()}
    if not owners:
        assert name not in _authorities(), "deleted owners must leave no stale contract"
        return
    assert name in _authorities()
    assert set(_authorities()[name]["protected_modules"]) == owners


def test_authority_contracts_protect_exact_modules():
    contracts = _authorities()
    assert contracts
    for contract in contracts.values():
        assert contract["type"] == "protected"
        assert contract["as_packages"] is False
        assert not contract.get("ignore_imports")


def test_registered_authority_importers_are_not_stale():
    contracts = _authorities()
    assert contracts
    for contract in contracts.values():
        owners = set(contract["protected_modules"])
        for importer in set(contract["allowed_importers"]) - owners:
            path = _module_path(importer)
            assert path.exists(), importer
            imported = set()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    imported.add(node.module)
                    imported.update(f"{node.module}.{alias.name}" for alias in node.names)
            assert imported & owners, f"{importer} no longer imports any declared owner"


def test_translation_adds_no_ignored_imports():
    ignored = {entry for contract in _contracts() for entry in contract.get("ignore_imports", [])}
    assert ignored <= {
        "forge.durable.budgets -> forge.adaptive.usage_ingestion",
        "forge.durable.budgets -> forge.runs.candidate",
    }
```

- [ ] **Step 2: Observe the missing-contract failure.**

```bash
set -o pipefail
.venv/bin/python -m pytest -q tests/test_boundary_contract_translation.py
```

Expected, observed before adding contracts on `main` at `0bf2a4e`: `13 failed, 1 passed`, exit 1. The unchanged ignore-debt check passes; every missing authority and scope/honesty check fails.

- [ ] **Step 3: Translate the existing direct import rules.** Save as `/tmp/forge-p1-boundary-contracts.py` while the production registry still exists:

```python
from pathlib import Path
import json

from forge.adaptive.boundary_registry import BOUNDARIES


def exists(module):
    path = Path('src').joinpath(*module.split('.'))
    return path.with_suffix('.py').exists() or (path / '__init__.py').exists()


def values(items):
    return json.dumps(list(items))


blocks = []
for boundary in BOUNDARIES:
    owners = [module for module in boundary.owner_modules if exists(module)]
    if not owners:
        continue
    importers = sorted(set(owners) | {module for module in boundary.allowed_dependents if exists(module)})
    blocks.append(
        '\n[[tool.importlinter.contracts]]\n'
        f'id = "authority-{boundary.name}"\n'
        f'name = "{boundary.name}: registered direct importers only"\n'
        'type = "protected"\n'
        f'protected_modules = {values(owners)}\n'
        f'allowed_importers = {values(importers)}\n'
        'as_packages = false\n'
    )
blocks.append(
    '\n[[tool.importlinter.contracts]]\n'
    'id = "usecases-provider-neutral"\n'
    'name = "usecases have no direct provider or gateway imports"\n'
    'type = "forbidden"\n'
    'source_modules = ["forge.runs.usecases"]\n'
    'forbidden_modules = ["forge.integrations", "forge.gateway"]\n'
    'allow_indirect_imports = true\n'
)
if exists('forge.adaptive.reference'):
    homes = [module for module in (
        'forge.adaptive.system_verification',
        'forge.adaptive.saga_durable',
        'forge.adaptive.steering_causality',
    ) if exists(module)]
    blocks.append(
        '\n[[tool.importlinter.contracts]]\n'
        'id = "reference-importers"\n'
        'name = "reference package has only registered compatibility importers"\n'
        'type = "protected"\n'
        'protected_modules = ["forge.adaptive.reference"]\n'
        f'allowed_importers = {values(homes)}\n'
    )
    blocks.append(
        '\n[[tool.importlinter.contracts]]\n'
        'id = "reference-direction"\n'
        'name = "reference package does not import production entry points"\n'
        'type = "forbidden"\n'
        'source_modules = ["forge.adaptive.reference"]\n'
        'forbidden_modules = ["forge.main", "forge.lane_driver", '
        '"forge.adaptive.wiring", "forge.runs.service", '
        '"forge.runs.github_service", "forge.runs.azure_service"]\n'
        'allow_indirect_imports = true\n'
    )
if exists('forge.adaptive.compat_fixtures'):
    blocks.append(
        '\n[[tool.importlinter.contracts]]\n'
        'id = "fixture-isolation"\n'
        'name = "production modules do not import qualification fixtures"\n'
        'type = "protected"\n'
        'protected_modules = ["forge.adaptive.compat_fixtures"]\n'
        'allowed_importers = []\n'
        'as_packages = false\n'
    )
path = Path('pyproject.toml')
original = path.read_text(encoding='utf-8')
assert 'id = "authority-' not in original, 'contracts already translated'
path.write_text(original + ''.join(blocks), encoding='utf-8')
print(f'Added {len(blocks)} contracts; no ignore_imports entries added.')
```

Run `uv run python /tmp/forge-p1-boundary-contracts.py`. Expected on the rehearsal snapshot: `Added 15 contracts; no ignore_imports entries added.` After earlier approved deletion tasks, the exact count is lower because their owner modules have disappeared; the surviving-owner tests still require all remaining groups. The script is a migration aid outside the repository, not a runtime replacement registry.

- [ ] **Step 4: Verify the contracts and the unchanged architecture evidence.**

```bash
uv run lint-imports --no-cache --no-logo
```

Expected, observed before P1-02/P1-03/P1-04: `Contracts: 17 kept, 0 broken`, with the same two ignored durable imports. The new translation test gives `14 passed`; the existing architecture/debt suite gives `71 passed`. Their combination is required; a green contract run alone does not prove the non-import rules.

- [ ] **Step 5: Mutation-test each new contract, then restore the tree.** Save as `/tmp/forge-p1-boundary-mutations.py`:

```python
from pathlib import Path
import subprocess
import tomllib

root = Path.cwd()
config = tomllib.loads(Path('pyproject.toml').read_text(encoding='utf-8'))
contracts = [contract for contract in config['tool']['importlinter']['contracts'] if contract.get('id')]
assert contracts
for contract in contracts:
    if contract['type'] == 'protected':
        path = root / 'src/forge/_boundary_contract_probe.py'
        assert not path.exists()
        original = None
        content = f"import {contract['protected_modules'][0]}\n"
    else:
        module = contract['source_modules'][0]
        base = root / 'src' / Path(*module.split('.'))
        path = base.with_suffix('.py')
        if not path.exists():
            path = base / '__init__.py'
        original = path.read_bytes()
        content = original.decode('utf-8') + f"\nimport {contract['forbidden_modules'][0]}\n"
    try:
        path.write_text(content, encoding='utf-8')
        done = subprocess.run(
            [str(root / '.venv/bin/lint-imports'), '--no-cache', '--no-logo', '--contract', contract['id']],
            capture_output=True, text=True,
        )
        assert done.returncode == 1 and '1 broken' in done.stdout, (
            contract['id'], done.returncode, done.stdout, done.stderr
        )
        print(f"{contract['id']}: forbidden import detected (exit 1)")
    finally:
        if original is None:
            path.unlink()
        else:
            path.write_bytes(original)
print(f"{len(contracts)} contract mutations rejected; source files restored.")
```

Run `uv run python /tmp/forge-p1-boundary-mutations.py`. Expected, observed: every one of the 15 new contracts rejects its forbidden import with exit 1, then `15 contract mutations rejected; source files restored.` Re-run the import gate: `17 kept, 0 broken`. No mutation or probe file may be committed.

- [ ] **Step 6: Resolve the non-import evidence before retiring the registry.**

The implementation request adopts retention of all surviving checks as focused tests. Move only their allow-set constants into test support, and source import ownership from TOML. Retiring any still-applicable rule instead requires a separate owner decision. Keep the current test file while its ledger disposition is `keep`. If the owner instead authorizes a file rename or split, obtain that ledger decision first and port every still-applicable pin in the same PR. A replacement test must fail under the corresponding existing intentional violation, and Invariant Suite evidence may not be removed without an ADR.

| Rule not expressible as a module import | Existing evidence to retain or rehome | Reason |
| --- | --- | --- |
| Legacy checkpoint lookup only inside its opt-in adapter | `_check_legacy_lookup_confinement`; traps for legacy dialing, raw-index read and lookup outside revival adapter | Symbol references and enclosing function, not module dependency |
| `resolve_repository` call monopoly | `_check_resolve_repository_monopoly`; `test_an_unregistered_resolve_repository_call_is_caught` | Other permitted readers import the same owner; only selected callers compose it |
| `AttemptStartSpec` constructors and dispatch composition | `_check_attempt_start_construction`; service hand-build trap | Importing its type is not permission to construct it |
| Blob unlink only under locked GC sweep | `_check_gc_unlink_confinement`; `test_a_blob_unlink_outside_the_sweep_entries_is_caught` | Receiver and enclosing locked function |
| Continuation-mode construction and default vocabulary | `_check_continuation_mode_selection`; `test_a_mode_rederivation_is_caught` | Literal/call-site confinement inside a permitted owner consumer |
| Budget-amendment applications use one ledger | `_check_budget_amendment_applicants`; third-applicant and drained-applicant traps | Services consume package facade; applications are symbol-level operations |
| Persisted grant loaded before credential resolution | `_check_redemption_grant_authority`; missing-grant trap and real-endpoint contrast | Call ordering and inverse loading proof |
| Approved-input brief at every dispatch | `_check_approved_input_brief`; `test_a_spec_plan_summary_brief_in_the_dispatch_is_caught` | Dispatched expression and approved bytes, part of I4 |
| Locator allocation cannot bypass broker collision check | `_check_native_locator_allocation`; legacy-carrier trap and broker contrast | Which function is called through an allowed module |
| Factory-branch derivation stays in its home | `_check_factory_branch_confinement`; shortcut/new-module traps and real-service contrast | Symbol derivation, mixed modules |
| Cache-confirmed durable dedup and legacy SET-NX confinement | `_check_gateway_dedup_confinement`; probe, SET-NX and drained-helper traps | Cache call and inverse helper presence, not merely importing ingress |
| Round admission owns child-before-budget transaction | `_check_round_admission_confinement`; bypass, drained-service and drained-transaction traps | Child flush, budget call, row composition and ordering |
| Command-outcome classification parity | `_check_command_outcome_parity`; both refused-status directions, missing vocabulary and renamed-seam traps | Set and mapping values across two modules |
| Equivalent typed checkpoint refusals and provider resume-gap honesty | every `TestProviderConformance` test | Behavior and provider-specific documented gaps |
| Startup loads no fixture/reference module | `TestSyntheticFixtureIsolation::test_default_production_startup_loads_no_fixture_or_reference_module` | Independent runtime import probe, retained until its banned modules are gone |
| Epoch oracle ownership and envelope epoch binding | every `TestStaleEpochConformance` test | Typed refusal, digest behavior and symbol monopoly |
| Registration inverse honesty and lazy reference imports | stale-registration and lazy-import traps, plus `test_registered_authority_importers_are_not_stale` | An allowed-importer list must not become a stale permission grant |

The registry metadata-only tests can be replaced with the TOML schema/surviving-owner tests above when registry retirement is approved. Import-only reference/fixture checks can retire only after their modules' approved removal, or be replaced by the mutation-proven contracts while they exist. Retain the independent startup probe if a banned module still exists.

**Current stop condition:** do not mark P1-05 done, delete the registry or delete its kept test file on the strength of this rehearsal. Once the entry gate and P0-13 review are complete, finish a separate rehearsed retirement patch inside this same P1-05 PR, run each preserved violation trap and the complete common gates, update ratchet debt and measured ledger columns, then commit and run prose lint immediately. The complete P1-05 Done when remains “registry deleted and contracts green”; candidate contracts alone are not completion.

**Rehearsal provenance for these two sections:** detached scratch worktree `/private/tmp/forge-p1-config-boundaries-rehearsal`, from local `main` at `0bf2a4e26ec48429c157e789df49db609aa700c3`, with its own Python 3.13.11 environment. A second detached worktree `/private/tmp/forge-p1-config-final-red` replayed the final tests on the original tree and the final scripts in order. No commit, push, tag, merge, settings change, Phase 1 implementation in the owner's checkout or curated ledger edit was made. Mutation probes restored byte-identical source contents. Parent execution supplies the full default/Invariant results separately; focused results here do not claim GitHub acceptance.

### Task 6: Remove closure matrix (ROADMAP P1-06)

**Goal:** the product package no longer carries qualification evidence
grading; runtime invariants and their real pins remain.

**Depends:** P1-03 completed by the owner-approved corrected Done when,
plus the batch prerequisites.

**Files:** delete `src/forge/adaptive/closure_matrix.py`,
`docs/operations/closure-matrix.md` and `tests/test_closure_matrix.py`
(`drop-with-code`, no protected marker). Repair active documentation links
in `docs/adr/0033-execution-ownership-consolidation.md` and
`0034-execution-ownership-map.md`. The immutable Phase 0 plan, DESIGN,
ROADMAP and HANDOFF contain historical/task mentions and are not importers.

- [ ] **Step 1: Run the failing deletion check**

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
from pathlib import Path

paths = [
    Path("src/forge/adaptive/closure_matrix.py"),
    Path("docs/operations/closure-matrix.md"),
    Path("tests/test_closure_matrix.py"),
]
remaining = [path.as_posix() for path in paths if path.exists()]
assert not remaining, f"closure matrix remains: {remaining}"
PY
```

Observed before deletion: exit 1 listing all three files. In scratch,
removing just these files preserved register and invariant-registry
integrity: **31 passed** in `tests/test_edge_case_register.py` and
`tests/invariants/test_registry_integrity.py`. This focused result is not
a full default or Invariant Suite pass.

- [ ] **Step 2: Register the evidence honesty lessons**

Before deleting the module, record that a missing evidence path never
proves a capability; pending-human/customer-acceptance never means
proven; unit, process, native execution and customer acceptance remain
different evidence levels. The current pins are `test_closure_matrix.py`'s
validation and render cases. Retire matrix-specific grading rows with the
reason that ADR-0035 removes that catalog, while carrying continuing
release-evidence rules into the surviving release validators and then
P1-08's signed-smoke evidence. Do not remove existing invariant/register
rows merely because a qualification table is gone.

- [ ] **Step 3: Delete, repair active links and distinguish history**

Delete the three files only after the owner's reviewed ledger permits it.
Change the superseded ADRs' active links to the binding ADR-0035 decision
and mark their matrix description as historical. Preserve their historical
decision rather than rewriting it into a claim about current execution.
Keep the ledger task mapping in `build_test_ledger.py`, which is historical
disposition metadata. A literal zero-occurrence grep across the whole
repository cannot hold while the roadmap and immutable plans name this
task; “nothing references it” means no importer, active CLI/template,
current evidence pin or broken active documentation link. This Done when
clarification was adopted on 2026-10-23; include it in the task PR.

- [ ] **Step 4: Verify and close one P1-06 PR**

Repeat the absence check, scan runtime/tooling/test strings and active
documentation links, then run shared gates, register validation and the
Invariant Suite. Regenerate measured ledger columns; the PR must record the dropped row's owner-approved disposition; all
surviving rows retain their curated fields. Lower the ratchet
baseline. Keep the phase-level zero-test-only exit gate and all test/evidence
validators intact.


---

## Common verification and completion steps

- [ ] **Verify the entry gate again before opening a task branch.** Local
  `main` must equal the approved, pushed Phase 0 base on `origin/main`.
  Check every prerequisite above and the task's own Depends. P1-04 remains
  deferred under the P1-07 dependency adopted on 2026-10-23.
- [ ] **Rehearse the resolved implementation.** For a conditional deletion
  or registry-retirement patch, implement the approved test migrations in
  scratch first and run its characterization mutations and full gates.
  The absence probes in this plan do not substitute for that green patch.
- [ ] **Regenerate measured metadata after test/source changes.** Run
  `uv run python scripts/build_test_ledger.py` and compare curated columns
  to the parent ledger for every surviving row; list each removed row and
  its owner-approved disposition in the PR. Run
  `uv run python scripts/ratchet.py --update`
  after shrinkage. Only the reviewed P1-01 relocation uses `--accept`.
  Run `uv run python scripts/metrics.py` without `--write` for the PR delta;
  a phase-gate metrics snapshot is a separate closure action.
- [ ] **Stage the exact task files, then run all gates before each commit.**
  Stage new source files so diff coverage measures them. This block was
  run in the combined rehearsal scratch; the source import path is explicit
  because its venv was shared. `UV_NO_SYNC` avoids rebinding that editable
  environment; in a task branch run `uv sync` first. Comparison against
  `main` is valid only after the entry gate proves it equals `origin/main`.

```bash
set -euo pipefail
export UV_NO_SYNC=1
export PYTHONPATH="$PWD/src:$PWD"
.venv/bin/python -m pytest -q -m "not slow" --junitxml=/tmp/forge-p1-default.xml --cov=forge --cov=scripts --cov-report=xml:/tmp/forge-p1-coverage.xml
uv run diff-cover /tmp/forge-p1-coverage.xml --compare-branch=main --fail-under=85
.venv/bin/python -m pytest -q $(uv run python -m tests.invariants.registry) --junitxml=/tmp/forge-p1-invariants.xml
.venv/bin/ruff format .
.venv/bin/ruff check .
uv run mypy src/forge/durable src/forge/runs src/forge/repository src/forge/factory src/forge/execution src/forge/integrations
uv run lint-imports
uv run python scripts/ratchet.py
uv run python scripts/prose_lint.py --base origin/main
uv run python scripts/gate_public_artifacts.py
uv run python scripts/generate_template_pins.py --exact-composition
uv run python scripts/gate_conformance.py --report /tmp/forge-p1-conformance.json
git diff --check
```

Expected: pytest exits 0 with no leak banner; changed-line coverage is at
least 85%; formatting, lint, types, import contracts, ratchet, prose,
artifact, composition and conformance checks succeed; no whitespace error.
Local PostgreSQL-only skips must be reported. They do not satisfy the
GitHub invariant job's zero-skip requirement or the entry gate.

The formatter must leave the staged task files byte-identical. If it rewrites
anything, re-stage only the task files and restart the entire gate block
before committing. Do not commit an index that differs from the tested
working files; a lint pass alone does not validate the newly formatted bytes.

If the approved patch changes lifecycle behavior or a slow production-entry
path, also run `.venv/bin/python -m pytest -q -m slow` separately. A lifecycle
change needs the live-lab path in AGENTS.md: authorized issue, factory branch,
green CI, one Draft MR/PR and `ready_for_human`. Follow forge-lab and obtain
only the missing access needed for that run; no live experiment was done
by this planning task.

- [ ] **Commit only after all gates pass.** Use the repository's explained
  `area: change` subjects and a small commit sequence within one ROADMAP
  task. This planning session made no commits. No command here authorizes
  publishing a branch or opening a GitHub PR.
- [ ] **Immediately after every commit, run**
  `uv run python scripts/prose_lint.py --base origin/main`.
  The earlier invocation checks existing commits; it cannot prove the new
  commit's added source lines until that commit exists. If it fails, repair
  the source and repeat the complete precommit gates before the correction.
- [ ] **Complete the PR template locally and obtain an independent review.**
  Give invariants and their tests/mutations, before/after metrics, ADR impact,
  ledger/register changes, scope, size and any approved ROADMAP amendment.
  Re-review fixes. The owner must explicitly authorize remote publication
  and merges. Record task Status only when its actual Done when holds.

## Historical verification record — 2026-10-11

The following counts and locations record the original planning session.
Its raw `/tmp` worktrees and report files are no longer available as of
2026-10-23. They are historical provenance, not current executable proof.
Use the new implementation-entry evidence in
`.forge/verification/phase0-20261023/` for the current prerequisite audit.

The baseline setup used forge-setup, `uv sync`, and the uv-managed Python
3.13.11. PostgreSQL support was restored with `uv sync --extra postgres`
because the existing runtime configuration requests that backend. Ruff
left all source files unchanged; lint, mypy, original import contracts,
size ratchet, prose lint, public-artifact, exact-composition and conformance
checks passed. Doctor exited 1: configured Redis and LiteLLM cannot be
reached, and the configured database hostname does not resolve on macOS.
Read-only inspection found the Forge lab containers stopped. Redis is
configured at localhost:6379; doctor misleadingly reports a failed connection
as unset. The database uses the container-only `host.containers.internal`
hostname. Runtime setup is therefore incomplete, even though the hermetic
tests need no services. No lab service or configuration state was changed.
Secrets and the owner's `.env` were not changed or printed.

The first baseline default-tier run had two failures because `uv sync`
removed the optional PostgreSQL driver required by the existing `.env`.
After restoring that extra, the affected ingress tests passed and the full
default tier passed: **9,562 passed, 38 skipped, 189 deselected** in 573.46
seconds. This is local evidence, not proof of the 480-second Linux CI target.

The first combined scratch run passed its default tier (**9,587 passed,
38 skipped, 189 deselected**) but stopped at changed-line coverage: **75%**,
below the unchanged 85% threshold. Meaningful triage/debug runner wiring
tests cover the two missed lazy imports; focused coverage is now **100%**.
The exact final common gate block then exited **0**: **9,589 passed,
38 skipped, 189 deselected** in 659.94 seconds for the default tier;
**797 passed, 3 skipped** in 34.54 seconds for the registered Invariant
Suite; **100% changed-line coverage** (8 measured lines, 0 missing).
Ruff left 684 files unchanged, mypy checked 49 source files, import-linter
kept all 17 contracts with none broken, and ratchet/prose/artifact/exact-
composition checks passed. Conformance was green: 66 executed arm IDs
across 5 checks, all mutation self-tests caught, 0 schema findings. No
leak banner appeared. Local invariant skips do not meet the GitHub
zero-skip requirement; full Linux CI acceptance remains open. All three
Invariant Suite skips require a disposable `FORGE_PG_TEST_URL`: one
failure-injection collection and two real-PostgreSQL budget-race cases.

Historical rehearsal locations (raw artifacts now unavailable):

| Rehearsal | Location | What it demonstrates |
| --- | --- | --- |
| Configuration and contracts | `/private/tmp/forge-p1-config-boundaries-rehearsal` | Focused green tests, debt transfer, no changed curated columns, 15 contract mutations |
| Final red/green replay | `/private/tmp/forge-p1-config-final-red` | Exact final test/script blocks replayed on the original base |
| Deletion checks | `/tmp/forge-p1-deletions-rehearsal` | Each absence check red before removal and green with paths absent; all deletions restored |
| Combined local gates | `/tmp/forge-phase1-20261011/gates-scratch` | Config move, new edge rows and candidate contracts together; registry/tests retained |
| Baseline default log and JUnit | `/tmp/forge-phase1-20261011/default-repaired.log`, `default-repaired.xml` | Green owner checkout validation with no source changes |
| Combined gate log | `/tmp/forge-phase1-20261011/scratch-common-gates-final.log` | Exact final common command block execution |
| Final scratch reports | `/tmp/forge-phase1-20261011/scratch-default-final.xml`, `scratch-coverage-final.xml`, `scratch-invariants-final.xml`, `scratch-conformance-final.json` | Historical copies of the exact final gate outputs |
| Runtime inspection | `/tmp/forge-phase1-20261011/lab-service-state.txt` | Forge lab services stopped; no service state changed |
| Public GitHub audit | `/tmp/forge-ci-audit-public.json`, `/tmp/forge-phase1-20261011/gh-run-list-final.json` | Historical remote refs, CI jobs and run results |

These were isolated rehearsals, with no implementation commit, branch
publication, tag, merge or settings change. Their historical validation
does not waive dependencies. The current task work will re-establish
implementation evidence once the entry gate clears. Current local
prerequisite logs are retained in the ignored verification directory.

## Self-review record

- **Spec coverage:** P1-01 → Task 1; P1-02 → Task 2; P1-03 → Task 3's five
  PRs; P1-04 → Task 4; P1-05 → Task 5; P1-06 → Task 6.
- **Dependencies:** Phase 0 code presence is insufficient. P1-01 needs
  the complete Phase 0 exit; P1-02 needs reviewed ledger rows; P1-03 needs
  completed P1-02; P1-04/P1-06 need completed P1-03; P1-05 needs CI import
  enforcement and resolution of its kept-test conflict. Batch-wide release,
  recovery tag and owner ledger gates remain open. The adopted P1-04 delay is
  acyclic and clearly outside this batch's executable scope.
- **Evidence protection:** no change to the Invariant Suite, edge-case
  register or curated ledger in the owner's tree. Same-task consumer,
  registration and evidence-catalog repairs are required; no skip, ignore,
  missing-pointer exemption or metric-classifier exception is proposed.
- **Intentional proposal differences:** P1-02's dependent reference leaf
  goes with its cluster; P1-03 gets an achievable task criterion while the
  phase keeps zero test-only modules; P1-04 waits for tooling retirement;
  P1-05 retains focused non-import evidence; P1-06 preserves history while
  removing active references. The implementation request adopted these
  scope choices on 2026-10-23; the owner separately confirmed the ledger
  review that day without changing dispositions.
- **Limits of rehearsal:** the exact code blocks were tested as stated.
  P1-02/03/04 deletion migrations and P1-05 registry retirement are not
  validated green implementations; they await owner choices and another
  scratch rehearsal. The document does not claim these tasks are done.

## Follow-ups

- Get owner-authorized Phase 0 publication, new CI evidence, required
  invariant/coverage checks, maintenance branch and recovery tag before
  task execution. The owner ledger review was separately confirmed on
  2026-10-23; retain its unchanged dispositions.
- Incorporate the adopted ROADMAP scope amendments in the relevant task
  PRs after the entry gate clears. The implementation request does not
  replace the separate owner ledger review.
- After the entry gate clears, finish and rehearse the specific retained-test migrations
  and registry-retirement patch rather than bulk-deleting kept test files.
- Keep the default-tier time target visible. The plan does not move tests
  out of the default tier or relax the 480-second Phase 0/Phase 1 target.
- Do not create tracking issues, a Project board, release tags or archive
  branches during this planning batch; those need an exact owner request.
