"""Enforce the approved dispatch before obtaining a model grant."""

import asyncio
from pathlib import Path

from forge_protocol import Brief, LaneDispatch

from .driver import Driver
from .errors import ExitCode, LaneFailure


async def prepare_policy(
    brief: Brief, dispatch: LaneDispatch, driver: Driver, workspace: Path, *, timeout: float
) -> None:
    spec = brief.frozen_spec
    if spec is not None and (
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
    elif spec is not None or brief.execution_policy.mcp_servers:
        raise LaneFailure(
            ExitCode.DRIVER,
            "policy_capability_unavailable",
            "Driver cannot enforce this frozen policy",
        )
