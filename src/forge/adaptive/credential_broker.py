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

Q39-01 (#320) adds the AUTHORIZATION object between the plan and the
redemption: :class:`CredentialOperationGrant` — the frozen, persisted
grant the dispatch mints beside a redemption-mode delivery plan
(subject, work, attempt generation, driver/model route, the EXACT
credential ref + binding revision, delivery mode, the permitted
operation, a grant_id and an ABSOLUTE redemption deadline fixed at
dispatch authorization). The redemption endpoint
(:mod:`forge.api_lane_control`) judges every request against THIS
document — project membership never was operation authorization — with
the whole refusal ladder typed and performed BEFORE any broker I/O
(``grant_route_mismatch``, ``grant_ref_mismatch``,
``grant_absent_native_only``, ``grant_absent_legacy``, ``grant_expired``,
``attempt_terminal``). The deadline is a fixed instant persisted in the
run evidence (idempotent per attempt+route+ref), never a per-request
``now + TTL``; the retry window for an acknowledged-but-lost response is
the grant's own lifetime (a repeated request under the SAME grant is
idempotent; the receipt joins on ``grant_id``).

Q39-04 (#323) makes the NATIVE carrier locators collision-safe and
profile/driver-specific — the P05 defect this module shipped with:
:func:`credential_secret_segment` maps punctuation to ``_`` and
uppercases, so ``vault:kv/team-a`` and ``vault:kv/team_a`` shared ONE
native secret name in a shared namespace (rotation between them was a
silent alias). The additions:

- :func:`native_locator` — the collision-safe carrier locator: a
  bounded ASCII-readable prefix (the ref's leaf identifier, sanitized)
  + the first 12 hex of sha256 over the FULL ref. Distinct refs can
  never share a locator (the digest differs); Unicode/overlength refs
  get the same bounded form (the digest disambiguates) and the truly
  unrepresentable refuse typed (``native_locator_unrepresentable``).
- :class:`NativeLocatorRegistry` — the persisted
  ``native_locator_map`` (ref → locator + allocation timestamp).
  Allocation collision-checks WITHIN the actual provider namespace +
  route (case-insensitively — GitHub secret names are
  case-insensitively unique; independent project namespaces are
  independent by design). Two colliding LEGACY refs (the pre-locator
  ``FORGE_MODEL_<SEGMENT>`` mappings) require explicit operator
  resolution (:func:`~NativeLocatorRegistry.resolve_legacy_collision`)
  — ``legacy_locator_collision`` names every candidate, never
  first-match; a live legacy carrier is NEVER silently renamed (the
  migration inventory lists old and new side by side; the runbook
  documents create-new → verify sentinel consumption → retire-old).
- The conformance dimension (:func:`delivery_template_conformance`):
  the selected DRIVER + template identity — the GitLab SDK lanes and
  batch routes validate against THEIR OWN templates, not
  claude-code's — plus the INSTALLED template digest
  (``profile_template_digest_mismatch`` when the target's installed
  template is not the one onboarding validated, even though the LOCAL
  shipped template passes), with STRUCTURAL consumer checks over
  substring presence.

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

import hashlib
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol, runtime_checkable

from forge.adaptive.operator_snapshot import CanonicalSubject
from forge.adaptive.project_credentials import (
    BINDING_SCHEMA,
    PROVIDER_ENV_VARS,
    PROVIDER_ROUTE_OF_DRIVER,
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
    "DEFAULT_OPERATION_GRANT_WINDOW_SECONDS",
    "DEFAULT_REGISTRY_TTL_SECONDS",
    "DELIVERY_PLAN_SCHEMA",
    "DELIVERY_ROUTE_ENV",
    "DELIVERY_TEMPLATE_DIR_ENV",
    "DEFAULT_AZURE_CREDENTIAL_GROUP",
    "DRIVER_TEMPLATE_FILES",
    "ENV_REF_SCHEME",
    "EVIDENCE_OPERATION_GRANTS_KEY",
    "LANE_CREDENTIAL_REDEEM_ROUTE",
    "NATIVE_LOCATOR_DIGEST_CHARS",
    "NATIVE_LOCATOR_PREFIX_MAX",
    "NATIVE_LOCATOR_SCHEMA",
    "NATIVE_LOCATOR_SEPARATOR",
    "OPERATION_GRANT_SCHEMA",
    "OPERATION_GRANT_WINDOW_ENV",
    "PERMITTED_OPERATION_REDEMPTION",
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
    "CredentialOperationGrant",
    "EnvBroker",
    "LegacyLocatorCollision",
    "NativeLocatorAllocation",
    "NativeLocatorRegistry",
    "ResolvedCredential",
    "SecretValue",
    "StagedBroker",
    "StagedDispatchCredential",
    "attempt_delivery_mode",
    "consumer_receipt_document",
    "credential_policy",
    "credential_secret_name",
    "credential_secret_segment",
    "declared_delivery_routes",
    "delivery_plan",
    "delivery_template_conformance",
    "locator_namespace",
    "merge_operation_grant",
    "native_locator",
    "native_locator_carrier",
    "native_route_structural_gaps",
    "operation_grant_for_plan",
    "operation_grant_key",
    "operation_grant_window_seconds",
    "operation_grants_for_attempt",
    "registry_ttl_seconds",
    "reveal_secret",
    "stage_dispatch_credential",
    "template_identity_digest",
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

#: The PER-DRIVER shipped template (Q39-04): on the gitlab profile
#: every driver dispatches through ITS OWN harness recipe — the SDK
#: lanes and the batch lanes are different files — so the conformance
#: validates the DISPATCHED driver's template, never claude-code's by
#: default. The github/azure profiles carry ONE parameterized template
#: per profile (``FORGE_DRIVER`` / the ``driver`` parameter), so those
#: profiles keep :data:`PROFILE_TEMPLATE_FILES`.
DRIVER_TEMPLATE_FILES: dict[str, str] = {
    "claude-code": "claude-code.gitlab-ci.yml",
    "claude-sdk-lane": "claude-sdk-lane.gitlab-ci.yml",
    "codex-sdk-lane": "codex-sdk-lane.gitlab-ci.yml",
    "copilot-sdk-lane": "copilot-sdk-lane.gitlab-ci.yml",
    "opencode-sdk-lane": "opencode-sdk-lane.gitlab-ci.yml",
    "opencode": "opencode.gitlab-ci.yml",
    "copilot": "copilot.gitlab-ci.yml",
    "grok-build": "grok.gitlab-ci.yml",
    "dotnet-lane": "dotnet-lane.gitlab-ci.yml",
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


# ---------------------------------------------------------------------------
# Q39-04 (#323) — the collision-safe native LOCATOR: a bounded readable
# prefix + a digest of the FULL ref, so distinct refs can never share a
# newly allocated carrier. The LEGACY segment mapping above stays the
# compat spelling until the operator migrates (never a silent rename).
# ---------------------------------------------------------------------------

#: The schema discriminator of the persisted native locator map.
NATIVE_LOCATOR_SCHEMA = "forge.credential.native-locator-map/1"

#: The separator between the readable prefix and the ref digest. The
#: carrier must be provisionable on every provider's native secret
#: facility, and the three charset surfaces accept ``[A-Z0-9_]`` and
#: NOT hyphens (GitHub's secret API refuses a hyphenated name with a
#: 422; GitLab CI/CD variable and Azure variable-group names follow
#: the env-var shape) — so the locator stays inside that intersection.
NATIVE_LOCATOR_SEPARATOR = "_"

#: The readable prefix's bound (characters): long enough to keep a
#: human-readable identifier, bounded so the whole carrier name stays
#: provider-short (12 prefix + 24 + 1 + 12 digest = 49 chars).
NATIVE_LOCATOR_PREFIX_MAX = 24

#: The ref digest's length (hex characters): 48 bits of sha256 over the
#: FULL ref. Collision safety within one namespace needs ~2^24 refs
#: before a 50% birthday chance — and the registry's allocation check
#: still refuses (typed) if one ever landed.
NATIVE_LOCATOR_DIGEST_CHARS = 12

#: The ref's segment separators: scheme, path, fragment, query pieces.
_LOCATOR_SEGMENT_SEPARATORS = re.compile(r"[:/#?&]")


def _locator_prefix(credential_ref: str) -> str:
    """The bounded ASCII-readable prefix of a ref: its LEAF identifier
    (the most human-identifying segment — ``team-a`` of
    ``vault:kv/team-a``), upper-cased, kept inside ``[A-Z0-9_]`` with
    every other character dropped (``team-a`` → ``TEAMA``), trimmed and
    truncated to :data:`NATIVE_LOCATOR_PREFIX_MAX`. A leaf with no ASCII
    identifier characters falls back to the WHOLE ref sanitized; a ref
    with none anywhere raises ``native_locator_unrepresentable`` (the
    digest could still disambiguate it, but an unreadable prefix helps
    no operator reconcile a carrier by hand — re-binding to an ASCII
    ref is the actionable fix)."""
    segments = [part for part in _LOCATOR_SEGMENT_SEPARATORS.split(str(credential_ref)) if part]
    candidates = [segments[-1], str(credential_ref)] if segments else [str(credential_ref)]
    for candidate in candidates:
        sanitized = "".join(
            char
            for char in str(candidate).upper()
            if char == "_" or (char.isascii() and char.isalnum())
        ).strip("_")
        if sanitized:
            return sanitized[:NATIVE_LOCATOR_PREFIX_MAX]
    raise CredentialRefusal(
        "native_locator_unrepresentable",
        {
            "credential_ref": str(credential_ref)[:32],
            "prefix_charset": "[A-Z0-9_]",
            "prefix_max": NATIVE_LOCATOR_PREFIX_MAX,
            "instruction": (
                "the credential ref carries no ASCII identifier characters to build a "
                "readable native locator from — re-bind the route to a ref with at "
                "least one ASCII letter or digit (the ref is an id you choose, never "
                "the secret value)"
            ),
        },
    )


def native_locator(credential_ref: str) -> str:
    """The collision-safe native carrier LOCATOR of a credential ref
    (Q39-04): ``<PREFIX><SEP><DIGEST12>`` — a bounded ASCII-readable
    prefix + the first 12 hex of sha256 over the FULL ref.

    ``vault:kv/team-a`` → ``TEAMA_505D2A14EE03``; the full carrier name
    (:func:`native_locator_carrier`) is
    ``FORGE_MODEL_TEAMA_505D2A14EE03``. Two refs differing by
    slash/dash/underscore/case — or any Unicode detail — can NEVER share
    a locator: the digest is over the exact ref, so the readable prefix
    carrying over (``team-a`` and ``team_a`` both read as ``TEAM…``) is
    cosmetic, never an alias. Overlength and Unicode refs get the same
    bounded form (the prefix truncates, the digest disambiguates); a ref
    with no ASCII identifier characters anywhere refuses typed
    (``native_locator_unrepresentable``). The locator is CANONICAL
    UPPERCASE ``[A-Z0-9_]`` — the intersection GitHub secret names,
    GitLab CI/CD variable names and Azure variable-group names all
    accept (a ``-`` separator would be refused by GitHub's secret API at
    provisioning), and one canonical case cannot near-miss under
    GitHub's case-insensitive secret-name uniqueness.
    """
    digest = hashlib.sha256(str(credential_ref).encode("utf-8")).hexdigest()
    return (
        f"{_locator_prefix(credential_ref)}"
        f"{NATIVE_LOCATOR_SEPARATOR}"
        f"{digest[:NATIVE_LOCATOR_DIGEST_CHARS].upper()}"
    )


def native_locator_carrier(credential_ref: str) -> str:
    """The collision-safe native carrier NAME of a ref:
    ``FORGE_MODEL_<LOCATOR>`` (the locator under the same
    :data:`CREDENTIAL_SECRET_PREFIX` every native profile uses)."""
    return CREDENTIAL_SECRET_PREFIX + native_locator(credential_ref)


def locator_namespace(subject: CanonicalSubject | str, profile: str) -> str:
    """The provider SECRET NAMESPACE a native carrier is allocated in:
    the dispatch profile + the canonical subject — ``<profile>/<family>/
    <connection>/<native_id>``. The subject's connection + native id IS
    the provider project/repo identity, so two independent projects'
    namespaces are independent (the same locator may live in both — no
    global uniqueness is required or wanted). A SHARED namespace (a
    GitLab group, a GitHub org/environment) is named explicitly by the
    operator through the registry API (see docs/operations/
    native-locators.md) — never guessed from a subject it does not
    cover."""
    subject_id = getattr(subject, "subject_id", None)
    identity = subject_id() if callable(subject_id) else str(subject)
    return f"{str(profile or '').strip()}/{identity}"


@dataclass(frozen=True)
class NativeLocatorAllocation:
    """One persisted allocation in the ``native_locator_map``: the ref,
    its locator and carrier name, the namespace + route it was
    allocated in, the allocation timestamp — and, for a MIGRATED ref,
    the LEGACY carrier (``FORGE_MODEL_<SEGMENT>``) the pre-#323 era
    provisioned, kept until the operator retires it (never a silent
    rename). Refs and timestamps only — no value, by construction."""

    credential_ref: str
    locator: str
    carrier_name: str
    namespace: str
    route: str
    allocated_at: str
    legacy_carrier: str = ""
    legacy_retired_at: str = ""

    def as_document(self) -> dict[str, Any]:
        return {
            "credential_ref": self.credential_ref,
            "locator": self.locator,
            "carrier_name": self.carrier_name,
            "namespace": self.namespace,
            "route": self.route,
            "allocated_at": self.allocated_at,
            "legacy_carrier": self.legacy_carrier,
            "legacy_retired_at": self.legacy_retired_at,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> NativeLocatorAllocation:
        """Load one allocation; a malformed document is the TYPED
        ``native_locator_map_invalid`` refusal (fail closed — a corrupt
        map never degrades into a permissive alias)."""
        required = {
            "credential_ref": str(document.get("credential_ref") or "").strip(),
            "locator": str(document.get("locator") or "").strip(),
            "carrier_name": str(document.get("carrier_name") or "").strip(),
            "namespace": str(document.get("namespace") or "").strip(),
            "route": str(document.get("route") or "").strip(),
            "allocated_at": str(document.get("allocated_at") or "").strip(),
        }
        missing = sorted(name for name, value in required.items() if not value)
        if missing:
            raise CredentialRefusal(
                "native_locator_map_invalid",
                {
                    "credential_ref": required["credential_ref"][:24],
                    "missing": missing,
                    "instruction": "repair or regenerate the native locator map document",
                },
            )
        return cls(
            credential_ref=required["credential_ref"],
            locator=required["locator"],
            carrier_name=required["carrier_name"],
            namespace=required["namespace"],
            route=required["route"],
            allocated_at=required["allocated_at"],
            legacy_carrier=str(document.get("legacy_carrier") or ""),
            legacy_retired_at=str(document.get("legacy_retired_at") or ""),
        )


@dataclass(frozen=True)
class LegacyLocatorCollision:
    """One LEGACY collision group: refs whose pre-locator
    ``FORGE_MODEL_<SEGMENT>`` mapping shared a carrier within one
    namespace + route. Listed by the migration inventory; resolving one
    is an explicit operator act
    (:meth:`NativeLocatorRegistry.resolve_legacy_collision`), never a
    first-match."""

    namespace: str
    route: str
    legacy_carrier: str
    candidates: tuple[str, ...]

    def as_document(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "route": self.route,
            "legacy_carrier": self.legacy_carrier,
            "candidates": list(self.candidates),
        }


@dataclass
class NativeLocatorRegistry:
    """The persisted ``native_locator_map`` (Q39-04): WHO owns WHICH
    native carrier, per provider namespace + route.

    - :meth:`allocate` mints the ref's collision-safe locator
      (idempotent per namespace+route+ref — the allocation timestamp is
      FROZEN at first allocation) and refuses ``native_locator_collision``
      (typed, both refs named) when the locator is already held by a
      DIFFERENT ref in the SAME namespace + route. Namespaces compare
      CASE-INSENSITIVELY (GitHub secret names are case-insensitively
      unique; ``GitLab/Example/1`` and ``gitlab/example/1`` are one
      namespace) and routes are compared exactly — independent project
      namespaces never collide with each other by design.
    - :meth:`adopt_legacy` records the MIGRATION inventory: the
      pre-#323 carrier a live ref already uses (``FORGE_MODEL_<SEGMENT>``
      — the lossy spelling). Adopting NEVER renames anything: the
      legacy carrier stays live until the operator runs the runbook
      (create-new → verify sentinel consumption → retire-old).
    - :meth:`resolve_legacy` resolves a legacy segment to ONE ref;
      an ambiguous group refuses ``legacy_locator_collision`` with
      EVERY candidate named — never first-match.

    Persistence mirrors :class:`ConcurrentCredentialRegistry`: a JSON
    document landed through EXCLUSIVE lock + atomic rename (no torn
    documents, no lost read-modify-writes); *path* ``None`` keeps the
    map in memory (tests, ephemeral use).
    """

    path: Path | None = None

    _allocations: dict[tuple[str, str, str], NativeLocatorAllocation] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.path is not None and self.path.is_file():
            self._load()

    # -- the document -----------------------------------------------------

    @staticmethod
    def _key(credential_ref: str) -> str:
        return str(credential_ref)

    @staticmethod
    def _namespace_key(namespace: str) -> str:
        return str(namespace).strip().casefold()

    def _load(self) -> None:
        assert self.path is not None
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CredentialRefusal(
                "native_locator_map_invalid",
                {
                    "path": str(self.path),
                    "problem": str(exc)[:120],
                    "instruction": (
                        "the persisted native locator map is unreadable — repair it or "
                        "move it aside (allocations then re-mint idempotently; legacy "
                        "carriers keep their live names either way)"
                    ),
                },
            ) from exc
        if document.get("schema") != NATIVE_LOCATOR_SCHEMA:
            raise CredentialRefusal(
                "native_locator_map_invalid",
                {
                    "path": str(self.path),
                    "schema": str(document.get("schema")),
                    "expected": NATIVE_LOCATOR_SCHEMA,
                    "instruction": "repair or regenerate the native locator map document",
                },
            )
        allocations: dict[tuple[str, str, str], NativeLocatorAllocation] = {}
        for raw in document.get("allocations") or []:
            if not isinstance(raw, Mapping):
                continue  # an unreadable row is skipped, never trusted
            allocation = NativeLocatorAllocation.from_document(raw)
            key = (
                self._namespace_key(allocation.namespace),
                allocation.route,
                self._key(allocation.credential_ref),
            )
            allocations[key] = allocation
        self._allocations = allocations

    def as_document(self) -> dict[str, Any]:
        return {
            "schema": NATIVE_LOCATOR_SCHEMA,
            "allocations": [
                allocation.as_document()
                for allocation in sorted(
                    self._allocations.values(),
                    key=lambda allocation: (
                        allocation.namespace,
                        allocation.route,
                        allocation.credential_ref,
                    ),
                )
            ],
        }

    def _persist(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(
            json.dumps(self.as_document(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(tmp, self.path)

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """The exclusive write lock (flock on the sibling ``.lock``
        file) — the same multi-writer contract as
        :class:`ConcurrentCredentialRegistry`; a no-op for an in-memory
        map."""
        if self.path is None:
            yield
            return
        import fcntl

        lock_path = self.path.with_name(self.path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _locked_write(self, operate: Any) -> Any:
        if self.path is None:
            return operate()
        with self._exclusive():
            if self.path.is_file():
                self._load()
            result = operate()
            self._persist()
            return result

    # -- the lookups ------------------------------------------------------

    def allocations(self, *, namespace: str = "", route: str = "") -> list[NativeLocatorAllocation]:
        """The allocations, optionally narrowed to one namespace/route
        (the namespace compares case-insensitively)."""
        wanted_ns = self._namespace_key(namespace) if namespace else None
        return sorted(
            (
                allocation
                for allocation in self._allocations.values()
                if (wanted_ns is None or self._namespace_key(allocation.namespace) == wanted_ns)
                and (not route or allocation.route == route)
            ),
            key=lambda allocation: allocation.credential_ref,
        )

    def allocation_for(
        self, credential_ref: str, *, namespace: str, route: str
    ) -> NativeLocatorAllocation | None:
        """The ref's existing allocation in one namespace + route, or
        None when the ref was never allocated there."""
        return self._allocations.get(
            (self._namespace_key(namespace), route, self._key(credential_ref))
        )

    def _holder_of_locator(
        self, locator: str, *, namespace: str, route: str
    ) -> NativeLocatorAllocation | None:
        wanted = str(locator).upper()
        for allocation in self._allocations.values():
            if (
                self._namespace_key(allocation.namespace) == self._namespace_key(namespace)
                and allocation.route == route
                and allocation.locator.upper() == wanted
            ):
                return allocation
        return None

    def _legacy_group(
        self, legacy_carrier: str, *, namespace: str, route: str
    ) -> list[NativeLocatorAllocation]:
        """The refs sharing one legacy carrier in a namespace + route —
        the live legacy carrier matches CASE-INSENSITIVELY (GitHub
        secret names are case-insensitively unique, so ``…_TEAM_A`` and
        ``…_team_a`` are one carrier there)."""
        wanted = str(legacy_carrier).strip().upper()
        if wanted and not wanted.startswith(CREDENTIAL_SECRET_PREFIX):
            wanted = CREDENTIAL_SECRET_PREFIX + wanted
        return sorted(
            (
                allocation
                for allocation in self._allocations.values()
                if allocation.legacy_carrier
                and not allocation.legacy_retired_at
                and allocation.legacy_carrier.upper() == wanted
                and self._namespace_key(allocation.namespace) == self._namespace_key(namespace)
                and allocation.route == route
            ),
            key=lambda allocation: allocation.credential_ref,
        )

    # -- the write side ---------------------------------------------------

    def _collision_refusal(
        self,
        *,
        reason: str,
        namespace: str,
        route: str,
        carrier: str,
        holder: str,
        credential_ref: str,
        instruction: str,
    ) -> CredentialRefusal:
        return CredentialRefusal(
            reason,
            {
                "observability": "credential.native_locator_collision",
                "namespace": namespace,
                "route": route,
                "carrier": carrier,
                "candidates": sorted({holder, credential_ref}),
                "instruction": instruction,
            },
        )

    def allocate(
        self,
        credential_ref: str,
        *,
        namespace: str,
        route: str,
        now: datetime | None = None,
    ) -> NativeLocatorAllocation:
        """Mint (idempotently) the ref's collision-safe locator within
        ONE provider namespace + route.

        - an existing allocation for the SAME ref returns unchanged
          (the timestamp frozen at first allocation — a re-dispatch
          never re-anchors it);
        - a locator already held by a DIFFERENT ref in the same
          namespace + route refuses ``native_locator_collision`` (both
          refs named; observability ``credential.native_locator_collision``)
          — the registry defends its own invariant even against a
          hand-edited map;
        - the same locator in a DIFFERENT namespace or route is FINE
          (independent project namespaces are independent).
        """
        return self._locked_write(
            lambda: self._allocate_unlocked(
                credential_ref, namespace=namespace, route=route, now=now
            )
        )

    def _allocate_unlocked(
        self,
        credential_ref: str,
        *,
        namespace: str,
        route: str,
        now: datetime | None = None,
    ) -> NativeLocatorAllocation:
        locator = native_locator(credential_ref)
        carrier = CREDENTIAL_SECRET_PREFIX + locator
        # The registry's own invariant FIRST (before the idempotent
        # return): the locator is held by a DIFFERENT ref in this
        # namespace+route → typed refusal naming both — the digest
        # allocator makes this near-impossible, but the JSON map is
        # operator-editable and the registry defends itself.
        holder = self._holder_of_locator(locator, namespace=namespace, route=route)
        if holder is not None and holder.credential_ref != credential_ref:
            raise self._collision_refusal(
                reason="native_locator_collision",
                namespace=namespace,
                route=route,
                carrier=carrier,
                holder=holder.credential_ref,
                credential_ref=credential_ref,
                instruction=(
                    f"the locator {locator} is already allocated to ref "
                    f"{holder.credential_ref!r} in this namespace+route — resolve "
                    "the native locator map by hand (the digest allocator made "
                    "this near-impossible; a hand-edited map did not)"
                ),
            )
        existing = self.allocation_for(credential_ref, namespace=namespace, route=route)
        if existing is not None:
            return existing
        legacy = self._legacy_group(
            credential_secret_name(credential_ref), namespace=namespace, route=route
        )
        for allocation in legacy:
            if allocation.credential_ref != credential_ref:
                raise self._collision_refusal(
                    reason="native_locator_collision",
                    namespace=namespace,
                    route=route,
                    carrier=credential_secret_name(credential_ref),
                    holder=allocation.credential_ref,
                    credential_ref=credential_ref,
                    instruction=(
                        "the legacy carrier of this ref collides with another "
                        "adopted legacy ref — resolve the legacy group first "
                        "(resolve_legacy_collision), then allocate"
                    ),
                )
        allocation = NativeLocatorAllocation(
            credential_ref=str(credential_ref),
            locator=locator,
            carrier_name=carrier,
            namespace=str(namespace),
            route=str(route),
            allocated_at=(now or datetime.now(timezone.utc)).isoformat(),
        )
        self._allocations[
            (self._namespace_key(namespace), str(route), self._key(credential_ref))
        ] = allocation
        return allocation

    def adopt_legacy(
        self,
        credential_ref: str,
        *,
        namespace: str,
        route: str,
        now: datetime | None = None,
    ) -> NativeLocatorAllocation:
        """Record ONE pre-#323 mapping in the migration inventory: the
        ref's LEGACY carrier (``FORGE_MODEL_<SEGMENT>`` — the lossy
        spelling) stays the LIVE carrier; the collision-safe locator is
        computed and recorded BESIDE it (the migration target), but
        NOTHING is renamed here. Two refs may adopt the SAME legacy
        carrier — that is the collision group the inventory exists to
        surface (:meth:`legacy_collision_groups`), recorded, never
        silently resolved."""

        def operate() -> NativeLocatorAllocation:
            existing = self.allocation_for(credential_ref, namespace=namespace, route=route)
            legacy_carrier = credential_secret_name(credential_ref)
            if existing is not None:
                if existing.legacy_carrier and existing.legacy_carrier != legacy_carrier:
                    raise CredentialRefusal(
                        "native_locator_map_invalid",
                        {
                            "credential_ref": str(credential_ref)[:24],
                            "recorded_legacy": existing.legacy_carrier,
                            "presented_legacy": legacy_carrier,
                            "instruction": (
                                "the ref already carries a different legacy carrier — "
                                "retire it before adopting another"
                            ),
                        },
                    )
                updated = replace(existing, legacy_carrier=legacy_carrier)
                self._allocations[
                    (self._namespace_key(namespace), str(route), self._key(credential_ref))
                ] = updated
                return updated
            allocation = NativeLocatorAllocation(
                credential_ref=str(credential_ref),
                locator=native_locator(credential_ref),
                carrier_name=native_locator_carrier(credential_ref),
                namespace=str(namespace),
                route=str(route),
                allocated_at=(now or datetime.now(timezone.utc)).isoformat(),
                legacy_carrier=legacy_carrier,
            )
            self._allocations[
                (self._namespace_key(namespace), str(route), self._key(credential_ref))
            ] = allocation
            return allocation

        return self._locked_write(operate)

    def legacy_collision_groups(
        self, *, namespace: str = "", route: str = ""
    ) -> list[LegacyLocatorCollision]:
        """Every LEGACY collision group in the map (optionally narrowed
        to one namespace/route): refs sharing one legacy carrier — the
        ambiguity the pre-locator spelling created. The migration
        inventory surface; empty means no operator resolution owed."""
        groups: dict[tuple[str, str, str], dict[str, Any]] = {}
        for allocation in self.allocations(namespace=namespace, route=route):
            if not allocation.legacy_carrier or allocation.legacy_retired_at:
                continue
            key = (
                self._namespace_key(allocation.namespace),
                allocation.route,
                allocation.legacy_carrier.upper(),
            )
            group = groups.setdefault(
                key,
                {"namespace": allocation.namespace, "route": allocation.route, "refs": []},
            )
            group["refs"].append(allocation.credential_ref)
        return [
            LegacyLocatorCollision(
                namespace=group["namespace"],
                route=group["route"],
                legacy_carrier=carrier,
                candidates=tuple(sorted(group["refs"])),
            )
            for (_ns_key, _route, carrier), group in sorted(groups.items())
            if len(group["refs"]) > 1
        ]

    def resolve_legacy(
        self, legacy_carrier: str, *, namespace: str, route: str
    ) -> NativeLocatorAllocation:
        """Resolve a LEGACY carrier to its ONE owning ref — the lookup
        an in-flight pre-upgrade lane (dispatched under the old
        spelling) needs. Zero candidates → ``legacy_locator_absent``;
        two or more → ``legacy_locator_collision`` with EVERY candidate
        named (never first-match): the operator resolves the group
        explicitly (:meth:`resolve_legacy_collision`)."""
        group = self._legacy_group(legacy_carrier, namespace=namespace, route=route)
        if not group:
            raise CredentialRefusal(
                "legacy_locator_absent",
                {
                    "namespace": namespace,
                    "route": route,
                    "legacy_carrier": str(legacy_carrier),
                    "instruction": (
                        "no adopted legacy mapping holds this carrier in this "
                        "namespace+route — adopt the pre-upgrade refs first "
                        "(adopt_legacy) or dispatch under the locator profile"
                    ),
                },
            )
        if len(group) > 1:
            candidates = [allocation.credential_ref for allocation in group]
            raise self._collision_refusal(
                reason="legacy_locator_collision",
                namespace=namespace,
                route=route,
                carrier=group[0].legacy_carrier,
                holder=candidates[0],
                credential_ref=candidates[-1],
                instruction=(
                    f"the legacy carrier is ambiguous between {candidates} — resolve "
                    "the group explicitly (registry.resolve_legacy_collision with the "
                    "ref that keeps the live carrier), never a first match"
                ),
            )
        return group[0]

    def resolve_legacy_collision(
        self,
        *,
        namespace: str,
        route: str,
        legacy_carrier: str,
        keep_ref: str,
        now: datetime | None = None,
    ) -> list[NativeLocatorAllocation]:
        """The EXPLICIT operator resolution of a legacy collision group:
        *keep_ref* keeps the live legacy carrier; every OTHER ref in
        the group is allocated its own collision-safe locator (the
        create-new half of the runbook — the operator then provisions
        the new carrier, verifies sentinel consumption, and retires the
        old one). Nothing is renamed: both carriers coexist until the
        operator retires the legacy one."""

        def operate() -> list[NativeLocatorAllocation]:
            group = self._legacy_group(legacy_carrier, namespace=namespace, route=route)
            if not group:
                raise CredentialRefusal(
                    "legacy_locator_absent",
                    {
                        "namespace": namespace,
                        "route": route,
                        "legacy_carrier": str(legacy_carrier),
                        "instruction": "no legacy group holds this carrier here",
                    },
                )
            if not any(allocation.credential_ref == keep_ref for allocation in group):
                raise CredentialRefusal(
                    "legacy_locator_collision",
                    {
                        "observability": "credential.native_locator_collision",
                        "namespace": namespace,
                        "route": route,
                        "legacy_carrier": str(legacy_carrier),
                        "candidates": [allocation.credential_ref for allocation in group],
                        "keep_ref": keep_ref,
                        "instruction": (
                            "the ref named to keep the legacy carrier is not one of the "
                            "colliding candidates — name one of the candidates"
                        ),
                    },
                )
            resolved: list[NativeLocatorAllocation] = []
            for allocation in group:
                if allocation.credential_ref == keep_ref:
                    resolved.append(allocation)
                    continue
                # The losing ref DROPS its legacy claim (the ambiguity
                # ends here — this is the explicit operator act) and
                # keeps its collision-safe locator record: create-new
                # is provisioning the locator carrier on the provider;
                # the record, timestamps and audit trail are unchanged.
                updated = replace(allocation, legacy_carrier="")
                self._allocations[
                    (self._namespace_key(namespace), route, self._key(allocation.credential_ref))
                ] = updated
                resolved.append(updated)
            return resolved

        return self._locked_write(operate)

    def retire_legacy(
        self,
        credential_ref: str,
        *,
        namespace: str,
        route: str,
        now: datetime | None = None,
    ) -> NativeLocatorAllocation | None:
        """The retire-old half of the runbook: after the operator
        provisioned the locator carrier and VERIFIED sentinel
        consumption through it, the legacy carrier is marked retired
        (timestamped) — it stops resolving, but nothing is deleted: the
        audit trail keeps the mapping. Returns the updated allocation,
        or None when the ref has no allocation."""

        def operate() -> NativeLocatorAllocation | None:
            existing = self.allocation_for(credential_ref, namespace=namespace, route=route)
            if existing is None:
                return None
            updated = replace(
                existing,
                legacy_retired_at=(now or datetime.now(timezone.utc)).isoformat(),
            )
            self._allocations[
                (self._namespace_key(namespace), route, self._key(credential_ref))
            ] = updated
            return updated

        return self._locked_write(operate)


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
    #: Q39-01 (#320): the OPERATION GRANT minted for a redemption-mode
    #: plan when the caller named the work + attempt (``None`` under the
    #: native modes and for callers that plan without an attempt
    #: identity). The caller persists it beside this plan BEFORE the
    #: lane can exist to redeem against it.
    operation_grant: CredentialOperationGrant | None = None
    #: Q39-04 (#323): the collision-safe LOCATOR the plan dispatched
    #: under (``""`` — the legacy segment spelling — when no locator
    #: registry governed the allocation). A locator, never a value.
    native_locator: str = ""
    #: The provider secret namespace the locator was allocated in
    #: (``""`` when none) — :func:`locator_namespace`.
    locator_namespace: str = ""
    #: The DISPATCHED driver whose OWN shipped template the conformance
    #: validated (``""`` — the profile default — when the caller named
    #: no driver).
    template_driver: str = ""
    #: The identity digest of the template text the conformance
    #: validated (:func:`template_identity_digest`; ``""`` when the
    #: conformance did not run).
    template_digest: str = ""

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
            "native_locator": self.native_locator,
            "locator_namespace": self.locator_namespace,
            "template_driver": self.template_driver,
            "template_digest": self.template_digest,
        }


def _shipped_template_text(
    profile: str, environ: Mapping[str, str] | None, *, driver: str = ""
) -> str | None:
    """The SHIPPED lane template's text the DISPATCHED driver's
    conformance reads (its OWN recipe on the gitlab profile —
    :data:`DRIVER_TEMPLATE_FILES` — the profile's single template
    otherwise), or None when no template copy is reachable (the
    conformance then refuses typed)."""
    source = os.environ if environ is None else environ
    configured = str(source.get(DELIVERY_TEMPLATE_DIR_ENV, "") or "").strip()
    root = Path(configured) if configured else Path.cwd() / "ci" / "templates"
    filename = PROFILE_TEMPLATE_FILES[profile]
    if driver and profile == "gitlab":
        filename = DRIVER_TEMPLATE_FILES.get(driver, filename)
    path = root / filename
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def template_identity_digest(template_text: str) -> str:
    """The identity digest of a template text (first 16 hex of sha256)
    — the value onboarding records for the INSTALLED target template
    and every compatibility-sensitive change re-verifies. A digest of
    TEMPLATE text, never of credential material."""
    return hashlib.sha256(str(template_text).encode("utf-8")).hexdigest()[:16]


#: The credential-consumption guard line, structurally (the same shape
#: every shipped recipe's block opens with — a restructured block fails
#: the structural check loudly instead of passing on a substring).
_NATIVE_ROUTE_GUARD_RE = re.compile(
    r"^\s*if \[ -n \"\$\{FORGE_CREDENTIAL_REF:-\}\" \] && "
    r"\[ \"\$\{FORGE_CREDENTIAL_REDEEM:-\}\" != \"1\" \]; then",
    re.MULTILINE,
)

#: The consumer mapping, structurally: the export that moves the
#: delivered credential into the provider env slot the model process
#: reads (``export VAR="$_CRED_VALUE"`` on the gitlab lanes,
#: ``="$FORGE_MODEL_CREDENTIAL"`` on GitHub).
_NATIVE_ROUTE_EXPORT_RE = re.compile(
    r"^\s*export\s+(?P<var>[A-Z0-9_]+)=\"\$\w+\"\s*$", re.MULTILINE
)

#: The Azure shape: the block reads the mapped secret variable directly
#: (``[ -z "${ANTHROPIC_AUTH_TOKEN:-}" ]``) — the YAML env mapping, not
#: an export, carries the value.
_NATIVE_ROUTE_SLOT_CHECK_RE = re.compile(r"\[\s+-z\s+\"\$\{(?P<var>[A-Z0-9_]+):-\}\"\s+\]")


def native_route_structural_gaps(template_text: str, env_var: str) -> list[str]:
    """The STRUCTURAL gaps in a template's native credential route for
    *env_var* (Q39-04): the guard line, a consumer mapping of the env
    slot (the export form the gitlab/GitHub lanes use, or the direct
    slot check the Azure block uses), and the fail-closed marker. A
    restructured-but-equivalent block still passes (the regexes accept
    the shape, not a frozen string); a block that merely MENTIONS the
    route in a comment or a removed mapping fails — structural checks
    over substring presence, per the issue."""
    text = str(template_text)
    gaps: list[str] = []
    if not _NATIVE_ROUTE_GUARD_RE.search(text):
        gaps.append("credential_guard")
    consumed = any(
        (match.group("var") == env_var) for match in _NATIVE_ROUTE_EXPORT_RE.finditer(text)
    ) or any(match.group("var") == env_var for match in _NATIVE_ROUTE_SLOT_CHECK_RE.finditer(text))
    if not consumed:
        gaps.append(f"consumer_mapping:{env_var}")
    if "FORGE_BOOTSTRAP_FAILED" not in text:
        gaps.append("fail_closed_marker")
    return gaps


def _assert_driver_route_known(driver: str) -> None:
    """A named driver whose credential-consumption route is unknown
    (no provider route, no shipped template) refuses typed
    ``consumer_route_unknown`` — the conformance cannot validate a
    route nobody can name, so the dispatch fails closed instead."""
    name = str(driver or "").strip()
    if not name:
        return
    if name in DRIVER_TEMPLATE_FILES or name in PROVIDER_ROUTE_OF_DRIVER:
        return
    raise CredentialRefusal(
        "consumer_route_unknown",
        {
            "observability": "credential.consumer_route_unknown",
            "driver": name,
            "known_drivers": sorted(DRIVER_TEMPLATE_FILES),
            "instruction": (
                f"the driver {name!r} names no credential-consumption route (no "
                "provider route, no shipped template) — fix the driver selection or "
                "ship its template before dispatching a bound credential through it"
            ),
        },
    )


def delivery_template_conformance(
    plan: CredentialDeliveryPlan,
    template_text: str,
    *,
    driver: str = "",
    expected_template_digest: str = "",
) -> None:
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

    Q39-04 (#323) adds two dimensions on top of the marker checks:

    - *driver* — the DISPATCHED driver's template identity. On the
      gitlab profile every driver ships its OWN recipe (the SDK lanes
      and the batch lanes are different files), so the caller validates
      the text of THAT driver's template: the structural consumer
      checks (:func:`native_route_structural_gaps`) must hold for the
      plan's env slot. A wrong driver's template — one whose recipe
      does not implement the negotiated route — refuses typed, never a
      claude-code-shaped pass. An unknown driver refuses
      ``consumer_route_unknown``.
    - *expected_template_digest* — the INSTALLED target template's
      digest as onboarding recorded it. A mismatch refuses
      ``profile_template_digest_mismatch`` (observability
      ``profile.template_digest_mismatch``) BEFORE any marker check:
      the local shipped template passing proves nothing about a target
      whose installed template is a different text.
    """
    _assert_driver_route_known(driver)
    if expected_template_digest:
        actual = template_identity_digest(template_text)
        if actual != str(expected_template_digest).strip().lower():
            raise CredentialRefusal(
                "profile_template_digest_mismatch",
                {
                    "observability": "profile.template_digest_mismatch",
                    "profile": plan.profile,
                    "driver": driver,
                    "expected_digest": str(expected_template_digest),
                    "actual_digest": actual,
                    "instruction": (
                        "the INSTALLED target template's digest differs from the one "
                        "onboarding recorded — re-run the target's onboarding (the "
                        "shipped template must be installed verbatim) before "
                        "dispatching a bound credential; a local-template pass is "
                        "not evidence about a different installed text"
                    ),
                },
            )
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
                "driver": driver,
                "transport_ref": plan.transport_ref,
                "missing_markers": missing,
                "instruction": instruction,
            },
        )
    if not driver:
        return
    # The driver dimension (Q39-04): the DISPATCHED driver's own recipe
    # must STRUCTURALLY implement the negotiated route — the guard, the
    # consumer mapping of the plan's env slot, the fail-closed marker.
    # The gitlab SDK lanes and batch routes are validated against THEIR
    # templates here, not claude-code's; a parameterized single template
    # (github/azure) passes the same structural floor.
    gaps = native_route_structural_gaps(template_text, plan.env_var)
    if gaps:
        template_name = (
            DRIVER_TEMPLATE_FILES.get(driver, PROFILE_TEMPLATE_FILES.get(plan.profile, ""))
            if plan.profile == "gitlab"
            else PROFILE_TEMPLATE_FILES.get(plan.profile, "")
        )
        raise CredentialRefusal(
            "delivery_template_mismatch",
            {
                "profile": plan.profile,
                "mode": plan.mode,
                "driver": driver,
                "template": template_name,
                "transport_ref": plan.transport_ref,
                "structural_gaps": gaps,
                "instruction": (
                    f"the dispatched driver {driver!r} consumes the credential through "
                    f"{template_name}, whose recipe does not structurally implement the "
                    f"negotiated route ({', '.join(gaps)}) — upgrade THAT template to "
                    "the shipped credential-consumption block, or dispatch a driver "
                    "whose lane supports the route"
                ),
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
    work_id: str = "",
    attempt_generation: int | None = None,
    environ: Mapping[str, str] | None = None,
    locator_registry: NativeLocatorRegistry | None = None,
    driver: str = "",
    installed_template_digest: str = "",
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

    Q39-01 (#320): a redemption-mode plan minted with *work_id* +
    *attempt_generation* carries its :class:`CredentialOperationGrant`
    (``plan.operation_grant``) — the dispatch caller persists it beside
    the plan, BEFORE the provider call, so the lane that boots can only
    ever redeem against the authorization this dispatch actually gave it.

    Q39-04 (#323) adds three optional dimensions:

    - *locator_registry* — a :class:`NativeLocatorRegistry` makes the
      native modes allocate the ref's COLLISION-SAFE locator
      (:func:`native_locator`) within the dispatch's provider namespace
      + route: the transport becomes ``FORGE_MODEL_<LOCATOR>`` and
      ``dispatch_ref`` the locator. Without one, the legacy
      ``FORGE_MODEL_<SEGMENT>`` spelling stays (NEVER a silent rename —
      the migration runbook owns that transition).
    - *driver* — the dispatched driver's OWN shipped template is the
      conformance surface on the gitlab profile
      (:data:`DRIVER_TEMPLATE_FILES`); an unknown driver refuses
      ``consumer_route_unknown``.
    - *installed_template_digest* — the digest onboarding recorded for
      the INSTALLED target template; a mismatch refuses
      ``profile_template_digest_mismatch`` before any marker check
      (the local template passing is not evidence about another text).
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
    _assert_driver_route_known(driver)
    segment = credential_secret_segment(dispatch_credential.credential_ref)
    locator = ""
    locator_namespace_value = ""
    if locator_registry is not None and mode != DELIVERY_MODE_RUNNER_REDEMPTION:
        # The collision-safe allocation (Q39-04): the locator carrier
        # within THIS provider namespace + route. Azure's carrier stays
        # the env-slot-named group secret (fixed per route — no ref
        # spelling in it to collide); the locator is still the dispatch
        # identity the lane's guard interpolates.
        namespace = locator_namespace(dispatch_credential.subject, profile)
        allocation = locator_registry.allocate(
            dispatch_credential.credential_ref, namespace=namespace, route=mode
        )
        locator = allocation.locator
        locator_namespace_value = namespace
        segment = allocation.locator
    if mode == DELIVERY_MODE_GITHUB_NATIVE or mode == DELIVERY_MODE_GITLAB_PROTECTED:
        transport_ref = (
            CREDENTIAL_SECRET_PREFIX + locator
            if locator
            else credential_secret_name(dispatch_credential.credential_ref)
        )
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
        native_locator=locator,
        locator_namespace=locator_namespace_value,
    )
    template_text = _shipped_template_text(profile, source, driver=driver)
    if template_text is None:
        raise CredentialRefusal(
            "delivery_template_unavailable",
            {
                "profile": profile,
                "mode": mode,
                "driver": driver,
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
    expected = installed_template_digest or ""
    delivery_template_conformance(
        plan,
        template_text,
        driver=driver,
        expected_template_digest=expected,
    )
    plan = replace(
        plan, template_driver=driver, template_digest=template_identity_digest(template_text)
    )
    if redemption and work_id and attempt_generation is not None:
        # Q39-01 (#320): authorizing a redemption-mode dispatch MINTS the
        # operation grant — the persisted authorization object the lane's
        # redemption will be judged against. The caller persists it beside
        # this plan (idempotently, per attempt+route+ref) BEFORE the lane
        # can exist to dial the endpoint.
        grant = operation_grant_for_plan(
            plan, work_id=work_id, attempt_generation=attempt_generation, environ=source
        )
        return replace(plan, operation_grant=grant)
    return plan


# ---------------------------------------------------------------------------
# Q39-01 (#320) — the OPERATION GRANT: the persisted authorization object a
# redemption is judged against. Distinct from the delivery PLAN (HOW the
# credential travels) and from the response's ``expires_at`` (presentation
# metadata): the grant says WHICH operation may redeem WHICH exact ref,
# until WHICH fixed instant.
# ---------------------------------------------------------------------------

#: The schema discriminator every persisted operation grant carries.
OPERATION_GRANT_SCHEMA = "forge.credential.operation-grant/1"

#: Where the deployment declares the grant's redemption WINDOW (seconds)
#: — the span from dispatch authorization to the grant's absolute
#: redemption deadline. Computed ONCE at grant creation and persisted as
#: the deadline instant; a restart never re-derives it.
OPERATION_GRANT_WINDOW_ENV = "FORGE_CREDENTIAL_GRANT_WINDOW_SECONDS"

#: The default redemption window (seconds) — one hour, the same span the
#: redemption TTL defaults to.
DEFAULT_OPERATION_GRANT_WINDOW_SECONDS = 3600.0

#: The one permitted operation a redemption-mode grant authorizes.
PERMITTED_OPERATION_REDEMPTION = "credential-redemption"

#: The run-evidence key holding the per-attempt grant documents (a dict
#: keyed by :func:`operation_grant_key` — attempt generation + route).
EVIDENCE_OPERATION_GRANTS_KEY = "credential_operation_grants"


def operation_grant_window_seconds(environ: Mapping[str, str] | None = None) -> float:
    """The grant redemption window from env (default 1h). Operator state:
    a malformed or non-positive value is a typed failure, never a silent
    re-default (the same doctrine as every credential-window knob)."""
    source = os.environ if environ is None else environ
    raw = str(source.get(OPERATION_GRANT_WINDOW_ENV, "") or "").strip()
    if not raw:
        return float(DEFAULT_OPERATION_GRANT_WINDOW_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        raise CredentialRefusal(
            "operation_grant_window_invalid",
            {
                "env": OPERATION_GRANT_WINDOW_ENV,
                "value": raw[:32],
                "instruction": (
                    f"{OPERATION_GRANT_WINDOW_ENV} must be a positive number of seconds"
                ),
            },
        ) from None
    if value <= 0:
        raise CredentialRefusal(
            "operation_grant_window_invalid",
            {
                "env": OPERATION_GRANT_WINDOW_ENV,
                "value": raw[:32],
                "instruction": (
                    f"{OPERATION_GRANT_WINDOW_ENV} must be a positive number of seconds"
                ),
            },
        )
    return value


def _grant_instant(raw: Any) -> datetime | None:
    """Parse a persisted grant instant; None when unreadable (naive → UTC)."""
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class CredentialOperationGrant:
    """THE authorization a redemption is judged against (Q39-01/#320).

    Minted at DISPATCH authorization beside a redemption-mode delivery
    plan and persisted in the run evidence keyed by attempt+route
    (:func:`operation_grant_key`, idempotent per attempt+route+ref — the
    grant_id and deadline are FROZEN at first authorization). Every field
    is a reference: subject, work, attempt generation, the driver/model
    route, the EXACT credential ref and binding revision, the delivery
    mode, the one permitted operation, the grant id, and the ABSOLUTE
    ``redemption_deadline`` — a fixed instant captured once, never a
    per-request ``now + TTL`` (a restart reads the same deadline). No
    value slot, by construction.
    """

    grant_id: str
    work_id: str
    subject: str
    provider: str
    credential_ref: str
    binding_revision: int
    attempt_generation: int
    delivery_mode: str
    redemption_deadline: datetime
    created_at: datetime
    operation: str = PERMITTED_OPERATION_REDEMPTION

    def key(self) -> str:
        """The evidence key: attempt generation + provider route."""
        return operation_grant_key(self.attempt_generation, self.provider)

    def expired_at(self, now: datetime) -> bool:
        """Whether *now* is at/past the ABSOLUTE deadline (the boundary
        itself refuses — the comparison is inclusive, as ever)."""
        return now >= self.redemption_deadline

    def as_document(self) -> dict[str, Any]:
        """The JSON-shape document persisted in the run evidence — refs
        and metadata only, never a value."""
        return {
            "schema": OPERATION_GRANT_SCHEMA,
            "grant_id": self.grant_id,
            "work_id": self.work_id,
            "subject": self.subject,
            "provider": self.provider,
            "credential_ref": self.credential_ref,
            "binding_revision": int(self.binding_revision),
            "attempt_generation": int(self.attempt_generation),
            "delivery_mode": self.delivery_mode,
            "operation": self.operation,
            "redemption_deadline": self.redemption_deadline.isoformat(),
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> CredentialOperationGrant:
        """Load a persisted grant; a malformed document is the TYPED
        ``operation_grant_invalid`` refusal (fail closed — a corrupt
        authorization object never degrades into a permissive read)."""
        deadline = _grant_instant(document.get("redemption_deadline"))
        created = _grant_instant(document.get("created_at"))
        required = {
            "grant_id": str(document.get("grant_id") or "").strip(),
            "work_id": str(document.get("work_id") or "").strip(),
            "provider": str(document.get("provider") or "").strip(),
            "credential_ref": str(document.get("credential_ref") or "").strip(),
        }
        missing = sorted(name for name, value in required.items() if not value)
        if missing or deadline is None or created is None:
            raise CredentialRefusal(
                "operation_grant_invalid",
                {
                    "grant_id": required["grant_id"][:16],
                    "missing": missing,
                    "deadlines_readable": deadline is not None and created is not None,
                },
            )
        try:
            generation = int(document.get("attempt_generation"))  # type: ignore[arg-type]
            revision = int(document.get("binding_revision"))  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise CredentialRefusal(
                "operation_grant_invalid",
                {"grant_id": required["grant_id"][:16], "problem": "non-integer attempt/revision"},
            ) from exc
        return cls(
            grant_id=required["grant_id"],
            work_id=required["work_id"],
            subject=str(document.get("subject") or ""),
            provider=required["provider"],
            credential_ref=required["credential_ref"],
            binding_revision=revision,
            attempt_generation=generation,
            delivery_mode=str(document.get("delivery_mode") or ""),
            redemption_deadline=deadline,
            created_at=created,
            operation=str(document.get("operation") or PERMITTED_OPERATION_REDEMPTION),
        )


def operation_grant_key(attempt_generation: int, provider: str) -> str:
    """The evidence key of one attempt's route grant — the attempt
    generation plus the provider route, the identity a re-dispatch of
    the SAME attempt under the SAME route re-uses."""
    return f"{int(attempt_generation)}:{provider}"


def operation_grant_for_plan(
    plan: CredentialDeliveryPlan,
    *,
    work_id: str,
    attempt_generation: int,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> CredentialOperationGrant:
    """Mint the operation grant for a redemption-mode *plan* (Q39-01).

    The deadline is ``now + window`` computed ONCE, HERE, at dispatch
    authorization — then persisted and never re-derived: the endpoint
    compares against the STORED instant, so the window is fixed across
    requests and restarts alike. *now* exists for the controlled-clock
    tests; production always mints at the real clock."""
    moment = now or datetime.now(timezone.utc)
    deadline = moment + timedelta(seconds=operation_grant_window_seconds(environ))
    return CredentialOperationGrant(
        grant_id=uuid.uuid4().hex,
        work_id=work_id,
        subject=plan.subject,
        provider=plan.provider,
        credential_ref=plan.credential_ref,
        binding_revision=plan.binding_revision,
        attempt_generation=int(attempt_generation),
        delivery_mode=plan.mode,
        redemption_deadline=deadline,
        created_at=moment,
    )


def merge_operation_grant(
    evidence: Mapping[str, Any], grant: CredentialOperationGrant
) -> tuple[dict[str, Any], CredentialOperationGrant]:
    """The IDEMPOTENT upsert into the run evidence (Q39-01).

    Per attempt+route+ref: when the key already holds a grant for the
    SAME credential ref, the EXISTING grant wins — its grant_id and
    absolute deadline are the authorization the first dispatch gave this
    attempt, and a re-dispatch (revival, re-drive) neither widens nor
    re-anchors the window. A DIFFERENT ref at the same key (a rotation
    that somehow kept the attempt identity) replaces the document — the
    registry's rotation checks govern that path; here the evidence stays
    honest about what is authorized NOW. Other keys are untouched.

    Returns the merged evidence and the EFFECTIVE grant (the surviving
    document — the one a redemption under this attempt+route will load).
    """
    merged = dict(evidence or {})
    grants = dict(merged.get(EVIDENCE_OPERATION_GRANTS_KEY) or {})
    existing_raw = grants.get(grant.key())
    existing: CredentialOperationGrant | None = None
    if isinstance(existing_raw, Mapping):
        try:
            existing = CredentialOperationGrant.from_document(existing_raw)
        except CredentialRefusal:
            existing = None  # a corrupt document is replaced, never trusted
    if existing is not None and existing.credential_ref == grant.credential_ref:
        grants[grant.key()] = existing.as_document()
        merged[EVIDENCE_OPERATION_GRANTS_KEY] = grants
        return merged, existing
    grants[grant.key()] = grant.as_document()
    merged[EVIDENCE_OPERATION_GRANTS_KEY] = grants
    return merged, grant


def operation_grants_for_attempt(
    evidence: Mapping[str, Any], attempt_generation: int | None
) -> list[CredentialOperationGrant]:
    """The attempt's persisted grants (one per authorized route). A
    document that fails to load is SKIPPED — the endpoint's authorization
    treats an unreadable grant as absent (fail closed: no grant, no
    redemption), never as a parse error that 500s the surface."""
    if attempt_generation is None:
        return []
    raw = (evidence or {}).get(EVIDENCE_OPERATION_GRANTS_KEY)
    if not isinstance(raw, Mapping):
        return []
    prefix = f"{int(attempt_generation)}:"
    grants: list[CredentialOperationGrant] = []
    for key, document in sorted(raw.items()):
        if not str(key).startswith(prefix) or not isinstance(document, Mapping):
            continue
        try:
            grants.append(CredentialOperationGrant.from_document(document))
        except CredentialRefusal:
            continue
    return grants


def attempt_delivery_mode(evidence: Mapping[str, Any], attempt_generation: int | None) -> str:
    """The delivery mode THIS attempt's dispatch selected (the latest
    ``dispatch_credential`` plan matching the attempt), ``""`` when the
    evidence names none — how the endpoint distinguishes a NATIVE-only
    attempt (redemption never authorized) from an unbound legacy run."""
    harness = (evidence or {}).get("harness")
    dispatch_credential = (
        harness.get("dispatch_credential") if isinstance(harness, Mapping) else None
    )
    if not isinstance(dispatch_credential, Mapping):
        return ""
    try:
        if int(dispatch_credential.get("attempt_generation")) != int(attempt_generation):  # type: ignore[arg-type]
            return ""
    except (TypeError, ValueError):
        return ""
    return str(dispatch_credential.get("mode") or "")


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
    grant_id: str = "",
    resolved_version: str = "",
    resolved_version_kind: str = "",
) -> dict[str, Any]:
    """Assemble the consumer-side receipt (schema
    :data:`CONSUMER_RECEIPT_SCHEMA`) — refs and metadata ONLY.

    The join the operator needs: WHICH binding revision paid for WHICH
    attempt, consumed by WHICH runner job — ``redemption_id`` /
    ``broker_receipt_id`` correlate with the control plane's durable
    redemption audit row and the broker's own receipt, and (Q39-01)
    ``grant_id`` is the FOREIGN KEY into the operation grant the
    redemption was authorized by (and, when #Q39-03's receipt store
    lands, the key that joins it); the value has NO slot here by
    construction (the delivered material lives only in the consumer's
    env slot, never in evidence). ``resolved_version_kind`` keeps
    presence-stamp honesty on this surface too.
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
        "grant_id": str(grant_id),
        "resolved_version": str(resolved_version),
        "resolved_version_kind": str(resolved_version_kind),
        "issued_at": _utcnow_iso(),
    }
