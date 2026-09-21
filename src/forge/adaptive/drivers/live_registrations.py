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
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from forge.adaptive.adapters import DriverMatrix, SDKS

__all__ = ["LIVE_REGISTRATIONS", "LiveRegistration", "seed_live_matrix"]

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


def seed_live_matrix(matrix: DriverMatrix | None = None) -> DriverMatrix:
    """Register every live-verified combination onto *matrix* (fresh if None).

    Each entry's evidence file must exist under the repo root AND its
    recorded ``all_ok`` must be true — a checkout holding a failed
    smoke run cannot claim the combination. Returns the matrix so
    onboarding can use the call inline.
    """
    target = matrix if matrix is not None else DriverMatrix()
    for entry in LIVE_REGISTRATIONS:
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
