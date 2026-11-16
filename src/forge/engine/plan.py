"""Freeze the actual plan, prove the displayed input and atomically open approval."""

import json
from collections.abc import Callable

from forge.domain.states import Handler, State
from forge.command_values import CommandLease
from forge.domain.run_spec import RunPolicy
from forge.engine.effects import Effects
from forge.engine.plan_values import PlanIntent
from forge.engine.runner import Park, Retryable, StepContext, StepResult
from forge.port_values import Applied, SubjectRef, TaskSnapshot
from forge.domain.verdicts import Unknown
from forge.ports import ActorPort, Clock, Planner
from forge.planning.limits import ModelTransportError, PlanningRefused
from forge.store.admission import AdmissionStore
from forge.store.approval import ApprovalStore
from forge.store.approval_values import OpenGate
from forge.store.records import Lease, RunRecord, RunRequest, StepRequest, StoreConflict
from forge.store.runs import RunStore


def result_json(**values: object) -> str:
    return json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def subject_binding(subject: SubjectRef) -> tuple[str, str, str, str, str]:
    identity = subject.repo.identity
    return (
        identity.provider,
        identity.connection_id,
        identity.repository_id,
        subject.kind,
        str(subject.number),
    )


def task_binding(task: TaskSnapshot) -> tuple[object, ...]:
    return subject_binding(task.subject), task.title, task.description


def run_binding(run: RunRequest | RunRecord) -> tuple[str, str, str, str, str]:
    return run.provider, run.connection_id, run.repository_id, run.subject_kind, run.subject_id


class Implement:
    def __init__(self, admission: AdmissionStore, actors: ActorPort, clock: Clock) -> None:
        self.admission, self.actors, self.clock = admission, actors, clock

    async def __call__(
        self,
        request: RunRequest,
        intent: PlanIntent,
        *,
        command_lease: CommandLease | None = None,
        supersede_run_id: str | None = None,
    ) -> RunRecord:
        subject = intent.task.subject
        if run_binding(request) != subject_binding(subject):
            raise StoreConflict("admission subject scope mismatch")
        if intent.actor.is_bot or not await self.actors.is_approver(intent.actor, subject):
            raise StoreConflict("actor is not an approver")
        return await self.admission.admit(
            request,
            intent.canonical_bytes.decode(),
            max_active=intent.max_active,
            now=self.clock.now(),
            command_lease=command_lease,
            supersede_run_id=supersede_run_id,
        )


class PlanHandler:
    def __init__(
        self,
        runs: RunStore,
        approvals: ApprovalStore,
        planner: Callable[[Callable[[], Lease], RunPolicy], Planner],
        actors: ActorPort,
        clock: Clock,
    ) -> None:
        self.runs, self.approvals, self.planner = runs, approvals, planner
        self.actors, self.clock = actors, clock

    async def __call__(self, context: StepContext[Effects]) -> StepResult:
        intent = PlanIntent.model_validate_json(context.payload_json)
        run = await self.runs.load(context.lease.run_id)
        if run is None:
            raise StoreConflict("run missing")
        if run_binding(run) != subject_binding(intent.task.subject):
            raise StoreConflict("frozen plan subject scope mismatch")
        if intent.actor.is_bot or not await self.actors.is_approver(
            intent.actor, intent.task.subject
        ):
            return StepResult(
                result_json(refused="actor_not_authorized"), park=Park("actor_not_authorized")
            )
        if run.state == State.QUEUED:
            successor = StepRequest(
                run.id,
                Handler.PLAN,
                f"{run.id}:{run.generation}:plan-execute",
                context.payload_json,
                self.clock.now(),
            )
            return StepResult(
                result_json(planning=True), successors=(successor,), next_state=State.PLANNING
            )
        if run.state != State.PLANNING:
            return StepResult(result_json(refused="not_planning"))
        frozen = await self.approvals.load(run.id)
        if frozen is None:
            try:
                planner = self.planner(lambda: context.lease, intent.policy)
                plan = await planner.plan(intent.task, intent.snapshot)
            except PlanningRefused as error:
                return StepResult(result_json(refused=error.reason), park=Park(error.reason))
            except ModelTransportError:
                raise Retryable("planner_model_unavailable") from None
            except TimeoutError:
                raise Retryable("planner_unavailable") from None
            spec = intent.freeze(plan)
            frozen = await self.approvals.prepare(
                context.lease, spec, ttl_seconds=intent.gate_ttl_seconds, now=self.clock.now()
            )
        elif frozen.spec != intent.freeze(frozen.spec.plan):
            raise StoreConflict("frozen planning input changed")
        effect_key = f"{run.id}:plan:comment"
        outcome = await context.effects.post_comment(
            context.lease, effect_key, intent.task.subject, frozen.comment, frozen.marker
        )
        if not isinstance(outcome, Applied):
            reason = "effect_unknown" if isinstance(outcome, Unknown) else "plan_comment_refused"
            return StepResult(result_json(refused=reason), park=Park(reason))
        return StepResult(
            result_json(gate_id=frozen.gate_id, approved_digest=frozen.approved.approved_digest),
            next_state=State.AWAITING_APPROVAL,
            approval=OpenGate(frozen.gate_id, effect_key),
        )
