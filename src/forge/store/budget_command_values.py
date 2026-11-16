"""Exact accepted command and active key facts needed before a native cap update."""

from datetime import datetime
import json
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.budget.command_values import AmendmentRequest, parse_amendment
from forge.command_values import CommandEnvelope, CommandResult
from forge.domain.budget import Pool, Resources
from forge.domain.states import State
from forge.port_values import GatewayKeyRef
from forge.store.command_runs import target_runs
from forge.store.models_budget import EngineBudgetAmendment, EngineBudgetPool, EngineGatewayKey
from forge.store.models_commands import EngineCommand
from forge.store.models_runs import EngineRun
from forge.store.records import StoreConflict
from forge.store.transactions import locked_run, stored_time


def effect_key(command_id: str) -> str:
    return "command:" + command_id + ":budget-update"


async def owned_run(
    session: AsyncSession, command: EngineCommand, request: AmendmentRequest
) -> EngineRun:
    envelope = CommandEnvelope.model_validate_json(command.document_json)
    run = await locked_run(session, request.run_id)
    if (
        command.kind != "amend"
        or envelope.command.kind != "amend"
        or parse_amendment(envelope.command.argument) != request
        or run.id not in {row.id for row in await target_runs(session, envelope)}
    ):
        raise StoreConflict("run_scope_mismatch")
    return run


async def active_key(
    session: AsyncSession,
    run: EngineRun,
    purpose: Literal["implementation", "closing"],
    now: datetime,
) -> EngineGatewayKey:
    row = await session.scalar(
        select(EngineGatewayKey).where(
            EngineGatewayKey.run_id == run.id,
            EngineGatewayKey.attempt == run.attempt,
            EngineGatewayKey.generation == run.generation,
            EngineGatewayKey.purpose == purpose,
        )
    )
    if (
        run.cancel_requested
        or State(run.state) in {State.CANCELLED, State.READY_FOR_HUMAN}
        or row is None
        or row.run_id != run.id
        or row.purpose != purpose
        or row.attempt != run.attempt
        or row.generation != run.generation
        or row.state != "active"
        or stored_time(row.expires_at) <= now
        or row.reference_json is None
    ):
        raise StoreConflict("amendment_grant_inactive")
    reference = GatewayKeyRef.model_validate_json(row.reference_json)
    if reference.blocked or reference.alias != row.alias or reference.expires_at <= now:
        raise StoreConflict("amendment_grant_inactive")
    return row


async def pool_row(session: AsyncSession, run_id: str, purpose: str) -> EngineBudgetPool:
    row = await session.scalar(
        select(EngineBudgetPool)
        .where(EngineBudgetPool.run_id == run_id, EngineBudgetPool.purpose == purpose)
        .with_for_update()
    )
    if row is None:
        raise StoreConflict("amendment_grant_inactive")
    return row


async def amendment_row(session: AsyncSession, command: EngineCommand) -> EngineBudgetAmendment:
    row = await session.scalar(
        select(EngineBudgetAmendment)
        .where(
            EngineBudgetAmendment.command_key == command.id,
            EngineBudgetAmendment.run_id == command.run_id,
        )
        .with_for_update()
    )
    if row is None or row.target_json is None or row.reservation_key is None:
        raise StoreConflict("amendment decision is missing")
    return row


def update_payload(row: EngineBudgetAmendment, alias: str) -> str:
    assert row.reservation_key is not None and row.target_json is not None
    return json.dumps(
        {
            "alias": alias,
            "allowance_json": Resources.model_validate_json(row.target_json).model_dump_json(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


async def authorize_update(
    session: AsyncSession, command: EngineCommand, payload: str, now: datetime
) -> None:
    if command.result_json is None or command.kind != "amend":
        raise StoreConflict("command update requires a durable amendment decision")
    result = CommandResult.model_validate_json(command.result_json)
    request = parse_amendment(
        CommandEnvelope.model_validate_json(command.document_json).command.argument
    )
    decision = json.loads(result.data_json)
    run = await owned_run(session, command, request)
    key = await active_key(session, run, request.purpose, now)
    row = await amendment_row(session, command)
    assert row.target_json is not None
    pool = await pool_row(session, run.id, request.purpose)
    value = Pool.model_validate_json(pool.state_json)
    reservation = next(
        (item for item in value.reservations if item.identity == row.reservation_key), None
    )
    if (
        result.status != "accepted"
        or result.reason != "budget_amendment_authorized"
        or row.state not in ("authorized", "applied")
        or row.gateway_effect_key != effect_key(command.id)
        or row.amount_json != request.amount.model_dump_json()
        or row.reason != request.reason
        or row.actor != CommandEnvelope.model_validate_json(command.document_json).actor.native_id
        or row.reservation_key != f"{request.purpose}:{run.attempt}:{run.generation}"
        or decision.get("alias") != key.alias
        or decision.get("purpose") != request.purpose
        or decision.get("amount") != request.amount.model_dump(mode="json")
        or decision.get("target")
        != Resources.model_validate_json(row.target_json).model_dump(mode="json")
        or decision.get("cancellation_generation") != run.cancellation_generation
        or reservation is None
        or reservation.status != "open"
        or payload != update_payload(row, key.alias)
        or reservation.allowance != Resources.model_validate_json(row.target_json)
        or not any(
            item.command_id == command.id and item.amount == request.amount
            for item in value.amendments
        )
    ):
        raise StoreConflict("command update differs from its persisted amendment")
