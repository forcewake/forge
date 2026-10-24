"""Concurrent execution, control delivery, liveness and wall-clock enforcement."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from forge_protocol import ControlAck, ControlCommand, ControlDelivery, Heartbeat, Usage

from .checkpoint import Checkpoints
from .client import LaneClient
from .control_queue import ControlQueue
from .driver import Driver, DriverEvent, Session
from .errors import ExitCode, LaneFailure
from .trace import Trace


@dataclass(frozen=True)
class Timings:
    controls: float = 1.0
    heartbeat: float = 15.0
    checkpoint: float = 600.0
    shutdown: float = 10.0

    def __post_init__(self) -> None:
        if min(self.controls, self.heartbeat, self.checkpoint, self.shutdown) <= 0:
            raise ValueError("Supervisor intervals must be positive")


@dataclass(frozen=True)
class Outcome:
    code: ExitCode
    status: Literal["completed", "failed", "cancelled", "paused", "timed_out"]
    classification: str
    reason: str
    checkpoint_id: str | None = None
    usage: Usage | None = None


class Supervisor:
    def __init__(
        self,
        client: LaneClient,
        driver: Driver,
        session: Session,
        checkpoints: Checkpoints,
        timings: Timings,
        trace: Trace | None = None,
    ):
        self.client, self.driver, self.session = client, driver, session
        self.checkpoints, self.timings = checkpoints, timings
        self.trace = trace
        self.usage: Usage | None = None
        self.resume_session: str | None = None
        self._stop_lock = asyncio.Lock()
        self._checkpoint_lock = asyncio.Lock()
        self.stopped = False
        self.interrupting = asyncio.Event()

    async def stop(self) -> None:
        async with self._stop_lock:
            if self.stopped:
                return
            if self.interrupting.is_set():
                raise LaneFailure(
                    ExitCode.DRIVER, "interrupt_failed", "Driver stop outcome remains unknown"
                )
            self.interrupting.set()
            try:
                await asyncio.wait_for(self.session.interrupt(), self.timings.shutdown)
            except (Exception, asyncio.TimeoutError):
                raise LaneFailure(
                    ExitCode.DRIVER, "interrupt_failed", "Driver did not stop safely"
                ) from None
            self.stopped = True

    async def checkpoint(self) -> str:
        async with self._checkpoint_lock:
            manifest = await self.checkpoints.capture(self.resume_session)
            return manifest.checkpoint_id

    async def events(self) -> Outcome:
        async for event in self.session.events():
            if self.interrupting.is_set():
                await asyncio.Future()
            if not isinstance(event, DriverEvent):
                raise LaneFailure(
                    ExitCode.DRIVER, "invalid_driver_event", "Driver emitted an invalid event"
                )
            if self.trace is not None:
                self.trace.event(event)
            if event.usage is not None:
                self.usage = event.usage
            if event.resume_session is not None:
                self.resume_session = event.resume_session
            if event.kind == "failed":
                return Outcome(
                    ExitCode.DRIVER,
                    "failed",
                    "driver_failed",
                    event.terminal_reason or "Driver failed",
                    usage=self.usage,
                )
            if event.kind == "completed":
                return Outcome(
                    ExitCode.COMPLETED,
                    "completed",
                    "completed",
                    event.terminal_reason or "Driver completed",
                    usage=self.usage,
                )
        if self.interrupting.is_set():
            await asyncio.Future()
        raise LaneFailure(
            ExitCode.DRIVER, "incomplete_driver_stream", "Driver ended without a terminal event"
        )

    async def controls(self) -> Outcome:
        queue = ControlQueue(self.client, self.timings.controls)
        while True:
            command = await queue.next()
            if command.action == "steer" and "steer" in self.driver.capabilities:
                urgent = await self.steer(command, queue)
                if urgent is None:
                    continue
                command = urgent
            outcome = await self.apply(command)
            if outcome is not None:
                return outcome

    async def steer(self, command: ControlCommand, queue: ControlQueue) -> ControlCommand | None:
        delivery = await self.client.begin(command)
        work = asyncio.create_task(self.session.steer(command.text or ""))
        try:
            while True:
                done, _ = await asyncio.wait({work}, timeout=self.timings.controls)
                if work in done:
                    try:
                        work.result()
                    except Exception:
                        raise LaneFailure(
                            ExitCode.TRANSPORT, "control_failed", "Driver steering failed"
                        ) from None
                    await self.ack(delivery, "applied")
                    return None
                await queue.poll()
                urgent = queue.urgent()
                if urgent is not None:
                    await self.stop()
                    await self.ack(delivery, "refused", "Steering interrupted by operator control")
                    return urgent
        finally:
            if not work.done():
                work.cancel()
                done, _ = await asyncio.wait({work}, timeout=self.timings.shutdown)
                if not done:
                    raise LaneFailure(
                        ExitCode.DRIVER, "control_cleanup_failed", "Driver steering did not stop"
                    )
            if work.done() and not work.cancelled():
                work.exception()

    async def ack(
        self,
        delivery: ControlDelivery,
        status: Literal["applied", "refused"],
        detail: str = "",
        checkpoint_id: str | None = None,
    ) -> None:
        await self.client.ack(
            ControlAck(
                **delivery.model_dump(),
                status=status,
                detail=detail,
                checkpoint_id=checkpoint_id,
            )
        )

    async def apply(self, command: ControlCommand) -> Outcome | None:
        delivery = await self.client.begin(command)
        status: Literal["applied", "refused"] = "applied"
        detail, checkpoint_id, outcome = "", None, None
        if command.action in ("pause", "interrupt"):
            await self.stop()
            if command.action == "pause":
                checkpoint_id = await self.checkpoint()
                outcome = Outcome(
                    ExitCode.PAUSED,
                    "paused",
                    "paused",
                    "Paused by operator",
                    checkpoint_id,
                    self.usage,
                )
            else:
                outcome = Outcome(
                    ExitCode.CANCELLED,
                    "cancelled",
                    "interrupted",
                    "Interrupted by operator",
                    usage=self.usage,
                )
        elif command.action == "checkpoint":
            checkpoint_id = await self.checkpoint()
        elif command.action == "steer" and "steer" in self.driver.capabilities:
            await self.session.steer(command.text or "")
        else:
            status, detail = (
                "refused",
                "Resume needs a new generation"
                if command.action == "resume"
                else "Driver cannot steer",
            )
        await self.ack(delivery, status, detail, checkpoint_id)
        return outcome

    async def heartbeats(self) -> None:
        sequence = 0
        while True:
            sequence += 1
            await self.client.heartbeat(
                Heartbeat(
                    **self.client.identity,
                    sequence=sequence,
                    observed_at=datetime.now(timezone.utc),
                    phase="running",
                )
            )
            await asyncio.sleep(self.timings.heartbeat)

    async def periodic_checkpoints(self) -> None:
        while True:
            await asyncio.sleep(self.timings.checkpoint)
            await self.checkpoint()

    async def deadline(self, remaining: float) -> Outcome:
        await asyncio.sleep(max(0, remaining))
        await self.stop()
        return Outcome(
            ExitCode.DEADLINE,
            "timed_out",
            "wall_clock_exceeded",
            "Lane wall-clock budget exceeded",
            usage=self.usage,
        )

    async def run(self, remaining: float) -> Outcome:
        tasks = [
            asyncio.create_task(coroutine)
            for coroutine in (
                self.controls(),
                self.deadline(remaining),
                self.events(),
                self.heartbeats(),
                self.periodic_checkpoints(),
            )
        ]
        completed = False
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.exception() is not None:
                    task.result()
            for task in tasks:
                if task in done:
                    result = task.result()
                    if not isinstance(result, Outcome):
                        raise LaneFailure(
                            ExitCode.DRIVER, "supervisor_stopped", "Supervisor ended unexpectedly"
                        )
                    completed = result.code == ExitCode.COMPLETED
                    return result
            raise AssertionError("No completed supervisor task")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if not completed:
                await self.stop()
