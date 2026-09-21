"""Package-level wiring pins for :mod:`forge.adaptive.drivers`.

The adapter contracts in ``adapters.py`` are frozen; the driver modules
are new. This file pins the JOIN between them: every real client class
must present exactly the Protocol surface its adapter injects, the
package re-exports must stay importable WITHOUT any vendor package
installed (the ``interactive`` extra is optional), and the three driver
families must cover the closed :data:`SDKS` vocabulary. A rename on
either side of the join fails here instead of in a lane.
"""

from __future__ import annotations

import inspect

import forge.adaptive.drivers as drivers
from forge.adaptive.adapters import SDKS

#: (driver class, the Protocol's method names) — the frozen join.
_PROTOCOL_JOIN: tuple[tuple[type, tuple[str, ...]], ...] = (
    (drivers.ClaudeSDKDriverClient, ("start_session", "send", "interrupt", "query")),
    (drivers.CodexAppDriverClient, ("start_thread", "send_turn", "steer_active_turn", "interrupt")),
    (drivers.OpenCodeDriverClient, ("start_session", "prompt", "events", "abort")),
)


def test_real_clients_present_the_protocol_surface() -> None:
    """Every driver class exposes its Protocol's methods, all async."""
    for cls, methods in _PROTOCOL_JOIN:
        for name in methods:
            fn = getattr(cls, name, None)
            assert fn is not None, f"{cls.__name__} is missing Protocol method {name!r}"
            assert inspect.iscoroutinefunction(fn), f"{cls.__name__}.{name} must be async"


def test_drivers_cover_the_closed_sdk_vocabulary() -> None:
    """One driver family per SDK name in :data:`SDKS` — no orphan surface."""
    classes = {cls for cls, _ in _PROTOCOL_JOIN}
    assert len(classes) == len(SDKS), (
        f"SDKS declares {len(SDKS)} surfaces but {len(classes)} driver classes exist"
    )


def test_package_imports_without_vendor_packages() -> None:
    """The drivers package imports clean with no vendor SDK installed.

    The claude lane guards its import; importing the PACKAGE must never
    require ``claude-agent-sdk`` (CI and the API process both import
    forge without the ``interactive`` extra).
    """
    assert drivers.ClaudeSDKDriverClient is not None
    assert drivers.CodexAppDriverClient is not None
    assert drivers.OpenCodeDriverClient is not None


def test_factories_are_exported() -> None:
    """The three ``*_from_env`` factories ride the package surface."""
    for name in (
        "claude_sdk_client_from_env",
        "codex_app_client_from_env",
        "opencode_client_from_env",
    ):
        assert callable(getattr(drivers, name, None)), f"missing factory export {name!r}"
