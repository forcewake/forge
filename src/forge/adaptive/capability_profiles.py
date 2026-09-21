"""Versioned capability and credential profiles (FND-04, R08/R09/R11/X03/X08).

A driver NAME is not a promise of live steering, resumability or BYOK
compatibility — "a CLI with checkpoint-only control" must never be
advertised as native interactive, and an SDK/provider/credential
combination that was never tested must fail during onboarding, not
mid-run. This module is the substrate for that: versioned, immutable
records of WHAT one tested driver/harness combination can actually do
and WHICH credential it was bound to.

- :class:`CapabilityProfile` — the versioned capability record (read
  tools, structured output, interrupt, live input, checkpoint export,
  questions, usage completeness) bound to a driver package/image
  digest, a provider route and a credential mode.
- :class:`CredentialBinding` — a reference to a broker-owned credential
  ID. NEVER a secret value: RunSpecs and artifacts must not carry key
  material (X03).
- :func:`validate_profile_binding` — profile/binding compatibility,
  checked at onboarding; a missing selected credential never falls back
  to another provider's key (X08).
- :func:`role_allows` — role/profile compatibility; discovery and
  verification cannot silently select a write-enabled profile.
- :func:`manifest_status` — absent versus explicitly-empty manifests
  (the D06 distinction: only a manifest declared NOWHERE keeps the
  legacy default; an explicitly empty one allows nothing).

Pure stdlib and frozen throughout: profiles are pinned by digest into
run specs and must never mutate under a running run.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "CAPABILITIES",
    "CapabilityProfile",
    "CredentialBinding",
    "PROFILE_SCHEMA",
    "manifest_status",
    "role_allows",
    "validate_profile_binding",
]

#: The schema discriminator every capability profile carries (versioned:
#: a breaking change to the record's meaning bumps the tag, and pinned
#: runs keep the version they were approved with).
PROFILE_SCHEMA = "forge.capability.profile/1"

#: The closed capability vocabulary — the axes interactive planning
#: actually needs to know about, no more (a name outside this tuple is a
#: modelling error, not a silent pass-through).
CAPABILITIES: tuple[str, ...] = (
    "read_tools",
    "structured_output",
    "interrupt",
    "live_input",
    "checkpoint_export",
    "questions",
    "usage_complete",
)

#: Capabilities that keep a human in the loop in real time — they only
#: work with an explicitly bound credential, never an ambient fallback.
_INTERACTIVE_CAPABILITIES = ("live_input", "interrupt")

#: The roles a lane may run as. Discovery and verification are READ-ONLY
#: phases: a write-enabled profile selected there is either a
#: misconfiguration or an attempt to smuggle writes past the contract.
_WRITE_FORBIDDEN_ROLES = ("discovery", "verification")


@dataclass(frozen=True)
class CapabilityProfile:
    """WHAT one tested driver/harness combination can actually do.

    ``capabilities`` is the closed :data:`CAPABILITIES` vocabulary the
    profile was TESTED for (never inferred from a method name — X08);
    ``driver_digest`` pins the exact package/image, ``provider_route``
    and ``credential_mode`` pin the route it was tested with;
    ``is_write_enabled`` marks profiles allowed to produce repository
    writes at all.
    """

    schema: str = PROFILE_SCHEMA
    profile_id: str = ""
    capabilities: tuple[str, ...] = ()
    driver_digest: str = ""
    provider_route: str = ""
    credential_mode: str = ""
    is_write_enabled: bool = False

    def __post_init__(self) -> None:
        if self.schema != PROFILE_SCHEMA:
            raise ValueError(f"schema must be {PROFILE_SCHEMA!r}, got {self.schema!r}")

    def supports(self, capability: str) -> bool:
        """Whether THIS profile advertises *capability*.

        Advertised means tested-for: a checkpoint-only CLI answers False
        for ``interrupt`` no matter what similarly-named methods exist.
        """
        return capability in self.capabilities


@dataclass(frozen=True)
class CredentialBinding:
    """A reference to a broker-owned credential — never a secret value.

    ``credential_ref`` is the ID the credential broker resolves at use
    time; key material itself never enters a RunSpec or an artifact
    (X03). :func:`validate_profile_binding` refuses refs that LOOK like
    values, which is how a pasted token gets caught at onboarding.
    """

    credential_ref: str
    provider: str
    mode: str


def validate_profile_binding(
    profile: CapabilityProfile, binding: CredentialBinding | None
) -> tuple[bool, str]:
    """Check profile/binding compatibility; ``(ok, reason)`` (fail closed).

    Rules, in order:

    - a profile requiring ``live_input`` or ``interrupt`` MUST have a
      binding — interactive control without a bound credential is the
      case that must fail during onboarding, and a missing selected
      credential does NOT fall back to another provider's key (X08);
    - ``credential_ref`` must not contain ``SECRET`` or ``=`` — a
      value-looking ref means someone pasted the secret itself;
    - when both ``profile.provider_route`` and ``binding.provider`` are
      set they must match — a binding minted for another provider is an
      incompatible combination, not a best-effort try.
    """

    requires_interactive = any(profile.supports(c) for c in _INTERACTIVE_CAPABILITIES)
    if binding is None:
        if requires_interactive:
            # No fallback: an unbound interactive profile is refused, never
            # silently routed to whatever ambient credential exists.
            return False, "missing credential binding"
        return True, "ok"

    ref = binding.credential_ref
    if "SECRET" in ref or "=" in ref:
        return False, "credential_ref looks like a secret value, not a broker-owned id"
    if profile.provider_route and binding.provider and profile.provider_route != binding.provider:
        return False, (
            f"provider mismatch: profile route {profile.provider_route!r} "
            f"cannot use a {binding.provider!r} credential"
        )
    return True, "ok"


def role_allows(profile: CapabilityProfile, role: str) -> tuple[bool, str]:
    """Whether *role* may select *profile*; ``(ok, reason)`` (fail closed).

    ``implementation`` may select anything. ``discovery`` and
    ``verification`` are read-only phases: a write-enabled profile there
    is refused loudly — discovery cannot select a write-enabled profile
    silently. Unknown roles are refused outright rather than defaulted.
    """
    if role not in ("discovery", "implementation", "verification"):
        return False, f"unknown role {role!r}"
    if profile.is_write_enabled and role in _WRITE_FORBIDDEN_ROLES:
        return False, f"{role} cannot select a write-enabled profile"
    return True, "ok"


def manifest_status(declared: set[str] | None) -> str:
    """Distinguish an absent from an explicitly-empty manifest (D06).

    ``None`` → ``"unset"`` (declared NOWHERE: the legacy default, every
    shipped driver); an EMPTY set → ``"declared_empty"`` (an explicit
    boundary that allows nothing); anything else → ``"declared"``.
    Callers widen only ``"unset"`` — an explicitly empty manifest is a
    decision, not a gap to repair.
    """
    if declared is None:
        return "unset"
    if not declared:
        return "declared_empty"
    return "declared"
