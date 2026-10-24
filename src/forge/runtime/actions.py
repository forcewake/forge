"""Initial implementation and approval actions consume trusted command identity."""

from collections.abc import Mapping
from uuid import UUID, uuid5

from forge.command_values import CommandEnvelope, CommandLease, CommandResult
from forge.engine.commands import Action, refusal
from forge.engine.approve import Approve
from forge.engine.plan import Implement, task_binding
from forge.engine.plan_values import PlanIntent
from forge.port_values import NeighborSnapshot, PlanningSnapshot
from forge.domain.states import State
from forge.ports import Clock
from forge.runtime.routing import Registry
from forge.runtime.settings import RuntimeSettings
from forge.store.admission import AdmissionStore
from forge.store.approval import ApprovalStore
from forge.store.approval_values import ApprovalClaim
from forge.store.command_runs import CommandRuns
from forge.store.records import RunRecord, RunRequest, StoreConflict, StaleLease
from forge.store.runs import RunStore
from forge.store.steps import StepStore
from forge.store.transactions import SessionFactory


class InitialActions:
    def __init__(
        self, config: RuntimeSettings, ports: Registry, factory: SessionFactory, clock: Clock
    ) -> None:
        self.config, self.ports, self.clock = config, ports, clock
        self.factory, self.runs, self.approvals = factory, RunStore(factory), ApprovalStore(factory)
        self.commands = CommandRuns(factory)

    @property
    def actions(self) -> Mapping[str, Action]:
        return {"implement": self.implement, "go": self.approve}

    async def implement(
        self, lease: CommandLease, envelope: CommandEnvelope, run: RunRecord | None
    ) -> CommandResult:
        if envelope.target.kind != "issue" or envelope.command.argument.strip():
            return refusal("malformed_command", run)
        identity = envelope.target.repo.identity
        entry = self.ports.for_repo(envelope.target.repo)
        name = next(
            key for key, value in self.ports.entries.items() if value.ref.identity == identity
        )
        connection, repository = self.config.repositories()[name]
        run_id = uuid5(UUID("83c8c7e2-54f2-46a0-8c99-a6471d94448c"), envelope.source_key).hex
        prior = await self.runs.load(run_id)
        if prior is not None:
            return CommandResult(status="accepted", reason="implementation_queued", run_id=prior.id)
        task = await self.ports.get_task(envelope.target)
        original = None
        if envelope.issue_action == "edited":
            if run is None or not (
                run.state == State.AWAITING_APPROVAL
                or (run.state == State.BLOCKED and run.reason == "approval_task_changed")
            ):
                return CommandResult(
                    status="observed",
                    reason="issue_no_longer_pending",
                    run_id=run.id if run else None,
                )
            initial = await StepStore(self.factory).get(f"{run.id}:plan")
            if initial is None:
                raise StoreConflict("original admission is missing")
            original = PlanIntent.model_validate_json(initial.payload_json)
            if task_binding(original.task) == task_binding(task):
                return CommandResult(
                    status="observed", reason="issue_text_unchanged", run_id=run.id
                )
        policy = repository.policy if original is None else original.policy
        head = await self.ports.head(entry.ref, policy.target_branch)
        if head is None:
            return refusal("base_not_found")
        neighbors = []
        for allowed in () if original is not None else repository.neighbors:
            other_connection, other = self.config.repositories()[allowed.repository]
            ref = self.ports.named(allowed.repository).ref
            sha = await self.ports.head(ref, other.policy.target_branch)
            if sha is None:
                return refusal("neighbor_snapshot_unavailable")
            neighbors.append(
                NeighborSnapshot(repo=ref, sha=sha, allowed_globs=allowed.allowed_globs)
            )
        intent = PlanIntent(
            actor=envelope.actor,
            task=task,
            snapshot=PlanningSnapshot(
                primary=NeighborSnapshot(repo=entry.ref, sha=head),
                neighbors=original.snapshot.neighbors if original is not None else tuple(neighbors),
            ),
            policy=policy,
            max_active=self.config.max_active if original is None else original.max_active,
            gate_ttl_seconds=3600 if original is None else original.gate_ttl_seconds,
        )
        request = RunRequest(
            run_id,
            connection.provider,
            connection.key,
            repository.native_id,
            "issue",
            str(envelope.target.number),
        )
        try:
            created = await Implement(AdmissionStore(self.factory), entry.actor, self.clock)(
                request,
                intent,
                command_lease=lease,
                supersede_run_id=run.id if envelope.issue_action == "edited" and run else None,
            )
        except StaleLease:
            raise
        except StoreConflict:
            return refusal("another_run_active", run)
        return CommandResult(status="accepted", reason="implementation_queued", run_id=created.id)

    async def approve(
        self, lease: CommandLease, envelope: CommandEnvelope, run: RunRecord | None
    ) -> CommandResult:
        if run is None:
            return refusal("run_not_found")
        frozen = await self.approvals.load(run.id)
        if frozen is None:
            return refusal("gate_not_open", run)
        approved = frozen.approved
        claim = ApprovalClaim(
            gate_id=frozen.gate_id,
            actor=envelope.actor,
            **approved.model_dump(exclude={"brief_text"}),
        )
        await Approve(StepStore(self.factory), self.clock)(
            run.id, claim, command_key=lease.command_id, command_lease=lease
        )
        return CommandResult(status="accepted", reason="approval_queued", run_id=run.id)
