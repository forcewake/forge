"""Current engine lane API transactions and their cancellation/version fence."""

from collections.abc import Callable
import inspect
from typing import Any, TypeVar, cast

from forge_protocol import ControlCommand, RunIdentity
from sqlalchemy import exists, select, update
from sqlalchemy.engine import CursorResult

from forge.api.lane_auth_values import PendingDispatch, identity_of
from forge.api.lane_v1_values import LaneConflict
from forge.domain.states import TERMINAL
from forge.store.lane_auth import locked_current
from forge.store.lane_state import load_namespace, persist_namespace
from forge.store.models_lane import EngineLaneSession
from forge.store.models_runs import EngineRun
from forge.store.transactions import SessionFactory, transaction

T = TypeVar("T")


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
            return result

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
