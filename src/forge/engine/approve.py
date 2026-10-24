"""Authorize current actors and delegate consume-once approval to the step transaction."""

from forge.domain.states import Handler, State
from forge.command_values import CommandLease
from forge.engine.effects import Effects
from forge.engine.plan import result_json, run_binding, subject_binding
from forge.engine.plan_values import PlanIntent
from forge.engine.runner import Retryable, StepContext, StepResult
from forge.port_values import ReadUnavailable
from forge.ports import ActorPort, Clock, RepoPort
from forge.store.approval import ApprovalStore
from forge.store.approval_values import ApprovalClaim, ConsumeGate
from forge.store.records import StepRecord, StepRequest, StoreConflict
from forge.store.runs import RunStore
from forge.store.steps import StepStore


class Approve:
    def __init__(self, steps: StepStore, clock: Clock) -> None:
        self.steps, self.clock = steps, clock

    async def __call__(
        self,
        run_id: str,
        claim: ApprovalClaim,
        *,
        command_key: str,
        command_lease: CommandLease | None = None,
    ) -> StepRecord:
        key = f"{run_id}:approve:{command_key}"
        payload = claim.canonical_bytes.decode()
        previous = await self.steps.get(key)
        if previous is not None:
            if previous.run_id != run_id or previous.payload_json != payload:
                raise StoreConflict("approval command identity changed")
            return previous
        try:
            return await self.steps.enqueue(
                StepRequest(run_id, Handler.APPROVE, key, payload, self.clock.now()),
                command_lease=command_lease,
                now=self.clock.now(),
            )
        except StoreConflict:
            previous = await self.steps.get(key)
            if (
                previous is not None
                and previous.run_id == run_id
                and previous.payload_json == payload
            ):
                return previous
            raise


class ApproveHandler:
    def __init__(
        self,
        runs: RunStore,
        steps: StepStore,
        approvals: ApprovalStore,
        actors: ActorPort,
        repositories: RepoPort,
        clock: Clock,
    ) -> None:
        self.runs, self.steps, self.approvals = runs, steps, approvals
        self.actors, self.repositories, self.clock = actors, repositories, clock

    async def __call__(self, context: StepContext[Effects]) -> StepResult:
        claim = ApprovalClaim.model_validate_json(context.payload_json)
        run = await self.runs.load(context.lease.run_id)
        initial = await self.steps.get(f"{context.lease.run_id}:plan")
        frozen = await self.approvals.load(context.lease.run_id)
        if run is None or initial is None or frozen is None:
            raise StoreConflict("approval input missing")
        intent = PlanIntent.model_validate_json(initial.payload_json)
        if run_binding(run) != subject_binding(intent.task.subject):
            raise StoreConflict("frozen approval subject scope mismatch")
        if claim.actor.is_bot or not await self.actors.is_approver(
            claim.actor, intent.task.subject
        ):
            return StepResult(result_json(refused="actor_not_authorized"))
        if run.state != State.AWAITING_APPROVAL:
            return StepResult(result_json(refused="gate_not_open"))
        try:
            head = await self.repositories.head(intent.task.subject.repo, frozen.spec.target_branch)
        except ReadUnavailable:
            raise Retryable("approval_base_unavailable") from None
        successor = StepRequest(
            run.id,
            Handler.DISPATCH,
            f"{run.id}:{run.generation}:dispatch:{frozen.approved.revision}",
            result_json(
                approved_digest=frozen.approved.approved_digest, revision=frozen.approved.revision
            ),
            self.clock.now(),
        )
        return StepResult(
            result_json(approved_digest=frozen.approved.approved_digest),
            successors=(successor,),
            next_state=State.RUNNING,
            approval=ConsumeGate(claim, head),
        )
