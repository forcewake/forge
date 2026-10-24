"""Readiness needs current native heads, durable review and fresh independent CI."""

import json

from forge.domain.states import State
from forge.domain.verdicts import CheckVerdict
from forge.engine.delivery_values import ClosingWork
from forge.engine.effects import Effects
from forge.engine.plan import result_json
from forge.engine.publish import stopped
from forge.engine.runner import StepContext, StepResult
from forge.port_values import Applied, CommentOutcome, ReadUnavailable, ReviewVerdict
from forge.ports import CIPort, Clock, RepoPort, ReviewPort
from forge.store.delivery import DeliveryStore
from forge.store.lane_work import LaneWorkStore
from forge.verification.checks import verdict
from pydantic import TypeAdapter


class ReadyHandler:
    def __init__(
        self,
        lanes: LaneWorkStore,
        store: DeliveryStore,
        repo: RepoPort,
        reviews: ReviewPort,
        ci: CIPort,
        clock: Clock,
    ) -> None:
        self.lanes, self.store, self.repo, self.reviews = lanes, store, repo, reviews
        self.ci, self.clock = ci, clock

    async def __call__(self, context: StepContext[Effects]) -> StepResult:
        work = ClosingWork.model_validate_json(context.payload_json)
        approved = await self.lanes.approved(context.lease)
        evidence = await self.store.evidence(
            context.lease, work.candidate_sha, now=self.clock.now()
        )
        review = ReviewVerdict.model_validate_json(json.dumps(evidence.get("review")))
        previous = CheckVerdict.model_validate_json(json.dumps(evidence.get("verification")))
        if (
            review.candidate_sha != work.candidate_sha
            or review.decision != "approve"
            or not previous.verified_for(
                work.candidate_sha,
                approved.spec.verification.required_checks,
                now=self.clock.now(),
                max_age_seconds=work.settings.check_max_age_seconds,
            )
        ):
            return stopped("readiness_evidence_stale")
        effect = await context.effects.store.get(
            f"{context.lease.run_id}:{context.lease.attempt}:{context.lease.generation}:review"
        )
        if (
            effect is None
            or effect.state not in ("committed", "adopted")
            or effect.outcome_json is None
            or not isinstance(
                TypeAdapter(CommentOutcome).validate_json(effect.outcome_json), Applied
            )
        ):
            return stopped("readiness_review_unpublished")
        try:
            if (
                await self.repo.head(approved.repo, f"forge/{approved.root_run_id}")
                != work.candidate_sha
                or await self.reviews.review_head(work.review) != work.candidate_sha
            ):
                return stopped("readiness_stale:branch_drift")
            checks = await self.ci.checks_for(approved.repo, work.candidate_sha)
        except ReadUnavailable:
            return stopped("readiness_verification_unavailable")
        fresh = verdict(
            checks,
            work.candidate_sha,
            approved.spec.verification.required_checks,
            now=self.clock.now(),
            max_age_seconds=work.settings.check_max_age_seconds,
        )
        if not fresh.verified_for(
            work.candidate_sha,
            approved.spec.verification.required_checks,
            now=self.clock.now(),
            max_age_seconds=work.settings.check_max_age_seconds,
        ):
            return stopped("readiness_verification_" + fresh.status)
        if (
            await self.repo.head(approved.repo, f"forge/{approved.root_run_id}")
            != work.candidate_sha
            or await self.reviews.review_head(work.review) != work.candidate_sha
        ):
            return stopped("readiness_stale:branch_drift")
        await self.store.record(context.lease, work.candidate_sha, fresh, now=self.clock.now())
        return StepResult(
            result_json(
                candidate_sha=work.candidate_sha,
                verification=fresh.model_dump(mode="json"),
                review=review.model_dump(mode="json"),
            ),
            next_state=State.READY_FOR_HUMAN,
        )
