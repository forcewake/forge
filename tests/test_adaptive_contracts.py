"""The adaptive contracts parse the review package's own examples.

The examples under docs/roadmap/2026-09-21-adaptive/contracts/ ARE the
executable specification — the review proposed them; these tests prove
the implementation accepts them verbatim and rejects the corruption
classes each schema guards.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from forge.adaptive import (
    CandidateSet,
    ChangeProposal,
    Checkpoint,
    ControlCommand,
    PlanRevision,
    SnapshotSet,
    WorkContract,
)

CONTRACTS = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "roadmap"
    / "2026-09-21-adaptive"
    / "contracts"
)


def _load(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("model", "example"),
    [
        (WorkContract, "work-contract.example.json"),
        (PlanRevision, "plan-revision.example.json"),
        (ControlCommand, "control-command.example.json"),
        (SnapshotSet, "snapshot-set.example.json"),
        (CandidateSet, "candidate-set.example.json"),
        (Checkpoint, "checkpoint.example.json"),
    ],
)
def test_the_review_examples_parse_verbatim(model, example):
    raw = _load(example)
    parsed = model.model_validate(raw)
    dumped = parsed.model_dump()

    # nested members gain their implicit schema tag on dump; compare
    # field-by-field over the example's own keys, recursively dropping the
    # injected tags.
    def _strip(d):
        if isinstance(d, dict):
            return {k: _strip(v) for k, v in d.items() if k != "schema"}
        if isinstance(d, list):
            return [_strip(v) for v in d]
        return d

    assert _strip(dumped) == _strip(raw)


class TestWorkContract:
    def test_write_scope_outside_read_scope_is_invalid(self):
        base = _load("work-contract.example.json")
        base["write_scope"][0]["repository_id"] = "not-in-read-scope"
        with pytest.raises(ValidationError, match="write scope outside read scope"):
            WorkContract.model_validate(base)

    def test_empty_read_scope_is_invalid(self):
        base = _load("work-contract.example.json")
        base["read_scope"] = []
        with pytest.raises(ValidationError):
            WorkContract.model_validate(base)

    def test_stray_fields_are_refused(self):
        base = _load("work-contract.example.json")
        base["surprise"] = True
        with pytest.raises(ValidationError):
            WorkContract.model_validate(base)


class TestPlanRevision:
    def test_unknown_step_dependency_is_invalid(self):
        base = _load("plan-revision.example.json")
        base["steps"][1]["depends_on"] = ["NOPE"]
        with pytest.raises(ValidationError, match="unknown steps"):
            PlanRevision.model_validate(base)

    def test_self_dependency_is_invalid(self):
        base = _load("plan-revision.example.json")
        base["steps"][0]["depends_on"] = ["S1"]
        with pytest.raises(ValidationError, match="depends on itself"):
            PlanRevision.model_validate(base)

    def test_parent_revision_must_precede(self):
        base = _load("plan-revision.example.json")
        base["parent_revision"] = 1
        with pytest.raises(ValidationError, match="parent_revision must precede"):
            PlanRevision.model_validate(base)

    def test_digest_shapes_are_enforced(self):
        base = _load("plan-revision.example.json")
        base["work_contract_digest"] = "not-hex"
        with pytest.raises(ValidationError, match="sha256"):
            PlanRevision.model_validate(base)


class TestControlCommand:
    def test_the_state_vocabulary_is_closed(self):
        base = _load("control-command.example.json")
        base["status"] = "teleported"
        with pytest.raises(ValidationError):
            ControlCommand.model_validate(base)

    def test_the_kind_vocabulary_is_closed(self):
        base = _load("control-command.example.json")
        base["kind"] = "delete-everything"
        with pytest.raises(ValidationError):
            ControlCommand.model_validate(base)


class TestSnapshotSet:
    def test_a_repository_appears_once(self):
        base = _load("snapshot-set.example.json")
        base["snapshots"].append(dict(base["snapshots"][0]))
        with pytest.raises(ValidationError, match="twice"):
            SnapshotSet.model_validate(base)

    def test_oids_are_git_shas(self):
        base = _load("snapshot-set.example.json")
        base["snapshots"][0]["source_oid"] = "xyz"
        with pytest.raises(ValidationError):
            SnapshotSet.model_validate(base)


class TestCandidateSet:
    def test_changed_and_baseline_roles_are_the_vocabulary(self):
        base = _load("candidate-set.example.json")
        base["members"][0]["role"] = "maybe"
        with pytest.raises(ValidationError):
            CandidateSet.model_validate(base)


class TestChangeProposal:
    def test_material_classification_is_the_gate_vocabulary(self):
        proposal = {
            "schema": "forge.proposal.change-proposal/1",
            "proposal_id": "cp-1",
            "work_id": "wp-demo-1",
            "from_revision": 1,
            "classification": "material_migration",
            "rationale": "A durable deduplication key needs a schema change.",
            "new_evidence": ["ev-1"],
        }
        parsed = ChangeProposal.model_validate(proposal)
        assert parsed.classification == "material_migration"

    def test_tactical_and_material_are_both_representable(self):
        base = {
            "schema": "forge.proposal.change-proposal/1",
            "proposal_id": "cp-2",
            "work_id": "wp-demo-1",
            "from_revision": 1,
            "rationale": "internal reordering",
        }
        for classification in ("tactical_internal", "material_scope", "material_contract"):
            ChangeProposal.model_validate({**base, "classification": classification})
        with pytest.raises(ValidationError):
            ChangeProposal.model_validate({**base, "classification": "cosmetic"})
