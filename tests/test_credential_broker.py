"""NEXT-19 (#207): the credential broker contract — the resolution step
between a bound REF and the env slot a dispatched lane consumes.

Covered here: the protocol shape (``resolve(ref, grant)`` →
``ResolvedCredential(version, staged_env, receipt)``), the default
:class:`EnvBroker` (today's ambient behavior, now receipted), the
:class:`StagedBroker` double, the dispatch-seam composition
(:func:`stage_dispatch_credential`) with its fail-closed refusals
(rotation, wrong-subject ref, staged-slot mismatch), the value-leak
canary (the credential VALUE never appears in any receipt, proof or
document), the unbound-subject opt-out, and the in-flight snapshot
semantics (a staged generation is never re-read mid-flight).
"""

from __future__ import annotations

import json

import pytest

from forge.adaptive.credential_broker import (
    BROKER_RECEIPT_SCHEMA,
    BrokerCredentialRefusal,
    CredentialBroker,
    EnvBroker,
    StagedBroker,
    stage_dispatch_credential,
)
from forge.adaptive.operator_snapshot import CanonicalSubject
from forge.adaptive.project_credentials import (
    CredentialRefusal,
    ProjectCredentialRegistry,
    resolve_dispatch_credential,
)

SUBJECT = CanonicalSubject(provider_family="gitlab", connection="gitlab.example", native_id="90210")
OTHER_SUBJECT = CanonicalSubject(
    provider_family="gitlab", connection="gitlab.other.example", native_id="90210"
)

#: The planted credential VALUE — the string that must never appear in
#: any receipt, proof or staged-document EXCEPT the staged_env itself
#: (the one place the value legitimately lives).
CANARY_VALUE = "sk-canary-0123456789abcdef"

ENV_REF = "env:ANTHROPIC_AUTH_TOKEN"


def _bound_registry() -> ProjectCredentialRegistry:
    registry = ProjectCredentialRegistry()
    registry.bind(SUBJECT, "anthropic-gateway", ENV_REF, bound_by="ops@a")
    return registry


class TestEnvBroker:
    async def test_an_env_ref_resolves_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", CANARY_VALUE)
        resolved = await EnvBroker().resolve(ENV_REF, grant={"run_id": "r1"})
        assert resolved.staged_env == {"ANTHROPIC_AUTH_TOKEN": CANARY_VALUE}
        # The version is a PRESENCE stamp — never a digest of the content
        # (a hash of a low-entropy secret is a lookup table entry).
        assert resolved.version == "env:ANTHROPIC_AUTH_TOKEN:present"
        assert "canary" not in resolved.version

    async def test_the_receipt_records_names_and_presence_never_content(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", CANARY_VALUE)
        resolved = await EnvBroker().resolve(ENV_REF)
        receipt = resolved.receipt
        assert receipt["schema"] == BROKER_RECEIPT_SCHEMA
        assert receipt["resolver_identity"] == "env"
        assert receipt["provider_route"] == "anthropic-gateway"
        assert receipt["env_var"] == "ANTHROPIC_AUTH_TOKEN"
        assert receipt["env_present"] is True
        assert receipt["resolved_version"] == resolved.version
        assert CANARY_VALUE not in json.dumps(receipt)

    async def test_a_missing_env_var_refuses_typed(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        with pytest.raises(BrokerCredentialRefusal, match="env_absent") as caught:
            await EnvBroker().resolve(ENV_REF)
        # The refusal names the variable, never the content.
        assert caught.value.detail == {
            "resolver": "env",
            "env_var": "ANTHROPIC_AUTH_TOKEN",
            "env_present": False,
        }

    async def test_a_foreign_ref_scheme_refuses_typed(self):
        with pytest.raises(BrokerCredentialRefusal, match="unknown_ref_scheme"):
            await EnvBroker().resolve("vault:kv/eng#42")

    async def test_a_pinned_environ_is_read_not_the_process_env(self):
        broker = EnvBroker(environ={"ANTHROPIC_AUTH_TOKEN": "pinned"})
        resolved = await broker.resolve(ENV_REF)
        assert resolved.staged_env == {"ANTHROPIC_AUTH_TOKEN": "pinned"}

    async def test_the_brokers_implement_the_protocol(self):
        assert isinstance(EnvBroker(), CredentialBroker)
        assert isinstance(StagedBroker(), CredentialBroker)


class TestStagedBroker:
    async def test_a_staged_ref_resolves_to_its_pinned_value_and_version(self):
        broker = StagedBroker()
        broker.stage(
            "vault:kv/eng#42", "vault-secret-material", env_var="ANTHROPIC_AUTH_TOKEN", version="v7"
        )
        resolved = await broker.resolve("vault:kv/eng#42")
        assert resolved.version == "v7"
        assert resolved.staged_env == {"ANTHROPIC_AUTH_TOKEN": "vault-secret-material"}
        assert resolved.receipt["resolver_identity"] == "staged"
        assert resolved.receipt["resolved_version"] == "v7"

    async def test_an_unstaged_ref_refuses_typed(self):
        with pytest.raises(BrokerCredentialRefusal, match="unresolved_ref"):
            await StagedBroker().resolve("vault:kv/eng#42")


class TestStageDispatchCredential:
    async def test_the_happy_path_stages_the_brokers_selection_with_the_proof(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", CANARY_VALUE)
        staged = await stage_dispatch_credential(
            _bound_registry(),
            EnvBroker(),
            subject=SUBJECT,
            provider="anthropic-gateway",
            grant={"run_id": "run-1", "attempt_generation": 0},
        )
        assert staged is not None
        assert staged.staged_env == {"ANTHROPIC_AUTH_TOKEN": CANARY_VALUE}
        assert staged.env_var == "ANTHROPIC_AUTH_TOKEN"
        assert staged.binding_revision == 1
        assert staged.resolver_identity == "env"
        assert staged.resolved_version == "env:ANTHROPIC_AUTH_TOKEN:present"
        proof = staged.proof
        assert proof["schema"] == "forge.project.dispatch-credential-proof/2"
        assert proof["subject"] == SUBJECT.subject_id()
        assert proof["binding_revision"] == 1
        assert proof["resolver_identity"] == "env"
        assert proof["resolved_version"] == "env:ANTHROPIC_AUTH_TOKEN:present"
        assert proof["grant"] == {"run_id": "run-1", "attempt_generation": "0"}
        assert proof["receipt"]["schema"] == BROKER_RECEIPT_SCHEMA
        # The VALUE never appears in the proof — the audit trail cites
        # the version, never the material.
        assert CANARY_VALUE not in json.dumps(proof)

    async def test_an_unbound_subject_stages_nothing(self):
        staged = await stage_dispatch_credential(
            _bound_registry(),
            EnvBroker(),
            subject=OTHER_SUBJECT,
            provider="anthropic-gateway",
        )
        assert staged is None  # today's ambient behavior; attribution unknown

    async def test_an_unknown_route_stages_nothing(self):
        staged = await stage_dispatch_credential(
            _bound_registry(),
            EnvBroker(),
            subject=SUBJECT,
            provider="",
        )
        assert staged is None

    async def test_a_revoked_binding_refuses_before_the_broker_is_consulted(self):
        broker = StagedBroker()
        broker.stage(ENV_REF, CANARY_VALUE, env_var="ANTHROPIC_AUTH_TOKEN")
        registry = _bound_registry()
        registry.revoke(SUBJECT, "anthropic-gateway", revoked_by="ops@a")
        with pytest.raises(CredentialRefusal, match="revoked") as caught:
            await stage_dispatch_credential(
                registry, broker, subject=SUBJECT, provider="anthropic-gateway"
            )
        assert caught.value.reason == "revoked"
        assert broker.resolve_calls == []  # zero broker calls, zero provider calls

    async def test_rotation_between_resolution_and_dispatch_refuses_typed_rotated(self):
        registry = _bound_registry()
        broker = StagedBroker()
        broker.stage(ENV_REF, "generation-one", env_var="ANTHROPIC_AUTH_TOKEN", version="v1")
        broker.stage(
            "vault:kv/eng#2", "generation-two", env_var="ANTHROPIC_AUTH_TOKEN", version="v2"
        )
        # The first dispatch resolved ENV_REF; the rotation moves the
        # binding to the vault ref BEFORE the re-dispatch of the same
        # attempt presents the prior ref.
        registry.bind(SUBJECT, "anthropic-gateway", "vault:kv/eng#2", bound_by="ops@a")
        with pytest.raises(CredentialRefusal, match="rotated"):
            await stage_dispatch_credential(
                registry,
                broker,
                subject=SUBJECT,
                provider="anthropic-gateway",
                presented_ref=ENV_REF,
            )
        # The retry (a NEW attempt presents nothing) re-resolves the
        # rotated-in version — never a silent substitution.
        staged = await stage_dispatch_credential(
            registry, broker, subject=SUBJECT, provider="anthropic-gateway"
        )
        assert staged is not None
        assert staged.credential_ref == "vault:kv/eng#2"
        assert staged.resolved_version == "v2"
        assert staged.binding_revision == 2

    async def test_a_foreign_presented_ref_refuses_wrong_project_ref(self):
        registry = _bound_registry()
        with pytest.raises(CredentialRefusal, match="wrong_project_ref"):
            await stage_dispatch_credential(
                registry,
                StagedBroker(),
                subject=SUBJECT,
                provider="anthropic-gateway",
                presented_ref="env:ZAI_API_KEY",
            )

    async def test_a_broker_staging_the_wrong_slot_refuses_typed(self):
        registry = _bound_registry()
        broker = StagedBroker()
        # Correct ref, mismatched staged credential slot: the broker's
        # staged env disagrees with the binding's env var.
        broker.stage(ENV_REF, CANARY_VALUE, env_var="ZAI_API_KEY")
        with pytest.raises(CredentialRefusal, match="staged_slot_mismatch") as caught:
            await stage_dispatch_credential(
                registry, broker, subject=SUBJECT, provider="anthropic-gateway"
            )
        assert caught.value.detail["bound_env_var"] == "ANTHROPIC_AUTH_TOKEN"
        assert caught.value.detail["staged_env_vars"] == ["ZAI_API_KEY"]

    async def test_colliding_numeric_ids_resolve_their_own_bindings_only(self):
        """Two connections, equal numeric ids: the staging seam resolves
        per SUBJECT — one instance's lane can never stage the other's
        credential (the acceptance criterion, through the seam)."""
        registry = ProjectCredentialRegistry()
        registry.bind(SUBJECT, "anthropic-gateway", "env:ANTHROPIC_AUTH_TOKEN", bound_by="ops@a")
        registry.bind(OTHER_SUBJECT, "anthropic-gateway", "vault:kv/other#1", bound_by="ops@b")
        broker = StagedBroker()
        broker.stage("env:ANTHROPIC_AUTH_TOKEN", "a-material", env_var="ANTHROPIC_AUTH_TOKEN")
        broker.stage("vault:kv/other#1", "b-material", env_var="ANTHROPIC_AUTH_TOKEN")
        staged = await stage_dispatch_credential(
            registry, broker, subject=SUBJECT, provider="anthropic-gateway"
        )
        assert staged is not None
        assert staged.staged_env == {"ANTHROPIC_AUTH_TOKEN": "a-material"}
        # And a lookup by one subject cannot even SEE the other's ref.
        proof = resolve_dispatch_credential(registry, subject=SUBJECT, provider="anthropic-gateway")
        assert proof.credential_ref == "env:ANTHROPIC_AUTH_TOKEN"


class TestInFlightSnapshot:
    async def test_the_staged_generation_is_a_snapshot_never_re_read(self, monkeypatch):
        """In-flight semantics: after staging, the snapshot holds — the
        broker/environment changing underneath never rewrites a staged
        lane; only the NEXT staging call re-resolves."""
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "generation-one")
        broker_env = {"ANTHROPIC_AUTH_TOKEN": "generation-one"}
        broker = EnvBroker(environ=broker_env)
        staged = await stage_dispatch_credential(
            _bound_registry(), broker, subject=SUBJECT, provider="anthropic-gateway"
        )
        assert staged is not None
        assert staged.staged_env == {"ANTHROPIC_AUTH_TOKEN": "generation-one"}

        # The credential world moves on (rotation at the source) — the
        # in-flight snapshot does not.
        broker_env["ANTHROPIC_AUTH_TOKEN"] = "generation-two"
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "generation-two")
        assert staged.staged_env == {"ANTHROPIC_AUTH_TOKEN": "generation-one"}

        # The NEXT dispatch re-resolves the new generation.
        restaged = await stage_dispatch_credential(
            _bound_registry(), broker, subject=SUBJECT, provider="anthropic-gateway"
        )
        assert restaged is not None
        assert restaged.staged_env == {"ANTHROPIC_AUTH_TOKEN": "generation-two"}
        assert restaged.resolved_version == staged.resolved_version  # presence stamp
