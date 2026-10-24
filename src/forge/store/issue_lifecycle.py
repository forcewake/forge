"""A signed issue edit withdraws one pending gate before atomic fresh admission."""

from datetime import datetime
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.command_values import CommandEnvelope
from forge.domain.states import Handler, State
from forge.store.models_commands import EngineCommand
from forge.store.models_execution import EngineStep
from forge.store.models_runs import EngineGate
from forge.store.records import StoreConflict
from forge.store.transactions import locked_run


async def supersede_pending(
    session: AsyncSession,
    command: EngineCommand | None,
    run_id: str,
    subject_key: str,
    payload_json: str,
    max_active: int,
    now: datetime,
) -> None:
    if command is None:
        raise StoreConflict("issue refresh requires its signed command authority")
    envelope = CommandEnvelope.model_validate_json(command.document_json)
    run = await locked_run(session, run_id)
    pending = run.state == State.AWAITING_APPROVAL.value or (
        run.state == State.BLOCKED.value
        and run.failed_handler == Handler.APPROVE.value
        and run.reason == "approval_task_changed"
    )
    if envelope.issue_action != "edited" or not pending or run.cancel_requested:
        raise StoreConflict("issue_no_longer_pending")
    if run.subject_key != subject_key:
        raise StoreConflict("run_scope_mismatch")
    initial = await session.scalar(select(EngineStep).where(EngineStep.key == f"{run.id}:plan"))
    if initial is None:
        raise StoreConflict("original admission is missing")
    original, replacement = json.loads(initial.payload_json), json.loads(payload_json)
    if (
        original["policy"] != replacement["policy"]
        or original["snapshot"]["neighbors"] != replacement["snapshot"]["neighbors"]
        or original["max_active"] != replacement["max_active"]
        or original["gate_ttl_seconds"] != replacement["gate_ttl_seconds"]
        or replacement["max_active"] != max_active
    ):
        raise StoreConflict("native refresh changed the original frozen policy")
    run.cancel_requested, run.state, run.reason = True, State.CANCELLED.value, "issue_edited"
    run.cancellation_generation += 1
    run.version += 1
    gates = await session.scalars(
        select(EngineGate).where(
            EngineGate.run_id == run.id, EngineGate.state.in_(("prepared", "open"))
        )
    )
    for gate in gates:
        gate.state = "refused"
    steps = await session.scalars(
        select(EngineStep).where(
            EngineStep.run_id == run.id, EngineStep.state.in_(("scheduled", "running"))
        )
    )
    for step in steps:
        step.state, step.lease_owner, step.lease_expires_at = "cancelled", None, None
    await session.flush()
