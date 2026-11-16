"""Spend the closing partition on one durable review and journal its publication."""

from collections.abc import Callable
import asyncio
import json

from forge.domain.approved_input import ApprovedInput
from forge.domain.budget import BudgetExhausted
from forge.domain.states import Handler
from forge.domain.verdicts import CheckVerdict
from forge.engine.delivery_values import ClosingWork
from forge.engine.effects import Effects
from forge.engine.plan import result_json
from forge.engine.publish import stopped
from forge.engine.runner import StepContext, StepResult
from forge.budget.service import BudgetService, alias_for
from forge.port_values import Applied, ReviewBody, ReviewVerdict
from forge.ports import BlobPort, Clock, RepoPort, Reviewer, ReviewPort
from forge.store.delivery import DeliveryStore
from forge.store.lane_work import LaneWorkStore
from forge.store.records import Lease, StepRequest, StoreConflict
from forge_protocol import RunIdentity

ReviewerFactory = Callable[[Callable[[], Lease], str], Reviewer]


class ReviewHandler:
    def __init__(
        self,
        lanes: LaneWorkStore,
        store: DeliveryStore,
        repo: RepoPort,
        reviews: ReviewPort,
        blobs: BlobPort,
        budget: BudgetService,
        reviewer: ReviewerFactory,
        clock: Clock,
    ) -> None:
        self.lanes, self.store, self.repo, self.reviews = lanes, store, repo, reviews
        self.blobs, self.budget, self.reviewer, self.clock = blobs, budget, reviewer, clock

    async def __call__(self, context: StepContext[Effects]) -> StepResult:
        work = ClosingWork.model_validate_json(context.payload_json)
        approved = await self.lanes.approved(context.lease)
        evidence = await self.store.evidence(
            context.lease, work.candidate_sha, now=self.clock.now()
        )
        verification = CheckVerdict.model_validate_json(json.dumps(evidence.get("verification")))
        if not verification.verified_for(
            work.candidate_sha,
            approved.spec.verification.required_checks,
            now=self.clock.now(),
            max_age_seconds=work.settings.check_max_age_seconds,
        ):
            return stopped("review_verification_stale")
        if self.clock.now() >= work.expires_at:
            return stopped("closing_deadline_expired")
        if (
            await self.repo.head(approved.repo, f"forge/{approved.root_run_id}")
            != work.candidate_sha
            or await self.reviews.review_head(work.review) != work.candidate_sha
        ):
            return stopped("review_stale:branch_drift")
        if "review" in evidence:
            result = ReviewVerdict.model_validate_json(json.dumps(evidence["review"]))
        else:
            try:
                grant = await self.budget.prepare(
                    context.lease,
                    approved.spec,
                    "closing",
                    approved.spec.closing_reserve,
                    work.settings.closing_minimum,
                    work.expires_at,
                )
            except BudgetExhausted:
                return stopped("closing_budget_exhausted")
            if not isinstance(grant, Applied):
                return stopped("closing_gateway_unavailable")
            candidate = await self.store.candidate(context.lease, now=self.clock.now())
            diff = await self.blobs.get(candidate.diff_sha256)
            if diff is None:
                raise StoreConflict("closing review diff is missing")
            identity = RunIdentity(
                forge_run_id=context.lease.run_id,
                attempt=context.lease.attempt,
                generation=context.lease.generation,
            )
            try:
                reviewer = self.reviewer(lambda: context.lease, alias_for(identity, "closing"))
                async with asyncio.timeout((work.expires_at - self.clock.now()).total_seconds()):
                    result = await reviewer.review(
                        diff,
                        ApprovedInput.from_spec(approved.spec),
                        candidate_sha=work.candidate_sha,
                        checks_passed=verification.successful,
                    )
            except Exception:
                await self.budget.finish(context.lease, "closing")
                return stopped("closing_review_unknown")
            if result.candidate_sha != work.candidate_sha:
                await self.budget.finish(context.lease, "closing")
                return stopped("closing_review_wrong_candidate")
            await self.store.record(context.lease, work.candidate_sha, result, now=self.clock.now())
        await self.budget.finish(context.lease, "closing")
        if (
            await self.repo.head(approved.repo, f"forge/{approved.root_run_id}")
            != work.candidate_sha
            or await self.reviews.review_head(work.review) != work.candidate_sha
        ):
            return stopped("review_stale:branch_drift")
        return await self.post(context, work, result)

    async def post(
        self, context: StepContext[Effects], work: ClosingWork, result: ReviewVerdict
    ) -> StepResult:
        posted = await context.effects.post_review(
            context.lease,
            f"{context.lease.run_id}:{context.lease.attempt}:{context.lease.generation}:review",
            work.review,
            ReviewBody(**result.model_dump(exclude={"evidence"})),
            f"<!-- forge:{context.lease.run_id}:review:{work.candidate_sha} -->",
        )
        if not isinstance(posted, Applied):
            return stopped("effect_unknown:post_review")
        if result.decision != "approve":
            return stopped("review_" + result.decision)
        successor = StepRequest(
            context.lease.run_id,
            Handler.READY,
            context.step.key + ":ready",
            work.canonical_bytes.decode(),
            self.clock.now(),
        )
        return StepResult(
            result_json(review=result.model_dump(mode="json")), successors=(successor,)
        )
