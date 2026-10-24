"""Normalized lane artifact and mailbox rows, committed with their session fence."""

import json
from typing import Any
from uuid import uuid4

from forge_protocol import CandidateBundle, CheckpointManifest, ControlCommand, digest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.api.lane_auth_values import PendingDispatch
from forge.api.lane_v1_values import LaneConflict, bind_submission
from forge.store.lane_auth import encoded
from forge.store.models_execution import EngineCandidate
from forge.store.models_lane import (
    EngineBlob,
    EngineCheckpoint,
    EngineControlCommand,
    EngineLaneSession,
)
from forge.store.models_runs import EngineRun
from forge.store.lane_control_receipts import persist_receipts

Json = dict[str, Any]


def dispatch_conditions(model: Any, pending: PendingDispatch) -> tuple[Any, ...]:
    return (
        model.run_id == pending.identity.forge_run_id,
        model.attempt == pending.identity.attempt,
        model.generation == pending.identity.generation,
    )


async def load_namespace(
    session: AsyncSession, row: EngineLaneSession, pending: PendingDispatch
) -> tuple[Json, Json]:
    state: Json = json.loads(row.api_state_json)
    if not isinstance(state, dict):
        raise LaneConflict("stored lane API state is invalid")
    identity = pending.identity.model_dump(mode="json")
    if state.get("identity", identity) != identity:
        raise LaneConflict("stored API identity differs from dispatch")
    state["identity"] = identity
    blobs = (
        await session.scalars(select(EngineBlob).where(*dispatch_conditions(EngineBlob, pending)))
    ).all()
    controls = (
        await session.scalars(
            select(EngineControlCommand)
            .where(*dispatch_conditions(EngineControlCommand, pending))
            .order_by(EngineControlCommand.sequence)
        )
    ).all()
    candidates = await session.scalar(
        select(EngineCandidate).where(*dispatch_conditions(EngineCandidate, pending))
    )
    checkpoints = (
        await session.scalars(select(EngineCheckpoint).where(EngineCheckpoint.run_id == row.run_id))
    ).all()
    state["blobs"] = {item.digest: item.size for item in blobs}
    state["controls"] = [json.loads(item.payload_json) for item in controls]
    state["deliveries"] = {
        item.command_key: json.loads(item.delivery_json)
        for item in controls
        if item.delivery_json is not None
    }
    state["acks"] = {
        item.command_key: json.loads(item.acknowledgement_json)
        for item in controls
        if item.acknowledgement_json is not None
    }
    state.pop("candidate", None)
    if candidates is not None:
        state["candidate"] = json.loads(candidates.manifest_json)
    next_sequence = 1 + (
        await session.scalar(
            select(func.max(EngineControlCommand.sequence)).where(
                EngineControlCommand.run_id == row.run_id,
                EngineControlCommand.attempt == row.attempt,
            )
        )
        or 0
    )
    return state, {
        "current": state,
        "checkpoints": {item.id: json.loads(item.manifest_json) for item in checkpoints},
        "control_next_sequence": next_sequence,
        "pending_controls": [
            json.loads(item.payload_json)
            for item in controls
            if item.state == "pending"
            and item.acknowledgement_json is None
            and item.delivery_json is None
        ],
    }


async def persist_checkpoints(
    session: AsyncSession, pending: PendingDispatch, namespace: Json
) -> None:
    for checkpoint_id, raw in namespace.get("checkpoints", {}).items():
        value = CheckpointManifest.model_validate(raw)
        if value.checkpoint_id != checkpoint_id:
            raise LaneConflict("checkpoint identity differs from its document")
        previous = await session.get(EngineCheckpoint, checkpoint_id)
        document = encoded(value.model_dump(mode="json"))
        if previous is not None:
            if (
                previous.run_id != pending.identity.forge_run_id
                or previous.manifest_json != document
            ):
                raise LaneConflict("checkpoint is already registered differently")
            continue
        bind_submission(pending, value)
        session.add(
            EngineCheckpoint(
                id=checkpoint_id,
                run_id=pending.identity.forge_run_id,
                attempt=value.attempt,
                generation=value.generation,
                digest=checkpoint_id,
                base_sha=value.base_sha,
                approved_digest=value.approved_digest,
                driver=value.driver,
                manifest_json=document,
                retained=True,
            )
        )
    await session.flush()


async def persist_blobs(session: AsyncSession, pending: PendingDispatch, state: Json) -> None:
    rows = (
        await session.scalars(select(EngineBlob).where(*dispatch_conditions(EngineBlob, pending)))
    ).all()
    previous = {row.digest: row for row in rows}
    for digest_value, size in state.get("blobs", {}).items():
        if type(size) is not int or size < 0:
            raise LaneConflict("invalid blob byte count")
        if digest_value in previous:
            if previous[digest_value].size != size:
                raise LaneConflict("blob size is already registered differently")
            continue
        session.add(
            EngineBlob(
                id=uuid4().hex,
                run_id=pending.identity.forge_run_id,
                attempt=pending.identity.attempt,
                generation=pending.identity.generation,
                digest=digest_value,
                size=size,
                storage_ref=digest_value,
            )
        )


async def persist_candidate(session: AsyncSession, pending: PendingDispatch, state: Json) -> None:
    raw = state.get("candidate")
    if raw is None:
        return
    value = CandidateBundle.model_validate(raw)
    bind_submission(pending, value)
    document = encoded(value.model_dump(mode="json"))
    previous = await session.scalar(
        select(EngineCandidate).where(*dispatch_conditions(EngineCandidate, pending))
    )
    if previous is not None:
        if previous.manifest_json != document:
            raise LaneConflict("candidate is already registered differently")
        return
    session.add(
        EngineCandidate(
            id=uuid4().hex,
            run_id=pending.identity.forge_run_id,
            attempt=value.attempt,
            generation=value.generation,
            digest=digest(value),
            base_sha=value.base_sha,
            approved_digest=value.approved_digest,
            manifest_json=document,
            state="received",
        )
    )


async def persist_controls(
    session: AsyncSession, run: EngineRun, pending: PendingDispatch, state: Json
) -> None:
    for raw in state.get("controls", []):
        value = ControlCommand.model_validate(raw)
        if (value.forge_run_id, value.attempt, value.generation) != (
            run.id,
            run.attempt,
            run.generation,
        ):
            raise LaneConflict("control identity differs from dispatch")
        document = encoded(value.model_dump(mode="json"))
        previous = await session.scalar(
            select(EngineControlCommand).where(
                EngineControlCommand.run_id == run.id,
                EngineControlCommand.command_key == value.command_id,
            )
        )
        if previous is None:
            previous = EngineControlCommand(
                id=uuid4().hex,
                run_id=run.id,
                command_key=value.command_id,
                attempt=run.attempt,
                generation=run.generation,
                sequence=value.sequence,
                world_version=run.version,
                payload_json=document,
                state="pending",
                deadline_at=pending.expires_at,
            )
            session.add(previous)
            await session.flush()
        elif previous.payload_json != document:
            raise LaneConflict("control is already registered differently")
        persist_receipts(previous, value, state)


async def persist_namespace(
    session: AsyncSession,
    run: EngineRun,
    row: EngineLaneSession,
    pending: PendingDispatch,
    state: Json,
    namespace: Json,
) -> str:
    if state.get("identity") != pending.identity.model_dump(mode="json"):
        raise LaneConflict("API operation changed its dispatch identity")
    await persist_checkpoints(session, pending, namespace)
    await persist_blobs(session, pending, state)
    await persist_candidate(session, pending, state)
    await persist_controls(session, run, pending, state)
    return encoded(
        {
            key: value
            for key, value in state.items()
            if key not in {"blobs", "controls", "deliveries", "acks", "candidate"}
        }
    )
