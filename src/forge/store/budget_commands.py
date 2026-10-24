"""A current command owns an immutable amendment and its exact external cap update."""

from datetime import datetime
import json
from uuid import uuid4

from sqlalchemy import select

from forge.budget.command_values import AmendmentRequest
from forge.command_values import CommandEnvelope, CommandLease, CommandResult
from forge.domain.budget import Pool, Resources
from forge.port_values import Applied, GatewayOutcome, GatewayKeyRef
from pydantic import TypeAdapter
from forge.store.budget_command_values import (
    active_key,
    amendment_row,
    authorize_update,
    effect_key,
    owned_run,
    pool_row,
    update_payload,
)
from forge.store.budgets import BudgetStore
from forge.store.commands import command_fence, resolve_in
from forge.store.models_budget import EngineBudgetAmendment, EngineGatewayKey
from forge.store.models_execution import EngineEffect
from forge.store.records import StoreConflict
from forge.store.transactions import SessionFactory, payload_digest, transaction


class BudgetCommands:
    def __init__(self, factory: SessionFactory) -> None:
        self.factory = factory

    async def authorize(
        self, lease: CommandLease, request: AmendmentRequest, *, now: datetime
    ) -> CommandResult:
        async with transaction(self.factory) as session:
            command = await command_fence(session, lease, now)
            if command.result_json is not None:
                return CommandResult.model_validate_json(command.result_json)
            run = await owned_run(session, command, request)
            key = await active_key(session, run, request.purpose, now)
            pool = await pool_row(session, run.id, request.purpose)
            value = Pool.model_validate_json(pool.state_json)
            reservation_key = f"{request.purpose}:{run.attempt}:{run.generation}"
            reservation = next(
                (row for row in value.reservations if row.identity == reservation_key), None
            )
            pending = await session.scalar(
                select(EngineBudgetAmendment.id).where(
                    EngineBudgetAmendment.pool_id == pool.id,
                    EngineBudgetAmendment.reservation_key == reservation_key,
                    EngineBudgetAmendment.state.in_(("authorized", "unknown")),
                )
            )
            if reservation is None or reservation.status != "open" or pending is not None:
                raise StoreConflict("amendment_grant_inactive")
            assert key.reference_json is not None
            if (
                GatewayKeyRef.model_validate_json(key.reference_json).allowance
                != reservation.allowance
            ):
                raise StoreConflict("amendment_grant_inactive")
            target = reservation.allowance + request.amount
            envelope = CommandEnvelope.model_validate_json(command.document_json)
            amendment = EngineBudgetAmendment(
                id=uuid4().hex,
                run_id=run.id,
                pool_id=pool.id,
                command_key=command.id,
                actor=envelope.actor.native_id,
                reason=request.reason,
                amount_json=request.amount.model_dump_json(),
                state="authorized",
                gateway_effect_key=effect_key(command.id),
                reservation_key=reservation_key,
                target_json=target.model_dump_json(),
            )
            session.add(amendment)
            await BudgetStore.stage_in(session, amendment)
            result = CommandResult(
                status="accepted",
                reason="budget_amendment_authorized",
                run_id=run.id,
                data_json=json.dumps(
                    {
                        "alias": key.alias,
                        "purpose": request.purpose,
                        "amount": request.amount.model_dump(mode="json"),
                        "target": target.model_dump(mode="json"),
                        "cancellation_generation": run.cancellation_generation,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            resolve_in(command, result)
            return result

    async def pending(self, lease: CommandLease, *, now: datetime) -> str | None:
        async with transaction(self.factory) as session:
            command = await command_fence(session, lease, now)
            if command.result_json is None:
                raise StoreConflict("amendment decision was not committed")
            result = CommandResult.model_validate_json(command.result_json)
            if result.status != "accepted" or result.reason != "budget_amendment_authorized":
                return None
            row = await amendment_row(session, command)
            if row.state in ("applied", "refused"):
                return None
            payload = update_payload(row, json.loads(result.data_json)["alias"])
            try:
                await authorize_update(session, command, payload, now)
            except StoreConflict:
                effect = await session.scalar(
                    select(EngineEffect).where(EngineEffect.key == effect_key(command.id))
                )
                if effect is None or effect.state == "requested":
                    await BudgetStore.unstage_in(session, row)
                row.state = "refused"
                return None
            return payload

    async def apply(self, lease: CommandLease, payload: str, *, now: datetime) -> bool:
        async with transaction(self.factory) as session:
            command = await command_fence(session, lease, now)
            await authorize_update(session, command, payload, now)
            row = await amendment_row(session, command)
            effect = await session.scalar(
                select(EngineEffect).where(EngineEffect.key == effect_key(command.id))
            )
            if (
                effect is None
                or effect.command_id != command.id
                or effect.kind != "gw_update"
                or effect.run_id != command.run_id
                or effect.step_id is not None
                or effect.payload_json != payload
                or effect.payload_digest != payload_digest(payload)
                or effect.state not in ("committed", "adopted")
                or effect.outcome_json is None
            ):
                return False
            assert row.target_json is not None
            outcome: GatewayOutcome = TypeAdapter(GatewayOutcome).validate_json(effect.outcome_json)
            reference = await session.scalar(
                select(EngineGatewayKey.reference_json).where(
                    EngineGatewayKey.alias == json.loads(payload)["alias"],
                    EngineGatewayKey.run_id == command.run_id,
                )
            )
            if not isinstance(outcome, Applied) or (
                outcome.value.alias != json.loads(payload)["alias"]
                or outcome.value.allowance != Resources.model_validate_json(row.target_json)
                or reference is None
                or outcome.value != GatewayKeyRef.model_validate_json(reference)
            ):
                return False
            await BudgetStore.apply_in(session, row)
            return True

    async def refuse(self, lease: CommandLease, payload: str, *, now: datetime) -> bool:
        async with transaction(self.factory) as session:
            command = await command_fence(session, lease, now)
            row = await amendment_row(session, command)
            effect = await session.scalar(
                select(EngineEffect).where(EngineEffect.key == effect_key(command.id))
            )
            if (
                effect is None
                or effect.command_id != command.id
                or effect.kind != "gw_update"
                or effect.run_id != command.run_id
                or effect.step_id is not None
                or effect.key != effect_key(command.id)
                or effect.payload_json != payload
                or effect.payload_digest != payload_digest(payload)
                or effect.state != "failed"
                or effect.outcome_json is None
            ):
                return False
            from forge.port_values import Refused

            outcome: GatewayOutcome = TypeAdapter(GatewayOutcome).validate_json(effect.outcome_json)
            if not isinstance(outcome, Refused):
                return False
            await BudgetStore.unstage_in(session, row)
            row.state = "refused"
            return True
