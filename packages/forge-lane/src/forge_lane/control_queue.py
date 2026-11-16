"""Poll ordered commands while steering waits; urgent interruption can preempt it."""

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable

from forge_protocol import ControlCommand

from .client import LaneClient, transient
from .errors import ExitCode, LaneFailure


def transient_poll(exc: LaneFailure) -> bool:
    return transient(exc)


class ControlQueue:
    def __init__(
        self,
        client: LaneClient,
        interval: float,
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.client, self.interval = client, interval
        self._sleep = sleep or asyncio.sleep
        self.after = 0
        self.seen: set[str] = set()
        self.pending: deque[ControlCommand] = deque()

    async def poll(self) -> None:
        for command in await self._controls():
            if command.sequence <= self.after or command.command_id in self.seen:
                raise LaneFailure(
                    ExitCode.TRANSPORT, "control_order", "Controls are not unique and ordered"
                )
            if len(self.seen) >= 256:
                raise LaneFailure(
                    ExitCode.TRANSPORT, "control_limit", "Control count exceeds its bound"
                )
            self.after = command.sequence
            self.seen.add(command.command_id)
            self.pending.append(command)

    async def _controls(self) -> list[ControlCommand]:
        delay = min(self.interval, 1.0)
        for attempt in range(3):
            try:
                return await self.client.controls(self.after)
            except LaneFailure as exc:
                if attempt == 2 or not transient_poll(exc):
                    raise
                await self._sleep(delay)
                delay = min(delay * 2, 5.0)
        raise LaneFailure(ExitCode.TRANSPORT, "transport_error", "Control poll failed")

    async def next(self) -> ControlCommand:
        while not self.pending:
            await self.poll()
            if not self.pending:
                await asyncio.sleep(self.interval)
        return self.pending.popleft()

    def urgent(self) -> ControlCommand | None:
        for command in self.pending:
            if command.action in ("pause", "interrupt"):
                self.pending.remove(command)
                return command
        return None
