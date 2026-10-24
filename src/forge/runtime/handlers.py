"""Build step collaborators from the immutable run and exact native repository."""

from collections.abc import Mapping
from pydantic import HttpUrl

from forge.api.lane_auth import LaneAuthService
from forge.runtime.blobs import Blobs
from forge.budget.service import BudgetService
from forge.budget.server_models import ServerModels
from forge.domain.budget import Resources, partition
from forge.domain.states import Handler
from forge.engine.approve import ApproveHandler
from forge.engine.await_lane import AwaitLaneHandler
from forge.engine.dispatch import DispatchHandler
from forge.engine.delivery_values import DeliverySettings
from forge.engine.lane_values import LaneSettings
from forge.engine.model_services import ServerPlanners, ServerReviewers
from forge.engine.plan import PlanHandler
from forge.engine.publish import PublishHandler
from forge.engine.ready import ReadyHandler
from forge.engine.review import ReviewHandler
from forge.engine.verify import VerifyHandler
from forge.engine.effects import Effects
from forge.engine.runner import StepContext, StepHandler, StepResult
from forge.ports import Clock
from forge.runtime.routing import Registry
from forge.runtime.settings import RuntimeSettings
from forge.store.approval import ApprovalStore
from forge.store.budgets import BudgetStore
from forge.store.command_runs import CommandRuns
from forge.store.delivery import DeliveryStore
from forge.store.lane_work import LaneWorkStore
from forge.store.runs import RunStore
from forge.store.steps import StepStore
from forge.store.transactions import SessionFactory


class Handlers:
    def __init__(
        self,
        config: RuntimeSettings,
        factory: SessionFactory,
        ports: Registry,
        auth: LaneAuthService,
        budget: BudgetService,
        models: ServerModels,
        planners: ServerPlanners,
        reviewers: ServerReviewers,
        blobs: Blobs,
        clock: Clock,
    ) -> None:
        self.config, self.factory, self.ports, self.auth, self.budget = (
            config,
            factory,
            ports,
            auth,
            budget,
        )
        self.models, self.planners, self.reviewers, self.blobs, self.clock = (
            models,
            planners,
            reviewers,
            blobs,
            clock,
        )
        self.lanes, self.delivery = LaneWorkStore(factory), DeliveryStore(factory)
        self.runs, self.steps, self.approvals = (
            RunStore(factory),
            StepStore(factory),
            ApprovalStore(factory),
        )

    @property
    def mapping(self) -> Mapping[Handler, StepHandler[Effects]]:
        return {
            Handler.PLAN: self.plan,
            Handler.APPROVE: self.approve,
            Handler.DISPATCH: self.dispatch,
            Handler.AWAIT_LANE: AwaitLaneHandler(self.lanes, self.budget, self.ports, self.clock),
            Handler.PUBLISH: self.publish,
            Handler.VERIFY: VerifyHandler(
                self.lanes,
                self.delivery,
                self.ports,
                self.ports,
                self.ports,
                BudgetStore(self.factory),
                self.clock,
            ),
            Handler.REVIEW: ReviewHandler(
                self.lanes,
                self.delivery,
                self.ports,
                self.ports,
                self.blobs,
                self.budget,
                self.reviewers,
                self.clock,
            ),
            Handler.READY: ReadyHandler(
                self.lanes, self.delivery, self.ports, self.ports, self.ports, self.clock
            ),
        }

    async def plan(self, context: StepContext[Effects]) -> StepResult:
        subject = await CommandRuns(self.factory).subject(context.lease.run_id)
        return await PlanHandler(
            self.runs, self.approvals, self.planners, self.ports.actor_for(subject.repo), self.clock
        )(context)

    async def approve(self, context: StepContext[Effects]) -> StepResult:
        subject = await CommandRuns(self.factory).subject(context.lease.run_id)
        return await ApproveHandler(
            self.runs,
            self.steps,
            self.approvals,
            self.ports.actor_for(subject.repo),
            self.ports,
            self.clock,
        )(context)

    async def dispatch(self, context: StepContext[Effects]) -> StepResult:
        approved = await self.lanes.approved(context.lease)
        _, repository = next(
            (c, r)
            for c in self.config.connections
            for r in c.repositories
            if c.ref(r).identity == approved.repo.identity
        )
        available = partition(approved.spec.limits, approved.spec.closing_reserve).implementation
        minimum = self.models.minimum(approved.spec.model)
        settings = LaneSettings(
            server_url=HttpUrl(self.config.public_url),
            requested=available,
            minimum=minimum,
            github_workflow_path=repository.lane_workflow,
        )
        return await DispatchHandler(
            self.lanes, self.auth, self.budget, self.clock, settings, self.ports
        )(context)

    async def publish(self, context: StepContext[Effects]) -> StepResult:
        approved = await self.lanes.approved(context.lease)
        minimum = self.models.minimum(approved.spec.model)
        settings = DeliverySettings(
            repair_minimum=minimum, closing_minimum=Resources(usd=minimum.usd, calls=1, tokens=1)
        )
        return await PublishHandler(
            self.lanes, self.delivery, self.ports, self.blobs, self.clock, settings
        )(context)
