"""Closed command transactions revoke run authority before cleanup is attempted."""

from datetime import datetime
import json

from sqlalchemy import select

from forge.command_values import CommandEnvelope, CommandLease, CommandResult
from forge.domain.states import TERMINAL, State
from forge.store.commands import command_fence, resolve_in
from forge.store.command_runs import target_runs
from forge.store.models_execution import EngineStep
from forge.store.models_budget import EngineGatewayKey
from forge.store.records import StoreConflict
from forge.store.transactions import SessionFactory, locked_run, transaction


class OperatorStore:
    def __init__(self, factory: SessionFactory) -> None:
        self.factory = factory

    async def cancel(self, lease: CommandLease, run_id: str, *, now: datetime) -> CommandResult:
        async with transaction(self.factory) as session:
            command = await command_fence(session, lease, now)
            if command.result_json is not None:
                return CommandResult.model_validate_json(command.result_json)
            envelope = CommandEnvelope.model_validate_json(command.document_json)
            if envelope.command.kind != "cancel":
                raise StoreConflict("command is not a cancellation")
            run = await locked_run(session, run_id)
            if run.id not in {row.id for row in await target_runs(session, envelope)}:
                raise StoreConflict("run_scope_mismatch")
            if State(run.state) in TERMINAL:
                result = CommandResult(status="observed", reason="run_terminal", run_id=run.id)
            else:
                # Commit revocation and withdrawal before any gateway/provider I/O.
                run.cancel_requested, run.state = True, State.CANCELLED.value
                run.cancellation_generation += 1
                run.version += 1
                run.reason = "operator_cancelled"
                rows = await session.scalars(
                    select(EngineStep).where(
                        EngineStep.run_id == run.id, EngineStep.state.in_(("scheduled", "running"))
                    )
                )
                for row in rows:
                    row.state, row.lease_owner, row.lease_expires_at = "cancelled", None, None
                aliases = list(
                    await session.scalars(
                        select(EngineGatewayKey.alias).where(
                            EngineGatewayKey.run_id == run.id, EngineGatewayKey.state != "purged"
                        )
                    )
                )
                result = CommandResult(
                    status="accepted",
                    reason="cancelled",
                    run_id=run.id,
                    data_json=json.dumps(
                        {
                            "cancellation_generation": run.cancellation_generation,
                            "cleanup_pending": sorted(aliases),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            resolve_in(command, result)
            return result

    async def cleanup_aliases(self, lease: CommandLease, *, now: datetime) -> tuple[str, ...]:
        async with transaction(self.factory) as session:
            command = await command_fence(session, lease, now)
            if command.kind != "cancel" or command.result_json is None or command.run_id is None:
                return ()
            result = CommandResult.model_validate_json(command.result_json)
            if result.status != "accepted" or result.reason != "cancelled":
                return ()
            return tuple(
                await session.scalars(
                    select(EngineGatewayKey.alias).where(
                        EngineGatewayKey.run_id == command.run_id,
                        EngineGatewayKey.state != "purged",
                    )
                )
            )
