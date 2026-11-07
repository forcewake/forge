"""Read the exact persisted planning or published review input under a live fence."""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_protocol import Brief, CandidateBundle, LaneDispatch, RunIdentity, digest

from forge.domain.approved_input import ApprovedInput
from forge.domain.run_spec import RunSpec2
from forge.domain.states import Handler
from forge.port_values import RepoRef
from forge.store.models_execution import EngineCandidate, EngineEffect, EngineStep
from forge.store.models_lane import EngineLaneSession
from forge.store.models_runs import EngineApprovedInput, EngineRun, EngineRunSpec
from forge.store.repair import repair_brief
from forge.store.records import Lease, StoreConflict
from forge.store.transactions import SessionFactory, fence, stored_time, transaction


@dataclass(frozen=True)
class PlanningInput:
    payload_json: str
    created_at: datetime
    deadline_at: datetime | None


class ModelInputStore:
    def __init__(self, factory: SessionFactory) -> None:
        self.factory = factory

    async def planning(self, lease: Lease, *, now: datetime) -> PlanningInput:
        if lease.handler != Handler.PLAN:
            raise StoreConflict("planning requires the planning step")
        async with transaction(self.factory) as session:
            step = await fence(session, lease, now)
            return PlanningInput(
                step.payload_json,
                stored_time(step.created_at),
                stored_time(step.deadline_at) if step.deadline_at is not None else None,
            )

    async def review(
        self,
        lease: Lease,
        approved: ApprovedInput,
        diff: bytes,
        candidate_sha: str,
        *,
        now: datetime,
    ) -> RunSpec2:
        if lease.handler != Handler.REVIEW:
            raise StoreConflict("review requires the review step")
        async with transaction(self.factory) as session:
            await fence(session, lease, now)
            frozen = await session.scalar(
                select(EngineRunSpec).where(EngineRunSpec.run_id == lease.run_id)
            )
            if frozen is None:
                raise StoreConflict("review requires the frozen run specification")
            spec = RunSpec2.model_validate_json(frozen.document_json)
            if spec.spec_json != frozen.document_json or spec.digest != frozen.digest:
                raise StoreConflict("review specification is not canonical or digest-bound")
            if approved != ApprovedInput.from_spec(spec):
                raise StoreConflict("review approved input differs from the frozen specification")
            consumed = await session.scalar(
                select(EngineApprovedInput).where(
                    EngineApprovedInput.run_id == lease.run_id,
                    EngineApprovedInput.revision == approved.revision,
                )
            )
            if consumed is None or (
                consumed.digest,
                consumed.spec_digest,
                consumed.base_sha,
                consumed.document_json,
            ) != (
                approved.approved_digest,
                approved.spec_digest,
                approved.base_sha,
                approved.canonical_bytes.decode(),
            ):
                raise StoreConflict("review input has no matching consumed approval")
            base = await execution_base(session, lease, approved, spec)
            candidate = await session.scalar(
                select(EngineCandidate).where(
                    EngineCandidate.run_id == lease.run_id,
                    EngineCandidate.attempt == lease.attempt,
                    EngineCandidate.generation == lease.generation,
                )
            )
            if candidate is None or (
                candidate.state,
                candidate.published_sha,
                candidate.base_sha,
                candidate.approved_digest,
            ) != ("published", candidate_sha, base, approved.approved_digest):
                raise StoreConflict("review candidate is not the published approved candidate")
            wire = CandidateBundle.model_validate_json(candidate.manifest_json)
            if (
                candidate.digest != digest(wire)
                or (wire.driver, wire.model) != (spec.driver, spec.model)
                or wire.identity[:5]
                != (
                    lease.run_id,
                    lease.attempt,
                    lease.generation,
                    base,
                    approved.approved_digest,
                )
                or (wire.diff_sha256, wire.diff_size_bytes)
                != (hashlib.sha256(diff).hexdigest(), len(diff))
            ):
                raise StoreConflict("review diff differs from the immutable accepted candidate")
            return spec


async def execution_base(
    session: AsyncSession, lease: Lease, approved: ApprovedInput, spec: RunSpec2
) -> str:
    identity = RunIdentity(
        forge_run_id=lease.run_id, attempt=lease.attempt, generation=lease.generation
    )
    brief = await repair_brief(session, identity, approved.to_brief(identity, spec))
    row = await session.scalar(
        select(EngineLaneSession).where(
            EngineLaneSession.run_id == lease.run_id,
            EngineLaneSession.attempt == lease.attempt,
            EngineLaneSession.generation == lease.generation,
        )
    )
    run = await session.get(EngineRun, lease.run_id)
    if row is None or run is None or row.cancellation_generation != run.cancellation_generation:
        raise StoreConflict("review requires the current durable lane session")
    dispatch = LaneDispatch.model_validate_json(row.dispatch_json)
    if (
        Brief.model_validate_json(row.brief_json) != brief
        or (dispatch.forge_run_id, dispatch.attempt, dispatch.generation, dispatch.provider)
        != (lease.run_id, lease.attempt, lease.generation, run.provider)
        or (
            dispatch.base_sha,
            dispatch.approved_digest,
            dispatch.driver,
            dispatch.model,
            dispatch.repair,
        )
        != (spec.base_sha, approved.approved_digest, spec.driver, spec.model, brief.repair)
        or json.loads(row.provider_binding_json).get("repository_id") != run.repository_id
        or hashlib.sha256(("lane-oidc-nonce:" + (dispatch.nonce or "")).encode()).hexdigest()
        != row.nonce_hash
    ):
        raise StoreConflict("review lane session differs from its frozen execution authority")
    if brief.repair is not None:
        await repair_source(session, run, brief)
    return dispatch.execution_base_sha


async def repair_source(session: AsyncSession, run: EngineRun, brief: Brief) -> None:
    repair = brief.repair
    assert repair is not None
    effect = await session.scalar(
        select(EngineEffect).where(
            EngineEffect.key
            == f"{run.id}:{repair.prior_attempt}:{repair.prior_generation}:publish:commit"
        )
    )
    if effect is None:
        raise StoreConflict("review repair source receipt is missing")
    step = await session.get(EngineStep, effect.step_id)
    payload = json.loads(effect.payload_json)
    repo = RepoRef.model_validate_json(json.dumps(payload["repo"]))
    if (
        step is None
        or (step.run_id, step.attempt, step.generation, step.handler)
        != (run.id, repair.prior_attempt, repair.prior_generation, "publish")
        or (repo.identity.provider, repo.identity.connection_id, repo.identity.repository_id)
        != (run.provider, run.connection_id, run.repository_id)
        or payload["branch"] != "forge/" + run.root_run_id
    ):
        raise StoreConflict("review repair source belongs to another native execution")
