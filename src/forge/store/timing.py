"""The step's fenced state transition commits its measured interval once."""

from datetime import datetime
import math
from typing import Literal
from uuid import UUID, uuid5

from sqlalchemy.ext.asyncio import AsyncSession

from forge.store.models_timing import EngineStepTiming
from forge.store.records import Lease, StoreConflict


async def observe_timing(
    session: AsyncSession,
    lease: Lease,
    seconds: float | None,
    outcome: Literal["completed", "parked", "rescheduled"],
    now: datetime,
) -> None:
    if seconds is None:
        return
    if not math.isfinite(seconds) or not 0 <= seconds <= 86400:
        raise ValueError("observed step execution interval exceeds its finite bound")
    identity = uuid5(UUID(lease.step_id), "timing:" + str(lease.epoch)).hex
    previous = await session.get(EngineStepTiming, identity)
    if previous is not None:
        if (
            previous.run_id,
            previous.step_id,
            previous.lease_epoch,
            previous.handler,
            previous.seconds,
            previous.outcome,
        ) != (lease.run_id, lease.step_id, lease.epoch, lease.handler.value, seconds, outcome):
            raise StoreConflict("observed step execution interval is immutable")
        return
    session.add(
        EngineStepTiming(
            id=identity,
            run_id=lease.run_id,
            step_id=lease.step_id,
            lease_epoch=lease.epoch,
            handler=lease.handler.value,
            seconds=seconds,
            outcome=outcome,
            observed_at=now,
        )
    )
