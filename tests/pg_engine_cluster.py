"""Independent worker pools share only PostgreSQL and authoritative provider fakes."""

import copy

import httpx
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forge.api.lane_auth import LaneAuthService
from forge.budget.credentials import CredentialVault
from forge.budget.gateway import LiteLLMGateway
from forge.budget.service import BudgetService
from forge.budget.settlement import Settlement
from forge.engine.effects import Effects
from forge.engine.runner import StepRunner
from forge.store.approval import ApprovalStore
from forge.store.budgets import BudgetStore
from forge.store.delivery import DeliveryStore
from forge.store.effects import EffectStore
from forge.store.gateway_keys import GatewayKeyStore
from forge.store.lane_auth import EngineLaneAuthStore
from forge.store.lane_work import LaneWorkStore
from forge.store.runs import RunStore
from forge.store.steps import StepStore


class WorkerDeath(BaseException):
    pass


class DeathAfterCompletion(StepStore):
    def __init__(self, factory, handler):
        super().__init__(factory)
        self.handler = handler

    async def complete_and_enqueue(self, lease, *args, **kwargs):
        result = await super().complete_and_enqueue(lease, *args, **kwargs)
        if lease.handler == self.handler:
            raise WorkerDeath("worker died after its fenced completion committed")
        return result


class Worker:
    def __init__(self, engine, factory, runner, gateway):
        self.engine, self.factory, self.runner, self.gateway = engine, factory, runner, gateway

    async def close(self):
        await self.gateway.aclose()
        await self.engine.dispose()


class Cluster:
    def __init__(self, case, settings):
        self.case, self.settings, self.workers = case, settings, []

    def worker(self, name, *, after_completion=None, hook=None):
        env = self.case["env"]
        engine = create_async_engine(env.factory.kw["bind"].url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        native = self.case["native"]
        keys = GatewayKeyStore(factory, CredentialVault(b"synthetic-server-secret-32-bytes!!"))
        gateway = LiteLLMGateway(
            "https://gateway.invalid",
            SecretStr("synthetic-admin"),
            keys,
            env.clock,
            transport=httpx.MockTransport(native),
            strict_policy=native.policy,
            strict_artifact_digest=native.artifact,
        )
        effects = Effects(
            EffectStore(factory),
            env.fake,
            env.fake,
            env.fake,
            env.fake,
            env.fake,
            gateway,
            env.clock,
            hook=hook,
        )
        budgets = BudgetStore(factory)
        budget = BudgetService(
            budgets,
            keys,
            effects,
            Settlement(gateway, budgets, window=0),
            env.clock,
            self.case["admission"],
        )
        auth = LaneAuthService(EngineLaneAuthStore(factory), self.settings)
        constructors = (
            RunStore,
            ApprovalStore,
            StepStore,
            LaneWorkStore,
            DeliveryStore,
            BudgetStore,
        )
        handlers = {}
        for kind, original in env.runner.handlers.items():
            handler = copy.copy(original)
            for attribute, owner in vars(original).items():
                if isinstance(owner, constructors):
                    setattr(handler, attribute, type(owner)(factory))
                elif isinstance(owner, BudgetService):
                    setattr(handler, attribute, budget)
                elif isinstance(owner, LaneAuthService):
                    setattr(handler, attribute, auth)
            handlers[kind] = handler
        steps = (
            StepStore(factory)
            if after_completion is None
            else DeathAfterCompletion(factory, after_completion)
        )
        runner = StepRunner(steps, handlers, effects=effects, clock=env.clock, owner=name)
        worker = Worker(engine, factory, runner, gateway)
        self.workers.append(worker)
        return worker

    def use(self, worker):
        self.case["env"].runner = worker.runner

    async def close(self):
        for worker in self.workers:
            await worker.close()
