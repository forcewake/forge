"""R28-25: project-bound model credentials — refs, never values.

The binding is a durable (project, provider) → credential-REF record
(broker doctrine); dispatch resolves it with a typed refusal for every
failure mode: a wrong project's ref, a revoked binding, a missing
binding (no default route), an unstaged env var. No surface — proof,
error, or persisted document — ever carries a credential VALUE.
"""

from __future__ import annotations

import json

import pytest

from forge.adaptive.project_credentials import (
    BINDING_SCHEMA,
    PROVIDER_ENV_VARS,
    CredentialRefusal,
    DispatchCredential,
    ProjectCredentialBinding,
    ProjectCredentialRegistry,
    registry_from_env,
    resolve_dispatch_credential,
)

PROJECT_A = 101
PROJECT_B = 202

#: A planted canary VALUE — the thing that must never appear anywhere.
CANARY_VALUE = "sk-canary-0123456789abcdef"


def _bound_registry() -> ProjectCredentialRegistry:
    registry = ProjectCredentialRegistry()
    registry.bind(PROJECT_A, "anthropic-gateway", "vault:kv/project-a#1", bound_by="ops@a")
    registry.bind(PROJECT_B, "anthropic-gateway", "vault:kv/project-b#1", bound_by="ops@b")
    return registry


class TestBindingRecord:
    def test_binding_maps_project_and_provider_to_a_ref(self):
        binding = ProjectCredentialBinding(
            project_id=PROJECT_A,
            provider="zai",
            credential_ref="vault:kv/project-a#zai",
            env_var=PROVIDER_ENV_VARS["zai"],
            bound_at="2026-09-23T00:00:00+00:00",
            bound_by="ops@a",
        )
        assert binding.live
        document = binding.as_document()
        assert document["schema"] == BINDING_SCHEMA
        assert document["credential_ref"] == "vault:kv/project-a#zai"
        assert ProjectCredentialBinding.from_document(document) == binding

    def test_a_value_looking_ref_is_refused_at_bind_time(self):
        with pytest.raises(CredentialRefusal, match="value_looking_ref"):
            ProjectCredentialBinding(
                project_id=PROJECT_A,
                provider="zai",
                credential_ref="ZAI_API_KEY=sk-live-12345678",
                env_var=PROVIDER_ENV_VARS["zai"],
                bound_at="",
                bound_by="ops@a",
            )

    def test_unknown_provider_route_is_refused(self):
        with pytest.raises(CredentialRefusal, match="unknown_provider"):
            ProjectCredentialBinding(
                project_id=PROJECT_A,
                provider="cheaper-fallback",
                credential_ref="vault:kv/x#1",
                env_var="WHATEVER",
                bound_at="",
                bound_by="ops@a",
            )

    def test_provider_and_env_var_must_agree(self):
        with pytest.raises(CredentialRefusal, match="provider_env_mismatch"):
            ProjectCredentialBinding(
                project_id=PROJECT_A,
                provider="zai",
                credential_ref="vault:kv/project-a#zai",
                env_var="OPENAI_API_KEY",
                bound_at="",
                bound_by="ops@a",
            )


class TestDispatchEnforcement:
    def test_the_bound_project_resolves_with_a_proof(self):
        proof = resolve_dispatch_credential(
            _bound_registry(), project_id=PROJECT_A, provider="anthropic-gateway"
        )
        assert isinstance(proof, DispatchCredential)
        assert proof.credential_ref == "vault:kv/project-a#1"
        assert proof.env_var == "ANTHROPIC_AUTH_TOKEN"
        document = proof.as_document()
        assert document["project_id"] == PROJECT_A
        assert document["credential_ref"] == "vault:kv/project-a#1"
        assert CANARY_VALUE not in json.dumps(document)

    def test_another_projects_ref_is_refused(self):
        """The cross-tenant leak: the ref project B bound must never
        resolve for project A, even when presented explicitly."""
        registry = _bound_registry()
        with pytest.raises(CredentialRefusal, match="wrong_project_ref") as caught:
            resolve_dispatch_credential(
                registry,
                project_id=PROJECT_A,
                provider="anthropic-gateway",
                presented_ref="vault:kv/project-b#1",
            )
        assert caught.value.detail["presented"] == "vault:kv/project-b#1"

    def test_a_revoked_binding_refuses_dispatch(self):
        registry = _bound_registry()
        registry.revoke(PROJECT_A, "anthropic-gateway", revoked_by="ops@a")
        with pytest.raises(CredentialRefusal, match="revoked"):
            resolve_dispatch_credential(
                registry, project_id=PROJECT_A, provider="anthropic-gateway"
            )

    def test_no_binding_means_no_default_route(self):
        """Configuration outage: nothing bound → refuse, never silently
        fall back to a cheaper or ambient shared credential."""
        registry = ProjectCredentialRegistry()
        with pytest.raises(CredentialRefusal, match="no_binding"):
            resolve_dispatch_credential(
                registry, project_id=PROJECT_A, provider="anthropic-gateway"
            )

    def test_an_unstaged_env_var_refuses_the_dispatch(self):
        registry = _bound_registry()
        with pytest.raises(CredentialRefusal, match="env_absent"):
            resolve_dispatch_credential(
                registry,
                project_id=PROJECT_A,
                provider="anthropic-gateway",
                environ={"SOME_OTHER_VAR": "x"},
            )

    def test_env_presence_without_value_reading_resolves(self):
        registry = _bound_registry()
        proof = resolve_dispatch_credential(
            registry,
            project_id=PROJECT_A,
            provider="anthropic-gateway",
            environ={"ANTHROPIC_AUTH_TOKEN": CANARY_VALUE},
        )
        # the VALUE is never copied into the proof.
        assert CANARY_VALUE not in json.dumps(proof.as_document())


class TestRotation:
    def test_rotation_keeps_the_superseded_binding_in_history(self):
        registry = _bound_registry()
        rotated = registry.bind(
            PROJECT_A, "anthropic-gateway", "vault:kv/project-a#2", bound_by="ops@a"
        )
        assert rotated.credential_ref == "vault:kv/project-a#2"
        assert len(registry.history) == 1
        superseded = registry.history[0]
        assert superseded.credential_ref == "vault:kv/project-a#1"
        assert superseded.revoked_at  # the old route is provably closed

    def test_the_old_ref_no_longer_resolves_after_rotation(self):
        registry = _bound_registry()
        registry.bind(PROJECT_A, "anthropic-gateway", "vault:kv/project-a#2", bound_by="ops@a")
        with pytest.raises(CredentialRefusal, match="wrong_project_ref"):
            resolve_dispatch_credential(
                registry,
                project_id=PROJECT_A,
                provider="anthropic-gateway",
                presented_ref="vault:kv/project-a#1",
            )

    def test_revoke_is_idempotent(self):
        registry = _bound_registry()
        assert registry.revoke(PROJECT_A, "anthropic-gateway", revoked_by="ops@a") is not None
        assert registry.revoke(PROJECT_A, "anthropic-gateway", revoked_by="ops@a") is None


class TestPersistence:
    async def test_the_registry_round_trips_through_its_json_document(self, tmp_path):
        path = tmp_path / "credentials.json"
        registry = ProjectCredentialRegistry(path=path)
        registry.bind(PROJECT_A, "anthropic-gateway", "vault:kv/project-a#1", bound_by="ops@a")
        registry.bind(PROJECT_A, "anthropic-gateway", "vault:kv/project-a#2", bound_by="ops@a")

        reopened = ProjectCredentialRegistry(path=path)
        binding = reopened.binding_for(PROJECT_A, "anthropic-gateway")
        assert binding is not None and binding.credential_ref == "vault:kv/project-a#2"
        assert [entry.credential_ref for entry in reopened.history] == ["vault:kv/project-a#1"]

        # The persisted document carries refs only — never values.
        assert CANARY_VALUE not in path.read_text()

    def test_registry_from_env_reads_the_document_path(self, tmp_path, monkeypatch):
        path = tmp_path / "bindings.json"
        registry = ProjectCredentialRegistry(path=path)
        registry.bind(PROJECT_B, "zai", "vault:kv/project-b#zai", bound_by="ops@b")
        monkeypatch.setenv("FORGE_CREDENTIAL_BINDINGS", str(path))

        from_env = registry_from_env()
        binding = from_env.binding_for(PROJECT_B, "zai")
        assert binding is not None and binding.env_var == "ZAI_API_KEY"

    def test_registry_from_env_defaults_to_in_memory(self):
        assert registry_from_env({}).path is None
