"""Actual current effect journal over complete provider-state publication."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import update

from forge.domain.candidate import (
    CandidateBundle,
    FullReplacement,
    ValidatedCandidate,
    validate_candidate_bundle,
)
from forge.domain.plan import RepositoryIdentity
from forge.domain.states import Handler
from forge.engine.effects import Effects
from forge.engine.reconciler import Reconciler
from forge.port_values import RepoRef
from forge.providers.fake import Blob, FakeProvider
from forge.store.effects import EffectStore
from forge.store.models_execution import EngineStep
from forge.store.records import Lease, RunRecord, RunRequest, StepRequest
from forge.store.recovery import RecoveryStore
from forge.store.runs import RunStore
from forge.store.steps import StepStore
from forge.store.transactions import SessionFactory
from tests.fakes.ports import FixedClock
from tests.native_store_fixture import native_store as native_store


class PowerLoss(BaseException):
    pass


@dataclass
class Journal:
    factory: SessionFactory
    run: RunRecord
    lease: Lease
    clock: FixedClock
    provider: FakeProvider
    repo: RepoRef
    candidate: ValidatedCandidate
    base: str
    key: str
    marker: str

    def engine(self, *, hook=None, provider=None):
        port = provider or self.provider
        return Effects(
            EffectStore(self.factory), port, port, port, port, port, port, self.clock, hook=hook
        )

    async def publish(self, *, engine=None, key=None, marker=None):
        return await (engine or self.engine()).commit(
            self.lease, key or self.key, self.repo, self.candidate, self.base, marker or self.marker
        )

    async def crash(self, point):
        def kill(actual, effect):
            if actual == point and effect.kind == "commit":
                raise PowerLoss(point)

        with pytest.raises(PowerLoss):
            await self.publish(engine=self.engine(hook=kill))
        return await EffectStore(self.factory).get(self.key)

    async def expire(self):
        self.clock.instant = self.lease.expires_at + timedelta(seconds=1)
        self.provider.now = self.clock.now()
        async with self.factory() as session, session.begin():
            await session.execute(
                update(EngineStep)
                .where(
                    EngineStep.id == self.lease.step_id,
                    EngineStep.lease_epoch == self.lease.epoch,
                )
                .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
            )

    async def reclaim(self):
        await self.expire()
        (self.lease,) = await StepStore(self.factory).claim_due("restarted", self.clock.now(), 60)
        return self.lease

    def reconciler(self):
        return Reconciler(
            EffectStore(self.factory),
            RecoveryStore(self.factory),
            probe=self.engine().recover,
            clock=self.clock,
        )

    async def row(self):
        return await EffectStore(self.factory).get(self.key)

    def writes(self):
        return sum(name == "commit" for name, _ in self.provider.calls)


@pytest.fixture(params=("github", "gitlab"))
async def journal(native_store, request):
    provider = FakeProvider(provider=request.param)
    repo = RepoRef(
        identity=RepositoryIdentity(
            provider=request.param, connection_id="test", repository_id="42"
        ),
        full_name="owner/project",
    )
    base = provider.seed_repository(repo, {"a.py": Blob(b"old\n")})
    run = await RunStore(native_store).admit(
        RunRequest(uuid4().hex, request.param, "test", "42", "issue", "1")
    )
    clock = FixedClock(datetime.now(timezone.utc))
    provider.now = clock.now()
    branch = "forge/" + run.id
    provider.repo(repo).branches[branch] = base
    candidate = validate_candidate_bundle(
        CandidateBundle(
            attempt_base_oid=base,
            driver_exit="completed",
            entries=(FullReplacement("a.py", "new\n"),),
        ),
        base_contents={"a.py": "old\n"},
        branch=branch,
        commit_message="Approved correction",
    )
    await StepStore(native_store).enqueue(
        StepRequest(run.id, Handler.PUBLISH, f"{run.id}:publication", "{}", clock.now())
    )
    (lease,) = await StepStore(native_store).claim_due("original", clock.now(), 60)
    return Journal(
        native_store,
        run,
        lease,
        clock,
        provider,
        repo,
        candidate,
        base,
        f"{run.id}:1:1:publication",
        f"<!-- forge:{run.id}:publication -->",
    )
