"""Tests for the reachability-based capability manifest (review NXT-02).

The honesty guard and its negatives: no row claims production wiring without
an entry point, the routed-command claims match the LIVE ingress sets, the
adaptive commands are recorded as NOT routed, and removing a binding in the
gateway breaks the claim instead of silently passing.
"""

from __future__ import annotations

import pytest

import forge.gateway.router as gateway_router
from forge.capability_manifest import (
    ADAPTIVE_OPERATOR_COMMANDS,
    CAPABILITIES,
    CLASSIC_OPERATOR_COMMANDS,
    TIER_LADDER,
    Capability,
    capabilities,
    format_matrix,
    ingress_routed_commands,
    manifest_problems,
    validate_manifest,
)
from forge.doctor import check_capabilities, format_report


def _by_name(name: str) -> Capability:
    matches = [row for row in CAPABILITIES if row.name == name]
    assert matches, f"capability {name!r} is missing from the manifest"
    return matches[0]


def _repo_root() -> "object":
    from pathlib import Path

    return Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# The honest registry itself
# ---------------------------------------------------------------------------


def test_manifest_validates_clean():
    """The seeded registry breaks none of its own rules (evidence included)."""
    validate_manifest(CAPABILITIES, root=_repo_root())  # raises nothing
    assert manifest_problems(CAPABILITIES) == []


def test_tier_ladder_is_the_reviews_four_degrees():
    assert TIER_LADDER == (
        "domain_contract",
        "production_wiring",
        "real_provider_scenario",
        "cross_process_recovery",
    )


def test_required_capabilities_are_covered():
    """The NXT-02 minimum set: 4 batch harnesses, 3 SDK lanes, the adaptive
    substrate surfaces, and both operator-command rows."""
    names = {row.name for row in capabilities()}
    required = {
        "harness/batch-claude-code",
        "harness/batch-grok-build",
        "harness/batch-opencode",
        "harness/batch-copilot",
        "sdk-lane/claude",
        "sdk-lane/codex",
        "sdk-lane/opencode",
        "adaptive/discovery-service",
        "adaptive/plan-revisions",
        "adaptive/durable-mailbox",
        "adaptive/steering-bridge",
        "adaptive/work-package-coordination",
        "adaptive/candidate-set-verification",
        "adaptive/executed-evidence-claims",
        "operator-commands/classic",
        "operator-commands/adaptive",
        "adaptive/pause-resume-checkpoint",
    }
    missing = required - names
    assert not missing, f"manifest lost required rows: {sorted(missing)}"


def test_no_row_claims_wiring_without_entry_point():
    """THE honesty guard: production_wiring+ requires a non-None entry point."""
    for row in capabilities():
        if row.tier in {"production_wiring", "real_provider_scenario", "cross_process_recovery"}:
            assert row.entry_point, f"{row.name}: tier {row.tier} without an entry point"
        else:
            assert row.tier == "domain_contract"
            assert row.entry_point is None, f"{row.name}: not-wired tier has an entry point"


def test_cross_process_recovery_is_claimed_nowhere():
    """No capability has proven cross-process recovery — the top tier stays
    empty rather than aspirational (a driver smoke must not mark
    /pause-to-checkpoint recovery as passed)."""
    assert [row.name for row in capabilities() if row.tier == "cross_process_recovery"] == []


# ---------------------------------------------------------------------------
# Operator commands: exactly the routed set, never more
# ---------------------------------------------------------------------------


def test_classic_commands_match_the_live_ingress_sets():
    routed = ingress_routed_commands()
    assert set(CLASSIC_OPERATOR_COMMANDS) == set(routed)
    classic = _by_name("operator-commands/classic")
    assert set(classic.commands) == set(routed)


def test_adaptive_commands_are_not_routed_anywhere():
    """The NXT-02 gap stated as data: /pause /steer /answer /resume exist in
    the control substrate but no checked ingress routes them."""
    routed = ingress_routed_commands()
    for command in ADAPTIVE_OPERATOR_COMMANDS:
        assert command not in routed
    adaptive = _by_name("operator-commands/adaptive")
    assert adaptive.entry_point is None
    assert adaptive.tier == "domain_contract"
    # The row lists the unavailable commands only in its note, never as
    # supported commands.
    assert adaptive.commands == ()


def test_helper_without_production_caller_is_library_level_only():
    """A capability with an entry-point-free helper (steering bridge, mailbox)
    is reported domain_contract — never dressed as wiring."""
    for name in ("adaptive/steering-bridge", "adaptive/durable-mailbox"):
        row = _by_name(name)
        assert row.tier == "domain_contract"
        assert row.entry_point is None
        assert row.note  # the WHY is mandatory


def test_removing_an_ingress_binding_breaks_the_claim(monkeypatch):
    """Negative/recovery: drop one command from the live ingress set and the
    manifest must flag the now-unsupported claim (no silent over-claim)."""
    shrunken = frozenset(set(gateway_router._RUN_COMMANDS) - {"/reconcile"})
    monkeypatch.setattr(gateway_router, "_RUN_COMMANDS", shrunken)
    problems = manifest_problems(CAPABILITIES)
    flagged = [p for p in problems if "/reconcile" in p and "does not route" in p]
    assert flagged, f"a removed binding must surface as a problem, got: {problems}"


def test_manifest_rejects_unrouted_command_claim():
    """A row claiming a command outside the routed set is invalid even when
    every other rule holds."""
    bad = Capability(
        name="operator-commands/phantom",
        tier="production_wiring",
        entry_point="nowhere.real",
        evidence=("tests/test_slash_routing.py",),
        commands=("/teleport",),
    )
    problems = manifest_problems([bad])
    assert any("/teleport" in p and "does not route" in p for p in problems)


# ---------------------------------------------------------------------------
# Structural guard negatives
# ---------------------------------------------------------------------------


def test_wired_tier_with_none_entry_point_is_rejected():
    bad = Capability(
        name="phantom/wired",
        tier="production_wiring",
        entry_point=None,
        evidence=("tests/test_slash_routing.py",),
    )
    problems = manifest_problems([bad])
    assert any("entry_point is None" in p for p in problems)


def test_domain_contract_with_entry_point_is_rejected():
    bad = Capability(
        name="phantom/unwired",
        tier="domain_contract",
        entry_point="forge.gateway.router._RUN_COMMANDS",
        evidence=("tests/test_adaptive_control.py",),
        note="contradictory row",
    )
    problems = manifest_problems([bad])
    assert any("at least production_wiring" in p for p in problems)


def test_real_provider_scenario_requires_docs_artifact():
    """A test file alone never proves a provider ran (a driver smoke cannot
    mark a runner cycle as passed)."""
    bad = Capability(
        name="phantom/live",
        tier="real_provider_scenario",
        entry_point="forge.gateway.router._RUN_COMMANDS",
        evidence=("tests/test_slash_routing.py",),
    )
    problems = manifest_problems([bad])
    assert any("under docs/" in p for p in problems)


def test_not_wired_row_requires_a_why():
    bad = Capability(
        name="phantom/silent",
        tier="domain_contract",
        entry_point=None,
        evidence=("tests/test_adaptive_control.py",),
    )
    problems = manifest_problems([bad])
    assert any("must say WHY" in p for p in problems)


def test_duplicate_names_are_rejected():
    row = Capability(
        name="dup/row",
        tier="domain_contract",
        entry_point=None,
        evidence=("tests/test_adaptive_control.py",),
        note="x",
    )
    assert any("duplicate" in p for p in manifest_problems([row, row]))


def test_unknown_tier_is_rejected():
    bad = Capability(
        name="phantom/tier",
        tier="almost_ready",  # type: ignore[arg-type]
        entry_point="x",
        evidence=("tests/test_slash_routing.py",),
    )
    assert any("unknown tier" in p for p in manifest_problems([bad]))


def test_missing_evidence_file_fails_validation(tmp_path):
    bad = Capability(
        name="phantom/evidence",
        tier="production_wiring",
        entry_point="forge.gateway.router._RUN_COMMANDS",
        evidence=("tests/does-not-exist.py",),
    )
    with pytest.raises(Exception) as excinfo:
        validate_manifest([bad], root=tmp_path)
    assert "does-not-exist" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Rendering + the doctor hook
# ---------------------------------------------------------------------------


def test_format_matrix_marks_unwired_rows_explicitly():
    text = format_matrix(CAPABILITIES)
    assert "not wired" in text
    assert "operator-commands/classic" in text
    assert "production_wiring" in text
    assert "17 of" in text  # every seeded row counted
    unwired = sum(1 for row in CAPABILITIES if row.entry_point is None)
    assert f"{unwired} of {len(CAPABILITIES)} capabilities are NOT wired" in text


def test_doctor_capability_check_passes_and_reports_rows():
    result = check_capabilities()
    assert result.status == "pass"
    report = format_report([result])
    assert "capabilities.manifest" in report
    assert f"{len(CAPABILITIES)} rows" in result.detail


def test_doctor_capability_check_fails_on_drift(monkeypatch):
    """Doctor never reports an unavailable command as supported: a drifted
    ingress set turns the check into a FAIL."""
    shrunken = frozenset(set(gateway_router._RUN_COMMANDS) - {"/status"})
    monkeypatch.setattr(gateway_router, "_RUN_COMMANDS", shrunken)
    result = check_capabilities()
    assert result.status == "fail"
    assert "/status" in result.detail
