"""Protocol fixtures pin the actual lane/server boundary and its refusals."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from forge_protocol import Brief, CheckpointEntry, CheckpointManifest, canonical_json, digest
from forge_protocol.schema import WIRE_TYPES, export

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = json.loads((ROOT / "packages/forge-protocol/fixtures/v1.json").read_text())


@pytest.mark.parametrize("model", WIRE_TYPES, ids=lambda model: model.__name__)
def test_golden_roundtrip_and_unknown_field_refusal(model):
    document = FIXTURES[model.__name__]
    value = model.model_validate(document)
    assert json.loads(canonical_json(value)) == document
    assert model.model_validate_json(canonical_json(value)) == value
    with pytest.raises(ValidationError):
        model.model_validate({**document, "undeclared": True})
    with pytest.raises(ValidationError):
        model.model_validate({**document, "protocol": "forge.lane/2"})
    with pytest.raises(ValidationError):
        value.protocol = "forge.lane/1"


def test_schemas_are_the_committed_contract():
    assert export(ROOT / "packages/forge-protocol/schemas", check=True) == []


def test_approval_tampering_refuses_and_key_order_does_not_change_identity():
    document = FIXTURES["Brief"]
    assert digest(document) == digest(dict(reversed(tuple(document.items()))))
    for key in ("brief_text", "plan_digest", "spec_digest", "base_sha", "revision"):
        changed = {
            **document,
            key: document[key] + 1 if key == "revision" else "f" * len(document[key]),
        }
        with pytest.raises(ValidationError, match="digest mismatch"):
            Brief.model_validate(changed)


@pytest.mark.parametrize(
    "path", ["/tmp/x", "../x", "a/../x", "a\\x", ".git/config", ".forge/key", "C:/x", "a//x"]
)
def test_checkpoint_paths_cannot_escape_or_replace_repository_metadata(path):
    with pytest.raises(ValidationError):
        CheckpointEntry.model_validate({**FIXTURES["CheckpointEntry"], "path": path})


def test_checkpoint_identity_covers_content_and_refuses_duplicate_paths():
    document = FIXTURES["CheckpointManifest"]
    manifest = CheckpointManifest.model_validate(document)
    changed = CheckpointManifest.model_validate({**document, "sequence": manifest.sequence + 1})
    assert manifest.checkpoint_id != changed.checkpoint_id
    with pytest.raises(ValidationError, match="unique paths"):
        CheckpointManifest.model_validate({**document, "entries": document["entries"] * 2})
    with pytest.raises(ValidationError, match="no blob"):
        CheckpointEntry.model_validate({**FIXTURES["CheckpointEntry"], "state": "deleted"})
