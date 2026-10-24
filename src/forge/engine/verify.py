"""Candidate-bound independent CI, bounded repairs and exact native-head checks."""

from datetime import timedelta

from forge_protocol import RepairContext

from forge.domain.budget import Resources
from forge.domain.states import Handler, State
from forge.engine.delivery_values import ClosingWork, DeliveryWork
from forge.engine.effects import Effects
from forge.engine.lane_values import DispatchInput
from forge.engine.plan import result_json
from forge.engine.publish import stopped
from forge.engine.runner import Retryable, StepContext, StepResult
from forge.port_values import ReadUnavailable
from forge.ports import CIPort, Clock, RepoPort, ReviewPort
from forge.store.budgets import BudgetStore
from forge.store.delivery import DeliveryStore
from forge.store.lane_work import LaneWorkStore
from forge.store.records import RestartLane, StepRequest, StoreConflict
from forge.verification.checks import code_failure, verdict


class VerifyHandler:
    def __init__(
        self,
        lanes: LaneWorkStore,
        store: DeliveryStore,
        repo: RepoPort,
        reviews: ReviewPort,
        ci: CIPort,
        budgets: BudgetStore,
        clock: Clock,
    ) -> None:
        self.lanes, self.store, self.repo, self.reviews = lanes, store, repo, reviews
        self.ci, self.budgets, self.clock = ci, budgets, clock

    async def __call__(self, context: StepContext[Effects]) -> StepResult:
        work = DeliveryWork.model_validate_json(context.payload_json)
        approved = await self.lanes.approved(context.lease)
        await self.store.evidence(context.lease, work.candidate_sha, now=self.clock.now())
        if work.review.repo.identity != approved.repo.identity:
            raise StoreConflict("verification draft differs from approved repository")
        age = (self.clock.now() - work.published_at).total_seconds()
        if age >= work.settings.verification_seconds:
            return stopped("verification_timeout")
        try:
            if (
                await self.repo.head(approved.repo, f"forge/{approved.root_run_id}")
                != work.candidate_sha
                or await self.reviews.review_head(work.review) != work.candidate_sha
            ):
                return stopped("verification_stale:branch_drift")
            checks = await self.ci.checks_for(approved.repo, work.candidate_sha)
        except ReadUnavailable:
            if (
                self.clock.now() - work.published_at
            ).total_seconds() >= work.settings.verification_seconds:
                return stopped("verification_unavailable_timeout")
            raise Retryable("verification_unavailable") from None
        age = (self.clock.now() - work.published_at).total_seconds()
        if age >= work.settings.verification_seconds:
            return stopped("verification_timeout")
        result = verdict(
            checks,
            work.candidate_sha,
            approved.spec.verification.required_checks,
            now=self.clock.now(),
            max_age_seconds=work.settings.check_max_age_seconds,
            missing_is_final=age >= work.settings.missing_checks_grace_seconds,
        )
        await self.store.record(context.lease, work.candidate_sha, result, now=self.clock.now())
        if result.status == "passed":
            closing = ClosingWork(
                **work.model_dump(),
                expires_at=self.clock.now() + timedelta(seconds=work.settings.closing_seconds),
            )
            successor = StepRequest(
                context.lease.run_id,
                Handler.REVIEW,
                context.step.key + ":review",
                closing.canonical_bytes.decode(),
                self.clock.now(),
            )
            return StepResult(
                result_json(verification=result.model_dump(mode="json")),
                successors=(successor,),
                next_state=State.REVIEWING,
            )
        if result.status == "failed":
            if not code_failure(checks, result):
                return stopped("verification_non_code_failure")
            return await self.repair(
                context,
                work,
                approved.brief.approved_digest,
                approved.brief.base_sha,
                approved.brief.revision,
                result.evidence,
            )
        if result.status in ("stale", "unknown", "not_configured"):
            return stopped("verification_" + result.status)
        return StepResult(
            result_json(waiting="independent_checks"),
            defer_until=self.clock.now() + timedelta(seconds=work.settings.poll_seconds),
        )

    async def repair(
        self,
        context: StepContext[Effects],
        work: DeliveryWork,
        approved_digest: str,
        original_base: str,
        revision: int,
        errors: str,
    ) -> StepResult:
        if context.lease.attempt > work.settings.repair_limit:
            return stopped("repair_limit_exhausted")
        pool = await self.budgets.load(context.lease.run_id, "implementation")
        if pool.balance.shortfall != Resources() or not pool.balance.available.covers(
            work.settings.repair_minimum
        ):
            return stopped("budget_exhausted")
        next_attempt, next_generation = context.lease.attempt + 1, context.lease.generation + 1
        repair = RepairContext(
            forge_run_id=context.lease.run_id,
            attempt=next_attempt,
            generation=next_generation,
            approved_digest=approved_digest,
            original_base_sha=original_base,
            candidate_sha=work.candidate_sha,
            prior_attempt=context.lease.attempt,
            prior_generation=context.lease.generation,
            failure_context=errors[:32768],
        )
        payload = DispatchInput(approved_digest=approved_digest, revision=revision, repair=repair)
        successor = StepRequest(
            context.lease.run_id,
            Handler.DISPATCH,
            f"{context.lease.run_id}:{next_attempt}:{next_generation}:repair",
            payload.canonical_bytes.decode(),
            self.clock.now(),
        )
        return StepResult(
            result_json(repair=repair.model_dump(mode="json")),
            successors=(successor,),
            restart_lane=RestartLane("ci_code_failure"),
        )
