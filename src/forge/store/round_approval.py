"""A correction gate belongs to one durable native approval command and round."""

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.command_values import CommandEnvelope, CommandResult
from forge.port_values import ReviewRef
from forge.store.approval import load_prepared
from forge.store.approval_values import ApprovalClaim
from forge.store.models_commands import EngineCommand
from forge.store.models_execution import EngineStep
from forge.store.models_lane import EngineReviewRound
from forge.store.models_runs import EngineRun
from forge.store.records import StoreConflict
from forge.store.rounds import RoundWindow, original, review_key, revision_intent, window_in


async def native_command(
    session: AsyncSession, row: EngineCommand | None, run_id: str, reason: str
) -> CommandEnvelope:
    if (
        row is None
        or row.run_id != run_id
        or row.result_json is None
        or row.state not in ("resolved", "completed")
    ):
        raise StoreConflict("round approval command is missing")
    result = CommandResult.model_validate_json(row.result_json)
    if (result.status, result.run_id, result.reason) != ("accepted", run_id, reason):
        raise StoreConflict("round approval command has no accepted child")
    envelope = CommandEnvelope.model_validate_json(row.document_json)
    if envelope.source_key != row.source_key or envelope.command.kind != row.kind:
        raise StoreConflict("round approval command identity changed")
    return envelope


async def approval_window(
    session: AsyncSession, run_id: str, step_id: str, claim: ApprovalClaim
) -> tuple[RoundWindow, CommandEnvelope, CommandEnvelope]:
    run = await session.get(EngineRun, run_id)
    step = await session.get(EngineStep, step_id)
    prefix = f"{run_id}:approve:"
    if (
        run is None
        or run.root_run_id == run.id
        or run.parent_run_id is None
        or step is None
        or step.run_id != run_id
        or step.handler != "approve"
        or not step.key.startswith(prefix)
        or not re.fullmatch(r"[0-9a-f]{32}", step.key[len(prefix) :])
    ):
        raise StoreConflict("child approval requires its native revision command")
    approving = await native_command(
        session,
        await session.get(EngineCommand, step.key[len(prefix) :]),
        run_id,
        "revision_approval_scheduled",
    )
    if (
        approving.command.kind != "approve_revision"
        or approving.command.argument.strip() != run_id
        or not isinstance(approving.target, ReviewRef)
        or approving.actor != claim.actor
    ):
        raise StoreConflict("child approval differs from its native revision command")
    slot = await session.scalar(
        select(EngineReviewRound).where(
            EngineReviewRound.child_run_id == run_id,
            EngineReviewRound.state == "open",
            EngineReviewRound.root_review_ref == review_key(approving.target),
        )
    )
    if (
        slot is None
        or slot.root_run_id != run.root_run_id
        or slot.parent_run_id != run.parent_run_id
    ):
        raise StoreConflict("child approval round lineage is missing")
    fixing = await native_command(
        session,
        await session.scalar(
            select(EngineCommand).where(EngineCommand.source_key == slot.feedback_key)
        ),
        run_id,
        "revision_staged",
    )
    if (
        fixing.command.kind != "fix"
        or not isinstance(fixing.target, ReviewRef)
        or review_key(fixing.target) != slot.root_review_ref
    ):
        raise StoreConflict("original correction feedback differs from its round")
    window = await window_in(session, run_id, approving.target, correction=False)
    intent, prepared = await original(session, run_id), await load_prepared(session, run_id)
    if (
        window.base_sha != slot.base_sha
        or window.parent_id != slot.parent_run_id
        or intent != revision_intent(window, fixing.actor, fixing)
        or prepared is None
        or intent.freeze(prepared.spec.plan) != prepared.spec
        or prepared.approved.approved_digest != claim.approved_digest
    ):
        raise StoreConflict("child proposal differs from its frozen correction authority")
    return window, fixing, approving
