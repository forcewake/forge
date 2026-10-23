"""Current operator redaction remains after optional audit export retires."""

import copy
import json
import pytest
from forge.adaptive.operator_redaction import redact


def test_recursive_operator_redaction_keeps_input_immutable():
    original = {
        "nested": [{"api_key": "private-value", "note": "Bearer ghp_aaaaaaaaaaaaaaaa"}],
        "safe": {"run_id": "run-1", "outcome": "unknown"},
    }
    before = copy.deepcopy(original)
    result = redact(original)
    assert original == before
    assert result["nested"][0] == {"api_key": "[redacted]", "note": "[redacted]"}
    assert result["safe"] == {"run_id": "run-1", "outcome": "unknown"}


@pytest.mark.parametrize(
    "value", ["sk-abcdefghijk", "ghp_abcdefghijk", "glpat-private", "Bearer private"]
)
def test_secret_values_are_redacted_even_inside_declared_credential_metadata(value):
    document = {
        "schema": "forge.credential.broker-receipt/1",
        "env_var": "ANTHROPIC_AUTH_TOKEN",
        "credential_ref": "env:ANTHROPIC_AUTH_TOKEN",
        "resolved_version": value,
    }
    result = redact(document)
    assert result["env_var"] == "ANTHROPIC_AUTH_TOKEN"
    assert result["credential_ref"] == "env:ANTHROPIC_AUTH_TOKEN"
    assert result["resolved_version"] == "[redacted]"
    assert value not in json.dumps(result)


def test_unknown_credential_shape_cannot_bypass_key_filtering():
    result = redact(
        {"credential": {"schema": "invented/1", "value": "private"}, "status": "unknown_outcome"}
    )
    assert result["credential"] == "[redacted]"
    assert result["status"] == "unknown_outcome"
