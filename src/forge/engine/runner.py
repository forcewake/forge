"""Fenced execution of one durable step, with bounded retries and lease renewal."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
import json
import math
from typing import Generic, Protocol, TypeVar

from forge.domain.states import Handler, State
from forge.store.records import RestartLane
from forge.ports import Clock
from forge.store.approval_values import ApprovalCommit
from forge.store.records import Lease, StepRecord, StepRequest, StaleLease
from forge.store.steps import StepStore
from forge.store.transactions import canonical_json

E = TypeVar("E")


class Retryable(Exception):
    """A safe retry observation; an unknown external effect is never this error."""

    def __init__(self, reason: str) -> None:
        if not reason or len(reason) > 200:
            raise ValueError("retry reason must be a bounded nonsecret code")
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class RetryPolicy:
    base_seconds: float = 60
    cap_seconds: float = 900

    def __post_init__(self) -> None:
        if (
            not all(
                math.isfinite(value) and value > 0
                for value in (self.base_seconds, self.cap_seconds)
            )
            or self.base_seconds > self.cap_seconds
            or self.cap_seconds > 3600
        ):
            raise ValueError("invalid retry bounds")

    def delay(self, attempt: int) -> float:
        return float(min(self.cap_seconds, self.base_seconds * 2 ** min(max(attempt - 1, 0), 30)))


@dataclass(frozen=True)
class Park:
    reason: str
    state: State = State.BLOCKED

    def __post_init__(self) -> None:
        if (
            self.state not in {State.BLOCKED, State.FAILED}
            or not self.reason
            or len(self.reason) > 300
        ):
            raise ValueError("park requires a bounded reason and blocked/failed state")


@dataclass(frozen=True)
class StepResult:
    result_json: str
    successors: tuple[StepRequest, ...] = ()
    park: Park | None = None
    next_state: State | None = None
    approval: ApprovalCommit | None = None
    restart_lane: RestartLane | None = None
    defer_until: datetime | None = None

    def __post_init__(self) -> None:
        canonical_json(self.result_json)
        if self.defer_until is not None and (
            self.successors or self.park or self.next_state or self.approval or self.restart_lane
        ):
            raise ValueError("a waiting step cannot complete or mutate lifecycle")
        if self.restart_lane is not None and (self.park or self.next_state or self.approval):
            raise ValueError("lane revival is a separate atomic completion")
        object.__setattr__(self, "successors", tuple(self.successors))
        if self.park is not None and (
            self.successors or self.next_state is not None or self.approval is not None
        ):
            raise ValueError("a parked step cannot schedule successors or another state")


@dataclass
class LeaseHolder:
    current: Lease


@dataclass(frozen=True)
class StepContext(Generic[E]):
    holder: LeaseHolder
    step: StepRecord
    effects: E

    @property
    def lease(self) -> Lease:
        return self.holder.current

    @property
    def payload_json(self) -> str:
        return self.step.payload_json


class StepHandler(Protocol[E]):
    async def __call__(self, context: StepContext[E]) -> StepResult: ...


@dataclass(frozen=True)
class RunTick:
    claimed: int
    completed: int
    rescheduled: int
    parked: int
    lost: int


class StepRunner(Generic[E]):
    def __init__(
        self,
        store: StepStore,
        handlers: Mapping[Handler, StepHandler[E]],
        *,
        effects: E,
        clock: Clock,
        owner: str,
        lease_seconds: float = 60,
        renew_seconds: float | None = None,
        retry: RetryPolicy = RetryPolicy(),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        renewal = renew_seconds if renew_seconds is not None else lease_seconds / 3
        if not 0 < renewal < lease_seconds <= 3600:
            raise ValueError("renewal must occur before a bounded lease expires")
        self.store, self.handlers, self.effects = store, dict(handlers), effects
        self.clock, self.owner, self.lease_seconds = clock, owner, lease_seconds
        self.renew_seconds, self.retry, self.sleep = renewal, retry, sleep

    async def tick(self, *, limit: int = 1) -> RunTick:
        leases = await self.store.claim_due(self.owner, self.clock.now(), self.lease_seconds, limit)
        tasks = [asyncio.create_task(self._execute(lease)) for lease in leases]
        try:
            outcomes = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return RunTick(
            len(leases),
            outcomes.count("completed"),
            outcomes.count("rescheduled"),
            outcomes.count("parked"),
            outcomes.count("lost"),
        )

    async def _execute(self, lease: Lease) -> str:
        holder = LeaseHolder(lease)
        step = await self.store.load(lease.step_id)
        if step is None:
            return "lost"
        try:
            await self.store.assert_current(lease, now=self.clock.now())
        except StaleLease:
            return "lost"
        handler = self.handlers.get(lease.handler)
        if handler is None:
            return await self._park(holder.current, "handler_unregistered")
        task = asyncio.create_task(handler(StepContext(holder, step, self.effects)))
        renewal = asyncio.create_task(self._renew(holder, task))
        try:
            result = await task
            if not isinstance(result, StepResult):
                raise TypeError("handler returned an invalid step result")
        except Retryable as error:
            await self._stop(renewal)
            try:
                retry = await self.store.reschedule(
                    holder.current,
                    due_at=self.clock.now() + timedelta(seconds=self.retry.delay(step.attempts)),
                    now=self.clock.now(),
                    reason=f"retry_exhausted:{error.reason}",
                )
                return "parked" if retry.state == "dead" else "rescheduled"
            except StaleLease:
                return "lost"
        except asyncio.CancelledError:
            if (
                renewal.done()
                and not renewal.cancelled()
                and isinstance(renewal.exception(), StaleLease)
            ):
                return "lost"
            raise
        except Exception as error:
            await self._stop(renewal)
            return await self._park(holder.current, f"handler_error:{type(error).__name__}")
        finally:
            await self._stop(renewal)
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        return await self._complete(holder, result)

    async def _complete(self, holder: LeaseHolder, result: StepResult) -> str:
        try:
            if result.defer_until is not None:
                retried = await self.store.reschedule(
                    holder.current,
                    due_at=result.defer_until,
                    reason="awaiting_observation",
                    now=self.clock.now(),
                )
                return "rescheduled" if retried.state == "scheduled" else "parked"
            completed = await self.store.complete_and_enqueue(
                holder.current,
                result.result_json,
                result.successors,
                now=self.clock.now(),
                transition_to=result.park.state if result.park else result.next_state,
                reason=result.park.reason if result.park else None,
                approval=result.approval,
                restart_lane=result.restart_lane,
            )
            return "parked" if result.park or completed.state == "failed" else "completed"
        except StaleLease:
            return "lost"

    async def _renew(self, holder: LeaseHolder, task: asyncio.Task[StepResult]) -> None:
        try:
            while not task.done():
                await self.sleep(self.renew_seconds)
                if not task.done():
                    holder.current = await self.store.renew(
                        holder.current, now=self.clock.now(), lease_seconds=self.lease_seconds
                    )
        except Exception:
            task.cancel()
            raise

    async def _park(self, lease: Lease, reason: str) -> str:
        try:
            await self.store.complete_and_enqueue(
                lease,
                json.dumps({"reason": reason}, separators=(",", ":")),
                now=self.clock.now(),
                transition_to=State.BLOCKED,
                reason=reason,
                outcome="failed",
            )
            return "parked"
        except StaleLease:
            return "lost"

    @staticmethod
    async def _stop(task: asyncio.Task[None]) -> None:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, StaleLease):
            await task
