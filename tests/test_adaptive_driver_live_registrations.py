"""Tests for the live-verified DriverMatrix seed (EXE-07's honest half).

The seed's whole value is its honesty contract: an entry is
registrable ONLY when its evidence file exists in the checkout AND the
recorded run passed. These tests pin both refusals with synthetic
registrations (the REAL ones stay untouched — mutating repo evidence
in a test would be forging it).

NXT-27 adds the versioned observed-behavior half: capabilities are
recorded PER BINARY VERSION, an unknown/upgraded version answers
unknown (never the previous version's observations), and seeding with
``installed_versions`` refuses both drift and an unknown present.
"""

from __future__ import annotations

import json

import pytest

from forge.adaptive.adapters import DriverMatrix
from forge.adaptive.drivers.live_registrations import (
    LIVE_OBSERVED_CAPABILITIES,
    LIVE_REGISTRATIONS,
    OBSERVED_CAPABILITY_VALUES,
    LiveRegistration,
    ObservedCapabilities,
    _REPO_ROOT,
    install_pin_of,
    observed_capabilities,
    sdk_version_of,
    seed_live_matrix,
)


def test_every_entrys_evidence_exists_in_the_checkout() -> None:
    """No registration may cite evidence the repo does not carry."""
    for entry in LIVE_REGISTRATIONS:
        assert (_REPO_ROOT / entry.evidence).is_file(), entry.evidence


def test_seed_refuses_missing_evidence(tmp_path, monkeypatch) -> None:
    entry = LiveRegistration(
        sdk="claude-sdk",
        provider_route="a-route",
        credential_mode="a-mode",
        evidence="docs/evaluation/does-not-exist.json",
        verified_against=("x"),
        date="2026-09-21",
    )
    monkeypatch.setattr("forge.adaptive.drivers.live_registrations.LIVE_REGISTRATIONS", (entry,))
    with pytest.raises(FileNotFoundError, match="not registerable"):
        seed_live_matrix()


def test_seed_refuses_a_failed_evidence_run(tmp_path, monkeypatch) -> None:
    failed = tmp_path / "failed.json"
    failed.write_text(json.dumps({"all_ok": False, "failures": ["turn1"]}))
    entry = LiveRegistration(
        sdk="codex-app",
        provider_route="a-route",
        credential_mode="a-mode",
        evidence=str(failed),
        verified_against=("x"),
        date="2026-09-21",
    )
    monkeypatch.setattr("forge.adaptive.drivers.live_registrations.LIVE_REGISTRATIONS", (entry,))
    with pytest.raises(ValueError, match="FAILED"):
        seed_live_matrix()


def test_seed_registers_a_passing_evidence_run(tmp_path, monkeypatch) -> None:
    passed = tmp_path / "passed.json"
    passed.write_text(json.dumps({"all_ok": True, "steps": []}))
    entry = LiveRegistration(
        sdk="opencode-server",
        provider_route="a-route",
        credential_mode="a-mode",
        evidence=str(passed),
        verified_against=("x"),
        date="2026-09-21",
    )
    monkeypatch.setattr("forge.adaptive.drivers.live_registrations.LIVE_REGISTRATIONS", (entry,))
    matrix = seed_live_matrix()
    assert matrix.supports("opencode-server", "a-route", "a-mode") is True


def test_entries_cover_each_sdk_at_most_once() -> None:
    sdks = [entry.sdk for entry in LIVE_REGISTRATIONS]
    assert len(sdks) == len(set(sdks)), "one registration row per sdk, no ambiguity"


def test_sdk_version_of_reports_the_recorded_binary() -> None:
    for entry in LIVE_REGISTRATIONS:
        assert sdk_version_of(entry.sdk) == entry.verified_against[0]
    assert sdk_version_of("not-an-sdk") is None


def test_registration_rejects_unknown_sdk() -> None:
    with pytest.raises(ValueError, match="sdk must be one of"):
        LiveRegistration(
            sdk="not-an-sdk",
            provider_route="r",
            credential_mode="m",
            evidence="docs/evaluation/x.json",
            verified_against=("x"),
            date="2026-09-21",
        )


def test_seed_on_an_existing_matrix_accumulates(tmp_path, monkeypatch) -> None:
    passed = tmp_path / "passed.json"
    passed.write_text(json.dumps({"all_ok": True}))
    entry = LiveRegistration(
        sdk="claude-sdk",
        provider_route="r2",
        credential_mode="m2",
        evidence=str(passed),
        verified_against=("x"),
        date="2026-09-21",
    )
    monkeypatch.setattr("forge.adaptive.drivers.live_registrations.LIVE_REGISTRATIONS", (entry,))
    matrix = DriverMatrix()
    matrix.register("claude-sdk", "r1", "m1")
    seeded = seed_live_matrix(matrix)
    assert seeded is matrix
    assert matrix.supports("claude-sdk", "r1", "m1")
    assert matrix.supports("claude-sdk", "r2", "m2")


# ---------------------------------------------------------------------------
# NXT-27 — capabilities as versioned observed behavior
# ---------------------------------------------------------------------------


class TestObservedCapabilities:
    def test_the_live_rows_cover_each_sdk_exactly_once(self) -> None:
        sdks = [row.sdk for row in LIVE_OBSERVED_CAPABILITIES]
        assert sorted(sdks) == sorted({entry.sdk for entry in LIVE_REGISTRATIONS})
        assert len(sdks) == len(set(sdks))

    def test_every_row_binds_its_registrations_binary_version(self) -> None:
        """The observation row and the registration cite the SAME binary:
        the recorded ``verified_against[0]`` — the two halves of one
        smoke cannot disagree."""
        for row in LIVE_OBSERVED_CAPABILITIES:
            assert row.binary_version == sdk_version_of(row.sdk), row.sdk

    def test_every_install_pin_is_the_binary_versions_version_spec(self) -> None:
        for row in LIVE_OBSERVED_CAPABILITIES:
            assert row.install_pin in row.binary_version, row.sdk

    def test_lookup_answers_only_for_the_exact_recorded_version(self) -> None:
        for row in LIVE_OBSERVED_CAPABILITIES:
            found = observed_capabilities(row.sdk, row.binary_version)
            assert found is row
            assert found is not None and found.observed == row.observed

    def test_an_upgraded_binary_inherits_nothing(self) -> None:
        # The negative/recovery test verbatim: "upgrade a vendor binary
        # without rerunning the profile canary" — the drifted version
        # answers UNKNOWN (None), never the previous version's rows.
        for row in LIVE_OBSERVED_CAPABILITIES:
            upgraded = f"{row.binary_version}+upgraded"
            assert observed_capabilities(row.sdk, upgraded) is None
        assert observed_capabilities("claude-sdk", "") is None
        assert observed_capabilities("not-an-sdk", "whatever") is None

    def test_supports_refuses_names_outside_the_closed_vocabulary(self) -> None:
        row = LIVE_OBSERVED_CAPABILITIES[0]
        with pytest.raises(ValueError, match="unknown observed capability"):
            row.supports("definitely_not_a_capability")

    def test_unobserved_vocabulary_answers_false_and_is_recorded(self) -> None:
        # "untested" is data: the claude row does not claim mid-turn
        # steering (its send was NEXT-turn buffering), and NO row claims
        # checkpoint portability — an interrupt smoke never implies one.
        claude = observed_capabilities("claude-sdk", "claude 2.1.273 (Claude Code)")
        assert claude is not None
        assert claude.supports("next_turn_input") is True
        assert claude.supports("mid_turn_steer") is False
        assert "mid_turn_steer" in claude.unobserved()
        for row in LIVE_OBSERVED_CAPABILITIES:
            assert row.supports("wip_export") is False
            assert row.supports("cross_runner_restore") is False

    def test_codex_observes_mid_turn_steer_and_the_interrupt_outcome(self) -> None:
        codex = observed_capabilities("codex-app", "codex-cli 0.153.4")
        assert codex is not None
        assert codex.supports("mid_turn_steer") is True
        assert codex.supports("interrupt_outcome_observed") is True
        assert codex.supports("next_turn_input") is False  # never exercised live

    def test_the_vocabulary_is_the_seven_behaviors(self) -> None:
        assert OBSERVED_CAPABILITY_VALUES == (
            "turn",
            "native_interrupt",
            "interrupt_outcome_observed",
            "mid_turn_steer",
            "next_turn_input",
            "wip_export",
            "cross_runner_restore",
        )

    def test_row_construction_failures_are_fail_closed(self) -> None:
        with pytest.raises(ValueError, match="sdk must be one of"):
            ObservedCapabilities(
                sdk="not-an-sdk",
                binary_version="b",
                install_pin="1",
                observed=("turn",),
                date="2026-09-21",
            )
        with pytest.raises(ValueError, match="closed vocabulary"):
            ObservedCapabilities(
                sdk="claude-sdk",
                binary_version="b",
                install_pin="1",
                observed=("telepathy",),
                date="2026-09-21",
            )
        with pytest.raises(ValueError, match="records nothing"):
            ObservedCapabilities(
                sdk="claude-sdk",
                binary_version="b",
                install_pin="1",
                observed=(),
                date="2026-09-21",
            )
        with pytest.raises(ValueError, match="install_pin"):
            ObservedCapabilities(
                sdk="claude-sdk",
                binary_version="b",
                install_pin="",
                observed=("turn",),
                date="2026-09-21",
            )

    def test_install_pin_of_reports_the_newest_rows_pin(self) -> None:
        for row in LIVE_OBSERVED_CAPABILITIES:
            assert install_pin_of(row.sdk) == row.install_pin
        assert install_pin_of("not-an-sdk") is None


class TestSeedingVersionGate:
    def _installed(self) -> dict[str, str]:
        """The mapping a machine running the LIVE-verified binaries reports."""
        return {entry.sdk: entry.verified_against[0] for entry in LIVE_REGISTRATIONS}

    def test_matching_installed_versions_seed_normally(self, monkeypatch) -> None:
        monkeypatch.chdir(_REPO_ROOT)  # the real evidence is relative to it
        matrix = seed_live_matrix(installed_versions=self._installed())
        for entry in LIVE_REGISTRATIONS:
            assert matrix.supports(entry.sdk, entry.provider_route, entry.credential_mode)

    def test_a_drifted_binary_is_refused_as_past_evidence(self, monkeypatch) -> None:
        # The upgrade-without-rerun case: the registration was verified
        # against an older binary — a stale registration is evidence of
        # the PAST, not a claim about the PRESENT.
        monkeypatch.chdir(_REPO_ROOT)
        drifted = self._installed()
        drifted["codex-app"] = "codex-cli 0.154.0"
        with pytest.raises(ValueError, match=r"codex-cli 0\.154\.0.*vendor drift"):
            seed_live_matrix(installed_versions=drifted)

    def test_an_unknown_installed_version_refuses_to_seed(self, monkeypatch) -> None:
        monkeypatch.chdir(_REPO_ROOT)
        partial = {"codex-app": "codex-cli 0.153.4"}  # claude/opencode unseen
        with pytest.raises(ValueError, match="installed binary version is UNKNOWN"):
            seed_live_matrix(installed_versions=partial)

    def test_without_the_mapping_no_present_tense_claim_is_made(self) -> None:
        # The evidence-only path (historical seeding on a machine with no
        # binaries installed): unchanged legacy behavior.
        matrix = seed_live_matrix()
        assert matrix.supports("opencode-server", "zai-coding-plan", "server-basic+stored-key")
