"""Narrow recovery of the original logical step after effect reconciliation."""

from datetime import datetime

from sqlalchemy import select

from forge.domain.states import TERMINAL, State
from forge.store.models_execution import EngineEffect, EngineStep
from forge.store.records import EffectRecord, StoreConflict
from forge.store.transactions import (
    SessionFactory,
    aware,
    locked_run,
    stored_time,
    transaction,
    notify_steps,
)


class RecoveryStore:
    def __init__(self, factory: SessionFactory) -> None:
        self.factory = factory

    async def requeue_original(self, effect: EffectRecord, *, now: datetime) -> bool:
        """Only a current requested intent may reclaim its expired or ended step.

        A reconciler never revives a parked run, resets a lease epoch, changes a
        dispatch identity, or grants a new logical step an old effect's authority.
        The effect-layer adapter owns DefinitelyAbsent proof and its call window.
        """
        now = aware(now)
        async with transaction(self.factory) as session:
            run = await locked_run(session, effect.run_id)
            step = await session.scalar(
                select(EngineStep).where(EngineStep.id == effect.step_id).with_for_update()
            )
            row = await session.scalar(
                select(EngineEffect).where(EngineEffect.id == effect.id).with_for_update()
            )
            if row is None or row.version != effect.version or row.state != "requested":
                raise StoreConflict("effect reconciliation version changed")
            if (
                row.run_id != run.id
                or row.step_id != effect.step_id
                or step is None
                or step.run_id != run.id
            ):
                raise StoreConflict("effect no longer belongs to its original step")
            if run.cancel_requested or State(run.state) in TERMINAL:
                return False
            if (step.attempt, step.generation, step.cancellation_generation) != (
                run.attempt,
                run.generation,
                run.cancellation_generation,
            ):
                return False
            if (
                step.state == "running"
                and step.lease_expires_at is not None
                and stored_time(step.lease_expires_at) > now
            ):
                return False
            if step.state in {"scheduled", "succeeded", "cancelled"}:
                return False
            if step.deadline_at is not None and stored_time(step.deadline_at) <= now:
                return False
            if step.attempts >= step.max_attempts:
                return False
            # Old result bytes stay visible until a newly fenced completion replaces them.
            step.state, step.due_at = "scheduled", now
            step.lease_owner, step.lease_expires_at = None, None
            await session.flush()
            await notify_steps(session)
            return True
