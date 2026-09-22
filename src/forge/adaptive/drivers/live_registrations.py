"""LIVE-verified DriverMatrix registrations (EXE-07's missing half).

The :class:`~forge.adaptive.adapters.DriverMatrix` refuses any
combination that was not TESTED — until now nothing populated it,
because nothing had run against the real vendor surfaces. The
``scripts/driver_live_smoke.py`` runs (2026-09-21, this machine) are
the first live evidence; this module ships them as DATA so onboarding
seeds its matrix from evidence instead of aspiration.

Honesty rules (the executed-evidence doctrine):

- Every entry names its evidence file under ``docs/evaluation/`` —
  :func:`seed_live_matrix` REFUSES an entry whose evidence file does
  not exist in the checkout, so a stripped release cannot claim
  combinations it cannot show.
- An entry records the binary versions it was verified against;
  vendor drift (the opencode v2.0.10 route rewrite, the codex sandbox
  spelling) is exactly what these fields are for.
- Registration means "the smoke's steps passed", nothing broader: not
  a full implement-verify-merge cycle, which the lane pilot records
  separately when it runs.

NXT-27 — capabilities are VERSIONED OBSERVED BEHAVIOR, not driver
names. :class:`ObservedCapabilities` records what one smoke actually
watched a SPECIFIC binary version do (native interrupt, mid-turn
steer, next-turn buffering...), and the lookup refuses to answer for
any other version: an unknown or upgraded binary has UNKNOWN
capabilities — never the ones inherited from the version the evidence
was recorded on. A stale registration is evidence about the PAST, so
:func:`seed_live_matrix` with ``installed_versions`` refuses to seed
present-tense support when the installed binary no longer matches the
recorded one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from forge.adaptive.adapters import DriverMatrix, SDKS

__all__ = [
    "LIVE_OBSERVED_CAPABILITIES",
    "LIVE_REGISTRATIONS",
    "LiveRegistration",
    "OBSERVED_CAPABILITY_VALUES",
    "ObservedCapabilities",
    "install_pin_of",
    "observed_capabilities",
    "sdk_version_of",
    "seed_live_matrix",
]

_REPO_ROOT = Path(__file__).resolve().parents[4]


@dataclass(frozen=True)
class LiveRegistration:
    """One (sdk, provider_route, credential_mode) proven on a real vendor."""

    sdk: str
    provider_route: str
    credential_mode: str
    evidence: str
    verified_against: tuple[str, ...]
    date: str

    def __post_init__(self) -> None:
        if self.sdk not in SDKS:
            raise ValueError(f"sdk must be one of {SDKS}, got {self.sdk!r}")


#: The live-verified combinations. Adding an entry requires the smoke
#: evidence committed at its ``evidence`` path — seed_live_matrix
#: enforces existence at runtime.
LIVE_REGISTRATIONS: tuple[LiveRegistration, ...] = (
    LiveRegistration(
        sdk="claude-sdk",
        provider_route="zai-anthropic-gateway",
        credential_mode="byok-env-token",
        evidence="docs/evaluation/2026-09-21-drivers/claude-live.json",
        verified_against=("claude 2.1.273 (Claude Code)", "claude-agent-sdk 0.2.157"),
        date="2026-09-21",
    ),
    LiveRegistration(
        sdk="codex-app",
        provider_route="chatgpt-login",
        credential_mode="chatgpt-plan",
        evidence="docs/evaluation/2026-09-21-drivers/codex-live.json",
        verified_against=("codex-cli 0.153.4",),
        date="2026-09-21",
    ),
    LiveRegistration(
        sdk="opencode-server",
        provider_route="zai-coding-plan",
        credential_mode="server-basic+stored-key",
        evidence="docs/evaluation/2026-09-21-drivers/opencode-live.json",
        verified_against=("opencode v2.0.10",),
        date="2026-09-21",
    ),
)


def seed_live_matrix(
    matrix: DriverMatrix | None = None,
    *,
    installed_versions: Mapping[str, str] | None = None,
) -> DriverMatrix:
    """Register every live-verified combination onto *matrix* (fresh if None).

    Each entry's evidence file must exist under the repo root AND its
    recorded ``all_ok`` must be true — a checkout holding a failed
    smoke run cannot claim the combination. Returns the matrix so
    onboarding can use the call inline.

    NXT-27: *installed_versions* (sdk → the INSTALLED binary's
    ``--version`` string; parameterized so tests and the lane preflight
    can supply it) engages the present-tense gate — a registration is
    evidence about the PAST, and seeding present-tense support requires
    the installed binary to still match the recorded
    ``verified_against[0]`` EXACTLY:

    - an sdk whose installed version is missing from the mapping is
      refused (unknown present ⇒ no claim), and
    - a mismatch is refused as vendor drift — re-run
      ``scripts/driver_live_smoke.py`` on the new binary before the
      combination may be seeded again.

    ``None`` (the default) keeps the evidence-only path: historical
    seeding where no binaries are installed makes no present-tense
    claim to gate.
    """
    target = matrix if matrix is not None else DriverMatrix()
    for entry in LIVE_REGISTRATIONS:
        if installed_versions is not None:
            installed = installed_versions.get(entry.sdk)
            if installed is None:
                raise ValueError(
                    f"live registration ({entry.sdk}/{entry.provider_route}): the "
                    f"installed binary version is UNKNOWN — a past registration "
                    f"is not a claim about an unverified present"
                )
            recorded = entry.verified_against[0]
            if installed != recorded:
                raise ValueError(
                    f"live registration ({entry.sdk}/{entry.provider_route}) verified "
                    f"against {recorded!r} but the installed binary reports "
                    f"{installed!r} — vendor drift; re-run the live smoke on this "
                    f"version before seeding"
                )
        path = _REPO_ROOT / entry.evidence
        if not path.is_file():
            raise FileNotFoundError(
                f"live registration ({entry.sdk}/{entry.provider_route}) claims "
                f"{entry.evidence}, which is not in this checkout — a combination "
                f"without its evidence is not registerable"
            )
        evidence = json.loads(path.read_text())
        if not evidence.get("all_ok"):
            failures = evidence.get("failures") or ["unknown"]
            raise ValueError(
                f"live registration ({entry.sdk}/{entry.provider_route}) cites "
                f"{entry.evidence} whose recorded run FAILED ({failures}) — "
                f"re-run scripts/driver_live_smoke.py until green before seeding"
            )
        target.register(entry.sdk, entry.provider_route, entry.credential_mode)
    return target


def sdk_version_of(sdk: str) -> str | None:
    """The binary version this checkout's evidence was verified against.

    The recorded ``verified_against`` of the newest registration for
    *sdk*; vendor-drift diagnosis starts here.
    """
    for entry in reversed(LIVE_REGISTRATIONS):
        if entry.sdk == sdk:
            return entry.verified_against[0]
    return None


# ---------------------------------------------------------------------------
# NXT-27 — capabilities as versioned observed behavior
# ---------------------------------------------------------------------------

#: The closed observed-capability vocabulary. These are BEHAVIORS a smoke
#: watched a real binary do — deliberately distinct from the adapter
#: METHOD names (a method existing is not a capability working) and from
#: :data:`~forge.adaptive.capability_profiles.CAPABILITIES` (the planning
#: axes). The ladder matters: an interrupt REQUEST accepted is not the
#: interrupt OUTCOME observed; input buffered for the NEXT turn is not
#: mid-turn steering; neither implies a portable checkpoint.
OBSERVED_CAPABILITY_VALUES: tuple[str, ...] = (
    #: A driven turn reached its terminal verdict on this version.
    "turn",
    #: The vendor's interrupt/abort REQUEST was issued without error.
    "native_interrupt",
    #: The terminal OUTCOME of the turn after an interrupt was observed
    #: (e.g. codex ``turn/completed(interrupted)``) — acceptance of the
    #: request alone does not earn this.
    "interrupt_outcome_observed",
    #: Input was applied to the ACTIVE turn on this version.
    "mid_turn_steer",
    #: Input was buffered and applied to a LATER turn — never advertised
    #: as immediate mid-turn application (NXT-27 acceptance criterion).
    "next_turn_input",
    #: WIP export was captured from a live/interrupted session.
    "wip_export",
    #: A native session restored on ANOTHER runner (never inferred from
    #: an interrupt smoke — the review's core refusal).
    "cross_runner_restore",
)


@dataclass(frozen=True)
class ObservedCapabilities:
    """What ONE smoke watched ONE binary version actually do (NXT-27).

    ``binary_version`` is the exact ``--version`` string the evidence
    recorded (``LiveRegistration.verified_against[0]``); ``install_pin``
    is the CLI-installable version spec the lane templates pin by
    default (the npm tag / installer ``--version`` argument), so the
    templates' defaults and the evidence cannot drift apart silently.
    ``observed`` is the closed :data:`OBSERVED_CAPABILITY_VALUES`
    vocabulary — anything NOT in it was not observed on this version,
    and :func:`observed_capabilities` never answers for a different
    version.
    """

    sdk: str
    binary_version: str
    install_pin: str
    observed: tuple[str, ...]
    date: str

    def __post_init__(self) -> None:
        if self.sdk not in SDKS:
            raise ValueError(f"sdk must be one of {SDKS}, got {self.sdk!r}")
        if not self.binary_version:
            raise ValueError("binary_version must be the recorded --version string")
        if not self.install_pin:
            raise ValueError("install_pin must be the CLI-installable version spec")
        unknown = set(self.observed) - set(OBSERVED_CAPABILITY_VALUES)
        if unknown:
            raise ValueError(
                f"observed capabilities outside the closed vocabulary: {sorted(unknown)}"
            )
        if not self.observed:
            raise ValueError("an empty observation row records nothing — drop the row instead")

    def supports(self, capability: str) -> bool:
        """Whether THIS binary version was observed doing *capability*.

        A name outside the closed vocabulary is a modelling error and
        raises; a vocabulary name not in ``observed`` answers False —
        unobserved is unsupported, never guessed.
        """
        if capability not in OBSERVED_CAPABILITY_VALUES:
            raise ValueError(
                f"unknown observed capability {capability!r}; "
                f"vocabulary is {OBSERVED_CAPABILITY_VALUES}"
            )
        return capability in self.observed

    def unobserved(self) -> tuple[str, ...]:
        """The vocabulary this version was NOT observed doing — recorded
        explicitly so "untested" is data, not absence of data."""
        return tuple(c for c in OBSERVED_CAPABILITY_VALUES if c not in self.observed)


#: The per-driver observed-capability rows, one per LIVE registration's
#: binary version. Grounded in the evidence files' own ``steps`` — no
#: capability is listed that a recorded step does not show, and the
#: absent ones (wip export, cross-runner restore on every driver so far)
#: stay absent: DriverMatrix can never infer checkpoint portability from
#: an interrupt smoke.
LIVE_OBSERVED_CAPABILITIES: tuple[ObservedCapabilities, ...] = (
    ObservedCapabilities(
        sdk="claude-sdk",
        binary_version="claude 2.1.273 (Claude Code)",
        install_pin="2.1.273",
        observed=(
            "turn",  # query(turn1=PONG) → ResultMessage(completed)
            "next_turn_input",  # send+query(turn2=BONG): buffered for the NEXT turn
            "native_interrupt",  # interrupt(long turn) issued without error
            "interrupt_outcome_observed",  # post-interrupt ResultMessage seen — terminal_reason "completed": the turn RAN ON despite the request
        ),
        date="2026-09-21",
    ),
    ObservedCapabilities(
        sdk="codex-app",
        binary_version="codex-cli 0.153.4",
        install_pin="0.153.4",
        observed=(
            "turn",  # turn1(completed)
            "mid_turn_steer",  # steer_active_turn on the running turn
            "native_interrupt",  # interrupt issued
            "interrupt_outcome_observed",  # interrupt(->interrupted): the vendor status rode back
        ),
        date="2026-09-21",
    ),
    ObservedCapabilities(
        sdk="opencode-server",
        binary_version="opencode v2.0.10",
        install_pin="2.0.10",
        observed=(
            "turn",  # prompt(BONG)+events
            "native_interrupt",  # abort(long session) issued; events kept flowing
        ),
        date="2026-09-21",
    ),
)


def observed_capabilities(sdk: str, binary_version: str) -> ObservedCapabilities | None:
    """The observation row for *sdk* on EXACTLY *binary_version*, or None.

    None is the honest answer for an unknown, upgraded or reformatted
    version string: capabilities are never inherited from another
    version, and a ``None`` row means every ``supports()`` question
    about that binary is unknown — re-run the smoke (or pin the
    template default back to the verified version) before claiming
    anything.
    """
    for row in LIVE_OBSERVED_CAPABILITIES:
        if row.sdk == sdk and row.binary_version == binary_version:
            return row
    return None


def install_pin_of(sdk: str) -> str | None:
    """The CLI-installable version pin the lane templates default to.

    The newest observation row's :attr:`ObservedCapabilities.install_pin`
    for *sdk* — the single source the templates' FORGE_*_VERSION
    defaults cite, so a new smoke recording a new version moves the
    template pins with it (or the pins and the evidence visibly
    disagree).
    """
    for row in reversed(LIVE_OBSERVED_CAPABILITIES):
        if row.sdk == sdk:
            return row.install_pin
    return None
