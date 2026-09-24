"""R28-25 + NEXT-19 (#207): project-bound model credentials — refs,
never values, keyed by CANONICAL subject.

The binding is a durable (canonical subject, provider) → credential-REF
record (broker doctrine); dispatch resolves it with a typed refusal for
every failure mode: a wrong subject's ref, a revoked binding, a rotated-
away ref, a missing binding (no default route), an unstaged env var. No
surface — proof, error, or persisted document — ever carries a
credential VALUE.

The /2 update (NEXT-19) re-keys bindings from the legacy
``(int project_id, provider)`` to ``(subject_id, provider)`` — the
update edits below are exactly the tests that encoded the int-key shape
(``_bound_registry``, the rotation/refusal calls that passed
``project_id=``, and the persistence round-trip lookups); the /1 read
adapter keeps persisted /1 documents loadable under a synthesized
default-connection subject.
"""

from __future__ import annotations

import json

import pytest

from forge.adaptive.operator_snapshot import CanonicalSubject
from forge.adaptive.project_credentials import (
    BINDING_SCHEMA,
    BINDING_SCHEMA_V1,
    DISPATCH_PROOF_SCHEMA,
    PROVIDER_ENV_VARS,
    PROVIDER_ROUTE_OF_DRIVER,
    ProjectCredentialBinding,
    ProjectCredentialRegistry,
    CredentialRefusal,
    binding_subject_of_run,
    legacy_subject_for_project,
    provider_route_for_driver,
    registry_from_env,
    resolve_dispatch_credential,
)

#: Two subjects with the SAME numeric platform id on DIFFERENT
#: connections — the collision the /2 key exists to prevent.
SUBJECT_A = CanonicalSubject(
    provider_family="gitlab", connection="gitlab.a.example", native_id="101"
)
SUBJECT_B = CanonicalSubject(
    provider_family="gitlab", connection="gitlab.b.example", native_id="101"
)
#: The same numeric id on a different PROVIDER FAMILY, too.
SUBJECT_GH = CanonicalSubject(
    provider_family="github", connection="github.example", native_id="acme/forge"
)

#: A planted canary VALUE — the thing that must never appear anywhere.
CANARY_VALUE = "sk-canary-0123456789abcdef"


def _bound_registry() -> ProjectCredentialRegistry:
    registry = ProjectCredentialRegistry()
    registry.bind(SUBJECT_A, "anthropic-gateway", "vault:kv/project-a#1", bound_by="ops@a")
    registry.bind(SUBJECT_B, "anthropic-gateway", "vault:kv/project-b#1", bound_by="ops@b")
    return registry


class TestBindingRecord:
    def test_binding_maps_subject_and_provider_to_a_ref(self):
        binding = ProjectCredentialBinding(
            subject=SUBJECT_A.subject_id(),
            provider="zai",
            credential_ref="vault:kv/project-a#zai",
            env_var=PROVIDER_ENV_VARS["zai"],
            bound_at="2026-09-23T00:00:00+00:00",
            bound_by="ops@a",
        )
        assert binding.live
        document = binding.as_document()
        assert document["schema"] == BINDING_SCHEMA
        assert document["subject"] == SUBJECT_A.subject_id()
        assert document["credential_ref"] == "vault:kv/project-a#zai"
        assert ProjectCredentialBinding.from_document(document) == binding

    def test_a_value_looking_ref_is_refused_at_bind_time(self):
        with pytest.raises(CredentialRefusal, match="value_looking_ref"):
            ProjectCredentialBinding(
                subject=SUBJECT_A.subject_id(),
                provider="zai",
                credential_ref="ZAI_API_KEY=sk-live-12345678",
                env_var=PROVIDER_ENV_VARS["zai"],
                bound_at="",
                bound_by="ops@a",
            )

    def test_unknown_provider_route_is_refused(self):
        with pytest.raises(CredentialRefusal, match="unknown_provider"):
            ProjectCredentialBinding(
                subject=SUBJECT_A.subject_id(),
                provider="cheaper-fallback",
                credential_ref="vault:kv/x#1",
                env_var="WHATEVER",
                bound_at="",
                bound_by="ops@a",
            )

    def test_provider_and_env_var_must_agree(self):
        with pytest.raises(CredentialRefusal, match="provider_env_mismatch"):
            ProjectCredentialBinding(
                subject=SUBJECT_A.subject_id(),
                provider="zai",
                credential_ref="vault:kv/project-a#zai",
                env_var="OPENAI_API_KEY",
                bound_at="",
                bound_by="ops@a",
            )

    def test_a_non_subject_key_is_refused_loudly(self):
        with pytest.raises(ValueError, match="canonical subject"):
            ProjectCredentialBinding(
                subject="not-a-subject",
                provider="zai",
                credential_ref="vault:kv/x#1",
                env_var=PROVIDER_ENV_VARS["zai"],
                bound_at="",
                bound_by="ops@a",
            )


class TestCanonicalSubjectKeys:
    def test_equal_numeric_ids_on_different_connections_never_share_a_binding(self):
        """The collision the /1 numeric key allowed: two self-managed
        instances, both ``project 101`` — separate bindings, and a
        lookup by one can never resolve the other's ref."""
        registry = _bound_registry()
        proof_a = resolve_dispatch_credential(
            registry, subject=SUBJECT_A, provider="anthropic-gateway"
        )
        proof_b = resolve_dispatch_credential(
            registry, subject=SUBJECT_B, provider="anthropic-gateway"
        )
        assert proof_a.credential_ref == "vault:kv/project-a#1"
        assert proof_b.credential_ref == "vault:kv/project-b#1"

    def test_another_subjects_ref_is_refused_as_the_cross_subject_leak(self):
        registry = _bound_registry()
        with pytest.raises(CredentialRefusal, match="wrong_project_ref") as caught:
            resolve_dispatch_credential(
                registry,
                subject=SUBJECT_A,
                provider="anthropic-gateway",
                presented_ref="vault:kv/project-b#1",
            )
        assert caught.value.detail["presented"] == "vault:kv/project-b#1"
        assert caught.value.detail["subject"] == SUBJECT_A.subject_id()

    def test_a_bound_subject_without_this_route_fails_closed(self):
        """The deployment bound this subject's anthropic route; a zai
        dispatch for the SAME subject has no default route — refuse,
        never fall back to an ambient shared credential."""
        registry = _bound_registry()
        with pytest.raises(CredentialRefusal, match="no_binding"):
            resolve_dispatch_credential(registry, subject=SUBJECT_A, provider="zai")

    def test_the_registry_knows_which_subjects_are_bound_at_all(self):
        registry = _bound_registry()
        assert registry.subject_is_bound(SUBJECT_A)
        assert registry.subject_is_bound(SUBJECT_B)
        assert not registry.subject_is_bound(SUBJECT_GH)


class TestLegacyReadAdapter:
    def test_a_v1_document_loads_under_the_synthesized_default_connection_subject(self):
        legacy_document = {
            "schema": BINDING_SCHEMA_V1,
            "project_id": 101,
            "provider": "anthropic-gateway",
            "credential_ref": "vault:kv/legacy#1",
            "env_var": "ANTHROPIC_AUTH_TOKEN",
            "bound_at": "2026-01-01T00:00:00+00:00",
            "bound_by": "ops@legacy",
            "revoked_at": None,
        }
        binding = ProjectCredentialBinding.from_document(dict(legacy_document))
        assert binding.subject == legacy_subject_for_project(101).subject_id()
        assert binding.subject == "gitlab/-/101"
        assert binding.credential_ref == "vault:kv/legacy#1"

    def test_a_full_subject_dispatch_never_matches_a_legacy_binding(self):
        """The adapter preserves the /1 document, it does not widen it: a
        recorded-connection subject is a DIFFERENT key, so a /2 dispatch
        fails closed until the operator re-binds under /2."""
        registry = ProjectCredentialRegistry()
        legacy = ProjectCredentialBinding.from_document(
            {
                "project_id": 101,
                "provider": "anthropic-gateway",
                "credential_ref": "vault:kv/legacy#1",
                "env_var": "ANTHROPIC_AUTH_TOKEN",
            }
        )
        registry.bindings[(legacy.subject, legacy.provider)] = legacy
        assert registry.binding_for(SUBJECT_A, "anthropic-gateway") is None
        legacy_subject = legacy_subject_for_project(101)
        assert (
            resolve_dispatch_credential(
                registry, subject=legacy_subject, provider="anthropic-gateway"
            ).credential_ref
            == "vault:kv/legacy#1"
        )

    def test_a_legacy_document_without_a_project_id_is_refused(self):
        with pytest.raises(CredentialRefusal, match="legacy_binding_without_project"):
            ProjectCredentialBinding.from_document(
                {"provider": "zai", "credential_ref": "vault:kv/x", "env_var": "ZAI_API_KEY"}
            )


class TestDispatchEnforcement:
    def test_the_bound_subject_resolves_with_a_proof(self):
        proof = resolve_dispatch_credential(
            _bound_registry(), subject=SUBJECT_A, provider="anthropic-gateway"
        )
        assert proof.credential_ref == "vault:kv/project-a#1"
        assert proof.env_var == "ANTHROPIC_AUTH_TOKEN"
        document = proof.as_document()
        assert document["schema"] == DISPATCH_PROOF_SCHEMA
        assert document["subject"] == SUBJECT_A.subject_id()
        assert document["binding_revision"] == 1
        assert document["credential_ref"] == "vault:kv/project-a#1"
        assert CANARY_VALUE not in json.dumps(document)

    def test_a_revoked_binding_refuses_dispatch(self):
        registry = _bound_registry()
        registry.revoke(SUBJECT_A, "anthropic-gateway", revoked_by="ops@a")
        with pytest.raises(CredentialRefusal, match="revoked"):
            resolve_dispatch_credential(registry, subject=SUBJECT_A, provider="anthropic-gateway")

    def test_no_binding_means_no_default_route(self):
        """Configuration outage: nothing bound → refuse, never silently
        fall back to a cheaper or ambient shared credential."""
        registry = ProjectCredentialRegistry()
        registry.bind(SUBJECT_GH, "anthropic-gateway", "vault:kv/gh#1", bound_by="ops@gh")
        with pytest.raises(CredentialRefusal, match="no_binding"):
            resolve_dispatch_credential(registry, subject=SUBJECT_A, provider="anthropic-gateway")

    def test_an_unstaged_env_var_refuses_the_dispatch(self):
        registry = _bound_registry()
        with pytest.raises(CredentialRefusal, match="env_absent"):
            resolve_dispatch_credential(
                registry,
                subject=SUBJECT_A,
                provider="anthropic-gateway",
                environ={"SOME_OTHER_VAR": "x"},
            )

    def test_env_presence_without_value_reading_resolves(self):
        registry = _bound_registry()
        proof = resolve_dispatch_credential(
            registry,
            subject=SUBJECT_A,
            provider="anthropic-gateway",
            environ={"ANTHROPIC_AUTH_TOKEN": CANARY_VALUE},
        )
        # the VALUE is never copied into the proof.
        assert CANARY_VALUE not in json.dumps(proof.as_document())


class TestRotation:
    def test_rotation_keeps_the_superseded_binding_in_history(self):
        registry = _bound_registry()
        rotated = registry.bind(
            SUBJECT_A, "anthropic-gateway", "vault:kv/project-a#2", bound_by="ops@a"
        )
        assert rotated.credential_ref == "vault:kv/project-a#2"
        assert rotated.revision == 2  # the rotation is a NEW decision
        assert len(registry.history) == 1
        superseded = registry.history_for(SUBJECT_A, "anthropic-gateway")[0]
        assert superseded.credential_ref == "vault:kv/project-a#1"
        assert superseded.revoked_at  # the old route is provably closed

    def test_the_rotated_away_ref_refuses_typed_rotated(self):
        """The /2 rotation contract: presenting the ref a prior dispatch
        of this attempt resolved, after a rotation, is the typed
        ``rotated`` refusal — never ``wrong_project_ref`` (that is the
        cross-subject leak), never a silent substitution."""
        registry = _bound_registry()
        registry.bind(SUBJECT_A, "anthropic-gateway", "vault:kv/project-a#2", bound_by="ops@a")
        with pytest.raises(CredentialRefusal, match="rotated") as caught:
            resolve_dispatch_credential(
                registry,
                subject=SUBJECT_A,
                provider="anthropic-gateway",
                presented_ref="vault:kv/project-a#1",
            )
        assert caught.value.detail["binding_revision"] == 2

    def test_the_live_ref_resolves_after_rotation(self):
        registry = _bound_registry()
        registry.bind(SUBJECT_A, "anthropic-gateway", "vault:kv/project-a#2", bound_by="ops@a")
        proof = resolve_dispatch_credential(
            registry, subject=SUBJECT_A, provider="anthropic-gateway"
        )
        assert proof.credential_ref == "vault:kv/project-a#2"
        assert proof.binding_revision == 2

    def test_revoke_is_idempotent(self):
        registry = _bound_registry()
        assert registry.revoke(SUBJECT_A, "anthropic-gateway", revoked_by="ops@a") is not None
        assert registry.revoke(SUBJECT_A, "anthropic-gateway", revoked_by="ops@a") is None


class TestDriverRoutes:
    def test_every_declared_driver_names_a_known_route(self):
        from forge.runs.harness_selection import DRIVER_CREDENTIAL_VARS

        for driver, route in PROVIDER_ROUTE_OF_DRIVER.items():
            assert route in PROVIDER_ENV_VARS
        # The mirror stays in sync with the driver-side credential table:
        # each driver's PRIMARY credential var is its route's env var
        # (dotnet-lane declares none — it rides the gateway ambiently).
        for driver, variables in DRIVER_CREDENTIAL_VARS.items():
            if not variables or driver not in PROVIDER_ROUTE_OF_DRIVER:
                continue
            assert PROVIDER_ENV_VARS[PROVIDER_ROUTE_OF_DRIVER[driver]] == variables[0]

    def test_an_unknown_driver_names_no_route(self):
        assert provider_route_for_driver("mystery-lane") == ""
        assert provider_route_for_driver("") == ""


class TestBindingSubjectOfRun:
    def test_a_gitlab_run_row_derives_its_subject_from_its_own_columns(self):
        class _Run:
            provider = "gitlab"
            project_id = 90210
            evidence = {"connection": "https://GitLab.Example/team"}

        subject = binding_subject_of_run(_Run())
        assert subject is not None
        assert subject.subject_id() == "gitlab/gitlab.example/90210"

    def test_a_run_without_a_subject_names_none(self):
        class _Run:
            provider = "fake"
            project_id = 1
            evidence = {}

        assert binding_subject_of_run(_Run()) is None


class TestPersistence:
    async def test_the_registry_round_trips_through_its_json_document(self, tmp_path):
        path = tmp_path / "credentials.json"
        registry = ProjectCredentialRegistry(path=path)
        registry.bind(SUBJECT_A, "anthropic-gateway", "vault:kv/project-a#1", bound_by="ops@a")
        registry.bind(SUBJECT_A, "anthropic-gateway", "vault:kv/project-a#2", bound_by="ops@a")

        reopened = ProjectCredentialRegistry(path=path)
        binding = reopened.binding_for(SUBJECT_A, "anthropic-gateway")
        assert binding is not None and binding.credential_ref == "vault:kv/project-a#2"
        assert [entry.credential_ref for entry in reopened.history] == ["vault:kv/project-a#1"]

        # The persisted document carries refs only — never values.
        assert CANARY_VALUE not in path.read_text()

    def test_a_v1_document_file_loads_through_the_adapter(self, tmp_path):
        path = tmp_path / "bindings-v1.json"
        path.write_text(
            json.dumps(
                {
                    "schema": BINDING_SCHEMA_V1,
                    "bindings": [
                        {
                            "schema": BINDING_SCHEMA_V1,
                            "project_id": 202,
                            "provider": "zai",
                            "credential_ref": "vault:kv/legacy-zai",
                            "env_var": "ZAI_API_KEY",
                            "bound_at": "2026-01-01T00:00:00+00:00",
                            "bound_by": "ops@legacy",
                        }
                    ],
                    "history": [],
                }
            ),
            encoding="utf-8",
        )
        registry = ProjectCredentialRegistry(path=path)
        binding = registry.binding_for(legacy_subject_for_project(202), "zai")
        assert binding is not None and binding.env_var == "ZAI_API_KEY"
        # Persisting again writes the /2 shape (the adapter is one-way).
        registry.bind(
            legacy_subject_for_project(202), "zai", "vault:kv/legacy-zai#2", bound_by="ops"
        )
        assert BINDING_SCHEMA_V1 not in path.read_text()

    def test_registry_from_env_reads_the_document_path(self, tmp_path, monkeypatch):
        path = tmp_path / "bindings.json"
        registry = ProjectCredentialRegistry(path=path)
        registry.bind(SUBJECT_B, "zai", "vault:kv/project-b#zai", bound_by="ops@b")
        monkeypatch.setenv("FORGE_CREDENTIAL_BINDINGS", str(path))

        from_env = registry_from_env()
        binding = from_env.binding_for(SUBJECT_B, "zai")
        assert binding is not None and binding.env_var == "ZAI_API_KEY"

    def test_registry_from_env_defaults_to_in_memory(self):
        assert registry_from_env({}).path is None
