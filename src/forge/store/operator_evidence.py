"""Allowlisted SQL evidence; observed rows never imply unobserved native outcomes."""

from collections.abc import Sequence
from datetime import datetime
import json
from typing import TypeVar

from forge_protocol import ControlAck, ControlCommand, LaneReport
from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from forge.command_values import CommandResult
from forge.domain.verdicts import CheckVerdict
from forge.port_values import ReviewVerdict
from forge.store.models_commands import EngineCommand
from forge.store.models_execution import EngineCandidate, EngineStep
from forge.store.models_lane import (
    EngineCheckpoint,
    EngineControlCommand,
    EngineLaneSession,
    EngineReviewRound,
)
from forge.store.models_runs import EngineApprovedInput, EngineGate, EngineRun, EngineRunSpec
from forge.store.models_review import EngineReactiveReview
from forge.store.records import RunRecord
from forge.store.transactions import stored_time

T = TypeVar("T")


def timestamp(value: datetime | None) -> str | None:
    return stored_time(value).isoformat() if value is not None else None


async def rows(
    session: AsyncSession, query: Select[tuple[T]], limit: int = 50
) -> tuple[Sequence[T], bool]:
    values = (await session.scalars(query.limit(limit + 1))).all()
    return values[:limit], len(values) <= limit


def section(values: list[dict[str, object]], complete: bool) -> dict[str, object]:
    return {"latest": values, "complete": complete}


async def steps(session: AsyncSession, run: RunRecord) -> dict[str, object]:
    values, complete = await rows(
        session,
        select(EngineStep)
        .where(EngineStep.run_id == run.id)
        .order_by(EngineStep.created_at.desc(), EngineStep.id),
    )
    return section(
        [
            {
                "id": row.id,
                "handler": row.handler,
                "state": row.state,
                "attempt": row.attempt,
                "generation": row.generation,
                "lease_epoch": row.lease_epoch,
                "lease_expires_at": timestamp(row.lease_expires_at),
                "attempts": row.attempts,
                "max_attempts": row.max_attempts,
                "updated_at": timestamp(row.updated_at),
                "deadline_at": timestamp(row.deadline_at),
            }
            for row in values
        ],
        complete,
    )


async def commands(session: AsyncSession, run: RunRecord) -> dict[str, object]:
    values, complete = await rows(
        session,
        select(EngineCommand)
        .where(EngineCommand.run_id == run.id)
        .order_by(EngineCommand.created_at.desc(), EngineCommand.id),
    )
    result: list[dict[str, object]] = []
    for row in values:
        decision = CommandResult.model_validate_json(row.result_json) if row.result_json else None
        result.append(
            {
                "id": row.id,
                "kind": row.kind,
                "state": row.state,
                "status": decision.status if decision else None,
                "reason": decision.reason if decision else None,
            }
        )
    return section(result, complete)


async def controls(session: AsyncSession, run: RunRecord) -> dict[str, object]:
    values, complete = await rows(
        session,
        select(EngineControlCommand)
        .where(EngineControlCommand.run_id == run.id)
        .order_by(EngineControlCommand.attempt.desc(), EngineControlCommand.sequence.desc()),
    )
    result = []
    for row in values:
        wire = ControlCommand.model_validate_json(row.payload_json)
        ack = (
            ControlAck.model_validate_json(row.acknowledgement_json)
            if row.acknowledgement_json
            else None
        )
        result.append(
            {
                "id": row.id,
                "command_id": row.command_key,
                "action": wire.action,
                "state": row.state,
                "attempt": row.attempt,
                "generation": row.generation,
                "sequence": row.sequence,
                "world_version": row.world_version,
                "delivery_intent": "recorded" if row.delivery_json is not None else "absent",
                "delivery_outcome": ack.status
                if ack
                else ("unknown" if row.delivery_json is not None else "unobserved"),
                "acknowledged_status": ack.status if ack else None,
                "checkpoint_id": ack.checkpoint_id if ack else None,
            }
        )
    return section(result, complete)


async def checkpoints(session: AsyncSession, run: RunRecord) -> dict[str, object]:
    values, complete = await rows(
        session,
        select(EngineCheckpoint)
        .where(EngineCheckpoint.run_id == run.id)
        .order_by(EngineCheckpoint.created_at.desc(), EngineCheckpoint.id),
        20,
    )
    return section(
        [
            {
                "id": row.id,
                "digest": row.digest,
                "base_sha": row.base_sha,
                "approved_digest": row.approved_digest,
                "attempt": row.attempt,
                "generation": row.generation,
                "driver": row.driver,
                "retained": row.retained,
                "blob_availability": "unobserved" if row.retained else "not_retained",
            }
            for row in values
        ],
        complete,
    )


async def rounds(session: AsyncSession, run: RunRecord) -> dict[str, object]:
    parent, child = aliased(EngineRun), aliased(EngineRun)
    query = (
        select(EngineReviewRound)
        .join(parent, parent.id == EngineReviewRound.parent_run_id)
        .join(child, child.id == EngineReviewRound.child_run_id)
        .where(
            or_(
                EngineReviewRound.root_run_id == run.id,
                EngineReviewRound.parent_run_id == run.id,
                EngineReviewRound.child_run_id == run.id,
            ),
            parent.provider == run.provider,
            child.provider == run.provider,
            parent.connection_id == run.connection_id,
            child.connection_id == run.connection_id,
            parent.repository_id == run.repository_id,
            child.repository_id == run.repository_id,
        )
        .order_by(EngineReviewRound.created_at.desc(), EngineReviewRound.id)
    )
    values, complete = await rows(session, query)
    return section(
        [
            {
                "id": row.id,
                "root_run_id": row.root_run_id,
                "parent_run_id": row.parent_run_id,
                "child_run_id": row.child_run_id,
                "state": row.state,
                "base_sha": row.base_sha,
            }
            for row in values
        ],
        complete,
    )


def verdicts(row: EngineCandidate) -> dict[str, object]:
    data = json.loads(row.validation_json or "{}")
    verification: dict[str, object] | None = None
    review: dict[str, object] | None = None
    if "verification" in data:
        value = CheckVerdict.model_validate_json(json.dumps(data["verification"]))
        verification = {
            "status": value.status,
            "tested_sha": value.tested_sha,
            "bound_to_candidate": value.tested_sha == row.published_sha,
            "observed_at": timestamp(value.observed_at),
        }
    if "review" in data:
        result = ReviewVerdict.model_validate_json(json.dumps(data["review"]))
        review = {
            "decision": result.decision,
            "candidate_sha": result.candidate_sha,
            "bound_to_candidate": result.candidate_sha == row.published_sha,
        }
    return {"verification": verification, "review": review}


async def candidates(session: AsyncSession, run: RunRecord) -> dict[str, object]:
    values, complete = await rows(
        session,
        select(EngineCandidate)
        .where(EngineCandidate.run_id == run.id)
        .order_by(
            EngineCandidate.attempt.desc(), EngineCandidate.generation.desc(), EngineCandidate.id
        ),
        20,
    )
    return section(
        [
            {
                "id": row.id,
                "digest": row.digest,
                "base_sha": row.base_sha,
                "approved_digest": row.approved_digest,
                "state": row.state,
                "sha": row.published_sha,
                "attempt": row.attempt,
                "generation": row.generation,
                "current": (row.attempt, row.generation) == (run.attempt, run.generation),
                **verdicts(row),
            }
            for row in values
        ],
        complete,
    )


async def identity(session: AsyncSession, run: RunRecord) -> dict[str, object]:
    spec = await session.scalar(select(EngineRunSpec).where(EngineRunSpec.run_id == run.id))
    approved = await session.scalar(
        select(EngineApprovedInput)
        .where(EngineApprovedInput.run_id == run.id)
        .order_by(EngineApprovedInput.revision.desc())
        .limit(1)
    )
    gates, complete = await rows(
        session,
        select(EngineGate)
        .where(EngineGate.run_id == run.id)
        .order_by(EngineGate.created_at.desc(), EngineGate.id),
        20,
    )
    return {
        "spec_digest": spec.digest if spec else None,
        "approved_digest": approved.digest if approved else None,
        "base_sha": approved.base_sha if approved else None,
        "approved_revision": approved.revision if approved else None,
        "gates": section(
            [
                {
                    "id": row.id,
                    "state": row.state,
                    "approved_digest": row.approved_digest,
                    "expires_at": timestamp(row.expires_at),
                    "consumed_at": timestamp(row.consumed_at),
                }
                for row in gates
            ],
            complete,
        ),
    }


async def lane(session: AsyncSession, run: RunRecord) -> dict[str, object] | None:
    row = await session.scalar(
        select(EngineLaneSession).where(
            EngineLaneSession.run_id == run.id,
            EngineLaneSession.attempt == run.attempt,
            EngineLaneSession.generation == run.generation,
        )
    )
    if row is None:
        return None
    raw = json.loads(row.api_state_json)
    report = (
        LaneReport.model_validate_json(json.dumps(raw["report"])) if raw.get("report") else None
    )
    return {
        "attempt": row.attempt,
        "generation": row.generation,
        "expires_at": timestamp(row.expires_at),
        "report": {
            "status": report.status,
            "classification": report.classification,
            "checkpoint_id": report.checkpoint_id,
            "candidate_diff_sha256": report.candidate_diff_sha256,
        }
        if report
        else None,
    }


async def read_evidence(session: AsyncSession, run: RunRecord) -> dict[str, object]:
    history = await candidates(session, run)
    return {
        **(await native_review(session, run)),
        "identity": await identity(session, run),
        "steps": await steps(session, run),
        "commands": await commands(session, run),
        "controls": await controls(session, run),
        "checkpoints": await checkpoints(session, run),
        "rounds": await rounds(session, run),
        "candidates": history["latest"],
        "candidates_complete": history["complete"],
        "lane": await lane(session, run),
        "acceptance": {"state": "unobserved"},
    }


async def native_review(session: AsyncSession, run: RunRecord) -> dict[str, object]:
    row = await session.scalar(
        select(EngineReactiveReview).where(EngineReactiveReview.run_id == run.id)
    )
    if row is None:
        return {}
    result = ReviewVerdict.model_validate_json(row.result_json) if row.result_json else None
    return {
        "role": "read_only_review",
        "native_review": {
            "base_sha": row.base_sha,
            "head_sha": row.head_sha,
            "state": row.state,
            "decision": result.decision if result else None,
        },
    }
