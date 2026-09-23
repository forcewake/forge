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

NEXT-13 — the capability vocabulary extends past the LIVE-registered
sdks to ``copilot-acp``: the ACP client's capability profile is
published from CONTRACT-TEST evidence
(:mod:`forge.adaptive.drivers.copilot_acp`'s suite over the
shape-faithful wire fake + the wire facts
``docs/research/2026-09-23-copilot-sdk-lane.md`` documents), NEVER from
a live smoke that has not run — so the row names its ``evidence``
class explicitly, no ``LiveRegistration`` backs it, and
:data:`DRIVER_SDK_OF` keeps the copilot lanes unregistered (no
present-tense live claim). The profile is honest about what is NOT
supported: ACP v1 has no steer method (protocol absence, §5), so
``mid_turn_steer`` stays unobserved however well the client tests.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from forge.adaptive.adapters import DriverMatrix, SDKS

__all__ = [
    "COPILOT_ACP_SDK",
    "DRIVER_SDK_OF",
    "EVIDENCE_CLASSES",
    "LIVE_OBSERVED_CAPABILITIES",
    "LIVE_REGISTRATIONS",
    "LiveRegistration",
    "OBSERVED_CAPABILITY_SDKS",
    "OBSERVED_CAPABILITY_VALUES",
    "ObservedCapabilities",
    "RegistrationVerdict",
    "install_pin_of",
    "observed_capabilities",
    "provenance_report",
    "registration_verdict",
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

#: The ACP driver's sdk name (NEXT-13). NOT in :data:`SDKS`/the
#: :class:`~forge.adaptive.adapters.DriverMatrix` vocabulary: the matrix
#: registers LIVE-verified combinations, and no live smoke has run against
#: ``copilot --acp`` — the capability row below is contract-test evidence,
#: which is a different (weaker, honestly labelled) evidence class.
COPILOT_ACP_SDK = "copilot-acp"

#: The closed sdk vocabulary of the OBSERVATION rows: the three
#: LIVE-registered sdks plus the contract-tested copilot-acp row. Wider
#: than :data:`~forge.adaptive.adapters.SDKS` by exactly that one name —
#: observation rows may cite evidence the registration matrix refuses to
#: seed, never the reverse.
OBSERVED_CAPABILITY_SDKS: tuple[str, ...] = (*SDKS, COPILOT_ACP_SDK)

#: The closed evidence-class vocabulary an observation row may cite.
#: ``live-smoke`` — a real binary run recorded under ``docs/evaluation/``
#: (what every registration-backed row is). ``contract-tests`` — the
#: driver's own suite over a shape-faithful wire fake plus the research
#: doc's documented wire facts: it proves the CLIENT's behavior model, and
#: makes NO present-tense claim about any installed binary.
EVIDENCE_CLASSES: tuple[str, ...] = ("live-smoke", "contract-tests")

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
    """What ONE evidence run watched ONE binary version actually do (NXT-27).

    ``binary_version`` is the exact version string the evidence recorded
    (``LiveRegistration.verified_against[0]`` for a live smoke; the
    handshake ``agentInfo`` string the contract-test fake reports for
    copilot-acp); ``install_pin`` is the CLI-installable version spec the
    lane templates pin by default (the npm tag / installer ``--version``
    argument), so the templates' defaults and the evidence cannot drift
    apart silently. ``observed`` is the closed
    :data:`OBSERVED_CAPABILITY_VALUES` vocabulary — anything NOT in it
    was not observed on this version, and :func:`observed_capabilities`
    never answers for a different version. ``evidence`` (NEXT-13) names
    the evidence class: a ``contract-tests`` row proves the client's
    behavior model, never a live claim about the pinned binary.
    """

    sdk: str
    binary_version: str
    install_pin: str
    observed: tuple[str, ...]
    date: str
    evidence: str = "live-smoke"

    def __post_init__(self) -> None:
        if self.sdk not in OBSERVED_CAPABILITY_SDKS:
            raise ValueError(f"sdk must be one of {OBSERVED_CAPABILITY_SDKS}, got {self.sdk!r}")
        if not self.binary_version:
            raise ValueError("binary_version must be the recorded --version string")
        if not self.install_pin:
            raise ValueError("install_pin must be the CLI-installable version spec")
        if self.evidence not in EVIDENCE_CLASSES:
            raise ValueError(
                f"evidence class must be one of {EVIDENCE_CLASSES}, got {self.evidence!r}"
            )
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


#: The per-driver observed-capability rows. The three registration-backed
#: rows are grounded in the evidence files' own ``steps`` — no capability
#: is listed that a recorded step does not show, and the absent ones (wip
#: export, cross-runner restore on every driver so far) stay absent:
#: DriverMatrix can never infer checkpoint portability from an interrupt
#: smoke. The copilot-acp row (NEXT-13) is CONTRACT-TEST evidence: exactly
#: what ``tests/test_adaptive_driver_copilot_acp.py`` exercises — the
#: prompt flow to a terminal record (``turn``), the session/cancel
#: notification with its ledger bookkeeping (``native_interrupt``), and
#: serial next-turn prompts on the same session (``next_turn_input``) —
#: nothing more. ``mid_turn_steer`` is a PROTOCOL absence on ACP v1 (§5,
#: the client raises TurnInProgressError rather than queueing), and
#: ``interrupt_outcome_observed`` requires a real binary's wire (#4561's
#: lying ``end_turn``), which no fake can testify to; both stay
#: unobserved, and so do both checkpoint portability behaviors.
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
    # NEXT-13 — the tested Copilot ACP capability profile. binary_version
    # is the handshake ``agentInfo`` string the contract-test fake (and the
    # documented real handshake shape, research §3.1) reports for the
    # PINNED CLI; install_pin is the same DEFAULT_DRIVER_VERSIONS spec the
    # copilot lanes install. NO LiveRegistration backs this row: a live
    # smoke has not run, so seeding/verdict paths make no present-tense
    # claim — this row describes the CLIENT's tested behavior model.
    ObservedCapabilities(
        sdk=COPILOT_ACP_SDK,
        binary_version="Copilot 1.0.86 (protocol v1)",
        install_pin="1.0.86",
        observed=(
            "turn",  # session/prompt → terminal record (stopReason, ledger-corrected)
            "native_interrupt",  # session/cancel notification + the CancelLedger bookkeeping
            "next_turn_input",  # send() = a NEW prompt on the same session, serially
        ),
        date="2026-09-23",
        evidence="contract-tests",
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


# ---------------------------------------------------------------------------
# R28-26 — exact-binary provenance: what the dispatch DECLARED, what the
# registration VERIFIED, what the runner ACTUALLY INSTALLED.
# ---------------------------------------------------------------------------

#: harness driver id → the registration sdk whose ``verified_against[0]``
#: recorded the binary this lane's CLI was last LIVE-verified as. The
#: scripted ``claude-code`` lane installs the same
#: ``@anthropic-ai/claude-code`` binary the ``claude-sdk`` registration
#: verified; grok-build and copilot have no live registration yet — they
#: are absent, which reads as ``unregistered`` (the honest non-claim),
#: never as a silently inherited verdict.
DRIVER_SDK_OF: Mapping[str, str] = {
    "claude-code": "claude-sdk",
    "claude-sdk-lane": "claude-sdk",
    "codex-sdk-lane": "codex-app",
    "opencode": "opencode-server",
    "opencode-sdk-lane": "opencode-server",
}


@dataclass(frozen=True)
class RegistrationVerdict:
    """The present-tense comparison of ONE lane binary against its registration.

    The evidence-matrix doctrine, executable: ``recorded`` is the PAST
    (the exact ``--version`` string the smoke verified), ``installed``
    is the PRESENT (the exact string the runner reported), and the
    ``status`` never conflates them — ``match``, ``drift``, or
    ``unknown_present`` when the runner did not report a version. A
    drift carries a ``warning`` (the lane keeps running — the
    registration is evidence, not a gate) naming BOTH versions and the
    remedy: re-run ``scripts/driver_live_smoke.py`` on the new binary.
    """

    driver: str
    sdk: str
    recorded: str
    installed: str
    status: str
    warning: str


def registration_verdict(driver: str, installed_cli_version: str) -> RegistrationVerdict | None:
    """Compare one lane driver's ACTUAL binary against its live registration.

    ``None`` for a driver no registration covers (``DRIVER_SDK_OF`` has
    no entry, or the sdk has no registration row): no registration, no
    verdict — an unregistered driver is reported by
    :func:`provenance_report` as exactly that, never as a match
    inherited from a sibling sdk.
    """
    sdk = DRIVER_SDK_OF.get(driver)
    if sdk is None:
        return None
    recorded = sdk_version_of(sdk)
    if recorded is None:
        return None
    installed = str(installed_cli_version or "").strip()
    if not installed:
        return RegistrationVerdict(
            driver,
            sdk,
            recorded,
            "",
            "unknown_present",
            f"{driver}: the runner did not report the installed CLI version"
            f" ({sdk} was verified against {recorded!r}) — the present tense"
            " of this lane's binary is unknown",
        )
    if installed == recorded:
        return RegistrationVerdict(driver, sdk, recorded, installed, "match", "")
    return RegistrationVerdict(
        driver,
        sdk,
        recorded,
        installed,
        "drift",
        f"{driver}: live registration verified {sdk} against {recorded!r} but the"
        f" runner installed {installed!r} — the registration is evidence of the"
        " PAST, not a claim about this binary; re-run"
        " scripts/driver_live_smoke.py on the installed version before"
        " promoting a recipe that cites it",
    )


def _pin_satisfied(declared_pin: str, installed_cli_version: str) -> bool | None:
    """Whether the declared pin plausibly produced the installed version.

    ``None`` is the honest non-answer: an unknown pin, an unreported
    install, or the literal ``latest`` (an unpinned install makes no
    exactness claim to check). Otherwise the pin must appear in the
    installed ``--version`` string (the same containment the
    observation rows' ``install_pin``/``binary_version`` pair uses).
    """
    pin = str(declared_pin or "").strip()
    installed = str(installed_cli_version or "").strip()
    if not pin or not installed or pin == "latest":
        return None
    return pin in installed


def provenance_report(
    driver: str,
    *,
    installed_cli_version: str = "",
    declared_pin: str | None = None,
    expected_resource_sha256: str = "",
    installed_resource_sha256: str = "",
) -> dict[str, object]:
    """Reconcile the THREE provenance sources for one lane driver (R28-26).

    - what the DISPATCH declared — *declared_pin* (the resolved
      ``FORGE_DRIVER_VERSIONS`` entry; the shipped
      ``DEFAULT_DRIVER_VERSIONS`` table when not supplied);
    - what the REGISTRATION verified — the newest live registration's
      ``verified_against[0]`` for the driver's sdk;
    - what the RUNNER actually installed — *installed_cli_version* (the
      ``FORGE_DRIVER_FINGERPRINT`` the lane preamble reports).

    NEXT-17 adds the IMMUTABLE-RESOURCE leg the shipped templates pin:
    *expected_resource_sha256* is the content hash the lane template
    declared (the forge wheel's ``#sha256=`` pin — ``.forge/
    lane_install.json`` records both halves at install time) and
    *installed_resource_sha256* what the runner actually hashed. The
    report surfaces ``expected_resource_sha256``,
    ``installed_resource_sha256`` and ``resource_hash_matches`` —
    ``None`` when neither half is known (no claim), ``False`` (with a
    loud warning) when a template-pinned resource hashed elsewhere
    than the template said it would.

    Returns a JSON-shaped report (``driver``, ``sdk``, ``declared_pin``,
    ``registration_verified``, ``registration_date``,
    ``installed_cli_version``, ``pin_matches_install``,
    ``registration_status`` — ``match``/``drift``/``unknown_present``/
    ``unregistered`` — the three ``resource_sha256`` fields and
    ``warnings``). Warnings never gate: the doctrine is that a
    registration is evidence of the PAST and the fingerprint is the
    PRESENT, so a drift is SAID, loudly, while the lane keeps running.
    """
    from forge.harnesses.script_render import DEFAULT_DRIVER_VERSIONS

    pin = str(
        declared_pin if declared_pin is not None else DEFAULT_DRIVER_VERSIONS.get(driver, "")
    ).strip()
    installed = str(installed_cli_version or "").strip()
    expected_hash = str(expected_resource_sha256 or "").strip().lower()
    installed_hash = str(installed_resource_sha256 or "").strip().lower()
    warnings: list[str] = []

    if not pin:
        warnings.append(f"{driver}: no declared version pin — the install rides a default")
    elif pin == "latest":
        warnings.append(
            f"{driver}: the dispatch declared the unpinned 'latest' dist-tag — the"
            " installed binary is whatever the registry served, by design"
        )
    if not installed:
        warnings.append(
            f"{driver}: the runner did not report the installed CLI version —"
            " provenance of the PRESENT is unknown"
        )
    if expected_hash and not installed_hash:
        warnings.append(
            f"{driver}: the template pinned resource sha256:{expected_hash} but the "
            "runner never reported the installed resource hash — the immutable-"
            "resource leg of this lane's provenance is unknown"
        )
    resource_hash_matches: bool | None
    if expected_hash and installed_hash:
        resource_hash_matches = expected_hash == installed_hash
        if resource_hash_matches is False:
            warnings.append(
                f"{driver}: the template pinned resource sha256:{expected_hash} but "
                f"the installed resource hashed to sha256:{installed_hash} — the "
                "bytes this lane runs are NOT the bytes the recipe approved"
            )
    else:
        resource_hash_matches = None

    verdict = registration_verdict(driver, installed)
    if verdict is None:
        sdk = DRIVER_SDK_OF.get(driver, "")
        status = "unregistered"
        recorded = ""
        date = ""
        if sdk:
            warnings.append(
                f"{driver}: no live registration covers sdk {sdk!r} — no past"
                " verification to compare the installed binary against"
            )
        else:
            warnings.append(
                f"{driver}: no sdk mapping to a live registration — this driver's"
                " binary has never been smoke-verified"
            )
    else:
        sdk = verdict.sdk
        status = verdict.status
        recorded = verdict.recorded
        date = next((entry.date for entry in LIVE_REGISTRATIONS if entry.sdk == verdict.sdk), "")
        if verdict.warning:
            warnings.append(verdict.warning)

    matches = _pin_satisfied(pin, installed)
    if matches is False:
        warnings.append(
            f"{driver}: the declared pin {pin!r} does not appear in the installed"
            f" version string {installed!r} — the install is not what was dispatched"
        )
    return {
        "driver": driver,
        "sdk": sdk,
        "declared_pin": pin,
        "registration_verified": recorded,
        "registration_date": date,
        "installed_cli_version": installed,
        "pin_matches_install": matches,
        "registration_status": status,
        "expected_resource_sha256": expected_hash,
        "installed_resource_sha256": installed_hash,
        "resource_hash_matches": resource_hash_matches,
        "warnings": warnings,
    }
