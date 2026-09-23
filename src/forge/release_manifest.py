"""Release-evidence manifest (review A16 / R28): claims from evidence, not prose.

Every capability/status claim forge makes in README or CHANGELOG is backed by a
machine-readable manifest that THIS module generates. The manifest states, per
capability and provider/backend, WHICH level of evidence exists and WHERE it
lives — a test file, a CI job, or nothing. The discipline (A16 acceptance
criteria):

- A level is never claimed above what in-tree evidence proves. ``not_run`` and
  unknown stay EXPLICIT entries — they are never silently converted to pass.
- Boot canary, subprocess failure injection, coroutine failure injection and
  real-provider e2e are SEPARATE evidence classes. A green boot canary is not
  an SDLC e2e claim; a contract suite over fakes is not a live claim.
- Version consistency (the R30 release-guard discipline) is asserted at
  manifest build time: pyproject and ``forge.__version__`` must agree or the
  generator REFUSES to emit a manifest.

Levels (strongest last):
    implemented         code exists with unit-level tests
    contract_tested     CI contract suite (fakes/stubs), or a real-runtime
                        failure-injection suite — see ``evidence_class``
    live_canary_tested  exercised against the real runtime: the built release
                        artifact (boot/migrate canary) or the real provider
    not_run             no in-tree, CI-reproducible evidence — recorded, not
                        claimed

Run ``python -m forge.release_manifest`` (repo root) to emit the JSON; pass
``--out PATH`` to write it. The manifest carries the HEAD sha it describes;
regenerate per release rather than committing a drifting copy. The one
COMMITTED manifest artifact is the per-release snapshot archived by
:mod:`forge.release_promotion` under ``docs/releases/evidence/v<version>/``
— written once, at promotion time, and never regenerated after.

The seeded registry below is derived from the ACTUAL suite (grep-verifiable)
and is kept honest by ``tests/test_release_manifest.py``: every evidence
pointer must exist, every finding mapped to a closure must have landed test
evidence whose file contains the expected marker, and CI job names must exist
in the workflows. Findings WITHOUT verified in-tree evidence (A01, A04-A12
from the d16f523 review) are deliberately ABSENT from the closure table —
absence means "not claimed", never "closed".
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from . import __version__

__all__ = [
    "EvidenceClass",
    "FindingClosure",
    "Gating",
    "LEVEL_LEGEND",
    "ManifestEntry",
    "ManifestIntegrityError",
    "SCHEMA_VERSION",
    "build_manifest",
    "entries",
    "finding_closures",
    "render_manifest",
    "resolve_head_sha",
]

#: JSON schema version of the emitted manifest document.
SCHEMA_VERSION: Final[int] = 1

ManifestLevel = Literal["implemented", "contract_tested", "live_canary_tested", "not_run"]
EvidenceClass = Literal[
    "boot_canary",
    "subprocess_fi",
    "coroutine_fi",
    "contract_suite",
    "real_provider_e2e",
    "none",
]
Gating = Literal["pr_gate", "nightly", "release_tag", "manual", "none"]

LEVEL_LEGEND: Final[dict[str, str]] = {
    "implemented": "code + unit tests; no CI contract suite yet",
    "contract_tested": "CI suite over fakes/stubs, or a real-runtime failure-injection suite "
    "(see evidence_class); the platform/agent is NOT real in the loop",
    "live_canary_tested": "exercised against the real runtime — the built release artifact "
    "(boot/migrate canary) or the real provider in the dogfood loop",
    "not_run": "no in-tree, CI-reproducible evidence; recorded explicitly, NEVER counted as pass",
}

EVIDENCE_CLASS_LEGEND: Final[dict[str, str]] = {
    "boot_canary": "release-artifact canary: real alembic chain, boot gate, /health, doctor "
    "(scripts/canary_smoke.py)",
    "subprocess_fi": "real ``python -m forge.worker`` subprocesses SIGKILLed at each durable "
    "checkpoint (tests/test_failure_injection_os.py)",
    "coroutine_fi": "in-process workers killed at each checkpoint against real Postgres "
    "(tests/test_failure_injection.py)",
    "contract_suite": "pytest contract suite over fakes/stubs on the PR gate",
    "real_provider_e2e": "the full /implement → Draft MR/PR loop against a real provider "
    "(dogfood loop / live smoke script)",
    "none": "no evidence class applies (not_run entries)",
}

GATING_LEGEND: Final[dict[str, str]] = {
    "pr_gate": "runs on every push/PR (blocking CI job)",
    "nightly": "scheduled/manual CI job only — NOT on the PR gate",
    "release_tag": "runs in the tag-triggered release workflow before publish",
    "manual": "maintainer-run; not CI-reproducible from this tree",
    "none": "not run anywhere",
}

_PROVIDERS: Final[frozenset[str]] = frozenset({"gitlab", "github", "azure", "*"})
_BACKENDS: Final[frozenset[str]] = frozenset({"builtin", "harness", "*"})


class ManifestIntegrityError(Exception):
    """The manifest cannot be built honestly (missing evidence, version drift)."""


@dataclass(frozen=True)
class ManifestEntry:
    """One capability's evidence claim.

    ``evidence`` holds repo-relative pointers (test files, scripts, workflow
    files, docs). ``ci_jobs`` holds CI job ids that must exist in
    ``.github/workflows/*.yml``. Empty for ``manual``/``not_run`` entries.
    """

    capability: str
    provider: str  # gitlab | github | azure | "*"
    backend: str  # builtin | harness | "*"
    level: ManifestLevel
    evidence_class: EvidenceClass
    evidence: tuple[str, ...] = ()
    ci_jobs: tuple[str, ...] = ()
    gating: Gating = "pr_gate"
    note: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "capability": self.capability,
            "provider": self.provider,
            "backend": self.backend,
            "level": self.level,
            "evidence_class": self.evidence_class,
            "evidence": list(self.evidence),
            "ci_jobs": list(self.ci_jobs),
            "gating": self.gating,
            "note": self.note,
        }


@dataclass(frozen=True)
class FindingClosure:
    """A review finding mapped to its landed, grep-verifiable evidence.

    Only findings whose fix is VERIFIED in the current tree appear here. A
    closure's ``capability`` must match a :data:`ENTRIES` capability slug when
    one exists; the note carries any scope limitation (e.g. provider-lane-only
    fixes) so a partial fix is never presented as a full closure.
    """

    finding: str  # review item id, e.g. "R04" / "A02"
    capability: str  # ManifestEntry capability slug, or "*" when infra-wide
    level: ManifestLevel
    evidence: tuple[str, ...]
    note: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "finding": self.finding,
            "capability": self.capability,
            "level": self.level,
            "evidence": list(self.evidence),
            "note": self.note,
        }


# --------------------------------------------------------------------------
# The seeded registry — HONEST state of the tree at manifest inception.
# Levels are derived from the actual suite (grep which test files cover which
# capability), not from aspirational prose. tests/test_release_manifest.py
# holds this file to every pointer below.
# --------------------------------------------------------------------------

_CI_TEST: Final[tuple[str, ...]] = ("test",)
_NIGHTLY_OS: Final[tuple[str, ...]] = ("integration-os",)
_NIGHTLY_CANARY: Final[tuple[str, ...]] = ("release-canary",)

ENTRIES: Final[tuple[ManifestEntry, ...]] = (
    # ---- contract-tested capabilities (PR gate, fakes/stubs) ----
    ManifestEntry(
        capability="runs/spec-freeze",
        provider="*",
        backend="*",
        level="contract_tested",
        evidence_class="contract_suite",
        evidence=("tests/test_runs_spec.py",),
        ci_jobs=_CI_TEST,
        note="ExecutableRunSpec v3: digest re-verified on every read; legacy v2 refused "
        "(R04; A02 wired the same spec to GitHub/Azure builders).",
    ),
    ManifestEntry(
        capability="runs/verification-contract",
        provider="*",
        backend="*",
        level="contract_tested",
        evidence_class="contract_suite",
        evidence=("tests/test_runs_verification.py",),
        ci_jobs=_CI_TEST,
        note="Unified VerificationResult: verified means the required checks RAN and SUCCEEDED "
        "(R02); absence of CI is honest unverified.",
    ),
    ManifestEntry(
        capability="runs/verification-gate",
        provider="gitlab",
        backend="*",
        level="contract_tested",
        evidence_class="contract_suite",
        evidence=("tests/test_runs_service.py",),
        ci_jobs=_CI_TEST,
        note="/implement parks at waiting_ci on FakeGitLab; required-jobs profile per ADR-0008.",
    ),
    ManifestEntry(
        capability="runs/verification-gate",
        provider="github",
        backend="*",
        level="contract_tested",
        evidence_class="contract_suite",
        evidence=("tests/test_github_runs.py",),
        ci_jobs=_CI_TEST,
        note="waiting_ci + checks correlation on FakeGitHub; required-checks surface still has an "
        "un-enforced branch-protection path (github_service.py: 'no required checks are enforced "
        "yet') — review A01 not claimed.",
    ),
    ManifestEntry(
        capability="runs/verification-gate",
        provider="azure",
        backend="*",
        level="contract_tested",
        evidence_class="contract_suite",
        evidence=("tests/test_azure_runs.py",),
        ci_jobs=_CI_TEST,
        note="waiting_ci with Builds correlation by candidate sha (R02 parity).",
    ),
    ManifestEntry(
        capability="publication-boundary",
        provider="*",
        backend="*",
        level="contract_tested",
        evidence_class="contract_suite",
        evidence=("tests/test_conformance.py", "tests/test_publication_boundary.py"),
        ci_jobs=_CI_TEST,
        note="R01: one publication boundary; negative conformance kit attests each lane "
        "(attest_conformance) — deny scenarios publish with zero commit-API calls.",
    ),
    ManifestEntry(
        capability="publication-intents",
        provider="*",
        backend="*",
        level="contract_tested",
        evidence_class="contract_suite",
        evidence=("tests/test_publication_intents.py", "tests/test_runs_reconciler.py"),
        ci_jobs=_CI_TEST,
        note="R11: intent persisted before HTTP; probe-first reconciliation adopts lost pushes.",
    ),
    ManifestEntry(
        capability="execution-claims",
        provider="*",
        backend="*",
        level="contract_tested",
        evidence_class="contract_suite",
        evidence=("tests/test_claims.py",),
        ci_jobs=_CI_TEST,
        note="R10: ExecutionClaim guarded transitions; cancel fences via generation pin.",
    ),
    ManifestEntry(
        capability="numeric-budgets",
        provider="*",
        backend="*",
        level="contract_tested",
        evidence_class="contract_suite",
        evidence=("tests/test_budgets.py", "tests/test_budget_wiring.py"),
        ci_jobs=_CI_TEST,
        note="R13: budget profiles frozen into the spec and opened before planning; harness lanes "
        "record honest partial enforcement.",
    ),
    ManifestEntry(
        capability="blob-reads",
        provider="*",
        backend="*",
        level="contract_tested",
        evidence_class="contract_suite",
        evidence=("tests/test_blob_reads.py",),
        ci_jobs=_CI_TEST,
        note="R14: only a provider-confirmed 404 proves absence.",
    ),
    ManifestEntry(
        capability="bounded-steps",
        provider="*",
        backend="*",
        level="contract_tested",
        evidence_class="contract_suite",
        evidence=("tests/test_bounded_steps.py", "tests/test_step_runtime.py"),
        ci_jobs=_CI_TEST,
        note="R07: checkpoint/replay — a crash never re-calls the model or re-derives published work.",
    ),
    ManifestEntry(
        capability="patch-engine",
        provider="*",
        backend="*",
        level="contract_tested",
        evidence_class="contract_suite",
        evidence=("tests/test_patch_differential.py", "tests/test_repository_changeset.py"),
        ci_jobs=_CI_TEST,
        note="R08/R09: discriminated representations with digest verification; differential suite "
        "against git apply as the oracle.",
    ),
    ManifestEntry(
        capability="durable-liveness",
        provider="*",
        backend="*",
        level="contract_tested",
        evidence_class="contract_suite",
        evidence=("tests/test_liveness.py",),
        ci_jobs=_CI_TEST,
        note="R17: deadline/cancel evaluated before provider I/O; late callbacks are superseded "
        "evidence, never READY.",
    ),
    ManifestEntry(
        capability="provider-namespaces",
        provider="*",
        backend="*",
        level="contract_tested",
        evidence_class="contract_suite",
        evidence=("tests/test_provider_namespaces.py",),
        ci_jobs=_CI_TEST,
        note="R03: (provider, project_id, issue_iid) identity — numeric ids across providers "
        "coexist.",
    ),
    ManifestEntry(
        capability="mcp-authorization",
        provider="*",
        backend="*",
        level="contract_tested",
        evidence_class="contract_suite",
        evidence=("tests/test_mcp_server.py", "tests/test_mcp_runs_tools.py"),
        ci_jobs=_CI_TEST,
        note="R19: default-deny tool wrapper, repo-target allowlist, denial auditing.",
    ),
    ManifestEntry(
        capability="harness-selection",
        provider="*",
        backend="*",
        level="contract_tested",
        evidence_class="contract_suite",
        evidence=("tests/test_harness_selection.py",),
        ci_jobs=_CI_TEST,
        note="R31: capability-aware harness selection — manifest + policy-bound planner proposal.",
    ),
    ManifestEntry(
        capability="harness-artifact",
        provider="*",
        backend="harness",
        level="contract_tested",
        evidence_class="contract_suite",
        evidence=("tests/test_harness_entry.py", "tests/test_runs_harness_service.py"),
        ci_jobs=_CI_TEST,
        note="R16: candidate artifact recipe (meta schema, ZIP caps) and the waiting_harness leg.",
    ),
    ManifestEntry(
        capability="project-config-scope",
        provider="*",
        backend="*",
        level="contract_tested",
        evidence_class="contract_suite",
        evidence=("tests/test_config_scope.py",),
        ci_jobs=_CI_TEST,
        note="A13: config reads are typed (confirmed_absent/valid/unreadable/invalid); only "
        "confirmed_absent earns the default profile — scope never widens on a read failure.",
    ),
    ManifestEntry(
        capability="operator-commands",
        provider="*",
        backend="*",
        level="contract_tested",
        evidence_class="contract_suite",
        evidence=("tests/test_runs_revival.py", "tests/test_slash_routing.py"),
        ci_jobs=_CI_TEST,
        note="R29: /retry + bounded auto-revive backoff; operator command routing.",
    ),
    ManifestEntry(
        capability="delivery-metrics",
        provider="*",
        backend="*",
        level="contract_tested",
        evidence_class="contract_suite",
        evidence=("tests/test_mcp_runs_tools.py",),
        ci_jobs=_CI_TEST,
        note="R24: honest delivery metrics via the forge_delivery_ladder MCP tool.",
    ),
    # ---- real-runtime evidence classes (kept SEPARATE from the contract suite) ----
    ManifestEntry(
        capability="durable-failure-injection",
        provider="*",
        backend="*",
        level="contract_tested",
        evidence_class="coroutine_fi",
        evidence=("tests/test_failure_injection.py",),
        ci_jobs=("integration",),
        gating="pr_gate",
        note="ADR-0017 exit bar: in-process workers killed at each checkpoint against REAL "
        "Postgres. Self-skips without FORGE_PG_TEST_URL — only the CI / integration job "
        "enforces it, not the plain test job.",
    ),
    ManifestEntry(
        capability="os-process-failure-injection",
        provider="*",
        backend="*",
        level="contract_tested",
        evidence_class="subprocess_fi",
        evidence=("tests/test_failure_injection_os.py",),
        ci_jobs=_NIGHTLY_OS,
        gating="nightly",
        note="Real ``python -m forge.worker`` subprocesses SIGKILLed at each checkpoint against "
        "the real Postgres/Redis lab. NIGHTLY ONLY — not on the PR gate (R20).",
    ),
    ManifestEntry(
        capability="release-artifact-canary",
        provider="*",
        backend="*",
        level="live_canary_tested",
        evidence_class="boot_canary",
        evidence=("scripts/canary_smoke.py", ".github/workflows/release.yml"),
        ci_jobs=_NIGHTLY_CANARY + ("publish-ghcr",),
        gating="nightly",
        note="R30: the IMAGE is smoke-tested — real alembic chain, boot gate at head, /health "
        "version, MCP mount fail-closed, doctor; also the tag-gate before publish "
        "(Release / publish-ghcr). R32-19: the migrate stage seeds real-shaped rows and "
        "asserts the upgrade preserves them; stage outcomes are capability-tagged "
        "machine records the promotion gate consumes. Covers boot/migrate ONLY — see "
        "the not_run entries below.",
    ),
    ManifestEntry(
        capability="release-promotion-gate",
        provider="*",
        backend="*",
        level="contract_tested",
        evidence_class="contract_suite",
        evidence=(
            "src/forge/release_promotion.py",
            "tests/test_release_promotion.py",
            ".github/workflows/release.yml",
        ),
        ci_jobs=("test", "promotion-gate"),
        gating="release_tag",
        note="R32-19: mutable tags attach ONLY when the promotion gate qualifies the digest "
        "— explicit provenance-bound required CI checks on the tagged sha plus the "
        "capability-tagged canary outcomes; a failed (or never-executed) check blocks "
        "fail-closed even when the canary passed, and a failed-then-passed retry stays a "
        "conditional pass on record. Per-release promotion evidence is archived COMMITTED "
        "under docs/releases/evidence/v<version>/ and renders the README pins.",
    ),
    ManifestEntry(
        capability="real-provider-e2e",
        provider="github",
        backend="*",
        level="live_canary_tested",
        evidence_class="real_provider_e2e",
        evidence=(
            "docs/reference/dogfooding.md",
            ".github/workflows/forge-harness.yml",
            "scripts/gh_live_smoke.py",
        ),
        gating="manual",
        note="The dogfood loop on THIS repo: /implement → plan → /go → Actions agent → Draft PR. "
        "Event-driven and maintainer-run — no CI job replays it.",
    ),
    ManifestEntry(
        capability="real-provider-e2e",
        provider="gitlab",
        backend="*",
        level="not_run",
        evidence_class="real_provider_e2e",
        gating="none",
        note="Live exercises against the maintainer's real GitLab CE are maintainer-run outside "
        "this tree (the README prose claims them); NO CI-reproducible or in-tree evidence "
        "exists, so the manifest records not_run — never converted to pass.",
    ),
    ManifestEntry(
        capability="real-provider-e2e",
        provider="azure",
        backend="*",
        level="not_run",
        evidence_class="real_provider_e2e",
        gating="none",
        note="Live exercises against a real Azure DevOps org are maintainer-run outside this "
        "tree (the README prose claims them); NO CI-reproducible or in-tree evidence exists, "
        "so the manifest records not_run — never converted to pass.",
    ),
    # ---- explicit not_run gaps (unknown stays explicit) ----
    ManifestEntry(
        capability="release-canary/target-harness-upload",
        provider="*",
        backend="harness",
        level="not_run",
        evidence_class="none",
        gating="none",
        note="The release canary boots and migrates the artifact but NEVER exercises a "
        "target-harness upload (review A14 observation) — no CI job covers it. Flip this "
        "entry only when a canary stage drives a real harness upload.",
    ),
    ManifestEntry(
        capability="release-canary/previous-release-upgrade",
        provider="*",
        backend="*",
        level="not_run",
        evidence_class="boot_canary",
        gating="none",
        note="The canary's migrate stage (prev → this schema) is CONDITIONAL: it self-skips "
        "when the previous image is not pullable (first release, private registry). Recorded "
        "as not_run so a skipped stage is never counted as a passed upgrade.",
    ),
    ManifestEntry(
        capability="cohort-economics",
        provider="*",
        backend="*",
        level="contract_tested",
        evidence_class="contract_suite",
        gating="pr_gate",
        ci_jobs=("test",),
        evidence=("tests/test_cohort_runner.py",),
        note=(
            "B09: unknown stays unknown in the cohort economics (all-unknown "
            "durations/repairs are None; a receipt-less attempt makes exact cost "
            "unknown with a priced lower bound; rates only over matched "
            "populations). B10: the pass-1 ledger/report carry the backfilled "
            "profile with honest unknown spend."
        ),
    ),
)

#: Findings with VERIFIED landed evidence in the current tree. A01, A04-A12
#: (d16f523 review) are deliberately absent: their fixes are not fully
#: verifiable in-tree, and absence here means "not claimed".
FINDING_CLOSURES: Final[tuple[FindingClosure, ...]] = (
    FindingClosure(
        finding="OPS-08",
        capability="release-artifact-canary",
        level="contract_tested",
        evidence=("docs/operations/adaptive-runbook.md",),
    ),
    FindingClosure(
        finding="OPS-06",
        capability="release-artifact-canary",
        level="contract_tested",
        evidence=("tests/test_adaptive_executed_evidence.py",),
    ),
    FindingClosure(
        finding="OPS-07",
        capability="real-provider-e2e",
        level="live_canary_tested",
        evidence=("docs/evaluation/2026-09-21-adaptive-pilot/pilot-v2-result.json",),
    ),
    FindingClosure(
        finding="WIRING",
        capability="runs/spec-freeze",
        level="contract_tested",
        evidence=("tests/test_adaptive_wiring.py",),
    ),
    # 05868e9 backlog (the full eight-epic substrate, v0.17.0)
    FindingClosure(
        finding="FND-03",
        capability="blob-reads",
        level="contract_tested",
        evidence=("tests/test_adaptive_read_guards.py",),
    ),
    FindingClosure(
        finding="FND-04",
        capability="harness-selection",
        level="contract_tested",
        evidence=("tests/test_adaptive_capability_profiles.py",),
    ),
    FindingClosure(
        finding="FND-05",
        capability="harness-artifact",
        level="contract_tested",
        evidence=("tests/test_adaptive_artifact_store.py",),
    ),
    FindingClosure(
        finding="FND-06",
        capability="durable-failure-injection",
        level="contract_tested",
        evidence=("tests/test_adaptive_dedup.py",),
    ),
    FindingClosure(
        finding="FND-07",
        capability="runs/spec-freeze",
        level="contract_tested",
        evidence=("tests/test_adaptive_compat.py",),
    ),
    FindingClosure(
        finding="FND-08",
        capability="durable-failure-injection",
        level="contract_tested",
        evidence=("tests/test_adaptive_compat.py",),
    ),
    FindingClosure(
        finding="DSC-EPIC",
        capability="runs/spec-freeze",
        level="contract_tested",
        evidence=(
            "tests/test_adaptive_snapshots.py",
            "tests/test_adaptive_discovery_tools.py",
            "tests/test_adaptive_project_map.py",
            "tests/test_adaptive_discovery.py",
            "tests/test_adaptive_system_manifest.py",
            "tests/test_adaptive_impact.py",
        ),
    ),
    FindingClosure(
        finding="PLN-EPIC",
        capability="runs/spec-freeze",
        level="contract_tested",
        evidence=("tests/test_adaptive_revisions.py",),
    ),
    FindingClosure(
        finding="CTL-EPIC",
        capability="runs/spec-freeze",
        level="contract_tested",
        evidence=("tests/test_adaptive_control.py",),
    ),
    FindingClosure(
        finding="EXE-EPIC",
        capability="harness-selection",
        level="contract_tested",
        evidence=(
            "tests/test_adaptive_runtime.py",
            "tests/test_adaptive_adapters.py",
        ),
    ),
    FindingClosure(
        finding="MRP-EPIC",
        capability="publication-boundary",
        level="contract_tested",
        evidence=("tests/test_adaptive_workpackage.py",),
    ),
    FindingClosure(
        finding="VER-EPIC",
        capability="runs/verification-contract",
        level="contract_tested",
        evidence=(
            "tests/test_adaptive_verification_sets.py",
            "tests/test_adaptive_benchmark.py",
        ),
    ),
    FindingClosure(
        finding="OPS-EPIC",
        capability="delivery-metrics",
        level="contract_tested",
        evidence=("tests/test_adaptive_ops.py",),
    ),
    # 05868e9 review (first slice — FND-01/FND-02 + the contracts
    # substrate; the 64-story roadmap continues in the milestone)
    FindingClosure(
        finding="FND-01",
        capability="project-config-scope",
        level="contract_tested",
        evidence=("tests/test_project_config.py", "src/forge/repository/identity.py"),
    ),
    FindingClosure(
        finding="FND-02",
        capability="publication-boundary",
        level="contract_tested",
        evidence=("tests/test_github_runs.py",),
    ),
    FindingClosure(
        finding="ADAPTIVE-CONTRACTS",
        capability="runs/spec-freeze",
        level="contract_tested",
        evidence=("tests/test_adaptive_contracts.py",),
    ),
    # 44cdae review (D01-D12, closed 2026-09-21 — the authority boundary
    # campaign)
    FindingClosure(
        finding="D01",
        capability="project-config-scope",
        level="contract_tested",
        evidence=("tests/test_project_config.py",),
    ),
    FindingClosure(
        finding="D02",
        capability="project-config-scope",
        level="contract_tested",
        evidence=("tests/test_project_config.py",),
    ),
    FindingClosure(
        finding="D03",
        capability="publication-boundary",
        level="contract_tested",
        evidence=("tests/test_github_runs.py",),
    ),
    FindingClosure(
        finding="D04",
        capability="publication-boundary",
        level="contract_tested",
        evidence=("tests/test_github_runs.py",),
    ),
    FindingClosure(
        finding="D05",
        capability="runs/spec-freeze",
        level="contract_tested",
        evidence=("tests/test_runs_backends.py",),
    ),
    FindingClosure(
        finding="D06",
        capability="harness-selection",
        level="contract_tested",
        evidence=("tests/test_harness_selection.py",),
    ),
    FindingClosure(
        finding="D07",
        capability="cohort-economics",
        level="contract_tested",
        evidence=("tests/test_cohort_runner.py",),
    ),
    FindingClosure(
        finding="D08",
        capability="runs/spec-freeze",
        level="contract_tested",
        evidence=("src/forge/harness_entry.py", "tests/test_execution_profile.py"),
    ),
    FindingClosure(
        finding="D09",
        capability="runs/verification-contract",
        level="contract_tested",
        evidence=("tests/test_github_runs.py",),
    ),
    FindingClosure(
        finding="D10",
        capability="durable-failure-injection",
        level="contract_tested",
        evidence=("tests/test_github_runs.py",),
    ),
    FindingClosure(
        finding="D11",
        capability="publication-boundary",
        level="contract_tested",
        evidence=("src/forge/runs/github_service.py", "tests/test_github_runs.py"),
    ),
    FindingClosure(
        finding="D12",
        capability="release-artifact-canary",
        level="contract_tested",
        evidence=("tests/test_release_manifest.py",),
    ),
    # 7f0139e review (C01-C12, closed 2026-09-21 — the contract hand-off
    # campaign; each closure carries its composed regression)
    FindingClosure(
        finding="C01",
        capability="runs/verification-contract",
        level="contract_tested",
        evidence=("tests/test_github_runs.py",),
    ),
    FindingClosure(
        finding="C02",
        capability="numeric-budgets",
        level="contract_tested",
        evidence=("tests/test_github_runs.py",),
    ),
    FindingClosure(
        finding="C03",
        capability="harness-selection",
        level="contract_tested",
        evidence=("tests/test_azure_integration_joins.py", "tests/test_harness_selection.py"),
    ),
    FindingClosure(
        finding="C04",
        capability="runs/verification-contract",
        level="contract_tested",
        evidence=("tests/test_azure_runs.py",),
    ),
    FindingClosure(
        finding="C05",
        capability="runs/spec-freeze",
        level="contract_tested",
        evidence=("tests/test_runs_backends.py", "tests/test_runs_service.py"),
    ),
    FindingClosure(
        finding="C06",
        capability="harness-selection",
        level="contract_tested",
        evidence=("tests/test_harness_selection.py",),
    ),
    FindingClosure(
        finding="C07",
        capability="cohort-economics",
        level="contract_tested",
        evidence=("tests/test_cohort_runner.py",),
    ),
    FindingClosure(
        finding="C08",
        capability="publication-intents",
        level="contract_tested",
        evidence=("tests/test_runs_service.py",),
    ),
    FindingClosure(
        finding="C09",
        capability="durable-failure-injection",
        level="contract_tested",
        evidence=("alembic/versions/019_mr_reservations.py",),
    ),
    FindingClosure(
        finding="C10",
        capability="runs/spec-freeze",
        level="contract_tested",
        evidence=("tests/test_harness_entry.py", "tests/test_execution_profile.py"),
    ),
    FindingClosure(
        finding="C11",
        capability="runs/verification-contract",
        level="contract_tested",
        evidence=("tests/test_github_runs.py",),
    ),
    FindingClosure(
        finding="C12",
        capability="release-artifact-canary",
        level="contract_tested",
        evidence=("tests/test_release_manifest.py",),
    ),
    # e53ffd2 review (B01-B15, closed 2026-09-21 — every finding carries its
    # composed regression on a production service path; B14's own coverage
    # IS this block, machine-validated like the R-series)
    FindingClosure(
        finding="B01",
        capability="runs/verification-contract",
        level="contract_tested",
        evidence=("tests/test_github_runs.py", "tests/test_azure_runs.py"),
    ),
    FindingClosure(
        finding="B02",
        capability="runs/verification-contract",
        level="contract_tested",
        evidence=("tests/test_github_runs.py",),
    ),
    FindingClosure(
        finding="B03",
        capability="publication-intents",
        level="contract_tested",
        evidence=("tests/test_runs_service.py", "tests/test_failure_injection.py"),
    ),
    FindingClosure(
        finding="B04",
        capability="runs/spec-freeze",
        level="contract_tested",
        evidence=("tests/test_harness_entry.py", "tests/test_azure_integration_joins.py"),
    ),
    FindingClosure(
        finding="B05",
        capability="runs/spec-freeze",
        level="contract_tested",
        evidence=(
            "tests/test_azure_integration_joins.py",
            "tests/test_azure_pipelines_executor.py",
        ),
    ),
    FindingClosure(
        finding="B06",
        capability="runs/verification-contract",
        level="contract_tested",
        evidence=("tests/test_github_runs.py", "tests/test_runs_spec.py"),
    ),
    FindingClosure(
        finding="B07",
        capability="runs/verification-contract",
        level="contract_tested",
        evidence=("tests/test_usecases.py", "tests/test_github_runs.py"),
    ),
    FindingClosure(
        finding="B08",
        capability="runs/verification-contract",
        level="contract_tested",
        evidence=("tests/test_github_runs.py",),
    ),
    FindingClosure(
        finding="B09",
        capability="cohort-economics",
        level="contract_tested",
        evidence=("tests/test_cohort_runner.py",),
    ),
    FindingClosure(
        finding="B10",
        capability="cohort-economics",
        level="contract_tested",
        evidence=("docs/evaluation/2026-09-20-pass1/report.json",),
    ),
    FindingClosure(
        finding="B11",
        capability="runs/spec-freeze",
        level="contract_tested",
        evidence=("tests/test_github_runs.py", "tests/test_runs_service.py"),
    ),
    FindingClosure(
        finding="B12",
        capability="runs/spec-freeze",
        level="contract_tested",
        evidence=("tests/test_execution_profile.py", "tests/test_harness_entry.py"),
    ),
    FindingClosure(
        finding="B13",
        capability="runs/verification-contract",
        level="contract_tested",
        evidence=("tests/test_github_harness.py", "tests/test_azure_runs.py"),
    ),
    FindingClosure(
        finding="B14",
        capability="durable-failure-injection",
        level="contract_tested",
        evidence=("tests/test_conformance.py",),
    ),
    FindingClosure(
        finding="B15",
        capability="runs/verification-contract",
        level="contract_tested",
        evidence=("tests/test_runs_revival.py", "tests/test_usecases.py"),
    ),
    # e8bf381 review (the R-series the CHANGELOG claims closed)
    FindingClosure(
        finding="R01",
        capability="publication-boundary",
        level="contract_tested",
        evidence=("tests/test_conformance.py", "tests/test_publication_boundary.py"),
    ),
    FindingClosure(
        finding="R02",
        capability="runs/verification-contract",
        level="contract_tested",
        evidence=("tests/test_runs_verification.py", "tests/test_azure_runs.py"),
    ),
    FindingClosure(
        finding="R03",
        capability="provider-namespaces",
        level="contract_tested",
        evidence=("tests/test_provider_namespaces.py",),
    ),
    FindingClosure(
        finding="R04",
        capability="runs/spec-freeze",
        level="contract_tested",
        evidence=("tests/test_runs_spec.py",),
    ),
    FindingClosure(
        finding="R05",
        capability="runs/spec-freeze",
        level="contract_tested",
        evidence=(
            "tests/test_runs_service.py",
            "tests/test_github_runs.py",
            "tests/test_azure_runs.py",
        ),
        note="Brief binding: the plan comment carries the frozen implementation block on all "
        "three providers.",
    ),
    FindingClosure(
        finding="R07",
        capability="bounded-steps",
        level="contract_tested",
        evidence=("tests/test_bounded_steps.py", "tests/test_step_runtime.py"),
    ),
    FindingClosure(
        finding="R08",
        capability="patch-engine",
        level="contract_tested",
        evidence=("tests/test_patch_differential.py",),
    ),
    FindingClosure(
        finding="R09",
        capability="patch-engine",
        level="contract_tested",
        evidence=("tests/test_patch_differential.py", "tests/test_repository_changeset.py"),
    ),
    FindingClosure(
        finding="R10",
        capability="execution-claims",
        level="contract_tested",
        evidence=("tests/test_claims.py",),
    ),
    FindingClosure(
        finding="R11",
        capability="publication-intents",
        level="contract_tested",
        evidence=("tests/test_publication_intents.py", "tests/test_runs_reconciler.py"),
    ),
    FindingClosure(
        finding="R13",
        capability="numeric-budgets",
        level="contract_tested",
        evidence=("tests/test_budgets.py", "tests/test_budget_wiring.py"),
    ),
    FindingClosure(
        finding="R14",
        capability="blob-reads",
        level="contract_tested",
        evidence=("tests/test_blob_reads.py",),
    ),
    FindingClosure(
        finding="R16",
        capability="harness-artifact",
        level="contract_tested",
        evidence=("tests/test_harness_entry.py", "tests/test_runs_harness_service.py"),
    ),
    FindingClosure(
        finding="R17",
        capability="durable-liveness",
        level="contract_tested",
        evidence=("tests/test_liveness.py",),
    ),
    FindingClosure(
        finding="R19",
        capability="mcp-authorization",
        level="contract_tested",
        evidence=("tests/test_mcp_server.py", "tests/test_mcp_runs_tools.py"),
    ),
    FindingClosure(
        finding="R24",
        capability="delivery-metrics",
        level="contract_tested",
        evidence=("tests/test_mcp_runs_tools.py",),
    ),
    FindingClosure(
        finding="R29",
        capability="operator-commands",
        level="contract_tested",
        evidence=("tests/test_runs_revival.py", "tests/test_slash_routing.py"),
    ),
    FindingClosure(
        finding="R31",
        capability="harness-selection",
        level="contract_tested",
        evidence=("tests/test_harness_selection.py", "tests/test_budget_wiring.py"),
    ),
    # d16f523 review — only findings verified landed in the current tree
    FindingClosure(
        finding="A02",
        capability="runs/spec-freeze",
        level="contract_tested",
        evidence=(
            "tests/test_github_runs.py",
            "tests/test_azure_runs.py",
            "tests/test_runs_spec.py",
        ),
        note="GitHub and Azure builders freeze/consume the SAME executable spec v3 "
        "(EXECUTABLE_SPEC_SCHEMA_VERSION) — verified in tree.",
    ),
    FindingClosure(
        finding="A03",
        capability="harness-artifact",
        level="contract_tested",
        evidence=("tests/test_github_harness.py", "tests/test_harness_entry.py"),
        note="BriefEnvelope landed on the GITHUB lane only — GitLab/Azure legs not verified in "
        "tree; a scope-limited fix, NOT a full closure.",
    ),
    FindingClosure(
        finding="A13",
        capability="project-config-scope",
        level="contract_tested",
        evidence=(
            "tests/test_config_scope.py",
            "tests/test_github_runs.py",
            "tests/test_azure_runs.py",
        ),
    ),
)


def entries() -> tuple[ManifestEntry, ...]:
    """The seeded capability registry."""
    return ENTRIES


def finding_closures() -> tuple[FindingClosure, ...]:
    """Findings with verified in-tree evidence (absence means not claimed)."""
    return FINDING_CLOSURES


def resolve_head_sha(root: Path) -> str:
    """HEAD sha of the repo at *root*, or ``"unknown"`` outside a git repo."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ("git", "rev-parse", "HEAD"),
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if proc.returncode != 0:
        return "unknown"
    sha = proc.stdout.strip()
    return sha if sha else "unknown"


def _read_pyproject_version(root: Path) -> str:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        raise ManifestIntegrityError(f"pyproject.toml not found under {root}")
    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)
    try:
        version = data["project"]["version"]
    except KeyError as exc:  # pragma: no cover - project always declares it
        raise ManifestIntegrityError("pyproject.toml has no project.version") from exc
    if not isinstance(version, str) or not version:
        raise ManifestIntegrityError("pyproject.toml project.version is not a non-empty string")
    return version


def _validate_entry(entry: ManifestEntry, index: int) -> None:
    where = f"entry[{index}] ({entry.capability or '<empty>'})"
    if not entry.capability:
        raise ManifestIntegrityError(f"{where}: empty capability")
    if entry.provider not in _PROVIDERS:
        raise ManifestIntegrityError(f"{where}: bad provider {entry.provider!r}")
    if entry.backend not in _BACKENDS:
        raise ManifestIntegrityError(f"{where}: bad backend {entry.backend!r}")

    if entry.level == "not_run":
        if entry.ci_jobs:
            raise ManifestIntegrityError(f"{where}: a not_run entry cannot claim CI jobs")
        if entry.gating != "none":
            raise ManifestIntegrityError(f"{where}: a not_run entry must gate as 'none'")
        if not entry.note:
            raise ManifestIntegrityError(f"{where}: a not_run entry must say WHY it is not run")
    else:
        if not entry.evidence:
            raise ManifestIntegrityError(f"{where}: level {entry.level} requires evidence pointers")
        if entry.gating == "manual" and entry.ci_jobs:
            raise ManifestIntegrityError(f"{where}: a manual entry cannot claim CI jobs")
        if entry.gating in {"pr_gate", "nightly", "release_tag"} and not entry.ci_jobs:
            raise ManifestIntegrityError(f"{where}: CI-gated entries must name their CI jobs")

    # Evidence-class / level cross-rules: a class never impersonates a stronger level.
    if entry.evidence_class == "none" and entry.level != "not_run":
        raise ManifestIntegrityError(f"{where}: evidence_class 'none' is only for not_run")
    if entry.evidence_class == "real_provider_e2e" and entry.level == "implemented":
        raise ManifestIntegrityError(f"{where}: real_provider_e2e cannot back 'implemented'")
    if entry.evidence_class in {"boot_canary", "real_provider_e2e"} and entry.level in {
        "implemented",
        "contract_tested",
    }:
        raise ManifestIntegrityError(
            f"{where}: {entry.evidence_class} evidence cannot back level {entry.level!r} — "
            "a canary/live class never impersonates a contract claim"
        )


def _validate_findings(closures: tuple[FindingClosure, ...], capabilities: frozenset[str]) -> None:
    seen: set[str] = set()
    for closure in closures:
        if closure.finding in seen:
            raise ManifestIntegrityError(f"duplicate finding closure {closure.finding}")
        seen.add(closure.finding)
        if not closure.evidence:
            raise ManifestIntegrityError(f"finding {closure.finding}: closure without evidence")
        if closure.capability != "*" and closure.capability not in capabilities:
            raise ManifestIntegrityError(
                f"finding {closure.finding}: capability {closure.capability!r} is not in ENTRIES"
            )


def _validate_evidence_files(manifest_entries: tuple[ManifestEntry, ...], root: Path) -> None:
    for entry in manifest_entries:
        for pointer in entry.evidence:
            if not (root / pointer).is_file():
                raise ManifestIntegrityError(
                    f"evidence pointer {pointer!r} of {entry.capability!r} does not exist in the tree"
                )
    for closure in FINDING_CLOSURES:
        for pointer in closure.evidence:
            if not (root / pointer).is_file():
                raise ManifestIntegrityError(
                    f"evidence pointer {pointer!r} of finding {closure.finding} does not exist"
                )


def _validate_ci_jobs(manifest_entries: tuple[ManifestEntry, ...], root: Path) -> None:
    workflows_dir = root / ".github" / "workflows"
    if not workflows_dir.is_dir():
        raise ManifestIntegrityError(f"no workflows directory under {root}")
    declared: set[str] = set()
    for workflow in workflows_dir.glob("*.yml"):
        for line in workflow.read_text(encoding="utf-8").splitlines():
            # top-level job ids are two-space indented keys under `jobs:`
            if line.startswith("  ") and line.endswith(":") and not line.startswith("    "):
                declared.add(line.strip().rstrip(":"))
    for entry in manifest_entries:
        for job in entry.ci_jobs:
            if job not in declared:
                raise ManifestIntegrityError(
                    f"CI job {job!r} of {entry.capability!r} is not declared in any workflow"
                )


def build_manifest(root: Path | None = None) -> dict[str, object]:
    """Build the manifest dict, refusing anything that cannot be verified.

    Fails closed (raises :class:`ManifestIntegrityError`) on: pyproject /
    ``forge.__version__`` drift, evidence pointers that do not exist, CI job
    names not declared in the workflows, evidence-class/level impersonation,
    or a not_run entry trying to claim a run.
    """
    base = root if root is not None else Path.cwd()
    pyproject_version = _read_pyproject_version(base)
    if pyproject_version != __version__:
        raise ManifestIntegrityError(
            f"version drift: pyproject {pyproject_version!r} != forge.__version__ {__version__!r} "
            "(R30 discipline, asserted at manifest time — not only in the release workflow)"
        )

    for index, entry in enumerate(ENTRIES):
        _validate_entry(entry, index)
    capabilities = frozenset(entry.capability for entry in ENTRIES)
    _validate_findings(FINDING_CLOSURES, capabilities)
    _validate_evidence_files(ENTRIES, base)
    _validate_ci_jobs(ENTRIES, base)

    return {
        "schema_version": SCHEMA_VERSION,
        "version": __version__,
        "sha": resolve_head_sha(base),
        "legend": {
            "levels": LEVEL_LEGEND,
            "evidence_classes": EVIDENCE_CLASS_LEGEND,
            "gating": GATING_LEGEND,
        },
        "entries": [entry.to_json() for entry in ENTRIES],
        "finding_evidence": [closure.to_json() for closure in FINDING_CLOSURES],
    }


def render_manifest(manifest: dict[str, object]) -> str:
    """Render the manifest as deterministic JSON (sorted keys, 2-space indent)."""
    return json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI: emit the manifest JSON to stdout or ``--out PATH``."""
    parser = argparse.ArgumentParser(
        prog="python -m forge.release_manifest",
        description="Generate the release-evidence manifest (review A16).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository root (default: cwd)",
    )
    parser.add_argument("--out", type=Path, default=None, help="write JSON here (default: stdout)")
    args = parser.parse_args(argv)
    try:
        manifest = build_manifest(args.root)
    except ManifestIntegrityError as exc:
        print(f"release-manifest: REFUSED: {exc}", file=sys.stderr)
        return 1
    rendered = render_manifest(manifest)
    if args.out is None:
        sys.stdout.write(rendered)
    else:
        args.out.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
