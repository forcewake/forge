"""Tests for the reachability-based capability manifest (review NXT-02).

The honesty guard and its negatives: no row claims production wiring without
an entry point, the routed-command claims match the LIVE ingress sets, the
adaptive commands are recorded as wired behind their rollout flag, and
removing a binding in the gateway breaks the claim instead of silently
passing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import forge.gateway.azure_webhook as azure_webhook
import forge.gateway.github_webhook as github_webhook
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


def _repo_root() -> Path:
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
    classic = _by_name("operator-commands/classic")
    adaptive = _by_name("operator-commands/adaptive")
    # The live routed set is EXACTLY the classic verbs plus the adaptive
    # verbs — nothing phantom, nothing missing.
    assert set(routed) == set(classic.commands) | set(adaptive.commands)
    assert set(classic.commands) == set(CLASSIC_OPERATOR_COMMANDS)
    assert set(CLASSIC_OPERATOR_COMMANDS) == set(routed) - set(ADAPTIVE_OPERATOR_COMMANDS)


def test_adaptive_commands_are_routed_behind_the_rollout_flag():
    """NXT-10 raised the adaptive row: all four verbs are parsed and routed
    by the three ingresses through ControlCommandRouter — wired code with an
    entry point, honestly gated by FORGE_ADAPTIVE_COMMANDS_ENABLED (default
    OFF; with the flag off the gateways do not parse the verbs at all)."""
    routed = ingress_routed_commands()
    for command in ADAPTIVE_OPERATOR_COMMANDS:
        assert command in routed
    adaptive = _by_name("operator-commands/adaptive")
    assert adaptive.tier == "production_wiring"
    assert adaptive.entry_point and "command_router" in adaptive.entry_point
    assert adaptive.entry_point and "FORGE_ADAPTIVE_COMMANDS_ENABLED" in adaptive.entry_point
    assert tuple(adaptive.commands) == ADAPTIVE_OPERATOR_COMMANDS
    assert "default OFF" in adaptive.note


def test_adaptive_router_flag_defaults_off():
    """The rollout gate fails closed: without the env var the gateways'
    parsed command sets do NOT include the adaptive verbs (zero routing —
    not parse-then-refuse)."""
    from forge.adaptive.command_router import (
        ADAPTIVE_NOTE_COMMANDS,
        adaptive_commands_enabled,
        adaptive_command_set,
    )

    assert adaptive_commands_enabled({}) is False
    assert adaptive_command_set({}) == frozenset()
    assert adaptive_commands_enabled({"FORGE_ADAPTIVE_COMMANDS_ENABLED": "1"}) is True
    assert adaptive_command_set({"FORGE_ADAPTIVE_COMMANDS_ENABLED": "on"}) == (
        ADAPTIVE_NOTE_COMMANDS
    )


def test_helper_without_production_caller_is_library_level_only():
    """A capability with an entry-point-free helper (the durable mailbox —
    PostgresMailbox exists and is FI-tested, but no production caller
    SELECTS it) is reported domain_contract — never dressed as wiring."""
    row = _by_name("adaptive/durable-mailbox")
    assert row.tier == "domain_contract"
    assert row.entry_point is None
    assert row.note  # the WHY is mandatory


def test_steering_bridge_is_wired_but_default_off():
    """NXT-11 raised the bridge to production_wiring: lane_driver attaches
    it on every lane — gated by FORGE_STEERING_ENABLED, default OFF (the
    ingress does not route /steer yet, so ON would be a structural
    no-op). The tier reflects the REACHABLE entry point; the note keeps
    the rollout state honest."""
    row = _by_name("adaptive/steering-bridge")
    assert row.tier == "production_wiring"
    assert row.entry_point and "FORGE_STEERING_ENABLED" in row.entry_point
    assert "default OFF" in row.note


def _unbind_everywhere(monkeypatch, command: str) -> None:
    """Remove a command from ALL three checked ingresses — an application
    binding is gone, not one provider's spelling of it. The adaptive verbs
    live in the per-gateway ``_ADAPTIVE_NOTE_COMMANDS`` attributes, so an
    unbound adaptive verb must vanish from all three too."""
    for module, attr in (
        (gateway_router, "_RUN_COMMANDS"),
        (github_webhook, "_GITHUB_RUN_COMMANDS"),
        (azure_webhook, "_AZDO_RUN_COMMANDS"),
    ):
        monkeypatch.setattr(module, attr, frozenset(set(getattr(module, attr)) - {command}))
    for module in (gateway_router, github_webhook, azure_webhook):
        bound = getattr(module, "_ADAPTIVE_NOTE_COMMANDS", frozenset())
        monkeypatch.setattr(module, "_ADAPTIVE_NOTE_COMMANDS", frozenset(bound - {command}))


def test_removing_an_ingress_binding_breaks_the_claim(monkeypatch):
    """Negative/recovery: drop one command from the live ingress sets and the
    manifest must flag the now-unsupported claim (no silent over-claim)."""
    _unbind_everywhere(monkeypatch, "/reconcile")
    problems = manifest_problems(CAPABILITIES)
    flagged = [p for p in problems if "/reconcile" in p and "does not route" in p]
    assert flagged, f"a removed binding must surface as a problem, got: {problems}"


def test_removing_an_adaptive_binding_breaks_the_claim(monkeypatch):
    """The same honesty guard on the NXT-10 surface: unbind /pause in every
    gateway and the adaptive row's claim must FAIL (never silently pass)."""
    _unbind_everywhere(monkeypatch, "/pause")
    problems = manifest_problems(CAPABILITIES)
    flagged = [p for p in problems if "/pause" in p and "does not route" in p]
    assert flagged, f"a removed adaptive binding must surface as a problem, got: {problems}"


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
    _unbind_everywhere(monkeypatch, "/status")
    result = check_capabilities()
    assert result.status == "fail"
    assert "/status" in result.detail
