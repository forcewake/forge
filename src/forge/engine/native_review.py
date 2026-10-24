"""Bounded human-push review: native snapshots, one model call and one comment."""

import asyncio
from collections.abc import Callable
import hashlib
import json

from forge.domain.budget import BudgetExhausted
from forge.domain.states import State
from forge.engine.effects import Effects
from forge.engine.plan import result_json
from forge.engine.runner import Park, Retryable, StepContext, StepResult
from forge.port_values import Applied, ReadUnavailable, Refused, ReviewVerdict
from forge.ports import Clock, RepoPort, ReviewPort
from forge.budget.service import BudgetService, alias_for
from forge_protocol import RunIdentity
from forge.review.native_diff import NativeDiffRefused, native_diff
from forge.review.native_values import NativeReviewInput, ReadOnlyReviewer
from forge.store.native_reviews import (
    NativeReviewStore,
    NativeReviewWork,
    comment_body,
    comment_key,
    marker,
)
from forge.store.records import Lease
from forge.store.rounds import review_key

NativeReviewerFactory = Callable[[Callable[[], Lease], str], ReadOnlyReviewer]


def stopped(reason: str) -> StepResult:
    return StepResult(result_json(reason=reason), park=Park(reason))


class NativeReviewHandler:
    def __init__(
        self,
        store: NativeReviewStore,
        repositories: RepoPort,
        reviews: ReviewPort,
        budget: BudgetService,
        reviewer: NativeReviewerFactory,
        clock: Clock,
    ) -> None:
        self.store, self.repositories, self.reviews = store, repositories, reviews
        self.budget, self.reviewer, self.clock = budget, reviewer, clock

    async def current(self, work: NativeReviewWork) -> bool:
        branch = "forge/" + work.root_id
        found = await self.reviews.find_draft(work.review.repo, branch)
        return (
            found is not None
            and review_key(found) == review_key(work.review)
            and await self.reviews.review_head(work.review) == work.head_sha
            and await self.repositories.head(work.review.repo, branch) == work.head_sha
        )

    async def __call__(self, context: StepContext[Effects]) -> StepResult:
        work = await self.store.load(context.lease, now=self.clock.now())
        if json.loads(context.payload_json) != {"request_id": work.request_id}:
            return stopped("native_review_request_mismatch")
        if self.clock.now() >= work.expires_at:
            return stopped("native_review_deadline")
        try:
            if not await self.current(work):
                return stopped("native_review_head_drift")
            if work.result is None:
                result = await self.model(context, work)
                if isinstance(result, StepResult):
                    return result
                work = await self.store.load(context.lease, now=self.clock.now())
            await self.budget.finish(context.lease, "closing")
            if not await self.current(work):
                return stopped("native_review_head_drift")
            assert work.result is not None
            key = comment_key(context.lease.run_id)
            prior = await context.effects.store.get(key)
            if prior is not None and prior.state in ("unknown", "dispatched"):
                await context.effects.recover(prior)
            posted = await context.effects.post_comment(
                context.lease, key, work.review, comment_body(work, work.result), marker(work)
            )
            if not isinstance(posted, Applied):
                return stopped(
                    "native_review_comment_refused"
                    if isinstance(posted, Refused)
                    else "native_review_comment_unknown"
                )
            await self.store.commented(context.lease, now=self.clock.now())
            if not await self.current(work):
                return stopped("native_review_posted_before_head_drift")
            if not await self.store.advance(
                context.lease, observed_head=work.head_sha, now=self.clock.now()
            ):
                return stopped("native_review_cursor_conflict")
            return StepResult(
                result_json(reviewed_sha=work.head_sha), next_state=State.READY_FOR_HUMAN
            )
        except ReadUnavailable:
            if work.state == "accepted" or work.result is not None:
                raise Retryable("native_review_read_unavailable") from None
            return stopped("native_review_read_unavailable")

    async def model(
        self, context: StepContext[Effects], work: NativeReviewWork
    ) -> ReviewVerdict | StepResult:
        if work.state == "model_requested":
            await self.budget.finish(context.lease, "closing")
            return stopped("native_review_model_unknown")
        try:
            diff = await native_diff(
                self.repositories, work.review.repo, work.base_sha, work.head_sha, work.policy
            )
        except NativeDiffRefused as error:
            return stopped(str(error))
        if not diff:
            return stopped("native_review_no_changes")
        value = NativeReviewInput(
            request_id=work.request_id,
            review=work.review,
            base_sha=work.base_sha,
            head_sha=work.head_sha,
            actor=work.actor,
            diff_sha256=hashlib.sha256(diff).hexdigest(),
            policy_digest=work.policy.digest,
        )
        await self.store.prepare_input(context.lease, value, now=self.clock.now())
        try:
            grant = await self.budget.prepare(
                context.lease,
                work.policy.budget,
                "closing",
                work.policy.requested,
                work.policy.minimum,
                work.expires_at,
            )
        except BudgetExhausted:
            return stopped("native_review_budget_exhausted")
        if not isinstance(grant, Applied):
            return stopped("native_review_gateway_unavailable")
        if not await self.current(work):
            await self.budget.finish(context.lease, "closing")
            return stopped("native_review_head_drift")
        if not await self.store.start_model(context.lease, now=self.clock.now()):
            await self.budget.finish(context.lease, "closing")
            return stopped("native_review_model_unknown")
        identity = RunIdentity(
            forge_run_id=context.lease.run_id,
            attempt=context.lease.attempt,
            generation=context.lease.generation,
        )
        reviewer = self.reviewer(lambda: context.lease, alias_for(identity, "closing"))
        try:
            async with asyncio.timeout(
                max(0, (work.expires_at - self.clock.now()).total_seconds())
            ):
                result = await reviewer.review(diff, value)
        except Exception:
            await self.budget.finish(context.lease, "closing")
            return stopped("native_review_model_unknown")
        if result.candidate_sha != work.head_sha:
            await self.budget.finish(context.lease, "closing")
            return stopped("native_review_model_wrong_head")
        await self.store.record(context.lease, result, now=self.clock.now())
        return result


class NativeReviewReconciler:
    def __init__(
        self,
        store: NativeReviewStore,
        repositories: RepoPort,
        reviews: ReviewPort,
        effects: Effects,
        clock: Clock,
    ) -> None:
        self.store, self.repositories, self.reviews = store, repositories, reviews
        self.effects, self.clock = effects, clock

    async def tick(self, *, limit: int = 20) -> int:
        count = 0
        for request_id in await self.store.recoverable(limit):
            work = await self.store.historical(request_id)
            effect = await self.effects.store.get(comment_key(request_id))
            if effect is None:
                continue
            if effect.state in ("unknown", "dispatched"):
                effect = await self.effects.recover(effect)
            if effect.state not in ("committed", "adopted"):
                continue
            try:
                branch = "forge/" + work.root_id
                found = await self.reviews.find_draft(work.review.repo, branch)
                if (
                    found is None
                    or review_key(found) != review_key(work.review)
                    or await self.reviews.review_head(work.review) != work.head_sha
                    or await self.repositories.head(work.review.repo, branch) != work.head_sha
                ):
                    continue
            except ReadUnavailable:
                continue
            count += await self.store.adopt_comment(request_id, observed_head=work.head_sha)
        return count
