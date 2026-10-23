"""Experimental community drivers require explicit operator activation."""

import os

from ..errors import ExitCode, LaneFailure


def require_enabled() -> None:
    if os.environ.get("FORGE_LANE_ENABLE_CONTRIB") != "1":
        raise LaneFailure(
            ExitCode.CONFIGURATION,
            "contrib_disabled",
            "Set FORGE_LANE_ENABLE_CONTRIB=1 to activate experimental contrib drivers",
        )
