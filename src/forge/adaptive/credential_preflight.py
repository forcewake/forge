"""The credential-delivery PREFLIGHT (R42-03 / issue #376) — ONE
``CredentialDeliveryPlan`` resolver for BOTH ``forge doctor`` and the
dispatch legs, plus the per-mode prerequisite rules and the
review-feedback scope check the live #364 onboarding record demanded.

THE SAME RESOLVER, TWO CONSUMERS. The dispatch legs resolve their
delivery plan through :func:`forge.adaptive.credential_broker.delivery_plan`
(the seam ``RunService``/``GitHubRunService``/``AzureRunService`` call
before any provider call). This module does NOT re-implement that
resolution: :func:`resolve_delivery_plan` IS that function, called with
references-only inputs — the doctor's diagnostic resolution performs NO
value redemption as a side effect (``delivery_plan`` never resolves a
value; it reads the binding registry, the declared route env and the
shipped template, all metadata/references). A test pins the identity
(``credential_preflight.delivery_plan is credential_broker.delivery_plan``)
and that the doctor consults this seam — if either consumer's mapping
disconnects, the tests fail.

WHAT THE LIVE RECORD (#364) ESTABLISHED, ENCODED AS RULES:

- ``check_harness_lanes`` understood the ambient ``DRIVER_CREDENTIAL_VARS``
  names only; the live run provisioned a DUPLICATE ambient secret just to
  keep doctor green while the lane actually consumed the native
  ``FORGE_MODEL_<SEGMENT>`` carrier. RULE: a bound subject under a
  non-legacy delivery mode does not owe the ambient name — the carrier
  (native modes) or the redemption (runner-redemption) is the consumer
  route (:func:`substituted_ambient_var`).
- Protected CI variables do not reach unprotected refs (docs/research/
  2026-09-27-gitlab-pipeline-sources/README.md: "MR pipelines do not have
  access to protected variables or protected runners"). RULE
  (:func:`carrier_compatibility`): a protected-only carrier with an
  UNPROTECTED factory-ref profile is INCOMPATIBLE — reported BEFORE any
  model call, with the two documented remedies (mask-not-protect the
  carrier, or dispatch on protected refs).
- An empty write scope (no ``.forge.yml implement.paths``) classifies
  every ``/fix`` as a material change — discovered only AFTER readiness.
  RULE (:func:`review_feedback_scope_status`): a correction-enabled
  project without an explicit scope gets an ONBOARDING action at doctor
  time and at the feedback-feature validation — and the empty/malformed
  scope NEVER widens permissions (the classifier stays fail-closed).

Every outcome is REDACTED by construction: names, modes, refs and
metadata only — zero secret values, zero reversible fingerprints (the
report carries no digests of credential material at all).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from forge.adaptive.credential_broker import (
    DELIVERY_MODE_AZURE_GROUP,
    DELIVERY_MODE_GITHUB_NATIVE,
    DELIVERY_MODE_GITLAB_PROTECTED,
    DELIVERY_MODE_RUNNER_REDEMPTION,
    LANE_CREDENTIAL_REDEEM_ROUTE,
    CredentialDeliveryPlan,
    CredentialRefusal,
    EnvBroker,
    delivery_plan,
)
from forge.adaptive.operator_snapshot import CanonicalSubject
from forge.adaptive.project_credentials import (
    PROVIDER_ENV_VARS,
    ProjectCredentialRegistry,
    legacy_subject_for_project,
    provider_route_for_driver,
    registry_from_env,
)

__all__ = [
    "CARRIER_REF_INCOMPATIBLE_REMEDIES",
    "CarrierMetadata",
    "CredentialPreflightReport",
    "LEGACY_DELIVERY_MODE",
    "MODE_RULE_TABLE",
    "ModePrerequisite",
    "OUTCOME_FAIL",
    "OUTCOME_PASS",
    "OUTCOME_UNKNOWN",
    "OUTCOME_WARN",
    "PREFLIGHT_OUTCOME_CODES",
    "REF_CLASS_PROTECTED",
    "REF_CLASS_UNPROTECTED",
    "REF_CLASS_UNKNOWN",
    "ReviewScopeStatus",
    "carrier_compatibility",
    "credential_delivery_preflight",
    "delivery_plan",
    "legacy_policy_prerequisites",
    "native_carrier_prerequisites",
    "project_subject_for_diagnostics",
    "redemption_prerequisites",
    "resolve_delivery_plan",
    "review_feedback_scope_status",
    "route_consumer_match",
    "substituted_ambient_var",
    "validate_review_feedback_feature",
]

#: The delivery-mode spelling of an UNBOUND subject (the broker's
#: ``ambient-legacy`` attribution — the lane consumes its ambient
#: credential; the doctor reports the policy in force, explicitly).
LEGACY_DELIVERY_MODE = "ambient-legacy"

#: Prerequisite outcome vocabulary (distinct from PASS/WARN/FAIL check
#: statuses only by the explicit UNKNOWN: an axis that could not be
#: OBSERVED is never reported as success).
OUTCOME_PASS = "pass"
OUTCOME_WARN = "warn"
OUTCOME_FAIL = "fail"
OUTCOME_UNKNOWN = "unknown"

#: The dispatch target ref class (the factory refs
#: ``factory/<issue>/<run>`` are UNPROTECTED unless the operator
#: protected them — the axis the protected-carrier rule turns on).
REF_CLASS_PROTECTED = "protected"
REF_CLASS_UNPROTECTED = "unprotected"
REF_CLASS_UNKNOWN = "unknown"

#: The closed vocabulary of preflight outcome codes. Mode-specific by
#: construction and DISJOINT from the credential-expiry spellings
#: (``grant_expired``, superseded generations) and the
#: provider-availability spellings (``litellm``, HTTP statuses): a
#: preflight failure names the delivery ROUTE's configuration problem,
#: never a credential's validity or a provider's reachability.
PREFLIGHT_OUTCOME_CODES: frozenset[str] = frozenset(
    {
        "native_carrier_absent",
        "carrier_metadata_unknown",
        "carrier_compatibility_unknown",
        "carrier_ref_incompatible",
        "carrier_not_masked",
        "redemption_grant_window_invalid",
        "redemption_ttl_invalid",
        "redemption_prerequisites_ready",
        "native_carrier_ready",
        "legacy_policy_ambient",
        "legacy_policy_refused",
        "consumer_mapping_mismatch",
        "consumer_route_unnamed",
    }
)

#: The docs-confirmable mechanism behind the #364 protected-carrier
#: failure, as the two remedies the operator actually has.
CARRIER_REF_INCOMPATIBLE_REMEDIES = (
    "provision the carrier masked but NOT protected (the lab posture), or "
    "protect the dispatch refs (Settings → Repository → Protected branches) "
    "so MR pipelines on them can see protected variables"
)


@dataclass(frozen=True)
class CarrierMetadata:
    """The REDACTED metadata of one native carrier variable — flags only.

    ``exists`` is ``None`` when the metadata source itself was
    inaccessible (the CI-variable listing could not be read): the
    prerequisite then reports UNKNOWN, never success. ``protected`` /
    ``masked`` are ``None`` when the provider does not expose the axis
    (GitHub repo secrets carry no protected flag) or the entry omitted
    it — an unobservable axis is unknown, not false.
    """

    exists: bool | None = None
    protected: bool | None = None
    masked: bool | None = None

    @classmethod
    def from_variable_document(cls, document: Mapping[str, Any]) -> CarrierMetadata:
        """One CI-variable API row (GitLab shape) — ``key``-less flags
        only; a row without the flags reads as unknown axes, and a row
        that is not a mapping at all reads as fully unknown."""
        if not isinstance(document, Mapping):
            return cls(exists=True, protected=None, masked=None)
        return cls(
            exists=True,
            protected=_optional_flag(document.get("protected")),
            masked=_optional_flag(document.get("masked")),
        )


def _optional_flag(raw: Any) -> bool | None:
    if raw is None:
        return None
    return bool(raw)


@dataclass(frozen=True)
class ModePrerequisite:
    """One per-mode prerequisite outcome: the outcome word, the typed
    code (inside :data:`PREFLIGHT_OUTCOME_CODES`, or the broker's own
    typed refusal reason when the RESOLUTION itself refused), and the
    redacted instruction. No value, no fingerprint — ever."""

    outcome: str
    code: str
    detail: str = ""


@dataclass(frozen=True)
class CredentialPreflightReport:
    """The ONE redacted preflight report (R42-03): the selected executor,
    the target ref class, the model route, the expected credential
    consumer, and the per-mode prerequisite outcome.

    ``plan`` is the shared-resolver product (``None`` for the legacy
    ambient posture and for a typed refusal — ``refusal_code`` carries
    the broker's reason then). Rendering helpers (:meth:`summary`)
    produce operator text from refs/metadata only.
    """

    executor: str
    profile: str
    ref_class: str
    provider_route: str
    env_var: str
    delivery_mode: str
    consumer: str
    prerequisite: ModePrerequisite
    plan: CredentialDeliveryPlan | None = None
    refusal_code: str = ""
    consumer_match: bool = True
    consumer_match_code: str = ""

    def summary(self) -> str:
        """The redacted one-line report — executor, ref class, model
        route, expected consumer, mode and outcome (the observability
        spelling ``doctor.prerequisite_outcome{delivery_mode}``)."""
        route = f"{self.provider_route} → {self.env_var}" if self.provider_route else "none"
        head = (
            f"executor {self.executor}, target ref class {self.ref_class}, model route {route}, "
            f"credential consumer {self.consumer}, delivery_mode {self.delivery_mode}, "
            f"doctor.prerequisite_outcome{{delivery_mode={self.delivery_mode}}}="
            f"{self.prerequisite.outcome} [{self.prerequisite.code}]"
        )
        if self.refusal_code:
            head += f" (resolution refused: {self.refusal_code})"
        return head

    def substitutes_ambient(self) -> bool:
        """Whether this delivery outcome REPLACES the ambient-name
        requirement for its provider slot: a plan under a non-legacy
        mode whose prerequisite did not fail or stay unknown. The
        #364 rule — never require the ambient name when it is not the
        consumer route; never WAIVE it on an unproven carrier."""
        if self.plan is None or not self.env_var:
            return False
        return self.prerequisite.outcome in (OUTCOME_PASS, OUTCOME_WARN)


def substituted_ambient_var(report: CredentialPreflightReport | None) -> str:
    """The ambient variable name a delivery plan substitutes (``""``
    when none): the provider's env slot when :meth:`substitutes_ambient`
    holds. ``check_harness_lanes`` drops exactly this name from the
    driver's required ambient set — nothing wider, ever."""
    if report is None or not report.substitutes_ambient():
        return ""
    return report.env_var


# ---------------------------------------------------------------------------
# THE shared resolution seam — doctor AND dispatch, one function object.
# ---------------------------------------------------------------------------


async def resolve_delivery_plan(
    *,
    subject: CanonicalSubject,
    provider_route: str,
    profile: str,
    driver: str = "",
    registry: ProjectCredentialRegistry | None = None,
    broker: Any | None = None,
    locator_registry: Any | None = None,
    environ: Mapping[str, str] | None = None,
) -> CredentialDeliveryPlan | None:
    """Resolve the delivery plan through THE dispatch seam.

    This is :func:`forge.adaptive.credential_broker.delivery_plan` — the
    same function object the provider dispatch legs call — invoked with
    the deployment defaults the dispatch constructs (the
    ``FORGE_CREDENTIAL_BINDINGS`` registry, the ambient
    ``EnvBroker``). The diagnostic callers pass the same inputs and get
    the same plan the next dispatch would; NO value is redeemed (the
    seam reads refs, the declared route and the shipped template only —
    the broker is consulted for its identity, never resolved).
    """
    source: Mapping[str, str] = os.environ if environ is None else environ
    effective_registry = registry if registry is not None else registry_from_env(dict(source))
    if broker is None:
        broker = EnvBroker()
    return await delivery_plan(
        effective_registry,
        broker,
        subject=subject,
        provider_route=provider_route,
        profile=profile,
        environ=dict(source),
        driver=driver,
        locator_registry=locator_registry,
    )


async def credential_delivery_preflight(
    *,
    executor: str,
    profile: str,
    subject: CanonicalSubject,
    carrier_metadata: Mapping[str, CarrierMetadata] | None = None,
    ref_class: str = REF_CLASS_UNKNOWN,
    registry: ProjectCredentialRegistry | None = None,
    broker: Any | None = None,
    environ: Mapping[str, str] | None = None,
) -> CredentialPreflightReport:
    """Assemble the ONE redacted preflight report for one executor.

    *carrier_metadata* is the native-carrier view: a mapping READ from a
    CI-variable listing (a carrier absent from it is PROVEN absent), or
    ``None`` when the listing was inaccessible (existence UNKNOWN —
    never success, never a false absent). *ref_class* is the dispatch
    target ref class (:data:`REF_CLASS_UNKNOWN` when unobserved).

    Resolution first (the shared seam — a typed refusal becomes the
    report's ``refusal_code`` and a FAIL prerequisite whose code is the
    broker's own reason: ``delivery_route_unsupported``,
    ``delivery_route_ambiguous``, ``delivery_template_unavailable``,
    ``strict_unbound_route``, … — all delivery-route configuration
    problems, none of them credential-expiry or provider-availability
    spellings), then the per-mode rule, then the consumer mapping. Zero
    provider calls, zero broker resolutions, zero values.
    """
    provider_route = provider_route_for_driver(executor)
    env_var = PROVIDER_ENV_VARS.get(provider_route, "")
    source: Mapping[str, str] = os.environ if environ is None else environ
    refusal_code = ""
    plan: CredentialDeliveryPlan | None = None
    try:
        plan = await resolve_delivery_plan(
            subject=subject,
            provider_route=provider_route,
            profile=profile,
            driver=executor,
            registry=registry,
            broker=broker,
            environ=source,
        )
    except CredentialRefusal as exc:
        refusal_code = str(exc.reason)

    if plan is None and not refusal_code:
        # The legacy posture: unbound subject (or a driver naming no
        # route) under the explicitly chosen policy — never a silent
        # default, the policy is named in the outcome.
        prerequisite = legacy_policy_prerequisites(environ=source)
        mode = LEGACY_DELIVERY_MODE
        consumer = "the lane's ambient environment (no binding on this subject)"
    elif plan is None:
        prerequisite = ModePrerequisite(
            outcome=OUTCOME_FAIL,
            code=refusal_code,
            detail=(
                "the delivery plan refused typed at the shared dispatch seam — fix the "
                f"delivery-route configuration ({refusal_code}) before any dispatch; "
                "this is a routing/configuration problem, not a credential-expiry or "
                "provider-availability one"
            ),
        )
        mode = refusal_code
        consumer = "none — the dispatch parks at the credential seam"
    elif plan.mode == DELIVERY_MODE_RUNNER_REDEMPTION:
        mode = plan.mode
        prerequisite = redemption_prerequisites(environ=source)
        consumer = f"the lane redeems at {LANE_CREDENTIAL_REDEEM_ROUTE} into {plan.env_var}"
    else:
        mode = plan.mode
        # A metadata mapping the caller READ from a listing proves
        # absence: a carrier missing from it does not exist. ``None``
        # means the listing itself was inaccessible — unknown, never
        # success (and never a false "absent" either).
        if carrier_metadata is None:
            carrier = CarrierMetadata(exists=None)
        else:
            carrier = carrier_metadata.get(plan.transport_ref) or CarrierMetadata(exists=False)
        prerequisite = native_carrier_prerequisites(plan, carrier=carrier, ref_class=ref_class)
        consumer = (
            f"the CI provider's secret facility ({plan.transport_ref}) maps into "
            f"{plan.env_var} runner-side"
        )

    match_ok, match_code = route_consumer_match(executor, plan)
    return CredentialPreflightReport(
        executor=executor,
        profile=profile,
        ref_class=ref_class,
        provider_route=provider_route,
        env_var=env_var,
        delivery_mode=mode,
        consumer=consumer,
        prerequisite=prerequisite,
        plan=plan,
        refusal_code=refusal_code,
        consumer_match=match_ok,
        consumer_match_code=match_code,
    )


# ---------------------------------------------------------------------------
# The per-mode rule table.
# ---------------------------------------------------------------------------

#: The per-mode prerequisite rule, as the operator reads it (one row per
#: supported delivery mode plus the legacy posture; the functions below
#: ARE this table).
MODE_RULE_TABLE: tuple[tuple[str, str, str], ...] = (
    (
        DELIVERY_MODE_GITLAB_PROTECTED,
        "the selected FORGE_MODEL_* carrier exists as a project CI/CD variable",
        "masked/protected/ref compatibility: a protected-only carrier never serves "
        "an unprotected factory ref (MR pipelines cannot see protected variables)",
    ),
    (
        DELIVERY_MODE_GITHUB_NATIVE,
        "the selected FORGE_MODEL_* repo/org secret exists",
        "GitHub secrets carry no protected axis — existence is the checkable rule "
        "(environment protection is the GitHub analog, honestly out of this table)",
    ),
    (
        DELIVERY_MODE_AZURE_GROUP,
        "the authorized variable group holds the route's secret variable",
        "template parameters never carry secret values — the group is the carrier",
    ),
    (
        DELIVERY_MODE_RUNNER_REDEMPTION,
        "the grant window and the endpoint TTL are configured (parse)",
        "a reference NEVER proves a usable provider key — the lane proves "
        "consumption at redemption; this check claims configuration only",
    ),
    (
        LEGACY_DELIVERY_MODE,
        "the explicitly chosen legacy policy is reported (compat/strict-broker)",
        "an unbound subject under compat keeps the ambient lane, labeled; under "
        "strict-broker a registry holding bindings refuses the route typed",
    ),
)


def _first_line(exc: BaseException) -> str:
    """The redacted first line of an exception's message (empty → the
    class name) — the detail text budget for a typed prerequisite."""
    text = str(exc).strip()
    return (text.splitlines()[0] if text else exc.__class__.__name__)[:200]


def carrier_compatibility(
    carrier: CarrierMetadata, *, ref_class: str, profile: str
) -> ModePrerequisite:
    """The protected-carrier compatibility rule (the #364 lesson, encoded).

    Docs-confirmable mechanism (docs/research/2026-09-27-gitlab-pipeline-
    sources/README.md): "MR pipelines do not have access to protected
    variables or protected runners" — so on the GitLab profile a
    PROTECTED-only carrier is invisible to pipelines on UNPROTECTED
    factory refs, and the lane would fail its bootstrap fence after the
    dispatch was already paid for. The rule fires BEFORE any model call:

    - protected carrier + unprotected ref → ``carrier_ref_incompatible``
      with the two remedies (this is a FAIL, distinct from every
      expiry/availability spelling);
    - the protected axis or the ref class UNOBSERVED → unknown, never
      success (``carrier_compatibility_unknown``);
    - the rule is profile-scoped to GitLab (where the docs state it);
      GitHub/Azure carriers carry no protected axis to check.
    """
    if profile != "gitlab":
        return ModePrerequisite(OUTCOME_PASS, "native_carrier_ready", "carrier present")
    if carrier.protected is None or ref_class == REF_CLASS_UNKNOWN:
        return ModePrerequisite(
            OUTCOME_UNKNOWN,
            "carrier_compatibility_unknown",
            "the carrier's protected flag or the target ref class could not be "
            "observed — the compatibility verdict is unknown, never success",
        )
    if carrier.protected and ref_class == REF_CLASS_UNPROTECTED:
        return ModePrerequisite(
            OUTCOME_FAIL,
            "carrier_ref_incompatible",
            "a PROTECTED carrier cannot serve pipelines on UNPROTECTED refs — MR "
            "pipelines do not have access to protected variables (the #364 live "
            f"failure); either {CARRIER_REF_INCOMPATIBLE_REMEDIES}",
        )
    return ModePrerequisite(OUTCOME_PASS, "native_carrier_ready", "carrier present")


def native_carrier_prerequisites(
    plan: CredentialDeliveryPlan,
    *,
    carrier: CarrierMetadata,
    ref_class: str = REF_CLASS_UNKNOWN,
) -> ModePrerequisite:
    """The native-mode prerequisite: the SELECTED carrier exists, plus
    the masked/protected/ref compatibility rule.

    Metadata inaccessibility is UNKNOWN, never success
    (``carrier_metadata_unknown``); a missing carrier is
    ``native_carrier_absent`` naming the carrier and the provisioning
    step; an unmasked-but-present carrier is a WARN (log-exposure
    hygiene — distinct from the compatibility FAIL).
    """
    name = plan.transport_ref
    if carrier.exists is None:
        return ModePrerequisite(
            OUTCOME_UNKNOWN,
            "carrier_metadata_unknown",
            f"the CI-variable metadata for the carrier {name} could not be read — "
            "existence unproven, reported unknown, never success",
        )
    if not carrier.exists:
        return ModePrerequisite(
            OUTCOME_FAIL,
            "native_carrier_absent",
            f"the selected native carrier {name} does not exist on the target — "
            "provision it once with the credential value (Settings → CI/CD → "
            "Variables; the lane derives this name from the dispatched ref); an "
            "unrelated ambient variable does not satisfy this route",
        )
    compatibility = carrier_compatibility(carrier, ref_class=ref_class, profile=plan.profile)
    if compatibility.outcome != OUTCOME_PASS:
        return compatibility
    if carrier.masked is False:
        return ModePrerequisite(
            OUTCOME_WARN,
            "carrier_not_masked",
            f"the carrier {name} exists and is ref-compatible but NOT masked — "
            "mask it so job logs cannot echo the value (hygiene; the lane still "
            "consumes it correctly)",
        )
    masked_note = "masked" if carrier.masked else "masking unobserved"
    return ModePrerequisite(
        OUTCOME_PASS,
        "native_carrier_ready",
        f"the selected carrier {name} exists ({masked_note}) and is ref-compatible "
        f"with the {ref_class} dispatch refs",
    )


def redemption_prerequisites(*, environ: Mapping[str, str] | None = None) -> ModePrerequisite:
    """The redemption-mode prerequisite: grant/endpoint CONFIGURATION.

    Checks that the grant window
    (:func:`~forge.adaptive.credential_broker.operation_grant_window_seconds`)
    and the endpoint TTL
    (:func:`~forge.api_lane_control.redemption_ttl_seconds`) are
    configured and parse. It NEVER claims the credential ref proves a
    usable provider key: a reference is an address, not a validation —
    the lane proves consumption at redemption time, and an expired or
    wrong key refuses there with its own typed codes (``grant_expired``
    and friends), which is exactly why this preflight's failure codes
    stay configuration-shaped.
    """
    from forge.adaptive.credential_broker import operation_grant_window_seconds
    from forge.api_lane_control import redemption_ttl_seconds

    try:
        window = operation_grant_window_seconds(environ)
    except CredentialRefusal as exc:
        return ModePrerequisite(OUTCOME_FAIL, "redemption_grant_window_invalid", _first_line(exc))
    try:
        ttl = redemption_ttl_seconds(environ)
    except ValueError as exc:  # LegacyWindowInvalid — the TTL's typed failure
        return ModePrerequisite(OUTCOME_FAIL, "redemption_ttl_invalid", _first_line(exc))
    return ModePrerequisite(
        OUTCOME_PASS,
        "redemption_prerequisites_ready",
        f"grant window {window:g}s and endpoint TTL {ttl:g}s configured for "
        f"{LANE_CREDENTIAL_REDEEM_ROUTE} — configuration only: a reference never "
        "proves a usable provider key (consumption is proven at redemption)",
    )


def legacy_policy_prerequisites(*, environ: Mapping[str, str] | None = None) -> ModePrerequisite:
    """The legacy posture's prerequisite: the policy is EXPLICITLY
    reported (compat keeps today's labeled ambient lane;
    strict-broker's refusals surface through the shared seam as the
    report's typed ``refusal_code``)."""
    from forge.adaptive.credential_broker import (
        CREDENTIAL_POLICY_COMPAT,
        CREDENTIAL_POLICY_ENV,
        credential_policy,
    )

    try:
        policy = credential_policy(environ)
    except CredentialRefusal as exc:
        return ModePrerequisite(OUTCOME_FAIL, "legacy_policy_refused", _first_line(exc))
    return ModePrerequisite(
        OUTCOME_PASS if policy == CREDENTIAL_POLICY_COMPAT else OUTCOME_WARN,
        "legacy_policy_ambient",
        f"no binding on this subject — the ambient-legacy lane under the explicit "
        f"policy {policy} ({CREDENTIAL_POLICY_ENV}); bind the subject's route to "
        "move to a delivery plan",
    )


def route_consumer_match(executor: str, plan: CredentialDeliveryPlan | None) -> tuple[bool, str]:
    """Whether the selected executor's credential-consumption route
    agrees with the resolved plan's consumer slot — the
    ``credential.route_consumer_match`` observability axis. A driver
    naming no route is honestly unmatched (``consumer_route_unnamed``);
    a plan slot differing from the driver's provider slot is the
    consumer-mapping disconnect (``consumer_mapping_mismatch``)."""
    provider_route = provider_route_for_driver(executor)
    if not provider_route:
        return False, "consumer_route_unnamed"
    expected = PROVIDER_ENV_VARS.get(provider_route, "")
    if plan is None:
        return True, ""  # the legacy posture maps by construction (ambient slot)
    if plan.env_var == expected:
        return True, ""
    return False, "consumer_mapping_mismatch"


# ---------------------------------------------------------------------------
# The review-feedback SCOPE preflight (the third #364 lesson).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewScopeStatus:
    """The correction-scope readiness of one target project: the
    status word, the typed code, and the ONBOARDING action owed ("" —
    nothing owed). The scope NEVER widens permissions: this status only
    surfaces the debt; ``classify_review_feedback`` stays fail-closed
    on an empty or malformed scope exactly as before."""

    status: str
    code: str
    action: str = ""
    detail: str = ""


def review_feedback_scope_status(
    *,
    feedback_enabled: bool,
    config_status: str,
    implement_paths: list[str] | tuple[str, ...] | None,
) -> ReviewScopeStatus:
    """The scope rule for a correction-enabled project (R42-03).

    - feature OFF → nothing owed (the verbs are not even parsed);
    - feature ON + the ``.forge.yml`` read failed (unreadable/invalid)
      → ``scope_unreadable``: the scope is UNKNOWN, the config must be
      repaired — never widened, never treated as declared;
    - feature ON + config readable + ``implement.paths`` EMPTY →
      ``scope_missing``: every ``/fix`` will classify as a material
      change (the recorded #364 surprise, learned only after
      readiness) — the ONBOARDING action names the declaration;
    - feature ON + a nonempty declared scope → ``scope_declared``.

    An explicitly declared EMPTY list is the same debt as a missing
    declaration (``[]`` is pydantic's normalization of an absent key
    and of an explicitly empty one — both leave the classifier with
    nothing to prove scope against; fail-closed either way).
    """
    if not feedback_enabled:
        return ReviewScopeStatus(
            OUTCOME_PASS,
            "feature_off",
            detail="review feedback verbs are not parsed (FORGE_REVIEW_FEEDBACK_ENABLED off)",
        )
    if config_status in ("unreadable", "invalid"):
        return ReviewScopeStatus(
            OUTCOME_FAIL,
            "scope_unreadable",
            action=(
                "repair the target repo's .forge.yml — its restrictions are unknown, "
                "so no /fix scope can be proven while it stays unreadable"
            ),
            detail=f"the .forge.yml read status is {config_status}",
        )
    if implement_paths is None:
        return ReviewScopeStatus(
            OUTCOME_UNKNOWN,
            "scope_unknown",
            action="re-run the scope check with the project's config readable",
            detail="the permitted-path contract could not be observed",
        )
    if not [entry for entry in implement_paths if str(entry).strip()]:
        return ReviewScopeStatus(
            OUTCOME_FAIL,
            "scope_missing",
            action=(
                "declare the correction write scope in the target repo's .forge.yml "
                "(implement.paths: the glob list a /fix may touch) — without it every "
                "/fix classifies as a material change and needs the material-revision "
                "route; onboarding the scope BEFORE readiness is the fix"
            ),
            detail=(
                "corrections are enabled but no implement.paths is declared — the "
                "classifier fail-closes every /fix into a material proposal"
            ),
        )
    declared = len([entry for entry in implement_paths if str(entry).strip()])
    return ReviewScopeStatus(
        OUTCOME_PASS,
        "scope_declared",
        detail=f"{declared} implement.paths glob(s) declared — /fix scope is provable",
    )


async def validate_review_feedback_feature(
    *,
    feedback_enabled: bool,
    config_status: str,
    implement_paths: list[str] | tuple[str, ...] | None,
) -> ReviewScopeStatus:
    """The feedback-feature validation seam (R42-03): the feature-level
    check a deployment/onboarding surface calls to answer "is this
    project ready for corrections" — the SAME status function the
    doctor's ``onboarding.review_scope`` check renders
    (:func:`review_feedback_scope_status`), exposed as its own entry
    point so the feature's validation and the doctor's report cannot
    drift apart (one function, two named consumers)."""
    return review_feedback_scope_status(
        feedback_enabled=feedback_enabled,
        config_status=config_status,
        implement_paths=implement_paths,
    )


# ---------------------------------------------------------------------------
# Doctor-side helpers: the metadata the report consumes (names/flags only).
# ---------------------------------------------------------------------------
# The report's inputs are metadata the CALLER observes (doctor reads the
# CI-variable listing and the protected-branch set through its existing
# GitLab client): names, boolean flags and ref classes — never values.
# The module stays free of provider I/O so every rule above is testable
# as a pure function of redacted metadata.


def project_subject_for_diagnostics(
    registry: ProjectCredentialRegistry,
    project_id: int,
    provider_route: str,
) -> CanonicalSubject:
    """The subject a project-scoped diagnostic resolves under.

    The dispatch resolves under the RUN's own canonical subject; a
    project-level preflight has no run, so the honest approximation is
    the registry's OWN binding subject for this project + route (any
    connection — scanned from refs/metadata only, sorted for
    determinism), falling back to the legacy default-connection subject
    when the registry holds none (the resolution then reports the
    legacy posture, exactly as an unbound dispatch would)."""
    from forge.adaptive.operator_snapshot import subject_from_ref

    if provider_route:
        candidates: list[str] = []
        for subject_id, provider in registry.bindings:
            if provider != provider_route:
                continue
            try:
                subject = subject_from_ref(subject_id)
            except ValueError:
                continue  # an unparseable row is skipped, never guessed
            if subject.provider_family == "gitlab" and subject.native_id == str(int(project_id)):
                candidates.append(subject_id)
        if candidates:
            return subject_from_ref(sorted(candidates)[0])
    return legacy_subject_for_project(project_id)
