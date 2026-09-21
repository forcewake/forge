"""FND-04: versioned capability and credential profiles.

A driver name is not a promise. These tests pin the profile substrate:
the closed capability vocabulary, the broker-owned credential binding
(never a secret value), onboarding-time profile/binding compatibility
with NO credential fallback, role gating that keeps write-enabled
profiles out of discovery and verification, and the D06 absent-versus-
explicitly-empty manifest distinction.
"""

from __future__ import annotations

import dataclasses

import pytest

from forge.adaptive.capability_profiles import (
    CAPABILITIES,
    CapabilityProfile,
    CredentialBinding,
    manifest_status,
    role_allows,
    validate_profile_binding,
)


def interactive_profile(**overrides) -> CapabilityProfile:
    base = {
        "profile_id": "native-interactive-1",
        "capabilities": ("read_tools", "structured_output", "interrupt", "live_input"),
        "driver_digest": "a" * 64,
        "provider_route": "github",
        "credential_mode": "app",
    }
    return CapabilityProfile(**{**base, **overrides})


def checkpoint_only_cli_profile(**overrides) -> CapabilityProfile:
    # A CLI with checkpoint-only control — resumable, never interactive.
    base = {
        "profile_id": "cli-checkpoint-1",
        "capabilities": ("read_tools", "structured_output", "checkpoint_export", "usage_complete"),
    }
    return CapabilityProfile(**{**base, **overrides})


def binding(**overrides) -> CredentialBinding:
    base = {"credential_ref": "broker/cred/0192", "provider": "github", "mode": "app"}
    return CredentialBinding(**{**base, **overrides})


class TestCapabilityProfile:
    def test_default_schema_tag_is_versioned(self):
        profile = checkpoint_only_cli_profile()
        assert profile.schema == "forge.capability.profile/1"

    def test_a_wrong_schema_tag_is_refused(self):
        with pytest.raises(ValueError, match="schema must be"):
            CapabilityProfile(profile_id="p", schema="forge.capability.profile/2")

    def test_supports_answers_from_the_declared_capabilities(self):
        profile = checkpoint_only_cli_profile()
        assert profile.supports("read_tools") is True
        assert profile.supports("checkpoint_export") is True
        assert profile.supports("interrupt") is False
        assert profile.supports("live_input") is False

    def test_write_disabled_by_default(self):
        assert checkpoint_only_cli_profile().is_write_enabled is False

    def test_the_record_is_frozen(self):
        profile = checkpoint_only_cli_profile()
        with pytest.raises(dataclasses.FrozenInstanceError):
            profile.is_write_enabled = True

    def test_the_capability_vocabulary_is_the_reviewed_seven(self):
        assert CAPABILITIES == (
            "read_tools",
            "structured_output",
            "interrupt",
            "live_input",
            "checkpoint_export",
            "questions",
            "usage_complete",
        )

    def test_driver_digest_and_routes_are_part_of_the_record(self):
        profile = interactive_profile(
            driver_digest="b" * 64, provider_route="openai", credential_mode="byok"
        )
        assert profile.driver_digest == "b" * 64
        assert profile.provider_route == "openai"
        assert profile.credential_mode == "byok"


class TestCredentialBinding:
    def test_it_holds_a_broker_owned_reference_not_a_secret(self):
        record = binding()
        assert record.credential_ref == "broker/cred/0192"
        assert record.provider == "github"
        assert record.mode == "app"

    def test_it_is_frozen(self):
        record = binding()
        with pytest.raises(dataclasses.FrozenInstanceError):
            record.credential_ref = "ghs_pasted_token"


class TestValidateProfileBinding:
    def test_an_unbound_non_interactive_profile_is_valid(self):
        ok, reason = validate_profile_binding(checkpoint_only_cli_profile(), None)
        assert ok is True

    @pytest.mark.parametrize("capability", ["live_input", "interrupt"])
    def test_an_interactive_profile_requires_a_binding(self, capability):
        profile = interactive_profile(capabilities=("read_tools", capability))
        ok, reason = validate_profile_binding(profile, None)
        assert ok is False
        assert reason == "missing credential binding"

    def test_an_interactive_profile_with_a_binding_is_valid(self):
        ok, _ = validate_profile_binding(interactive_profile(), binding())
        assert ok is True

    def test_a_missing_selected_credential_never_falls_back(self):
        # the refusal is the point: no ambient credential of another
        # provider is silently tried instead (X08)
        profile = interactive_profile(provider_route="openai")
        ok, reason = validate_profile_binding(profile, None)
        assert ok is False
        assert "missing credential binding" in reason

    @pytest.mark.parametrize(
        "credential_ref",
        ["SECRET_TOKEN", "sk=abc123", "broker?token=abc123"],
    )
    def test_value_looking_credential_refs_are_rejected(self, credential_ref):
        ok, reason = validate_profile_binding(
            interactive_profile(), binding(credential_ref=credential_ref)
        )
        assert ok is False
        assert "secret value" in reason

    def test_a_broker_owned_id_passes(self):
        ok, _ = validate_profile_binding(
            interactive_profile(), binding(credential_ref="broker/cred/7")
        )
        assert ok is True

    def test_provider_mismatch_is_rejected(self):
        profile = interactive_profile(provider_route="github")
        ok, reason = validate_profile_binding(profile, binding(provider="gitlab"))
        assert ok is False
        assert "provider mismatch" in reason

    def test_matching_provider_is_valid(self):
        profile = interactive_profile(provider_route="github")
        ok, _ = validate_profile_binding(profile, binding(provider="github"))
        assert ok is True

    def test_a_profile_without_a_route_accepts_any_bound_provider(self):
        ok, _ = validate_profile_binding(checkpoint_only_cli_profile(), binding(provider="gitlab"))
        assert ok is True

    def test_a_binding_without_a_provider_passes_the_route_check(self):
        ok, _ = validate_profile_binding(interactive_profile(), binding(provider=""))
        assert ok is True


class TestRoleAllows:
    def test_discovery_cannot_select_a_write_enabled_profile(self):
        ok, reason = role_allows(interactive_profile(is_write_enabled=True), "discovery")
        assert ok is False
        assert "write-enabled" in reason

    def test_verification_cannot_select_a_write_enabled_profile(self):
        ok, reason = role_allows(checkpoint_only_cli_profile(is_write_enabled=True), "verification")
        assert ok is False
        assert "write-enabled" in reason

    @pytest.mark.parametrize("role", ["discovery", "implementation", "verification"])
    def test_read_only_profiles_are_allowed_everywhere(self, role):
        ok, _ = role_allows(checkpoint_only_cli_profile(), role)
        assert ok is True

    def test_implementation_may_select_a_write_enabled_profile(self):
        ok, _ = role_allows(interactive_profile(is_write_enabled=True), "implementation")
        assert ok is True

    def test_unknown_roles_fail_closed(self):
        ok, reason = role_allows(checkpoint_only_cli_profile(), "deployment")
        assert ok is False
        assert "unknown role" in reason


class TestCheckpointOnlyCliProfile:
    """FND-04 acceptance: a checkpoint-only CLI is never native interactive."""

    @pytest.mark.parametrize("role", ["discovery", "implementation", "verification"])
    def test_it_serves_every_read_only_role(self, role):
        ok, _ = role_allows(checkpoint_only_cli_profile(), role)
        assert ok is True

    def test_it_advertises_no_interrupt_and_no_live_input(self):
        profile = checkpoint_only_cli_profile()
        assert profile.supports("interrupt") is False
        assert profile.supports("live_input") is False
        assert profile.supports("checkpoint_export") is True

    def test_it_needs_no_credential_binding(self):
        ok, _ = validate_profile_binding(checkpoint_only_cli_profile(), None)
        assert ok is True


class TestManifestStatus:
    def test_none_is_unset(self):
        assert manifest_status(None) == "unset"

    def test_an_explicitly_empty_manifest_is_declared_empty(self):
        assert manifest_status(set()) == "declared_empty"

    def test_a_populated_manifest_is_declared(self):
        assert manifest_status({"cli-checkpoint-1"}) == "declared"
