"""Q39-17 (issue #336): the closure matrix — ADR-0033 §4's honest table.

Three contracts under test:

- **the validation** — every cell's evidence path EXISTS under the
  repository (a phantom trace is a validation failure), every
  capability carries exactly the six levels, every class is in the
  vocabulary, and every pending cell names its gap;
- **the render** — each capability × level is visible with its class
  and path, and the published doc (docs/operations/closure-matrix.md)
  carries the rendered view in sync;
- **the honest gap** — a pending-human or pending level renders
  PENDING, never proven, and an issue closed in GitHub does not empty
  the pending set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.adaptive.closure_matrix import (
    CLOSURE_LEVELS,
    CLOSURE_MATRIX_STAMP,
    CapabilityClosure,
    ClosureMatrix,
    EVIDENCE_CLASSES,
    LevelEvidence,
    PENDING_EVIDENCE_CLASSES,
    PROVEN_EVIDENCE_CLASSES,
    closure_matrix,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "operations" / "closure-matrix.md"
ADR = REPO_ROOT / "docs" / "adr" / "0033-execution-ownership-consolidation.md"


class TestTheMatrixData:
    def test_the_six_decision_owners_are_the_capabilities(self) -> None:
        assert closure_matrix().capability_names() == (
            "approved-plan",
            "operation-grant",
            "active-attempt",
            "checkpoint",
            "verified-candidate",
            "usage-receipt",
        )

    def test_every_capability_carries_exactly_the_six_levels_in_order(self) -> None:
        for item in closure_matrix().capabilities:
            assert [cell.level for cell in item.evidence] == list(CLOSURE_LEVELS)
            assert item.decision and item.owner and item.consumer_contract

    def test_the_vocabulary_is_the_documented_one(self) -> None:
        assert CLOSURE_LEVELS == (
            "domain-contract",
            "wired-caller",
            "executed-process",
            "native-execution",
            "cross-process-recovery",
            "customer-acceptance",
        )
        assert EVIDENCE_CLASSES == (
            "unit-proven",
            "pe-proven",
            "live",
            "pending-human",
            "pending",
        )
        # The classes partition: proven vs pending — nothing renders both.
        assert PROVEN_EVIDENCE_CLASSES | PENDING_EVIDENCE_CLASSES == set(EVIDENCE_CLASSES)
        assert not PROVEN_EVIDENCE_CLASSES & PENDING_EVIDENCE_CLASSES


class TestMatrixValidation:
    def test_the_matrix_validates_against_the_repository(self) -> None:
        """Every evidence path exists — the matrix cites no phantom
        trace (this is the test that keeps the DATA honest as the tree
        moves; a renamed file is a finding, never a silent stale
        citation)."""
        assert closure_matrix().validate(REPO_ROOT) == []

    def test_every_cited_evidence_path_exists_on_disk(self) -> None:
        for item in closure_matrix().capabilities:
            for cell in item.evidence:
                assert (REPO_ROOT / cell.path).exists(), (
                    f"{item.capability}/{cell.level} cites {cell.path}"
                )

    def test_a_phantom_evidence_path_is_a_validation_failure(self) -> None:
        phantom = ClosureMatrix(
            capabilities=(
                CapabilityClosure(
                    capability="made-up",
                    decision="a fabricated capability",
                    owner="forge.nowhere",
                    consumer_contract="forge.nowhere/1",
                    evidence=tuple(
                        LevelEvidence(
                            level=level,
                            evidence_class="unit-proven",
                            path="tests/does_not_exist_anywhere.py",
                        )
                        for level in CLOSURE_LEVELS
                    ),
                ),
            )
        )
        findings = phantom.validate(REPO_ROOT)
        assert len(findings) == len(CLOSURE_LEVELS)
        assert all("phantom" in text for text in findings)

    def test_a_missing_level_is_a_validation_failure(self) -> None:
        short = ClosureMatrix(
            capabilities=(
                CapabilityClosure(
                    capability="short",
                    decision="d",
                    owner="forge.x",
                    consumer_contract="forge.x/1",
                    evidence=(LevelEvidence("domain-contract", "unit-proven", "tests"),),
                ),
            )
        )
        assert any("exactly" in text for text in short.validate(REPO_ROOT))

    def test_an_unknown_class_is_a_validation_failure(self) -> None:
        bogus = ClosureMatrix(
            capabilities=(
                CapabilityClosure(
                    capability="bogus",
                    decision="d",
                    owner="forge.x",
                    consumer_contract="forge.x/1",
                    evidence=tuple(
                        LevelEvidence(level, "vibes-proven", "tests") for level in CLOSURE_LEVELS
                    ),
                ),
            )
        )
        assert any("unknown evidence class" in text for text in bogus.validate(REPO_ROOT))

    def test_a_silent_gap_is_a_validation_failure(self) -> None:
        """A pending cell without a note fabricates closure."""
        silent = ClosureMatrix(
            capabilities=(
                CapabilityClosure(
                    capability="silent",
                    decision="d",
                    owner="forge.x",
                    consumer_contract="forge.x/1",
                    evidence=tuple(
                        LevelEvidence(level, "pending", "tests") for level in CLOSURE_LEVELS
                    ),
                ),
            )
        )
        findings = silent.validate(REPO_ROOT)
        assert len(findings) == len(CLOSURE_LEVELS)
        assert all("note" in text for text in findings)


class TestTheRender:
    def test_every_capability_and_level_is_visible(self) -> None:
        rendered = closure_matrix().render()
        assert CLOSURE_MATRIX_STAMP in rendered
        for item in closure_matrix().capabilities:
            assert item.capability in rendered
            assert item.owner in rendered
            for cell in item.evidence:
                assert cell.level in rendered
                assert cell.path in rendered

    def test_closure_of_renders_the_honest_per_capability_table(self) -> None:
        table = closure_matrix().closure_of("operation-grant").render()
        assert "operation-grant" in table
        assert "forge.credential.operation-grant/1" in table
        for level in CLOSURE_LEVELS:
            assert level in table
        assert table.count("| domain-contract") == 1

    def test_an_unknown_capability_refuses_with_the_known_names(self) -> None:
        with pytest.raises(KeyError, match="approved-plan"):
            closure_matrix().closure_of("vibes-driven-development")

    def test_the_published_doc_carries_the_rendered_view(self) -> None:
        """docs/operations/closure-matrix.md is the publication — a
        drift here means the doc claims a state the data left (or
        vice versa); regenerate the doc from render()."""
        text = DOC.read_text()
        assert closure_matrix().render() in text
        assert "[ADR-0033](../adr/0033-execution-ownership-consolidation.md)" in text


class TestTheHonestGap:
    """The issue's headline: an issue closed in GitHub is NOT every
    level proven — the matrix makes the gap visible instead of hiding
    it behind a closed ticket."""

    def test_a_pending_human_level_renders_pending_never_proven(self) -> None:
        cell = closure_matrix().closure_of("verified-candidate").level("customer-acceptance")
        assert cell.evidence_class == "pending-human"
        assert "PENDING" in cell.render()
        assert "proven" not in cell.render()

    def test_every_pending_level_renders_pending(self) -> None:
        for item in closure_matrix().capabilities:
            for cell in item.evidence:
                if cell.evidence_class in PENDING_EVIDENCE_CLASSES:
                    assert "PENDING" in cell.render()
                    assert "proven" not in cell.render()

    def test_no_capability_is_fully_closed_today(self) -> None:
        """The honest state: every capability carries at least one
        pending level — a fully-proven row would be the fabricated
        parity the review rejects. (When a row genuinely closes, tighten
        this test in the same change — that is the reviewed way.)"""
        for item in closure_matrix().capabilities:
            assert item.pending_levels(), (
                f"{item.capability} claims full closure — either the evidence "
                "landed (tighten this test in the same change) or the claim "
                "is fabricated"
            )

    def test_the_live_arm_records_its_failure_as_a_failure(self) -> None:
        """The live interrupt/resume arm stays INFORMATIONAL — the
        matrix carries the delivery failure, never a green wash."""
        cell = closure_matrix().closure_of("active-attempt").level("native-execution")
        assert cell.evidence_class == "live"
        assert "delivery FAILED" in cell.note

    def test_proven_and_pending_partition_the_levels(self) -> None:
        for item in closure_matrix().capabilities:
            assert sorted(item.proven_levels() + item.pending_levels()) == sorted(CLOSURE_LEVELS)


class TestADRAndPublication:
    def test_the_adr_carries_the_six_owner_map_and_the_ladder(self) -> None:
        text = ADR.read_text()
        for owner in (
            "forge.adaptive.revisions",
            "forge.adaptive.credential_broker",
            "forge.adaptive.continuation",
            "forge.adaptive.checkpoint_repository",
            "forge.adaptive.verification_binding",
            "forge.durable.budgets.ingest_usage_receipt",
        ):
            assert owner in text
        assert "runtime recipe" in text and "harness profile" in text
        assert "extraction ladder" in text.lower()
        assert "closure matrix" in text.lower()

    def test_the_adr_cross_links_the_publication(self) -> None:
        assert "docs/operations/closure-matrix.md" in ADR.read_text()
