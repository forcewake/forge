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
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, runtime_checkable

from forge.adaptive.operator_snapshot import CanonicalSubject
from forge.adaptive.project_credentials import (
    PROVIDER_ENV_VARS,
    CredentialRefusal,
    ProjectCredentialRegistry,
    resolve_dispatch_credential,
)

__all__ = [
    "BROKER_RECEIPT_SCHEMA",
    "ENV_REF_SCHEME",
    "RESOLVER_ENV",
    "RESOLVER_STAGED",
    "BrokerCredentialRefusal",
    "CredentialBroker",
    "EnvBroker",
    "ResolvedCredential",
    "StagedBroker",
    "StagedDispatchCredential",
    "stage_dispatch_credential",
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


class BrokerCredentialRefusal(CredentialRefusal):
    """A broker could not resolve a ref — fail closed, typed, with the
    resolver identity named (the message carries refs, never values)."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ResolvedCredential:
    """One broker resolution: the version, the staged env, the receipt.

    ``staged_env`` maps env var NAMES to values — the only place the
    VALUE exists, and the only thing a dispatch may move into the lane's
    variable set. ``resolver_identity`` names WHO resolved (the broker's
    own identity, carried on the resolution so the proof needs no
    back-reference). ``receipt`` is refs/metadata only (schema
    :data:`BROKER_RECEIPT_SCHEMA`).
    """

    version: str
    staged_env: dict[str, str]
    receipt: dict[str, Any]
    resolver_identity: str = ""


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
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The refs/metadata-only receipt shared by every broker shape.

    By construction there is no value slot here — the receipt names the
    ref, the env var, the provider route and the resolved version; a
    broker that smuggles the value into ``extra`` is refused by the
    allowlist exports (:mod:`forge.adaptive.audit_export`) and by the
    dispatch seam's own slot check.
    """
    receipt: dict[str, Any] = {
        "schema": BROKER_RECEIPT_SCHEMA,
        "resolver_identity": resolver_identity,
        "provider_route": _PROVIDER_ROUTE_OF_VAR.get(env_var, "unknown"),
        "credential_ref": credential_ref,
        "env_var": env_var,
        "env_present": True,
        "resolved_version": version,
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
        )
        return ResolvedCredential(
            version=version,
            staged_env={env_var: value},
            receipt=receipt,
            resolver_identity=self.resolver_identity,
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
        )
        return ResolvedCredential(
            version=version,
            staged_env={env_var: value},
            receipt=receipt,
            resolver_identity=self.resolver_identity,
        )


@dataclass(frozen=True)
class StagedDispatchCredential:
    """What a dispatch leg staged: the snapshot the lane consumes plus
    the extended proof that rides the run evidence.

    ``staged_env`` is the SNAPSHOT (copy-on-stage): mutating the broker
    or the environment afterwards never rewrites it — the in-flight lane
    keeps this generation until terminal. ``proof`` is the
    ``forge.project.dispatch-credential-proof/2`` document: the binding
    subject/ref/revision, the resolver identity, the resolved version,
    the grant refs and the broker receipt — refs and metadata only.
    """

    subject: str
    provider: str
    credential_ref: str
    env_var: str
    binding_revision: int
    resolved_version: str
    resolver_identity: str
    staged_env: dict[str, str] = field(default_factory=dict)
    proof: dict[str, Any] = field(default_factory=dict)


async def stage_dispatch_credential(
    registry: ProjectCredentialRegistry,
    broker: CredentialBroker,
    *,
    subject: CanonicalSubject,
    provider: str,
    presented_ref: str = "",
    grant: Mapping[str, Any] | None = None,
) -> StagedDispatchCredential | None:
    """The dispatch seam: resolve a subject's provider credential under
    the active execution grant, BEFORE any provider call.

    Returns ``None`` — today's ambient behavior — when *provider* names
    no route or the registry holds NO binding decision for *subject*
    (attribution honestly unknown; the deployment never opted this
    subject into bound credentials). Otherwise, in order:

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
      assembled: binding revision, resolver identity, resolved version,
      the grant refs and the broker receipt. No value, ever.

    Observability spellings the dispatch legs log from the result:
    ``credential.binding_subject`` · ``credential.resolved_version`` ·
    ``credential.resolver_identity`` (and ``credential.rotation_refusal``
    on the typed rotation refusal).
    """
    if not provider:
        return None
    if not registry.subject_is_bound(subject):
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
    )
