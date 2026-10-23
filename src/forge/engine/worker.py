"""New-engine worker composition, without a legacy queue or Redis adapter."""

import asyncio
from dataclasses import dataclass
from typing import Generic, TypeVar

from forge.engine.reconciler import Reconciler, ReconcilePass
from forge.engine.runner import RunTick, StepRunner
from forge.engine.wakeups import Wakeups

E = TypeVar("E")


@dataclass(frozen=True)
class WorkerTick:
    recovery: ReconcilePass
    execution: RunTick


class EngineWorker(Generic[E]):
    def __init__(
        self,
        runner: StepRunner[E],
        reconciler: Reconciler,
        wakeups: Wakeups,
        *,
        concurrency: int = 4,
    ) -> None:
        if not 1 <= concurrency <= 100:
            raise ValueError("worker concurrency must be bounded")
        self.runner, self.reconciler, self.wakeups = runner, reconciler, wakeups
        self.concurrency = concurrency

    async def tick(self) -> WorkerTick:
        recovery = await self.reconciler.pass_once()
        execution = await self.runner.tick(limit=self.concurrency)
        return WorkerTick(recovery, execution)

    async def serve(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            tick = await self.tick()
            if tick.execution.claimed < self.concurrency:
                await self._idle(stop)

    async def _idle(self, stop: asyncio.Event) -> None:
        waiter, stopped = asyncio.create_task(self.wakeups.wait()), asyncio.create_task(stop.wait())
        try:
            await asyncio.wait((waiter, stopped), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (waiter, stopped):
                if not task.done():
                    task.cancel()
            await asyncio.gather(waiter, stopped, return_exceptions=True)
