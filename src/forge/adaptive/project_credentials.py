"""Project-bound model credentials (R28-25, NEXT-19/#207): project-scoped
BYOK refs keyed by CANONICAL subject.

Enterprise onboarding needs clear credential ownership: WHICH project's
key paid for a run, and proof it was THAT key. This module owns the
binding side of that question:

- :class:`ProjectCredentialBinding` — the durable record mapping a
  CANONICAL SUBJECT (provider family + connection/host + native project
  identity, :class:`~forge.adaptive.operator_snapshot.CanonicalSubject`)
  plus a provider route to a credential REF. Never a value: the ref is
  the id a credential BROKER (:mod:`forge.adaptive.credential_broker`)
  resolves at use time; the lane receives the credential itself through
  the broker's staged env slot (``ANTHROPIC_AUTH_TOKEN``, ``ZAI_API_KEY``,
  ``FORGE_GROK_AUTH``, …) — the binding never moves key material, it
  proves which project's key was used.
- :class:`ProjectCredentialRegistry` — the binding store. A JSON
  document merged on every write (the artifact-store persistence
  pattern: the shape is fixed so a production swap to Postgres is a
  storage detail, not a contract change).
- :func:`resolve_dispatch_credential` — the DISPATCH-TIME check: the ref
  about to be staged for a lane must be THIS subject's binding for THAT
  provider route. A wrong subject's ref, a revoked binding, a rotated-
  away ref, or a missing binding with no default route is REFUSED with a
  typed reason (a cheaper fallback route is exactly what must never
  happen silently). The returned :class:`DispatchCredential` carries the
  audit-proof record — refs and env var NAMES only, never values.

NEXT-19 (#207) — the v2 key and the rotation contract:

- Schema ``forge.project.credential-binding/2`` keys bindings by
  ``(subject_id, provider)``. The /1 shape ``(int project_id, provider)``
  — a numeric platform project number alone — let two self-managed
  instances with EQUAL numeric ids share a key; the canonical subject is
  the collision-proof axis everywhere else (R37-02). A /1 document loads
  through a read adapter under a SYNTHESIZED default-connection subject
  (``gitlab/-/<project_id>`` — the single-connection GitLab-era default
  of the generation the /1 schema shipped in, because the /1 substrate
  had no production caller and therefore no non-GitLab documents); a
  full-subject dispatch does NOT silently match a legacy binding —
  re-bind under /2 is the migration.
- Rotation is a first-class refusal: a ref that WAS this binding's live
  ref but was rotated away (present in the binding's history) refuses
  with the typed reason ``rotated`` — never ``wrong_project_ref`` (that
  is the cross-subject leak), never a silent substitution. A NEW attempt
  generation (a ``/retry``, a revival) re-resolves the live binding; the
  retry then stages the rotated-in version.
- IN-FLIGHT SEMANTICS: a dispatched lane keeps the staged credential
  generation it was dispatched with until the attempt is terminal — the
  broker's staged env is a SNAPSHOT taken at dispatch and never re-read
  mid-flight; only the NEXT dispatch re-resolves.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from forge.adaptive.operator_snapshot import (
    CanonicalSubject,
    subject_from_ref,
    subject_of_run,
)

__all__ = [
    "BINDING_SCHEMA",
    "BINDING_SCHEMA_V1",
    "DISPATCH_PROOF_SCHEMA",
    "PROVIDER_ENV_VARS",
    "PROVIDER_ROUTE_OF_DRIVER",
    "CredentialRefusal",
    "ProjectCredentialBinding",
    "ProjectCredentialRegistry",
    "DispatchCredential",
    "binding_subject_of_run",
    "provider_route_for_driver",
    "resolve_dispatch_credential",
]

#: The schema discriminator every persisted binding document carries.
#: /2 (NEXT-19): the canonical-subject key (see module docstring).
BINDING_SCHEMA = "forge.project.credential-binding/2"

#: The retired /1 discriminator — still READ (the adapter below), never
#: written. A /1 document keys by ``(project_id, provider)`` only.
BINDING_SCHEMA_V1 = "forge.project.credential-binding/1"

#: The schema discriminator of the dispatch-time proof document.
DISPATCH_PROOF_SCHEMA = "forge.project.dispatch-credential-proof/2"

#: The closed provider-route → env-var table: the AMBIENT variable the
#: lane consumes for each provider (the same surfaces
#: ``forge.runs.harness_selection.DRIVER_CREDENTIAL_VARS`` documents per
#: driver). A binding names its provider's var so the dispatch proof can
#: say WHICH env slot carried the (broker-resolved) credential.
PROVIDER_ENV_VARS: dict[str, str] = {
    "anthropic-gateway": "ANTHROPIC_AUTH_TOKEN",
    "zai": "ZAI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "github-copilot": "COPILOT_GITHUB_TOKEN",
    "xai-grok": "FORGE_GROK_AUTH",
}

#: The lane driver → provider-route table: WHICH route's credential a
#: dispatched driver consumes. Mirrors the driver side of
#: ``forge.runs.harness_selection.DRIVER_CREDENTIAL_VARS`` (a test keeps
#: the two in sync); a driver outside the table names no route, so no
#: binding axis applies to its dispatches.
PROVIDER_ROUTE_OF_DRIVER: dict[str, str] = {
    "claude-code": "anthropic-gateway",
    "claude-sdk-lane": "anthropic-gateway",
    # dotnet-lane declares NO per-driver credential var — it rides the
    # same forge gateway surface the claude-code lane consumes.
    "dotnet-lane": "anthropic-gateway",
    "grok-build": "xai-grok",
    "opencode": "zai",
    "opencode-sdk-lane": "zai",
    "copilot": "github-copilot",
    "copilot-sdk-lane": "github-copilot",
    "codex-sdk-lane": "openai",
}


def provider_route_for_driver(driver: str) -> str:
    """The provider route whose credential a driver's lane consumes.

    ``""`` when the driver names no route (an unknown driver has no
    binding axis — the dispatch then stages nothing rather than guess).
    """
    return PROVIDER_ROUTE_OF_DRIVER.get(str(driver or "").strip(), "")


def binding_subject_of_run(run: Any) -> CanonicalSubject | None:
    """The dispatch's binding subject: the run's own canonical subject.

    ``None`` when the run row carries no subject a binding could name —
    such a run has no binding axis and no broker staging applies. The
    subject is derived from the run's OWN persisted columns (family +
    recorded connection + native identity), so equal numeric ids on
    different connections/providers NEVER share a binding.
    """
    return subject_of_run(run)


class CredentialRefusal(Exception):
    """A dispatch-time credential check failed — fail closed, typed.

    The message is OPERATOR-SAFE by construction: it carries refs and
    provider names, never credential values (issue 183: "exports and
    error logs contain no credential values").
    """

    def __init__(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        self.reason = reason
        self.detail = detail or {}
        super().__init__(f"{reason}: {self.detail}" if self.detail else reason)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _assert_ref_is_not_a_value(credential_ref: str) -> None:
    """The X03 guard: a ref that LOOKS like a pasted secret (carries
    ``SECRET`` or an ``=`` assignment) is refused at bind time — the
    moment someone pastes the value, not at dispatch."""
    if "SECRET" in credential_ref or "=" in credential_ref:
        raise CredentialRefusal(
            "value_looking_ref",
            {"credential_ref": credential_ref[:8] + "…"},
        )


def _subject_id(subject: CanonicalSubject | str) -> str:
    """The comparable subject key — a :class:`CanonicalSubject` or its
    serialized ``<family>/<connection>/<native_id>`` spelling (validated
    through :class:`CanonicalSubject` parsing, never a bare string guess)."""
    if isinstance(subject, CanonicalSubject):
        return subject.subject_id()
    return subject_from_ref(str(subject)).subject_id()


#: The /1 read adapter's legacy default: the family and connection marker
#: a ``(project_id, provider)``-keyed document loads under. The /1
#: generation had a single GitLab connection and no production caller,
#: so every real /1 document is a GitLab-era one; the spelling is honest
#: about that provenance and NEVER matches a full-subject dispatch
#: (``gitlab/gitlab.example/101`` has a recorded connection — the legacy
#: subject does not).
LEGACY_BINDING_FAMILY = "gitlab"


def legacy_subject_for_project(project_id: int) -> CanonicalSubject:
    """The synthesized default-connection subject of a /1 binding."""
    return CanonicalSubject(
        provider_family=LEGACY_BINDING_FAMILY,
        connection="-",
        native_id=str(int(project_id)),
    )


@dataclass(frozen=True)
class ProjectCredentialBinding:
    """The durable (canonical subject, provider) → credential-ref record.

    ``credential_ref`` is the broker-owned id (e.g. ``vault:kv/eng#42``
    or ``env:ANTHROPIC_AUTH_TOKEN``), NEVER the key material.
    ``env_var`` names the variable the lane consumes
    (:data:`PROVIDER_ENV_VARS`); ``bound_by`` is the actor who made the
    binding decision; ``revoked_at`` (ISO, None while live) marks a
    rotation away or a revocation — a revoked binding refuses dispatch
    instead of falling back. ``revision`` counts the binding decisions
    for this (subject, provider) key (rotation increments it), so a
    receipt can name the exact revision a dispatch resolved.
    ``project_id`` is AUDIT PROVENANCE ONLY (the numeric platform id
    observed at bind time) — it is never part of the key.
    """

    subject: str
    provider: str
    credential_ref: str
    env_var: str
    bound_at: str
    bound_by: str
    revision: int = 1
    project_id: int = 0
    revoked_at: str | None = None

    def __post_init__(self) -> None:
        if self.provider not in PROVIDER_ENV_VARS:
            known = ", ".join(sorted(PROVIDER_ENV_VARS))
            raise CredentialRefusal("unknown_provider", {"provider": self.provider, "known": known})
        if PROVIDER_ENV_VARS[self.provider] != self.env_var:
            raise CredentialRefusal(
                "provider_env_mismatch",
                {"provider": self.provider, "env_var": self.env_var},
            )
        _assert_ref_is_not_a_value(self.credential_ref)
        if not self.credential_ref.strip():
            raise CredentialRefusal("empty_ref", {"provider": self.provider})
        object.__setattr__(self, "subject", _subject_id(self.subject))

    @property
    def live(self) -> bool:
        return self.revoked_at is None

    def as_document(self) -> dict[str, Any]:
        """The JSON-shape record for persistence and audit surfaces."""
        return {
            "schema": BINDING_SCHEMA,
            "subject": self.subject,
            "provider": self.provider,
            "credential_ref": self.credential_ref,
            "env_var": self.env_var,
            "bound_at": self.bound_at,
            "bound_by": self.bound_by,
            "revision": int(self.revision),
            "project_id": int(self.project_id),
            "revoked_at": self.revoked_at,
        }

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> ProjectCredentialBinding:
        """Load a persisted binding — /2 documents natively, /1
        documents through the read adapter (the synthesized
        default-connection subject; see :func:`legacy_subject_for_project`)."""
        subject = str(document.get("subject") or "").strip()
        project_id = int(document.get("project_id") or 0)
        if not subject:
            if not project_id:
                raise CredentialRefusal(
                    "legacy_binding_without_project", {"document_keys": sorted(document)}
                )
            # The /1 read adapter: the old (project_id, provider) key
            # loads under the legacy default-connection subject.
            subject = legacy_subject_for_project(project_id).subject_id()
        return cls(
            subject=subject,
            provider=str(document["provider"]),
            credential_ref=str(document["credential_ref"]),
            env_var=str(document["env_var"]),
            bound_at=str(document.get("bound_at") or ""),
            bound_by=str(document.get("bound_by") or ""),
            revision=int(document.get("revision") or 1),
            project_id=project_id,
            revoked_at=(str(document["revoked_at"]) if document.get("revoked_at") else None),
        )


@dataclass
class ProjectCredentialRegistry:
    """The binding store — one JSON document, merged on every write.

    *path* None keeps the registry in memory (tests, ephemeral use); a
    path persists beside the deployment exactly like the artifact
    store's metadata document. Bind REPLACES the live binding for
    ``(subject, provider)`` and appends the superseded one to the
    history (its revision incremented on the new record); revoke marks
    the live binding revoked (a later bind is a fresh decision, recorded
    as such).
    """

    path: Path | None = None
    bindings: dict[tuple[str, str], ProjectCredentialBinding] = field(default_factory=dict)
    history: list[ProjectCredentialBinding] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.path is not None and self.path.is_file():
            self._load()

    # -- persistence ------------------------------------------------------

    def _load(self) -> None:
        document = json.loads(self.path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        self.bindings = {
            (binding.subject, binding.provider): binding
            for binding in (
                ProjectCredentialBinding.from_document(entry)
                for entry in document.get("bindings", [])
            )
        }
        self.history = [
            ProjectCredentialBinding.from_document(entry) for entry in document.get("history", [])
        ]

    def _persist(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "schema": BINDING_SCHEMA,
            "bindings": [binding.as_document() for binding in self.bindings.values()],
            "history": [binding.as_document() for binding in self.history],
        }
        self.path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    # -- the registry surface ---------------------------------------------

    def bind(
        self,
        subject: CanonicalSubject | str,
        provider: str,
        credential_ref: str,
        *,
        bound_by: str,
        project_id: int = 0,
    ) -> ProjectCredentialBinding:
        """Bind (or rotate) THIS subject's credential ref for *provider*.

        Rotation is explicit: the superseded binding moves to the history
        with its revocation stamp, and the new binding is a NEW decision
        with a NEW timestamp and the NEXT revision — an approved route
        never changes silently, and a receipt can name which revision a
        dispatch resolved.
        """
        key_subject = _subject_id(subject)
        previous = self.bindings.get((key_subject, provider))
        binding = ProjectCredentialBinding(
            subject=key_subject,
            provider=provider,
            credential_ref=credential_ref,
            env_var=PROVIDER_ENV_VARS[provider],
            bound_at=_utcnow_iso(),
            bound_by=bound_by,
            revision=(previous.revision + 1) if previous is not None else 1,
            project_id=project_id or (previous.project_id if previous is not None else 0),
        )
        if previous is not None:
            self.history.append(replace(previous, revoked_at=_utcnow_iso()))
        self.bindings[(key_subject, provider)] = binding
        self._persist()
        return binding

    def revoke(
        self,
        subject: CanonicalSubject | str,
        provider: str,
        *,
        revoked_by: str,
    ) -> ProjectCredentialBinding | None:
        """Mark the live binding revoked (the negative-test surface for a
        revoked provider key). Returns the revoked record, or None when
        nothing was live — revoking nothing is a no-op, never an error.

        The history entry keeps the ORIGINAL ``bound_by``/``bound_at``
        and gains the revocation stamp; *revoked_by* rides the returned
        record's proof surface (the caller journals the decision).
        """
        key = (_subject_id(subject), provider)
        binding = self.bindings.get(key)
        if binding is None or not binding.live:
            return None
        revoked = replace(binding, revoked_at=_utcnow_iso())
        self.bindings[key] = revoked
        self.history.append(revoked)
        self._persist()
        del revoked_by  # journaled by the caller; the record stays pure refs
        return revoked

    def binding_for(
        self, subject: CanonicalSubject | str, provider: str
    ) -> ProjectCredentialBinding | None:
        return self.bindings.get((_subject_id(subject), provider))

    def history_for(
        self, subject: CanonicalSubject | str, provider: str
    ) -> list[ProjectCredentialBinding]:
        """The superseded generations of one (subject, provider) key —
        the rotation record a ``rotated`` refusal consults."""
        key_subject = _subject_id(subject)
        return [
            binding
            for binding in self.history
            if binding.subject == key_subject and binding.provider == provider
        ]

    def subject_is_bound(self, subject: CanonicalSubject | str) -> bool:
        """Whether the deployment made ANY binding decision for the
        subject. The staging seam's opt-in line: a subject with no
        binding at all keeps today's ambient behavior (attribution
        honestly unknown); a subject the deployment HAS bound fails
        closed on every other route."""
        key_subject = _subject_id(subject)
        return any(key == key_subject for (key, _provider) in self.bindings)


@dataclass(frozen=True)
class DispatchCredential:
    """The dispatch-time resolution of a subject's provider credential.

    ``proof`` is the audit record: WHOSE subject's key, WHICH provider
    route, WHICH ref, WHICH env slot, WHICH binding revision — refs and
    names only, never values. The lane's env keeps carrying the resolved
    value from the broker; this record is what the audit trail can cite.
    """

    subject: str
    provider: str
    credential_ref: str
    env_var: str
    binding_revision: int
    proof: dict[str, Any]

    def as_document(self) -> dict[str, Any]:
        return dict(self.proof)


def resolve_dispatch_credential(
    registry: ProjectCredentialRegistry,
    *,
    subject: CanonicalSubject | str,
    provider: str,
    presented_ref: str = "",
    environ: dict[str, str] | None = None,
) -> DispatchCredential:
    """Check the credential a dispatch is about to stage (fail closed).

    The rules, in order:

    - the subject must have a LIVE binding for the provider route — no
      binding means NO default route (a configuration outage refuses
      the dispatch, it never silently falls back to a cheaper or shared
      credential);
    - a revoked binding refuses the same way;
    - when the caller can name the ref it is about to stage
      (*presented_ref* — the ref the prior dispatch of THIS attempt
      resolved, or the one an approval recorded), it must equal the
      binding's. A ref that was rotated away for THIS key refuses with
      the typed reason ``rotated`` (a new attempt generation re-resolves
      the rotated-in ref — never a silent substitution); any other
      foreign ref is the cross-subject leak and refuses
      ``wrong_project_ref``;
    - the ambient env var NAME is carried into the proof. When *environ*
      is supplied (the dispatch environment), the var's PRESENCE is
      checked — the value is never read, never logged, never exported;
      a missing var refuses the dispatch (the credential is not staged).

    Raises :class:`CredentialRefusal` with the typed reason; returns the
    :class:`DispatchCredential` proof on success.
    """
    key_subject = _subject_id(subject)
    binding = registry.binding_for(key_subject, provider)
    if binding is None:
        raise CredentialRefusal("no_binding", {"subject": key_subject, "provider": provider})
    if not binding.live:
        raise CredentialRefusal(
            "revoked",
            {
                "subject": key_subject,
                "provider": provider,
                "credential_ref": binding.credential_ref,
            },
        )
    if environ is not None and binding.env_var not in environ:
        raise CredentialRefusal(
            "env_absent",
            {"subject": key_subject, "provider": provider, "env_var": binding.env_var},
        )
    if presented_ref and presented_ref != binding.credential_ref:
        rotated = any(
            entry.credential_ref == presented_ref
            for entry in registry.history_for(key_subject, provider)
        )
        if rotated:
            # Rotation between the prior resolution (or the approval)
            # and this dispatch: refuse typed, never substitute.
            raise CredentialRefusal(
                "rotated",
                {
                    "subject": key_subject,
                    "provider": provider,
                    "presented": presented_ref,
                    "bound": binding.credential_ref,
                    "binding_revision": binding.revision,
                },
            )
        raise CredentialRefusal(
            "wrong_project_ref",
            {
                "subject": key_subject,
                "provider": provider,
                "presented": presented_ref,
                "bound": binding.credential_ref,
            },
        )
    proof = {
        "schema": DISPATCH_PROOF_SCHEMA,
        "subject": key_subject,
        "provider": provider,
        "credential_ref": binding.credential_ref,
        "env_var": binding.env_var,
        "binding_revision": int(binding.revision),
        "resolved_at": _utcnow_iso(),
        "bound_at": binding.bound_at,
        "bound_by": binding.bound_by,
    }
    return DispatchCredential(
        subject=key_subject,
        provider=provider,
        credential_ref=binding.credential_ref,
        env_var=binding.env_var,
        binding_revision=int(binding.revision),
        proof=proof,
    )


def registry_from_env(environ: dict[str, str] | None = None) -> ProjectCredentialRegistry:
    """The deployment's registry: ``FORGE_CREDENTIAL_BINDINGS`` names the
    JSON document path (persisted registry); unset means in-memory —
    callers then bind programmatically before any dispatch resolves."""
    source = os.environ if environ is None else environ
    raw = str(source.get("FORGE_CREDENTIAL_BINDINGS", "") or "").strip()
    return ProjectCredentialRegistry(path=Path(raw) if raw else None)
