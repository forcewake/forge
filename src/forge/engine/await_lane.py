"""Consume current lane receipts; native CI status supplies only execution evidence."""

from datetime import timedelta

from forge_protocol import RunIdentity

from forge.budget.service import BudgetService
from forge.domain.revival import TRANSIENT, backoff_seconds, classify_terminal_failure
from forge.domain.states import Handler, State
from forge.engine.dispatch import lane_key
from forge.engine.effects import Effects
from forge.store.records import RestartLane
from forge.engine.lane_values import AwaitLane, DispatchInput
from forge.engine.plan import result_json
from forge.engine.runner import Park, StepContext, StepResult
from forge.port_values import ReadUnavailable
from forge.ports import CIPort, Clock
from forge.store.lane_work import LaneReceipt, LaneWorkStore
from forge.store.records import StepRequest, StoreConflict


class AwaitLaneHandler:
    def __init__(
        self, store: LaneWorkStore, budget: BudgetService, ci: CIPort, clock: Clock
    ) -> None:
        self.store, self.budget, self.ci, self.clock = store, budget, ci, clock

    async def __call__(self, context: StepContext[Effects]) -> StepResult:
        work = AwaitLane.model_validate_json(context.payload_json)
        approved = await self.store.approved(context.lease)
        if (work.approved_digest, work.revision, work.handle.repo) != (
            approved.brief.approved_digest,
            approved.brief.revision,
            approved.repo,
        ):
            raise StoreConflict("await input differs from consumed approval")
        identity = RunIdentity(
            forge_run_id=context.lease.run_id,
            attempt=context.lease.attempt,
            generation=context.lease.generation,
        )
        pending = await self.store.pending(identity)
        if pending is None or pending.dispatch.nonce != work.handle.correlation_id:
            raise StoreConflict("await handle is not the registered dispatch")
        receipt = await self.store.receipt(identity)
        if receipt.report is not None:
            report = receipt.report
            if report.status == "completed":
                return await self.completed(context, work, receipt, identity)
            if report.status == "failed" or report.status == "timed_out":
                reason = f"{report.classification}: {report.terminal_reason}"
                return await self.failed(context, work, reason, report.model_dump(mode="json"))
            return await self.park(
                context, work, f"lane_{report.status}", report.model_dump(mode="json")
            )
        try:
            native = await self.ci.lane_status(work.handle)
        except ReadUnavailable:
            native = None
        if native is not None:
            if native.handle != work.handle:
                raise StoreConflict("native lane observation is for another job")
            if native.status == "succeeded" and receipt.candidate is not None:
                return await self.completed(context, work, receipt, identity)
            if native.status in {"failed", "cancelled"}:
                return await self.failed(
                    context, work, f"native_lane_{native.status}", native.model_dump(mode="json")
                )
        if self.clock.now() >= work.expires_at:
            return await self.park(context, work, "lane_timeout_unquiesced", None)
        return StepResult(
            result_json(waiting="lane_receipt"),
            defer_until=min(
                work.expires_at, self.clock.now() + timedelta(seconds=work.settings.poll_seconds)
            ),
        )

    async def finish(self, context: StepContext[Effects], work: AwaitLane) -> str:
        approved = await self.store.approved(context.lease)
        if approved.spec.metering == "copilot-scoped-unmetered":
            return "unmetered"
        return await self.budget.finish(context.lease, "implementation")

    async def completed(
        self,
        context: StepContext[Effects],
        work: AwaitLane,
        receipt: LaneReceipt,
        identity: RunIdentity,
    ) -> StepResult:
        candidate = receipt.candidate
        if candidate is None or (
            receipt.report is not None
            and receipt.report.candidate_diff_sha256 != candidate.diff_sha256
        ):
            raise StoreConflict("completed lane requires its accepted candidate receipt")
        settlement = await self.finish(context, work)
        successor = StepRequest(
            context.lease.run_id,
            Handler.PUBLISH,
            lane_key(identity) + ":publish",
            result_json(candidate=candidate.model_dump(mode="json")),
            self.clock.now(),
        )
        return StepResult(
            result_json(candidate_diff_sha256=candidate.diff_sha256, settlement=settlement),
            successors=(successor,),
            next_state=State.PUBLISHING,
        )

    async def failed(
        self, context: StepContext[Effects], work: AwaitLane, reason: str, evidence: object
    ) -> StepResult:
        settlement = await self.finish(context, work)
        precise = "lane_failed:" + reason[:270]
        if (
            classify_terminal_failure(reason) != TRANSIENT
            or work.revivals >= work.settings.auto_revive_limit
        ):
            return StepResult(
                result_json(reason=reason, evidence=evidence, settlement=settlement),
                park=Park(precise),
            )
        next_input = DispatchInput(
            approved_digest=work.approved_digest,
            revision=work.revision,
            revivals=work.revivals + 1,
            repair=(
                work.repair.model_copy(
                    update={
                        "attempt": context.lease.attempt + 1,
                        "generation": context.lease.generation + 1,
                    }
                )
                if work.repair is not None
                else None
            ),
        )
        due = self.clock.now() + timedelta(
            seconds=backoff_seconds(work.revivals, work.settings.revive_backoff_seconds)
        )
        successor = StepRequest(
            context.lease.run_id,
            Handler.DISPATCH,
            f"{context.lease.run_id}:{context.lease.attempt + 1}:{context.lease.generation + 1}:revive",
            next_input.canonical_bytes.decode(),
            due,
        )
        return StepResult(
            result_json(reason=reason, evidence=evidence, settlement=settlement, auto_revive=True),
            successors=(successor,),
            restart_lane=RestartLane(precise),
        )

    async def park(
        self, context: StepContext[Effects], work: AwaitLane, reason: str, evidence: object
    ) -> StepResult:
        settlement = await self.finish(context, work)
        return StepResult(
            result_json(reason=reason, evidence=evidence, settlement=settlement), park=Park(reason)
        )
