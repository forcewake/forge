"""Single-repository authority retained from the deferred work-package coordinator.

Multiwriter phase scheduling and aggregated tested-world/applicability digests
are not 1.0 runtime features. PORTS-workpackage.csv records every original
lesson and its current proof or explicit mechanism retirement.
"""

import json

from forge_protocol import RunIdentity
from pydantic import ValidationError
import pytest

from forge.domain.approved_input import ApprovedInput
from forge.domain.plan import Citation, PlanStep
from forge.domain.run_spec import RunSpec2, VerificationContract
from forge.runtime.settings import Neighbor
from tests.test_domain_approved_input import citation, spec


@pytest.mark.parametrize(
    "field",
    (
        "work_package",
        "package_id",
        "items",
        "phases",
        "execution_dag",
        "candidate_set",
        "candidate_set_id",
        "tested_world_digest",
        "applicability_digest",
        "baseline_members",
        "publication_saga",
        "coordinator_state",
    ),
)
def test_deferred_package_fields_do_not_create_execution_authority(field):
    document = spec().model_dump(mode="json")
    document[field] = {"repository_id": "another", "writable": True, "verified": True}
    with pytest.raises(ValidationError):
        RunSpec2.model_validate_json(json.dumps(document))


@pytest.mark.parametrize("field", ("repository_id", "writable", "depends_on", "phase", "children"))
def test_a_plan_step_cannot_assign_another_writer_or_child(field):
    step = spec().plan.steps[0].model_dump(mode="json")
    step[field] = "unapproved authority"
    with pytest.raises(ValidationError):
        PlanStep.model_validate_json(json.dumps(step))


@pytest.mark.parametrize("field", ("writable", "write_policy", "mode", "children"))
def test_configured_neighbors_have_no_write_or_child_execution_fields(field):
    document = {"repository": "reference", "allowed_globs": ["src/**"], field: True}
    with pytest.raises(ValidationError):
        Neighbor.model_validate(document)
    reader = Neighbor(repository="reference", allowed_globs=("src/**",))
    assert reader.model_dump() == {"repository": "reference", "allowed_globs": ("src/**",)}


@pytest.mark.parametrize("field", ("writable", "write_policy", "execution_role"))
def test_a_reference_citation_is_not_an_execution_assignment(field):
    document = citation().model_dump(mode="json")
    document[field] = "writer"
    with pytest.raises(ValidationError):
        Citation.model_validate_json(json.dumps(document))


@pytest.mark.parametrize("field,value", (("connection_id", "other-host"), ("repository_id", "999")))
def test_reference_native_identity_changes_the_frozen_approval(field, value):
    frozen = spec()
    document = frozen.model_dump(mode="json")
    document["plan"]["steps"][0]["citations"][0]["repository"][field] = value
    changed = RunSpec2.model_validate_json(json.dumps(document))
    assert changed.plan.digest != frozen.plan.digest
    assert (
        ApprovedInput.from_spec(changed).approved_digest
        != ApprovedInput.from_spec(frozen).approved_digest
    )


@pytest.mark.parametrize("mutation", ("base", "revision", "checks", "reference"))
def test_changed_source_or_policy_cannot_reuse_an_earlier_approval(mutation):
    frozen = spec()
    approved = ApprovedInput.from_spec(frozen)
    document = frozen.model_dump(mode="json")
    if mutation == "base":
        document["base_sha"] = "b" * 40
    elif mutation == "revision":
        document["revision"] = 2
    elif mutation == "checks":
        document["verification"] = VerificationContract(required_checks=("other",)).model_dump()
    else:
        document["plan"]["steps"][0]["citations"][0]["source_sha"] = "c" * 40
    changed = RunSpec2.model_validate_json(json.dumps(document))
    assert changed.digest != frozen.digest
    with pytest.raises(ValueError, match="does not match"):
        approved.to_brief(RunIdentity(forge_run_id="current-run", attempt=1, generation=1), changed)


@pytest.mark.parametrize("field", ("children", "siblings", "package_id", "world_digest"))
def test_lane_identity_cannot_authorize_a_package_of_other_runs(field):
    document = {"forge_run_id": "current-run", "attempt": 1, "generation": 1, field: ["other"]}
    with pytest.raises(ValidationError):
        RunIdentity.model_validate(document)


def test_reference_arrays_are_frozen_before_the_approved_bytes_are_used():
    document = spec().model_dump(mode="json")
    frozen = RunSpec2.model_validate_json(json.dumps(document))
    approved = ApprovedInput.from_spec(frozen)
    document["plan"]["steps"][0]["citations"][0]["source_sha"] = "f" * 40
    document["plan"]["steps"][0]["files"].append("unapproved.py")
    brief = approved.to_brief(
        RunIdentity(forge_run_id="current-run", attempt=1, generation=1), frozen
    )
    assert brief.spec_json == frozen.spec_json
    assert "unapproved.py" not in brief.spec_json
    assert brief.approved_digest == approved.approved_digest
    assert isinstance(frozen.plan.steps, tuple)
    assert isinstance(frozen.plan.steps[0].files, tuple)
    assert isinstance(frozen.plan.steps[0].citations, tuple)
