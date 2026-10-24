"""Enforce the approved dispatch before obtaining a model grant."""

import asyncio
from pathlib import Path

from forge_protocol import Brief, LaneDispatch

from .driver import Driver
from .errors import ExitCode, LaneFailure


async def prepare_policy(
    brief: Brief, dispatch: LaneDispatch, driver: Driver, workspace: Path, *, timeout: float
) -> None:
    if brief.repair != dispatch.repair:
        raise LaneFailure(
            ExitCode.APPROVAL, "repair_mismatch", "Repair context differs from dispatch"
        )
    spec = brief.frozen_spec
    if (
        spec.driver != dispatch.driver
        or spec.model != dispatch.model
        or spec.limits.wallclock_seconds != dispatch.timeout_seconds
    ):
        raise LaneFailure(
            ExitCode.APPROVAL,
            "frozen_dispatch_mismatch",
            "Dispatch differs from frozen execution limits",
        )
    prepare = getattr(driver, "prepare", None)
    if callable(prepare):
        try:
            async with asyncio.timeout(timeout):
                await prepare(brief, workspace)
        except TimeoutError:
            raise LaneFailure(
                ExitCode.DEADLINE,
                "wall_clock_exceeded",
                "Policy preflight exceeded wall-clock budget",
            ) from None
    else:
        raise LaneFailure(
            ExitCode.DRIVER,
            "policy_capability_unavailable",
            "Driver cannot enforce this frozen policy",
        )

    if (spec.metering == "metered") != driver.requires_model_key:
        raise LaneFailure(
            ExitCode.DRIVER,
            "unsupported_metering_policy",
            "Driver credential mode differs from the approved metering policy",
        )
