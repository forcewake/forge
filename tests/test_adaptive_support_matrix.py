"""NEXT-26 — the release support matrix from EXECUTED profile evidence.

The review's refusal, pinned as tests: "a batch Claude run cannot
promote Copilot steering support" — support is stated per
(driver, provider, recipe) with the evidence class each status name
rests on, and the matrix NEVER claims more than the evidence:

- ``tested`` requires a live registration mapped to the driver whose
  evidence artifact EXISTS in the checkout — a missing artifact is a
  loud problem and a degraded row, never a silently kept badge;
- ``supported`` requires the capability manifest to carry the driver at
  a wired tier WITH an entry-point test pointer;
- ``declared_only`` is a shipped recipe with no test-class evidence;
- ``unsupported`` is anything the inputs name that nothing declares.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.adaptive.drivers.live_registrations import LiveRegistration
from forge.adaptive.support_matrix import (
    DECLARED_ONLY,
    STATUS_VALUES,
    SUPPORTED,
    TESTED,
    UNSUPPORTED,
    SupportRow,
    format_support_matrix,
    support_matrix,
)
from forge.capability_manifest import Capability

# ---------------------------------------------------------------------------
# Parameterized fixtures: injectable evidence, tmp-path artifacts
# ---------------------------------------------------------------------------


def _registration(
    *, sdk: str = "claude-sdk", provider: str = "zai-anthropic-gateway", evidence: str
) -> LiveRegistration:
    return LiveRegistration(
        sdk=sdk,
        provider_route=provider,
        credential_mode="byok-env-token",
        evidence=evidence,
        verified_against=("some-cli 1.2.3",),
        date="2026-09-23",
    )


def _write_evidence(root: Path, relative: str) -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"all_ok": true}\n')
    return relative


class TestTheEvidenceRungs:
    def test_a_tested_driver_shows_tested(self, tmp_path):
        evidence = _write_evidence(tmp_path, "docs/evaluation/x/claude-live.json")
        matrix = support_matrix(
            manifest_rows=[],
            registrations=[_registration(evidence=evidence)],
            driver_sdk={"claude-code": "claude-sdk"},
            shipped={"claude-code"},
            evidence_root=tmp_path,
        )

        rows = matrix.of_driver("claude-code")
        (row,) = rows
        assert row.status == TESTED
        assert row.provider == "zai-anthropic-gateway"
        assert row.recipe == "byok-env-token"
        assert row.evidence == (evidence,)
        assert "live smoke 2026-09-23" in row.note
        assert matrix.problems == ()

    def test_a_declared_only_recipe_shows_declared_only(self, tmp_path):
        matrix = support_matrix(
            manifest_rows=[],
            registrations=[],
            driver_sdk={},
            shipped={"dotnet-lane"},
            evidence_root=tmp_path,
        )

        (row,) = matrix.of_driver("dotnet-lane")
        assert row.status == DECLARED_ONLY
        assert row.provider == ""  # no provider route is claimed
        assert "declared" in row.note

    def test_a_wired_manifest_row_with_a_test_pointer_is_supported(self, tmp_path):
        matrix = support_matrix(
            manifest_rows=[
                Capability(
                    name="harness/batch-grok-build",
                    tier="production_wiring",
                    entry_point="forge.runs.backends",
                    evidence=("tests/test_harness_selection.py",),
                )
            ],
            registrations=[],
            driver_sdk={},
            shipped={"grok-build"},
            evidence_root=tmp_path,
        )

        (row,) = matrix.of_driver("grok-build")
        assert row.status == SUPPORTED
        assert "entry-point test" in row.note
        assert "no recorded real-provider run" in row.note

    def test_a_manifest_row_without_a_test_pointer_stays_declared_only(self, tmp_path):
        """A wired tier whose evidence carries no tests/ pointer has NOT
        earned the supported rung — docs alone are not wiring evidence."""
        matrix = support_matrix(
            manifest_rows=[
                Capability(
                    name="harness/batch-copilot",
                    tier="production_wiring",
                    entry_point="forge.runs.backends",
                    evidence=("ci/templates/copilot.gitlab-ci.yml",),
                )
            ],
            registrations=[],
            driver_sdk={},
            shipped={"copilot"},
            evidence_root=tmp_path,
        )

        (row,) = matrix.of_driver("copilot")
        assert row.status == DECLARED_ONLY

    def test_an_undeclared_driver_is_unsupported(self, tmp_path):
        matrix = support_matrix(
            manifest_rows=[],
            registrations=[],
            driver_sdk={"neo-lane": "claude-sdk"},  # named, but nothing declares it
            shipped=set(),
            evidence_root=tmp_path,
        )

        (row,) = matrix.of_driver("neo-lane")
        assert row.status == UNSUPPORTED
        assert "nothing" in row.note or "no shipped recipe" in row.note


class TestTheMatrixNeverClaimsMoreThanTheEvidence:
    def test_a_registration_with_missing_evidence_degrades_not_claims(self, tmp_path):
        """The teeth: a registration citing an artifact the checkout
        cannot show is NOT tested — the row falls to the declaration
        rung and the absence is a problem, never a silent pass."""
        matrix = support_matrix(
            manifest_rows=[],
            registrations=[_registration(evidence="docs/evaluation/x/gone.json")],
            driver_sdk={"claude-code": "claude-sdk"},
            shipped={"claude-code"},
            evidence_root=tmp_path,
        )

        (row,) = matrix.of_driver("claude-code")
        assert row.status != TESTED
        assert row.status == DECLARED_ONLY  # declared (shipped), unproven
        assert any(
            "not in this checkout" in problem and "NOT tested" in problem
            for problem in matrix.problems
        )

    def test_support_is_never_inherited_from_a_sibling_sdk(self, tmp_path):
        """A registration verifies the binary of the sdk it names — a
        driver the DRIVER_SDK_OF table does not map to that sdk gets
        nothing from it (a batch Claude smoke cannot promote Copilot)."""
        evidence = _write_evidence(tmp_path, "docs/evaluation/x/claude-live.json")
        matrix = support_matrix(
            manifest_rows=[],
            registrations=[_registration(evidence=evidence)],
            driver_sdk={"claude-code": "claude-sdk"},  # copilot NOT mapped
            shipped={"claude-code", "copilot"},
            evidence_root=tmp_path,
        )

        assert matrix.of_driver("claude-code")[0].status == TESTED
        assert matrix.of_driver("copilot")[0].status == DECLARED_ONLY

    def test_two_manifest_rows_for_one_driver_are_a_problem_not_a_guess(self, tmp_path):
        row = Capability(
            name="harness/batch-copilot",
            tier="production_wiring",
            entry_point="e",
            evidence=("tests/test_x.py",),
        )
        matrix = support_matrix(
            manifest_rows=[row, row],
            registrations=[],
            driver_sdk={},
            shipped=set(),
            evidence_root=tmp_path,
        )

        assert any("two capability-manifest rows" in problem for problem in matrix.problems)

    def test_a_domain_contract_manifest_row_declares_only(self, tmp_path):
        matrix = support_matrix(
            manifest_rows=[
                Capability(
                    name="sdk-lane/neo",
                    tier="domain_contract",
                    entry_point=None,
                    evidence=("tests/test_neo.py",),
                    note="not wired",
                )
            ],
            registrations=[],
            driver_sdk={},
            shipped=set(),
            evidence_root=tmp_path,
        )

        (row,) = matrix.of_driver("neo-sdk-lane")
        assert row.status == DECLARED_ONLY

    def test_the_status_vocabulary_is_closed(self):
        with pytest.raises(ValueError):
            SupportRow(driver="d", provider="", recipe="r", status="certified")


class TestTheSeededMatrix:
    def test_the_seeded_tree_folds_with_its_evidence_on_disk(self):
        """The real matrix: every tested row's artifacts exist, and the
        rung distribution is the honest one for this checkout."""
        matrix = support_matrix()

        assert matrix.problems == ()
        tested = {row.driver for row in matrix.rows if row.status == TESTED}
        # The live-registered binaries: claude (batch + lane share it),
        # codex, opencode — and NOT copilot or dotnet.
        assert {"claude-code", "claude-sdk-lane", "codex-sdk-lane", "opencode"} <= tested
        assert "copilot-sdk-lane" not in tested
        assert "dotnet-lane" not in tested
        for row in matrix.rows:
            if row.status == TESTED:
                assert row.evidence, "a tested row cites its evidence"
                assert any(pointer.startswith("docs/") for pointer in row.evidence)
        # Every status is from the closed vocabulary.
        assert {row.status for row in matrix.rows} <= set(STATUS_VALUES)

    def test_the_batch_claude_evidence_does_not_certify_the_acp_copilot_lane(self):
        """The review's exact sentence, as an assertion: the copilot ACP
        lane is at most declared, whatever claude-code proved."""
        matrix = support_matrix()
        (row,) = matrix.of_driver("copilot-sdk-lane")
        assert row.status in (DECLARED_ONLY, SUPPORTED)
        assert row.status == DECLARED_ONLY  # no manifest row, no registration

    def test_to_json_is_the_doctor_field_shape(self):
        document = support_matrix().to_json()

        assert set(document) == {"legend", "rows", "problems"}
        assert set(document["legend"]) == set(STATUS_VALUES)
        for row in document["rows"]:
            assert set(row) == {"driver", "provider", "recipe", "status", "evidence", "note"}

    def test_format_renders_every_row_and_its_evidence(self):
        text = format_support_matrix(support_matrix())

        assert "forge support matrix" in text
        assert "evidence ladder: unsupported → declared_only → supported → tested" in text
        for status in ("tested", "declared_only", "supported"):
            assert status in text
        assert "evidence:" in text


class TestDoctorExposesTheMatrix:
    def test_the_standalone_mode_prints_and_exits_on_problems(self, monkeypatch, capsys, tmp_path):
        from forge.doctor import _support_matrix_mode

        monkeypatch.setattr(
            "forge.adaptive.support_matrix.support_matrix",
            lambda: support_matrix(
                manifest_rows=[],
                registrations=[_registration(evidence="docs/gone.json")],
                driver_sdk={"claude-code": "claude-sdk"},
                shipped={"claude-code"},
                evidence_root=tmp_path,
            ),
        )
        assert _support_matrix_mode(as_json=False) == 1
        out = capsys.readouterr().out
        assert "forge support matrix" in out
        assert "PROBLEM" in out

    def test_the_standalone_mode_json_and_clean_exit(self, monkeypatch, capsys, tmp_path):
        import json

        from forge.doctor import _support_matrix_mode

        evidence = _write_evidence(tmp_path, "docs/ok.json")
        monkeypatch.setattr(
            "forge.adaptive.support_matrix.support_matrix",
            lambda: support_matrix(
                manifest_rows=[],
                registrations=[_registration(evidence=evidence)],
                driver_sdk={"claude-code": "claude-sdk"},
                shipped={"claude-code"},
                evidence_root=tmp_path,
            ),
        )
        assert _support_matrix_mode(as_json=True) == 0
        document = json.loads(capsys.readouterr().out)
        assert document["rows"][0]["status"] == TESTED
        assert document["problems"] == []

    def test_capabilities_json_carries_the_additive_field(self, capsys):
        import json

        from forge.doctor import _capabilities_mode

        assert _capabilities_mode(as_json=True) == 0
        document = json.loads(capsys.readouterr().out)
        assert "support_matrix" in document
        assert document["support_matrix"]["rows"]
