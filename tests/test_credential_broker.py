"""NEXT-19 (#207) + R38-02 (#303): the credential broker contract — the
resolution step between a bound REF and the env slot a dispatched lane
consumes, and the DELIVERY plan that decides how the credential reaches
the lane (one supported transport per profile, references only).

Covered here: the protocol shape (``resolve(ref, grant)`` →
``ResolvedCredential(version, staged_env, receipt)``), the default
:class:`EnvBroker` (today's ambient behavior, now receipted), the
:class:`StagedBroker` double, the dispatch-seam composition
(:func:`stage_dispatch_credential`) with its fail-closed refusals
(rotation, wrong-subject ref, staged-slot mismatch), the value-leak
canary (the credential VALUE never appears in any receipt, proof or
document), the unbound-subject opt-out, and the in-flight snapshot
semantics (a staged generation is never re-read mid-flight).

R38-02 adds the delivery-plan matrix (:func:`delivery_plan`): the mode
selection per profile × declared route, the typed
``delivery_route_unsupported`` refusal (undeclared/unsupported for a
BOUND subject — never an ambient fallback), the transport references
(the secret NAME, never the value), the dispatch/template conformance
against the ACTUAL shipped templates, and the unbound ambient-legacy
opt-out.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge.adaptive.credential_broker import (
    BROKER_RECEIPT_SCHEMA,
    CREDENTIAL_POLICY_COMPAT,
    DELIVERY_MODE_AZURE_GROUP,
    DELIVERY_MODE_GITHUB_NATIVE,
    DELIVERY_MODE_GITLAB_PROTECTED,
    DELIVERY_MODE_RUNNER_REDEMPTION,
    DELIVERY_PLAN_SCHEMA,
    DELIVERY_ROUTE_ENV,
    DELIVERY_TEMPLATE_DIR_ENV,
    OPERATION_GRANT_SCHEMA,
    PERMITTED_OPERATION_REDEMPTION,
    VERSION_KIND_FIXTURE,
    VERSION_KIND_PRESENCE,
    BrokerCredentialRefusal,
    CredentialBroker,
    CredentialDeliveryPlan,
    CredentialOperationGrant,
    EnvBroker,
    StagedBroker,
    credential_secret_name,
    credential_secret_segment,
    delivery_plan,
    delivery_template_conformance,
    native_locator,
    stage_dispatch_credential,
    template_identity_digest,
    validate_operation_grant_document,
)
from forge.adaptive.operator_snapshot import CanonicalSubject
from forge.adaptive.project_credentials import (
    BINDING_REVISION_UNKNOWN,
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

#: The repo's ci/templates directory (the conformance surface).
TEMPLATES_DIR = Path(__file__).parents[1] / "ci" / "templates"


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


# ----------------------------------------------------------------------
# R38-02 (#303) — the delivery plan: mode selection, refusal matrix,
# transport references, dispatch/template conformance.
# ----------------------------------------------------------------------


def _delivery_env(*routes: str) -> dict[str, str]:
    """Env declaring *routes* plus the shipped-templates dir (the
    conformance reads the ACTUAL repo templates)."""
    return {
        DELIVERY_ROUTE_ENV: ",".join(routes),
        DELIVERY_TEMPLATE_DIR_ENV: str(TEMPLATES_DIR),
    }


class TestSecretNameDerivation:
    def test_the_segment_is_uppercase_alnum_underscore(self):
        assert credential_secret_segment("env:ANTHROPIC_AUTH_TOKEN") == ("ENV_ANTHROPIC_AUTH_TOKEN")
        assert credential_secret_segment("vault:kv/eng#42") == "VAULT_KV_ENG_42"
        assert credential_secret_segment("anthropic-main") == "ANTHROPIC_MAIN"
        assert credential_secret_name("env:ANTHROPIC_AUTH_TOKEN") == (
            "FORGE_MODEL_ENV_ANTHROPIC_AUTH_TOKEN"
        )


class TestDeliveryPlanModeSelection:
    async def test_github_native_selects_the_ref_derived_secret(self):
        plan = await delivery_plan(
            _bound_registry(),
            StagedBroker(),
            subject=SUBJECT,
            provider_route="anthropic-gateway",
            profile="github",
            environ=_delivery_env(DELIVERY_MODE_GITHUB_NATIVE),
        )
        assert plan is not None
        assert plan.mode == DELIVERY_MODE_GITHUB_NATIVE
        assert plan.transport_ref == "FORGE_MODEL_ENV_ANTHROPIC_AUTH_TOKEN"
        assert plan.dispatch_ref == "ENV_ANTHROPIC_AUTH_TOKEN"
        assert plan.redemption is False
        assert plan.expected_identity["binding_revision"] == 1
        assert plan.expected_identity["resolver"] == DELIVERY_MODE_GITHUB_NATIVE

    async def test_gitlab_native_selects_the_protected_variable(self):
        plan = await delivery_plan(
            _bound_registry(),
            StagedBroker(),
            subject=SUBJECT,
            provider_route="anthropic-gateway",
            profile="gitlab",
            environ=_delivery_env(DELIVERY_MODE_GITLAB_PROTECTED),
        )
        assert plan is not None
        assert plan.mode == DELIVERY_MODE_GITLAB_PROTECTED
        assert plan.transport_ref == "FORGE_MODEL_ENV_ANTHROPIC_AUTH_TOKEN"

    async def test_azure_native_selects_the_group_and_env_slot_secret(self):
        plan = await delivery_plan(
            _bound_registry(),
            StagedBroker(),
            subject=SUBJECT,
            provider_route="anthropic-gateway",
            profile="azure",
            environ=_delivery_env(DELIVERY_MODE_AZURE_GROUP),
        )
        assert plan is not None
        assert plan.mode == DELIVERY_MODE_AZURE_GROUP
        assert plan.transport_ref == "forge-lane-credentials/ANTHROPIC_AUTH_TOKEN"
        assert plan.expected_identity["resolver"] == DELIVERY_MODE_AZURE_GROUP

    async def test_runner_redemption_selects_the_lane_control_route_and_raw_ref(self):
        broker = StagedBroker()
        plan = await delivery_plan(
            _bound_registry(),
            broker,
            subject=SUBJECT,
            provider_route="anthropic-gateway",
            profile="github",
            environ=_delivery_env(DELIVERY_MODE_RUNNER_REDEMPTION),
        )
        assert plan is not None
        assert plan.mode == DELIVERY_MODE_RUNNER_REDEMPTION
        assert plan.transport_ref == "/lane/credentials/redeem"
        # The RAW ref rides the payload under redemption (the endpoint
        # re-checks it against the live binding).
        assert plan.dispatch_ref == ENV_REF
        assert plan.redemption is True
        assert plan.expected_identity["resolver"] == broker.resolver_identity

    async def test_redemption_is_uniform_across_all_three_profiles(self):
        for profile in ("github", "azure", "gitlab"):
            plan = await delivery_plan(
                _bound_registry(),
                StagedBroker(),
                subject=SUBJECT,
                provider_route="anthropic-gateway",
                profile=profile,
                environ=_delivery_env(DELIVERY_MODE_RUNNER_REDEMPTION),
            )
            assert plan is not None and plan.mode == DELIVERY_MODE_RUNNER_REDEMPTION


class TestDeliveryPlanRefusals:
    async def test_a_bound_subject_with_no_declared_route_refuses_typed(self):
        with pytest.raises(CredentialRefusal, match="delivery_route_unsupported") as caught:
            await delivery_plan(
                _bound_registry(),
                StagedBroker(),
                subject=SUBJECT,
                provider_route="anthropic-gateway",
                profile="github",
                environ={DELIVERY_TEMPLATE_DIR_ENV: str(TEMPLATES_DIR)},
            )
        assert caught.value.detail["env"] == DELIVERY_ROUTE_ENV
        assert caught.value.detail["supported"] == [
            DELIVERY_MODE_GITHUB_NATIVE,
            DELIVERY_MODE_RUNNER_REDEMPTION,
        ]

    async def test_a_route_supported_by_another_profile_refuses_typed(self):
        with pytest.raises(CredentialRefusal, match="delivery_route_unsupported"):
            await delivery_plan(
                _bound_registry(),
                StagedBroker(),
                subject=SUBJECT,
                provider_route="anthropic-gateway",
                profile="github",
                environ=_delivery_env(DELIVERY_MODE_GITLAB_PROTECTED),  # gitlab's mode
            )

    async def test_two_supported_routes_for_one_profile_refuse_ambiguous(self):
        with pytest.raises(CredentialRefusal, match="delivery_route_ambiguous"):
            await delivery_plan(
                _bound_registry(),
                StagedBroker(),
                subject=SUBJECT,
                provider_route="anthropic-gateway",
                profile="github",
                environ=_delivery_env(DELIVERY_MODE_GITHUB_NATIVE, DELIVERY_MODE_RUNNER_REDEMPTION),
            )

    async def test_a_revoked_binding_refuses_before_any_delivery_decision(self):
        registry = _bound_registry()
        registry.revoke(SUBJECT, "anthropic-gateway", revoked_by="ops@a")
        with pytest.raises(CredentialRefusal, match="revoked"):
            await delivery_plan(
                registry,
                StagedBroker(),
                subject=SUBJECT,
                provider_route="anthropic-gateway",
                profile="github",
                environ=_delivery_env(DELIVERY_MODE_GITHUB_NATIVE),
            )

    async def test_a_rotated_presented_ref_refuses_typed(self):
        registry = _bound_registry()
        registry.bind(SUBJECT, "anthropic-gateway", "vault:kv/eng#2", bound_by="ops@a")
        with pytest.raises(CredentialRefusal, match="rotated"):
            await delivery_plan(
                registry,
                StagedBroker(),
                subject=SUBJECT,
                provider_route="anthropic-gateway",
                profile="gitlab",
                presented_ref=ENV_REF,
                environ=_delivery_env(DELIVERY_MODE_GITLAB_PROTECTED),
            )

    async def test_an_unreadable_shipped_template_refuses_pre_paid(self, tmp_path):
        with pytest.raises(CredentialRefusal, match="delivery_template_unavailable"):
            await delivery_plan(
                _bound_registry(),
                StagedBroker(),
                subject=SUBJECT,
                provider_route="anthropic-gateway",
                profile="github",
                environ={
                    DELIVERY_ROUTE_ENV: DELIVERY_MODE_GITHUB_NATIVE,
                    DELIVERY_TEMPLATE_DIR_ENV: str(tmp_path / "no-such-dir"),
                },
            )


class TestDeliveryPlanOptOuts:
    async def test_an_unbound_subject_plans_nothing_ambient_legacy(self):
        plan = await delivery_plan(
            _bound_registry(),
            StagedBroker(),
            subject=OTHER_SUBJECT,
            provider_route="anthropic-gateway",
            profile="gitlab",
            environ=_delivery_env(DELIVERY_MODE_GITLAB_PROTECTED),
        )
        assert plan is None  # ambient-legacy: separable from strict BYOK

    async def test_an_unknown_provider_route_plans_nothing(self):
        plan = await delivery_plan(
            _bound_registry(),
            StagedBroker(),
            subject=SUBJECT,
            provider_route="",
            profile="gitlab",
            environ=_delivery_env(DELIVERY_MODE_GITLAB_PROTECTED),
        )
        assert plan is None

    async def test_an_unknown_profile_plans_nothing(self):
        plan = await delivery_plan(
            _bound_registry(),
            StagedBroker(),
            subject=SUBJECT,
            provider_route="anthropic-gateway",
            profile="gitea",
            environ=_delivery_env(DELIVERY_MODE_GITHUB_NATIVE),
        )
        assert plan is None


class TestDeliveryPlanDocument:
    async def test_the_plan_document_is_refs_only_no_value_slot(self):
        plan = await delivery_plan(
            _bound_registry(),
            StagedBroker(),
            subject=SUBJECT,
            provider_route="anthropic-gateway",
            profile="github",
            environ=_delivery_env(DELIVERY_MODE_GITHUB_NATIVE),
        )
        assert plan is not None
        document = plan.as_document()
        assert document["schema"] == DELIVERY_PLAN_SCHEMA
        assert document["attribution"] == "bound-delivery"
        assert document["mode"] == DELIVERY_MODE_GITHUB_NATIVE
        assert document["credential_ref"] == ENV_REF
        # The canary value has nowhere to appear — there is no value slot.
        assert CANARY_VALUE not in json.dumps(document)
        assert "value" not in json.dumps(document)


class TestDeliveryTemplateConformance:
    """The ACTUAL shipped templates satisfy each profile's transport —
    and an older delivery schema is refused with the instruction."""

    @staticmethod
    def _plan(mode: str) -> CredentialDeliveryPlan:
        return CredentialDeliveryPlan(
            subject="gitlab/gitlab.example/90210",
            provider="anthropic-gateway",
            profile={"github-native-secret": "github"}.get(mode, "gitlab"),
            credential_ref=ENV_REF,
            env_var="ANTHROPIC_AUTH_TOKEN",
            binding_revision=1,
            mode=mode,
            transport_ref={
                DELIVERY_MODE_GITHUB_NATIVE: "FORGE_MODEL_ENV_ANTHROPIC_AUTH_TOKEN",
                DELIVERY_MODE_GITLAB_PROTECTED: "FORGE_MODEL_ENV_ANTHROPIC_AUTH_TOKEN",
                DELIVERY_MODE_AZURE_GROUP: "forge-lane-credentials/ANTHROPIC_AUTH_TOKEN",
                DELIVERY_MODE_RUNNER_REDEMPTION: "/lane/credentials/redeem",
            }[mode],
            dispatch_ref="ENV_ANTHROPIC_AUTH_TOKEN",
            redemption=mode == DELIVERY_MODE_RUNNER_REDEMPTION,
        )

    def _template(self, name: str) -> str:
        return (TEMPLATES_DIR / name).read_text()

    def test_the_shipped_github_workflow_conforms(self):
        delivery_template_conformance(
            self._plan(DELIVERY_MODE_GITHUB_NATIVE),
            self._template("forge-harness.github.yml"),
        )

    def test_the_shipped_gitlab_batch_lane_conforms(self):
        delivery_template_conformance(
            self._plan(DELIVERY_MODE_GITLAB_PROTECTED),
            self._template("claude-code.gitlab-ci.yml"),
        )

    def test_the_shipped_azure_pipeline_conforms(self):
        delivery_template_conformance(
            self._plan(DELIVERY_MODE_AZURE_GROUP),
            self._template("forge-lane.azure-pipelines.yml"),
        )

    @pytest.mark.parametrize(
        ("mode", "name"),
        [
            (DELIVERY_MODE_GITHUB_NATIVE, "forge-harness.github.yml"),
            (DELIVERY_MODE_GITLAB_PROTECTED, "claude-code.gitlab-ci.yml"),
            (DELIVERY_MODE_AZURE_GROUP, "forge-lane.azure-pipelines.yml"),
        ],
    )
    def test_an_older_delivery_schema_is_refused_with_the_instruction(self, mode, name):
        plan = self._plan(mode)
        with pytest.raises(CredentialRefusal, match="delivery_template_mismatch") as caught:
            delivery_template_conformance(
                plan, "name: forge-harness\non: workflow_dispatch\ninputs: {}\n"
            )
        assert caught.value.detail["missing_markers"]
        instruction = str(caught.value.detail["instruction"])
        assert instruction
        # The instruction names the transport (the whole reference, or its
        # group and secret halves for the Azure variable-group transport).
        assert plan.transport_ref in instruction or all(
            part in instruction for part in plan.transport_ref.split("/") if part
        )

    def test_the_shipped_templates_consume_the_redemption_flag(self):
        for name in (
            "forge-harness.github.yml",
            "claude-code.gitlab-ci.yml",
            "forge-lane.azure-pipelines.yml",
            "claude-sdk-lane.gitlab-ci.yml",
        ):
            delivery_template_conformance(
                self._plan(DELIVERY_MODE_RUNNER_REDEMPTION), self._template(name)
            )


# ----------------------------------------------------------------------
# Q39-04 (#323) — the driver/template identity + the installed-template
# digest dimensions of the conformance.
# ----------------------------------------------------------------------


class TestDriverTemplateConformance:
    """The GitLab SDK lanes and batch routes validate against THEIR OWN
    templates — never claude-code's by default; the INSTALLED digest is
    verified against the local pass."""

    async def test_a_named_driver_conforms_against_its_own_template(self):
        plan = await delivery_plan(
            _bound_registry(),
            StagedBroker(),
            subject=SUBJECT,
            provider_route="anthropic-gateway",
            profile="gitlab",
            driver="claude-sdk-lane",
            environ=_delivery_env(DELIVERY_MODE_GITLAB_PROTECTED),
        )
        assert plan is not None
        sdk_template = (TEMPLATES_DIR / "claude-sdk-lane.gitlab-ci.yml").read_text()
        assert plan.template_driver == "claude-sdk-lane"
        assert plan.template_digest == template_identity_digest(sdk_template)
        # NOT claude-code's template digest — the driver's own file read
        assert plan.template_digest != template_identity_digest(
            (TEMPLATES_DIR / "claude-code.gitlab-ci.yml").read_text()
        )

    async def test_a_driver_whose_template_lacks_the_route_refuses(self):
        """The wrong-driver acceptance: a bound dispatch under
        codex-sdk-lane must validate CODEX's recipe — which ships no
        credential-consumption block yet — and refuse, not fall through
        to a claude-code-shaped pass."""
        with pytest.raises(CredentialRefusal, match="delivery_template_mismatch") as caught:
            await delivery_plan(
                _bound_registry(),
                StagedBroker(),
                subject=SUBJECT,
                provider_route="anthropic-gateway",
                profile="gitlab",
                driver="codex-sdk-lane",
                environ=_delivery_env(DELIVERY_MODE_GITLAB_PROTECTED),
            )
        detail = caught.value.detail
        assert detail["driver"] == "codex-sdk-lane"
        # either arm refusing is correct fail-closed behavior for a
        # recipe with no block: the mode markers or the structural
        # consumer mapping is what it lacks — and the refusal names the
        # DRIVER whose template was validated (never claude-code's).
        assert detail.get("structural_gaps") or detail.get("missing_markers")

    async def test_an_unknown_driver_refuses_consumer_route_unknown(self):
        with pytest.raises(CredentialRefusal, match="consumer_route_unknown") as caught:
            await delivery_plan(
                _bound_registry(),
                StagedBroker(),
                subject=SUBJECT,
                provider_route="anthropic-gateway",
                profile="gitlab",
                driver="forge-not-a-driver",
                environ=_delivery_env(DELIVERY_MODE_GITLAB_PROTECTED),
            )
        assert caught.value.detail["observability"] == "credential.consumer_route_unknown"

    def test_the_structural_check_refuses_marker_mentions_without_a_block(self):
        """Structural schema/consumer checks over substring presence: a
        template that MENTIONS every required marker (in comments) but
        ships no consumption block refuses when a driver is named."""
        mention_only = (
            "# credential_ref rides here: FORGE_CREDENTIAL_REF\n"
            "# and the carrier: FORGE_MODEL_${FORGE_CREDENTIAL_REF}\n"
        )
        plan = CredentialDeliveryPlan(
            subject="s",
            provider="anthropic-gateway",
            profile="gitlab",
            credential_ref=ENV_REF,
            env_var="ANTHROPIC_AUTH_TOKEN",
            binding_revision=1,
            mode=DELIVERY_MODE_GITLAB_PROTECTED,
            transport_ref="FORGE_MODEL_ENV_ANTHROPIC_AUTH_TOKEN",
            dispatch_ref="ENV_ANTHROPIC_AUTH_TOKEN",
            redemption=False,
        )
        with pytest.raises(CredentialRefusal, match="delivery_template_mismatch") as caught:
            delivery_template_conformance(plan, mention_only, driver="claude-code")
        assert "structural_gaps" in caught.value.detail

    async def test_an_installed_digest_mismatch_refuses_before_the_markers(self):
        shipped = (TEMPLATES_DIR / "forge-harness.github.yml").read_text()
        with pytest.raises(CredentialRefusal, match="profile_template_digest_mismatch") as caught:
            await delivery_plan(
                _bound_registry(),
                StagedBroker(),
                subject=SUBJECT,
                provider_route="anthropic-gateway",
                profile="github",
                installed_template_digest="0" * 16,
                environ=_delivery_env(DELIVERY_MODE_GITHUB_NATIVE),
            )
        detail = caught.value.detail
        assert detail["observability"] == "profile.template_digest_mismatch"
        assert detail["actual_digest"] == template_identity_digest(shipped)

    async def test_a_matching_installed_digest_passes_and_is_stamped_in_the_plan(self):
        shipped_digest = template_identity_digest(
            (TEMPLATES_DIR / "forge-harness.github.yml").read_text()
        )
        plan = await delivery_plan(
            _bound_registry(),
            StagedBroker(),
            subject=SUBJECT,
            provider_route="anthropic-gateway",
            profile="github",
            installed_template_digest=shipped_digest,
            environ=_delivery_env(DELIVERY_MODE_GITHUB_NATIVE),
        )
        assert plan is not None
        assert plan.template_digest == shipped_digest
        assert plan.as_document()["template_digest"] == shipped_digest

    def test_the_direct_conformance_refuses_an_unknown_driver(self):
        plan = CredentialDeliveryPlan(
            subject="s",
            provider="anthropic-gateway",
            profile="gitlab",
            credential_ref=ENV_REF,
            env_var="ANTHROPIC_AUTH_TOKEN",
            binding_revision=1,
            mode=DELIVERY_MODE_GITLAB_PROTECTED,
            transport_ref="FORGE_MODEL_ENV_ANTHROPIC_AUTH_TOKEN",
            dispatch_ref="ENV_ANTHROPIC_AUTH_TOKEN",
            redemption=False,
        )
        with pytest.raises(CredentialRefusal, match="consumer_route_unknown"):
            delivery_template_conformance(
                plan,
                (TEMPLATES_DIR / "claude-code.gitlab-ci.yml").read_text(),
                driver="forge-not-a-driver",
            )

    def test_a_locator_shaped_dispatch_ref_is_charset_safe(self):
        """The dispatch_ref a locator registry sends is inside the
        provider carrier charset (the conformance's marker composition
        consumes it verbatim)."""
        locator = native_locator("vault:kv/team-a")
        assert locator == locator.upper()
        assert all(char.isalnum() or char == "_" for char in locator)


# ----------------------------------------------------------------------
# R38-04 (#305) — the receipt correlation fields: every broker receipt
# carries its own id, the resolved version's KIND (a presence stamp is
# never a unique-secret-version proof) and the policy in force.
# ----------------------------------------------------------------------


class TestReceiptCorrelationFields:
    async def test_the_env_broker_receipt_declares_a_presence_version(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", CANARY_VALUE)
        resolved = await EnvBroker().resolve(ENV_REF)
        receipt = resolved.receipt
        assert receipt["receipt_id"]  # the id the consumer receipt joins on
        assert receipt["resolved_version_kind"] == VERSION_KIND_PRESENCE
        assert receipt["credential_policy"] == CREDENTIAL_POLICY_COMPAT

    async def test_the_staged_broker_receipt_declares_a_fixture_version(self):
        broker = StagedBroker()
        broker.stage(ENV_REF, CANARY_VALUE, env_var="ANTHROPIC_AUTH_TOKEN", version="v1")
        receipt = (await broker.resolve(ENV_REF)).receipt
        assert receipt["receipt_id"]
        assert receipt["resolved_version_kind"] == VERSION_KIND_FIXTURE

    async def test_the_dispatch_proof_carries_the_kind_and_the_policy(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", CANARY_VALUE)
        staged = await stage_dispatch_credential(
            _bound_registry(), EnvBroker(), subject=SUBJECT, provider="anthropic-gateway"
        )
        assert staged is not None
        assert staged.proof["resolved_version_kind"] == VERSION_KIND_PRESENCE
        assert staged.proof["credential_policy"] == CREDENTIAL_POLICY_COMPAT
        assert staged.proof["receipt"]["receipt_id"]
        assert CANARY_VALUE not in json.dumps(staged.proof)


# ----------------------------------------------------------------------
# R40-06 (#342) — the COMPLETE grant identity validation, the binding
# revision carried (and compared) at every seam, and the EXPLICIT
# pre-revision version adapter. Sameness of a binding is never inferred
# from a locator string alone.
# ----------------------------------------------------------------------


def _grant_document(**overrides: object) -> dict:
    """A well-formed persisted grant document for THIS subject/world."""
    from datetime import datetime, timedelta, timezone

    document: dict = {
        "schema": OPERATION_GRANT_SCHEMA,
        "grant_id": "g-r40-06",
        "work_id": "run-r40-06",
        "subject": SUBJECT.subject_id(),
        "provider": "anthropic-gateway",
        "credential_ref": ENV_REF,
        "binding_revision": 1,
        "attempt_generation": 2,
        "delivery_mode": DELIVERY_MODE_RUNNER_REDEMPTION,
        "operation": PERMITTED_OPERATION_REDEMPTION,
        "redemption_deadline": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    document.update(overrides)
    return document


class TestGrantIdentityValidation:
    """The validation entrypoint the redemption seam calls on the ONE
    authoritative copy — every wrong field is a typed refusal naming
    the field, BEFORE any broker invocation."""

    def test_a_well_formed_document_validates_and_loads(self):
        document = _grant_document()
        grant = validate_operation_grant_document(
            document,
            work_id="run-r40-06",
            provider="anthropic-gateway",
            credential_ref=ENV_REF,
            attempt_generation=2,
            subject_id=SUBJECT.subject_id(),
        )
        assert grant == CredentialOperationGrant.from_document(document)

    def test_a_wrong_field_is_a_typed_refusal_naming_the_field(self):
        """Schema tag, operation, delivery mode, work, canonical subject,
        internal generation, route and ref — each refused on its own."""
        cases = (
            ("schema", {"schema": "forge.credential.operation-grant/2"}),
            ("operation", {"operation": "credential-exfiltration"}),
            ("delivery_mode", {"delivery_mode": DELIVERY_MODE_GITHUB_NATIVE}),
            ("work_id", {"work_id": "run-other"}),
            ("subject", {"subject": OTHER_SUBJECT.subject_id()}),
            ("attempt_generation", {"attempt_generation": 7}),
            ("provider", {"provider": "openai"}),
            ("credential_ref", {"credential_ref": "vault:kv/other#1"}),
        )
        for field, mutation in cases:
            with pytest.raises(CredentialRefusal, match="grant_invalid_field") as caught:
                validate_operation_grant_document(
                    _grant_document(**mutation),
                    work_id="run-r40-06",
                    provider="anthropic-gateway",
                    credential_ref=ENV_REF,
                    attempt_generation=2,
                    subject_id=SUBJECT.subject_id(),
                )
            assert caught.value.detail["field"] == field
            assert caught.value.detail["observability"] == "credential.grant_invalid_field"

    def test_a_malformed_document_keeps_its_own_typed_refusal(self):
        with pytest.raises(CredentialRefusal, match="operation_grant_invalid"):
            validate_operation_grant_document(
                _grant_document(redemption_deadline="not-a-time"),
                work_id="run-r40-06",
                provider="anthropic-gateway",
            )


class TestGrantRevisionVersionAdapter:
    """The EXPLICIT adapter for pre-revision grant documents: an absent
    revision is the named unknown marker — never an inferred pass, never
    a coerced guess; a present-but-malformed revision stays typed."""

    def test_an_absent_revision_loads_as_the_explicit_unknown_marker(self):
        document = _grant_document()
        del document["binding_revision"]
        grant = CredentialOperationGrant.from_document(document)
        assert grant.binding_revision == BINDING_REVISION_UNKNOWN == 0
        # A real revision can never be 0 (the first bind is revision 1),
        # so the marker cannot collide with a live generation.
        assert _bound_registry().binding_for(SUBJECT, "anthropic-gateway").revision == 1

    def test_a_present_but_malformed_revision_stays_a_typed_refusal(self):
        with pytest.raises(CredentialRefusal, match="operation_grant_invalid") as caught:
            CredentialOperationGrant.from_document(_grant_document(binding_revision="two"))
        assert "non-integer binding_revision" in str(caught.value.detail["problem"])


class TestBindingRevisionComparison:
    """R40-06: the revision the authorization recorded vs the LIVE
    binding — same-ref rebind is a NEW decision; the ref string alone
    never carries an old authorization across it."""

    @staticmethod
    def _resolve(registry: ProjectCredentialRegistry, presented_revision: int | None):
        return resolve_dispatch_credential(
            registry,
            subject=SUBJECT,
            provider="anthropic-gateway",
            presented_ref=ENV_REF,
            presented_revision=presented_revision,
        )

    def test_the_same_revision_verifies_equal_and_passes(self):
        proof = self._resolve(_bound_registry(), 1)
        assert proof.binding_revision == 1

    def test_a_same_ref_rebind_refuses_typed_with_the_event_named(self):
        registry = _bound_registry()
        # Revoke, then REGRANT at the SAME locator: the ref string is
        # unchanged, the revision is 2 — a NEW binding decision.
        registry.revoke(SUBJECT, "anthropic-gateway", revoked_by="ops@a")
        registry.bind(SUBJECT, "anthropic-gateway", ENV_REF, bound_by="ops@a")
        with pytest.raises(CredentialRefusal, match="binding_revision_mismatch") as caught:
            self._resolve(registry, 1)
        detail = caught.value.detail
        assert detail["observability"] == "credential.binding_revision_mismatch"
        assert detail["authorized_binding_revision"] == 1
        assert detail["live_binding_revision"] == 2
        assert detail["credential_ref"] == ENV_REF  # the ref MATCHED — the revision did not

    def test_a_rebind_to_a_different_ref_keeps_the_rotation_reasons(self):
        """The changed-ref case is SEPARATE: the presented ref no longer
        matches, so the typed rotation reasons own the refusal."""
        registry = _bound_registry()
        registry.bind(SUBJECT, "anthropic-gateway", "vault:kv/eng#42", bound_by="ops@a")
        with pytest.raises(CredentialRefusal, match="rotated"):
            resolve_dispatch_credential(
                registry,
                subject=SUBJECT,
                provider="anthropic-gateway",
                presented_ref=ENV_REF,
                presented_revision=1,
            )

    def test_the_explicit_unknown_revision_grandfathers_one_named_branch(self):
        """The adapter's marker skips the comparison — the documented
        pre-revision grandfather, never an inferred pass: the marker is
        the ONLY value that does."""
        registry = _bound_registry()
        registry.revoke(SUBJECT, "anthropic-gateway", revoked_by="ops@a")
        registry.bind(SUBJECT, "anthropic-gateway", ENV_REF, bound_by="ops@a")
        assert self._resolve(registry, BINDING_REVISION_UNKNOWN).binding_revision == 2

    def test_no_presented_revision_skips_the_axis(self):
        """Callers that predate the axis (plain dispatch staging) verify
        exactly as they always did."""
        assert self._resolve(_bound_registry(), None).binding_revision == 1
