"""Bind read-only planners and reviewers to one current step and durable grant."""

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
import os

from forge.budget.server_models import ServerModels
from forge.budget.service import BudgetService, alias_for
from forge.domain.approved_input import ApprovedInput
from forge.domain.budget import BudgetExhausted, Resources
from forge.domain.plan import Plan
from forge.domain.run_spec import RunPolicy
from forge.domain.verdicts import Sha
from forge.engine.plan_values import PlanIntent
from forge.planning.limits import PlanningLimits, PlanningRefused
from forge.planning.planner import ResearchPlanner
from forge.port_values import Applied, PlanningSnapshot, ReviewVerdict, TaskSnapshot
from forge.ports import Clock, Planner, RepoPort, Reviewer
from forge.review.reviewer import ReviewLimits, TextReviewer
from forge.store.model_inputs import ModelInputStore
from forge.store.records import Lease, StoreConflict
from forge_protocol import RunIdentity

LeaseSource = Callable[[], Lease]


def planning_model_calls(allowance_calls: int, configured: int) -> int:
    """The planning allowance's call ceiling replaces the hardcoded planner default."""
    if (
        isinstance(allowance_calls, bool)
        or not isinstance(allowance_calls, int)
        or allowance_calls <= 0
    ):
        return configured
    return min(allowance_calls, 32)


def planner_reasks(configured: int) -> int:
    raw = os.environ.get("FORGE_PLANNER_REASKS")
    if raw is not None and raw.isdecimal() and 1 <= int(raw) <= 8:
        return int(raw)
    return configured


class ServerPlanners:
    def __init__(
        self,
        reader: RepoPort,
        models: ServerModels,
        budget: BudgetService,
        inputs: ModelInputStore,
        clock: Clock,
        *,
        limits: PlanningLimits = PlanningLimits(),
    ) -> None:
        self.reader, self.models, self.budget = reader, models, budget
        self.inputs, self.clock, self.limits = inputs, clock, limits

    def __call__(self, current_lease: LeaseSource, policy: RunPolicy) -> Planner:
        return _PlanningStep(self, current_lease, policy)


def planning_window(
    created_at: datetime, deadline_at: datetime | None, seconds: int, now: datetime
) -> datetime:
    """Admission window from creation, unless retry stored a later deadline."""
    expires = created_at + timedelta(seconds=seconds)
    if deadline_at is not None and deadline_at > now and expires <= now:
        return min(deadline_at, now + timedelta(seconds=seconds))
    if deadline_at is not None:
        return min(expires, deadline_at)
    return expires


class _PlanningStep:
    def __init__(self, factory: ServerPlanners, current: LeaseSource, policy: RunPolicy):
        self.factory, self.current, self.policy = factory, current, policy

    async def plan(self, task: TaskSnapshot, snapshot: PlanningSnapshot) -> Plan:
        service, lease = self.factory, self.current()
        persisted = await service.inputs.planning(lease, now=service.clock.now())
        intent = PlanIntent.model_validate_json(persisted.payload_json)
        if (intent.policy, intent.task, intent.snapshot) != (self.policy, task, snapshot):
            raise StoreConflict("planner input differs from the immutable admission")
        seconds = min(service.limits.wallclock_seconds, self.policy.limits.wallclock_seconds)
        now = service.clock.now()
        expires = planning_window(persisted.created_at, persisted.deadline_at, seconds, now)
        if expires <= now:
            raise PlanningRefused("planning_deadline_expired")
        minimum = service.models.minimum(self.policy.model)
        try:
            prepared = await service.budget.prepare(
                self.current(),
                self.policy,
                "planning",
                self.policy.planning_allowance,
                Resources(usd=minimum.usd, calls=2, tokens=1),
                expires,
            )
        except BudgetExhausted:
            raise PlanningRefused("planning_budget_exhausted") from None
        if not isinstance(prepared, Applied):
            raise PlanningRefused("planning_grant_unavailable")
        identity = RunIdentity(
            forge_run_id=lease.run_id, attempt=lease.attempt, generation=lease.generation
        )
        alias = alias_for(identity, "planning")
        model = service.models.client(
            self.current, alias=alias, model=self.policy.model, purpose="planning"
        )
        planner = ResearchPlanner(
            service.reader,
            model,
            model=self.policy.model,
            credential_alias=alias,
            limits=service.limits.model_copy(
                update={
                    "model_calls": planning_model_calls(
                        self.policy.planning_allowance.calls, service.limits.model_calls
                    ),
                    "reasks": planner_reasks(service.limits.reasks),
                }
            ),
        )
        try:
            remaining = (expires - service.clock.now()).total_seconds()
            if remaining <= 0:
                raise PlanningRefused("planning_deadline_expired")
            async with asyncio.timeout(remaining):
                result = await planner.plan(task, snapshot)
        except BaseException:
            try:
                await service.budget.finish(self.current(), "planning")
            except Exception:
                # The original failure remains authoritative; durable funds stay held.
                pass
            raise
        await service.budget.finish(self.current(), "planning")
        return result


class ServerReviewers:
    def __init__(
        self,
        models: ServerModels,
        inputs: ModelInputStore,
        clock: Clock,
        *,
        limits: ReviewLimits = ReviewLimits(),
    ) -> None:
        self.models, self.inputs, self.clock, self.limits = models, inputs, clock, limits

    def __call__(self, current_lease: LeaseSource, alias: str) -> Reviewer:
        return _ReviewStep(self.models, self.inputs, self.clock, current_lease, alias, self.limits)


class _ReviewStep:
    def __init__(
        self,
        models: ServerModels,
        inputs: ModelInputStore,
        clock: Clock,
        current: LeaseSource,
        alias: str,
        limits: ReviewLimits,
    ) -> None:
        self.models, self.inputs, self.clock = models, inputs, clock
        self.current, self.alias, self.limits = current, alias, limits

    async def review(
        self,
        candidate_diff: bytes,
        approved_input: ApprovedInput,
        *,
        candidate_sha: Sha,
        checks_passed: tuple[str, ...] = (),
        repair_context: str = "",
    ) -> ReviewVerdict:
        spec = await self.inputs.review(
            self.current(), approved_input, candidate_diff, candidate_sha, now=self.clock.now()
        )
        model = self.models.client(
            self.current, alias=self.alias, model=spec.model, purpose="closing"
        )
        return await TextReviewer(
            spec, model, credential_alias=self.alias, limits=self.limits
        ).review(
            candidate_diff,
            approved_input,
            candidate_sha=candidate_sha,
            checks_passed=checks_passed,
            repair_context=repair_context,
        )
