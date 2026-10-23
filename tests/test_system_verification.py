"""Frozen verification evidence on the actual per-run binding owner.

Multi-service twins are deferred. The shipped verifier still binds the
exact candidate, base, tested commit, revision and environment.
"""

from dataclasses import FrozenInstanceError, replace
import pytest
from forge.adaptive.verification_binding import (
    BoundEvidence,
    GenerationBinding,
    VerificationSubject,
    invalidate_for_environment_change,
    subject_freshness,
)
from forge.runs.verification import evaluate_positive_proof


def subject():
    return VerificationSubject(
        candidate_digest="1" * 64,
        source_oid="a" * 40,
        tested_oid="a" * 40,
        source_base_oid="b" * 40,
        plan_revision_digest="2" * 64,
        environment_profile_digest="3" * 64,
        generation=GenerationBinding(
            work_id="run-1", checkpoint_id="4" * 64, checkpoint_binding="exact", source="generation"
        ),
    )


@pytest.mark.parametrize(
    "field",
    [
        "candidate_digest",
        "source_oid",
        "tested_oid",
        "source_base_oid",
        "plan_revision_digest",
        "environment_profile_digest",
    ],
)
def test_every_changed_runtime_input_invalidates_the_frozen_verdict(field):
    original = subject()
    moved = replace(original, **{field: "9" * len(getattr(original, field))})
    assert moved.subject_digest != original.subject_digest
    verdict = subject_freshness({"subject_identity": original.as_document()}, current_subject=moved)
    assert verdict.status == "stale"
    assert field in verdict.reason


def test_generation_identity_changes_the_canonical_frozen_subject():
    original = subject()
    moved = replace(original, generation=replace(original.generation, checkpoint_id="9" * 64))
    assert moved.subject_digest != original.subject_digest


def test_unchanged_identity_round_trips_deterministically_and_is_immutable():
    original = subject()
    assert subject().subject_digest == original.subject_digest
    assert VerificationSubject.from_document(original.as_document()) == original
    with pytest.raises(FrozenInstanceError):
        original.source_oid = "9" * 40


@pytest.mark.parametrize(
    "field",
    [
        "candidate_digest",
        "source_oid",
        "tested_oid",
        "source_base_oid",
        "plan_revision_digest",
        "environment_profile_digest",
    ],
)
def test_tampered_serialized_identity_is_unknown_never_repaired_into_a_pass(field):
    original = subject()
    document = original.as_document()
    document[field] = "tampered"
    assert VerificationSubject.from_document(document) is None
    assert (
        subject_freshness({"subject_identity": document}, current_subject=original).status
        == "unknown"
    )


@pytest.mark.parametrize("document", [None, {}, {"schema": "future/99"}])
def test_missing_or_foreign_identity_cannot_claim_current_evidence(document):
    assert (
        subject_freshness({"subject_identity": document}, current_subject=subject()).status
        == "unknown"
    )


def test_environment_invalidation_retains_history_and_only_withdraws_matching_authority():
    records = (
        BoundEvidence("old", "s1", "old-env"),
        BoundEvidence("new", "s2", "new-env"),
        BoundEvidence("unknown", "s3", ""),
    )
    result = invalidate_for_environment_change(
        records, previous_digest="old-env", current_digest="new-env"
    )
    assert len(result) == len(records)
    assert result[0].invalidated and result[0].invalidated_reason
    assert result[1:] == records[1:]
    assert records[0].invalidated is False


@pytest.mark.parametrize("conclusion", [None, "skipped", "cancelled", "failure", "unknown"])
def test_a_required_check_must_have_run_successfully(conclusion):
    result = evaluate_positive_proof(
        ("required",),
        {"required": conclusion},
        success_conclusions=frozenset({"success"}),
        code_failure_conclusions=frozenset({"failure"}),
        infra_conclusions=frozenset({"cancelled"}),
    )
    assert result.verified is False
