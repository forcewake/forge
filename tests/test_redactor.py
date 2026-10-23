"""Configured bounded evidence redaction; no claim of detecting every secret."""

import pytest
from forge.config import Settings
from forge.policy.evidence import EvidencePolicy


@pytest.mark.parametrize("prefix", ["glpat-", "ghs_", "sk-", "xai-"])
def test_configured_default_secret_prefixes_are_redacted(prefix):
    secret = prefix + "fixture-only-secret"
    value, changed = EvidencePolicy().apply_policy("failure " + secret)
    assert changed and secret not in value and "[REDACTED:rule]" in value


def test_deployment_deny_patterns_and_bounds_are_preserved():
    settings = Settings(
        _env_file=None,
        FORGE_GITLAB_ENABLED=False,
        FORGE_EVIDENCE_DENY_PATTERNS="vault-, customer-",
        FORGE_EVIDENCE_MAX_CHARS=64,
    )
    policy = EvidencePolicy.from_settings(settings)
    value, changed = policy.apply_policy("vault-credential customer-key " + "x" * 100)
    assert changed and len(value) == 64
    assert "credential" not in value and "customer-key" not in value


def test_redaction_is_idempotent():
    policy = EvidencePolicy()
    first, _ = policy.apply_policy("glpat-fixture-key")
    second, changed = policy.apply_policy(first)
    assert first == second and not changed


def test_clean_context_is_unchanged_inside_the_bound():
    assert EvidencePolicy().apply_policy("ordinary compiler error") == (
        "ordinary compiler error",
        False,
    )


def test_truncation_alone_does_not_claim_a_redacted_secret():
    value, changed = EvidencePolicy(max_chars=20).apply_policy("a" * 200)
    assert value == "a" * 20 and not changed
