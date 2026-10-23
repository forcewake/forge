"""Operational identity is bound by the actual frozen execution specification.

Historical deployment-profile scoring and percentiles retire. A current
specification still binds its exact approved input, model, credential route,
checkpoint and composition, rather than accepting a vaguely matching profile.
"""

from dataclasses import replace
import pytest

from forge.adaptive.execution_spec import (
    CompositionRefusal,
    read_execution_spec,
    render_template_variables,
)
from tests.test_execution_spec import _spec


def test_frozen_model_and_authority_are_rendered_without_ambient_overrides(monkeypatch):
    spec = _spec(approved_input_digest="c" * 64)
    monkeypatch.setenv("FORGE_MODEL", "unapproved-model")
    monkeypatch.setenv("FORGE_CREDENTIAL_REF", "ambient-ref")
    values = render_template_variables(spec)
    assert values["FORGE_MODEL"] == spec.model
    assert values["FORGE_EXECUTION_SPEC_DIGEST"] == spec.spec_digest()
    assert read_execution_spec(spec.to_document()) == spec


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "different-model"),
        ("profile_digest", "c" * 64),
        ("approved_input_digest", "d" * 64),
        ("credential_ref", "different-reference"),
    ],
)
def test_any_changed_authority_axis_changes_the_spec_identity(field, value):
    spec = _spec(approved_input_digest="b" * 64)
    changed = replace(spec, **{field: value})
    assert changed.spec_digest() != spec.spec_digest()
    assert spec.to_document()[field] != changed.to_document()[field]


def test_required_resume_names_an_exact_content_address_not_a_mutable_label():
    spec = _spec(resume_mode="required", continuation_ref="c" * 64)
    assert read_execution_spec(spec.to_document()).continuation_ref == "c" * 64
    with pytest.raises(CompositionRefusal):
        _spec(resume_mode="required", continuation_ref="latest")


def test_unknown_persisted_composition_or_schema_refuses_honestly():
    document = _spec().to_document()
    document["schema_version"] = "forge.execution-spec/999"
    with pytest.raises(CompositionRefusal):
        read_execution_spec(document)
