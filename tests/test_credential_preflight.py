"""R42-03 / issue #376: the credential-delivery preflight.

The recorded #364 basis, as tests:

- ``check_harness_lanes`` understood ambient ``DRIVER_CREDENTIAL_VARS``
  names only — the live run provisioned a DUPLICATE ambient secret just
  to keep doctor green. THE HEADLINE ARM: a native-carrier-only install
  (no ambient duplicate) passes preflight.
- Protected CI variables do not reach unprotected factory refs
  (docs/research/2026-09-27-gitlab-pipeline-sources/README.md) — a
  protected-only carrier with an unprotected-ref profile gets the
  precise incompatibility report BEFORE any model call.
- An empty write scope classifies every /fix as material — the scope
  preflight surfaces the onboarding action, and the empty/malformed
  scope never widens permissions.

And the structural property: doctor and dispatch resolve through THE
SAME CredentialDeliveryPlan resolver — a disconnected consumer mapping
fails these tests.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any


import forge.adaptive.credential_broker as credential_broker
import forge.adaptive.credential_preflight as preflight
from forge.adaptive.credential_broker import (
    DELIVERY_MODE_GITLAB_PROTECTED,
    DELIVERY_MODE_RUNNER_REDEMPTION,
    EnvBroker,
    credential_secret_name,
)
from forge.adaptive.credential_preflight import (
    CARRIER_REF_INCOMPATIBLE_REMEDIES,
    PREFLIGHT_OUTCOME_CODES,
    CarrierMetadata,
    CredentialPreflightReport,
    REF_CLASS_PROTECTED,
    REF_CLASS_UNPROTECTED,
    REF_CLASS_UNKNOWN,
    credential_delivery_preflight,
    native_carrier_prerequisites,
    project_subject_for_diagnostics,
    resolve_delivery_plan,
    review_feedback_scope_status,
    route_consumer_match,
    substituted_ambient_var,
)
from forge.adaptive.operator_snapshot import CanonicalSubject
from forge.adaptive.project_credentials import ProjectCredentialRegistry

REPO = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = str(REPO / "ci" / "templates")

SUBJECT = CanonicalSubject(provider_family="gitlab", connection="gitlab.example", native_id="160")
OTHER_SUBJECT = CanonicalSubject(
    provider_family="gitlab", connection="gitlab.example", native_id="161"
)
CREDENTIAL_REF = "env:ANTHROPIC_AUTH_TOKEN"
CARRIER = credential_secret_name(CREDENTIAL_REF)
SENTINEL_SECRET = "sk-sentinel-do-not-print-9f1c"


def _native_env(**extra: str) -> dict[str, str]:
    env = {
        "FORGE_CREDENTIAL_DELIVERY": DELIVERY_MODE_GITLAB_PROTECTED,
        "FORGE_CREDENTIAL_TEMPLATE_DIR": TEMPLATE_DIR,
    }
    env.update(extra)
    return env


def _redemption_env(**extra: str) -> dict[str, str]:
    env = {
        "FORGE_CREDENTIAL_DELIVERY": DELIVERY_MODE_RUNNER_REDEMPTION,
        "FORGE_CREDENTIAL_TEMPLATE_DIR": TEMPLATE_DIR,
    }
    env.update(extra)
    return env


def _bound_registry() -> ProjectCredentialRegistry:
    registry = ProjectCredentialRegistry()
    registry.bind(SUBJECT, "anthropic-gateway", CREDENTIAL_REF, bound_by="test")
    return registry


class _NoResolveBroker(EnvBroker):
    """A broker whose resolve() is a tripwire: the PREFLIGHT must never
    redeem a value (a diagnostic resolution is metadata/references
    only) — any resolve call fails the test loudly."""

    def __init__(self) -> None:
        super().__init__()
        self.resolve_calls = 0

    async def resolve(self, credential_ref: str, *, grant: Any = None) -> Any:
        self.resolve_calls += 1
        raise AssertionError("the preflight redeemed a credential value")


async def _report(
    *,
    env: dict[str, str] | None = None,
    registry: ProjectCredentialRegistry | None = None,
    carrier_metadata: dict[str, CarrierMetadata] | None = None,
    ref_class: str = REF_CLASS_UNPROTECTED,
    executor: str = "claude-code",
    subject: CanonicalSubject = SUBJECT,
    broker: Any = None,
) -> CredentialPreflightReport:
    return await credential_delivery_preflight(
        executor=executor,
        profile="gitlab",
        subject=subject,
        carrier_metadata=carrier_metadata,
        ref_class=ref_class,
        registry=registry if registry is not None else _bound_registry(),
        broker=broker,
        environ=env if env is not None else _native_env(),
    )


# ----------------------------------------------------------------------
# THE structural property: one resolver, two consumers.
# ----------------------------------------------------------------------


class TestOneResolverTwoConsumers:
    async def test_the_preflight_resolver_is_the_dispatch_seam(self):
        # The very object the dispatch legs import and call — not a
        # re-implementation. A forked/copied resolution disconnects here.
        assert preflight.delivery_plan is credential_broker.delivery_plan

    async def test_the_doctor_resolution_equals_the_dispatch_resolution(self):
        registry = _bound_registry()
        env = _native_env()
        broker = EnvBroker()
        dispatch_plan = await credential_broker.delivery_plan(
            registry,
            broker,
            subject=SUBJECT,
            provider_route="anthropic-gateway",
            profile="gitlab",
            driver="claude-code",
            environ=dict(env),
        )
        report = await _report(env=env, registry=registry)
        assert dispatch_plan is not None and report.plan is not None
        assert report.plan.mode == dispatch_plan.mode
        assert report.plan.transport_ref == dispatch_plan.transport_ref
        assert report.plan.env_var == dispatch_plan.env_var
        assert report.plan.dispatch_ref == dispatch_plan.dispatch_ref
        assert report.plan.credential_ref == dispatch_plan.credential_ref

    async def test_a_disconnected_mapping_fails_the_doctor_resolution(self, monkeypatch):
        """If the preflight stops routing through the shared seam (its
        own mode table, a copied resolver), this arm fails: the sentinel
        refusal must surface through the doctor-shaped call."""

        async def _sentinel(*args: Any, **kwargs: Any) -> Any:
            raise credential_broker.CredentialRefusal(
                "sentinel_mapping_disconnect", {"seam": "delivery_plan"}
            )

        monkeypatch.setattr(preflight, "delivery_plan", _sentinel)
        report = await _report()
        assert report.plan is None
        assert report.refusal_code == "sentinel_mapping_disconnect"
        assert report.prerequisite.outcome == "fail"
        assert report.prerequisite.code == "sentinel_mapping_disconnect"

    async def test_a_mode_table_fork_fails_the_dispatch_parity(self):
        """The broker's own PROFILE_DELIVERY_MODES is the mode truth:
        the rule table covers exactly its modes plus the legacy posture
        — a new broker mode without a rule row (or a rule row for a mode
        the seam would never select) disconnects here."""
        broker_modes = set()
        for modes in credential_broker.PROFILE_DELIVERY_MODES.values():
            broker_modes |= set(modes)
        table_modes = {row[0] for row in preflight.MODE_RULE_TABLE}
        assert table_modes == broker_modes | {preflight.LEGACY_DELIVERY_MODE}

    async def test_the_diagnostic_resolution_never_redeems(self):
        broker = _NoResolveBroker()
        report = await _report(broker=broker)
        assert report.plan is not None
        assert broker.resolve_calls == 0

    async def test_the_consumer_mapping_disconnect_is_detected(self):
        # A claude-code plan (anthropic slot) checked against the
        # opencode executor (zai slot) is the disconnect the
        # credential.route_consumer_match axis exists to catch.
        report = await _report()
        assert report.consumer_match is True
        ok, code = route_consumer_match("opencode", report.plan)
        assert ok is False
        assert code == "consumer_mapping_mismatch"
        ok, code = route_consumer_match("claude-code", None)
        assert ok is True  # the legacy posture maps by construction

    def test_the_executor_naming_no_route_is_honestly_unmatched(self):
        ok, code = route_consumer_match("totally-unknown-driver", None)
        assert ok is False
        assert code == "consumer_route_unnamed"


# ----------------------------------------------------------------------
# Native mode: the carrier + the masked/protected/ref compatibility.
# ----------------------------------------------------------------------


class TestNativeCarrierRule:
    async def test_the_headline_arm_carrier_only_without_the_ambient_duplicate(self):
        """THE #364 lesson: a masked (not protected) carrier on
        unprotected factory refs passes — the ambient
        ANTHROPIC_AUTH_TOKEN duplicate is not required."""
        report = await _report(
            carrier_metadata={CARRIER: CarrierMetadata(exists=True, protected=False, masked=True)},
            ref_class=REF_CLASS_UNPROTECTED,
        )
        assert report.plan is not None
        assert report.plan.mode == DELIVERY_MODE_GITLAB_PROTECTED
        assert report.plan.transport_ref == CARRIER
        assert report.prerequisite.outcome == "pass"
        assert report.prerequisite.code == "native_carrier_ready"
        # the substitution is exactly the provider's ambient slot
        assert substituted_ambient_var(report) == "ANTHROPIC_AUTH_TOKEN"

    async def test_protected_carrier_passes_on_protected_refs(self):
        report = await _report(
            carrier_metadata={CARRIER: CarrierMetadata(exists=True, protected=True, masked=True)},
            ref_class=REF_CLASS_PROTECTED,
        )
        assert report.prerequisite.outcome == "pass"

    async def test_removing_only_the_carrier_with_an_unrelated_ambient_key_fails_precisely(
        self,
    ):
        """An unrelated ambient key (a ZAI slot on an anthropic route)
        does not satisfy the native carrier: the failure names the
        carrier and the provisioning step."""
        report = await _report(
            carrier_metadata={"ZAI_API_KEY": CarrierMetadata(exists=True)},
            ref_class=REF_CLASS_UNPROTECTED,
        )
        assert report.prerequisite.outcome == "fail"
        assert report.prerequisite.code == "native_carrier_absent"
        assert CARRIER in report.prerequisite.detail
        assert "ZAI_API_KEY" not in report.prerequisite.detail
        # a failed prerequisite never substitutes the ambient name
        assert substituted_ambient_var(report) == ""

    async def test_a_protected_only_carrier_with_unprotected_refs_is_incompatible(self):
        """The docs-confirmable rule: MR pipelines do not have access to
        protected variables — the incompatibility report lands BEFORE
        any model call (zero broker resolutions, zero provider calls)."""
        broker = _NoResolveBroker()
        report = await _report(
            carrier_metadata={CARRIER: CarrierMetadata(exists=True, protected=True, masked=True)},
            ref_class=REF_CLASS_UNPROTECTED,
            broker=broker,
        )
        assert report.prerequisite.outcome == "fail"
        assert report.prerequisite.code == "carrier_ref_incompatible"
        assert "protected" in report.prerequisite.detail
        for remedy in CARRIER_REF_INCOMPATIBLE_REMEDIES.split(" or "):
            assert remedy.split(" (")[0] in report.prerequisite.detail
        assert broker.resolve_calls == 0  # BEFORE model calls — no redemption attempted
        assert substituted_ambient_var(report) == ""

    async def test_inaccessible_metadata_is_unknown_never_success(self):
        report = await _report(carrier_metadata=None, ref_class=REF_CLASS_UNPROTECTED)
        assert report.prerequisite.outcome == "unknown"
        assert report.prerequisite.code == "carrier_metadata_unknown"
        assert substituted_ambient_var(report) == ""

    async def test_an_unobservable_compatibility_axis_is_unknown(self):
        # The carrier exists but the protected flag never came back
        # (a degraded listing): unknown, not a pass, not a fail.
        report = await _report(
            carrier_metadata={CARRIER: CarrierMetadata(exists=True, protected=None, masked=True)},
            ref_class=REF_CLASS_UNPROTECTED,
        )
        assert report.prerequisite.outcome == "unknown"
        assert report.prerequisite.code == "carrier_compatibility_unknown"

    async def test_an_unobservable_ref_class_is_unknown(self):
        report = await _report(
            carrier_metadata={CARRIER: CarrierMetadata(exists=True, protected=True, masked=True)},
            ref_class=REF_CLASS_UNKNOWN,
        )
        assert report.prerequisite.outcome == "unknown"
        assert report.prerequisite.code == "carrier_compatibility_unknown"

    async def test_an_unmasked_carrier_is_a_hygiene_warning_not_a_failure(self):
        report = await _report(
            carrier_metadata={CARRIER: CarrierMetadata(exists=True, protected=False, masked=False)},
            ref_class=REF_CLASS_UNPROTECTED,
        )
        assert report.prerequisite.outcome == "warn"
        assert report.prerequisite.code == "carrier_not_masked"
        # a passing-with-warning carrier still substitutes (the route works)
        assert substituted_ambient_var(report) == "ANTHROPIC_AUTH_TOKEN"

    async def test_the_carrier_metadata_reader_never_carries_values(self):
        row = {
            "key": CARRIER,
            "value": SENTINEL_SECRET,
            "protected": True,
            "masked": True,
        }
        carrier = CarrierMetadata.from_variable_document(row)
        assert carrier == CarrierMetadata(exists=True, protected=True, masked=True)
        assert SENTINEL_SECRET not in repr(carrier)
        assert CarrierMetadata.from_variable_document("not-a-mapping") == CarrierMetadata(
            exists=True, protected=None, masked=None
        )

    async def test_the_github_profile_has_no_protected_axis_to_check(self):
        # The rule is GitLab's (the docs state it there); a GitHub
        # carrier passes on existence — the axis honestly does not apply.
        registry = _bound_registry()
        plan = await credential_broker.delivery_plan(
            registry,
            EnvBroker(),
            subject=SUBJECT,
            provider_route="anthropic-gateway",
            profile="github",
            driver="claude-code",
            environ={
                "FORGE_CREDENTIAL_DELIVERY": "github-native-secret",
                "FORGE_CREDENTIAL_TEMPLATE_DIR": TEMPLATE_DIR,
            },
        )
        assert plan is not None
        outcome = native_carrier_prerequisites(
            plan,
            carrier=CarrierMetadata(exists=True, protected=False, masked=True),
            ref_class=REF_CLASS_UNPROTECTED,
        )
        assert outcome.outcome == "pass"

    async def test_the_azure_group_mode_reports_its_own_carrier_rule(self):
        rows = {row[0] for row in preflight.MODE_RULE_TABLE}
        assert {
            credential_broker.DELIVERY_MODE_AZURE_GROUP,
            credential_broker.DELIVERY_MODE_GITHUB_NATIVE,
            DELIVERY_MODE_GITLAB_PROTECTED,
            DELIVERY_MODE_RUNNER_REDEMPTION,
            preflight.LEGACY_DELIVERY_MODE,
        } <= rows


# ----------------------------------------------------------------------
# Redemption mode: grants/endpoints configured — a reference proves
# nothing about the key.
# ----------------------------------------------------------------------


class TestRedemptionMode:
    async def test_configured_grant_window_and_endpoint_ttl_pass(self):
        report = await _report(env=_redemption_env(), ref_class=REF_CLASS_UNPROTECTED)
        assert report.plan is not None
        assert report.plan.mode == DELIVERY_MODE_RUNNER_REDEMPTION
        assert report.prerequisite.outcome == "pass"
        assert report.prerequisite.code == "redemption_prerequisites_ready"
        assert "/lane/credentials/redeem" in report.prerequisite.detail
        # never claims a reference proves a usable provider key
        assert "never" in report.prerequisite.detail
        assert "usable provider key" in report.prerequisite.detail

    async def test_a_malformed_grant_window_fails_typed(self):
        report = await _report(env=_redemption_env(FORGE_CREDENTIAL_GRANT_WINDOW_SECONDS="soon"))
        assert report.prerequisite.outcome == "fail"
        assert report.prerequisite.code == "redemption_grant_window_invalid"

    async def test_a_malformed_endpoint_ttl_fails_typed(self):
        report = await _report(env=_redemption_env(FORGE_CREDENTIAL_REDEEM_TTL_SECONDS="-5"))
        assert report.prerequisite.outcome == "fail"
        assert report.prerequisite.code == "redemption_ttl_invalid"

    async def test_redemption_substitutes_the_ambient_name_too(self):
        # The redeemed value lands in the provider slot at lane startup —
        # the ambient name is not this lane's consumer route either.
        report = await _report(env=_redemption_env())
        assert substituted_ambient_var(report) == "ANTHROPIC_AUTH_TOKEN"


# ----------------------------------------------------------------------
# Legacy mode: the explicitly chosen policy, and mode-specific errors
# distinct from expiry and availability.
# ----------------------------------------------------------------------


class TestLegacyModeAndErrorTaxonomy:
    async def test_an_unbound_subject_reports_the_explicit_legacy_policy(self):
        report = await _report(
            registry=ProjectCredentialRegistry(),
            subject=OTHER_SUBJECT,
            env=_native_env(),
        )
        assert report.plan is None
        assert report.delivery_mode == preflight.LEGACY_DELIVERY_MODE
        assert report.prerequisite.outcome == "pass"
        assert report.prerequisite.code == "legacy_policy_ambient"
        assert "compat" in report.prerequisite.detail
        assert substituted_ambient_var(report) == ""

    async def test_a_bound_subject_with_no_declared_route_refuses_typed(self):
        report = await _report(env={"FORGE_CREDENTIAL_TEMPLATE_DIR": TEMPLATE_DIR})
        assert report.plan is None
        assert report.refusal_code == "delivery_route_unsupported"
        assert report.prerequisite.outcome == "fail"
        assert report.delivery_mode == "delivery_route_unsupported"

    async def test_two_declared_supported_modes_are_ambiguous(self):
        both = f"{DELIVERY_MODE_GITLAB_PROTECTED},{DELIVERY_MODE_RUNNER_REDEMPTION}"
        report = await _report(env=_native_env(FORGE_CREDENTIAL_DELIVERY=both))
        assert report.refusal_code == "delivery_route_ambiguous"
        assert report.prerequisite.outcome == "fail"

    async def test_strict_broker_refuses_an_unbound_subject_typed(self):
        report = await _report(
            registry=_bound_registry(),
            subject=OTHER_SUBJECT,
            env=_native_env(FORGE_CREDENTIAL_POLICY="strict-broker"),
        )
        assert report.refusal_code == "strict_unbound_route"
        assert report.prerequisite.outcome == "fail"

    def test_the_failure_codes_are_distinct_from_expiry_and_availability(self):
        """A preflight failure names the delivery ROUTE's problem — the
        vocabulary is disjoint from credential-expiry (grant_expired,
        superseded generations) and provider-availability spellings."""
        expiry_and_availability = {
            "grant_expired",
            "env_absent",
            "credential_redemption_failed",
            "provider_unavailable",
            "litellm_unreachable",
            "expired",
        }
        surfaced = set(PREFLIGHT_OUTCOME_CODES) | {
            "delivery_route_unsupported",
            "delivery_route_ambiguous",
            "delivery_template_unavailable",
            "strict_unbound_route",
        }
        assert not (surfaced & expiry_and_availability)
        # the broker's own expiry spelling stays outside the table
        assert "grant_expired" not in surfaced


# ----------------------------------------------------------------------
# Zero secrets, zero reversible fingerprints.
# ----------------------------------------------------------------------


class TestZeroSecrets:
    async def test_no_secret_value_or_fingerprint_reaches_the_report(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", SENTINEL_SECRET)
        report = await _report(
            carrier_metadata={CARRIER: CarrierMetadata(exists=True, protected=False, masked=True)},
        )
        rendered = report.summary() + report.prerequisite.detail + report.consumer
        rendered += repr(asdict(report.prerequisite))
        assert SENTINEL_SECRET not in rendered
        # no sha/md5-style fingerprint of the material either
        import hashlib

        digest = hashlib.sha256(SENTINEL_SECRET.encode()).hexdigest()
        for prefix in (digest[:16], digest[:12], digest[:8]):
            assert prefix not in rendered

    async def test_the_plan_document_is_references_only(self):
        report = await _report(
            carrier_metadata={CARRIER: CarrierMetadata(exists=True, protected=False, masked=True)},
        )
        assert report.plan is not None
        document = report.plan.as_document()
        assert SENTINEL_SECRET not in repr(document)
        assert "staged_env" not in document  # the value slot has no plan-side key at all


# ----------------------------------------------------------------------
# The review-feedback scope preflight.
# ----------------------------------------------------------------------


class TestReviewScopePreflight:
    def test_feature_off_owes_nothing(self):
        status = review_feedback_scope_status(
            feedback_enabled=False, config_status="valid", implement_paths=[]
        )
        assert status.status == "pass"
        assert status.code == "feature_off"

    def test_a_declared_scope_passes(self):
        status = review_feedback_scope_status(
            feedback_enabled=True, config_status="valid", implement_paths=["src/**"]
        )
        assert status.status == "pass"
        assert status.code == "scope_declared"

    def test_a_missing_scope_gets_the_onboarding_action_not_a_ready_message(self):
        status = review_feedback_scope_status(
            feedback_enabled=True, config_status="valid", implement_paths=[]
        )
        assert status.status == "fail"
        assert status.code == "scope_missing"
        assert "implement.paths" in status.action
        assert "material" in status.detail
        assert "BEFORE readiness" in status.action

    def test_a_confirmed_absent_config_is_the_same_missing_scope_debt(self):
        status = review_feedback_scope_status(
            feedback_enabled=True, config_status="confirmed_absent", implement_paths=[]
        )
        assert status.code == "scope_missing"
        assert status.status == "fail"

    def test_a_malformed_config_is_a_separate_failure(self):
        status = review_feedback_scope_status(
            feedback_enabled=True, config_status="invalid", implement_paths=None
        )
        assert status.status == "fail"
        assert status.code == "scope_unreadable"
        assert status.code != "scope_missing"  # separately named, separately fixed

    def test_an_unobservable_scope_is_unknown(self):
        status = review_feedback_scope_status(
            feedback_enabled=True, config_status="valid", implement_paths=None
        )
        assert status.status == "unknown"
        assert status.code == "scope_unknown"

    def test_neither_empty_nor_malformed_scope_widens_permissions(self):
        """The preflight surfaces the debt; it never changes the
        classifier — an empty scope still fail-closes every /fix into a
        material proposal (the recorded behavior, deliberately kept)."""
        from forge.adaptive.revisions import (
            MATERIAL_CHANGE_CLASS,
            classify_review_feedback,
        )

        assert classify_review_feedback("fix", "edit src/app.py", allowed_paths=[]) == (
            MATERIAL_CHANGE_CLASS
        )
        assert classify_review_feedback("fix", "edit src/app.py") == MATERIAL_CHANGE_CLASS


# ----------------------------------------------------------------------
# The project-level diagnostic subject.
# ----------------------------------------------------------------------


class TestDiagnosticSubject:
    def test_the_registrys_own_binding_subject_is_found(self):
        registry = _bound_registry()
        subject = project_subject_for_diagnostics(registry, 160, "anthropic-gateway")
        assert subject.subject_id() == SUBJECT.subject_id()

    def test_no_binding_falls_back_to_the_legacy_subject(self):
        registry = _bound_registry()
        subject = project_subject_for_diagnostics(registry, 999, "anthropic-gateway")
        assert subject.subject_id() == "gitlab/-/999"

    async def test_a_revoked_binding_surfaces_as_its_own_typed_refusal(self):
        """The shared seam's fail-closed checks ride the diagnostic
        resolution too — a revoked binding is a refusal, never a legacy
        fallback (and 'revoked' stays outside the mode-failure table:
        it is a binding-state problem, not a route problem)."""
        registry = _bound_registry()
        registry.revoke(SUBJECT, "anthropic-gateway", revoked_by="test")
        report = await credential_delivery_preflight(
            executor="claude-code",
            profile="gitlab",
            subject=SUBJECT,
            registry=registry,
            environ=_native_env(),
        )
        assert report.plan is None
        assert report.refusal_code == "revoked"


# ----------------------------------------------------------------------
# The shared resolver's defaults (the deployment shape the dispatch uses).
# ----------------------------------------------------------------------


class TestResolverDefaults:
    async def test_the_deployment_registry_and_broker_are_the_dispatch_defaults(
        self, tmp_path, monkeypatch
    ):
        registry = ProjectCredentialRegistry(path=tmp_path / "bindings.json")
        registry.bind(SUBJECT, "anthropic-gateway", CREDENTIAL_REF, bound_by="test")
        monkeypatch.setenv("FORGE_CREDENTIAL_BINDINGS", str(tmp_path / "bindings.json"))
        monkeypatch.setenv("FORGE_CREDENTIAL_DELIVERY", DELIVERY_MODE_GITLAB_PROTECTED)
        monkeypatch.setenv("FORGE_CREDENTIAL_TEMPLATE_DIR", TEMPLATE_DIR)
        plan = await resolve_delivery_plan(
            subject=SUBJECT, provider_route="anthropic-gateway", profile="gitlab"
        )
        assert plan is not None
        assert plan.mode == DELIVERY_MODE_GITLAB_PROTECTED
        assert plan.transport_ref == CARRIER
