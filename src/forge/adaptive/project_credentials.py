"""Project-bound model credentials (R28-25): project-scoped BYOK refs.

Enterprise onboarding needs clear credential ownership: WHICH project's
key paid for a run, and proof it was THAT key. This module owns the
binding side of that question:

- :class:`ProjectCredentialBinding` — the durable record mapping
  ``(project_id, provider)`` to a credential REF. Never a value: the ref
  is the id a credential BROKER resolves at use time (the X03 doctrine
  :class:`forge.adaptive.capability_profiles.CredentialBinding` set);
  the lane still receives the credential itself through the existing
  ambient env vars (``ANTHROPIC_AUTH_TOKEN``, ``ZAI_API_KEY``,
  ``FORGE_GROK_AUTH``, …) — the binding never moves key material, it
  proves which project's key was used.
- :class:`ProjectCredentialRegistry` — the binding store. A JSON
  document merged on every write (the artifact-store persistence
  pattern: the shape is fixed so a production swap to Postgres is a
  storage detail, not a contract change).
- :func:`resolve_dispatch_credential` — the DISPATCH-TIME check: the
  ref about to be staged for a lane must be THIS project's binding for
  THAT provider route. A wrong project's ref, a revoked binding, or a
  missing binding with no default route is REFUSED with a typed reason
  (a cheaper fallback route is exactly what must never happen
  silently). The returned :class:`DispatchCredential` carries the
  audit-proof record — refs and env var NAMES only, never values.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "BINDING_SCHEMA",
    "PROVIDER_ENV_VARS",
    "CredentialRefusal",
    "ProjectCredentialBinding",
    "ProjectCredentialRegistry",
    "DispatchCredential",
    "resolve_dispatch_credential",
]

#: The schema discriminator every persisted binding document carries.
BINDING_SCHEMA = "forge.project.credential-binding/1"

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


@dataclass(frozen=True)
class ProjectCredentialBinding:
    """The durable (project, provider) → credential-ref record.

    ``credential_ref`` is the broker-owned id (e.g. ``vault:kv/eng#42``),
    NEVER the key material. ``env_var`` names the ambient variable the
    lane consumes (:data:`PROVIDER_ENV_VARS`); ``bound_by`` is the actor
    who made the binding decision; ``revoked_at`` (ISO, None while
    live) marks a rotation away or a revocation — a revoked binding
    refuses dispatch instead of falling back.
    """

    project_id: int
    provider: str
    credential_ref: str
    env_var: str
    bound_at: str
    bound_by: str
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

    @property
    def live(self) -> bool:
        return self.revoked_at is None

    def as_document(self) -> dict[str, Any]:
        """The JSON-shape record for persistence and audit surfaces."""
        return {
            "schema": BINDING_SCHEMA,
            "project_id": self.project_id,
            "provider": self.provider,
            "credential_ref": self.credential_ref,
            "env_var": self.env_var,
            "bound_at": self.bound_at,
            "bound_by": self.bound_by,
            "revoked_at": self.revoked_at,
        }

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> ProjectCredentialBinding:
        return cls(
            project_id=int(document["project_id"]),
            provider=str(document["provider"]),
            credential_ref=str(document["credential_ref"]),
            env_var=str(document["env_var"]),
            bound_at=str(document.get("bound_at") or ""),
            bound_by=str(document.get("bound_by") or ""),
            revoked_at=(str(document["revoked_at"]) if document.get("revoked_at") else None),
        )


@dataclass
class ProjectCredentialRegistry:
    """The binding store — one JSON document, merged on every write.

    *path* None keeps the registry in memory (tests, ephemeral use); a
    path persists beside the deployment exactly like the artifact
    store's metadata document. Bind REPLACES the live binding for
    ``(project, provider)`` and appends the superseded one to the
    history; revoke marks the live binding revoked (a later bind is a
    fresh decision, recorded as such).
    """

    path: Path | None = None
    bindings: dict[tuple[int, str], ProjectCredentialBinding] = field(default_factory=dict)
    history: list[ProjectCredentialBinding] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.path is not None and self.path.is_file():
            self._load()

    # -- persistence ------------------------------------------------------

    def _load(self) -> None:
        document = json.loads(self.path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        self.bindings = {
            (binding.project_id, binding.provider): binding
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
        project_id: int,
        provider: str,
        credential_ref: str,
        *,
        bound_by: str,
    ) -> ProjectCredentialBinding:
        """Bind (or rotate) THIS project's credential ref for *provider*.

        Rotation is explicit: the superseded binding moves to the history
        with its revocation stamp, and the new binding is a NEW decision
        with a NEW timestamp — an approved route never changes silently.
        """
        binding = ProjectCredentialBinding(
            project_id=project_id,
            provider=provider,
            credential_ref=credential_ref,
            env_var=PROVIDER_ENV_VARS[provider],
            bound_at=_utcnow_iso(),
            bound_by=bound_by,
        )
        key = (project_id, provider)
        previous = self.bindings.get(key)
        if previous is not None:
            self.history.append(replace(previous, revoked_at=_utcnow_iso()))
        self.bindings[key] = binding
        self._persist()
        return binding

    def revoke(
        self, project_id: int, provider: str, *, revoked_by: str
    ) -> ProjectCredentialBinding | None:
        """Mark the live binding revoked (the negative-test surface for a
        revoked provider key). Returns the revoked record, or None when
        nothing was live — revoking nothing is a no-op, never an error.

        The history entry keeps the ORIGINAL ``bound_by``/``bound_at``
        and gains the revocation stamp; *revoked_by* rides the returned
        record's proof surface (the caller journals the decision).
        """
        key = (project_id, provider)
        binding = self.bindings.get(key)
        if binding is None or not binding.live:
            return None
        revoked = replace(binding, revoked_at=_utcnow_iso())
        self.bindings[key] = revoked
        self.history.append(revoked)
        self._persist()
        del revoked_by  # journaled by the caller; the record stays pure refs
        return revoked

    def binding_for(self, project_id: int, provider: str) -> ProjectCredentialBinding | None:
        return self.bindings.get((project_id, provider))


@dataclass(frozen=True)
class DispatchCredential:
    """The dispatch-time resolution of a project's provider credential.

    ``proof`` is the audit record: WHOSE project's key, WHICH provider
    route, WHICH ref, WHICH env slot — refs and names only, never
    values. The lane's env keeps carrying the resolved value from the
    broker; this record is what the audit trail can cite.
    """

    project_id: int
    provider: str
    credential_ref: str
    env_var: str
    proof: dict[str, Any]

    def as_document(self) -> dict[str, Any]:
        return dict(self.proof)


def resolve_dispatch_credential(
    registry: ProjectCredentialRegistry,
    *,
    project_id: int,
    provider: str,
    presented_ref: str = "",
    environ: dict[str, str] | None = None,
) -> DispatchCredential:
    """Check the credential a dispatch is about to stage (fail closed).

    The rules, in order:

    - the project must have a LIVE binding for the provider route — no
      binding means NO default route (a configuration outage refuses
      the dispatch, it never silently falls back to a cheaper or shared
      credential);
    - a revoked binding refuses the same way;
    - when the caller can name the ref it is about to stage
      (*presented_ref*, e.g. observed from the broker response), it must
      equal the binding's — a WRONG project's ref is refused loudly,
      which is the cross-tenant leak this module exists to prevent;
    - the ambient env var NAME is carried into the proof. When *environ*
      is supplied (the dispatch environment), the var's PRESENCE is
      checked — the value is never read, never logged, never exported;
      a missing var refuses the dispatch (the credential is not staged).

    Raises :class:`CredentialRefusal` with the typed reason; returns the
    :class:`DispatchCredential` proof on success.
    """
    binding = registry.binding_for(project_id, provider)
    if binding is None:
        raise CredentialRefusal("no_binding", {"project_id": project_id, "provider": provider})
    if not binding.live:
        raise CredentialRefusal(
            "revoked",
            {
                "project_id": project_id,
                "provider": provider,
                "credential_ref": binding.credential_ref,
            },
        )
    if environ is not None and binding.env_var not in environ:
        raise CredentialRefusal(
            "env_absent",
            {"project_id": project_id, "provider": provider, "env_var": binding.env_var},
        )
    if presented_ref and presented_ref != binding.credential_ref:
        raise CredentialRefusal(
            "wrong_project_ref",
            {
                "project_id": project_id,
                "provider": provider,
                "presented": presented_ref,
                "bound": binding.credential_ref,
            },
        )
    proof = {
        "schema": "forge.project.dispatch-credential-proof/1",
        "project_id": project_id,
        "provider": provider,
        "credential_ref": binding.credential_ref,
        "env_var": binding.env_var,
        "resolved_at": _utcnow_iso(),
        "bound_at": binding.bound_at,
        "bound_by": binding.bound_by,
    }
    return DispatchCredential(
        project_id=project_id,
        provider=provider,
        credential_ref=binding.credential_ref,
        env_var=binding.env_var,
        proof=proof,
    )


def registry_from_env(environ: dict[str, str] | None = None) -> ProjectCredentialRegistry:
    """The deployment's registry: ``FORGE_CREDENTIAL_BINDINGS`` names the
    JSON document path (persisted registry); unset means in-memory —
    callers then bind programmatically before any dispatch resolves."""
    source = os.environ if environ is None else environ
    raw = str(source.get("FORGE_CREDENTIAL_BINDINGS", "") or "").strip()
    return ProjectCredentialRegistry(path=Path(raw) if raw else None)
