"""Honesty guards for the release-evidence manifest (review A16 / R28).

The manifest (``forge.release_manifest``) is the machine-readable backing for
every capability/status claim README and CHANGELOG make. These tests keep it
HONEST — the manifest may only claim what the tree proves:

- version consistency (pyproject == forge.__version__) is asserted HERE, at
  test time, so drift fails before the release workflow's R30 guard does;
- every evidence pointer exists and pytest evidence files really contain
  tests; every CI job id is declared in a workflow;
- boot canary / subprocess FI / coroutine FI / real-provider e2e stay
  SEPARATE evidence classes — no class impersonates a stronger level;
- every finding mapped to a closure has landed, grep-verifiable test
  evidence (and the A02 closure is re-proven against the src builders);
- findings whose fixes are NOT verified in-tree (A01, A04-A12) stay OUT of
  the closure table — absence means "not claimed";
- not_run entries keep their reason and never claim a CI job.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from forge import release_manifest
from forge.release_manifest import (
    ENTRIES,
    FINDING_CLOSURES,
    ManifestEntry,
    build_manifest,
    render_manifest,
)

ROOT = Path(__file__).resolve().parents[1]

#: Review findings whose fixes are NOT fully verifiable in the current tree.
#: The manifest must NOT list them as closures — absence means "not claimed".
UNCLAIMED_FINDINGS: frozenset[str] = frozenset(
    {"A01", "A04", "A05", "A06", "A07", "A08", "A09", "A10", "A11", "A12"}
)

#: Grep marker per claimed finding: each closure's evidence must contain it,
#: so a closure is only ever pointed at test files that actually test it.
FINDING_MARKERS: dict[str, str] = {
    "R01": "attest_conformance",
    "R02": "waiting_ci",
    "R03": "across_providers",
    "R04": "test_spec_frozen_at_plan_acceptance",
    "R05": "plan_comment_carries_the_implementation_block",
    "R07": "test_record_then_load_round_trips",
    "R08": "test_zero_context_insertion_after_line_3",
    "R09": "test_minimal_valid_changeset",
    "R10": "test_two_actors_same_expectation_exactly_one_applies",
    "R11": "test_exactly_one_marker_and_parent_match_adopts",
    "R13": "test_finite_profile_freezes_spec_and_opens_budget_before_planning",
    "R14": "test_found_computes_sha256_of_utf8_bytes",
    "R16": "test_script_follows_the_unattended_contract",
    "R17": "test_expired_deadline_blocks_without_any_provider_call",
    "R19": "test_unknown_token_is_401",
    "R24": "ladder",
    "R29": "test_backoff_ladder_is_bounded",
    "R31": "test_five_fixed_lines_with_model_and_chain",
    "A02": "executable spec",
    "A03": "BriefEnvelope",
    "A13": "test_confirmed_404_is_the_only_absence",
    "B01": "verification_epoch",
    "B02": "old_rerun_success_never_masks",
    "B03": "MRReservation",
    "B04": "TestRenderBriefAzureEnforcedB04",
    "B05": "trigger: none",
    "B06": "waived_conclusions",
    "B07": "missing MANDATORY GATE",
    "B08": "never_touches_another_repository",
    "B09": "cost_lower_bound_usd_mean",
    "B10": "backfilled_at",
    "B11": "TestCurrentPlanSelectionB11",
    "B12": "observed_execution",
    "B13": "verification pending",
    "B14": "TestRegistrationRule",
    "B15": "test_provider_services_never_reimplement_recovery_scans",
    "C01": "ambiguous_check_identity",
    "C02": "one_set_of_numbers",
    "C03": "TestCredentialContractC03",
    "C04": "never_touches_another_repository",
    "C05": "BackendStartSpec",
    "C06": "StrictManifestC06",
    "C07": "effective_output_rate",
    "C08": "TestBoundedMrIoC08",
    "C09": "downgrade 019->018 refused",
    "C10": "TestCommandReceiptsC10",
    "C11": "TestMutationGuardsC11",
    "C12": "v0.13.0",
}

#: The known unknowns: capabilities the manifest must keep as explicit
#: not_run entries until real evidence lands (flip them, with evidence, in a
#: deliberate change — this test will hold the line meanwhile).
EXPLICIT_NOT_RUN: frozenset[str] = frozenset(
    {
        "release-canary/target-harness-upload",
        "release-canary/previous-release-upgrade",
        "real-provider-e2e/gitlab",
        "real-provider-e2e/azure",
    }
)


def _entry_key(entry: ManifestEntry) -> str:
    if entry.provider == "*":
        return entry.capability
    return f"{entry.capability}/{entry.provider}"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Version consistency (R30 discipline at test time)
# ---------------------------------------------------------------------------


def test_pyproject_version_matches_dunder_version() -> None:
    pyproject = _read("pyproject.toml")
    assert f'version = "{release_manifest.__version__}"' in pyproject


def test_manifest_refuses_version_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release_manifest, "__version__", "9.9.9-drift")
    with pytest.raises(release_manifest.ManifestIntegrityError, match="version drift"):
        build_manifest(ROOT)


def test_manifest_carries_consistent_version_and_head_sha() -> None:
    manifest = build_manifest(ROOT)
    assert manifest["version"] == release_manifest.__version__
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert manifest["sha"] == head
    assert len(str(manifest["sha"])) == 40


# ---------------------------------------------------------------------------
# Manifest shape and rendering
# ---------------------------------------------------------------------------


def test_manifest_schema_shape() -> None:
    manifest = build_manifest(ROOT)
    assert manifest["schema_version"] == release_manifest.SCHEMA_VERSION
    legend = manifest["legend"]
    assert isinstance(legend, dict)
    assert set(legend) == {"levels", "evidence_classes", "gating"}
    entries = manifest["entries"]
    assert isinstance(entries, list) and entries
    required = {"capability", "provider", "backend", "level", "evidence_class", "gating"}
    for entry in entries:
        assert isinstance(entry, dict)
        assert required <= set(entry)
    assert manifest["finding_evidence"], "the closure table must not be empty"


def test_render_is_deterministic_json() -> None:
    first = render_manifest(build_manifest(ROOT))
    second = render_manifest(build_manifest(ROOT))
    assert first == second
    parsed = json.loads(first)
    assert parsed["schema_version"] == release_manifest.SCHEMA_VERSION


def test_build_refuses_a_tree_without_pyproject(tmp_path: Path) -> None:
    with pytest.raises(release_manifest.ManifestIntegrityError, match="pyproject"):
        build_manifest(tmp_path)


# ---------------------------------------------------------------------------
# Evidence pointers are real
# ---------------------------------------------------------------------------


def test_every_claimed_evidence_pointer_exists() -> None:
    for entry in ENTRIES:
        if entry.level == "not_run":
            continue
        assert entry.evidence, f"{_entry_key(entry)} claims {entry.level} with no evidence"
        for pointer in entry.evidence:
            assert (ROOT / pointer).is_file(), f"{_entry_key(entry)}: missing {pointer}"


def test_pytest_evidence_files_really_contain_tests() -> None:
    for entry in ENTRIES:
        for pointer in entry.evidence:
            if pointer.startswith("tests/") and pointer.endswith(".py"):
                assert "def test_" in _read(pointer), f"{pointer} has no tests"


def test_every_ci_job_is_declared_in_a_workflow() -> None:
    declared: set[str] = set()
    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        for line in workflow.read_text(encoding="utf-8").splitlines():
            if line.startswith("  ") and line.endswith(":") and not line.startswith("    "):
                declared.add(line.strip().rstrip(":"))
    for entry in ENTRIES:
        for job in entry.ci_jobs:
            assert job in declared, f"{_entry_key(entry)} claims undeclared CI job {job!r}"


def test_gating_matches_ci_jobs() -> None:
    for entry in ENTRIES:
        if entry.gating in {"pr_gate", "nightly", "release_tag"}:
            assert entry.ci_jobs, f"{_entry_key(entry)} is {entry.gating} with no CI job"
        if entry.gating == "manual":
            assert not entry.ci_jobs, f"{_entry_key(entry)} is manual but claims CI jobs"


# ---------------------------------------------------------------------------
# Evidence classes stay separate (A16: boot canary != subprocess FI != e2e)
# ---------------------------------------------------------------------------


def test_runtime_evidence_classes_are_all_present_and_distinct() -> None:
    classes = {entry.evidence_class for entry in ENTRIES}
    for runtime_class in ("boot_canary", "subprocess_fi", "coroutine_fi", "real_provider_e2e"):
        assert runtime_class in classes, f"evidence class {runtime_class} must be represented"


def test_a_live_class_never_backs_a_contract_or_unit_level() -> None:
    for entry in ENTRIES:
        if entry.evidence_class in {"boot_canary", "real_provider_e2e"}:
            assert entry.level in {"live_canary_tested", "not_run"}, (
                f"{_entry_key(entry)}: {entry.evidence_class} cannot back {entry.level}"
            )


def test_the_contract_suite_never_claims_live() -> None:
    for entry in ENTRIES:
        if entry.evidence_class == "contract_suite":
            assert entry.level in {"implemented", "contract_tested"}, (
                f"{_entry_key(entry)}: fake-platform evidence cannot claim {entry.level}"
            )


# ---------------------------------------------------------------------------
# not_run stays explicit — never converted to pass
# ---------------------------------------------------------------------------


def test_not_run_entries_keep_their_reason_and_claim_nothing() -> None:
    not_run = [entry for entry in ENTRIES if entry.level == "not_run"]
    assert not_run, "the manifest must carry explicit not_run entries"
    for entry in not_run:
        assert not entry.ci_jobs, f"{_entry_key(entry)} is not_run but claims CI jobs"
        assert entry.gating == "none"
        assert entry.note, f"{_entry_key(entry)} must say why it is not run"


def test_known_unknowns_are_still_recorded() -> None:
    recorded = {_entry_key(entry) for entry in ENTRIES if entry.level == "not_run"}
    missing = EXPLICIT_NOT_RUN - recorded
    assert not missing, (
        f"these not_run facts were removed from the manifest without evidence: {sorted(missing)}"
    )


# ---------------------------------------------------------------------------
# Finding closures are grep-verifiable; unverified findings stay unclaimed
# ---------------------------------------------------------------------------


def test_every_claimed_finding_has_grep_verifiable_evidence() -> None:
    assert set(FINDING_MARKERS) == {closure.finding for closure in FINDING_CLOSURES}, (
        "FINDING_MARKERS and FINDING_CLOSURES must cover the same findings"
    )
    for closure in FINDING_CLOSURES:
        marker = FINDING_MARKERS[closure.finding]
        assert any(marker in _read(path) for path in closure.evidence), (
            f"{closure.finding}: no evidence file contains {marker!r}"
        )


def test_a02_closure_is_proven_against_the_src_builders() -> None:
    # A16 acceptance: you cannot claim the executable spec on an adapter that
    # still freezes schema v2. Both builders must freeze the v3 constant.
    for builder in ("src/forge/runs/github_service.py", "src/forge/runs/azure_service.py"):
        assert "EXECUTABLE_SPEC_SCHEMA_VERSION" in _read(builder), (
            f"{builder} no longer freezes the executable spec — the A02 closure is stale"
        )


def test_unverified_findings_are_not_claimed() -> None:
    claimed = {closure.finding for closure in FINDING_CLOSURES}
    overclaimed = claimed & UNCLAIMED_FINDINGS
    assert not overclaimed, (
        f"findings {sorted(overclaimed)} are not fully verified in-tree — remove them from "
        "FINDING_CLOSURES or land the evidence first"
    )


def test_closure_capabilities_exist_in_the_registry() -> None:
    capabilities = {entry.capability for entry in ENTRIES}
    for closure in FINDING_CLOSURES:
        if closure.capability != "*":
            assert closure.capability in capabilities, (
                f"{closure.finding}: capability {closure.capability!r} is not in ENTRIES"
            )


# ---------------------------------------------------------------------------
# The docs scoping (CHANGELOG qualifier / README legend) cannot regress
# ---------------------------------------------------------------------------


def test_changelog_qualifies_the_every_finding_closed_claim() -> None:
    changelog = _read("CHANGELOG.md")
    for line in changelog.splitlines():
        if "every finding closed" in line:
            assert "manifest" in changelog, (
                "the 'every finding closed' claim must be scoped to the release manifest"
            )
            break
    else:
        pytest.fail("the CHANGELOG 'every finding closed' line disappeared — keep it scoped")


def test_readme_guarantee_levels_reference_the_manifest_levels() -> None:
    readme = _read("README.md")
    marker = readme.index("## Guarantee levels")
    section = readme[marker : readme.index("## ", marker + 1)]
    for level in ("implemented", "contract-tested", "live-canary-tested", "not-run"):
        assert level in section, f"the guarantee legend must state the level {level!r}"
    assert "forge.release_manifest" in section, (
        "the guarantee legend must point at the machine-readable manifest"
    )
