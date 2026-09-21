"""Tests for the live-verified DriverMatrix seed (EXE-07's honest half).

The seed's whole value is its honesty contract: an entry is
registrable ONLY when its evidence file exists in the checkout AND the
recorded run passed. These tests pin both refusals with synthetic
registrations (the REAL ones stay untouched — mutating repo evidence
in a test would be forging it).
"""

from __future__ import annotations

import json

import pytest

from forge.adaptive.adapters import DriverMatrix
from forge.adaptive.drivers.live_registrations import (
    LIVE_REGISTRATIONS,
    LiveRegistration,
    _REPO_ROOT,
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
