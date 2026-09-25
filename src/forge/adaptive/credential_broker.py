"""The credential broker contract (NEXT-19 / issue #207) — the resolution
step between a project's bound credential REF and the env slot a
dispatched lane actually consumes.

The binding side (:mod:`forge.adaptive.project_credentials`) persists
REFS, never values — ``vault:kv/eng#42``, ``env:ANTHROPIC_AUTH_TOKEN``.
This module owns the other half: WHO turns a ref into a staged
credential at dispatch time, and WHAT the audit trail may say about it.

- :class:`CredentialBroker` — the protocol: ``resolve(ref, grant)`` →
  :class:`ResolvedCredential(version, staged_env, receipt)`. The
  ``staged_env`` is the ONLY credential material the dispatch moves
  into the lane's variable set; the ``receipt`` is refs/metadata ONLY —
  the binding revision, the resolver identity, the provider route, the
  resolved version. NEVER the value, and never a hash of a low-entropy
  secret (a hash of an API key is a lookup table entry, not a proof).
- :class:`EnvBroker` — the default resolver: an ``env:<VAR_NAME>`` ref
  resolves from the process environment (today's ambient behavior, now
  an explicit, auditable decision). Its receipt records the env-var
  NAME and PRESENCE — never the content. External brokers (Vault et
  al.) implement the same protocol later; no vendor lock lives here.
- :class:`StagedBroker` — the deterministic test double for fixtures.
- :func:`stage_dispatch_credential` — the DISPATCH-SEAM composition the
  provider dispatch legs call under their active execution lease/grant,
  BEFORE any provider call: the registry's fail-closed checks (live
  binding, revocation, rotation, wrong-subject ref) run first, then the
  broker resolves, then the staged slot is verified against the
  binding's env var (a broker staging a DIFFERENT slot than the binding
  names is a typed refusal — never a silent substitution).

Unbound deployments keep working: a subject the registry has NO binding
decision for stages NOTHING (``stage_dispatch_credential`` returns
``None``) — the lane keeps consuming its ambient credential and the
attribution honestly stays unknown, never promoted to a claim. A
subject the deployment HAS bound fails closed on every axis above.

In-flight semantics: the returned ``staged_env`` is a SNAPSHOT. A
dispatched lane keeps the staged generation it was dispatched with
until its attempt is terminal; the broker is never re-read mid-flight.
Only the next dispatch re-resolves (a new attempt generation picking up
a rotated-in credential is exactly the retry contract).

R38-02 (#303) adds the DELIVERY half: :class:`CredentialDeliveryPlan`
and :func:`delivery_plan` — the dispatch-seam composition that decides
HOW a bound credential reaches the lane (one supported transport per
profile: the provider-native secret facility, or runner-time redemption
over the existing authenticated lane-control channel). The plan carries
REFERENCES only (the credential ref, the secret/variable NAME, the
redemption route); the resolved VALUE never again rides a dispatch
input, template parameter or trigger variable — every one of those
channels is documented as visible, precedence-trumping or log-exposed
run metadata (research ``2026-09-24-credential-delivery``).

R38-04 (#305) adds the CONSUMPTION half — what the runner boundary
proves and how rotation behaves around it:

- :class:`SecretValue` — every resolved VALUE a broker returns is
  wrapped so ``repr``/``str``/exception serialization carry the LENGTH
  CLASS only, never the material (secret-bearing objects stay out of
  logs, receipts and audit exports by construction);
- :data:`VERSION_KIND_PRESENCE` and friends — an EnvBroker version is
  a PRESENCE stamp, never a unique-secret-version fingerprint; every
  surface that shows a resolved version shows its KIND beside it;
- :func:`credential_policy` — ``FORGE_CREDENTIAL_POLICY`` ∈
  {``compat``, ``strict-broker``}: compat keeps today's unbound →
  ambient-legacy opt-out (labeled); strict-broker refuses an unbound
  route on a registry that holds ANY binding (typed
  ``strict_unbound_route``). The policy is stamped in every receipt;
- :class:`ConcurrentCredentialRegistry` — the JSON registry's
  multi-worker contract, made explicit: writes take an exclusive file
  lock and land through atomic rename (no torn documents, no lost
  read-modify-writes between registry writers), reads observe another
  worker's change within a documented stat TTL
  (:data:`REGISTRY_TTL_ENV`, default 5s). The JSON store stays a
  PROTOTYPE — a production swap to Postgres remains the real
  concurrent-rotation authority this contract documents;
- :data:`CONSUMER_RECEIPT_SCHEMA` — the consumer-side receipt the
  trusted runner bootstrap emits after secret staging (see
  :mod:`forge.lane_driver`): the broker/redemption receipt ids, binding
  revision, resolver identity, route, work, attempt and consumer
  identity — the delivered env-slot NAME, value-free by construction.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol, runtime_checkable

from forge.adaptive.operator_snapshot import CanonicalSubject
from forge.adaptive.project_credentials import (
    BINDING_SCHEMA,
    PROVIDER_ENV_VARS,
    CredentialRefusal,
    ProjectCredentialBinding,
    ProjectCredentialRegistry,
    resolve_dispatch_credential,
)

__all__ = [
    "AZURE_CREDENTIAL_GROUP_ENV",
    "BROKER_RECEIPT_SCHEMA",
    "CONSUMER_RECEIPT_SCHEMA",
    "CREDENTIAL_DELIVERY_REF_VARIABLE",
    "CREDENTIAL_DELIVERY_REDEEM_VARIABLE",
    "CREDENTIAL_POLICY_COMPAT",
    "CREDENTIAL_POLICY_ENV",
    "CREDENTIAL_POLICY_STRICT_BROKER",
    "CREDENTIAL_SECRET_PREFIX",
    "DEFAULT_REGISTRY_TTL_SECONDS",
    "DELIVERY_PLAN_SCHEMA",
    "DELIVERY_ROUTE_ENV",
    "DELIVERY_TEMPLATE_DIR_ENV",
    "DEFAULT_AZURE_CREDENTIAL_GROUP",
    "ENV_REF_SCHEME",
    "LANE_CREDENTIAL_REDEEM_ROUTE",
    "PROFILE_DELIVERY_MODES",
    "PROFILE_TEMPLATE_FILES",
    "REGISTRY_TTL_ENV",
    "RESOLVER_ENV",
    "RESOLVER_STAGED",
    "VERSION_KIND_BINDING_REVISION",
    "VERSION_KIND_FIXTURE",
    "VERSION_KIND_PRESENCE",
    "VERSION_KIND_SECRET_VERSION",
    "BrokerCredentialRefusal",
    "ConcurrentCredentialRegistry",
    "CredentialBroker",
    "CredentialDeliveryPlan",
    "EnvBroker",
    "ResolvedCredential",
    "SecretValue",
    "StagedBroker",
    "StagedDispatchCredential",
    "consumer_receipt_document",
    "credential_policy",
    "credential_secret_name",
    "credential_secret_segment",
    "declared_delivery_routes",
    "delivery_plan",
    "delivery_template_conformance",
    "registry_ttl_seconds",
    "reveal_secret",
    "stage_dispatch_credential",
    "version_is_unique_secret_proof",
]

#: The schema discriminator every broker receipt document carries.
BROKER_RECEIPT_SCHEMA = "forge.credential.broker-receipt/1"

#: The ref scheme :class:`EnvBroker` owns — ``env:<VAR_NAME>``.
ENV_REF_SCHEME = "env"

#: The resolver identity of the default environment broker.
RESOLVER_ENV = "env"

#: The resolver identity of the staged test double.
RESOLVER_STAGED = "staged"

#: env var name → provider route (the inverse of the closed route table;
#: an unrecognized variable resolves its route as ``unknown`` — honest,
#: never guessed).
_PROVIDER_ROUTE_OF_VAR: dict[str, str] = {var: route for route, var in PROVIDER_ENV_VARS.items()}


# ---------------------------------------------------------------------------
# R38-04 (#305) — the resolved version's KIND (presence honesty), the
# SecretValue wrapper, and the strict/compat credential policy.
# ---------------------------------------------------------------------------

#: The schema discriminator of the CONSUMER-side receipt (the runner
#: bootstrap's proof — emitted by :mod:`forge.lane_driver` after secret
#: staging; joined with the broker/redemption receipt ids, value-free).
CONSUMER_RECEIPT_SCHEMA = "forge.credential.consumer-receipt/1"

#: EnvBroker's resolved ``version`` is a PRESENCE stamp — it proves the
#: variable was non-empty at resolve time, NEVER that a unique secret
#: version was consumed (``env:<VAR>:present`` is the same string for
#: every generation of the variable's content).
VERSION_KIND_PRESENCE = "presence"

#: The native transports' attribution axis: no CI secret facility
#: exposes a secret-version id, so attribution rides the BINDING
#: REVISION plus resolver metadata — never a secret fingerprint.
VERSION_KIND_BINDING_REVISION = "binding-revision"

#: A future broker that returns the provider's OWN secret/version id
#: (a Vault version number, a cloud-secret version) — the only kind
#: that may be treated as a unique-secret-version proof.
VERSION_KIND_SECRET_VERSION = "secret-version"

#: The staged test double's pinned version label — a fixture label, not
#: a secret version (same honesty rule, one vocabulary).
VERSION_KIND_FIXTURE = "fixture"


def version_is_unique_secret_proof(version_kind: str) -> bool:
    """Whether a resolved version of *version_kind* may be treated as
    proof of a unique secret version. Only a real provider secret
    version id (:data:`VERSION_KIND_SECRET_VERSION`) qualifies — a
    presence stamp never does, no matter how unique it looks."""
    return version_kind == VERSION_KIND_SECRET_VERSION


class SecretValue:
    """One resolved credential VALUE, wrapped against leakage.

    ``repr``/``str`` return the LENGTH CLASS only (``<secret len=…>``
    with a bucketed length — not even the exact length is disclosed), so
    a value that rides an exception's args, a dataclass repr
    (``ResolvedCredential.staged_env``), or a log line renders as the
    class, never the material. :func:`reveal_secret` is the ONLY
    deliberate unwrap (the env-slot application, the redemption
    response) — both are seams that never log.

    Equality deliberately compares the underlying value (a plain ``str``
    compares equal too), so the existing proof assertions
    (``staged_env == {"ANTHROPIC_AUTH_TOKEN": value}``) keep working
    while the repr stays safe.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = str(value)

    def reveal(self) -> str:
        """The material — the deliberate unwrap (env application only)."""
        return self._value

    def length_class(self) -> str:
        """The bucketed length: tiny/short/medium/long (≤16/≤64/≤256/…)."""
        size = len(self._value)
        if size <= 16:
            return "tiny"
        if size <= 64:
            return "short"
        if size <= 256:
            return "medium"
        return "long"

    def __repr__(self) -> str:
        return f"<secret len={self.length_class()}>"

    def __str__(self) -> str:
        return repr(self)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SecretValue):
            return self._value == other._value
        if isinstance(other, str):
            return self._value == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(("forge.SecretValue", self._value))


def reveal_secret(value: str | SecretValue) -> str:
    """The deliberate unwrap of a resolved value (or a passthrough for a
    plain string) — the seams that MUST hold the material call this;
    everything else keeps the wrapper."""
    return value.reveal() if isinstance(value, SecretValue) else str(value)


#: Where the deployment declares its credential POLICY:
#: ``compat`` (default — an unbound subject keeps today's ambient-legacy
#: behavior, explicitly labeled) or ``strict-broker`` (an unbound route
#: on a registry that holds ANY binding refuses typed — the deployment
#: opted into bound credentials, so an unbound route is a configuration
#: defect, never a silent ambient lane).
CREDENTIAL_POLICY_ENV = "FORGE_CREDENTIAL_POLICY"

CREDENTIAL_POLICY_COMPAT = "compat"
CREDENTIAL_POLICY_STRICT_BROKER = "strict-broker"

#: The closed policy vocabulary.
CREDENTIAL_POLICIES: frozenset[str] = frozenset(
    {CREDENTIAL_POLICY_COMPAT, CREDENTIAL_POLICY_STRICT_BROKER}
)


def credential_policy(environ: Mapping[str, str] | None = None) -> str:
    """The deployment's credential policy (:data:`CREDENTIAL_POLICY_ENV`).

    Operator state doctrine: an unset/empty value is ``compat`` (today's
    behavior); a value outside the closed vocabulary is a TYPED failure
    (:class:`CredentialRefusal` ``credential_policy_invalid``), never a
    silent re-default.
    """
    source = os.environ if environ is None else environ
    raw = str(source.get(CREDENTIAL_POLICY_ENV, "") or "").strip().lower()
    if not raw:
        return CREDENTIAL_POLICY_COMPAT
    if raw in CREDENTIAL_POLICIES:
        return raw
    raise CredentialRefusal(
        "credential_policy_invalid",
        {
            "env": CREDENTIAL_POLICY_ENV,
            "value": raw[:32],
            "known": sorted(CREDENTIAL_POLICIES),
            "instruction": (
                f"set {CREDENTIAL_POLICY_ENV} to one of {sorted(CREDENTIAL_POLICIES)} "
                "— the credential policy is never silently re-defaulted"
            ),
        },
    )


def _strict_unbound_route(
    registry: ProjectCredentialRegistry,
    *,
    subject: CanonicalSubject,
    axis: str,
    policy: str,
) -> None:
    """The strict-broker refusal for an UNBOUND route: the registry holds
    at least one binding decision (this deployment opted into bound
    credentials), so a route that names no binding for *subject* is a
    configuration defect — typed refusal, never an ambient fallback."""
    if policy != CREDENTIAL_POLICY_STRICT_BROKER:
        return
    if not registry.bindings:
        return
    raise CredentialRefusal(
        "strict_unbound_route",
        {
            "policy": policy,
            "subject": subject.subject_id(),
            "unbound_axis": axis,
            "registry_bindings": len(registry.bindings),
            "instruction": (
                f"{CREDENTIAL_POLICY_ENV}=strict-broker refuses a route with no "
                "binding for this subject while the registry holds bindings — bind "
                "the subject's route or unset the policy; an ambient credential is "
                "never silently substituted"
            ),
        },
    )


class BrokerCredentialRefusal(CredentialRefusal):
    """A broker could not resolve a ref — fail closed, typed, with the
    resolver identity named (the message carries refs, never values)."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ResolvedCredential:
    """One broker resolution: the version, the staged env, the receipt.

    ``staged_env`` maps env var NAMES to :class:`SecretValue` wrapped
    values — the only place the VALUE exists, and the only thing a
    dispatch may move into the lane's variable set (``reveal_secret``
    at the application seam). ``version_kind`` says what ``version``
    IS (:data:`VERSION_KIND_PRESENCE` for the env broker — a presence
    stamp, never a unique-secret-version proof). ``resolver_identity``
    names WHO resolved (the broker's own identity, carried on the
    resolution so the proof needs no back-reference). ``receipt`` is
    refs/metadata only (schema :data:`BROKER_RECEIPT_SCHEMA`).
    """

    version: str
    staged_env: dict[str, SecretValue]
    receipt: dict[str, Any]
    resolver_identity: str = ""
    version_kind: str = VERSION_KIND_PRESENCE


@runtime_checkable
class CredentialBroker(Protocol):
    """The broker contract: refs in, a staged credential + receipt out."""

    #: WHO resolved — the identity the receipt and the dispatch proof
    #: record (``env``, ``staged``, a future ``vault:…`` address, …).
    resolver_identity: str

    async def resolve(
        self,
        credential_ref: str,
        *,
        grant: Mapping[str, Any] | None = None,
    ) -> ResolvedCredential:
        """Resolve *credential_ref* under the active execution *grant*.

        Raises :class:`BrokerCredentialRefusal` (typed) on any failure —
        an unknown ref scheme, an absent credential, a broker outage:
        the dispatch fails closed, it never falls back to an ambient
        credential.
        """
        ...  # pragma: no cover — the protocol shape


def _receipt_document(
    *,
    resolver_identity: str,
    credential_ref: str,
    env_var: str,
    version: str,
    version_kind: str = VERSION_KIND_PRESENCE,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The refs/metadata-only receipt shared by every broker shape.

    By construction there is no value slot here — the receipt names the
    ref, the env var, the provider route, the resolved version AND ITS
    KIND (a presence stamp displays as one; R38-04), and the deployment
    policy in force; a broker that smuggles the value into ``extra`` is
    refused by the allowlist exports (:mod:`forge.adaptive.audit_export`)
    and by the dispatch seam's own slot check.
    """
    receipt: dict[str, Any] = {
        "schema": BROKER_RECEIPT_SCHEMA,
        "receipt_id": uuid.uuid4().hex,
        "resolver_identity": resolver_identity,
        "provider_route": _PROVIDER_ROUTE_OF_VAR.get(env_var, "unknown"),
        "credential_ref": credential_ref,
        "env_var": env_var,
        "env_present": True,
        "resolved_version": version,
        "resolved_version_kind": version_kind,
        "credential_policy": credential_policy(),
        "resolved_at": _utcnow_iso(),
    }
    for key, value in (extra or {}).items():
        if key not in receipt:
            receipt[key] = value
    return receipt


class EnvBroker:
    """The default broker: an ``env:<VAR_NAME>`` ref resolves from the
    process environment.

    This is today's behavior made auditable — the lane's ambient
    credential, now resolved through an explicit, receipted decision.
    The receipt records the env-var NAME and its PRESENCE; the resolved
    ``version`` is a presence stamp (``env:<VAR>:present``), never a
    digest of the content. *environ* pins a private environment for
    tests; the process environment is read live at each resolve.
    """

    resolver_identity: str = RESOLVER_ENV

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ

    async def resolve(
        self,
        credential_ref: str,
        *,
        grant: Mapping[str, Any] | None = None,
    ) -> ResolvedCredential:
        del grant  # the env resolver needs no grant; the receipt names it at the seam
        ref = str(credential_ref or "")
        scheme, sep, name = ref.partition(":")
        if scheme != ENV_REF_SCHEME or not sep or not name.strip():
            raise BrokerCredentialRefusal(
                "unknown_ref_scheme",
                {
                    "resolver": self.resolver_identity,
                    "credential_ref": ref[:24],
                    "known": f"{ENV_REF_SCHEME}:<VAR_NAME>",
                },
            )
        env_var = name.strip()
        source: Mapping[str, str] = os.environ if self._environ is None else self._environ
        value = str(source.get(env_var) or "")
        if not value:
            raise BrokerCredentialRefusal(
                "env_absent",
                {"resolver": self.resolver_identity, "env_var": env_var, "env_present": False},
            )
        version = f"{ENV_REF_SCHEME}:{env_var}:present"
        receipt = _receipt_document(
            resolver_identity=self.resolver_identity,
            credential_ref=ref,
            env_var=env_var,
            version=version,
            version_kind=VERSION_KIND_PRESENCE,
        )
        return ResolvedCredential(
            version=version,
            staged_env={env_var: SecretValue(value)},
            receipt=receipt,
            resolver_identity=self.resolver_identity,
            version_kind=VERSION_KIND_PRESENCE,
        )


class StagedBroker:
    """The deterministic broker double for fixtures: refs staged
    programmatically resolve to pinned (value, version) pairs.

    The staged values NEVER come from the process environment — a
    production-entry fixture can prove the dispatch stages exactly the
    broker's selection while the ambient env carries something else.
    """

    resolver_identity: str = RESOLVER_STAGED

    def __init__(self) -> None:
        self._entries: dict[str, tuple[str, str, str]] = {}
        self.resolve_calls: list[str] = []

    def stage(self, credential_ref: str, value: str, *, env_var: str, version: str = "") -> None:
        """Pin one ref's resolution: the staged value, its env slot and
        its resolved version (a fixture ref shape is free-form — the
        broker double is how a ``vault:``-shaped future stays testable)."""
        self._entries[str(credential_ref)] = (str(value), str(env_var), str(version or "v1"))

    async def resolve(
        self,
        credential_ref: str,
        *,
        grant: Mapping[str, Any] | None = None,
    ) -> ResolvedCredential:
        del grant
        self.resolve_calls.append(str(credential_ref))
        entry = self._entries.get(str(credential_ref))
        if entry is None:
            raise BrokerCredentialRefusal(
                "unresolved_ref",
                {"resolver": self.resolver_identity, "credential_ref": str(credential_ref)[:24]},
            )
        value, env_var, version = entry
        receipt = _receipt_document(
            resolver_identity=self.resolver_identity,
            credential_ref=str(credential_ref),
            env_var=env_var,
            version=version,
            version_kind=VERSION_KIND_FIXTURE,
        )
        return ResolvedCredential(
            version=version,
            staged_env={env_var: SecretValue(value)},
            receipt=receipt,
            resolver_identity=self.resolver_identity,
            version_kind=VERSION_KIND_FIXTURE,
        )


@dataclass(frozen=True)
class StagedDispatchCredential:
    """What a dispatch leg staged: the snapshot the lane consumes plus
    the extended proof that rides the run evidence.

    ``staged_env`` is the SNAPSHOT (copy-on-stage, values wrapped in
    :class:`SecretValue`): mutating the broker or the environment
    afterwards never rewrites it — the in-flight lane keeps this
    generation until terminal. ``proof`` is the
    ``forge.project.dispatch-credential-proof/2`` document: the binding
    subject/ref/revision, the resolver identity, the resolved version
    AND ITS KIND, the credential policy, the grant refs and the broker
    receipt — refs and metadata only.
    """

    subject: str
    provider: str
    credential_ref: str
    env_var: str
    binding_revision: int
    resolved_version: str
    resolver_identity: str
    staged_env: dict[str, SecretValue] = field(default_factory=dict)
    proof: dict[str, Any] = field(default_factory=dict)
    resolved_version_kind: str = VERSION_KIND_PRESENCE
    credential_policy: str = CREDENTIAL_POLICY_COMPAT


async def stage_dispatch_credential(
    registry: ProjectCredentialRegistry,
    broker: CredentialBroker,
    *,
    subject: CanonicalSubject,
    provider: str,
    presented_ref: str = "",
    grant: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> StagedDispatchCredential | None:
    """The dispatch seam: resolve a subject's provider credential under
    the active execution grant, BEFORE any provider call.

    Returns ``None`` — the ``ambient-legacy`` attribution — when
    *provider* names no route or the registry holds NO binding decision
    for *subject* (attribution honestly unknown; the deployment never
    opted this subject into bound credentials) — UNLESS the deployment
    policy is ``strict-broker`` (:data:`CREDENTIAL_POLICY_ENV`) and the
    registry holds ANY binding: an unbound route is then a typed
    ``strict_unbound_route`` refusal, never an ambient lane. Otherwise,
    in order:

    - the registry's fail-closed checks run first (no live binding →
      ``no_binding``; revoked → ``revoked``; a rotated-away presented
      ref → ``rotated``; a foreign presented ref →
      ``wrong_project_ref``) — BEFORE the broker is consulted, so a
      refused dispatch performs zero broker and zero provider calls;
    - the broker resolves the binding's ref (its own typed refusals
      propagate — ``env_absent``, ``unknown_ref_scheme``, …);
    - the staged SLOT must be exactly the binding's env var: a broker
      staging a different slot (or extra slots) is the silent
      substitution this seam exists to prevent → ``staged_slot_mismatch``;
    - the extended proof (schema ``…dispatch-credential-proof/2``) is
      assembled: binding revision, resolver identity, the resolved
      version AND ITS KIND, the credential policy, the grant refs and
      the broker receipt. No value, ever.

    Observability spellings the dispatch legs log from the result:
    ``credential.binding_subject`` · ``credential.resolved_version`` ·
    ``credential.resolver_identity`` (and ``credential.rotation_refusal``
    on the typed rotation refusal).
    """
    policy = credential_policy(environ)
    if not provider:
        _strict_unbound_route(registry, subject=subject, axis="unknown-route", policy=policy)
        return None
    if not registry.subject_is_bound(subject):
        _strict_unbound_route(registry, subject=subject, axis="absent-binding", policy=policy)
        return None
    dispatch_credential = resolve_dispatch_credential(
        registry, subject=subject, provider=provider, presented_ref=presented_ref
    )
    resolved = await broker.resolve(dispatch_credential.credential_ref, grant=grant)
    staged_keys = set(resolved.staged_env)
    if staged_keys != {dispatch_credential.env_var}:
        raise CredentialRefusal(
            "staged_slot_mismatch",
            {
                "subject": dispatch_credential.subject,
                "provider": dispatch_credential.provider,
                "bound_env_var": dispatch_credential.env_var,
                "staged_env_vars": sorted(staged_keys),
                "resolver": resolved.resolver_identity,
            },
        )
    grant_refs = {str(key): str(value) for key, value in (grant or {}).items()}
    proof = {
        **dispatch_credential.proof,
        "resolver_identity": resolved.resolver_identity,
        "resolved_version": resolved.version,
        "resolved_version_kind": resolved.version_kind,
        "credential_policy": policy,
        "grant": grant_refs,
        "receipt": resolved.receipt,
    }
    return StagedDispatchCredential(
        subject=dispatch_credential.subject,
        provider=dispatch_credential.provider,
        credential_ref=dispatch_credential.credential_ref,
        env_var=dispatch_credential.env_var,
        binding_revision=dispatch_credential.binding_revision,
        resolved_version=resolved.version,
        resolver_identity=resolved.resolver_identity,
        staged_env=dict(resolved.staged_env),
        proof=proof,
        resolved_version_kind=resolved.version_kind,
        credential_policy=policy,
    )


# ---------------------------------------------------------------------------
# R38-02 (#303) — the credential DELIVERY plan: references only, one
# supported transport per profile, never a value in the dispatch payload.
# ---------------------------------------------------------------------------

#: The schema discriminator every credential delivery plan carries.
DELIVERY_PLAN_SCHEMA = "forge.credential.delivery-plan/1"

#: Where the deployment DECLARES its credential delivery route(s) — a
#: comma-separated list of mode words (one per dispatch profile in use).
#: A bound subject with no declared route supporting the dispatching
#: profile is a typed refusal, never an ambient fallback.
DELIVERY_ROUTE_ENV = "FORGE_CREDENTIAL_DELIVERY"

#: Where the SHIPPED lane templates live for dispatch/template conformance
#: (defaults to ``ci/templates`` under the process working directory — the
#: repo-checkout deployment shape; a deployment without the checkout
#: points this at its copy of the shipped templates).
DELIVERY_TEMPLATE_DIR_ENV = "FORGE_CREDENTIAL_TEMPLATE_DIR"

#: The native GitHub Actions profile (a): the dispatch input carries the
#: credential ref ONLY; the workflow reads the repo/org/environment secret
#: named ``FORGE_MODEL_<REF>`` runner-side (research doc 01).
DELIVERY_MODE_GITHUB_NATIVE = "github-native-secret"
#: The native Azure Pipelines profile (a): templateParameters carry the
#: ref only; a secret variable inside an authorized VARIABLE GROUP is the
#: documented carrier (research doc 02 — parameters are documented "No
#: support for secret values").
DELIVERY_MODE_AZURE_GROUP = "azure-variable-group"
#: The native GitLab CE profile (a): the trigger payload carries the ref
#: only; a protected+masked project/group CI/CD variable named
#: ``FORGE_MODEL_<REF>`` is the carrier (research doc 03 — trigger
#: variables display on job pages and OUTRANK project variables).
DELIVERY_MODE_GITLAB_PROTECTED = "gitlab-protected-variable"
#: Profile (b) — runner-time redemption over the EXISTING authenticated
#: lane-control channel: the lane holds an attempt-scoped HMAC token and
#: redeems the value at startup, TTL-bound to the attempt (research doc 04).
DELIVERY_MODE_RUNNER_REDEMPTION = "runner-redemption"

#: The dispatch profile (the provider leg dispatching) → the delivery
#: modes it supports. Exactly ONE supported transport per profile.
PROFILE_DELIVERY_MODES: dict[str, frozenset[str]] = {
    "github": frozenset({DELIVERY_MODE_GITHUB_NATIVE, DELIVERY_MODE_RUNNER_REDEMPTION}),
    "azure": frozenset({DELIVERY_MODE_AZURE_GROUP, DELIVERY_MODE_RUNNER_REDEMPTION}),
    "gitlab": frozenset({DELIVERY_MODE_GITLAB_PROTECTED, DELIVERY_MODE_RUNNER_REDEMPTION}),
}

#: The SHIPPED template each profile's dispatch/template conformance
#: validates against (under :data:`DELIVERY_TEMPLATE_DIR_ENV`).
PROFILE_TEMPLATE_FILES: dict[str, str] = {
    "github": "forge-harness.github.yml",
    "azure": "forge-lane.azure-pipelines.yml",
    "gitlab": "claude-code.gitlab-ci.yml",
}

#: The runner-redemption endpoint on the lane-control router (the route
#: the plan names; the lane dials it with its attempt-scoped token).
LANE_CREDENTIAL_REDEEM_ROUTE = "/lane/credentials/redeem"

#: The lane-side variables the dispatch envelope carries for delivery:
#: the (non-secret) credential ref and the redemption-mode flag.
CREDENTIAL_DELIVERY_REF_VARIABLE = "FORGE_CREDENTIAL_REF"
CREDENTIAL_DELIVERY_REDEEM_VARIABLE = "FORGE_CREDENTIAL_REDEEM"

#: The native-secret name prefix: the secret/variable NAME derives from
#: the credential REF (``FORGE_MODEL_<SEGMENT>`` — one documented
#: provisioning step per bound ref; rotation overwrites the secret).
CREDENTIAL_SECRET_PREFIX = "FORGE_MODEL_"

#: The Azure variable group holding the lane's secret variables (the
#: protected resource the pipeline is authorized against; research doc 02).
AZURE_CREDENTIAL_GROUP_ENV = "FORGE_AZURE_CREDENTIAL_GROUP"
DEFAULT_AZURE_CREDENTIAL_GROUP = "forge-lane-credentials"


def credential_secret_segment(credential_ref: str) -> str:
    """The secret-name-safe segment of a credential ref.

    ``env:ANTHROPIC_AUTH_TOKEN`` → ``ENV_ANTHROPIC_AUTH_TOKEN``;
    ``vault:kv/eng#42`` → ``VAULT_KV_ENG_42`` (upper-case, every char
    outside ``[A-Za-z0-9_]`` becomes ``_`` — the charset GitHub secret
    names, GitLab variable names and Azure variable names all accept).
    The derivation is lossy by design (two refs MAY share a segment —
    the native profile's carrier is per provider route anyway, whose
    env slot :data:`PROVIDER_ENV_VARS` fixes to ONE variable).
    """
    cleaned = "".join(char if char.isalnum() or char == "_" else "_" for char in credential_ref)
    return cleaned.upper()


def credential_secret_name(credential_ref: str) -> str:
    """The native carrier NAME derived from the ref (GitHub secret /
    GitLab protected+masked variable): ``FORGE_MODEL_<SEGMENT>``."""
    return CREDENTIAL_SECRET_PREFIX + credential_secret_segment(credential_ref)


def declared_delivery_routes(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    """The deployment's declared delivery routes
    (:data:`DELIVERY_ROUTE_ENV`, comma-separated) — refs only, never
    validated against a value; an unset/empty env declares NOTHING (the
    bound-subject refusal is :func:`delivery_plan`'s to raise)."""
    source = os.environ if environ is None else environ
    raw = str(source.get(DELIVERY_ROUTE_ENV, "") or "")
    return frozenset(word.strip() for word in raw.split(",") if word.strip())


@dataclass(frozen=True)
class CredentialDeliveryPlan:
    """HOW a bound credential reaches the lane — references only.

    Distinct from :class:`ResolvedCredential` (which carries the VALUE in
    ``staged_env``): the plan carries the credential REF, the selected
    transport mode, the transport REFERENCE (the secret/variable NAME, or
    the redemption route) and the expected identity (binding revision +
    who resolves). By construction there is no value slot: the dispatch
    payload carries ``dispatch_ref`` (the ref, secret-name-safe for the
    native profiles, raw for redemption) and NOTHING else
    credential-shaped.
    """

    subject: str
    provider: str
    profile: str
    credential_ref: str
    env_var: str
    binding_revision: int
    mode: str
    transport_ref: str
    #: What the dispatch payload carries (a REF; the native profiles'
    #: secret-name-safe segment, the raw ref under runner-redemption).
    dispatch_ref: str
    #: True under ``runner-redemption`` — the payload then also carries
    #: the (non-secret) redemption flag for the lane's startup hook.
    redemption: bool
    expected_identity: dict[str, Any] = field(default_factory=dict)
    #: The credential POLICY in force at plan time (R38-04) — stamped in
    #: the plan document; an unbound route under ``strict-broker`` never
    #: reaches a plan (it refuses typed at the seam).
    credential_policy: str = CREDENTIAL_POLICY_COMPAT

    def as_document(self) -> dict[str, Any]:
        """The refs/metadata-only proof document (schema
        :data:`DELIVERY_PLAN_SCHEMA`) the dispatch evidence carries —
        attribution ``bound-delivery``, the credential POLICY in force,
        never a value."""
        return {
            "schema": DELIVERY_PLAN_SCHEMA,
            "attribution": "bound-delivery",
            "subject": self.subject,
            "provider": self.provider,
            "profile": self.profile,
            "credential_ref": self.credential_ref,
            "env_var": self.env_var,
            "binding_revision": int(self.binding_revision),
            "mode": self.mode,
            "transport_ref": self.transport_ref,
            "dispatch_ref": self.dispatch_ref,
            "credential_policy": self.credential_policy,
            "expected_identity": dict(self.expected_identity),
        }


def _shipped_template_text(profile: str, environ: Mapping[str, str] | None) -> str | None:
    """The SHIPPED lane template's text for *profile*, or None when no
    template copy is reachable (the conformance then refuses typed)."""
    source = os.environ if environ is None else environ
    configured = str(source.get(DELIVERY_TEMPLATE_DIR_ENV, "") or "").strip()
    root = Path(configured) if configured else Path.cwd() / "ci" / "templates"
    path = root / PROFILE_TEMPLATE_FILES[profile]
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def delivery_template_conformance(plan: CredentialDeliveryPlan, template_text: str) -> None:
    """Validate the rendered/shipped template against the plan's transport
    BEFORE the native start call (mismatch → typed refusal with the
    onboarding instruction — an older delivery schema never reaches paid
    work).

    Per profile (research docs 01–03): GitHub — the ``credential_ref``
    input is declared AND the workflow maps the env from the ref-derived
    secret; GitLab — the ref variable's derived-name consumption is
    present; Azure — the variable group is referenced AND the group's
    secret is mapped into the lane env; runner-redemption — the
    redemption flag consumption is present on every profile.
    """
    if plan.mode == DELIVERY_MODE_GITHUB_NATIVE:
        required: tuple[str, ...] = (
            "credential_ref:",
            "secrets[format('FORGE_MODEL_{0}', inputs.credential_ref)]",
        )
        instruction = (
            "upgrade the target repository's forge-harness workflow to the shipped "
            "template (ci/templates/forge-harness.github.yml): it must declare the "
            "credential_ref input and map the lane credential from the repo secret "
            f"{plan.transport_ref} via secrets[format('FORGE_MODEL_{0}', "
            "inputs.credential_ref)] — create that secret once (gh secret set) "
            "with the credential value"
        )
    elif plan.mode == DELIVERY_MODE_GITLAB_PROTECTED:
        required = (
            CREDENTIAL_DELIVERY_REF_VARIABLE,
            "FORGE_MODEL_${" + CREDENTIAL_DELIVERY_REF_VARIABLE + "}",
        )
        instruction = (
            "upgrade the target project's forge lane template to the shipped one "
            "(ci/templates/claude-code.gitlab-ci.yml) and create the protected + "
            f"masked project CI/CD variable {plan.transport_ref} (Settings → CI/CD → "
            "Variables; the value is the credential) — the lane derives the "
            "variable name from the dispatched ref"
        )
    elif plan.mode == DELIVERY_MODE_AZURE_GROUP:
        group = plan.transport_ref.partition("/")[0]
        secret_name = plan.transport_ref.partition("/")[2]
        required = (
            group,
            f"$({secret_name})",
        )
        instruction = (
            "provision the credential as a SECRET variable in the lane pipeline "
            f"(recommended: the authorized variable group {group}, Pipelines → "
            f"Library; the secret is named {secret_name} after the provider env "
            "slot) and authorize the pipeline against it — the lane template "
            "maps that secret into the step env; template parameters never "
            "carry secret values"
        )
    else:  # runner-redemption — the lane must consume the flag on every profile
        required = (CREDENTIAL_DELIVERY_REDEEM_VARIABLE,)
        instruction = (
            "upgrade the target's forge lane template to the shipped one: the "
            "runner-redemption profile requires the lane to receive "
            f"{CREDENTIAL_DELIVERY_REDEEM_VARIABLE} and redeem the credential at "
            f"startup through {LANE_CREDENTIAL_REDEEM_ROUTE}"
        )
    missing = [marker for marker in required if marker not in template_text]
    if missing:
        raise CredentialRefusal(
            "delivery_template_mismatch",
            {
                "profile": plan.profile,
                "mode": plan.mode,
                "transport_ref": plan.transport_ref,
                "missing_markers": missing,
                "instruction": instruction,
            },
        )


async def delivery_plan(
    registry: ProjectCredentialRegistry,
    broker: CredentialBroker,
    *,
    subject: CanonicalSubject,
    provider_route: str,
    profile: str,
    presented_ref: str = "",
    environ: Mapping[str, str] | None = None,
) -> CredentialDeliveryPlan | None:
    """The R38-02 dispatch seam: plan HOW a bound subject's credential is
    delivered — one supported transport per profile, refs only.

    Returns ``None`` — the ``ambient-legacy`` attribution — when
    *provider_route* names no route, the registry holds no binding
    decision for *subject*, or *profile* names no dispatch leg — each of
    those honestly names no project-bound operation. UNLESS the
    deployment policy is ``strict-broker``
    (:data:`CREDENTIAL_POLICY_ENV`) and the registry holds ANY binding:
    an unbound route is then a typed ``strict_unbound_route`` refusal
    (never an ambient lane, never a project-bound claim). Otherwise, in
    order (every failure a TYPED refusal that parks the run with ZERO
    provider calls):

    - the registry's fail-closed checks run first (live binding, revoked,
      rotated-away presented ref, foreign presented ref — the value is
      NOT resolved here: under every supported mode the dispatch moves a
      REFERENCE, and the value holder is the CI provider's secret
      facility or the redemption endpoint, never the dispatch payload);
    - the deployment's DECLARED delivery route
      (:data:`DELIVERY_ROUTE_ENV`) must select exactly one mode the
      *profile* supports — undeclared/unsupported →
      ``delivery_route_unsupported``, never an ambient fallback;
    - the SHIPPED template for the profile must conform to the selected
      transport (``delivery_template_mismatch`` /
      ``delivery_template_unavailable`` with the onboarding instruction)
      — a target running an older delivery schema is refused BEFORE the
      native start call.
    """
    source = os.environ if environ is None else environ
    policy = credential_policy(environ)
    if not provider_route:
        _strict_unbound_route(registry, subject=subject, axis="unknown-route", policy=policy)
        return None
    profile = str(profile or "").strip()
    supported = PROFILE_DELIVERY_MODES.get(profile)
    if supported is None:
        _strict_unbound_route(registry, subject=subject, axis="unknown-profile", policy=policy)
        return None
    if not registry.subject_is_bound(subject):
        _strict_unbound_route(registry, subject=subject, axis="absent-binding", policy=policy)
        return None
    dispatch_credential = resolve_dispatch_credential(
        registry, subject=subject, provider=provider_route, presented_ref=presented_ref
    )
    declared = declared_delivery_routes(environ)
    selected = sorted(declared & supported)
    if not selected:
        raise CredentialRefusal(
            "delivery_route_unsupported",
            {
                "subject": dispatch_credential.subject,
                "provider": dispatch_credential.provider,
                "profile": profile,
                "declared": sorted(declared),
                "supported": sorted(supported),
                "env": DELIVERY_ROUTE_ENV,
                "instruction": (
                    f"declare the deployment's credential delivery route in "
                    f"{DELIVERY_ROUTE_ENV} (one of {sorted(supported)} for the "
                    f"{profile} profile) — a bound credential never falls back "
                    "to an ambient one"
                ),
            },
        )
    if len(selected) > 1:
        raise CredentialRefusal(
            "delivery_route_ambiguous",
            {
                "profile": profile,
                "selected": selected,
                "env": DELIVERY_ROUTE_ENV,
            },
        )
    mode = selected[0]
    segment = credential_secret_segment(dispatch_credential.credential_ref)
    if mode == DELIVERY_MODE_GITHUB_NATIVE or mode == DELIVERY_MODE_GITLAB_PROTECTED:
        transport_ref = credential_secret_name(dispatch_credential.credential_ref)
        dispatch_ref, redemption = segment, False
    elif mode == DELIVERY_MODE_AZURE_GROUP:
        group = str(source.get(AZURE_CREDENTIAL_GROUP_ENV, "") or "").strip() or (
            DEFAULT_AZURE_CREDENTIAL_GROUP
        )
        # The group's secret variable is named after the provider's ENV
        # SLOT — the variable the lane template's per-driver conditional
        # maps into the step env (Azure macro $(NAME) references and
        # secret-variable mappings cannot be composed from a runtime
        # parameter, so the name is fixed per provider route).
        transport_ref = f"{group}/{dispatch_credential.env_var}"
        dispatch_ref, redemption = segment, False
    else:  # runner-redemption — the value is redeemed at lane startup
        transport_ref, dispatch_ref, redemption = (
            LANE_CREDENTIAL_REDEEM_ROUTE,
            (dispatch_credential.credential_ref),
            True,
        )
    plan = CredentialDeliveryPlan(
        subject=dispatch_credential.subject,
        provider=dispatch_credential.provider,
        profile=profile,
        credential_ref=dispatch_credential.credential_ref,
        env_var=dispatch_credential.env_var,
        binding_revision=dispatch_credential.binding_revision,
        mode=mode,
        transport_ref=transport_ref,
        dispatch_ref=dispatch_ref,
        redemption=redemption,
        expected_identity={
            "binding_revision": int(dispatch_credential.binding_revision),
            # WHO resolves the value: the redemption broker under profile
            # (b), the CI provider's secret facility under the native
            # profiles (forge holds nothing).
            "resolver": (getattr(broker, "resolver_identity", "") if redemption else mode),
            "env_var": dispatch_credential.env_var,
            # WHAT the attribution axis is: no CI secret facility exposes
            # a secret-version id, so the native transports' identity is
            # the BINDING REVISION plus resolver metadata — a presence
            # version is never a unique-secret-version proof (R38-04).
            "version_kind": VERSION_KIND_BINDING_REVISION,
        },
        credential_policy=policy,
    )
    template_text = _shipped_template_text(profile, source)
    if template_text is None:
        raise CredentialRefusal(
            "delivery_template_unavailable",
            {
                "profile": profile,
                "mode": mode,
                "env": DELIVERY_TEMPLATE_DIR_ENV,
                "instruction": (
                    "the shipped lane template could not be read for dispatch/"
                    "template conformance — point "
                    f"{DELIVERY_TEMPLATE_DIR_ENV} at the directory holding the "
                    "shipped templates (ci/templates) or place it under the "
                    "working directory; a bound dispatch is never sent against "
                    "an unconformable template"
                ),
            },
        )
    delivery_template_conformance(plan, template_text)
    return plan


# ---------------------------------------------------------------------------
# R38-04 (#305) — the JSON registry's MULTI-WORKER contract: locked,
# atomic writes + a documented stat-TTL read propagation bound. The JSON
# store stays a PROTOTYPE (a production swap to Postgres remains the
# real concurrent-rotation authority); this class makes the prototype's
# semantics explicit instead of accidental.
# ---------------------------------------------------------------------------

#: Where the deployment declares the registry's read-side stat TTL — the
#: propagation bound within which another worker process's bind/revoke
#: becomes visible to THIS process's next resolve.
REGISTRY_TTL_ENV = "FORGE_CREDENTIAL_REGISTRY_TTL_SECONDS"

#: The default propagation bound (seconds).
DEFAULT_REGISTRY_TTL_SECONDS = 5.0


def registry_ttl_seconds(environ: Mapping[str, str] | None = None) -> float:
    """The registry stat TTL from env (default 5s). Operator state: a
    malformed or non-positive value is a typed failure, never a silent
    re-default (the same doctrine as the redemption TTL)."""
    source = os.environ if environ is None else environ
    raw = str(source.get(REGISTRY_TTL_ENV, "") or "").strip()
    if not raw:
        return float(DEFAULT_REGISTRY_TTL_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        raise CredentialRefusal(
            "credential_registry_ttl_invalid",
            {
                "env": REGISTRY_TTL_ENV,
                "value": raw[:32],
                "instruction": f"{REGISTRY_TTL_ENV} must be a positive number of seconds",
            },
        ) from None
    if value <= 0:
        raise CredentialRefusal(
            "credential_registry_ttl_invalid",
            {
                "env": REGISTRY_TTL_ENV,
                "value": raw[:32],
                "instruction": f"{REGISTRY_TTL_ENV} must be a positive number of seconds",
            },
        )
    return value


@dataclass
class ConcurrentCredentialRegistry(ProjectCredentialRegistry):
    """The binding store's multi-worker contract (R38-04), explicit:

    - WRITES (:meth:`bind` / :meth:`revoke`) take an EXCLUSIVE file lock
      beside the document, re-load the freshest state UNDER the lock,
      and land through ATOMIC RENAME (``os.replace``) — no torn
      documents, and no lost read-modify-write between two registry
      writers (the lock serializes the whole decision, the rename makes
      the landing indivisible for readers).
    - READS (:meth:`binding_for` / :meth:`history_for` /
      :meth:`subject_is_bound` — everything a resolve consults) observe
      another worker's write within the documented stat TTL
      (:data:`REGISTRY_TTL_ENV`, default 5s): a read older than the TTL
      re-stats the document and reloads when its (mtime_ns, size)
      fingerprint moved. Within the TTL the cached view stands — that is
      the contract, not an accident.
    - IN-FLIGHT rotation semantics are unchanged and stay the caller's:
      a staged snapshot (a :class:`StagedDispatchCredential`, an applied
      redemption) is a GENERATION — a revocation that lands mid-flight
      never rewrites it; only the NEXT resolve (a NEW dispatch/redeem
      attempt) re-checks the registry and refuses typed.

      The JSON prototype is NOT a concurrent-rotation authority beyond
      this contract: cross-datacenter propagation, audit-grade write
      history and transactional multi-key rotation remain the production
      Postgres swap's (:mod:`forge.adaptive.project_credentials` keeps
      the storage shape fixed so the swap is a storage detail).
    """

    #: The read-side propagation bound (seconds); None resolves from
    #: :data:`REGISTRY_TTL_ENV` at construction.
    ttl_seconds: float | None = None

    _ttl: float = field(default=0.0, init=False, repr=False)
    _last_check: float = field(default=0.0, init=False, repr=False)
    _fingerprint: tuple[int, int] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.ttl_seconds is None:
            self._ttl = registry_ttl_seconds()
        else:
            if self.ttl_seconds <= 0:
                raise CredentialRefusal(
                    "credential_registry_ttl_invalid",
                    {"ttl_seconds": self.ttl_seconds, "instruction": "must be positive"},
                )
            self._ttl = float(self.ttl_seconds)
        self._mark_fingerprint()

    # -- the read side: TTL-bounded observation ---------------------------

    def _mark_fingerprint(self) -> None:
        """Record the loaded document's fingerprint + check time."""
        self._last_check = time.monotonic()
        self._fingerprint = self._stat_fingerprint()

    def _stat_fingerprint(self) -> tuple[int, int] | None:
        if self.path is None:
            return None
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _maybe_reload(self) -> None:
        """The read-side contract check: past the TTL, re-stat and reload
        when the fingerprint moved. Within the TTL the cached view
        stands (the documented propagation window)."""
        if self.path is None:
            return
        if (time.monotonic() - self._last_check) < self._ttl:
            return
        fingerprint = self._stat_fingerprint()
        self._last_check = time.monotonic()
        if fingerprint is not None and fingerprint != self._fingerprint:
            self._load()
            self._fingerprint = fingerprint

    def binding_for(
        self, subject: CanonicalSubject | str, provider: str
    ) -> ProjectCredentialBinding | None:
        self._maybe_reload()
        return super().binding_for(subject, provider)

    def history_for(
        self, subject: CanonicalSubject | str, provider: str
    ) -> list[ProjectCredentialBinding]:
        self._maybe_reload()
        return super().history_for(subject, provider)

    def subject_is_bound(self, subject: CanonicalSubject | str) -> bool:
        self._maybe_reload()
        return super().subject_is_bound(subject)

    # -- the write side: locked, atomic -----------------------------------

    def _lock_path(self) -> Path:
        assert self.path is not None
        return self.path.with_name(self.path.name + ".lock")

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """The exclusive write lock (flock on the sibling ``.lock``
        file). POSIX-only by design — the JSON prototype's supported
        deployment surface."""
        import fcntl

        self._lock_path().parent.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path(), "a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _persist(self) -> None:
        """Atomic persistence: the document lands through RENAME, never
        an in-place rewrite — a concurrent reader sees the whole old or
        the whole new document, never a torn file. (The document shape
        mirrors :meth:`ProjectCredentialRegistry._persist` — the storage
        shape is fixed by contract; only the landing differs.)"""
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "schema": BINDING_SCHEMA,
            "bindings": [binding.as_document() for binding in self.bindings.values()],
            "history": [binding.as_document() for binding in self.history],
        }
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
        self._mark_fingerprint()

    def _locked_write(self, operate: Any) -> Any:
        """One serialized registry decision: load freshest under the
        lock, operate, land atomically — still under the lock."""
        if self.path is None:
            return operate()
        with self._exclusive():
            if self.path.is_file():
                self._load()
            self._fingerprint = self._stat_fingerprint()
            result = operate()
            self._persist()
            return result

    def bind(
        self,
        subject: CanonicalSubject | str,
        provider: str,
        credential_ref: str,
        *,
        bound_by: str,
        project_id: int = 0,
    ) -> ProjectCredentialBinding:
        """Bind/rotate under the multi-worker contract (see class doc)."""

        # Explicit class call: a zero-arg ``super()`` inside the closure
        # has no __class__ cell to bind.
        def operate() -> ProjectCredentialBinding:
            return ProjectCredentialRegistry.bind(
                self, subject, provider, credential_ref, bound_by=bound_by, project_id=project_id
            )

        return self._locked_write(operate)

    def revoke(
        self,
        subject: CanonicalSubject | str,
        provider: str,
        *,
        revoked_by: str,
    ) -> ProjectCredentialBinding | None:
        """Revoke under the multi-worker contract (see class doc)."""

        def operate() -> ProjectCredentialBinding | None:
            return ProjectCredentialRegistry.revoke(self, subject, provider, revoked_by=revoked_by)

        return self._locked_write(operate)


# ---------------------------------------------------------------------------
# R38-04 (#305) — the CONSUMER receipt: the trusted runner bootstrap's
# value-free proof that the delivered credential was staged for THIS
# attempt, joined with the broker/redemption receipts.
# ---------------------------------------------------------------------------


def consumer_receipt_document(
    *,
    consumer_receipt_id: str,
    env_var: str,
    consumer_identity: Mapping[str, Any],
    delivery_route: str,
    resolver_identity: str,
    credential_policy: str,
    binding_revision: int | None = None,
    provider_route: str = "",
    credential_ref: str = "",
    work_id: str = "",
    attempt_generation: int | None = None,
    redemption_id: str = "",
    broker_receipt_id: str = "",
    resolved_version: str = "",
    resolved_version_kind: str = "",
) -> dict[str, Any]:
    """Assemble the consumer-side receipt (schema
    :data:`CONSUMER_RECEIPT_SCHEMA`) — refs and metadata ONLY.

    The join the operator needs: WHICH binding revision paid for WHICH
    attempt, consumed by WHICH runner job — ``redemption_id`` /
    ``broker_receipt_id`` correlate with the control plane's durable
    redemption audit row and the broker's own receipt; the value has NO
    slot here by construction (the delivered material lives only in the
    consumer's env slot, never in evidence). ``resolved_version_kind``
    keeps presence-stamp honesty on this surface too.
    """
    return {
        "schema": CONSUMER_RECEIPT_SCHEMA,
        "consumer_receipt_id": str(consumer_receipt_id),
        "consumer_identity": dict(consumer_identity),
        "delivery_route": str(delivery_route),
        "resolver_identity": str(resolver_identity),
        "credential_policy": str(credential_policy),
        "binding_revision": binding_revision,
        "provider_route": str(provider_route),
        "credential_ref": str(credential_ref),
        "env_var": str(env_var),
        "work_id": str(work_id),
        "attempt_generation": attempt_generation,
        "redemption_id": str(redemption_id),
        "broker_receipt_id": str(broker_receipt_id),
        "resolved_version": str(resolved_version),
        "resolved_version_kind": str(resolved_version_kind),
        "issued_at": _utcnow_iso(),
    }
