"""Current engine lane API transactions and their cancellation/version fence."""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import inspect
from typing import Any, TypeVar, cast

from forge_protocol import ControlCommand, RunIdentity
from sqlalchemy import and_, delete, exists, or_, select, update
from sqlalchemy.sql.selectable import Select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from forge.api.lane_auth_values import PendingDispatch, identity_of
from forge.api.lane_v1_values import LaneConflict
from forge.domain.states import TERMINAL
from forge.store.lane_auth import locked_current
from forge.store.lane_state import load_namespace, persist_namespace
from forge.store.models_execution import EngineStep
from forge.store.models_lane import EngineBlob, EngineLaneSession
from forge.store.models_runs import EngineRun
from forge.store.transactions import SessionFactory, notify_steps, transaction

T = TypeVar("T")


async def release_await_step(session: AsyncSession, row: EngineLaneSession) -> None:
    """A stored lane report should not wait out the await poll interval."""
    now = datetime.now(timezone.utc)
    result = await session.execute(
        update(EngineStep)
        .where(
            EngineStep.run_id == row.run_id,
            EngineStep.attempt == row.attempt,
            EngineStep.generation == row.generation,
            EngineStep.handler == "await_lane",
            EngineStep.state == "scheduled",
            EngineStep.due_at > now,
        )
        .values(due_at=now)
        .execution_options(synchronize_session=False)
    )
    if cast(CursorResult[Any], result).rowcount:
        await notify_steps(session)


class EngineLaneV1Store:
    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory

    async def access(
        self,
        identity: RunIdentity,
        operation: Callable[[PendingDispatch, dict[str, Any], dict[str, Any]], T],
        *,
        write: bool = False,
    ) -> T:
        identity = identity_of(identity)
        async with transaction(self.session_factory) as session:
            run, row, pending = await locked_current(session, identity)
            state, namespace = await load_namespace(session, row, pending)
            result = operation(pending, state, namespace)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise LaneConflict("lane store operations must be synchronous")
            if write:
                document = await persist_namespace(session, run, row, pending, state, namespace)
                active = exists(
                    select(EngineRun.id).where(
                        EngineRun.id == row.run_id,
                        EngineRun.attempt == row.attempt,
                        EngineRun.generation == row.generation,
                        EngineRun.cancellation_generation == row.cancellation_generation,
                        EngineRun.cancel_requested.is_(False),
                        EngineRun.version == run.version,
                        EngineRun.state.not_in(tuple(item.value for item in TERMINAL)),
                    )
                )
                result_row = await session.execute(
                    update(EngineLaneSession)
                    .where(
                        EngineLaneSession.id == row.id,
                        EngineLaneSession.cancellation_generation
                        == pending.cancellation_generation,
                        active,
                    )
                    .values(api_state_json=document)
                    .execution_options(synchronize_session=False)
                )
                if cast(CursorResult[Any], result_row).rowcount != 1:
                    raise LaneConflict("run changed during lane mutation")
                if isinstance(state.get("report"), dict):
                    await release_await_step(session, row)
            return result

    def _reapable_runs(self, now: datetime, grace_seconds: int) -> Select[tuple[str]]:
        cutoff = now - timedelta(seconds=grace_seconds)
        unretryable = or_(
            EngineRun.state.in_(("cancelled", "ready_for_human")),
            and_(
                EngineRun.state.in_(("blocked", "failed")),
                EngineRun.failed_handler.is_(None),
            ),
        )
        return select(EngineRun.id).where(unretryable, EngineRun.updated_at <= cutoff)

    async def terminal_only_digests(
        self, limit: int, *, now: datetime | None = None, grace_seconds: int = 24 * 3600
    ) -> list[str]:
        moment = now or datetime.now(timezone.utc)
        eligible = self._reapable_runs(moment, grace_seconds)
        retained = select(EngineBlob.digest).where(EngineBlob.run_id.not_in(eligible))
        async with self.session_factory() as session:
            rows = await session.scalars(
                select(EngineBlob.digest)
                .where(EngineBlob.run_id.in_(eligible), EngineBlob.digest.not_in(retained))
                .limit(limit)
            )
        return list(dict.fromkeys(rows))

    async def forget_terminal_digests(
        self,
        digests: list[str],
        *,
        now: datetime | None = None,
        grace_seconds: int = 24 * 3600,
    ) -> list[str]:
        if not digests:
            return []
        moment = now or datetime.now(timezone.utc)
        eligible = self._reapable_runs(moment, grace_seconds)
        async with transaction(self.session_factory) as session:
            await session.execute(
                delete(EngineBlob).where(
                    EngineBlob.digest.in_(digests),
                    EngineBlob.run_id.in_(eligible),
                )
            )
            remaining = set(
                await session.scalars(
                    select(EngineBlob.digest).where(EngineBlob.digest.in_(digests))
                )
            )
        return [digest for digest in digests if digest not in remaining]

    async def digest_referenced(self, digest: str) -> bool:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(EngineBlob.digest).where(EngineBlob.digest == digest).limit(1)
            )
        return row is not None

    async def enqueue_control(self, command: ControlCommand) -> ControlCommand:
        def append(
            pending: PendingDispatch, state: dict[str, Any], namespace: dict[str, Any]
        ) -> ControlCommand:
            controls = state.setdefault("controls", [])
            raw = command.model_dump(mode="json")
            for previous in controls:
                if previous["command_id"] == command.command_id:
                    if previous != raw:
                        raise LaneConflict("control id is already registered differently")
                    return command
            if state.get("report") is not None:
                raise LaneConflict("dispatch already has a terminal report")
            if len(controls) >= 256 or command.sequence != namespace["control_next_sequence"]:
                raise LaneConflict(
                    "control sequence must be contiguous within its attempt and bounded"
                )
            controls.append(raw)
            return command

        return await self.access(command, append, write=True)
