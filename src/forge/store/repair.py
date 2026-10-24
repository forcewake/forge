"""Validate execution context against this run's durable published candidate."""

import json

from forge_protocol import Brief, RepairContext, RunIdentity, digest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.verdicts import Committed
from forge.store.models_execution import EngineCandidate, EngineEffect, EngineStep
from forge.store.records import StoreConflict


async def repair_brief(session: AsyncSession, identity: RunIdentity, brief: Brief) -> Brief:
    rows = await session.scalars(
        select(EngineStep).where(
            EngineStep.run_id == identity.forge_run_id,
            EngineStep.attempt == identity.attempt,
            EngineStep.generation == identity.generation,
            EngineStep.handler == "dispatch",
        )
    )
    contexts = [
        RepairContext.model_validate_json(json.dumps(raw))
        for row in rows
        if (raw := json.loads(row.payload_json).get("repair")) is not None
    ]
    if not contexts:
        return brief
    repair = contexts[0]
    if any(item != repair for item in contexts):
        raise StoreConflict("dispatches disagree on frozen repair authority")
    value = Brief.model_validate({**brief.model_dump(), "repair": repair})
    candidate = await session.scalar(
        select(EngineCandidate).where(
            EngineCandidate.run_id == identity.forge_run_id,
            EngineCandidate.attempt == repair.prior_attempt,
            EngineCandidate.generation == repair.prior_generation,
            EngineCandidate.state == "published",
            EngineCandidate.published_sha == repair.candidate_sha,
            EngineCandidate.approved_digest == brief.approved_digest,
        )
    )
    key = f"{identity.forge_run_id}:{repair.prior_attempt}:{repair.prior_generation}:publish:commit"
    effect = await session.scalar(select(EngineEffect).where(EngineEffect.key == key))
    if (
        candidate is None
        or effect is None
        or effect.kind != "commit"
        or (
            effect.run_id != identity.forge_run_id
            or effect.state not in ("committed", "adopted")
            or effect.outcome_json is None
            or digest(json.loads(effect.payload_json)) != effect.payload_digest
        )
    ):
        raise StoreConflict("repair requires this run's published candidate and commit receipt")
    outcome = Committed.model_validate_json(effect.outcome_json)
    if outcome.sha != repair.candidate_sha:
        raise StoreConflict("repair base differs from the durable commit receipt")
    return value
