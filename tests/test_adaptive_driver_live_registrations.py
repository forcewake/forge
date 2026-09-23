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

R28-26 adds the provenance half: the dispatch's declared pin, the
registration's ``verified_against`` and the runner's actually-installed
CLI version are reconciled in ONE report — a drift WARNS (the
registration is evidence of the PAST), never fails the lane.
"""

from __future__ import annotations

import json
import re

import pytest

from forge.adaptive.adapters import DriverMatrix
from forge.adaptive.drivers.live_registrations import (
    COPILOT_ACP_SDK,
    DRIVER_SDK_OF,
    EVIDENCE_CLASSES,
    LIVE_OBSERVED_CAPABILITIES,
    LIVE_REGISTRATIONS,
    OBSERVED_CAPABILITY_SDKS,
    OBSERVED_CAPABILITY_VALUES,
    LiveRegistration,
    ObservedCapabilities,
    _REPO_ROOT,
    install_pin_of,
    observed_capabilities,
    provenance_report,
    registration_verdict,
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
    #: NEXT-13: the rows may exceed the registration sdks by exactly the
    #: contract-tested set — an observation row may cite evidence the
    #: registration matrix refuses to seed (weaker, honestly labelled).
    CONTRACT_TESTED_SDKS = {"copilot-acp"}

    def test_the_live_rows_cover_each_sdk_exactly_once(self) -> None:
        sdks = [row.sdk for row in LIVE_OBSERVED_CAPABILITIES]
        assert len(sdks) == len(set(sdks)), "one observation row per sdk, no ambiguity"
        assert set(sdks) == {entry.sdk for entry in LIVE_REGISTRATIONS} | self.CONTRACT_TESTED_SDKS

    def test_every_live_row_binds_its_registrations_binary_version(self) -> None:
        """Every registration-backed row cites the SAME binary as its
        registration: the recorded ``verified_against[0]`` — the two halves
        of one smoke cannot disagree. Contract-test rows (copilot-acp)
        have NO registration to bind, which is the point."""
        registered = {entry.sdk for entry in LIVE_REGISTRATIONS}
        for row in LIVE_OBSERVED_CAPABILITIES:
            if row.sdk in registered:
                assert row.binary_version == sdk_version_of(row.sdk), row.sdk

    def test_contract_test_rows_carry_no_live_evidence(self) -> None:
        """NEXT-13's honesty bound: every row outside the registration set
        declares the contract-tests evidence class — a live-smoke claim on
        a binary no smoke ran against is refused at construction."""
        registered = {entry.sdk for entry in LIVE_REGISTRATIONS}
        for row in LIVE_OBSERVED_CAPABILITIES:
            if row.sdk in registered:
                assert row.evidence == "live-smoke", row.sdk
            else:
                assert row.sdk in self.CONTRACT_TESTED_SDKS, row.sdk
                assert row.evidence == "contract-tests", row.sdk

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
        with pytest.raises(ValueError, match="evidence class"):
            ObservedCapabilities(
                sdk="claude-sdk",
                binary_version="b",
                install_pin="1",
                observed=("turn",),
                date="2026-09-21",
                evidence="vibes",
            )

    def test_install_pin_of_reports_the_newest_rows_pin(self) -> None:
        for row in LIVE_OBSERVED_CAPABILITIES:
            assert install_pin_of(row.sdk) == row.install_pin
        assert install_pin_of("not-an-sdk") is None


# ---------------------------------------------------------------------------
# NEXT-13 — the tested Copilot ACP capability profile
# ---------------------------------------------------------------------------


class TestCopilotAcpCapabilityProfile:
    """The profile reflects TESTED reality exactly: the three behaviors the
    contract suite exercises, and NOTHING more — the honest-not-supported
    half is the deliverable."""

    def _row(self) -> ObservedCapabilities:
        row = observed_capabilities(COPILOT_ACP_SDK, "Copilot 1.0.86 (protocol v1)")
        assert row is not None, "the copilot-acp row must exist"
        return row

    def test_the_row_exists_with_exactly_the_tested_set_and_nothing_more(self) -> None:
        row = self._row()
        # ONLY what tests/test_adaptive_driver_copilot_acp.py exercises:
        # the prompt flow, the cancel ledger, serial next-turn prompts.
        assert row.observed == ("turn", "native_interrupt", "next_turn_input")
        for capability in row.observed:
            assert row.supports(capability) is True

    def test_unobserved_names_the_untested_set_correctly(self) -> None:
        row = self._row()
        # mid_turn_steer is a PROTOCOL absence (ACP v1 §5); the interrupt
        # OUTCOME needs a real binary's lying wire (#4561); neither
        # checkpoint-portability behavior was even attempted.
        assert row.unobserved() == (
            "interrupt_outcome_observed",
            "mid_turn_steer",
            "wip_export",
            "cross_runner_restore",
        )
        for capability in row.unobserved():
            assert row.supports(capability) is False

    def test_the_row_is_contract_test_evidence_never_a_live_claim(self) -> None:
        row = self._row()
        assert row.evidence == "contract-tests"
        # No live registration backs it: seeding and provenance make NO
        # present-tense claim about a copilot binary (the research doc's
        # "once a smoke exists" gate).
        assert COPILOT_ACP_SDK not in {entry.sdk for entry in LIVE_REGISTRATIONS}
        assert "copilot-sdk-lane" not in DRIVER_SDK_OF
        assert registration_verdict("copilot-sdk-lane", "Copilot 1.0.86 (protocol v1)") is None
        assert OBSERVED_CAPABILITY_SDKS == (
            "claude-sdk",
            "codex-app",
            "opencode-server",
            COPILOT_ACP_SDK,
        )
        assert EVIDENCE_CLASSES == ("live-smoke", "contract-tests")

    def test_the_install_pin_is_the_pin_the_copilot_lanes_install(self) -> None:
        from forge.harnesses.script_render import DEFAULT_DRIVER_VERSIONS

        row = self._row()
        assert row.install_pin == DEFAULT_DRIVER_VERSIONS["copilot-sdk-lane"]
        assert row.install_pin == DEFAULT_DRIVER_VERSIONS["copilot"]
        assert install_pin_of(COPILOT_ACP_SDK) == row.install_pin

    def test_an_unknown_or_upgraded_copilot_binary_answers_unknown(self) -> None:
        assert observed_capabilities(COPILOT_ACP_SDK, "Copilot 1.0.88 (protocol v1)") is None
        assert observed_capabilities(COPILOT_ACP_SDK, "") is None
        with pytest.raises(ValueError, match="sdk must be one of"):
            ObservedCapabilities(
                sdk="copilot-cli",
                binary_version="Copilot 1.0.86 (protocol v1)",
                install_pin="1.0.86",
                observed=("turn",),
                date="2026-09-23",
            )


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


# ---------------------------------------------------------------------------
# R28-26 — exact-binary provenance: declared pin vs registration vs install
# ---------------------------------------------------------------------------


class TestRegistrationVerdict:
    def test_every_mapped_driver_maps_to_a_registered_sdk(self) -> None:
        for driver, sdk in DRIVER_SDK_OF.items():
            assert sdk in {entry.sdk for entry in LIVE_REGISTRATIONS}, driver

    def test_a_matching_install_verdicts_match(self) -> None:
        for driver, sdk in DRIVER_SDK_OF.items():
            verdict = registration_verdict(driver, sdk_version_of(sdk))
            assert verdict is not None, driver
            assert verdict.status == "match"
            assert verdict.warning == ""
            assert verdict.recorded == verdict.installed

    def test_drift_warns_but_is_never_a_failure(self) -> None:
        verdict = registration_verdict("codex-sdk-lane", "codex-cli 0.154.0")
        assert verdict is not None
        assert verdict.status == "drift"
        assert "codex-cli 0.153.4" in verdict.warning  # the PAST is named
        assert "codex-cli 0.154.0" in verdict.warning  # the PRESENT is named
        assert "driver_live_smoke" in verdict.warning  # the remedy is named

    def test_an_unreported_install_is_unknown_present(self) -> None:
        verdict = registration_verdict("claude-sdk-lane", "")
        assert verdict is not None
        assert verdict.status == "unknown_present"
        assert "did not report" in verdict.warning

    def test_an_unregistered_driver_has_no_verdict_at_all(self) -> None:
        assert registration_verdict("grok-build", "grok 1.0.30") is None
        assert registration_verdict("copilot", "1.0.86") is None
        assert registration_verdict("not-a-driver", "whatever") is None


class TestProvenanceReport:
    def test_all_three_sources_are_reconciled(self) -> None:
        report = provenance_report("codex-sdk-lane", installed_cli_version="codex-cli 0.153.4")
        # what the dispatch declared (the shipped default pin)
        assert report["declared_pin"] == "0.153.4"
        # what the registration verified
        assert report["registration_verified"] == "codex-cli 0.153.4"
        assert report["registration_date"] == "2026-09-21"
        # what the runner actually installed
        assert report["installed_cli_version"] == "codex-cli 0.153.4"
        assert report["registration_status"] == "match"
        assert report["pin_matches_install"] is True
        assert report["warnings"] == []

    def test_the_declared_pin_can_be_supplied(self) -> None:
        report = provenance_report(
            "codex-sdk-lane",
            installed_cli_version="codex-cli 0.153.4",
            declared_pin="0.153.4",
        )
        assert report["declared_pin"] == "0.153.4"

    def test_a_version_mismatch_produces_a_warning(self) -> None:
        # The negative/recovery case verbatim: "swap same-version/
        # different-build fixture binaries" — the registration's past
        # cannot promote the present; the report SAYS so.
        report = provenance_report("codex-sdk-lane", installed_cli_version="codex-cli 0.154.0")
        assert report["registration_status"] == "drift"
        assert report["pin_matches_install"] is False
        warnings = " ".join(str(w) for w in report["warnings"])
        assert "verified" in warnings and "0.153.4" in warnings and "0.154.0" in warnings
        assert "re-run" in warnings  # the remedy, not just the alarm

    def test_an_unknown_install_reports_unknown(self) -> None:
        report = provenance_report("claude-code")
        assert report["installed_cli_version"] == ""
        assert report["registration_status"] == "unknown_present"
        assert report["pin_matches_install"] is None  # no claim either way
        assert any("did not report" in str(w) for w in report["warnings"])

    def test_the_latest_pin_is_flagged_as_unpinned(self) -> None:
        report = provenance_report(
            "claude-code",
            installed_cli_version="claude 2.1.290 (Claude Code)",
            declared_pin="latest",
        )
        assert any("'latest'" in str(w) for w in report["warnings"])
        assert report["pin_matches_install"] is None
        assert report["registration_status"] == "drift"  # 2.1.273 was verified

    def test_an_unregistered_driver_is_reported_as_such(self) -> None:
        report = provenance_report("grok-build", installed_cli_version="grok 1.0.30")
        assert report["registration_status"] == "unregistered"
        assert report["registration_verified"] == ""
        assert any("never been smoke-verified" in str(w) for w in report["warnings"])

    def test_the_report_is_json_shaped(self) -> None:
        report = provenance_report("opencode-sdk-lane", installed_cli_version="opencode v2.0.10")
        assert json.loads(json.dumps(report)) == report


class TestHarnessFingerprint:
    def test_the_fingerprint_env_lands_in_the_provenance(self) -> None:
        from forge.harness_entry import FORGE_DRIVER_FINGERPRINT_ENV, driver_provenance

        assert FORGE_DRIVER_FINGERPRINT_ENV == "FORGE_DRIVER_FINGERPRINT"
        provenance = driver_provenance(
            "codex-sdk-lane",
            env={FORGE_DRIVER_FINGERPRINT_ENV: "codex-cli 0.153.4"},
        )
        assert provenance["installed_cli_version"] == "codex-cli 0.153.4"
        assert provenance["declared_pin"] == "0.153.4"  # resolved from the env pins
        assert provenance["registration_status"] == "match"
        assert re.fullmatch(r"[0-9a-f]{64}", provenance["forge_wheel_sha256"])

    def test_the_fingerprint_ride_the_candidate_meta_additively(self, tmp_path, monkeypatch):
        from forge.harness_entry import driver_provenance, emit_candidate_meta

        monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
        monkeypatch.delenv("GITHUB_RUN_ATTEMPT", raising=False)
        staged = tmp_path / "forge-output"
        staged.mkdir()
        diff = staged / "candidate.diff"
        diff.write_bytes(b"+one line\n")
        provenance = driver_provenance(
            "claude-sdk-lane",
            env={
                "FORGE_DRIVER_FINGERPRINT": "claude 2.1.273 (Claude Code)",
                "FORGE_DRIVER_VERSIONS": json.dumps({"claude-sdk-lane": "2.1.273"}),
            },
        )
        kwargs = dict(
            run_id="d" * 32,
            attempt_base_oid="1" * 40,
            driver="claude-sdk-lane",
            model="m",
            diff_file=str(diff),
        )
        with_meta = emit_candidate_meta(
            meta_file=str(staged / "with.json"), driver_provenance=provenance, **kwargs
        )
        assert with_meta["driver_provenance"] == provenance
        assert with_meta["driver_provenance"]["registration_status"] == "match"

        # additive: NOT supplied → the key is absent, pre-R28-26 metas unchanged
        without = emit_candidate_meta(meta_file=str(staged / "without.json"), **kwargs)
        assert "driver_provenance" not in without

    def test_a_mismatched_fingerprint_warns_at_lane_startup(self):
        from forge.harness_entry import lane_provenance_warning

        warning = lane_provenance_warning(
            "codex-sdk-lane", env={"FORGE_DRIVER_FINGERPRINT": "codex-cli 0.154.0"}
        )
        assert "0.153.4" in warning and "0.154.0" in warning
        # the matching lane says nothing — a quiet start is a good start
        quiet = lane_provenance_warning(
            "codex-sdk-lane", env={"FORGE_DRIVER_FINGERPRINT": "codex-cli 0.153.4"}
        )
        assert quiet == ""

    def test_a_drifted_fingerprint_is_a_warning_never_an_error(self):
        # The lane-startup posture end to end: drift (and an unverified
        # present) is SAID loudly — it must never raise or fail closed,
        # because the registration is evidence of the PAST and the
        # fingerprint is the PRESENT.
        from forge.harness_entry import lane_provenance_warning

        warning = lane_provenance_warning(
            "claude-code", env={"FORGE_DRIVER_FINGERPRINT": "claude 2.1.290 (Claude Code)"}
        )
        assert warning  # drift is said
        assert "PAST" in warning or "re-run" in warning

    def test_forge_install_sha256_is_stable_and_shaped(self) -> None:
        from forge.harness_entry import forge_install_sha256

        first = forge_install_sha256()
        assert forge_install_sha256() == first  # deterministic for one install
        assert first == "" or re.fullmatch(r"[0-9a-f]{64}", first)
