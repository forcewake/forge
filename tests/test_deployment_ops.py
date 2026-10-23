"""Credential resolution is scoped, immutable and secret-free in evidence."""

import json
import pytest

from forge.adaptive.credential_broker import EnvBroker, BrokerCredentialRefusal
from tests.test_credential_broker import SUBJECT, OTHER_SUBJECT, ENV_REF, _bound_registry


async def test_rotated_environment_does_not_rewrite_already_staged_generation():
    broker = EnvBroker(environ={"ANTHROPIC_AUTH_TOKEN": "first-private-value"})
    staged = await broker.resolve(ENV_REF, grant={"run_id": "run-1"})
    other = await EnvBroker(environ={"ANTHROPIC_AUTH_TOKEN": "second-private-value"}).resolve(
        ENV_REF
    )
    assert staged.staged_env == {"ANTHROPIC_AUTH_TOKEN": "first-private-value"}
    assert other.staged_env != staged.staged_env
    evidence = json.dumps(staged.receipt)
    assert "first-private-value" not in evidence
    assert "second-private-value" not in evidence
    assert "ANTHROPIC_AUTH_TOKEN" in evidence


async def test_absent_or_foreign_reference_has_no_ambient_fallback():
    with pytest.raises(BrokerCredentialRefusal, match="env_absent"):
        await EnvBroker(environ={}).resolve(ENV_REF)
    with pytest.raises(BrokerCredentialRefusal, match="unknown_ref_scheme"):
        await EnvBroker(environ={"ANTHROPIC_AUTH_TOKEN": "unrelated"}).resolve("vault:foreign")


def test_project_credential_binding_is_connection_scoped():
    registry = _bound_registry()
    assert registry.subject_is_bound(SUBJECT)
    assert registry.subject_is_bound(OTHER_SUBJECT) is False
