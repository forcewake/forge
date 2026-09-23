"""NEXT-26/R32-12 — the support matrix from EXECUTED profile evidence.

The review's refusal, pinned as tests: "a batch Claude run cannot
promote Copilot steering support" (NEXT-26) and "the matrix dimensions
are misnamed — provider was the MODEL route, recipe was the credential
mode" (R32-12). Support is stated per
``(driver, source_platform, runtime_recipe)`` with the evidence class
each status name rests on, and the matrix NEVER claims more than the
evidence:

- ``tested`` requires a live registration mapped to the driver whose
  evidence artifact EXISTS in the checkout AND the source platform's
  gateway contract tests — for the runtime recipe the wiring pins;
- ``supported`` requires the capability manifest to carry the driver at
  a wired tier WITH a wired entry point and an entry-point test pointer,
  on a contract-tested platform;
- ``declared_only`` is a shipped recipe with no test-class evidence;
- ``unsupported`` is anything the inputs name that nothing declares.

R32-12's own acceptance pair: a (claude-sdk-lane, github, python-3-13)
row is ``tested``; a (copilot-sdk-lane, gitlab, dotnet-9) row is
``declared_only`` unless registered — the model route and the credential
mode moved into the row NOTE, and a registration still cannot promote a
cell whose recipe the wiring does not pin.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.adaptive.drivers.live_registrations import LiveRegistration
from forge.adaptive.support_matrix import (
    DECLARED_ONLY,
    SOURCE_PLATFORMS,
    STATUS_VALUES,
    SUPPORTED,
    TESTED,
    UNSUPPORTED,
    SourcePlatform,
    SupportRow,
    format_support_matrix,
    support_matrix,
)
from forge.capability_manifest import Capability

# ---------------------------------------------------------------------------
# Parameterized fixtures: injectable evidence, tmp-path artifacts
# ---------------------------------------------------------------------------


def _registration(
    *,
    sdk: str = "claude-sdk",
    provider: str = "zai-anthropic-gateway",
    evidence: str,
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


def _platforms(root: Path) -> dict[str, SourcePlatform]:
    """The three gateway platforms with their contract tests materialized
    under *root* — the platform axis, injectable without the repo tree."""
    return {
        name: SourcePlatform(
            platform=wiring.platform,
            gateway=wiring.gateway,
            evidence=tuple(_write_evidence(root, pointer) for pointer in wiring.evidence),
        )
        for name, wiring in SOURCE_PLATFORMS.items()
    }


class TestTheEvidenceRungs:
    def test_a_tested_driver_shows_tested_per_platform_cell(self, tmp_path):
        evidence = _write_evidence(tmp_path, "docs/evaluation/x/claude-live.json")
        matrix = support_matrix(
            manifest_rows=[],
            registrations=[_registration(evidence=evidence)],
            driver_sdk={"claude-code": "claude-sdk"},
            shipped={"claude-code"},
            source_platforms=_platforms(tmp_path),
            evidence_root=tmp_path,
        )

        rows = matrix.of_driver("claude-code")
        assert len(rows) == 3  # one cell per gateway platform
        for row in rows:
            assert row.status == TESTED
            assert row.source_platform in ("gitlab", "github", "azure")
            # The recipe axis is the EXECUTION PROFILE recipe the wiring
            # pins — never the registration's credential mode.
            assert row.runtime_recipe == (
                "node-22" if row.source_platform == "gitlab" else "python-3-13"
            )
            assert any(p.startswith("tests/") for p in row.evidence)
            assert "live smoke 2026-09-23" in row.note
            # The model route + credential mode stay DIAGNOSTICS in the note.
            assert "zai-anthropic-gateway" in row.note
            assert "byok-env-token" in row.note
        assert matrix.problems == ()

    def test_a_declared_only_recipe_shows_declared_only(self, tmp_path):
        matrix = support_matrix(
            manifest_rows=[],
            registrations=[],
            driver_sdk={},
            shipped={"dotnet-lane"},
            source_platforms=_platforms(tmp_path),
            evidence_root=tmp_path,
        )

        (row,) = matrix.of_driver("dotnet-lane")  # gitlab-only wiring
        assert row.status == DECLARED_ONLY
        assert row.source_platform == "gitlab"
        assert row.runtime_recipe == "dotnet-9"  # the digest-pinned image
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
            source_platforms=_platforms(tmp_path),
            evidence_root=tmp_path,
        )

        rows = matrix.of_driver("grok-build")
        assert len(rows) == 3
        for row in rows:
            assert row.status == SUPPORTED
            assert "wired entry point" in row.note
            assert "no recorded real-provider run" in row.note

    def test_a_manifest_row_without_an_entry_point_stays_declared_only(self, tmp_path):
        """A wired tier whose row carries tests/ but NO production entry
        point has not earned the supported rung — nothing wires it."""
        matrix = support_matrix(
            manifest_rows=[
                Capability(
                    name="harness/batch-copilot",
                    tier="production_wiring",
                    entry_point=None,
                    evidence=("tests/test_harness_selection.py",),
                )
            ],
            registrations=[],
            driver_sdk={},
            shipped={"copilot"},
            source_platforms=_platforms(tmp_path),
            evidence_root=tmp_path,
        )

        for row in matrix.of_driver("copilot"):
            assert row.status == DECLARED_ONLY

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
            source_platforms=_platforms(tmp_path),
            evidence_root=tmp_path,
        )

        for row in matrix.of_driver("copilot"):
            assert row.status == DECLARED_ONLY

    def test_an_undeclared_driver_is_unsupported(self, tmp_path):
        matrix = support_matrix(
            manifest_rows=[],
            registrations=[],
            driver_sdk={"neo-lane": "claude-sdk"},  # named, but nothing declares it
            shipped=set(),
            source_platforms=_platforms(tmp_path),
            evidence_root=tmp_path,
        )

        for row in matrix.of_driver("neo-lane"):
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
            source_platforms=_platforms(tmp_path),
            evidence_root=tmp_path,
        )

        rows = matrix.of_driver("claude-code")
        assert rows
        for row in rows:
            assert row.status == DECLARED_ONLY  # declared (shipped), unproven
        assert any(
            "not in this checkout" in problem and "NOT tested" in problem
            for problem in matrix.problems
        )

    def test_a_platform_with_missing_gateway_tests_cannot_be_tested(self, tmp_path):
        """R32-12's platform leg: a registration cannot promote a cell on
        a platform whose gateway contract tests are absent from the
        checkout — the row degrades to the manifest rung, loudly."""
        evidence = _write_evidence(tmp_path, "docs/evaluation/x/claude-live.json")
        platforms = _platforms(tmp_path)
        bare = dict(platforms)
        bare["azure"] = SourcePlatform(  # the gateway wiring exists...
            platform="azure", gateway="forge.gateway.azure_webhook"
        )
        (tmp_path / "tests/test_azure_webhook.py").unlink()  # ...its proof is gone

        matrix = support_matrix(
            manifest_rows=[],
            registrations=[_registration(evidence=evidence)],
            driver_sdk={"claude-code": "claude-sdk"},
            shipped={"claude-code"},
            source_platforms=bare,
            evidence_root=tmp_path,
        )

        by_platform = {row.source_platform: row.status for row in matrix.of_driver("claude-code")}
        assert by_platform["gitlab"] == TESTED
        assert by_platform["github"] == TESTED
        assert by_platform["azure"] != TESTED  # no platform evidence, no badge

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
            source_platforms=_platforms(tmp_path),
            evidence_root=tmp_path,
        )

        assert all(row.status == TESTED for row in matrix.of_driver("claude-code"))
        assert all(row.status == DECLARED_ONLY for row in matrix.of_driver("copilot"))

    def test_a_registration_never_certifies_a_recipe_the_wiring_does_not_pin(self, tmp_path):
        """The R32-12 acceptance: "a Claude model-route smoke does not
        certify ... a .NET runtime recipe". With the claude registration
        in place, a dotnet-9 cell for a python-wired driver does not
        exist in the matrix at all — asking for it answers None, never
        tested."""
        evidence = _write_evidence(tmp_path, "docs/evaluation/x/claude-live.json")
        matrix = support_matrix(
            manifest_rows=[],
            registrations=[_registration(evidence=evidence)],
            driver_sdk={"claude-sdk-lane": "claude-sdk"},
            shipped={"claude-sdk-lane"},
            source_platforms=_platforms(tmp_path),
            evidence_root=tmp_path,
        )

        assert matrix.of_cell("claude-sdk-lane", "github", "python-3-13").status == TESTED
        assert matrix.of_cell("claude-sdk-lane", "github", "dotnet-9") is None
        assert matrix.of_cell("claude-sdk-lane", "gitlab", "dotnet-9") is None

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
            source_platforms=_platforms(tmp_path),
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
            source_platforms=_platforms(tmp_path),
            evidence_root=tmp_path,
        )

        for row in matrix.of_driver("neo-sdk-lane"):
            assert row.status == DECLARED_ONLY

    def test_the_status_vocabulary_is_closed(self):
        with pytest.raises(ValueError):
            SupportRow(driver="d", source_platform="gitlab", runtime_recipe="r", status="certified")


class TestTheR32DimensionNaming:
    """R32-12's exact acceptance pair, plus the vocabulary checks."""

    def test_a_claude_sdk_lane_github_python_cell_is_tested(self, tmp_path):
        """The named cell: (claude-sdk-lane, github, python-3-13) — the
        registration proves the binary, the github gateway tests prove
        the platform, and python-3-13 is the recipe the Actions wiring
        pins for the SDK lanes."""
        evidence = _write_evidence(tmp_path, "docs/evaluation/x/claude-live.json")
        matrix = support_matrix(
            manifest_rows=[],
            registrations=[_registration(evidence=evidence)],
            driver_sdk={"claude-sdk-lane": "claude-sdk"},
            shipped={"claude-sdk-lane"},
            source_platforms=_platforms(tmp_path),
            evidence_root=tmp_path,
        )

        row = matrix.of_cell("claude-sdk-lane", "github", "python-3-13")
        assert row is not None
        assert row.status == TESTED

    def test_a_copilot_gitlab_dotnet_cell_is_declared_only_until_registered(self, tmp_path):
        """The other named cell: (copilot-sdk-lane, gitlab, dotnet-9) —
        without a registration the combination is declared intent only;
        WITH a covering registration (and the cell pinned as the wired
        recipe) it is tested. The credential-mode/model-route columns of
        the old matrix appear nowhere as dimensions."""
        matrix = support_matrix(
            manifest_rows=[],
            registrations=[],
            driver_sdk={"copilot-sdk-lane": "claude-sdk"},
            shipped={"copilot-sdk-lane"},
            source_platforms=_platforms(tmp_path),
            driver_recipes={("copilot-sdk-lane", "gitlab"): "dotnet-9"},
            evidence_root=tmp_path,
        )

        row = matrix.of_cell("copilot-sdk-lane", "gitlab", "dotnet-9")
        assert row is not None
        assert row.status == DECLARED_ONLY

        evidence = _write_evidence(tmp_path, "docs/evaluation/x/copilot-live.json")
        registered = support_matrix(
            manifest_rows=[],
            registrations=[
                LiveRegistration(
                    sdk="claude-sdk",
                    provider_route="github-copilot",
                    credential_mode="pat-env-token",
                    evidence=evidence,
                    verified_against=("copilot 1.0.0",),
                    date="2026-09-23",
                )
            ],
            driver_sdk={"copilot-sdk-lane": "claude-sdk"},
            shipped={"copilot-sdk-lane"},
            source_platforms=_platforms(tmp_path),
            driver_recipes={("copilot-sdk-lane", "gitlab"): "dotnet-9"},
            evidence_root=tmp_path,
        )

        row = registered.of_cell("copilot-sdk-lane", "gitlab", "dotnet-9")
        assert row is not None
        assert row.status == TESTED
        assert row.runtime_recipe == "dotnet-9"  # a recipe, not a credential mode
        assert "github-copilot" in row.note  # the model route is a note now

    def test_the_recipe_axis_comes_from_the_execution_profile(self):
        """Every emitted row's recipe is in the execution profile's own
        vocabulary — the axis IS the RuntimeRecipe set, wired per cell."""
        from forge.runs.execution_profile import RUNTIME_RECIPES

        matrix = support_matrix()
        assert matrix.rows
        for row in matrix.rows:
            assert row.runtime_recipe in RUNTIME_RECIPES, row

    def test_the_platform_axis_is_the_gateway_vocabulary(self):
        matrix = support_matrix()
        assert {row.source_platform for row in matrix.rows} <= set(SOURCE_PLATFORMS)
        assert {row.source_platform for row in matrix.rows} == {"gitlab", "github", "azure"}


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
        rows = matrix.of_driver("copilot-sdk-lane")
        assert rows
        for row in rows:
            assert row.status in (DECLARED_ONLY, SUPPORTED)
            assert row.status == DECLARED_ONLY  # no manifest row, no registration

    def test_the_dotnet_lane_is_a_gitlab_only_dotnet_nine_cell(self):
        """The recipe axis done right: the .NET lane exists ONLY as the
        (gitlab, dotnet-9) cell — no Actions or Azure python row exists
        for it to silently inherit."""
        matrix = support_matrix()
        rows = matrix.of_driver("dotnet-lane")

        (row,) = rows
        assert row.source_platform == "gitlab"
        assert row.runtime_recipe == "dotnet-9"
        assert matrix.of_cell("dotnet-lane", "github", "python-3-13") is None
        assert matrix.of_cell("dotnet-lane", "azure", "python-3-13") is None

    def test_to_json_is_the_doctor_field_shape(self):
        document = support_matrix().to_json()

        assert set(document) == {"legend", "rows", "problems"}
        assert set(document["legend"]) == set(STATUS_VALUES)
        for row in document["rows"]:
            assert set(row) == {
                "driver",
                "source_platform",
                "runtime_recipe",
                "status",
                "evidence",
                "note",
            }

    def test_format_renders_every_row_and_its_evidence(self):
        text = format_support_matrix(support_matrix())

        assert "forge support matrix" in text
        assert "(driver, source_platform, runtime_recipe)" in text
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
                source_platforms=_platforms(tmp_path),
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
                source_platforms=_platforms(tmp_path),
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
