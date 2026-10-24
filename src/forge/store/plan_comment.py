"""The original posted plan reference is authority only for its exact frozen body."""

import json

from forge_protocol import digest
from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.port_values import Applied, CommentOutcome, CommentRef, SubjectRef
from forge.store.approval_values import PreparedPlan
from forge.store.models_execution import EngineEffect, EngineStep
from forge.store.models_runs import EngineRun


async def posted_plan(
    session: AsyncSession, run: EngineRun, prepared: PreparedPlan
) -> CommentRef | None:
    effect = await session.scalar(
        select(EngineEffect).where(EngineEffect.key == f"{run.id}:plan:comment")
    )
    if (
        effect is None
        or effect.run_id != run.id
        or effect.kind != "post_comment"
        or effect.state not in ("committed", "adopted")
        or effect.outcome_json is None
    ):
        return None
    step = await session.get(EngineStep, effect.step_id) if effect.step_id else None
    if (
        effect.command_id is not None
        or step is None
        or step.run_id != run.id
        or step.handler != "plan"
        or step.state != "succeeded"
        or step.key != f"{run.id}:{run.generation}:plan-execute"
    ):
        return None
    try:
        outcome: CommentOutcome = TypeAdapter(CommentOutcome).validate_json(effect.outcome_json)
        payload = json.loads(effect.payload_json)
        if not isinstance(outcome, Applied) or outcome.value.kind != "issue_comment":
            return None
        ref = outcome.value
        subject = SubjectRef.model_validate(payload.get("subject"))
        repo = subject.repo.identity
        if (
            not isinstance(ref.subject, SubjectRef)
            or (ref.subject.repo.identity, ref.subject.kind, ref.subject.number)
            != (repo, subject.kind, subject.number)
            or (
                repo.provider,
                repo.connection_id,
                repo.repository_id,
                subject.kind,
                str(subject.number),
            )
            != (
                run.provider,
                run.connection_id,
                run.repository_id,
                run.subject_kind,
                run.subject_id,
            )
            or digest(payload) != effect.payload_digest
            or payload.get("body") != prepared.comment
            or payload.get("marker") != prepared.marker
        ):
            return None
        return ref
    except (ValueError, TypeError, AttributeError):
        return None
