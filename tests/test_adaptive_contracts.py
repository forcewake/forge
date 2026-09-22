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
from forge.adaptive.models import UNRESOLVED_IMAGE_DIGEST

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
    # injected tags. Fields the example PREDATES (later model additions
    # such as CandidateSet's persisted world fields) are compared only
    # when the example carries them — an addition may join, but it must
    # default cleanly and never disturb the example's own round trip.
    def _strip(d):
        if isinstance(d, dict):
            return {k: _strip(v) for k, v in d.items() if k != "schema"}
        if isinstance(d, (list, tuple)):
            # tuples (NXT-22 frozen members) and lists are the same
            # sequence as far as the example is concerned
            return [_strip(v) for v in d]
        return d

    assert _strip({k: v for k, v in dumped.items() if k in raw}) == _strip(raw)


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


class TestCandidateSetMemberOids:
    """NXT-22: member OIDs get the git-sha discipline Snapshot always had."""

    def test_a_non_sha_candidate_oid_is_refused(self):
        base = _load("candidate-set.example.json")
        base["members"][0]["candidate_oid"] = "not-an-oid"
        with pytest.raises(ValidationError, match="oid must be"):
            CandidateSet.model_validate(base)

    def test_a_non_sha_base_oid_is_refused(self):
        base = _load("candidate-set.example.json")
        base["members"][0]["base_oid"] = "xyz"
        with pytest.raises(ValidationError, match="oid must be"):
            CandidateSet.model_validate(base)

    def test_uppercase_oids_are_refused(self):
        # git shas are lowercase hex; UPPER is a different (wrong) spelling
        base = _load("candidate-set.example.json")
        base["members"][0]["candidate_oid"] = "A" * 40
        with pytest.raises(ValidationError, match="oid must be a lowercase"):
            CandidateSet.model_validate(base)

    def test_a_wrong_length_oid_is_refused(self):
        base = _load("candidate-set.example.json")
        base["members"][0]["candidate_oid"] = "1" * 39
        with pytest.raises(ValidationError, match="oid must be"):
            CandidateSet.model_validate(base)


class TestCandidateSetImageDigest:
    """NXT-22: an image reference must be an EXACT artifact, never a tag."""

    @pytest.mark.parametrize(
        "bad",
        [
            "latest",  # a bare mutable tag
            "nginx",  # a bare name
            "nginx:latest",  # a tagged reference
            "nginx:1.21",  # a version tag is still a tag, not hex
            "sha256:" + "E" * 64,  # uppercase hex
            "sha256:" + "e" * 63,  # wrong length for the algorithm
            "md5:" + "d" * 32,  # unsupported algorithm
            "",  # nothing at all
        ],
    )
    def test_mutable_or_malformed_references_are_refused(self, bad):
        base = _load("candidate-set.example.json")
        base["members"][0]["image_digest"] = bad
        # "" is caught by the min_length guard; everything else by the
        # exact-artifact validator — both refuse, with their own message.
        with pytest.raises(
            ValidationError, match="mutable tags and bare names|at least 1 character"
        ):
            CandidateSet.model_validate(base)

    def test_the_sha512_spelling_is_supported(self):
        base = _load("candidate-set.example.json")
        base["members"][0]["image_digest"] = "sha512:" + "f" * 128
        parsed = CandidateSet.model_validate(base)
        assert parsed.members[0].image_digest == "sha512:" + "f" * 128

    def test_the_explicit_unresolved_sentinel_is_accepted(self):
        # a member may ride along WITHOUT a recorded artifact yet — but
        # it must SAY so; the sentinel is a claim, never a fake digest.
        base = _load("candidate-set.example.json")
        base["members"][0]["image_digest"] = UNRESOLVED_IMAGE_DIGEST
        parsed = CandidateSet.model_validate(base)
        assert parsed.members[0].image_digest == UNRESOLVED_IMAGE_DIGEST


class TestCandidateSetFrozenMembers:
    """NXT-22: a frozen model must not hold a mutable members list."""

    def test_members_are_frozen_to_a_tuple(self):
        parsed = CandidateSet.model_validate(_load("candidate-set.example.json"))
        assert isinstance(parsed.members, tuple)
        with pytest.raises(AttributeError):
            parsed.members.append(parsed.members[0])  # type: ignore[attr-defined]

    def test_a_list_input_is_still_accepted_and_converted(self):
        base = _load("candidate-set.example.json")
        parsed = CandidateSet.model_validate(base)  # the example is a JSON list
        assert isinstance(parsed.members, tuple) and len(parsed.members) == 2

    def test_mutating_the_callers_list_after_construction_changes_nothing(self):
        base = _load("candidate-set.example.json")
        members = CandidateSet.model_validate(base).members
        caller_list = list(members)
        parsed = CandidateSet(
            work_id=base["work_id"],
            plan_revision=base["plan_revision"],
            work_contract_digest=base["work_contract_digest"],
            members=caller_list,
        )
        caller_list.append(caller_list[0])  # the caller mutates ITS list
        assert len(parsed.members) == 2  # the stored set never notices

    def test_serialization_is_stable_across_round_trips(self):
        base = _load("candidate-set.example.json")
        parsed = CandidateSet.model_validate(base)
        first = parsed.model_dump()
        again = CandidateSet.model_validate(first).model_dump()
        assert first == again


class TestCandidateSetPersistedWorld:
    """NXT-22: the freeze-time world fields on CandidateSet itself."""

    def _base(self) -> dict:
        return _load("candidate-set.example.json")

    def test_persisted_digests_must_be_64_hex(self):
        base = self._base()
        base["tested_world_digest"] = "not-hex"
        base["applicability_digest"] = "also-not-hex"
        with pytest.raises(ValidationError, match="sha256"):
            CandidateSet.model_validate(base)

    def test_the_world_freeze_is_all_or_nothing(self):
        # half a binding is a set that claims a recorded world it cannot
        # reconstruct — refused.
        base = self._base()
        base["tested_world_digest"] = "f" * 64
        with pytest.raises(ValidationError, match="come as a pair"):
            CandidateSet.model_validate(base)

    def test_pins_must_be_exact_artifacts_not_tags(self):
        base = self._base()
        base["environment_pins"] = {"postgres": "postgres:latest"}
        with pytest.raises(ValidationError, match="mutable tags and bare names"):
            CandidateSet.model_validate(base)

    def test_the_unresolved_sentinel_is_not_an_exact_pin(self):
        # a pin IS a resolution — "pinned to unresolved" is a contradiction
        base = self._base()
        base["environment_pins"] = {"postgres": UNRESOLVED_IMAGE_DIGEST}
        with pytest.raises(ValidationError, match="not an exact pin"):
            CandidateSet.model_validate(base)

    def test_pins_accept_a_mapping_and_normalize_to_sorted_pairs(self):
        base = self._base()
        base["environment_pins"] = {
            "zookeeper": "sha256:" + "d" * 64,
            "postgres": "sha256:" + "e" * 64,
        }
        parsed = CandidateSet.model_validate(base)
        assert parsed.environment_pins == (
            ("postgres", "sha256:" + "e" * 64),
            ("zookeeper", "sha256:" + "d" * 64),
        )

    def test_a_service_pinned_twice_is_refused(self):
        base = self._base()
        base["environment_pins"] = [
            ["postgres", "sha256:" + "e" * 64],
            ["postgres", "sha256:" + "d" * 64],
        ]
        with pytest.raises(ValidationError, match="pinned twice"):
            CandidateSet.model_validate(base)

    def test_policy_refs_are_sorted_and_deduplicated(self):
        base = self._base()
        base["policy_refs"] = ["compat/b@1", "compat/a@1", "compat/a@1"]
        parsed = CandidateSet.model_validate(base)
        assert parsed.policy_refs == ("compat/a@1", "compat/b@1")

    def test_a_blank_policy_ref_is_refused(self):
        base = self._base()
        base["policy_refs"] = ["compat/a@1", "  "]
        with pytest.raises(ValidationError, match="non-blank"):
            CandidateSet.model_validate(base)

    def test_the_persisted_fields_survive_a_round_trip(self):
        base = self._base()
        base["environment_pins"] = {"postgres": "sha256:" + "e" * 64}
        base["policy_refs"] = ["compat/a@1"]
        base["tested_world_digest"] = "a" * 64
        base["applicability_digest"] = "b" * 64
        parsed = CandidateSet.model_validate(base)
        again = CandidateSet.model_validate(parsed.model_dump())
        assert again == parsed


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
