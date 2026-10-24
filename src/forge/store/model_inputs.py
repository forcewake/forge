"""Read the exact persisted planning or published review input under a live fence."""

from dataclasses import dataclass
from datetime import datetime
import hashlib

from sqlalchemy import select

from forge_protocol import CandidateBundle, digest

from forge.domain.approved_input import ApprovedInput
from forge.domain.run_spec import RunSpec2
from forge.domain.states import Handler
from forge.store.models_execution import EngineCandidate
from forge.store.models_runs import EngineApprovedInput, EngineRunSpec
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
            ) != ("published", candidate_sha, spec.base_sha, approved.approved_digest):
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
                    spec.base_sha,
                    approved.approved_digest,
                )
                or (wire.diff_sha256, wire.diff_size_bytes)
                != (hashlib.sha256(diff).hexdigest(), len(diff))
            ):
                raise StoreConflict("review diff differs from the immutable accepted candidate")
            return spec
