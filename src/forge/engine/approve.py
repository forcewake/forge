"""Authorize current actors and delegate consume-once approval to the step transaction."""

from forge.domain.states import Handler, State
from forge.command_values import CommandLease
from forge.engine.effects import Effects
from forge.engine.approval_comment import displayed_plan
from forge.engine.approval_round import original_round_task
from forge.engine.plan import result_json, run_binding, subject_binding, task_binding
from forge.engine.plan_values import PlanIntent
from forge.engine.runner import Park, Retryable, StepContext, StepResult
from forge.port_values import ReadUnavailable
from forge.ports import ActorPort, Clock, RepoPort, SubjectPort, ReviewPort
from typing import Protocol
from forge.store.approval import ApprovalStore
from forge.store.approval_values import ApprovalClaim, ConsumeGate, PreparedPlan
from forge.store.records import RunRecord, StepRecord, StepRequest, StoreConflict
from forge.store.runs import RunStore
from forge.store.rounds import RoundStore
from forge.store.steps import StepStore


class ApprovalSources(RepoPort, SubjectPort, ReviewPort, Protocol):
    pass


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
        repositories: ApprovalSources,
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
        problem = await self.task_current(context, run, intent, claim)
        if problem is not None:
            return problem
        try:
            head = await self.repositories.head(intent.task.subject.repo, frozen.spec.target_branch)
        except ReadUnavailable:
            raise Retryable("approval_base_unavailable") from None
        if not await self.comment_current(run.id, frozen):
            return StepResult(
                result_json(refused="plan_comment_changed"),
                park=Park("approval_plan_comment_changed"),
            )
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

    async def comment_current(self, run_id: str, frozen: PreparedPlan) -> bool:
        comment = await self.approvals.comment_ref(run_id)
        if comment is None:
            return False
        try:
            return await displayed_plan(self.repositories, comment, frozen)
        except ReadUnavailable:
            raise Retryable("approval_plan_comment_unavailable") from None

    async def task_current(
        self,
        context: StepContext[Effects],
        run: RunRecord,
        intent: PlanIntent,
        claim: ApprovalClaim,
    ) -> StepResult | None:
        try:
            original_task = (
                intent.task
                if run.root_run_id == run.id
                else await original_round_task(
                    RoundStore(self.approvals.factory),
                    run,
                    context.lease.step_id,
                    claim,
                    self.repositories,
                    self.repositories,
                    self.actors,
                    self.clock,
                )
            )
            task = await self.repositories.get_task(original_task.subject)
        except ReadUnavailable:
            raise Retryable("approval_task_unavailable") from None
        except StoreConflict:
            return StepResult(
                result_json(refused="revision_changed"), park=Park("approval_revision_changed")
            )
        if task_binding(task) != task_binding(original_task):
            return StepResult(
                result_json(refused="task_changed"),
                park=Park("approval_task_changed"),
            )
        return None
