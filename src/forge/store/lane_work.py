"""Read frozen approved lane inputs and current API receipts from engine rows."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json

from forge_protocol import Brief, CandidateBundle, Heartbeat, LaneReport, RunIdentity
from sqlalchemy import select

from forge.api.lane_auth_values import PendingDispatch
from forge.domain.run_spec import RunSpec2
from forge.port_values import RepoRef, TaskSnapshot
from forge.store.lane_auth import EngineLaneAuthStore, approved_brief
from forge.store.lane_v1 import EngineLaneV1Store
from forge.store.models_execution import EngineStep
from forge.store.models_lane import EngineLaneSession
from forge.store.records import Lease, StoreConflict
from forge.store.transactions import SessionFactory, fence, locked_run, transaction


@dataclass(frozen=True)
class ApprovedLane:
    repo: RepoRef
    brief: Brief
    spec: RunSpec2
    root_run_id: str


@dataclass(frozen=True)
class LaneReceipt:
    report: LaneReport | None
    candidate: CandidateBundle | None
    heartbeat: Heartbeat | None


class LaneWorkStore:
    def __init__(self, factory: SessionFactory) -> None:
        self.factory = factory
        self.auth = EngineLaneAuthStore(factory)
        self.api = EngineLaneV1Store(factory)

    async def approved(self, lease: Lease) -> ApprovedLane:
        async with transaction(self.factory) as session:
            await fence(session, lease, datetime.now(timezone.utc))
            run = await locked_run(session, lease.run_id)
            initial = await session.scalar(
                select(EngineStep).where(EngineStep.key == f"{run.id}:plan")
            )
            if initial is None:
                raise StoreConflict("frozen admission task is missing")
            task = TaskSnapshot.model_validate_json(
                json.dumps(json.loads(initial.payload_json)["task"])
            )
            if (
                task.subject.repo.identity.provider,
                task.subject.repo.identity.connection_id,
                task.subject.repo.identity.repository_id,
                task.subject.kind,
                str(task.subject.number),
            ) != (
                run.provider,
                run.connection_id,
                run.repository_id,
                run.subject_kind,
                run.subject_id,
            ):
                raise StoreConflict("frozen lane repository differs from its run")
            identity = RunIdentity(
                forge_run_id=run.id, attempt=run.attempt, generation=run.generation
            )
            brief, spec = await approved_brief(session, identity)
            return ApprovedLane(task.subject.repo, brief, spec, run.root_run_id)

    async def pending(self, identity: RunIdentity) -> PendingDispatch | None:
        async with self.factory() as session:
            found = await session.scalar(
                select(EngineLaneSession.id).where(
                    EngineLaneSession.run_id == identity.forge_run_id,
                    EngineLaneSession.attempt == identity.attempt,
                    EngineLaneSession.generation == identity.generation,
                )
            )
        return await self.auth.load(identity) if found is not None else None

    async def receipt(self, identity: RunIdentity) -> LaneReceipt:
        return await self.api.access(
            identity,
            lambda p, s, n: LaneReceipt(
                LaneReport.model_validate(s["report"]) if "report" in s else None,
                CandidateBundle.model_validate(s["candidate"]) if "candidate" in s else None,
                Heartbeat.model_validate(s["heartbeat"]) if "heartbeat" in s else None,
            ),
        )
